#!/usr/bin/env bash
# provision.sh — idempotent provisioning of obsidian-mcp on an Oracle Cloud
# Always Free VM (Ampere A1, Ubuntu 24.04 aarch64). Run as root ON THE VM from
# a copy of this deploy/ directory:
#
#   sudo COMMIT=<40-hex sha> ./provision.sh [--skip-upgrade] [--no-start]
#
# Settings (environment variables or flags):
#   --domain / DOMAIN                  public hostname          (obsidian.mcp.htarrega.me)
#   --repo-url / REPO_URL              git repository           (https://github.com/htarrega/obsidian-mcp.git)
#   --commit / COMMIT                  REQUIRED, full 40-hex commit SHA (no branches/tags)
#   --ob-version / OB_VERSION          obsidian-headless npm version (0.0.14)
#   --uv-version / UV_VERSION          uv version               (0.11.7)
#   --python-version / PYTHON_VERSION  uv-managed CPython        (3.12; may be a full 3.12.x)
#   --node-major / NODE_MAJOR          NodeSource major version (22)
#   --caddy-version / CADDY_VERSION    apt version of caddy     (empty = newest in repo)
#   --acme-email / ACME_EMAIL          ACME account e-mail      (empty = none)
#   --ssh-ports / SSH_ALLOWED_PORTS    comma-separated SSH ports kept open in
#                                      iptables and watched by fail2ban (22)
#   --skip-upgrade                     skip `apt-get upgrade`
#   --no-start                         never (re)start obsidian-mcp
#
# Re-running is safe: existing /etc/obsidian-mcp/env is never overwritten, rules
# and packages are only added when missing.

set -euo pipefail
umask 022

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DOMAIN="${DOMAIN:-obsidian.mcp.htarrega.me}"
REPO_URL="${REPO_URL:-https://github.com/htarrega/obsidian-mcp.git}"
COMMIT="${COMMIT:-}"
OB_VERSION="${OB_VERSION:-0.0.14}"
UV_VERSION="${UV_VERSION:-0.11.7}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
NODE_MAJOR="${NODE_MAJOR:-22}"
CADDY_VERSION="${CADDY_VERSION:-}"
ACME_EMAIL="${ACME_EMAIL:-}"
SSH_ALLOWED_PORTS="${SSH_ALLOWED_PORTS:-22}"
SKIP_UPGRADE=0
NO_START=0

SVC_USER=obsidian
BASE_DIR=/var/lib/obsidian
VAULT_DIR="$BASE_DIR/vault"
MCP_DATA_DIR="$BASE_DIR/mcp-data"
XDG_DIR="$BASE_DIR/.config"
OPT_DIR=/opt/obsidian-mcp
APP_DIR="$OPT_DIR/app"
PY_DIR="$OPT_DIR/python"
ETC_DIR=/etc/obsidian-mcp
ENV_FILE="$ETC_DIR/env"
UNIT_NAME=obsidian-mcp.service
CONSOLE_SCRIPT=obsidian-remote-mcp

export DEBIAN_FRONTEND=noninteractive
APT_OPTS=(-y -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# uv: root-owned shared interpreter location (readable by `obsidian` with
# ProtectHome=true), never fall back to a system Python.
export UV_PYTHON_INSTALL_DIR="$PY_DIR"
export UV_PYTHON_PREFERENCE=only-managed
export UV_CACHE_DIR=/var/cache/obsidian-mcp/uv

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'; }

parse_args() {
	while (($#)); do
		case "$1" in
			--domain) DOMAIN="${2:?}"; shift 2 ;;
			--repo-url) REPO_URL="${2:?}"; shift 2 ;;
			--commit) COMMIT="${2:?}"; shift 2 ;;
			--ob-version) OB_VERSION="${2:?}"; shift 2 ;;
			--uv-version) UV_VERSION="${2:?}"; shift 2 ;;
			--python-version) PYTHON_VERSION="${2:?}"; shift 2 ;;
			--node-major) NODE_MAJOR="${2:?}"; shift 2 ;;
			--caddy-version) CADDY_VERSION="${2:?}"; shift 2 ;;
			--acme-email) ACME_EMAIL="${2:?}"; shift 2 ;;
			--ssh-ports) SSH_ALLOWED_PORTS="${2:?}"; shift 2 ;;
			--skip-upgrade) SKIP_UPGRADE=1; shift ;;
			--no-start) NO_START=1; shift ;;
			-h | --help) usage; exit 0 ;;
			*) die "unknown argument: $1 (see --help)" ;;
		esac
	done
}

