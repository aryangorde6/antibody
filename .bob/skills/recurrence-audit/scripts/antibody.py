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

    skipped = report.get("skipped", 0)
    xfailed = report.get("xfailed", 0)
    xpassed = report.get("xpassed", 0)
    # passed + skipped + xfailed + xpassed must equal collected.
    # The plugin counts each test exactly once across these four buckets.
    passed = collected - len(failed) - skipped - xfailed - xpassed

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


# Git trailers that may contain person names or emails: any "<Word>-by:" key
# (Signed-off-by and the rest) and "Cc:". Matched case-insensitively.
_PERSON_TRAILER_RE = re.compile(
    r"^([A-Za-z]+(?:-[A-Za-z]+)*-by|Cc)\s*:",
    re.IGNORECASE,
)

# Inline trailer pattern: a trailer key appearing *anywhere* in the line
# (e.g. "fix: blahSigned-off-by: Name <email>").
_INLINE_TRAILER_RE = re.compile(
    r"([A-Za-z]+(?:-[A-Za-z]+)*-by|(?<![A-Za-z])Cc)\s*:",
    re.IGNORECASE,
)


def _clean_subject(raw_subject: str) -> str:
    """
    Return only the first line of a commit subject, stripping any
    Signed-off-by and other "<Word>-by:" trailers that may have been
    appended when git's %s format captured the whole first paragraph.
    Also cuts the subject at the point where an inline trailer starts,
    and removes anything shaped like an email address.
    """
    # Split on newlines; take only the first non-empty line.
    lines = raw_subject.splitlines()
    first_line = lines[0].strip() if lines else raw_subject.strip()
    # Guard: if the subject itself starts with a trailer, return empty
    if _PERSON_TRAILER_RE.match(first_line):
        return ""
    # Cut at any inline trailer (e.g. "fix: blahSigned-off-by: Name <email>")
    m = _INLINE_TRAILER_RE.search(first_line)
    if m:
        first_line = first_line[:m.start()].strip()
    # Strip anything shaped like an email address
    first_line = _EMAIL_RE.sub("", first_line).strip()
    return first_line


