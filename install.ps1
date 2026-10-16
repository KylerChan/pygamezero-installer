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
#
# Notes for editors:
# - Everything down to the end of the "preflight" section must run on
#   PowerShell 2.0 and in Constrained Language Mode: no .NET method calls and
#   no syntax newer than PowerShell 2.0 (the whole file is parsed before any of
#   it runs, so avoid -in/-notin, classes, ?? and ternaries everywhere).
# - Never call `exit` except at the very end: under `irm | iex` it would close
#   the student's PowerShell window.
# - Native command output inside functions becomes the function's return
#   value, so functions that run Python or winget report through $script:
#   variables or Write-Host instead of returning that output.
param(
    [switch]$Uninstall,
    [switch]$Recreate,
    [switch]$NoModifyPath,
    [switch]$Yes
)

# The values below are written by tools/release.py. Do not edit by hand.
$PgzRepo = "KylerChan/pygamezero-installer"
$PgzVersion = "v1.0.0"
$PgzInstallPySha256 = "f33d1bf6ec79d2587068f6b07477fca12ea9f00b4ea397ab867494434cb67d19"

# ---------------------------------------------------------------------------
# Preflight (PowerShell 2.0 and Constrained Language Mode safe)
# ---------------------------------------------------------------------------

function Write-PgzManualInstall {
    Write-Host ""
    Write-Host "You can also install without this script:"
    Write-Host "  1. Install Python 3.13 (64-bit) from https://www.python.org/downloads/windows/"
    Write-Host "  2. Download the installer file (right-click > Save link as...):"
    Write-Host "         https://raw.githubusercontent.com/$PgzRepo/$PgzVersion/install.py"
    Write-Host "  3. Open Command Prompt in the folder where you saved it and run:"
    Write-Host "         py install.py"
    Write-Host ""
    Write-Host "More help: https://github.com/$PgzRepo"
}

$script:PgzExitCode = 1
$PgzPreflightOk = $true
$PgzPsMajor = 0
$PgzPsMinor = 0
if ($PSVersionTable -and $PSVersionTable.PSVersion) {
    $PgzPsMajor = $PSVersionTable.PSVersion.Major
    $PgzPsMinor = $PSVersionTable.PSVersion.Minor
}

if ($PgzPsMajor -lt 5 -or ($PgzPsMajor -eq 5 -and $PgzPsMinor -lt 1)) {
    $PgzPreflightOk = $false
    $script:PgzExitCode = 2
    Write-Host ""
    Write-Host "This installer needs Windows PowerShell 5.1 or newer, but this is PowerShell $PgzPsMajor.$PgzPsMinor."
    Write-Host "Nothing was changed on your computer."
    Write-Host ""
    Write-Host "Windows 10 and 11 already include PowerShell 5.1: open the Start menu and"
    Write-Host "search for 'Windows PowerShell'. On older Windows (7 or 8.1), install"
    Write-Host "'Windows Management Framework 5.1' from Microsoft first:"
    Write-Host "    https://www.microsoft.com/download/details.aspx?id=54616"
    Write-PgzManualInstall
} elseif ($ExecutionContext.SessionState.LanguageMode -ne "FullLanguage") {
    $PgzPreflightOk = $false
    $script:PgzExitCode = 7
    Write-Host ""
    Write-Host "PowerShell on this computer is running in '$($ExecutionContext.SessionState.LanguageMode)' mode."
    Write-Host ""
    Write-Host "Schools and workplaces often switch this on (using AppLocker or Windows"
    Write-Host "Defender Application Control) to limit what scripts may do. In this mode the"
    Write-Host "installer cannot safely download and check its files, so it stopped."
    Write-Host "Nothing was changed on your computer."
    Write-Host ""
    Write-Host "What you can do:"
    Write-Host "  - Ask your IT support or teacher to install Python and Pygame Zero for you,"
    Write-Host "    or to allow this installer to run."
    Write-Host "  - Or try the manual steps below (they may also be blocked on managed computers)."
    Write-PgzManualInstall
}

# ---------------------------------------------------------------------------
# Finding Python
# ---------------------------------------------------------------------------

# Prints "major.minor|path"; works in Python 2 too, so we can say why it was skipped.
$PgzPythonProbe = "import sys; print('%d.%d|%s' % (sys.version_info[0], sys.version_info[1], sys.executable))"

