# src/app/endpoints/cookies.py
"""
Public cookie management endpoints for Docker / headless environments.

These endpoints allow setting and retrieving the Gemini authentication cookies
without needing the Admin UI. Especially useful for:
- Docker containers (no browser available)
- CI/CD pipelines
- Programmatic cookie rotation
"""

import os

from fastapi import APIRouter, Depends, HTTPException, Header
from pydantic import BaseModel, Field

from app.config import CONFIG, write_config


async def _verify_password(x_password: str = Header(..., description="Password for cookie management endpoints")):
    password = os.environ.get("COOKIES_PASSWORD", "")
    if not password:
        raise HTTPException(status_code=500, detail="COOKIES_PASSWORD not configured in environment")
    if x_password != password:
        raise HTTPException(status_code=401, detail="Invalid password")
from app.logger import logger
from app.services.gemini_client import (
    GeminiClientNotInitializedError,
    get_client_status,
    get_gemini_client,
    init_gemini_client,
    start_cookie_persister,
)
from app.services.session_manager import init_session_managers

router = APIRouter(prefix="/v1/cookies", tags=["Cookie Management"], dependencies=[Depends(_verify_password)])


# --- Request / Response models ---


class SetCookiesRequest(BaseModel):
    """Request body to set Gemini cookies.

    You can find these cookies by:
    1. Logging into https://gemini.google.com in a browser
    2. Opening DevTools (F12) → Application → Cookies → gemini.google.com
    3. Copying the values for ``__Secure-1PSID`` and ``__Secure-1PSIDTS``

    Example::

        {
          "secure_1psid": "g.a000...",
          "secure_1psidts": "sidts-CjE..."
        }
    """
    secure_1psid: str = Field(
        ...,
        min_length=10,
        description="Value of the __Secure-1PSID cookie from gemini.google.com.",
    )
    secure_1psidts: str = Field(
        ...,
        min_length=10,
        description="Value of the __Secure-1PSIDTS cookie from gemini.google.com.",
    )
    reinitialize: bool = Field(
        default=True,
        description="If true (default), immediately reinitialize the Gemini client with the new cookies.",
    )


class CookieStatusResponse(BaseModel):
    """Response showing the current cookie and connection status."""
    cookies_configured: bool = Field(description="Whether both cookies are present in config.")
    gemini_connected: bool = Field(description="Whether the Gemini client is currently connected and authenticated.")
    secure_1psid: str = Field(description="Full value of __Secure-1PSID cookie.")
    secure_1psidts: str = Field(description="Full value of __Secure-1PSIDTS cookie.")
    error: str | None = Field(description="Error message if the client failed to connect.")
    error_code: str | None = Field(description="Error code: auth_expired, no_cookies, network, disabled, unknown.")


class SetCookiesResponse(BaseModel):
    """Response after setting cookies."""
    success: bool = Field(description="Whether the Gemini client connected successfully with the new cookies.")
    cookies_saved: bool = Field(description="Whether cookies were persisted to config.conf.")
    gemini_connected: bool = Field(description="Whether the Gemini client is now connected.")
    message: str = Field(description="Human-readable status message.")
    error_code: str | None = Field(default=None, description="Error code if connection failed.")
    error_detail: str | None = Field(default=None, description="Detailed error message if connection failed.")


# --- Helpers ---


# --- Endpoints ---


@router.get(
    "",
    response_model=CookieStatusResponse,
    summary="Get cookies and status",
    description=(
        "Returns the full Gemini cookie values and connection health.\n\n"
        "Requires `X-Password` header for authentication.\n\n"
        "**Example:**\n"
        "```bash\n"
        "curl http://localhost:6969/v1/cookies -H 'X-Password: <password>'\n"
        "```"
    ),
)
async def get_cookies():
    """
    Get current cookie values and Gemini connection health.

    Returns full cookie values, connection status, and any error details.
    Requires password via X-Password header.
    """
    psid = CONFIG["Cookies"].get("gemini_cookie_1PSID", "")
    psidts = CONFIG["Cookies"].get("gemini_cookie_1PSIDTS", "")
    client_status = get_client_status()

    try:
        get_gemini_client()
        connected = True
    except GeminiClientNotInitializedError:
        connected = False

    return CookieStatusResponse(
        cookies_configured=bool(psid and psidts),
        gemini_connected=connected,
        secure_1psid=psid,
        secure_1psidts=psidts,
        error=client_status.get("error"),
        error_code=client_status.get("error_code"),
    )


