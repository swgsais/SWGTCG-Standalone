<#
  SWGTCG-Standalone -- shared launcher core (paths + operations).

  Dot-sourced by BOTH front-ends so the delicate bits (server start/stop, DB sidecar
  cleanup, backup/restore) live in ONE place:
    * launcher.ps1       -- the text menu (double-click "Play SWGTCG.cmd")
    * launcher-gui.ps1   -- the themed GUI ("SWGTCG Launcher.cmd")

  This file defines PATHS + FUNCTIONS ONLY -- it never prompts and never runs a UI loop,
  so each front-end owns its own interaction (Read-Host vs. dialogs). Directory-independent:
  every path resolves from this file's own folder, so the whole SWGTCG-Standalone folder can
  be zipped, copied to any 64-bit Windows PC, and run.
#>
$ErrorActionPreference = 'Stop'

$Root      = if ($PSScriptRoot) { $PSScriptRoot } else { Split-Path -Parent $MyInvocation.MyCommand.Path }
$Game      = Join-Path $Root 'TCGStandalone'
$Exe       = Join-Path $Game 'SWGTCGGame.exe'
$Manager   = Join-Path $Root 'collectionmanager\index.html'
$Backups   = Join-Path $Root 'Backups'
$Ext       = Join-Path $Root '_ext'
$PyExe     = Join-Path $Ext 'python\python.exe'
$Server    = Join-Path $Ext 'server\swgtcg_server.py'
$SrvCwd    = Join-Path $Ext 'server'
$Db        = Join-Path $Ext 'server\swgtcg.db'
$ArtDir    = Join-Path $Root 'collectionmanager\art\images\card'
$HostsFile = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'
$HostNames = @('sdkccg-02-04.station.sony.com','sdkccg-02-11.station.sony.com')
$GameProc  = [System.IO.Path]::GetFileNameWithoutExtension($Exe)   # 'SWGTCGGame'
$LauncherVersion = 'V4'

# Auto-login args: --host resolves (via hosts) to 127.0.0.1 = our local server. The server binds the
# fixed sessionID below to the single StandAloneUser account (refreshed on every boot), so login always
# lands on StandAloneUser -- which owns all four starter decks. characterID is cosmetic here.
$GameArgs = @(
    '--realm=production',
    '--host=sdkccg-02-04.station.sony.com',
    '--username=StandAloneUser',
    '--sessionID=deadbeefdeadbeef',
    '--challenge=cafebabecafebabe',
    '--characterID=1'
)

# Player-2 auto-login args for the 1v1 PvP test. The server binds this session to the Player2 account when
# started with SWGTCG_ENABLE_P2 (see db.ensure_second_player); distinct sessionID/challenge from Player 1.
$GameArgsP2 = @(
    '--realm=production',
    '--host=sdkccg-02-04.station.sony.com',
    '--username=Player2',
    '--sessionID=feedfacefeedface',
    '--challenge=baddecafbaddecaf',
    '--characterID=2'
)

# ---- read-only status probes ---------------------------------------------------------------------
function Test-Install {
    return ((Test-Path $Exe) -and (Test-Path $PyExe) -and (Test-Path $Server))
}

function Test-Hosts {
    $txt = Get-Content $HostsFile -ErrorAction SilentlyContinue
    foreach ($h in $HostNames) {
        $hit = $txt | Where-Object { $_ -notmatch '^\s*#' -and $_ -match [regex]::Escape($h) }
        if (-not $hit) { return $false }
    }
    return $true
}

# Fast port-listen probe via the managed IP-stack API (~1ms) instead of Get-NetTCPConnection (~100-400ms).
# The slow cmdlet, called on the GUI's status timer, was blocking the UI thread every tick and making the
# mouse feel sluggish. Falls back to the cmdlet only if the managed call is unavailable.
function Test-ServerUp {
    try {
        $props = [System.Net.NetworkInformation.IPGlobalProperties]::GetIPGlobalProperties()
        foreach ($ep in $props.GetActiveTcpListeners()) { if ($ep.Port -eq 16782) { return $true } }
        return $false
    } catch {
        return [bool](Get-NetTCPConnection -LocalPort 16782 -State Listen -ErrorAction SilentlyContinue)
    }
}

# Managed process query (avoids Get-Process cmdlet overhead on the status timer).
function Test-GameRunning {
    return ([System.Diagnostics.Process]::GetProcessesByName($GameProc).Length -gt 0)
}

function Test-CardArtExtracted { return (Test-Path $ArtDir) }

function Test-CardsRcc { return (Test-Path (Join-Path $Game 'cards.rcc')) }

