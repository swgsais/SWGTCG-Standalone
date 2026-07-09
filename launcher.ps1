<#
  SWGTCG-Standalone -- offline single-player launcher (server-login model).

  Directory-independent: every path resolves from $PSScriptRoot, so the whole
  SWGTCG-Standalone folder can be zipped, copied to any 64-bit Windows PC, and run.

  HOW IT WORKS
  ------------
  The retail client greys out the game modes until it has logged in. This package
  ships a tiny LOCAL login server (in _ext, bundled with its own Python runtime).
  The launcher starts that server and launches the client with auto-login args, so
  the Login screen is bypassed and PLAY becomes usable -- then Tutorials / Scenarios
  / Skirmish run entirely on your machine (no internet, no real account).

  ONE-TIME REQUIREMENT: two lines in the Windows hosts file must point the client's
  login host at 127.0.0.1. Run "Add Hosts Entries.cmd" once (it self-elevates), or
  add them by hand (see README.md). The launcher checks and warns if they're missing.
#>
$ErrorActionPreference = 'Stop'

$Root    = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Game    = Join-Path $Root 'TCGStandalone'
$Exe     = Join-Path $Game 'SWGTCGGame.exe'
$Manager = Join-Path $Root 'collectionmanager\index.html'
$Backups = Join-Path $Root 'Backups'
$Ext     = Join-Path $Root '_ext'
$PyExe   = Join-Path $Ext 'python\python.exe'
$Server  = Join-Path $Ext 'server\swgtcg_server.py'
$SrvCwd  = Join-Path $Ext 'server'
$Db      = Join-Path $Ext 'server\swgtcg.db'
$HostsFile = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
$HostNames = @('sdkccg-02-04.station.sony.com','sdkccg-02-11.station.sony.com')

# Auto-login args: --host resolves (via hosts) to 127.0.0.1 = our local server.
# The server binds the fixed sessionID below to the single StandAloneUser account (it
# refreshes that session on every boot), so login always lands on StandAloneUser -- which
# owns all four starter decks (Imperial / Jedi / Rebel / Sith). characterID is cosmetic here.
$GameArgs = @(
    '--realm=production',
    '--host=sdkccg-02-04.station.sony.com',
    '--username=StandAloneUser',
    '--sessionID=deadbeefdeadbeef',
    '--challenge=cafebabecafebabe',
    '--characterID=1'
)

function Test-Install {
    if ((Test-Path $Exe) -and (Test-Path $PyExe) -and (Test-Path $Server)) { return $true }
    Write-Host ""
    Write-Host "ERROR: package is incomplete. Expected:" -ForegroundColor Red
    Write-Host "  $Exe"
    Write-Host "  $PyExe"
    Write-Host "  $Server"
    return $false
}

function Test-Hosts {
    $txt = Get-Content $HostsFile -ErrorAction SilentlyContinue
    foreach ($h in $HostNames) {
        $hit = $txt | Where-Object { $_ -notmatch '^\s*#' -and $_ -match [regex]::Escape($h) }
        if (-not $hit) { return $false }
    }
    return $true
}

function Test-ServerUp {
    $c = Get-NetTCPConnection -LocalPort 16782 -State Listen -ErrorAction SilentlyContinue
    return [bool]$c
}

