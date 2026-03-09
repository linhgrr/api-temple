# src/app/services/gemini_client.py
import asyncio
import os
from models.gemini import MyGeminiClient
from app.config import CONFIG, write_config
from app.logger import logger
from app.utils.browser import get_cookie_from_browser

# Import the specific exception to handle it gracefully
from gemini_webapi.exceptions import AuthError
import httpx
from urllib.parse import urlparse

_ACTIVE_NGROK_PROXY = None

# --- Monkeypatch httpx to route through our Ngrok REVERSE Proxy ---
# We patch at the lowest Transport layer so that `httpx` evaluates the original `gemini.google.com`
# URL against the CookieJar FIRST. This ensures `.google.com` cookies are securely attached 
# to the headers BEFORE we hijack the socket connection to send it to our Ngrok reverse proxy.
_original_handle_async_request = httpx.AsyncHTTPTransport.handle_async_request

async def _patched_handle_async_request(self, request, *args, **kwargs):
    global _ACTIVE_NGROK_PROXY
    proxy_url = _ACTIVE_NGROK_PROXY
    
    if proxy_url and ("ngrok" in proxy_url or "trycloudflare" in proxy_url):
        original_url = str(request.url)
        
        if "gemini.google.com" in original_url or "www.google.com" in original_url:
            base_proxy = proxy_url.rstrip("/")
            if "://" not in base_proxy:
                base_proxy = f"https://{base_proxy}"
            else:
                base_proxy = base_proxy.replace("http://", "https://")
            
            parsed_proxy = urlparse(base_proxy)
            parsed_original = urlparse(original_url)
            
            # Rewrite URL to HTTPS proxy (Ngrok forces HTTPS)
            new_url = original_url.replace(f"https://{parsed_original.netloc}", base_proxy)
            logger.info(f"Reverse Proxying Transport: {request.method} {new_url}")
            
            request.url = httpx.URL(new_url)
            # Host must match TLS SNI (= Ngrok hostname), otherwise 421
            request.headers["Host"] = parsed_proxy.netloc
            # Pass original host so home_proxy.py can restore it for Google
            request.headers["X-Forwarded-Host"] = parsed_original.netloc
            request.headers["ngrok-skip-browser-warning"] = "1"
            
            # Ngrok free tier doesn't support HTTP/2, use a shared HTTP/1.1 transport
            global _http1_transport
            if _http1_transport is None:
                _http1_transport = httpx.AsyncHTTPTransport(http2=False)
            return await _http1_transport.handle_async_request(request)
            
    return await _original_handle_async_request(self, request, *args, **kwargs)

_http1_transport = None
httpx.AsyncHTTPTransport.handle_async_request = _patched_handle_async_request
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
    global _gemini_client, _initialization_error, _error_code, _ACTIVE_NGROK_PROXY
    _initialization_error = None
    _error_code = None

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
                
            # If using ngrok/cloudflare tunnel, the Transport hook natively intercepts
            # these requests right before execution.
            _ACTIVE_NGROK_PROXY = gemini_proxy
            actual_proxy = gemini_proxy
            
            if gemini_proxy and ("ngrok" in gemini_proxy or "trycloudflare" in gemini_proxy):
                # We do NOT pass the proxy to httpx to prevent HTTP CONNECT requests
                actual_proxy = None
                logger.info(f"Active Reverse Proxy set to: {gemini_proxy}")
            else:
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

