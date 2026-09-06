from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Dict, Optional
from urllib.parse import unquote
from uuid import uuid4

import httpx

from fastapi import Depends, FastAPI, Header, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from .auth import auth_manager
from .config import settings
from .crawler import fetch_media, parse_shared_link, resolve_media_url, run_crawl
from .db import (
    add_analysis_items, add_uploaded_video, connection, create_session, create_task, create_user, delete_analysis_item, delete_owned_video,
    delete_session, delete_user, get_config, get_owned_thumbnail, get_owned_video, get_task, get_user_by_username,
    get_user_mimo_key, init_db, is_feishu_exported, list_analysis_items, list_inspiration_analyses,
    list_user_tasks, list_users, mark_feishu_exported, reset_user_password, set_config, set_user_mimo_key,
    set_video_inspiration, update_task, update_user_status, update_video_media_url,
)
from .dependencies import admin_user, current_user
from .feishu import EXPORT_HEADER, FeishuError, analysis_to_row, export_widths as _feishu_widths
from .feishu import ensure_header as _feishu_ensure_header
from .feishu import ensure_spreadsheet as _feishu_ensure_spreadsheet
from .feishu import get_tenant_access_token as _feishu_get_token
from .feishu import insert_image as _feishu_insert_image
from .feishu import read_used_row_count as _feishu_read_used_row_count
from .feishu import set_column_widths as _feishu_set_column_widths
from .feishu import style_row as _feishu_style_row
from .feishu import write_row_at as _feishu_write_row_at
from .platforms import get_platform
from .schemas import AnalysisItemsRequest, CrawlRequest, Credentials, InspirationUpdate, PasswordReset, SharedLinkRequest, TaskCreated
from .security import hash_password, new_session_token, token_hash, verify_password

running_tasks: set[asyncio.Task[None]] = set()
active_crawls: set[tuple[int, str]] = set()
crawl_tasks_by_id: dict[str, asyncio.Task[None]] = {}
crawl_semaphore = asyncio.Semaphore(settings.max_concurrent_crawls)
# 飞书自动追加临界区锁：把“查幂等 → 读行号 → 写新行 → 标记已导出”串行化，
# 避免多人并发分析完成时行号重叠互相覆盖。注意：这是进程内锁，生产 fetch-api
# 必须单 worker（uvicorn 不加 --workers）；将来多实例扩容需升级为 DB 行锁/租约。
_feishu_append_lock = asyncio.Lock()


async def _guarded_run_crawl(
    task_id: str, user_id: int, platform_key: str,
    keywords: list[str], count: int, created_time: str, min_likes: int, speed: str,
) -> None:
    async with crawl_semaphore:
        await run_crawl(task_id, user_id, platform_key, keywords, count, created_time, min_likes, speed)


def _schedule_analysis_trigger() -> None:
    """Fire-and-forget: notify the analyzer to process pending jobs immediately."""

    async def _trigger() -> None:
        import logging
        try:
            from httpx import AsyncClient
            async with AsyncClient(timeout=10) as client:
                analyze_url = f"http://{settings.analyze_api_host}:{settings.analyze_api_port}"
                resp = await client.post(
                    f"{analyze_url}/api/jobs/run-once",
                    headers={"X-Internal-Token": settings.internal_api_token},
                )
                if resp.status_code != 200:
                    logging.getLogger("fetch-api").warning("Analysis trigger returned %d", resp.status_code)
        except Exception as exc:
            logging.getLogger("fetch-api").warning("Analysis trigger failed: %s", exc)

    task = asyncio.create_task(_trigger())
    running_tasks.add(task)
    task.add_done_callback(running_tasks.discard)


@asynccontextmanager
async def lifespan(_: FastAPI):
    # Ensure crawler/client loggers (crawler, douyin_client, xhs_client) reach
    # .runtime/backend.log for diagnostics.
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        force=True,
    )
    init_db()
    yield
    for task in running_tasks: task.cancel()
    await asyncio.gather(*running_tasks, return_exceptions=True)
    await auth_manager.shutdown()


