import logging
import random
from datetime import datetime
from typing import Optional, Tuple


def compute_sleep_seconds(
    *,
    base_minutes: int,
    optimal_minutes: float,
    dynamic_backoff_minutes: int,
    sleep_jitter_seconds: int,
    is_prime_time: bool,
    backoff_until: Optional[datetime],
) -> Tuple[int, Optional[datetime]]:
    if optimal_minutes < base_minutes:
        adjusted_minutes = optimal_minutes
        logging.debug("Using optimized frequency: %.1f minutes (prime time: %s)", optimal_minutes, is_prime_time)
    else:
        adjusted_minutes = dynamic_backoff_minutes

    base_seconds = max(1, adjusted_minutes) * 60

    jitter = 0
    if sleep_jitter_seconds:
        jitter = random.randint(-sleep_jitter_seconds, sleep_jitter_seconds)

    min_sleep = 15 if is_prime_time else 30
    sleep_seconds = max(min_sleep, base_seconds + jitter)

    if backoff_until:
        now = datetime.now()
        if now < backoff_until:
            backoff_seconds = int((backoff_until - now).total_seconds())
            sleep_seconds = max(sleep_seconds, backoff_seconds)
            logging.debug("Applying scheduled backoff: %s seconds remaining", backoff_seconds)
        else:
            logging.debug("Backoff period expired, resuming normal schedule")
            backoff_until = None

    return int(sleep_seconds), backoff_until
