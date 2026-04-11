from __future__ import annotations

from dataclasses import dataclass, field
import logging
import time


@dataclass
class PerfTimer:
    started_at: float = field(default_factory=time.perf_counter)

    def elapsed_ms(self) -> float:
        return elapsed_ms_since(self.started_at)


def elapsed_ms_since(started_at: float) -> float:
    return (time.perf_counter() - started_at) * 1000.0


def log_timing(
    logger: logging.Logger,
    prefix: str,
    phase: str,
    duration_ms: float,
    **fields,
):
    parts = [
        prefix,
        f"phase={phase}",
        f"duration_ms={duration_ms:.1f}",
    ]
    for key, value in fields.items():
        if value is None:
            continue
        if isinstance(value, str) and not value:
            continue
        parts.append(f"{key}={value}")
    logger.info(" | ".join(parts))