def get_commit_log(tgt: Path, since: str | None) -> list[dict]:
    """
    Return list of {sha, subject, body} for non-merge commits since `since`.
    Uses record separator \x1e (ASCII record separator) which doesn't appear in git output.
    """
    sep = "\x1e"  # ASCII record separator — safe in subprocess args
    rec_end = "\x1f"  # ASCII unit separator — marks end of one commit record
    # Use %B (raw body = subject + blank line + body) so we always get the full
    # first paragraph when git's %s would join it.  We derive subject ourselves.
    fmt = f"%H{sep}%B{rec_end}"
    cmd = ["git", "log", "--no-merges", f"--format={fmt}"]
    if since:
        cmd.append(f"--since={since}")
    result = run(cmd, cwd=tgt, capture=True)

    commits = []
    for block in result.stdout.split(rec_end):
        block = block.strip()
        if not block:
            continue
        parts = block.split(sep, 1)
        if len(parts) < 2:
            continue
        sha = parts[0].strip()
        raw_body = parts[1]  # full commit message (subject + body)

        # Derive subject: first non-empty line of the message.
        msg_lines = raw_body.splitlines()
        subject_line = ""
        for ln in msg_lines:
            stripped = ln.strip()
            if stripped:
                subject_line = stripped
                break

        subject = _clean_subject(subject_line)
        if not subject:
            continue  # skip commits with no usable subject

        # Build body: lines after the first non-empty line,
        # dropping any trailer lines that name a person.
        after_subject = msg_lines[msg_lines.index(msg_lines[0]) + 1:] if msg_lines else []
        # Find the index of the subject line in the original list
        subj_idx = 0
        for idx, ln in enumerate(msg_lines):
            if ln.strip() == subject_line.strip():
                subj_idx = idx
                break
        body_lines = [
            ln for ln in msg_lines[subj_idx + 1:]
            if not _PERSON_TRAILER_RE.match(ln.strip())
        ]
        body = "\n".join(body_lines).strip()

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
    # Keep tests/antibody/: the new tests being proved live there, untracked.
    subprocess.run(
        ["git", "clean", "-fd", "-e", "tests/antibody/"],
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
            "check": _rel_to_root(check_file),
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
            "check": _rel_to_root(check_file),
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
                "check": _rel_to_root(check_file),
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
                "check": _rel_to_root(check_file),
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
                "check": _rel_to_root(check_file),
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
            "check": _rel_to_root(check_file),
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
        "check": _rel_to_root(check_file),
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

    # Resolve test path: a file path or node ID, relative to the target
    # (tests/antibody/test_x.py) or to the repository root, as the skill
    # passes it (targets/<name>/tests/antibody/test_x.py). pytest runs inside
    # the target, so it gets the path relative to the target.
    file_part, sep, node_rest = test_arg.partition("::")
    test_file: Path | None = Path(file_part)
    if not test_file.is_absolute():
        test_file = next(
            (p for p in (tgt / file_part, ROOT / file_part) if p.exists()),
            Path(file_part).resolve(),
        )
    try:
        test_node = str(test_file.resolve().relative_to(tgt.resolve())) + sep + node_rest
    except ValueError:
        test_node = test_arg

    # Reject test that reads the package source
    if test_file and test_file.exists():
        if _test_reads_package_source(test_file, tgt):
            msg = "NOT PROVEN: test reads package source"
            print(msg)
            with open(out_path, "w") as f:
                json.dump({
                    "verdict": "NOT PROVEN",
                    "test": test_node,
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
                    "test": test_node,
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
                    "test": test_node,
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
                    "test": test_node,
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
                    "test": test_node,
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
                        "test": test_node,
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
                        "test": test_node,
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
                        "test": test_node,
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
                            "test": test_node,
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
                "test": test_node,
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
        full_report = run_suite(
            name,
            extra_args=full_ignore_args,
            timeout_secs=300,
        )
    except RuntimeError as e:
        msg = f"NOT PROVEN: step 4\n{e}"
        print(msg)
        with open(out_path, "w") as f:
            json.dump({
                "verdict": "NOT PROVEN",
                "test": test_node,
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
                "test": test_node,
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
            "test": test_node,
            "step": None,
            "runs_with_fix": runs_with_fix,
            "runs_without_fix": runs_without_fix,
            "failure_messages": failure_messages,
        }, f, indent=2)
    print(f"Wrote {out_path}")


# ---------------------------------------------------------------------------
# ledger helpers
# ---------------------------------------------------------------------------

import hashlib
import datetime
import html as _html_mod
import shutil


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def _isodate() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="seconds")


def _rel_to_root(path) -> str:
    """A path under the repository root, written relative to it, so no machine's home
    directory ends up in probe files, the ledger or the page."""
    if not path:
        return ""
    try:
        return str(Path(path).resolve().relative_to(ROOT.resolve()))
    except (ValueError, OSError):
        return str(path)


def _is_not_a_bug(entry: dict | None) -> bool:
    """True if a curated.json entry's verdict is NOT-A-BUG (the pinned
    format); "NOT A BUG" is accepted too."""
    if not isinstance(entry, dict):
        return False
    return str(entry.get("verdict", "")).strip().upper().replace("-", " ") == "NOT A BUG"


def _map_status(row: dict, proofs_dir: Path | None, probes_dir: Path | None,
                curated: dict, explanations: dict) -> dict:
    """
    Return ledger row fields for one results row.
    curated: {sha: {verdict, reason, link}}
    explanations: {sha: "one line"}
    """
    sha = row["sha"]
    raw_status = row.get("status", "")
    subject = row.get("subject", "")
    catching_tests = row.get("catching_tests", [])
    reason = row.get("reason", "")

    line = explanations.get(sha, subject)

    # Curated override
    if sha in curated:
        c = curated[sha]
        if _is_not_a_bug(c):
            return {
                "sha": sha,
                "status": "NOT A BUG",
                "line": line,
                "reason": c.get("reason", ""),
                "link": c.get("link", ""),
                "catching_tests": catching_tests,
                "proof_code": None,
                "probe_output": None,
            }

    if raw_status == "CAUGHT":
        proof_code = None
        if proofs_dir:
            proof_path = proofs_dir / f"{sha}.json"
            if proof_path.exists():
                try:
                    proof_data = json.loads(proof_path.read_text())
                    runs = proof_data.get("runs_with_fix", [])
                    if runs:
                        proof_code = runs[0].get("output", "")
                except Exception:
                    pass
        return {
            "sha": sha,
            "status": "CAUGHT",
            "line": line,
            "reason": reason,
            "link": "",
            "catching_tests": catching_tests,
            "proof_code": proof_code,
            "probe_output": None,
        }

    if raw_status == "ESCAPED":
        probe_output = None
        probe_verdict = None
        if probes_dir:
            probe_path = probes_dir / f"{sha}.json"
            if probe_path.exists():
                try:
                    probe_data = json.loads(probe_path.read_text())
                    probe_verdict = probe_data.get("verdict", "")
                    probe_output = probe_data
                except Exception:
                    pass

        if probe_verdict == "NO CHANGE FOUND":
            out_w = probe_output.get("output_with_fix", "")
            out_wo = probe_output.get("output_without_fix", "")
            check = _rel_to_root(probe_output.get("check", ""))
            return {
                "sha": sha,
                "status": "NO CHANGE FOUND",
                "line": line,
                "reason": f"check: {check}",
                "link": "",
                "catching_tests": [],
                "proof_code": None,
                "probe_output": {"check": check, "with_fix": out_w, "without_fix": out_wo},
            }
        elif probe_verdict == "CHANGED":
            out_w = probe_output.get("output_with_fix", "")
            out_wo = probe_output.get("output_without_fix", "")
            check = _rel_to_root(probe_output.get("check", ""))
            return {
                "sha": sha,
                "status": "STILL EXPOSED",
                "line": line,
                "reason": "probe shows change; no test catches it",
                "link": "",
                "catching_tests": [],
                "proof_code": None,
                "probe_output": {"check": check, "with_fix": out_w, "without_fix": out_wo},
            }
        else:
            probe_note = probe_verdict or ""
            return {
                "sha": sha,
                "status": "STILL EXPOSED",
                "line": line,
                "reason": probe_note if probe_note else "no test catches it",
                "link": "",
                "catching_tests": [],
                "proof_code": None,
                "probe_output": None,
            }

    if raw_status.startswith("NO DATA"):
        detail = raw_status[len("NO DATA:"):] if ":" in raw_status else ""
        return {
            "sha": sha,
            "status": "NO DATA",
            "line": line,
            "reason": f"{detail}: {reason}" if detail else reason,
            "link": "",
            "catching_tests": [],
            "proof_code": None,
            "probe_output": None,
        }

    if raw_status.startswith("EXCLUDED"):
        detail = raw_status[len("EXCLUDED:"):] if ":" in raw_status else ""
        return {
            "sha": sha,
            "status": "EXCLUDED",
            "line": line,
            "reason": f"{detail}: {reason}" if detail else reason,
            "link": "",
            "catching_tests": [],
            "proof_code": None,
            "probe_output": None,
        }

    # Fallback
    return {
        "sha": sha,
        "status": raw_status,
        "line": line,
        "reason": reason,
        "link": "",
        "catching_tests": catching_tests,
        "proof_code": None,
        "probe_output": None,
    }


_HTML_PAGE_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antibody &#8212; {name}</title>
<style>
:root {{
  --bg: #fff;
  --surface: #f7f8fa;
  --border: #e5e7eb;
  --text: #1f2328;
  --muted: #57606a;
  --accent: #3b82d4;
  --green: #1a7f37;
  --red: #cf222e;
  --orange: #9a6700;
  --purple: #7c5cd8;
  --font: -apple-system, "Segoe UI", system-ui, sans-serif;
}}
@media (prefers-color-scheme: dark) {{
  :root {{
    --bg: #0d1117;
    --surface: #161b22;
    --border: #30363d;
    --text: #e6edf3;
    --muted: #8b949e;
    --accent: #58a6ff;
    --green: #3fb950;
    --red: #f85149;
    --orange: #d29922;
    --purple: #bc8cff;
  }}
}}
*, *::before, *::after {{ box-sizing: border-box; margin: 0; padding: 0; }}
body {{
  font-family: var(--font);
  font-size: 15px;
  line-height: 1.6;
  color: var(--text);
  background: var(--bg);
  padding: 0 1rem 3rem;
  max-width: 900px;
  margin: 0 auto;
}}
a {{ color: var(--accent); }}
h1 {{ font-size: 1.3rem; font-weight: 700; margin: 1.5rem 0 0.25rem; }}
h2 {{ font-size: 1rem; font-weight: 600; margin: 1.2rem 0 0.5rem; color: var(--muted); text-transform: uppercase; letter-spacing: .04em; }}
.question {{ font-size: 1.05rem; margin: 1.2rem 0 0.4rem; font-weight: 600; }}
.summary-line {{ font-size: 0.95rem; margin-bottom: 0.2rem; }}
.summary-note {{ font-size: 0.85rem; color: var(--muted); margin-bottom: 1rem; }}
.filters {{ display: flex; flex-wrap: wrap; gap: 0.4rem; margin: 1rem 0 0.5rem; align-items: center; }}
.filter-btn {{
  padding: 0.2rem 0.65rem;
  border: 1px solid var(--border);
  border-radius: 1rem;
  background: var(--surface);
  color: var(--text);
  font-size: 0.82rem;
  cursor: pointer;
  white-space: nowrap;
}}
.filter-btn.active {{ border-color: var(--accent); background: var(--accent); color: #fff; }}
#search {{
  flex: 1 1 12rem;
  min-width: 0;
  padding: 0.25rem 0.6rem;
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  background: var(--surface);
  color: var(--text);
  font-size: 0.85rem;
}}
table {{ width: 100%; border-collapse: collapse; margin-top: 0.5rem; font-size: 0.88rem; }}
th {{
  text-align: left;
  padding: 0.4rem 0.6rem;
  border-bottom: 2px solid var(--border);
  font-size: 0.78rem;
  text-transform: uppercase;
  letter-spacing: .05em;
  color: var(--muted);
  white-space: nowrap;
}}
td {{ padding: 0.45rem 0.6rem; border-bottom: 1px solid var(--border); vertical-align: top; word-break: break-word; }}
@media (max-width: 480px) {{
  th, td {{ padding-left: 0.3rem; padding-right: 0.3rem; }}
  th, td .badge {{ white-space: normal; word-break: normal; }}
}}
tr:last-child td {{ border-bottom: none; }}
.badge {{
  display: inline-block;
  padding: 0.1rem 0.5rem;
  border-radius: 1rem;
  font-size: 0.75rem;
  font-weight: 600;
  white-space: nowrap;
}}
.badge-caught {{ background: #dafbe1; color: var(--green); }}
.badge-now-caught {{ background: #dafbe1; color: var(--green); border: 1px solid var(--green); }}
.badge-exposed {{ background: #fff0f0; color: var(--red); }}
.badge-no-change {{ background: #fff8e1; color: var(--orange); }}
.badge-no-data {{ background: var(--surface); color: var(--muted); }}
.badge-not-a-bug {{ background: #f0f0ff; color: var(--purple); }}
.badge-excluded {{ background: var(--surface); color: var(--muted); }}
@media (prefers-color-scheme: dark) {{
  .badge-caught {{ background: #0f2a1a; }}
  .badge-now-caught {{ background: #0f2a1a; }}
  .badge-exposed {{ background: #2a0f0f; }}
  .badge-no-change {{ background: #2a1f00; }}
  .badge-not-a-bug {{ background: #1a1030; }}
}}
details summary {{ cursor: pointer; color: var(--accent); font-size: 0.82rem; user-select: none; }}
details summary:hover {{ text-decoration: underline; }}
pre {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.3rem;
  padding: 0.5rem 0.7rem;
  font-size: 0.78rem;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin-top: 0.4rem;
  max-height: 14rem;
}}
.test-id {{ font-family: monospace; font-size: 0.8rem; color: var(--muted); }}
.sha-link {{ font-family: monospace; font-size: 0.8rem; white-space: nowrap; word-break: normal; }}
td.sha-cell {{ white-space: nowrap; word-break: normal; }}
.hidden {{ display: none !important; }}
.commands-box {{
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: 0.4rem;
  padding: 0.7rem 1rem;
  margin: 1rem 0;
}}
.cmd-line {{
  font-family: monospace;
  font-size: 0.85rem;
  display: flex;
  align-items: flex-start;
  gap: 0.5rem;
  margin: 0.3rem 0;
}}
.cmd-line code {{ flex: 1 1 auto; min-width: 0; overflow-wrap: anywhere; user-select: all; }}
.copy-btn {{
  flex: 0 0 auto;
  padding: 0.1rem 0.5rem;
  border: 1px solid var(--border);
  border-radius: 0.3rem;
  background: var(--bg, transparent);
  color: var(--text);
  font-size: 0.75rem;
  cursor: pointer;
}}
.cmd-note {{ font-size: 0.8rem; color: var(--muted); margin: 0.4rem 0 0; }}
.method-box {{
  background: var(--surface);
  border-left: 3px solid var(--border);
  padding: 0.6rem 0.9rem;
  margin: 0.5rem 0;
  font-size: 0.88rem;
  color: var(--muted);
}}
footer {{
  margin-top: 3rem;
  padding-top: 1rem;
  border-top: 1px solid var(--border);
  text-align: center;
  font-size: 0.75rem;
  color: var(--muted);
}}
@media (max-width: 500px) {{
  body {{ font-size: 14px; }}
  table {{ font-size: 0.82rem; }}
  th, td {{ padding: 0.35rem 0.4rem; }}
}}
</style>
</head>
<body>
<h1>Antibody &#8212; {name}</h1>
<p class="question">Of the bugs this project already fixed, how many could come back without a test noticing?</p>
<p class="summary-line">{summary_line}</p>
<p class="summary-note">{summary_note}</p>

<div class="filters" id="filters">
{filter_buttons}
  <input id="search" type="search" placeholder="Filter by subject or SHA&hellip;" aria-label="Filter rows">
</div>

<noscript><p style="color:var(--muted);font-size:.85rem;margin:.5rem 0">Enable JavaScript for interactive filtering.</p></noscript>

<table id="rows-table">
<thead>
<tr>
  <th>Status</th>
  <th>Description</th>
  <th>Fix</th>
  <th>Issue</th>
</tr>
</thead>
<tbody id="rows-body">
{rows_html}
</tbody>
</table>

<h2>Run it on your own project</h2>
<div class="commands-box">
  <div class="cmd-line"><code>python3 .bob/skills/recurrence-audit/scripts/antibody.py setup &lt;name&gt; &lt;git-url&gt; [--rev SHA] [--deps PKG ...]</code><button class="copy-btn" type="button" hidden>Copy</button></div>
  <div class="cmd-line"><code>python3 .bob/skills/recurrence-audit/scripts/antibody.py candidates &lt;name&gt;</code><button class="copy-btn" type="button" hidden>Copy</button></div>
  <div class="cmd-line"><code>python3 .bob/skills/recurrence-audit/scripts/antibody.py run &lt;name&gt;</code><button class="copy-btn" type="button" hidden>Copy</button></div>
  <div class="cmd-line"><code>python3 .bob/skills/recurrence-audit/scripts/antibody.py ledger &lt;name&gt;</code><button class="copy-btn" type="button" hidden>Copy</button></div>
  <p class="cmd-note">Needs Python 3.12 or newer. Run from the root of the repository that holds <code>.bob/</code>.</p>
</div>

<h2>Method</h2>
<div class="method-box">
  For each past bug fix: apply the reverse patch, run the test suite, restore the code.
  A <strong>CAUGHT</strong> row means at least one test failed when the bug was put back.
  A <strong>NOW CAUGHT</strong> row means a new test was proved and just re-passed that check.
  A <strong>STILL EXPOSED</strong> row means the patch applied, all tests stayed green,
  and a probe confirmed the behaviour changed (or no check could tell either way).
  The row says which. <strong>NO CHANGE FOUND</strong>: a probe ran but both outputs matched
  (listed under &ldquo;Excluded after a check&rdquo;, with the check and both outputs).
  <strong>NO DATA</strong>: the patch could not be applied or no tests ran (with its reason).
</div>

<h2>What this can&rsquo;t tell you</h2>
<div class="method-box">
  Rows with no data are <em>unknown</em>, never safe.
  &ldquo;Caught&rdquo; means a test noticed the regression &mdash; not that the test is perfect or complete.
  A green suite on a reverted patch is a lower bound, not a guarantee.
</div>

<script>
(function() {{
  var activeFilter = "all";
  var searchVal = "";
  function applyFilters() {{
    document.querySelectorAll("#rows-body tr").forEach(function(row) {{
      var s = row.dataset.status || "";
      var text = (row.dataset.subject || "") + " " + (row.dataset.sha || "");
      var matchFilter = (activeFilter === "all") || (s === activeFilter);
      var matchSearch = !searchVal || text.toLowerCase().indexOf(searchVal) !== -1;
      row.classList.toggle("hidden", !(matchFilter && matchSearch));
    }});
  }}
  document.querySelectorAll(".filter-btn").forEach(function(btn) {{
    btn.addEventListener("click", function() {{
      activeFilter = btn.dataset.filter;
      document.querySelectorAll(".filter-btn").forEach(function(b) {{ b.classList.remove("active"); }});
      btn.classList.add("active");
      applyFilters();
    }});
  }});
  document.querySelectorAll(".copy-btn").forEach(function(btn) {{
    var code = btn.parentNode.querySelector("code");
    if (!code || !navigator.clipboard) return;
    btn.hidden = false;
    btn.addEventListener("click", function() {{
      navigator.clipboard.writeText(code.textContent).then(function() {{
        btn.textContent = "Copied";
        setTimeout(function() {{ btn.textContent = "Copy"; }}, 1500);
      }});
    }});
  }});
  var search = document.getElementById("search");
  if (search) {{
    search.addEventListener("input", function() {{
      searchVal = search.value.toLowerCase();
      applyFilters();
    }});
  }}
}})();
</script>

<footer>Made with IBM Bob</footer>
</body>
</html>"""


def _e(s: str) -> str:
    """HTML-escape a string."""
    return _html_mod.escape(str(s), quote=True)


def _status_badge(status: str) -> str:
    cls_map = {
        "CAUGHT": "badge-caught",
        "NOW CAUGHT": "badge-now-caught",
        "STILL EXPOSED": "badge-exposed",
        "NO CHANGE FOUND": "badge-no-change",
        "NO DATA": "badge-no-data",
        "NOT A BUG": "badge-not-a-bug",
        "EXCLUDED": "badge-excluded",
    }
    cls = cls_map.get(status, "badge-no-data")
    return f'<span class="badge {cls}" data-status="{_e(status)}">{_e(status)}</span>'


def _build_row_html(lr: dict, repo_url: str) -> str:
    sha = lr["sha"]
    status = lr["status"]
    line = lr["line"]
    reason = lr.get("reason", "")
    catching_tests = lr.get("catching_tests", [])
    proof_code = lr.get("proof_code")
    probe_output = lr.get("probe_output")
    link = lr.get("link", "")

    badge = _status_badge(status)

    # Fix commit link
    commit_url = ""
    if repo_url and repo_url.startswith("https://github.com"):
        commit_url = f"{repo_url.rstrip('/')}/commit/{sha}"
    sha_cell = (
        f'<a class="sha-link" href="{_e(commit_url)}" target="_blank" rel="noopener">{_e(sha[:8])}</a>'
        if commit_url else
        f'<span class="sha-link">{_e(sha[:8])}</span>'
    )

    # Issue link
    issue_cell = ""
    if link:
        label = link.rstrip("/").rsplit("/", 1)[-1]
        issue_cell = f'<a href="{_e(link)}" target="_blank" rel="noopener">{_e(label)}</a>'

    # Description cell
    desc_parts = [_e(line)]
    if reason and status not in ("CAUGHT", "NOW CAUGHT", "STILL EXPOSED", "NOT A BUG"):
        desc_parts.append(f'<br><small style="color:var(--muted)">{_e(reason)}</small>')

    # Expandable evidence
    evidence_parts = []
    if catching_tests:
        ids_html = "".join(f'<div class="test-id">{_e(t)}</div>' for t in catching_tests)
        evidence_parts.append(f"<strong>Catching tests:</strong>{ids_html}")
    if proof_code:
        evidence_parts.append(f'<strong>New test:</strong><pre>{_e(proof_code[:2000])}</pre>')
    if probe_output:
        check = _rel_to_root(probe_output.get("check", ""))
        with_fix = probe_output.get("with_fix", "")
        without_fix = probe_output.get("without_fix", "")
        evidence_parts.append(
            f'<strong>Probe:</strong> <code>{_e(check)}</code>'
            f'<br>With fix:<pre>{_e(str(with_fix)[:800])}</pre>'
            f'Without fix:<pre>{_e(str(without_fix)[:800])}</pre>'
        )
    if status == "STILL EXPOSED" and reason:
        evidence_parts.append(f'<strong>Note:</strong> {_e(reason)}')

    evidence_html = ""
    if evidence_parts:
        inner = "".join(f"<p style='margin:.3rem 0'>{p}</p>" for p in evidence_parts)
        evidence_html = f'<details><summary>Evidence</summary>{inner}</details>'

    desc_cell = "".join(desc_parts) + evidence_html

    return (
        f'<tr data-status="{_e(status)}" data-sha="{_e(sha)}" data-subject="{_e(line)}">'
        f'<td>{badge}</td>'
        f'<td>{desc_cell}</td>'
        f'<td class="sha-cell">{sha_cell}</td>'
        f'<td>{issue_cell}</td>'
        f'</tr>'
    )


def _build_ledger_page(name: str, ledger: dict, repo_url: str) -> str:
    from collections import Counter
    rows = ledger["rows"]
    stats = ledger["stats"]
    n_caught_before = stats["caught_before"]
    n_caught_after = stats["caught_after"]
    n_y = stats["checkable"]

    if n_y == 0:
        summary_line = "No past bug could be re-checked on today&#39;s code"
    else:
        summary_line = (
            f"Before: {n_caught_before} of {n_y} caught. "
            f"After: {n_caught_after} of {n_y} caught."
        )

    summary_note = (
        f"Y = {n_y}: the number of past bugs where the patch applied, tests ran, and a check was "
        f"possible (CAUGHT + NOW CAUGHT + STILL EXPOSED + NO CHANGE FOUND). "
        f"Excluded, NO DATA, and NOT A BUG rows are not counted."
    )

    status_counts: Counter = Counter(r["status"] for r in rows)
    all_count = len(rows)

    status_order = [
        ("all", "All"),
        ("CAUGHT", "Caught"),
        ("NOW CAUGHT", "Now Caught"),
        ("STILL EXPOSED", "Still Exposed"),
        ("NO CHANGE FOUND", "No Change Found"),
        ("NO DATA", "No Data"),
        ("NOT A BUG", "Not a Bug"),
        ("EXCLUDED", "Excluded"),
    ]

    filter_buttons = []
    for key, label in status_order:
        cnt = all_count if key == "all" else status_counts.get(key, 0)
        if key == "all" or cnt > 0:
            active = " active" if key == "all" else ""
            filter_buttons.append(
                f'  <button class="filter-btn{active}" data-filter="{_e(key)}">'
                f'{_e(label)} ({cnt})'
                f'</button>'
            )

    filter_buttons_html = "\n".join(filter_buttons)
    rows_html = "\n".join(_build_row_html(r, repo_url) for r in rows)

    return _HTML_PAGE_TEMPLATE.format(
        name=_e(name),
        summary_line=summary_line,
        summary_note=summary_note,
        filter_buttons=filter_buttons_html,
        rows_html=rows_html,
    )


def cmd_ledger(args: argparse.Namespace) -> None:
    name: str = args.name
    publish: bool = args.publish

    ab = antibody_dir(name)
    candidates_path = ab / "candidates.json"
    results_path = ab / "results.json"
    if not candidates_path.exists():
        die(f"No candidates.json for '{name}'. Run candidates first.")
    if not results_path.exists():
        die(f"No results.json for '{name}'. Run run first.")

    with open(candidates_path, "r", encoding="utf-8") as f:
        candidates: list[dict] = json.load(f)
    with open(results_path, "r", encoding="utf-8") as f:
        results_data: dict = json.load(f)

    results_rows = results_data.get("rows", [])

    curated_path = ab / "curated.json"
    curated: dict = {}
    curated_sha256: str | None = None
    curated_ts: str | None = None
    if curated_path.exists():
        with open(curated_path, "r", encoding="utf-8") as f:
            curated = json.load(f)
        curated_sha256 = _sha256_file(curated_path)
        curated_ts = datetime.datetime.fromtimestamp(
            curated_path.stat().st_mtime, tz=datetime.timezone.utc
        ).isoformat(timespec="seconds")

    # One ledger row per candidate. `run --shas` runs only the rows curated
    # as BUG, so a candidate may have no results row when curated.json marks
    # it NOT-A-BUG or candidates.json already excludes it.
    by_sha = {r["sha"]: r for r in results_rows}
    cand_shas = {c["sha"] for c in candidates}
    unknown = [s for s in by_sha if s not in cand_shas]
    if unknown:
        die(
            f"results.json has rows that are not in candidates.json: "
            f"{', '.join(s[:12] for s in unknown)}. Re-run `run {name}`."
        )
    rows_in: list[dict] = []
    missing: list[str] = []
    for c in candidates:
        sha = c["sha"]
        if sha in by_sha:
            rows_in.append(by_sha[sha])
        elif str(c.get("status", "")).startswith("EXCLUDED") or _is_not_a_bug(curated.get(sha)):
            rows_in.append({
                "sha": sha,
                "subject": c.get("subject", ""),
                "src_files": c.get("src_files", []),
                "status": c.get("status", ""),
                "reason": c.get("reason", ""),
                "catching_tests": [],
            })
        else:
            missing.append(sha)
    if missing:
        die(
            f"No results row for {len(missing)} candidate(s) that are neither "
            f"excluded nor curated NOT-A-BUG: {', '.join(s[:12] for s in missing)}. "
            f"Run `run {name} --shas FILE` with them."
        )

    setup_path = ab / "setup.json"
    if not setup_path.exists():
        die(f"No setup.json for '{name}'.")
    with open(setup_path, "r", encoding="utf-8") as f:
        setup_data: dict = json.load(f)

    repo_url: str = setup_data.get("repo", "")

    probes_dir = ab / "probes"
    proofs_dir = ab / "proofs"
    explanations_path = ab / "explanations.json"

    explanations: dict = {}
    if explanations_path.exists():
        with open(explanations_path, "r", encoding="utf-8") as f:
            explanations = json.load(f)

    # Re-prove: for every sha with a PROVEN proof, re-run prove with the test
    # the proof names. Only rows that re-prove successfully are eligible for
    # NOW CAUGHT.
    reproved_tests: dict[str, str] = {}
    if proofs_dir.exists():
        for proof_file in sorted(proofs_dir.glob("*.json")):
            try:
                proof_data = json.loads(proof_file.read_text())
            except Exception:
                continue
            if proof_data.get("verdict") != "PROVEN":
                continue
            sha = proof_file.stem
            result_row = next((r for r in rows_in if r["sha"] == sha), None)
            if not result_row:
                continue
            test_node = proof_data.get("test") or next(iter(result_row.get("catching_tests", [])), "")
            if not test_node:
                continue
            patch_path = diffs_dir(name) / f"{sha}.patch"
            if not patch_path.exists():
                continue
            print(f"Re-proving {sha[:10]} with {test_node}")
            with target_lock(name):
                try:
                    _do_prove(name, sha, test_node)
                    new_proof_path = proofs_dir / f"{sha}.json"
                    new_proof = json.loads(new_proof_path.read_text())
                    if new_proof.get("verdict") == "PROVEN":
                        reproved_tests[sha] = new_proof.get("test") or test_node
                except Exception:
                    pass

    # Build ledger rows
    tgt = target_dir(name)
    ledger_rows: list[dict] = []
    for row in rows_in:
        sha = row["sha"]
        lr = _map_status(
            row,
            proofs_dir if proofs_dir.exists() else None,
            probes_dir if probes_dir.exists() else None,
            curated,
            explanations,
        )
        # A new test that just re-proved turns an exposed row into NOW CAUGHT.
        # A CAUGHT row stays CAUGHT unless only antibody tests catch it.
        new_test = reproved_tests.get(sha)
        only_new = bool(row.get("catching_tests")) and all(
            str(t).startswith("tests/antibody/") for t in row.get("catching_tests", [])
        )
        if new_test and (lr["status"] in ("STILL EXPOSED", "NO CHANGE FOUND")
                         or (lr["status"] == "CAUGHT" and only_new)):
            lr["status"] = "NOW CAUGHT"
            lr["catching_tests"] = [new_test]
            lr["reason"] = ""
            lr["probe_output"] = None
            try:
                lr["proof_code"] = (tgt / new_test.split("::")[0]).read_text(encoding="utf-8")
            except OSError:
                lr["proof_code"] = None
        ledger_rows.append(lr)

    checkable_statuses = {"CAUGHT", "NOW CAUGHT", "STILL EXPOSED", "NO CHANGE FOUND"}
    y_rows = [r for r in ledger_rows if r["status"] in checkable_statuses]
    n_before_caught = sum(1 for r in ledger_rows if r["status"] == "CAUGHT")
    n_after_caught = sum(1 for r in ledger_rows if r["status"] in ("CAUGHT", "NOW CAUGHT"))
    n_y = len(y_rows)

    run_time = results_data.get("run_time") or datetime.datetime.fromtimestamp(
        results_path.stat().st_mtime, tz=datetime.timezone.utc
    ).isoformat(timespec="seconds")

    ledger: dict = {
        "name": name,
        "repo": repo_url,
        "head": setup_data.get("head", ""),
        "run_time": run_time,
        "ledger_time": _isodate(),
        "curated": (
            {"sha256": curated_sha256, "timestamp": curated_ts}
            if curated_sha256 else None
        ),
        "stats": {
            "total_candidates": len(candidates),
            "checkable": n_y,
            "caught_before": n_before_caught,
            "caught_after": n_after_caught,
        },
        "rows": ledger_rows,
    }

    out_path = ab / "ledger.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(ledger, f, indent=2)
    print(f"Wrote {out_path}")

    page_html = _build_ledger_page(name, ledger, repo_url)
    page_path = ab / "index.html"
    with open(page_path, "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"Wrote {page_path}")

    if publish:
        _publish_ledger(name, ab, ledger, page_html, proofs_dir)

    # The summary line, as the page shows it.
    if n_y == 0:
        print("No past bug could be re-checked on today's code")
    else:
        print(f"Before: {n_before_caught} of {n_y} caught. After: {n_after_caught} of {n_y} caught.")


def _publish_ledger(name: str, ab: Path, ledger: dict, page_html: str, proofs_dir: Path) -> None:
    root = ROOT
    audits_dir = root / "audits" / name
    audits_dir.mkdir(parents=True, exist_ok=True)

    shutil.copy2(ab / "ledger.json", audits_dir / "ledger.json")
    print(f"Published {audits_dir / 'ledger.json'}")

    # Publish only the tests of the rows that are NOW CAUGHT (proved and re-proved).
    tgt = target_dir(name)
    for row in ledger.get("rows", []):
        if row.get("status") != "NOW CAUGHT":
            continue
        for test in row.get("catching_tests", []):
            tf = tgt / str(test).split("::")[0]
            if tf.is_file():
                dest = audits_dir / tf.name
                shutil.copy2(tf, dest)
                print(f"Published {dest}")

    site_dir = root / "site" / name
    site_dir.mkdir(parents=True, exist_ok=True)
    site_page = site_dir / "index.html"
    with open(site_page, "w", encoding="utf-8") as f:
        f.write(page_html)
    print(f"Published {site_page}")

    _rebuild_site_index(root)


def _rebuild_site_index(root: Path) -> None:
    audits_root = root / "audits"
    site_root = root / "site"
    site_root.mkdir(parents=True, exist_ok=True)

    entries: list[str] = []
    if audits_root.exists():
        for folder in sorted(audits_root.iterdir()):
            if folder.is_dir():
                entries.append(folder.name)

    links = "\n".join(
        f'    <li><a href="{_e(e)}/index.html">{_e(e)}</a></li>'
        for e in entries
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Antibody Audits</title>
<style>
body {{ font-family: -apple-system, "Segoe UI", system-ui, sans-serif; max-width: 600px; margin: 2rem auto; padding: 0 1rem; }}
</style>
</head>
<body>
<h1>Antibody Audits</h1>
<ul>
{links}
</ul>
<footer style="margin-top:2rem;padding-top:.5rem;border-top:1px solid #e5e7eb;text-align:center;font-size:.75rem;color:#57606a">Made with IBM Bob</footer>
</body>
</html>"""

    with open(site_root / "index.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Rebuilt {site_root / 'index.html'}")


# ---------------------------------------------------------------------------
# gate
# ---------------------------------------------------------------------------

def _parse_accepted_yaml(path: Path) -> list[dict]:
    """
    Parse antibody-accepted.yaml using only the standard library.
    Pinned format:
      - sha: <full sha>
        reason: <one sentence>

    Returns list of {sha, reason} dicts.
    Raises ValueError naming the offending line on format errors.
    Never imports yaml.
    """
    lines = path.read_text(encoding="utf-8").splitlines()
    entries: list[dict] = []
    i = 0
    while i < len(lines):
        stripped = lines[i].rstrip()
        if not stripped:
            i += 1
            continue
        if not stripped.startswith("- sha:"):
            raise ValueError(
                f"line {i+1}: expected '- sha: <full-sha>', got: {stripped!r}"
            )
        sha_val = stripped[len("- sha:"):].strip()
        if not sha_val:
            raise ValueError(f"line {i+1}: sha value is empty")
        i += 1
        while i < len(lines) and not lines[i].rstrip():
            i += 1
        if i >= len(lines):
            raise ValueError(
                f"After sha '{sha_val}': expected '  reason:' line, got end of file"
            )
        reason_line = lines[i].rstrip()
        if not reason_line.startswith("  reason:"):
            raise ValueError(
                f"line {i+1}: expected '  reason: <sentence>', got: {reason_line!r}"
            )
        reason_val = reason_line[len("  reason:"):].strip()
        if not reason_val:
            raise ValueError(
                f"line {i+1}: reason for sha '{sha_val}' is empty — "
                "a real sentence is required"
            )
        if reason_val.lower() in ("todo", "tbd", "fixme", "?"):
            raise ValueError(
                f"line {i+1}: reason for sha '{sha_val}' is a placeholder "
                f"({reason_val!r}) — write a real sentence"
            )
        entries.append({"sha": sha_val, "reason": reason_val})
        i += 1
    return entries


def cmd_gate(args: argparse.Namespace) -> None:
    name: str = args.name

    ab = antibody_dir(name)
    ledger_path = ab / "ledger.json"
    if not ledger_path.exists():
        print(f"ERROR: no ledger.json for '{name}'. Run ledger first.", file=sys.stderr)
        sys.exit(1)

    with open(ledger_path, "r", encoding="utf-8") as f:
        ledger: dict = json.load(f)

    rows: list[dict] = ledger.get("rows", [])
    if not rows:
        print("ERROR: ledger is empty — nothing was checked.", file=sys.stderr)
        print("checked 0 of 0; 0 need re-audit; 0 accepted exposures")
        sys.exit(1)

    # Load antibody-accepted.yaml
    accepted_path = ROOT / "antibody-accepted.yaml"
    accepted_entries: list[dict] = []
    if accepted_path.exists():
        try:
            accepted_entries = _parse_accepted_yaml(accepted_path)
        except ValueError as e:
            print(f"ERROR: antibody-accepted.yaml: {e}", file=sys.stderr)
            sys.exit(1)

    accepted_shas = {e["sha"] for e in accepted_entries}

    # Warn about stale entries
    exposed_shas = {r["sha"] for r in rows if r["status"] == "STILL EXPOSED"}
    for entry in accepted_entries:
        if entry["sha"] not in exposed_shas:
            print(
                f"WARNING: stale entry in antibody-accepted.yaml: "
                f"{entry['sha'][:12]} is not STILL EXPOSED"
            )

    failures: list[str] = []
    warnings_list: list[str] = []
    checked = 0
    re_audit_count = 0
    accepted_exposures = 0

    tgt = target_dir(name)

    for row in rows:
        sha = row["sha"]
        status = row["status"]

        if status in ("CAUGHT", "NOW CAUGHT"):
            checked += 1
            catching_tests = row.get("catching_tests", [])

            # Rows caught by timeout or collection error have no test IDs
            has_no_ids = not catching_tests
            patch_path = diffs_dir(name) / f"{sha}.patch"

            if not patch_path.exists():
                # Can't re-verify — warn
                warnings_list.append(
                    f"re-audit needed: {sha[:12]} (no patch file)"
                )
                re_audit_count += 1
                continue

            # Check patch still applies
            check_result = subprocess.run(
                ["git", "apply", "--check", "-R", str(patch_path)],
                cwd=str(tgt),
                capture_output=True,
                text=True,
            )
            if check_result.returncode != 0:
                warnings_list.append(f"re-audit needed: {sha[:12]}")
                re_audit_count += 1
                continue

            if has_no_ids:
                # Run full suite; expect some kind of failure (timeout or test failures)
                ok, _ = git_apply_reverse(tgt, patch_path)
                if not ok:
                    warnings_list.append(f"re-audit needed: {sha[:12]}")
                    re_audit_count += 1
                    continue
                try:
                    try:
                        report = run_suite(name, timeout_secs=120)
                        if not report.get("failed") and not report.get("collection_errors"):
                            failures.append(
                                f"catching test missing: {sha[:12]} "
                                "(full suite passed with bug back)"
                            )
                    except RuntimeError:
                        pass  # timeout / missing-report counts as expected failure
                finally:
                    git_restore(tgt)
            else:
                # Step 1: each catching test must be collected and pass at HEAD
                for tid in catching_tests:
                    try:
                        report = run_suite(name, extra_args=[tid], timeout_secs=120)
                    except RuntimeError as e:
                        failures.append(f"catching test missing: {tid} (error: {e})")
                        continue
                    collected_ids = report.get("collected", [])
                    if not collected_ids:
                        failures.append(f"catching test missing: {tid}")
                        continue
                    # Check node ID is actually collected (handle parametrized suffixes)
                    if not any(
                        nid == tid or nid.startswith(tid) or nid.endswith(tid.split("::")[-1])
                        for nid in collected_ids
                    ):
                        failures.append(f"catching test missing: {tid}")
                        continue
                    if report.get("failed"):
                        failures.append(f"catching test already fails at HEAD: {tid}")

                # Step 2: each catching test must fail with bug back
                ok, _ = git_apply_reverse(tgt, patch_path)
                if not ok:
                    warnings_list.append(f"re-audit needed: {sha[:12]}")
                    re_audit_count += 1
                    continue
                try:
                    for tid in catching_tests:
                        try:
                            report = run_suite(name, extra_args=[tid], timeout_secs=120)
                        except RuntimeError:
                            continue  # timeout = failure, expected
                        if not report.get("failed"):
                            failures.append(
                                f"catching test no longer fails with bug back: {tid}"
                            )
                finally:
                    git_restore(tgt)

        elif status == "STILL EXPOSED":
            if sha not in accepted_shas:
                line_text = row.get("line", row.get("subject", ""))[:60]
                failures.append(
                    f"STILL EXPOSED row not in antibody-accepted.yaml: "
                    f"{sha[:12]} ({line_text})"
                )
            else:
                accepted_exposures += 1

    if not checked:
        failures.append("no rows were checked — nothing is verified")

    total = len(rows)
    summary = (
        f"checked {checked} of {total}; "
        f"{re_audit_count} need re-audit; "
        f"{accepted_exposures} accepted exposures"
    )

    for w in warnings_list:
        print(f"WARNING: {w}")

    if failures:
        for f_msg in failures:
            print(f"FAIL: {f_msg}")
        print(summary)
        sys.exit(1)
    else:
        print("OK: all checks passed")
        print(summary)


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

    # ledger
    p_ledger = sub.add_parser("ledger", help="Build ledger.json and static page")
    p_ledger.add_argument("name", help="Target name")
    p_ledger.add_argument("--publish", action="store_true", help="Copy to audits/ and site/")

    # gate
    p_gate = sub.add_parser("gate", help="CI gate: verify catching tests still work")
    p_gate.add_argument("name", help="Target name")

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
    elif args.command == "ledger":
        cmd_ledger(args)
    elif args.command == "gate":
        cmd_gate(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
