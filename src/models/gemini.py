# src/models/gemini.py
import asyncio
from typing import Optional, List, Union
from pathlib import Path
from gemini_webapi import GeminiClient as WebGeminiClient
from app.config import CONFIG
from app.logger import logger

# Errors that are transient — gemini-webapi auto-reinits the session after
# these, so retrying after a short delay usually succeeds.
_RETRYABLE_KEYWORDS = ("zombie stream", "failed to parse response body", "stalled")
_MAX_RETRIES = 2
_RETRY_DELAYS = (3.0, 5.0)  # seconds between retry attempts


class MyGeminiClient:
    """
    Wrapper for the Gemini Web API client with automatic retry on
    transient errors (zombie stream / parse failures).
    """
    def __init__(self, secure_1psid: str, secure_1psidts: str, proxy: str | None = None) -> None:
        self.client = WebGeminiClient(secure_1psid, secure_1psidts, proxy)

    async def init(self, auto_refresh: bool = False) -> None:
        """Initialize the Gemini client.

        auto_refresh is disabled by default because when the server runs behind
        a reverse-proxy tunnel (ngrok / cloudflare), the RotateCookies call
        goes directly from the server IP — not through the tunnel — causing
        Google to detect an IP mismatch and revoke the entire session.
        Cookie freshness is handled by the browser extension instead.
        """
        await self.client.init(auto_refresh=auto_refresh)

    async def generate_content(self, message: str, model: str, files: Optional[List[Union[str, Path]]] = None):
        """
        Generate content with automatic retry on transient errors.
        gemini-webapi reinitializes its session after zombie/parse errors
        (~2-3 s); retrying after that window succeeds in most cases.
        """
        last_exc: Exception | None = None
        for attempt in range(_MAX_RETRIES + 1):
            try:
                return await self.client.generate_content(message, model=model, files=files)
            except Exception as e:
                last_exc = e
                err_lower = str(e).lower()
                is_retryable = any(kw in err_lower for kw in _RETRYABLE_KEYWORDS)
                if is_retryable and attempt < _MAX_RETRIES:
                    delay = _RETRY_DELAYS[attempt]
                    logger.warning(
                        f"Gemini transient error (attempt {attempt + 1}/{_MAX_RETRIES + 1},"
                        f" model={model}): {e!r} — retrying in {delay}s"
                    )
                    await asyncio.sleep(delay)
                    continue
                # Non-retryable or exhausted retries
                raise
        raise last_exc  # unreachable, satisfies type checkers

    async def close(self) -> None:
        """Close the Gemini client."""
        await self.client.close()

    def start_chat(self, model: str):
        """
        Start a chat session with the given model.
        """
        return self.client.start_chat(model=model)