@router.put(
    "",
    response_model=SetCookiesResponse,
    summary="Set cookies and connect",
    description=(
        "Set Gemini authentication cookies and (optionally) reinitialize the client.\n\n"
        "This is the primary way to configure authentication in **Docker** environments\n"
        "where no browser is available for automatic cookie extraction.\n\n"
        "**Docker quick-start:**\n"
        "```bash\n"
        "# After starting the container:\n"
        'curl -X PUT http://localhost:6969/v1/cookies \\\n'
        "  -H 'Content-Type: application/json' \\\n"
        "  -H 'X-Password: <password>' \\\n"
        "  -d '{\n"
        '    "secure_1psid": "g.a000...",\n'
        '    "secure_1psidts": "sidts-CjE..."\n'
        "  }'\n"
        "```\n\n"
        "Cookies are persisted to ``config.conf`` (or ``/app/data/config.conf`` in Docker)\n"
        "and survive container restarts when using a volume mount.\n\n"
        "Set ``reinitialize: false`` if you only want to save cookies without connecting immediately."
    ),
)
async def set_cookies(request: SetCookiesRequest):
    """
    Set Gemini __Secure-1PSID and __Secure-1PSIDTS cookies.

    Saves to config.conf and optionally reinitializes the Gemini client.
    In Docker, ensure /app/data is mounted as a volume so cookies persist
    across container restarts.
    """
    # Save to config
    CONFIG["Cookies"]["gemini_cookie_1PSID"] = request.secure_1psid
    CONFIG["Cookies"]["gemini_cookie_1PSIDTS"] = request.secure_1psidts
    write_config(CONFIG)
    logger.info("Cookies updated via /v1/cookies endpoint.")

    connected = False
    status = get_client_status()

    if request.reinitialize:
        logger.info("Reinitializing Gemini client with new cookies...")
        success = await init_gemini_client()
        status = get_client_status()
        connected = success

        if success:
            # Also reinitialize session managers and cookie persister
            init_session_managers()
            start_cookie_persister()
            msg = "Cookies saved and Gemini client connected successfully!"
        else:
            msg = f"Cookies saved but connection failed: {status.get('error', 'unknown error')}"
    else:
        # Update in-memory cookies of the running client without full reinit.
        # This avoids hitting Google auth endpoints while keeping cookies fresh.
        try:
            client = get_gemini_client()
            client.client.cookies.set("__Secure-1PSID", request.secure_1psid, domain=".google.com")
            client.client.cookies.set("__Secure-1PSIDTS", request.secure_1psidts, domain=".google.com")
            connected = True
            msg = "Cookies saved and updated in running client (no reinit)."
            logger.info(msg)
        except GeminiClientNotInitializedError:
            connected = False
            msg = "Cookies saved to config. Client not running — reinitialize to connect."

    return SetCookiesResponse(
        success=connected or not request.reinitialize,
        cookies_saved=True,
        gemini_connected=connected,
        message=msg,
        error_code=status.get("error_code"),
        error_detail=status.get("error"),
    )


@router.delete(
    "",
    summary="Clear cookies",
    description=(
        "Remove stored cookies from config and disconnect the Gemini client.\n\n"
        "After clearing, the server will not be able to process Gemini requests\n"
        "until new cookies are provided via ``PUT /v1/cookies``."
    ),
)
async def clear_cookies():
    """
    Clear stored Gemini cookies and reinitialize the client (which will fail
    without cookies, effectively disconnecting).
    """
    CONFIG["Cookies"]["gemini_cookie_1PSID"] = ""
    CONFIG["Cookies"]["gemini_cookie_1PSIDTS"] = ""
    write_config(CONFIG)
    logger.info("Cookies cleared via /v1/cookies endpoint.")

    # Reinitialize — will fail with "no_cookies" error, disconnecting the client
    await init_gemini_client()

    return {
        "success": True,
        "message": "Cookies cleared. Gemini client disconnected.",
    }
