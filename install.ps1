# Pygame Zero Installer: Windows PowerShell bootstrapper.
#
#   irm https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.ps1 | iex
#
# This script only finds a Python 3 to run install.py with. It downloads
# install.py from a fixed release, checks its SHA-256 hash, and runs it.
# Everything else (choosing a compatible Python, the virtual environment,
# Pygame, Pygame Zero, PATH) happens in install.py.
#
# To pass options when using irm, run it as a script block, for example:
#   & ([scriptblock]::Create((irm https://.../install.ps1))) -Uninstall
#
# Works in Windows PowerShell 5.1 and PowerShell 7+. Needs no administrator rights.
param(
    [switch]$Uninstall,
    [switch]$Recreate,
    [switch]$NoModifyPath,
    [switch]$Yes
)

# The values below are written by tools/release.py. Do not edit by hand.
$PgzRepo = "KylerChan/pygamezero-installer"
$PgzVersion = "v1.0.0"
$PgzInstallPySha256 = "c2ac724fa82e1cab3f687f3477947aac18ad8be2dcc15dc1d3ae3c013b186337"

function Test-PythonCandidate([string]$Exe, [string[]]$PreArgs) {
    # Returns the real interpreter path, or $null. The Microsoft Store
    # "python.exe" stub fails this check without opening the Store.
    try {
        $out = & $Exe @PreArgs -c "import sys; print(sys.executable if sys.version_info >= (3, 6) else '')" 2>$null
        if ($LASTEXITCODE -eq 0 -and $out -and (Test-Path -LiteralPath "$out".Trim())) {
            return "$out".Trim()
        }
    } catch { }
    return $null
}

function Find-Python {
    if (Get-Command py -CommandType Application -ErrorAction SilentlyContinue) {
        $found = Test-PythonCandidate "py" @("-3")
        if ($found) { return $found }
    }
    foreach ($name in @("python", "python3")) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $found = Test-PythonCandidate $cmd.Source @()
            if ($found) { return $found }
        }
    }
    $patterns = @()
    if ($env:LOCALAPPDATA) {
        $patterns += Join-Path $env:LOCALAPPDATA "Programs\Python\Python3*\python.exe"
        $patterns += Join-Path $env:LOCALAPPDATA "Python\pythoncore-3.*\python.exe"
    }
    if ($env:ProgramFiles) { $patterns += Join-Path $env:ProgramFiles "Python3*\python.exe" }
    foreach ($pattern in $patterns) {
        foreach ($exe in @(Get-ChildItem -Path $pattern -ErrorAction SilentlyContinue | Sort-Object FullName -Descending)) {
            $found = Test-PythonCandidate $exe.FullName @()
            if ($found) { return $found }
        }
    }
    return $null
}

function Update-SessionPath {
    $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
    $user = [Environment]::GetEnvironmentVariable("Path", "User")
    $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ";"
}

function Install-PythonWithWinget {
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return $false }
    $wingetArgs = @("install", "--id", "Python.Python.3.13", "-e", "--scope", "user",
                    "--source", "winget", "--accept-package-agreements", "--accept-source-agreements")
    # Pygame has no ARM64 Windows packages; the x64 Python runs fine under emulation.
    if ($env:PROCESSOR_ARCHITECTURE -eq "ARM64" -or $env:PROCESSOR_ARCHITEW6432 -eq "ARM64") {
        $wingetArgs += @("--architecture", "x64")
    }
    Write-Host ""
    Write-Host "I can install Python 3.13 from python.org now, for your user only, by running:"
    Write-Host ("    winget " + ($wingetArgs -join " "))
    if (-not $Yes) {
        $answer = Read-Host "Install it now? [y/N]"
        if ($answer -notmatch '^(y|yes)$') { return $false }
    }
    $proc = Start-Process -FilePath "winget" -ArgumentList $wingetArgs -NoNewWindow -Wait -PassThru
    if ($proc.ExitCode -ne 0) { return $false }
    Update-SessionPath
    return $true
}

