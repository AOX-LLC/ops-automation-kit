"""The inbox rules, pinned against the committed sample emails. No model, no stack."""

from __future__ import annotations

from email import message_from_bytes, policy
from pathlib import Path

import pytest

from opskit.inbox import injection
from opskit.inbox import policy as inbox_policy
from opskit.inbox.policy import (
    DraftReply,
    GroundingReport,
    RecipientError,
    build_draft_inputs,
    check_grounding,
    reply_envelope,
)

ROOT = Path(__file__).parents[2] / "samples" / "inbox"
MESSAGES = ROOT / "messages"
PROFILE = (ROOT / "business_profile.md").read_text(encoding="utf-8")
INJECTED = ("m15", "m26", "m27")
CLEAN = [p.stem for p in sorted(MESSAGES.glob("m*.eml")) if p.stem not in INJECTED]


def _load(name: str) -> tuple[str, str, str]:
    """(subject, body, raw From header) of one sample."""
    parsed = message_from_bytes((MESSAGES / f"{name}.eml").read_bytes(), policy=policy.default)
    body = parsed.get_body(preferencelist=("plain",))
    assert body is not None
    return str(parsed["Subject"]), body.get_content(), str(parsed["From"])


def _scan(name: str) -> list[injection.InjectionHit]:
    subject, body, _ = _load(name)
    return injection.scan(subject + "\n" + body)


def test_sample_set_has_the_expected_shape() -> None:
    assert len(CLEAN) == 25


@pytest.mark.parametrize("name", INJECTED)
def test_scan_flags_the_injection_samples(name: str) -> None:
    assert _scan(name), f"{name} should be flagged"


@pytest.mark.parametrize("name", CLEAN)
def test_scan_flags_none_of_the_other_samples(name: str) -> None:
    hits = _scan(name)
    assert hits == [], f"false positive on {name}: {hits}"


def _envelope(
    from_header: str, reply_to: str | None = None, subject: str = "Hello"
) -> inbox_policy.Envelope:
    return reply_envelope(
        from_header=from_header, reply_to_header=reply_to, subject=subject, message_id="id@x"
    )


def test_envelope_uses_the_single_from_address() -> None:
    envelope = _envelope("Ann Lee <Ann.Lee@Mail.example>", subject="Question")
    assert envelope.to == "ann.lee@mail.example"
    assert envelope.subject == "Re: Question"
    assert envelope.in_reply_to == "id@x"
    assert envelope.reply_to_differs is False


def test_envelope_does_not_double_the_re_prefix() -> None:
    assert _envelope("a@mail.example", subject="RE: Hi").subject == "RE: Hi"


@pytest.mark.parametrize(
    "from_header",
    ["", "a@mail.example, b@mail.example", "not an address", "Ann <a@@mail.example>", "<>"],
)
def test_envelope_refuses_empty_multiple_or_malformed_from(from_header: str) -> None:
    with pytest.raises(RecipientError):
        _envelope(from_header)


def test_a_differing_reply_to_is_flagged_and_never_used() -> None:
    envelope = _envelope("a@mail.example", "Boss <boss@other.example>")
    assert envelope.reply_to_differs is True
    assert envelope.to == "a@mail.example"


def test_the_same_reply_to_is_not_flagged() -> None:
    assert _envelope("A <a@mail.example>", "a@mail.example").reply_to_differs is False


def test_m28_replies_to_from_not_reply_to() -> None:
    subject, _, from_header = _load("m28")
    raw = message_from_bytes((MESSAGES / "m28.eml").read_bytes(), policy=policy.default)
    envelope = reply_envelope(
        from_header=from_header,
        reply_to_header=str(raw["Reply-To"]),
        subject=subject,
        message_id=str(raw["Message-ID"]),
    )
    assert envelope.to == "gideon.larkin@mailbox-eighteen.example"
    assert envelope.reply_to_differs is True


def test_draft_inputs_have_exactly_the_documented_keys() -> None:
    inputs = build_draft_inputs(
        profile=PROFILE, category="support", sender="a@x.example", subject="s", body="b"
    )
    assert set(inputs) == {"profile", "category", "sender", "subject", "body"}


def test_another_emails_text_never_reaches_the_draft_inputs() -> None:
    subject, body, from_header = _load("m01")
    other_subject, other_body, _ = _load("m02")
    inputs = build_draft_inputs(
        profile=PROFILE, category="sales_inquiry", sender=from_header, subject=subject, body=body
    )
    flat = "\n".join(str(value) for value in inputs.values())
    assert other_body.strip() not in flat
    assert other_subject not in flat
    assert inputs["body"] == body


def _grounding(body: str, email_text: str = "", facts: list[str] | None = None) -> GroundingReport:
    return check_grounding(
        DraftReply(body=body, facts_used=facts or []),
        profile=PROFILE,
        email_text=email_text,
        allowed_addresses=["cust@mail.example"],
    )


def test_a_price_missing_from_the_profile_is_flagged() -> None:
    report = _grounding("The install costs $1,200.")
    assert any(f.startswith("money") for f in report.unsupported_facts)


def test_a_profile_price_is_allowed() -> None:
    report = _grounding("A 40-gallon tank water heater, installed, is $1,450.")
    assert report.grounded


def test_the_profiles_own_commitment_phrase_is_allowed() -> None:
    report = _grounding("You can have free cancellation with 24 hours' notice.")
    assert report.commitment_flags == ()


@pytest.mark.parametrize(
    "body", ["We'll give you a refund for that.", "We offer same-day service for leaks."]
)
def test_unlisted_commitments_are_flagged(body: str) -> None:
    assert _grounding(body).commitment_flags


def test_the_customers_own_phone_number_is_allowed() -> None:
    email = "Please call me on 555-0188 after noon."
    assert _grounding("We will call you on 555-0188.", email_text=email).grounded


def test_a_phone_number_nobody_gave_is_flagged() -> None:
    assert _grounding("Call us on 555-0199.").unsupported_facts


def test_a_fact_quote_not_in_the_profile_is_flagged() -> None:
    report = _grounding("Hello.", facts=["We never close"])
    assert report.unsupported_facts_used == ("We never close",)


def test_commitment_grounded_by_a_matching_profile_sentence() -> None:
    from opskit.inbox.policy import DraftReply, check_grounding

    profile = (
        "Refunds: Refund requests are reviewed by the owner.\n"
        "Cancellation: Free cancellation with 24 hours' notice."
    )
    ok = DraftReply(body="Any refund request is reviewed by the owner.", facts_used=[])
    assert check_grounding(ok, profile=profile, email_text="").commitment_flags == ()
    cancel = DraftReply(body="Cancellation is free with notice.", facts_used=[])
    assert check_grounding(cancel, profile=profile, email_text="").commitment_flags == ()
    invented = DraftReply(body="Installation is free this month.", facts_used=[])
    assert check_grounding(invented, profile=profile, email_text="").commitment_flags
