"""purge_payloads takes its audit records in one append, so its batch is capped below that cap."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aox_agent_core.approvals import RoleApproverPolicy

from opskit.core.errors import NotAuthorizedToPurgeError
from opskit.core.pg.approvals import PURGE_BATCH_MAX, PgApprovalQueue
from opskit.core.pg.audit import MAX_APPEND_BATCH
from opskit.core.ports import ADMIN_ROLE, N8N_SERVICE, Principal, PrincipalKind

ADMIN = Principal(id="ops.admin", kind=PrincipalKind.HUMAN, roles=frozenset({ADMIN_ROLE}))


def _queue() -> PgApprovalQueue:
    return PgApprovalQueue(
        object(),  # type: ignore[arg-type]
        policy=RoleApproverPolicy(roles_by_action={}),
        listed_actions=[],
    )


def test_a_purge_batch_always_fits_one_audit_append() -> None:
    assert PURGE_BATCH_MAX <= MAX_APPEND_BATCH


@pytest.mark.parametrize("limit", [0, -1, PURGE_BATCH_MAX + 1, MAX_APPEND_BATCH + 1])
async def test_a_limit_outside_the_batch_cap_is_refused_before_anything_runs(limit: int) -> None:
    with pytest.raises(ValueError, match="limit must be between"):
        await _queue().purge_payloads(principal=ADMIN, older_than=timedelta(days=2), limit=limit)


async def test_a_principal_without_the_approver_or_admin_role_is_refused_first() -> None:
    # The refusal comes before the limit, the age and the connection are looked at; the audit
    # note is best effort, so a store that cannot be reached does not turn it into another error.
    with pytest.raises(NotAuthorizedToPurgeError):
        await _queue().purge_payloads(
            principal=N8N_SERVICE, older_than=timedelta(seconds=1), limit=10**6
        )
