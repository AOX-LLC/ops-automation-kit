"""Secret shapes the kit refuses to store, shared by the audit log and the gateway recordings."""

from __future__ import annotations

# ai-gateway bearer tokens (aig_<lookup id>_<secret>) and the gw_ shape its integration notes name.
# .gitleaks.toml carries the same two shapes.
GATEWAY_TOKEN_PATTERNS = {
    "ai_gateway_token": r"aig_[a-z2-7]{8}_[A-Za-z0-9_-]{20,}",
    "ai_gateway_token_gw": r"gw_[A-Za-z0-9_-]{20,}",
}
