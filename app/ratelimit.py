import time
from collections import defaultdict, deque
from dataclasses import dataclass

from app.errors import rate_limited

# ponytail: in-memory sliding window, correct for a single-process local
# deployment only. Move to Redis (INCR + EXPIRE) if this ever runs replicated.
_hits: dict[str, deque[float]] = defaultdict(deque)


@dataclass
class RateLimitInfo:
    limit: int
    remaining: int
    reset: int  # unix epoch seconds when the window fully clears

    def headers(self) -> dict[str, str]:
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(self.remaining),
            "X-RateLimit-Reset": str(self.reset),
        }


def _check(bucket: str, limit: int, window_s: int) -> RateLimitInfo:
    now = time.time()
    q = _hits[bucket]
    while q and q[0] <= now - window_s:
        q.popleft()
    if len(q) >= limit:
        reset = int(q[0] + window_s)
        raise rate_limited(int(window_s - (now - q[0])) + 1, limit=limit, reset=reset)
    q.append(now)
    reset = int(q[0] + window_s) if q else int(now + window_s)
    return RateLimitInfo(limit=limit, remaining=max(0, limit - len(q)), reset=reset)


def check_scan_limits(key_id: str, *, per_min: int, per_day: int) -> RateLimitInfo:
    info = _check(f"scan:min:{key_id}", per_min, 60)
    _check(f"scan:day:{key_id}", per_day, 86400)
    return info  # per-minute bucket is the one reported in X-RateLimit-* (spec §9 example)


def check_live_lookup_budget(key_id: str, *, per_day: int) -> None:
    _check(f"live:day:{key_id}", per_day, 86400)


def check_read_limit(key_id: str, *, per_min: int) -> RateLimitInfo:
    return _check(f"read:min:{key_id}", per_min, 60)
