#!/usr/bin/env bash
# oauth-store-usage.sh — warn (journal, priority warning) when the FastMCP
# OAuth store grows beyond a limit. Dynamic Client Registration (/register)
# is unauthenticated and writes one record per client, so sustained spam
# shows up here first. Nothing is ever deleted automatically.
#
# Installed to /usr/local/sbin/obsidian-mcp-oauth-usage by provision.sh and
# run weekly by obsidian-mcp-oauth-usage.timer.
#
#   obsidian-mcp-oauth-usage [DIR] [LIMIT_MB]   (defaults: FASTMCP_HOME, 200)

set -euo pipefail

dir="${1:-/var/lib/obsidian/mcp-data}"
limit_mb="${2:-200}"

[[ $limit_mb =~ ^[0-9]+$ ]] || { echo "<3>LIMIT_MB must be a number" >&2; exit 2; }
[[ -d $dir ]] || { echo "<4>$dir does not exist; nothing to check"; exit 0; }

size_kb=$(du -sk -- "$dir" | awk '{print $1}')
files=$(find "$dir" -xdev -type f | wc -l)
size_mb=$((size_kb / 1024))

if ((size_kb > limit_mb * 1024)); then
	# The <4> prefix sets the journal priority to "warning".
	echo "<4>WARNING: $dir uses ${size_mb} MB in ${files} files (limit ${limit_mb} MB)." \
		"Possible OAuth /register spam: check fail2ban (fail2ban-client status" \
		"obsidian-oauth-register) and the Caddy access log. Nothing was deleted."
else
	echo "$dir uses ${size_mb} MB in ${files} files (limit ${limit_mb} MB): OK"
fi
