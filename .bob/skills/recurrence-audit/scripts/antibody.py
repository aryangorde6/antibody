#!/usr/bin/env python3
"""
antibody.py — Antibody runner.

Subcommands:
  setup      <name> <git-url> [--rev SHA] [--deps PKG ...]
  candidates <name> [--since DATE]
  run        <name> [--shas FILE]
  probe      <name> <sha> <check_file>
  prove      <name> <sha> <test>

Python 3.12, standard library only.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import fcntl
import json
import os
import re
import resource
import signal
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

SCRIPT_DIR = Path(__file__).parent
PLUGIN_FILE = SCRIPT_DIR / "antibody_plugin.py"

def repo_root() -> Path:
    """Walk up to find the git root (the antibody project root).
    Tests may override via _ANTIBODY_ROOT environment variable."""
    override = os.environ.get("_ANTIBODY_ROOT")
    if override:
        return Path(override)
    p = Path(__file__).resolve()
    for parent in [p, *p.parents]:
        if (parent / ".git").exists():
            return parent
    return Path.cwd()

ROOT = repo_root()
TARGETS_DIR = ROOT / "targets"
VENVS_DIR = TARGETS_DIR / ".venvs"
ANTIBODY_DIR = ROOT / ".antibody"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def run(cmd: list[str], *, cwd: Path | None = None, env: dict | None = None,
        check: bool = True, capture: bool = False, timeout: int | None = None) -> subprocess.CompletedProcess:
    kwargs: dict = dict(
        cwd=str(cwd) if cwd else None,
        env=env,
        check=check,
        timeout=timeout,
    )
    if capture:
        kwargs["stdout"] = subprocess.PIPE
        kwargs["stderr"] = subprocess.PIPE
        kwargs["text"] = True
    return subprocess.run(cmd, **kwargs)


def venv_python(name: str) -> Path:
    return VENVS_DIR / name / "bin" / "python"


def venv_pip(name: str) -> Path:
    return VENVS_DIR / name / "bin" / "pip"


def target_dir(name: str) -> Path:
    return TARGETS_DIR / name


def antibody_dir(name: str) -> Path:
    d = ANTIBODY_DIR / name
    d.mkdir(parents=True, exist_ok=True)
    return d


def diffs_dir(name: str) -> Path:
    d = antibody_dir(name) / "diffs"
    d.mkdir(parents=True, exist_ok=True)
    return d


def threads_dir(name: str) -> Path:
    """threads/ is gitignored; created on demand."""
    d = antibody_dir(name) / "threads"
    d.mkdir(parents=True, exist_ok=True)
    return d


def die(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


# ---------------------------------------------------------------------------
# Exclusive per-target lock
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def target_lock(name: str):
    """
    Hold an exclusive fcntl lock on targets/<name>.lock for the duration of
    the with-block.  A second caller prints "waiting for <name>'s lock" and
    blocks until the first releases it.
    """
    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    lock_path = TARGETS_DIR / f"{name}.lock"
    with open(lock_path, "w") as lf:
        # Try a non-blocking acquire first so we can print the wait message.
        try:
            fcntl.flock(lf, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"waiting for {name}'s lock", flush=True)
            fcntl.flock(lf, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lf, fcntl.LOCK_UN)


# ---------------------------------------------------------------------------
# Pytest report runner
# ---------------------------------------------------------------------------

def run_suite(name: str, *, extra_args: list[str] | None = None,
              timeout_secs: int = 120, report_path: Path | None = None) -> dict:
    """
    Run the test suite for target <name> via its venv's Python.
    Returns the parsed report dict (keys: collected, failed, collection_errors).
    Raises RuntimeError on timeout or missing report.
    """
    python = venv_python(name)
    tgt = target_dir(name)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        rpath = Path(tmp.name)

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ANTIBODY_REPORT_PATH"] = str(rpath)
    # Add the plugin folder to PYTHONPATH
    plugin_dir = str(PLUGIN_FILE.parent)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = plugin_dir + (":" + existing_pp if existing_pp else "")

    cmd = [
        str(python), "-m", "pytest",
        "-p", "no:cacheprovider",
        "-p", "antibody_plugin",
    ]
    if extra_args:
        cmd.extend(extra_args)

    # address-space limit: 1.5 GB
    as_limit = 1_500 * 1024 * 1024

    def set_limits():
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
        except Exception:
            pass

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(tgt),
            env=env,
            timeout=timeout_secs,
            preexec_fn=set_limits,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
    except subprocess.TimeoutExpired:
        rpath.unlink(missing_ok=True)
        raise RuntimeError("timeout")

    if not rpath.exists() or rpath.stat().st_size == 0:
        rpath.unlink(missing_ok=True)
        raise RuntimeError("missing-report")

    with open(rpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    rpath.unlink(missing_ok=True)
    return data


# ---------------------------------------------------------------------------
# setup
# ---------------------------------------------------------------------------

def cmd_setup(args: argparse.Namespace) -> None:
    name: str = args.name
    git_url: str = args.git_url
    rev: str | None = args.rev
    deps: list[str] = args.deps or []

    tgt = target_dir(name)
    venv = VENVS_DIR / name
    ab = antibody_dir(name)

    TARGETS_DIR.mkdir(parents=True, exist_ok=True)
    VENVS_DIR.mkdir(parents=True, exist_ok=True)

    # Clone
    if tgt.exists():
        print(f"Target directory {tgt} already exists; skipping clone.")
    else:
        print(f"Cloning {git_url} → {tgt} …")
        run(["git", "clone", "--recurse-submodules", git_url, str(tgt)])

    # Checkout rev
    if rev:
        print(f"Checking out {rev} …")
        run(["git", "checkout", rev], cwd=tgt)
        run(["git", "submodule", "update", "--init", "--recursive"], cwd=tgt)

    # Get HEAD sha
    result = run(["git", "rev-parse", "HEAD"], cwd=tgt, capture=True)
    head_sha = result.stdout.strip()

    # Create venv
    if venv.exists():
        print(f"Venv {venv} already exists; skipping creation.")
    else:
        print(f"Creating venv {venv} …")
        run(["uv", "venv", "--python", "3.12", str(venv)])

    python = str(venv / "bin" / "python")
    # Relative python path for recording in setup.json (relative to ROOT)
    python_rel = str((venv / "bin" / "python").relative_to(ROOT))
    tgt_rel = str(tgt.relative_to(ROOT))

    # Install project editable
    print("Installing project (editable) …")
    install_cmds = [
        ["uv", "pip", "install", "--python", python, "--quiet", "-e", str(tgt)],
    ]
    install_cmds_rel = [
        ["uv", "pip", "install", "--python", python_rel, "--quiet", "-e", tgt_rel],
    ]
    # Also install deps
    if deps:
        dep_cmd = ["uv", "pip", "install", "--python", python, "--quiet"] + deps
        install_cmds.append(dep_cmd)
        install_cmds_rel.append(["uv", "pip", "install", "--python", python_rel, "--quiet"] + deps)
    else:
        # Always install pytest
        dep_cmd = ["uv", "pip", "install", "--python", python, "--quiet", "pytest"]
        install_cmds.append(dep_cmd)
        install_cmds_rel.append(["uv", "pip", "install", "--python", python_rel, "--quiet", "pytest"])

    for cmd_list in install_cmds:
        run(cmd_list)

    # Run baseline
    print("Running baseline test suite …")
    t0 = time.monotonic()
    try:
        report = run_suite(name, timeout_secs=300)
    except RuntimeError as e:
        die(f"Baseline run failed: {e}")
    elapsed = time.monotonic() - t0

    collected = len(report.get("collected", []))
    failed = report.get("failed", [])
    errors = report.get("collection_errors", [])

    if failed or errors:
        die(
            f"Baseline is not fully green: {len(failed)} failure(s), "
            f"{len(errors)} collection error(s). Fix the target before continuing."
        )

    if collected == 0:
        die("Baseline collected 0 tests. Aborting.")

    passed = collected - len(failed)
    skipped = report.get("skipped", 0)
    xfailed = report.get("xfailed", 0)
    xpassed = report.get("xpassed", 0)

    setup_data = {
        "repo": git_url,
        "head": head_sha,
        "deps": deps,
        "install_commands": [" ".join(c) for c in install_cmds_rel],
        "baseline": {
            "collected": collected,
            "passed": passed,
            "skipped": skipped,
            "xfailed": xfailed,
            "xpassed": xpassed,
            "seconds": round(elapsed, 2),
        },
    }

    out_path = ab / "setup.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(setup_data, f, indent=2)
    print(f"Wrote {out_path}")
    print(f"Baseline: {collected} collected, {passed} passed, {skipped} skipped, "
          f"{xfailed} xfailed, {xpassed} xpassed, {elapsed:.1f}s  ✓")


# ---------------------------------------------------------------------------
# candidates
# ---------------------------------------------------------------------------

# Keywords that mark a fix commit.
# fix/bug/crash/regress match inside longer words (bugfix, hotfix, regression).
# Issue numbers match as #801, issue 801, issue752, ISSUE_801.
FIX_KEYWORDS = re.compile(
    r"(fix|bug|crash|regress)"
    r"|issue[\s_]?\d+"
    r"|#\d+"
    r"|CVE-\d{4}-\d+"
    r"|GHSA-[0-9a-z-]+",
    re.IGNORECASE,
)

# Folders that are NOT package source
NON_SOURCE_PATTERNS = re.compile(
    r"(^|/)(tests?|docs?|examples?|benchmarks?|scripts?|\.github|ci)(/|$)",
    re.IGNORECASE,
)

REVERT_RE = re.compile(r"This reverts commit ([0-9a-f]{7,40})", re.IGNORECASE)


def is_source_file(path: str) -> bool:
    """Return True if path is a .py file not in test/docs/etc folders."""
    if not path.endswith(".py"):
        return False
    if NON_SOURCE_PATTERNS.search(path):
        return False
    return True


def strip_non_behavioural(source: str) -> str:
    """
    Remove docstrings, comments, type annotations, TYPE_CHECKING blocks,
    typing imports, and cast() calls from Python source.
    Returns the stripped source suitable for AST comparison.
    """
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return source

    class Stripper(ast.NodeTransformer):
        def visit_Expr(self, node):
            # Remove docstrings (string constants as standalone statements)
            if isinstance(node.value, ast.Constant) and isinstance(node.value.value, str):
                return None
            return self.generic_visit(node)

        def visit_AnnAssign(self, node):
            # Remove type-annotated assignments with no value
            if node.value is None:
                return None
            return self.generic_visit(node)

        def visit_FunctionDef(self, node):
            node = self.generic_visit(node)
            # Strip return annotation
            node.returns = None
            # Strip argument annotations
            for arg in node.args.args + node.args.posonlyargs + node.args.kwonlyargs:
                arg.annotation = None
            if node.args.vararg:
                node.args.vararg.annotation = None
            if node.args.kwarg:
                node.args.kwarg.annotation = None
            return node

        visit_AsyncFunctionDef = visit_FunctionDef

        def visit_If(self, node):
            # Remove TYPE_CHECKING blocks
            test = node.test
            is_type_checking = (
                (isinstance(test, ast.Name) and test.id == "TYPE_CHECKING")
                or (isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING")
            )
            if is_type_checking:
                return None
            return self.generic_visit(node)

        def visit_Import(self, node):
            # Remove typing imports
            typing_mods = {"typing", "typing_extensions"}
            node.names = [a for a in node.names if a.name.split(".")[0] not in typing_mods]
            if not node.names:
                return None
            return node

        def visit_ImportFrom(self, node):
            if node.module and node.module.split(".")[0] in {"typing", "typing_extensions"}:
                return None
            return node

        def visit_Call(self, node):
            self.generic_visit(node)
            # Replace cast(T, x) with x
            func = node.func
            is_cast = (
                (isinstance(func, ast.Name) and func.id == "cast")
                or (isinstance(func, ast.Attribute) and func.attr == "cast")
            )
            if is_cast and len(node.args) == 2:
                return node.args[1]
            return node

    stripped = Stripper().visit(tree)
    ast.fix_missing_locations(stripped)
    try:
        return ast.dump(stripped)
    except Exception:
        return ast.dump(tree)


def ast_equal_after_strip(old_src: str, new_src: str) -> bool:
    """Return True if two Python source strings are behaviourally identical after stripping."""
    try:
        return strip_non_behavioural(old_src) == strip_non_behavioural(new_src)
    except Exception:
        return False


def get_commit_log(tgt: Path, since: str | None) -> list[dict]:
    """
    Return list of {sha, subject, body} for non-merge commits since `since`.
    Uses record separator \x1e (ASCII record separator) which doesn't appear in git output.
    """
    sep = "\x1e"  # ASCII record separator — safe in subprocess args
    rec_end = "\x1f"  # ASCII unit separator — marks end of one commit record
    fmt = f"%H{sep}%s{sep}%b{rec_end}"
    cmd = ["git", "log", "--no-merges", f"--format={fmt}"]
    if since:
        cmd.append(f"--since={since}")
    result = run(cmd, cwd=tgt, capture=True)

    commits = []
    for block in result.stdout.split(rec_end):
        block = block.strip()
        if not block:
            continue
        parts = block.split(sep, 2)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        subject = parts[1].strip()
        body = parts[2].strip() if len(parts) > 2 else ""
        if sha:
            commits.append({"sha": sha, "subject": subject, "body": body})
    return commits


def get_commit_files(tgt: Path, sha: str) -> list[str]:
    """Return list of files changed by a commit."""
    result = run(
        ["git", "diff-tree", "--no-commit-id", "-r", "--name-only", sha],
        cwd=tgt, capture=True
    )
    return [l.strip() for l in result.stdout.splitlines() if l.strip()]


def get_commit_diff(tgt: Path, sha: str, src_files: list[str]) -> str:
    """Return unified diff for the given source files in a commit.
    sha must come before -- so git treats it as a revision, not a pathspec.
    """
    result = run(
        ["git", "show", sha, "--format=", "--", *src_files],
        cwd=tgt, capture=True
    )
    return result.stdout


def get_file_at_commit(tgt: Path, sha: str, path: str) -> str | None:
    """Return file content at a given commit, or None if not present."""
    try:
        result = run(["git", "show", f"{sha}:{path}"], cwd=tgt, capture=True, check=True)
        return result.stdout
    except subprocess.CalledProcessError:
        return None


def count_diff_lines(diff_text: str) -> int:
    """Count added+removed source lines in a unified diff."""
    count = 0
    for line in diff_text.splitlines():
        if line.startswith(("+", "-")) and not line.startswith(("+++", "---")):
            count += 1
    return count


def build_patch(tgt: Path, sha: str, src_files: list[str]) -> str:
    """Return a diff of source files only (no author, email, or message body).
    Uses ``git show <sha> --format= -- <files>`` so the output starts directly
    with the diff header and can be reversed with ``git apply -R``.
    """
    result = run(
        ["git", "show", sha, "--format=", "--", *src_files],
        cwd=tgt, capture=True
    )
    return result.stdout


def fetch_gh(endpoint: str) -> dict | list | None:
    """Call `gh api <endpoint>`, return parsed JSON or None on failure."""
    try:
        result = run(["gh", "api", endpoint], capture=True, check=True, timeout=30)
        return json.loads(result.stdout)
    except Exception:
        return None


_HANDLE_RE = re.compile(r"@[A-Za-z0-9_-]+")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_AI_PARA_RE = re.compile(
    r"^[^\n]*\b(AI|agent|LLM|language\s+model)s?\b[^\n]*(\n|$)",
    re.IGNORECASE | re.MULTILINE,
)

def sanitise(text: str) -> str:
    """Remove author handles, emails, and AI-tool paragraphs."""
    if not text:
        return text
    # Remove AI paragraphs (whole paragraph: separated by blank lines)
    paragraphs = re.split(r"\n\n+", text)
    ai_word = re.compile(r"\b(AI|agent|LLM|language\s+model)s?\b", re.IGNORECASE)
    paragraphs = [p for p in paragraphs if not ai_word.search(p)]
    text = "\n\n".join(paragraphs)
    # Remove @handles
    text = _HANDLE_RE.sub("", text)
    # Replace emails
    text = _EMAIL_RE.sub("<email>", text)
    return text


def build_thread(owner: str, repo: str, sha: str, subject: str, body: str) -> str:
    """Build the thread markdown for a commit."""
    lines: list[str] = []
    lines.append(f"# {sanitise(subject)}\n")

    # Links in subject or body
    issue_nums = set(re.findall(r"#(\d+)", subject + " " + body))
    cve_ids = set(re.findall(r"CVE-\d{4}-\d+", subject + " " + body, re.IGNORECASE))
    ghsa_ids = set(re.findall(r"GHSA-[0-9a-z-]+", subject + " " + body, re.IGNORECASE))

    # Merged-via PR
    pulls_data = fetch_gh(f"repos/{owner}/{repo}/commits/{sha}/pulls") or []
    pr_nums = {str(pr.get("number")) for pr in pulls_data if isinstance(pr, dict)}
    issue_nums |= pr_nums

    for num in sorted(issue_nums, key=lambda x: int(x)):
        data = fetch_gh(f"repos/{owner}/{repo}/issues/{num}")
        if not data:
            continue
        title = sanitise(data.get("title", ""))
        body_text = sanitise(data.get("body") or "")
        lines.append(f"## Issue/PR #{num}: {title}\n")
        if body_text:
            lines.append(body_text + "\n")
        # Comments
        comments = fetch_gh(f"repos/{owner}/{repo}/issues/{num}/comments") or []
        for c in comments:
            if isinstance(c, dict):
                cbody = sanitise(c.get("body") or "")
                if cbody:
                    lines.append(cbody + "\n")

    for ghsa in sorted(ghsa_ids):
        data = fetch_gh(f"advisories/{ghsa}")
        if not data:
            continue
        title = sanitise(data.get("summary") or data.get("title") or ghsa)
        desc = sanitise(data.get("description") or "")
        lines.append(f"## Advisory {ghsa}: {title}\n")
        if desc:
            lines.append(desc + "\n")

    return "\n".join(lines)


def parse_owner_repo(git_url: str) -> tuple[str, str] | None:
    """Extract owner/repo from a GitHub URL."""
    m = re.search(r"github\.com[:/]([^/]+)/([^/\s]+?)(?:\.git)?$", git_url)
    if m:
        return m.group(1), m.group(2)
    return None


def cmd_candidates(args: argparse.Namespace) -> None:
    name: str = args.name
    since: str | None = args.since

    ab = antibody_dir(name)
    setup_path = ab / "setup.json"
    if not setup_path.exists():
        die(f"No setup.json found for '{name}'. Run setup first.")

    with open(setup_path, "r", encoding="utf-8") as f:
        setup_data = json.load(f)

    git_url: str = setup_data["repo"]
    tgt = target_dir(name)

    print(f"Scanning commit log for candidates (since={since or 'beginning'}) …")
    commits = get_commit_log(tgt, since)
    print(f"  {len(commits)} non-merge commits found")

    # Build set of revert targets
    reverted_shas: set[str] = set()
    for c in commits:
        m = REVERT_RE.search(c["subject"] + " " + c["body"])
        if m:
            reverted_shas.add(m.group(1)[:40])

    owner_repo = parse_owner_repo(git_url)

    candidates: list[dict] = []

    for c in commits:
        sha = c["sha"]
        subject = c["subject"]
        body = c["body"]

        # Must mention a fix keyword
        if not FIX_KEYWORDS.search(subject + " " + body):
            continue

        # Get changed files
        all_files = get_commit_files(tgt, sha)
        src_files = [f for f in all_files if is_source_file(f)]

        # 1-3 source files
        if not (1 <= len(src_files) <= 3):
            continue

        # Get diff
        diff_text = get_commit_diff(tgt, sha, src_files)
        line_count = count_diff_lines(diff_text)

        # At most 150 source lines changed
        if line_count > 150:
            continue

        # Check if reverted
        reverted = any(sha.startswith(rs) or rs.startswith(sha) for rs in reverted_shas)
        if reverted:
            candidates.append({
                "sha": sha,
                "subject": subject,
                "src_files": src_files,
                "diff_lines": line_count,
                "status": "EXCLUDED:reverted-later",
                "reason": "A later commit reverts this one",
            })
            continue

        # Check non-behavioural (compare ASTs before/after for each changed file)
        all_non_behavioural = True
        for fpath in src_files:
            before = get_file_at_commit(tgt, sha + "^", fpath)
            after = get_file_at_commit(tgt, sha, fpath)
            if before is None or after is None:
                all_non_behavioural = False
                break
            if not ast_equal_after_strip(before, after):
                all_non_behavioural = False
                break

        if all_non_behavioural:
            candidates.append({
                "sha": sha,
                "subject": subject,
                "src_files": src_files,
                "diff_lines": line_count,
                "status": "EXCLUDED:non-behavioural",
                "reason": "Source changes are identical after stripping non-behavioural elements",
            })
            continue

        # Save patch (source hunks only)
        patch = build_patch(tgt, sha, src_files)
        patch_path = diffs_dir(name) / f"{sha}.patch"
        with open(patch_path, "w", encoding="utf-8") as f:
            f.write(patch)

        # Save thread (if GitHub)
        if owner_repo:
            owner, repo = owner_repo
            thread_text = build_thread(owner, repo, sha, subject, body)
            tdir = threads_dir(name)
            tpath = tdir / f"{sha}.md"
            with open(tpath, "w", encoding="utf-8") as f:
                f.write(thread_text)

        candidates.append({
            "sha": sha,
            "subject": subject,
            "src_files": src_files,
            "diff_lines": line_count,
            "status": "candidate",
            "reason": "",
        })

    # Write candidates.json
    out_path = ab / "candidates.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(candidates, f, indent=2)

    n_cand = sum(1 for c in candidates if c["status"] == "candidate")
    n_excl = len(candidates) - n_cand
    print(f"Wrote {out_path}: {n_cand} candidates, {n_excl} excluded")


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------

def git_apply_reverse(tgt: Path, patch_path: Path) -> tuple[bool, str]:
    """
    Try to reverse-apply patch. Returns (success, reason_if_failed).
    """
    result = subprocess.run(
        ["git", "apply", "--check", "-R", str(patch_path)],
        cwd=str(tgt),
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return False, "code-moved"

    subprocess.run(
        ["git", "apply", "-R", str(patch_path)],
        cwd=str(tgt),
        check=True,
        capture_output=True,
    )
    return True, ""


def git_restore(tgt: Path) -> None:
    """Restore working tree to HEAD."""
    subprocess.run(
        ["git", "checkout", "--", "."],
        cwd=str(tgt),
        capture_output=True,
    )
    subprocess.run(
        ["git", "clean", "-fd"],
        cwd=str(tgt),
        capture_output=True,
    )


def check_tree_clean(tgt: Path) -> bool:
    """Return True if no tracked-file changes (ignores untracked files)."""
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tgt),
        capture_output=True,
        text=True,
    )
    # Only fail on lines that don't start with '??' (untracked)
    tracked_changes = [
        l for l in result.stdout.splitlines()
        if l and not l.startswith("??")
    ]
    return len(tracked_changes) == 0


def check_imports(name: str, src_files: list[str]) -> bool:
    """
    Check that the modules touched by the patch import from targets/<name>/,
    not from another installed copy.
    """
    python = str(venv_python(name))
    tgt = target_dir(name)

    for fpath in src_files:
        # Convert file path to dotted module name
        # e.g. sqlparse/engine/filter.py -> sqlparse.engine.filter
        parts = Path(fpath).with_suffix("").parts
        mod_name = ".".join(parts)
        # Try top-level package
        top_pkg = parts[0]
        script = textwrap.dedent(f"""
