from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
from typing import Any

import httpx

from .config import settings


def resolve_mimo_key(user_id: int | None = None) -> str:
    """优先读当前用户的 key，没有则读全局 key（.env 或 app_config 表）。"""
    key = settings.mimo_api_key
    try:
        from .db import connection, get_user_mimo_key
        if user_id:
            user_key = get_user_mimo_key(user_id)
            if user_key:
                return user_key
        with connection() as conn:
            row = conn.execute("SELECT value FROM app_config WHERE key='mimo_api_key'").fetchone()
            if row and row["value"]:
                key = row["value"]
    except Exception:
        pass
    return key

PROMPT = """请完整分析这个短视频，并只返回一个合法、紧凑的 JSON 对象，不要使用 Markdown 代码块或添加解释。
JSON 必须包含以下字符串字段：
- video_title：仔细观察视频首帧/封面画面的文字内容，视频标题文字一般位于画面靠上的部分，请提取该区域的文字作为视频标题（保留原始文字，不要改写）；若画面中没有标题文字，则用一句话概括视频主题作为标题，最多 60 个中文字符。
- transcript：按时间顺序提取视频中的口播、字幕和主要屏幕文案，并按正常标点符号使用规律补充标点（句号、逗号、感叹号、问号等），使文案通顺可读；保留关键信息，最多 3000 个中文字符，没有则写“无明确文案”。
- copy_structure：拆解视频文案的整体结构，如开头钩子、情绪铺垫、冲突或转折、观点输出、行动号召等，说明每个部分的时间段与作用，最多 1200 个中文字符。
- boost_points：提取该视频能够起量（获得高播放、高互动）的关键点，如情绪共鸣、悬念、冲突、反差、热点结合、互动引导、完播设计等；用“1.”“2.”“3.”数字编号逐条列出，每条单独占一行，最多 1000 个中文字符。

固定输出结构示例：{"transcript":"...","video_title":"...","copy_structure":"...","boost_points":"..."}
不要臆造无法从视频确认的信息。"""

SYSTEM_PROMPT = """你是资深的短视频内容分析与创作顾问，擅长拆解爆款短视频的标题写法、文案结构与起量逻辑，输出可直接用于模仿创作的洞察。分析要具体、可操作，所有结论都必须以视频实际内容为依据。"""


async def download_video(video_id: int) -> tuple[bytes, str]:
    url = f"{settings.fetch_api_url.rstrip('/')}/api/internal/videos/{video_id}/media"
    headers = {"X-Internal-Token": settings.internal_api_token}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180)) as client:
        async with client.stream("GET", url, headers=headers) as response:
            response.raise_for_status()
            content_type = response.headers.get("content-type", "video/mp4").split(";", 1)[0]
            chunks: list[bytes] = []
            size = 0
            async for chunk in response.aiter_bytes():
                size += len(chunk)
                if size > settings.max_video_bytes:
                    raise RuntimeError("视频超过 MiMo Base64 输入大小限制（原始文件约 37 MB）")
                chunks.append(chunk)
    return b"".join(chunks), content_type


async def platform_video_url(video_id: int) -> str:
    url = f"{settings.fetch_api_url.rstrip('/')}/api/internal/videos/{video_id}/source-url"
    headers = {"X-Internal-Token": settings.internal_api_token}
    async with httpx.AsyncClient(timeout=httpx.Timeout(180)) as client:
        response = await client.get(url, headers=headers)
        response.raise_for_status()
        return response.json()["url"]


def public_video_url(video_id: int) -> str:
    if not settings.public_fetch_api_url:
        raise RuntimeError("URL 模式需要配置可从公网访问的 APP_PUBLIC_FETCH_API_URL")
    expires = int(time.time()) + settings.public_video_url_ttl_seconds
    message = f"{video_id}:{expires}".encode()
    signature = hmac.new(settings.internal_api_token.encode(), message, hashlib.sha256).hexdigest()
    return f"{settings.public_fetch_api_url.rstrip('/')}/api/public/videos/{video_id}/media?expires={expires}&signature={signature}"


async def analyze_video(video: bytes | None, content_type: str = "video/mp4", video_url: str | None = None, user_id: int | None = None) -> dict[str, str]:
    api_key = resolve_mimo_key(user_id)
    if not api_key:
        raise RuntimeError("APP_MIMO_API_KEY 未配置")
    if video_url:
        source_url = video_url
    elif video is not None:
        source_url = f"data:{content_type};base64,{base64.b64encode(video).decode('ascii')}"
    else:
        raise RuntimeError("没有可用的视频输入")
    payload: dict[str, Any] = {
        "model": settings.mimo_model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {"role": "user", "content": [
                {"type": "video_url", "video_url": {"url": source_url}, "fps": 4, "media_resolution": "default"},
                {"type": "text", "text": PROMPT},
            ]},
        ],
        "response_format": {"type": "json_object"},
        "max_completion_tokens": 12000,
    }
    headers = {"api-key": api_key, "Content-Type": "application/json"}
    async with httpx.AsyncClient(timeout=httpx.Timeout(600)) as client:
        response = await client.post(f"{settings.mimo_base_url.rstrip('/')}/chat/completions", headers=headers, json=payload)
        response.raise_for_status()
        choice = response.json()["choices"][0]
        content = choice["message"].get("content") or ""
        if choice.get("finish_reason") == "length":
            raise RuntimeError("MiMo 输出达到 token 上限，结构化结果被截断")
        if not content:
            raise RuntimeError("MiMo 没有返回可解析的分析内容")
    return _parse_result(content)


def _parse_result(content: str) -> dict[str, str]:
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", content.strip(), flags=re.IGNORECASE)
    try:
        start = cleaned.find("{")
        if start < 0:
            raise json.JSONDecodeError("JSON object not found", cleaned, 0)
        value, _ = json.JSONDecoder(strict=False).raw_decode(cleaned[start:])
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"MiMo 返回的分析结果不是合法 JSON（{exc.msg}，位置 {exc.pos}）：{content[:300]}") from exc
    aliases = {"transcript": "transcript", "video_title": "video_title", "copy_structure": "copy_structure", "boost_points": "boost_points"}
    result = {target: str(value.get(source, "")).strip() for source, target in aliases.items()}
    if not all(result.values()):
        raise RuntimeError("MiMo 分析结果缺少必要字段")
    return result
