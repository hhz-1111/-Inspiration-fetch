"""Minimal proxy pool for platform API requests.

Proxies are consumed round-robin, one proxy per client session, so an account
keeps a stable egress IP for the whole crawl instead of churning per request.
"""
from __future__ import annotations

import itertools
from typing import Iterator


class ProxyPool:
    def __init__(self, proxies: str) -> None:
        self._items: list[str] = [p.strip() for p in proxies.split(",") if p.strip()]
        self._cursor: Iterator[str] = itertools.cycle(self._items)

    @property
    def enabled(self) -> bool:
        return bool(self._items)

    @property
    def size(self) -> int:
        return len(self._items)

    def pick(self) -> str | None:
        """Next proxy in round-robin order, or None when the pool is empty."""
        return next(self._cursor) if self._items else None