import importlib.util, sys, os
spec = importlib.util.find_spec({top_pkg!r})
if spec is None:
    sys.exit(1)
raw = spec.origin or (spec.submodule_search_locations[0] if spec.submodule_search_locations else "")
# Resolve symlinks so we compare real paths
print(os.path.realpath(raw))
""").strip()
        result = subprocess.run(
            [python, "-c", script],
            capture_output=True, text=True,
        )
        if result.returncode != 0:
            return False
        origin = result.stdout.strip()
        # Must be inside targets/<name>/ (resolve symlinks on both sides)
        tgt_real = str(tgt.resolve())
        if not origin.startswith(tgt_real):
            return False
    return True


def rerun_single(name: str, node_ids: list[str], baseline_timeout: int) -> list[str]:
    """
    Re-run a list of test node IDs individually. Return those that still fail.
    """
    python = venv_python(name)
    tgt = target_dir(name)
    plugin_dir = str(PLUGIN_FILE.parent)
    confirmed_failing: list[str] = []

    for nid in node_ids:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
            rpath = Path(tmp.name)

        env = os.environ.copy()
        env["PYTHONDONTWRITEBYTECODE"] = "1"
        env["ANTIBODY_REPORT_PATH"] = str(rpath)
        existing_pp = env.get("PYTHONPATH", "")
        env["PYTHONPATH"] = plugin_dir + (":" + existing_pp if existing_pp else "")

        cmd = [
            str(python), "-m", "pytest",
            "-p", "no:cacheprovider",
            "-p", "antibody_plugin",
            nid,
        ]

        as_limit = 1_500 * 1024 * 1024
        def set_limits():
            try:
                resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
            except Exception:
                pass

        try:
            subprocess.run(
                cmd,
                cwd=str(tgt),
                env=env,
                timeout=baseline_timeout,
                preexec_fn=set_limits,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
        except subprocess.TimeoutExpired:
            rpath.unlink(missing_ok=True)
            continue

        if not rpath.exists() or rpath.stat().st_size == 0:
            rpath.unlink(missing_ok=True)
            continue

        with open(rpath, "r", encoding="utf-8") as f:
            data = json.load(f)
        rpath.unlink(missing_ok=True)

        if data.get("failed"):
            confirmed_failing.extend(data["failed"])

    return confirmed_failing


def cmd_run(args: argparse.Namespace) -> None:
    name: str = args.name
    shas_file: str | None = args.shas

    # Make stdout line-buffered so progress lines appear immediately when redirected.
    sys.stdout.reconfigure(line_buffering=True)

    ab = antibody_dir(name)
    setup_path = ab / "setup.json"
    if not setup_path.exists():
        die(f"No setup.json for '{name}'. Run setup first.")

    candidates_path = ab / "candidates.json"
    if not candidates_path.exists():
        die(f"No candidates.json for '{name}'. Run candidates first.")

    with open(setup_path, "r", encoding="utf-8") as f:
        setup_data = json.load(f)

    with open(candidates_path, "r", encoding="utf-8") as f:
        all_candidates: list[dict] = json.load(f)

    baseline = setup_data["baseline"]
    baseline_collected: int = baseline["collected"]
    baseline_secs: float = baseline["seconds"]
    timeout_secs = max(120, int(10 * baseline_secs))

    tgt = target_dir(name)

    # Filter by --shas if provided
    if shas_file:
        with open(shas_file, "r", encoding="utf-8") as f:
            wanted_shas = {l.strip() for l in f if l.strip()}
        candidates = [c for c in all_candidates if c["sha"] in wanted_shas]
        # Add any shas from file not in candidates
        known = {c["sha"] for c in all_candidates}
        for sha in wanted_shas:
            if sha not in known:
                candidates.append({
                    "sha": sha,
                    "subject": "(unknown)",
                    "src_files": [],
                    "status": "NO DATA:unknown-sha",
                    "reason": "SHA not found in candidates.json",
                })
    else:
        candidates = list(all_candidates)

    # Handle SIGTERM/SIGINT by raising so finally blocks run
    def _handle_signal(sig, frame):
        raise SystemExit(f"Interrupted by signal {sig}")
    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    rows: list[dict] = []
    plugin_dir = str(PLUGIN_FILE.parent)

    with target_lock(name):
        for i, cand in enumerate(candidates):
            sha = cand["sha"]
            subject = cand["subject"]
            src_files = cand.get("src_files", [])
            status = cand.get("status", "candidate")

            # Pass-through excluded rows
            if status.startswith("EXCLUDED:"):
                row = {
                    "sha": sha,
                    "subject": subject,
                    "src_files": src_files,
                    "status": status,
                    "reason": cand.get("reason", ""),
                    "catching_tests": [],
                    "secs": 0.0,
                }
                rows.append(row)
                print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {status}")
                continue

            patch_path = diffs_dir(name) / f"{sha}.patch"
            if not patch_path.exists():
                row = {
                    "sha": sha,
                    "subject": subject,
                    "src_files": src_files,
                    "status": "NO DATA:missing-patch",
                    "reason": "Patch file not found",
                    "catching_tests": [],
                    "secs": 0.0,
                }
                rows.append(row)
                print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  NO DATA:missing-patch")
                continue

            row_status = ""
            row_reason = ""
            catching_tests: list[str] = []
            t0 = time.monotonic()

            applied = False
            try:
                # Apply reverse patch
                ok, fail_reason = git_apply_reverse(tgt, patch_path)
                if not ok:
                    row_status = f"NO DATA:{fail_reason}"
                    row_reason = "git apply -R failed"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                applied = True

                # Check if the patch actually changed anything
                clean_check = subprocess.run(
                    ["git", "diff", "--stat", "HEAD"],
                    cwd=str(tgt), capture_output=True, text=True,
                )
                if not clean_check.stdout.strip():
                    row_status = "NO DATA:no-change"
                    row_reason = "Reverse patch applied but changed nothing"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    applied = False  # nothing to restore
                    continue

                # Check imports
                if not check_imports(name, src_files):
                    row_status = "NO DATA:import-mismatch"
                    row_reason = "Module imports from outside targets/"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                # Run test suite
                try:
                    report = run_suite(name, timeout_secs=timeout_secs)
                except RuntimeError as e:
                    reason_str = str(e)
                    if "timeout" in reason_str:
                        row_status = "CAUGHT:timeout"
                        row_reason = "Test suite timed out"
                    else:
                        row_status = "NO DATA:missing-report"
                        row_reason = "Plugin did not write a report"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                collected = len(report.get("collected", []))
                failed_ids = report.get("failed", [])
                coll_errors = report.get("collection_errors", [])

                if coll_errors:
                    row_status = "CAUGHT:collection"
                    row_reason = f"Collection errors: {coll_errors[:3]}"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                if collected == 0:
                    row_status = "NO DATA:zero-collected"
                    row_reason = "0 tests collected"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                if collected != baseline_collected:
                    row_status = "NO DATA:count-mismatch"
                    row_reason = f"Collected {collected} vs baseline {baseline_collected}"
                    rows.append({
                        "sha": sha, "subject": subject, "src_files": src_files,
                        "status": row_status, "reason": row_reason,
                        "catching_tests": [], "secs": round(time.monotonic() - t0, 2),
                    })
                    print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")
                    continue

                if failed_ids:
                    # Re-run failing tests individually to confirm (not flaky)
                    confirmed = rerun_single(name, failed_ids, timeout_secs)
                    if confirmed:
                        row_status = "CAUGHT"
                        row_reason = ""
                        catching_tests = confirmed
                    else:
                        row_status = "NO DATA:flaky"
                        row_reason = "Failures did not reproduce individually"
                else:
                    # No failures, count matches → ESCAPED
                    row_status = "ESCAPED"
                    row_reason = ""

                rows.append({
                    "sha": sha, "subject": subject, "src_files": src_files,
                    "status": row_status, "reason": row_reason,
                    "catching_tests": catching_tests,
                    "secs": round(time.monotonic() - t0, 2),
                })
                print(f"[{i+1}/{len(candidates)}] {sha[:8]} {subject[:60]}  →  {row_status}")

            finally:
                if applied:
                    git_restore(tgt)
                    if not check_tree_clean(tgt):
                        die(f"Working tree is dirty after restoring {sha}. Stopping.")

    # Verify we have exactly one row per input sha
    if len(rows) != len(candidates):
        print(
            f"ERROR: row count mismatch: {len(rows)} rows for {len(candidates)} candidates",
            file=sys.stderr,
        )
        sys.exit(1)

    results = {
        "repo": setup_data["repo"],
        "head": setup_data["head"],
        "baseline": baseline,
        "rows": rows,
    }

    out_path = ab / "results.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"\nWrote {out_path}")

    # Print status summary
    from collections import Counter
    counter = Counter(r["status"] for r in rows)
    print("\nSummary:")
    for status, count in sorted(counter.items()):
        print(f"  {status}: {count}")


# ---------------------------------------------------------------------------
# Shared patch / coverage helpers used by probe and prove
# ---------------------------------------------------------------------------

def _get_patch_info(name: str, sha: str) -> tuple[Path, list[str]]:
    """
    Return (patch_path, src_files) for a sha, or die.
    Also validates that candidates.json has the sha.
    """
    ab = antibody_dir(name)

    # Try candidates.json first for src_files
    candidates_path = ab / "candidates.json"
    src_files: list[str] = []
    if candidates_path.exists():
        with open(candidates_path, "r", encoding="utf-8") as f:
            all_cands = json.load(f)
        for c in all_cands:
            if c["sha"] == sha or c["sha"].startswith(sha):
                src_files = c.get("src_files", [])
                sha = c["sha"]  # use full sha
                break

    patch_path = diffs_dir(name) / f"{sha}.patch"
    if not patch_path.exists():
        die(f"No patch found at {patch_path}. Run candidates first.")

    # If src_files not from candidates, derive from git
    if not src_files:
        tgt = target_dir(name)
        all_files = get_commit_files(tgt, sha)
        src_files = [f for f in all_files if is_source_file(f)]

    return patch_path, src_files


def _changed_lines(name: str, sha: str, src_files: list[str]) -> dict[str, set[int]]:
    """
    Return {filename: set_of_line_numbers} for lines added by this commit
    (i.e. the lines present in the fix that aren't in the bug).
    Line numbers are those in the post-patch (HEAD) file.
    """
    tgt = target_dir(name)
    diff_text = get_commit_diff(tgt, sha, src_files)
    result: dict[str, set[int]] = {}
    current_file: str | None = None
    new_lineno = 0

    for line in diff_text.splitlines():
        if line.startswith("+++ b/"):
            current_file = line[6:]
            result.setdefault(current_file, set())
        elif line.startswith("@@ "):
            # @@ -old_start,old_count +new_start,new_count @@
            m = re.search(r"\+(\d+)", line)
            if m:
                new_lineno = int(m.group(1)) - 1
        elif current_file is not None:
            if line.startswith("+") and not line.startswith("+++"):
                new_lineno += 1
                result[current_file].add(new_lineno)
            elif line.startswith("-") and not line.startswith("---"):
                pass  # removed line, no new_lineno increment
            else:
                new_lineno += 1

    return result


def _is_data_only_change(name: str, sha: str, src_files: list[str]) -> bool:
    """
    Return True if every changed line is at module level (outside any function
    or class body), i.e. the patch only edits module-level tables or constants.
    """
    tgt = target_dir(name)
    changed = _changed_lines(name, sha, src_files)
    if not changed:
        return False

    for fpath, linenos in changed.items():
        if not linenos:
            continue
        # Read the file at HEAD
        full_path = tgt / fpath
        if not full_path.exists():
            return False
        src = full_path.read_text(encoding="utf-8", errors="replace")
        try:
            tree = ast.parse(src)
        except SyntaxError:
            return False

        # Collect line ranges of all function/class bodies
        def_ranges: list[tuple[int, int]] = []
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                start = node.lineno
                end = node.end_lineno or start
                def_ranges.append((start, end))

        for lineno in linenos:
            inside = any(s <= lineno <= e for s, e in def_ranges)
            if inside:
                return False  # at least one changed line is inside a def/class

    return True


def _run_check_script(python: Path, check_file: Path, cwd: Path,
                      extra_env: dict | None = None, timeout: int = 30) -> str | None:
    """
    Run check_file as a script, capturing stdout+stderr combined.
    Returns the combined output string, or None on timeout.
    """
    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    if extra_env:
        env.update(extra_env)

    try:
        proc = subprocess.run(
            [str(python), str(check_file)],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # Return stdout; if empty use stderr (some bugs print to stderr)
        out = proc.stdout
        return out
    except subprocess.TimeoutExpired:
        return None


def _run_check_with_coverage(
    python: Path,
    check_file: Path,
    cwd: Path,
    changed_lines: dict[str, set[int]],
    timeout: int = 30,
) -> tuple[str | None, bool]:
    """
    Run check_file under coverage (or sys.settrace fallback).
    Returns (output, hit_changed_line).
    output is None on timeout.
    """
    # Build a small wrapper that uses sys.settrace to track covered lines
    # relative to the target package files.
    # We pass the changed_lines info via a temp JSON file.
    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", delete=False
    ) as tf:
        json.dump(
            {k: list(v) for k, v in changed_lines.items()},
            tf,
        )
        changed_json = tf.name

    with tempfile.NamedTemporaryFile(
        suffix=".json", mode="w", delete=False
    ) as tf:
        hit_json = tf.name

    wrapper = textwrap.dedent(f"""
import sys, json, os, runpy

_changed_json = {changed_json!r}
_hit_json = {hit_json!r}

with open(_changed_json) as _f:
    _changed = {{k: set(v) for k, v in json.load(_f).items()}}

_hit = False

def _tracer(frame, event, arg):
    global _hit
    if not _hit and event in ('line', 'call'):
        fname = frame.f_code.co_filename
        # normalise to relative path matching the keys in _changed
        for rel, lines in _changed.items():
            if fname.endswith(os.sep + rel) or fname.endswith('/' + rel):
                if frame.f_lineno in lines:
                    _hit = True
    return _tracer

sys.settrace(_tracer)
try:
    runpy.run_path({str(check_file)!r}, run_name='__main__')
finally:
    sys.settrace(None)
    with open(_hit_json, 'w') as _f:
        json.dump({{'hit': _hit}}, _f)
""").strip()

    with tempfile.NamedTemporaryFile(
        suffix=".py", mode="w", delete=False
    ) as wf:
        wf.write(wrapper)
        wrapper_path = wf.name

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"

    output: str | None = None
    hit = False
    try:
        proc = subprocess.run(
            [str(python), wrapper_path],
            cwd=str(cwd),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        output = proc.stdout
        if os.path.exists(hit_json) and os.path.getsize(hit_json) > 0:
            with open(hit_json) as f:
                data = json.load(f)
            hit = bool(data.get("hit", False))
    except subprocess.TimeoutExpired:
        output = None
    finally:
        for p in (changed_json, hit_json, wrapper_path):
            try:
                os.unlink(p)
            except OSError:
                pass

    return output, hit


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------

def cmd_probe(args: argparse.Namespace) -> None:
    name: str = args.name
    sha: str = args.sha
    check_file_arg: str = args.check_file

    sys.stdout.reconfigure(line_buffering=True)

    check_file = Path(check_file_arg).resolve()
    if not check_file.exists():
        die(f"Check file not found: {check_file}")

    with target_lock(name):
        _do_probe(name, sha, check_file)


def _do_probe(name: str, sha: str, check_file: Path) -> None:
    ab = antibody_dir(name)
    tgt = target_dir(name)
    python = venv_python(name)

    patch_path, src_files = _get_patch_info(name, sha)

    # Resolve full sha if abbreviated
    result = subprocess.run(
        ["git", "rev-parse", sha],
        cwd=str(tgt), capture_output=True, text=True,
    )
    if result.returncode == 0:
        sha = result.stdout.strip()

    probes_dir = ab / "probes"
    probes_dir.mkdir(parents=True, exist_ok=True)
    out_path = probes_dir / f"{sha}.json"

    # Determine changed lines (for coverage check)
    changed = _changed_lines(name, sha, src_files)

    def run_check(with_fix: bool) -> str | None:
        """Run check twice and return output if both agree, else None (non-deterministic)."""
        env_extra: dict = {}
        outputs: list[str] = []
        for _ in range(2):
            out = _run_check_script(python, check_file, tgt, extra_env=env_extra)
            if out is None:
                return None  # timeout
            outputs.append(out)
        if outputs[0] != outputs[1]:
            return None  # not deterministic
        return outputs[0]

    # Run with fix in (HEAD as-is)
    if not check_tree_clean(tgt):
        die(f"Working tree has tracked changes before probe — ensure it is clean first.")

    output_with_fix = run_check(with_fix=True)
    if output_with_fix is None:
        verdict = "INVALID (not deterministic)"
        result_data = {
            "verdict": verdict,
            "check": str(check_file),
            "output_with_fix": None,
            "output_without_fix": None,
        }
        print(verdict)
        with open(out_path, "w") as f:
            json.dump(result_data, f, indent=2)
        return

    if output_with_fix.strip() == "":
        verdict = "INVALID (prints nothing)"
        result_data = {
            "verdict": verdict,
            "check": str(check_file),
            "output_with_fix": output_with_fix,
            "output_without_fix": None,
        }
        print(verdict)
        with open(out_path, "w") as f:
            json.dump(result_data, f, indent=2)
        return

    # Now apply reverse patch (put the bug back)
    applied = False
    output_without_fix: str | None = None
    try:
        ok, fail_reason = git_apply_reverse(tgt, patch_path)
        if not ok:
            verdict = f"INVALID (patch did not apply: {fail_reason})"
            result_data = {
                "verdict": verdict,
                "check": str(check_file),
                "output_with_fix": output_with_fix,
                "output_without_fix": None,
            }
            print(verdict)
            with open(out_path, "w") as f:
                json.dump(result_data, f, indent=2)
            return

        applied = True

        output_without_fix = run_check(with_fix=False)
        if output_without_fix is None:
            verdict = "INVALID (not deterministic)"
            result_data = {
                "verdict": verdict,
                "check": str(check_file),
                "output_with_fix": output_with_fix,
                "output_without_fix": None,
            }
            print(verdict)
            with open(out_path, "w") as f:
                json.dump(result_data, f, indent=2)
            return

        if output_without_fix.strip() == "":
            verdict = "INVALID (prints nothing)"
            result_data = {
                "verdict": verdict,
                "check": str(check_file),
                "output_with_fix": output_with_fix,
                "output_without_fix": output_without_fix,
            }
            print(verdict)
            with open(out_path, "w") as f:
                json.dump(result_data, f, indent=2)
            return

    finally:
        if applied:
            git_restore(tgt)
            if not check_tree_clean(tgt):
                die(f"Working tree is dirty after restoring {sha}. Stopping.")

    # Coverage check: does the script ever execute a changed line?
    # We do this once with the fix in (HEAD), using sys.settrace.
    _, hit_changed = _run_check_with_coverage(python, check_file, tgt, changed)

    if not hit_changed:
        verdict = "INVALID (never reaches the changed code)"
        result_data = {
            "verdict": verdict,
            "check": str(check_file),
            "output_with_fix": output_with_fix,
            "output_without_fix": output_without_fix,
        }
        print(verdict)
        with open(out_path, "w") as f:
            json.dump(result_data, f, indent=2)
        return

    # Data-only change: all changed lines are at module level
    data_only = _is_data_only_change(name, sha, src_files)

    if output_with_fix == output_without_fix:
        if data_only:
            verdict = "INCONCLUSIVE (data-only change)"
        else:
            verdict = "NO CHANGE FOUND"
    else:
        verdict = "CHANGED"

    result_data = {
        "verdict": verdict,
        "check": str(check_file),
        "output_with_fix": output_with_fix,
        "output_without_fix": output_without_fix,
    }
    print(verdict)
    with open(out_path, "w") as f:
        json.dump(result_data, f, indent=2)
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# prove
# ---------------------------------------------------------------------------

# Pattern: reads from the package source
_READS_SOURCE_RE = re.compile(
    r"inspect\.getsource"
    r"|open\s*\(",
    re.MULTILINE,
)


def _test_reads_package_source(test_path: Path, tgt: Path) -> bool:
    """
    Return True if the test file appears to read the package's source files.
    Heuristic: contains inspect.getsource or an open() call that could
    open a file inside the package directory.
    """
    src = test_path.read_text(encoding="utf-8", errors="replace")
    if "inspect.getsource" in src:
        return True
    # Check for open() calls targeting package paths
    # We look for string literals that look like package paths
    if "open(" in src:
        # Find any string that contains a package directory component
        pkg_names = {p.name for p in tgt.iterdir() if (tgt / p).is_dir() and (tgt / p / "__init__.py").exists()}
        pkg_names.discard("tests")
        for pkg in pkg_names:
            # If a string containing the package name appears near an open( call
            if re.search(r'open\s*\([^)]*' + re.escape(pkg) + r'[^)]*\)', src):
                return True
    return False


def _run_pytest_single(
    name: str,
    test_node: str,
    extra_args: list[str] | None = None,
    timeout_secs: int = 60,
) -> dict:
    """
    Run a single test (or test file) and return the report dict.
    Raises RuntimeError on timeout or missing report.
    """
    python = venv_python(name)
    tgt = target_dir(name)
    plugin_dir = str(PLUGIN_FILE.parent)

    with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as tmp:
        rpath = Path(tmp.name)

    env = os.environ.copy()
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env["ANTIBODY_REPORT_PATH"] = str(rpath)
    existing_pp = env.get("PYTHONPATH", "")
    env["PYTHONPATH"] = plugin_dir + (":" + existing_pp if existing_pp else "")

    cmd = [
        str(python), "-m", "pytest",
        "-p", "no:cacheprovider",
        "-p", "antibody_plugin",
        "-v",
        test_node,
    ]
    if extra_args:
        cmd.extend(extra_args)

    as_limit = 1_500 * 1024 * 1024
    def set_limits():
        try:
            resource.setrlimit(resource.RLIMIT_AS, (as_limit, as_limit))
        except Exception:
            pass

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(tgt),
            env=env,
            timeout=timeout_secs,
            preexec_fn=set_limits,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        output = proc.stdout.decode(errors="replace") + proc.stderr.decode(errors="replace")
    except subprocess.TimeoutExpired:
        rpath.unlink(missing_ok=True)
        raise RuntimeError("timeout")

    if not rpath.exists() or rpath.stat().st_size == 0:
        rpath.unlink(missing_ok=True)
        raise RuntimeError(f"missing-report\n{output}")

    with open(rpath, "r", encoding="utf-8") as f:
        data = json.load(f)
    rpath.unlink(missing_ok=True)
    data["_output"] = output
    return data


def _is_allowed_failure(failed_node_id: str, output: str) -> bool:
    """
    Return True if a test failure is an AssertionError or pytest's "DID NOT RAISE".
    ImportError, AttributeError, NameError, or TypeError about missing symbols
    do NOT count.
    """
    # Look for the failure reason in the output
    # The output from -v will have FAILED lines and tracebacks.
    # We scan for the relevant exceptions.
    bad_patterns = [
        r"ImportError",
        r"AttributeError",
        r"NameError",
        r"TypeError.*object is not callable",
        r"TypeError.*takes \d+ positional argument",
        r"TypeError.*missing \d+ required",
        r"TypeError.*unexpected keyword argument",
        r"has no attribute",
        r"cannot import name",
        r"No module named",
    ]
    # Extract the section for this test
    # Look for the test's section in the output
    test_name = failed_node_id.split("::")[-1]
    for pat in bad_patterns:
        if re.search(pat, output, re.IGNORECASE):
            return False
    # Positive patterns
    good_patterns = [
        r"AssertionError",
        r"DID NOT RAISE",
        r"assert ",
    ]
    for pat in good_patterns:
        if re.search(pat, output, re.IGNORECASE):
            return True
    # If we can't tell, be conservative
    return False


def cmd_prove(args: argparse.Namespace) -> None:
    name: str = args.name
    sha: str = args.sha
    test_arg: str = args.test

    sys.stdout.reconfigure(line_buffering=True)

    with target_lock(name):
        _do_prove(name, sha, test_arg)


def _do_prove(name: str, sha: str, test_arg: str) -> None:
    ab = antibody_dir(name)
    tgt = target_dir(name)
    python = venv_python(name)

    patch_path, src_files = _get_patch_info(name, sha)

    # Resolve full sha if abbreviated
    result = subprocess.run(
        ["git", "rev-parse", sha],
        cwd=str(tgt), capture_output=True, text=True,
    )
    if result.returncode == 0:
        sha = result.stdout.strip()

    proofs_dir = ab / "proofs"
    proofs_dir.mkdir(parents=True, exist_ok=True)
    out_path = proofs_dir / f"{sha}.json"

    # Resolve test path: could be a file path or node ID
    # If it contains "::", treat as node ID; otherwise as a file path
    test_node = test_arg  # use as-is for pytest
    test_file: Path | None = None
    if "::" in test_arg:
        test_file = tgt / test_arg.split("::")[0]
    else:
        test_file = Path(test_arg) if Path(test_arg).is_absolute() else tgt / test_arg
        if not test_file.exists():
            # Try relative to cwd
            test_file = Path(test_arg).resolve()

    # Reject test that reads the package source
    if test_file and test_file.exists():
        if _test_reads_package_source(test_file, tgt):
            msg = "NOT PROVEN: test reads package source"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "step": "rejected: test reads package source",
                    "runs_with_fix": [],
                    "runs_without_fix": [],
                    "failure_messages": [],
                }, f, indent=2)
            return

    # Find other test files to --ignore (from tests/antibody/ only)
    tests_dir = tgt / "tests" / "antibody"
    ignore_args: list[str] = []
    if tests_dir.exists() and test_file:
        for tf in tests_dir.iterdir():
            if tf.suffix == ".py" and tf.resolve() != test_file.resolve():
                ignore_args.extend(["--ignore", str(tf)])

    # -------------------------
    # Step 1: Run with fix (HEAD), 3 times — must collect and pass all 3
    # -------------------------
    runs_with_fix: list[dict] = []
    for run_i in range(3):
        try:
            report = _run_pytest_single(name, test_node, extra_args=ignore_args)
        except RuntimeError as e:
            msg = f"NOT PROVEN: step 1\n{e}"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "step": f"step 1 run {run_i+1}: {e}",
                    "runs_with_fix": runs_with_fix,
                    "runs_without_fix": [],
                    "failure_messages": [],
                }, f, indent=2)
            return

        runs_with_fix.append({
            "run": run_i + 1,
            "collected": len(report.get("collected", [])),
            "failed": report.get("failed", []),
            "output": report.get("_output", ""),
        })

        # Check: collected > 0
        if len(report.get("collected", [])) == 0:
            msg = f"NOT PROVEN: step 1\nRun {run_i+1}: test not collected"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "step": f"step 1 run {run_i+1}: not collected",
                    "runs_with_fix": runs_with_fix,
                    "runs_without_fix": [],
                    "failure_messages": [],
                }, f, indent=2)
            return

        # Check: no failures
        if report.get("failed"):
            msg = f"NOT PROVEN: step 1\nRun {run_i+1}: test failed with fix in"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "step": f"step 1 run {run_i+1}: test failed with fix in",
                    "runs_with_fix": runs_with_fix,
                    "runs_without_fix": [],
                    "failure_messages": [],
                }, f, indent=2)
            return

    # -------------------------
    # Step 2: Apply reverse patch (bug back), run 3 times — must fail all 3
    # with only AssertionError or DID NOT RAISE
    # -------------------------
    applied = False
    runs_without_fix: list[dict] = []
    failure_messages: list[str] = []
    try:
        ok, fail_reason = git_apply_reverse(tgt, patch_path)
        if not ok:
            msg = f"NOT PROVEN: step 2\ngit apply -R failed: {fail_reason}"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "step": f"step 2: patch did not apply ({fail_reason})",
                    "runs_with_fix": runs_with_fix,
                    "runs_without_fix": [],
                    "failure_messages": [],
                }, f, indent=2)
            return

        applied = True

        for run_i in range(3):
            try:
                report = _run_pytest_single(name, test_node, extra_args=ignore_args)
            except RuntimeError as e:
                msg = f"NOT PROVEN: step 2\n{e}"
                print(msg)
                with open(out_path, "w") as f:
                    json.dump({
                        "verdict": "NOT PROVEN",
                        "step": f"step 2 run {run_i+1}: {e}",
                        "runs_with_fix": runs_with_fix,
                        "runs_without_fix": runs_without_fix,
                        "failure_messages": failure_messages,
                    }, f, indent=2)
                return

            output = report.get("_output", "")
            failed = report.get("failed", [])

            runs_without_fix.append({
                "run": run_i + 1,
                "collected": len(report.get("collected", [])),
                "failed": failed,
                "output": output,
            })

            # Check: collection errors → not collected properly
            coll_errors = report.get("collection_errors", [])
            if coll_errors:
                msg = f"NOT PROVEN: step 2\nRun {run_i+1}: collection error with bug back"
                print(msg)
                with open(out_path, "w") as f:
                    json.dump({
                        "verdict": "NOT PROVEN",
                        "step": f"step 2 run {run_i+1}: collection error",
                        "runs_with_fix": runs_with_fix,
                        "runs_without_fix": runs_without_fix,
                        "failure_messages": failure_messages,
                    }, f, indent=2)
                return

            # Must fail
            if not failed:
                msg = f"NOT PROVEN: step 2\nRun {run_i+1}: test still passes with bug back"
                print(msg)
                with open(out_path, "w") as f:
                    json.dump({
                        "verdict": "NOT PROVEN",
                        "step": f"step 2 run {run_i+1}: test still passes with bug back",
                        "runs_with_fix": runs_with_fix,
                        "runs_without_fix": runs_without_fix,
                        "failure_messages": failure_messages,
                    }, f, indent=2)
                return

            # All failures must be AssertionError or DID NOT RAISE
            for fnode in failed:
                if not _is_allowed_failure(fnode, output):
                    msg = f"NOT PROVEN: step 2\nRun {run_i+1}: failure is not AssertionError/DID NOT RAISE"
                    print(msg)
                    print(output[:2000])
                    with open(out_path, "w") as f:
                        json.dump({
                            "verdict": "NOT PROVEN",
                            "step": f"step 2 run {run_i+1}: failure not AssertionError (got disallowed exception)",
                            "runs_with_fix": runs_with_fix,
                            "runs_without_fix": runs_without_fix,
                            "failure_messages": failure_messages,
                        }, f, indent=2)
                    return

            failure_messages.append(output)

    finally:
        if applied:
            git_restore(tgt)
            if not check_tree_clean(tgt):
                die(f"Working tree is dirty after restoring {sha}. Stopping.")

    # -------------------------
    # Step 3: git status is clean except tests/antibody/
    # -------------------------
    status_result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tgt), capture_output=True, text=True,
    )
    dirty_lines = [
        l for l in status_result.stdout.splitlines()
        # ignore untracked files (?? prefix) — editable installs leave .egg-info etc.
        # also allow untracked tests/antibody/ files (the test being proved)
        if l.strip() and not l.startswith("??")
    ]
    if dirty_lines:
        msg = f"NOT PROVEN: step 3\ngit status shows unexpected dirty files:\n" + "\n".join(dirty_lines)
        print(msg)
        with open(out_path, "w") as f:
            json.dump({
                "verdict": "NOT PROVEN",
                "step": "step 3: working tree dirty after restore",
                "runs_with_fix": runs_with_fix,
                "runs_without_fix": runs_without_fix,
                "failure_messages": failure_messages,
            }, f, indent=2)
        return

    # -------------------------
    # Step 4: full suite at HEAD (with this test) still green
    # -------------------------
    full_ignore_args: list[str] = []
    if tests_dir.exists() and test_file:
        for tf in tests_dir.iterdir():
            if tf.suffix == ".py" and tf.resolve() != test_file.resolve():
                full_ignore_args.extend(["--ignore", str(tf)])

    try:
        full_report = _run_pytest_single(
            name,
            test_node,
            extra_args=full_ignore_args,
            timeout_secs=300,
        )
    except RuntimeError as e:
        msg = f"NOT PROVEN: step 4\n{e}"
        print(msg)
        with open(out_path, "w") as f:
            json.dump({
                "verdict": "NOT PROVEN",
                "step": f"step 4: {e}",
                "runs_with_fix": runs_with_fix,
                "runs_without_fix": runs_without_fix,
                "failure_messages": failure_messages,
            }, f, indent=2)
        return

    full_failed = full_report.get("failed", [])
    if full_failed:
        msg = f"NOT PROVEN: step 4\nFull suite has failures: {full_failed[:5]}"
        print(msg)
        with open(out_path, "w") as f:
            json.dump({
                "verdict": "NOT PROVEN",
                "step": f"step 4: full suite has failures",
                "runs_with_fix": runs_with_fix,
                "runs_without_fix": runs_without_fix,
                "failure_messages": failure_messages,
            }, f, indent=2)
        return

    # All steps passed
    print("PROVEN")
    with open(out_path, "w") as f:
        json.dump({
            "verdict": "PROVEN",
            "step": None,
            "runs_with_fix": runs_with_fix,
            "runs_without_fix": runs_without_fix,
            "failure_messages": failure_messages,
        }, f, indent=2)
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        prog="antibody",
        description="Antibody — find bug fixes that could silently recur.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # setup
    p_setup = sub.add_parser("setup", help="Clone target and run baseline")
    p_setup.add_argument("name", help="Target name (e.g. sqlparse-ref)")
    p_setup.add_argument("git_url", help="Git URL to clone")
    p_setup.add_argument("--rev", default=None, help="Commit SHA or ref to check out")
    p_setup.add_argument("--deps", nargs="*", default=[], help="Extra pip packages to install")

    # candidates
    p_cand = sub.add_parser("candidates", help="Find fix candidates in commit history")
    p_cand.add_argument("name", help="Target name")
    p_cand.add_argument("--since", default="2021-01-01", help="Only commits since this date (YYYY-MM-DD), default 2021-01-01")

    # run
    p_run = sub.add_parser("run", help="Run regression tests for candidates")
    p_run.add_argument("name", help="Target name")
    p_run.add_argument("--shas", default=None, dest="shas", help="File with SHAs to run (one per line)")

    # probe
    p_probe = sub.add_parser("probe", help="Probe a check script against a fix")
    p_probe.add_argument("name", help="Target name")
    p_probe.add_argument("sha", help="Commit SHA of the fix")
    p_probe.add_argument("check_file", help="Path to the check script")

    # prove
    p_prove = sub.add_parser("prove", help="Prove a regression test against a fix")
    p_prove.add_argument("name", help="Target name")
    p_prove.add_argument("sha", help="Commit SHA of the fix")
    p_prove.add_argument("test", help="Test file path or pytest node ID")

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "candidates":
        cmd_candidates(args)
    elif args.command == "run":
        cmd_run(args)
    elif args.command == "probe":
        cmd_probe(args)
    elif args.command == "prove":
        cmd_prove(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
