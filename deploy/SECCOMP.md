# Chromium container profile

`chromium-seccomp.json` derives from [Moby's Docker default profile](https://github.com/moby/profiles/blob/main/seccomp/default.json), under [Apache-2.0](MOBY-LICENSE). The downloaded upstream file's SHA-256 is `785b2429264afba4d594320337cb17f144f3c7d51585f9805eef72e28f4f9334`.

Modification: append one `SCMP_ACT_ALLOW` rule for `clone`, `setns`, and `unshare`, as described in [Playwright's container guidance](https://playwright.dev/python/docs/docker), to support sandboxed Chromium under the non-root runtime user. The rest of Docker's default rules remain intact. The Playwright documentation/example is also Apache-2.0; its license is included as `PLAYWRIGHT-LICENSE`.

The older full example profile shipped with Playwright v1.58.0 caused this workstation's Docker 29.7.2/runc runtime to crash before application startup. Docker's current default plus the documented namespace allowances passed the offline desktop/headless smoke test. TrueNAS compatibility still needs verification on the target release; do not use privileged mode or an unconfined profile as an implicit fallback.
