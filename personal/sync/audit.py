#!/usr/bin/env python3
"""Security audit of the upstream changes that a sync would bring into this fork.

The audit compares two git refs (BASE = last upstream commit already in the
fork, HEAD = new upstream tip). It never builds or runs upstream code.

Checks:
  1. Commit list, authors and GitHub signature verification (needs `gh`).
  2. Changed files in sensitive paths (CI, build scripts, installers, agent
     instructions, dependency policy).
  3. Cargo.lock changes: added crates, version changes, non-crates.io sources.
  4. Risky patterns in added lines (pipe-to-shell, obfuscated blobs,
     pull_request_target, prompt-injection phrases in agent-facing files).
  5. cargo-deny (advisories, bans, licenses, sources) when installed.
  6. gitleaks secret scan of the commit range when installed.

Verdict:
  FAIL   - a blocking finding. Do not merge.
  REVIEW - no blocking finding, but sensitive paths changed. Read the listed
           files before you merge.
  PASS   - no blocking finding and no sensitive path changed.

Exit code is 0 for PASS/REVIEW and 2 for FAIL, so CI can gate on it.
"""
from __future__ import annotations

import argparse
import fnmatch
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

UPSTREAM_REPO = "ModernRelay/omnigraph"

# Paths where a change can run code on a build machine, change what CI does,
# change what the dependency gate accepts, or change what agents are told.
SENSITIVE = [
    (".github/*", "CI/CD workflow or GitHub config"),
    ("*build.rs", "Cargo build script (runs at compile time)"),
    ("scripts/*", "install or maintenance script"),
    ("tools/*", "developer tool"),
    ("Dockerfile", "container build"),
    ("docker/*", "container build"),
    ("deploy/*", "deployment config"),
    (".cargo/*", "cargo config (registries, rustflags)"),
    ("rust-toolchain.toml", "toolchain pin"),
    ("flake.nix", "nix build"),
    ("flake.lock", "nix inputs"),
    ("deny.toml", "dependency policy (advisory ignores)"),
    ("Cargo.toml", "workspace manifest"),
    ("*/Cargo.toml", "crate manifest"),
    ("Cargo.lock", "dependency lock"),
    ("skills/*", "agent skill text exported to your agents"),
    ("AGENTS.md", "agent instructions"),
    ("CLAUDE.md", "agent instructions"),
    (".claude/*", "agent config"),
    (".config/*", "tool config"),
]
# Text an agent loads as instructions (the skill is exported to your agents).
AGENT_FACING = ["skills/*", "AGENTS.md", "*/AGENTS.md", "CLAUDE.md", "*/CLAUDE.md", ".claude/*"]
# Upstream must never write here: this is the fork's own space.
FORK_OWNED = ["personal/*", ".github/workflows/personal-*"]

RISKY_ADDED = [
    (r"(curl|wget)[^\n|]*\|\s*(sudo\s+)?(ba|z)?sh\b", "pipe-to-shell download", "high"),
    (r"\b(Invoke-Expression|iex)\b\s*\(?", "PowerShell Invoke-Expression", "high"),
    (r"base64\s+(-d|--decode)", "base64 decode in a script", "medium"),
    (r"\bpull_request_target\b", "pull_request_target trigger (runs with secrets on fork PRs)", "high"),
    (r"\bchmod\s+\+x\b", "makes a file executable", "low"),
    (r"[A-Za-z0-9+/]{240,}={0,2}", "long base64-like blob", "medium"),
    (r"\b(?:\d{1,3}\.){3}\d{1,3}\b", "literal IPv4 address", "low"),
    (r"Command::new\(", "spawns a process (Rust)", "low"),
]
PROMPT_INJECTION = [
    r"ignore (all |any )?(previous|prior|above) instructions",
    r"do not (tell|inform|show) the user",
    r"without (asking|telling) the user",
    r"exfiltrat",
    r"send (the |all )?(credentials|secrets|tokens|keys|\.env)",
    r"disable (the )?(safety|security|sandbox)",
    r"--dangerously",
]
BLOCKING_LEVEL = "block"


@dataclass
class Finding:
    level: str  # block | high | medium | low | info
    check: str
    detail: str


