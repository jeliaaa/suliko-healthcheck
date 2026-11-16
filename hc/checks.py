"""The health checks.

Each check is a function `fn(ctx) -> Result | list[Result]` registered with @check(group=...).
Raising is fine: the runner turns an exception into a failed result.
Groups: api, auth, sso, job, site, tls, gateway, host.
"""

from __future__ import annotations

import base64
import ctypes
import hashlib
import secrets
import shutil
import smtplib
import socket
import ssl
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable
from urllib.parse import urlparse

import requests

from .config import Config, env
from .model import CRITICAL, WARNING, Result


# ---------------------------------------------------------------- registry / context

@dataclass
class Check:
    name: str
    group: str
    fn: Callable[["Ctx"], "Result | list[Result]"]
    retry: bool = True


REGISTRY: list[Check] = []


def check(name: str, group: str, retry: bool = True):
    def deco(fn):
        REGISTRY.append(Check(name, group, fn, retry))
        return fn
    return deco


def jget(data, *keys):
    """Case-insensitive JSON lookup (ASP.NET serialises camelCase, but don't depend on it)."""
    if not isinstance(data, dict):
        return None
    lowered = {k.lower(): v for k, v in data.items()}
    for key in keys:
        if key.lower() in lowered:
            return lowered[key.lower()]
    return None


class Ctx:
    def __init__(self, cfg: Config):
        self.cfg = cfg
        self.base = cfg.get("api", "base_url").rstrip("/")
        self.timeout = cfg.get("api", "timeout_seconds", 20)
        self.http = requests.Session()
        self.http.headers["User-Agent"] = "suliko-healthcheck/1.0"
        self._token: str | None = None
        self._token_error: str | None = None
        self._token_tried = False

    def url(self, path: str) -> str:
        return self.base + path

    def get(self, path: str, **kw) -> requests.Response:
        return self.http.get(self.url(path), timeout=self.timeout, **kw)

    def post(self, path: str, **kw) -> requests.Response:
        return self.http.post(self.url(path), timeout=self.timeout, **kw)

    def bearer(self) -> dict:
        token, err = self.token()
        if not token:
            raise RuntimeError(err or "no token")
        return {"Authorization": f"Bearer {token}"}

    def token(self) -> tuple[str | None, str | None]:
        """Sign in once per run; every authenticated check reuses the token."""
        if self._token_tried:
            return self._token, self._token_error
        self._token_tried = True
        user, password = env("HC_TEST_USERNAME"), env("HC_TEST_PASSWORD")
        if not (user and password):
            self._token_error = "HC_TEST_USERNAME / HC_TEST_PASSWORD not set"
            return None, self._token_error
        try:
            r = self.post("/api/Auth/token", data={"username": user, "password": password})
            if r.status_code != 200:
                self._token_error = f"POST /api/Auth/token -> HTTP {r.status_code}"
            else:
                self._token = jget(r.json(), "access_token")
                if not self._token:
                    self._token_error = "token response had no access_token"
        except (requests.RequestException, ValueError) as exc:
            self._token_error = f"POST /api/Auth/token -> {type(exc).__name__}: {exc}"
        return self._token, self._token_error


def _creds_missing(name: str) -> Result | None:
    if not (env("HC_TEST_USERNAME") and env("HC_TEST_PASSWORD")):
        return Result(name, True, "skipped: test account not configured (.env)", skipped=True)
    return None


# ------------------------------------------------------------------------------ api

@check("api_live", "api")
def api_live(ctx: Ctx) -> Result:
    r = ctx.get("/health")
    if r.status_code == 404:
        # /health ships with this release. Until it is deployed, prove the app answers at all.
        s = ctx.get("/swagger/v1/swagger.json")
        if s.status_code == 200:
            return Result("api_live", True, "/health not deployed yet; API answers via swagger", skipped=True)
        return Result("api_live", False, f"/health 404 and swagger HTTP {s.status_code}")
    if r.status_code != 200:
        return Result("api_live", False, f"GET /health -> HTTP {r.status_code}")
    return Result("api_live", True, f"{int(r.elapsed.total_seconds() * 1000)} ms")