validate() {
	[[ $EUID -eq 0 ]] || die "must run as root (sudo)"
	[[ $COMMIT =~ ^[0-9a-f]{40}$ ]] \
		|| die "COMMIT must be a full 40-character lowercase hex SHA (got '${COMMIT}'); branch names and tags are refused"
	[[ $DOMAIN =~ ^[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?(\.[A-Za-z0-9]([A-Za-z0-9-]*[A-Za-z0-9])?)+$ ]] \
		|| die "DOMAIN is not a valid hostname: '$DOMAIN'"
	[[ $SSH_ALLOWED_PORTS =~ ^[0-9]+(,[0-9]+)*$ ]] || die "SSH_ALLOWED_PORTS must be comma-separated port numbers"
	[[ $OB_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "OB_VERSION must be an exact x.y.z version"
	[[ $UV_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "UV_VERSION must be an exact x.y.z version"
	[[ $NODE_MAJOR =~ ^[0-9]+$ ]] || die "NODE_MAJOR must be a number"
	[[ -z $ACME_EMAIL || $ACME_EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || die "ACME_EMAIL looks invalid"
	local f
	for f in obsidian-mcp.service Caddyfile.template env.example sysctl-obsidian.conf \
		sshd-hardening.conf logrotate-obsidian-headless fail2ban-sshd.local \
		fail2ban-caddy-oauth.conf fail2ban-caddy-oauth.local oauth-store-usage.sh \
		obsidian-mcp-oauth-usage.service obsidian-mcp-oauth-usage.timer; do
		[[ -f $SCRIPT_DIR/$f ]] || die "missing $SCRIPT_DIR/$f (copy the whole deploy/ directory)"
	done
	if [[ $(dpkg --print-architecture) != arm64 ]]; then
		warn "architecture is $(dpkg --print-architecture), this script targets arm64 (Ampere A1); continuing"
	fi
}

apt_install() { apt-get install "${APT_OPTS[@]}" --no-install-recommends "$@"; }

step_packages() {
	log "System packages"
	# iptables-persistent asks via debconf whether to save current rules.
	echo 'iptables-persistent iptables-persistent/autosave_v4 boolean true' | debconf-set-selections
	echo 'iptables-persistent iptables-persistent/autosave_v6 boolean true' | debconf-set-selections
	apt-get update
	if ((SKIP_UPGRADE)); then
		info "skipping apt-get upgrade (--skip-upgrade)"
	else
		apt-get upgrade "${APT_OPTS[@]}"
	fi
	apt_install unattended-upgrades fail2ban python3-systemd ripgrep netfilter-persistent \
		iptables-persistent git curl ca-certificates gnupg build-essential python3 logrotate \
		debian-keyring debian-archive-keyring apt-transport-https
}

step_unattended_upgrades() {
	log "Unattended upgrades"
	cat >/etc/apt/apt.conf.d/20auto-upgrades <<-'EOF'
		APT::Periodic::Update-Package-Lists "1";
		APT::Periodic::Unattended-Upgrade "1";
	EOF
	systemctl enable --now unattended-upgrades.service
}

step_sshd() {
	log "sshd hardening"
	install -m 0644 "$SCRIPT_DIR/sshd-hardening.conf" /etc/ssh/sshd_config.d/00-obsidian-hardening.conf
	# With socket activation (Ubuntu 24.04) the privsep dir may not exist yet.
	install -d -m 0755 /run/sshd
	sshd -t || die "sshd config test failed; NOT reloading ssh (fix /etc/ssh/sshd_config.d/)"
	sshd -T | grep -qx 'passwordauthentication no' \
		|| die "sshd -T still reports password authentication enabled"
	sshd -T | grep -qx 'permitrootlogin no' || warn "sshd -T does not report PermitRootLogin no"
	systemctl try-reload-or-restart ssh.service
	info "PasswordAuthentication no (verified with sshd -T)"
}

# Number of the first INPUT rule whose `-S` line contains $2 (empty if none).
rule_num() {
	local ipt=$1 pattern=$2
	"$ipt" -S INPUT | awk -v pat="$pattern" 'NR > 1 && index($0, pat) { print NR - 1; exit }'
}

# Ensure a tcp ACCEPT for $2 in INPUT sits BEFORE Oracle's catch-all REJECT.
ensure_input_accept() {
	local ipt=$1 port=$2 tag="obsidian-mcp-tcp-$2"
	local spec=(-p tcp -m state --state NEW -m tcp --dport "$port" -m comment --comment "$tag" -j ACCEPT)
	local reject_num rule_num_now
	reject_num=$(rule_num "$ipt" "-j REJECT")
	if "$ipt" -C INPUT "${spec[@]}" 2>/dev/null; then
		rule_num_now=$(rule_num "$ipt" "$tag")
		if [[ -z $reject_num || $rule_num_now -lt $reject_num ]]; then
			info "$ipt: tcp/$port already accepted (rule $rule_num_now)"
			return
		fi
		info "$ipt: tcp/$port rule is after REJECT, moving it"
		"$ipt" -D INPUT "${spec[@]}"
		reject_num=$(rule_num "$ipt" "-j REJECT")
	fi
	if [[ -n $reject_num ]]; then
		"$ipt" -I INPUT "$reject_num" "${spec[@]}"
		info "$ipt: inserted ACCEPT tcp/$port at position $reject_num (before REJECT)"
	else
		"$ipt" -A INPUT "${spec[@]}"
		info "$ipt: no REJECT rule in INPUT, appended ACCEPT tcp/$port"
	fi
}

step_firewall() {
	log "Host firewall (iptables, persisted with netfilter-persistent)"
	local port ports=()
	IFS=',' read -r -a ports <<<"$SSH_ALLOWED_PORTS"
	ports+=(80 443)
	for port in "${ports[@]}"; do
		ensure_input_accept iptables "$port"
	done
	# Oracle images usually ship no IPv6 REJECT; mirror the rules only if one
	# exists (otherwise there's nothing in the way to open).
	if command -v ip6tables >/dev/null && [[ -n $(rule_num ip6tables "-j REJECT") ]]; then
		for port in "${ports[@]}"; do
			ensure_input_accept ip6tables "$port"
		done
	else
		info "ip6tables: no REJECT rule in INPUT, nothing to do"
	fi
	netfilter-persistent save
}

step_sysctl() {
	log "sysctl (inotify limits)"
	install -m 0644 "$SCRIPT_DIR/sysctl-obsidian.conf" /etc/sysctl.d/60-obsidian.conf
	sysctl -p /etc/sysctl.d/60-obsidian.conf
}

step_user() {
	log "Service user and directories"
	getent group "$SVC_USER" >/dev/null || groupadd --system "$SVC_USER"
	if ! id -u "$SVC_USER" >/dev/null 2>&1; then
		useradd --system --gid "$SVC_USER" --home-dir "$BASE_DIR" --no-create-home \
			--shell /usr/sbin/nologin --comment "Obsidian MCP + Headless sync" "$SVC_USER"
	fi
	install -d -o "$SVC_USER" -g "$SVC_USER" -m 0750 "$BASE_DIR"
	install -d -o "$SVC_USER" -g "$SVC_USER" -m 0700 "$VAULT_DIR" "$MCP_DATA_DIR" "$XDG_DIR"
}

step_repos() {
	log "APT repositories (NodeSource ${NODE_MAJOR}.x, Caddy stable)"
	local arch
	arch=$(dpkg --print-architecture)
	install -d -m 0755 /etc/apt/keyrings
	if [[ ! -s /etc/apt/keyrings/nodesource.gpg ]]; then
		curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
			| gpg --dearmor --yes -o /etc/apt/keyrings/nodesource.gpg
	fi
	echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
		>/etc/apt/sources.list.d/nodesource.list
	# Prefer NodeSource's nodejs over Ubuntu's (18.x in noble).
	printf 'Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n' \
		>/etc/apt/preferences.d/nodesource

	if [[ ! -s /usr/share/keyrings/caddy-stable-archive-keyring.gpg ]]; then
		curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
			| gpg --dearmor --yes -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	fi
	curl -1sLf https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
		-o /etc/apt/sources.list.d/caddy-stable.list
	chmod 0644 /etc/apt/sources.list.d/caddy-stable.list /usr/share/keyrings/caddy-stable-archive-keyring.gpg
	apt-get update
}

step_node_ob() {
	log "Node.js ${NODE_MAJOR} + obsidian-headless@${OB_VERSION}"
	apt_install nodejs
	local major
	major=$(node -p 'process.versions.node.split(".")[0]')
	[[ $major == "$NODE_MAJOR" ]] || die "node major is $major, expected $NODE_MAJOR (check apt pinning)"
	if npm ls -g --depth=0 obsidian-headless 2>/dev/null | grep -q "obsidian-headless@${OB_VERSION}\$"; then
		info "obsidian-headless@${OB_VERSION} already installed"
	else
		# better-sqlite3 is native; build-essential + python3 (installed above)
		# let node-gyp compile it if no linux-arm64 prebuild matches.
		npm install -g --no-fund --no-audit "obsidian-headless@${OB_VERSION}"
	fi
	local ob_path
	ob_path=$(command -v ob) || die "ob not found on PATH after npm install"
	info "ob -> $ob_path ($(readlink -f "$ob_path"))"
	[[ $ob_path == /usr/bin/ob ]] || warn "ob is at $ob_path, not /usr/bin/ob: set OB_BIN accordingly in $ENV_FILE"
}

step_uv_python() {
	log "uv ${UV_VERSION} + CPython ${PYTHON_VERSION} (in ${PY_DIR})"
	local have=""
	[[ -x /usr/local/bin/uv ]] && have=$(/usr/local/bin/uv --version | awk '{print $2}')
	if [[ $have != "$UV_VERSION" ]]; then
		local installer
		installer=$(mktemp)
		curl -fsSL "https://astral.sh/uv/${UV_VERSION}/install.sh" -o "$installer"
		env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh "$installer"
		rm -f "$installer"
	fi
	[[ $(/usr/local/bin/uv --version | awk '{print $2}') == "$UV_VERSION" ]] || die "uv version mismatch after install"
	install -d -m 0755 "$OPT_DIR" "$PY_DIR"
	install -d -m 0700 "$UV_CACHE_DIR"
	/usr/local/bin/uv python install --no-bin "$PYTHON_VERSION"
}

step_app() {
	log "Application checkout at ${COMMIT}"
	if [[ ! -d $APP_DIR/.git ]]; then
		git clone --no-checkout "$REPO_URL" "$APP_DIR"
	else
		git -C "$APP_DIR" remote set-url origin "$REPO_URL"
	fi
	git -C "$APP_DIR" fetch --prune --tags origin
	if ! git -C "$APP_DIR" cat-file -e "${COMMIT}^{commit}" 2>/dev/null; then
		# Commit not reachable from any branch/tag (e.g. a PR head): fetch it directly.
		git -C "$APP_DIR" fetch origin "$COMMIT"
	fi
	local previous
	previous=$(git -C "$APP_DIR" rev-parse --verify -q HEAD || true)
	git -C "$APP_DIR" checkout --force --detach "$COMMIT"
	(
		cd "$APP_DIR"
		# --no-editable: install the package into the venv instead of a .pth to
		# src/, so the venv is self-contained; bytecode precompiled because the
		# tree is read-only for the service user.
		/usr/local/bin/uv sync --locked --no-dev --no-editable --compile-bytecode --python "$PYTHON_VERSION"
	)
	if [[ -n $previous && $previous != "$COMMIT" ]]; then
		echo "$previous" >"$OPT_DIR/PREVIOUS_COMMIT"
	fi
	echo "$COMMIT" >"$OPT_DIR/DEPLOYED_COMMIT"
	[[ -x $APP_DIR/.venv/bin/$CONSOLE_SCRIPT ]] || die "console script $CONSOLE_SCRIPT missing from venv"
	# Prove the service user can execute the interpreter and import the package.
	runuser -u "$SVC_USER" -- "$APP_DIR/.venv/bin/python" -c 'import obsidian_mcp, sys; print("    python", sys.version.split()[0], "imports obsidian_mcp OK")'
}

step_env() {
	log "Environment file ${ENV_FILE}"
	# root-only: systemd reads EnvironmentFile as root before dropping to the
	# service user, which never needs (and must not get) read access to it.
	install -d -o root -g root -m 0700 "$ETC_DIR"
	if [[ ! -e $ENV_FILE ]]; then
		local tmp
		tmp=$(mktemp)
		sed "s/obsidian\\.mcp\\.htarrega\\.me/${DOMAIN}/g" "$SCRIPT_DIR/env.example" >"$tmp"
		install -o root -g root -m 0600 "$tmp" "$ENV_FILE"
		rm -f "$tmp"
		info "created from env.example (fill in the RELLENAR values)"
	else
		chown root:root "$ENV_FILE"
		chmod 0600 "$ENV_FILE"
		info "exists, left untouched (permissions enforced)"
		grep -qx "PUBLIC_BASE_URL=https://${DOMAIN}" "$ENV_FILE" \
			|| warn "PUBLIC_BASE_URL in $ENV_FILE is not https://${DOMAIN}"
	fi
}

step_logrotate() {
	log "logrotate for ob sync.log"
	install -m 0644 "$SCRIPT_DIR/logrotate-obsidian-headless" /etc/logrotate.d/obsidian-headless
	logrotate -d /etc/logrotate.d/obsidian-headless >/dev/null 2>&1 \
		|| die "logrotate rejected /etc/logrotate.d/obsidian-headless (run: logrotate -d it)"
}

step_fail2ban() {
	log "fail2ban jails (sshd, OAuth endpoints)"
	sed "s/__SSH_PORTS__/${SSH_ALLOWED_PORTS}/" "$SCRIPT_DIR/fail2ban-sshd.local" \
		>/etc/fail2ban/jail.d/obsidian-sshd.local
	chmod 0644 /etc/fail2ban/jail.d/obsidian-sshd.local
	# Rate-limit unauthenticated POST /register (DCR) and GET /authorize from
	# Caddy's JSON access log. The log must exist or fail2ban refuses to start
	# the jail, so create it (caddy-owned) before the first request.
	install -m 0644 "$SCRIPT_DIR/fail2ban-caddy-oauth.conf" /etc/fail2ban/filter.d/obsidian-caddy-oauth.conf
	install -m 0644 "$SCRIPT_DIR/fail2ban-caddy-oauth.local" /etc/fail2ban/jail.d/obsidian-caddy-oauth.local
	local access_log=/var/log/caddy/obsidian-mcp-access.log
	if [[ ! -e $access_log ]]; then
		install -o caddy -g caddy -m 0644 /dev/null "$access_log"
	fi
	fail2ban-client -t >/dev/null || die "fail2ban configuration test failed"
	systemctl enable fail2ban.service
	systemctl restart fail2ban.service
}

step_caddy() {
	log "Caddy"
	if [[ -n $CADDY_VERSION ]]; then
		apt_install "caddy=${CADDY_VERSION}"
	else
		apt_install caddy
	fi
	local tmp
	tmp=$(mktemp)
	if [[ -n $ACME_EMAIL ]]; then
		sed -e "s/__DOMAIN__/${DOMAIN}/g" -e "s/__ACME_EMAIL__/${ACME_EMAIL}/g" "$SCRIPT_DIR/Caddyfile.template" >"$tmp"
	else
		sed -e "s/__DOMAIN__/${DOMAIN}/g" -e '/__ACME_EMAIL__/d' "$SCRIPT_DIR/Caddyfile.template" >"$tmp"
	fi
	caddy validate --config "$tmp" --adapter caddyfile
	install -o root -g caddy -m 0644 "$tmp" /etc/caddy/Caddyfile
	rm -f "$tmp"
	# `caddy validate` as root may have created the access log root-owned.
	install -d -o caddy -g caddy -m 0755 /var/log/caddy
	chown -R caddy:caddy /var/log/caddy
	systemctl enable caddy.service
	if systemctl is-active -q caddy.service; then
		systemctl reload caddy.service
	else
		systemctl start caddy.service
	fi
}

step_oauth_usage_timer() {
	log "Weekly OAuth store size check (FASTMCP_HOME, warns above 200 MB)"
	install -m 0755 "$SCRIPT_DIR/oauth-store-usage.sh" /usr/local/sbin/obsidian-mcp-oauth-usage
	install -m 0644 "$SCRIPT_DIR/obsidian-mcp-oauth-usage.service" "$SCRIPT_DIR/obsidian-mcp-oauth-usage.timer" \
		/etc/systemd/system/
	systemctl daemon-reload
	systemctl enable --now obsidian-mcp-oauth-usage.timer
}

step_service() {
	log "systemd unit ${UNIT_NAME}"
	install -m 0644 "$SCRIPT_DIR/obsidian-mcp.service" "/etc/systemd/system/${UNIT_NAME}"
	systemctl daemon-reload
	systemctl enable "$UNIT_NAME"
	if grep -q 'RELLENAR' "$ENV_FILE"; then
		warn "$ENV_FILE still contains RELLENAR placeholders: service NOT started"
		cat <<-EOF
			    Next steps (see deploy/README.md):
			      1. ob login / ob sync-setup / initial ob sync as user ${SVC_USER}
			      2. sudoedit ${ENV_FILE}   (OAUTH_GITHUB_CLIENT_ID / OAUTH_GITHUB_CLIENT_SECRET)
			      3. sudo systemctl start ${UNIT_NAME}
		EOF
		SERVICE_STATE="enabled, not started (secrets missing)"
		return
	fi
	if ((NO_START)); then
		SERVICE_STATE="enabled, not (re)started (--no-start)"
		return
	fi
	systemctl restart "$UNIT_NAME"
	local i
	for i in $(seq 1 60); do
		if curl -fsS -o /dev/null http://127.0.0.1:8000/health; then
			SERVICE_STATE="running, /health OK after ${i}s"
			return
		fi
		sleep 1
	done
	SERVICE_STATE="started, but /health not OK after 60s (journalctl -u ${UNIT_NAME})"
	warn "$SERVICE_STATE"
}

summary() {
	log "Summary"
	local caddy_v python_v
	caddy_v=$(caddy version 2>/dev/null | awk '{print $1}')
	python_v=$("$APP_DIR/.venv/bin/python" -c 'import sys; print(sys.version.split()[0])')
	cat <<-EOF
		    domain          : https://${DOMAIN}  (connector: https://${DOMAIN}/mcp)
		    repo            : ${REPO_URL}
		    deployed commit : $(git -C "$APP_DIR" rev-parse HEAD)  ($(git -C "$APP_DIR" log -1 --format='%cs %s'))
		    previous commit : $(cat "$OPT_DIR/PREVIOUS_COMMIT" 2>/dev/null || echo '-')
		    node            : $(node --version)
		    obsidian-headless: $(npm ls -g --depth=0 obsidian-headless 2>/dev/null | grep -o 'obsidian-headless@[^ ]*' || echo '?')
		    uv              : $(/usr/local/bin/uv --version)
		    python (venv)   : ${python_v}
		    caddy           : ${caddy_v}
		    service         : ${SERVICE_STATE}
	EOF
	if [[ -f /var/run/reboot-required ]]; then
		warn "a reboot is required to finish package upgrades (sudo reboot)"
	fi
}

main() {
	parse_args "$@"
	validate
	SERVICE_STATE="unknown"
	step_packages
	step_unattended_upgrades
	step_sshd
	step_firewall
	step_sysctl
	step_user
	step_repos
	step_node_ob
	step_uv_python
	step_app
	step_env
	step_logrotate
	# After Caddy: the OAuth jails read Caddy's access log (and its user).
	step_caddy
	step_fail2ban
	step_oauth_usage_timer
	step_service
	summary
}

main "$@"
