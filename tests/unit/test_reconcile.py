from __future__ import annotations

import itertools
import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from opskit.receipts.bank import BankLine, parse_statement
from opskit.receipts.reconcile import (
    ReceiptFacts,
    Reconciliation,
    ReconciliationRow,
    merchant_similarity,
    merchant_tokens,
    reconcile,
)

ROOT = Path(__file__).resolve().parents[2]
KEY_DIR = ROOT / "evals" / "answer_keys" / "receipts"
STATEMENT = ROOT / "samples" / "receipts" / "bank" / "statement-2026-08.csv"
DAY0 = date(2026, 8, 10)


def _facts_from_key() -> list[ReceiptFacts]:
    data = json.loads((KEY_DIR / "receipts.json").read_text())
    return [
        ReceiptFacts(
            file=r["file"],
            vendor=r["vendor"],
            receipt_date=date.fromisoformat(r["date"]),
            total_cents=r["total_cents"],
        )
        for r in data["receipts"]
    ]


def _answer_rows() -> list[dict[str, object]]:
    rows: list[dict[str, object]] = json.loads((KEY_DIR / "reconciliation.json").read_text())[
        "rows"
    ]
    return rows


def _key(row: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        row[name] for name in ("receipt", "bank_reference", "status", "delta_cents", "delta_days")
    )


def _receipt(
    file: str = "r1.png",
    vendor: str | None = "Harbor Bean Co",
    days: int = 0,
    cents: int | None = 1000,
    needs_review: bool = False,
) -> ReceiptFacts:
    return ReceiptFacts(file, vendor, DAY0 + timedelta(days=days), cents, needs_review)


def _line(
    ref: str = "STM-1",
    desc: str = "SQ *HARBOR BEAN CO",
    days: int = 0,
    cents: int = -1000,
) -> BankLine:
    return BankLine(ref, DAY0 + timedelta(days=days), desc, cents)


def _statuses(result: Reconciliation) -> list[str]:
    return [row.status for row in result.rows]


def test_answer_key_rows_match_exactly() -> None:
    result = reconcile(_facts_from_key(), parse_statement(STATEMENT))
    expected = _answer_rows()
    assert len(result.rows) == len(expected) == 43
    actual = [_key(row.model_dump()) for row in result.rows]
    assert actual == [_key(row) for row in expected]


def test_answer_key_precision_and_recall_are_perfect_per_status() -> None:
    result = reconcile(_facts_from_key(), parse_statement(STATEMENT))
    predicted = {(r.receipt, r.bank_reference, r.status) for r in result.rows}
    truth = {(r["receipt"], r["bank_reference"], r["status"]) for r in _answer_rows()}
    assert predicted == truth


def test_summary_counts_and_flagged() -> None:
    result = reconcile(_facts_from_key(), parse_statement(STATEMENT))
    assert sum(result.summary[s] for s in result.summary if s != "flagged") == 43
    non_flag = result.summary["matched"] + result.summary["out_of_scope"]
    assert result.summary["flagged"] == 43 - non_flag
    assert result.summary["needs_review"] == 0


class TestMerchantSimilarity:
    def test_tokens_strip_prefix_digits_punctuation_and_suffixes(self) -> None:
        assert merchant_tokens("SQ *HARBOR BEAN CO") == ["HARBOR", "BEAN"]
        assert merchant_tokens("COPPERLINE FUEL 0412") == ["COPPERLINE", "FUEL"]
        assert merchant_tokens("Cedar & Pine Hardware, LLC") == ["CEDAR", "PINE", "HARDWARE"]
        assert merchant_tokens("TST* SALTMARSH LUNCH") == ["SALTMARSH", "LUNCH"]
        assert merchant_tokens("LUMENFIELD*HOSTING") == ["LUMENFIELD", "HOSTING"]

    def test_truncated_descriptor_matches_by_prefix(self) -> None:
        score = merchant_similarity("Tallgrass Office Supply", "TALLGRASS OFFICE SUP")
        assert score == pytest.approx(2 / 3)
        assert merchant_similarity("Saltmarsh Lunch Counter", "TST* SALTMARSH LUNCH") == 1.0

    def test_short_tokens_do_not_prefix_match(self) -> None:
        assert merchant_similarity("Pebble Street Diner", "PEBBLE ST DINER") == pytest.approx(2 / 3)

    def test_unrelated_and_empty_score_zero(self) -> None:
        assert merchant_similarity("Ironbark Tool Rental", "BRIGHTWICK PRINT") == 0.0
        assert merchant_similarity(None, "ANYTHING") == 0.0
        assert merchant_similarity("Harbor Bean", "12345 #") == 0.0

    def test_symmetric_in_token_order(self) -> None:
        assert merchant_similarity("Bean Harbor", "HARBOR BEAN") == 1.0