@check("api_ready", "api")
def api_ready(ctx: Ctx) -> Result:
    r = ctx.get("/health/ready")
    if r.status_code == 404:
        return Result("api_ready", True, "skipped: /health/ready not deployed yet", skipped=True)
    try:
        body = r.json()
    except ValueError:
        body = {}
    checks = jget(body, "checks") or {}
    summary = ", ".join(f"{k}={jget(v, 'status')}" for k, v in checks.items()) if isinstance(checks, dict) else ""
    status = jget(body, "status")
    if r.status_code == 503 or status == "unhealthy":
        return Result("api_ready", False, f"UNHEALTHY ({summary or 'HTTP ' + str(r.status_code)})")
    if status == "degraded":
        return Result("api_ready", False, f"degraded ({summary})", severity=WARNING)
    if r.status_code != 200:
        return Result("api_ready", False, f"GET /health/ready -> HTTP {r.status_code}")
    return Result("api_ready", True, summary)


@check("api_public_db_read", "api")
def api_public_db_read(ctx: Ctx) -> Result:
    """Anonymous endpoint that reads the database: catches DB-down even if /health is stubbed."""
    r = ctx.get("/api/Language/public")
    if r.status_code != 200:
        return Result("api_public_db_read", False, f"GET /api/Language/public -> HTTP {r.status_code}")
    count = jget(r.json(), "count")
    if not count:
        return Result("api_public_db_read", False, "language list is empty (DB reachable but not seeded?)")
    return Result("api_public_db_read", True, f"{count} languages")


# ----------------------------------------------------------------------------- auth

@check("auth_sign_in", "auth")
def auth_sign_in(ctx: Ctx) -> Result:
    if (skip := _creds_missing("auth_sign_in")):
        return skip
    token, err = ctx.token()
    return Result("auth_sign_in", True, "token issued") if token else Result("auth_sign_in", False, err or "no token")


@check("auth_authenticated_call", "auth")
def auth_authenticated_call(ctx: Ctx) -> Result:
    if (skip := _creds_missing("auth_authenticated_call")):
        return skip
    token, err = ctx.token()
    if not token:
        return Result("auth_authenticated_call", False, f"no token: {err}")
    path = ctx.cfg.get("auth", "authenticated_path", "/api/Subscription/me")
    r = ctx.get(path, headers=ctx.bearer())
    # 200 = has a subscription, 204 = signed in but none: both prove the bearer token is accepted.
    ok = r.status_code in (200, 204)
    return Result("auth_authenticated_call", ok, f"GET {path} -> HTTP {r.status_code}")


@check("auth_web_session", "auth")
def auth_web_session(ctx: Ctx) -> Result:
    """suliko.ge's real sign-in path: login -> refresh -> sign-out."""
    key = env("HC_WEB_SESSION_KEY")
    if not key:
        return Result("auth_web_session", True, "skipped: HC_WEB_SESSION_KEY not set", skipped=True)
    if (skip := _creds_missing("auth_web_session")):
        return skip
    headers = {"X-Web-Session-Key": key}
    r = ctx.post("/api/web-session/login", headers=headers,
                 json={"phoneNumber": env("HC_TEST_USERNAME"), "password": env("HC_TEST_PASSWORD")})
    if r.status_code != 200:
        return Result("auth_web_session", False, f"login -> HTTP {r.status_code}")
    refresh = jget(r.json(), "refreshToken")
    if not (jget(r.json(), "token") and refresh):
        return Result("auth_web_session", False, "login 200 but token/refreshToken missing")

    r = ctx.post("/api/web-session/refresh", headers=headers, json={"refreshToken": refresh})
    if r.status_code != 200:
        return Result("auth_web_session", False, f"refresh -> HTTP {r.status_code}")
    refresh = jget(r.json(), "refreshToken") or refresh

    r = ctx.post("/api/web-session/sign-out", headers=headers, json={"refreshToken": refresh})
    if r.status_code not in (200, 204):
        return Result("auth_web_session", False, f"sign-out -> HTTP {r.status_code}")
    return Result("auth_web_session", True, "login/refresh/sign-out ok")


