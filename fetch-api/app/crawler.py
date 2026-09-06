"""MediaCrawler-inspired crawler.
- Douyin: pure API calls via DouyinClient (A-Bogus via execjs + douyin.js)
- XHS: pure API calls via XHSClient (X-S/X-T via xhshow)
"""
from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx
from playwright.async_api import async_playwright

from .auth import auth_manager
from .config import settings
from .db import add_shared_link_video, add_video, is_video_duplicate, update_task
from .douyin_client import (
    DouyinAPIError, DouyinClient, context_cookie_str, ensure_profile_cookies, load_storage_state,
)
from .proxy_pool import ProxyPool
from .xhs_client import XHSClient, XHSAPIError, load_cookies
from .platforms import format_video, get_platform

logger = logging.getLogger("crawler")

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
CHINA_TZ = timezone(datetime.now().astimezone().utcoffset())
RANGE_DAYS = {"today": 1, "week": 7, "month": 30, "three_months": 90, "six_months": 180, "year": 365}
PUBLISH_TIME_MAP = {"today": 1, "week": 7, "month": 30, "three_months": 90, "six_months": 180, "year": 365, "any": 0, "older": 0}
ITEM_DELAYS = settings.crawl_delays
JITTER_LOW, JITTER_HIGH = 0.7, 1.5
BATCH_PAUSE_MIN, BATCH_PAUSE_MAX = 1.0, 2.5
PROXY_POOL = ProxyPool(settings.crawl_proxies)

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"

STEALTH_INIT = """() => {
    delete Object.getPrototypeOf(navigator).webdriver;
    window.chrome = {runtime: {}, loadTimes: function(){}, csi: function(){}};
    Object.defineProperty(navigator, 'plugins', {get: () => [1, 2, 3, 4, 5]});
    Object.defineProperty(navigator, 'languages', {get: () => ['zh-CN', 'zh', 'en']});
    const _q = window.navigator.permissions.query;
    window.navigator.permissions.query = p => p.name === 'notifications' ? Promise.resolve({state: Notification.permission}) : _q(p);
}"""
BLOCK_PATTERNS = re.compile(r"\.(png|jpg|jpeg|gif|svg|webp|ico|bmp|css|woff2?|ttf|eot)(\?.*)?$", re.IGNORECASE)

MAX_SCROLL_BATCHES = 40
MAX_STALE_BATCHES = 4
BATCH_TIMEOUT = 10
SEARCH_API_PATTERN_XHS = "/api/sns/web/v1/search/notes"


async def _jittered_sleep(base_seconds: float) -> None:
    """Sleep with human-like variance around the configured base delay."""
    if base_seconds <= 0:
        return
    await asyncio.sleep(base_seconds * random.uniform(JITTER_LOW, JITTER_HIGH))


async def _batch_pause() -> None:
    """Small randomized pause between API page requests."""
    await asyncio.sleep(random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX))


MAX_RETRIES = 3
RETRYABLE_ERRORS = (DouyinAPIError, XHSAPIError, httpx.HTTPError)


async def _call_with_retry(coro_factory, description: str):
    """MediaCrawler-style: retry transient API failures with exponential backoff."""
    last_error: Exception | None = None
    for attempt in range(MAX_RETRIES):
        try:
            return await coro_factory()
        except RETRYABLE_ERRORS as exc:
            last_error = exc
            if attempt < MAX_RETRIES - 1:
                delay = (2 ** attempt) * random.uniform(0.8, 1.6)
                logger.warning("%s 失败（第 %d 次）：%s，%.1fs 后重试", description, attempt + 1, exc, delay)
                await asyncio.sleep(delay)
    raise last_error  # type: ignore[misc]


