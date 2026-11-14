<#
.SYNOPSIS
  Check for a new Omnigraph release, verify it, smoke-test it, and optionally install it.

.DESCRIPTION
  1. Reads the latest release of ModernRelay/omnigraph from the GitHub API.
  2. Downloads the Windows zip and its .sha256 file to a new temp folder.
  3. Stops if the SHA-256 does not match.
  4. Runs a smoke test with the NEW binary on a scratch graph only:
     init, load, query, branch create, load on the branch, branch merge.
     It reports whether graph branches work on this machine.
  5. With -Install: moves the current binaries to bin-<old version> and
     installs the new ones into %USERPROFILE%\.omnigraph\bin.

  It never touches your knowledge graph. Upgrade the graph format only after you
  read the release notes (omnigraph upgrade --check).

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File personal\tools\update-cli.ps1
  powershell -ExecutionPolicy Bypass -File personal\tools\update-cli.ps1 -Install
#>
param(
    [switch]$Install,
    [string]$Tag = ""
)
$ErrorActionPreference = "Stop"
$repo = "ModernRelay/omnigraph"
$binDir = Join-Path $env:USERPROFILE ".omnigraph\bin"
$asset = "omnigraph-windows-x86_64"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12

$api = if ($Tag) { "https://api.github.com/repos/$repo/releases/tags/$Tag" } else { "https://api.github.com/repos/$repo/releases/latest" }
$rel = Invoke-RestMethod -Uri $api -Headers @{ "User-Agent" = "omnigraph-update-check" }
$current = "none"
$exe = Join-Path $binDir "omnigraph.exe"
if (Test-Path $exe) { $current = ((& $exe version) | Select-Object -First 1) }
Write-Host "installed: $current"
Write-Host "latest:    $($rel.tag_name) ($($rel.published_at))"

$zipUrl = ($rel.assets | Where-Object { $_.name -eq "$asset.zip" }).browser_download_url
$sumUrl = ($rel.assets | Where-Object { $_.name -eq "$asset.sha256" }).browser_download_url
if (-not $zipUrl -or -not $sumUrl) { throw "release $($rel.tag_name) has no $asset.zip/.sha256" }

$work = Join-Path $env:TEMP ("omnigraph-update-" + [guid]::NewGuid().ToString("N").Substring(0, 8))
New-Item -ItemType Directory -Path $work | Out-Null
$zip = Join-Path $work "$asset.zip"
Invoke-WebRequest -UseBasicParsing -Uri $zipUrl -OutFile $zip
$sumFile = Join-Path $work "$asset.sha256"
Invoke-WebRequest -UseBasicParsing -Uri $sumUrl -OutFile $sumFile
$expected = ((Get-Content -Raw $sumFile).Trim() -split "\s+")[0].ToLower()
$actual = (Get-FileHash $zip -Algorithm SHA256).Hash.ToLower()
if ($expected -ne $actual) { throw "SHA-256 mismatch: expected $expected, got $actual. Nothing installed." }
Write-Host "sha256 OK: $actual"

$new = Join-Path $work "bin"
Expand-Archive -Path $zip -DestinationPath $new
$og = Join-Path $new "omnigraph.exe"
Write-Host ("new binary: " + ((& $og version) | Select-Object -First 1))

# ---- smoke test on a scratch graph
$t = Join-Path $work "smoke"
New-Item -ItemType Directory -Path $t | Out-Null
Set-Content -Path (Join-Path $t "s.pg") -Encoding ascii -Value "node Note {`n  slug: String @key`n  body: String`n}"
Set-Content -Path (Join-Path $t "q.gq") -Encoding ascii -Value "query notes() {`n  match { `$n: Note }`n  return { `$n.slug as slug }`n  order { slug asc }`n}"
Set-Content -Path (Join-Path $t "a.jsonl") -Encoding ascii -Value '{"type":"Note","data":{"slug":"a","body":"main"}}'
Set-Content -Path (Join-Path $t "b.jsonl") -Encoding ascii -Value '{"type":"Note","data":{"slug":"b","body":"branch"}}'
$g = Join-Path $t "g.omni"
function Step($name, [scriptblock]$block) {
    # The CLI writes progress to stderr; in Windows PowerShell that must not stop the script.
    $ErrorActionPreference = "Continue"
    $out = & $block 2>&1 | Out-String
    $ok = $LASTEXITCODE -eq 0
    $label = "FAIL"; if ($ok) { $label = "ok" }
    Write-Host ("  {0,-28} {1}" -f $name, $label)
    if (-not $ok) { Write-Host ("    " + ($out.Trim() -split "`n" | Select-Object -Last 2) -join "`n    ") }
    return $ok
}
Write-Host "smoke test (scratch graph only):"
$core = (Step "init" { & $og init --schema (Join-Path $t "s.pg") $g }) -and
        (Step "load main" { & $og load --data (Join-Path $t "a.jsonl") --mode merge --target $g }) -and
        (Step "query main" { & $og query notes --query (Join-Path $t "q.gq") --target $g })
$branches = $core -and
        (Step "branch create" { & $og branch create smoke --target $g }) -and
        (Step "load on branch" { & $og load --data (Join-Path $t "b.jsonl") --mode merge --branch smoke --target $g }) -and
        (Step "branch merge" { & $og branch merge smoke --into main --target $g })
Write-Host ""
if (-not $core) { Write-Host "RESULT: core operations FAILED. Do not install."; exit 2 }
if ($branches) { Write-Host "RESULT: core ok, graph branches WORK on this machine. Agents may switch to branches (personal/ONTOLOGY-AGENTS.md)." }
else { Write-Host "RESULT: core ok, graph branches still FAIL on this machine. Keep the proposal-file workflow." }

if ($Install) {
    if (Test-Path $binDir) {
        $old = ($current -replace "[^0-9A-Za-z.\-]", "_")
        $keep = Join-Path (Split-Path $binDir) ("bin-" + $old)
        if (Test-Path $keep) { $keep = $keep + "-" + (Get-Date -Format "yyyyMMddHHmm") }
        Move-Item $binDir $keep
        Write-Host "previous binaries kept in $keep"
    }
    New-Item -ItemType Directory -Path $binDir | Out-Null
    Copy-Item (Join-Path $new "*.exe") $binDir
    Write-Host "installed $($rel.tag_name) into $binDir"
    Write-Host "Next: read the release notes, then refresh the agent skill:"
    Write-Host "  omnigraph skill export `"$env:USERPROFILE\.claude\skills\omnigraph`" --force"
} else {
    Write-Host "Not installed. Run again with -Install to install."
}
Write-Host "work folder: $work"
