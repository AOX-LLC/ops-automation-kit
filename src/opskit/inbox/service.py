"""Triage and drafting for one email: the orchestration around the policy's rules.

Inbound email is untrusted. Triage runs the deterministic injection scan and the model's
judgement; either flags the email, and a flagged email is never drafted for. Drafting sees
only the profile and the one email, the helper (never the model) fixes the recipient, and a
draft is checked against the profile before it can go to a person for approval.
"""

from __future__ import annotations

from collections.abc import Sequence
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel

from opskit.config import Settings
from opskit.core.errors import ModelRefusalError, StructuredOutputError
from opskit.core.ports import ModelClient, RunContext, Tier
from opskit.db.fit import fit_list
from opskit.inbox import injection, policy
from opskit.inbox.injection import MAX_EVIDENCE
from opskit.inbox.mail import InboundMessage
from opskit.inbox.policy import (
    DRAFT_PROMPT,
    TRIAGE_PROMPT,
    DraftReply,
    RecipientError,
    TriageResult,
    build_triage_inputs,
)

DraftStatus = Literal["draft", "failed"]


class NotDraftable(Exception):
    """The message is held for review or needs no reply, so no draft may be made."""


class InjectionReason(BaseModel):
    rule: str
    evidence: str | None = None


class TriageOutcome(BaseModel):
    message_id: str
    category: str
    priority: str
    needs_reply: bool
    escalate: bool
    route: str
    quarantined: bool
    injection_reasons: list[InjectionReason]
    reply_to_differs: bool
    replay_key: str | None = None
    cost_usd: str = "0"
    latency_ms: int | None = None


class DraftOutcome(BaseModel):
    to: str
    subject: str
    in_reply_to: str | None = None
    body: str
    facts_used: list[str]
    grounding: dict[str, list[str]]
    reply_to_differs: bool
    status: DraftStatus
    failure_reason: str | None = None
    replay_key: str | None = None
    cost_usd: str = "0"
    latency_ms: int | None = None


def load_profile(settings: Settings) -> str:
    return (settings.samples_dir / "inbox" / "business_profile.md").read_text(encoding="utf-8")


def _squash(text: str) -> str:
    return " ".join(text.split())


def _cost_string(cost: Decimal) -> str:
    return format(cost, "f")


def reply_to_differs(msg: InboundMessage) -> bool:
    try:
        return policy.reply_envelope(
            from_header=msg.from_header,
            reply_to_header=msg.reply_to_header,
            subject=msg.subject,
            message_id=msg.message_id,
        ).reply_to_differs
    except RecipientError:
        return False


async def triage_message(
    models: ModelClient, ctx: RunContext, msg: InboundMessage
) -> TriageOutcome:
    """Scan, ask the small model, decide the route. Model errors and ReplayMissError propagate."""
    # The display name reaches the prompts too, so it is scanned with subject and body.
    email_text = f"{msg.from_header}\n{msg.subject}\n{msg.body_text}"
    hits = injection.scan(email_text)
    result = await models.call(
        TRIAGE_PROMPT,
        inputs=build_triage_inputs(sender=msg.from_header, subject=msg.subject, body=msg.body_text),
        output=TriageResult,
        tier=Tier.SMALL,
        context=ctx,
    )
    triage: TriageResult = result.output
    reasons = [InjectionReason(rule=hit.rule, evidence=hit.evidence) for hit in hits]
    if triage.injection_suspected:
        quote = triage.injection_evidence
        kept = quote if quote and _squash(quote) in _squash(email_text) else None
        # Stored evidence is cut to what a regex hit's is (the database caps the whole list).
        reasons.append(
            InjectionReason(rule="model", evidence=kept[:MAX_EVIDENCE] if kept else None)
        )
    quarantined = bool(hits) or triage.injection_suspected
    return TriageOutcome(
        message_id=msg.message_id,
        category=triage.category,
        priority=triage.priority,
        needs_reply=triage.needs_reply,
        escalate=triage.escalate,
        route=policy.route(triage, quarantined=quarantined).value,
        quarantined=quarantined,
        injection_reasons=reasons,
        reply_to_differs=reply_to_differs(msg),
        replay_key=result.replay_key or None,
        cost_usd=_cost_string(result.cost_usd),
        latency_ms=round(result.latency_ms),
    )