# ===========================================================================
# Douyin — pure API (MediaCrawler-style)
# ===========================================================================
async def _crawl_douyin(
    task_id: str, user_id: int, config: PlatformConfig,
    keywords: list[str], count: int, created_time: str, min_likes: int, speed: str,
) -> tuple[int, int]:
    storage_path = auth_manager.storage_path(user_id, "douyin")
    state = load_storage_state(storage_path)
    if not state.get("cookie_str"):
        raise RuntimeError("请先在采集页连接抖音账号")

    # MediaCrawler-style session bootstrap, in order of preference:
    # 1. CDP mode (upstream default): reuse the user's real Chrome over the
    #    DevTools protocol — live login state + real fingerprint.
    # 2. Persistent login profile (headless): same profile as login.
    # 3. Storage snapshot fallback if no browser session can start.
    proxy = PROXY_POOL.pick()
    client: DouyinClient | None = None
    pw = None
    ctx = None
    cdp = None
    try:
        pw = await async_playwright().start()
        if settings.douyin_cdp_enabled:
            from .cdp_browser import CDPBrowserManager
            cdp = CDPBrowserManager(settings.douyin_cdp_url, settings.douyin_cdp_connect_timeout)
            try:
                ctx = await cdp.connect(pw)
                page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45_000)
                await page.wait_for_timeout(3_000)
                live_cookie = await context_cookie_str(ctx, domains=("douyin.com",))
                client = await DouyinClient.from_page(live_cookie, page, proxy=proxy)
                logger.info("抖音采集使用 CDP 会话（%s）", settings.douyin_cdp_url)
            except Exception as exc:
                logger.warning("CDP 连接失败，回退到 headless profile 模式：%s", exc)
                if cdp:
                    await cdp.disconnect()
                cdp = None
                ctx = None
        if client is None:
            ctx = await pw.chromium.launch_persistent_context(
                user_data_dir=str(auth_manager.profile_dir(user_id, "douyin")),
                headless=settings.douyin_crawl_headless,
                viewport={"width": 1440, "height": 900},
                user_agent=UA,
                locale="zh-CN",
            )
            page = await ctx.new_page()
            await ensure_profile_cookies(ctx, storage_path)
            await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=45_000)
            await page.wait_for_timeout(3_000)
            live_cookie = await context_cookie_str(ctx)
            client = await DouyinClient.from_page(live_cookie, page, proxy=proxy)
    except Exception as exc:
        logger.warning("抖音浏览器会话启动失败，回退到存储快照：%s", exc)
        client = DouyinClient(
            cookie_str=state["cookie_str"], ms_token=state["ms_token"], proxy=proxy,
        )
    finally:
        # CDP: only disconnect (never close the user's browser). Otherwise close
        # the throwaway browser before the crawl loop so it never lingers.
        if cdp:
            await cdp.disconnect()
        elif ctx:
            await ctx.close()
        if pw:
            await pw.stop()

    collected = 0
    inspected = 0
    seen: set[str] = set()
    pub_time = PUBLISH_TIME_MAP.get(created_time, 0)

    for kw_idx, keyword in enumerate(keywords):
        if collected >= count:
            break
        pct = 5 + int(kw_idx / max(len(keywords), 1) * 80)
        update_task(task_id, "running", min(pct, 95), f"正在搜索：{keyword}")

        offset = 0
        while collected < count:
            try:
                batch = await _call_with_retry(
                    lambda: client.search_videos(keyword, offset=offset, count=15, publish_time=pub_time),
                    f"抖音搜索 {keyword!r}",
                )
            except DouyinAPIError as exc:
                logger.warning("Douyin search '%s': %s", keyword, exc)
                break
            if not batch:
                break

            batch_new = 0
            for raw in batch:
                if collected >= count:
                    break
                try:
                    video = format_video("douyin", raw, keyword)
                    if not video or not video.get("title") or not video.get("url"):
                        continue
                    identity = video["url"]
                    if identity in seen:
                        continue
                    seen.add(identity)
                    inspected += 1
                    if is_video_duplicate(user_id, identity):
                        continue

                    if min_likes > 0 and (video.get("likes") is None or video["likes"] < min_likes):
                        continue
                    if created_time != "any" and video.get("published_at"):
                        if not _date_filter(video["published_at"], created_time):
                            continue

                    add_video(task_id, video)
                    collected += 1
                    batch_new += 1
                    progress_pct = min(95, 10 + int(collected / max(count, 1) * 85))
                    update_task(task_id, "running", progress_pct, f"已获取 {collected}/{count} 条，检查 {inspected} 条")
                    if ITEM_DELAYS.get(speed, 0) and collected < count:
                        await _jittered_sleep(ITEM_DELAYS[speed])
                except Exception as exc:
                    # Upstream-style: a single broken item must not abort the whole batch
                    logger.warning("抖音单条视频处理失败，跳过：%s", exc)

            if batch_new == 0:
                break
            offset += len(batch)
            await _batch_pause()

    return collected, inspected


