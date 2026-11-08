"""Redaction of ob output: e-mails, tokens, passwords, keys, env secrets."""

from __future__ import annotations

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
