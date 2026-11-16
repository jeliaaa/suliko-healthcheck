"""Persistent state + the alert de-duplication rules.

Without this, an hourly checker texts you 24 times a day during a single outage. The rules:

  * alert immediately when a check starts failing that we have not already told you about;
  * while the same failures persist, remind every `repeat_after_hours`;
  * when everything is green again after an alert, send one "recovered" message.

`decide()` is a pure function so it can be unit-tested (tests/test_state.py).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .model import CRITICAL, Result


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


@dataclass
class Decision:
    kind: str                                   # "none" | "alert" | "reminder" | "recovery"
    failing: list[str] = field(default_factory=list)
    new: list[str] = field(default_factory=list)
    resolved: list[str] = field(default_factory=list)
    has_critical: bool = False


def load_state(path: Path) -> dict:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            data.setdefault("checks", {})
            data.setdefault("alerted", [])
            data.setdefault("last_alert_at", None)
            return data
    except (OSError, ValueError):
        pass  # missing or corrupt state must never stop the checks from running
    return {"checks": {}, "alerted": [], "last_alert_at": None}


def save_state(path: Path, state: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")
    os.replace(tmp, path)  # atomic: a crash mid-write cannot corrupt the state file


def update_and_decide(
    state: dict,
    results: list[Result],
    now: datetime,
    repeat_after: timedelta,
    send_recovery: bool = True,
) -> Decision:
    """Fold this run's results into `state` (mutated) and decide whether to alert."""
    checks: dict = state["checks"]

    for r in results:
        if r.skipped:
            continue  # unknown, so leave whatever we knew before untouched
        entry = checks.get(r.name)
        if r.ok:
            checks.pop(r.name, None)
        else:
            if entry is None:
                entry = {"since": _iso(now), "runs": 0}
            entry["runs"] += 1
            entry["detail"] = r.detail
            entry["severity"] = r.severity
            checks[r.name] = entry

    failing = sorted(checks)
    alerted = set(state["alerted"])
    last_alert_at = _parse(state["last_alert_at"])
    has_critical = any(checks[n]["severity"] == CRITICAL for n in failing)

    if not failing:
        was_critical = bool(state.get("alerted_critical"))
        state["alerted"] = []
        state["last_alert_at"] = None
        state["alerted_critical"] = False
        if alerted and send_recovery:
            # has_critical here means "the alert we are recovering from was critical", so a
            # recovery text only follows an alert text.
            return Decision("recovery", resolved=sorted(alerted), has_critical=was_critical)
        return Decision("none")

    new = sorted(set(failing) - alerted)
    resolved = sorted(alerted - set(failing))

    if new:
        state["alerted"] = failing
        state["last_alert_at"] = _iso(now)
        state["alerted_critical"] = has_critical or bool(state.get("alerted_critical"))
        return Decision("alert", failing=failing, new=new, resolved=resolved, has_critical=has_critical)

    if last_alert_at is None or now - last_alert_at >= repeat_after:
        state["alerted"] = failing
        state["last_alert_at"] = _iso(now)
        state["alerted_critical"] = has_critical or bool(state.get("alerted_critical"))
        return Decision("reminder", failing=failing, resolved=resolved, has_critical=has_critical)

    # Same failures, already told: stay quiet, but forget anything that recovered so that
    # if it breaks again it counts as new.
    state["alerted"] = sorted(alerted & set(failing))
    return Decision("none", failing=failing, resolved=resolved)
