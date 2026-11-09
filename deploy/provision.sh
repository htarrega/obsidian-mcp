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
#   --ob-version / OB_VERSION          obsidian-headless version expected in
#                                      ob-headless/package-lock.json (empty = take
#                                      it from the lock; a mismatch aborts)
#   --uv-version / UV_VERSION          uv version               (0.11.7)
#   --uv-sha256 / UV_SHA256            sha256 of the uv release tarball; required
#                                      only for a UV_VERSION/arch not pinned below
#   --python-version / PYTHON_VERSION  uv-managed CPython        (3.12; may be a full 3.12.x)
#   --node-major / NODE_MAJOR          NodeSource major version (22)
#   --caddy-version / CADDY_VERSION    apt version of caddy     (2.11.9; "latest" = newest in repo)
#   --acme-email / ACME_EMAIL          ACME account e-mail      (empty = none)
#   --ssh-ports / SSH_ALLOWED_PORTS    comma-separated SSH ports kept open in
#                                      iptables and watched by fail2ban (22)
#   --skip-upgrade                     skip `apt-get upgrade`
#   --no-start                         never (re)start obsidian-mcp
#
# Re-running is safe: existing /etc/obsidian-mcp/env is never overwritten, rules
# and packages are only added when missing; obsidian-headless is rebuilt only
# when ob-headless/package-lock.json (or the Node ABI) changes.

set -euo pipefail
umask 022

SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

DOMAIN="${DOMAIN:-obsidian.mcp.htarrega.me}"
REPO_URL="${REPO_URL:-https://github.com/htarrega/obsidian-mcp.git}"
COMMIT="${COMMIT:-}"
OB_VERSION="${OB_VERSION:-}"
UV_VERSION="${UV_VERSION:-0.11.7}"
UV_SHA256="${UV_SHA256:-}"
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
NODE_MAJOR="${NODE_MAJOR:-22}"
CADDY_VERSION="${CADDY_VERSION:-2.11.9}"
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
# obsidian-headless: installed with `npm ci` from the committed lock into a
# root-owned prefix, built by an unprivileged throwaway user.
OBH_SRC_DIR="$SCRIPT_DIR/ob-headless"
OBH_DIR=/opt/obsidian-headless
OBH_STAMP="$OBH_DIR/.provision-stamp"
OBH_BUILD_USER=obhbuild
OB_LINK=/usr/bin/ob

# Expected primary-key fingerprints of the APT repository signing keys. The
# downloaded key is refused unless it is exactly this one key.
# - Caddy (Cloudsmith caddy/stable): confirmed against Cloudsmith's repository
#   metadata API (https://api.cloudsmith.io/v1/repos/caddy/stable/gpg/) and
#   the key served at dl.cloudsmith.io, 2026-11-08.
CADDY_KEY_FPR=65760C51EDEA2017CEA2CA15155B6D79CA56EA34
# - NodeSource ("NSolid <nsolid-gpg@nodesource.com>", created 2016-05-23):
#   TODO(security): NodeSource does not publish this fingerprint in its docs;
#   the value is the key served at deb.nodesource.com and matches the copy on
#   keyserver.ubuntu.com (2026-11-08). Re-confirm against an official
#   NodeSource publication if one appears, and update it if they rotate keys.
NODESOURCE_KEY_FPR=6F71F525282841EEDAF851B42F59B5F99B1BE0B4

# SHA-256 of the official uv release tarballs
# (https://github.com/astral-sh/uv/releases/download/<version>/uv-<arch>-unknown-linux-gnu.tar.gz).
# 0.11.7: the GitHub release asset `digest`, the release's .sha256 files and a
# local sha256sum of the downloaded tarballs all agree (2026-11-09).
# To bump uv, add the new version's lines here (see deploy/README.md).
uv_pinned_sha256() {
	case "$1/$2" in
		0.11.7/aarch64) echo f2ee1cde9aabb4c6e43bd3f341dadaf42189a54e001e521346dc31547310e284 ;;
		0.11.7/x86_64) echo 6681d691eb7f9c00ac6a3af54252f7ab29ae72f0c8f95bdc7f9d1401c23ea868 ;;
		*) return 0 ;;
	esac
}