function Add-PgzSkipped([string]$Text) {
    $script:PgzSkipped = @($script:PgzSkipped) + $Text
}

function Test-PythonCandidate([string]$Label, [string]$Exe, [string[]]$PreArgs) {
    # Returns the real interpreter path, or $null (and records why it was
    # skipped). The Microsoft Store "python.exe" stub fails this check
    # without opening the Store.
    $out = $null
    $code = $null
    try {
        $out = & $Exe @PreArgs -c $PgzPythonProbe 2>$null
        $code = $LASTEXITCODE
    } catch {
        $reason = "$($_.Exception.Message)".Trim()
        Add-PgzSkipped "$Label - could not be started: $reason"
        return $null
    }

    $major = -1
    $minor = -1
    $exePath = $null
    foreach ($line in @($out)) {
        if ("$line".Trim() -match '^(\d+)\.(\d+)\|(.+)$') {
            $major = [int]$matches[1]
            $minor = [int]$matches[2]
            $exePath = $matches[3].Trim()
        }
    }

    if ($code -ne 0 -or -not $exePath) {
        if ($Exe -match '[\\/]WindowsApps[\\/]') {
            Add-PgzSkipped "$Label - the Microsoft Store shortcut (Python is not really installed)"
        } elseif ($Label -match '^py launcher') {
            Add-PgzSkipped "$Label - found, but it has no working Python 3 behind it (exit code $code)"
        } else {
            Add-PgzSkipped "$Label - did not work (exit code $code)"
        }
        return $null
    }
    if ($major -lt 3 -or ($major -eq 3 -and $minor -lt 6)) {
        Add-PgzSkipped "$Label - Python $major.$minor is too old (Python 3 is needed)"
        return $null
    }
    if (-not (Test-Path -LiteralPath $exePath -ErrorAction SilentlyContinue)) {
        Add-PgzSkipped "$Label - reported a Python at $exePath, which does not exist"
        return $null
    }
    return $exePath
}

function Find-Python {
    $py = Get-Command py -CommandType Application -ErrorAction SilentlyContinue | Select-Object -First 1
    if ($py) {
        $found = Test-PythonCandidate "py launcher ($($py.Source))" "py" @("-3")
        if ($found) { return $found }
    }
    foreach ($name in @("python", "python3")) {
        foreach ($cmd in @(Get-Command $name -CommandType Application -All -ErrorAction SilentlyContinue)) {
            $found = Test-PythonCandidate $cmd.Source $cmd.Source @()
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
            $found = Test-PythonCandidate $exe.FullName $exe.FullName @()
            if ($found) { return $found }
        }
    }
    return $null
}

function Update-SessionPath {
    # Pick up PATH changes made by an installer that ran in this session.
    if ($env:OS -ne "Windows_NT") { return }
    try {
        $machine = [Environment]::GetEnvironmentVariable("Path", "Machine")
        $user = [Environment]::GetEnvironmentVariable("Path", "User")
        $env:Path = (@($machine, $user, $env:Path) | Where-Object { $_ }) -join ";"
    } catch { }
}

function Read-PgzYesNo([string]$Prompt) {
    # $true / $false, or $null when this window cannot ask questions
    # (for example a non-interactive or automated PowerShell).
    try {
        $answer = Read-Host $Prompt
    } catch {
        return $null
    }
    if ("$answer" -match '^\s*(y|yes)\s*$') { return $true }
    return $false
}

function Get-PgzWingetHint([int]$Code) {
    $hex = "0x{0:X8}" -f $Code
    switch ($hex) {
        "0x8A150014" { return "winget could not find the Python package ('No packages found'). Its package list may be out of date or blocked by the network." }
        "0x8A150008" { return "downloading Python failed. Check your internet connection." }
        "0x8A150107" { return "there is no internet connection." }
        "0x8A150086" { return "the download came back empty. Check your internet connection." }
        "0x8A150045" { return "winget could not open its package list. Check your internet connection, or the network may block it." }
        "0x8A15004B" { return "winget could not open its package list. Check your internet connection, or the network may block it." }
        "0x8A15003A" { return "winget is turned off by your school's or organisation's settings (Group Policy)." }
        "0x8A15010F" { return "your school's or organisation's settings do not allow installing apps." }
        "0x8A150105" { return "the disk is full. Free up some space and try again." }
        "0x8A15010C" { return "the installation was cancelled." }
        "0x8A150005" { return "the installation was cancelled." }
        "0x8A150102" { return "another installation is already running. Wait for it to finish, then try again." }
        "0x8A150109" { return "Windows needs a restart to finish installing. Restart, then run this installer again." }
        "0x8A150011" { return "the downloaded Python installer did not match its expected fingerprint, so winget refused it." }
        "0x8A150010" { return "winget has no Python installer that fits this computer." }
    }
    return $null
}

