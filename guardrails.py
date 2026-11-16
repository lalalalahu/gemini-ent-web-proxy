import asyncio
import os
import random
import time
from typing import Any, Dict, Optional, Tuple

from fastapi import HTTPException


RATE_LIMIT_RPM: float = float(os.getenv("RATE_LIMIT_RPM", "20"))
MIN_REQUEST_INTERVAL_SEC: float = float(os.getenv("MIN_REQUEST_INTERVAL_SEC", "0.5"))
CIRCUIT_BREAKER_THRESHOLD: int = int(os.getenv("CIRCUIT_BREAKER_THRESHOLD", "3"))
CIRCUIT_BREAKER_COOLDOWN_SEC: float = float(os.getenv("CIRCUIT_BREAKER_COOLDOWN_SEC", "120"))


class TokenBucketRateLimiter:
    """Gentle anti-runaway token bucket + min-interval jitter for browser submissions."""

    def __init__(self, rpm: float = RATE_LIMIT_RPM, min_interval: float = MIN_REQUEST_INTERVAL_SEC) -> None:
        self.rpm = rpm
        self.min_interval = max(0.0, min_interval)
        self.capacity = max(1.0, min(5.0, rpm / 4.0)) if rpm > 0 else 1.0
        self.tokens = self.capacity
        self.refill_rate = (rpm / 60.0) if rpm > 0 else 0.0
        self._last_refill = time.monotonic()
        self._last_submit = 0.0
        self._lock = asyncio.Lock()

    def _refill(self, now: float) -> None:
        if self.refill_rate <= 0:
            return
        elapsed = max(0.0, now - self._last_refill)
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self._last_refill = now

    async def acquire(self) -> float:
        """Wait if needed to respect RPM and minimum interval, returning total waited seconds."""
        if self.rpm <= 0 and self.min_interval <= 0:
            return 0.0

        waited = 0.0
        async with self._lock:
            while True:
                now = time.monotonic()
                self._refill(now)

                wait_interval = 0.0
                if self.min_interval > 0 and self._last_submit > 0:
                    jitter = random.uniform(0.0, min(0.35, self.min_interval * 0.5))
                    target_next = self._last_submit + self.min_interval + jitter
                    if now < target_next:
                        wait_interval = target_next - now

                wait_token = 0.0
                if self.rpm > 0 and self.tokens < 1.0:
                    wait_token = (1.0 - self.tokens) / self.refill_rate

                sleep_for = max(wait_interval, wait_token)
                if sleep_for <= 0.005:
                    if self.rpm > 0:
                        self.tokens = max(0.0, self.tokens - 1.0)
                    self._last_submit = time.monotonic()
                    break

                await asyncio.sleep(sleep_for)
                waited += sleep_for

        if waited >= 0.5:
            print(f"⏳ [RateLimiter] Paced browser request by {waited:.2f}s", flush=True)
        return waited

    def get_status(self) -> Dict[str, Any]:
        now = time.monotonic()
        self._refill(now)
        return {
            "enabled": self.rpm > 0 or self.min_interval > 0,
            "rpm": self.rpm,
            "min_interval_sec": self.min_interval,
            "available_tokens": round(self.tokens, 2),
            "capacity": round(self.capacity, 2),
        }


class CircuitBreaker:
    """Standard CLOSED / OPEN / HALF_OPEN circuit breaker to protect portal accounts."""

    def __init__(
        self,
        threshold: int = CIRCUIT_BREAKER_THRESHOLD,
        cooldown_sec: float = CIRCUIT_BREAKER_COOLDOWN_SEC,
    ) -> None:
        self.threshold = threshold
        self.cooldown_sec = max(5.0, cooldown_sec)
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self._opened_at = 0.0
        self._last_reason = ""
        self._half_open_in_flight = False

    def before_request(self) -> None:
        if self.threshold <= 0:
            return

        if self.state == "OPEN":
            elapsed = time.monotonic() - self._opened_at
            if elapsed < self.cooldown_sec:
                remaining = max(1, int(self.cooldown_sec - elapsed + 0.999))
                raise HTTPException(
                    status_code=503,
                    detail=(
                        f"Gateway circuit breaker is OPEN ({self._last_reason or 'portal error'}). "
                        f"Retry after {remaining}s."
                    ),
                    headers={"Retry-After": str(remaining)},
                )
            self.state = "HALF_OPEN"
            self._half_open_in_flight = True
            print("🔄 [CircuitBreaker] Cooldown elapsed -> HALF_OPEN (allowing 1 probe request)", flush=True)
            return

        if self.state == "HALF_OPEN":
            if self._half_open_in_flight:
                raise HTTPException(
                    status_code=503,
                    detail="Gateway circuit breaker is HALF_OPEN (probe in progress). Retry after 5s.",
                    headers={"Retry-After": "5"},
                )
            self._half_open_in_flight = True

    def record_success(self) -> None:
        if self.state != "CLOSED" or self.consecutive_failures > 0:
            print("✅ [CircuitBreaker] Portal recovered -> CLOSED", flush=True)
        self.state = "CLOSED"
        self.consecutive_failures = 0
        self._opened_at = 0.0
        self._last_reason = ""
        self._half_open_in_flight = False

    def record_failure(self, reason: str, immediate_open: bool = False) -> None:
        if self.threshold <= 0:
            return

        self._half_open_in_flight = False
        self._last_reason = reason
        self.consecutive_failures += 1

        if self.state == "HALF_OPEN" or immediate_open or self.consecutive_failures >= self.threshold:
            self.state = "OPEN"
            self._opened_at = time.monotonic()
            print(
                f"🚨 [CircuitBreaker] OPENED for {int(self.cooldown_sec)}s "
                f"(reason={reason}, failures={self.consecutive_failures})",
                flush=True,
            )
        else:
            print(
                f"⚠️ [CircuitBreaker] Failure {self.consecutive_failures}/{self.threshold}: {reason}",
                flush=True,
            )

    def get_status(self) -> Dict[str, Any]:
        remaining = 0
        if self.state == "OPEN":
            elapsed = time.monotonic() - self._opened_at
            remaining = max(0, int(self.cooldown_sec - elapsed))
        return {
            "enabled": self.threshold > 0,
            "state": self.state,
            "consecutive_failures": self.consecutive_failures,
            "threshold": self.threshold,
            "cooldown_remaining_sec": remaining,
            "last_reason": self._last_reason or None,
        }


async def detect_page_anomaly(page: Any) -> Optional[Tuple[str, bool]]:
    """Inspect browser page URL/text for SSO redirects or 429/rate-limit signals. Returns (reason, immediate_open)."""
    if page is None:
        return None
    try:
        if page.is_closed():
            return ("Browser tab closed unexpectedly", False)
        url = (page.url or "").lower()
        if any(k in url for k in ("servicelogin", "accounts.google.com/v3/signin", "/signin")):
            return ("SSO session expired / redirected to login", True)

        snippet = await page.evaluate(
            "() => (document.body && document.body.innerText ? document.body.innerText.slice(0, 1500) : '').toLowerCase()"
        )
        if any(
            k in snippet
            for k in (
                "too many requests",
                "rate limit exceeded",
                "quota exceeded",
                "unusual traffic",
                "http error 429",
            )
        ):
            return ("Portal rate limit / 429 detected", True)
    except Exception:
        pass
    return None


rate_limiter = TokenBucketRateLimiter()
circuit_breaker = CircuitBreaker()