app = FastAPI(title="Inspiration Fetch API", version="0.2.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=[x.strip() for x in settings.cors_origins.split(",")], allow_credentials=True, allow_methods=["*"], allow_headers=["*"])


def public_user(user: dict[str, Any]) -> dict[str, Any]:
    return {key: user.get(key) for key in ("id", "username", "role", "status", "created_at", "approved_at")}


def local_video_path(video: dict[str, Any]) -> Path | None:
    raw_path = video.get("local_path")
    if not raw_path:
        return None
    path = Path(raw_path).resolve()
    uploads_root = (settings.data_dir / "uploads").resolve()
    return path if uploads_root in path.parents and path.is_file() else None


@app.get("/api/health")
async def health() -> dict[str, str]: return {"status": "ok"}


@app.get("/api/config/public")
async def public_config() -> dict[str, list[dict[str, object]]]:
    labels = {"sloth": "树懒", "human": "真人", "default": "默认", "flash": "闪电侠"}
    order = ("sloth", "human", "default", "flash")
    return {"crawl_speeds": [{"value": key, "label": labels[key], "delay_seconds": settings.crawl_delays[key]} for key in order]}


@app.post("/api/auth/register", status_code=201)
async def register(payload: Credentials) -> dict[str, Any]:
    user = create_user(payload.username, hash_password(payload.password))
    if not user: raise HTTPException(409, "用户名已存在")
    return {"message": "注册成功，请等待管理员审批", "user": user}


@app.post("/api/auth/login")
async def login(payload: Credentials, response: Response) -> dict[str, Any]:
    user = get_user_by_username(payload.username)
    if not user or not verify_password(payload.password, user["password_hash"]):
        raise HTTPException(401, "用户名或密码错误")
    if user["status"] == "pending": raise HTTPException(403, "账号正在等待管理员审批")
    if user["status"] != "approved": raise HTTPException(403, "账号已被停用")
    token, session_hash = new_session_token()
    create_session(session_hash, user["id"])
    response.set_cookie("fetch_session", token, max_age=604800, httponly=True, samesite="lax", secure=settings.cookie_secure, path="/")
    return {"user": public_user(user)}


@app.post("/api/auth/logout", status_code=204)
async def app_logout(request: Request, response: Response) -> Response:
    token = request.cookies.get("fetch_session")
    if token: delete_session(token_hash(token))
    response.delete_cookie("fetch_session", path="/")
    response.status_code = 204
    return response


@app.get("/api/auth/me")
async def me(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]: return {"user": public_user(user)}


@app.get("/api/admin/users")
async def admin_users(_: dict = Depends(admin_user)) -> dict[str, Any]: return {"users": list_users()}


@app.patch("/api/admin/users/{user_id}/approve")
async def approve_user(user_id: int, _: dict = Depends(admin_user)) -> dict[str, str]:
    if not update_user_status(user_id, "approved"): raise HTTPException(404, "用户不存在或不可修改")
    return {"message": "用户已审批"}


@app.patch("/api/admin/users/{user_id}/reject")
async def reject_user(user_id: int, _: dict = Depends(admin_user)) -> dict[str, str]:
    if not update_user_status(user_id, "rejected"): raise HTTPException(404, "用户不存在或不可修改")
    return {"message": "用户已停用"}


@app.patch("/api/admin/users/{user_id}/password")
async def admin_reset_password(user_id: int, payload: PasswordReset, _: dict = Depends(admin_user)) -> dict[str, str]:
    if not reset_user_password(user_id, hash_password(payload.password)): raise HTTPException(404, "用户不存在或不可修改")
    return {"message": "密码已重置，该用户需要重新登录"}


@app.delete("/api/admin/users/{user_id}", status_code=204)
async def admin_delete_user(user_id: int, _: dict = Depends(admin_user)) -> Response:
    await auth_manager.remove_user(user_id)
    if not delete_user(user_id): raise HTTPException(404, "用户不存在或不可删除")
    return Response(status_code=204)


@app.get("/api/admin/settings")
async def get_settings(user: dict = Depends(current_user)) -> dict[str, Any]:
    # 与 analyze-api 的 resolve_mimo_key 一致：用户 key → app_config 表 → .env 全局 key。
    # 仅当 key 属于当前用户时才回传完整值（可编辑）；全局 key 只回传 configured 状态，
    # 避免把服务端全局密钥下发到每个用户并可被误存进用户表。
    user_key = get_user_mimo_key(user["id"])
    config_key = get_config("mimo_api_key") or ""
    if user_key:
        return {"mimo_api_key": user_key, "configured": True, "source": "user"}
    if config_key:
        return {"mimo_api_key": "", "configured": True, "source": "config"}
    if settings.mimo_api_key:
        return {"mimo_api_key": "", "configured": True, "source": "env"}
    return {"mimo_api_key": "", "configured": False, "source": ""}


@app.put("/api/admin/settings")
async def update_settings(payload: dict[str, str], user: dict = Depends(current_user)) -> dict[str, Any]:
    key = payload.get("mimo_api_key", "").strip()
    set_user_mimo_key(user["id"], key)
    return {"mimo_api_key": key, "configured": bool(key)}


@app.get("/api/platform-auth/{platform}/status")
async def auth_status(platform: str, user: dict = Depends(current_user)) -> Dict[str, Optional[str]]:
    try: return await auth_manager.status(user["id"], platform)
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc


@app.post("/api/platform-auth/{platform}/sessions", status_code=201)
async def create_auth_session(platform: str, user: dict = Depends(current_user)) -> dict[str, str]:
    try: session = await auth_manager.start(user["id"], platform)
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc
    return {"session_id": session.id, "status": session.status, "message": session.message}


@app.get("/api/platform-auth/sessions/{session_id}/status")
async def auth_session_status(session_id: str, user: dict = Depends(current_user)) -> dict[str, str]:
    session = auth_manager.get_session(session_id)
    if not session or session.user_id != user["id"]: raise HTTPException(404, "授权会话不存在")
    return {"session_id": session.id, "status": session.status, "message": session.message}


@app.get("/api/platform-auth/sessions/{session_id}/qrcode")
async def auth_qrcode(session_id: str, user: dict = Depends(current_user)) -> Response:
    session = auth_manager.get_session(session_id)
    if not session or session.user_id != user["id"]: raise HTTPException(404, "授权会话不存在")
    if not session.qr_image: raise HTTPException(425, "二维码尚未准备好")
    return Response(session.qr_image, media_type="image/png", headers={"Cache-Control": "no-store"})


@app.delete("/api/platform-auth/{platform}", status_code=204)
async def platform_logout(platform: str, user: dict = Depends(current_user)) -> Response:
    try: await auth_manager.logout(user["id"], platform)
    except ValueError as exc: raise HTTPException(404, str(exc)) from exc
    return Response(status_code=204)


@app.post("/api/crawl-tasks", response_model=TaskCreated, status_code=202)
async def start_crawl(payload: CrawlRequest, user: dict = Depends(current_user)) -> TaskCreated:
    get_platform(payload.platform)
    auth = await auth_manager.status(user["id"], payload.platform)
    if auth["status"] != "authenticated": raise HTTPException(409, "平台账号尚未登录或登录状态已过期")
    crawl_key = (user["id"], payload.platform)
    if crawl_key in active_crawls:
        raise HTTPException(409, "该平台已有采集任务正在运行，请等待任务完成后再试")
    task_id = uuid4().hex
    create_task(task_id, user["id"], payload.platform, payload.keywords, payload.count, payload.created_time, payload.min_likes, payload.speed)
    active_crawls.add(crawl_key)
    task = asyncio.create_task(_guarded_run_crawl(task_id, user["id"], payload.platform, payload.keywords, payload.count, payload.created_time, payload.min_likes, payload.speed))
    running_tasks.add(task)
    crawl_tasks_by_id[task_id] = task
    task.add_done_callback(running_tasks.discard)
    task.add_done_callback(lambda _: active_crawls.discard(crawl_key))
    task.add_done_callback(lambda _: crawl_tasks_by_id.pop(task_id, None))
    return TaskCreated(id=task_id, status="pending")


@app.get("/api/crawl-tasks")
async def crawl_history(user: dict = Depends(current_user)) -> dict[str, list[dict]]:
    return {"tasks": list_user_tasks(user["id"])}


@app.post("/api/crawl-tasks/{task_id}/cancel", status_code=202)
async def cancel_crawl(task_id: str, user: dict = Depends(current_user)) -> dict[str, str]:
    task = get_task(task_id, user["id"])
    if not task:
        raise HTTPException(404, "采集任务不存在")
    if task["status"] not in ("pending", "running"):
        raise HTTPException(409, "任务已结束，无法取消")
    asyncio_task = crawl_tasks_by_id.get(task_id)
    if asyncio_task and not asyncio_task.done():
        asyncio_task.cancel()
    update_task(task_id, "cancelled", task["progress"], "任务已取消")
    return {"status": "cancelled", "message": "任务已取消"}


@app.get("/api/crawl-tasks/{task_id}")
async def crawl_status(task_id: str, user: dict = Depends(current_user)) -> dict:
    task = get_task(task_id, user["id"])
    if not task: raise HTTPException(404, "采集任务不存在")
    return task


@app.post("/api/analysis-items", status_code=201)
async def enqueue_analysis_items(payload: AnalysisItemsRequest, user: dict = Depends(current_user)) -> dict[str, int]:
    result = add_analysis_items(user["id"], payload.video_ids)
    if result["added"] == 0 and result["total"] == 0: raise HTTPException(404, "没有找到可加入的采集视频")
    return result


@app.get("/api/analysis-items")
async def analysis_items(user: dict = Depends(current_user)) -> dict[str, list[dict]]:
    return {"items": list_analysis_items(user["id"])}


@app.post("/api/analysis-items/parse-share", status_code=201)
async def parse_share_link(payload: SharedLinkRequest, user: dict = Depends(current_user)) -> dict:
    try:
        item = await parse_shared_link(user["id"], payload.platform, payload.url)
    except Exception as exc:
        raise HTTPException(422, f"分享链接解析失败：{exc}") from exc
    return {"item": item}


@app.post("/api/analysis-items/upload", status_code=201)
async def upload_local_video(request: Request, user: dict = Depends(current_user)) -> dict:
    filename = unquote(request.headers.get("x-upload-filename", "video.mp4"))
    title = unquote(request.headers.get("x-upload-title", "")).strip() or Path(filename).stem
    suffix = Path(filename).suffix.lower()
    if suffix not in {".mp4", ".mov", ".webm", ".m4v"}:
        raise HTTPException(415, "仅支持 MP4、MOV、WebM 或 M4V 视频文件")
    content_length = request.headers.get("content-length")
    if content_length and int(content_length) > 500 * 1024 * 1024:
        raise HTTPException(413, "视频文件不能超过 500 MB")
    upload_dir = settings.data_dir / "uploads" / str(user["id"])
    upload_dir.mkdir(parents=True, exist_ok=True)
    path = upload_dir / f"{uuid4().hex}{suffix}"
    total = 0
    limit = 500 * 1024 * 1024
    try:
        with open(path, "wb") as f:
            async for chunk in request.stream():
                total += len(chunk)
                if total > limit:
                    f.close()
                    path.unlink(missing_ok=True)
                    raise HTTPException(413, "视频文件不能超过 500 MB")
                f.write(chunk)
    except HTTPException:
        raise
    except Exception:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "视频上传失败")
    if total == 0:
        path.unlink(missing_ok=True)
        raise HTTPException(422, "请选择视频文件")
    return {"item": add_uploaded_video(user["id"], title, str(path))}


