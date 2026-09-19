from __future__ import annotations
from .models import Decision
from .extract import METER, _provider_model


def _fmt(amount: float, currency: str) -> str:
    if amount % 1:
        return f"{currency} {amount:,.2f}"
    return f"{currency} {amount:,.0f}"


def deterministic_explanation(decision: Decision, facts: dict) -> str:
    """Builds the explanation from the already-computed numbers, with no
    model involved.

    This exists so the system produces a correct, readable
    decision_explanation with no API key, no quota, and no response cache —
    which is what makes it safe to keep the LLM cache OUT of version
    control. It is also the fallback whenever a live call is unavailable or
    returns something that isn't usable prose.

    Everything here is a restatement of values the deterministic engine
    already computed; nothing is derived or recalculated.
    """
    currency = facts.get("currency", "")
    status = decision.affordability_status
    method = decision.recommended_payment_method
    safe = decision.amount_safe_to_pay
    minimum = facts.get("minimum_balance_to_keep")
    earliest = decision.earliest_date_for_full_payment
    requested = facts.get("requested_amount")

    # A request can land on "not_affordable" for two different reasons:
    #   (a) the balance genuinely can't support it within 90 days, or
    #   (b) the full amount is already safe to pay today, but none of the
    #       user's configured payment_methods_user_will_consider produced an
    #       eligible plan (e.g. full_payment isn't accepted, and partial/
    #       installment options don't apply once the full amount is safe).
    # These are financial-affordability vs payment-method-eligibility, and
    # must not be described with the same "isn't affordable" wording.
    safe_but_no_eligible_method = (
        status == "not_affordable" and requested is not None and safe >= requested
    )

    if status == "affordable_now":
        body = f"Pay {_fmt(safe, currency)} today."
    elif status == "affordable_with_plan" and method == "installments":
        n = len(decision.payment_plan.split("|")) if decision.payment_plan != "none" else 0
        body = f"Spread this over {n} scheduled payments rather than paying in full today."
    elif status == "affordable_with_plan" and method == "partial_payment":
        body = f"Pay {_fmt(safe, currency)} now and the remainder on {earliest}."
    elif status == "affordable_later":
        body = (
            f"Waiting until {earliest} is safer than paying now"
            if earliest else "Waiting is safer than paying now"
        ) + f"; only {_fmt(safe, currency)} is safe to pay today."
    elif safe_but_no_eligible_method:
        body = (
            f"{_fmt(safe, currency)} is financially safe to pay today, but no eligible "
            f"payment method matches your current payment preferences."
        )
    else:  # genuinely not_affordable
        body = (
            f"This isn't affordable within the next 90 days without dropping below your "
            f"{_fmt(minimum, currency)} minimum balance" if minimum is not None else
            "This isn't affordable within the next 90 days without dropping below your minimum balance"
        ) + f"; at most {_fmt(safe, currency)} is safe to pay today."

    # Only claim the minimum is preserved where the recommendation actually
    # preserves it. Saying "this keeps your minimum intact" on a
    # not_affordable / affordable_later outcome contradicts the sentence it
    # follows — those cases fold the minimum into their own wording above.
    min_clause = ""
    if minimum is not None and status in ("affordable_now", "affordable_with_plan"):
        min_clause = f" This keeps your {_fmt(minimum, currency)} minimum balance intact over the next 90 days."

    changes = decision.spending_changes_needed
    change_clause = ""
    if changes and changes != "none":
        n_changes = len(changes.split("|"))
        change_clause = f" Requires {n_changes} spending change{'s' if n_changes > 1 else ''}."

    return (body + min_clause + change_clause).strip()


def _looks_like_prose(text: str) -> bool:
    """Guards against the mock/no-op response (which is signal-schema JSON)
    or any other non-prose reply landing in decision_explanation, where it
    would be user-visible nonsense."""
    t = text.strip()
    if not t or len(t) < 15:
        return False
    if t.startswith("{") or t.startswith("["):
        return False
    return True


def generate_explanation(decision: Decision, facts: dict, call_llm) -> str:
    """
    facts is a small dict of the already-computed numbers (balance, min
    balance, key events considered, etc). The model is told these are FINAL
    and must not alter or recompute them — it is writing a caption, not
    doing arithmetic.

    Falls back to deterministic_explanation() whenever the model isn't
    actually reachable (no key, exhausted quota, timeout) or returns
    something that isn't prose, so this function always yields a sensible
    sentence.
    """
    import llm_client

    system = (
        "Write a 1-2 sentence explanation of a financial decision that has "
        "ALREADY been made. Do not recalculate anything. Do not introduce "
        "any number not present in the facts provided. Be specific and concrete. "
        "If facts['safe_today'] is already greater than or equal to "
        "facts['requested_amount'] but the decision was not 'affordable_now', "
        "that means the amount is financially safe but no eligible payment "
        "method matched the user's preferences — say that plainly, and do not "
        "imply the balance is insufficient."
    )
    user = (
        f"decision: {decision.affordability_status} / {decision.recommended_payment_method}\n"
        f"amount_safe_to_pay: {decision.amount_safe_to_pay}\n"
        f"facts: {facts}"
    )
    raw, in_tok, out_tok = call_llm(system, user)
    provider, model = _provider_model()
    METER.log(provider, model, "explanation", in_tok, out_tok)

    source = getattr(llm_client, "LAST_CALL_SOURCE", "mock")
    if source == "mock" or not _looks_like_prose(raw):
        return deterministic_explanation(decision, facts)
    return raw.strip()
