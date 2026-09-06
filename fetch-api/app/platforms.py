from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from urllib.parse import quote

CHINA_TZ = timezone(datetime.now().astimezone().utcoffset())


@dataclass(frozen=True)
class PlatformConfig:
    key: str
    label: str
    home_url: str
    login_url: str
    auth_cookie_names: tuple[str, ...]
    qr_selectors: tuple[str, ...]
    search_api_url: str
    search_method: str
    referer: str

    def search_url(self, keyword: str) -> str:
        encoded = quote(keyword)
        if self.key == "xiaohongshu":
            return f"https://www.xiaohongshu.com/search_result?keyword={encoded}&source=web_search_result_notes"
        return f"https://www.douyin.com/search/{encoded}?type=video"


def _format_douyin_video(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    info = item.get("aweme_info")
    if not info:
        # general/search/single returns mixed entries as aweme_mix_info.mix_items
        mix = item.get("aweme_mix_info") or {}
        mix_items = mix.get("mix_items") or []
        info = mix_items[0] if mix_items else None
    if not info:
        return None
    video = info.get("video", {})
    duration_ms = video.get("duration") or 0
    if duration_ms > 300_000:
        return None
    author = info.get("author", {})
    stats = info.get("statistics", {})
    cover = video.get("cover", {})
    cover_urls = cover.get("url_list") or cover.get("url") or []
    return {
        "platform": "抖音",
        "keyword": keyword,
        "title": (info.get("desc") or "")[:160],
        "author": (author.get("nickname") or "")[:80],
        "url": f"https://www.douyin.com/video/{info.get('aweme_id', '')}",
        "thumbnail_url": cover_urls[0] if isinstance(cover_urls, list) and cover_urls else str(cover_urls) if cover_urls else None,
        "duration": _format_ms(duration_ms),
        "views": str(stats.get("play_count", "--")),
        "likes": stats.get("digg_count"),
        "published_at": _ts_to_iso(info.get("create_time")),
        "is_video": True,
        "media_url": None,
    }


def _format_xhs_video(item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    note = item.get("note_card") or item
    if note.get("type") != "video" and "video" not in note:
        return None
    video_info = note.get("video") or {}
    duration_sec = video_info.get("duration") or 0
    if duration_sec > 300:
        return None
    user = note.get("user", {})
    interact = note.get("interact_info", {})
    cover_info = note.get("cover") or {}
    return {
        "platform": "小红书",
        "keyword": keyword,
        "title": (note.get("display_title") or note.get("title") or "")[:160],
        "author": (user.get("nickname") or user.get("name") or "")[:80],
        "url": f"https://www.xiaohongshu.com/explore/{note.get('note_id', '')}",
        "thumbnail_url": _xhs_cover_url(cover_info),
        "duration": _format_seconds(duration_sec),
        "views": str(interact.get("view_count", "--")),
        "likes": _parse_int(interact.get("liked_count")),
        "published_at": _ms_to_iso(note.get("time")),
        "is_video": True,
        "media_url": None,
    }


def _format_ms(ms: int | None) -> str | None:
    if not ms:
        return None
    total_s = max(0, ms // 1000)
    m, s = divmod(total_s, 60)
    return f"{m:02d}:{s:02d}"


def _format_seconds(sec: int | None) -> str | None:
    if not sec:
        return None
    m, s = divmod(int(sec), 60)
    return f"{m:02d}:{s:02d}"


def _ts_to_iso(ts: int | None) -> str | None:
    if not ts:
        return None
    return datetime.fromtimestamp(ts, tz=CHINA_TZ).isoformat()


def _ms_to_iso(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000, tz=CHINA_TZ).isoformat()


def _parse_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (ValueError, TypeError):
        return None


def _xhs_cover_url(cover: dict | list | str | None) -> str | None:
    if isinstance(cover, dict):
        url = cover.get("url") or cover.get("url_default")
        return str(url) if url else None
    if isinstance(cover, list):
        return str(cover[0]) if cover else None
    if isinstance(cover, str):
        return cover
    return None


PLATFORMS: dict[str, PlatformConfig] = {
    "xiaohongshu": PlatformConfig(
        key="xiaohongshu",
        label="小红书",
        home_url="https://www.xiaohongshu.com/explore",
        login_url="https://www.xiaohongshu.com/explore",
        auth_cookie_names=("web_session",),
        qr_selectors=(".login-container .qrcode-img", ".qrcode-img", "canvas", "img[src*='qr']"),
        search_api_url="https://edith.xiaohongshu.com/api/sns/web/v1/search/notes",
        search_method="POST",
        referer="https://www.xiaohongshu.com/",
    ),
    "douyin": PlatformConfig(
        key="douyin",
        label="抖音",
        home_url="https://www.douyin.com/",
        login_url="https://www.douyin.com/",
        auth_cookie_names=(
            "sessionid", "sessionid_ss", "sid_guard", "sid_tt", "sid_tt_ss",
            "uid_tt", "uid_tt_ss", "sid_ucp_v1", "ssid_ucp_v1",
        ),
        qr_selectors=("[data-e2e='qr-code']", "canvas", "img[src*='qr']", "img[alt*='二维码']"),
        search_api_url="https://www.douyin.com/aweme/v1/web/search/item/",
        search_method="GET",
        referer="https://www.douyin.com/",
    ),
}


def get_platform(key: str) -> PlatformConfig:
    try:
        return PLATFORMS[key]
    except KeyError as exc:
        raise ValueError(f"Unsupported platform: {key}") from exc


def format_video(platform_key: str, raw_item: dict[str, Any], keyword: str) -> dict[str, Any] | None:
    if platform_key == "douyin":
        return _format_douyin_video(raw_item, keyword)
    if platform_key == "xiaohongshu":
        return _format_xhs_video(raw_item, keyword)
    return None
