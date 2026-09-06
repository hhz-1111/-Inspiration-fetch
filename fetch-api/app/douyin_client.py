"""MediaCrawler-style Douyin API client.
A-Bogus signatures via PyExecJS + libs/douyin.js (same approach as upstream MediaCrawler).
msToken is extracted from browser localStorage; the Playwright page reference is kept
ONLY for that — zero DOM interaction, and the signature no longer depends on it.
"""
from __future__ import annotations

import json
import logging
import random
import urllib.parse
from pathlib import Path
from typing import Any, Optional

import httpx

try:
    import execjs
    _JS_AVAILABLE = True
except ImportError:
    _JS_AVAILABLE = False

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

HOST = "https://www.douyin.com"
LIBS_DIR = Path(__file__).resolve().parent.parent / "libs"


def _generate_web_id() -> str:
    def _e(t):
        if t is not None:
            return str(t ^ (int(16 * random.random()) >> (t // 4)))
        return ''.join([str(int(1e7)), '-', str(int(1e3)), '-', str(int(4e3)), '-', str(int(8e3)), '-', str(int(1e11))])
    web_id = ''.join(_e(int(x)) if x in '018' else x for x in _e(None))
    return web_id.replace('-', '')[:19]


class DouyinAPIError(RuntimeError):
    pass


_douyin_sign_obj: Any = None


def _looks_like_chunked(text: str) -> bool:
    """True when the body starts with an HTTP chunk-size frame (hex + CRLF)."""
    head = text.lstrip()[:32]
    import re as _re
    return bool(_re.match(r"^[0-9a-fA-F]{1,8}\r?\n", head))


def _decode_chunked(text: str) -> str:
    """Strip HTTP chunked framing from the stream-search response body.

    The douyin stream endpoint replies with Transfer-Encoding: chunked whose
    size frames are *forecast* values (tt_api_type: STREAM_FORECAST) that do not
    match the actual payload lengths, so a strict RFC chunk decoder fails. The
    JSON payload is compact (no literal CR/LF), so every `\\r\\n<hex>\\r\\n`
    sequence is a frame boundary and can be stripped safely.
    """
    import re as _re
    cleaned = _re.sub(r"^\s*[0-9a-fA-F]{1,8}\r?\n", "", text)          # leading size line
    cleaned = _re.sub(r"\r?\n[0-9a-fA-F]{1,8}\r?\n", "", cleaned)      # inter-chunk frames
    cleaned = _re.sub(r"\r?\n0\r?\n\s*$", "", cleaned)                 # terminating 0 frame
    return cleaned


def _parse_stream_json(text: str) -> dict[str, Any]:
    """Parse the stream-search body: several JSON objects are concatenated
    (data payload segments interleaved with trailing objects like time_cost),
    possibly with leftover chunk framing. Merges every `data` list segment
    and returns the combined object.
    """
    cleaned = _decode_chunked(text)
    decoder = json.JSONDecoder(strict=False)
    idx = 0
    length = len(cleaned)
    merged: dict[str, Any] | None = None
    data_list: list[Any] = []
    while idx < length:
        while idx < length and cleaned[idx] in " \t\r\n":
            idx += 1
        if idx >= length:
            break
        if cleaned[idx] != "{":
            idx += 1
            continue
        try:
            obj, idx = decoder.raw_decode(cleaned, idx)
        except json.JSONDecodeError:
            idx += 1
            continue
        if not isinstance(obj, dict) or "data" not in obj:
            continue
        if merged is None:
            merged = dict(obj)
            merged["data"] = []
        segment = obj.get("data")
        if isinstance(segment, list):
            data_list.extend(segment)
    if merged is None:
        raise DouyinAPIError(f"Invalid JSON (stream): {text[:300]}")
    merged["data"] = data_list
    return merged


def _load_sign_obj() -> Any:
    """Compile libs/douyin.js once (MediaCrawler-style A-Bogus via PyExecJS)."""
    global _douyin_sign_obj
    if _douyin_sign_obj is None:
        if not _JS_AVAILABLE:
            raise DouyinAPIError("PyExecJS 未安装，无法生成 A-Bogus 签名")
        source = LIBS_DIR.joinpath("douyin.js").read_text(encoding="utf-8-sig")
        _douyin_sign_obj = execjs.compile(source)
    return _douyin_sign_obj


class DouyinClient:
    """Douyin API client. msToken from browser localStorage, A-Bogus via execjs + douyin.js."""

    def __init__(self, cookie_str: str = "", ms_token: str = "", page=None, proxy: str | None = None, timeout: int = 30, user_agent: str | None = None):
        self._cookie = cookie_str
        self._ms_token = ms_token
        self._page = page
        self._proxy = proxy
        self._timeout = timeout
        # CDP mode must sign and send with the real browser UA (upstream uses
        # navigator.userAgent); the module constant is the headless fallback.
        self._ua = user_agent or UA
        self._webid: str | None = None

    @staticmethod
    async def from_page(cookie_str: str, page, proxy: str | None = None, user_agent: str | None = None) -> "DouyinClient":
        ls = await page.evaluate("() => window.localStorage")
        ms_token = (ls or {}).get("xmst", "")
        if user_agent is None:
            try:
                user_agent = await page.evaluate("() => navigator.userAgent")
            except Exception:
                user_agent = None
        return DouyinClient(cookie_str=cookie_str, ms_token=ms_token, page=page, proxy=proxy, user_agent=user_agent)

    async def _a_bogus(self, params_str: str) -> str:
        """MediaCrawler-style: A-Bogus via execjs + libs/douyin.js (sign_datail).

        Falls back to the deprecated page.evaluate approach only when execjs is
        unavailable; the signature no longer requires a Playwright page.
        """
        try:
            return _load_sign_obj().call("sign_datail", params_str, self._ua)
        except Exception as exc:
            logging.getLogger("douyin_client").warning("execjs A-Bogus 失败（%s），回退到 page.evaluate", exc)
        if self._page is None:
            return ""
        try:
            return await self._page.evaluate("""
                ([params, ua]) => {
                    const fn = window.byted_acrawler && window.byted_acrawler.sign;
                    if (fn) return fn({url: '?' + params});
                    try {
                        return window.bdms.init._v[2].p[42].apply(null, [0, 1, 8, params, '', ua]);
                    } catch(e) { return ''; }
                }
            """, [params_str, self._ua])
        except Exception:
            return ""

    def _web_id(self) -> str:
        if self._webid is None:
            self._webid = _generate_web_id()
        return self._webid

    def _headers(self, referer: str = HOST) -> dict[str, str]:
        # Full browser-like header set: without the sec-ch-ua / sec-fetch-* family
        # Douyin's risk control blocks plain httpx requests with "blocked".
        return {
            "User-Agent": self._ua,
            "Cookie": self._cookie,
            "Referer": referer,
            "Host": "www.douyin.com",
            "Origin": HOST,
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Accept-Encoding": "identity",
            "Content-Type": "application/json;charset=UTF-8",
            "sec-ch-ua": '"Not)A;Brand";v="99", "Google Chrome";v="131", "Chromium";v="131"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-origin",
        }

    async def _request(self, uri: str, params: dict | None = None, referer: str = HOST) -> dict:
        p = dict(params or {})
        # Match MediaCrawler's common_params
        p.setdefault("device_platform", "webapp")
        p.setdefault("aid", "6383")
        p.setdefault("channel", "channel_pc_web")
        p.setdefault("pc_client_type", "1")
        p.setdefault("version_code", "190600")
        p.setdefault("version_name", "19.6.0")
        p.setdefault("update_version_code", "170400")
        p.setdefault("cookie_enabled", "true")
        p.setdefault("browser_language", "zh-CN")
        p.setdefault("browser_platform", "Win32")
        p.setdefault("browser_name", "Chrome")
        p.setdefault("browser_version", "131.0.0.0")
        p.setdefault("browser_online", "true")
        p.setdefault("engine_name", "Blink")
        p.setdefault("engine_version", "131.0.0.0")
        p.setdefault("os_name", "Windows")
        p.setdefault("os_version", "10.0")
        p.setdefault("platform", "PC")
        p.setdefault("cpu_core_num", "16")
        p.setdefault("device_memory", "8")
        p.setdefault("screen_width", "2560")
        p.setdefault("screen_height", "1440")
        p.setdefault("effective_type", "4g")
        p.setdefault("round_trip_time", "50")
        p.setdefault("webid", self._web_id())
        if self._ms_token:
            p.setdefault("msToken", self._ms_token)

        # Build signed URL — param order must be preserved between sign and actual request.
        # Upstream MediaCrawler (fix: dy search) skips a_bogus for the general/search
        # family: adding it there makes Douyin return an empty data list.
        param_items = sorted(p.items(), key=lambda x: x[0])
        qs = urllib.parse.urlencode(param_items)
        if "/v1/web/general/search" not in uri:
            a_bogus = await self._a_bogus(qs)
            if a_bogus:
                param_items.append(("a_bogus", a_bogus))
        final_qs = urllib.parse.urlencode(param_items)
        full_url = f"{HOST}{uri}?{final_qs}"

        headers = self._headers(referer)

        async with httpx.AsyncClient(
            proxy=self._proxy, timeout=httpx.Timeout(self._timeout), follow_redirects=True,
        ) as client:
            resp = await client.get(full_url, headers=headers)
            if resp.status_code != 200:
                raise DouyinAPIError(f"API returned {resp.status_code}: {resp.text[:300]}")
            try:
                return resp.json()
            except Exception:
                text = resp.text
                if not text.strip():
                    raise DouyinAPIError("抖音返回空响应")
                if "blocked" in text[:300].lower():
                    # Upstream MediaCrawler treats a raw "blocked" body as risk control
                    raise DouyinAPIError("抖音账号被风控或登录态已过期（响应 blocked）")
                # The stream search endpoint replies with Transfer-Encoding: chunked
                # framing that httpx leaves in the body; decode it before parsing.
                if resp.headers.get("transfer-encoding", "").lower() == "chunked" or _looks_like_chunked(text):
                    try:
                        return _parse_stream_json(text)
                    except DouyinAPIError:
                        raise
                    except Exception as exc:
                        raise DouyinAPIError(f"Invalid JSON (chunked): {text[:300]}") from exc
                raise DouyinAPIError(f"Invalid JSON: {text[:300]}")

    async def search_videos(
        self, keyword: str, offset: int = 0, count: int = 15,
        publish_time: int = 0, search_id: str = "",
    ) -> list[dict[str, Any]]:
        """Search Douyin videos via the general search API, exactly matching
        upstream MediaCrawler (search_info_by_keyword): the single endpoint,
        the same query params, and NO a_bogus signature for this API family.
        """
        params: dict[str, Any] = {
            "search_channel": "aweme_video_web",
            "enable_history": "1",
            "keyword": keyword,
            "search_source": "tab_search",
            "query_correct_type": "1",
            "is_filter_search": "0",
            "from_group_id": "7378810571505847586",
            "offset": str(offset),
            "count": str(count),
            "need_filter_settings": "1",
            "list_type": "multi",
            "search_id": search_id,
        }
        if publish_time:
            params["filter_selected"] = json.dumps({"sort_type": "0", "publish_time": str(publish_time)})
            params["is_filter_search"] = "1"

        referer = urllib.parse.quote(
            f"https://www.douyin.com/search/{urllib.parse.quote(keyword)}?aid=f594bbd9-a0e2-4651-9319-ebe3cb6298c1&type=general",
            safe=":/",
        )
        data = await self._request("/aweme/v1/web/general/search/single/", params, referer=referer)
        return data.get("data") or []

    async def video_detail(self, aweme_id: str) -> dict[str, Any]:
        data = await self._request("/aweme/v1/web/aweme/detail/", {"aweme_id": aweme_id})
        return data.get("aweme_detail") or {}

    async def pong(self) -> bool:
        """Upstream MediaCrawler pong: check the localStorage login marker ONLY.

        Deliberately makes NO API request — frequent status checks and session
        probes must not inflate Douyin's risk-control pressure (repeated search
        requests from probes trigger bdturing verification and empty results).
        The real crawl will surface a dead session anyway via the search call.
        """
        if self._page is None:
            return False
        try:
            ls = await self._page.evaluate("() => window.localStorage")
            return (ls or {}).get("HasUserLogin") == "1"
        except Exception:
            return False

    async def search_all(
        self, keyword: str, max_count: int, publish_time: int = 0,
    ) -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        offset = 0
        while len(results) < max_count:
            batch = await self.search_videos(keyword, offset=offset, count=min(15, max_count - len(results)), publish_time=publish_time)
            if not batch:
                break
            results.extend(batch)
            offset += len(batch)
        return results


def load_cookies_from_storage(storage_path: Path) -> str:
    if not storage_path.exists():
        return ""
    data = json.loads(storage_path.read_text(encoding="utf-8"))
    cookies = data.get("cookies", [])
    parts = [f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")]
    return "; ".join(parts)


def load_storage_state(storage_path: Path) -> dict[str, str]:
    """Read cookie string, msToken and the login marker straight from a Playwright
    storage_state.json snapshot — no browser launch required.

    Returns {"cookie_str", "ms_token", "has_login"} (has_login is a "0"/"1" string).
    """
    if not storage_path.exists():
        return {}
    try:
        data = json.loads(storage_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    cookies = data.get("cookies", [])
    cookie_str = "; ".join(
        f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value")
    )
    ms_token = ""
    has_login = "0"
    for origin in data.get("origins", []) or []:
        for item in origin.get("localStorage") or []:
            if not isinstance(item, dict):
                continue
            name = item.get("name")
            if name == "xmst":
                ms_token = item.get("value") or ""
            elif name == "HasUserLogin":
                has_login = str(item.get("value") or "")
    return {"cookie_str": cookie_str, "ms_token": ms_token, "has_login": has_login}


async def ensure_profile_cookies(context, storage_path: Path) -> None:
    """Make sure a persistent context carries the login cookies.

    Upstream MediaCrawler reads cookies from the live browser session; on Windows,
    Playwright can fail to persist cookies to the profile directory on disk, so
    when the key auth cookie is missing we restore it from the storage snapshot.
    """
    try:
        cookies = await context.cookies()
        if any(c.get("name") == "sessionid" and c.get("value") for c in cookies):
            return
        if not storage_path.exists():
            return
        data = json.loads(storage_path.read_text(encoding="utf-8"))
        snapshot = data.get("cookies") or []
        if snapshot:
            await context.add_cookies(snapshot)
            logging.getLogger("douyin_client").warning(
                "profile 缺少登录 cookie，已从 storage 快照注入 %d 个", len(snapshot)
            )
    except Exception as exc:
        logging.getLogger("douyin_client").warning("cookie 注入失败：%s", exc)


async def context_cookie_str(context, domains: tuple[str, ...] | None = None) -> str:
    """Upstream convert_browser_context_cookies: flatten live context cookies.

    When `domains` is given, only cookies whose domain matches are included —
    needed in CDP mode where the context is the user's real browser (it carries
    cookies for every site, and upstream filters by platform domain).
    """
    cookies = await context.cookies()
    parts = []
    for c in cookies:
        if not (c.get("name") and c.get("value")):
            continue
        if domains and not any(d in str(c.get("domain", "")) for d in domains):
            continue
        parts.append(f"{c['name']}={c['value']}")
    return "; ".join(parts)
