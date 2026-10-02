"""Print the generated logins. Run only via `make login` (or the smoke test with --env)."""

from __future__ import annotations

import sys
from pathlib import Path

SECRETS = Path("/run/kit-secrets")


def _read(name: str) -> str:
    return (SECRETS / name).read_text(encoding="utf-8").strip()


def main() -> None:
    owner, approver = _read("n8n_owner_password"), _read("approver_password")
    if "--env" in sys.argv:
        # Machine-readable form for scripts/smoke.sh, which keeps the values in variables.
        print(f"KIT_OWNER_PASSWORD={owner}")
        print(f"KIT_APPROVER_PASSWORD={approver}")
        print(f"KIT_WEBHOOK_TOKEN={_read('n8n_webhook_token')}")
        return
    print("n8n editor      email: owner@kit.example")
    print(f"                password: {owner}")
    print(f"approver page   password: {approver}")


if __name__ == "__main__":
    main()
