# Implementation verification

Local verification completed on 2026-09-19. These results use sanitized fixtures, not a university account.

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
| Real Discord DM | Not sent; requires explicit setup invocation with configured bot |
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
