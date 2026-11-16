"""Shared result type."""

from __future__ import annotations

from dataclasses import dataclass

CRITICAL = "critical"
WARNING = "warning"


@dataclass
class Result:
    name: str
    ok: bool
    detail: str = ""
    severity: str = CRITICAL      # how bad it is when ok is False
    skipped: bool = False         # check could not run (feature not deployed / not configured)
    ms: int = 0

    @property
    def failed(self) -> bool:
        return not self.ok and not self.skipped
