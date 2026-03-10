# src/app/services/gemini_client.py
import asyncio
import os
from models.gemini import MyGeminiClient
from app.config import CONFIG, write_config
from app.logger import logger
from app.utils.browser import get_cookie_from_browser

# Import the specific exception to handle it gracefully
from gemini_webapi.exceptions import AuthError
from gemini_webapi.constants import Endpoint, Headers

# --- Ngrok Reverse Proxy: patch gemini_webapi constants directly ---
# Instead of monkeypatching httpx (fragile), we simply rewrite the
# endpoint URLs that the library uses. This way ALL internal clients,
# ephemeral or not, naturally send requests to our Ngrok reverse proxy.

_ORIGINAL_ENDPOINTS = {
    "GOOGLE": str(Endpoint.GOOGLE),
    "INIT": str(Endpoint.INIT),
    "GENERATE": str(Endpoint.GENERATE),
    "BATCH_EXEC": str(Endpoint.BATCH_EXEC),
    "ROTATE_COOKIES": str(Endpoint.ROTATE_COOKIES),
}

_ORIGINAL_HEADERS = dict(Headers.GEMINI.value)
_ORIGINAL_ROTATE_HEADERS = dict(Headers.ROTATE_COOKIES.value)

def _apply_ngrok_proxy(proxy_url: str):
    """Rewrite gemini_webapi endpoint constants to route through Ngrok reverse proxy.

    Endpoint.GOOGLE (www.google.com) is intentionally NOT rewritten because:
    1. The library's get_access_token sends a bare AsyncClient request to it
       with NO custom headers (no ngrok-skip-browser-warning) and NO timeout
       override — causing a 5-second httpx timeout through the tunnel.
    2. That request doesn't carry session cookies, so calling it directly from
       the server IP is safe — it only fetches auxiliary NID cookies.
    """
    proxy_url = proxy_url.rstrip("/")
    secure = proxy_url.replace("http://", "https://")
    
    from urllib.parse import urlparse
    proxy_host = urlparse(secure).netloc
    
    # Rewrite endpoints that carry session cookies through the tunnel.
    # GOOGLE is left untouched (see docstring above).
    type.__setattr__(Endpoint, "INIT", _ORIGINAL_ENDPOINTS["INIT"].replace("https://gemini.google.com", secure))
    type.__setattr__(Endpoint, "GENERATE", _ORIGINAL_ENDPOINTS["GENERATE"].replace("https://gemini.google.com", secure))
    type.__setattr__(Endpoint, "BATCH_EXEC", _ORIGINAL_ENDPOINTS["BATCH_EXEC"].replace("https://gemini.google.com", secure))
    type.__setattr__(Endpoint, "ROTATE_COOKIES", _ORIGINAL_ENDPOINTS["ROTATE_COOKIES"].replace("https://accounts.google.com", secure))
    
    # Patch Headers.GEMINI — used by INIT / GENERATE / BATCH_EXEC.
    # X-Forwarded-Host tells the home reverse proxy which Google domain to forward to.
    headers = dict(_ORIGINAL_HEADERS)
    headers["Host"] = proxy_host
    headers["Origin"] = secure
    headers["Referer"] = secure + "/"
    headers["X-Forwarded-Host"] = "gemini.google.com"
    headers["ngrok-skip-browser-warning"] = "1"
    Headers.GEMINI._value_ = headers
    
    # Patch Headers.ROTATE_COOKIES — used by RotateCookies (accounts.google.com).
    rotate_headers = dict(_ORIGINAL_ROTATE_HEADERS)
    rotate_headers["X-Forwarded-Host"] = "accounts.google.com"
    rotate_headers["ngrok-skip-browser-warning"] = "1"
    Headers.ROTATE_COOKIES._value_ = rotate_headers
    
    logger.info(f"Ngrok reverse proxy activated: endpoints rewritten to {secure}")
    logger.info(f"  Endpoint.INIT  = {Endpoint.INIT}")
    logger.info(f"  Endpoint.GOOGLE = {Endpoint.GOOGLE} (direct, not proxied)")