# ===========================================================================
# XHS — pure API (xhshow signatures)
# ===========================================================================
async def _crawl_xhs(
    task_id: str, user_id: int, config: PlatformConfig,
    keywords: list[str], count: int, created_time: str, min_likes: int, speed: str,
) -> tuple[int, int]:
    storage_path = auth_manager.storage_path(user_id, "xiaohongshu")
    cookie_str = load_cookies(storage_path)
    if not cookie_str:
        raise RuntimeError("请先在采集页连接小红书账号")

    from .xhs_client import get_search_id

    client = XHSClient(cookie_str=cookie_str, timeout=30, proxy=PROXY_POOL.pick())
    collected = 0
    inspected = 0
    seen: set[str] = set()

    for kw_idx, keyword in enumerate(keywords):
        if collected >= count:
            break
        pct = 5 + int(kw_idx / max(len(keywords), 1) * 80)
        update_task(task_id, "running", min(pct, 95), f"正在搜索：{keyword}")

        search_id = get_search_id()
        page = 1
        while collected < count:
            try:
                items = await _call_with_retry(
                    lambda: client.search_videos(keyword, page=page, page_size=20, search_id=search_id),
                    f"小红书搜索 {keyword!r}",
                )
            except XHSAPIError as exc:
                # Upstream-style: a rate-limited/blocked keyword is skipped and the
                # crawl continues with the next keyword instead of aborting.
                logger.warning("小红书搜索 %r 失败，跳过该关键词继续：%s", keyword, exc)
                break
            if not items:
                break

            batch_new = 0
            for raw in items:
                if collected >= count:
                    break
                try:
                    note = raw.get("note_card") or raw
                    video = format_video("xiaohongshu", note, keyword) if isinstance(note, dict) else None
                    if not video or not video.get("title") or not video.get("url"):
                        continue
                    identity = video["url"]
                    if identity in seen:
                        continue
                    seen.add(identity)
                    inspected += 1
                    if is_video_duplicate(user_id, identity):
                        continue

                    if min_likes > 0 and (video.get("likes") is None or video["likes"] < min_likes):
                        continue
                    if created_time != "any" and video.get("published_at"):
                        if not _date_filter(video["published_at"], created_time):
                            continue

                    add_video(task_id, video)
                    collected += 1
                    batch_new += 1
                    progress_pct = min(95, 10 + int(collected / max(count, 1) * 85))
                    update_task(task_id, "running", progress_pct, f"已获取 {collected}/{count} 条，检查 {inspected} 条")
                    if ITEM_DELAYS.get(speed, 0) and collected < count:
                        await _jittered_sleep(ITEM_DELAYS[speed])
                except Exception as exc:
                    # Upstream-style: a single broken item must not abort the whole batch
                    logger.warning("小红书单条视频处理失败，跳过：%s", exc)

            if batch_new == 0:
                break
            page += 1
            await _batch_pause()

    return collected, inspected


# ===========================================================================
# Helpers
# ===========================================================================
def _date_filter(published_iso: str, created_time_filter: str) -> bool:
    if created_time_filter == "any":
        return True
    days = RANGE_DAYS.get(created_time_filter)
    if days is None:
        return True
    try:
        dt = datetime.fromisoformat(published_iso)
    except ValueError:
        return True
    cutoff = datetime.now(tz=dt.tzinfo) - timedelta(days=days)
    return dt >= cutoff


def _extract_id_from_url(url: str, platform_key: str) -> str | None:
    if platform_key == "douyin":
        for pat in (r"/video/(\d+)", r"/note/(\d+)", r"modal_id=(\d+)", r"aweme_id=(\d+)", r"/share/video/(\d+)", r"/share/note/(\d+)"):
            m = re.search(pat, url)
            if m:
                return m.group(1)
    else:
        for pat in (r"/explore/([0-9a-zA-Z]+)", r"/discovery/item/([0-9a-zA-Z]+)", r"note_id=([0-9a-zA-Z]+)", r"/a/([0-9a-zA-Z]+)"):
            m = re.search(pat, url)
            if m:
                return m.group(1)
    return None


SHARE_URL_RE = re.compile(r"https?://[^\s，。！？、；;\"'<>【】（）()]+", re.IGNORECASE)
DOUYIN_HOST_MARKERS = ("douyin.com", "iesdouyin.com", "douyinvod.com", "amemv.com")
XHS_HOST_MARKERS = ("xiaohongshu.com", "xhslink.com", "xhslink.cn", "redbook.com")