@dataclass
class Report:
    base: str
    head: str
    commits: list[dict] = field(default_factory=list)
    files: list[tuple[str, str]] = field(default_factory=list)
    sensitive: list[tuple[str, str]] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    deps: dict = field(default_factory=dict)
    tools: dict = field(default_factory=dict)

    @property
    def verdict(self) -> str:
        if any(f.level == BLOCKING_LEVEL for f in self.findings):
            return "FAIL"
        if self.sensitive or any(f.level == "high" for f in self.findings):
            return "REVIEW"
        return "PASS"


def run(cmd: list[str], cwd: Path, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, cwd=cwd, check=check, capture_output=True, text=True,
                          encoding="utf-8", errors="replace")


def git(repo: Path, *args: str) -> str:
    return run(["git", *args], repo).stdout


def match_any(path: str, patterns: list[str]) -> bool:
    return any(fnmatch.fnmatch(path, p) for p in patterns)


def check_commits(rep: Report, repo: Path) -> None:
    log = git(repo, "log", "--format=%H%x09%an%x09%ae%x09%s", f"{rep.base}..{rep.head}")
    for line in log.splitlines():
        sha, name, mail, subject = (line.split("\t", 3) + ["", "", "", ""])[:4]
        rep.commits.append({"sha": sha, "author": name, "email": mail, "subject": subject,
                            "verified": None})
    if not shutil.which("gh") or not rep.commits:
        rep.tools["signatures"] = "skipped (gh not available)" if rep.commits else "no commits"
        return
    base_sha = git(repo, "rev-parse", rep.base).strip()
    head_sha = git(repo, "rev-parse", rep.head).strip()
    res = run(["gh", "api", f"repos/{UPSTREAM_REPO}/compare/{base_sha}...{head_sha}",
               "--jq", "[.commits[] | {sha: .sha, v: .commit.verification.verified}]"],
              repo, check=False)
    if res.returncode != 0:
        rep.tools["signatures"] = f"skipped (gh api failed: {res.stderr.strip()[:200]})"
        return
    verified = {c["sha"]: c["v"] for c in json.loads(res.stdout or "[]")}
    for c in rep.commits:
        c["verified"] = verified.get(c["sha"])
    unverified = [c for c in rep.commits if c["verified"] is False]
    rep.tools["signatures"] = f"{len(rep.commits) - len(unverified)}/{len(rep.commits)} verified"
    if unverified:
        rep.findings.append(Finding("medium", "signatures",
                                    f"{len(unverified)} commit(s) without a verified signature: "
                                    + ", ".join(c['sha'][:8] for c in unverified[:10])))


def check_files(rep: Report, repo: Path) -> None:
    out = git(repo, "diff", "--name-status", "--no-renames", rep.base, rep.head)
    for line in out.splitlines():
        status, path = line.split("\t", 1)
        rep.files.append((status, path))
        if match_any(path, FORK_OWNED):
            rep.findings.append(Finding(BLOCKING_LEVEL, "fork-owned path",
                                        f"upstream changed `{path}`, which belongs to this fork"))
        for pattern, why in SENSITIVE:
            if fnmatch.fnmatch(path, pattern):
                rep.sensitive.append((path, why))
                break
    binaries = git(repo, "diff", "--numstat", rep.base, rep.head)
    for line in binaries.splitlines():
        added, deleted, path = line.split("\t", 2)
        if added == "-" and deleted == "-":
            rep.findings.append(Finding("low", "binary file", f"binary file changed: `{path}`"))


def parse_lock(text: str) -> dict[str, tuple[str, str]]:
    if not text:
        return {}
    data = tomllib.loads(text)
    pkgs = {}
    for p in data.get("package", []):
        pkgs[f"{p['name']} {p['version']}"] = (p["name"], p.get("source", "workspace"))
    return pkgs