function Install-PythonWithWinget {
    # Returns "installed", "declined", "noninteractive", "missing" or "failed".
    if (-not (Get-Command winget -ErrorAction SilentlyContinue)) { return "missing" }
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
        $answer = Read-PgzYesNo "Install it now? [y/N]"
        if ($null -eq $answer) { return "noninteractive" }
        if (-not $answer) { return "declined" }
    }
    try {
        $proc = Start-Process -FilePath "winget" -ArgumentList $wingetArgs -NoNewWindow -Wait -PassThru -ErrorAction Stop
    } catch {
        Write-Host ""
        Write-Host "winget could not be started: $($_.Exception.Message)"
        return "failed"
    }
    $code = $proc.ExitCode
    if ($code -eq 0) { return "installed" }
    $hex = "0x{0:X8}" -f $code
    if ($hex -eq "0x8A15010D" -or $hex -eq "0x8A150061") {
        Write-Host "winget says Python is already installed. Looking for it again..."
        return "installed"
    }
    Write-Host ""
    Write-Host "winget could not install Python (exit code $code, $hex)."
    $hint = Get-PgzWingetHint $code
    if ($hint) { Write-Host "This usually means $hint" }
    return "failed"
}

function Write-PgzNoPythonFound {
    Write-Host ""
    Write-Host "Python 3 was not found on this computer."
    Write-Host "Pygame Zero needs Python 3.9 to 3.13 (3.13 is recommended)."
    $skipped = @($script:PgzSkipped | Select-Object -Unique)
    if ($skipped.Count -gt 0) {
        Write-Host ""
        Write-Host "These were checked but could not be used:"
        foreach ($item in $skipped) { Write-Host "  - $item" }
        if (($skipped -join "`n") -match '(?i)group policy|blocked|applocker|administrator has') {
            Write-Host ""
            Write-Host "Windows blocked a Python program from starting. On school computers this"
            Write-Host "usually means security settings only allow approved programs. Ask your IT"
            Write-Host "support or teacher to install or allow Python for you."
        }
    }
}

function Write-PgzPythonInstallHelp([string]$WingetStatus) {
    Write-Host ""
    if ($WingetStatus -eq "missing") {
        Write-Host "winget (the Windows package manager) is not available on this computer, so I"
        Write-Host "cannot install Python for you. winget comes with 'App Installer' from the"
        Write-Host "Microsoft Store: https://apps.microsoft.com/detail/9NBLGGH4NNS1"
        Write-Host "(On school computers the Store is often turned off; python.org works too.)"
        Write-Host ""
    } elseif ($WingetStatus -eq "noninteractive") {
        Write-Host "This PowerShell window cannot ask questions, so Python was not installed."
        Write-Host ""
    }
    Write-Host "Install Python (no administrator rights needed) with:"
    Write-Host "    winget install --id Python.Python.3.13 -e --scope user"
    Write-Host "or download the Windows installer (64-bit) from:"
    Write-Host "    https://www.python.org/downloads/windows/"
    Write-Host "(In the python.org installer, tick 'Add python.exe to PATH'.)"
    Write-Host ""
    Write-Host "Then open a new PowerShell window and run this installer again."
}

function Write-PgzPythonNotFoundYet {
    Write-Host ""
    Write-Host "Python was installed, but this PowerShell window cannot see it yet."
    Write-Host "Close this window, open a new PowerShell window, and run the installer again."
}

# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------

function Initialize-PgzNetwork {
    # Windows PowerShell 5.1 may default to old TLS versions.
    try {
        [Net.ServicePointManager]::SecurityProtocol = [Net.ServicePointManager]::SecurityProtocol -bor [Net.SecurityProtocolType]::Tls12
    } catch { }
    # Use the system proxy with the student's Windows sign-in, as many school
    # proxies require (otherwise they answer "407 Proxy Authentication Required").
    try {
        $proxy = [System.Net.WebRequest]::DefaultWebProxy
        if ($proxy) { $proxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials }
    } catch { }
    if ($PSVersionTable.PSVersion.Major -ge 6) {
        # PowerShell 7 downloads through HttpClient, which has its own default proxy.
        try {
            $proxy = [System.Net.Http.HttpClient]::DefaultProxy
            if ($proxy) { $proxy.Credentials = [System.Net.CredentialCache]::DefaultNetworkCredentials }
        } catch { }
    }
}

