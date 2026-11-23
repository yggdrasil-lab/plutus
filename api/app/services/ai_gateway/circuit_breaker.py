"""Thread-safe, instance-scoped circuit breaker for Jev System One calls."""

from __future__ import annotations

import logging
import threading
import time
from typing import Callable, Optional

logger = logging.getLogger(__name__)

DEFAULT_FAILURE_THRESHOLD = 3
DEFAULT_COOLDOWN_SECONDS = 60.0


class CircuitBreaker:
    """Instance-scoped consecutive-failure circuit breaker with injectable clock."""

    def __init__(
        self,
        failure_threshold: int = DEFAULT_FAILURE_THRESHOLD,
        cooldown_seconds: float = DEFAULT_COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if failure_threshold < 1:
            raise ValueError("failure_threshold must be at least 1.")
        if cooldown_seconds < 0:
            raise ValueError("cooldown_seconds must be non-negative.")
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._clock = clock
        self._lock = threading.Lock()
        self._consecutive_failures = 0
        self._open_until: Optional[float] = None

    def is_open(self) -> bool:
        """Return True while the cooldown window remains active."""
        with self._lock:
            if self._open_until is None:
                return False
            return self._clock() < self._open_until

    def record_success(self) -> None:
        """Clear accumulated failures and close the circuit."""
        self.reset()

    def record_failure(self) -> None:
        """Increment consecutive failures and open the circuit once threshold is reached."""
        with self._lock:
            self._consecutive_failures += 1
            if self._consecutive_failures >= self._failure_threshold:
                self._open_until = self._clock() + self._cooldown_seconds
                logger.warning(
                    "Jev circuit breaker opened for %.0fs after %d consecutive failures.",
                    self._cooldown_seconds,
                    self._consecutive_failures,
                )

    def reset(self) -> None:
        """Reset failure counter and close the circuit immediately."""
        with self._lock:
            self._consecutive_failures = 0
            self._open_until = None
