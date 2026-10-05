"""Builds the gateway client from settings, or nothing when the feature is off."""

from __future__ import annotations

from opskit.config import Settings
from opskit.core.ports import AuditLog
from opskit.gateway.client import GatewayClient
from opskit.gateway.recordings import RecordingStore, RecordingTransport, ReplayTransport
from opskit.gateway.transport import ToolTransport

REPLAY = "replay"
RECORD = "record"
LIVE = "live"


def build_gateway_client(
    settings: Settings, audit: AuditLog | None = None, *, scenario: str = ""
) -> GatewayClient | None:
    """None when `OPSKIT_GATEWAY_ENABLED` is off. The MCP transport is imported only when a live
    or record client is built, so an api in replay (or with the feature off) never loads the SDK."""
    if not settings.gateway_enabled:
        return None
    store = RecordingStore(settings.gateway_recordings_dir)
    transport: ToolTransport
    if settings.gateway_mode == REPLAY:
        transport = ReplayTransport(store, scenario)
    else:
        from opskit.gateway.mcp_transport import McpTransport

        live = McpTransport(
            settings.gateway_url, settings.read_gateway_token(), settings.gateway_timeout_s
        )
        transport = (
            live if settings.gateway_mode == LIVE else RecordingTransport(live, store, scenario)
        )
    return GatewayClient(transport, audit)