function Get-PgzHttpStatus($Exception) {
    # The HTTP status code of a failed download, or $null. Handles
    # Windows PowerShell 5.1 (WebException.Response.StatusCode) and
    # PowerShell 7 (HttpResponseException.Response.StatusCode,
    # HttpRequestException.StatusCode).
    $e = $Exception
    $depth = 0
    while ($e -and $depth -lt 10) {
        $depth++
        try {
            if ($e.PSObject.Properties["Response"]) {
                $response = $e.Response
                if ($response -and $response.PSObject.Properties["StatusCode"]) {
                    $code = [int]$response.StatusCode
                    if ($code -gt 0) { return $code }
                }
            }
        } catch { }
        try {
            if ($e.PSObject.Properties["StatusCode"] -and $null -ne $e.StatusCode) {
                $code = [int]$e.StatusCode
                if ($code -gt 0) { return $code }
            }
        } catch { }
        $e = $e.InnerException
    }
    if ("$($Exception.Message)" -match '(?:error: \((\d{3})\)|success: (\d{3}))') {
        if ($matches[1]) { return [int]$matches[1] }
        return [int]$matches[2]
    }
    return $null
}

function Get-PgzNetworkErrorKind($Exception) {
    # Classifies a download failure that has no HTTP status code.
    $e = $Exception
    $depth = 0
    $fallback = "other"
    $messages = @()
    while ($e -and $depth -lt 10) {
        $depth++
        $messages += "$($e.Message)"
        $typeName = $e.GetType().FullName

        $status = ""
        try { if ($e.PSObject.Properties["Status"]) { $status = "$($e.Status)" } } catch { }
        switch ($status) {
            "NameResolutionFailure" { return "dns" }
            "ProxyNameResolutionFailure" { return "proxy" }
            "Timeout" { return "timeout" }
            "TrustFailure" { return "tls" }
            "SecureChannelFailure" { return "tls" }
            "ConnectFailure" { $fallback = "connect" }
        }

        $requestError = ""
        try { if ($e.PSObject.Properties["HttpRequestError"]) { $requestError = "$($e.HttpRequestError)" } } catch { }
        switch ($requestError) {
            "NameResolutionError" { return "dns" }
            "SecureConnectionError" { return "tls" }
            "ProxyTunnelError" { return "proxy" }
            "ConnectionError" { $fallback = "connect" }
        }

        $socketError = ""
        try { if ($e.PSObject.Properties["SocketErrorCode"]) { $socketError = "$($e.SocketErrorCode)" } } catch { }
        switch ($socketError) {
            "HostNotFound" { return "dns" }
            "TryAgain" { return "dns" }
            "NoData" { return "dns" }
            "TimedOut" { return "timeout" }
            "ConnectionRefused" { return "connect" }
            "NetworkUnreachable" { return "offline" }
            "NetworkDown" { return "offline" }
            "HostUnreachable" { return "offline" }
        }

        if ($typeName -match 'AuthenticationException$') { return "tls" }
        if ($typeName -match '(TaskCanceledException|TimeoutException)$') { return "timeout" }
        if ($typeName -match 'UnauthorizedAccessException$') { return "save" }
        try {
            # ERROR_DISK_FULL / ERROR_HANDLE_DISK_FULL
            if ($e.HResult -eq -2147024784 -or $e.HResult -eq -2147024857) { return "disk" }
        } catch { }
        $e = $e.InnerException
    }

    $text = $messages -join " "
    if ($text -match '(?i)not enough space|disk is full|no space left') { return "disk" }
    if ($text -match '(?i)no such host|could not be resolved|name or service not known|nodename nor servname|name resolution') { return "dns" }
    if ($text -match '(?i)\b407\b|proxy authentication') { return "proxy" }
    if ($text -match '(?i)timed? ?out|timeout') { return "timeout" }
    if ($text -match '(?i)ssl|tls|certificate|trust relationship|secure channel') { return "tls" }
    return $fallback
}