def check_lock(rep: Report, repo: Path) -> None:
    before = run(["git", "show", f"{rep.base}:Cargo.lock"], repo, check=False).stdout
    after = run(["git", "show", f"{rep.head}:Cargo.lock"], repo, check=False).stdout
    old, new = parse_lock(before), parse_lock(after)
    added = sorted(set(new) - set(old))
    removed = sorted(set(old) - set(new))
    old_names = {v[0] for v in old.values()}
    new_crates = sorted({new[k][0] for k in added if new[k][0] not in old_names})
    rep.deps = {"added_versions": added, "removed_versions": removed, "new_crates": new_crates}
    for key in added:
        name, source = new[key]
        if source != "workspace" and not source.startswith("registry+https://github.com/rust-lang/crates.io-index"):
            rep.findings.append(Finding(BLOCKING_LEVEL, "dependency source",
                                        f"`{key}` comes from a non-crates.io source: {source}"))
    if new_crates:
        rep.findings.append(Finding("medium", "new crates",
                                    f"{len(new_crates)} crate(s) new to the dependency tree: "
                                    + ", ".join(new_crates[:40])))


def check_added_lines(rep: Report, repo: Path) -> None:
    diff = git(repo, "diff", "--unified=0", "--no-color", rep.base, rep.head)
    path = ""
    hits: dict[tuple[str, str], list[str]] = {}
    for line in diff.splitlines():
        if line.startswith("+++ b/"):
            path = line[6:]
            continue
        if not line.startswith("+") or line.startswith("+++"):
            continue
        text = line[1:]
        is_lock = path.endswith((".lock", ".svg", ".json"))
        for pattern, label, level in RISKY_ADDED:
            if is_lock and label in ("long base64-like blob", "literal IPv4 address"):
                continue
            if re.search(pattern, text):
                hits.setdefault((label, level), []).append(path)
        if match_any(path, AGENT_FACING):
            for pattern in PROMPT_INJECTION:
                if re.search(pattern, text, re.IGNORECASE):
                    rep.findings.append(Finding(BLOCKING_LEVEL, "agent-instruction risk",
                                                f"`{path}` adds text matching `{pattern}`: "
                                                f"{text.strip()[:160]!r}"))
    for (label, level), paths in sorted(hits.items()):
        uniq = sorted(set(paths))
        rep.findings.append(Finding(level, "added-line pattern",
                                    f"{label} in {len(uniq)} file(s): "
                                    + ", ".join(f"`{p}`" for p in uniq[:12])))


def check_cargo_deny(rep: Report, worktree: Path | None) -> None:
    exe = shutil.which("cargo-deny")
    if not exe or worktree is None or not shutil.which("cargo"):
        rep.tools["cargo-deny"] = "skipped (cargo-deny or cargo not installed)"
        rep.findings.append(Finding("info", "cargo-deny", "not run on this machine; CI runs it"))
        return
    res = run([exe, "--manifest-path", str(worktree / "Cargo.toml"), "check",
               "advisories", "bans", "licenses", "sources"], worktree, check=False)
    tail = (res.stderr or res.stdout).strip().splitlines()[-25:]
    rep.tools["cargo-deny"] = "passed" if res.returncode == 0 else "FAILED"
    if res.returncode != 0:
        rep.findings.append(Finding(BLOCKING_LEVEL, "cargo-deny",
                                    "cargo-deny check failed:\n```\n" + "\n".join(tail) + "\n```"))


def check_gitleaks(rep: Report, repo: Path, out_dir: Path) -> None:
    exe = shutil.which("gitleaks")
    if not exe:
        rep.tools["gitleaks"] = "skipped (not installed)"
        rep.findings.append(Finding("info", "gitleaks", "not run on this machine; CI runs it"))
        return
    report = out_dir / "gitleaks.json"
    config = Path(__file__).resolve().with_name("gitleaks.toml")  # reviewed exemptions
    extra = ["--config", str(config)] if config.exists() else []
    res = run([exe, "git", str(repo), *extra, f"--log-opts={rep.base}..{rep.head}", "--no-banner",
               "--report-format", "json", "--report-path", str(report), "--exit-code", "0",
               "--redact"], repo, check=False)
    if res.returncode != 0:
        rep.tools["gitleaks"] = "error"
        rep.findings.append(Finding("high", "gitleaks", f"gitleaks error: {res.stderr.strip()[:300]}"))
        return
    leaks = json.loads(report.read_text(encoding="utf-8") or "[]") if report.exists() else []
    rep.tools["gitleaks"] = f"{len(leaks)} finding(s)"
    for leak in leaks[:20]:
        rep.findings.append(Finding(BLOCKING_LEVEL, "secret",
                                    f"{leak.get('RuleID')} in `{leak.get('File')}` "
                                    f"(commit {str(leak.get('Commit'))[:8]})"))