# ------------------------------------------------------------------------------ sso

def _sso_label(uri: str) -> str:
    p = urlparse(uri)
    prefix = "/app" if p.path.startswith("/app/") else ""
    return f"{p.netloc}{prefix}"


@check("office_sso", "sso")
def office_sso(ctx: Ctx) -> list[Result]:
    """Issues a code for every allowed redirect URI (a URI dropped from the allow-list = silent
    sign-in outage for Office, as happened with the /app and www moves), then redeems one."""
    if (skip := _creds_missing("office_sso_code")):
        return [skip]
    token, err = ctx.token()
    uris = ctx.cfg.get("office_sso", "redirect_uris", [])
    if not token:
        return [Result(f"office_sso_code[{_sso_label(u)}]", False, f"no token: {err}") for u in uris]

    results: list[Result] = []
    redeemable = None
    for uri in uris:
        name = f"office_sso_code[{_sso_label(uri)}]"
        verifier = secrets.token_urlsafe(48)
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
        r = ctx.post("/api/office/sso/code", headers=ctx.bearer(),
                     json={"redirectUri": uri, "codeChallenge": challenge, "codeChallengeMethod": "S256"})
        if r.status_code == 429:
            results.append(Result(name, False, "rate limited (HTTP 429)", severity=WARNING))
        elif r.status_code != 200:
            results.append(Result(name, False, f"HTTP {r.status_code}: {r.text[:120]}"))
        else:
            code = jget(r.json(), "code")
            results.append(Result(name, bool(code), "code issued" if code else "200 but no code"))
            if code and redeemable is None:
                redeemable = (uri, code, verifier)

    office_key = env("HC_OFFICE_KEY")
    if office_key and redeemable:
        uri, code, verifier = redeemable
        r = ctx.post("/api/office/sso/redeem", headers={"X-Office-Key": office_key},
                     json={"code": code, "codeVerifier": verifier, "redirectUri": uri})
        results.append(Result("office_sso_redeem", r.status_code == 200, f"HTTP {r.status_code}"))
    return results


# ------------------------------------------------------------------------------ job

_TERMINAL_OK = {"completed"}
_TERMINAL_BAD = {"failed", "error", "cancelled", "canceled"}


@check("job_start", "job", retry=False)
def job_start(ctx: Ctx) -> Result:
    """Starts a real translation job and watches it leave the queue (and, optionally, finish)."""
    cfg = ctx.cfg
    if not cfg.get("job", "enabled", True):
        return Result("job_start", True, "skipped: disabled in config", skipped=True)
    if (skip := _creds_missing("job_start")):
        return skip
    token, err = ctx.token()
    if not token:
        return Result("job_start", False, f"no token: {err}")

    want = str(cfg.get("job", "target_language", "English")).lower()
    langs = jget(ctx.get("/api/Language/public").json(), "languages") or []
    def _lang_names(x):
        name = str(jget(x, "name")).strip().lower()
        return {name, name.removesuffix(" language").strip(), str(jget(x, "nameGeo")).strip().lower()}

    lang_id = next((jget(x, "id") for x in langs if want in _lang_names(x)), None)
    if lang_id is None:
        return Result("job_start", False, f"language '{want}' not found in /api/Language/public")

    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M")
    payload = f"Health check {stamp}. Hello, this is an automated test.".encode()
    r = ctx.post(
        "/api/Document/translate", headers=ctx.bearer(),
        files={"File": ("healthcheck.txt", payload, "text/plain")},
        data={
            "TargetLanguageId": lang_id, "OutputLanguageId": lang_id,
            "OutputFormat": cfg.get("job", "output_format", "Txt"),
            "Model": cfg.get("job", "model", "Gemini3Flash"),
        },
    )
    if r.status_code not in (200, 202):
        return Result("job_start", False, f"POST /api/Document/translate -> HTTP {r.status_code}: {r.text[:160]}")
    body = r.json()
    job_id, chat_id = jget(body, "jobId"), jget(body, "chatId")
    if not job_id:
        return Result("job_start", False, "job accepted but no jobId returned")

    try:
        return _watch_job(ctx, job_id)
    finally:
        _cleanup_chat(ctx, chat_id)


