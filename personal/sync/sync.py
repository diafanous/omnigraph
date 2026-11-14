#!/usr/bin/env python3
"""Weekly upstream sync for this fork: fetch, merge on a branch, audit, open a PR.

Nothing reaches `main` without a person merging the pull request.

Runs in GitHub Actions (personal-upstream-sync.yml) and on your own machine:

    python personal/sync/sync.py            # full run: push branch + open PR
    python personal/sync/sync.py --dry-run  # audit only, no push, no PR

Steps:
  1. Fetch origin/main and upstream/main.
  2. Stop if upstream has nothing new.
  3. Merge upstream/main into a new branch `sync/upstream-<sha>`.
     A merge conflict opens an issue instead.
  4. Run audit.py on the upstream range (never builds or runs upstream code).
  5. Push the branch and open a PR with the audit report and a label
     `audit:pass`, `audit:review` or `audit:fail`.
     In CI, GITHUB_TOKEN cannot push changes to .github/workflows. When upstream
     changed a workflow and no SYNC_TOKEN secret exists, the run opens an issue
     that asks you to run this script locally (your `gh` login can push them).
  6. Optional: with AUTO_MERGE_ON_PASS=true, a PASS verdict is merged
     automatically. REVIEW and FAIL always wait for you.
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

# Load the audit code now, before the merge changes the working tree.
sys.path.insert(0, str(Path(__file__).resolve().parent))
import audit  # noqa: E402

UPSTREAM_URL = "https://github.com/ModernRelay/omnigraph.git"
LABELS = {
    "audit:pass": ("0e8a16", "Upstream sync audit passed"),
    "audit:review": ("fbca04", "Upstream sync audit: sensitive paths changed, read before merge"),
    "audit:fail": ("b60205", "Upstream sync audit failed: do not merge"),
    "upstream-sync": ("1d76db", "Weekly upstream sync"),
}


def sh(*cmd: str, check: bool = True, cwd: Path | None = None) -> subprocess.CompletedProcess:
    res = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8",
                         errors="replace")
    if check and res.returncode != 0:
        raise RuntimeError(f"{' '.join(cmd)} failed ({res.returncode}):\n{res.stderr.strip()}")
    return res


def out(*cmd: str) -> str:
    return sh(*cmd).stdout.strip()


def ensure_labels() -> None:
    for name, (color, desc) in LABELS.items():
        sh("gh", "label", "create", name, "--color", color, "--description", desc, "--force",
           check=False)


def open_issue(title: str, body: str) -> None:
    ensure_labels()
    existing = out("gh", "issue", "list", "--state", "open", "--label", "upstream-sync",
                   "--search", title, "--json", "number", "--jq", "length")
    if existing not in ("", "0"):
        print(f"issue already open: {title}")
        return
    with tempfile.NamedTemporaryFile("w", suffix=".md", delete=False, encoding="utf-8") as f:
        f.write(body)
    sh("gh", "issue", "create", "--title", title, "--body-file", f.name, "--label", "upstream-sync")
    print(f"opened issue: {title}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="audit only; no push, PR or issue")
    ap.add_argument("--out", type=Path, default=Path(tempfile.gettempdir()) / "omnigraph-sync-audit")
    args = ap.parse_args()

    repo = Path(out("git", "rev-parse", "--show-toplevel"))
    os.chdir(repo)
    ci = os.environ.get("GITHUB_ACTIONS") == "true"
    has_sync_token = bool(os.environ.get("HAS_SYNC_TOKEN") == "true")

    if sh("git", "status", "--porcelain", "--untracked-files=no").stdout.strip():
        print("working tree has uncommitted changes; commit or stash them first")
        return 1
    start_ref = out("git", "rev-parse", "--abbrev-ref", "HEAD")

    remotes = out("git", "remote").split()
    if "upstream" not in remotes:
        sh("git", "remote", "add", "upstream", UPSTREAM_URL)
        sh("git", "remote", "set-url", "--push", "upstream", "DISABLE_PUSH_TO_UPSTREAM")
    sh("git", "fetch", "--quiet", "origin", "main")
    sh("git", "fetch", "--quiet", "--no-tags", "upstream", "main")

    head = out("git", "rev-parse", "upstream/main")
    if sh("git", "merge-base", "--is-ancestor", head, "origin/main", check=False).returncode == 0:
        print(f"up to date: upstream/main {head[:12]} is already in origin/main")
        return 0
    base = out("git", "merge-base", "origin/main", "upstream/main")
    short = head[:10]
    branch = f"sync/upstream-{short}"

    if not args.dry_run:
        existing = out("gh", "pr", "list", "--state", "open", "--head", branch, "--json", "url",
                       "--jq", ".[0].url // \"\"")
        if existing:
            print(f"a PR for this upstream commit is already open: {existing}")
            return 0

    sh("git", "checkout", "--quiet", "-B", branch, "origin/main")
    if ci:
        sh("git", "config", "user.name", "github-actions[bot]")
        sh("git", "config", "user.email", "41898282+github-actions[bot]@users.noreply.github.com")
    merge = sh("git", "merge", "--no-ff", "--no-edit", "-m",
               f"Merge upstream ModernRelay/omnigraph@{short}", "upstream/main", check=False)
    if merge.returncode != 0:
        conflicts = out("git", "diff", "--name-only", "--diff-filter=U")
        sh("git", "merge", "--abort", check=False)
        sh("git", "checkout", "--quiet", start_ref, check=False)
        body = (f"Merging upstream `{short}` into `main` conflicts in:\n\n```\n{conflicts}\n```\n\n"
                "Resolve locally: `git fetch upstream && git merge upstream/main`, then run "
                "`python personal/sync/audit.py --base <old upstream> --head upstream/main` "
                "before you push.")
        print(body)
        if not args.dry_run:
            open_issue(f"Upstream sync conflict at {short}", body)
        return 1

    args.out.mkdir(parents=True, exist_ok=True)
    rep = audit.audit(repo, base, head, repo, args.out)
    report = audit.render(rep)
    (args.out / "audit.md").write_text(report, encoding="utf-8")
    print(report)
    label = f"audit:{rep.verdict.lower()}"

    if args.dry_run:
        sh("git", "checkout", "--quiet", start_ref, check=False)
        print(f"dry run: verdict {rep.verdict}; branch {branch} kept locally, nothing pushed")
        return 2 if rep.verdict == "FAIL" else 0

    workflows_changed = any(p.startswith(".github/workflows/") for _, p in rep.files)
    if ci and workflows_changed and not has_sync_token:
        open_issue(f"Upstream sync {short} needs a local run",
                   "Upstream changed files in `.github/workflows/`. The Actions token cannot "
                   "push those. Run this on your machine (your `gh` login can):\n\n"
                   "```\ncd <your omnigraph clone>\ngit checkout main && git pull\n"
                   "python personal/sync/sync.py\n```\n\nOr add a `SYNC_TOKEN` secret "
                   "(fine-grained token: contents, pull requests and workflows write on this "
                   "repo only).\n\n" + report)
        return 0

    sh("git", "push", "--force", "--quiet", "origin", f"{branch}:{branch}")
    ensure_labels()
    title = f"Upstream sync: {len(rep.commits)} commits to {short} (audit {rep.verdict})"
    body_file = args.out / "audit.md"
    pr_url = out("gh", "pr", "create", "--base", "main", "--head", branch, "--title", title,
                 "--body-file", str(body_file), "--label", "upstream-sync", "--label", label)
    print(f"opened {pr_url}")
    if rep.verdict == "PASS" and os.environ.get("AUTO_MERGE_ON_PASS") == "true":
        sh("gh", "pr", "merge", pr_url, "--merge")
        print("auto-merged (AUTO_MERGE_ON_PASS=true and verdict PASS)")
    sh("git", "checkout", "--quiet", start_ref, check=False)
    (args.out / "result.json").write_text(json.dumps({"pr": pr_url, "verdict": rep.verdict}),
                                          encoding="utf-8")
    return 0


if __name__ == "__main__":
    sys.exit(main())