def render(rep: Report) -> str:
    icon = {"PASS": "PASS", "REVIEW": "REVIEW (read the sensitive files)", "FAIL": "FAIL (do not merge)"}
    lines = [f"## Upstream sync audit: {icon[rep.verdict]}", "",
             f"- Range: `{rep.base[:12]}..{rep.head[:12]}` ({len(rep.commits)} commits, "
             f"{len(rep.files)} files)",
             f"- Tools: " + "; ".join(f"{k}: {v}" for k, v in rep.tools.items()), "",
             "**Approval:** the audit does not approve the merge. You approve it by merging "
             "this pull request after you read the findings and the sensitive files.", ""]
    order = {BLOCKING_LEVEL: 0, "high": 1, "medium": 2, "low": 3, "info": 4}
    if rep.findings:
        lines += ["### Findings", ""]
        for f in sorted(rep.findings, key=lambda f: order.get(f.level, 9)):
            lines.append(f"- **{f.level.upper()}** · {f.check}: {f.detail}")
        lines.append("")
    if rep.sensitive:
        lines += ["### Sensitive paths changed", ""]
        lines += [f"- `{p}` — {why}" for p, why in rep.sensitive[:80]]
        if len(rep.sensitive) > 80:
            lines.append(f"- … and {len(rep.sensitive) - 80} more")
        lines.append("")
    if rep.deps.get("added_versions") or rep.deps.get("removed_versions"):
        lines += ["### Dependency changes", "",
                  f"- Added/changed versions: {len(rep.deps['added_versions'])}",
                  f"- Removed versions: {len(rep.deps['removed_versions'])}",
                  f"- Crates new to the tree: {', '.join(rep.deps['new_crates']) or 'none'}", ""]
    lines += ["<details><summary>Commits</summary>", ""]
    for c in rep.commits[:200]:
        mark = {True: "verified", False: "UNVERIFIED", None: "?"}[c["verified"]]
        lines.append(f"- `{c['sha'][:8]}` [{mark}] {c['subject']} — {c['author']}")
    lines += ["", "</details>", ""]
    return "\n".join(lines)


def audit(repo: Path, base: str, head: str, worktree: Path | None, out_dir: Path) -> Report:
    base = git(repo, "rev-parse", f"{base}^{{commit}}").strip()
    head = git(repo, "rev-parse", f"{head}^{{commit}}").strip()
    rep = Report(base=base, head=head)
    check_commits(rep, repo)
    check_files(rep, repo)
    check_lock(rep, repo)
    check_added_lines(rep, repo)
    check_cargo_deny(rep, worktree)
    check_gitleaks(rep, repo, out_dir)
    return rep


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--repo", default=".", type=Path)
    ap.add_argument("--base", required=True, help="last upstream commit already in the fork")
    ap.add_argument("--head", required=True, help="new upstream commit")
    ap.add_argument("--worktree", type=Path, help="checked-out merged tree for cargo-deny")
    ap.add_argument("--out", type=Path, default=Path("audit-out"))
    args = ap.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    rep = audit(args.repo.resolve(), args.base, args.head, args.worktree, args.out)
    (args.out / "audit.md").write_text(render(rep), encoding="utf-8")
    (args.out / "audit.json").write_text(json.dumps({
        "verdict": rep.verdict, "base": rep.base, "head": rep.head, "tools": rep.tools,
        "findings": [f.__dict__ for f in rep.findings], "sensitive": rep.sensitive,
        "deps": rep.deps, "commits": rep.commits}, indent=2), encoding="utf-8")
    print(f"verdict: {rep.verdict}")
    return 2 if rep.verdict == "FAIL" else 0


if __name__ == "__main__":
    sys.exit(main())
