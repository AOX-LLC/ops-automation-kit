"""Score receipt extraction and reconciliation against the answer key, with agent-core's
eval runner.

    uv run python -m opskit.evals.receipts                       # replay (default, free)
    AGENT_CORE_MODE=record uv run python -m opskit.evals.receipts --tier small

Replay reads recordings under fixtures/cassettes and never contacts a provider. Record
mode spends the caller's own key (AGENT_CORE_ANTHROPIC_API_KEY): calls run one at a time
and stop before the running cost passes --budget-usd.

Extraction: one EvalCase per receipt, one scorer per field. A printed tip or tax of zero
is not on the receipt, so null is the right answer there ("honest nulls"); a zero is
accepted for field accuracy but fails the honest-nulls scorer.
Reconciliation: precision and recall per flag, on the answer key's true receipt fields
(the matcher alone) and on the extracted fields (end to end).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

from aox_agent_core import AgentClient, Mode, RunContext, Tier, load_config
from aox_agent_core.evals import (
    EvalCase,
    EvalRunner,
    EvalSuite,
    Score,
    Scorecard,
    TargetOutput,
    render_scorecard_markdown,
    write_scorecard_json,
)
from pydantic import JsonValue

from opskit.evals.spend import Spend
from opskit.receipts.bank import parse_statement
from opskit.receipts.extraction import extract_receipt
from opskit.receipts.reconcile import ReceiptFacts, reconcile

REPO = Path(__file__).resolve().parents[3]
KEY = REPO / "evals" / "answer_keys" / "receipts"
SAMPLES = REPO / "samples" / "receipts"
FLAGS = (
    "matched",
    "amount_mismatch",
    "date_drift",
    "missing_in_bank",
    "duplicate_receipt",
    "duplicate_charge",
    "unreceipted_charge",
    "out_of_scope",
)


# --- extraction scoring ---------------------------------------------------------------


def _norm_text(value: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(value).lower()) if value is not None else ""


@dataclass(frozen=True)
class FieldScorer:
    """Exact comparison of one field after normalisation."""

    field: str
    expected_key: str
    unprinted_zero: bool = False  # the answer key's 0 means "no such line on the receipt"

    @property
    def name(self) -> str:
        return f"field:{self.field}"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        expected = case.expected[self.expected_key] if isinstance(case.expected, dict) else None
        got = output.get(self.field) if isinstance(output, dict) else None
        if self.unprinted_zero and expected == 0:
            passed = got in (None, 0)
        elif isinstance(expected, str):
            passed = _norm_text(got) == _norm_text(expected)
        else:
            passed = got == expected
        detail = None if passed else f"expected {expected!r}, got {got!r}"
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed, detail=detail)


@dataclass(frozen=True)
class HonestNulls:
    """A line that is not on the receipt must come back null, not a guessed zero."""

    name: str = "honest_nulls"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        expected = case.expected if isinstance(case.expected, dict) else {}
        got = output if isinstance(output, dict) else {}
        guessed = [
            field
            for field in ("tip_cents", "tax_cents")
            if expected.get(field) == 0 and got.get(field) is not None
        ]
        passed = not guessed
        detail = None if passed else f"guessed {', '.join(guessed)}"
        return Score(scorer=self.name, value=1.0 if passed else 0.0, passed=passed, detail=detail)


SCORERS: Sequence[Any] = (
    FieldScorer("vendor_name", "vendor"),
    FieldScorer("receipt_date", "date"),
    FieldScorer("subtotal_cents", "subtotal_cents"),
    FieldScorer("tax_cents", "tax_cents", unprinted_zero=True),
    FieldScorer("tip_cents", "tip_cents", unprinted_zero=True),
    FieldScorer("total_cents", "total_cents"),
    FieldScorer("card_last4", "card_last4"),
    HonestNulls(),
)


def answer_key() -> list[dict[str, Any]]:
    receipts: list[dict[str, Any]] = json.loads((KEY / "receipts.json").read_text())["receipts"]
    return sorted(receipts, key=lambda receipt: receipt["file"])


def extraction_suite() -> EvalSuite:
    cases = tuple(
        EvalCase(id=Path(r["file"]).stem, input={"file": r["file"]}, expected=r)
        for r in answer_key()
    )
    return EvalSuite(name="receipts-extraction", cases=cases)


def extraction_target(client: AgentClient, tier: Tier, spend: Spend) -> Callable[[EvalCase], Any]:
    async def target(case: EvalCase) -> TargetOutput:
        spend.check()
        assert isinstance(case.input, dict)
        file = str(case.input["file"])
        ctx = RunContext(run_id=f"eval-{case.id}", external_ids={"workflow": "receipts_eval"})
        outcome = await extract_receipt(
            client, ctx, SAMPLES / "inbox" / file, tier=tier, public_path=file
        )
        cost = Decimal(outcome.cost_usd)
        spend.total += cost
        fields = outcome.fields.model_dump(mode="json") if outcome.fields else {}
        output: dict[str, JsonValue] = {"status": outcome.status, **fields}
        return TargetOutput(output=output, cost_usd=cost)

    return target


def field_accuracy(card: Scorecard) -> dict[str, float]:
    names = [scorer.name for scorer in SCORERS]
    totals = dict.fromkeys(names, 0)
    for result in card.results:
        for score in result.scores:
            totals[score.scorer] += int(score.passed)
    count = len(card.results) or 1
    return {name: round(totals[name] / count, 4) for name in names}


# --- reconciliation scoring -----------------------------------------------------------


def _facts_from_key() -> list[ReceiptFacts]:
    return [
        ReceiptFacts(
            file=r["file"],
            vendor=r["vendor"],
            receipt_date=date.fromisoformat(r["date"]),
            total_cents=r["total_cents"],
        )
        for r in answer_key()
    ]


def _facts_from_outputs(card: Scorecard) -> list[ReceiptFacts]:
    facts = []
    for result in card.results:
        output = result.output if isinstance(result.output, dict) else {}
        when = output.get("receipt_date")
        facts.append(
            ReceiptFacts(
                file=f"{result.case_id}{_suffix(result.case_id)}",
                vendor=output.get("vendor_name"),  # type: ignore[arg-type]
                receipt_date=date.fromisoformat(when) if isinstance(when, str) else None,
                total_cents=output.get("total_cents"),  # type: ignore[arg-type]
                needs_review=output.get("status") != "extracted",
            )
        )
    return facts


def _suffix(stem: str) -> str:
    return next(Path(r["file"]).suffix for r in answer_key() if Path(r["file"]).stem == stem)


def precision_recall(facts: Sequence[ReceiptFacts]) -> dict[str, dict[str, float | int]]:
    bank = parse_statement(SAMPLES / "bank" / "statement-2026-08.csv")
    predicted = {(r.receipt, r.bank_reference, r.status) for r in reconcile(facts, bank).rows}
    expected = {
        (row["receipt"], row["bank_reference"], row["status"])
        for row in json.loads((KEY / "reconciliation.json").read_text())["rows"]
    }
    report: dict[str, dict[str, float | int]] = {}
    for flag in FLAGS:
        p = {item for item in predicted if item[2] == flag}
        e = {item for item in expected if item[2] == flag}
        hits = len(p & e)
        report[flag] = {
            "expected": len(e),
            "predicted": len(p),
            "precision": round(hits / len(p), 4) if p else (1.0 if not e else 0.0),
            "recall": round(hits / len(e), 4) if e else 1.0,
        }
    return report


# --- reporting ------------------------------------------------------------------------


def _markdown(
    card: Scorecard, accuracy: dict[str, float], recon: dict[str, Any], tier: Tier
) -> str:
    lines = [render_scorecard_markdown(card), "", f"## Field accuracy ({tier.value} tier)", ""]
    lines += ["| Scorer | Accuracy |", "| --- | --- |"]
    lines += [f"| {name} | {value:.0%} |" for name, value in accuracy.items()]
    for title, key in (
        ("Reconciliation on the answer key's true fields (the matcher alone)", "on_true_fields"),
        ("Reconciliation on the extracted fields (end to end)", "on_extracted_fields"),
    ):
        lines += ["", f"## {title}", "", "| Flag | Expected | Predicted | Precision | Recall |"]
        lines += ["| --- | --- | --- | --- | --- |"]
        for flag, row in recon[key].items():
            lines.append(
                f"| {flag} | {row['expected']} | {row['predicted']} | "
                f"{row['precision']:.0%} | {row['recall']:.0%} |"
            )
    return "\n".join(lines) + "\n"


async def run(tier: Tier, budget: Decimal) -> tuple[Scorecard, dict[str, Any], str]:
    config = load_config(REPO / "config" / "agent-core.toml")
    spend = Spend(limit=budget)
    concurrency = 1 if config.mode is not Mode.REPLAY else 4
    async with AgentClient(config) as client:
        runner = EvalRunner(SCORERS, concurrency=concurrency)
        card = await runner.run(
            extraction_suite(), extraction_target(client, tier, spend), mode=config.mode
        )
    accuracy = field_accuracy(card)
    recon = {
        "on_true_fields": precision_recall(_facts_from_key()),
        "on_extracted_fields": precision_recall(_facts_from_outputs(card)),
    }
    summary = {
        "suite": card.suite,
        "mode": config.mode.value,
        "tier": tier.value,
        "receipts": len(card.results),
        "errors": sum(1 for r in card.results if r.error),
        "field_accuracy": accuracy,
        "mean_field_accuracy": round(
            sum(v for k, v in accuracy.items() if k.startswith("field:")) / 7, 4
        ),
        "cost_total_usd": str(card.cost_total_usd),
        "cost_per_receipt_usd": str(card.cost_per_case_usd),
        "latency_p50_ms": card.latency_p50_ms,
        "latency_p95_ms": card.latency_p95_ms,
        "reconciliation": recon,
    }
    return card, summary, _markdown(card, accuracy, recon, tier)


def write_outputs(
    out_dir: Path, label: str, card: Scorecard, summary: dict[str, Any], markdown: str
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"receipts-{label}"
    write_scorecard_json(card, out_dir / f"{stem}.scorecard.json")
    (out_dir / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out_dir / f"{stem}.md").write_text(markdown)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--tier", default="small", choices=[t.value for t in Tier])
    parser.add_argument("--out", type=Path, default=REPO / "evals" / "scorecards")
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal("2.00"))
    parser.add_argument("--label", default=None, help="file stem suffix; defaults to the tier")
    parser.add_argument(
        "--min-field-accuracy",
        type=float,
        default=None,
        help="exit 1 if mean field accuracy is below this (CI floor)",
    )
    args = parser.parse_args()
    tier = Tier(args.tier)
    card, summary, markdown = asyncio.run(run(tier, args.budget_usd))
    write_outputs(args.out, args.label or tier.value, card, summary, markdown)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "mode",
                    "tier",
                    "receipts",
                    "errors",
                    "mean_field_accuracy",
                    "cost_total_usd",
                    "cost_per_receipt_usd",
                )
            }
        )
    )
    floor = args.min_field_accuracy
    if floor is not None and summary["mean_field_accuracy"] < floor:
        print(f"mean field accuracy below the floor of {floor}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