# ---- login server lifecycle --------------------------------------------------------------------
function Start-Server {
    if (Test-ServerUp) { return $true }
    $out = Join-Path $Ext 'server\server.log'
    Start-Process -FilePath $PyExe -ArgumentList @('-u', $Server) -WorkingDirectory $SrvCwd `
        -WindowStyle Hidden -RedirectStandardOutput $out -RedirectStandardError (Join-Path $Ext 'server\server.err.log') | Out-Null
    for ($i = 0; $i -lt 15; $i++) { Start-Sleep -Milliseconds 400; if (Test-ServerUp) { break } }
    return (Test-ServerUp)
}

# Remove SQLite sidecar journals (-wal/-shm/-journal). The server is force-killed on Stop, so a leftover
# journal from a prior run must never survive next to the account DB: with the DB swapped by the Collection
# Manager, a stale -wal would replay onto the new file and either revert edits or corrupt it. Safe to delete
# because the server uses rollback (DELETE) journal mode -- every committed write already lives in swgtcg.db.
function Remove-DbSidecars {
    foreach ($sfx in @('-wal', '-shm', '-journal')) {
        $s = $Db + $sfx
        if (Test-Path $s) { Remove-Item $s -Force -ErrorAction SilentlyContinue }
    }
}

function Stop-Server {
    $stopped = $false
    # 1) our bundled python by exe path (the normal case)
    $ours = Get-CimInstance Win32_Process -Filter "Name='python.exe'" -ErrorAction SilentlyContinue |
            Where-Object { $_.ExecutablePath -eq (Resolve-Path $PyExe -ErrorAction SilentlyContinue).Path }
    foreach ($p in $ours) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue; $stopped = $true }
    # 2) fallback: kill whatever is actually holding the server ports, so the Manager always frees the DB
    #    even if the process/path match above missed (e.g. server started differently).
    foreach ($port in 16782, 16783) {
        Get-NetTCPConnection -LocalPort $port -State Listen -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty OwningProcess -Unique |
            ForEach-Object { Stop-Process -Id $_ -Force -ErrorAction SilentlyContinue; $stopped = $true }
    }
    if ($stopped) { Start-Sleep -Milliseconds 300 }   # let the OS release the file handle before we clean up
    Remove-DbSidecars
    return $stopped
}

# ---- operations (PURE: no prompts -- callers own their UI) --------------------------------------
# Start the login server (if needed) and launch the client already auto-logged-in. Returns $true if the
# client was launched, $false if the install is incomplete. The hosts check is the CALLER's job (advisory).
function Start-Game {
    if (-not (Test-Install)) { return $false }
    Start-Server | Out-Null
    Start-Sleep -Seconds 2
    Start-Process -FilePath $Exe -ArgumentList $GameArgs -WorkingDirectory $Game | Out-Null
    return $true
}

# ---- 1v1 PvP test (Stage-0 infra): two client instances against the local server -----------------
# Restarts the login server in MULTIPLAYER mode (mints the Player2 account + turns on the online board
# launch path) then opens TWO client windows -- StandAloneUser (P1) + Player2 (P2). EXPLORATORY: the
# 2-player board/deal was proven on tcghost+DLL, NOT yet on this standalone client, so expect to reach the
# Casual lobby / match handshake first; the board, deck-bind and mulligan are the next stages. If the retail
# client is single-instance (2nd window won't open), that's the first thing to solve. Returns $true on launch.
function Start-TwoPlayerPvP {
    if (-not (Test-Install)) { return $false }
    Stop-Server | Out-Null                     # fresh server so the MP env applies
    Start-Sleep -Milliseconds 400
    $mp = [ordered]@{
        SWGTCG_ENABLE_P2       = '1'           # server mints the Player2 account + fixed session
        SWGTCG_LAUNCH_BOARD    = '1'           # enter the board on both-ready
        SWGTCG_DECK58          = '1'           # mirrored-engine deal (each client materializes its own deck)
        SWGTCG_SEAT_ACCTS      = '1,2'         # acct 1 = player 1, acct 2 = player 2
        SWGTCG_LOGIN_SCREEN    = 'casual'      # both land in the Casual lobby to Create / Join
        SWGTCG_EQ_SETUP        = '1'           # send EQ SetupGame(80007) position/team maps -> seat players (fix mPosition=-100)
        SWGTCG_DECK58_58_FIRST = '0'           # send 58 (deck) AFTER 67 (setup) so the players/game exist first (fix player-null)
        # pump ON (default) carries the 117 SM-ticks + the CardSelected(60) starting-mission pick (fix phase-5 stall)
    }
    foreach ($k in $mp.Keys) { Set-Item "env:$k" $mp[$k] }
    $out = Join-Path $Ext 'server\server.log'
    Start-Process -FilePath $PyExe -ArgumentList @('-u', $Server) -WorkingDirectory $SrvCwd -WindowStyle Hidden `
        -RedirectStandardOutput $out -RedirectStandardError (Join-Path $Ext 'server\server.err.log') | Out-Null
    for ($i = 0; $i -lt 15; $i++) { Start-Sleep -Milliseconds 400; if (Test-ServerUp) { break } }
    foreach ($k in $mp.Keys) { Remove-Item "env:$k" -ErrorAction SilentlyContinue }   # keep the launcher's own env clean
    if (-not (Test-ServerUp)) { return $false }
    Start-Process -FilePath $Exe -ArgumentList $GameArgs   -WorkingDirectory $Game | Out-Null   # Player 1
    Start-Sleep -Seconds 2
    Start-Process -FilePath $Exe -ArgumentList $GameArgsP2 -WorkingDirectory $Game | Out-Null   # Player 2
    return $true
}

