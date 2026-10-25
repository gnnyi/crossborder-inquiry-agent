from pathlib import Path

import pytest

from sandbox import kb


def test_line_numbers_match_editor_view():
    docs = kb.load_kb()
    for doc_id, lines in docs.items():
        raw = (kb.KB_DIR / f"{doc_id}.md").read_text(encoding="utf-8")
        editor_lines = raw.splitlines()  # what an editor shows (no \v / \f in our files)
        assert lines == editor_lines, doc_id
        assert lines[1] == f"- doc_id: {doc_id}"


def test_get_lines_is_one_based_and_inclusive():
    docs = kb.load_kb()
    lines = docs["policy_returns"]
    assert kb.get_lines("policy_returns", 1, 1) == [lines[0]]
    assert kb.get_lines("policy_returns", 4, 6) == lines[3:6]
    assert kb.get_lines("policy_returns", 5) == [lines[4]]
    assert lines[0].startswith("# ")


def test_invalid_spans_raise():
    n = kb.line_count("faq")
    with pytest.raises(kb.KBError):
        kb.get_lines("faq", 0, 1)
    with pytest.raises(kb.KBError):
        kb.get_lines("faq", n, n + 1)
    with pytest.raises(kb.KBError):
        kb.get_lines("faq", 5, 4)
    with pytest.raises(kb.KBError):
        kb.get_lines("no_such_doc", 1, 1)
    assert kb.is_valid_span("faq", n, n)
    assert not kb.is_valid_span("faq", "1", 2)


def test_line_numbers_stable_across_loads_and_line_endings(tmp_path: Path):
    (tmp_path / "a.md").write_bytes(b"# A\r\n- doc_id: a\r\n- fact one\r\n- fact two\r\n")
    (tmp_path / "b.md").write_text("# B\n- doc_id: b\n- only fact", encoding="utf-8")
    first = kb.load_kb(tmp_path)
    second = kb.load_kb(tmp_path)
    assert first == second
    assert first["a"] == ["# A", "- doc_id: a", "- fact one", "- fact two"]
    assert first["b"][2] == "- only fact"
    assert kb.get_lines("a", 3, 4, first) == ["- fact one", "- fact two"]


def test_blank_lines_keep_editor_numbering(tmp_path: Path):
    (tmp_path / "c.md").write_text("# C\n- doc_id: c\n\n- after blank\n", encoding="utf-8")
    docs = kb.load_kb(tmp_path)
    assert kb.get_lines("c", 4, 4, docs) == ["- after blank"]
    assert kb.line_count("c", docs) == 4


def test_render_with_line_numbers():
    out = kb.render_with_line_numbers("wholesale")
    rows = out.split("\n")
    assert len(rows) == kb.line_count("wholesale")
    assert rows[1].endswith("| - doc_id: wholesale")
    assert rows[0].strip().startswith("1 |") or rows[0].lstrip().startswith(" 1 |") or rows[0].split("|")[0].strip() == "1"
    assert "=== doc_id: faq ===" in kb.render_all()
