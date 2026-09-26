#!/usr/bin/env python3
"""
antibody.py — Antibody runner.

Subcommands:
  setup      <name> <git-url> [--rev SHA] [--deps PKG ...]
  candidates <name> [--since DATE]
  run        <name> [--shas FILE]

Python 3.12, standard library only.
"""
from __future__ import annotations

import argparse
import ast
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

    # Passed = collected - failed (xfail/xpass handled transparently by plugin recording "failed")
    passed = collected - len(failed)
    # We don't have a separate skipped count from the plugin — derive from collected vs passed
    # For the setup.json we record what we know.
    skipped = 0  # plugin doesn't track skipped; acceptable for setup

    setup_data = {
        "repo": git_url,
        "head": head_sha,
        "deps": deps,
        "install_commands": [" ".join(c) for c in install_cmds_rel],
        "baseline": {
            "collected": collected,
            "passed": passed,
            "skipped": skipped,
            "seconds": round(elapsed, 2),
        },
    }

    out_path = ab / "setup.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(setup_data, f, indent=2)
    print(f"Wrote {out_path}")
    print(f"Baseline: {collected} collected, {passed} passed, {elapsed:.1f}s  ✓")


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
    result = subprocess.run(
        ["git", "status", "--porcelain"],
        cwd=str(tgt),
        capture_output=True,
        text=True,
    )
    return result.stdout.strip() == ""


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

    args = parser.parse_args()

    if args.command == "setup":
        cmd_setup(args)
    elif args.command == "candidates":
        cmd_candidates(args)
    elif args.command == "run":
        cmd_run(args)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