def _watch_job(ctx: Ctx, job_id: str) -> Result:
    cfg = ctx.cfg
    poll = max(1, int(cfg.get("job", "poll_seconds", 5)))
    start_by = time.monotonic() + cfg.get("job", "start_timeout_seconds", 90)
    done_by = time.monotonic() + cfg.get("job", "complete_timeout_seconds", 300)
    wait_done = cfg.get("job", "wait_for_completion", True)
    started = False
    last = "no status yet"
    t0 = time.monotonic()

    while True:
        r = ctx.get(f"/api/Document/translate/status/{job_id}", headers=ctx.bearer())
        if r.status_code == 200:
            body = r.json()
            status = str(jget(body, "status") or "").lower()
            stage = str(jget(body, "stage") or "").lower()
            progress = jget(body, "progress") or 0
            last = f"status={status} stage={stage or '-'} progress={progress}"
            if status in _TERMINAL_BAD:
                return Result("job_start", False, f"job failed: {jget(body, 'message')}")
            if status in _TERMINAL_OK:
                return Result("job_start", True, f"completed in {time.monotonic() - t0:.0f}s")
            if not started and (progress > 10 or stage not in ("", "queued")):
                started = True
                if not wait_done:
                    return Result("job_start", True, f"started in {time.monotonic() - t0:.0f}s ({last})")
        elif r.status_code != 404:   # 404 right after creation can be a moment of lag
            last = f"status poll -> HTTP {r.status_code}"

        now = time.monotonic()
        if not started and now > start_by:
            return Result("job_start", False, f"job never left the queue within {cfg.get('job', 'start_timeout_seconds')}s ({last})")
        if now > done_by:
            return Result("job_start", False, f"job started but did not finish within {cfg.get('job', 'complete_timeout_seconds')}s ({last})")
        time.sleep(poll)


def _cleanup_chat(ctx: Ctx, chat_id) -> None:
    """Each probe creates a chat on the test account; remove it so 24/day don't pile up."""
    if not chat_id:
        return
    try:
        ctx.http.delete(ctx.url(f"/api/document-translation/chat/{chat_id}"),
                        headers=ctx.bearer(), timeout=ctx.timeout)
    except (requests.RequestException, RuntimeError):
        pass  # best effort


@check("payment_history", "api")
def payment_history(ctx: Ctx) -> Result:
    if (skip := _creds_missing("payment_history")):
        return skip
    token, err = ctx.token()
    if not token:
        return Result("payment_history", False, f"no token: {err}", severity=WARNING)
    r = ctx.get("/api/Payment/history", headers=ctx.bearer())
    return Result("payment_history", r.status_code == 200, f"HTTP {r.status_code}", severity=WARNING)


# ------------------------------------------------------------------------------ site

@check("site_pages", "site")
def site_pages(ctx: Ctx) -> list[Result]:
    out = []
    for url in ctx.cfg.get("site", "pages", []):
        name = f"site[{urlparse(url).netloc}{urlparse(url).path.rstrip('/') or '/'}]"
        r = ctx.http.get(url, timeout=ctx.timeout, allow_redirects=True)
        out.append(Result(name, r.status_code < 400, f"HTTP {r.status_code}"))
    return out


# ------------------------------------------------------------------------------- tls

@check("tls_certs", "tls")
def tls_certs(ctx: Ctx) -> list[Result]:
    warn = ctx.cfg.get("tls", "warn_days", 14)
    crit = ctx.cfg.get("tls", "critical_days", 5)
    out = []
    for host in ctx.cfg.get("tls", "hosts", []):
        name = f"tls[{host}]"
        try:
            with socket.create_connection((host, 443), timeout=10) as sock:
                with ssl.create_default_context().wrap_socket(sock, server_hostname=host) as tls:
                    cert = tls.getpeercert()
        except (OSError, ssl.SSLError) as exc:
            out.append(Result(name, False, f"TLS handshake failed: {type(exc).__name__}: {exc}"))
            continue
        expires = datetime.fromtimestamp(ssl.cert_time_to_seconds(cert["notAfter"]), timezone.utc)
        days = (expires - datetime.now(timezone.utc)).days
        if days <= crit:
            out.append(Result(name, False, f"expires in {days} days", severity=CRITICAL))
        elif days <= warn:
            out.append(Result(name, False, f"expires in {days} days", severity=WARNING))
        else:
            out.append(Result(name, True, f"{days} days left"))
    return out


