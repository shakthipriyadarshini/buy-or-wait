"""
Every amount must be normalized into the user's home_currency BEFORE it
touches the forecast engine. Doing conversion inline inside the simulator
is how you get silent off-by-currency bugs that still "run".
"""
from __future__ import annotations
from datetime import date


class RateTable:
    """rows: date, from_currency, to_currency, rate (amount_in_to = amount_in_from * rate)"""

    def __init__(self, rows: list[dict]):
        self._table: dict[tuple[str, str, date], float] = {}
        for r in rows:
            key = (r["from_currency"], r["to_currency"], r["rate_date"])
            self._table[key] = float(r["rate"])

    def convert(self, amount: float, from_ccy: str, to_ccy: str, on: date) -> float:
        if from_ccy == to_ccy:
            return amount
        key = (from_ccy, to_ccy, on)
        if key in self._table:
            return amount * self._table[key]
        inv_key = (to_ccy, from_ccy, on)
        if inv_key in self._table:
            return amount / self._table[inv_key]
        # Fall back to nearest earlier dated rate for that pair rather than
        # invent a rate. Adjust column names to match the actual CSV once
        # you've inspected it.
        candidates = [
            (d, rate) for (f, t, d), rate in self._table.items()
            if f == from_ccy and t == to_ccy and d <= on
        ]
        if candidates:
            candidates.sort(key=lambda x: x[0])
            return amount * candidates[-1][1]
        raise ValueError(f"No exchange rate for {from_ccy}->{to_ccy} on/before {on}")
