"""
Deterministic 90-day simulator. Recurrence projection now lives in
recurrence.py (it needed the raw historical detail); this module just walks
a day array given an already-concrete list of signed SimEvents plus any
candidate plan payments layered on top.
"""
from __future__ import annotations
from datetime import date, timedelta
from .models import Profile, FinancialEvent, SimEvent
from .recurrence import detect_and_project

HORIZON_DAYS = 90


def daily_deltas(sim_events: list[SimEvent], start: date, horizon_days: int = HORIZON_DAYS) -> dict[date, float]:
    end = start + timedelta(days=horizon_days)
    out: dict[date, float] = {}
    for se in sim_events:
        if start <= se.event_date <= end:
            out[se.event_date] = out.get(se.event_date, 0.0) + se.amount
    return out


def simulate_balance(
    opening_balance: float,
    deltas: dict[date, float],
    start: date,
    horizon_days: int = HORIZON_DAYS,
    extra_payments: list[tuple[date, float]] | None = None,
) -> dict[date, float]:
    balance = opening_balance
    out: dict[date, float] = {}
    extra: dict[date, float] = {}
    for d, amt in (extra_payments or []):
        extra[d] = extra.get(d, 0.0) - amt
    for i in range(horizon_days + 1):
        d = start + timedelta(days=i)
        balance += deltas.get(d, 0.0) + extra.get(d, 0.0)
        out[d] = balance
    return out


def min_balance(balances: dict[date, float]) -> float:
    return min(balances.values())


def amount_safe_to_pay(
    profile: Profile,
    user_events: list[FinancialEvent],
    request_date: date,
    requested_amount: float,
) -> float:
    sims = detect_and_project(user_events, request_date, HORIZON_DAYS)
    deltas = daily_deltas(sims, request_date)

    def safe(pay: float) -> bool:
        bal = simulate_balance(profile.current_available_balance, deltas, request_date,
                                extra_payments=[(request_date, pay)])
        return min_balance(bal) >= profile.minimum_balance_to_keep

    if not safe(0.0):
        return 0.0
    if safe(requested_amount):
        return round(requested_amount, 2)
    lo, hi = 0.0, requested_amount
    for _ in range(40):
        mid = (lo + hi) / 2
        if safe(mid):
            lo = mid
        else:
            hi = mid
    return round(lo, 2)


def earliest_date_for_full_payment(
    profile: Profile,
    user_events: list[FinancialEvent],
    request_date: date,
    requested_amount: float,
    horizon_days: int = HORIZON_DAYS,
) -> date | None:
    sims = detect_and_project(user_events, request_date, horizon_days)
    deltas = daily_deltas(sims, request_date, horizon_days)
    for i in range(horizon_days + 1):
        d = request_date + timedelta(days=i)
        bal = simulate_balance(profile.current_available_balance, deltas, request_date,
                                horizon_days=horizon_days, extra_payments=[(d, requested_amount)])
        if min_balance(bal) >= profile.minimum_balance_to_keep:
            return d
    return None


def plan_is_safe(
    profile: Profile,
    user_events: list[FinancialEvent],
    request_date: date,
    payments: list[tuple[date, float]],
    horizon_days: int = HORIZON_DAYS,
) -> bool:
    sims = detect_and_project(user_events, request_date, horizon_days)
    deltas = daily_deltas(sims, request_date, horizon_days)
    bal = simulate_balance(profile.current_available_balance, deltas, request_date,
                            horizon_days=horizon_days, extra_payments=payments)
    return min_balance(bal) >= profile.minimum_balance_to_keep