function Write-NoPythonHelp {
    Write-Host ""
    Write-Host "Python 3 was not found on this computer."
    Write-Host "Pygame Zero needs Python 3.9 to 3.13 (3.13 is recommended)."
    Write-Host ""
    Write-Host "Install it (no administrator rights needed) with:"
    Write-Host "    winget install --id Python.Python.3.13 -e --scope user"
    Write-Host "or download the Windows installer (64-bit) from:"
    Write-Host "    https://www.python.org/downloads/windows/"
    Write-Host ""
    Write-Host "Then open a new PowerShell window and run this installer again."
}

function Invoke-PgzInstaller {
    $py = Find-Python
    if (-not $py) {
        if (Install-PythonWithWinget) { $py = Find-Python }
        if (-not $py) { Write-NoPythonHelp; $script:PgzExitCode = 3; return }
    }

    $tmpDir = $null
    try {
        # Running from a downloaded copy? Use the install.py next to it, so
        # people can inspect both files before running them.
        $localInstaller = $null
        if ($PSScriptRoot) { $localInstaller = Join-Path $PSScriptRoot "install.py" }

        if ($localInstaller -and (Test-Path -LiteralPath $localInstaller)) {
            $installer = $localInstaller
            Write-Host "Using local installer: $installer"
        } else {
            $base = $env:PGZ_INSTALLER_BASE_URL
            if (-not $base) { $base = "https://raw.githubusercontent.com/$PgzRepo/$PgzVersion" }
            if (-not $base.StartsWith("https://")) {
                Write-Host "Refusing to download over an insecure connection: $base"
                $script:PgzExitCode = 1; return
            }
            # Windows PowerShell 5.1 may default to old TLS versions.
            [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12

            $tmpDir = Join-Path ([IO.Path]::GetTempPath()) ("pgzero-" + [Guid]::NewGuid().ToString("N"))
            New-Item -ItemType Directory -Path $tmpDir | Out-Null
            $installer = Join-Path $tmpDir "install.py"
            $url = "$base/install.py"
            try {
                $ProgressPreference = "SilentlyContinue"
                Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
            } catch {
                Write-Host ""
                Write-Host "Could not download $url"
                Write-Host "Check your internet connection and try again. ($($_.Exception.Message))"
                $script:PgzExitCode = 6; return
            }

            # Integrity check: the hash of install.py is pinned in this script.
            # PGZ_EXPECTED_SHA256 can override it when testing your own build.
            $expected = $env:PGZ_EXPECTED_SHA256
            if (-not $expected) { $expected = $PgzInstallPySha256 }
            $actual = (Get-FileHash -LiteralPath $installer -Algorithm SHA256).Hash.ToLowerInvariant()
            if ($actual -ne $expected.ToLowerInvariant()) {
                Write-Host ""
                Write-Host "The downloaded installer did not match its expected fingerprint (SHA-256)."
                Write-Host "It may have been changed or corrupted, so it was NOT run."
                Write-Host "    expected: $expected"
                Write-Host "    got:      $actual"
                $script:PgzExitCode = 1; return
            }
        }

        $pyArgs = @("-I", $installer)
        if ($Uninstall) { $pyArgs += "--uninstall" }
        if ($Recreate) { $pyArgs += "--recreate" }
        if ($NoModifyPath) { $pyArgs += "--no-modify-path" }
        if ($Yes) { $pyArgs += "--yes" }
        # Not captured, so Python talks to the console directly.
        & $py @pyArgs
        $script:PgzExitCode = $LASTEXITCODE
    } finally {
        if ($tmpDir -and (Test-Path -LiteralPath $tmpDir)) {
            Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

$script:PgzExitCode = 1
Invoke-PgzInstaller
# `exit` would close the window when this runs through `irm | iex`, so only
# use it when running as a script file.
if ($PSCommandPath) { exit $script:PgzExitCode }
$global:LASTEXITCODE = $script:PgzExitCode
