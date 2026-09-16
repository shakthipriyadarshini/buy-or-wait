"""
NEW module. Your dataset has no explicit recurrence field — recurring
expenses/income must be statistically detected from repeated historical
transactions, then projected forward into the forecast horizon.

Approach:
  1. Resolve each event to its terminal state by walking linked_event_id
     chains (e.g. authorization -> reversal; purchase -> valuation).
  2. Group SETTLED events by (user_id, category, description) — description
     is necessary because one category (e.g. "groceries", "transport") can
     contain several genuinely distinct merchants/series with different
     cadences and amounts. Grouping by category alone would blend them.
  3. Within a group, sort by date and look at consecutive gaps. If gaps are
     consistent (low relative spread) treat it as a recurring series and
     project forward from the LAST occurrence using the median gap and the
     MOST RECENT amount (not the average — a raise or a message-driven
     amendment should propagate forward, not get smoothed away).
  4. A group with too few points or inconsistent gaps is NOT treated as a
     predictable recurring series — but it's not dropped to zero either.
     Its historical spend is pooled with other non-periodic groups in the
     same category and converted into a smoothed average daily rate,
     injected as weekly synthetic entries. Confirmed by diffing against
     sample_requests.csv: dropping irregular-but-real spending (e.g.
     dining, transport — different merchants each time, never a clean
     schedule) entirely was making the forecast systematically too
     optimistic. These synthetic entries are never offered as a
     spending_changes_needed target (see MIN_POINTS_FOR_SMOOTHING below) —
     they're a pooled estimate, not one named stoppable/reducible bill.
  5. status == 'scheduled' rows (e.g. "Next confirmed salary") are always
     kept as an explicit one-off future instance regardless of recurrence
     detection — the dataset is handing you a known future fact directly.
  6. status == 'pending' rows: keep debits (a pending expense is still a
     real forward risk), drop credits (spec: "ignore pending credits").
  7. status in {cancelled, failed, unrealized} or direction == 'non_cash':
     always dropped.
"""
from __future__ import annotations
from dataclasses import dataclass
from datetime import date, timedelta
from calendar import monthrange
from statistics import median
from .models import FinancialEvent, SimEvent

MIN_POINTS_FOR_RECURRENCE = 3
MAX_RELATIVE_GAP_SPREAD = 0.35  # (max_gap - min_gap) / median_gap tolerance

# For spending that never forms a clean periodic pattern (different
# restaurants, different transport methods) but clearly still happens —
# e.g. dining, transport — treating it as "never happens again" is what
# was making amount_safe_to_pay systematically too optimistic (confirmed
# by diffing against sample_requests.csv: our forecast was higher than
# gold's in ~90% of mismatched rows). Below this many leftover points, or
# below this many days of observed spread, there's too little signal to
# trust an average rate, so it's left at zero rather than guessed.
MIN_POINTS_FOR_SMOOTHING = 2
MIN_SPAN_DAYS_FOR_SMOOTHING = 14
SMOOTHING_STEP_DAYS = 7  # weekly synthetic entries — frequent enough to
                          # affect a 90-day walk's minimum-balance day
                          # without generating a SimEvent per calendar day


def _add_months(d: date, n: int) -> date:
    """Add n calendar months, preserving day-of-month (clamped to the
    target month's length). Using a fixed +30/+31 day step for monthly
    series drifts the projected date away from the real recurring day
    (e.g. 'salary on the 15th') over a 90-day window — this is what a
    fixed-day-count projection gets systematically wrong."""
    month_index = d.month - 1 + n
    year = d.year + month_index // 12
    month = month_index % 12 + 1
    day = min(d.day, monthrange(year, month)[1])
    return date(year, month, day)


def resolve_chains(events: list[FinancialEvent]) -> list[FinancialEvent]:
    """Walk linked_event_id chains; mark every non-terminal event in a chain
    as superseded. 'Terminal' = no other event points back at it via
    linked_event_id."""
    by_id = {e.event_id: e for e in events}
    pointed_to = {e.linked_event_id for e in events if e.linked_event_id}
    for e in events:
        if e.event_id in pointed_to:
            e.superseded = True
    return events