@app.delete("/api/analysis-items/{item_id}", status_code=204)
async def discard_analysis_item(item_id: int, user: dict = Depends(current_user)) -> Response:
    if not delete_analysis_item(user["id"], item_id): raise HTTPException(404, "待分析视频不存在")
    return Response(status_code=204)


@app.delete("/api/videos/{video_id}", status_code=204)
async def delete_video(video_id: int, user: dict = Depends(current_user)) -> Response:
    if not delete_owned_video(user["id"], video_id):
        raise HTTPException(404, "视频不存在")
    return Response(status_code=204)


@app.patch("/api/videos/{video_id}/inspiration")
async def mark_video_inspiration(video_id: int, payload: InspirationUpdate, user: dict = Depends(current_user)) -> dict[str, bool]:
    if payload.marked and not (get_user_mimo_key(user["id"]) or settings.mimo_api_key):
        raise HTTPException(400, "请先在系统设置中配置 MiMo API Key 后再进行分析")
    if not set_video_inspiration(user["id"], video_id, payload.marked):
        raise HTTPException(404, "视频不存在")
    if payload.marked:
        _schedule_analysis_trigger()
    return {"marked": payload.marked}


async def _append_item_to_feishu(item: dict[str, Any]) -> dict[str, Any]:
    """Append one completed analysis row to the Feishu spreadsheet.

    The header is written only when the sheet is empty; the new row is placed
    right after the last used row so existing rows and hand-filled columns are
    never touched. Cover image is downloaded from the platform thumbnail_url
    and inserted into the B cell of the new row.
    """
    row = analysis_to_row(item)
    cover: tuple[bytes, str] | None = None
    thumb_url = item.get("thumbnail_url") or ""
    if thumb_url.startswith("http"):
        try:
            async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
                r = await client.get(thumb_url, headers={"User-Agent": "Mozilla/5.0"})
                if r.status_code == 200 and r.content:
                    cover = (r.content, r.headers.get("content-type", "image/jpeg"))
        except Exception:
            cover = None

    token = await _feishu_get_token()
    spreadsheet_token, sheet_id, _sheet_title = await _feishu_ensure_spreadsheet(token)
    # 首次（表为空）先写表头并设置整表格式；此后每次只追加一行数据
    first_time = await _feishu_read_used_row_count(token, spreadsheet_token, sheet_id, max_rows=1) == 0
    if first_time:
        await _feishu_ensure_header(token, spreadsheet_token, sheet_id, EXPORT_HEADER)
        await _feishu_set_column_widths(token, spreadsheet_token, sheet_id, _feishu_widths())
        await _feishu_style_row(token, spreadsheet_token, sheet_id, 1, header=True)
    used = await _feishu_read_used_row_count(token, spreadsheet_token, sheet_id)
    new_row = used + 1  # 表头占第 1 行，新数据从第 2 行起
    await _feishu_write_row_at(token, spreadsheet_token, sheet_id, new_row, row)
    await _feishu_style_row(token, spreadsheet_token, sheet_id, new_row)
    inserted = 0
    if cover:
        try:
            # B 列为封面（第 2 列），insert_image 的 row/column 是 0-based
            await _feishu_insert_image(token, spreadsheet_token, sheet_id, new_row - 1, 1, cover[0], cover[1])
            inserted = 1
        except Exception:
            inserted = 0
    return {"spreadsheet_token": spreadsheet_token, "row": new_row, "cover": inserted}


