"""MediaCrawler-style XHS API client.
Signatures (X-S/X-T/X-S-Common) via xhshow — zero browser needed.
Cookies extracted from Playwright storage_state.json.
"""
from __future__ import annotations

import json
import random
import time
from pathlib import Path
from typing import Any

import httpx
from xhshow import Xhshow

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)
HOST = "https://edith.xiaohongshu.com"


def get_trace_id() -> str:
    """Generate x-b3-traceid for link tracing (same as upstream MediaCrawler xhs_sign.py)."""
    return "".join(random.choice("abcdef0123456789") for _ in range(16))


class XHSAPIError(RuntimeError):
    pass


class XHSClient:
    """Pure-HTTP XHS API client with xhshow signature generation."""

    def __init__(self, cookie_str: str = "", proxy: str | None = None, timeout: int = 30):
        self._cookie = cookie_str
        self._proxy = proxy
        self._timeout = timeout
        self._xh = Xhshow()

    def _headers(self, referer: str = "https://www.xiaohongshu.com/") -> dict[str, str]:
        return {
            "accept": "application/json, text/plain, */*",
            "accept-language": "zh-CN,zh;q=0.9",
            "cache-control": "no-cache",
            "content-type": "application/json;charset=UTF-8",
            "origin": "https://www.xiaohongshu.com",
            "pragma": "no-cache",
            "referer": referer,
            "sec-ch-ua": '"Chromium";v="131", "Google Chrome";v="131", "Not.A/Brand";v="99"',
            "sec-ch-ua-mobile": "?0",
            "sec-ch-ua-platform": '"Windows"',
            "sec-fetch-dest": "empty",
            "sec-fetch-mode": "cors",
            "sec-fetch-site": "same-site",
            "user-agent": UA,
            "Cookie": self._cookie,
        }

    def _sign_get(self, uri: str, params: dict) -> dict[str, str]:
        headers = self._xh.sign_headers_get(uri=uri, cookies=self._cookie, params=params)
        return {**headers, "x-b3-traceid": headers.get("x-b3-traceid") or get_trace_id()}

    def _sign_post(self, uri: str, payload: dict) -> dict[str, str]:
        headers = self._xh.sign_headers_post(uri=uri, cookies=self._cookie, payload=payload)
        return {**headers, "x-b3-traceid": headers.get("x-b3-traceid") or get_trace_id()}

    async def _get(self, uri: str, params: dict, referer: str = "https://www.xiaohongshu.com/") -> dict:
        headers = {**self._headers(referer), **self._sign_get(uri, params)}
        full_url = f"{HOST}{uri}"

        async with httpx.AsyncClient(
            proxy=self._proxy, timeout=httpx.Timeout(self._timeout), follow_redirects=True,
        ) as client:
            resp = await client.get(full_url, params=params, headers=headers)
            return self._parse(resp)

    async def _post(self, uri: str, data: dict, referer: str = "https://www.xiaohongshu.com/") -> dict:
        headers = {**self._headers(referer), **self._sign_post(uri, data)}
        full_url = f"{HOST}{uri}"

        async with httpx.AsyncClient(
            proxy=self._proxy, timeout=httpx.Timeout(self._timeout), follow_redirects=True,
        ) as client:
            resp = await client.post(full_url, json=data, headers=headers)
            return self._parse(resp)

    @staticmethod
    def _parse(resp) -> dict:
        if resp.status_code not in (200, 201):
            raise XHSAPIError(f"XHS API returned {resp.status_code}: {resp.text[:300]}")
        try:
            data = resp.json()
        except Exception:
            raise XHSAPIError(f"Invalid JSON: {resp.text[:300]}")
        if not data.get("success", True):
            raise XHSAPIError(f"XHS API error: {data.get('msg', 'unknown')}")
        return data.get("data", data)

    async def search_notes(
        self, keyword: str, page: int = 1, page_size: int = 20, search_id: str = "",
    ) -> dict:
        """Search XHS notes. Returns raw API response (data dict)."""
        uri = "/api/sns/web/v1/search/notes"
        body = {
            "keyword": keyword,
            "page": page,
            "page_size": page_size,
            "search_id": search_id,
            "sort": "general",
            "note_type": 0,
        }
        return await self._post(uri, body)

    async def search_videos(
        self, keyword: str, page: int = 1, page_size: int = 20, search_id: str = "",
    ) -> list[dict[str, Any]]:
        """Search XHS and return just the items list."""
        data = await self.search_notes(keyword, page=page, page_size=page_size, search_id=search_id)
        return data.get("items") or []

    async def note_detail(self, note_id: str) -> dict[str, Any]:
        """Get XHS note detail."""
        uri = "/api/sns/web/v1/feed"
        body = {
            "source_note_id": note_id,
            "image_formats": ["jpg", "webp", "avif"],
            "extra": {"need_body_topic": 1},
        }
        data = await self._post(uri, body)
        items = data.get("items") or []
        if not items:
            raise XHSAPIError(f"Note {note_id} not found")
        return items[0].get("note_card") or items[0]

    async def pong(self) -> bool:
        """MediaCrawler-style session validation: calls user self-info API."""
        try:
            uri = "/api/sns/web/v1/user/selfinfo"
            result = await self._get(uri, {})
            return bool(result.get("result", {}).get("success"))
        except Exception:
            return False


def get_search_id() -> str:
    e = int(time.time() * 1000) << 64
    t = int(random.uniform(0, 2147483646))
    return _base36encode(e + t)


def _base36encode(number: int) -> str:
    alphabet = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ'
    if number == 0:
        return '0'
    result = ''
    while number:
        number, i = divmod(number, 36)
        result = alphabet[i] + result
    return result


def load_cookies(storage_path: Path) -> str:
    if not storage_path.exists():
        return ""
    data = json.loads(storage_path.read_text(encoding="utf-8"))
    cookies = data.get("cookies", [])
    return "; ".join(f"{c['name']}={c['value']}" for c in cookies if c.get("name") and c.get("value"))
