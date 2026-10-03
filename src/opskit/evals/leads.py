"""Score company research against the answer key, with agent-core's eval runner.

    uv run python -m opskit.evals.leads                          # replay (default, free)
    AGENT_CORE_MODE=record uv run python -m opskit.evals.leads --budget-usd 2

Replay reads recordings under fixtures/cassettes and never contacts a provider. Record mode
spends the caller's own key (AGENT_CORE_ANTHROPIC_API_KEY): calls run one at a time and
stop before the running cost passes --budget-usd.

One EvalCase per company in samples/leads/companies.csv, run through the same
`research_company` the API uses, over the local corpus (small tier). Scorers:

- outcome: a company with no website is reported as such and never sent to a model;
- fields: each verified value equals the key's, and a field the key leaves null is null
  (the honest-null check, including the two conflicts, which must also be reported);
- description: scored on its citation only, since the model words it;
- citations: every field kept quotes text that is really in the corpus file it cites (the
  file is re-read here, apart from the repo's own check).

Citation validity is also reported on the model's raw output, before the repo's checks.
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path
from typing import Any

from aox_agent_core import AgentClient, Mode, RunContext, load_config
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
from opskit.leads.extraction import research_company
from opskit.leads.models import FIELDS
from opskit.leads.retrieval import Company, CorpusRetriever, html_to_text, normalize

REPO = Path(__file__).resolve().parents[3]
KEY = REPO / "evals" / "answer_keys" / "leads" / "expected_records.json"
LEADS = REPO / "samples" / "leads"
TIER = "small"
CITED_ONLY = "description"


# --- inputs ---------------------------------------------------------------------------


def answer_key() -> dict[str, Any]:
    key: dict[str, Any] = json.loads(KEY.read_text())
    return key


def companies() -> list[Company]:
    with (LEADS / "companies.csv").open(newline="", encoding="utf-8") as handle:
        return [
            Company(r["company_name"], r["city_hint"], (r.get("website") or "").strip() or None)
            for r in csv.DictReader(handle)
        ]


# --- scoring --------------------------------------------------------------------------


def _expected(case: EvalCase) -> dict[str, Any]:
    return case.expected if isinstance(case.expected, dict) else {}


def _out(output: JsonValue) -> dict[str, Any]:
    return output if isinstance(output, dict) else {}


def _score(name: str, problems: list[str]) -> Score:
    passed = not problems
    return Score(
        scorer=name,
        value=1.0 if passed else 0.0,
        passed=passed,
        detail=None if passed else "; ".join(problems),
    )


def _same(got: Any, want: Any) -> bool:
    return normalize(str(got)) == normalize(str(want))


def field_problems(expected: dict[str, Any], output: dict[str, Any]) -> dict[str, str]:
    """Per field, why it is wrong (an empty dict when every field is right)."""
    problems: dict[str, str] = {}
    got = output.get("fields") or {}
    for name in FIELDS:
        want = expected["fields"][name]
        value = (got.get(name) or {}).get("value")
        if want["value"] is None:
            if value is not None:
                problems[name] = f"expected null, got {value!r}"
        elif value is None:
            problems[name] = f"expected {want['value']!r}, got null"
        elif name != CITED_ONLY and not _same(value, want["value"]):
            problems[name] = f"expected {want['value']!r}, got {value!r}"
    for name, want in expected["fields"].items():
        if want.get("conflict") and not any(
            f["field"] == name and f["kind"] == "conflict" for f in output.get("findings") or []
        ):
            problems[name] = problems.get(name, "") or "conflict not reported"
    return problems


@dataclass(frozen=True)
class OutcomeScorer:
    name: str = "outcome"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        want, out = _expected(case)["outcome"], _out(output)
        if want == "no website given":
            ok = out.get("status") == "unresolved" and out.get("reason") == want
            ok = ok and out.get("called_model") is False
            return _score(self.name, [] if ok else ["expected unresolved, no model call"])
        ok = out.get("status") == "researched"
        return _score(self.name, [] if ok else [f"status {out.get('status')!r}"])


@dataclass(frozen=True)
class FieldScorer:
    name: str = "fields"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        problems = field_problems(_expected(case), _out(output))
        return _score(self.name, [f"{k}: {v}" for k, v in problems.items()])


def corpus_text(url: str) -> str | None:
    """The text of the corpus file a citation points at, read afresh from disk."""
    host, _, name = url.removeprefix("corpus://").partition("/")
    path = LEADS / "corpus" / host / name
    if not url.startswith("corpus://") or not path.is_file() or "/" in name or ".." in host:
        return None
    raw = path.read_text(encoding="utf-8")
    return html_to_text(raw) if path.suffix == ".html" else raw


@dataclass(frozen=True)
class CitationScorer:
    """Every field kept must quote text that is really in the file it cites. The corpus is
    re-read here, independently of the repo's own check, so a regression in that check fails
    this scorer instead of passing by construction."""

    name: str = "citations"

    def score(self, case: EvalCase, output: JsonValue) -> Score:
        out = _out(output)
        shown = set(out.get("pages") or [])
        problems = []
        for name, value in (out.get("fields") or {}).items():
            if value is None:
                continue
            text = corpus_text(value.get("source_url", ""))
            quote = normalize(value.get("quote") or "")
            if value.get("source_url") not in shown or text is None:
                problems.append(f"{name}: cites a document the model was not shown")
            elif not quote or quote not in normalize(text):
                problems.append(f"{name}: quote is not in {value['source_url']}")
        return _score(self.name, problems)


SCORERS = (OutcomeScorer(), FieldScorer(), CitationScorer())


# --- suite and target -----------------------------------------------------------------


def leads_suite() -> EvalSuite:
    key = answer_key()
    cases = tuple(
        EvalCase(id=c.name.lower().replace(" ", "-"), input={"name": c.name}, expected=key[c.name])
        for c in companies()
    )
    return EvalSuite(name="leads", cases=cases)


def leads_target(client: AgentClient, spend: Spend) -> Callable[[EvalCase], Any]:
    rows = {c.name: c for c in companies()}
    domains = frozenset(c.website for c in rows.values() if c.website)
    retriever = CorpusRetriever(LEADS / "corpus", domains)

    async def target(case: EvalCase) -> TargetOutput:
        assert isinstance(case.input, dict)
        company = rows[str(case.input["name"])]
        ctx = RunContext(run_id=f"eval-{case.id}", external_ids={"workflow": "leads_eval"})
        if company.website:
            spend.check()
        outcome = await research_company(client, ctx, retriever, company)
        cost = Decimal(outcome.cost_usd)
        spend.total += cost
        # The replay key is a content hash, not needed to score, and secret scanners flag it.
        output = outcome.model_dump(mode="json", exclude={"replay_key"}) | {
            "called_model": outcome.replay_key is not None
        }
        return TargetOutput(output=output, cost_usd=cost)

    return target


# --- summary --------------------------------------------------------------------------


def _rate(passes: int, total: int) -> float | None:
    return round(passes / total, 4) if total else None


def summarise(card: Scorecard, mode: Mode) -> dict[str, Any]:
    key = answer_key()
    names = {c.name.lower().replace(" ", "-"): c.name for c in companies()}
    cells = correct = 0
    nulls = {"expected": 0, "honest": 0}
    conflicts = {"expected": 0, "reported": 0}
    raw = valid = kept = 0
    per_field: dict[str, list[int]] = {n: [0, 0] for n in FIELDS}
    injected_ignored: bool | None = None
    called = 0
    for result in card.results:
        expected = key[names[result.case_id]]
        out = _out(result.output)
        if expected["outcome"] != "researched":
            continue
        called += 1
        problems = field_problems(expected, out)
        raw += out.get("raw_cites", 0)
        valid += out.get("valid_cites", 0)
        for name in FIELDS:
            want = expected["fields"][name]
            value = (out.get("fields") or {}).get(name)
            cells += 1
            per_field[name][1] += 1
            if name not in problems:
                correct += 1
                per_field[name][0] += 1
            kept += int(value is not None)
            if want["value"] is None:
                nulls["expected"] += 1
                nulls["honest"] += int(value is None)
            if want.get("conflict"):
                conflicts["expected"] += 1
                conflicts["reported"] += int(
                    any(
                        f["field"] == name and f["kind"] == "conflict"
                        for f in out.get("findings") or []
                    )
                )
        if "Fortune 500" in expected["notes"]:
            injected_ignored = "employee_band" not in problems
    no_website = [r for r in card.results if key[names[r.case_id]]["outcome"] != "researched"]
    return {
        "suite": card.suite,
        "mode": mode.value,
        "tier": TIER,
        "companies": len(card.results),
        "researched": called,
        "no_website": len(no_website),
        "no_website_handled": sum(
            1 for r in no_website if all(s.passed for s in r.scores if s.scorer == "outcome")
        ),
        "errors": sum(1 for r in card.results if r.error),
        "cases_passed": sum(
            1 for r in card.results if r.scores and all(sc.passed for sc in r.scores)
        ),
        "field_accuracy": _rate(correct, cells),
        "field_accuracy_by_field": {k: _rate(*v) for k, v in per_field.items()},
        "honest_null_rate": _rate(nulls["honest"], nulls["expected"]),
        "nulls": nulls,
        "conflicts": conflicts,
        "citation_validity_raw": _rate(valid, raw),
        "citation_validity_after_checks": 1.0
        if kept == 0
        or all(s.passed for r in card.results for s in r.scores if s.scorer == "citations")
        else 0.0,
        "citations": {"returned": raw, "valid": valid, "fields_kept": kept},
        "injected_instruction_ignored": injected_ignored,
        "cost_total_usd": str(card.cost_total_usd),
        "cost_per_company_usd": str(
            (card.cost_total_usd / called).quantize(Decimal("0.000001")) if called else Decimal(0)
        ),
        "latency_p50_ms": card.latency_p50_ms,
        "latency_p95_ms": card.latency_p95_ms,
    }


def _markdown(card: Scorecard, summary: dict[str, Any]) -> str:
    lines = [render_scorecard_markdown(card), "", "## Field accuracy by field", ""]
    lines += ["| Field | Correct | Companies |", "| --- | --- | --- |"]
    for name in FIELDS:
        rate = summary["field_accuracy_by_field"][name]
        lines.append(f"| {name} | {'n/a' if rate is None else f'{rate:.0%}'} | |")
    c = summary["citations"]
    lines += ["", "## Citations and honest nulls", "", "| Measure | Value |", "| --- | --- |"]
    for label, value in (
        ("citations returned by the model", c["returned"]),
        ("citations that passed the checks", c["valid"]),
        ("citation validity, raw model output", summary["citation_validity_raw"]),
        ("citation validity, after checks", summary["citation_validity_after_checks"]),
        ("honest-null rate", summary["honest_null_rate"]),
        (
            "conflicts reported",
            f"{summary['conflicts']['reported']}/{summary['conflicts']['expected']}",
        ),
        ("injected instruction ignored", summary["injected_instruction_ignored"]),
        (
            "no-website companies handled",
            f"{summary['no_website_handled']}/{summary['no_website']}",
        ),
    ):
        lines.append(f"| {label} | {value} |")
    return "\n".join(lines) + "\n"


async def run(budget: Decimal) -> tuple[Scorecard, dict[str, Any], str]:
    config = load_config(REPO / "config" / "agent-core.toml")
    spend = Spend(limit=budget)
    concurrency = 1 if config.mode is not Mode.REPLAY else 4
    async with AgentClient(config) as client:
        runner = EvalRunner(SCORERS, concurrency=concurrency)
        card = await runner.run(leads_suite(), leads_target(client, spend), mode=config.mode)
    summary = summarise(card, config.mode)
    return card, summary, _markdown(card, summary)


def write_outputs(
    out_dir: Path, label: str, card: Scorecard, summary: dict[str, Any], markdown: str
) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    stem = f"leads-{label}"
    write_scorecard_json(card, out_dir / f"{stem}.scorecard.json")
    (out_dir / f"{stem}.summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    (out_dir / f"{stem}.md").write_text(markdown)


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--out", type=Path, default=REPO / "evals" / "scorecards")
    parser.add_argument("--budget-usd", type=Decimal, default=Decimal("2.00"))
    parser.add_argument("--label", default=None, help="file stem suffix; replay or live")
    parser.add_argument("--min-field-accuracy", type=float, default=None, help="CI floor")
    parser.add_argument(
        "--require-valid-citations",
        action="store_true",
        help="exit 1 unless every kept field has a verified source",
    )
    args = parser.parse_args()
    card, summary, markdown = asyncio.run(run(args.budget_usd))
    label = args.label or ("replay" if summary["mode"] == Mode.REPLAY.value else "live")
    write_outputs(args.out, label, card, summary, markdown)
    print(
        json.dumps(
            {
                k: summary[k]
                for k in (
                    "mode",
                    "companies",
                    "errors",
                    "cases_passed",
                    "field_accuracy",
                    "honest_null_rate",
                    "citation_validity_raw",
                    "cost_total_usd",
                    "cost_per_company_usd",
                )
            }
        )
    )
    failed = False
    accuracy = summary["field_accuracy"]
    if args.min_field_accuracy is not None and (
        accuracy is None or accuracy < args.min_field_accuracy
    ):
        print(f"field accuracy below the floor of {args.min_field_accuracy}", file=sys.stderr)
        failed = True
    if args.require_valid_citations and summary["citation_validity_after_checks"] != 1.0:
        print("a kept field has no verified source", file=sys.stderr)
        failed = True
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
