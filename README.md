# OSU parking permit monitor

Checks **Monthly Permit - Zone A1 (514)** hourly and sends Discord DMs on availability, followed by daily reminders while fresh checks still confirm availability. One Python process, SQLite, Playwright Chromium, and a temporary noVNC desktop, packaged for TrueNAS Docker Custom Apps.

**Live acceptance is not complete.** The university's authenticated navigation, reliable loading signal, MFA behavior, session lifetime, and TrueNAS sandbox compatibility must be verified with your account. The application fails closed until you configure readiness and install a session that survives a headless browser restart. Local fixtures do not prove university authentication works. See [the runbook](deploy/RUNBOOK.md) for the feasibility gate and 48-hour acceptance checklist.

## Quick start for development

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -e '.[test]'
playwright install --with-deps chromium
ruff check .
pytest -q
export PARKING_DATA_DIR="$PWD/data"
parking-bot status --json
```

Python 3.12+ on Linux is required. Tests intercept browser traffic and mock Discord. No credentials belong in the repository or CI. Interactive authentication additionally needs Xvfb, openbox, x11vnc, noVNC, websockify, and GNU timeout; the Docker image contains these tools.

## Commands

| Command | Purpose |
| --- | --- |
| `parking-bot run` | Scheduler, heartbeat, and independent notification retries |
| `parking-bot check --dry-run` | One live read-only check, no DMs or notification-history changes |
| `parking-bot auth start` | Foreground temporary remote browser; keep the terminal open |
| `parking-bot auth status` / `stop` | Inspect or cancel renewal from another shell |
| `parking-bot notify-test` | Explicitly send a setup DM |
| `parking-bot status --json` | Results, UTC timestamps, schedule, auth condition, delivery failures |
| `parking-bot health` | Local scheduler heartbeat, lock, and database access; no network requests |

Dry runs honor the persisted polling schedule and reserve the next eligible check to prevent restart/manual request bursts. They do not refresh session state. Authentication validation is an explicit setup action, separate from the polling schedule. Normal polling pauses after recognized authentication loss until successful renewal.

## Configuration

Environment variables use the `PARKING_` prefix. Configuration errors stop startup.

| Variable suffix | Default | Notes |
| --- | --- | --- |
| `PORTAL_URL` | `https://aims.parking.oregonstate.edu/permits/?cmd=new_auth` | Verify during setup |
| `PERMIT_ID` / `PERMIT_LABEL` | `514` / `Monthly Permit - Zone A1` | Exact identity required |
| `INTERVAL` | `3600` | Minimum 3600 seconds, plus 0–120 seconds jitter |
| `REMINDER` | `86400` | Minimum 24 hours from successful availability delivery |
| `DATA_DIR` | `/data` | Dedicated owner-only persistent directory |
| `RECIPIENT` | Empty | Numeric Discord user ID |
| `TOKEN_FILE` | `/run/secrets/discord_token` | Read-only secret mount; token reread on sends |
| `TIMEZONE` | `America/Los_Angeles` | Message display timezone; database stores UTC epoch seconds |
| `READY_SELECTOR` | Empty | **Required for checks:** verified visible completion signal |
| `NAVIGATION` | `[]` | JSON array of verified Playwright locators to click before inspecting |
| `AUTH_HOSTS` | `["login.microsoftonline.com","login.oregonstate.edu"]` | Exact recognized sign-in hosts; add only observed identity-provider hosts |
| `AUTH_SELECTOR` | Empty | Optional verified visible login/session-expiry locator |
| `DATA_REQUEST_PATTERNS` | `[]` | URL substrings identifying cross-origin permit-data requests; same-origin fetch/XHR and all documents always observed |
| `SESSION_STORAGE` | `false` | Optional origin-scoped sessionStorage export/restore if experiment proves needed |
| `AUTH_BIND` / `AUTH_PORT` | `127.0.0.1` / `6080` | Compose sets container bind to `0.0.0.0`, host bind stays loopback |
| `CHECK_TIMEOUT` | `120` | Total check deadline in seconds, maximum 120 |
| `SANDBOX` | `true` | Chromium sandbox; deployment should keep enabled |
| `EXECUTABLE` | Playwright managed Chromium | Development override only |

Navigation selectors must only advance to permit selection. Never configure purchase, cart, or checkout actions. Do not use `body` or an early-loading container as readiness merely to pass validation. See the runbook for calibration.

## Behavior and delivery guarantees

The checker requires one visible matching button, the exact normalized label and value, and verified page readiness. Any `disabled` attribute means unavailable, even `disabled="false"`. Inherited disabling, aria-disabled, hidden/duplicate/renamed/missing controls mean unknown. Recognized sign-in screens mean authentication required. Generic 403, server errors, timeouts, or missing controls never imply unavailable or expired authentication.

Unknown results preserve the availability episode. Only confirmed unavailability closes it. The initial available observation and each reopening create an alert. Repeated availability sends a reminder no sooner than 24 hours after the last successful availability delivery. A reopening can send sooner because it is a new episode. Reminders always require a fresh check.

Pending messages persist before REST calls, use a stable nonce, and are marked sent only after success. Unsent availability messages expire after one hour unless reconfirmed and are cancelled on confirmed closure. Delivery retries run independently every five seconds when eligible, with exponential delay; rate limits and longer server delays take precedence. Invalid tokens or disabled DMs retry daily and appear in status. Operational alerts are immediate for layout/authentication failures and after three consecutive transient check failures; repeated alerts are limited to daily. Resolved pending operational alerts are cancelled.

Discord nonce deduplication covers only a limited window. A crash after Discord accepts a message but before SQLite commits can cause a duplicate. This is not exactly-once delivery. Cached DM channels are keyed by recipient. Token rotation within the same bot is supported; changing bot identity requires clearing the `dm_channel` metadata while the app is stopped.

Polling failures back off to 2, 4, and 8 hours, with a longer Retry-After honored. The next check is reserved before browsing and persisted, so downtime does not cause catch-up bursts. Hourly polling may miss short openings. An enabled button offers a permit to this account at that moment; it neither reserves inventory nor guarantees checkout. Purchasing is outside scope.

## Layout and references

`browser.py` inspects rendered pages and handles session snapshots; `auth.py` manages temporary desktop renewal; `state.py` owns SQLite transitions and the outbox; `notify.py` handles Discord REST; `service.py` schedules work; `cli.py` exposes the commands. Browser and scheduler locks are Linux advisory file locks. Session replacement uses mode-0600 temporary files, fsync, and atomic rename.

The implementation follows [Playwright storage-state APIs](https://playwright.dev/python/docs/api/class-browsercontext#browser-context-storage-state), [Playwright container guidance](https://playwright.dev/python/docs/docker), [Discord message/nonce semantics](https://docs.discord.com/developers/resources/message#create-message), and [Discord rate limits](https://docs.discord.com/developers/topics/rate-limits). The included [seccomp profile](deploy/chromium-seccomp.json) starts from [Moby’s default profile](https://github.com/moby/profiles/blob/main/seccomp/default.json) and adds the `clone`, `setns`, and `unshare` allowances documented by Playwright. See [profile provenance](deploy/SECCOMP.md) and the included upstream licenses.
