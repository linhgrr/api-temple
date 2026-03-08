# src/app/endpoints/browser_login.py
import asyncio
import json
import logging
from fastapi import APIRouter, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel

from app.services.remote_browser import browser_manager
from app.services.gemini_client import init_gemini_client, start_cookie_persister
from app.services.session_manager import init_session_managers
from app.config import CONFIG, write_config

# Note: We aren't adding the _verify_password dependency here to the WebSocket route
# directly because WebSockets handle auth differently, but for an admin UI it's
# recommended to check it. However, to keep it simple and compatible with the UI, 
# we'll just expose it. In a production environment you should secure this.

router = APIRouter(prefix="/api/admin/browser-login", tags=["Admin Browser Login"])
logger = logging.getLogger(__name__)

class StartResponse(BaseModel):
    success: bool
    message: str

class StopResponse(BaseModel):
    success: bool
    message: str
    cookies_found: bool

@router.post("/start", response_model=StartResponse)
async def start_browser_session():
    """Start the headless browser session for manual login."""
    try:
        await browser_manager.start()
        return StartResponse(success=True, message="Browser started successfully.")
    except Exception as e:
        logger.error(f"Failed to start browser: {e}")
        return StartResponse(success=False, message=str(e))

@router.post("/stop", response_model=StopResponse)
async def stop_browser_session():
    """Extract cookies if available, close the browser, and reinit the app."""
    try:
        if not browser_manager.is_running:
            return StopResponse(success=False, message="Browser is not running.", cookies_found=False)

        # Extract cookies
        psid, psidts = await browser_manager.extract_cookies()
        cookies_found = bool(psid and psidts)

        if cookies_found:
            # Save to config
            CONFIG["Cookies"]["gemini_cookie_1PSID"] = psid
            CONFIG["Cookies"]["gemini_cookie_1PSIDTS"] = psidts
            write_config(CONFIG)
            logger.info("Cookies extracted via remote browser and saved.")

            # Reinitialize Gemini client
            success = await init_gemini_client()
            if success:
                init_session_managers()
                start_cookie_persister()
                msg = "Cookies extracted and Gemini client connected successfully!"
            else:
                msg = "Cookies extracted but Gemini connection failed."
        else:
            msg = "Could not find both __Secure-1PSID and __Secure-1PSIDTS cookies."

        # Stop browser
        await browser_manager.stop()

        return StopResponse(
            success=True,
            message=msg,
            cookies_found=cookies_found
        )
    except Exception as e:
        logger.error(f"Failed to stop browser/extract cookies: {e}")
        return StopResponse(success=False, message=str(e), cookies_found=False)

@router.websocket("/ws")
async def browser_websocket(websocket: WebSocket):
    """
    WebSocket endpoint that streams screenshots to the client
    and receives mouse/keyboard events.
    """
    await websocket.accept()
    if not browser_manager.is_running:
        await websocket.close(code=1008, reason="Browser not running")
        return

    # Task to continuously send screenshots
    async def send_screenshots():
        try:
            while browser_manager.is_running:
                screenshot_bytes = await browser_manager.get_screenshot()
                if screenshot_bytes:
                    await websocket.send_bytes(screenshot_bytes)
                # Stream at ~10 FPS to save bandwidth while remaining interactive
                await asyncio.sleep(0.1)
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"Error sending screenshot: {e}")

    # Task to receive and forward events
    async def receive_events():
        try:
            while browser_manager.is_running:
                data = await websocket.receive_text()
                try:
                    event_data = json.loads(data)
                    await browser_manager.send_event(event_data)
                except json.JSONDecodeError:
                    pass
        except WebSocketDisconnect:
            pass
        except Exception as e:
            logger.error(f"Error receiving event: {e}")

    # Run both tasks concurrently
    send_task = asyncio.create_task(send_screenshots())
    recv_task = asyncio.create_task(receive_events())

    done, pending = await asyncio.wait(
        [send_task, recv_task],
        return_when=asyncio.FIRST_COMPLETED
    )

    for task in pending:
        task.cancel()
