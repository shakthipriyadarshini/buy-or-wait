"""
Data models matching the ACTUAL dataset headers (verified against the
uploaded dataset.zip), not the guessed schema from problem_statement.md
prose.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from datetime import date
from typing import Optional, Literal


@dataclass
class Profile:
    user_id: str
    home_currency: str
    current_available_balance: float
    minimum_balance_to_keep: float
    financial_priorities: list[str]
    expense_categories_to_protect: set[str]
    expense_categories_user_is_willing_to_reduce: set[str]
    expense_categories_user_is_willing_to_stop: set[str]
    payment_methods_user_will_consider: set[str]
    max_installment_months: Optional[int]


Direction = Literal["debit", "credit", "non_cash"]
EventStatus = Literal["settled", "pending", "scheduled", "cancelled", "failed", "unrealized"]
Flexibility = Literal["fixed", "stoppable", "reducible", "reducible_or_stoppable"]


@dataclass
class FinancialEvent:
    event_id: str
    user_id: str
    event_type: str          # expense|income|subscription|debt_payment|refund|investment_purchase|investment_sale|investment_valuation
    description: str
    category: str
    direction: Direction
    amount: float             # unsigned, in original `currency`
    currency: str
    event_date: date
    settlement_date: Optional[date]
    status: EventStatus
    linked_event_id: Optional[str]
    flexibility: Flexibility
    minimum_allowed_amount: Optional[float]

    # derived fields, filled in by conflict_resolution.py — NOT from the CSV
    is_essential: bool = False
    is_flexible_stoppable: bool = False
    is_flexible_reducible: bool = False
    superseded: bool = False   # true if a later event in its linked_event_id chain replaces it


@dataclass
class SimEvent:
    """A single concrete cash-flow instance ready for the forecast engine —
    output of recurrence.py, after projecting recurring series and passing
    through one-off events. Amount is already SIGNED (debit=negative)."""
    event_date: date
    amount: float
    source_event_id: str
    category: str
    is_flexible_stoppable: bool
    is_flexible_reducible: bool
    minimum_allowed_amount: Optional[float]


@dataclass
class PaymentOption:
    payment_option_id: str
    request_id: str
    payment_method: Literal["full_payment", "installments"]
    payment_amount: float
    number_of_payments: int
    first_payment_date: date
    payment_frequency_days: Optional[int]
    financing_fee: float
    total_payable_amount: float


@dataclass
class Request:
    request_id: str
    user_id: str
    request_date: date
    request_type: str
    requested_amount: float
    desired_completion_date: date
    allows_partial_payment: bool
    request_text: str


@dataclass
class Plan:
    method: Literal["full_payment", "partial_payment", "installments", "wait", "not_recommended"]
    payments: list[tuple[date, float]]
    payment_option_id: Optional[str] = None
    spending_changes: list[str] = field(default_factory=list)

    def total_paid(self) -> float:
        return sum(amt for _, amt in self.payments)


@dataclass
class Decision:
    request_id: str
    amount_safe_to_pay: float
    affordability_status: str
    recommended_payment_method: str
    payment_plan: str
    earliest_date_for_full_payment: str
    spending_changes_needed: str
    decision_explanation: str
