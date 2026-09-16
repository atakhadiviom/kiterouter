"""Interactive Cline re-authentication via the WorkOS device authorization flow.

Cline's API rejects requests from accounts that are no longer linked, so a
token refresh alone cannot recover them — a fresh interactive login can. The
flow: start a device authorization, have the user approve the code in their
browser, then exchange the resulting WorkOS tokens for a Cline session.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

logger = logging.getLogger("kiterouter.cline_auth")

WORKOS_DEVICE_URL = "https://api.workos.com/user_management/authorize/device"
WORKOS_AUTHENTICATE_URL = "https://api.workos.com/user_management/authenticate"
CLINE_REGISTER_URL = "https://api.cline.bot/api/v1/auth/register"
WORKOS_CLIENT_ID = "client_01K3A541FN8TA3EPPHTD2325AR"
DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"

DEFAULT_EXPIRES_IN = 300
DEFAULT_POLL_INTERVAL = 5


class DeviceFlowError(Exception):
    """Terminal failure of the device flow (denied, expired, or upstream error)."""


class AuthorizationPending(Exception):
    """The user has not approved the device code yet; poll again later."""

    def __init__(self, interval: int = DEFAULT_POLL_INTERVAL):
        super().__init__("authorization_pending")
        self.interval = interval


def _unwrap(payload: Any) -> Any:
    """Unwrap the ``{"success": true, "data": {...}}`` Cline envelope."""
    if isinstance(payload, dict) and "data" in payload and "success" in payload:
        return payload.get("data")
    return payload


async def start_device_flow(client_id: str = WORKOS_CLIENT_ID) -> Dict[str, Any]:
    """Begin a device authorization and return the code the user must approve."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            WORKOS_DEVICE_URL,
            data={"client_id": client_id},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
    if resp.status_code != 200:
        raise DeviceFlowError(f"Device authorization failed (HTTP {resp.status_code})")

    data = resp.json()
    if not data.get("device_code") or not data.get("user_code"):
        raise DeviceFlowError("Device authorization response was incomplete")

    return {
        "device_code": data["device_code"],
        "user_code": data["user_code"],
        "verification_uri": data.get("verification_uri"),
        "verification_uri_complete": data.get("verification_uri_complete"),
        "expires_in": int(data.get("expires_in") or DEFAULT_EXPIRES_IN),
        "interval": int(data.get("interval") or DEFAULT_POLL_INTERVAL),
    }


async def poll_device_flow(
    device_code: str, client_id: str = WORKOS_CLIENT_ID
) -> Dict[str, Any]:
    """Check a device authorization once.

    Raises AuthorizationPending while the user has not approved yet, and
    DeviceFlowError when the flow is over. On success returns the WorkOS token
    pair, which still has to be exchanged via :func:`register_cline_session`.
    """
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            WORKOS_AUTHENTICATE_URL,
            data={
                "grant_type": DEVICE_GRANT_TYPE,
                "device_code": device_code,
                "client_id": client_id,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )

    if resp.status_code == 200:
        data = resp.json()
        if not data.get("access_token") or not data.get("refresh_token"):
            raise DeviceFlowError("Device authorization returned no token pair")
        return {
            "access_token": data["access_token"],
            "refresh_token": data["refresh_token"],
            "token_type": data.get("token_type") or "Bearer",
        }

    try:
        error = resp.json().get("error") or ""
    except Exception:
        error = ""

    if error == "authorization_pending":
        raise AuthorizationPending()
    if error == "slow_down":
        raise AuthorizationPending(interval=DEFAULT_POLL_INTERVAL + 5)
    raise DeviceFlowError(f"Device authorization failed: {error or f'HTTP {resp.status_code}'}")


async def register_cline_session(access_token: str, refresh_token: str) -> Dict[str, Any]:
    """Exchange a WorkOS token pair for a Cline session."""
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            CLINE_REGISTER_URL,
            json={"accessToken": access_token, "refreshToken": refresh_token},
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
    if resp.status_code != 200:
        raise DeviceFlowError(f"Cline token exchange failed (HTTP {resp.status_code})")

    data = _unwrap(resp.json())
    if not isinstance(data, dict) or not data.get("accessToken"):
        raise DeviceFlowError("Cline token exchange response was incomplete")

    user_info = data.get("userInfo") or {}
    return {
        "access_token": data["accessToken"],
        "refresh_token": data.get("refreshToken") or refresh_token,
        "expires_at": data.get("expiresAt"),
        "email": user_info.get("email") or "",
        "accounts": user_info.get("accounts"),
    }


async def complete_device_flow(
    device_code: str, client_id: str = WORKOS_CLIENT_ID
) -> Optional[Dict[str, Any]]:
    """Poll once and, when approved, return a usable Cline session."""
    tokens = await poll_device_flow(device_code, client_id)
    return await register_cline_session(tokens["access_token"], tokens["refresh_token"])
