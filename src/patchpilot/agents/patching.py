"""Safe patch application: exact-match edits in a temp workspace, unified diff out."""
from __future__ import annotations

import difflib
from pathlib import Path

from ..domain.models import FileEdit
from ..domain.policy import normalize


class PatchError(Exception):
    pass


def _safe_target(workspace: Path, rel: str) -> Path:
    rel = normalize(rel)
    target = (workspace / rel).resolve()
    if not rel or ".." in Path(rel).parts or workspace.resolve() not in target.parents:
        raise PatchError(f"path escapes workspace: {rel!r}")
    return target


def apply_edits(workspace: Path, edits: list[FileEdit]) -> tuple[str, list[str], int, list[str]]:
    """Returns (unified_diff, changed_files, changed_line_count, added_lines)."""
    if not edits:
        raise PatchError("patch contains no edits")
    originals: dict[str, str | None] = {}
    updated: dict[str, str] = {}
    for e in edits:
        rel = normalize(e.path)
        target = _safe_target(workspace, rel)
        if rel not in updated:
            originals[rel] = target.read_text() if target.exists() else None
            updated[rel] = originals[rel] or ""
        cur = updated[rel]
        if e.old == "":
            if originals[rel] is not None:
                raise PatchError(f"{rel}: empty `old` would overwrite an existing file")
            updated[rel] = cur + e.new
        else:
            n = cur.count(e.old)
            if n != 1:
                raise PatchError(f"{rel}: `old` text must match exactly once (matched {n} times)")
            updated[rel] = cur.replace(e.old, e.new, 1)
    diff_parts: list[str] = []
    changed: list[str] = []
    added: list[str] = []
    n_lines = 0
    for rel, new_text in updated.items():
        old_text = originals[rel] or ""
        if new_text == old_text:
            continue
        if "\0" in new_text:
            raise PatchError(f"{rel}: binary content rejected")
        d = list(difflib.unified_diff(old_text.splitlines(True), new_text.splitlines(True),
                                      f"a/{rel}" if originals[rel] is not None else "/dev/null", f"b/{rel}"))
        diff_parts += [ln if ln.endswith("\n") else ln + "\n" for ln in d]
        changed.append(rel)
        for ln in d:
            if ln.startswith("+") and not ln.startswith("+++"):
                added.append(ln[1:].rstrip("\n"))
                n_lines += 1
            elif ln.startswith("-") and not ln.startswith("---"):
                n_lines += 1
        t = _safe_target(workspace, rel)
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(new_text)
    if not changed:
        raise PatchError("patch makes no changes")
    return "".join(diff_parts), changed, n_lines, added