function Get-PgzDownloadProblem($ErrorRecord) {
    $ex = $ErrorRecord.Exception
    $status = Get-PgzHttpStatus $ex
    $kind = $null
    if ($status) {
        if ($status -eq 407) { $kind = "proxy" }
        elseif ($status -eq 404 -or $status -eq 410) { $kind = "notfound" }
        elseif ($status -eq 403 -or $status -eq 451) { $kind = "blocked" }
        elseif ($status -eq 408 -or $status -eq 429 -or $status -ge 500) { $kind = "server" }
        else { $kind = "http" }
    } else {
        $kind = Get-PgzNetworkErrorKind $ex
    }
    $retry = @("dns", "offline", "connect", "timeout", "server") -contains $kind
    return @{ Kind = $kind; Status = $status; Message = "$($ex.Message)".Trim(); Retry = $retry }
}

function Write-PgzDownloadHelp($Problem, [string]$Url) {
    $hostName = $Url
    try { $hostName = ([Uri]$Url).Host } catch { }
    Write-Host ""
    Write-Host "Could not download the installer from:"
    Write-Host "    $Url"
    Write-Host ""
    switch ($Problem.Kind) {
        "dns" {
            Write-Host "Your computer could not find $hostName. This usually means there is no"
            Write-Host "internet connection right now, or the network blocks this website."
            Write-Host "Check that you are connected (try opening https://github.com in a browser),"
            Write-Host "then run the installer again."
        }
        "offline" {
            Write-Host "The network could not be reached. Check that Wi-Fi or the network cable is"
            Write-Host "connected, then run the installer again."
        }
        "connect" {
            Write-Host "Could not connect to $hostName. A firewall or the school network may be"
            Write-Host "blocking it. Try again in a minute, or try a different network."
        }
        "timeout" {
            Write-Host "The download took too long and was stopped. The connection may be slow or"
            Write-Host "the site may be blocked. Try again, or try a different network."
        }
        "tls" {
            Write-Host "A secure (HTTPS) connection to $hostName could not be set up."
            Write-Host "On school networks this often happens when the network inspects secure"
            Write-Host "traffic and replaces the website's certificate. You can:"
            Write-Host "  - ask your IT support to allow $hostName,"
            Write-Host "  - try a different network (for example home Wi-Fi or a phone hotspot),"
            Write-Host "  - check that the computer's date and time are correct."
        }
        "proxy" {
            Write-Host "The network's proxy server wants you to sign in (HTTP 407), and signing in"
            Write-Host "with your Windows account did not work. Open a web browser and sign in to"
            Write-Host "the network if it asks, or ask your IT support to allow $hostName."
        }
        "notfound" {
            Write-Host "The installer file was not found there (HTTP $($Problem.Status)). The link may be"
            Write-Host "mistyped, or this version may have been removed. Copy the install command"
            Write-Host "again from https://github.com/$PgzRepo"
        }
        "blocked" {
            Write-Host "The server refused the download (HTTP $($Problem.Status)). A school web filter may"
            Write-Host "be blocking $hostName. Ask your IT support, or try a different network."
        }
        "server" {
            Write-Host "The server had a temporary problem (HTTP $($Problem.Status)). Wait a few minutes"
            Write-Host "and run the installer again."
        }
        "http" {
            Write-Host "The server answered with HTTP status $($Problem.Status) instead of the file."
            Write-Host "Try again later, or try a different network."
        }
        "disk" {
            Write-Host "The file could not be saved because the disk is full. Free up some space"
            Write-Host "and run the installer again."
        }
        "save" {
            Write-Host "The file could not be saved to the temporary folder (access denied)."
        }
        default {
            Write-Host "Check your internet connection and try again."
        }
    }
    if ($Problem.Message) {
        Write-Host ""
        Write-Host "(Details: $($Problem.Message))"
    }
}

function Save-PgzFile([string]$Url, [string]$OutFile) {
    # Returns $null on success, or a problem description (see Get-PgzDownloadProblem).
    $ProgressPreference = "SilentlyContinue"
    $maxAttempts = 3
    for ($attempt = 1; $attempt -le $maxAttempts; $attempt++) {
        try {
            Invoke-WebRequest -Uri $Url -OutFile $OutFile -UseBasicParsing -TimeoutSec 30 -ErrorAction Stop
            return $null
        } catch {
            $problem = Get-PgzDownloadProblem $_
            if (-not $problem.Retry -or $attempt -ge $maxAttempts) { return $problem }
            $why = @{ dns = "website not found"; offline = "no network"; connect = "could not connect";
                      timeout = "too slow"; server = "server problem" }[$problem.Kind]
            Write-Host "Download failed ($why); trying again in 2 seconds (attempt $($attempt + 1) of $maxAttempts)..."
            Start-Sleep -Seconds 2
        }
    }
}

