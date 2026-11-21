"""Retry delays shared by every worker that retries."""

import random


def equal_jitter(attempts: int, base: float, cap: float, rng: random.Random) -> float:
    """Exponential backoff with equal jitter: half fixed, half random.

    The fixed half guarantees a minimum gap; the random half spreads retries
    so a burst of failures does not retry in lockstep.
    """
    ceiling = min(cap, base * 2 ** max(attempts - 1, 0))
    return float(ceiling / 2 + rng.uniform(0, ceiling / 2))
