from __future__ import annotations

from obsidian_mcp.domain.markdown_scan import (
    mask_fenced_code,
    mask_inline_code_and_comments,
    mask_non_content,
)


def _same_shape(original: str, masked: str) -> None:
    assert len(original) == len(masked)
    assert [i for i, c in enumerate(original) if c == "\n"] == [
        i for i, c in enumerate(masked) if c == "\n"
    ]


def test_backtick_fence_is_masked_and_shape_preserved():
    text = "before\n```python\nx = [[link]]\n```\nafter"
    masked = mask_fenced_code(text)
    _same_shape(text, masked)
    assert "link" not in masked
    assert masked.startswith("before\n")
    assert masked.endswith("\nafter")


def test_tilde_fence_is_masked():
    text = "a\n~~~\n#tag\n~~~\nb"
    masked = mask_fenced_code(text)
    assert "#tag" not in masked
    assert masked.split("\n")[-1] == "b"


def test_fence_closes_only_with_same_char_and_enough_length():
    text = "````\n```\n#inside\n````\n#outside"
    masked = mask_fenced_code(text)
    assert "#inside" not in masked
    assert "#outside" in masked

    text = "~~~\n```\n#inside\n~~~\n#outside"
    masked = mask_fenced_code(text)
    assert "#inside" not in masked
    assert "#outside" in masked


def test_unclosed_fence_runs_to_end():
    masked = mask_fenced_code("ok\n```\n#a\n#b")
    assert masked.startswith("ok\n")
    assert "#" not in masked


def test_indented_and_blockquoted_fences():
    text = "- item\n    ```\n    [[x]]\n    ```\n> ```\n> [[y]]\n> ```\nend [[z]]"
    masked = mask_fenced_code(text)
    assert "[[x]]" not in masked
    assert "[[y]]" not in masked
    assert "[[z]]" in masked


def test_backtick_fence_info_string_with_backtick_is_not_a_fence():
    text = "```not `a fence\n[[kept]]"
    assert "[[kept]]" in mask_fenced_code(text)


def test_inline_code_span_masked():
    text = "see `[[x]]` and ``a ` b`` then [[y]]"
    masked = mask_inline_code_and_comments(text)
    _same_shape(text, masked)
    assert "[[x]]" not in masked
    assert "a ` b" not in masked
    assert "[[y]]" in masked


def test_unmatched_backtick_is_literal():
    text = "a ` lonely backtick [[y]]"
    assert mask_inline_code_and_comments(text) == text


def test_inline_code_does_not_cross_paragraphs():
    text = "start `open\n\n[[kept]] `"
    assert "[[kept]]" in mask_inline_code_and_comments(text)


def test_percent_comment_masked_multiline():
    text = "a %% hidden [[x]]\n#tag\n%% b [[y]]"
    masked = mask_inline_code_and_comments(text)
    _same_shape(text, masked)
    assert "[[x]]" not in masked
    assert "#tag" not in masked
    assert "[[y]]" in masked


def test_unclosed_percent_is_literal():
    text = "100%% sure [[y]]"
    assert mask_inline_code_and_comments(text) == text


def test_first_opener_wins():
    # The "%%" inside the code span does not open a comment, so [[a]] stays.
    assert mask_inline_code_and_comments("`%%` [[a]] %%") == "     [[a]] %%"
    masked = mask_inline_code_and_comments("%% `code %% [[a]]` x")
    assert "[[a]]" in masked


def test_mask_non_content_combines_everything():
    text = "```\n[[a]]\n```\n`[[b]]` %%[[c]]%% [[d]]"
    masked = mask_non_content(text)
    _same_shape(text, masked)
    assert "[[a]]" not in masked
    assert "[[b]]" not in masked
    assert "[[c]]" not in masked
    assert "[[d]]" in masked
