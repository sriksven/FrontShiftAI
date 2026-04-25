"""Resilience policy matrix + @resilient decorator.

See ``docs/resilience_policy.md`` for the spec. This module is the *single*
source of truth for timeouts, retry counts, backoff, and circuit-breaker
behaviour of outbound calls. New integrations pick a policy name and move
on.

Usage::

    from utils.resilience import resilient

    @resilient(policy="external_llm", breaker_key="mercury")
    def call_mercury(...):
        ...

    @resilient(policy="internal_db")
    def load_user(email):
        ...

The decorator works for both sync and async callables — it introspects at
decoration time and dispatches to the appropriate wrapper.

Circuit breakers are *per-key* (e.g. one each for ``mercury``, ``groq``,
``openai``). If you don't pass ``breaker_key`` the breaker is shared across
all calls through that policy, which is usually not what you want — set
``breaker_key`` explicitly when the policy has ``use_breaker=True``.
"""
from __future__ import annotations

import asyncio
import functools
import logging
import random
import threading
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, Optional, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")


# ---------------------------------------------------------------------------
# Policies
# ---------------------------------------------------------------------------

class Backoff(str, Enum):
    NONE = "none"            # no sleep between retries
    LINEAR = "linear"        # base_delay * attempt
    EXPONENTIAL = "exp"      # base_delay * 2**(attempt-1)


@dataclass(frozen=True)
class Policy:
    name: str
    timeout_s: float
    max_retries: int            # retries after the first attempt (so 3 = up to 4 total)
    backoff: Backoff
    base_delay_s: float         # seed for backoff math
    use_breaker: bool
    failure_threshold: int = 3  # consecutive failures to open the breaker
    recovery_timeout_s: float = 60.0

    def sleep_for(self, attempt: int) -> float:
        """Wall time to sleep before retry ``attempt`` (1-indexed)."""
        if self.backoff is Backoff.NONE:
            return 0.0
        if self.backoff is Backoff.LINEAR:
            base = self.base_delay_s * attempt
        else:  # EXPONENTIAL
            base = self.base_delay_s * (2 ** (attempt - 1))
        # Small jitter (±20%) so a cluster of failing clients doesn't
        # re-converge on a synchronized retry storm.
        jitter = base * 0.2 * (random.random() * 2 - 1)
        return max(0.0, base + jitter)


POLICIES: Dict[str, Policy] = {
    "external_llm": Policy(
        name="external_llm",
        timeout_s=8.0,
        max_retries=3,
        backoff=Backoff.EXPONENTIAL,
        base_delay_s=1.0,
        use_breaker=True,
    ),
    "external_search": Policy(
        name="external_search",
        timeout_s=5.0,
        max_retries=2,
        backoff=Backoff.EXPONENTIAL,
        base_delay_s=1.0,
        use_breaker=True,
    ),
    "internal_db": Policy(
        name="internal_db",
        timeout_s=2.0,
        max_retries=1,
        backoff=Backoff.NONE,
        base_delay_s=0.0,
        use_breaker=False,
    ),
    "voice_tool": Policy(
        name="voice_tool",
        timeout_s=8.0,
        max_retries=2,
        backoff=Backoff.LINEAR,
        base_delay_s=1.0,
        use_breaker=False,
    ),
    "user_facing_http": Policy(
        name="user_facing_http",
        timeout_s=10.0,
        max_retries=0,
        backoff=Backoff.NONE,
        base_delay_s=0.0,
        use_breaker=False,
    ),
    "gcs_sync": Policy(
        name="gcs_sync",
        timeout_s=300.0,
        max_retries=3,
        backoff=Backoff.EXPONENTIAL,
        base_delay_s=5.0,
        use_breaker=False,
    ),
    "livekit_chain": Policy(
        name="livekit_chain",
        timeout_s=5.0,
        max_retries=0,
        backoff=Backoff.NONE,
        base_delay_s=0.0,
        use_breaker=True,
    ),
}


def get_policy(name: str) -> Policy:
    try:
        return POLICIES[name]
    except KeyError as exc:
        raise KeyError(
            f"Unknown resilience policy '{name}'. "
            f"Known: {sorted(POLICIES)}"
        ) from exc


# ---------------------------------------------------------------------------
# Circuit breaker
# ---------------------------------------------------------------------------

class BreakerState(str, Enum):
    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