async def _append_analysis_to_feishu(analysis_id: int) -> dict[str, Any]:
    """幂等地把一条已完成分析追加到飞书共享表（团队共用一张表）。

    在 _feishu_append_lock 临界区内完成“查幂等 → 读分析/视频数据（带分析人）
    → 计算新行号并写入 → 标记 feishu_exported”，保证多人并发分析完成时不会
    行号重叠互相覆盖。已导出的记录直接跳过（重复通知不会产生重复行）。
    """
    async with _feishu_append_lock:
        if is_feishu_exported(analysis_id):
            return {"message": "该分析已导出到飞书，跳过", "skipped": True}
        with connection() as conn:
            row = conn.execute(
                """SELECT u.username, ia.id analysis_id, ia.status analysis_status,
                   ia.transcript, ia.video_title, ia.copy_structure, ia.boost_points,
                   v.id, v.platform, v.keyword, v.title, v.author,
                   v.thumbnail_url, v.thumbnail_data, v.thumbnail_content_type, v.likes
                   FROM inspiration_analyses ia
                   JOIN users u ON u.id = ia.user_id
                   JOIN videos v ON v.id = ia.video_id
                   WHERE ia.id=?""",
                (analysis_id,),
            ).fetchone()
        if not row or row["analysis_status"] != "completed":
            raise HTTPException(404, "分析记录不存在或未完成")
        item = dict(row)
        result = await _append_item_to_feishu(item)
        mark_feishu_exported(analysis_id)
        return {"message": f"已追加视频 {item['id']} 到飞书表格", **result}


