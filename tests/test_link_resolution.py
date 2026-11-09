from __future__ import annotations

import pytest

from obsidian_mcp.domain.links import LinkResolver, link_kind, normalize_name
from obsidian_mcp.domain.parser import extract_wikilinks
from obsidian_mcp.tools.query import (
    find_broken_links,
    get_broken_links,
    get_vault_stats,
    resolve_alias,
)

# ── parsing ───────────────────────────────────────────────────────────────


def test_wikilinks_ignore_code_and_comments():
    body = (
        "```\n[[in fence]]\n```\n~~~\n[[in tilde]]\n~~~\n"
        "`[[in span]]` %% [[in comment]] %% [[real]] ![[pic.png]]"
    )
    links = extract_wikilinks(body)
    assert [link.target for link in links] == ["real", "pic.png"]
    assert [link.embed for link in links] == [False, True]


@pytest.mark.parametrize(
    "text", ["[[1,320,320,3]]", "[[ 1, 2 ]]", "[[-1.5, 2]]", "[[[nested]]]", "[[42]]"]
)
def test_data_lookalike_links_are_discarded(text):
    assert extract_wikilinks(f"shape {text} here") == []


def test_escaped_table_pipe_is_stripped_from_target():
    links = extract_wikilinks("| [[Note\\|alias]] |")
    assert links[0].target == "Note"


# ── resolver ──────────────────────────────────────────────────────────────

FILES = [
    "(100) Attachments/images/performance/mobileExec.png",
    "(300) Permanent/Café Notes.md",
    "(300) Permanent/TwoPointers.excalidraw.md",
    "a/Dup.md",
    "b/Dup.md",
    "b/deeper/Dup.md",
    "img/x.png",
    "other/img/x.png",
]


def test_attachment_resolves_by_file_name_vault_wide():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("mobileExec.png", source="notes/n.md") == FILES[0]
    assert resolver.resolve("MOBILEEXEC.PNG") == FILES[0]


def test_partial_path_resolves():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("performance/mobileExec.png") == FILES[0]
    assert resolver.resolve("other/img/x.png") == "other/img/x.png"
    assert resolver.resolve("img/x.png") == "img/x.png"  # exact path wins
    assert resolver.resolve("missing/mobileExec.png") is None


def test_excalidraw_link_resolves_to_markdown_file():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("TwoPointers.excalidraw") == "(300) Permanent/TwoPointers.excalidraw.md"


def test_case_insensitive_note_match_and_unicode_normalization():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("café notes") == "(300) Permanent/Café Notes.md"
    # NFD-decomposed accent still matches the NFC file name.
    assert resolver.resolve("Café Notes") == "(300) Permanent/Café Notes.md"


def test_ambiguous_name_prefers_same_folder_then_shortest_path():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("Dup", source="b/deeper/n.md") == "b/deeper/Dup.md"
    assert resolver.resolve("Dup", source="z.md") == "a/Dup.md"
    assert sorted(resolver.candidates("Dup")) == ["a/Dup.md", "b/Dup.md", "b/deeper/Dup.md"]
    assert set(resolver.ambiguous_stems()["dup"]) == {"a/Dup.md", "b/Dup.md", "b/deeper/Dup.md"}


def test_relative_and_absolute_paths():
    resolver = LinkResolver(FILES)
    assert resolver.resolve("../a/Dup", source="b/n.md") == "a/Dup.md"
    assert resolver.resolve("/a/Dup") == "a/Dup.md"


def test_alias_fallback():
    resolver = LinkResolver(["n/Real.md"], {"Nickname": "n/Real.md"})
    assert resolver.resolve("nickname") == "n/Real.md"


def test_kind_and_suggestion():
    resolver = LinkResolver(FILES)
    assert link_kind("x.PNG") == "attachment"
    assert link_kind("Some note") == "note"
    assert link_kind("Foo.excalidraw") == "note"
    assert link_kind("v1.2 release") == "note"
    assert resolver.suggest("Cafe notes!") == "(300) Permanent/Café Notes.md"
    assert resolver.suggest("mobileExe.png") == FILES[0]
    assert resolver.suggest("Totally unrelated title") is None
    # Attachments are only suggested for attachment links, notes for notes.
    assert resolver.suggest("mobileExec") is None


def test_normalize_name():
    assert normalize_name("Ação — Ñandú (v2)") == "acaonandu" "v2"


# ── tools ─────────────────────────────────────────────────────────────────


def _vault(vault_factory, tmp_path):
    idx = vault_factory({
        "(300) Permanent/Linker.md": (
            "![[mobileExec.png]] [[performance/mobileExec.png]] [[TwoPointers.excalidraw]]\n"
            "[[xnova onboarding]] [[Theory of Computation]] [[Missing.png]]\n"
            "```\n[[Code Example]]\n```\nshape [[1,320,320,3]] %% [[Commented]] %%\n"
            "[[Dup]] [[Theory of Computation#Intro]]\n"
        ),
        "(300) Permanent/xNova Onboarding.md": "x",
        "(300) Permanent/TwoPointers.excalidraw.md": "drawing",
        "a/Dup.md": "",
        "b/Dup.md": "",
        "Theory of Computations.md": "",
    })
    img = tmp_path / "(100) Attachments/images/performance/mobileExec.png"
    img.parent.mkdir(parents=True, exist_ok=True)
    img.write_bytes(b"\x89PNG")
    return idx


def test_broken_links_obsidian_semantics(vault_factory, tmp_path):
    idx = _vault(vault_factory, tmp_path)
    report = find_broken_links(idx)
    broken = {item["link"]: item for item in report["items"]}
    assert set(broken) == {"Theory of Computation", "Missing.png"}
    assert broken["Theory of Computation"]["kind"] == "note"
    assert broken["Theory of Computation"]["suggestion"] == "Theory of Computations.md"
    assert broken["Missing.png"]["kind"] == "attachment"
    assert broken["Missing.png"]["suggestion"] is None
    assert report["warnings"] == [{
        "type": "ambiguous_link",
        "link": "Dup",
        "candidates": ["a/Dup.md", "b/Dup.md"],
        "sources": ["(300) Permanent/Linker.md"],
    }]
    assert get_broken_links(idx) == report["items"]
    assert get_vault_stats(idx)["broken_links_count"] == 2


def test_resolve_alias_attachment_name(vault_factory, tmp_path):
    idx = _vault(vault_factory, tmp_path)
    assert resolve_alias("mobileExec.png", idx) == (
        "(100) Attachments/images/performance/mobileExec.png"
    )
    assert resolve_alias("xnova onboarding", idx) == "(300) Permanent/xNova Onboarding.md"
    assert resolve_alias("nothing-here.png", idx) is None
