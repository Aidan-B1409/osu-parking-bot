# Implementation verification

## Online presence — 2026-09-22

Added `discord.py==2.7.1` for a background Gateway connection during `parking-bot run`. Local verification used the same temporary Python 3.14.7 environment described below.

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| Complete `pytest -q` suite | 124 passed, including rendered Chromium fixtures |
| Presence configuration and cleanup | Online status with zero intents; reconnect enabled; cancellation closes the client |
| Failed starts and tokens | Safe diagnostics, bounded retry delays, token rereading, missing-token handling, and cancellation during retry covered |
| Scheduler integration | Monitoring proceeds while presence connects; shutdown cancels presence; authentication pause still runs presence |
| Real Discord presence | Not tested; rebuild/restart the deployed image and check online, reconnect, and shutdown behavior using the runbook |
| Container build and offline smoke | Not rerun; existing CI includes both checks |

Gateway startup was mocked, Discord message delivery used mock transports, and browser fixture traffic was intercepted. No real notifications, Gateway connections, or university requests were made. The full suite ran outside the agent sandbox with Chromium's own sandbox enabled. Heartbeat and session-resume protocol handling is delegated to discord.py, rather than independently validated by these mocked tests.

## Channel notification migration — 2026-09-22

Verification for the switch to `PARKING_CHANNEL_ID` and `#parking-alerts` used a temporary virtual environment with Python 3.14.7 and pinned Playwright 1.58.0. No real Discord notifications or university requests were made.

| Check | Result |
| --- | --- |
| `ruff check .` | Passed |
| Complete `pytest -q` suite | 115 passed, including rendered Chromium fixtures |
| Configuration and CLI | Missing/malformed channel IDs rejected before HTTP client allocation; legacy-only configuration rejected; new variable wins when both exist; offline commands and authentication setup work without a destination |
| Direct delivery and mention policy | One channel message POST per attempt; legacy DM cache ignored; initial availability, reopening, and daily availability reminders enable only the intentional broadcast mention; operational notices/reminders and setup tests disable mentions |
| Retry and failure handling | Destination-specific 24-character nonces stable across retries/restarts; timeout, server error, rate limits, retry deadlines, exponential backoff, token rereading, locking, invalid responses, and sanitized channel diagnostics covered |
| Existing schema-version-1 state | History, timestamps, episodes, pending events, retry/rate-limit deadlines preserved on reopen; sent events not replayed; stale/cancelled events unsent; reminder timing unchanged |
| Explicit setup test | Mocked success/failure output and client cleanup passed; successful setup test leaves scheduled history untouched |
| Container build and offline container smoke | Not rerun for this change; existing CI includes both checks |
| Real channel posting and active availability mention | Not performed; follow the runbook's migration and acceptance steps |

The agent sandbox initially blocked Chromium launch with `Operation not permitted`; the complete suite passed after running outside that restriction, with Chromium's own sandbox still enabled. Playwright used its Ubuntu 24.04 fallback browser build on this Linux host. All fixture browser requests were intercepted and Discord delivery used mock transports.

For deployment, prepare `#parking-alerts` and its permissions, back up the stopped deployment, replace `PARKING_RECIPIENT` with `PARKING_CHANNEL_ID`, and keep the existing database. Run `notify-test` once to confirm posting without a ping. Separately verify the next naturally occurring availability alert's active `@everyone` mention; the setup test does not establish mention permission. See [the migration runbook](RUNBOOK.md#5-releases-upgrades-backups-rollback-and-token-rotation).

## Historical implementation validation — 2026-09-19

The following results predate the channel migration and are retained as historical evidence. They use sanitized fixtures, not a university account, and do not establish validation of the current container image.

| Check | Result |
| --- | --- |
| Ruff static checks | Passed |
| Python unit and rendered Chromium fixture tests | 54 passed |
| Docker image build | Passed; local tag `osu-parking-bot:local` |
| Compose file against official Compose JSON schema | Passed; Docker Compose CLI was unavailable locally |
| Non-root container with Chromium sandbox enabled | Passed using the supplied profile on Docker 29.7.2 |
| Temporary Xvfb/openbox/x11vnc/noVNC startup | Passed |
| noVNC HTTP page and VNC password-required handshake | Passed |
| Cookie snapshot presented after fresh headless browser restart | Passed against an offline fixture that rejects missing reused cookies |
| Session file permissions and login-port cleanup | Mode 0600 and both ports closed after success |
| Running-container heartbeat and SIGTERM | Healthy before/after restart; clean exit code 0 |
| Persistent named-volume state across restart | Pending event and notification-failure timestamp preserved; no extra event |
| GitHub Actions execution/GHCR publication | Configured, not executed or published in this workspace |
| University login, MFA, authenticated navigation/readiness | Not performed; requires administrator account |
| Real Discord DM (historical workflow) | Not sent during the original validation |
| TrueNAS deployment and 48-hour soak | Not performed; use the runbook acceptance checklist |

All browser-fixture traffic was intercepted. Container desktop verification used `--network none` and a loopback fixture server. The scheduler restart test had no session or token and made no external requests. Temporary test containers and volumes were removed; the built local image remains available.

The build initially encountered Debian-managed Python dependency conflicts, resolved by installing the application in `/opt/venv`. The older Playwright example seccomp profile caused a local runc startup crash; the final profile uses Moby's current default with Playwright's three documented namespace allowances. See `SECCOMP.md` for provenance.

To repeat the desktop smoke test after building the image:

```sh
docker build -t osu-parking-bot:local .
docker run --rm -t --init --network none --shm-size=1g \
  --security-opt "seccomp=$PWD/deploy/chromium-seccomp.json" \
  --mount "type=bind,source=$PWD/tests/container_smoke.py,target=/tmp/container_smoke.py,readonly" \
  --entrypoint python osu-parking-bot:local /tmp/container_smoke.py
```

The smoke test automatically navigates only its local fixture and uses synthetic session data. It does not prove university session reuse, mobile MFA, or real permit availability. Authentication cancellation is unit-tested; actual mobile renewal and the full 15-minute timeout should also be exercised during target deployment acceptance.
