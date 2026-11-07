"""trash_info()/restore() only accept a bare trash entry name."""
from __future__ import annotations

import pytest

from obsidian_mcp.storage.filesystem import VaultStorage
from obsidian_mcp.storage.policy import VaultAccessPolicy, VaultPathError

BAD_NAMES = [
    "",
    ".",
    "..",
    "../note.md",
    "sub/note.md",
    "/etc/passwd",
    "a\\b.md",
    "..\\note.md",
    "note\0.md",
]


def _storage(root):
    (root / ".trash").mkdir(exist_ok=True)
    return VaultStorage(VaultAccessPolicy(root))


@pytest.mark.parametrize("name", BAD_NAMES)
def test_trash_info_rejects_non_bare_names(tmp_path, name):
    storage = _storage(tmp_path)
    with pytest.raises(VaultPathError, match="bare filename"):
        storage.trash_info(name)


@pytest.mark.parametrize("name", BAD_NAMES)
def test_restore_rejects_non_bare_names(tmp_path, name):
    storage = _storage(tmp_path)
    with pytest.raises(VaultPathError, match="bare filename"):
        storage.restore(name, "restored.md")
    assert not (tmp_path / "restored.md").exists()


def test_backslash_name_rejected_even_if_present_in_trash(tmp_path):
    """On POSIX a backslash is a legal filename character; still refuse it so
    behaviour doesn't depend on the platform's separator."""
    storage = _storage(tmp_path)
    (tmp_path / ".trash" / "a\\b.md").write_text("x")
    with pytest.raises(VaultPathError):
        storage.trash_info("a\\b.md")


def test_bare_name_still_works(tmp_path):
    storage = _storage(tmp_path)
    (tmp_path / ".trash" / "note.md").write_text("hello")

    info = storage.trash_info("note.md")
    assert info.name == "note.md"
    assert info.is_dir is False

    storage.restore("note.md", "note.md")
    assert (tmp_path / "note.md").read_text() == "hello"