export DEBIAN_FRONTEND=noninteractive
# Wait for the dpkg/apt lock (cloud-init and unattended-upgrades hold it on first boot).
APT_OPTS=(-y -o DPkg::Lock::Timeout=900 -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold)

# uv: root-owned shared interpreter location (readable by `obsidian` with
# ProtectHome=true), never fall back to a system Python.
export UV_PYTHON_INSTALL_DIR="$PY_DIR"
export UV_PYTHON_PREFERENCE=only-managed
export UV_CACHE_DIR=/var/cache/obsidian-mcp/uv

log()  { printf '\n\033[1;34m==> %s\033[0m\n' "$*"; }
info() { printf '    %s\n' "$*"; }
warn() { printf '\033[1;33mWARN: %s\033[0m\n' "$*" >&2; }
die()  { printf '\033[1;31mERROR: %s\033[0m\n' "$*" >&2; exit 1; }

usage() { sed -n '2,29p' "$0" | sed 's/^# \{0,1\}//'; }

parse_args() {
	while (($#)); do
		case "$1" in
			--domain) DOMAIN="${2:?}"; shift 2 ;;
			--repo-url) REPO_URL="${2:?}"; shift 2 ;;
			--commit) COMMIT="${2:?}"; shift 2 ;;
			--ob-version) OB_VERSION="${2:?}"; shift 2 ;;
			--uv-version) UV_VERSION="${2:?}"; shift 2 ;;
			--uv-sha256) UV_SHA256="${2:?}"; shift 2 ;;
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
	[[ -z $OB_VERSION || $OB_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "OB_VERSION must be an exact x.y.z version"
	[[ $UV_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "UV_VERSION must be an exact x.y.z version"
	[[ -z $UV_SHA256 || $UV_SHA256 =~ ^[0-9a-f]{64}$ ]] || die "UV_SHA256 must be 64 lowercase hex characters"
	[[ $NODE_MAJOR =~ ^[0-9]+$ ]] || die "NODE_MAJOR must be a number"
	[[ $CADDY_VERSION == latest || $CADDY_VERSION =~ ^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.~+]+)?$ ]] \
		|| die "CADDY_VERSION must be an exact apt version (e.g. 2.11.9) or 'latest'"
	[[ -z $ACME_EMAIL || $ACME_EMAIL =~ ^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+$ ]] || die "ACME_EMAIL looks invalid"
	local f
	for f in obsidian-mcp.service Caddyfile.template env.example sysctl-obsidian.conf \
		sshd-hardening.conf logrotate-obsidian-headless fail2ban-sshd.local \
		fail2ban-caddy-oauth.conf fail2ban-caddy-oauth.local oauth-store-usage.sh \
		obsidian-mcp-oauth-usage.service obsidian-mcp-oauth-usage.timer \
		ob-headless/package.json ob-headless/package-lock.json; do
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
	apt-get -o DPkg::Lock::Timeout=900 update
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
	# Capture first: `sshd -T | grep -q` would trip pipefail via SIGPIPE.
	local effective
	effective=$(sshd -T)
	grep -qx 'passwordauthentication no' <<<"$effective" \
		|| die "sshd -T still reports password authentication enabled"
	grep -qx 'permitrootlogin no' <<<"$effective" || warn "sshd -T does not report PermitRootLogin no"
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

# Primary-key fingerprints in key file $1 (armored or binary), one per line.
key_fingerprints() {
	local gnupg_home
	gnupg_home=$(mktemp -d)
	GNUPGHOME=$gnupg_home gpg --batch --show-keys --with-colons "$1" 2>/dev/null \
		| awk -F: '$1 == "pub" { want = 1; next } want && $1 == "fpr" { print $10; want = 0 }'
	rm -rf "$gnupg_home"
}

# Die unless key file $1 holds exactly one primary key with fingerprint $2.
expect_key() {
	local file=$1 expected=$2 name=$3 got
	got=$(key_fingerprints "$file")
	[[ $got == "$expected" ]] \
		|| die "$name signing key fingerprint mismatch: got '${got//$'\n'/ }', expected $expected (refusing to trust $file)"
	info "$name signing key $expected verified"
}

# Download the armored key at $1, verify it is $2, install it dearmored at $3.
install_repo_key() {
	local url=$1 expected=$2 dest=$3 name=$4 tmp
	if [[ -s $dest ]]; then
		expect_key "$dest" "$expected" "$name"
		return
	fi
	tmp=$(mktemp)
	curl -fsSL "$url" -o "$tmp"
	expect_key "$tmp" "$expected" "$name"
	gpg --dearmor --yes -o "$dest" "$tmp"
	rm -f "$tmp"
	chmod 0644 "$dest"
}

step_repos() {
	log "APT repositories (NodeSource ${NODE_MAJOR}.x, Caddy stable)"
	local arch
	arch=$(dpkg --print-architecture)
	install -d -m 0755 /etc/apt/keyrings
	install_repo_key https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
		"$NODESOURCE_KEY_FPR" /etc/apt/keyrings/nodesource.gpg NodeSource
	echo "deb [arch=${arch} signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_${NODE_MAJOR}.x nodistro main" \
		>/etc/apt/sources.list.d/nodesource.list
	# Prefer NodeSource's nodejs over Ubuntu's (18.x in noble).
	printf 'Package: nodejs\nPin: origin deb.nodesource.com\nPin-Priority: 600\n' \
		>/etc/apt/preferences.d/nodesource

	install_repo_key https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
		"$CADDY_KEY_FPR" /usr/share/keyrings/caddy-stable-archive-keyring.gpg Caddy
	# Written locally (same content as Cloudsmith's debian.deb.txt) instead of
	# downloading a sources file that could point signed-by elsewhere.
	echo "deb [signed-by=/usr/share/keyrings/caddy-stable-archive-keyring.gpg] https://dl.cloudsmith.io/public/caddy/stable/deb/debian any-version main" \
		>/etc/apt/sources.list.d/caddy-stable.list
	chmod 0644 /etc/apt/sources.list.d/caddy-stable.list
	apt-get -o DPkg::Lock::Timeout=900 update
}

# Version of obsidian-headless pinned in the committed lockfile.
obh_lock_version() {
	node -p 'require(process.argv[1]).packages["node_modules/obsidian-headless"].version' \
		"$OBH_SRC_DIR/package-lock.json"
}

# Content of the install stamp: lockfile digest + Node ABI (native module).
obh_stamp() {
	local lock_sha
	lock_sha=$(sha256sum "$OBH_SRC_DIR/package-lock.json")
	printf 'lock-sha256=%s node-abi=%s\n' "${lock_sha%% *}" "$(node -p process.versions.modules)"
}

# Build obsidian-headless from the committed lock into $OBH_DIR.
#
# `npm ci` verifies every tarball against its sha512 `integrity` in
# package-lock.json and refuses a lock that disagrees with package.json. It runs
# as the unprivileged system user $OBH_BUILD_USER (no-new-privs, empty
# environment) in a throwaway directory, because better-sqlite3 needs its
# install script: it is compiled from its (integrity-checked) sources with
# node-gyp (npm_config_build_from_source) instead of downloading an unverified
# prebuilt binary. The result is then copied to a root-owned, read-only prefix,
# so neither the build user nor the service user can modify `ob`.
obh_build() {
	local version=$1 build nodedir_env=()
	getent group "$OBH_BUILD_USER" >/dev/null || groupadd --system "$OBH_BUILD_USER"
	if ! id -u "$OBH_BUILD_USER" >/dev/null 2>&1; then
		useradd --system --gid "$OBH_BUILD_USER" --home-dir /nonexistent --no-create-home \
			--shell /usr/sbin/nologin --comment "obsidian-headless npm build (unprivileged)" "$OBH_BUILD_USER"
	fi
	# Headers shipped with the nodejs package avoid node-gyp downloading them.
	if [[ -f /usr/include/node/node_api.h ]]; then
		nodedir_env=(npm_config_nodedir=/usr)
	fi

	build=$(mktemp -d /var/tmp/obh-build.XXXXXXXX)
	install -m 0644 "$OBH_SRC_DIR/package.json" "$OBH_SRC_DIR/package-lock.json" "$build/"
	chown -R "$OBH_BUILD_USER:$OBH_BUILD_USER" "$build"
	info "npm ci as $OBH_BUILD_USER in $build (compiling better-sqlite3, takes a few minutes)"
	(
		cd "$build"
		runuser -u "$OBH_BUILD_USER" -- setpriv --no-new-privs \
			env -i HOME="$build" PATH=/usr/bin:/bin LANG=C.UTF-8 \
			npm_config_cache="$build/.npm" npm_config_build_from_source=true \
			npm_config_update_notifier=false npm_config_fund=false npm_config_audit=false \
			"${nodedir_env[@]}" \
			npm ci --omit=dev --no-fund --no-audit
	) || die "npm ci failed (build dir kept for inspection: $build)"

	# Smoke test as the build user, before anything is installed.
	local got
	got=$(cd "$build" && runuser -u "$OBH_BUILD_USER" -- env -i PATH=/usr/bin:/bin \
		node -e 'const D = require(require.resolve("better-sqlite3", { paths: [process.argv[1]] })); new D(":memory:").prepare("select 1").get();' \
		"$build/node_modules/obsidian-headless" \
		&& runuser -u "$OBH_BUILD_USER" -- env -i PATH=/usr/bin:/bin HOME="$build" "$build/node_modules/.bin/ob" --version) \
		|| die "freshly built obsidian-headless does not run (build dir kept: $build)"
	[[ $got == "$version" ]] || die "built ob reports version '$got', expected $version"

	# Root-owned, read-only copy (cp -R keeps the .bin symlinks as symlinks).
	rm -rf "$OBH_DIR.new" "$OBH_DIR.old"
	install -d -o root -g root -m 0755 "$OBH_DIR.new"
	cp -R "$build/package.json" "$build/package-lock.json" "$build/node_modules" "$OBH_DIR.new/"
	chown -R root:root "$OBH_DIR.new"
	chmod -R u=rwX,go=rX "$OBH_DIR.new"
	rm -rf "$build"
	if [[ -e $OBH_DIR ]]; then
		mv "$OBH_DIR" "$OBH_DIR.old"
	fi
	mv "$OBH_DIR.new" "$OBH_DIR"
	rm -rf "$OBH_DIR.old"
}

step_node_ob() {
	log "Node.js ${NODE_MAJOR} + obsidian-headless (npm ci from ob-headless/package-lock.json)"
	apt_install nodejs
	local major
	major=$(node -p 'process.versions.node.split(".")[0]')
	[[ $major == "$NODE_MAJOR" ]] || die "node major is $major, expected $NODE_MAJOR (check apt pinning)"

	local version
	version=$(obh_lock_version)
	[[ $version =~ ^[0-9]+\.[0-9]+\.[0-9]+$ ]] || die "cannot read the obsidian-headless version from $OBH_SRC_DIR/package-lock.json"
	if [[ -n $OB_VERSION && $OB_VERSION != "$version" ]]; then
		die "OB_VERSION=$OB_VERSION but ob-headless/package-lock.json pins $version (bump it in package.json and regenerate the lock, see deploy/README.md)"
	fi

	local stamp
	stamp=$(obh_stamp)
	if [[ -f $OBH_STAMP && $(<"$OBH_STAMP") == "$stamp" && -f $OBH_DIR/node_modules/obsidian-headless/cli.js ]]; then
		info "obsidian-headless@${version} already installed from this lock ($OBH_DIR)"
	else
		obh_build "$version"
		printf '%s\n' "$stamp" >"$OBH_STAMP"
		chmod 0644 "$OBH_STAMP"
		info "obsidian-headless@${version} installed in $OBH_DIR"
	fi

	# Drop the old global install (npm install -g) from earlier provisions.
	local npm_root
	npm_root=$(npm root -g)
	if [[ -d $npm_root/obsidian-headless ]]; then
		info "removing the global npm install of obsidian-headless ($npm_root)"
		npm uninstall -g --no-fund --no-audit obsidian-headless
	fi

	# OB_BIN stays /usr/bin/ob.
	ln -sfn "$OBH_DIR/node_modules/.bin/ob" "$OB_LINK"
	[[ $(readlink -f "$OB_LINK") == "$OBH_DIR/node_modules/obsidian-headless/cli.js" ]] \
		|| die "$OB_LINK does not resolve into $OBH_DIR"
	local got
	got=$(cd / && runuser -u "$SVC_USER" -- "$OB_LINK" --version) || die "$SVC_USER cannot run $OB_LINK"
	[[ $got == "$version" ]] || die "$OB_LINK reports version '$got', expected $version"
	info "$OB_LINK -> $OBH_DIR/node_modules/.bin/ob (obsidian-headless $got, runs as $SVC_USER)"
}

# Install uv/uvx ${UV_VERSION} into /usr/local/bin from the official GitHub
# release tarball, refusing it unless its sha256 is the pinned (or explicitly
# given UV_SHA256) value. No installer script is executed.
install_uv() {
	local arch pinned expected triple url tmp got
	arch=$(uname -m)
	case "$arch" in
		aarch64 | x86_64) ;;
		*) die "no uv release tarball handled for architecture $arch" ;;
	esac
	pinned=$(uv_pinned_sha256 "$UV_VERSION" "$arch")
	if [[ -n $pinned && -n $UV_SHA256 && $UV_SHA256 != "$pinned" ]]; then
		die "UV_SHA256 ($UV_SHA256) contradicts the hash pinned in provision.sh for uv $UV_VERSION/$arch ($pinned)"
	fi
	expected=${pinned:-$UV_SHA256}
	[[ -n $expected ]] \
		|| die "no sha256 pinned for uv $UV_VERSION/$arch: pass UV_SHA256=<hex> (or --uv-sha256) from the release's .sha256 file, or pin it in provision.sh"
	triple="${arch}-unknown-linux-gnu"
	url="https://github.com/astral-sh/uv/releases/download/${UV_VERSION}/uv-${triple}.tar.gz"
	tmp=$(mktemp -d)
	info "downloading $url"
	curl -fsSL --proto '=https' --tlsv1.2 "$url" -o "$tmp/uv.tar.gz"
	got=$(sha256sum "$tmp/uv.tar.gz")
	got=${got%% *}
	if [[ $got != "$expected" ]]; then
		rm -rf "$tmp"
		die "uv tarball sha256 mismatch: got $got, expected $expected (refusing to install)"
	fi
	info "uv tarball sha256 $got verified"
	tar -xzf "$tmp/uv.tar.gz" -C "$tmp" --no-same-owner --no-same-permissions
	local bin
	for bin in uv uvx; do
		[[ -f $tmp/uv-$triple/$bin ]] || die "uv tarball lacks uv-$triple/$bin"
		install -o root -g root -m 0755 "$tmp/uv-$triple/$bin" "/usr/local/bin/$bin"
	done
	rm -rf "$tmp"
}

step_uv_python() {
	log "uv ${UV_VERSION} + CPython ${PYTHON_VERSION} (in ${PY_DIR})"
	local have=""
	[[ -x /usr/local/bin/uv ]] && have=$(/usr/local/bin/uv --version | awk '{print $2}')
	if [[ $have != "$UV_VERSION" ]]; then
		install_uv
	else
		info "uv ${UV_VERSION} already installed"
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
		info "created from env.example (fill in the FILL_ME_IN values)"
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
	if [[ $CADDY_VERSION == latest ]]; then
		warn "CADDY_VERSION=latest: installing the newest caddy in the repo (unpinned)"
		apt_install caddy
	else
		# --allow-downgrades: re-provisioning with an older pin must win.
		apt_install --allow-downgrades "caddy=${CADDY_VERSION}"
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
	if grep -q 'FILL_ME_IN' "$ENV_FILE"; then
		warn "$ENV_FILE still contains FILL_ME_IN placeholders: service NOT started"
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
		    obsidian-headless: $(node -p 'require(process.argv[1]).version' "$OBH_DIR/node_modules/obsidian-headless/package.json" 2>/dev/null || echo '?')  (${OBH_DIR}, $OB_LINK)
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
