"""`python -m opskit.seed`: load sample manifest, demo CRM and sample emails. Idempotent."""

from __future__ import annotations

import asyncio
import sys

from opskit.config import Settings
from opskit.core.factory import build_core
from opskit.core.ports import AuditEvent
from opskit.db.engine import make_engine, make_session_factory
from opskit.seed import crm, mailpit, manifest


async def run(settings: Settings) -> dict[str, int]:
    samples = settings.samples_dir
    rows = manifest.build_manifest(samples)
    accounts = crm.parse_accounts(samples / "crm" / "accounts.csv")

    engine = make_engine(settings)
    session_factory = make_session_factory(engine)
    try:
        async with session_factory() as session, session.begin():
            manifest_count = await manifest.upsert_manifest(session, rows)
            account_count = await crm.upsert_accounts(session, accounts)
        sent, present = await asyncio.to_thread(
            mailpit.load_emails, settings, samples / "inbox" / "messages"
        )
        counts = {
            "sample_files": manifest_count,
            "crm_accounts": account_count,
            "emails_sent": sent,
            "emails_already_present": present,
        }
        core = build_core(settings, session_factory)
        await core.audit.append(
            AuditEvent(action="seed.loaded", actor_id="seed", payload=dict(counts))
        )
        return counts
    finally:
        await engine.dispose()


def main() -> int:
    settings = Settings()
    if not settings.samples_dir.is_dir():
        print(f"seed: samples directory not found: {settings.samples_dir}", file=sys.stderr)
        return 1
    counts = asyncio.run(run(settings))
    print("seed: " + ", ".join(f"{name}={value}" for name, value in counts.items()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
