# personal/ — what this fork adds

This is a personal fork of [ModernRelay/omnigraph](https://github.com/ModernRelay/omnigraph).
Everything the fork adds lives in `personal/` and in the two
`.github/workflows/personal-*.yml` files, so upstream merges do not conflict.
No graph data, uploaded file or password is ever stored in this repository:
those live in a private `knowledge` folder outside the clone.

| Path | Purpose |
|---|---|
| `inbox/` | **Omnigraph Inbox**: a local web app to upload files and connect an email folder. Each file becomes a `Document` node. See [inbox/README.md](inbox/README.md). |
| `ontology/` | Starter schema (`inbox.pg`) and stored queries copied into the knowledge folder on first run. |
| `ONTOLOGY-AGENTS.md` | The procedure agents follow to build the ontology from Documents, with reviewer approval. |
| `sync/` | Weekly upstream sync and security audit. See [SYNC.md](SYNC.md). |
| `tools/update-cli.ps1` | Verifies and smoke-tests a new release before you install it. |

## Quick start (Windows)

1. Install the CLI from a verified release (already done on this machine,
   `%USERPROFILE%\.omnigraph\bin`). To check for updates:
   `powershell -ExecutionPolicy Bypass -File personal\tools\update-cli.ps1`
2. Start the inbox: double-click `personal\inbox\start-inbox.cmd`.
3. Upload a file. The first upload creates the graph at `..\knowledge\graph.omni`.

## Known limit: Windows and graph branches

With the 0.15.0 Windows build, `branch create` fails on a local graph (a
`\\?\` extended-length path reaches Lance as a URL; related upstream issue
[#907](https://github.com/ModernRelay/omnigraph/issues/907)) and left the test
graph unreadable. Main-branch operations work: init, load, query, search,
Blob read and write, export, schema plan and apply. Until a build passes
`update-cli.ps1`'s smoke test, agents stage changes as proposal files that a
reviewer loads (see `ONTOLOGY-AGENTS.md`).
