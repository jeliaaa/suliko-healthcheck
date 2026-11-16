# RDP setup instructions (for Claude Code running on the RDP server)

You are setting up the **Suliko hourly health checker** on this Windows server. It was written
and tested on the developer's PC; your job is to install it here, point it at production, and
verify it. Follow the steps in order, run the verification at each step, and **stop and ask the
user** wherever it says ASK. Do not invent credentials. Never print secrets back in chat.

Target location: `D:\suliko-healthcheck\` (the user's choice; if D: does not exist on this
server, ASK before using another path).

## What this is

A Python script run by a Windows **Scheduled Task** every hour (no long-running service). It
calls the production API/site, checks sign-in, Office SSO, a real translation job, TLS expiry,
SMTP/SMS gateways and host health, then emails **info@th.com.ge** and SMSes the team on failure
(de-duplicated; one RECOVERED message when fixed). Full description: `README.md`.

## Step 0 — Prerequisites

```powershell
python --version        # must be 3.11 or newer (uses tomllib)
```
If missing/older: install Python 3.12+ from python.org (tick "Add to PATH"), reopen PowerShell.
The folder must already contain `main.py`, `hc\`, `config.toml`, `.env.example`, `scripts\`
(copied from the developer's `D:\suliko-healthcheck`). If it doesn't, ASK the user to copy it.

## Step 1 — Create the dedicated test account (ASK the user)

The checker signs in and starts a tiny translation job as a **dedicated test user**. Ask the
user to create one in the Suliko app (normal customer, **not admin**, not a real person) and:
- give it enough page balance for ~24 one-page jobs/day (the job check spends ~1 page per run;
  to avoid spending balance set `[job] enabled = false` in `config.toml`, or
  `wait_for_completion = false`),
- tell you its username (phone number) and password.

## Step 2 — Fill in `.env`

```powershell
cd D:\suliko-healthcheck
Copy-Item .env.example .env
notepad .env
```
Values (ASK the user for anything you can't read from the server's existing API config):

| Key | Where it comes from |
|---|---|
| `HC_TEST_USERNAME` / `HC_TEST_PASSWORD` | step 1 |
| `HC_SMTP_USERNAME` / `HC_SMTP_PASSWORD` | the same values as `Security:EMAIL_USERNAME` / `EMAIL_PASSWORD` in the deployed API's `appsettings.json` / env (Gmail app password) |
| `HC_SMTP_FROM` | `info@th.com.ge` |
| `HC_SMS_API_KEY` | same as `Security:SMS_API_KEY` in the deployed API config |
| `HC_SMS_NUMBERS` | ASK: comma-separated phones to text, e.g. `5XXXXXXXX,5YYYYYYYY` |
| `HC_WEB_SESSION_KEY` | optional: the API's web-session key (enables the real suliko.ge login check) |
| `HC_OFFICE_KEY` | optional: the API's Office key (enables the SSO redeem check) |
| `HC_HEARTBEAT_URL` | optional: ASK if they want a healthchecks.io dead-man's-switch URL |

Rules: no quotes needed, no trailing spaces, never commit `.env`.

## Step 3 — Adjust `config.toml` for THIS server

- `[api] base_url` → `https://content.api24.ge` (production; already the default)
- `[host] disk_paths` → drives that exist here; `tcp_ports` → where Postgres/OCR really listen
  (default Postgres 5432, OCR service 8000 — verify with `Get-NetTCPConnection -State Listen`)
- `[office_sso] redirect_uris` → must match `Office:SsoRedirectUris` if the server config
  overrides it; otherwise leave the defaults.

## Step 4 — Dry run (sends nothing)

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe main.py --doctor
.\.venv\Scripts\python.exe main.py --dry-run
```
Read the table. Expected on a healthy production server: every line `OK`, with `SKIP` only for
optional things the user didn't configure. Diagnose any `FAIL`/`WARN` — do **not** loosen a
check just to make it green. Common causes:
- `auth_sign_in` HTTP 401 → wrong test credentials. 400 on `/api/Auth/token` → fields missing.
- `office_sso_code[...]` HTTP 400 `invalid_redirect_uri` → the URI is not in the server's allow-list
  (this is a real finding; tell the user).
- `job_start` fails → test account has no balance, or the AI provider is down (real finding).
- `api_live`/`api_ready` SKIP "not deployed yet" → the API release containing
  `Controllers/HealthController.cs` (`/health`, `/health/ready`) has not been deployed.
  Tell the user; it deploys by pushing the repo's `master` branch (self-hosted runner).
- `port[ocr-service]` WARN → OCR micro-service (`micro/`, port 8000) isn't running here.

## Step 5 — Test the alert path (ASK first: this really emails and texts people)

Tell the user an email to info@th.com.ge and an SMS will go out, then:
```powershell
.\.venv\Scripts\python.exe main.py --test-alert
```
Both lines must say `sent`. Ask the user to confirm they received both. If SMS fails, check
`HC_SMS_API_KEY`, `HC_SMS_NUMBERS`, and the smsoffice.ge balance. If email fails: Gmail app
password / "less secure" settings / outbound port 587 blocked by the firewall.

## Step 6 — Install the hourly task (elevated PowerShell)

```powershell
cd D:\suliko-healthcheck
.\scripts\install-task.ps1
Start-ScheduledTask -TaskName Suliko-HealthCheck
Start-Sleep 90
Get-ScheduledTaskInfo -TaskName Suliko-HealthCheck | Select LastRunTime, LastTaskResult
Get-Content .\logs\healthcheck.log -Tail 40
```
`LastTaskResult` 0 = all green, 1 = failures were found and reported, 2 = alert delivery failed
(fix channels immediately). The task runs as SYSTEM, hourly, even with nobody logged in.
Confirm `.env` permissions: `icacls .env` should list only SYSTEM and Administrators.

## Step 7 — Prove alerting end to end (ASK first)

With the user's OK, simulate an outage without touching production:
```powershell
$env:HC_API_BASE_URL = 'https://content.api24.ge.invalid'
.\.venv\Scripts\python.exe main.py --only api       # real alert email + SMS expected
Remove-Item Env:HC_API_BASE_URL
.\.venv\Scripts\python.exe main.py --only api       # RECOVERED message expected
```
(Run the second command only after the first sent its alert. State lives in `state\state.json`.)

## Step 8 — Report back

Tell the user: the dry-run table, whether both alert channels were confirmed received, task
status, and anything SKIPped/failing with the reason. Do not paste `.env` contents.

## Operating notes

- Change interval: re-run `install-task.ps1 -IntervalMinutes 30`. Remove: `scripts\uninstall-task.ps1`.
- Silence a known issue temporarily: set `[job] enabled = false` etc. in `config.toml`, or
  delete its line from state with `state\state.json`. Prefer fixing the cause.
- Updating: replace files, keep `.env`, `state\`, `logs\`, then re-run `pip install -r requirements.txt`.
- The alternative to a Scheduled Task, if the user insists on a daemon: NSSM wrapping
  `.venv\Scripts\python.exe main.py` would need a loop added to `main.py` — not recommended.
