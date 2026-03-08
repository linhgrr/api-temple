# src/app/services/remote_browser.py
import asyncio
import logging
from typing import Optional, Tuple
from playwright.async_api import async_playwright, Browser, BrowserContext, Page

logger = logging.getLogger(__name__)

class RemoteBrowserManager:
    def __init__(self):
        self.playwright = None
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None
        self.is_running = False

    async def start(self):
        if self.is_running:
            return
        
        logger.info("Starting remote browser session...")
        self.playwright = await async_playwright().start()
        
        # Start Chromium in headless mode, but with some args to make login easier
        self.browser = await self.playwright.chromium.launch(
            headless=True,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
                "--disable-setuid-sandbox",
            ]
        )
        
        # User-agent to avoid detection
        self.context = await self.browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36",
            viewport={"width": 1024, "height": 768}
        )
        
        self.page = await self.context.new_page()
        
        # Go to Google login
        await self.page.goto("https://accounts.google.com/ServiceLogin")
        self.is_running = True

    async def get_screenshot(self) -> Optional[bytes]:
        if not self.page or not self.is_running:
            return None
        try:
            return await self.page.screenshot(type="jpeg", quality=60)
        except Exception as e:
            logger.error(f"Error capturing screenshot: {e}")
            return None

    async def send_event(self, event_data: dict):
        if not self.page or not self.is_running:
            return
        
        try:
            msg_type = event_data.get("type")
            if msg_type == "click":
                x = event_data.get("x", 0)
                y = event_data.get("y", 0)
                await self.page.mouse.click(x, y)
            elif msg_type == "type":
                text = event_data.get("text", "")
                if text:
                    for char in text:
                        await self.page.keyboard.type(char, delay=10)
            elif msg_type == "keydown":
                key = event_data.get("key", "")
                if key:
                    await self.page.keyboard.press(key)
        except Exception as e:
            logger.error(f"Error handling browser event: {e}")

    async def extract_cookies(self) -> Tuple[Optional[str], Optional[str]]:
        if not self.context:
            return None, None
            
        cookies = await self.context.cookies()
        secure_1psid = None
        secure_1psidts = None
        
        for cookie in cookies:
            if "google" in cookie.get("domain", ""):
                if cookie["name"] == "__Secure-1PSID":
                    secure_1psid = cookie["value"]
                elif cookie["name"] == "__Secure-1PSIDTS":
                    secure_1psidts = cookie["value"]
                    
        return secure_1psid, secure_1psidts

    async def stop(self):
        if not self.is_running:
            return
            
        logger.info("Stopping remote browser session...")
        if self.context:
            await self.context.close()
        if self.browser:
            await self.browser.close()
        if self.playwright:
            await self.playwright.stop()
            
        self.context = None
        self.browser = None
        self.playwright = None
        self.page = None
        self.is_running = False

# Global instance
browser_manager = RemoteBrowserManager()
