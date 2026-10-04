"""purge_payloads takes its audit records in one append, so its batch is capped below that cap."""

from __future__ import annotations

from datetime import timedelta

import pytest
from aox_agent_core.approvals import RoleApproverPolicy

from opskit.core.pg.approvals import PURGE_BATCH_MAX, PgApprovalQueue
from opskit.core.pg.audit import MAX_APPEND_BATCH
from opskit.core.ports import SWEEP_SERVICE


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
        await _queue().purge_payloads(
            principal=SWEEP_SERVICE, older_than=timedelta(days=2), limit=limit
        )
