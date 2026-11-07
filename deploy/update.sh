#!/usr/bin/env bash
# update.sh — deploy another commit of obsidian-mcp on the VM, or roll back.
#
#   sudo ./update.sh <40-hex commit sha>   # deploy that exact commit
#   sudo ./update.sh rollback              # redeploy /opt/obsidian-mcp/PREVIOUS_COMMIT
#
# Steps: fetch, `git checkout --detach`, `uv sync --locked --no-dev`, restart
# the service, wait for /health, show status. The commit that was live before
# is written to /opt/obsidian-mcp/PREVIOUS_COMMIT, so `rollback` toggles
# between the last two deployments. If checkout or uv sync fails, the previous
# commit and its venv are restored before exiting.
#
# Only the application changes; host config (unit, Caddyfile, packages) is
# handled by provision.sh.

set -euo pipefail
umask 022

OPT_DIR=/opt/obsidian-mcp
APP_DIR="${OPT_DIR}/app"
UNIT_NAME=obsidian-mcp.service
PYTHON_VERSION="${PYTHON_VERSION:-3.12}"
UV=/usr/local/bin/uv

export UV_PYTHON_INSTALL_DIR="${OPT_DIR}/python"
export UV_PYTHON_PREFERENCE=only-managed
export UV_CACHE_DIR=/var/cache/obsidian-mcp/uv

die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }
log() { printf '\n==> %s\n' "$*"; }

[[ ${EUID} -eq 0 ]] || die "must run as root (sudo)"
[[ $# -eq 1 ]] || die "usage: $0 <40-hex commit sha> | rollback"
[[ -d ${APP_DIR}/.git ]] || die "${APP_DIR} is not a checkout; run provision.sh first"

target=$1
if [[ ${target} == rollback ]]; then
	[[ -s ${OPT_DIR}/PREVIOUS_COMMIT ]] || die "no ${OPT_DIR}/PREVIOUS_COMMIT recorded"
	target=$(<"${OPT_DIR}/PREVIOUS_COMMIT")
	log "Rolling back to ${target}"
fi
[[ ${target} =~ ^[0-9a-f]{40}$ ]] || die "commit must be a full 40-character lowercase hex SHA (no branches/tags): '${target}'"

current=$(git -C "${APP_DIR}" rev-parse HEAD)
if [[ ${current} == "${target}" ]]; then
	log "Already at ${target}; restarting only"
fi

sync_venv() {
	(cd "${APP_DIR}" && "${UV}" sync --locked --no-dev --no-editable --compile-bytecode --python "${PYTHON_VERSION}")
}

restore() {
	printf 'ERROR: update failed, restoring %s\n' "${current}" >&2
	git -C "${APP_DIR}" checkout --force --detach "${current}" && sync_venv \
		|| printf 'ERROR: restore failed too; inspect %s manually\n' "${APP_DIR}" >&2
}

log "Fetching"
git -C "${APP_DIR}" fetch --prune --tags origin
if ! git -C "${APP_DIR}" cat-file -e "${target}^{commit}" 2>/dev/null; then
	git -C "${APP_DIR}" fetch origin "${target}"
fi

log "Checking out ${target} and syncing venv"
trap restore ERR
git -C "${APP_DIR}" checkout --force --detach "${target}"
sync_venv
trap - ERR

if [[ ${current} != "${target}" ]]; then
	echo "${current}" >"${OPT_DIR}/PREVIOUS_COMMIT"
fi
echo "${target}" >"${OPT_DIR}/DEPLOYED_COMMIT"

log "Restarting ${UNIT_NAME}"
systemctl restart "${UNIT_NAME}"
healthy=0
for _ in $(seq 1 60); do
	if curl -fsS -o /dev/null http://127.0.0.1:8000/health; then
		healthy=1
		break
	fi
	sleep 1
done

systemctl --no-pager --lines=20 status "${UNIT_NAME}" || true
printf '\ndeployed : %s (%s)\nprevious : %s\n' \
	"${target}" "$(git -C "${APP_DIR}" log -1 --format='%cs %s')" "$(cat "${OPT_DIR}/PREVIOUS_COMMIT" 2>/dev/null || echo '-')"
if ((healthy)); then
	echo "health   : OK"
else
	echo "health   : NOT OK after 60s — check: journalctl -u ${UNIT_NAME} -n 200; rollback: sudo $0 rollback" >&2
	exit 1
fi