@app.get("/api/inspirations")
async def inspirations(user: dict = Depends(current_user)) -> dict[str, list[dict]]:
    return {"items": list_inspiration_analyses(user["id"])}


@app.post("/api/export/feishu")
async def export_inspirations_to_feishu(user: dict = Depends(current_user)) -> dict[str, Any]:
    """Append all completed analyses that are not yet in the Feishu table.

    Auto-append on analysis completion is the primary path; this endpoint is a
    catch-up for rows that may have been missed (e.g. Feishu was offline). It
    never overwrites existing rows or hand-filled columns, and is idempotent:
    analyses already marked feishu_exported are skipped (可重复运行补导).
    """
    items = [item for item in list_inspiration_analyses(user["id"]) if item.get("analysis_status") == "completed"]
    if not items:
        raise HTTPException(400, "暂无可导出的已完成分析")
    added = 0
    spreadsheet_token = ""
    try:
        # 预检并确保表格存在：飞书凭证/网络整体故障时尽早给出明确错误
        token = await _feishu_get_token()
        spreadsheet_token, _sheet_id, _sheet_title = await _feishu_ensure_spreadsheet(token)
        for item in items:
            analysis_id = item.get("analysis_id")
            if not analysis_id:
                continue
            try:
                result = await _append_analysis_to_feishu(analysis_id)
                if not result.get("skipped"):
                    added += 1
            except Exception:
                # 单条失败不影响其余；下次补导可重试
                continue
    except FeishuError as exc:
        raise HTTPException(502, f"飞书导出失败：{exc}") from exc
    return {"message": f"已补导 {added} 条灵感分析到飞书表格", "spreadsheet_token": spreadsheet_token, "rows": added}


