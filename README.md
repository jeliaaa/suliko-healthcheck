# Suliko health checker

Runs once an hour (Windows Scheduled Task), checks that Suliko works end to end, and **emails
info@th.com.ge + SMS** when something breaks. Quiet when healthy.

## What it checks

| Group | Check | Fails when | Severity |
|---|---|---|---|
| api | `api_live` | `GET /health` is not 200 | critical |
| api | `api_ready` | `GET /health/ready` is 503 (DB down) / `degraded` (OCR down) | critical / warning |
| api | `api_public_db_read` | anonymous `/api/Language/public` fails or is empty | critical |
| api | `payment_history` | authenticated `/api/Payment/history` not 200 | warning |
| auth | `auth_sign_in` | `POST /api/Auth/token` with the test account fails | critical |
| auth | `auth_authenticated_call` | the issued token is rejected by `/api/Subscription/me` | critical |
| auth | `auth_web_session` | suliko.ge web login → refresh → sign-out fails (needs `HC_WEB_SESSION_KEY`) | critical |
| sso | `office_sso_code[...]` | one per allowed redirect URI (`app.`, `office.`, `suliko.ge/app`, `www.suliko.ge/app`) | critical |
| sso | `office_sso_redeem` | code can't be redeemed (needs `HC_OFFICE_KEY`) | critical |
| job | `job_start` | a tiny translation job never leaves the queue / fails / doesn't finish | critical |
| site | `site[...]` | public pages don't answer | critical |
| tls | `tls[host]` | certificate ≤14 days (warning) / ≤5 days (critical) from expiry | warning/critical |
| gateway | `smtp_login` | Gmail SMTP login rejected (the API's emails + these alerts) | warning |
| gateway | `sms_gateway` | smsoffice.ge unreachable | warning |
| host | `disk[...]`, `host_memory`, `port[postgres]`, `port[ocr-service]` | low disk/RAM, port closed | critical/warning |

Checks that cannot run (feature not deployed, credentials not set) are **skipped**, shown as
`SKIP` in the log, and never alert. `python main.py --doctor` lists what's missing.

## Alert behaviour

* Alerts on the **first** failure of each check; **no repeats** for 6 h while it persists, then
  a reminder; one **RECOVERED** message when all is green.
* A failing check is retried once after 20 s before it counts (kills one-packet blips).
* **SMS only for critical** failures (`alerts.sms_on = "all"` for everything).
* If email *and* SMS both fail, the next run alerts again, and the run exits with code 2.
* Optional `HC_HEARTBEAT_URL` (healthchecks.io etc.) catches the case where *this checker*
  stops running — nothing else can alert you about that.

## Commands

```powershell
.\.venv\Scripts\python.exe main.py --dry-run          # run everything, print, send nothing
.\.venv\Scripts\python.exe main.py --only api,tls      # subset
.\.venv\Scripts\python.exe main.py --test-alert        # test email + SMS
.\.venv\Scripts\python.exe main.py --doctor            # missing settings
.\.venv\Scripts\python.exe -m unittest discover -s tests
```

Logs: `logs\healthcheck.log` (rotating). State: `state\state.json` (delete to reset alert memory).

## Files

`main.py` entry · `hc/checks.py` all checks (add one with `@check(...)`) · `hc/state.py` alert
de-dup · `hc/notify.py` email + SMS · `config.toml` settings · `.env` secrets ·
`scripts/install-task.ps1` scheduler · `RDP_SETUP.md` server setup steps.
