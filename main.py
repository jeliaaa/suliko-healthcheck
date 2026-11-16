"""Suliko hourly health check. One run = run every check, alert on change, exit.

    python main.py                  normal run (what the scheduled task does)
    python main.py --dry-run        run + print results, never alert, never touch state
    python main.py --only api,auth  run only these groups/checks
    python main.py --test-alert     send a test email + SMS and exit
    python main.py --doctor         show which settings are missing
    python main.py --list           list the checks

Exit codes: 0 all passing | 1 failures found (alerts sent) | 2 could not deliver an alert / crashed
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import sys
from datetime import timedelta

import requests

from hc import __version__
from hc.config import Config, env
from hc.notify import notify, send_email, send_sms
from hc.runner import run_checks
from hc.state import Decision, load_state, save_state, update_and_decide, utcnow

log = logging.getLogger("hc")


def setup_logging(cfg: Config, verbose: bool) -> None:
    path = cfg.path("logging", "file")
    path.parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fh = logging.handlers.RotatingFileHandler(
        path, maxBytes=cfg.get("logging", "max_bytes", 2_000_000),
        backupCount=cfg.get("logging", "backup_count", 14), encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    root.addHandler(fh)
    root.addHandler(sh)


def print_table(results) -> None:
    width = max((len(r.name) for r in results), default=10)
    for r in results:
        tag = "SKIP" if r.skipped else ("OK  " if r.ok else ("FAIL" if r.severity == "critical" else "WARN"))
        log.info("%s  %-*s  %s", tag, width, r.name, r.detail)


def heartbeat(suffix: str = "") -> None:
    url = env("HC_HEARTBEAT_URL")
    if not url:
        return
    try:
        requests.get(url.rstrip("/") + suffix, timeout=15)
    except requests.RequestException as exc:
        log.warning("heartbeat ping failed: %s", type(exc).__name__)


def doctor(cfg: Config) -> int:
    items = {
        "HC_TEST_USERNAME": "sign-in / SSO / job / payment checks",
        "HC_TEST_PASSWORD": "sign-in / SSO / job / payment checks",
        "HC_SMTP_USERNAME": "email alerts + smtp_login check",
        "HC_SMTP_PASSWORD": "email alerts + smtp_login check",
        "HC_SMS_API_KEY": "SMS alerts",
        "HC_SMS_NUMBERS": "SMS alerts",
        "HC_WEB_SESSION_KEY": "(optional) web-session sign-in check",
        "HC_OFFICE_KEY": "(optional) Office SSO redeem check",
        "HC_HEARTBEAT_URL": "(optional) dead-man's switch",
    }
    for key, why in items.items():
        print(f"{'set    ' if env(key) else 'MISSING'}  {key:20s} {why}")
    print(f"\nAPI: {cfg.get('api', 'base_url')}   alerts -> {cfg.get('alerts', 'email_to')}")
    return 0


def test_alert(cfg: Config) -> int:
    ok = True
    try:
        send_email(cfg, "[SULIKO] TEST alert from health checker", "This is a test. Alerting works.\n")
        print("email: sent")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"email: FAILED {type(exc).__name__}: {exc}")
    try:
        send_sms("SULIKO TEST: health checker alert path works.")
        print("sms: sent")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"sms: FAILED {type(exc).__name__}: {exc}")
    return 0 if ok else 2


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--only", help="comma-separated groups or check names")
    ap.add_argument("--test-alert", action="store_true")
    ap.add_argument("--doctor", action="store_true")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)

    cfg = Config.load()
    if args.doctor:
        return doctor(cfg)
    if args.test_alert:
        return test_alert(cfg)
    if args.list:
        from hc.checks import REGISTRY
        for c in REGISTRY:
            print(f"{c.group:8s} {c.name}")
        return 0

    setup_logging(cfg, args.verbose)
    log.info("=== health run v%s -> %s ===", __version__, cfg.get("api", "base_url"))

    only = {p.strip() for p in args.only.split(",")} if args.only else None
    results = run_checks(cfg, only)
    print_table(results)

    failing = [r for r in results if r.failed]
    if args.dry_run:
        log.info("dry run: %d failing, no alerts sent, state untouched", len(failing))
        return 1 if failing else 0

    state_path = cfg.path("state", "file")
    state = load_state(state_path)
    decision: Decision = update_and_decide(
        state, results, utcnow(),
        timedelta(hours=cfg.get("alerts", "repeat_after_hours", 6)),
        cfg.get("alerts", "send_recovery", True),
    )

    delivered = True
    if decision.kind != "none":
        log.info("decision: %s (failing=%s new=%s)", decision.kind, decision.failing, decision.new)
        delivered = notify(cfg, decision, results, state)

    if delivered:
        save_state(state_path, state)
    else:
        # Nobody was told. Keep tracking the failures, but forget that we "alerted" so the
        # next run tries again instead of believing the message went out.
        _save_without_alert_marks(state_path, state)

    if not delivered:
        heartbeat("/fail")
        log.error("ALERT NOT DELIVERED on any channel")
        return 2
    heartbeat()
    return 1 if failing else 0


def _save_without_alert_marks(path, state: dict) -> None:
    """Keep failure tracking but forget that we alerted, so the next run alerts again."""
    state["alerted"] = []
    state["last_alert_at"] = None
    save_state(path, state)


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:  # noqa: BLE001 - last resort: say we crashed, on whatever channel works
        logging.getLogger("hc").exception("health checker crashed")
        try:
            cfg = Config.load()
            send_email(cfg, "[SULIKO] health checker CRASHED", "The health checker itself crashed. See logs/healthcheck.log.")
        except Exception:  # noqa: BLE001
            pass
        try:
            send_sms("SULIKO: health checker itself CRASHED. See logs.")
        except Exception:  # noqa: BLE001
            pass
        heartbeat("/fail")
        sys.exit(2)