@app.post("/api/internal/feishu/append")
async def internal_append_to_feishu(payload: dict[str, Any], x_internal_token: str = Header(default="")) -> dict[str, Any]:
    """Append one completed analysis (by analysis_id) to the Feishu table.

    Called by the analyze worker right after an analysis completes so each
    finished video lands in the spreadsheet automatically. Idempotent: a row
    that was already exported (feishu_exported=1) is skipped, so duplicate
    notify calls from concurrent workers never duplicate spreadsheet rows.
    幂等检查、行号定位、写行与标记都在 _feishu_append_lock 临界区内串行执行。
    """
    if not secrets.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(401, "内部服务认证失败")
    analysis_id = payload.get("analysis_id")
    if not analysis_id:
        raise HTTPException(400, "缺少 analysis_id")
    try:
        return await _append_analysis_to_feishu(analysis_id)
    except FeishuError as exc:
        raise HTTPException(502, f"飞书自动追加失败：{exc}") from exc


@app.get("/api/videos/{video_id}/playback")
async def video_playback(video_id: int, user: dict = Depends(current_user)) -> dict[str, str]:
    video = get_owned_video(user["id"], video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    if local_video_path(video):
        return {"media_url": f"/api/videos/{video_id}/stream"}
    try:
        media_url = await resolve_media_url(user["id"], video)
    except Exception as exc:
        raise HTTPException(502, f"无法解析视频播放地址：{exc}") from exc
    if not media_url:
        raise HTTPException(422, "平台页面未提供可播放的视频地址")
    update_video_media_url(user["id"], video_id, media_url)
    return {"media_url": f"/api/videos/{video_id}/stream"}


@app.get("/api/videos/{video_id}/thumbnail")
async def video_thumbnail(video_id: int, user: dict = Depends(current_user)) -> Response:
    thumbnail = get_owned_thumbnail(user["id"], video_id)
    if not thumbnail:
        raise HTTPException(404, "缩略图不存在")
    data, content_type = thumbnail
    return Response(data, media_type=content_type, headers={"Cache-Control": "private, max-age=86400"})


@app.get("/api/videos/{video_id}/stream")
async def video_stream(video_id: int, request: Request, user: dict = Depends(current_user)) -> Response:
    video = get_owned_video(user["id"], video_id)
    if not video:
        raise HTTPException(404, "视频不存在")
    if path := local_video_path(video):
        return FileResponse(path, media_type="video/mp4", filename=path.name)
    media_url = video.get("media_url")
    if not media_url:
        media_url = await resolve_media_url(user["id"], video)
        if not media_url:
            raise HTTPException(422, "平台页面未提供可播放的视频地址")
        update_video_media_url(user["id"], video_id, media_url)
    try:
        status_code, body, source_headers = await fetch_media(user["id"], video, media_url, request.headers.get("range"))
        if status_code >= 400:
            raise RuntimeError(f"平台媒体服务器返回 {status_code}")
    except Exception as exc:
        raise HTTPException(502, f"无法读取平台视频：{exc}") from exc
    headers = {"Accept-Ranges": source_headers.get("accept-ranges", "bytes"), "Cache-Control": "private, max-age=300"}
    for source, target in (("content-range", "Content-Range"), ("content-length", "Content-Length")):
        if source in source_headers:
            headers[target] = source_headers[source]
    return Response(body, status_code=status_code, media_type=source_headers.get("content-type", "video/mp4"), headers=headers)


@app.get("/api/internal/videos/{video_id}/media")
async def internal_video_media(video_id: int, x_internal_token: str = Header(default="")) -> Response:
    if not secrets.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(401, "内部服务认证失败")
    with connection() as conn:
        row = conn.execute("SELECT * FROM videos WHERE id=? AND workflow_status='inspiration'", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "灵感视频不存在")
    video = dict(row)
    if path := local_video_path(video):
        return FileResponse(path, media_type="video/mp4", filename=path.name)
    with connection() as conn:
        owner = conn.execute("SELECT user_id FROM crawl_tasks WHERE id=?", (video["task_id"],)).fetchone()
    if not owner:
        raise HTTPException(404, "视频所属用户不存在")
    media_url = video.get("media_url") or await resolve_media_url(owner["user_id"], video)
    if not media_url:
        raise HTTPException(422, "无法解析平台视频地址")
    update_video_media_url(owner["user_id"], video_id, media_url)
    try:
        status_code, body, source_headers = await fetch_media(owner["user_id"], video, media_url, None)
    except Exception:
        # 缓存的播放地址可能已过期（403 等），强制重新解析后再试一次
        media_url = await resolve_media_url(owner["user_id"], video, force_refresh=True)
        if not media_url:
            raise HTTPException(502, "平台视频地址已过期且无法重新解析")
        update_video_media_url(owner["user_id"], video_id, media_url)
        status_code, body, source_headers = await fetch_media(owner["user_id"], video, media_url, None)
    if status_code >= 400:
        media_url = await resolve_media_url(owner["user_id"], video, force_refresh=True)
        if not media_url:
            raise HTTPException(502, "平台视频地址已过期且无法重新解析")
        update_video_media_url(owner["user_id"], video_id, media_url)
        status_code, body, source_headers = await fetch_media(owner["user_id"], video, media_url, None)
        if status_code >= 400:
            raise HTTPException(502, f"平台媒体服务器返回 {status_code}")
    return Response(body, media_type=source_headers.get("content-type", "video/mp4"))


@app.get("/api/internal/videos/{video_id}/source-url")
async def internal_video_source_url(video_id: int, x_internal_token: str = Header(default="")) -> dict[str, str]:
    if not secrets.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(401, "内部服务认证失败")
    with connection() as conn:
        row = conn.execute("""SELECT v.*,t.user_id FROM videos v JOIN crawl_tasks t ON t.id=v.task_id
                            WHERE v.id=? AND v.workflow_status='inspiration'""", (video_id,)).fetchone()
    if not row:
        raise HTTPException(404, "灵感视频不存在")
    video = dict(row)
    media_url = await resolve_media_url(video["user_id"], video, force_refresh=True)
    if not media_url:
        raise HTTPException(422, "无法解析平台视频地址")
    update_video_media_url(video["user_id"], video_id, media_url)
    return {"url": media_url}


@app.get("/api/public/videos/{video_id}/media")
async def signed_public_video_media(video_id: int, expires: int, signature: str) -> Response:
    if expires < int(time.time()) or expires > int(time.time()) + settings.public_video_url_ttl_seconds + 60:
        raise HTTPException(401, "视频访问地址已过期")
    message = f"{video_id}:{expires}".encode()
    expected = hmac.new(settings.internal_api_token.encode(), message, hashlib.sha256).hexdigest()
    if not secrets.compare_digest(signature, expected):
        raise HTTPException(401, "视频访问签名无效")
    return await internal_video_media(video_id, settings.internal_api_token)
