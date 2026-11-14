<#
.SYNOPSIS
  Build the fork's patched Windows binaries, smoke-test them, and optionally install them.

.DESCRIPTION
  Runs the same build as upstream's Windows release:
    cargo build --release --locked -p omnigraph-cli -p omnigraph-server --features omnigraph-server/aws
  then runs update-cli.ps1 -Binary target\release (branch smoke test on a
  scratch graph). With -Install, installs into %USERPROFILE%\.omnigraph\bin and
  keeps the previous binaries in bin-<old version>.

  Needs: Rust (rust-toolchain.toml pins the version), Visual Studio C++ build
  tools, CMake, and protoc. protoc is looked up in PROTOC, then
  %USERPROFILE%\.omnigraph\tools\protoc\bin\protoc.exe, then PATH.
  A first build takes about 25 minutes; later builds are faster.

  Run it after you merge an upstream sync pull request.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File personal\tools\build-windows.ps1 -Install
#>
param([switch]$Install)
$ErrorActionPreference = "Stop"
$repo = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $repo

if (-not $env:PROTOC) {
    $local = Join-Path $env:USERPROFILE ".omnigraph\tools\protoc\bin\protoc.exe"
    if (Test-Path $local) { $env:PROTOC = $local }
    elseif (Get-Command protoc -ErrorAction SilentlyContinue) { $env:PROTOC = (Get-Command protoc).Source }
    else { throw "protoc not found. Install it (github.com/protocolbuffers/protobuf releases) or set PROTOC." }
}
$dirty = (git status --porcelain --untracked-files=no)
if ($dirty) { Write-Host "warning: uncommitted changes; the version stamp names HEAD, not your working tree" }
$env:OMNIGRAPH_SOURCE_VERSION = (git rev-parse HEAD).Trim()
$env:RUSTFLAGS = ""   # same as the release workflow
Write-Host "building $($env:OMNIGRAPH_SOURCE_VERSION) with protoc $env:PROTOC"
& cargo build --release --locked -p omnigraph-cli -p omnigraph-server --features omnigraph-server/aws
if ($LASTEXITCODE -ne 0) { throw "cargo build failed" }

$check = Join-Path $PSScriptRoot "update-cli.ps1"
$bin = Join-Path $repo "target\release"
if ($Install) { & $check -Binary $bin -Install } else { & $check -Binary $bin }