# Open the account DB's folder in Explorer with swgtcg.db highlighted, so the user never has to hunt for
# the file the Collection Manager's "Open Account DB" picker asks for.
function Open-DbFolder {
    if (Test-Path $Db) { Start-Process explorer.exe -ArgumentList ('/select,"{0}"' -f $Db) | Out-Null }
    elseif (Test-Path $SrvCwd) { Start-Process explorer.exe -ArgumentList ('"{0}"' -f $SrvCwd) | Out-Null }
}

# Stop the login server (release swgtcg.db), open the offline manager, and pop the DB folder. Returns
# $false only if the manager HTML is missing.
function Open-Manager {
    if (-not (Test-Path $Manager)) { return $false }
    Stop-Server | Out-Null
    Start-Process $Manager | Out-Null
    Open-DbFolder
    return $true
}

# Apply the newest edited swgtcg*.db from Downloads over the account DB. Returns the applied file name,
# or $null if none was found.
function Apply-EditedDb {
    Stop-Server | Out-Null
    $dl = Get-ChildItem (Join-Path $env:USERPROFILE 'Downloads') -Filter 'swgtcg*.db' -ErrorAction SilentlyContinue |
          Sort-Object LastWriteTime -Descending | Select-Object -First 1
    if (-not $dl) { return $null }
    Remove-DbSidecars                       # drop any stale journal BEFORE swapping in the new image...
    Copy-Item $dl.FullName $Db -Force
    Remove-DbSidecars                       # ...and after, so nothing from the old db replays onto it
    return $dl.Name
}

# Extract booster card art from the client's cards.rcc (~140MB, one-time). Returns $true on run,
# $false if the extractor or cards.rcc is missing.
function Extract-CardArt {
    $tool = Join-Path $Ext 'tools\extract_card_art.py'
    if (-not (Test-Path $tool)) { return $false }
    if (-not (Test-CardsRcc))   { return $false }
    & $PyExe $tool
    return $true
}

# Zip the account DB + local collections/decks into Backups\saves_<stamp>.zip. Returns the zip path,
# or $null if there was nothing to back up. Callers supply the timestamp so this stays deterministic.
function Backup-Saves {
    param([string]$Stamp)
    if (-not $Stamp) { $Stamp = Get-Date -Format 'yyyyMMdd_HHmmss' }
    New-Item -ItemType Directory -Force -Path $Backups | Out-Null
    $zip   = Join-Path $Backups "saves_$Stamp.zip"
    $paths = @()
    foreach ($p in @($Db, (Join-Path $Game 'data\collections'), (Join-Path $Game 'data\decks'))) {
        if (Test-Path $p) { $paths += $p }
    }
    if ($paths.Count -eq 0) { return $null }
    Compress-Archive -Path $paths -DestinationPath $zip -Force
    return $zip
}

function Get-Backups {
    if (-not (Test-Path $Backups)) { return @() }
    return @(Get-ChildItem $Backups -Filter *.zip | Sort-Object LastWriteTime -Descending)
}

# Restore one backup zip over the account DB + local collections/decks. Returns $true on success.
function Restore-Backup {
    param([Parameter(Mandatory)][string]$ZipPath)
    Stop-Server | Out-Null
    if (-not (Test-Path $ZipPath)) { return $false }
    $tmp = Join-Path $env:TEMP ("swgrestore_" + [guid]::NewGuid().ToString('N'))
    Expand-Archive -Path $ZipPath -DestinationPath $tmp -Force
    if (Test-Path (Join-Path $tmp 'swgtcg.db')) { Remove-DbSidecars; Copy-Item (Join-Path $tmp 'swgtcg.db') $Db -Force; Remove-DbSidecars }
    foreach ($d in @('collections','decks')) {
        if (Test-Path (Join-Path $tmp $d)) { Copy-Item (Join-Path $tmp $d) (Join-Path $Game 'data') -Recurse -Force }
    }
    Remove-Item $tmp -Recurse -Force -ErrorAction SilentlyContinue
    return $true
}

# Run the self-elevating hosts-entry helper (adds the two 127.0.0.1 lines the client needs to log in).
function Add-HostsEntries {
    $h = Join-Path $Ext 'add-hosts.ps1'
    if (-not (Test-Path $h)) { return $false }
    Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-File', $h) | Out-Null
    return $true
}