def _reset_endpoints():
    """Restore original endpoint URLs and headers."""
    type.__setattr__(Endpoint, "GOOGLE", _ORIGINAL_ENDPOINTS["GOOGLE"])
    type.__setattr__(Endpoint, "INIT", _ORIGINAL_ENDPOINTS["INIT"])
    type.__setattr__(Endpoint, "GENERATE", _ORIGINAL_ENDPOINTS["GENERATE"])
    type.__setattr__(Endpoint, "BATCH_EXEC", _ORIGINAL_ENDPOINTS["BATCH_EXEC"])
    type.__setattr__(Endpoint, "ROTATE_COOKIES", _ORIGINAL_ENDPOINTS["ROTATE_COOKIES"])
    Headers.GEMINI._value_ = dict(_ORIGINAL_HEADERS)
    Headers.ROTATE_COOKIES._value_ = dict(_ORIGINAL_ROTATE_HEADERS)

# ----------------------------------------------------------------

class GeminiClientNotInitializedError(Exception):
    """Raised when the Gemini client is not initialized or initialization failed."""
    pass


# Global variable to store the Gemini client instance
_gemini_client = None
_initialization_error = None
_error_code = None  # "auth_expired", "no_cookies", "network", "disabled", "unknown"
_persist_task: asyncio.Task = None  # Background task for persisting rotated cookies

async def init_gemini_client() -> bool:
    """
    Initialize and set up the Gemini client based on the configuration.
    Returns True on success, False on failure.
    """
    global _gemini_client, _initialization_error, _error_code
    _initialization_error = None
    _error_code = None

    # Close the previous client to stop its auto_refresh background task.
    # Without this, each reinit leaks an extra loop that calls
    # accounts.google.com/RotateCookies every 9 minutes.
    if _gemini_client is not None:
        try:
            await _gemini_client.close()
            logger.info("Previous Gemini client closed.")
        except Exception as e:
            logger.warning(f"Error closing previous Gemini client: {e}")
        _gemini_client = None

    if CONFIG.getboolean("EnabledAI", "gemini", fallback=True):
        try:
            gemini_cookie_1PSID = CONFIG["Cookies"].get("gemini_cookie_1PSID")
            gemini_cookie_1PSIDTS = CONFIG["Cookies"].get("gemini_cookie_1PSIDTS")
            # Resolve proxy: config value → env var fallback
            gemini_proxy = CONFIG["Proxy"].get("http_proxy") or ""
            if not gemini_proxy:
                gemini_proxy = os.environ.get("NGROK_PROXY_URL", "")
                if gemini_proxy:
                    logger.info(f"Using NGROK_PROXY_URL from env: {gemini_proxy}")

            if not gemini_cookie_1PSID or not gemini_cookie_1PSIDTS:
                cookies = get_cookie_from_browser("gemini")
                if cookies:
                    gemini_cookie_1PSID, gemini_cookie_1PSIDTS = cookies

            if gemini_proxy == "":
                gemini_proxy = None

            logger.info(f"Proxy config resolved to: {gemini_proxy!r}")
                
            # If using ngrok/cloudflare tunnel, rewrite library constants to
            # route through reverse proxy. Don't pass proxy to httpx (no CONNECT needed).
            actual_proxy = gemini_proxy
            if gemini_proxy and ("ngrok" in gemini_proxy or "trycloudflare" in gemini_proxy):
                _apply_ngrok_proxy(gemini_proxy)
                actual_proxy = None
            else:
                _reset_endpoints()
                if gemini_proxy:
                    logger.info(f"Using standard HTTP proxy: {gemini_proxy}")
                else:
                    logger.info("No proxy configured — calling Google directly.")

            if gemini_cookie_1PSID and gemini_cookie_1PSIDTS:
                _gemini_client = MyGeminiClient(secure_1psid=gemini_cookie_1PSID, secure_1psidts=gemini_cookie_1PSIDTS, proxy=actual_proxy)
                await _gemini_client.init()
                logger.info("Gemini client initialized successfully.")
                return True
            else:
                _error_code = "no_cookies"
                _initialization_error = "Gemini cookies not found."
                logger.error(_initialization_error)
                return False

        except AuthError as e:
            _error_code = "auth_expired"
            _initialization_error = str(e)
            logger.error(f"Gemini authentication failed: {e}")
            _gemini_client = None
            return False

        except (ConnectionError, OSError, TimeoutError) as e:
            _error_code = "network"
            _initialization_error = str(e)
            logger.error(f"Network error initializing Gemini client: {e}")
            _gemini_client = None
            return False

        except Exception as e:
            _error_code = "unknown"
            _initialization_error = str(e)
            logger.error(f"Unexpected error initializing Gemini client: {e}", exc_info=True)
            _gemini_client = None
            return False
    else:
        _error_code = "disabled"
        _initialization_error = "Gemini client is disabled in config."
        logger.info(_initialization_error)
        return False


