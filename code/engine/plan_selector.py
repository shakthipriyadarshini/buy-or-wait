"""
Candidates now come from three places, matching the real dataset:
  - request_payment_options.csv rows with payment_method == 'full_payment'
  - request_payment_options.csv rows with payment_method == 'installments'
  - partial_payment, built per the spec's fixed two-payment formula
    (not present in the options catalog — spec says so explicitly)

Each candidate is simulated for FULL 90-day safety. If unsafe, we attempt a
spending-change rescue (spending_changes.find_rescue) before discarding it.
'wait' is only offered when nothing above is safe today but the full amount
becomes safe later.
"""
from __future__ import annotations
from datetime import date
from .models import Profile, Request, FinancialEvent, PaymentOption, Plan
from .forecast import plan_is_safe
from .spending_changes import find_rescue


def _installment_payments(opt: PaymentOption) -> list[tuple[date, float]]:
    from datetime import timedelta
    step = opt.payment_frequency_days or 0
    return [(opt.first_payment_date + timedelta(days=step * i), opt.payment_amount)
            for i in range(opt.number_of_payments)]


def build_candidates(
    profile: Profile,
    req: Request,
    user_events: list[FinancialEvent],
    options: list[PaymentOption],
    safe_today: float,
    earliest_full: date | None,
) -> list[Plan]:
    candidates: list[Plan] = []
    methods_ok = profile.payment_methods_user_will_consider

    def try_add(method: str, payments: list[tuple[date, float]], payment_option_id: str | None):
        if payments[-1][0] > req.desired_completion_date:
            return  # rule 1 of the tie-break is a HARD gate for these two methods: must complete by deadline
        if plan_is_safe(profile, user_events, req.request_date, payments):
            candidates.append(Plan(method, payments, payment_option_id=payment_option_id))
            return
        rescue = find_rescue(profile, user_events, req.request_date, payments)
        if rescue is not None:
            candidates.append(Plan(method, payments, payment_option_id=payment_option_id, spending_changes=rescue))

    if "full_payment" in methods_ok:
        for opt in options:
            if opt.payment_method == "full_payment":
                try_add("full_payment", [(opt.first_payment_date, opt.payment_amount)], opt.payment_option_id)

    if "installments" in methods_ok:
        for opt in options:
            if opt.payment_method == "installments":
                if profile.max_installment_months is not None:
                    span_days = (opt.payment_frequency_days or 30) * (opt.number_of_payments - 1)
                    if span_days > profile.max_installment_months * 30:
                        continue
                try_add("installments", _installment_payments(opt), opt.payment_option_id)

    if (
        req.allows_partial_payment
        and "partial_payment" in methods_ok
        and 0 < safe_today < req.requested_amount
        and earliest_full is not None
        and earliest_full <= req.desired_completion_date
    ):
        remainder = round(req.requested_amount - safe_today, 2)
        payments = [(req.request_date, safe_today), (earliest_full, remainder)]
        # partial payment's own two-step schedule was already derived FROM the
        # safety check (safe_today, earliest_full), so it's safe by construction —
        # still verify, since the remainder payment interacts with everything else
        if plan_is_safe(profile, user_events, req.request_date, payments):
            candidates.append(Plan("partial_payment", payments))

    if "full_payment" in methods_ok and earliest_full is not None and earliest_full > req.request_date:
        candidates.append(Plan("wait", [(earliest_full, req.requested_amount)]))

    return candidates


def rank(candidates: list[Plan], req: Request) -> Plan | None:
    if not candidates:
        return None

    def sort_key(p: Plan):
        try:
            opt_num = int(p.payment_option_id.split("_")[-1]) if p.payment_option_id else 10**9
        except ValueError:
            opt_num = 10**9  # non-numeric / synthetic option id (e.g. ad-hoc API requests) — sorts last
        on_time = p.payments[-1][0] <= req.desired_completion_date
        return (
            0 if on_time else 1,
            0 if not p.spending_changes else 1,
            round(p.total_paid(), 2),
            p.payments[0][0],
            len(p.payments),
            opt_num,
        )

    return sorted(candidates, key=sort_key)[0]