function Start-Server {
    if (Test-ServerUp) { Write-Host "Local login server already running." -ForegroundColor DarkGray; return }
    $out = Join-Path $Ext 'server\server.log'
    Start-Process -FilePath $PyExe -ArgumentList @('-u', $Server) -WorkingDirectory $SrvCwd `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError (Join-Path $Ext 'server\server.err.log') | Out-Null
    for ($i = 0; $i -lt 15; $i++) { Start-Sleep -Milliseconds 400; if (Test-ServerUp) { break } }
    if (Test-ServerUp) { Write-Host "Local login server started (127.0.0.1:16782/16783)." -ForegroundColor Green }
    else { Write-Host "WARNING: login server did not open its port -- see _ext\server\server.err.log" -ForegroundColor Yellow }
}

# Remove SQLite sidecar journals (-wal/-shm/-journal). The server is force-killed on Stop,
# so a leftover journal from a prior run must never survive next to the account DB: with the
# DB swapped by the Collection Manager, a stale -wal would replay onto the new file and either
# revert edits or corrupt it. Safe to delete because the server uses rollback (DELETE) journal
# mode -- every committed write already lives in swgtcg.db itself, not in a sidecar.
function Remove-DbSidecars {
    foreach ($sfx in @('-wal', '-shm', '-journal')) {
        $s = $Db + $sfx
        if (Test-Path $s) { Remove-Item $s -Force -ErrorAction SilentlyContinue }
    }
}

function Stop-Server {
    $ours = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq (Resolve-Path $PyExe).Path }
    foreach ($p in $ours) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
    if ($ours) { Start-Sleep -Milliseconds 300 }   # let the OS release the file handle before we clean up
    Remove-DbSidecars
    if ($ours) { Write-Host "Login server stopped." -ForegroundColor Green } else { Write-Host "Login server was not running." -ForegroundColor DarkGray }
}

function Start-Game {
    if (-not (Test-Install)) { return }
    if (-not (Test-Hosts)) {
        Write-Host ""
        Write-Host "!! hosts entries are MISSING -- the client will NOT be able to log in and PLAY will stay greyed out." -ForegroundColor Red
        Write-Host "   Run 'Add Hosts Entries.cmd' once (it self-elevates), or add these two lines to" -ForegroundColor Yellow
        Write-Host "   $HostsFile :" -ForegroundColor Yellow
        foreach ($h in $HostNames) { Write-Host "        127.0.0.1   $h" -ForegroundColor White }
        $go = Read-Host "Launch anyway? (y/N)"
        if ($go -notmatch '^[Yy]') { return }
    }
    Start-Server
    Start-Sleep -Seconds 2
    Write-Host ""
    Write-Host "Launching SWGTCG (auto-login, offline single-player)..." -ForegroundColor Green
    Write-Host "The Login screen is bypassed. When the lobby appears:" -ForegroundColor Cyan
    Write-Host "  left-edge Navigator -> PLAY -> Tutorials / Scenarios -> Skirmish -> Begin." -ForegroundColor Cyan
    Start-Process -FilePath $Exe -ArgumentList $GameArgs -WorkingDirectory $Game | Out-Null
}

function Open-Manager {
    if (-not (Test-Path $Manager)) { Write-Host "Collection Manager not found at $Manager" -ForegroundColor Red; return }
    Stop-Server   # release swgtcg.db so edits can be saved without conflict
    Start-Process $Manager | Out-Null
    Write-Host "Opened the Collection & Deck Manager (login server stopped for safe editing)." -ForegroundColor Green
    Write-Host "  In it: 'Open Account DB' -> _ext\server\swgtcg.db, edit, then 'Save to game'." -ForegroundColor Cyan
    Write-Host "  If Save DOWNLOADS the file instead, use menu [6] to apply it, then [1] Play." -ForegroundColor Cyan
}

function Apply-EditedDb {
    Stop-Server
    $dl = Get-ChildItem (Join-Path $env:USERPROFILE 'Downloads') -Filter 'swgtcg*.db' -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $dl) { Write-Host "No edited swgtcg*.db found in your Downloads folder -- Save it from the manager first." -ForegroundColor Yellow; return }
    Remove-DbSidecars                       # drop any stale journal BEFORE swapping in the new image...
    Copy-Item $dl.FullName $Db -Force
    Remove-DbSidecars                       # ...and after, so nothing from the old db replays onto it
    Write-Host "Applied '$($dl.Name)' (newest download) -> _ext\server\swgtcg.db. Choose [1] Play to use it." -ForegroundColor Green
}

function Backup-Saves {
    New-Item -ItemType Directory -Force -Path $Backups | Out-Null
    $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
    $zip   = Join-Path $Backups "saves_$stamp.zip"
    $paths = @()
    foreach ($p in @((Join-Path $Ext 'server\swgtcg.db'), (Join-Path $Game 'data\collections'), (Join-Path $Game 'data\decks'))) {
        if (Test-Path $p) { $paths += $p }
    }
    if ($paths.Count -eq 0) { Write-Host "Nothing to back up." -ForegroundColor Yellow; return }
    Compress-Archive -Path $paths -DestinationPath $zip -Force
    Write-Host "Backed up account DB + local collections/decks -> $zip" -ForegroundColor Green
}

function Restore-Saves {
    Stop-Server   # release the DB before overwriting it
    if (-not (Test-Path $Backups)) { Write-Host "No Backups folder yet." -ForegroundColor Yellow; return }
    $zips = @(Get-ChildItem $Backups -Filter *.zip | Sort-Object LastWriteTime -Descending)
    if ($zips.Count -eq 0) { Write-Host "No backups found." -ForegroundColor Yellow; return }
    for ($k = 0; $k -lt $zips.Count; $k++) { Write-Host ("  [{0}] {1}" -f $k, $zips[$k].Name) }
    $sel = Read-Host "Number to restore (blank = cancel)"
    if ($sel -match '^\d+$' -and [int]$sel -lt $zips.Count) {
        $tmp = Join-Path $env:TEMP ("swgrestore_" + [guid]::NewGuid().ToString('N'))
        Expand-Archive -Path $zips[[int]$sel].FullName -DestinationPath $tmp -Force
        if (Test-Path (Join-Path $tmp 'swgtcg.db')) { Remove-DbSidecars; Copy-Item (Join-Path $tmp 'swgtcg.db') $Db -Force; Remove-DbSidecars }
        foreach ($d in @('collections','decks')) { if (Test-Path (Join-Path $tmp $d)) { Copy-Item (Join-Path $tmp $d) (Join-Path $Game 'data') -Recurse -Force } }
        Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
        Write-Host "Restored $($zips[[int]$sel].Name)" -ForegroundColor Green
    } else { Write-Host "Cancelled." }
}

$running = $true
try {
    while ($running) {
        $hostsOk = Test-Hosts
        Write-Host ""
        Write-Host "======================================================" -ForegroundColor DarkCyan
        Write-Host "   Star Wars Galaxies TCG -- Standalone (offline)  V2"  -ForegroundColor White
        Write-Host "======================================================" -ForegroundColor DarkCyan
        Write-Host ("   hosts entries: " + $(if ($hostsOk) { "OK" } else { "MISSING (run 'Add Hosts Entries.cmd')" })) -ForegroundColor $(if ($hostsOk) { 'Green' } else { 'Yellow' })
        Write-Host ("   login server : " + $(if (Test-ServerUp) { "running" } else { "stopped" })) -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "   [1] Play        (auto-login -> offline vs AI)"
        Write-Host "   [2] Collection & Deck Manager"
        Write-Host "   [3] Backup saves      [4] Restore saves"
        Write-Host "   [5] Stop login server [6] Apply edited DB from Downloads"
        Write-Host "   [Q] Quit (stops the login server)"
        $choice = Read-Host "Choose"
        switch ($choice.Trim().ToUpper()) {
            '1'     { Start-Game }
            '2'     { Open-Manager }
            '3'     { Backup-Saves }
            '4'     { Restore-Saves }
            '5'     { Stop-Server }
            '6'     { Apply-EditedDb }
            'Q'     { $running = $false }
            default { Write-Host "Unknown choice." -ForegroundColor Yellow }
        }
    }
}
finally {
    Stop-Server
}