function Get-PgzDownloadContentProblem([string]$Path) {
    # "empty", "html" or "" (looks fine). A Wi-Fi sign-in page ("captive
    # portal") or web filter often answers with HTML instead of the file.
    $item = Get-Item -LiteralPath $Path -ErrorAction SilentlyContinue
    if (-not $item -or $item.Length -eq 0) { return "empty" }
    $head = (@(Get-Content -LiteralPath $Path -TotalCount 20 -ErrorAction SilentlyContinue) -join "`n")
    if ($head.Trim().Length -eq 0) { return "empty" }
    if ($head -match '(?i)<\s*(!doctype\s+html|html)[\s>]') { return "html" }
    if ($head -match '^\s*<' -and $head -match '(?i)<\s*(head|body|title|meta)[\s>]') { return "html" }
    return ""
}

function Get-PgzSha256([string]$Path) {
    # Lower-case SHA-256 hex digest, or $null if it cannot be computed.
    if (Get-Command Get-FileHash -ErrorAction SilentlyContinue) {
        try {
            return (Get-FileHash -LiteralPath $Path -Algorithm SHA256 -ErrorAction Stop).Hash.ToLowerInvariant()
        } catch { }
    }
    $sha = $null
    $stream = $null
    try {
        $sha = [System.Security.Cryptography.SHA256]::Create()
        $stream = [System.IO.File]::OpenRead($Path)
        $bytes = $sha.ComputeHash($stream)
        return ([BitConverter]::ToString($bytes) -replace "-", "").ToLowerInvariant()
    } catch {
        return $null
    } finally {
        if ($stream) { $stream.Dispose() }
        if ($sha) { $sha.Dispose() }
    }
}

# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

