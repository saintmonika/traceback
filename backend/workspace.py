"""Workspace manager.

Run folder layout:
  runs/<run_id>/
    state.json
    workspace/   copy of the fixture; the only place the repair is applied
    mutants/     one fresh copy per mutation (m01, m02, ...)
    checks/      temporary copies used to validate gap tests
    evidence/    numbered evidence files

Every write is checked to be inside the run folder. Anything else is refused.
The original fixture folder is only ever read.
"""

import difflib
import hashlib
import shutil
from datetime import datetime
from pathlib import Path

from . import config

IGNORED_NAMES = {".git", "__pycache__", ".pytest_cache"}
_ignore = shutil.ignore_patterns(*IGNORED_NAMES)


def make_run_id():
    base = "run_" + datetime.now().strftime("%Y%m%d_%H%M%S")
    run_id, n = base, 2
    while (config.RUNS_ROOT / run_id).exists():
        run_id = f"{base}_{n}"
        n += 1
    return run_id


def _is_ignored(rel_parts):
    return any(p in IGNORED_NAMES for p in rel_parts)


def hash_tree(folder):
    """SHA-256 of every file in a folder (caches and .git skipped)."""
    folder = Path(folder)
    hashes = {}
    for path in sorted(folder.rglob("*")):
        rel = path.relative_to(folder)
        if path.is_file() and not _is_ignored(rel.parts):
            hashes[rel.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return hashes


def list_files(folder):
    """Relative paths of every file in a folder (caches and .git skipped)."""
    return sorted(hash_tree(folder).keys())


class RunFolder:
    def __init__(self, run_id):
        self.run_id = run_id
        self.root = (config.RUNS_ROOT / run_id).resolve()
        self.workspace = self.root / "workspace"
        self.mutants = self.root / "mutants"
        self.checks = self.root / "checks"
        self.evidence = self.root / "evidence"

    def create(self):
        self.root.mkdir(parents=True, exist_ok=False)
        for d in (self.mutants, self.checks, self.evidence):
            d.mkdir()

    # -- safety -------------------------------------------------------------
    def safe(self, path):
        """Resolve a path and refuse it unless it is inside this run folder."""
        p = Path(path).resolve()
        if p != self.root and self.root not in p.parents:
            raise ValueError(f"Refusing to write outside the run folder: {p}")
        return p

    # -- copies -------------------------------------------------------------
    def copy_fixture(self):
        shutil.copytree(config.FIXTURE_DIR, self.safe(self.workspace), ignore=_ignore)

    def fresh_copy(self, dest):
        """Copy the current workspace into dest (inside the run folder)."""
        dest = self.safe(dest)
        if dest == self.workspace or dest == self.root:
            raise ValueError("Refusing to overwrite the workspace or run folder")
        if dest.exists():
            shutil.rmtree(dest)
        shutil.copytree(self.workspace, dest, ignore=_ignore)
        return dest

    # -- files --------------------------------------------------------------
    def read(self, base, rel_path):
        return (self.safe(Path(base) / rel_path)).read_text(encoding="utf-8")

    def write(self, base, rel_path, text):
        p = self.safe(Path(base) / rel_path)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    def delete(self, base, rel_path):
        p = self.safe(Path(base) / rel_path)
        if p.exists():
            p.unlink()

    def apply_file(self, rel_path, new_text):
        """Replace a workspace file. Returns the previous content."""
        previous = self.read(self.workspace, rel_path)
        self.write(self.workspace, rel_path, new_text)
        return previous


def unified_diff(old_text, new_text, rel_path):
    return "".join(difflib.unified_diff(
        old_text.splitlines(keepends=True),
        new_text.splitlines(keepends=True),
        fromfile=f"a/{rel_path}",
        tofile=f"b/{rel_path}",
    ))


def changed_lines(old_text, new_text):
    """Line numbers (1-based) in the NEW text that fall inside a replaced or
    inserted region. These are the only lines the mutation generator may touch."""
    matcher = difflib.SequenceMatcher(
        a=old_text.splitlines(), b=new_text.splitlines(), autojunk=False
    )
    lines = set()
    for tag, _i1, _i2, j1, j2 in matcher.get_opcodes():
        if tag in ("replace", "insert"):
            lines.update(range(j1 + 1, j2 + 1))
    return sorted(lines)