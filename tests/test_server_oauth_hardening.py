"""GitHubProvider hardening: scopes, redirect allowlist, CIMD, JWT lifetime."""
from __future__ import annotations

import pytest
from fastmcp.server.auth.redirect_validation import validate_redirect_uri

from obsidian_mcp import server
from obsidian_mcp.config import ConfigError, oauth_hardening_from_env

_HARDENING_VARS = (
    "OAUTH_GITHUB_SCOPES",
    "OAUTH_ENABLE_CIMD",
    "OAUTH_ALLOWED_CLIENT_REDIRECT_URIS",
    "OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS",
)


@pytest.fixture
def oauth_env(monkeypatch):
    monkeypatch.delenv("VAULTS_CONFIG", raising=False)
    monkeypatch.delenv("API_KEY", raising=False)
    monkeypatch.delenv("OBSIDIAN_MCP_API_KEY", raising=False)
    monkeypatch.setenv("OAUTH_GITHUB_CLIENT_ID", "dummy-id")
    monkeypatch.setenv("OAUTH_GITHUB_CLIENT_SECRET", "dummy-secret")
    monkeypatch.setenv("OAUTH_GITHUB_ALLOWED_LOGINS", "octocat")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.com")
    for name in _HARDENING_VARS:
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


def _captured_kwargs(monkeypatch) -> dict:
    captured: dict = {}
    real = server.GitHubProvider

    def fake(**kwargs):
        captured.update(kwargs)
        return real(**kwargs)

    monkeypatch.setattr(server, "GitHubProvider", fake)
    server._build_auth()
    return captured


def test_defaults_are_hardened(oauth_env):
    kwargs = _captured_kwargs(oauth_env)

    assert kwargs["required_scopes"] == ["read:user"]
    assert kwargs["enable_cimd"] is False
    assert kwargs["allowed_client_redirect_uris"] == [
        "https://claude.ai/api/mcp/auth_callback",
        "https://claude.com/api/mcp/auth_callback",
        "http://localhost:*",
        "http://127.0.0.1:*",
    ]
    assert kwargs["fastmcp_access_token_expiry_seconds"] == 30 * 24 * 3600
    assert kwargs["require_authorization_consent"] is True


def test_provider_receives_settings(oauth_env):
    """The real provider actually stores what we pass (guards against a
    fastmcp upgrade silently renaming/ignoring a kwarg)."""
    provider = server._build_auth()

    assert provider._token_validator._base.required_scopes == ["read:user"]
    assert provider._cimd_manager is None
    assert provider._allowed_client_redirect_uris[0] == "https://claude.ai/api/mcp/auth_callback"
    assert provider._fastmcp_access_token_expiry_seconds == 30 * 24 * 3600
    assert provider._require_authorization_consent is True


def test_env_overrides(oauth_env):
    oauth_env.setenv("OAUTH_GITHUB_SCOPES", "read:user, user:email")
    oauth_env.setenv("OAUTH_ENABLE_CIMD", "true")
    oauth_env.setenv("OAUTH_ALLOWED_CLIENT_REDIRECT_URIS", "https://a.example/cb, http://localhost:*")
    oauth_env.setenv("OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "3600")

    kwargs = _captured_kwargs(oauth_env)

    assert kwargs["required_scopes"] == ["read:user", "user:email"]
    assert kwargs["enable_cimd"] is True
    assert kwargs["allowed_client_redirect_uris"] == ["https://a.example/cb", "http://localhost:*"]
    assert kwargs["fastmcp_access_token_expiry_seconds"] == 3600


def test_blank_list_values_fall_back_to_defaults(oauth_env):
    oauth_env.setenv("OAUTH_GITHUB_SCOPES", " , ")
    oauth_env.setenv("OAUTH_ALLOWED_CLIENT_REDIRECT_URIS", "")

    hardening = oauth_hardening_from_env()

    assert hardening.github_scopes == ("read:user",)
    assert "https://claude.ai/api/mcp/auth_callback" in hardening.allowed_client_redirect_uris


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("OAUTH_ENABLE_CIMD", "maybe"),
        ("OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "0"),
        ("OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "-5"),
        ("OAUTH_ACCESS_TOKEN_EXPIRY_SECONDS", "soon"),
    ],
)
def test_invalid_values_raise(oauth_env, name, value):
    oauth_env.setenv(name, value)
    with pytest.raises(ConfigError, match=name):
        oauth_hardening_from_env()


def test_config_validates_hardening(oauth_env, tmp_path):
    import obsidian_mcp.config as cfg_mod

    oauth_env.setenv("VAULT_PATH", str(tmp_path))
    oauth_env.setenv("OAUTH_ENABLE_CIMD", "maybe")
    cfg_mod._config = None
    try:
        with pytest.raises(ConfigError, match="OAUTH_ENABLE_CIMD"):
            cfg_mod.get_config()
    finally:
        cfg_mod._config = None


@pytest.mark.parametrize(
    ("uri", "allowed"),
    [
        ("https://claude.ai/api/mcp/auth_callback", True),
        ("https://claude.com/api/mcp/auth_callback", True),
        ("http://localhost:6274/oauth/callback", True),
        ("http://127.0.0.1:33418/callback", True),
        ("http://localhost/callback", True),
        ("https://evil.example/api/mcp/auth_callback", False),
        ("https://claude.ai.evil.example/api/mcp/auth_callback", False),
        ("https://claude.ai/api/mcp/other", False),
        ("http://localhost@evil.example/cb", False),
        ("https://localhost:8443/cb", False),
    ],
)
def test_default_redirect_allowlist_matching(oauth_env, uri, allowed):
    """Wildcard ports match any loopback port via fastmcp's own matcher."""
    patterns = list(oauth_hardening_from_env().allowed_client_redirect_uris)
    assert validate_redirect_uri(uri, patterns) is allowed


# ── login allowlist verifier ─────────────────────────────────────────────────

class _CountingVerifier(server.TokenVerifier):
    def __init__(self, login: str) -> None:
        super().__init__()
        self.login = login
        self.calls = 0

    async def verify_token(self, token):
        self.calls += 1
        return server.AccessToken(
            token=token, client_id="1", scopes=["read:user"], claims={"login": self.login}
        )


def test_provider_validator_is_login_allowlist(oauth_env):
    provider = server._build_auth()
    assert isinstance(provider._token_validator, server._RestrictedGitHubVerifier)


@pytest.mark.asyncio
async def test_allowlist_is_enforced_on_every_verification():
    """The allowlist wrapper is what load_access_token() calls per request,
    so it must re-check the login every time, not just once."""
    base = _CountingVerifier("octocat")
    verifier = server._RestrictedGitHubVerifier(base, ["octocat"])

    assert await verifier.verify_token("t") is not None
    base.login = "mallory"
    assert await verifier.verify_token("t") is None
    assert base.calls == 2
