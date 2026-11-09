# Deploying to Oracle Cloud (Always Free, Ampere A1)

Deployment-as-code for **obsidian-mcp** on an Oracle Cloud Always Free VM
(Ampere A1, Ubuntu 24.04 aarch64), with Obsidian Headless (`ob`) keeping the
vault in sync through Obsidian Sync and Caddy in front with automatic TLS.

- Public URL: `https://obsidian.mcp.htarrega.me`
- MCP connector (claude.ai → Settings → Connectors): `https://obsidian.mcp.htarrega.me/mcp`
- GitHub OAuth callback: `https://obsidian.mcp.htarrega.me/auth/callback`
- Authorized GitHub login: `htarrega`

The domain is configurable (`DOMAIN` / `--domain`) in every script and template.

> Architecture diagrams: [docs/deployment-architecture.md](../docs/deployment-architecture.md)

## Architecture

```
  claude.ai / Claude Code / Desktop
              │  HTTPS 443 (Let's Encrypt TLS, DNS-only on Cloudflare)
              ▼
 ┌──────────────────────── Oracle A1 VM (Ubuntu 24.04 arm64) ────────────────────────┐
 │  OCI Security List: 22, 80, 443   ─►  iptables INPUT (ACCEPT before the REJECT)    │
 │                                                                                    │
 │  caddy.service  :80/:443  ── reverse_proxy (flush_interval -1) ──►  127.0.0.1:8000 │
 │                                                                         │          │
 │  obsidian-mcp.service (User=obsidian, systemd sandbox)                  ▼          │
 │   └─ /opt/obsidian-mcp/app/.venv/bin/obsidian-remote-mcp   (TRANSPORT=http)        │
 │        ├─ GitHub OAuth (FASTMCP_HOME=/var/lib/obsidian/mcp-data)                    │
 │        ├─ index + watcher (inotify) on /var/lib/obsidian/vault                      │
 │        └─ supervised child: ob sync --continuous  (ENABLE_OB=true)                  │
 │                 └─ credentials/logs: /var/lib/obsidian/.config/obsidian-headless    │
 └──────────────────────────────────────────────────────────────────┬─────────────────┘
                                                                    │ outbound HTTPS
                                                                    ▼
                                                     Obsidian Sync (E2E) ◄─► your devices
```

There is no separate systemd unit for `ob`: the MCP server itself launches and
supervises `ob sync --continuous` (with backoff up to `OB_RESTART_BACKOFF_MAX`).
`KillMode=mixed` means that when the service stops, SIGTERM goes to the Python
process (which shuts `ob` down), and anything still alive after 30 s gets SIGKILL.

### Paths on the VM

| Path | Owner / mode | Contents |
|---|---|---|
| `/opt/obsidian-mcp/app` | root, 0755 | `git clone` at a specific commit (detached) + `.venv` |
| `/opt/obsidian-mcp/python` | root, 0755 | CPython managed by uv (`UV_PYTHON_INSTALL_DIR`) |
| `/opt/obsidian-mcp/DEPLOYED_COMMIT`, `PREVIOUS_COMMIT` | root | active and previous commit (rollback) |
| `/opt/obsidian-headless` | root, 0755 (read-only for everyone else) | `ob` installed with `npm ci` from `ob-headless/package-lock.json`; `.provision-stamp` = lock sha256 + Node ABI |
| `/usr/bin/ob` | root, symlink | → `/opt/obsidian-headless/node_modules/.bin/ob` (the `OB_BIN` default) |
| `/usr/local/bin/uv`, `uvx` | root, 0755 | uv from the sha256-pinned release tarball |
| `/etc/obsidian-mcp/env` | root:root 0600 (directory root:root 0700) | configuration and secrets (`EnvironmentFile`) |
| `/var/lib/obsidian` | obsidian 0750 | HOME of the `obsidian` user (shell `nologin`) |
| `/var/lib/obsidian/vault` | obsidian 0700 | the synced vault |
| `/var/lib/obsidian/mcp-data` | obsidian 0700 | `FASTMCP_HOME`: OAuth tokens, locks, audit log |
| `/var/lib/obsidian/.config/obsidian-headless` | obsidian 0700 | `ob` credentials, `sync/<id>/sync.log` |

