from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Any, Iterator

from .config import settings


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


@contextmanager
def connection() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(settings.database_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=30000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def initialize() -> None:
    if not settings.database_path.exists():
        raise RuntimeError(f"Database does not exist: {settings.database_path}")
    with connection() as conn:
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(inspiration_analyses)")}
        required = {"transcript", "video_title", "copy_structure", "boost_points", "model", "attempts", "next_retry_at"}
        missing = required - columns
        if missing:
            raise RuntimeError(f"Restart fetch-api once to migrate the database. Missing columns: {', '.join(sorted(missing))}")
        stale_before = (datetime.now(timezone.utc) - timedelta(minutes=30)).isoformat()
        conn.execute("""UPDATE inspiration_analyses SET status='pending',error='分析服务中断，任务已重新排队',updated_at=?
                      WHERE status='processing' AND updated_at<?""", (now(), stale_before))


def claim_next_job() -> dict[str, Any] | None:
    timestamp = now()
    with connection() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute("""SELECT ia.*,v.title,v.keyword,v.platform,v.author,v.duration,v.likes,v.published_at
            FROM inspiration_analyses ia JOIN videos v ON v.id=ia.video_id
            WHERE v.workflow_status='inspiration' AND ia.status='pending'
              AND (ia.next_retry_at IS NULL OR ia.next_retry_at<=?)
            ORDER BY ia.created_at LIMIT 1""", (timestamp,)).fetchone()
        if not row:
            return None
        changed = conn.execute("""UPDATE inspiration_analyses SET status='processing',attempts=attempts+1,error=NULL,updated_at=?
                                WHERE id=? AND status='pending'""", (timestamp, row["id"])).rowcount
        return dict(row) if changed else None


def complete_job(job_id: int, result: dict[str, str], model: str) -> None:
    analysis_text = "\n\n".join((
        f"视频文案\n{result['transcript']}", f"视频标题\n{result['video_title']}",
        f"文案结构\n{result['copy_structure']}", f"起量点\n{result['boost_points']}",
    ))
    with connection() as conn:
        conn.execute("""UPDATE inspiration_analyses SET status='completed',analysis_text=?,transcript=?,video_title=?,copy_structure=?,boost_points=?,model=?,error=NULL,next_retry_at=NULL,updated_at=? WHERE id=?""",
            (analysis_text, result["transcript"], result["video_title"], result["copy_structure"],
             result["boost_points"], model, now(), job_id))


def fail_job(job_id: int, attempts: int, error: str) -> None:
    final = attempts >= settings.max_attempts
    retry_at = None if final else (datetime.now(timezone.utc) + timedelta(minutes=5 * attempts)).isoformat()
    with connection() as conn:
        conn.execute("UPDATE inspiration_analyses SET status=?,error=?,next_retry_at=?,updated_at=? WHERE id=?",
                     ("failed" if final else "pending", error[:1000], retry_at, now(), job_id))


def get_user_mimo_key(user_id: int) -> str:
    with connection() as conn:
        row = conn.execute("SELECT mimo_api_key FROM users WHERE id=?", (user_id,)).fetchone()
    return row["mimo_api_key"] if row else ""
