"""Which actions the approver side will decide, and that nothing test-only is among them."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

from aox_agent_core.approvals import (
    ApprovalRequest,
    DenialReason,
    Principal,
    PrincipalKind,
    RoleApproverPolicy,
    approval_payload_hash,
)

from opskit.core.ports import APPROVER, APPROVER_ROLE, N8N_SERVICE, ROLES_BY_ACTION
from opskit.inbox.store import SEND_REPLY_ACTION

NOW = datetime.now(UTC)


def request(action: str, role: str = APPROVER_ROLE) -> ApprovalRequest:
    return ApprovalRequest(
        id=uuid4(),
        action=action,
        summary="s",
        payload_sha256=approval_payload_hash(action, {}),
        requested_by=N8N_SERVICE.id,
        required_role=role,
        created_at=NOW,
        expires_at=NOW + timedelta(hours=1),
    )


def test_every_real_action_is_listed_and_no_test_kind_is() -> None:
    assert set(ROLES_BY_ACTION) == {SEND_REPLY_ACTION, "kit_smoke.echo"}
    assert not any(name.startswith("test") for name in ROLES_BY_ACTION)


def test_the_policy_decides_listed_actions_for_the_approver() -> None:
    policy = RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION)
    assert policy.evaluate(APPROVER, request(SEND_REPLY_ACTION), now=NOW).allowed


def test_an_unlisted_action_is_refused() -> None:
    policy = RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION)
    verdict = policy.evaluate(APPROVER, request("test.approval"), now=NOW)
    assert not verdict.allowed and verdict.reason is DenialReason.UNKNOWN_ACTION


def test_a_requester_cannot_ask_for_a_weaker_role() -> None:
    policy = RoleApproverPolicy(roles_by_action=ROLES_BY_ACTION)
    weak = Principal(id="intern", kind=PrincipalKind.HUMAN, roles=frozenset({"intern"}))
    verdict = policy.evaluate(weak, request(SEND_REPLY_ACTION, role="intern"), now=NOW)
    assert not verdict.allowed and verdict.reason is DenialReason.ROLE_MISMATCH
