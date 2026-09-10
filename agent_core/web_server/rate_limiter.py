"""In-memory IP rate limiter for public webhook endpoints.

Built for the LINE webhook (/line/webhook). HMAC signature verification is
cheap but not free, and a few million spoofed signatures still cost Cloud
Run request slots if nothing stops them at the door. This module gives the
app a quick "drop the request before doing real work" tier.

Design: token bucket per (key) — refill at `rate` tokens/sec, capped at
`burst`. Stored in a process-local dict guarded by a lock. On Cloud Run
each instance has its own bucket; that's fine — auto-scaling spins more
instances under real load but an attacker spamming a single instance
still gets capped, and instances cycle quickly enough that no persistent
state matters.

What this does NOT solve: a coordinated attack spread across many IPs.
Cloud Armor / Cloudflare in front of Cloud Run is the right answer for
that. This limiter just blocks the dumb-spray case.
"""
from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _Bucket:
    tokens: float
    last_refill: float


class IPRateLimiter:
    """Token-bucket rate limiter keyed by an arbitrary string (caller picks
    what to key on — IP, hashed IP, etc.).

    `rate`: tokens added per second.
    `burst`: max tokens held (also the initial credit a new key gets).
    Default 1 req/sec with a burst of 10 — generous for a webhook whose
    real traffic is at most a few per second.
    """

    def __init__(self, rate: float = 1.0, burst: float = 10.0):
        self._rate = float(rate)
        self._burst = float(burst)
        self._buckets: dict[str, _Bucket] = {}
        self._lock = threading.Lock()
        # Soft cap on the dict so a unique-key spray (each request from a
        # different fake key) cannot grow memory unboundedly. We evict
        # the oldest-touched bucket when we hit the cap.
        self._max_keys = 10_000

    def allow(self, key: str) -> bool:
        """Consume 1 token from `key`'s bucket. Return True if allowed,
        False if the bucket was empty (i.e. caller should reject)."""
        now = time.monotonic()
        with self._lock:
            bucket = self._buckets.get(key)
            if bucket is None:
                if len(self._buckets) >= self._max_keys:
                    self._evict_one()
                bucket = _Bucket(tokens=self._burst, last_refill=now)
                self._buckets[key] = bucket
            elapsed = max(0.0, now - bucket.last_refill)
            bucket.tokens = min(self._burst, bucket.tokens + elapsed * self._rate)
            bucket.last_refill = now
            if bucket.tokens >= 1.0:
                bucket.tokens -= 1.0
                return True
            return False

    def _evict_one(self) -> None:
        # Pop a single key; ordering of dict preserves insertion order in
        # Python 3.7+, and we touch buckets on every allow() so newer keys
        # tend to be later in iteration order. Drop the first.
        try:
            oldest = next(iter(self._buckets))
            del self._buckets[oldest]
        except StopIteration:
            pass

    def reset(self) -> None:
        """Testing helper — wipe all buckets."""
        with self._lock:
            self._buckets.clear()


def client_ip(scope_headers: list[tuple[bytes, bytes]] | None, fallback: str = "") -> str:
    """Extract the originating IP for rate-limit keying.

    Cloud Run sets X-Forwarded-For to the client's IP; fall back to the
    direct peer address or the supplied fallback when neither is present.
    """
    if not scope_headers:
        return fallback or "unknown"
    for name, value in scope_headers:
        if name.lower() == b"x-forwarded-for":
            forwarded = value.decode("ascii", errors="replace").strip()
            if forwarded:
                parts = [p.strip() for p in forwarded.split(",") if p.strip()]
                if not parts:
                    break
                # XFF 左→右 = client, proxy1, proxy2…。最左是 client 自己宣稱、可偽造
                # （append 式 LB 後變每請求一桶、繞過 per-IP 限流）。設
                # RED_RATE_LIMIT_TRUSTED_HOPS=N（你這邊有 N 層可信代理）→ 取右數第 N+1
                # 個 = 最外層可信代理看到的真實來源。預設 0 = 維持取最左（不改既有行為、不破
                # Cloud Run）；本限流屬防禦縱深，真閘是 line_bot 的 HMAC 簽章驗證。（健檢 Low）
                from agent_core.env_utils import env_int
                hops = env_int("RED_RATE_LIMIT_TRUSTED_HOPS", 0, min_value=0)
                if hops and len(parts) > hops:
                    return parts[-(hops + 1)]
                return parts[0]
    return fallback or "unknown"