def _extract_share_url(value: str) -> str | None:
    """Pull the first http(s) URL out of pasted share text (title + link + copy)."""
    match = SHARE_URL_RE.search(value or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,;:!?)]}")
    return url or None


def _detect_platform_from_url(url: str) -> str | None:
    lower = (url or "").lower()
    if any(marker in lower for marker in DOUYIN_HOST_MARKERS):
        return "douyin"
    if any(marker in lower for marker in XHS_HOST_MARKERS):
        return "xiaohongshu"
    return None


async def _resolve_share_redirect(url: str) -> str:
    """Follow a short share link (v.douyin.com / xhslink.com / …) to its final URL."""
    try:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(20), follow_redirects=True,
            headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
        ) as client:
            resp = await client.get(url)
            if resp.status_code < 400:
                final = str(resp.url)
                if final.startswith("http"):
                    return final
    except Exception:
        pass
    return url


def _auth_storage_file(user_id: int, platform_key: str) -> Any | None:
    """Locate the platform cookie snapshot.

    Current layout keeps it at ``{auth_dir}/{user_id}/{platform}_profile/storage.json``;
    sessions created by older versions of the app live directly at
    ``{auth_dir}/{user_id}/{platform}.json`` — read whichever exists.
    """
    primary = auth_manager.storage_path(user_id, platform_key)
    if primary.exists():
        return primary
    legacy = settings.auth_dir / str(user_id) / f"{platform_key}.json"
    return legacy if legacy.exists() else None


def _topic_keyword(text: str) -> str:
    """First real topic tag (#话题) from a title/description, else the manual bucket."""
    for match in re.finditer(r"[#＃]([^#＃\s，。！？、；：,.;!?]+)", text or ""):
        tag = match.group(1).strip()
        if tag and tag not in {"小红书", "抖音", "话题", "标签"}:
            return tag[:80]
    return ""


async def _session_valid(user_id: int, platform_key: str, storage_path) -> bool:
    """MediaCrawler-style: validate session via persistent profile.

    Douyin adds an API probe (pong) on top of the localStorage marker so that
    stale cookies are detected before a crawl starts.
    """
    try:
        if platform_key == "douyin":
            # MediaCrawler-style: validate the session. CDP mode first (live
            # user Chrome), then the persistent login profile. The localStorage
            # marker alone can survive cookie expiry, so confirm with a real API
            # probe (pong) using the live page msToken.
            pw = await async_playwright().start()
            cdp = None
            ctx = None
            try:
                if settings.douyin_cdp_enabled:
                    from .cdp_browser import CDPBrowserManager
                    cdp = CDPBrowserManager(settings.douyin_cdp_url, settings.douyin_cdp_connect_timeout)
                    try:
                        ctx = await cdp.connect(pw)
                        page = ctx.pages[0] if ctx.pages else await ctx.new_page()
                    except Exception as exc:
                        logger.warning("CDP 校验失败，回退 headless profile：%s", exc)
                        if cdp:
                            await cdp.disconnect()
                        cdp = None
                        ctx = None
                if ctx is None:
                    ctx = await pw.chromium.launch_persistent_context(
                        user_data_dir=str(auth_manager.profile_dir(user_id, platform_key)),
                        headless=settings.douyin_crawl_headless,
                        viewport={"width": 1440, "height": 900},
                        user_agent=UA, locale="zh-CN",
                    )
                    page = await ctx.new_page()
                    await ensure_profile_cookies(ctx, storage_path)
                await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=30_000)
                await page.wait_for_timeout(2_000)
                ls = await page.evaluate("() => window.localStorage")
                if (ls or {}).get("HasUserLogin") != "1":
                    return False
                live_cookie = await context_cookie_str(ctx, domains=("douyin.com",) if cdp else None)
                if not live_cookie:
                    return False
                client = await DouyinClient.from_page(live_cookie, page, proxy=PROXY_POOL.pick())
                return await client.pong()
            finally:
                if cdp:
                    await cdp.disconnect()
                elif ctx:
                    await ctx.close()
                await pw.stop()
        else:
            cookie_str = load_cookies(storage_path)
            if not cookie_str:
                return False
            client = XHSClient(cookie_str=cookie_str, timeout=10)
            return await client.pong()
    except Exception:
        return False


