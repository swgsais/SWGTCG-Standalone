<#
  SWGTCG-Standalone -- all-in-one themed GUI launcher.

  Feature parity with the text menu (launcher.ps1); both dot-source launcher-core.ps1, so the server /
  DB / backup logic is shared. Launch via "SWGTCG Launcher.cmd" (runs this -STA, console hidden).

  Live status: hosts entries, login server, game process -- refreshed on a timer. Buttons: Play,
  Collection & Deck Manager (auto-opens the swgtcg.db folder), Extract card art, Backup / Restore,
  Apply edited DB, Stop server, Add hosts entries (shown only when missing).
#>
$ErrorActionPreference = 'Stop'
. (Join-Path $PSScriptRoot 'launcher-core.ps1')

Add-Type -AssemblyName System.Windows.Forms
Add-Type -AssemblyName System.Drawing
[System.Windows.Forms.Application]::EnableVisualStyles()

# ---- theme (gold-on-black, Star Wars vibe) ----
$script:Bg      = [System.Drawing.Color]::FromArgb(10, 11, 16)
$script:Header  = [System.Drawing.Color]::FromArgb(6, 7, 11)
$script:Panel   = [System.Drawing.Color]::FromArgb(18, 20, 28)
$script:PanelHi = [System.Drawing.Color]::FromArgb(30, 33, 44)
$script:Border  = [System.Drawing.Color]::FromArgb(48, 52, 64)
$script:Gold     = [System.Drawing.Color]::FromArgb(232, 185, 35)
$script:GoldHi   = [System.Drawing.Color]::FromArgb(255, 214, 102)
$script:TextCol  = [System.Drawing.Color]::FromArgb(206, 208, 216)
$script:Muted    = [System.Drawing.Color]::FromArgb(120, 128, 140)
$script:GreenCol = [System.Drawing.Color]::FromArgb(63, 185, 80)
$script:YellowCol= [System.Drawing.Color]::FromArgb(227, 179, 65)
$script:RedCol   = [System.Drawing.Color]::FromArgb(219, 90, 90)

function New-Font { param([single]$Size, [string]$Style = 'Regular')
    return New-Object System.Drawing.Font('Segoe UI', $Size, [System.Drawing.FontStyle]::$Style)
}

# ---- form ----
$form = New-Object System.Windows.Forms.Form
$form.Text = 'Star Wars Galaxies TCG - Standalone'
$form.ClientSize = New-Object System.Drawing.Size(540, 700)
$form.FormBorderStyle = 'FixedDialog'
$form.MaximizeBox = $false
$form.StartPosition = 'CenterScreen'
$form.BackColor = $script:Bg
$form.Font = New-Font 9.5
try { if (Test-Path $Exe) { $form.Icon = [System.Drawing.Icon]::ExtractAssociatedIcon($Exe) } } catch {}

# ---- header ----
$hdr = New-Object System.Windows.Forms.Panel
$hdr.Location = New-Object System.Drawing.Point(0, 0)
$hdr.Size = New-Object System.Drawing.Size(540, 92)
$hdr.BackColor = $script:Header
$form.Controls.Add($hdr)

$title = New-Object System.Windows.Forms.Label
$title.Text = 'STAR WARS GALAXIES'
$title.ForeColor = $script:Gold
$title.Font = New-Object System.Drawing.Font('Segoe UI', 17, [System.Drawing.FontStyle]::Bold)
$title.Location = New-Object System.Drawing.Point(24, 16)
$title.Size = New-Object System.Drawing.Size(492, 32)
$title.TextAlign = 'MiddleCenter'
$hdr.Controls.Add($title)

$subtitle = New-Object System.Windows.Forms.Label
$subtitle.Text = 'TRADING CARD GAME  '+[char]0x00B7+'  STANDALONE  '+[char]0x00B7+'  OFFLINE  '+[char]0x00B7+'  '+$LauncherVersion
$subtitle.ForeColor = $script:Muted
$subtitle.Font = New-Font 8.5
$subtitle.Location = New-Object System.Drawing.Point(24, 52)
$subtitle.Size = New-Object System.Drawing.Size(492, 20)
$subtitle.TextAlign = 'MiddleCenter'
$hdr.Controls.Add($subtitle)

