"""Bank statement CSV parsing. Amounts are integer cents, debits negative."""

from __future__ import annotations

import csv
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from pathlib import Path

REQUIRED_COLUMNS = ("posted_date", "description", "amount", "reference")


@dataclass(frozen=True)
class BankLine:
    reference: str
    posted_date: date
    description: str
    amount_cents: int


def parse_statement(path: Path) -> list[BankLine]:
    """Read a statement CSV in file order; raise ValueError naming the bad line."""
    with path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        missing = [name for name in REQUIRED_COLUMNS if name not in (reader.fieldnames or [])]
        if missing:
            raise ValueError(f"{path.name}: missing column(s) {', '.join(missing)}")
        return [_parse_row(row, reader.line_num) for row in reader]


def _parse_row(row: dict[str, str | None], line_number: int) -> BankLine:
    values = {name: (row.get(name) or "").strip() for name in REQUIRED_COLUMNS}
    empty = [name for name, value in values.items() if not value]
    if empty:
        raise ValueError(f"line {line_number}: missing {', '.join(empty)}")
    try:
        posted = date.fromisoformat(values["posted_date"])
    except ValueError as exc:
        raise ValueError(f"line {line_number}: bad posted_date {values['posted_date']!r}") from exc
    return BankLine(
        reference=values["reference"],
        posted_date=posted,
        description=values["description"],
        amount_cents=_to_cents(values["amount"], line_number),
    )


def _to_cents(text: str, line_number: int) -> int:
    try:
        cents = Decimal(text) * 100
    except InvalidOperation as exc:
        raise ValueError(f"line {line_number}: bad amount {text!r}") from exc
    if not cents.is_finite() or cents != cents.to_integral_value():
        raise ValueError(f"line {line_number}: amount {text!r} is not a whole number of cents")
    return int(cents)
