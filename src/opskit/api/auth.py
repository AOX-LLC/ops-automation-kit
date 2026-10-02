"""Service authentication: n8n calls `/v1/*` with a bearer token from the kit-secrets volume.

This token can start runs and request approvals. It cannot decide one: decisions happen
only on the approver page, behind its own login.
"""

from __future__ import annotations

import hmac
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

bearer = HTTPBearer(auto_error=False)


def require_service(
    request: Request,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer)],
) -> None:
    expected: str = request.app.state.service_token
    supplied = credentials.credentials if credentials else ""
    if not hmac.compare_digest(supplied.encode(), expected.encode()):
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED,
            "invalid service token",
            headers={"WWW-Authenticate": "Bearer"},
        )


ServiceAuth = Depends(require_service)
