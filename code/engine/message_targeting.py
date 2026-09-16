"""
NEW module. ~35% of messages.csv rows have neither related_event_id nor
request_id populated — they're background updates (e.g. a payroll change
from an 'employer' source) that must be matched to the right existing
event by content, not by ID. This does lightweight keyword matching to
find a small candidate set, then hands the CHOICE to the LLM extractor
(engine.extract) rather than guessing definitively in Python — Python
narrows, the model with actual language understanding decides, and the
result still goes through the same validated schema either way.

Keep the keyword map generic (category names, not per-user specifics) so
this doesn't become file-specific hardcoding.
"""
from __future__ import annotations
from .models import FinancialEvent

_CATEGORY_KEYWORDS = {
    "salary": ["salary", "payroll", "gaji", "wage", "paycheck"],
    "rent": ["rent", "sewa", "lease", "landlord"],
    "utilities": ["utility", "utilities", "electricity", "water bill"],
    "debt_repayment": ["loan", "debt", "repayment", "installment due", "emi"],
    "subscription": ["subscription", "membership", "renewal"],
    "insurance": ["insurance", "premium", "policy"],
    "investment": ["investment", "portfolio", "contribution", "dividend"],
}


def guess_category(message_text: str) -> str | None:
    lower = message_text.lower()
    for category, keywords in _CATEGORY_KEYWORDS.items():
        if any(kw in lower for kw in keywords):
            return category
    return None


def candidate_events_for_message(
    message_text: str, user_id: str, events_by_user: dict[str, list[FinancialEvent]], max_candidates: int = 3,
) -> list[FinancialEvent]:
    category = guess_category(message_text)
    user_events = events_by_user.get(user_id, [])
    if category:
        pool = [e for e in user_events if e.category == category]
    else:
        pool = user_events
    # most recent first — a message almost always refers to the latest
    # known instance of a series, not an old settled one
    pool = sorted(pool, key=lambda e: e.event_date, reverse=True)
    return pool[:max_candidates]