# What inbox_0003 lets a draft hold (facts_used: 16384 bytes; grounding: 16384 bytes over three
# lists plus their keys), with a margin; a test cross-checks them against the migration.
STORED_ITEMS_MAX = 64
STORED_ITEM_CHARS_MAX = 500
STORED_FACTS_BYTES = 16000
STORED_GROUNDING_LIST_BYTES = 5000
# drafts.body: with the rest of the reply it must fit core.approvals.payload (see core_0010).
DRAFT_BODY_MAX = 16384


def _stored_list(values: Sequence[str], *, budget: int) -> list[str]:
    """Model-written lists, cut to what the database accepts, by items, characters and bytes.
    Groundedness was judged on the full lists first, so cutting what is stored cannot change an
    outcome."""
    return fit_list(
        values, budget=budget, max_items=STORED_ITEMS_MAX, item_chars=STORED_ITEM_CHARS_MAX
    )


def _failed(reason: str, *, to: str = "", subject: str = "", differs: bool = False) -> DraftOutcome:
    return DraftOutcome(
        to=to,
        subject=subject,
        body="",
        facts_used=[],
        grounding={},
        reply_to_differs=differs,
        status="failed",
        failure_reason=reason,
    )


async def draft_reply(
    models: ModelClient,
    ctx: RunContext,
    msg: InboundMessage,
    triage: TriageOutcome,
    profile: str,
) -> DraftOutcome:
    """Draft one reply, or report why none can be made. ReplayMissError propagates."""
    if triage.route != policy.Route.DRAFT or triage.quarantined:
        raise NotDraftable("held for review" if triage.quarantined else "no reply needed")
    try:
        envelope = policy.reply_envelope(
            from_header=msg.from_header,
            reply_to_header=msg.reply_to_header,
            subject=msg.subject,
            message_id=msg.message_id,
        )
    except RecipientError:
        return _failed("invalid_sender")
    try:
        result = await models.call(
            DRAFT_PROMPT,
            inputs=policy.build_draft_inputs(
                profile=profile,
                category=triage.category,
                sender=msg.from_header,
                subject=msg.subject,
                body=msg.body_text,
            ),
            output=DraftReply,
            tier=Tier.MID,
            context=ctx,
        )
    except (ModelRefusalError, StructuredOutputError) as error:
        failed = _failed(
            type(error).__name__,
            to=envelope.to,
            subject=envelope.subject,
            differs=envelope.reply_to_differs,
        )
        return failed.model_copy(update={"in_reply_to": envelope.in_reply_to})
    # Zero-width and bidi characters are dropped, so what the approver reads is what is sent.
    draft = result.output.model_copy(
        update={"body": injection.HIDDEN_CHARS.sub("", result.output.body)}
    )
    if len(draft.body) > DRAFT_BODY_MAX:
        # Too long to store or to send as one approval: a failed draft, not a refused insert.
        too_long = _failed(
            "too_long",
            to=envelope.to,
            subject=envelope.subject,
            differs=envelope.reply_to_differs,
        )
        return too_long.model_copy(
            update={
                "in_reply_to": envelope.in_reply_to,
                # The model call ran and was paid for; keep its trace on the row.
                "replay_key": result.replay_key or None,
                "cost_usd": _cost_string(result.cost_usd),
                "latency_ms": round(result.latency_ms),
            }
        )
    report = policy.check_grounding(
        draft, profile=profile, email_text=msg.body_text, allowed_addresses=[envelope.to]
    )
    ungrounded = bool(report.unsupported_facts or report.unsupported_facts_used)
    return DraftOutcome(
        to=envelope.to,
        subject=envelope.subject,
        in_reply_to=envelope.in_reply_to,
        body=draft.body,
        facts_used=_stored_list(draft.facts_used, budget=STORED_FACTS_BYTES),
        grounding={
            name: _stored_list(values, budget=STORED_GROUNDING_LIST_BYTES)
            for name, values in (
                ("unsupported_facts", report.unsupported_facts),
                ("unsupported_facts_used", report.unsupported_facts_used),
                ("commitment_flags", report.commitment_flags),
            )
        },
        reply_to_differs=envelope.reply_to_differs,
        status="failed" if ungrounded else "draft",
        failure_reason="ungrounded" if ungrounded else None,
        replay_key=result.replay_key or None,
        cost_usd=_cost_string(result.cost_usd),
        latency_ms=round(result.latency_ms),
    )
