import subprocess

import pytest

from patchpilot.repository.indexer import build_index, chunk_python, clone_at
from patchpilot.repository.retrieval import Retriever, tokenize

APP = '''
import re

EMAIL = re.compile(r"^[^@]+@[^@]+$")


def validate_email(value):
    """Return True when value looks like an email."""
    if value is None:
        return False
    return bool(EMAIL.match(value))


def slugify(title):
    return title.lower().replace(" ", "-")


class UserForm:
    def clean_name(self, name):
        return name.strip()
'''
TEST = '''
from app.validators import validate_email


def test_valid():
    assert validate_email("a@b.co")
'''


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "app" / "validators.py").write_text(APP)
    (tmp_path / "tests" / "test_validators.py").write_text(TEST)
    (tmp_path / "app" / "api_secret.py").write_text("KEY='x'")
    (tmp_path / ".env").write_text("TOKEN=abc")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "x.py").write_text("def x(): pass")
    return tmp_path


def test_chunking_symbols_and_lines():
    chunks = chunk_python("app/v.py", APP, "abc12345")
    names = {c.symbol for c in chunks}
    assert {"validate_email", "slugify", "UserForm", "UserForm.clean_name"} <= names
    fn = next(c for c in chunks if c.symbol == "validate_email")
    assert fn.text.startswith("def validate_email") and fn.end_line > fn.start_line
    assert fn.citation().commit_sha == "abc12345"


def test_index_excludes_secrets_and_vendored(repo):
    idx = build_index(repo, "deadbeefcafe")
    assert "app/validators.py" in idx.files
    for bad in (".env", "app/api_secret.py", "node_modules/x.py"):
        assert bad not in idx.files
    assert all(c.commit_sha == "deadbeefcafe" for c in idx.chunks)


def test_sha_fingerprint_without_git(repo):
    a, b = build_index(repo).commit_sha, build_index(repo).commit_sha
    assert a == b and a.startswith("content-")
    (repo / "app" / "validators.py").write_text(APP + "\n# change\n")
    assert build_index(repo).commit_sha != a


def test_retrieval_finds_symbol_and_linked_test(repo):
    r = Retriever(build_index(repo, "sha1"))
    hits = r.search("validate_email accepts empty email string", k=6)
    assert hits[0].chunk.symbol == "validate_email"
    assert any(h.chunk.is_test for h in hits)  # its test is in the evidence set


def test_test_to_source_link_expansion(repo):
    hits = Retriever(build_index(repo, "sha1")).search("slugify title lower", k=6)
    assert hits[0].chunk.symbol == "slugify"
    assert any(h.why == "test_link" and h.chunk.path == "tests/test_validators.py" for h in hits)


def test_camelcase_tokenization():
    assert "user" in tokenize("UserForm") and "form" in tokenize("UserForm")


def test_clone_at_pinned_sha(tmp_path):
    src = tmp_path / "src"
    src.mkdir()

    def run(*a):
        return subprocess.run(["git", "-C", str(src), *a], check=True, capture_output=True)

    run("init", "-q")
    run("config", "user.email", "t@t")
    run("config", "user.name", "t")
    (src / "a.py").write_text("x = 1\n")
    run("add", ".")
    run("commit", "-qm", "one")
    sha = subprocess.run(["git", "-C", str(src), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()
    (src / "a.py").write_text("x = 2\n")
    run("commit", "-qam", "two")
    out = clone_at(str(src), sha, tmp_path / "dst")
    assert (out / "a.py").read_text() == "x = 1\n"
    assert build_index(out).commit_sha == sha
