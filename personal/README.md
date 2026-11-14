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
| `tools/update-cli.ps1` | Verifies and smoke-tests a new release (or, with `-Binary`, a local build) before you install it. |
| `tools/build-windows.ps1` | Builds the fork's patched Windows binaries, smoke-tests them, installs with `-Install`. |
| `WINDOWS.md` | The Windows path fix this fork carries, and how it was tested. |

## Quick start (Windows)

1. Build and install the fork's CLI (already done on this machine,
   `%USERPROFILE%\.omnigraph\bin`):
   `powershell -ExecutionPolicy Bypass -File personal\tools\build-windows.ps1 -Install`
2. Start the inbox: double-click `personal\inbox\start-inbox.cmd`.
3. Upload a file. The first upload creates the graph at `..\knowledge\graph.omni`.

## Windows fix

Stock releases up to 0.15.0 cannot create graph branches or plan clusters on
Windows. This fork carries a small source fix and builds its own Windows
binaries; see [WINDOWS.md](WINDOWS.md). Build, test and install:

```
powershell -ExecutionPolicy Bypass -File personal\tools\build-windows.ps1 -Install
```
