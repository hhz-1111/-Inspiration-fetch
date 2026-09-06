import asyncio
import logging

import httpx

from .config import settings
from .db import claim_next_job, complete_job, fail_job
from .mimo import analyze_video, download_video, platform_video_url, public_video_url

logger = logging.getLogger(__name__)


async def _notify_feishu_append(job_id: int, video_id: int) -> None:
    """Best-effort: tell fetch-api to append the finished analysis to Feishu."""
    try:
        headers = {"X-Internal-Token": settings.internal_api_token, "Content-Type": "application/json"}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.post(
                f"{settings.fetch_api_url.rstrip('/')}/api/internal/feishu/append",
                headers=headers, json={"analysis_id": job_id},
            )
            if resp.status_code != 200:
                logger.warning("Feishu append notify returned %d for analysis %s", resp.status_code, job_id)
    except Exception as exc:
        logger.warning("Feishu append notify failed for analysis %s: %s", job_id, exc)


async def process_pending_jobs() -> int:
    processed = 0
    while job := claim_next_job():
        uid = job.get("user_id")
        try:
            if settings.video_input_mode == "url":
                source_url = public_video_url(job["video_id"]) if settings.public_fetch_api_url else await platform_video_url(job["video_id"])
                result = await analyze_video(None, video_url=source_url, user_id=uid)
            elif settings.video_input_mode == "auto" and settings.public_fetch_api_url:
                result = await analyze_video(None, video_url=public_video_url(job["video_id"]), user_id=uid)
            else:
                try:
                    video, content_type = await download_video(job["video_id"])
                    result = await analyze_video(video, content_type, user_id=uid)
                except RuntimeError as exc:
                    if settings.video_input_mode != "auto" or "37 MB" not in str(exc):
                        raise
                    logger.info("Video %s is too large for Base64; falling back to its platform CDN URL", job["video_id"])
                    result = await analyze_video(None, video_url=await platform_video_url(job["video_id"]), user_id=uid)
            complete_job(job["id"], result, settings.mimo_model)
            logger.info("Completed analysis job %s for video %s", job["id"], job["video_id"])
            # 分析完成 → 自动追加到飞书表格（失败不影响分析结果）
            asyncio.create_task(_notify_feishu_append(job["id"], job["video_id"]))
        except Exception as exc:
            logger.exception("Analysis job %s failed", job["id"])
            fail_job(job["id"], job["attempts"] + 1, str(exc))
        processed += 1
    return processed


async def scheduler(stop_event: asyncio.Event) -> None:
    while not stop_event.is_set():
        try:
            await asyncio.wait_for(stop_event.wait(), timeout=settings.scan_interval_seconds)
            continue
        except asyncio.TimeoutError:
            pass
        await process_pending_jobs()
