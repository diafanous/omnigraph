# Windows fix carried by this fork

Release builds up to 0.15.0 fail on Windows in two places:

1. **Local graph branches.** `branch create` and `load --branch X --from main`
   fail with `Invalid table location`. In our test the failed attempt also left
   the graph unreadable.
2. **Clusters and the server.** `cluster plan` fails with
   `invalid file uri 'file://\\?\C:\...'` (upstream issue
   [#907](https://github.com/ModernRelay/omnigraph/issues/907)). The server
   is cluster-only, so it cannot be served.

## Cause

`std::fs::canonicalize` returns an extended-length path on Windows
(`\\?\C:\...`). Omnigraph used that spelling as the graph's storage root.
Lance and the cluster code then read it as URL text: the `?` starts a URL
query, and the path is lost. Separately, the cluster code builds roots with
`/` and compares them as strings with canonical paths that use `\`.

## Change

All in source, no new dependency:

| File | Change |
|---|---|
| `crates/omnigraph-storage/src/lib.rs` | New `canonicalize_path`, `simplify_verbatim_path` and `local_path_file_uri`. Storage identities use them. On Windows, `normalize_local_path` gives one spelling: no `\\?\` prefix and `/` separators (`C:/x/g.omni`). Two Windows unit tests. Other platforms are unchanged. |
| `crates/omnigraph/src/blob.rs` | External Blob path checks use `canonicalize_path`. |
| `crates/omnigraph-cluster/src/store.rs` | The cluster's canonical root uses `canonicalize_path` and `local_path_file_uri`. |
| `crates/omnigraph-storage/src/read_through.rs` | A Unix-only symlink test is marked `#[cfg(unix)]`, so the crate's tests compile on Windows. |

## Checks run on Windows 11 (2026-11-14)

- `update-cli.ps1` smoke test on a scratch graph passes: init, load, query,
  branch create, load on branch, branch diff, branch merge, merged row visible
  on main.
- The agent branch workflow passes on a copy of the real knowledge graph
  (created by 0.15.0): load on `agent/claude/test --from main`, diff, merge.
- `cluster plan` and `cluster apply` succeed in a folder whose name has a space.
  After the documented `cluster force-unlock`, `omnigraph-server` boots from
  that cluster: `/readyz` reports ready, with 1 of 1 graphs ready.
- The Inbox app adds a file, full-text search finds it, and `blob get` returns
  the original bytes.
- `cargo test -p omnigraph-storage --lib`: 118 pass. 6 fail, and the same 6
  fail without this change (existing upstream tests that assume Unix paths).
- Not checked: the full workspace test suite, HTTP writes through the server
  (the test cluster had no access policy), S3 or Azure storage, UNC network
  paths.

## Keeping it after upstream updates

The patch touches upstream files. A weekly sync pull request can conflict with
it. The sync then opens an issue instead of a PR; resolve the conflict by
keeping both changes. After you merge a sync PR, rebuild and test:

```
powershell -ExecutionPolicy Bypass -File personal\tools\build-windows.ps1 -Install
```

If upstream fixes the bug itself, the smoke test with a stock release passes
(`update-cli.ps1` without `-Binary`). Then you can drop the patch and go back
to release binaries.
