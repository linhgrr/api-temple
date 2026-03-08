# src/app/services/remote_browser.py
import asyncio
import logging
import base64
from typing import Optional, Tuple
import nodriver as uc
from xvfbwrapper import Xvfb

logger = logging.getLogger(__name__)

class RemoteBrowserManager:
    def __init__(self):
        self.browser: Optional[uc.Browser] = None
        self.page: Optional[uc.Tab] = None
        self.vdisplay = None
        self.is_running = False

    async def start(self):
        if self.is_running:
            return
        
        logger.info("Starting remote browser session via nodriver...")
        try:
            # Start Xvfb virtual display
            self.vdisplay = Xvfb(width=1024, height=768, colordepth=24)
            self.vdisplay.start()
        except OSError as e:
            logger.warning(f"Xvfb not found, running without virtual display: {e}")
            self.vdisplay = None
            
            # Start Chromium via nodriver with timeout
            logger.info("Initializing nodriver...")
            self.browser = await asyncio.wait_for(
                uc.start(
                    headless=False, # We want "headful" but in xvfb
                    browser_args=[
                        "--no-sandbox",
                        "--disable-setuid-sandbox",
                        "--window-size=1024,768",
                        "--disable-dev-shm-usage",     # Fixes crashes in Docker
                        "--disable-gpu",               # Save memory
                        "--disable-software-rasterizer",
                    ]
                ), 
                timeout=15.0
            )
            
            logger.info("Browser process started, navigating to Google...")
            
            try:
                self.page = await asyncio.wait_for(
                    self.browser.get("https://accounts.google.com/ServiceLogin"),
                    timeout=15.0
                )
            except Exception as get_err:
                logger.warning(f"Browser get() threw an error, trying to get main tab manually: {get_err}")
                try:
                    # In some environments, nodriver drops the internal CDP HTTP connection due to IPv6/IPv4 
                    # localhost conflict, resulting in 'Connection refused'. We fallback to picking the 
                    # main tab.
                    self.page = self.browser.main_tab
                    if self.page:
                        # Use raw CDP navigation which goes through the WS stream instead of HTTP
                        await self.page.send(uc.cdp.page.navigate(url="https://accounts.google.com/ServiceLogin"))
                    else:
                        raise get_err
                except Exception as inner_e:
                    logger.error(f"Fallback navigation also failed: {inner_e}")
                    raise inner_e
            
            # Additional safety to wait for network/idle 
            # to make sure the page is completely present before websocket starts sending
            if self.page:
                try:
                    await asyncio.wait_for(self.page.bring_to_front(), timeout=5.0)
                except Exception as e:
                    logger.warning(f"bring_to_front failed: {e}")
            await asyncio.sleep(2)
            
            self.is_running = True
            logger.info("Browser started and navigated to Google login.")
        except asyncio.TimeoutError:
            logger.error("Timeout while starting browser or navigating.")
            await self.stop()
            raise Exception("Browser initialization timed out.")
        except Exception as e:
            logger.error(f"Error starting browser: {e}")
            await self.stop()
            raise e

    async def get_screenshot(self) -> Optional[bytes]:
        if not self.page or not self.is_running:
            return None
        try:
            # nodriver get_screenshot returns base64 string
            b64_img = await self.page.send(uc.cdp.page.capture_screenshot(format_="jpeg", quality=60))
            if isinstance(b64_img, str):
               return base64.b64decode(b64_img)
            return None
        except Exception as e:
            # If the page isn't fully ready, nodriver throws "Not attached to an active page"
            return None

    async def send_event(self, event_data: dict):
        if not self.page or not self.is_running:
            return
        
        try:
            msg_type = event_data.get("type")
            if msg_type == "click":
                x = event_data.get("x", 0)
                y = event_data.get("y", 0)
                await self.page.send(uc.cdp.input_.dispatch_mouse_event(
                    type_="mousePressed", x=x, y=y, button="left", click_count=1
                ))
                await self.page.send(uc.cdp.input_.dispatch_mouse_event(
                    type_="mouseReleased", x=x, y=y, button="left", click_count=1
                ))
            elif msg_type == "keydown":
                key = event_data.get("key", "")
                if key:
                    # Very basic mapping, for a full remote desktop we'd need better keycode handling
                    # Enter, Backspace, Tab
                    key_map = {
                        "Enter": "\r",
                        "Backspace": "\b",
                        "Tab": "\t",
                    }
                    char = key_map.get(key, key)
                    if len(char) == 1:
                         # Use Input.insertText for single characters
                        if char in ["\r", "\b", "\t"]:
                             # DispatchKeyEvent for control keys
                            await self.page.send(uc.cdp.input_.dispatch_key_event(
                                type_="keyDown", text=char
                            ))
                            await self.page.send(uc.cdp.input_.dispatch_key_event(
                                type_="keyUp", text=char
                            ))
                        else:
                            await self.page.send(uc.cdp.input_.insert_text(text=char))
        except Exception as e:
            logger.error(f"Error handling browser event: {e}")

    async def extract_cookies(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.browser:
            return None, None
            
        try:
            cookies = await self.browser.cookies.get_all()
            secure_1psid = None
            secure_1psidts = None
            
            for cookie in cookies:
                if "google" in cookie.domain:
                    if cookie.name == "__Secure-1PSID":
                        secure_1psid = cookie.value
                    elif cookie.name == "__Secure-1PSIDTS":
                        secure_1psidts = cookie.value
                        
            return secure_1psid, secure_1psidts
        except Exception as e:
            logger.error(f"Error extracting cookies: {e}")
            return None, None

    async def stop(self):
        logger.info("Stopping remote browser session...")
        self.is_running = False
        
        try:
            if self.browser:
                await self.browser.stop()
        except:
            pass
            
        try:
            if self.vdisplay:
                self.vdisplay.stop()
        except:
            pass
            
        self.browser = None
        self.page = None
        self.vdisplay = None

# Global instance
browser_manager = RemoteBrowserManager()
