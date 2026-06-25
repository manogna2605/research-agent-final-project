"""
A deliberately simple in-memory rate limiter, keyed by client IP, using a
rolling window. No extra dependency, no external store required.

This is the right amount of protection for a small self-hosted deployment
running as a single process. It is NOT the right tool if you run multiple
worker processes or multiple machines behind a load balancer -- each
process keeps its own counters, so the effective limit becomes
(limit x number of processes). If you scale out, swap this for a shared
store instead (Redis + the `limits` / `slowapi` libraries are the standard
choice -- see the README).

Also includes GlobalDailyCap: a site-wide ceiling independent of who's
asking. Per-IP limiting alone doesn't protect your API bill once a site is
public -- 50 different visitors each under the per-IP limit can still add
up to a bill you didn't plan for. The daily cap is the backstop.
"""
import time
from collections import defaultdict
from threading import Lock

from fastapi import HTTPException, Request


class RateLimiter:
    def __init__(self, limit: int, window_seconds: int):
        self.limit = limit
        self.window_seconds = window_seconds
        self._hits = defaultdict(list)
        self._lock = Lock()

    def check(self, key: str) -> None:
        """Raises HTTPException(429) if `key` has exceeded the limit within
        the current window; otherwise records the hit and returns None."""
        if self.limit <= 0:
            return  # 0 or negative disables this limiter entirely

        now = time.time()
        with self._lock:
            hits = self._hits[key]
            cutoff = now - self.window_seconds
            while hits and hits[0] < cutoff:
                hits.pop(0)

            if len(hits) >= self.limit:
                retry_after = int(self.window_seconds - (now - hits[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"Rate limit reached: {self.limit} request(s) per "
                        f"{self.window_seconds}s for this client. "
                        f"Try again in {retry_after}s."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )

            hits.append(now)


class GlobalDailyCap:
    """A single shared counter for one endpoint, reset every 24h on a
    rolling basis. Once `limit` requests have been made by ANYONE within
    the last 24 hours, every further request is rejected until the oldest
    one ages out -- independent of which IP is asking."""

    def __init__(self, limit: int, label: str):
        self.limit = limit
        self.label = label
        self._hits = []
        self._lock = Lock()
        self._window = 86400  # 24 hours

    def check(self) -> None:
        if self.limit <= 0:
            return  # 0 or negative disables this cap entirely

        now = time.time()
        with self._lock:
            cutoff = now - self._window
            while self._hits and self._hits[0] < cutoff:
                self._hits.pop(0)

            if len(self._hits) >= self.limit:
                retry_after = int(self._window - (now - self._hits[0])) + 1
                raise HTTPException(
                    status_code=429,
                    detail=(
                        f"This site has hit its daily limit for {self.label} "
                        f"({self.limit} requests/24h, shared across all visitors). "
                        f"This protects the site owner's API budget. Try again later."
                    ),
                    headers={"Retry-After": str(retry_after)},
                )

            self._hits.append(now)

    def remaining(self) -> int:
        if self.limit <= 0:
            return -1  # unlimited
        now = time.time()
        with self._lock:
            cutoff = now - self._window
            while self._hits and self._hits[0] < cutoff:
                self._hits.pop(0)
            return max(0, self.limit - len(self._hits))


def client_key(request: Request) -> str:
    """Identifies the caller. Trusts the first hop of X-Forwarded-For if
    you're behind a reverse proxy that sets it; otherwise falls back to the
    direct connection's IP."""
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