# --------------------------------------------------------------------------- gateway

@check("smtp_login", "gateway")
def smtp_login(ctx: Ctx) -> Result:
    """The SMTP account sends verification codes, password resets AND these alerts."""
    if not ctx.cfg.get("email_gateway", "check_login", True):
        return Result("smtp_login", True, "skipped: disabled", skipped=True)
    host, port = env("HC_SMTP_HOST", "smtp-relay.brevo.com"), int(env("HC_SMTP_PORT", "587"))
    user, password = env("HC_SMTP_USERNAME"), env("HC_SMTP_PASSWORD")
    if not (user and password):
        return Result("smtp_login", True, "skipped: SMTP credentials not set", skipped=True)
    try:
        with smtplib.SMTP(host, port, timeout=20) as smtp:
            smtp.starttls(context=ssl.create_default_context())
            smtp.login(user, password)
    except (smtplib.SMTPException, OSError) as exc:
        return Result("smtp_login", False, f"{type(exc).__name__}: {exc}", severity=WARNING)
    return Result("smtp_login", True, f"{host}:{port} login ok")


@check("sms_gateway", "gateway")
def sms_gateway(ctx: Ctx) -> Result:
    url = "https://api.ubill.dev/" if env("HC_SMS_API_KEY_UBILL") else "https://smsoffice.ge/"
    try:
        r = requests.get(url, timeout=15)
    except requests.RequestException as exc:
        return Result("sms_gateway", False, f"{url} unreachable: {type(exc).__name__}", severity=WARNING)
    return Result("sms_gateway", r.status_code < 500, f"HTTP {r.status_code}", severity=WARNING)


# ---------------------------------------------------------------------------- host

@check("host_disk", "host", retry=False)
def host_disk(ctx: Ctx) -> list[Result]:
    min_pct = ctx.cfg.get("host", "min_free_disk_percent", 10)
    min_gb = ctx.cfg.get("host", "min_free_disk_gb", 5)
    out = []
    for path in ctx.cfg.get("host", "disk_paths", []):
        name = f"disk[{path}]"
        try:
            u = shutil.disk_usage(path)
        except OSError:
            out.append(Result(name, True, "skipped: path not present", skipped=True))
            continue
        pct, gb = u.free / u.total * 100, u.free / 2**30
        low = pct < min_pct or gb < min_gb
        out.append(Result(name, not low, f"{gb:.1f} GB free ({pct:.0f}%)"))
    return out


@check("host_memory", "host")
def host_memory(ctx: Ctx) -> Result:
    if sys.platform != "win32":
        return Result("host_memory", True, "skipped: not Windows", skipped=True)

    class MEMORYSTATUSEX(ctypes.Structure):
        _fields_ = [("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong), ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong), ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("sullAvailExtendedVirtual", ctypes.c_ulonglong)]

    st = MEMORYSTATUSEX()
    st.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
    ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(st))
    limit = ctx.cfg.get("host", "max_memory_used_percent", 92)
    return Result("host_memory", st.dwMemoryLoad <= limit, f"{st.dwMemoryLoad}% used", severity=WARNING)


@check("host_ports", "host")
def host_ports(ctx: Ctx) -> list[Result]:
    out = []
    for entry in ctx.cfg.get("host", "tcp_ports", []):
        name = f"port[{entry['name']}]"
        try:
            with socket.create_connection((entry["host"], entry["port"]), timeout=5):
                out.append(Result(name, True, f"{entry['host']}:{entry['port']} open"))
        except OSError as exc:
            out.append(Result(name, False, f"{entry['host']}:{entry['port']} {type(exc).__name__}",
                              severity=entry.get("severity", CRITICAL)))
    return out
