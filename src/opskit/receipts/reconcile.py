"""Deterministic receipts-versus-bank reconciliation (plan section 3, rules 1-7).

Pure functions over integer cents and dates taken from the inputs: no model calls,
no clock, no randomness.
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from typing import Literal

from pydantic import BaseModel

from opskit.receipts.bank import BankLine

Status = Literal[
    "matched",
    "amount_mismatch",
    "date_drift",
    "missing_in_bank",
    "duplicate_receipt",
    "duplicate_charge",
    "unreceipted_charge",
    "out_of_scope",
    "needs_review",
]
STATUSES: tuple[Status, ...] = (
    "matched",
    "amount_mismatch",
    "date_drift",
    "missing_in_bank",
    "duplicate_receipt",
    "duplicate_charge",
    "unreceipted_charge",
    "out_of_scope",
    "needs_review",
)
UNFLAGGED: frozenset[Status] = frozenset({"matched", "out_of_scope"})

EARLIEST_POSTING_DAYS = -1
LATEST_POSTING_DAYS = 10
MAX_MATCHED_LAG_DAYS = 3
AMOUNT_TOLERANCE_PERCENT = 20
DUPLICATE_CHARGE_WINDOW_DAYS = 7
PREFIX_MIN_CHARS = 4

PROCESSOR_PREFIXES = re.compile(r"^(?:SQ ?\*|TST ?\*|PAYPAL ?\*|PP ?\*)\s*")
NOT_LETTERS = re.compile(r"[^A-Z]+")
CORPORATE_WORDS = frozenset({"LLC", "INC", "CO", "CORP", "LTD", "THE", "AND"})
OUT_OF_SCOPE_DESCRIPTOR = re.compile(r"\b(?:ACH CREDIT|TRANSFER|DEPOSIT)\b", re.IGNORECASE)


@dataclass(frozen=True)
class ReceiptFacts:
    file: str
    vendor: str | None
    receipt_date: date | None
    total_cents: int | None
    needs_review: bool = False


class ReconciliationRow(BaseModel):
    receipt: str | None = None
    bank_reference: str | None = None
    status: Status
    delta_cents: int | None = None
    delta_days: int | None = None
    vendor: str | None = None
    receipt_date: date | None = None
    receipt_total_cents: int | None = None
    bank_description: str | None = None
    posted_date: date | None = None
    bank_amount_cents: int | None = None
    match_reason: str = ""


class Reconciliation(BaseModel):
    rows: list[ReconciliationRow]
    summary: dict[str, int]
    needs_review: list[str]


@dataclass(frozen=True)
class _Candidate:
    receipt: ReceiptFacts
    line: BankLine
    line_index: int
    exact: bool
    similarity: float
    lag_days: int

    def sort_key(self) -> tuple[int, float, int, int, str, date, int]:
        # Exact amount first, then merchant similarity, then the smallest lag. Lag is
        # compared by absolute value so a posting the day before ranks like one the day after.
        # Remaining ties go to the first-posted line, then the earlier statement position;
        # never to the reference string, which says nothing about when a charge happened.
        return (
            0 if self.exact else 1,
            -self.similarity,
            abs(self.lag_days),
            self.lag_days,
            self.receipt.file,
            self.line.posted_date,
            self.line_index,
        )


def merchant_tokens(text: str) -> list[str]:
    """Normalise a vendor name or bank descriptor into comparable tokens."""
    upper = PROCESSOR_PREFIXES.sub("", text.upper().strip())
    words = NOT_LETTERS.sub(" ", upper).split()
    return [word for word in words if word not in CORPORATE_WORDS]


def _tokens_match(left: str, right: str) -> bool:
    if left == right:
        return True
    short, long = sorted((left, right), key=len)
    return len(short) >= PREFIX_MIN_CHARS and long.startswith(short)


def merchant_similarity(vendor: str | None, descriptor: str) -> float:
    """Matched tokens over the token count of the shorter side, 0.0 when either is empty.

    Prefixes of 4+ characters count, so a truncated descriptor still matches its vendor.
    """
    if not vendor:
        return 0.0
    left, right = merchant_tokens(vendor), merchant_tokens(descriptor)
    if not left or not right:
        return 0.0
    short, long = (left, right) if len(left) <= len(right) else (right, left)
    available = list(long)
    matched = 0
    for token in short:
        hit = next((i for i, other in enumerate(available) if _tokens_match(token, other)), None)
        if hit is not None:
            matched += 1
            del available[hit]
    return matched / len(short)


def is_out_of_scope(line: BankLine) -> bool:
    """Rule 1 (descriptor half): deposits and transfers never expect a receipt."""
    return OUT_OF_SCOPE_DESCRIPTOR.search(line.description) is not None


def _is_refund(receipt: ReceiptFacts) -> bool:
    return receipt.total_cents is not None and receipt.total_cents < 0


def _sign_agrees(receipt: ReceiptFacts, line: BankLine) -> bool:
    """Rule 1 (credit half): a credit is in scope only against a refund receipt."""
    return _is_refund(receipt) == (line.amount_cents > 0)


def _lag_days(receipt: ReceiptFacts, line: BankLine) -> int | None:
    if receipt.receipt_date is None:
        return None
    return (line.posted_date - receipt.receipt_date).days


def _candidate(receipt: ReceiptFacts, line: BankLine, line_index: int) -> _Candidate | None:
    """Rule 2: exact amount, or within 20 % with a merchant match, inside the posting window."""
    lag = _lag_days(receipt, line)
    if receipt.total_cents is None or lag is None or not _sign_agrees(receipt, line):
        return None
    if not EARLIEST_POSTING_DAYS <= lag <= LATEST_POSTING_DAYS:
        return None
    similarity = merchant_similarity(receipt.vendor, line.description)
    receipt_abs, bank_abs = abs(receipt.total_cents), abs(line.amount_cents)
    exact = receipt_abs == bank_abs
    close = abs(bank_abs - receipt_abs) * 100 <= AMOUNT_TOLERANCE_PERCENT * receipt_abs
    if not exact and not (close and similarity > 0):
        return None
    return _Candidate(receipt, line, line_index, exact, similarity, lag)


def _assign(
    receipts: Sequence[ReceiptFacts], lines: Sequence[tuple[int, BankLine]]
) -> list[_Candidate]:
    """Rule 3: greedy, best first, every receipt and bank line used at most once.

    Lines are identified by their index in the statement, never by reference, so a
    repeated reference cannot merge two charges. When candidates tie on everything
    that matters, the first-posted line wins the receipt (then the earlier statement
    position); later identical lines are left for rule 6.
    """
    candidates = [c for r in receipts for index, line in lines if (c := _candidate(r, line, index))]
    used_receipts: set[str] = set()
    used_lines: set[int] = set()
    chosen: list[_Candidate] = []
    for cand in sorted(candidates, key=_Candidate.sort_key):
        if cand.receipt.file in used_receipts or cand.line_index in used_lines:
            continue
        used_receipts.add(cand.receipt.file)
        used_lines.add(cand.line_index)
        chosen.append(cand)
    return chosen


def _receipt_key(receipt: ReceiptFacts) -> tuple[str, date, int] | None:
    if receipt.vendor is None or receipt.receipt_date is None or receipt.total_cents is None:
        return None
    return (" ".join(merchant_tokens(receipt.vendor)), receipt.receipt_date, receipt.total_cents)


def _split_duplicate_receipts(
    receipts: Sequence[ReceiptFacts],
) -> tuple[list[ReceiptFacts], set[str]]:
    """Rule 4: the first receipt (file order) with a vendor/date/total is the original.

    Decided before assignment so that both copies cannot compete for one charge.
    """
    seen: set[tuple[str, date, int]] = set()
    originals: list[ReceiptFacts] = []
    duplicates: set[str] = set()
    for receipt in receipts:
        key = _receipt_key(receipt)
        if key is not None and key in seen:
            duplicates.add(receipt.file)
            continue
        if key is not None:
            seen.add(key)
        originals.append(receipt)
    return originals, duplicates


def _pair_status(cand: _Candidate) -> Status:
    """Rule 5: amount difference beats date; lag of 0-3 days is matched, longer is drift."""
    if not cand.exact:
        return "amount_mismatch"
    if cand.lag_days <= MAX_MATCHED_LAG_DAYS:
        return "matched"
    return "date_drift"


def _describe_match(cand: _Candidate) -> str:
    assert cand.receipt.total_cents is not None  # candidates always have a total
    delta = abs(cand.line.amount_cents) - abs(cand.receipt.total_cents)
    amount = "exact amount" if cand.exact else f"amount off by {delta} cents"
    days = f"{cand.lag_days} day{'' if abs(cand.lag_days) == 1 else 's'} lag"
    return f"{amount}, {days}, merchant {cand.similarity:.2f}"


def _normalised_descriptor(line: BankLine) -> str:
    return " ".join(line.description.upper().split())


def _earlier_twin(line: BankLine, matched_lines: Sequence[BankLine]) -> BankLine | None:
    """Rule 6 (duplicate charge): same descriptor and amount as a matched line 0-7 days earlier.

    Evaluated after assignment against every matched line, so it does not depend on
    statement order or reference strings. Among identical charges the first-posted line
    wins the receipt (rule 3 tie-break), so later identical lines within 7 days
    (age = unused.posted - matched.posted, 0 <= age <= 7) are duplicates. A line posted
    before the matched line, or more than 7 days after it, is unreceipted. When several
    matched lines qualify, the earliest-posted one is named.
    """
    qualifying = [
        earlier
        for earlier in matched_lines
        if 0 <= (line.posted_date - earlier.posted_date).days <= DUPLICATE_CHARGE_WINDOW_DAYS
        and earlier.amount_cents == line.amount_cents
        and _normalised_descriptor(earlier) == _normalised_descriptor(line)
    ]
    return min(qualifying, key=lambda e: (e.posted_date, e.reference), default=None)


def _receipt_fields(receipt: ReceiptFacts) -> dict[str, object]:
    return {
        "receipt": receipt.file,
        "vendor": receipt.vendor,
        "receipt_date": receipt.receipt_date,
        "receipt_total_cents": receipt.total_cents,
    }


def _bank_fields(line: BankLine) -> dict[str, object]:
    return {
        "bank_reference": line.reference,
        "bank_description": line.description,
        "posted_date": line.posted_date,
        "bank_amount_cents": line.amount_cents,
    }


def _paired_row(cand: _Candidate) -> ReconciliationRow:
    assert cand.receipt.total_cents is not None
    return ReconciliationRow(
        **_receipt_fields(cand.receipt),
        **_bank_fields(cand.line),
        status=_pair_status(cand),
        delta_cents=abs(cand.line.amount_cents) - abs(cand.receipt.total_cents),
        delta_days=cand.lag_days,
        match_reason=_describe_match(cand),
    )


def _bank_only_row(line: BankLine, matched_lines: Sequence[BankLine]) -> ReconciliationRow:
    """Rules 1 and 6 for a bank line with no receipt."""
    fields = _bank_fields(line)
    if is_out_of_scope(line) or line.amount_cents > 0:
        return ReconciliationRow(**fields, status="out_of_scope", match_reason="credit or transfer")
    twin = _earlier_twin(line, matched_lines)
    if twin is not None:
        return ReconciliationRow(
            **fields,
            status="duplicate_charge",
            match_reason=f"same descriptor and amount as {twin.reference}",
        )
    return ReconciliationRow(
        **fields, status="unreceipted_charge", match_reason="no receipt for this charge"
    )


def _receipt_only_row(receipt: ReceiptFacts, status: Status, reason: str) -> ReconciliationRow:
    return ReconciliationRow(**_receipt_fields(receipt), status=status, match_reason=reason)


def _summarise(rows: Sequence[ReconciliationRow]) -> dict[str, int]:
    summary: dict[str, int] = dict.fromkeys(STATUSES, 0)
    for row in rows:
        summary[row.status] += 1
    summary["flagged"] = sum(1 for row in rows if row.status not in UNFLAGGED)
    return summary


def reconcile(receipts: Sequence[ReceiptFacts], bank: Sequence[BankLine]) -> Reconciliation:
    """Reconcile receipts against a statement. Output is independent of receipt input order."""
    ordered = sorted(receipts, key=lambda r: r.file)
    reviewing = [r for r in ordered if r.needs_review]
    candidates_in = [r for r in ordered if not r.needs_review]
    originals, duplicate_files = _split_duplicate_receipts(candidates_in)

    in_scope = [(i, line) for i, line in enumerate(bank) if not is_out_of_scope(line)]
    pairs = {c.line_index: c for c in _assign(originals, in_scope)}
    matched_receipts = {c.receipt.file for c in pairs.values()}
    matched_lines = [bank[index] for index in sorted(pairs)]

    rows: list[ReconciliationRow] = []
    for index, line in enumerate(bank):
        pair = pairs.get(index)
        rows.append(_paired_row(pair) if pair is not None else _bank_only_row(line, matched_lines))

    review_files = {r.file for r in reviewing}
    for receipt in ordered:
        if receipt.file in review_files:
            rows.append(_receipt_only_row(receipt, "needs_review", "extraction needs review"))
        elif receipt.file in duplicate_files:
            rows.append(
                _receipt_only_row(receipt, "duplicate_receipt", "same vendor, date and total")
            )
        elif receipt.file not in matched_receipts:
            rows.append(_receipt_only_row(receipt, "missing_in_bank", "no candidate charge"))

    return Reconciliation(
        rows=rows,
        summary=_summarise(rows),
        needs_review=[r.file for r in reviewing],
    )