class TestRules:
    @pytest.mark.parametrize(
        ("lag", "expected"), [(0, "matched"), (3, "matched"), (4, "date_drift")]
    )
    def test_lag_boundary(self, lag: int, expected: str) -> None:
        result = reconcile([_receipt()], [_line(days=lag)])
        assert result.rows[0].status == expected
        assert result.rows[0].delta_days == lag

    def test_amount_mismatch_beats_date_drift(self) -> None:
        result = reconcile([_receipt(cents=1000)], [_line(days=6, cents=-1100)])
        row = result.rows[0]
        assert row.status == "amount_mismatch"
        assert row.delta_cents == 100

    def test_amount_beyond_twenty_percent_is_not_a_candidate(self) -> None:
        result = reconcile([_receipt(cents=1000)], [_line(cents=-1300)])
        assert _statuses(result) == ["unreceipted_charge", "missing_in_bank"]

    def test_posting_before_window_is_not_a_candidate(self) -> None:
        result = reconcile([_receipt()], [_line(days=-2)])
        assert _statuses(result) == ["unreceipted_charge", "missing_in_bank"]

    def test_exact_amount_beats_better_merchant_with_other_amount(self) -> None:
        receipts = [_receipt(cents=1000)]
        bank = [
            _line("STM-1", "SQ *HARBOR BEAN CO", cents=-1050),
            _line("STM-2", "UNRELATED THING", days=2, cents=-1000),
        ]
        rows = reconcile(receipts, bank).rows
        assert rows[1].receipt == "r1.png"
        assert rows[0].status == "unreceipted_charge"

    def test_duplicate_receipt_labels_only_the_resubmission(self) -> None:
        receipts = [_receipt("a.png"), _receipt("b.png")]
        result = reconcile(receipts, [_line()])
        by_file = {r.receipt: r for r in result.rows if r.receipt}
        assert by_file["a.png"].status == "matched"
        assert by_file["a.png"].bank_reference == "STM-1"
        assert by_file["b.png"].status == "duplicate_receipt"
        assert by_file["b.png"].bank_reference is None

    def test_duplicate_charge_labels_only_the_repeat(self) -> None:
        bank = [_line("STM-1"), _line("STM-2", days=1)]
        rows = reconcile([_receipt()], bank).rows
        assert [r.status for r in rows] == ["matched", "duplicate_charge"]
        assert rows[1].delta_cents is None

    def test_repeat_charge_after_seven_days_is_unreceipted(self) -> None:
        bank = [_line("STM-1"), _line("STM-2", days=8)]
        assert _statuses(reconcile([_receipt()], bank)) == ["matched", "unreceipted_charge"]

    def test_same_amount_different_descriptor_is_not_duplicate_charge(self) -> None:
        bank = [_line("STM-1"), _line("STM-2", desc="OTHER SHOP", days=1)]
        assert _statuses(reconcile([_receipt()], bank)) == ["matched", "unreceipted_charge"]

    def test_numeric_reference_order_does_not_pick_the_original(self) -> None:
        bank = [_line("TX-9"), _line("TX-10")]
        rows = reconcile([_receipt()], bank).rows
        assert [(r.bank_reference, r.status) for r in rows] == [
            ("TX-9", "matched"),
            ("TX-10", "duplicate_charge"),
        ]

    def test_newest_first_statement_still_names_the_first_posted_as_original(self) -> None:
        bank = [_line("STM-2", days=2), _line("STM-1", days=0)]
        rows = reconcile([_receipt()], bank).rows
        assert [(r.bank_reference, r.status) for r in rows] == [
            ("STM-2", "duplicate_charge"),
            ("STM-1", "matched"),
        ]
        assert rows[0].match_reason == "same descriptor and amount as STM-1"

    def test_twin_exactly_seven_days_later_is_duplicate(self) -> None:
        bank = [_line("STM-1"), _line("STM-2", days=7)]
        assert _statuses(reconcile([_receipt()], bank)) == ["matched", "duplicate_charge"]

    def test_twin_eight_days_later_is_unreceipted_in_any_order(self) -> None:
        for bank in (
            [_line("STM-1"), _line("STM-2", days=8)],
            [_line("STM-2", days=8), _line("STM-1")],
        ):
            rows = reconcile([_receipt()], bank).rows
            assert {r.bank_reference: r.status for r in rows} == {
                "STM-1": "matched",
                "STM-2": "unreceipted_charge",
            }

    def test_first_posted_line_wins_and_twin_posted_before_a_better_match_is_unreceipted(
        self,
    ) -> None:
        # Both lines fit the receipt; day 0 has the smaller lag so it takes the receipt.
        # The line posted a day earlier is not 0-7 days after it, so it is not a duplicate.
        bank = [_line("STM-1", days=-1), _line("STM-2", days=0)]
        rows = reconcile([_receipt()], bank).rows
        assert [(r.bank_reference, r.status) for r in rows] == [
            ("STM-1", "unreceipted_charge"),
            ("STM-2", "matched"),
        ]

    def test_first_posted_wins_when_lags_tie(self) -> None:
        # Lag -1 and +1 rank equally by absolute value; the first-posted (-1) wins the receipt.
        bank = [_line("STM-2", days=1), _line("STM-1", days=-1)]
        rows = reconcile([_receipt()], bank).rows
        assert [(r.bank_reference, r.status) for r in rows] == [
            ("STM-2", "duplicate_charge"),
            ("STM-1", "matched"),
        ]

    def test_repeat_is_duplicate_of_a_matched_line_that_comes_later_in_the_file(self) -> None:
        bank = [_line("STM-3", days=3), _line("STM-1", days=0), _line("STM-2", days=1)]
        assert _statuses(reconcile([_receipt()], bank)) == [
            "duplicate_charge",
            "matched",
            "duplicate_charge",
        ]

    def test_repeated_reference_does_not_drop_a_charge(self) -> None:
        bank = [
            _line("X", desc="CORNER CAFE", cents=-1250),
            _line("X", desc="HARDWARE", cents=-999),
        ]
        receipts = [_receipt("cafe.png", "Corner Cafe", cents=1250)]
        rows = reconcile(receipts, bank).rows
        assert [(r.bank_description, r.status) for r in rows] == [
            ("CORNER CAFE", "matched"),
            ("HARDWARE", "unreceipted_charge"),
        ]

    def test_repeated_reference_with_two_receipts_matches_each_charge(self) -> None:
        bank = [
            _line("X", desc="CORNER CAFE", cents=-1250),
            _line("X", desc="HARDWARE", cents=-999),
        ]
        receipts = [
            _receipt("cafe.png", "Corner Cafe", cents=1250),
            _receipt("hw.png", "Hardware", cents=999),
        ]
        rows = reconcile(receipts, bank).rows
        assert [(r.bank_description, r.receipt, r.status) for r in rows] == [
            ("CORNER CAFE", "cafe.png", "matched"),
            ("HARDWARE", "hw.png", "matched"),
        ]

    @pytest.mark.parametrize(
        "description",
        ["ACH CREDIT ACME", "ONLINE TRANSFER TO SAV 4471", "MOBILE DEPOSIT", "TRANSFER FROM SAV"],
    )
    def test_descriptor_out_of_scope_even_when_receipt_matches_amount(
        self, description: str
    ) -> None:
        result = reconcile([_receipt()], [_line(desc=description)])
        assert result.rows[0].status == "out_of_scope"
        assert result.rows[1].status == "missing_in_bank"

    def test_credit_without_refund_receipt_is_out_of_scope(self) -> None:
        result = reconcile([], [_line(desc="INTEREST PAID", cents=312)])
        assert _statuses(result) == ["out_of_scope"]
        assert result.summary["flagged"] == 0

    def test_credit_matching_refund_receipt_is_matched(self) -> None:
        refund = _receipt(vendor="Tallgrass Office Supply", cents=-9570)
        credit = _line(desc="TALLGRASS OFFICE SUP", days=2, cents=9570)
        row = reconcile([refund], [credit]).rows[0]
        assert row.status == "matched"
        assert row.delta_cents == 0

    def test_refund_receipt_does_not_match_a_debit(self) -> None:
        refund = _receipt(cents=-1000)
        assert _statuses(reconcile([refund], [_line()])) == [
            "unreceipted_charge",
            "missing_in_bank",
        ]

    def test_needs_review_receipt_is_never_matched(self) -> None:
        result = reconcile([_receipt(needs_review=True)], [_line()])
        assert _statuses(result) == ["unreceipted_charge", "needs_review"]
        assert result.needs_review == ["r1.png"]
        assert result.summary["needs_review"] == 1
        assert result.summary["flagged"] == 2

    def test_needs_review_receipt_with_no_fields(self) -> None:
        blank = ReceiptFacts("x.png", None, None, None, needs_review=True)
        row = reconcile([blank], []).rows[0]
        assert row.status == "needs_review"
        assert row.vendor is None

    def test_receipt_missing_fields_is_missing_in_bank(self) -> None:
        partial = ReceiptFacts("x.png", "Harbor Bean Co", None, 1000)
        assert _statuses(reconcile([partial], [_line()])) == [
            "unreceipted_charge",
            "missing_in_bank",
        ]

    def test_each_bank_line_used_once(self) -> None:
        receipts = [
            _receipt("a.png", "Harbor Bean Co"),
            _receipt("b.png", "Harbor Bean Co", days=1),
        ]
        rows = reconcile(receipts, [_line()]).rows
        assert [r.status for r in rows] == ["matched", "missing_in_bank"]

    def test_match_reason_is_readable(self) -> None:
        row = reconcile([_receipt()], [_line(days=1)]).rows[0]
        assert row.match_reason == "exact amount, 1 day lag, merchant 1.00"


