<#
  SWGTCG-Standalone -- offline single-player launcher (text menu, server-login model).

  Directory-independent: every path resolves from $PSScriptRoot, so the whole SWGTCG-Standalone
  folder can be zipped, copied to any 64-bit Windows PC, and run.

  HOW IT WORKS
  ------------
  The retail client greys out the game modes until it has logged in. This package ships a tiny LOCAL
  login server (in _ext, bundled with its own Python runtime). The launcher starts that server and
  launches the client with auto-login args, so the Login screen is bypassed and PLAY becomes usable --
  then Tutorials / Scenarios / Skirmish run entirely on your machine (no internet, no real account).

  ONE-TIME REQUIREMENT: two lines in the Windows hosts file must point the client's login host at
  127.0.0.1. Run "Add Hosts Entries.cmd" once (it self-elevates). The launcher checks and warns if missing.

  All operations live in launcher-core.ps1 (shared with the GUI, launcher-gui.ps1); this file is just the
  text menu + its prompts.
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-core.ps1')

function Invoke-Play {
    if (-not (Test-Install)) {
        Write-Host ""
        Write-Host "ERROR: package is incomplete. Expected:" -ForegroundColor Red
        Write-Host "  $Exe"; Write-Host "  $PyExe"; Write-Host "  $Server"
        return
    }
    if (-not (Test-Hosts)) {
        Write-Host ""
        Write-Host "!! hosts entries are MISSING -- the client will NOT be able to log in and PLAY will stay greyed out." -ForegroundColor Red
        Write-Host "   Run 'Add Hosts Entries.cmd' once (it self-elevates), or add these two lines to" -ForegroundColor Yellow
        Write-Host "   $HostsFile :" -ForegroundColor Yellow
        foreach ($h in $HostNames) { Write-Host "        127.0.0.1   $h" -ForegroundColor White }
        $go = Read-Host "Launch anyway? (y/N)"
        if ($go -notmatch '^[Yy]') { return }
    }
    Write-Host ""
    Write-Host "Launching SWGTCG (auto-login, offline single-player)..." -ForegroundColor Green
    Write-Host "The Login screen is bypassed. You land on the Home screen -- move to the LEFT edge:" -ForegroundColor Cyan
    Write-Host "  Navigator -> PLAY -> Tutorials / Scenarios / Skirmish -> Begin." -ForegroundColor Cyan
    Start-Game | Out-Null
}

function Invoke-Manager {
    if (-not (Test-Path $Manager)) { Write-Host "Collection Manager not found at $Manager" -ForegroundColor Red; return }
    # Offer to build booster card art if it hasn't been extracted yet (images are never shipped).
    if (-not (Test-CardArtExtracted)) {
        Write-Host ""
        Write-Host "The Boosters tab can show real card art, but the images aren't extracted yet." -ForegroundColor Yellow
        $go = Read-Host "Extract card images now from your cards.rcc? (~140MB, one-time) (y/N)"
        if ($go -match '^[Yy]') {
            if (-not (Extract-CardArt)) { Write-Host "Extractor or cards.rcc missing -- skipped." -ForegroundColor Red }
        } else { Write-Host "Skipped. Boosters will list cards without art until you run [7] Extract card art." -ForegroundColor DarkGray }
    }
    if (Open-Manager) {
        Write-Host "Opened the Collection & Deck Manager (login server stopped for safe editing)." -ForegroundColor Green
        Write-Host "  Opened its folder too -- swgtcg.db is highlighted in Explorer for the 'Open Account DB' picker." -ForegroundColor Cyan
        Write-Host "  In it: 'Open Account DB' -> _ext\server\swgtcg.db, edit, then 'Save to game'." -ForegroundColor Cyan
        Write-Host "  If Save DOWNLOADS the file instead, use menu [6] to apply it, then [1] Play." -ForegroundColor Cyan
    }
}

function Invoke-Backup {
    $zip = Backup-Saves
    if ($zip) { Write-Host "Backed up account DB + local collections/decks -> $zip" -ForegroundColor Green }
    else      { Write-Host "Nothing to back up." -ForegroundColor Yellow }
}

