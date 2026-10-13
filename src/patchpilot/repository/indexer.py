"""Repository snapshot + symbol-level chunking (Python AST; tree-sitter is the multi-language upgrade)."""
from __future__ import annotations

import ast
import hashlib
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from ..domain.models import Citation
from ..domain.policy import glob_match

DEFAULT_EXCLUDES = [
    "**/.git/**", "**/__pycache__/**", "**/.venv/**", "**/venv/**", "**/node_modules/**",
    "**/*.egg-info/**", "**/build/**", "**/dist/**", "**/.env*", "**/*secret*", "**/*.pem", "**/*.key",
]
MAX_FILE_BYTES = 200_000


@dataclass
class Chunk:
    path: str
    symbol: str
    kind: str  # module | function | class | method
    start_line: int
    end_line: int
    text: str
    commit_sha: str
    is_test: bool = False
    imports: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return hashlib.sha256(f"{self.path}:{self.start_line}:{self.text}".encode()).hexdigest()[:16]

    def citation(self) -> Citation:
        return Citation(path=self.path, symbol=self.symbol, start_line=self.start_line,
                        end_line=self.end_line, commit_sha=self.commit_sha, chunk_hash=self.hash)


@dataclass
class RepoIndex:
    root: Path
    commit_sha: str
    chunks: list[Chunk]
    files: list[str]

    def by_path(self, path: str) -> list[Chunk]:
        return [c for c in self.chunks if c.path == path]


def resolve_sha(root: Path, hint: str | None = None) -> str:
    if hint:
        return hint
    try:
        out = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"], capture_output=True, text=True, timeout=10)
        if out.returncode == 0:
            return out.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        pass
    # Not a git repo: derive a stable content fingerprint so provenance is still pinned.
    h = hashlib.sha256()
    for p in sorted(root.rglob("*.py")):
        if ".git" not in p.parts:
            h.update(p.relative_to(root).as_posix().encode() + p.read_bytes())
    return "content-" + h.hexdigest()[:32]


def clone_at(url: str, sha: str, dest: Path) -> Path:
    """Clone a repo and check out a fixed commit SHA (used when not working from a local path)."""
    dest.mkdir(parents=True, exist_ok=True)
    for cmd in (["git", "clone", "--no-checkout", url, str(dest)], ["git", "-C", str(dest), "checkout", sha]):
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0:
            raise RuntimeError(f"{' '.join(cmd[:3])} failed: {r.stderr.strip()[:200]}")
    return dest


def iter_source_files(root: Path, excludes: list[str] | None = None):
    excludes = DEFAULT_EXCLUDES if excludes is None else excludes
    for p in sorted(root.rglob("*")):
        if not p.is_file():
            continue
        rel = p.relative_to(root).as_posix()
        if any(glob_match(rel, pat) for pat in excludes):
            continue
        if p.stat().st_size > MAX_FILE_BYTES:
            continue
        yield p, rel


def _is_test(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return name.startswith("test_") or name.endswith("_test.py") or rel.startswith("tests/")


def _imports(tree: ast.AST) -> list[str]:
    mods: list[str] = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Import):
            mods += [a.name for a in n.names]
        elif isinstance(n, ast.ImportFrom) and n.module:
            mods.append(n.module)
    return sorted(set(mods))


def chunk_python(rel: str, src: str, sha: str) -> list[Chunk]:
    lines = src.splitlines()
    test = _is_test(rel)
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return [Chunk(rel, rel, "module", 1, max(1, len(lines)), src, sha, test)]
    imps = _imports(tree)
    chunks: list[Chunk] = []

    def add(node, symbol, kind):
        start = min([node.lineno] + [d.lineno for d in getattr(node, "decorator_list", [])])
        end = node.end_lineno or node.lineno
        chunks.append(Chunk(rel, symbol, kind, start, end, "\n".join(lines[start - 1:end]), sha, test, imps))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            add(node, node.name, "function")
        elif isinstance(node, ast.ClassDef):
            add(node, node.name, "class")
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    add(sub, f"{node.name}.{sub.name}", "method")
    header_end = tree.body[0].lineno - 1 if tree.body else len(lines)
    chunks.append(Chunk(rel, rel, "module", 1, max(1, len(lines)), "\n".join(lines[: max(header_end, 1)] or [""]), sha, test, imps))
    return chunks


def build_index(root: str | Path, commit_sha: str | None = None, excludes: list[str] | None = None) -> RepoIndex:
    root = Path(root).resolve()
    sha = resolve_sha(root, commit_sha)
    chunks: list[Chunk] = []
    files: list[str] = []
    for p, rel in iter_source_files(root, excludes):
        files.append(rel)
        if rel.endswith(".py"):
            try:
                chunks += chunk_python(rel, p.read_text(encoding="utf-8"), sha)
            except UnicodeDecodeError:
                continue
    return RepoIndex(root=root, commit_sha=sha, chunks=chunks, files=files)
