"""
NEW module — this was missing from the v1 skeleton entirely. When a
candidate plan (full_payment / installments) fails the 90-day safety check
on its own, the spec allows rescuing it with up to 3 stop/reduce changes to
FLEXIBLE recurring expenses. This does a small bounded search rather than a
hand-written heuristic per case, so it generalizes across users instead of
overfitting to sample rows.
"""
from __future__ import annotations
from datetime import date
from itertools import combinations
from .models import Profile, FinancialEvent, Plan
from .recurrence import detect_and_project
from .forecast import daily_deltas, simulate_balance, min_balance, HORIZON_DAYS

MAX_CHANGES = 3
MAX_CANDIDATE_POOL = 8  # bound combinatorics: C(8,3) = 56 evaluations, cheap


def _candidate_series(user_events: list[FinancialEvent], request_date: date, horizon_days: int) -> list[dict]:
    """One entry per distinct flexible recurring source_event_id, with the
    total amount it contributes inside the horizon (stop-savings) and the
    per-instance floor (reduce-savings)."""
    sims = detect_and_project(user_events, request_date, horizon_days)
    by_source: dict[str, list] = {}
    for se in sims:
        if se.is_flexible_stoppable or se.is_flexible_reducible:
            by_source.setdefault(se.source_event_id, []).append(se)

    out = []
    for src_id, instances in by_source.items():
        total_amt = sum(-i.amount for i in instances)  # amount is negative (debit)
        floor = instances[0].minimum_allowed_amount
        stoppable = instances[0].is_flexible_stoppable
        reducible = instances[0].is_flexible_reducible and floor is not None
        stop_savings = total_amt if stoppable else 0.0
        reduce_savings = sum(max(0.0, (-i.amount) - floor) for i in instances) if reducible else 0.0
        out.append(dict(source_event_id=src_id, stop_savings=stop_savings,
                         reduce_savings=reduce_savings, floor=floor,
                         stoppable=stoppable, reducible=reducible))
    return out


def _apply_change(sims, change: dict, use_stop: bool):
    """Returns a NEW deltas-compatible list of SimEvents with the change applied."""
    out = []
    for se in sims:
        if se.source_event_id != change["source_event_id"]:
            out.append(se)
            continue
        if use_stop:
            continue  # dropped entirely
        # reduce_to: the instance's new amount becomes the floor value
        se.amount = -(change["floor"] or 0.0)
        out.append(se)
    return out


def find_rescue(
    profile: Profile,
    user_events: list[FinancialEvent],
    request_date: date,
    payments: list[tuple[date, float]],
    horizon_days: int = HORIZON_DAYS,
) -> list[str] | None:
    """Returns formatted spending_changes_needed strings (<=3) that make the
    plan safe, or None if no combination within the search bound works."""
    pool = _candidate_series(user_events, request_date, horizon_days)
    # rank by best available savings (stop if stoppable else reduce), descending
    pool.sort(key=lambda c: max(c["stop_savings"], c["reduce_savings"]), reverse=True)
    pool = pool[:MAX_CANDIDATE_POOL]

    def build_options(c: dict) -> list[tuple[dict, bool, str]]:
        opts = []
        if c["stoppable"]:
            opts.append((c, True, f"stop:{c['source_event_id']}"))
        if c["reducible"]:
            opts.append((c, False, f"reduce_to:{c['source_event_id']}:{c['floor']:g}"))
        return opts

    all_options = [opt for c in pool for opt in build_options(c)]

    for size in range(1, MAX_CHANGES + 1):
        for combo in combinations(all_options, size):
            # stop and reduce on the SAME event are mutually exclusive —
            # combinations() over per-series options already can't pick both
            # for one series since each series contributes independent tuples,
            # but guard anyway:
            src_ids = [c["source_event_id"] for c, _, _ in combo]
            if len(set(src_ids)) != len(src_ids):
                continue
            sims = detect_and_project(user_events, request_date, horizon_days)
            for change, use_stop, _label in combo:
                sims = _apply_change(sims, change, use_stop)
            deltas = daily_deltas(sims, request_date, horizon_days)
            bal = simulate_balance(profile.current_available_balance, deltas, request_date,
                                    horizon_days=horizon_days, extra_payments=payments)
            if min_balance(bal) >= profile.minimum_balance_to_keep:
                return [label for _, _, label in combo]
    return None
