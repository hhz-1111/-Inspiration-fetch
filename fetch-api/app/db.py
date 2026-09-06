from __future__ import annotations

import json
import shutil
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator, Optional, Tuple, Union
from uuid import uuid4

from .config import settings
from .security import hash_password

DB_PATH = settings.database_path


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _public_video(row: Union[sqlite3.Row, dict[str, Any]]) -> dict[str, Any]:
    video = dict(row)
    has_thumbnail = bool(video.pop("thumbnail_data", None))
    video.pop("thumbnail_content_type", None)
    if has_thumbnail:
        video["thumbnail_url"] = f"/api/videos/{video['id']}/thumbnail"
    return video


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}


def init_db() -> None:
    with connection() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL, role TEXT NOT NULL DEFAULT 'user',
                status TEXT NOT NULL DEFAULT 'pending', created_at TEXT NOT NULL, approved_at TEXT
            );
            CREATE TABLE IF NOT EXISTS user_sessions (
                token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, created_at TEXT NOT NULL,
                expires_at TEXT NOT NULL, FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS crawl_tasks (
                id TEXT PRIMARY KEY, platform TEXT NOT NULL, keywords TEXT NOT NULL,
                requested_count INTEGER NOT NULL, status TEXT NOT NULL, progress INTEGER NOT NULL DEFAULT 0,
                message TEXT, error TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS videos (
                id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT NOT NULL, platform TEXT NOT NULL,
                keyword TEXT NOT NULL, title TEXT NOT NULL, author TEXT, url TEXT,
                thumbnail_url TEXT, duration TEXT, views TEXT, raw_json TEXT,
                FOREIGN KEY(task_id) REFERENCES crawl_tasks(id) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS analysis_items (
                id INTEGER PRIMARY KEY AUTOINCREMENT, video_id INTEGER NOT NULL UNIQUE,
                added_at TEXT NOT NULL, FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
            );
        """)
        root = conn.execute("SELECT id FROM users WHERE username=?", (settings.admin_username,)).fetchone()
        if not root:
            conn.execute("INSERT INTO users(username,password_hash,role,status,created_at,approved_at) VALUES(?,?,?,?,?,?)",
                         (settings.admin_username, hash_password(settings.admin_password), "admin", "approved", now(), now()))
            root_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
        else:
            root_id = root["id"]

        if "user_id" not in _columns(conn, "crawl_tasks"):
            conn.execute("ALTER TABLE crawl_tasks ADD COLUMN user_id INTEGER")
        if "created_time_filter" not in _columns(conn, "crawl_tasks"):
            conn.execute("ALTER TABLE crawl_tasks ADD COLUMN created_time_filter TEXT NOT NULL DEFAULT 'any'")
        if "min_likes" not in _columns(conn, "crawl_tasks"):
            conn.execute("ALTER TABLE crawl_tasks ADD COLUMN min_likes INTEGER NOT NULL DEFAULT 0")
        if "crawl_speed" not in _columns(conn, "crawl_tasks"):
            conn.execute("ALTER TABLE crawl_tasks ADD COLUMN crawl_speed TEXT NOT NULL DEFAULT 'default'")
        if "user_id" not in _columns(conn, "analysis_items"):
            conn.execute("ALTER TABLE analysis_items ADD COLUMN user_id INTEGER")
        if "likes" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN likes INTEGER")
        if "published_at" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN published_at TEXT")
        if "media_url" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN media_url TEXT")
        if "local_path" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN local_path TEXT")
        if "is_inspiration" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN is_inspiration INTEGER NOT NULL DEFAULT 0")
        if "workflow_status" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN workflow_status TEXT NOT NULL DEFAULT 'collected'")
        if "thumbnail_data" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN thumbnail_data BLOB")
        if "thumbnail_content_type" not in _columns(conn, "videos"):
            conn.execute("ALTER TABLE videos ADD COLUMN thumbnail_content_type TEXT")
        conn.execute("""UPDATE videos SET workflow_status='queued' WHERE id IN
                      (SELECT video_id FROM analysis_items) AND is_inspiration=0""")
        conn.execute("UPDATE videos SET workflow_status='inspiration' WHERE is_inspiration=1")
        conn.execute("""CREATE TABLE IF NOT EXISTS inspiration_analyses (
            id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL, video_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending', analysis_text TEXT, error TEXT,
            created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
            UNIQUE(user_id,video_id),
            FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE,
            FOREIGN KEY(video_id) REFERENCES videos(id) ON DELETE CASCADE
        )""")
        conn.execute("""CREATE TABLE IF NOT EXISTS app_config (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL DEFAULT ''
        )""")
        for column, definition in (
            ("transcript", "TEXT"), ("background_music", "TEXT"), ("video_description", "TEXT"),
            ("video_understanding", "TEXT"), ("model", "TEXT"), ("attempts", "INTEGER NOT NULL DEFAULT 0"),
            ("next_retry_at", "TEXT"),
            ("video_title", "TEXT"), ("copy_structure", "TEXT"), ("boost_points", "TEXT"),
            ("feishu_exported", "INTEGER NOT NULL DEFAULT 0"),
        ):
            if column not in _columns(conn, "inspiration_analyses"):
                conn.execute(f"ALTER TABLE inspiration_analyses ADD COLUMN {column} {definition}")
        timestamp = now()
        conn.execute("""INSERT OR IGNORE INTO inspiration_analyses(user_id,video_id,status,created_at,updated_at)
                      SELECT t.user_id,v.id,'pending',?,? FROM videos v JOIN crawl_tasks t ON t.id=v.task_id
                      WHERE v.is_inspiration=1 AND t.user_id IS NOT NULL""", (timestamp, timestamp))
        conn.execute("DELETE FROM analysis_items WHERE video_id IN (SELECT id FROM videos WHERE workflow_status='inspiration')")
        conn.execute("UPDATE crawl_tasks SET user_id=? WHERE user_id IS NULL", (root_id,))
        conn.execute("UPDATE analysis_items SET user_id=? WHERE user_id IS NULL", (root_id,))

        auth_exists = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_auth'").fetchone()
        if auth_exists and "user_id" not in _columns(conn, "platform_auth"):
            conn.execute("ALTER TABLE platform_auth RENAME TO platform_auth_legacy")
        conn.execute("""CREATE TABLE IF NOT EXISTS platform_auth (
            user_id INTEGER NOT NULL, platform TEXT NOT NULL, status TEXT NOT NULL, storage_path TEXT,
            authenticated_at TEXT, last_checked_at TEXT, message TEXT,
            PRIMARY KEY(user_id,platform), FOREIGN KEY(user_id) REFERENCES users(id) ON DELETE CASCADE
        )""")
        legacy = conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='platform_auth_legacy'").fetchone()
        if legacy:
            conn.execute("""INSERT OR IGNORE INTO platform_auth(user_id,platform,status,storage_path,authenticated_at,last_checked_at,message)
                          SELECT ?,platform,status,storage_path,authenticated_at,last_checked_at,message FROM platform_auth_legacy""", (root_id,))
            conn.execute("DROP TABLE platform_auth_legacy")
        conn.execute("UPDATE crawl_tasks SET status='failed',error='服务重启，任务已中断' WHERE status IN ('pending','running')")
        conn.execute("DELETE FROM user_sessions WHERE expires_at < ?", (now(),))

        if "mimo_api_key" not in _columns(conn, "users"):
            conn.execute("ALTER TABLE users ADD COLUMN mimo_api_key TEXT NOT NULL DEFAULT ''")

    root_auth = settings.auth_dir / str(root_id)
    root_auth.mkdir(parents=True, exist_ok=True)
    for platform in ("douyin", "xiaohongshu"):
        for suffix in ("json", "verified"):
            old = settings.auth_dir / f"{platform}.{suffix}"
            if old.exists() and not (root_auth / old.name).exists():
                shutil.move(str(old), str(root_auth / old.name))


def create_user(username: str, password_hash: str) -> dict[str, Any] | None:
    try:
        with connection() as conn:
            conn.execute("INSERT INTO users(username,password_hash,role,status,created_at) VALUES(?,?,'user','pending',?)", (username, password_hash, now()))
            row = conn.execute("SELECT id,username,role,status,created_at,approved_at FROM users WHERE username=?", (username,)).fetchone()
        return dict(row)
    except sqlite3.IntegrityError:
        return None


def get_user_by_username(username: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("SELECT * FROM users WHERE username=?", (username,)).fetchone()
    return dict(row) if row else None


def get_user_by_session(session_hash: str) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("""SELECT u.id,u.username,u.role,u.status,u.created_at,u.approved_at
                            FROM user_sessions s JOIN users u ON u.id=s.user_id
                            WHERE s.token_hash=? AND s.expires_at>?""", (session_hash, now())).fetchone()
    return dict(row) if row else None


def create_session(session_hash: str, user_id: int) -> None:
    expires = (datetime.now(timezone.utc) + timedelta(days=7)).isoformat()
    with connection() as conn:
        conn.execute("INSERT INTO user_sessions VALUES(?,?,?,?)", (session_hash, user_id, now(), expires))


def delete_session(session_hash: str) -> None:
    with connection() as conn:
        conn.execute("DELETE FROM user_sessions WHERE token_hash=?", (session_hash,))


def list_users() -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("SELECT id,username,role,status,created_at,approved_at FROM users ORDER BY created_at DESC").fetchall()
    return [dict(row) for row in rows]


def update_user_status(user_id: int, user_status: str) -> bool:
    with connection() as conn:
        cur = conn.execute("UPDATE users SET status=?,approved_at=? WHERE id=? AND role!='admin'", (user_status, now() if user_status == "approved" else None, user_id))
        if user_status != "approved":
            conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
    return cur.rowcount > 0


def reset_user_password(user_id: int, password_hash: str) -> bool:
    with connection() as conn:
        cur = conn.execute("UPDATE users SET password_hash=? WHERE id=?", (password_hash, user_id))
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
    return cur.rowcount > 0


def delete_user(user_id: int) -> bool:
    with connection() as conn:
        task_ids = [row["id"] for row in conn.execute("SELECT id FROM crawl_tasks WHERE user_id=?", (user_id,))]
        conn.execute("DELETE FROM analysis_items WHERE user_id=?", (user_id,))
        if task_ids:
            placeholders = ",".join("?" for _ in task_ids)
            conn.execute(f"DELETE FROM videos WHERE task_id IN ({placeholders})", task_ids)
        conn.execute("DELETE FROM crawl_tasks WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM platform_auth WHERE user_id=?", (user_id,))
        conn.execute("DELETE FROM user_sessions WHERE user_id=?", (user_id,))
        cur = conn.execute("DELETE FROM users WHERE id=? AND role!='admin'", (user_id,))
    shutil.rmtree(settings.auth_dir / str(user_id), ignore_errors=True)
    return cur.rowcount > 0


def upsert_auth(user_id: int, platform: str, status: str, storage_path: str | None = None, message: str | None = None) -> None:
    timestamp = now()
    with connection() as conn:
        conn.execute("""INSERT INTO platform_auth(user_id,platform,status,storage_path,authenticated_at,last_checked_at,message)
            VALUES(?,?,?,?,?,?,?) ON CONFLICT(user_id,platform) DO UPDATE SET status=excluded.status,
            storage_path=COALESCE(excluded.storage_path,platform_auth.storage_path),
            authenticated_at=CASE WHEN excluded.status='authenticated' THEN excluded.authenticated_at ELSE platform_auth.authenticated_at END,
            last_checked_at=excluded.last_checked_at,message=excluded.message""",
            (user_id, platform, status, storage_path, timestamp if status == "authenticated" else None, timestamp, message))


def create_task(task_id: str, user_id: int, platform: str, keywords: list[str], count: int, created_time: str, min_likes: int, speed: str) -> None:
    timestamp = now()
    with connection() as conn:
        conn.execute("""INSERT INTO crawl_tasks(id,platform,keywords,requested_count,status,progress,message,error,created_at,updated_at,user_id,created_time_filter,min_likes,crawl_speed)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, platform, json.dumps(keywords, ensure_ascii=False), count, "pending", 0, "任务已创建", None, timestamp, timestamp, user_id, created_time, min_likes, speed))


def update_task(task_id: str, status: str, progress: int, message: str, error: str | None = None) -> None:
    with connection() as conn:
        conn.execute("UPDATE crawl_tasks SET status=?,progress=?,message=?,error=?,updated_at=? WHERE id=?", (status, progress, message, error, now(), task_id))


def add_video(task_id: str, video: dict[str, Any]) -> None:
    raw_video = {key: value for key, value in video.items() if key not in {"thumbnail_data", "thumbnail_content_type"}}
    with connection() as conn:
        cursor = conn.execute("""INSERT INTO videos(task_id,platform,keyword,title,author,url,thumbnail_url,thumbnail_data,thumbnail_content_type,duration,views,raw_json,likes,published_at,media_url)
          VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (task_id, video["platform"], video["keyword"], video["title"], video.get("author"), video.get("url"), video.get("thumbnail_url"), video.get("thumbnail_data"), video.get("thumbnail_content_type"), video.get("duration"), video.get("views"), json.dumps(raw_video, ensure_ascii=False), video.get("likes"), video.get("published_at"), video.get("media_url")))
    return cursor.lastrowid


def add_shared_link_video(user_id: int, platform: str, video: dict[str, Any]) -> dict[str, Any]:
    task_id = f"share-{uuid4().hex}"
    timestamp = now()
    with connection() as conn:
        conn.execute("""INSERT INTO crawl_tasks(id,platform,keywords,requested_count,status,progress,message,error,created_at,updated_at,user_id,created_time_filter,min_likes,crawl_speed)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (task_id, platform, json.dumps([video["keyword"]], ensure_ascii=False), 1, "completed", 100,
                      "已通过分享链接解析", None, timestamp, timestamp, user_id, "any", 0, "manual"))
    video_id = add_video(task_id, video)
    with connection() as conn:
        conn.execute("INSERT INTO analysis_items(video_id,added_at,user_id) VALUES(?,?,?)", (video_id, timestamp, user_id))
        conn.execute("UPDATE videos SET workflow_status='queued' WHERE id=?", (video_id,))
        row = conn.execute("""SELECT ai.id analysis_item_id,ai.added_at,v.id,v.task_id,v.platform,v.keyword,v.title,v.author,v.url,v.thumbnail_url,v.thumbnail_data,v.thumbnail_content_type,v.duration,v.views,v.likes,v.published_at,v.media_url,v.is_inspiration
                              FROM analysis_items ai JOIN videos v ON v.id=ai.video_id WHERE ai.video_id=?""", (video_id,)).fetchone()
    return _public_video(row)


def add_uploaded_video(user_id: int, title: str, local_path: str) -> dict[str, Any]:
    task_id = f"upload-{uuid4().hex}"
    timestamp = now()
    video = {"platform": "本地上传", "keyword": "手动", "title": title[:160], "author": "本地文件",
             "url": None, "thumbnail_url": None, "duration": None, "views": "--", "likes": None,
             "published_at": None, "media_url": None}
    with connection() as conn:
        conn.execute("""INSERT INTO crawl_tasks(id,platform,keywords,requested_count,status,progress,message,error,created_at,updated_at,user_id,created_time_filter,min_likes,crawl_speed)
                      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                     (task_id, "upload", json.dumps(["手动"], ensure_ascii=False), 1, "completed", 100,
                      "已上传本地视频", None, timestamp, timestamp, user_id, "any", 0, "manual"))
    video_id = add_video(task_id, video)
    with connection() as conn:
        conn.execute("UPDATE videos SET local_path=?,workflow_status='queued' WHERE id=?", (local_path, video_id))
        conn.execute("INSERT INTO analysis_items(video_id,added_at,user_id) VALUES(?,?,?)", (video_id, timestamp, user_id))
        row = conn.execute("""SELECT ai.id analysis_item_id,ai.added_at,v.id,v.task_id,v.platform,v.keyword,v.title,v.author,v.url,v.thumbnail_url,v.thumbnail_data,v.thumbnail_content_type,v.duration,v.views,v.likes,v.published_at,v.media_url,v.is_inspiration
                              FROM analysis_items ai JOIN videos v ON v.id=ai.video_id WHERE ai.video_id=?""", (video_id,)).fetchone()
    return _public_video(row)


def get_task(task_id: str, user_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        task = conn.execute("SELECT * FROM crawl_tasks WHERE id=? AND user_id=?", (task_id, user_id)).fetchone()
        if not task: return None
        videos = [_public_video(row) for row in conn.execute("SELECT * FROM videos WHERE task_id=? ORDER BY id", (task_id,))]
    result = dict(task); result["keywords"] = json.loads(result["keywords"]); result["videos"] = videos
    return result


def add_analysis_items(user_id: int, video_ids: list[int]) -> dict[str, int]:
    ids = list(dict.fromkeys(video_ids)); placeholders = ",".join("?" for _ in ids)
    if not ids: return {"added": 0, "total": 0}
    with connection() as conn:
        owned = {r["id"] for r in conn.execute(f"SELECT v.id FROM videos v JOIN crawl_tasks t ON t.id=v.task_id WHERE t.user_id=? AND v.id IN ({placeholders})", [user_id, *ids])}
        before = conn.total_changes
        conn.executemany("INSERT OR IGNORE INTO analysis_items(video_id,added_at,user_id) VALUES(?,?,?)", ((vid, now(), user_id) for vid in owned))
        if owned:
            owned_placeholders = ",".join("?" for _ in owned)
            conn.execute(f"UPDATE videos SET workflow_status='queued' WHERE id IN ({owned_placeholders})", list(owned))
        added = conn.total_changes - before
        total = conn.execute("SELECT COUNT(*) FROM analysis_items WHERE user_id=?", (user_id,)).fetchone()[0]
    return {"added": added, "total": total}


def list_analysis_items(user_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("""SELECT ai.id analysis_item_id,ai.added_at,v.id,v.task_id,v.platform,v.keyword,v.title,v.author,v.url,v.thumbnail_url,v.thumbnail_data,v.thumbnail_content_type,v.duration,v.views,v.likes,v.published_at,v.media_url,v.is_inspiration
          FROM analysis_items ai JOIN videos v ON v.id=ai.video_id
          WHERE ai.user_id=? AND v.workflow_status='queued' ORDER BY v.keyword COLLATE NOCASE,ai.added_at DESC""", (user_id,)).fetchall()
    return [_public_video(row) for row in rows]


def delete_analysis_item(user_id: int, item_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("DELETE FROM analysis_items WHERE id=? AND user_id=?", (item_id, user_id))
    return cur.rowcount > 0


def delete_owned_video(user_id: int, video_id: int) -> bool:
    with connection() as conn:
        cur = conn.execute("""DELETE FROM videos WHERE id=? AND task_id IN
                            (SELECT id FROM crawl_tasks WHERE user_id=?)""", (video_id, user_id))
    return cur.rowcount > 0


def set_video_inspiration(user_id: int, video_id: int, marked: bool) -> bool:
    with connection() as conn:
        workflow_status = "inspiration" if marked else "queued"
        cur = conn.execute("""UPDATE videos SET is_inspiration=?,workflow_status=? WHERE id=? AND task_id IN
                            (SELECT id FROM crawl_tasks WHERE user_id=?)""", (int(marked), workflow_status, video_id, user_id))
        if cur.rowcount and marked:
            timestamp = now()
            conn.execute("DELETE FROM analysis_items WHERE user_id=? AND video_id=?", (user_id, video_id))
            conn.execute("""INSERT INTO inspiration_analyses(user_id,video_id,status,created_at,updated_at)
                          VALUES(?,?,'pending',?,?) ON CONFLICT(user_id,video_id) DO UPDATE SET updated_at=excluded.updated_at""",
                         (user_id, video_id, timestamp, timestamp))
        elif cur.rowcount:
            conn.execute("DELETE FROM inspiration_analyses WHERE user_id=? AND video_id=?", (user_id, video_id))
            conn.execute("INSERT OR IGNORE INTO analysis_items(video_id,added_at,user_id) VALUES(?,?,?)", (video_id, now(), user_id))
    return cur.rowcount > 0


def list_inspiration_analyses(user_id: int) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute("""SELECT ia.id analysis_id,ia.status analysis_status,ia.analysis_text,ia.error analysis_error,
            ia.transcript,ia.video_title,ia.copy_structure,ia.boost_points,ia.model,
            ia.created_at marked_at,ia.updated_at analysis_updated_at,
            u.username,
            v.id,v.task_id,v.platform,v.keyword,v.title,v.author,v.url,v.thumbnail_url,v.thumbnail_data,v.thumbnail_content_type,v.duration,
            v.views,v.likes,v.published_at,v.media_url
            FROM inspiration_analyses ia JOIN videos v ON v.id=ia.video_id
            JOIN users u ON u.id=ia.user_id
            WHERE ia.user_id=? ORDER BY ia.created_at DESC""", (user_id,)).fetchall()
    return [_public_video(row) for row in rows]


def get_owned_video(user_id: int, video_id: int) -> dict[str, Any] | None:
    with connection() as conn:
        row = conn.execute("""SELECT v.* FROM videos v JOIN crawl_tasks t ON t.id=v.task_id
                            WHERE v.id=? AND t.user_id=?""", (video_id, user_id)).fetchone()
    return dict(row) if row else None


def get_owned_thumbnail(user_id: int, video_id: int) -> Optional[Tuple[bytes, str]]:
    with connection() as conn:
        row = conn.execute("""SELECT v.thumbnail_data,v.thumbnail_content_type FROM videos v JOIN crawl_tasks t ON t.id=v.task_id
                            WHERE v.id=? AND t.user_id=?""", (video_id, user_id)).fetchone()
    if not row or not row["thumbnail_data"]:
        return None
    return row["thumbnail_data"], row["thumbnail_content_type"] or "image/jpeg"


def update_video_media_url(user_id: int, video_id: int, media_url: str) -> bool:
    with connection() as conn:
        cur = conn.execute("""UPDATE videos SET media_url=? WHERE id=? AND task_id IN
                            (SELECT id FROM crawl_tasks WHERE user_id=?)""", (media_url, video_id, user_id))
    return cur.rowcount > 0


def is_video_duplicate(user_id: int, video_url: str) -> bool:
    with connection() as conn:
        row = conn.execute(
            "SELECT 1 FROM videos v JOIN crawl_tasks t ON t.id=v.task_id "
            "WHERE t.user_id=? AND v.url=?",
            (user_id, video_url),
        ).fetchone()
    return row is not None


def is_feishu_exported(analysis_id: int) -> bool:
    """Whether this analysis has already been appended to the Feishu table."""
    with connection() as conn:
        row = conn.execute(
            "SELECT feishu_exported FROM inspiration_analyses WHERE id=?", (analysis_id,)
        ).fetchone()
    return bool(row and row["feishu_exported"])


def mark_feishu_exported(analysis_id: int) -> None:
    """Record that this analysis has been appended to the Feishu table."""
    with connection() as conn:
        conn.execute("UPDATE inspiration_analyses SET feishu_exported=1 WHERE id=?", (analysis_id,))


def list_user_tasks(user_id: int, limit: int = 20) -> list[dict[str, Any]]:
    with connection() as conn:
        rows = conn.execute(
            "SELECT id,platform,keywords,requested_count,status,progress,message,error,created_at,updated_at "
            "FROM crawl_tasks WHERE user_id=? ORDER BY created_at DESC, id DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
    result: list[dict[str, Any]] = [dict(row) for row in rows]
    for row in result:
        try:
            row["keywords"] = json.loads(row["keywords"])
        except (json.JSONDecodeError, TypeError):
            row["keywords"] = []
    return result


def get_config(key: str) -> str | None:
    with connection() as conn:
        row = conn.execute("SELECT value FROM app_config WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_config(key: str, value: str) -> None:
    with connection() as conn:
        conn.execute("INSERT INTO app_config(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, value))


def get_user_mimo_key(user_id: int) -> str:
    with connection() as conn:
        row = conn.execute("SELECT mimo_api_key FROM users WHERE id=?", (user_id,)).fetchone()
    return row["mimo_api_key"] if row else ""


def set_user_mimo_key(user_id: int, key: str) -> None:
    with connection() as conn:
        conn.execute("UPDATE users SET mimo_api_key=? WHERE id=?", (key, user_id))