def _signed(ev: FinancialEvent) -> float:
    if ev.direction == "debit":
        return -ev.amount
    if ev.direction == "credit":
        return ev.amount
    return 0.0  # non_cash — never reaches the simulator anyway


def _usable(ev: FinancialEvent) -> bool:
    if ev.direction == "non_cash":
        return False
    if ev.status in ("cancelled", "failed", "unrealized", "unresolved"):
        return False  # "unresolved" = a receipt whose amount could not be
                      # read; excluded rather than modelled at a guessed value
    if ev.superseded:
        return False
    if ev.status == "pending" and ev.direction == "credit":
        return False  # ignore pending credits
    return True


def _test_periodicity(sorted_group: list[FinancialEvent]) -> tuple[bool, float]:
    """Shared test used at both the per-(category,description) tier and the
    pooled per-category tier. Returns (is_periodic, median_gap_days)."""
    if len(sorted_group) < MIN_POINTS_FOR_RECURRENCE:
        return False, 0.0
    gaps = [(sorted_group[i + 1].event_date - sorted_group[i].event_date).days
            for i in range(len(sorted_group) - 1)]
    gaps = [g for g in gaps if g > 0]
    if not gaps:
        return False, 0.0
    med_gap = median(gaps)
    if med_gap <= 0:
        return False, 0.0
    spread = (max(gaps) - min(gaps)) / med_gap
    return spread <= MAX_RELATIVE_GAP_SPREAD, med_gap


def _project_periodic(last: FinancialEvent, med_gap: float, horizon_start: date, horizon_end: date, to_sim, signed_amount: float) -> list[SimEvent]:
    """Projects forward from `last`'s date/phase using `signed_amount` as the
    recurring value. The caller decides what that value should be — tier 1
    (a single named bill) uses the literal last observed amount, since a
    message-driven amendment (a raise, a plan change) targets one specific
    event_id and should propagate forward exactly; tier 2 (several
    merchants pooled under one category) uses a recent average instead,
    since a single grocery run's price is noise, not a "current rate"."""
    out = []
    is_monthly = 27 <= med_gap <= 32
    if is_monthly:
        n = 1
        d = _add_months(last.event_date, n)
        while d <= horizon_end:
            if d >= horizon_start:
                out.append(to_sim(last, d, signed_amount))
            n += 1
            d = _add_months(last.event_date, n)
    else:
        step = round(med_gap)
        d = last.event_date + timedelta(days=step)
        while d <= horizon_end:
            if d >= horizon_start:
                out.append(to_sim(last, d, signed_amount))
            d += timedelta(days=step)
    return out


