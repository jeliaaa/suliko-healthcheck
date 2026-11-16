"""Runs the registered checks with timing, exception capture and in-run retries."""

from __future__ import annotations

import logging
import time

from . import checks as checks_module
from .checks import REGISTRY, Check, Ctx
from .config import Config
from .model import Result

log = logging.getLogger("hc.runner")


def _run_once(chk: Check, ctx: Ctx) -> list[Result]:
    start = time.monotonic()
    try:
        out = chk.fn(ctx)
        results = out if isinstance(out, list) else [out]
    except Exception as exc:  # noqa: BLE001 - a crashing check is itself a finding
        results = [Result(chk.name, False, f"{type(exc).__name__}: {exc}")]
    elapsed = int((time.monotonic() - start) * 1000)
    for r in results:
        r.ms = elapsed
    return results


def run_checks(cfg: Config, only: set[str] | None = None) -> list[Result]:
    ctx = Ctx(cfg)
    retries = max(0, int(cfg.get("alerts", "retries", 1)))
    delay = float(cfg.get("alerts", "retry_delay_seconds", 20))
    all_results: list[Result] = []

    for chk in REGISTRY:
        if only and chk.group not in only and chk.name not in only:
            continue
        results = _run_once(chk, ctx)
        attempt = 0
        while chk.retry and attempt < retries and any(r.failed for r in results):
            attempt += 1
            log.info("%s failed, retry %d/%d in %.0fs", chk.name, attempt, retries, delay)
            time.sleep(delay)
            results = _run_once(chk, ctx)
        all_results.extend(results)
    return all_results


__all__ = ["run_checks", "checks_module"]
