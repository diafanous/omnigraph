# Weekly upstream sync, audit and approval

This fork follows `ModernRelay/omnigraph`. Once a week a GitHub Action checks
for new upstream commits, audits them, and opens a pull request. Nothing
reaches `main` until you merge that pull request. Merging it is your approval.

## What runs

| Workflow | When | What it does |
|---|---|---|
| `personal-upstream-sync` | Mondays 13:17 UTC, or **Run workflow** in the Actions tab | Merges `upstream/main` into `sync/upstream-<sha>`, runs `personal/sync/audit.py`, opens a PR |
| `personal-fork-guard` | Every push to `main` | Disables upstream's own workflows (CI, release, publish, nightlies) in this fork |

## What the audit checks

The audit never builds or runs upstream code. The sync code is copied out of
the tree before the merge, so an upstream change cannot alter its own audit.

1. **Signatures.** How many upstream commits GitHub marks as verified.
2. **Sensitive paths.** Changes to CI workflows, `build.rs`, install scripts,
   Docker and deploy files, cargo config, `deny.toml`, manifests and lock file,
   and agent-facing text (`skills/`, `AGENTS.md`, `CLAUDE.md`, `.claude/`).
3. **Fork-owned paths.** Upstream must never change `personal/` or the
   `personal-*` workflows. If it does, the verdict is FAIL.
4. **Dependencies.** Crates added to `Cargo.lock`. A crate from any source other
   than crates.io is FAIL.
5. **Added lines.** Pipe-to-shell installs, `Invoke-Expression`,
   `pull_request_target`, long encoded blobs, literal IP addresses, new process
   spawns. Prompt-injection phrases in agent-facing files are FAIL.
6. **cargo-deny** 0.20.2 (pinned, checksum verified): RustSec advisories,
   banned crates, licenses, sources. A failure is FAIL.
7. **gitleaks** 8.30.1 (pinned, checksum verified): secrets in the new commits.
   A finding is FAIL.

## Verdicts and what you do

| Label | Meaning | Your action |
|---|---|---|
| `audit:pass` | No blocking finding, no sensitive path changed | Skim the PR, then merge |
| `audit:review` | No blocking finding, but sensitive paths changed | Open **Files changed**, read each listed sensitive file, then merge or close |
| `audit:fail` | Blocking finding | Do not merge. Close the PR, or ask an agent to investigate |

Read changes to `skills/omnigraph/` with care: that text becomes your agents'
Omnigraph instructions after you export the skill.

After you merge: pull locally (`git pull`) and, if a new release exists, run
`personal/tools/update-cli.ps1` (verifies the download and runs the branch
smoke test). Export the skill again only after you read its diff.

## Automatic merge (off)

Set the repository variable `AUTO_MERGE_ON_PASS` to `true`
(Settings > Secrets and variables > Actions > Variables) to merge `audit:pass`
PRs without you. REVIEW and FAIL always wait. Leave it off unless you want
that trade-off.

## When the Action cannot push

`GITHUB_TOKEN` cannot push commits that change `.github/workflows/`. When
upstream changes a workflow, the Action opens an issue instead. Fix it in
one of two ways:

- Run the sync on your machine (your `gh` login has the `workflow` scope):

  ```
  git checkout main
  git pull
  python personal/sync/sync.py
  ```

- Or add a secret `SYNC_TOKEN`: a fine-grained token for this repository only,
  with Contents, Pull requests and Workflows set to read and write.

A merge conflict also opens an issue. Resolve it locally, then run
`python personal/sync/audit.py --base <old upstream sha> --head upstream/main`.

## Local commands

```
python personal/sync/sync.py --dry-run    # audit only, push nothing
python personal/sync/sync.py              # full run from your machine
python personal/sync/audit.py --base v0.15.0 --head upstream/main --out audit-out
```

Locally, cargo-deny and gitleaks run only if they are on PATH; otherwise the
report says they were skipped and the CI run covers them.
