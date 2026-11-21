"""Fault profile and the single seeded random source behind every decision."""

import random
import threading
from dataclasses import asdict, dataclass, replace
from typing import Any


@dataclass(frozen=True)
class FaultProfile:
    decline_rate: float = 0.0  # charge answered "declined"
    slow_rate: float = 0.0  # respond after slow_seconds (still within timeout)
    slow_seconds: float = 0.5
    unavailable_rate: float = 0.0  # 503, nothing recorded
    charge_then_timeout_rate: float = 0.0  # record the charge, then hang
    refund_fail_rate: float = 0.0  # refund answered "failed"
    refund_then_timeout_rate: float = 0.0  # record the refund, then hang
    webhook_drop_rate: float = 0.0  # never send the webhook (until redelivered)
    webhook_duplicate_rate: float = 0.0  # send the same webhook twice
    webhook_delay_max_seconds: float = 0.0  # send after a random delay up to this
    # Settlement files: per-line faults, decided per reference (stable)
    settlement_amount_mismatch_rate: float = 0.0
    settlement_missing_rate: float = 0.0
    settlement_duplicate_rate: float = 0.0
    settlement_extra_rate: float = 0.0  # per file: one line for a charge never made

    def __post_init__(self) -> None:
        for name, value in asdict(self).items():
            if name.endswith("_rate") and not 0.0 <= value <= 1.0:
                raise ValueError(f"{name} must be within [0, 1], got {value}")
        if self.slow_seconds < 0:
            raise ValueError("slow_seconds must be >= 0")
        if self.webhook_delay_max_seconds < 0:
            raise ValueError("webhook_delay_max_seconds must be >= 0")


class Faults:
    """Thread-safe holder of the active profile and the seeded RNG."""

    def __init__(self, profile: FaultProfile | None = None, seed: int = 0) -> None:
        self._lock = threading.Lock()
        self._profile = profile or FaultProfile()
        self._seed = seed
        self._rng = random.Random(seed)

    def configure(self, profile: FaultProfile, seed: int | None = None) -> None:
        with self._lock:
            self._profile = profile
            if seed is not None:
                self._seed = seed
                self._rng = random.Random(seed)

    def update(self, **changes: Any) -> None:
        with self._lock:
            self._profile = replace(self._profile, **changes)

    @property
    def profile(self) -> FaultProfile:
        return self._profile

    @property
    def seed(self) -> int:
        return self._seed

    def roll(self, rate: float) -> bool:
        """One draw per decision, in request order, from the seeded RNG."""
        with self._lock:
            draw = self._rng.random()
        return draw < rate

    def uniform(self, low: float, high: float) -> float:
        with self._lock:
            return self._rng.uniform(low, high)
