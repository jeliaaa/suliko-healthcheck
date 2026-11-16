"""Alert delivery: email (SMTP) and SMS (smsoffice.ge, the gateway the API already uses)."""

from __future__ import annotations

import html
import logging
import re
import smtplib
import socket
import ssl
from datetime import datetime
from email.message import EmailMessage
from email.utils import formataddr

import requests

from .config import Config, env
from .model import CRITICAL, Result
from .state import Decision

log = logging.getLogger("hc.notify")

SMS_API_URL = "https://smsoffice.ge/api/v2/send/"   # same endpoint as SmsNotificationService
SMS_SENDER = "SULIKO.GE"                             # same sender name the API uses


# ----------------------------------------------------------------------------- email

def send_email(cfg: Config, subject: str, text: str, html_body: str | None = None) -> None:
    host = env("HC_SMTP_HOST", "smtp.gmail.com")
    port = int(env("HC_SMTP_PORT", "587"))
    user = env("HC_SMTP_USERNAME")
    password = env("HC_SMTP_PASSWORD")
    sender = env("HC_SMTP_FROM", user)
    recipients = cfg.get("alerts", "email_to", ["info@th.com.ge"])
    if not (user and password):
        raise RuntimeError("HC_SMTP_USERNAME / HC_SMTP_PASSWORD are not set")

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = formataddr((env("HC_SMTP_FROM_NAME", "Suliko HealthCheck"), sender))
    msg["To"] = ", ".join(recipients)
    msg.set_content(text)
    if html_body:
        msg.add_alternative(html_body, subtype="html")

    ctx = ssl.create_default_context()
    if port == 465:
        with smtplib.SMTP_SSL(host, port, timeout=30, context=ctx) as smtp:
            smtp.login(user, password)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(host, port, timeout=30) as smtp:
            smtp.starttls(context=ctx)
            smtp.login(user, password)
            smtp.send_message(msg)


# ------------------------------------------------------------------------------ sms

def normalize_phone(raw: str) -> str:
    """Same rule as SmsNotificationService.NormalizePhone: 5XXXXXXXX / 05XXXXXXXX -> +9955XXXXXXXX."""
    s = re.sub(r"\s+", "", raw)
    if re.fullmatch(r"0?5\d{8}", s):
        return "+995" + re.sub(r"^0?", "", s)
    return s


def _ci_get(data: dict, key: str):
    for k, v in data.items():
        if k.lower() == key.lower():
            return v
    return None


def send_sms(text: str) -> None:
    api_key = env("HC_SMS_API_KEY")
    numbers = [n for n in env("HC_SMS_NUMBERS").split(",") if n.strip()]
    if not api_key or not numbers:
        raise RuntimeError("HC_SMS_API_KEY / HC_SMS_NUMBERS are not set")

    errors = []
    for number in numbers:
        destination = normalize_phone(number)
        try:
            resp = requests.post(
                SMS_API_URL,
                data={"key": api_key, "destination": destination, "sender": SMS_SENDER, "content": text},
                timeout=30,
            )
            body = resp.json()
            if _ci_get(body, "success") is not True:
                errors.append(f"{destination}: {_ci_get(body, 'message') or resp.text[:120]}")
        except (requests.RequestException, ValueError) as exc:
            errors.append(f"{destination}: {type(exc).__name__}")
    if errors:
        raise RuntimeError("; ".join(errors))


# --------------------------------------------------------------------- composition

def _host_label() -> str:
    return socket.gethostname()


def _now_label() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M %Z")


def build_messages(decision: Decision, results: list[Result], state: dict) -> tuple[str, str, str, str]:
    """Returns (subject, text_body, html_body, sms_text)."""
    by_name = {r.name: r for r in results}
    checks = state.get("checks", {})

    if decision.kind == "recovery":
        subject = "[SULIKO] RECOVERED: all checks passing"
        text = (
            f"All health checks are passing again.\n\nResolved: {', '.join(decision.resolved)}\n\n"
            f"Host: {_host_label()}\nTime: {_now_label()}\n"
        )
        sms = "SULIKO: RECOVERED. All health checks passing."
        return subject, text, f"<pre>{html.escape(text)}</pre>", sms

    crit = [n for n in decision.failing if checks.get(n, {}).get("severity") == CRITICAL]
    tag = "ALERT" if decision.kind == "alert" else "STILL FAILING"
    level = "CRITICAL" if decision.has_critical else "warning"
    subject = f"[SULIKO] {tag} ({level}): {len(decision.failing)} failing - {', '.join(decision.failing[:3])}"
    if len(decision.failing) > 3:
        subject += ", ..."

    lines = []
    rows = []
    for name in decision.failing:
        c = checks.get(name, {})
        detail = c.get("detail") or by_name.get(name, Result(name, False)).detail
        mark = "NEW " if name in decision.new else ""
        lines.append(f"- {mark}[{c.get('severity', '?').upper()}] {name}: {detail}\n    failing since {c.get('since', '?')} ({c.get('runs', '?')} run(s))")
        rows.append(
            f"<tr><td>{html.escape(mark + name)}</td><td>{html.escape(c.get('severity', '?'))}</td>"
            f"<td>{html.escape(detail)}</td><td>{html.escape(str(c.get('since', '?')))}</td></tr>"
        )
    passing = sorted(r.name for r in results if r.ok)
    text = (
        f"{len(decision.failing)} health check(s) failing.\n\n" + "\n".join(lines)
        + (f"\n\nRecovered since last alert: {', '.join(decision.resolved)}" if decision.resolved else "")
        + f"\n\nPassing this run: {len(passing)}\nHost: {_host_label()}\nTime: {_now_label()}\n"
    )
    html_body = (
        f"<h3>{html.escape(subject)}</h3>"
        "<table border='1' cellpadding='6' cellspacing='0' style='border-collapse:collapse;font-family:monospace'>"
        "<tr><th>check</th><th>severity</th><th>detail</th><th>since (UTC)</th></tr>" + "".join(rows) + "</table>"
        f"<p>Passing this run: {len(passing)}<br>Host: {html.escape(_host_label())}<br>Time: {html.escape(_now_label())}</p>"
    )
    names = ", ".join((crit or decision.failing)[:3])
    more = len(decision.failing) - min(3, len(crit or decision.failing))
    sms = f"SULIKO {tag}: {len(decision.failing)} failing: {names}" + (f" +{more} more" if more > 0 else "") + ". Check email."
    return subject, text, html_body, sms[:300]


def notify(cfg: Config, decision: Decision, results: list[Result], state: dict) -> bool:
    """Send the alert on every applicable channel. Returns False only if NOTHING got through."""
    subject, text, html_body, sms_text = build_messages(decision, results, state)

    want_sms = cfg.get("alerts", "sms_on", "critical") == "all" or decision.has_critical
    delivered = 0

    try:
        send_email(cfg, subject, text, html_body)
        log.info("email sent: %s", subject)
        delivered += 1
    except Exception as exc:  # noqa: BLE001 - any channel failure must not stop the other one
        log.error("email FAILED: %s: %s", type(exc).__name__, exc)

    if want_sms:
        try:
            send_sms(sms_text)
            log.info("sms sent: %s", sms_text)
            delivered += 1
        except Exception as exc:  # noqa: BLE001
            log.error("sms FAILED: %s: %s", type(exc).__name__, exc)

    return delivered > 0
