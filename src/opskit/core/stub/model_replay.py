"""Mock-mode model client: replays recorded responses, keyed by what was asked."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from decimal import Decimal
from pathlib import Path

from pydantic import BaseModel
from sqlalchemy import insert

from opskit.core.errors import FixtureMissing, LiveModeUnavailable
from opskit.core.fixtures import fixture_key, fixture_path, request_digest
from opskit.core.ports import (
    Attachment,
    JsonValue,
    Mode,
    ModelResult,
    PromptRef,
    RunContext,
    Tier,
)
from opskit.core.stub.audit_pg import append_in
from opskit.db.engine import SessionFactory
from opskit.db.tables import model_calls


class ReplayModelClient:
    def __init__(self, *, fixtures_dir: Path, session_factory: SessionFactory, mode: Mode) -> None:
        self._fixtures_dir = fixtures_dir
        self._session_factory = session_factory
        self._mode = mode

    async def structured[T: BaseModel](
        self,
        *,
        ctx: RunContext,
        prompt: PromptRef,
        tier: Tier,
        schema: type[T],
        inputs: Mapping[str, JsonValue],
        attachments: Sequence[Attachment] = (),
    ) -> ModelResult[T]:
        if self._mode is not Mode.MOCK:
            raise LiveModeUnavailable()
        digest = request_digest(
            prompt=prompt,
            tier=tier,
            schema_name=schema.__name__,
            inputs=inputs,
            attachments=attachments,
        )
        key = fixture_key(digest)
        path = fixture_path(self._fixtures_dir, ctx.workflow, prompt.id, key)
        if not path.is_file():
            raise FixtureMissing(key, path)
        recorded = json.loads(path.read_text(encoding="utf-8"))
        result = ModelResult(
            output=schema.model_validate(recorded["response"]),
            input_tokens=recorded["usage"]["input_tokens"],
            output_tokens=recorded["usage"]["output_tokens"],
            cost_usd=Decimal(recorded["cost_usd"]),
            latency_ms=recorded["latency_ms"],
            fixture_key=key,
            mode=Mode.MOCK,
        )
        await self._record_call(ctx, prompt, tier, result)
        return result

    async def _record_call(
        self, ctx: RunContext, prompt: PromptRef, tier: Tier, result: ModelResult[BaseModel]
    ) -> None:
        async with self._session_factory.begin() as session:
            await session.execute(
                insert(model_calls).values(
                    run_id=ctx.run_id,
                    prompt_id=prompt.id,
                    prompt_version=prompt.version,
                    tier=tier.value,
                    fixture_key=result.fixture_key,
                    mode=result.mode.value,
                    input_tokens=result.input_tokens,
                    output_tokens=result.output_tokens,
                    cost_usd=result.cost_usd,
                    latency_ms=result.latency_ms,
                )
            )
            # IDs and the key only: prompt text and inputs stay out of the audit log.
            await append_in(
                session,
                ctx=ctx,
                actor="system",
                action="model.call",
                subject_type="prompt",
                subject_id=prompt.id,
                details={"fixture_key": result.fixture_key, "mode": result.mode.value},
            )