# ---- status panel ----
$statusPanel = New-Object System.Windows.Forms.Panel
$statusPanel.Location = New-Object System.Drawing.Point(24, 108)
$statusPanel.Size = New-Object System.Drawing.Size(492, 96)
$statusPanel.BackColor = $script:Panel
$form.Controls.Add($statusPanel)

function New-StatusRow { param([int]$Y)
    $dot = New-Object System.Windows.Forms.Label
    $dot.Text = [char]0x25CF   # filled circle
    $dot.Font = New-Object System.Drawing.Font('Segoe UI', 12)
    $dot.ForeColor = $script:Muted
    $dot.Location = New-Object System.Drawing.Point(14, $Y)
    $dot.Size = New-Object System.Drawing.Size(20, 22)
    $dot.TextAlign = 'MiddleCenter'
    $statusPanel.Controls.Add($dot)
    $txt = New-Object System.Windows.Forms.Label
    $txt.ForeColor = $script:TextCol
    $txt.Font = New-Font 9.5
    $txt.Location = New-Object System.Drawing.Point(38, $Y)
    $txt.Size = New-Object System.Drawing.Size(440, 22)
    $txt.TextAlign = 'MiddleLeft'
    $statusPanel.Controls.Add($txt)
    return @($dot, $txt)
}
$rowHosts = New-StatusRow 12
$rowSrv   = New-StatusRow 40
$rowGame  = New-StatusRow 68
$script:dotHosts = $rowHosts[0]; $script:txtHosts = $rowHosts[1]
$script:dotSrv   = $rowSrv[0];   $script:txtSrv   = $rowSrv[1]
$script:dotGame  = $rowGame[0];  $script:txtGame  = $rowGame[1]

# ---- button factory ----
function New-Btn { param([string]$Text, [int]$X, [int]$Y, [int]$W, [int]$H, [switch]$Primary)
    $b = New-Object System.Windows.Forms.Button
    $b.Text = $Text
    $b.Location = New-Object System.Drawing.Point($X, $Y)
    $b.Size = New-Object System.Drawing.Size($W, $H)
    $b.FlatStyle = 'Flat'
    $b.BackColor = $script:Panel
    $b.ForeColor = if ($Primary) { $script:GoldHi } else { $script:TextCol }
    $b.Font = if ($Primary) { New-Font 13 'Bold' } else { New-Font 10 }
    $b.TextAlign = 'MiddleLeft'
    $b.Padding = New-Object System.Windows.Forms.Padding(14, 0, 0, 0)
    $b.Cursor = [System.Windows.Forms.Cursors]::Hand
    $rest = if ($Primary) { $script:Gold } else { $script:Border }
    $b.FlatAppearance.BorderColor = $rest
    $b.FlatAppearance.BorderSize = if ($Primary) { 2 } else { 1 }
    $b | Add-Member -NotePropertyName RestBorder -NotePropertyValue $rest -Force
    $b.Add_MouseEnter({ $this.BackColor = $script:PanelHi; $this.FlatAppearance.BorderColor = $script:Gold })
    $b.Add_MouseLeave({ $this.BackColor = $script:Panel;   $this.FlatAppearance.BorderColor = $this.RestBorder })
    $form.Controls.Add($b)
    return $b
}

$btnPlay    = New-Btn ([char]0x25B6 + '   PLAY   (auto-login  '+[char]0x2192+'  offline vs AI)') 24 224 492 54 -Primary
$btnManager = New-Btn ([char]0x270E + '  Collection & Deck Manager') 24 290 241 44
$btnArt     = New-Btn ([char]0x2726 + '  Extract Card Art') 275 290 241 44
$btnBackup  = New-Btn ([char]0x2193 + '  Backup Saves') 24 342 241 44
$btnRestore = New-Btn ([char]0x267B + '  Restore Saves') 275 342 241 44
$btnApply   = New-Btn ([char]0x2714 + '  Apply Edited DB') 24 394 241 44
$btnStop    = New-Btn ([char]0x25A0 + '  Stop Login Server') 275 394 241 44
$btnHosts   = New-Btn ([char]0x26A0 + '  Add Hosts Entries  (one-time setup)') 24 448 492 44
$btnHosts.ForeColor = $script:YellowCol
$btnPvP     = New-Btn ([char]0x2694 + '  1v1 PvP  (two clients -- experimental)') 24 500 492 42
$btnPvP.ForeColor = $script:GoldHi

