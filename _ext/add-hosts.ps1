# Adds the two host entries the SWGTCG client needs to reach the bundled local
# login server. Self-elevates (UAC) because editing the hosts file needs Administrator.
# Idempotent: it never adds a line that is already present.
$ErrorActionPreference = 'Stop'

$Entries = @(
    '127.0.0.1   sdkccg-02-04.station.sony.com',
    '127.0.0.1   sdkccg-02-11.station.sony.com'
)
$HostsFile = Join-Path $env:WINDIR 'System32\drivers\etc\hosts'

$isAdmin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()
           ).IsInRole([Security.Principal.WindowsBuiltinRole]::Administrator)
if (-not $isAdmin) {
    Write-Host "Requesting Administrator rights (needed to edit the hosts file)..."
    try {
        Start-Process powershell -Verb RunAs -ArgumentList @(
            '-NoProfile','-ExecutionPolicy','Bypass','-File', "`"$PSCommandPath`"")
    } catch {
        Write-Host "Elevation was cancelled. The hosts file was NOT changed." -ForegroundColor Yellow
        Write-Host "You can add these two lines by hand (Notepad as Administrator):" -ForegroundColor Yellow
        $Entries | ForEach-Object { Write-Host "   $_" }
        Read-Host "Press Enter to close"
    }
    return
}

$cur = Get-Content $HostsFile -ErrorAction SilentlyContinue
$added = @()
foreach ($e in $Entries) {
    $name = ($e -split '\s+')[1]
    $present = $cur | Where-Object { $_ -notmatch '^\s*#' -and $_ -match [regex]::Escape($name) }
    if (-not $present) {
        Add-Content -Path $HostsFile -Value $e
        $added += $e
    }
}

Write-Host ""
if ($added.Count) {
    Write-Host "Added to the hosts file:" -ForegroundColor Green
    $added | ForEach-Object { Write-Host "   $_" }
} else {
    Write-Host "Both host entries were already present -- nothing to do." -ForegroundColor Green
}
Write-Host ""
Write-Host "You can now run 'Play SWGTCG.cmd'." -ForegroundColor Cyan
Read-Host "Done. Press Enter to close"