## Files in this directory

| File | Destination on the VM |
|---|---|
| `provision.sh` | run once (idempotent, safe to re-run) |
| `update.sh` | deploy another commit / `rollback` |
| `ob-headless/package.json`, `package-lock.json` | `/opt/obsidian-headless` (`npm ci`, see [Bumping `ob`](#bumping-ob-obsidian-headless)) |
| `obsidian-mcp.service` | `/etc/systemd/system/obsidian-mcp.service` |
| `env.example` | `/etc/obsidian-mcp/env` (only if it does not exist) |
| `Caddyfile.template` | `/etc/caddy/Caddyfile` (rendered with `DOMAIN`) |
| `sysctl-obsidian.conf` | `/etc/sysctl.d/60-obsidian.conf` (inotify limits) |
| `sshd-hardening.conf` | `/etc/ssh/sshd_config.d/00-obsidian-hardening.conf` |
| `logrotate-obsidian-headless` | `/etc/logrotate.d/obsidian-headless` |
| `fail2ban-sshd.local` | `/etc/fail2ban/jail.d/obsidian-sshd.local` |
| `fail2ban-caddy-oauth.conf` | `/etc/fail2ban/filter.d/obsidian-caddy-oauth.conf` (filter over Caddy's JSON access log) |
| `fail2ban-caddy-oauth.local` | `/etc/fail2ban/jail.d/obsidian-caddy-oauth.local` (jails `obsidian-oauth-register` and `obsidian-oauth-authorize`) |
| `oauth-store-usage.sh` | `/usr/local/sbin/obsidian-mcp-oauth-usage` |
| `obsidian-mcp-oauth-usage.{service,timer}` | `/etc/systemd/system/` (weekly warning if `FASTMCP_HOME` > 200 MB) |

## OCI resources

Creating the VM, VCN, subnet, Security List (ingress TCP 22/80/443) and
reserved public IP is documented in [`oci-resources.md`](oci-resources.md)
*(pending: filled in by another step)*.

## Pinned versions

| Component | Version | Where it is pinned |
|---|---|---|
| obsidian-headless (`ob`) + its dependencies | `0.0.14`, every package with its sha512 `integrity` | `ob-headless/package.json` + `ob-headless/package-lock.json` (`OB_VERSION` / `--ob-version` only asserts the locked version) |
| Node.js | 22.x LTS (NodeSource, arm64) | `NODE_MAJOR` / `--node-major` |
| uv | `0.11.7`, release tarball sha256 per arch | `UV_VERSION` / `--uv-version`; hashes in `uv_pinned_sha256()` in `provision.sh` (`UV_SHA256` / `--uv-sha256` for an unpinned version) |
| Python | 3.12 (latest patch managed by uv; an exact `3.12.x` is accepted) | `PYTHON_VERSION` |
| Python dependencies | those in `uv.lock` (`uv sync --locked`) | deployed commit |
| obsidian-mcp | 40-hex commit SHA (never a branch/tag) | `COMMIT` / `--commit` |
| Caddy | `2.11.9` (apt, Cloudsmith repo `caddy/stable`; `latest` = unpinned) | `CADDY_VERSION` / `--caddy-version` |
| APT keys | fingerprints pinned in `provision.sh` (`CADDY_KEY_FPR`, `NODESOURCE_KEY_FPR`); aborts if the downloaded key does not match | `provision.sh` |

## Prerequisites

1. An Ampere A1 VM running Ubuntu 24.04, with a public IP and a Security List
   allowing ingress on TCP 22, 80 and 443 (see `oci-resources.md`).
2. DNS on Cloudflare: **A** record `obsidian.mcp` → public IP, **DNS only**
   (grey cloud). With the orange-cloud proxy, Caddy cannot issue the certificate
   as configured and streaming behaves differently.
3. An SSH alias on your machine (`~/.ssh/config`):

   ```
   Host obsidian-vps
       HostName <public IP>
       User ubuntu
       IdentityFile ~/.ssh/<your_key>
   ```

   Check that key-based login works **before** provisioning: the script disables
   `PasswordAuthentication` and `PermitRootLogin`.
4. A GitHub OAuth App (GitHub → Settings → Developer settings → OAuth Apps):
   - Homepage URL: `https://obsidian.mcp.htarrega.me`
   - Authorization callback URL: `https://obsidian.mcp.htarrega.me/auth/callback`
5. An Obsidian account with Sync, and the E2E encryption password of the remote vault.
6. The SHA of the commit to deploy (it must be pushed to `htarrega/obsidian-mcp`):
   `git rev-parse HEAD`.

## Step-by-step deployment

### 1. Copy `deploy/` to the VM

```bash
scp -r deploy obsidian-vps:~/deploy
# or on the VM: git clone https://github.com/htarrega/obsidian-mcp ~/src && cp -r ~/src/deploy ~/deploy
```

### 2. Provision

```bash
COMMIT=$(git rev-parse HEAD)   # on your machine, inside the repo
ssh -t obsidian-vps "sudo ~/deploy/provision.sh --commit $COMMIT"
# options: --domain, --acme-email you@example.com, --skip-upgrade, --no-start, --ssh-ports 22
```

Use the flags instead of `sudo COMMIT=... ./provision.sh`: `sudo` filters the
environment and only passes variables given on the command line if the sudoers
rule is `ALL` or has `SETENV` (it is on Oracle, but the flags do not depend
on it).

What it does (idempotent): `apt upgrade`; base packages; unattended-upgrades;
password-less `sshd` (verified with `sshd -T`); iptables rules for 80/443 (and
the SSH ports) inserted **before** Oracle's `REJECT` and saved with
`netfilter-persistent`; inotify limits; the `obsidian` user and directories; Node
22 + `obsidian-headless@0.0.14` (`npm ci` from the committed lock, built as
the unprivileged `obhbuild` user, installed root-owned in
`/opt/obsidian-headless`, `/usr/bin/ob` symlinked to it); uv from its
sha256-pinned release tarball + CPython in `/opt/obsidian-mcp/python`;
repo clone at the requested commit + `uv sync --locked --no-dev`;
`/etc/obsidian-mcp/env` from `env.example` (only if it does not exist); systemd
unit enabled; Caddy from the official repo (Cloudsmith) with `caddy validate`;
logrotate for `sync.log`; fail2ban jails for `sshd` and for the unauthenticated
OAuth endpoints (`POST /register`, `GET /authorize`, read from Caddy's JSON
access log); a weekly timer that warns in the journal if `FASTMCP_HOME` exceeds
200 MB (it never deletes anything). At the end it prints a summary with
versions and commit.

The first time, the service **does not start**: the env file still contains
`FILL_ME_IN`.

### 3. Obsidian Headless: login, vault and first sync

Every command runs as the `obsidian` user with the same `HOME` and
`XDG_CONFIG_HOME` the systemd unit uses, so the credentials end up in
`/var/lib/obsidian/.config/obsidian-headless`.

> **Is `-s /bin/bash` needed because the shell is `nologin`?** No.
> `sudo -u obsidian cmd` runs `cmd` directly (execve), without going through the
> user's login shell; the shell only matters with `sudo -i`, `sudo -s` or `su -`.
> `-H` and the explicit `env HOME=... XDG_CONFIG_HOME=...` avoid depending on
> sudo's environment policy (which would drop `XDG_CONFIG_HOME`).

Log in (asks for your Obsidian email, password and 2FA code):

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob login'
```

List the remote vaults:

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync-list-remote'
```

Link the remote vault to the local directory (asks for the E2E password):

```bash
ssh -t obsidian-vps 'sudo -u obsidian -H env -C /var/lib/obsidian/vault HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync-setup --vault "<NAME>" --path /var/lib/obsidian/vault --device-name oracle-vps'
```

> Note: `env -C` (coreutils ≥ 8.28) is used instead of
> `cd /var/lib/obsidian/vault && sudo -u obsidian ...` because the vault is
> `0700` and owned by `obsidian`, so the `ubuntu` user cannot `cd` into it;
> `env -C` changes directory while already running as `obsidian`.

First full sync, in the foreground (with the service stopped so that two `ob`
processes never run on the same vault at once):

```bash
ssh -t obsidian-vps 'sudo systemctl stop obsidian-mcp; sudo -u obsidian -H env HOME=/var/lib/obsidian XDG_CONFIG_HOME=/var/lib/obsidian/.config ob sync --path /var/lib/obsidian/vault'
ssh obsidian-vps "sudo find /var/lib/obsidian/vault -name '*.md' | wc -l"
```

The number of notes should (roughly) match your desktop vault.

### 4. Secrets

```bash
ssh -t obsidian-vps sudoedit /etc/obsidian-mcp/env
```

Fill in `OAUTH_GITHUB_CLIENT_ID` and `OAUTH_GITHUB_CLIENT_SECRET` (replacing
`FILL_ME_IN`); review `PUBLIC_BASE_URL`, `OAUTH_GITHUB_ALLOWED_LOGINS` and the
write policy. The file is not version-controlled and `provision.sh` never
overwrites it.

### 5. Start and watch the logs

```bash
ssh obsidian-vps 'sudo systemctl start obsidian-mcp && systemctl status obsidian-mcp --no-pager'
ssh obsidian-vps 'journalctl -u obsidian-mcp -f'           # Ctrl-C to exit
ssh obsidian-vps 'journalctl -u caddy -n 100 --no-pager'   # certificate issuance
```

(Re-running `provision.sh` once the env file is complete also starts/restarts
the service and waits for `/health`.)

### 6. Connect claude.ai

Settings → Connectors → Add custom connector → URL
`https://obsidian.mcp.htarrega.me/mcp` → log in with GitHub (`htarrega`).

## Verification

From your machine:

```bash
D=obsidian.mcp.htarrega.me
curl -fsS https://$D/health                                   # {"status":"ok",...}
curl -fsS https://$D/.well-known/oauth-protected-resource/mcp  # resource metadata
curl -fsS https://$D/.well-known/oauth-authorization-server    # AS metadata (issuer = https://$D)
curl -si -X POST https://$D/mcp -H 'content-type: application/json' -d '{}' | head -n 15
#   -> HTTP/2 401 + WWW-Authenticate: Bearer ... resource_metadata=...
curl -sI https://$D/health | grep -iE '^(server|strict-transport|x-content-type|referrer)'
#   -> no Server header; HSTS, nosniff and no-referrer present
nmap -Pn -p 1-65535 --open $D        # only 22, 80, 443 (or: nc -zv $D 8000 must fail)
```

On the VM:

```bash
sudo ss -tlnp            # 8000 (python) and 2019 (caddy admin) ONLY on 127.0.0.1; 80/443 caddy; 22 sshd
sudo iptables -L INPUT -n --line-numbers   # ACCEPT 80/443 above the REJECT
sudo sshd -T | grep -E 'passwordauthentication|permitrootlogin'
sudo fail2ban-client status sshd
sudo fail2ban-client status obsidian-oauth-register    # >20 POST /register in 10 min -> 1 h ban
sudo fail2ban-client status obsidian-oauth-authorize   # >60 GET /authorize in 10 min -> 1 h ban
sudo fail2ban-regex /var/log/caddy/obsidian-mcp-access.log \
  '/etc/fail2ban/filter.d/obsidian-caddy-oauth.conf[oauth_method=POST, oauth_path=/register]'
systemctl list-timers obsidian-mcp-oauth-usage.timer
journalctl -u obsidian-mcp-oauth-usage -n 5     # FASTMCP_HOME size (warns only, never deletes)
systemd-analyze security obsidian-mcp      # exposure ~2.9 "OK"
sysctl fs.inotify.max_user_watches fs.inotify.max_user_instances
pgrep -u obsidian -a                         # python + node (ob sync --continuous)
sudo logrotate -d /etc/logrotate.d/obsidian-headless
```

Checklist:

- [ ] `/health` returns 200 over HTTPS with a valid certificate.
- [ ] `/mcp` without a token → 401 with `WWW-Authenticate`.
- [ ] Port 8000 is not reachable from outside.
- [ ] Logging in from claude.ai as `htarrega` works; any other GitHub account is rejected.
- [ ] Creating a note from Claude → it shows up on the desktop via Sync (and vice versa).
- [ ] `systemctl restart obsidian-mcp` leaves no orphaned `ob` processes (`pgrep -u obsidian -a`).

## Updating and rolling back

```bash
# on your machine, after pushing:
NEW=$(git rev-parse HEAD)
ssh -t obsidian-vps "sudo ~/deploy/update.sh $NEW"   # fetch, checkout --detach, uv sync --locked, restart, /health
ssh -t obsidian-vps 'sudo ~/deploy/update.sh rollback' # back to /opt/obsidian-mcp/PREVIOUS_COMMIT
ssh obsidian-vps 'cat /opt/obsidian-mcp/DEPLOYED_COMMIT /opt/obsidian-mcp/PREVIOUS_COMMIT'
```

`update.sh` only changes the application. If the unit, the Caddyfile, `ob` or
the Node/uv versions change: copy the new `deploy/` over and re-run
`provision.sh --commit <sha> --skip-upgrade` (it does not touch
`/etc/obsidian-mcp/env`). If `uv sync` fails, `update.sh` restores the previous
commit before exiting.

### Bumping `ob` (obsidian-headless)

The version and every transitive dependency come from
`deploy/ob-headless/package-lock.json`; nothing is resolved on the VM.

1. On your machine, set the new exact version in
   `deploy/ob-headless/package.json` (`"obsidian-headless": "x.y.z"`, no `^`/`~`).
2. Regenerate the lock without running any install script:

   ```bash
   cd deploy/ob-headless
   rm -f package-lock.json
   npm install --package-lock-only --ignore-scripts --no-audit --no-fund
   ```

3. Review the diff of `package-lock.json`: every package must have a
   `resolved` URL on `registry.npmjs.org` and a `sha512-` `integrity`; check
   which packages have `hasInstallScript` (today only `better-sqlite3`), since
   those run during the build. Commit both files.
4. Copy the new `deploy/` to the VM and re-run `provision.sh --commit <sha>
   --skip-upgrade`. The lock digest changed, so it rebuilds `/opt/obsidian-headless`
   (a few minutes: better-sqlite3 is compiled), relinks `/usr/bin/ob` and
   restarts the service.

How it is built: `npm ci --omit=dev` verifies every tarball against the lock
and runs as the system user `obhbuild` (`setpriv --no-new-privs`, empty
environment) in a throwaway `/var/tmp/obh-build.*` directory. Install scripts
are allowed only in that unprivileged build, because better-sqlite3 needs one;
it is compiled from its integrity-checked sources with node-gyp
(`npm_config_build_from_source=true`, using the headers in `/usr/include/node`
when the nodejs package ships them, otherwise node-gyp downloads them from
nodejs.org and checks them against its `SHASUMS256.txt`) rather than
downloading an unverified prebuilt binary. After a smoke test (load
better-sqlite3, `ob --version`), the tree is copied to `/opt/obsidian-headless`
owned by `root:root` and not writable by anyone else, so neither `obhbuild` nor
the `obsidian` service user can alter `ob`. On re-provision, a leftover global
`npm install -g obsidian-headless` is uninstalled.

### Bumping uv

`provision.sh` downloads
`https://github.com/astral-sh/uv/releases/download/<version>/uv-<arch>-unknown-linux-gnu.tar.gz`
and refuses it unless its sha256 matches the value pinned in
`uv_pinned_sha256()` (aarch64 and x86_64). To bump:

1. Get the digests of the new release, e.g.
   `gh release view -R astral-sh/uv <version> --json assets --jq '.assets[] | select(.name | test("^uv-(aarch64|x86_64)-unknown-linux-gnu.tar.gz$")) | "\(.name) \(.digest)"'`,
   and cross-check them with the release's `.sha256` files (or a local
   `sha256sum` of the tarballs).
2. Add `<version>/aarch64` and `<version>/x86_64` lines to `uv_pinned_sha256()`
   and change the `UV_VERSION` default in `provision.sh`.
3. Re-run `provision.sh` on the VM.

For a one-off version without editing the script, pass both
`--uv-version <version> --uv-sha256 <hex>`; without a pinned or explicit hash
`provision.sh` aborts, and an explicit hash that contradicts a pinned one is
refused.

## Policy settings

**Restrict writes to a folder** (`WRITE_PATHS`): empty = the whole vault
except `DENY_WRITE_PATHS`. With a trailing slash the rule covers the folder and
everything below it; without one, only that exact path. Separate multiple rules
with commas:

```bash
ssh -t obsidian-vps sudoedit /etc/obsidian-mcp/env   # WRITE_PATHS=Inbox/,AI-Output/
ssh obsidian-vps 'sudo systemctl restart obsidian-mcp'
```

`DENY_WRITE_PATHS` **replaces** the default list (it does not extend it): keep
`.obsidian/` (Obsidian config and `ob`'s `.obsidian/.sync.lock`), `.trash/`
and `_AI_INSTRUCTIONS.md` in addition to whatever you add.

**Temporary read-only mode**: `READ_ONLY=true` and restart.

**`ENABLE_OB_ADMIN`**: leave it `false`. Enable it only temporarily to change
the sync configuration without SSH (excluded folders, file types, config
categories, conflict strategy) and set it back to `false` when done: these are
persistent changes and any authenticated MCP session could make them. It is
ignored when `READ_ONLY=true`. With `ENABLE_OB=true`, `sync_status`,
`sync_logs`, `ob_info` and `list_remote_vaults` are always available;
`ENABLE_OB_OPS=true` adds `sync_restart`. Login, logout, setup and unlink are
never exposed.

**High-impact tools** (`ENABLE_DELETE`, `ENABLE_MOVE`,
`ENABLE_FOLDER_RENAME`, `ENABLE_BULK_REPLACE`): disabled; any change propagates
to all your devices through Sync. If you enable any of them, keep a backup or
Obsidian Sync version history at hand.

## Oracle Always Free: caveats

- **Idle reclamation**: Oracle may stop (and reclaim) Always Free instances
  considered *idle* if, over 7 days, the 95th percentile of CPU usage stays
  below 20 % (and network and, on A1, memory are also < 20 %). A personal MCP
  server is idle almost all the time, so this is a real risk.
- **Pay-As-You-Go**: upgrading the account to PAYG (with a card) removes the
  idle-instance reclamation and keeps everything within the Always Free limits
  free (A1: 4 OCPU / 24 GB in total). Set a budget alert (e.g. €1) to catch any
  paid resource created by mistake.
- Back up `/etc/obsidian-mcp/env` and, if you want to keep the OAuth sessions,
  `/var/lib/obsidian/mcp-data`. The vault can be recovered from Obsidian Sync.

## Pending (supply-chain hardening)

- **NodeSource key fingerprint**: NodeSource does not publish it in its
  documentation. `NODESOURCE_KEY_FPR` is the fingerprint of the key served by
  `deb.nodesource.com`, which matches the one on `keyserver.ubuntu.com`
  (2026-11-08). Confirm it against an official source if one appears. The Caddy
  fingerprint has been checked against the Cloudsmith API.

Done: `ob` and its dependencies are installed with `npm ci` from a committed,
integrity-pinned `package-lock.json` in an unprivileged build, and uv is
installed from its release tarball with a pinned sha256 (see
[Bumping `ob`](#bumping-ob-obsidian-headless) and [Bumping uv](#bumping-uv)).

## Troubleshooting

- **Caddy cannot obtain a certificate**: DNS is not *DNS only*, or 80/443 are
  missing from the Security List or iptables (`sudo iptables -L INPUT -n --line-numbers`).
- **`ob` fails with ENOSPC / does not detect changes**: inotify limits (`sysctl
  fs.inotify.max_user_watches`).
- **`npm ci` fails compiling `better-sqlite3`**: `build-essential`/`python3`
  are missing (`provision.sh` installs them); the build directory
  `/var/tmp/obh-build.*` is kept for inspection (delete it afterwards).
- **`ob` version or lock mismatch**: `provision.sh` aborts if `--ob-version`
  differs from the version in `ob-headless/package-lock.json`, or if `npm ci`
  finds the lock out of sync with `package.json`; regenerate the lock (see
  [Bumping `ob`](#bumping-ob-obsidian-headless)).
- **uv sha256 mismatch**: the downloaded tarball is not the pinned one; do not
  override it without checking the release's `.sha256` files.
- **The service restarts in a loop**: `journalctl -u obsidian-mcp -n 200`;
  usually a `ConfigError` (misspelled variable) or the vault not configured in `ob`.