# ---- footer ----
$footer = New-Object System.Windows.Forms.Label
$footer.Location = New-Object System.Drawing.Point(24, 554)
$footer.Size = New-Object System.Drawing.Size(492, 130)
$footer.ForeColor = $script:Muted
$footer.Font = New-Font 9
$footer.TextAlign = 'TopLeft'
$footer.Text = 'Ready.'
$form.Controls.Add($footer)

function Set-Footer { param([string]$Msg, [System.Drawing.Color]$Color = $script:Muted)
    $footer.ForeColor = $Color
    $footer.Text = $Msg
    $footer.Refresh()   # repaint just the footer (cheap) so the message shows before any blocking action
}

# ---- status refresh ----
# The timer ticks on the UI thread, so the probes must be cheap AND we must only touch controls when a
# value actually CHANGES (every redundant .Text/.ForeColor set forces a repaint). Install is static; hosts
# change rarely (only via the Add-Hosts button or externally) so it's polled every ~8th tick; server/game
# are the fast managed probes from launcher-core.ps1.
$script:stTick    = 0
$script:stInstall = $null
$script:stHosts   = $null
$script:stSrv     = $null
$script:stGame    = $null
function Update-Status {
    $script:stTick++

    if ($null -eq $script:stInstall -or -not $script:stInstall) {
        $inst = [bool](Test-Install)
        if ($inst -ne $script:stInstall) {
            $script:stInstall = $inst
            $btnPlay.Enabled = $inst
            $btnPlay.Text = if ($inst) { [char]0x25B6 + '   PLAY   (auto-login  '+[char]0x2192+'  offline vs AI)' }
                            else { [char]0x26A0 + '   Client not found in TCGStandalone\' }
        }
    }

    if ($null -eq $script:stHosts -or ($script:stTick % 8) -eq 0) {
        $h = [bool](Test-Hosts)
        if ($h -ne $script:stHosts) {
            $script:stHosts = $h
            if ($h) { $script:dotHosts.ForeColor = $script:GreenCol; $script:txtHosts.Text = 'Hosts entries: OK' }
            else    { $script:dotHosts.ForeColor = $script:YellowCol; $script:txtHosts.Text = 'Hosts entries: MISSING  (click "Add Hosts Entries")' }
            $btnHosts.Visible = -not $h
        }
    }

    $srv = [bool](Test-ServerUp)
    if ($srv -ne $script:stSrv) {
        $script:stSrv = $srv
        if ($srv) { $script:dotSrv.ForeColor = $script:GreenCol; $script:txtSrv.Text = 'Login server: RUNNING  (127.0.0.1:16782 / 16783)'; $btnStop.Enabled = $true }
        else      { $script:dotSrv.ForeColor = $script:Muted; $script:txtSrv.Text = 'Login server: stopped'; $btnStop.Enabled = $false }
    }

    $game = [bool](Test-GameRunning)
    if ($game -ne $script:stGame) {
        $script:stGame = $game
        if ($game) { $script:dotGame.ForeColor = $script:GreenCol; $script:txtGame.Text = 'Game: RUNNING' }
        else       { $script:dotGame.ForeColor = $script:Muted; $script:txtGame.Text = 'Game: not running' }
    }
}

# ---- helpers ----
function Show-Info  { param([string]$M, [string]$T = 'SWGTCG') [void][System.Windows.Forms.MessageBox]::Show($form, $M, $T, 'OK', 'Information') }
function Show-Warn  { param([string]$M, [string]$T = 'SWGTCG') [void][System.Windows.Forms.MessageBox]::Show($form, $M, $T, 'OK', 'Warning') }
function Show-Error { param([string]$M, [string]$T = 'SWGTCG') [void][System.Windows.Forms.MessageBox]::Show($form, $M, $T, 'OK', 'Error') }
function Ask-YesNo  { param([string]$M, [string]$T = 'SWGTCG')
    return ([System.Windows.Forms.MessageBox]::Show($form, $M, $T, 'YesNo', 'Question') -eq 'Yes')
}

# ---- button actions ----
$btnPlay.Add_Click({
    try {
        if (-not (Test-Install)) { Show-Error "The retail client isn't in place.`n`nExpected:`n$Exe`n`nSee README -> Getting the client."; return }
        if (-not (Test-Hosts)) {
            if (-not (Ask-YesNo "The hosts entries are MISSING, so the client can't reach the local login server and PLAY will stay greyed out.`n`nLaunch anyway?")) { return }
        }
        Set-Footer 'Starting login server and launching the client (auto-login)...' $script:GoldHi
        if (Start-Game) {
            Set-Footer ("Launched. You land on the Home screen -- move to the LEFT edge for the Navigator:" + [char]0x0A + "  PLAY -> Tutorials / Scenarios / Skirmish -> Begin.") $script:GreenCol
        } else { Show-Error 'Launch failed (install incomplete).' }
    } catch { Show-Error "Launch failed:`n$($_.Exception.Message)" }
    Update-Status
})

$btnManager.Add_Click({
    try {
        if (-not (Test-Path $Manager)) { Show-Error "Collection Manager not found at`n$Manager"; return }
        if (-not (Test-CardArtExtracted) -and (Test-CardsRcc)) {
            if (Ask-YesNo "Extract card images from cards.rcc now? (~140MB, one-time) `n`nThe Boosters tab shows real art once extracted. You can also do this later with 'Extract Card Art'.") {
                Start-Process $PyExe -ArgumentList (Join-Path $Ext 'tools\extract_card_art.py') -WorkingDirectory $Ext | Out-Null
                Set-Footer 'Card-art extraction running in its own window...' $script:GoldHi
            }
        }
        if (Open-Manager) {
            Set-Footer ("Collection & Deck Manager opened; its folder is open with swgtcg.db highlighted." + [char]0x0A + "In it: 'Open Account DB' -> swgtcg.db, edit, 'Save to game'. (Login server stopped for safe editing.)") $script:GreenCol
        }
    } catch { Show-Error "Could not open the manager:`n$($_.Exception.Message)" }
    Update-Status
})

$btnArt.Add_Click({
    try {
        if (-not (Test-CardsRcc)) { Show-Error "cards.rcc not found in TCGStandalone\ (it ships with the game client)."; return }
        if (Test-CardArtExtracted) { if (-not (Ask-YesNo 'Card art already appears extracted. Re-extract anyway?')) { return } }
        Start-Process $PyExe -ArgumentList (Join-Path $Ext 'tools\extract_card_art.py') -WorkingDirectory $Ext | Out-Null
        Set-Footer 'Extracting card art from cards.rcc in its own window (~140MB, one-time)...' $script:GoldHi
    } catch { Show-Error "Extraction failed to start:`n$($_.Exception.Message)" }
})

$btnBackup.Add_Click({
    try {
        $stamp = Get-Date -Format 'yyyyMMdd_HHmmss'
        $zip = Backup-Saves -Stamp $stamp
        if ($zip) { Set-Footer "Backed up account DB + local collections/decks ->`n$zip" $script:GreenCol }
        else { Show-Warn 'Nothing to back up yet.' }
    } catch { Show-Error "Backup failed:`n$($_.Exception.Message)" }
})

$btnRestore.Add_Click({
    try {
        $zips = Get-Backups
        if ($zips.Count -eq 0) { Show-Warn 'No backups found in the Backups folder.'; return }
        $chosen = Show-RestorePicker $zips
        if ($chosen) {
            if (Restore-Backup -ZipPath $chosen.FullName) { Set-Footer "Restored $($chosen.Name)." $script:GreenCol }
            else { Show-Error 'Restore failed.' }
        }
    } catch { Show-Error "Restore failed:`n$($_.Exception.Message)" }
    Update-Status
})

$btnApply.Add_Click({
    try {
        $name = Apply-EditedDb
        if ($name) { Set-Footer "Applied '$name' (newest Downloads copy) -> _ext\server\swgtcg.db. Hit PLAY to use it." $script:GreenCol }
        else { Show-Warn "No edited swgtcg*.db found in your Downloads folder.`nSave it from the manager first." }
    } catch { Show-Error "Apply failed:`n$($_.Exception.Message)" }
    Update-Status
})

$btnStop.Add_Click({
    try { if (Stop-Server) { Set-Footer 'Login server stopped.' $script:TextCol } else { Set-Footer 'Login server was not running.' $script:Muted } }
    catch { Show-Error "Stop failed:`n$($_.Exception.Message)" }
    Update-Status
})

$btnHosts.Add_Click({
    try {
        if (Add-HostsEntries) { $script:stHosts = $null; Set-Footer 'Hosts helper launched (accept the UAC prompt). Status refreshes when done.' $script:GoldHi }
        else { Show-Error 'Hosts helper (_ext\add-hosts.ps1) not found.' }
    } catch { Show-Error "Could not start the hosts helper:`n$($_.Exception.Message)" }
})

$btnPvP.Add_Click({
    try {
        if (-not (Test-Install)) { Show-Error "The retail client isn't in place (TCGStandalone\)."; return }
        Set-Footer 'Restarting the server in MP mode + launching two clients (P1 StandAloneUser, P2 Player2)...' $script:GoldHi
        if (Start-TwoPlayerPvP) {
            Set-Footer ("1v1 PvP launched. In BOTH windows: Casual lobby -> P1 Create Match -> P2 Join -> both Ready." + [char]0x0A + "Watch _ext/server/server.log. If the 2nd window doesn't open, the client is single-instance -- tell me.") $script:GreenCol
        } else { Show-Error 'PvP launch failed (client missing, or the server did not start).' }
    } catch { Show-Error "PvP launch failed:`n$($_.Exception.Message)" }
    Update-Status
})

# ---- restore picker sub-dialog ----
function Show-RestorePicker { param($Zips)
    $dlg = New-Object System.Windows.Forms.Form
    $dlg.Text = 'Restore a backup'
    $dlg.ClientSize = New-Object System.Drawing.Size(420, 300)
    $dlg.FormBorderStyle = 'FixedDialog'; $dlg.MaximizeBox = $false; $dlg.MinimizeBox = $false
    $dlg.StartPosition = 'CenterParent'; $dlg.BackColor = $script:Bg
    $lb = New-Object System.Windows.Forms.ListBox
    $lb.Location = New-Object System.Drawing.Point(16, 16)
    $lb.Size = New-Object System.Drawing.Size(388, 210)
    $lb.BackColor = $script:Panel; $lb.ForeColor = $script:TextCol; $lb.BorderStyle = 'FixedSingle'
    foreach ($z in $Zips) { [void]$lb.Items.Add(('{0}   ({1:yyyy-MM-dd HH:mm})' -f $z.Name, $z.LastWriteTime)) }
    $lb.SelectedIndex = 0
    $dlg.Controls.Add($lb)
    $ok = New-Object System.Windows.Forms.Button
    $ok.Text = 'Restore'; $ok.Location = New-Object System.Drawing.Point(228, 240); $ok.Size = New-Object System.Drawing.Size(84, 32)
    $ok.FlatStyle = 'Flat'; $ok.BackColor = $script:Panel; $ok.ForeColor = $script:GoldHi; $ok.FlatAppearance.BorderColor = $script:Gold
    $ok.DialogResult = 'OK'; $dlg.Controls.Add($ok); $dlg.AcceptButton = $ok
    $cancel = New-Object System.Windows.Forms.Button
    $cancel.Text = 'Cancel'; $cancel.Location = New-Object System.Drawing.Point(320, 240); $cancel.Size = New-Object System.Drawing.Size(84, 32)
    $cancel.FlatStyle = 'Flat'; $cancel.BackColor = $script:Panel; $cancel.ForeColor = $script:TextCol; $cancel.FlatAppearance.BorderColor = $script:Border
    $cancel.DialogResult = 'Cancel'; $dlg.Controls.Add($cancel); $dlg.CancelButton = $cancel
    $warn = New-Object System.Windows.Forms.Label
    $warn.Text = 'Overwrites the current account DB + local collections/decks.'
    $warn.Location = New-Object System.Drawing.Point(16, 246); $warn.Size = New-Object System.Drawing.Size(200, 40)
    $warn.ForeColor = $script:Muted; $warn.Font = New-Font 8
    $dlg.Controls.Add($warn)
    $res = $dlg.ShowDialog($form)
    if ($res -eq 'OK' -and $lb.SelectedIndex -ge 0) { return $Zips[$lb.SelectedIndex] }
    return $null
}

# ---- lifecycle ----
$timer = New-Object System.Windows.Forms.Timer
$timer.Interval = 2000
$timer.Add_Tick({ Update-Status })
$timer.Start()

$form.Add_Shown({ Update-Status })
$form.Add_FormClosing({
    $timer.Stop()
    # Leave the server up if the game is still running (it needs the login connection); otherwise tidy up.
    if (-not (Test-GameRunning)) { Stop-Server | Out-Null }
})

[void]$form.ShowDialog()
