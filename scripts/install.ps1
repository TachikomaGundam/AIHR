# AIHR turnkey installer — Windows PowerShell (pwsh 7+ or Windows PowerShell 5.1).
# Mirrors scripts/install.sh: checksum-verified bundle into D = %LOCALAPPDATA%\aihr,
# then hands over to `hr install-post`.
#
# Ergonomics:
#   powershell -ExecutionPolicy Bypass -File install.ps1 -Version 0.4.0
#   & { param($Version) ... } style also accepts irm|iex with defaults.
#   pwsh -c ".\install.ps1 -Reinstall -Port 5433"
[CmdletBinding()]
param(
    [string]$Version = "",
    [string]$Bundle = "",
    [int]$Port = 0,
    [switch]$Reinstall = $false,
    [switch]$NoRunInstaller = $false,
    [switch]$AllowElevated = $false
)

Set-StrictMode -Version Latest
$ErrorActionPreference = "Stop"

$Repo = if ($env:AIHR_REPO) { $env:AIHR_REPO } else { "TachikomaGundam/AIHR" }
$D = Join-Path $env:LOCALAPPDATA "aihr"

function Write-Say([string]$Message) { Write-Host $Message }
function Write-Err([string]$Message) {
    Write-Host "error: $Message" -ForegroundColor Red
    exit 1
}

$principal = New-Object Security.Principal.WindowsPrincipal(
    [Security.Principal.WindowsIdentity]::GetCurrent())
if ($principal.IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator) -and -not $AllowElevated) {
    Write-Err "user-scoped only: run as the standard user who will use AIHR (no elevation; CI may pass -AllowElevated)"
}

$Arch = if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64") { "aarch64" } else { "x86_64" }
$Work = $null
try {
    if ($Bundle) {
        if (-not (Test-Path -LiteralPath $Bundle -PathType Leaf)) {
            Write-Err "bundle not found: $Bundle"
        }
        $BundlePath = (Resolve-Path -LiteralPath $Bundle).Path
        if (-not $Version) {
            if ((Split-Path -Leaf $BundlePath) -match '^aihr-([^-]*)-') {
                $Version = $Matches[1]
            } else {
                $Version = "unknown"
            }
        }
    } else {
        if (-not $Version) {
            $latest = "https://api.github.com/repos/$Repo/releases/latest"
            try {
                $rel = Invoke-RestMethod -Uri $latest -UseBasicParsing -Headers @{ "User-Agent" = "aihr-install" }
                $Version = $rel.tag_name -replace '^v', ''
            } catch {
                Write-Err "could not resolve the latest release; pass -Version V"
            }
        }
        $Asset = "aihr-$Version-windows-$Arch.zip"
        $Base = "https://github.com/$Repo/releases/download/v$Version/$Asset"
        $Work = Join-Path ([IO.Path]::GetTempPath()) ("aihr-install-" + [guid]::NewGuid().ToString("N"))
        New-Item -ItemType Directory -Path $Work | Out-Null
        $BundlePath = Join-Path $Work $Asset
        Write-Say "downloading $Base"
        try {
            Invoke-WebRequest -Uri $Base -OutFile $BundlePath -UseBasicParsing
            Invoke-WebRequest -Uri "$Base.sha256" -OutFile "$BundlePath.sha256" -UseBasicParsing
        } catch {
            Write-Err "download failed: $Base (exists for windows-$Arch? pass -Version V)"
        }
    }

    $Sidecar = "$BundlePath.sha256"
    if (Test-Path -LiteralPath $Sidecar -PathType Leaf) {
        Write-Say "verifying sha256..."
        $expectedLine = (Get-Content -LiteralPath $Sidecar -TotalCount 5 |
            Select-String -Pattern '\b([0-9a-fA-F]{64})\b' |
            Select-Object -First 1)
        if (-not $expectedLine) { Write-Err "no 64-hex digest found in $Sidecar" }
        $expected = $expectedLine.Matches[0].Groups[1].Value.ToLowerInvariant()
        $actual = (Get-FileHash -LiteralPath $BundlePath -Algorithm SHA256).Hash.ToLowerInvariant()
        if ($expected -ne $actual) {
            Write-Err "checksum mismatch for $(Split-Path -Leaf $BundlePath) - refusing to install"
        }
        Write-Say "checksum ok"
    } elseif ($Bundle) {
        Write-Say "warning: no .sha256 sidecar next to the bundle - checksum NOT verified"
    } else {
        Write-Err "checksum sidecar missing"
    }

    if ((Test-Path -LiteralPath $D) -and -not $Reinstall) {
        Write-Err "$D already exists - pass -Reinstall to refresh app/pg/share (data is never touched)"
    }

    if (-not $Work) {
        $Work = Join-Path ([IO.Path]::GetTempPath()) ("aihr-stage-" + [guid]::NewGuid().ToString("N"))
    }
    $StageRoot = Join-Path $Work "root"
    New-Item -ItemType Directory -Path $StageRoot -Force | Out-Null
    Expand-Archive -LiteralPath $BundlePath -DestinationPath $StageRoot -Force
    if (-not (Test-Path -LiteralPath (Join-Path $StageRoot "app\hr.exe") -PathType Leaf)) {
        Write-Err "corrupt bundle: app\hr.exe missing"
    }
    if (-not (Test-Path -LiteralPath (Join-Path $StageRoot "pg\bin\initdb.exe") -PathType Leaf)) {
        Write-Err "corrupt bundle: pg\bin\initdb.exe missing"
    }

    New-Item -ItemType Directory -Path $D -Force | Out-Null
    foreach ($comp in @("app", "pg", "share")) {
        $target = Join-Path $D $comp
        if (Test-Path -LiteralPath $target) { Remove-Item -LiteralPath $target -Recurse -Force }
        Move-Item -LiteralPath (Join-Path $StageRoot $comp) -Destination $target
    }
    Write-Say "placed bundle at $D (data untouched)"

    $HrExe = Join-Path $D "app\hr.exe"
    if ($NoRunInstaller) {
        Write-Say ""
        Write-Say "files placed; next steps:"
        $portArg = if ($Port) { " -Port $Port" } else { "" }
        Write-Say "  `"$HrExe`" install-post$portArg"
        Write-Say "  `"$HrExe`" db-up$portArg"
    } else {
        $installArgs = @("install-post")
        $dbArgs = @("db-up")
        if ($Port) { $installArgs += @("--port", "$Port"); $dbArgs += @("--port", "$Port") }
        & $HrExe @installArgs
        if ($LASTEXITCODE -ne 0) { Write-Err "hr install-post failed - see output above; D=$D" }
        & $HrExe @dbArgs
        if ($LASTEXITCODE -ne 0) {
            Write-Say "warning: hr db-up failed - start it later with: `"$HrExe`" db-up"
        } else {
            Write-Say "database up"
        }
    }

    Write-Say ""
    Write-Say "AIHR $Version (windows-$Arch) installed at $D"
    Write-Say "  PATH hint:  `$env:Path = `"$D\bin;`$env:Path`"  (or setx PATH permanently)"
    Write-Say "  uninstall:  hr self-uninstall --yes"
} finally {
    if ($Work -and (Test-Path -LiteralPath $Work)) {
        Remove-Item -LiteralPath $Work -Recurse -Force -ErrorAction SilentlyContinue
    }
}
