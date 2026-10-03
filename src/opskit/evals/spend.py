"""The spend guard shared by the record-mode evals: stop before the budget is passed."""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal


class BudgetExceeded(RuntimeError):
    pass


@dataclass
class Spend:
    """Running cost from each call's token usage and the configured prices. Calls run one at
    a time in record mode, so the most it can pass `limit` by is one call."""

    limit: Decimal
    total: Decimal = Decimal(0)

    def check(self) -> None:
        if self.total >= self.limit:
            raise BudgetExceeded(f"stopped at ${self.total} of a ${self.limit} budget")
