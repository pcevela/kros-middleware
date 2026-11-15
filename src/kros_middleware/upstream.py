"""HTTP client for the KROS API with client-side throttling."""

from __future__ import annotations

import asyncio
import time

import httpx

from . import __version__
from .config import Settings


class TokenBucket:
    def __init__(self, rate_per_second: float, capacity: int) -> None:
        self.rate = rate_per_second
        self.capacity = capacity
        self.tokens = float(capacity)
        self.updated = time.monotonic()
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            while True:
                now = time.monotonic()
                self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
                self.updated = now
                if self.tokens >= 1:
                    self.tokens -= 1
                    return
                await asyncio.sleep((1 - self.tokens) / self.rate)


class Upstream:
    """Talks to KROS. The real token never leaves this class."""

    # Response headers we pass back to clients; everything else is dropped.
    PASS_HEADERS = (
        "content-type",
        "content-disposition",
        "x-rate-limit-limit",
        "x-rate-limit-remaining",
        "x-rate-limit-reset",
        "retry-after",
    )

    def __init__(self, settings: Settings, transport: httpx.AsyncBaseTransport | None = None) -> None:
        self._token = settings.upstream_token
        self._client = httpx.AsyncClient(
            base_url=settings.upstream_base_url,
            timeout=settings.upstream_timeout,
            follow_redirects=False,
            trust_env=False,
            transport=transport,
            headers={"User-Agent": f"kros-middleware/{__version__}"},
        )
        self._second = TokenBucket(settings.upstream_rate_per_second, settings.upstream_burst)
        self._minute = TokenBucket(settings.upstream_rate_per_minute / 60.0, settings.upstream_rate_per_minute)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: str,
        path: str,
        params: list[tuple[str, str]] | None = None,
        json_body: bytes | None = None,
        accept: str | None = None,
    ) -> httpx.Response:
        if not path.startswith("/api/"):
            raise ValueError("upstream path must start with /api/")
        await self._minute.acquire()
        await self._second.acquire()
        headers = {"Authorization": f"Bearer {self._token}", "Accept": accept or "application/json"}
        if json_body is not None:
            headers["Content-Type"] = "application/json; charset=utf-8"
        return await self._client.request(method, path, params=params, content=json_body, headers=headers)