def detect_and_project(
    events: list[FinancialEvent],
    horizon_start: date,
    horizon_days: int = 90,
) -> list[SimEvent]:
    """Returns concrete, signed, dated SimEvents covering
    [horizon_start, horizon_start+horizon_days].

    Three tiers, in order, per category:
      1. Per-(category, description) periodicity — a single named bill
         (rent, one specific subscription).
      2. Pooled per-category periodicity, across whatever's left after
         tier 1 — catches a real pattern that rotates between several
         merchants for the same need (e.g. groceries at 7 different
         stores, but always roughly every 10 days) that no single
         merchant's history alone would ever reach 3 points for.
      3. Smoothed average daily rate for whatever's still left after both
         tiers — genuinely irregular spending with no clean cadence at
         either grain, which still represents real forward risk and
         shouldn't be dropped to zero (confirmed by diffing against
         sample_requests.csv: doing so made the forecast systematically
         too optimistic).
    """
    horizon_end = horizon_start + timedelta(days=horizon_days)
    usable = [e for e in events if _usable(e)]

    out: list[SimEvent] = []

    def to_sim(ev: FinancialEvent, d: date, amount: float) -> SimEvent:
        return SimEvent(
            event_date=d, amount=amount, source_event_id=ev.event_id,
            category=ev.category,
            is_flexible_stoppable=ev.is_flexible_stoppable,
            is_flexible_reducible=ev.is_flexible_reducible,
            minimum_allowed_amount=ev.minimum_allowed_amount,
        )

    # --- explicit one-offs already inside the window: scheduled, or
    #     pending-debit, or settled-but-dated-in-window (rare, but don't drop) ---
    explicit_statuses = {"scheduled", "pending"}
    for ev in usable:
        if horizon_start <= ev.event_date <= horizon_end and (
            ev.status in explicit_statuses or ev.status == "settled"
        ):
            out.append(to_sim(ev, ev.event_date, _signed(ev)))

    # --- TIER 1: per-(category, description) periodicity ---
    settled_history = [e for e in usable if e.status == "settled" and e.event_date < horizon_start]
    groups: dict[tuple[str, str, str], list[FinancialEvent]] = {}
    for ev in settled_history:
        groups.setdefault((ev.user_id, ev.category, ev.description), []).append(ev)

    remaining_by_category: dict[tuple[str, str], list[FinancialEvent]] = {}

    for (user_id, category, _description), group in groups.items():
        group.sort(key=lambda e: e.event_date)
        is_periodic, med_gap = _test_periodicity(group)
        if is_periodic:
            # tier 1: a single named bill — use the LITERAL last observed
            # amount, so a message-driven amendment (a raise, a plan
            # change) that already updated it propagates forward exactly
            out.extend(_project_periodic(group[-1], med_gap, horizon_start, horizon_end, to_sim, _signed(group[-1])))
        else:
            remaining_by_category.setdefault((user_id, category), []).extend(group)

    # --- TIER 2: pooled per-category periodicity on whatever tier 1 missed ---
    leftover_by_category: dict[tuple[str, str], list[FinancialEvent]] = {}

    for (user_id, category), pooled in remaining_by_category.items():
        pooled.sort(key=lambda e: e.event_date)
        is_periodic, med_gap = _test_periodicity(pooled)
        if is_periodic:
            # Tried averaging the last few occurrences here first, reasoning
            # that a single grocery run's price is noisier than one named
            # bill's amount. Measured aggregate amount_safe_to_pay error
            # across sample_requests.csv both ways: averaging helped two
            # individual rows but made the aggregate MAE worse (164.6k ->
            # 177.1k) by moving other rows further from gold. Reverted to
            # the same rule as tier 1 (literal last value) on the strength
            # of that measurement, not the initial intuition.
            out.extend(_project_periodic(pooled[-1], med_gap, horizon_start, horizon_end, to_sim, _signed(pooled[-1])))
        else:
            debit_only = [e for e in pooled if e.direction == "debit"]
            # Credits (income) are deliberately excluded from smoothing, not
            # just missed by accident — the observed bias (this fix exists to
            # correct) was the forecast being too OPTIMISTIC from missing
            # expenses. Smoothing irregular INCOME in as a cushion would push
            # the same bug in the wrong direction. A safety-margin system
            # should only ever add assumed caution from uncertain patterns,
            # never add assumed extra safety from them.
            if debit_only:
                leftover_by_category[(user_id, category)] = debit_only

    # --- TIER 3: smoothed average for whatever's still genuinely irregular ---
    for (user_id, category), leftover in leftover_by_category.items():
        if len(leftover) < MIN_POINTS_FOR_SMOOTHING:
            continue
        leftover.sort(key=lambda e: e.event_date)
        span_days = (leftover[-1].event_date - leftover[0].event_date).days
        if span_days < MIN_SPAN_DAYS_FOR_SMOOTHING:
            continue
        total_spend = sum(e.amount for e in leftover)  # all debits in this category, unsigned
        daily_rate = total_spend / span_days
        d = horizon_start + timedelta(days=SMOOTHING_STEP_DAYS)
        step_amount = -daily_rate * SMOOTHING_STEP_DAYS  # negative: debit
        while d <= horizon_end:
            out.append(SimEvent(
                event_date=d, amount=step_amount,
                source_event_id=f"avg:{user_id}:{category}",  # synthetic id — deliberately
                                                                 # won't match any real event_id,
                                                                 # so it's never offered as a
                                                                 # spending_changes_needed target;
                                                                 # it's an aggregate estimate, not
                                                                 # one stoppable/reducible commitment
                category=category,
                is_flexible_stoppable=False,
                is_flexible_reducible=False,
                minimum_allowed_amount=None,
            ))
            d += timedelta(days=SMOOTHING_STEP_DAYS)

    return out
