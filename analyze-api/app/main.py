import asyncio
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from .config import settings
from .db import initialize
from .worker import process_pending_jobs, scheduler


@asynccontextmanager
async def lifespan(_: FastAPI):
    initialize()
    stop_event = asyncio.Event()
    task = asyncio.create_task(scheduler(stop_event))
    yield
    stop_event.set()
    await task


app = FastAPI(title="Inspiration Analyze API", version="0.1.0", lifespan=lifespan)


@app.get("/api/health")
async def health() -> dict[str, object]:
    return {"status": "ok", "mimo_configured": bool(settings.mimo_api_key), "scan_interval_seconds": settings.scan_interval_seconds}


@app.post("/api/jobs/run-once")
async def run_once(x_internal_token: str = Header(default="")) -> dict[str, int]:
    if not secrets.compare_digest(x_internal_token, settings.internal_api_token):
        raise HTTPException(401, "内部服务认证失败")
    return {"processed": await process_pending_jobs()}
