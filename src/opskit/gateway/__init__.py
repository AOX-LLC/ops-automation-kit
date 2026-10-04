"""An opt-in client for the AI gateway's tools, speaking MCP over streamable HTTP.

Off by default (`OPSKIT_GATEWAY_ENABLED`). Replay serves recorded gateway answers, so tests and
demos run with the gateway down; only `mcp_transport` imports the MCP SDK.
"""

from opskit.gateway.client import GatewayArgumentError, GatewayClient
from opskit.gateway.factory import build_gateway_client
from opskit.gateway.outcomes import (
    ApprovalExpired,
    ApprovalRejected,
    NotAvailable,
    Ok,
    Outcome,
    Pending,
    PolicyRefused,
    RateLimited,
    Unauthorized,
    Unavailable,
    UpstreamError,
)

__all__ = [
    "ApprovalExpired",
    "ApprovalRejected",
    "GatewayArgumentError",
    "GatewayClient",
    "NotAvailable",
    "Ok",
    "Outcome",
    "Pending",
    "PolicyRefused",
    "RateLimited",
    "Unauthorized",
    "Unavailable",
    "UpstreamError",
    "build_gateway_client",
]
