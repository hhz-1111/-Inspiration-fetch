from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from .config import settings
from .db import upsert_auth
from .douyin_client import DouyinClient, context_cookie_str, ensure_profile_cookies
from .platforms import PlatformConfig, get_platform


@dataclass
class LoginSession:
    id: str
    user_id: int
    platform: str
    status: str = "checking"
    message: str = "正在打开登录页面"
    qr_image: bytes | None = None
    playwright: Playwright | None = None
    browser: Browser | None = None
    context: BrowserContext | None = None
    page: Page | None = None
    monitor: asyncio.Task[None] | None = field(default=None, repr=False)
    # CDP mode: the login page lives in the user's real Chrome; on cleanup we
    # must only disconnect, never close that browser or its contexts.
    cdp: "CDPBrowserManager | None" = field(default=None, repr=False)


class AuthManager:
    def __init__(self) -> None:
        self.sessions: dict[str, LoginSession] = {}
        self.platform_sessions: dict[tuple[int, str], str] = {}
        self.locks: dict[tuple[int, str], asyncio.Lock] = {}

    def profile_dir(self, user_id: int, platform: str) -> Path:
        path = settings.auth_dir / str(user_id) / f"{platform}_profile"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def storage_path(self, user_id: int, platform: str) -> Path:
        return self.profile_dir(user_id, platform) / "storage.json"

    def verified_path(self, user_id: int, platform: str) -> Path:
        return self.profile_dir(user_id, platform) / ".logged_in"

    async def status(self, user_id: int, platform_key: str) -> dict[str, str | None]:
        config = get_platform(platform_key)
        key = (user_id, platform_key)
        session_id = self.platform_sessions.get(key)
        if session_id and (session := self.sessions.get(session_id)) and session.status not in {"expired", "failed", "cancelled"}:
            return self._response(session.status, session.message, session.id)

        path = self.storage_path(user_id, platform_key)
        marker = self.verified_path(user_id, platform_key)
        if not marker.exists():
            return self._response("qr_required", "尚未连接平台账号")
        valid = await self._validate(config, user_id, platform_key)
        if valid:
            upsert_auth(user_id, platform_key, "authenticated", str(path), "登录状态有效")
            return self._response("authenticated", "账号已连接")
        upsert_auth(user_id, platform_key, "expired", str(path), "登录状态已过期")
        return self._response("expired", "登录状态已过期，请重新扫码")

    async def start(self, user_id: int, platform_key: str) -> LoginSession:
        config = get_platform(platform_key)
        key = (user_id, platform_key)
        lock = self.locks.setdefault(key, asyncio.Lock())
        async with lock:
            existing_id = self.platform_sessions.get(key)
            if existing_id and (existing := self.sessions.get(existing_id)) and existing.status not in {"expired", "failed", "cancelled", "authenticated"}:
                return existing
            session = LoginSession(id=uuid4().hex, user_id=user_id, platform=platform_key)
            self.sessions[session.id] = session
            self.platform_sessions[key] = session.id
            session.monitor = asyncio.create_task(self._run_login(session, config))
            return session

    def get_session(self, session_id: str) -> LoginSession | None:
        return self.sessions.get(session_id)

    async def logout(self, user_id: int, platform_key: str) -> None:
        import shutil
        get_platform(platform_key)
        profile = self.profile_dir(user_id, platform_key)
        shutil.rmtree(str(profile), ignore_errors=True)
        session_id = self.platform_sessions.pop((user_id, platform_key), None)
        if session_id and (session := self.sessions.pop(session_id, None)):
            await self._close(session)
        upsert_auth(user_id, platform_key, "qr_required", None, "登录状态已清除")

    async def shutdown(self) -> None:
        await asyncio.gather(*(self._close(session) for session in self.sessions.values()), return_exceptions=True)

    async def remove_user(self, user_id: int) -> None:
        targets = [session for session in self.sessions.values() if session.user_id == user_id]
        await asyncio.gather(*(self._close(session) for session in targets), return_exceptions=True)
        for session in targets:
            self.sessions.pop(session.id, None)
            self.platform_sessions.pop((user_id, session.platform), None)

    async def _run_login(self, session: LoginSession, config: PlatformConfig) -> None:
        try:
            session.message = "正在启动安全浏览器"
            session.playwright = await asyncio.wait_for(async_playwright().start(), timeout=15)
            # CDP mode (upstream default): open the login page inside the user's
            # real Chrome so the login state lives in their daily browser.
            if config.key == "douyin" and settings.douyin_cdp_enabled:
                from .cdp_browser import CDPBrowserManager
                cdp = CDPBrowserManager(settings.douyin_cdp_url, settings.douyin_cdp_connect_timeout)
                try:
                    session.context = await cdp.connect(session.playwright)
                    session.cdp = cdp
                    session.message = "已在你的 Chrome 中打开登录页面（CDP 模式）"
                except Exception as exc:
                    import logging as _logging
                    _logging.getLogger("auth").warning("CDP 登录不可用，回退独立登录窗口：%s", exc)
                    if cdp:
                        await cdp.disconnect()
                    session.cdp = None
                    session.context = None
            if session.context is None:
                profile_path = self.profile_dir(session.user_id, config.key)
                # MediaCrawler-style: launch_persistent_context keeps EVERYTHING (cookies, localStorage, IndexedDB)
                session.context = await asyncio.wait_for(
                    session.playwright.chromium.launch_persistent_context(
                        user_data_dir=str(profile_path),
                        headless=settings.headless,
                        viewport={"width": 1440, "height": 900},
                        locale="zh-CN",
                    ),
                    timeout=20,
                )
            disconnected = asyncio.Event()
            session.page = await session.context.new_page()
            session.message = "正在加载平台登录页面"
            await session.page.goto(config.login_url, wait_until="domcontentloaded", timeout=45_000)
            title = await session.page.title()
            if "验证码中间页" in title or "安全验证" in title:
                if settings.headless:
                    raise RuntimeError(f"{config.label}触发了平台安全验证，当前无头浏览器无法取得登录二维码")
                session.message = f"{config.label}要求安全验证，请先在打开的浏览器中完成验证"
            session.message = "正在识别登录二维码"
            await self._open_login_panel(session.page)
            # Login success must not depend on QR screenshot detection. Douyin can render the
            # login UI in elements that do not match our QR selectors while still setting cookies.
            baseline_cookies = await self._auth_cookie_values(session.context, config)
            qr_seen = False
            qr_cookie_values: dict[str, str] | None = None
            qr_deadline = time.monotonic() + 90
            retry_open_at = 0
            success_checks = 0
            for attempt in range(600):
                if disconnected.is_set() or not session.context:
                    session.status = "cancelled"
                    session.message = "登录窗口已关闭，账号未连接"
                    return
                image = await self._capture_qr(session.page, config)
                if disconnected.is_set() or not session.context:
                    session.status = "cancelled"
                    session.message = "登录窗口已关闭，账号未连接"
                    return
                if image:
                    session.qr_image = image
                    session.status = "waiting_scan"
                    session.message = "请在打开的浏览器中扫码或完成其他登录方式"
                    if not qr_seen:
                        # Cookies written while the login page itself is booting are anonymous
                        # session data. Use the first visible QR state as the baseline instead.
                        qr_cookie_values = await self._auth_cookie_values(session.context, config)
                    qr_seen = True
                else:
                    if not qr_seen:
                        session.status = "checking"
                        session.message = "正在等待平台加载登录二维码"
                        if attempt >= retry_open_at:
                            await self._open_login_panel(session.page)
                            retry_open_at = attempt + 4
                page_authenticated = await self._page_authenticated(session.page, config)
                current_cookies = await self._auth_cookie_values(session.context, config)
                auth_changed_after_qr = bool(qr_seen and qr_cookie_values is not None and current_cookies
                                             and current_cookies != qr_cookie_values)
                # A login completed in a popup, iframe, or a new tab still grants fresh auth
                # cookies on the shared browser context. Cookie names that were absent when the
                # login page first loaded are a reliable success signal for that path.
                new_login_cookie = bool(current_cookies) and any(
                    name not in baseline_cookies for name in current_cookies
                )
                # A platform may refresh an anonymous web_session cookie while merely opening
                # the login panel. Treating that change as a login made the UI falsely report
                # "账号已连接" before the user had scanned anything.
                success_checks = success_checks + 1 if page_authenticated or auth_changed_after_qr or new_login_cookie else 0
                if success_checks >= 2:
                    path = self.storage_path(session.user_id, config.key)
                    if session.cdp:
                        # CDP: the login lives in the user's real Chrome; never
                        # export their whole cookie jar. But mirror ONLY the
                        # platform-domain cookies into storage.json so the headless
                        # fallback (debug browser closed) still has a fresh session.
                        self.verified_path(session.user_id, config.key).write_text("cdp-login-v1", encoding="utf-8")
                        try:
                            platform_domain = config.key + ".com" if config.key != "xiaohongshu" else "xiaohongshu.com"
                            all_cookies = await session.context.cookies()
                            platform_cookies = [
                                c for c in all_cookies
                                if c.get("name") and c.get("value") and platform_domain in str(c.get("domain", ""))
                            ]
                            path.parent.mkdir(parents=True, exist_ok=True)
                            path.write_text(
                                json.dumps({"cookies": platform_cookies, "origins": []}),
                                encoding="utf-8",
                            )
                        except Exception as exc:
                            import logging as _logging
                            _logging.getLogger("auth").warning("CDP 登录镜像 cookies 失败：%s", exc)
                        session.status = "authenticated"
                        session.message = "扫码成功，已连接你的 Chrome 登录态"
                        upsert_auth(session.user_id, config.key, "authenticated", str(path), session.message)
                    else:
                        # Persistent context auto-saves to profile directory.
                        # Also export storage_state.json for the crawler's cookie extraction.
                        await session.context.storage_state(path=path, indexed_db=True)
                        self.verified_path(session.user_id, config.key).write_text("persistent-login-v3", encoding="utf-8")
                        session.status = "authenticated"
                        session.message = "扫码成功，登录状态已保存"
                        upsert_auth(session.user_id, config.key, "authenticated", str(path), session.message)
                    return
                if attempt % 20 == 19:
                    print(f"[auth] watching {config.key}: qr_seen={qr_seen} page_auth={page_authenticated} "
                          f"cookies={sorted(current_cookies)} new_cookie={new_login_cookie}", flush=True)
                if not qr_seen and time.monotonic() >= qr_deadline:
                    raise RuntimeError(f"等待{config.label}登录二维码或登录状态超时，请检查网络或平台安全验证")
                await asyncio.sleep(0.5)
            session.status = "expired"
            session.message = "二维码已过期，请重新获取"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if session.context:
                session.status = "failed"
                session.message = f"平台登录失败：{exc}"
        finally:
            await self._close_browser(session)

    async def _validate(self, config: PlatformConfig, user_id: int, platform_key: str) -> bool:
        # MediaCrawler-style: validate the session. CDP mode first (live user
        # Chrome), then the persistent login profile (headless for status checks).
        # Douyin checks the localStorage marker and confirms with a real API
        # probe (pong); XHS uses DOM-based avatar detection.
        playwright = await async_playwright().start()
        cdp = None
        ctx = None
        try:
            if config.key == "douyin" and settings.douyin_cdp_enabled:
                from .cdp_browser import CDPBrowserManager
                cdp = CDPBrowserManager(settings.douyin_cdp_url, settings.douyin_cdp_connect_timeout)
                try:
                    ctx = await cdp.connect(playwright)
                except Exception as exc:
                    import logging as _logging
                    _logging.getLogger("auth").warning("CDP 校验失败，回退 headless profile：%s", exc)
                    if cdp:
                        await cdp.disconnect()
                    cdp = None
                    ctx = None
            if ctx is None:
                ctx = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(self.profile_dir(user_id, platform_key)),
                    headless=True,
                    viewport={"width": 1440, "height": 900},
                    locale="zh-CN",
                )
            page = await ctx.new_page()
            if cdp is None:
                await ensure_profile_cookies(ctx, self.storage_path(user_id, platform_key))
            await page.goto(config.home_url, wait_until="domcontentloaded", timeout=30_000)
            await page.wait_for_timeout(2_000)

            if config.key == "douyin":
                try:
                    ls = await page.evaluate("() => window.localStorage")
                    if (ls or {}).get("HasUserLogin") != "1":
                        return False
                    live_cookie = await context_cookie_str(ctx, domains=("douyin.com",) if cdp else None)
                    if not live_cookie:
                        return False
                    client = await DouyinClient.from_page(live_cookie, page)
                    return await client.pong()
                except Exception:
                    return False

            # XHS fallback: DOM-based avatar detection
            return await self._page_authenticated(page, config)
        except Exception:
            return False
        finally:
            if cdp:
                await cdp.disconnect()
            elif ctx:
                await ctx.close()
            await playwright.stop()

    @staticmethod
    async def _open_login_panel(page: Page) -> None:
        for label in ("登录", "扫码登录"):
            locator = page.get_by_text(label, exact=True).first
            try:
                if await locator.is_visible(timeout=1_500):
                    await locator.click()
                    await page.wait_for_timeout(800)
                    return
            except Exception:
                continue

    @staticmethod
    async def _capture_qr(page: Page, config: PlatformConfig) -> bytes | None:
        for selector in config.qr_selectors:
            try:
                locator = page.locator(selector).first
                if await locator.is_visible(timeout=800):
                    return await locator.screenshot(type="png")
            except Exception:
                continue
        return None

    @staticmethod
    async def _page_authenticated(page: Page, config: PlatformConfig) -> bool:
        try:
            login_visible = await page.get_by_text("登录", exact=True).first.is_visible(timeout=400)
        except Exception:
            login_visible = False
        if login_visible:
            return False
        selectors = (
            ("[data-e2e='user-avatar'] img", "[data-e2e='user-info'] img", "header img[class*='avatar']", "[class*='header'] img[class*='avatar']")
            if config.key == "douyin" else
            ("header img[class*='avatar']", "[class*='header'] img[class*='avatar']", "[class*='user'] img[class*='avatar']", "a[href*='/user/profile'] img")
        )
        for selector in selectors:
            try:
                locator = page.locator(selector).first
                if not await locator.is_visible(timeout=400):
                    continue
                box = await locator.bounding_box()
                if box and box["y"] < 180:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    async def _has_auth_cookie(context: BrowserContext, config: PlatformConfig) -> bool:
        return bool(await AuthManager._auth_cookie_values(context, config))

    @staticmethod
    async def _auth_cookie_values(context: BrowserContext, config: PlatformConfig) -> dict[str, str]:
        cookies = await context.cookies()
        return {
            cookie["name"]: cookie["value"]
            for cookie in cookies
            if cookie.get("name") in config.auth_cookie_names and cookie.get("value")
        }

    async def _close(self, session: LoginSession) -> None:
        current = asyncio.current_task()
        if session.monitor and session.monitor is not current and not session.monitor.done():
            session.monitor.cancel()
        await self._close_browser(session)

    @staticmethod
    async def _close_browser(session: LoginSession) -> None:
        if session.cdp:
            # CDP: only disconnect — never close the user's browser or contexts.
            await session.cdp.disconnect()
            session.cdp = None
            session.context = None
            session.page = None
        else:
            if session.context:
                try:
                    await session.context.close()
                except Exception:
                    pass
                session.context = None
            session.page = None
        if session.playwright:
            await session.playwright.stop()
            session.playwright = None

    @staticmethod
    def _response(status: str, message: str, session_id: str | None = None) -> dict[str, str | None]:
        return {"status": status, "message": message, "session_id": session_id}


auth_manager = AuthManager()