# ===========================================================================
# Public API (same signatures as before)
# ===========================================================================
async def run_crawl(
    task_id: str, user_id: int, platform_key: str,
    keywords: list[str], count: int, created_time: str, min_likes: int, speed: str,
) -> None:
    config = get_platform(platform_key)
    storage_path = auth_manager.storage_path(user_id, platform_key)
    if not storage_path.exists():
        update_task(task_id, "failed", 0, "平台尚未授权", "请先登录平台账号")
        return

    update_task(task_id, "running", 3, "正在启动")
    if not await _session_valid(user_id, platform_key, storage_path):
        update_task(task_id, "failed", 0, "登录态已过期，请重新扫码连接账号", "登录态已过期")
        return
    try:
        if platform_key == "douyin":
            collected, inspected = await _crawl_douyin(
                task_id, user_id, config, keywords, count, created_time, min_likes, speed,
            )
        else:
            collected, inspected = await _crawl_xhs(
                task_id, user_id, config, keywords, count, created_time, min_likes, speed,
            )
    except asyncio.CancelledError:
        update_task(task_id, "cancelled", 0, "任务已取消")
        raise
    except Exception as exc:
        update_task(task_id, "failed", 0, "采集失败", str(exc))
        return

    if collected == 0:
        if inspected == 0:
            # The platform returned no candidates at all — a session or
            # risk-control problem, not a keyword problem (upstream logs
            # "账号也许被风控了" for this case).
            update_task(
                task_id, "failed", 0,
                "平台接口未返回任何候选视频，登录态可能已过期或账号被风控，请重新扫码",
                "请重新扫码连接平台账号",
            )
        else:
            detail = f"（检查了 {inspected} 条候选视频）"
            update_task(task_id, "failed", 0, f"没有找到符合条件的视频{detail}", "请尝试更换关键词或放宽条件")
    elif collected < count:
        update_task(task_id, "completed", 100, f"部分完成：获取 {collected}/{count} 条，检查 {inspected} 条")
    else:
        update_task(task_id, "completed", 100, f"采集完成，共获取 {collected} 条（检查 {inspected} 条）")


async def parse_shared_link(user_id: int, platform_key: str, shared_url: str) -> dict[str, Any]:
    """Parse a Douyin / Xiaohongshu share link (or full share text) and add the
    video straight into the user's analysis queue.

    Handles pasted share copy (title + link + trailing text), platform short
    links (v.douyin.com / xhslink.com / …), and platform auto-detection so the
    caller can pass ``auto`` instead of guessing.
    """
    raw = _extract_share_url(shared_url) or (shared_url or "").strip()
    if not raw:
        raise RuntimeError("分享文案中没有找到有效链接")

    resolved = await _resolve_share_redirect(raw)
    detected = _detect_platform_from_url(resolved) or _detect_platform_from_url(raw)
    effective = platform_key
    if effective == "auto" or effective not in {"douyin", "xiaohongshu"}:
        if not detected:
            raise RuntimeError("无法识别链接所属平台，请手动选择抖音或小红书")
        effective = detected
    elif detected and detected != platform_key:
        # The URL clearly belongs to the other platform than the one selected —
        # trust the link so users do not need to switch the dropdown first.
        effective = detected

    config = get_platform(effective)
    storage = _auth_storage_file(user_id, effective)
    if storage is None:
        raise RuntimeError(f"请先在采集页连接{config.label}账号")

    video_id = _extract_id_from_url(resolved, effective) or _extract_id_from_url(raw, effective)
    if not video_id:
        raise RuntimeError("未能从链接中识别视频 ID，请确认粘贴的是抖音/小红书视频分享链接")

    if effective == "douyin":
        state = load_storage_state(storage)
        cookie_str = state.get("cookie_str", "")
        if not cookie_str:
            raise RuntimeError("抖音登录状态为空，请先在采集页重新连接抖音账号")
        client = DouyinClient(
            cookie_str=cookie_str, ms_token=state.get("ms_token", ""), proxy=PROXY_POOL.pick(),
        )
        try:
            aweme = await _call_with_retry(
                lambda: client.video_detail(video_id), f"抖音详情 {video_id}",
            )
        except DouyinAPIError as exc:
            raise RuntimeError(
                "抖音接口暂时被风控或登录态已过期，请稍后重试或先在采集页重新连接抖音账号"
            ) from exc
        if not aweme:
            raise RuntimeError("抖音未返回该视频详情，链接可能已失效，请确认后重试")
        keyword = _topic_keyword(aweme.get("desc") or "") or "手动"
        video = format_video("douyin", {"aweme_info": aweme}, keyword)
        if not video:
            raise RuntimeError("无法解析抖音分享链接中的视频信息")
        video["url"] = f"https://www.douyin.com/video/{aweme.get('aweme_id') or video_id}"
        return add_shared_link_video(user_id, config.label, video)

    # XHS
    cookie_str = load_cookies(storage)
    if not cookie_str:
        raise RuntimeError("小红书登录状态为空，请先在采集页重新连接小红书账号")
    client = XHSClient(cookie_str=cookie_str, timeout=30, proxy=PROXY_POOL.pick())
    try:
        note_card = await client.note_detail(video_id)
    except XHSAPIError as exc:
        raise RuntimeError(f"小红书笔记解析失败：{exc}") from exc
    if not note_card:
        raise RuntimeError("小红书未返回该笔记详情，链接可能已失效，请确认后重试")
    keyword = _topic_keyword(note_card.get("display_title") or note_card.get("title") or "") or "手动"
    video = format_video("xiaohongshu", note_card, keyword)
    if not video:
        raise RuntimeError("无法解析小红书分享链接中的视频信息")
    note_id = note_card.get("note_id") or video_id
    video["url"] = f"https://www.xiaohongshu.com/explore/{note_id}"
    return add_shared_link_video(user_id, config.label, video)