def get_gemini_client():
    """
    Returns the initialized Gemini client instance.

    Raises:
        GeminiClientNotInitializedError: If the client is not initialized.
    """
    if _gemini_client is None:
        error_detail = _initialization_error or "Gemini client was not initialized. Check logs for details."
        raise GeminiClientNotInitializedError(error_detail)
    return _gemini_client


def get_client_status() -> dict:
    """Return the current status of the Gemini client for the admin UI."""
    return {
        "initialized": _gemini_client is not None,
        "error": _initialization_error,
        "error_code": _error_code,
    }


async def _persist_cookies_loop():
    """
    Background task that watches for cookie rotation by gemini-webapi's auto_refresh
    mechanism and persists any updated values back to config.conf.

    The library rotates __Secure-1PSIDTS every ~9 minutes in-memory only.
    Without this task, a server restart would reload the original (expired) cookies.
    """
    # Wait one full refresh cycle before first check so the library has time to rotate
    await asyncio.sleep(600)
    while True:
        try:
            if _gemini_client is not None:
                # Access the underlying WebGeminiClient cookies dict
                client_cookies = _gemini_client.client.cookies
                new_1psid = client_cookies.get("__Secure-1PSID")
                new_1psidts = client_cookies.get("__Secure-1PSIDTS")

                current_1psid = CONFIG["Cookies"].get("gemini_cookie_1PSID", "")
                current_1psidts = CONFIG["Cookies"].get("gemini_cookie_1PSIDTS", "")

                changed = False
                if new_1psid and new_1psid != current_1psid:
                    CONFIG["Cookies"]["gemini_cookie_1PSID"] = new_1psid
                    changed = True
                    logger.info("__Secure-1PSID rotated — will persist to config.")
                if new_1psidts and new_1psidts != current_1psidts:
                    CONFIG["Cookies"]["gemini_cookie_1PSIDTS"] = new_1psidts
                    changed = True
                    logger.info("__Secure-1PSIDTS rotated — will persist to config.")

                if changed:
                    write_config(CONFIG)
                    logger.info("Rotated Gemini cookies persisted to config.conf.")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.warning(f"Cookie persist check failed: {e}")

        await asyncio.sleep(600)  # Re-check every 10 minutes


def start_cookie_persister() -> asyncio.Task:
    """Start the background cookie-persist task. Safe to call multiple times."""
    global _persist_task
    if _persist_task is not None and not _persist_task.done():
        return _persist_task
    _persist_task = asyncio.create_task(_persist_cookies_loop())
    logger.info("Cookie persist task started (checks every 10 min).")
    return _persist_task


def stop_cookie_persister():
    """Cancel the cookie persister task on shutdown."""
    global _persist_task
    if _persist_task is not None and not _persist_task.done():
        _persist_task.cancel()
        logger.info("Cookie persist task stopped.")
    _persist_task = None

