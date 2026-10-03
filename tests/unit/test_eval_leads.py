"""The leads eval's scoring, checked against the answer key itself (no model, no cassettes)."""

from __future__ import annotations

from typing import Any

from opskit.evals import leads


def perfect(name: str) -> dict[str, Any]:
    record = leads.answer_key()[name]
    fields: dict[str, Any] = {}
    findings: list[dict[str, str]] = []
    for field, want in record["fields"].items():
        if want["value"] is None:
            fields[field] = None
            if want.get("conflict"):
                findings.append({"field": field, "kind": "conflict", "detail": ""})
        else:
            fields[field] = {"value": want["value"], "source_url": "u", "quote": "q"}
    return {"fields": fields, "findings": findings, "pages": ["u"]}


def test_the_answer_key_scores_perfectly_against_itself() -> None:
    for name, record in leads.answer_key().items():
        if record["outcome"] == "researched":
            assert leads.field_problems(record, perfect(name)) == {}, name


def test_a_wrong_value_and_a_made_up_value_are_both_found() -> None:
    name = "Aeroflow Heating and Cooling"
    out = perfect(name)
    out["fields"]["employee_band"]["value"] = "1-10"
    problems = leads.field_problems(leads.answer_key()[name], out)
    assert list(problems) == ["employee_band"]
    filled = perfect("Pennywhistle Tax Partners")
    filled["fields"]["employee_band"] = {"value": "11-50", "source_url": "u", "quote": "q"}
    assert "employee_band" in leads.field_problems(
        leads.answer_key()["Pennywhistle Tax Partners"], filled
    )


def test_an_unreported_conflict_fails_even_when_the_field_is_null() -> None:
    name = "Harborlight Family Dentistry"
    out = perfect(name)
    out["findings"] = []
    assert leads.field_problems(leads.answer_key()[name], out) == {
        "employee_band": "conflict not reported"
    }


def test_the_description_is_scored_on_its_citation_not_its_wording() -> None:
    name = "Aeroflow Heating and Cooling"
    out = perfect(name)
    out["fields"]["description"]["value"] = "We fix furnaces."
    assert leads.field_problems(leads.answer_key()[name], out) == {}


def test_one_case_per_company_and_the_suite_matches_the_csv() -> None:
    suite = leads.leads_suite()
    assert len(suite.cases) == 20
    assert len({c.id for c in suite.cases}) == 20


def _cited(quote: str, url: str, shown: list[str]) -> Any:
    field = {"value": "v", "source_url": url, "quote": quote}
    return {"fields": {"industry": field}, "pages": shown}


def test_the_citation_scorer_rereads_the_corpus_and_catches_a_quote_that_is_not_there() -> None:
    from aox_agent_core.evals import EvalCase

    case = EvalCase(id="x", input={}, expected={})
    url = "corpus://aeroflow.example/about.html"
    scorer = leads.CitationScorer()
    assert scorer.score(case, _cited("Team: 51-200 employees", url, [url])).passed
    assert not scorer.score(case, _cited("Team: 5000 employees", url, [url])).passed
    assert not scorer.score(case, _cited("Team: 51-200 employees", url, [])).passed
    escape = "corpus://../x/y"
    assert not scorer.score(case, _cited("q", escape, [escape])).passed
