"""deploy/fail2ban-caddy-oauth.*: the filter matches Caddy's JSON access log.

fail2ban itself is not a test dependency, so this approximates what
fail2ban-regex does: read the filter's failregex, substitute the jail's
filter arguments and ``<HOST>``, and run it over sample Caddy log lines.
"""

from __future__ import annotations

import configparser
import json
import re
from pathlib import Path

import pytest

DEPLOY = Path(__file__).parents[1] / "deploy"
FILTER = DEPLOY / "fail2ban-caddy-oauth.conf"
JAIL = DEPLOY / "fail2ban-caddy-oauth.local"
CADDYFILE = DEPLOY / "Caddyfile.template"
ACCESS_LOG = "/var/log/caddy/obsidian-mcp-access.log"


def _parser(path: Path) -> configparser.RawConfigParser:
    parser = configparser.RawConfigParser(inline_comment_prefixes=None)
    parser.read_string(path.read_text())
    return parser


def _jail_args(jail: str) -> dict[str, str]:
    spec = _parser(JAIL)[jail]["filter"]
    match = re.fullmatch(r"obsidian-caddy-oauth\[(.*)\]", spec)
    assert match, spec
    return dict(part.strip().split("=", 1) for part in match.group(1).split(","))


def _failregex(jail: str) -> re.Pattern[str]:
    definition = _parser(FILTER)["Definition"]
    regex = definition["failregex"]
    for name, value in _jail_args(jail).items():
        assert name in definition, f"{name} is not a filter option"
        regex = regex.replace(f"<{name}>", re.escape(value))
    # fail2ban's <HOST> is an address/hostname group.
    regex = regex.replace("<HOST>", r"(?P<host>[0-9A-Fa-f:.]+)")
    assert "<" not in regex.replace("(?P<host>", ""), regex
    return re.compile(regex)


def _caddy_line(method: str, uri: str, ip: str = "203.0.113.7", **headers: str) -> str:
    """One access-log line in Caddy 2.x's JSON format."""
    return json.dumps(
        {
            "level": "info",
            "ts": 1762600000.123456,
            "logger": "http.log.access.log0",
            "msg": "handled request",
            "request": {
                "remote_ip": ip,
                "remote_port": "51234",
                "client_ip": ip,
                "proto": "HTTP/2.0",
                "method": method,
                "host": "obsidian.mcp.example.com",
                "uri": uri,
                "headers": {"User-Agent": ["curl/8.5.0"], **{k: [v] for k, v in headers.items()}},
                "tls": {"resumed": False, "version": 772, "server_name": "obsidian.mcp.example.com"},
            },
            "bytes_read": 120,
            "user_id": "",
            "duration": 0.0123,
            "size": 312,
            "status": 201,
            "resp_headers": {"Content-Type": ["application/json"]},
        },
        separators=(",", ":"),
    )


@pytest.mark.parametrize(
    ("jail", "method", "uri"),
    [
        ("obsidian-oauth-register", "POST", "/register"),
        ("obsidian-oauth-authorize", "GET", "/authorize"),
        (
            "obsidian-oauth-authorize",
            "GET",
            "/authorize?response_type=code&client_id=abc&redirect_uri=https%3A%2F%2Fclaude.ai",
        ),
    ],
)
def test_filter_matches_oauth_requests(jail, method, uri):
    match = _failregex(jail).search(_caddy_line(method, uri, ip="198.51.100.23"))
    assert match and match.group("host") == "198.51.100.23"


def test_filter_matches_ipv6_clients():
    match = _failregex("obsidian-oauth-register").search(
        _caddy_line("POST", "/register", ip="2001:db8::1")
    )
    assert match and match.group("host") == "2001:db8::1"


@pytest.mark.parametrize(
    ("jail", "line"),
    [
        ("obsidian-oauth-register", _caddy_line("GET", "/register")),
        ("obsidian-oauth-register", _caddy_line("POST", "/registerX")),
        ("obsidian-oauth-register", _caddy_line("POST", "/mcp")),
        ("obsidian-oauth-register", _caddy_line("POST", "/token")),
        ("obsidian-oauth-authorize", _caddy_line("POST", "/authorize")),
        ("obsidian-oauth-authorize", _caddy_line("GET", "/.well-known/oauth-authorization-server")),
        # A header value cannot fake the method/uri of another request.
        (
            "obsidian-oauth-register",
            _caddy_line("GET", "/health", X_Fake='"method":"POST","uri":"/register"'),
        ),
    ],
)
def test_filter_ignores_other_requests(jail, line):
    assert _failregex(jail).search(line) is None


def test_jail_thresholds_and_ban():
    jails = _parser(JAIL)
    expected = {"obsidian-oauth-register": "21", "obsidian-oauth-authorize": "61"}
    for name, maxretry in expected.items():
        jail = jails[name]
        assert jail["enabled"] == "true"
        assert jail["maxretry"] == maxretry  # ban on the 21st / 61st request
        assert jail["findtime"] == "10m"
        assert jail["bantime"] == "1h"
        assert jail["banaction"] == "iptables-multiport"
        assert jail["port"] == "80,443"
        assert jail["logpath"] == ACCESS_LOG


def test_caddy_writes_the_json_log_the_jails_read():
    text = CADDYFILE.read_text()
    assert f"output file {ACCESS_LOG}" in text
    assert re.search(r"^\s*format json\s*$", text, re.MULTILINE)
    assert "roll_size" in text and "roll_keep" in text


def test_datepattern_targets_caddy_ts():
    assert _parser(FILTER)["Definition"]["datepattern"].strip() == '"ts":{EPOCH}'


def test_provision_installs_filter_jail_and_usage_timer():
    script = (DEPLOY / "provision.sh").read_text()
    assert "/etc/fail2ban/filter.d/obsidian-caddy-oauth.conf" in script
    assert "/etc/fail2ban/jail.d/obsidian-caddy-oauth.local" in script
    assert "obsidian-mcp-oauth-usage.timer" in script
    # fail2ban must run after Caddy is installed (log file + caddy user).
    main = script[script.index("main() {") :]
    assert main.index("step_caddy") < main.index("step_fail2ban")