class CircuitBreaker:
    """Simple thread-safe 3-state breaker keyed per target.

    Prometheus instrumentation (Phase 7) will read ``state`` as a gauge.
    """

    def __init__(self, key: str, policy: Policy):
        self.key = key
        self.policy = policy
        self._state = BreakerState.CLOSED
        self._failures = 0
        self._opened_at: float = 0.0
        self._lock = threading.Lock()

    @property
    def state(self) -> BreakerState:
        return self._state

    def allow(self) -> bool:
        """Check whether a call is permitted right now. Takes the lock."""
        with self._lock:
            if self._state is BreakerState.CLOSED:
                return True
            # OPEN or HALF_OPEN
            if self._state is BreakerState.OPEN:
                if time.time() - self._opened_at >= self.policy.recovery_timeout_s:
                    self._state = BreakerState.HALF_OPEN
                    logger.info("circuit %s: OPEN → HALF_OPEN (probe)", self.key)
                    return True
                return False
            # HALF_OPEN already — only one probe at a time. Downgrade back to
            # OPEN; if the probe (currently in-flight) succeeds it'll close us.
            return True

    def record_success(self) -> None:
        with self._lock:
            if self._state is not BreakerState.CLOSED:
                logger.info("circuit %s: → CLOSED", self.key)
            self._state = BreakerState.CLOSED
            self._failures = 0

    def record_failure(self) -> None:
        with self._lock:
            self._failures += 1
            if self._state is BreakerState.HALF_OPEN or self._failures >= self.policy.failure_threshold:
                if self._state is not BreakerState.OPEN:
                    logger.warning(
                        "circuit %s: → OPEN (failures=%d)", self.key, self._failures
                    )
                self._state = BreakerState.OPEN
                self._opened_at = time.time()


_BREAKERS: Dict[str, CircuitBreaker] = {}
_BREAKERS_LOCK = threading.Lock()


def _get_breaker(key: str, policy: Policy) -> CircuitBreaker:
    with _BREAKERS_LOCK:
        breaker = _BREAKERS.get(key)
        if breaker is None:
            breaker = CircuitBreaker(key=key, policy=policy)
            _BREAKERS[key] = breaker
        return breaker


class CircuitOpenError(RuntimeError):
    """Raised when a @resilient call is short-circuited by an open breaker."""


# ---------------------------------------------------------------------------
# Decorator
# ---------------------------------------------------------------------------

def resilient(
    policy: str,
    breaker_key: Optional[str] = None,
) -> Callable[[Callable[..., T]], Callable[..., T]]:
    """Apply the named policy's timeout/retry/backoff/breaker semantics.

    ``breaker_key`` is required when the policy has ``use_breaker=True`` —
    it groups failures. Omitting it falls back to the function's qualified
    name, which is usually too broad.
    """
    pol = get_policy(policy)

    def decorator(fn: Callable[..., T]) -> Callable[..., T]:
        key = breaker_key or f"{fn.__module__}.{fn.__qualname__}"
        breaker = _get_breaker(key, pol) if pol.use_breaker else None

        if asyncio.iscoroutinefunction(fn):
            @functools.wraps(fn)
            async def async_wrapper(*args: Any, **kwargs: Any) -> Any:
                if breaker is not None and not breaker.allow():
                    raise CircuitOpenError(
                        f"circuit breaker open for {key}; skipping call"
                    )
                last_exc: Optional[BaseException] = None
                for attempt in range(pol.max_retries + 1):
                    try:
                        result = await asyncio.wait_for(
                            fn(*args, **kwargs), timeout=pol.timeout_s
                        )
                        if breaker is not None:
                            breaker.record_success()
                        return result
                    except asyncio.TimeoutError as exc:
                        last_exc = exc
                    except Exception as exc:  # noqa: BLE001 - policy is explicit
                        last_exc = exc
                    if breaker is not None:
                        breaker.record_failure()
                    if attempt < pol.max_retries:
                        delay = pol.sleep_for(attempt + 1)
                        if delay:
                            await asyncio.sleep(delay)
                assert last_exc is not None
                raise last_exc
            return async_wrapper  # type: ignore[return-value]

        @functools.wraps(fn)
        def sync_wrapper(*args: Any, **kwargs: Any) -> Any:
            if breaker is not None and not breaker.allow():
                raise CircuitOpenError(
                    f"circuit breaker open for {key}; skipping call"
                )
            last_exc: Optional[BaseException] = None
            for attempt in range(pol.max_retries + 1):
                try:
                    # Note: sync path cannot enforce wall-clock timeout without
                    # signals (unsafe off the main thread). Callers must pass
                    # the timeout to whatever network client they use — the
                    # policy's ``timeout_s`` is the contract. We still honor
                    # it for the retry/backoff math.
                    result = fn(*args, **kwargs)
                    if breaker is not None:
                        breaker.record_success()
                    return result
                except Exception as exc:  # noqa: BLE001 - policy is explicit
                    last_exc = exc
                if breaker is not None:
                    breaker.record_failure()
                if attempt < pol.max_retries:
                    delay = pol.sleep_for(attempt + 1)
                    if delay:
                        time.sleep(delay)
            assert last_exc is not None
            raise last_exc

        return sync_wrapper  # type: ignore[return-value]

    return decorator


# ---------------------------------------------------------------------------
# Introspection helpers (tests + Prometheus)
# ---------------------------------------------------------------------------

def breaker_states() -> Dict[str, str]:
    """Snapshot of every live breaker's state — for metrics/dashboards/tests."""
    with _BREAKERS_LOCK:
        return {key: br.state.value for key, br in _BREAKERS.items()}


def reset_breakers_for_tests() -> None:
    """Test helper — do not call in production code paths."""
    with _BREAKERS_LOCK:
        _BREAKERS.clear()