function Invoke-Restore {
    $zips = Get-Backups
    if ($zips.Count -eq 0) { Write-Host "No backups found." -ForegroundColor Yellow; return }
    for ($k = 0; $k -lt $zips.Count; $k++) { Write-Host ("  [{0}] {1}" -f $k, $zips[$k].Name) }
    $sel = Read-Host "Number to restore (blank = cancel)"
    if ($sel -match '^\d+$' -and [int]$sel -lt $zips.Count) {
        if (Restore-Backup -ZipPath $zips[[int]$sel].FullName) { Write-Host "Restored $($zips[[int]$sel].Name)" -ForegroundColor Green }
        else { Write-Host "Restore failed." -ForegroundColor Red }
    } else { Write-Host "Cancelled." }
}

function Invoke-Apply {
    $name = Apply-EditedDb
    if ($name) { Write-Host "Applied '$name' (newest download) -> _ext\server\swgtcg.db. Choose [1] Play to use it." -ForegroundColor Green }
    else       { Write-Host "No edited swgtcg*.db found in your Downloads folder -- Save it from the manager first." -ForegroundColor Yellow }
}

function Invoke-Extract {
    Write-Host "Extracting card art from cards.rcc (~140MB, one-time; images are NOT shipped)..." -ForegroundColor Cyan
    if (Extract-CardArt) { Write-Host "Done. Open the Collection & Deck Manager -> Boosters to use them." -ForegroundColor Green }
    else { Write-Host "Extractor not found, or cards.rcc missing (it ships with the game client)." -ForegroundColor Red }
}

$running = $true
try {
    while ($running) {
        $hostsOk = Test-Hosts
        Write-Host ""
        Write-Host "======================================================" -ForegroundColor DarkCyan
        Write-Host ("   Star Wars Galaxies TCG -- Standalone (offline)  " + $LauncherVersion)  -ForegroundColor White
        Write-Host "======================================================" -ForegroundColor DarkCyan
        Write-Host ("   hosts entries: " + $(if ($hostsOk) { "OK" } else { "MISSING (run 'Add Hosts Entries.cmd')" })) -ForegroundColor $(if ($hostsOk) { 'Green' } else { 'Yellow' })
        Write-Host ("   login server : " + $(if (Test-ServerUp) { "running" } else { "stopped" })) -ForegroundColor DarkGray
        Write-Host ""
        Write-Host "   [1] Play        (auto-login -> offline vs AI)"
        Write-Host "   [2] Collection & Deck Manager"
        Write-Host "   [3] Backup saves      [4] Restore saves"
        Write-Host "   [5] Stop login server [6] Apply edited DB from Downloads"
        Write-Host "   [7] Extract card art  (build booster images from cards.rcc)"
        Write-Host "   [P] 1v1 PvP test      (two client windows -- experimental)"
        Write-Host "   [G] GUI launcher      [Q] Quit (stops the login server)"
        $choice = Read-Host "Choose"
        switch ($choice.Trim().ToUpper()) {
            '1'     { Invoke-Play }
            '2'     { Invoke-Manager }
            '3'     { Invoke-Backup }
            '4'     { Invoke-Restore }
            '5'     { if (Stop-Server) { Write-Host "Login server stopped." -ForegroundColor Green } else { Write-Host "Login server was not running." -ForegroundColor DarkGray } }
            '6'     { Invoke-Apply }
            '7'     { Invoke-Extract }
            'P'     {
                        if (Start-TwoPlayerPvP) {
                            Write-Host "1v1 PvP: server restarted in MP mode + two clients launched (StandAloneUser + Player2)." -ForegroundColor Green
                            Write-Host "  In BOTH windows: Casual lobby -> P1 Create Match -> P2 Join -> both Ready." -ForegroundColor Cyan
                            Write-Host "  Watch _ext\server\server.log. If the 2nd window doesn't open, the client is single-instance (tell me)." -ForegroundColor Cyan
                        } else { Write-Host "PvP launch failed (client missing, or server didn't start)." -ForegroundColor Red }
                    }
            'G'     { Start-Process powershell.exe -ArgumentList @('-NoProfile','-ExecutionPolicy','Bypass','-STA','-File', (Join-Path $PSScriptRoot 'launcher-gui.ps1')) | Out-Null }
            'Q'     { $running = $false }
            default { Write-Host "Unknown choice." -ForegroundColor Yellow }
        }
    }
}
finally {
    Stop-Server | Out-Null
}