async def resolve_media_url(user_id: int, video: dict[str, Any], force_refresh: bool = False) -> str | None:
    media_url = video.get("media_url")
    if media_url and not force_refresh:
        return media_url

    platform_str = video.get("platform", "")
    is_douyin = "抖音" in platform_str
    platform_key = "douyin" if is_douyin else "xiaohongshu"
    storage_path = _auth_storage_file(user_id, platform_key)
    if storage_path is None:
        return None

    video_id = _extract_id_from_url(video.get("url", ""), platform_key)
    if not video_id:
        return None

    if is_douyin:
        state = load_storage_state(storage_path)
        cookie_str = state.get("cookie_str", "")
        client = DouyinClient(
            cookie_str=cookie_str, ms_token=state.get("ms_token", ""), proxy=PROXY_POOL.pick(),
        )
        # 抖音详情接口偶发失败（限流/瞬时网络），重试几次后再放弃
        last_exc: Exception | None = None
        for attempt in range(3):
            try:
                aweme = await client.video_detail(video_id)
                play_addr = (aweme.get("video") or {}).get("play_addr") or {}
                urls = play_addr.get("url_list") or []
                if urls:
                    return str(urls[0])
            except Exception as exc:  # noqa: PERF203
                last_exc = exc
            if attempt < 2:
                await asyncio.sleep(1.5 * (attempt + 1))
        logger.warning("抖音解析播放地址失败 video=%s: %s", video_id, last_exc)
        return None

    # XHS
    cookie_str = load_cookies(storage_path)
    client = XHSClient(cookie_str=cookie_str, timeout=30, proxy=PROXY_POOL.pick())
    try:
        note_card = await client.note_detail(video_id)
        video_info = note_card.get("video") or {}
        media = video_info.get("media") or {}
        stream = media.get("stream") or {}
        codecs = stream.get("h264") or stream.get("h265") or []
        return str(codecs[0].get("master_url")) if codecs else None
    except Exception:
        return None


async def fetch_media(
    user_id: int, video: dict[str, Any], media_url: str, range_header: str | None = None,
) -> tuple[int, bytes, dict[str, str]]:
    platform = video.get("platform", "")
    referer = "https://www.douyin.com/" if "抖音" in str(platform) else "https://www.xiaohongshu.com/"
    headers = {
        "User-Agent": UA, "Referer": referer, "Accept": "*/*",
        "Accept-Encoding": "identity", "Connection": "keep-alive",
    }
    if range_header:
        headers["Range"] = range_header
    async with httpx.AsyncClient(
        proxy=PROXY_POOL.pick(), timeout=httpx.Timeout(120), follow_redirects=True,
    ) as client:
        response = await client.get(media_url, headers=headers)
        response.raise_for_status()
        resp_headers = {k.lower(): v for k, v in response.headers.items()}
        return response.status_code, response.content, resp_headers
