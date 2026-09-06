"""CDP browser manager — upstream MediaCrawler tools/cdp_browser.py pattern.

Connects to the user's existing Chrome over the DevTools protocol and reuses
its default browser context (live login state, real fingerprint) for the
lowest risk-control odds. Only disconnects on cleanup — never closes the
user's browser or its contexts.
"""
from __future__ import annotations

import logging
import socket
from typing import Any

import httpx
from playwright.async_api import Browser, BrowserContext, Playwright

logger = logging.getLogger("cdp_browser")


class CDPConnectionError(RuntimeError):
    pass


class CDPBrowserManager:
    def __init__(self, cdp_url: str, connect_timeout: int = 30) -> None:
        # e.g. http://127.0.0.1:9222
        self.cdp_url = cdp_url.rstrip("/")
        self.connect_timeout = connect_timeout
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None

    @property
    def debug_port(self) -> int:
        return int(self.cdp_url.rsplit(":", 1)[-1])

    async def connect(self, playwright: Playwright) -> BrowserContext:
        """Wait for the CDP port, connect, and reuse the existing default context."""
        await self._wait_for_port()
        ws_url = f"ws://127.0.0.1:{self.debug_port}/devtools/browser"
        logger.info("正在连接已有 Chrome（CDP）：%s", ws_url)
        try:
            # Chrome 136+ does not expose /json/version; connect directly
            self.browser = await playwright.chromium.connect_over_cdp(
                ws_url, timeout=self.connect_timeout * 1000,
            )
        except Exception:
            # 404 on the direct path is expected on modern Chrome; discovery is
            # the normal fallback, not an error worth a warning.
            logger.info("直连路径不可用，改用 /json/version 发现…")
            discovered = await self._discover_ws_url()
            self.browser = await playwright.chromium.connect_over_cdp(
                discovered, timeout=self.connect_timeout * 1000,
            )
        if not self.browser.is_connected():
            raise CDPConnectionError("CDP 连接失败")
        contexts = self.browser.contexts
        if contexts:
            # Reuse the user's default context: it carries the live login state
            self.context = contexts[0]
            logger.info("复用已有浏览器上下文（%d 个标签页）", len(self.context.pages))
        else:
            self.context = await self.browser.new_context()
            logger.info("已在浏览器中创建新上下文")
        return self.context

    async def _wait_for_port(self) -> None:
        """Quick probe — fail fast so a crawl falls back to the headless mode
        immediately when remote debugging is not enabled. Enable debugging first
        (chrome://inspect/#remote-debugging), then crawl again to use CDP."""
        try:
            with socket.create_connection(("127.0.0.1", self.debug_port), timeout=3):
                return
        except OSError:
            raise CDPConnectionError(
                f"无法连接 {self.cdp_url}：请先在 Chrome 地址栏打开 "
                f"chrome://inspect/#remote-debugging 并勾选 \"Allow remote debugging for this browser instance\"，"
                f"或使用 --remote-debugging-port={self.debug_port} 启动 Chrome"
            )

    async def _discover_ws_url(self) -> str:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(f"{self.cdp_url}/json/version")
            resp.raise_for_status()
            ws_url = resp.json().get("webSocketDebuggerUrl")
            if not ws_url:
                raise CDPConnectionError("webSocketDebuggerUrl not found")
            return ws_url

    async def disconnect(self) -> None:
        """Disconnect the CDP session. NEVER closes the user's browser."""
        if self.context is not None:
            # Do not call context.close() — that would close the user's tabs.
            self.context = None
        if self.browser is not None:
            try:
                if self.browser.is_connected():
                    await self.browser.close()  # connect_over_cdp close == disconnect
            except Exception as exc:
                logger.debug("断开 CDP 连接时出错（忽略）：%s", exc)
            finally:
                self.browser = None
