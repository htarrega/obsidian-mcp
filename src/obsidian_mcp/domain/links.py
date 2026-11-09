"""Wikilink resolution with Obsidian semantics.

Obsidian resolves ``[[target]]`` case-insensitively against every file in the
vault, not just notes next to the linking one:

- a target with a non-Markdown extension (``[[diagram.png]]``) names a file;
  a target without one names a note (``[[Foo]]`` → ``Foo.md``, and
  ``[[Foo.excalidraw]]`` → ``Foo.excalidraw.md``);
- a bare name matches that file name anywhere in the vault, a partial path
  (``[[images/diagram.png]]``) matches any file whose path ends with it, and
  ``../`` / ``./`` paths are taken relative to the linking note;
- when several files match, the exact vault path wins, then a file in the
  linking note's folder, then the shortest path ("shortest unique path").
"""
from __future__ import annotations

import difflib
import posixpath
import unicodedata
from collections import defaultdict
from collections.abc import Callable, Iterable, Mapping

# Extensions Obsidian treats as attachments (non-note files).
ATTACHMENT_EXTENSIONS = frozenset({
    # images
    "png", "jpg", "jpeg", "gif", "bmp", "svg", "webp", "avif", "ico", "tif", "tiff", "heic",
    # audio
    "mp3", "wav", "m4a", "ogg", "flac", "3gp", "webm", "aac",
    # video
    "mp4", "mov", "mkv", "ogv", "avi",
    # documents and other files commonly embedded
    "pdf", "canvas", "base", "csv", "txt", "json", "zip", "docx", "xlsx", "pptx",
    "doc", "xls", "ppt", "odt", "ods", "epub", "html", "xml", "drawio", "excalidraw",
})

# Name similarity (0..1) required before an existing file is suggested.
SUGGESTION_CUTOFF = 0.75


def fold(value: str) -> str:
    """Case-insensitive comparison key (Unicode NFC + casefold)."""
    return unicodedata.normalize("NFC", value).casefold()


def normalize_name(value: str) -> str:
    """Accent-, case- and punctuation-insensitive key for fuzzy matching."""
    decomposed = unicodedata.normalize("NFKD", value)
    stripped = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return "".join(ch for ch in stripped.casefold() if ch.isalnum())


def link_extension(target: str) -> str:
    """Lower-case extension of the last path segment ('' if none)."""
    name = target.rstrip("/").rsplit("/", 1)[-1]
    if "." not in name.strip("."):
        return ""
    return name.rsplit(".", 1)[-1].lower()


def link_kind(target: str) -> str:
    """'attachment' for a known non-note file extension, else 'note'.
    ``.excalidraw`` drawings are Markdown files, so they count as notes."""
    ext = link_extension(target)
    return "attachment" if ext in ATTACHMENT_EXTENSIONS and ext != "excalidraw" else "note"


def _stem(path: str) -> str:
    name = path.rsplit("/", 1)[-1]
    return name[:-3] if name.lower().endswith(".md") else name


class LinkResolver:
    """Resolve link targets against a snapshot of vault file paths.

    ``files`` are vault-relative POSIX paths of every linkable file (notes and
    attachments). ``aliases`` maps a frontmatter alias to its note path.
    """

    def __init__(self, files: Iterable[str], aliases: Mapping[str, str] | None = None) -> None:
        self._files = sorted(set(files))
        self._by_path: dict[str, str] = {}
        self._by_name: dict[str, list[str]] = defaultdict(list)
        for path in self._files:
            self._by_path[fold(path)] = path
            self._by_name[fold(path.rsplit("/", 1)[-1])].append(path)
        self._aliases = {fold(alias): path for alias, path in (aliases or {}).items()}

    @property
    def files(self) -> list[str]:
        return list(self._files)

    def candidates(self, target: str, source: str | None = None) -> list[str]:
        """Every file the target could refer to (unordered preference)."""
        t = target.strip().replace("\\", "/")
        if source and (t.startswith("./") or t.startswith("../")):
            t = posixpath.normpath(posixpath.join(posixpath.dirname(source), t))
        t = t.lstrip("/")
        while t.startswith("./"):
            t = t[2:]
        if not t:
            return []
        variants = [t]
        if not t.lower().endswith(".md"):
            variants.append(t + ".md")
        found: list[str] = []
        for variant in variants:
            key = fold(variant)
            for path in self._by_name.get(key.rsplit("/", 1)[-1], ()):
                path_key = fold(path)
                if (path_key == key or path_key.endswith("/" + key)) and path not in found:
                    found.append(path)
        if not found and "/" not in t:
            alias_hit = self._aliases.get(fold(t))
            if alias_hit:
                found.append(alias_hit)
        return found

    def resolve(self, target: str, source: str | None = None) -> str | None:
        """The single file Obsidian would open for this link, or None."""
        found = self.candidates(target, source)
        if not found:
            return None
        return min(found, key=lambda path: self._preference(path, target, source))

    def _preference(self, path: str, target: str, source: str | None) -> tuple:
        key = fold(target.strip().lstrip("/"))
        path_key = fold(path)
        exact = path_key in (key, key + ".md")
        same_folder = source is not None and posixpath.dirname(path) == posixpath.dirname(source)
        return (not exact, not same_folder, path.count("/"), len(path), path)

    def suggest(
        self, target: str, include: Callable[[str], bool] | None = None
    ) -> str | None:
        """Closest existing file of the same kind by normalized name, or None
        if nothing is similar enough."""
        kind = link_kind(target)
        wanted_name = target.strip().rstrip("/").rsplit("/", 1)[-1]
        if kind == "note" and wanted_name.lower().endswith(".md"):
            wanted_name = wanted_name[:-3]
        wanted = normalize_name(wanted_name)
        if not wanted:
            return None
        pool: dict[str, str] = {}
        for path in self._files:
            if include is not None and not include(path):
                continue
            is_note = path.lower().endswith(".md")
            if kind == "note" and is_note:
                pool.setdefault(normalize_name(_stem(path)), path)
            elif kind == "attachment" and not is_note:
                pool.setdefault(normalize_name(path.rsplit("/", 1)[-1]), path)
        if kind == "note":
            for alias, path in self._aliases.items():
                if include is None or include(path):
                    pool.setdefault(normalize_name(alias), path)
        pool.pop("", None)
        best = difflib.get_close_matches(wanted, list(pool), n=1, cutoff=SUGGESTION_CUTOFF)
        return pool[best[0]] if best else None

    def ambiguous_stems(self) -> dict[str, list[str]]:
        """Folded note stem → note paths, for stems shared by several notes."""
        groups: dict[str, list[str]] = defaultdict(list)
        for path in self._files:
            if path.lower().endswith(".md"):
                groups[fold(_stem(path))].append(path)
        return {stem: paths for stem, paths in groups.items() if len(paths) > 1}
