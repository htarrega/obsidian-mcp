"""Redaction of ob output: e-mails, tokens, passwords, keys, env secrets."""

from __future__ import annotations

import pytest

from obsidian_mcp.ob import redact as redact_mod
from obsidian_mcp.ob.redact import EMAIL_MASK, MASK, redact, redact_obj


def test_masks_emails():
    out = redact("Logged in as alice.smith+obs@example.co.uk now")
    assert "alice" not in out and "example" not in out
    assert EMAIL_MASK in out


def test_masks_bearer_tokens():
    out = redact("Authorization: Bearer abc.def-ghi")
    assert "abc.def-ghi" not in out
    assert f"Bearer {MASK}" in out


def test_masks_password_and_token_assignments():
    out = redact('password=hunter2 token: shorty "encryptionKey": "k3y" api_key=xyz')
    for secret in ("hunter2", "shorty", "k3y", "xyz"):
        assert secret not in out


def test_masks_long_hex_and_mixed_tokens():
    hex_token = "a" * 8 + "0123456789abcdef0123"
    mixed = "AbCdEfGhIjKlMnOpQrStUv0123"
    out = redact(f"tok {hex_token} and {mixed}")
    assert hex_token not in out and mixed not in out


def test_keeps_ordinary_log_lines_and_paths():
    line = "Uploading file daily/2024-01-15-meeting-notes-with-team.md"
    assert redact(line) == line
    assert redact("Fully synced") == "Fully synced"


def test_masks_value_of_obsidian_auth_token_env(monkeypatch):
    monkeypatch.setenv("OBSIDIAN_AUTH_TOKEN", "tok-1234")
    assert "tok-1234" not in redact("using tok-1234 for auth")


def test_redact_obj_drops_secret_keys_and_keeps_vault_ids():
    data = {
        "vaultId": "0123456789abcdef0123456789abcdef",
        "encryptionKey": "anything",
        "token": "t",
        "nested": [{"id": "fedcba9876543210fedcba9876543210", "email": "a@b.io"}],
        "vaultPath": "/srv/Vaults/MyVault2024Notes/Sub",
        "note": "contact bob@example.com",
    }
    out = redact_obj(data)
    assert out["vaultId"] == data["vaultId"]
    assert out["encryptionKey"] == MASK
    assert out["token"] == MASK
    assert out["nested"][0]["id"] == data["nested"][0]["id"]
    assert out["nested"][0]["email"] == MASK
    assert out["vaultPath"] == data["vaultPath"]
    assert "bob@" not in out["note"]


@pytest.fixture(autouse=True)
def _no_registered_secrets():
    redact_mod.clear_registered_secrets()
    yield
    redact_mod.clear_registered_secrets()


@pytest.mark.parametrize(
    "text, secret",
    [
        ('password="hunter 2 x" ok', "hunter 2 x"),
        ("password='with spaces too' ok", "with spaces too"),
        ('token: "a\\"b c" tail', 'b c'),
        ("password=correct horse battery staple", "horse battery staple"),
        ("passphrase: my long phrase", "long phrase"),
        ("secret = s3 cr3t words", "cr3t words"),
        ("OAUTH_GITHUB_CLIENT_SECRET=gh secret value", "gh secret value"),
        ("key=abc123def", "abc123def"),
        ('{"key": "k e y"}', "k e y"),
        ("Cookie: sid=abc; theme=dark", "sid=abc"),
        ("Set-Cookie: sid=abc; HttpOnly", "sid=abc"),
        ("session: s3ss10n", "s3ss10n"),
        ("session_id=abcd&next=1", "abcd"),
    ],
)
def test_masks_extended_key_value_forms(text, secret):
    out = redact(text)
    assert secret not in out
    assert MASK in out


@pytest.mark.parametrize(
    "line",
    [
        "Uploading file notes/key=value.md",
        "Downloading daily/session-notes.md",
        "hotkey: ctrl+s",
        "monkey=banana",
        "Uploading file keys/session.md",
        "configs: app, hotkey, core-plugin",
    ],
)
def test_extended_patterns_keep_ordinary_lines(line):
    assert redact(line) == line


def test_quoted_value_keeps_rest_of_line():
    assert redact('password="x y" next=1') == f'password="{MASK}" next=1'


def test_redact_obj_drops_keys_containing_secret_fragments():
    data = {
        "vaultId": "0123456789abcdef0123456789abcdef",
        "id": "fedcba9876543210fedcba9876543210",
        "keyHash": "h",
        "userToken": "t",
        "clientSecret": "s",
        "Password2": "p",
        "keySalt": "x",
        "sessionCookie": "c",
        "vaultName": "Vault",
    }
    out = redact_obj(data)
    for name in ("keyHash", "userToken", "clientSecret", "Password2", "keySalt", "sessionCookie"):
        assert out[name] == MASK, name
    assert out["vaultId"] == data["vaultId"]
    assert out["id"] == data["id"]
    assert out["vaultName"] == "Vault"


def test_registered_secret_file_is_masked(tmp_path):
    token_file = tmp_path / "auth_token"
    token_file.write_text("plain-tok-9\n")
    assert redact_mod.register_secret_file(token_file) is True
    assert redact("using plain-tok-9 now") == f"using {MASK} now"
    assert redact_obj({"note": "x plain-tok-9"}) == {"note": f"x {MASK}"}


def test_missing_or_tiny_secret_file_registers_nothing(tmp_path):
    assert redact_mod.register_secret_file(tmp_path / "missing") is False
    (tmp_path / "tiny").write_text("ab")
    assert redact_mod.register_secret_file(tmp_path / "tiny") is False
    assert redact("ab abc") == "ab abc"
