"""Line-addressable knowledge base loader.

Each eval/kb/<doc_id>.md is split on "\n" exactly as an editor shows it: line numbers start at 1,
a trailing newline does not create an extra empty line, and a "\r" before "\n" is dropped.
"""
from __future__ import annotations

from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
KB_DIR = REPO_ROOT / "eval" / "kb"


class KBError(LookupError):
    pass


def split_lines(text: str) -> list[str]:
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line[:-1] if line.endswith("\r") else line for line in lines]


def load_kb(kb_dir: Path | str | None = None) -> dict[str, list[str]]:
    """Return {doc_id: [line1, line2, ...]}; doc_id is the file name without .md."""
    root = Path(kb_dir) if kb_dir else KB_DIR
    kb = {}
    for path in sorted(root.glob("*.md")):
        kb[path.stem] = split_lines(path.read_text(encoding="utf-8"))
    return kb


_CACHE: dict[str, dict[str, list[str]]] = {}


def _kb(kb: dict[str, list[str]] | None) -> dict[str, list[str]]:
    if kb is not None:
        return kb
    key = str(KB_DIR)
    if key not in _CACHE:
        _CACHE[key] = load_kb()
    return _CACHE[key]


def line_count(doc_id: str, kb: dict[str, list[str]] | None = None) -> int:
    docs = _kb(kb)
    if doc_id not in docs:
        raise KBError(f"unknown doc_id: {doc_id}")
    return len(docs[doc_id])


def is_valid_span(doc_id: str, start: int, end: int, kb: dict[str, list[str]] | None = None) -> bool:
    docs = _kb(kb)
    if doc_id not in docs or not isinstance(start, int) or not isinstance(end, int):
        return False
    return 1 <= start <= end <= len(docs[doc_id])


def get_lines(doc_id: str, start: int, end: int | None = None, kb: dict[str, list[str]] | None = None) -> list[str]:
    """Lines start..end inclusive, 1-based. end defaults to start."""
    end = start if end is None else end
    if not is_valid_span(doc_id, start, end, kb):
        raise KBError(f"invalid span {doc_id}:{start}-{end}")
    return _kb(kb)[doc_id][start - 1:end]


def render_with_line_numbers(doc_id: str, kb: dict[str, list[str]] | None = None) -> str:
    lines = _kb(kb).get(doc_id)
    if lines is None:
        raise KBError(f"unknown doc_id: {doc_id}")
    width = len(str(len(lines)))
    return "\n".join(f"{i:>{width}} | {line}" for i, line in enumerate(lines, 1))


def render_all(kb: dict[str, list[str]] | None = None) -> str:
    """Whole KB with doc headers and line numbers, e.g. for a single long prompt."""
    docs = _kb(kb)
    return "\n\n".join(f"=== doc_id: {d} ===\n{render_with_line_numbers(d, docs)}" for d in sorted(docs))