class TestBoundaries:
    def test_empty_inputs(self) -> None:
        result = reconcile([], [])
        assert result.rows == []
        assert result.needs_review == []
        assert result.summary["flagged"] == 0

    def test_receipts_without_statement(self) -> None:
        assert _statuses(reconcile([_receipt()], [])) == ["missing_in_bank"]

    def test_statement_without_receipts(self) -> None:
        assert _statuses(reconcile([], [_line()])) == ["unreceipted_charge"]

    def test_rows_serialise_to_json(self) -> None:
        result = reconcile([_receipt()], [_line()])
        dumped = result.model_dump(mode="json")
        assert dumped["rows"][0]["posted_date"] == "2026-08-10"
        json.dumps(dumped)


class TestDeterminism:
    def test_same_input_twice_is_identical(self) -> None:
        receipts, bank = _facts_from_key(), parse_statement(STATEMENT)
        assert reconcile(receipts, bank) == reconcile(receipts, bank)

    def test_receipt_order_does_not_matter(self) -> None:
        receipts, bank = _facts_from_key(), parse_statement(STATEMENT)
        baseline = reconcile(receipts, bank)
        for shuffled in (
            receipts[::-1],
            receipts[7:] + receipts[:7],
            receipts[1::2] + receipts[::2],
        ):
            assert reconcile(shuffled, bank) == baseline

    def test_bank_order_does_not_matter(self) -> None:
        receipts, bank = _facts_from_key(), parse_statement(STATEMENT)
        baseline = _without_references(reconcile(receipts, bank))
        for shuffled in (
            bank[::-1],
            bank[11:] + bank[:11],
            bank[1::2] + bank[::2],
            bank[2::3] + bank[1::3] + bank[::3],
        ):
            assert _without_references(reconcile(receipts, shuffled)) == baseline

    def test_bank_order_does_not_matter_for_repeated_charges(self) -> None:
        bank = [
            _line("TX-9", days=0),
            _line("TX-10", days=2),
            _line("TX-11", days=8),
            _line("TX-12", days=9, desc="OTHER SHOP"),
        ]
        baseline = _by_reference(reconcile([_receipt()], bank))
        assert {ref: row.status for ref, row in baseline.items()} == {
            "TX-9": "matched",
            "TX-10": "duplicate_charge",
            "TX-11": "unreceipted_charge",
            "TX-12": "unreceipted_charge",
        }
        for ordering in itertools.permutations(bank):
            assert _by_reference(reconcile([_receipt()], list(ordering))) == baseline

    def test_output_keeps_statement_order(self) -> None:
        bank = [_line("STM-2", days=2), _line("STM-1", days=0)]
        rows = reconcile([_receipt(), _receipt("b.png", "Other", cents=500)], bank).rows
        assert [r.bank_reference for r in rows[:2]] == ["STM-2", "STM-1"]
        assert rows[2].bank_reference is None and rows[2].receipt == "b.png"