function Invoke-PgzInstaller {
    # Local to this function: do not let a caller's "Stop" preference turn
    # Python's stderr output into errors.
    $ErrorActionPreference = "Continue"
    $script:PgzSkipped = @()

    $py = Find-Python
    if (-not $py) {
        Write-PgzNoPythonFound
        $wingetStatus = Install-PythonWithWinget
        if ($wingetStatus -ne "installed") {
            Write-PgzPythonInstallHelp $wingetStatus
            $script:PgzExitCode = 3; return
        }
        Update-SessionPath
        $script:PgzSkipped = @()
        $py = Find-Python
        if (-not $py -and $env:LOCALAPPDATA) {
            $expected = Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"
            if (Test-Path -LiteralPath $expected) {
                $py = Test-PythonCandidate $expected $expected @()
            }
        }
        if (-not $py) { Write-PgzPythonNotFoundYet; $script:PgzExitCode = 3; return }
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
            $base = $base.TrimEnd("/")
            if (-not $base.StartsWith("https://")) {
                Write-Host "Refusing to download over an insecure connection: $base"
                $script:PgzExitCode = 1; return
            }
            Initialize-PgzNetwork

            try {
                $tempRoot = [IO.Path]::GetTempPath()
                $tmpDir = Join-Path $tempRoot ("pgzero-" + [Guid]::NewGuid().ToString("N"))
                New-Item -ItemType Directory -Path $tmpDir -ErrorAction Stop | Out-Null
            } catch {
                Write-Host ""
                Write-Host "Could not create a temporary folder for the download."
                Write-Host "The disk may be full, or the TEMP folder may not be writable."
                Write-Host "Free up some disk space and try again."
                Write-Host "(Details: $($_.Exception.Message))"
                $script:PgzExitCode = 4; return
            }
            $installer = Join-Path $tmpDir "install.py"
            $url = "$base/install.py"

            Write-Host "Downloading the installer from $url"
            $problem = Save-PgzFile $url $installer
            if ($problem) {
                Write-PgzDownloadHelp $problem $url
                if ($problem.Kind -eq "disk" -or $problem.Kind -eq "save") { $script:PgzExitCode = 4 } else { $script:PgzExitCode = 6 }
                return
            }

            $contentProblem = Get-PgzDownloadContentProblem $installer
            if ($contentProblem) {
                Write-Host ""
                if ($contentProblem -eq "empty") {
                    Write-Host "The download finished, but the file was empty."
                } else {
                    Write-Host "The download returned a web page instead of the installer."
                    Write-Host "This usually means the Wi-Fi wants you to sign in first (open any website"
                    Write-Host "in a browser to see the sign-in page), or a web filter blocked the download."
                }
                Write-Host "The file was NOT run. Check your connection and run the installer again."
                Write-Host "    $url"
                $script:PgzExitCode = 6; return
            }

            # Integrity check: the hash of install.py is pinned in this script.
            # PGZ_EXPECTED_SHA256 can override it when testing your own build.
            $expected = $env:PGZ_EXPECTED_SHA256
            if (-not $expected) { $expected = $PgzInstallPySha256 }
            $actual = Get-PgzSha256 $installer
            if (-not $actual) {
                Write-Host ""
                Write-Host "Could not compute the downloaded installer's fingerprint (SHA-256) on this"
                Write-Host "computer, so it was NOT run."
                Write-PgzManualInstall
                $script:PgzExitCode = 1; return
            }
            if ($actual -ne $expected.Trim().ToLowerInvariant()) {
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
        $code = $null
        try {
            # Not captured, so Python talks to the console directly.
            & $py @pyArgs
            $code = $LASTEXITCODE
        } catch {
            $reason = "$($_.Exception.Message)".Trim()
            Write-Host ""
            Write-Host "Python ($py) could not be started."
            if ($reason -match '(?i)group policy|blocked|applocker|administrator has') {
                Write-Host "Windows blocked it. On school computers this usually means security settings"
                Write-Host "only allow approved programs. Ask your IT support or teacher to allow Python."
                $script:PgzExitCode = 7
            } else {
                $script:PgzExitCode = 1
            }
            Write-Host "(Details: $reason)"
            return
        }
        if ($null -eq $code) { $code = 1 }
        $hex = "0x{0:X8}" -f $code
        if ($hex -eq "0xC000013A") {
            # Windows "STATUS_CONTROL_C_EXIT": stopped with Ctrl+C before Python could handle it.
            Write-Host ""
            Write-Host "Installation cancelled."
            $code = 130
        } elseif ($code -lt 0 -or ($code -gt 8 -and $code -ne 130)) {
            Write-Host ""
            Write-Host "Python stopped unexpectedly (exit code $code, $hex)."
            if ($hex -eq "0xC0000135") {
                Write-Host "A file Python needs is missing. Try reinstalling Python from https://www.python.org/downloads/windows/"
            }
        }
        $script:PgzExitCode = $code
    } finally {
        if ($tmpDir -and (Test-Path -LiteralPath $tmpDir)) {
            Remove-Item -LiteralPath $tmpDir -Recurse -Force -ErrorAction SilentlyContinue
        }
    }
}

function Write-PgzUnexpectedError($ErrorRecord) {
    Write-Host ""
    Write-Host "Sorry, something unexpected went wrong, so the installer stopped:"
    Write-Host "    $("$($ErrorRecord.Exception.Message)".Trim())"
    $line = $null
    try { $line = $ErrorRecord.InvocationInfo.ScriptLineNumber } catch { }
    if ($line) { Write-Host "    (install.ps1 line $line)" }
    Write-Host ""
    Write-Host "Please run the installer again. If it keeps happening, show this message to your"
    Write-Host "teacher or report it at https://github.com/$PgzRepo/issues"
}

if ($PgzPreflightOk) {
    $script:PgzFinished = $false
    try {
        try {
            Invoke-PgzInstaller
        } catch {
            Write-PgzUnexpectedError $_
            $script:PgzExitCode = 1
        }
        $script:PgzFinished = $true
    } finally {
        # Not finished here means Ctrl+C stopped the script (finally blocks
        # still run then, catch blocks do not).
        if (-not $script:PgzFinished) {
            $script:PgzExitCode = 130
            $global:LASTEXITCODE = 130
            try { Write-Host ""; Write-Host "Installation cancelled." } catch { }
        }
    }
}
# `exit` would close the window when this runs through `irm | iex`, so only
# use it when running as a script file.
if ($PSCommandPath) { exit $script:PgzExitCode }
$global:LASTEXITCODE = $script:PgzExitCode
