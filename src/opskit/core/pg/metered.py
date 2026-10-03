"""A ModelClient wrapper that records every call's usage, cost and replay key.

agent-core does the routing, recording, replay and pricing. This wrapper only writes what
the scorecards and the audit trail need: one core.model_calls row and one model.call audit
record per call. Prompt text and model output never enter either.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, overload
from uuid import UUID

from aox_agent_core import Attachment, CallResult, ModelClient, PromptRef, RunContext, Tier
from aox_agent_core.audit import AuditEvent
from aox_agent_core.models import Prompt
from pydantic import BaseModel, JsonValue
from sqlalchemy import insert

from opskit.core.pg.audit import append_in
from opskit.db.engine import SessionFactory
from opskit.db.tables import model_calls


class MeteredModelClient:
    def __init__(self, inner: ModelClient, session_factory: SessionFactory) -> None:
        self._inner = inner
        self._session_factory = session_factory

    @overload
    async def call(
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: None = None,
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[str]: ...

    @overload
    async def call[OutputModelT: BaseModel](
        self,
        prompt: Prompt | PromptRef,
        *,
        inputs: Mapping[str, JsonValue] | None = None,
        attachments: Sequence[Attachment] = (),
        output: type[OutputModelT],
        tier: Tier | None = None,
        task: str | None = None,
        system: str | None = None,
        max_tokens: int | None = None,
        max_attempts: int = 2,
        context: RunContext | None = None,
    ) -> CallResult[OutputModelT]: ...

    async def call(self, prompt: Any, **kwargs: Any) -> Any:
        result: CallResult[Any] = await self._inner.call(prompt, **kwargs)
        await self._record(prompt, result, kwargs.get("context"))
        return result

    async def _record(
        self, prompt: Prompt | PromptRef, result: CallResult[Any], context: RunContext | None
    ) -> None:
        prompt_id = prompt.id if isinstance(prompt, PromptRef) else "adhoc"
        version = prompt.version if isinstance(prompt, PromptRef) else 0
        async with self._session_factory.begin() as session:
            await session.execute(
                insert(model_calls).values(
                    run_id=UUID(context.run_id) if context is not None else None,
                    prompt_id=prompt_id,
                    prompt_version=version,
                    tier=result.tier.value,
                    replay_key=result.replay_key,
                    model=result.model,
                    mode=result.mode.value,
                    input_tokens=result.usage.input_tokens,
                    output_tokens=result.usage.output_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=round(result.latency_ms),
                )
            )
            await append_in(
                session,
                AuditEvent(
                    action="model.call",
                    actor_id="system",
                    subject_id=prompt_id,
                    payload={
                        "replay_key": result.replay_key,
                        "mode": result.mode.value,
                        "tier": result.tier.value,
                        "cost_usd": str(result.cost_usd),
                    },
                    context=context,
                ),
            )