def _without_references(result: Reconciliation) -> list[str]:
    """Rows minus reference-bearing fields, sorted.

    Same-day identical charges differ only by reference, so which one holds the receipt
    follows statement position; every other field and every status is order-free.
    """
    skip = {"bank_reference", "match_reason"}
    dumped = (row.model_dump(mode="json", exclude=skip) for row in result.rows)
    return sorted(json.dumps(row, sort_keys=True) for row in dumped)


def _by_reference(result: Reconciliation) -> dict[str | None, ReconciliationRow]:
    return {row.bank_reference or f"receipt:{row.receipt}": row for row in result.rows}


class TestParseStatement:
    def test_parses_committed_statement(self) -> None:
        lines = parse_statement(STATEMENT)
        assert len(lines) == 39
        assert lines[1] == BankLine("STM-0002", date(2026, 8, 3), "SQ *HARBOR BEAN CO", -1875)
        assert lines[0].amount_cents == 285000

    def _write(self, tmp_path: Path, body: str) -> Path:
        path = tmp_path / "s.csv"
        path.write_text("posted_date,description,amount,balance,reference\n" + body)
        return path

    def test_amounts_are_decimal_safe(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "2026-08-01,X,-0.29,0,R1\n2026-08-01,Y,1.10,0,R2\n")
        assert [line.amount_cents for line in parse_statement(path)] == [-29, 110]

    def test_missing_field_names_the_line(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "2026-08-01,X,-1.00,0,R1\n2026-08-02,,-2.00,0,R2\n")
        with pytest.raises(ValueError, match=r"line 3: missing description"):
            parse_statement(path)

    def test_bad_amount_names_the_line(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "2026-08-01,X,abc,0,R1\n")
        with pytest.raises(ValueError, match=r"line 2: bad amount"):
            parse_statement(path)

    def test_fractional_cents_rejected(self, tmp_path: Path) -> None:
        path = self._write(tmp_path, "2026-08-01,X,1.005,0,R1\n")
        with pytest.raises(ValueError, match="whole number of cents"):
            parse_statement(path)

    def test_duplicate_reference_names_the_line(self, tmp_path: Path) -> None:
        path = self._write(
            tmp_path,
            "2026-08-01,CORNER CAFE,-12.50,0,X\n2026-08-02,HARDWARE,-9.99,0,X\n",
        )
        with pytest.raises(ValueError, match=r"line 3: duplicate reference X"):
            parse_statement(path)

    def test_missing_column_rejected(self, tmp_path: Path) -> None:
        path = tmp_path / "s.csv"
        path.write_text("posted_date,description\n2026-08-01,X\n")
        with pytest.raises(ValueError, match="missing column"):
            parse_statement(path)
