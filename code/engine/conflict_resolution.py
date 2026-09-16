"""
Three responsibilities, matched to the real dataset:

1. Apply LLM-extracted message/image signals onto specific events
   (cancel / amend_amount / amend_date / delay / confirm), following the
   spec's precedence: explicit cancellation/settlement/amendment > newer
   record from the same source > settled over estimate > safer interpretation.

2. Resolve linked_event_id chains to terminal state (delegated to
   recurrence.resolve_chains, called from here so callers only need one
   entry point).

3. Classify is_essential / is_flexible_stoppable / is_flexible_reducible
   per event, combining the event's own `flexibility` tag with the user's
   profile-level category consent. The profile is authoritative for
   PERMISSION (what this user allows), the event tag is authoritative for
   MECHANISM (what kind of change is structurally possible). A category the
   profile marks 'to_protect' is never touched even if the individual event
   row says 'stoppable' — protect always wins (the safer interpretation).
"""
from __future__ import annotations
from .models import FinancialEvent, Profile
from .recurrence import resolve_chains


def classify_flexibility(ev: FinancialEvent, profile: Profile) -> FinancialEvent:
    protected = ev.category in profile.expense_categories_to_protect
    can_reduce = ev.category in profile.expense_categories_user_is_willing_to_reduce
    can_stop = ev.category in profile.expense_categories_user_is_willing_to_stop

    if protected:
        ev.is_essential = True
        ev.is_flexible_stoppable = False
        ev.is_flexible_reducible = False
        return ev

    ev.is_essential = ev.flexibility == "fixed"
    mech_stop = ev.flexibility in ("stoppable", "reducible_or_stoppable")
    mech_reduce = ev.flexibility in ("reducible", "reducible_or_stoppable")

    ev.is_flexible_stoppable = mech_stop and can_stop
    ev.is_flexible_reducible = mech_reduce and can_reduce and ev.minimum_allowed_amount is not None
    return ev


def apply_extracted_signals(events: list[FinancialEvent], signals: list[dict]) -> list[FinancialEvent]:
    by_id = {e.event_id: e for e in events}
    for sig in signals:
        target = by_id.get(sig["event_id"])
        if target is None:
            continue
        action = sig["action"]
        rank = sig["origin_rank"]
        if action == "cancel":
            target.status = "cancelled"           # rule 1: explicit cancellation always wins
        elif action == "amend_amount" and rank >= sig.get("target_rank", 0):
            target.amount = sig["value"]
            if target.status not in ("settled", "cancelled"):
                target.status = "scheduled"
        elif action in ("amend_date", "delay") and rank >= sig.get("target_rank", 0):
            target.event_date = sig["value"]
        elif action == "confirm":
            if target.status == "pending":
                target.status = "scheduled"        # rule 3: settled/confirmed beats an estimate
    return list(by_id.values())


def resolve_and_classify(events: list[FinancialEvent], profiles: dict[str, Profile]) -> list[FinancialEvent]:
    events = resolve_chains(events)
    for ev in events:
        classify_flexibility(ev, profiles[ev.user_id])
    return events
