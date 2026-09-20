# Deployment and operation

## 1. Prepare Discord and storage

Create a bot at the [Discord Developer Portal](https://discord.com/developers/applications). Install it in a private server you belong to with the bot scope; no administrator permission or privileged intents are needed. Enable DMs from that server. Enable Developer Mode in Discord and copy your numeric user ID. Store the bot token in a dedicated file, not an environment variable or Compose YAML.

Create a dedicated TrueNAS dataset and secret directory. The image runs as `pwuser`, UID/GID 1000; match ownership or set the Compose user to your dataset owner and verify browser launch. Example commands in the NAS shell (replace POOL):

```sh
install -d -m 700 -o 1000 -g 1000 /mnt/POOL/parking-bot/data
install -d -m 700 -o 1000 -g 1000 /mnt/POOL/parking-bot/secrets
```

Create `secrets/discord_token` using a secure editor, with mode 0400 and owner 1000. Do not put its value in shell command arguments/history. Session files can convey broader SSO access; protect dataset snapshots and backups as credentials. A read-only mount does not encrypt its host source file.

Run `parking-bot notify-test` in the application shell to test Discord independently of university authentication or readiness configuration. Delivery errors report the failing operation (create DM or send DM), HTTP status, and Discord's numeric error code when present. The application prints only locally defined explanations, never raw response bodies or tokens. Code 50007 means Discord cannot send messages to that user; verify your personal recipient ID, shared server membership, DM privacy settings, and blocked users. Codes 50001 and 50013 indicate access or permission failures. See [Discord's error definitions](https://docs.discord.com/developers/topics/opcodes-and-status-codes#json). An older image may print only `DeliveryError`; update the image to get these diagnostics. A successful manual test does not clear the background worker's previous error in `status`.

## 2. Install the container

Use TrueNAS **Apps → Discover → Install via YAML**, as described in the [Custom Apps documentation](https://apps.truenas.com/managing-apps/installing-custom-apps/). Start from [compose.yaml](compose.yaml). Replace the GHCR owner/version, pool paths, Discord user ID, and readiness placeholder. Use an empty readiness string for the first discovery session. Copy `chromium-seccomp.json` alongside your dataset and update its absolute path in `security_opt`.

The image and Python dependency both pin Playwright 1.58.0; update both together. The profile permits Chromium user namespaces under a non-root user. Keep Chromium's sandbox enabled. TrueNAS/kernel restrictions may still prevent launch: validate locally and on the NAS before trusting monitoring. Do not solve launch failures with privileged mode or silently disable the sandbox.

The Compose file has an init process, restart policy, 1 GiB shared memory, 2 GiB initial memory limit, and bounded logs. Measure memory during authentication and checks, then adjust as needed. Only port 6080 is published, on host loopback by default. VNC 5900 and X display TCP are not exposed. Chromium uses Playwright's internal pipe, with no remote debugging TCP endpoint.

For access from a laptop, connect over the existing VPN and forward the NAS port:

```sh
ssh -L 6080:127.0.0.1:6080 ADMIN@NAS_VPN_IP
```

Open `http://127.0.0.1:6080/vnc.html` locally during authentication. For direct phone access, bind the host side of Compose's port mapping to the NAS's specific VPN address and restrict the firewall to your administrator device. Open `http://NAS_VPN_IP:6080/vnc.html` on the VPN-connected phone. Never use a wildcard host binding. If traffic leaves the VPN over an unencrypted network segment, add HTTPS termination or an encrypted SSH tunnel across that segment. VNC's eight-character temporary password authenticates access; it does not encrypt traffic.

The web listener exists only during an explicit `auth start`. Browser tools and password files are cleaned up on success, cancellation, timeout, or normal termination. A separate GNU timeout limits desktop subprocesses even if the auth Python process is killed. After an uncatchable termination, wait for that deadline or restart the app before another login if ports/display remain occupied.

## 3. Feasibility gate: first authenticated inspection

The actual authenticated permit page has not been inspected for this implementation. Do this before considering the scheduler deployable:

1. Open an interactive TrueNAS application shell, or use `docker exec -it CONTAINER parking-bot auth start` from SSH. The command prints a temporary VNC password only to its TTY and stays foreground for at most 15 minutes. Keep that terminal open.
2. Connect to noVNC, complete the actual university sign-in and MFA, then navigate manually to permit selection. Never submit a purchase.
3. For initial discovery, leave `PARKING_READY_SELECTOR` empty. Use Chromium developer tools in the remote desktop (Ctrl-Shift-I) to inspect the loading behavior and identify the exact navigation links and a visible element that only appears after the full permit list is loaded. An empty selector allows inspection but never saves/installs a session. Do not capture credentials, page bodies, screenshots, traces, or network exports into this repository.
4. Verify exactly one `button#ptypeid_btn_514`, normalized label `Monthly Permit - Zone A1`, and value `514`. Observe available and unavailable controls when possible. Verify loading behavior under slow network conditions. Find any permit-data endpoints on other origins and configure `PARKING_DATA_REQUEST_PATTERNS` to observe them.
5. Cancel with Ctrl-C or `parking-bot auth stop`. Set `PARKING_READY_SELECTOR` and, if required, `PARKING_NAVIGATION` to a JSON array of locator strings that navigate only to selection. Keep these settings identical in the scheduler and auth command. Update/restart the app.
6. Run `auth start` again and sign in. On finding the verified ready permit list, the app exports cookies, local storage, and IndexedDB, closes remote access, and tests the candidate session in fresh headless Chromium through the configured navigation. Only a successful AVAILABLE or UNAVAILABLE result atomically replaces the old session and resumes the scheduler.
7. If reuse fails, the prior session remains intact. Investigate locally: wrong navigation/readiness, conditional access, short session lifetime, or origin-specific sessionStorage. Enable `PARKING_SESSION_STORAGE=true` only if sessionStorage is needed, then repeat. Do not claim feasibility if MFA is required on every visit or policy blocks the remote browser. Revise the design with the observed restriction.
8. Run `parking-bot notify-test` once and confirm the DM reaches your phone. `status --json` should then show a successful scheduled check. An explicit dry run may be delayed until the next eligible check; it does not send notifications.

Default recognized auth hosts are Microsoft login and OSU login. Add actual verified Duo/identity-provider hosts or a portal login selector if needed. A generic 403 intentionally raises an unknown operational alert rather than pausing as authentication loss. Missing or changed page structure requires review; never loosen selectors merely to suppress an alert.

## 4. Everyday operation and renewal

Run commands inside the application shell:

```sh
parking-bot status --json
parking-bot auth status
parking-bot auth start
parking-bot auth stop
parking-bot check --dry-run
parking-bot health
```

`auth start` must use an interactive TTY and holds the browser lock. If a check owns that lock, retry after it finishes (bounded to two minutes). Authentication-required alerts pause requests; daily reminders continue until validated renewal. The scheduler resumes automatically while preserving a future next-check time. It does not send an availability DM merely from viewing the login desktop; a scheduled fresh observation supplies that event.

Status shows the last result/reason/time, last successful check, next eligible check, auth condition, notification error, consecutive transient failures, heartbeat, and pending message count. Times are UTC epoch seconds. Logs contain result categories and safe reasons only. A healthy process can still need login or have broken Discord delivery: inspect status as well as Docker health. Docker health alone sends no external alert. A stopped NAS or broken Discord channel cannot report its own failure; independent uptime monitoring is outside v1.

## 5. Releases, upgrades, backups, rollback, and token rotation

Push a version tag such as `v0.1.0` after reviewing tests. GitHub Actions tests pull requests and main pushes, then publishes tag builds to `ghcr.io/<owner>/osu-parking-bot:0.1.0` and a full commit-SHA tag using `GITHUB_TOKEN`. Images do not automatically deploy. GHCR package visibility may require explicit configuration; if private, install a read-only package pull credential in TrueNAS. CI never needs the NAS, VPN, bot token, or university state.

Before upgrading, note the existing image digest, stop the app, and snapshot/back up the entire restricted data directory (including SQLite WAL files if present). Stopping first makes a filesystem copy consistent. Keep schema backups paired with image versions. Update the YAML image to the selected version or digest, restart, inspect health/status, and verify session reuse at the next scheduled check. Do not reset `next_check` to force repeated checks. To roll back, stop the app, restore the matching database backup if a schema upgrade occurred, and select the prior image reference. This release uses schema version 1 and rejects unknown versions.

Rotate the token in Discord, then overwrite the existing host token file in place using a secure editor while the app is stopped; preserve mode/owner. Editors that replace the inode can leave an existing bind mount on the old file, so restart/recreate the app after rotation. Run `notify-test` and inspect delivery failures. For a different bot identity, clear the `dm_channel` row from SQLite's `meta` table while stopped, then test; the usual same-bot token rotation does not need this.

## 6. Acceptance log: 48-hour TrueNAS soak

Do not mark the deployment accepted until the following are recorded on the target NAS:

- Initial mobile/desktop noVNC login and MFA succeed; no browser/login listener remains after success, stop, timeout, or cancellation during validation.
- A fresh headless process reuses the university session. Record sign-in time and any expiry time over 48 hours; do not assume a renewal interval.
- Confirm the calibrated readiness signal, intervening navigation, permit identity, disabled behavior, and relevant network requests on the actual page. If one availability state cannot be observed, mark it as unverified rather than changing inventory.
- Checks occur hourly plus jitter; restarting does not create an immediate extra check or replay missed checks. Restart once during the soak and confirm state survives.
- A first available observation creates one DM; reminders occur only after 24 hours and a fresh confirmation. Closure/reopening behavior is fixture-tested; observe live if it happens naturally.
- Expired authentication pauses polling, alerts once, reminds daily, and resumes after renewal. Check UNKNOWN and notification-failure visibility using local fixtures rather than disrupting university services.
- Verify NAS VPN-only port reachability, Chromium sandbox launch, permissions, graceful shutdown, heartbeat, memory/CPU peaks, backup recovery, and rollback.

Record timestamps, image digest, sanitized outcomes, measured memory, session lifetime, and unresolved items in a private operator log. Never copy university credentials or authenticated browser artifacts into CI evidence. Live sign-in, Discord delivery, TrueNAS operation, and this soak require administrator execution and are not established by the automated tests.
