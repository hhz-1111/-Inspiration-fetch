from typing import Any, Optional

from fastapi import Cookie, Depends, HTTPException

from .db import get_user_by_session
from .security import token_hash


def current_user(fetch_session: Optional[str] = Cookie(default=None)) -> dict[str, Any]:
    if not fetch_session:
        raise HTTPException(401, "请先登录")
    user = get_user_by_session(token_hash(fetch_session))
    if not user or user["status"] != "approved":
        raise HTTPException(401, "登录已失效")
    return user


def admin_user(user: dict[str, Any] = Depends(current_user)) -> dict[str, Any]:
    if user["role"] != "admin":
        raise HTTPException(403, "仅管理员可以执行此操作")
    return user
