#!/usr/bin/env bash
# Pygame Zero Installer: macOS / Linux bootstrapper.
#
#   curl -fsSL https://kylerchan.github.io/pygamezero-installer/install.sh | bash
#
# This script only finds a Python 3 to run install.py with. It downloads
# install.py from a fixed release, checks its SHA-256 hash, and runs it.
# Everything else (choosing a compatible Python, the virtual environment,
# Pygame, Pygame Zero, PATH) happens in install.py.
#
# Pass options after `bash -s --`, for example:
#   curl -fsSL .../install.sh | bash -s -- --uninstall

# This check must stay first and use only plain POSIX sh: `curl ... | sh`
# runs this file with dash/zsh/sh, which would choke on the bash code below.
if [ -z "${BASH_VERSION:-}" ]; then
    printf '%s\n' \
        "" \
        "This installer needs bash, but it was started with a different shell." \
        "Please run it with bash instead:" \
        "" \
        "    curl -fsSL https://kylerchan.github.io/pygamezero-installer/install.sh | bash" \
        "" \
        "or, if you downloaded install.sh:" \
        "" \
        "    bash install.sh" >&2
    exit 1
fi

# The values below are written by tools/release.py. Do not edit by hand.
PGZ_SITE="https://kylerchan.github.io/pygamezero-installer"
PGZ_VERSION="v1.2.0"
PGZ_INSTALL_PY_SHA256="60c494459b0925437f5774e742807af66d3cc753907f282b4a3a20834fb259af"

# Everything is inside main(), so a partially downloaded script does nothing.
# Must run on macOS's bash 3.2: no bash 4 features.
main() {
    set -u
    unset CDPATH  # `cd` would print the folder and break $(cd ... && pwd)

    say() { printf '%s\n' "$*"; }
    die() { printf '\n%s\n' "$1" >&2; exit "${2:-1}"; }

    os_name=$(uname -s 2>/dev/null) || os_name=unknown
    case "$os_name" in
        Darwin|Linux) ;;
        *) die "This script is for macOS and Linux. On Windows, use install.ps1 in PowerShell." 2 ;;
    esac

    # --- clean up and Ctrl+C ----------------------------------------------------
    tmpdir=""
    cleanup() { if [ -n "$tmpdir" ]; then rm -rf "$tmpdir"; fi; }
    on_signal() {
        trap - INT TERM HUP
        printf '\n\n%s\n' "Cancelled. You can safely run the installer again." >&2
        exit "$1"
    }
    trap cleanup EXIT
    trap 'on_signal 130' INT
    trap 'on_signal 143' TERM
    trap 'on_signal 129' HUP

    # --- find a Python 3 that can run install.py (any 3.6+) -------------------
    # Sets PY. Candidates that exist but do not work (Python 2, a broken pyenv
    # shim, no permission...) are skipped and listed in $skipped.
    PY=""
    skipped=""
    seen=" "
    find_python() {
        local name candidate out rc
        for name in python3 python python3.13 python3.12 python3.11 python3.10 python3.9; do
            candidate=$(type -P "$name" 2>/dev/null) || continue
            [ -n "$candidate" ] || continue
            case "$seen" in *" $candidate "*) continue ;; esac
            seen="$seen$candidate "
            # macOS: /usr/bin/python3 is a stub that pops up an install dialog
            # unless the Command Line Tools are installed. Don't trigger it.
            if [ "$os_name" = "Darwin" ] && [ "$candidate" = "/usr/bin/python3" ] \
                && ! xcode-select -p >/dev/null 2>&1; then
                skipped="$skipped
    $candidate (Apple's placeholder; the Command Line Tools are not installed)"
                continue
            fi
            # This line must run on Python 2 too, so it can say "too old".
            out=$("$candidate" -c 'import sys; sys.stdout.write("%d.%d" % sys.version_info[:2]); sys.exit(0 if sys.version_info >= (3, 6) else 1)' \
                </dev/null 2>&1)
            rc=$?
            out=${out%%$'\n'*}  # first line only
            if [ "$rc" -eq 0 ]; then
                PY=$candidate
                return 0
            elif [ "$rc" -eq 1 ] && [ -n "$out" ] && [ -z "${out//[0-9.]/}" ]; then
                skipped="$skipped
    $candidate (Python $out is too old)"
            elif [ "$rc" -eq 126 ]; then
                skipped="$skipped
    $candidate (not allowed to run it: permission denied)"
            else
                skipped="$skipped
    $candidate (does not work: ${out:-exit code $rc})"
            fi
        done
        return 1
    }

    no_python_help() {
        say ""
        if [ -n "$skipped" ]; then
            say "No working Python 3 was found on this computer."
            say "These were found but skipped:$skipped"
        else
            say "Python 3 was not found on this computer."
        fi
        say "Pygame Zero needs Python 3.9 to 3.13 (3.13 is recommended)."
        say ""
        case "$os_name" in
            Darwin)
                if type -P brew >/dev/null 2>&1; then
                    say "Install it with Homebrew:"
                    say "    brew install python@3.13"
                else
                    say "Download and run the macOS installer from:"
                    say "    https://www.python.org/downloads/macos/"
                fi
                ;;
            Linux)
                say "Install it with your package manager, for example:"
                if type -P apt-get >/dev/null 2>&1; then
                    say "    sudo apt update && sudo apt install python3 python3-venv"
                elif type -P dnf >/dev/null 2>&1; then
                    say "    sudo dnf install python3"
                elif type -P pacman >/dev/null 2>&1; then
                    say "    sudo pacman -S python"
                elif type -P zypper >/dev/null 2>&1; then
                    say "    sudo zypper install python3"
                else
                    say "    (use your distribution's package manager to install python3)"
                fi
                ;;
        esac
        say ""
        say "Then open a new terminal and run this installer again."
    }

    find_python || { no_python_help; exit 3; }

    # --- download helpers -------------------------------------------------------
    make_tmpdir() {
        local dir
        for dir in "${TMPDIR:-/tmp}" /tmp "${HOME:-/nonexistent}"; do
            tmpdir=$(mktemp -d "${dir%/}/pgzero-installer.XXXXXX" 2>/dev/null) && [ -d "$tmpdir" ] && return 0
        done
        tmpdir=""
        die "Could not create a temporary folder to download the installer into.
Your disk may be full, or the temporary folder (${TMPDIR:-/tmp}) is not writable.
Free up some disk space and try again." 1
    }

    no_downloader_help() {
        say ""
        say "This installer needs curl or wget to download files, and neither was found."
        case "$os_name" in
            Darwin)
                say "macOS normally includes curl, so something unusual is going on."
                say "You can download install.sh and install.py from"
                say "    $PGZ_SITE/releases/$PGZ_VERSION/"
                say "put them in the same folder, and run:  bash install.sh"
                ;;
            Linux)
                say "Install curl, for example:"
                if type -P apt-get >/dev/null 2>&1; then say "    sudo apt update && sudo apt install curl"
                elif type -P dnf >/dev/null 2>&1; then say "    sudo dnf install curl"
                elif type -P pacman >/dev/null 2>&1; then say "    sudo pacman -S curl"
                elif type -P zypper >/dev/null 2>&1; then say "    sudo zypper install curl"
                elif type -P apk >/dev/null 2>&1; then say "    sudo apk add curl"
                else say "    (use your distribution's package manager to install curl)"
                fi
                ;;
        esac
        say "Then run this installer again."
    }

    # One download attempt. Sets dl_kind to "" on success, otherwise to one of:
    # dns connect timeout tls cert http proxy partial disk insecure other.
    # Sets dl_detail to the tool's own error message and http_code if known.
    download_once() {
        local url=$1 out=$2 log="$tmpdir/download.log" rc help
        dl_kind="" dl_detail="" http_code=""
        : >"$out" 2>/dev/null || { dl_kind=disk; return; }
        if [ "$downloader" = curl ]; then
            http_code=$(curl -fsSL --proto '=https' --tlsv1.2 --connect-timeout 15 --max-time 120 \
                -w '%{http_code}' -o "$out" "$url" 2>"$log")
            rc=$?
            case "$rc" in
                0) return ;;
                6) dl_kind=dns ;;
                7) dl_kind=connect ;;
                28) dl_kind=timeout ;;
                35|53|54|58|59|66|80) dl_kind=tls ;;
                51|60|77|83|90|91) dl_kind=cert ;;
                22) dl_kind=http ;;
                5|97) dl_kind=proxy ;;
                16|18|52|55|56|92) dl_kind=partial ;;
                23) dl_kind=disk ;;
                1) dl_kind=insecure ;;
                *) dl_kind=other ;;
            esac
            # Newer curl reports some HTTP errors (e.g. 404 over HTTP/2) as 56.
            case "$http_code" in [45][0-9][0-9]) dl_kind=http ;; esac
        else
            help=$(wget --help 2>&1)
            case "$help" in
                *--https-only*)  # GNU wget
                    wget -nv --https-only --timeout=15 --tries=1 -O "$out" "$url" 2>"$log" ;;
                *)               # BusyBox wget
                    wget -q -T 60 -O "$out" "$url" 2>"$log" ;;
            esac
            rc=$?
            case "$rc" in
                0) return ;;
                3) dl_kind=disk ;;
                4) dl_kind=connect ;;  # DNS, connect or timeout: wget doesn't say which
                5) dl_kind=cert ;;
                8) dl_kind=http ;;
                *) dl_kind=other ;;
            esac
            if [ "$dl_kind" = http ] && [ -s "$log" ]; then
                help=$(<"$log")
                case "$help" in *"ERROR "[0-9]*)
                    http_code=${help##*ERROR }
                    http_code=${http_code%%[!0-9]*} ;;
                esac
            fi
        fi
        dl_detail=""
        if [ -s "$log" ]; then  # last non-empty line of the tool's error output
            while IFS= read -r help || [ -n "$help" ]; do
                [ -z "$help" ] || dl_detail=$help
            done <"$log"
        fi
        [ -n "$dl_detail" ] || dl_detail="$downloader exit code $rc"
    }

    download_failed() {
        local msg
        case "$dl_kind" in
            dns) msg="Could not find the download server (DNS lookup failed).
Check that you are connected to the internet (try opening a web page), then try again." ;;
            connect) msg="Could not connect to the download server.
Check that you are connected to the internet (try opening a web page).
If you are on a school or work network, it may block GitHub; try another network." ;;
            timeout) msg="The download timed out. Your connection may be slow or unstable.
Check your internet connection and try again." ;;
            tls|cert) msg="Could not make a secure (HTTPS) connection to the download server.
This often happens on school or work networks that inspect HTTPS traffic,
or when the computer's date and time are wrong. Check the clock, or try
another network (for example a phone hotspot)." ;;
            http)
                case "$http_code" in
                    404) msg="The installer file was not found (error 404).
The link may be wrong or this version was removed. Get the current command from
    https://kylerchan.github.io/pygamezero-installer/" ;;
                    403|429) msg="The download server refused the request (error $http_code).
Your network may be blocking GitHub or too many people downloaded at once.
Wait a few minutes and try again, or try another network." ;;
                    5*) msg="The download server is having problems (error $http_code).
Wait a few minutes and try again." ;;
                    *) msg="The download server returned an error${http_code:+ ($http_code)}.
The link may be wrong or this version was removed." ;;
                esac ;;
            proxy) msg="Could not connect through the network proxy.
Check your proxy settings (the https_proxy / HTTPS_PROXY variables), or try another network." ;;
            partial) msg="The download was interrupted before it finished.
Your connection may be unstable. Try again." ;;
            disk) die "Could not save the download to $tmpdir.
Your disk may be full. Free up some space and try again.

    Details: $dl_detail" 1 ;;
            insecure) msg="The download was redirected to an insecure (non-HTTPS) address, so it was stopped." ;;
            *) msg="Could not download the installer." ;;
        esac
        die "$msg

    Address: $url
    Details: $dl_detail" 6
    }

    # Retries transient failures (no internet yet, Wi-Fi dropping out, busy server).
    download() {
        local attempt=1
        while :; do
            download_once "$url" "$installer"
            [ -n "$dl_kind" ] || return 0
            case "$dl_kind:$http_code" in
                dns:*|connect:*|timeout:*|tls:*|partial:*|http:408|http:429|http:5*) ;;
                *) download_failed ;;
            esac
            [ "$attempt" -lt 3 ] || download_failed
            say "Download failed ($dl_detail). Trying again..." >&2
            sleep $((attempt * 2))
            attempt=$((attempt + 1))
        done
    }

    # --- get install.py --------------------------------------------------------
    # Running from a downloaded copy (bash install.sh)? Use the install.py next
    # to it, so people can inspect both files before running them. With
    # `curl | bash` BASH_SOURCE is empty, so we download it instead.
    script_dir=""
    if [ -n "${BASH_SOURCE[0]:-}" ] && [ -f "${BASH_SOURCE[0]}" ]; then
        script_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" 2>/dev/null && pwd -P) || script_dir=""
    fi

    if [ -n "$script_dir" ] && [ -f "$script_dir/install.py" ]; then
        installer="$script_dir/install.py"
        [ -r "$installer" ] || die "Cannot read $installer (permission denied)." 1
        say "Using local installer: $installer"
    else
        base="${PGZ_INSTALLER_BASE_URL:-$PGZ_SITE/releases/$PGZ_VERSION}"
        base=${base%/}
        case "$base" in
            https://*) ;;
            *) die "Refusing to download over an insecure connection: $base" 1 ;;
        esac
        url="$base/install.py"

        if type -P curl >/dev/null 2>&1; then downloader=curl
        elif type -P wget >/dev/null 2>&1; then downloader=wget
        else no_downloader_help >&2; exit 1
        fi

        make_tmpdir
        installer="$tmpdir/install.py"
        download

        # Wi-Fi login pages (captive portals) answer every address with a web
        # page, which would otherwise show up as a confusing hash mismatch.
        if [ ! -s "$installer" ]; then
            die "The downloaded installer was empty.
If you are on school or public Wi-Fi, you may need to log in first: open a web
page in your browser to log in to the Wi-Fi, then try again.

    Address: $url" 6
        fi
        head_bytes=""
        IFS= read -r -d '' -n 512 head_bytes <"$installer" || true
        case "$head_bytes" in
            *"<html"*|*"<HTML"*|*"<!DOCTYPE"*|*"<!doctype"*|*"<head"*|*"<HEAD"*|*"<body"*|*"<BODY"*)
                die "A web page was downloaded instead of the installer.
This usually means the Wi-Fi wants you to log in first (common on school and
public Wi-Fi): open a web page in your browser to log in to the Wi-Fi, then
try again. If that doesn't help, your network may be blocking GitHub.

    Address: $url" 6 ;;
        esac

        # Integrity check: the hash of install.py is pinned in this script.
        # PGZ_EXPECTED_SHA256 can override it when testing your own build.
        expected="${PGZ_EXPECTED_SHA256:-$PGZ_INSTALL_PY_SHA256}"
        actual=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$installer") \
            || die "Could not check the downloaded installer (Python failed to read it)." 1
        if [ "$actual" != "$expected" ]; then
            die "The downloaded installer did not match its expected fingerprint (SHA-256).
It may have been changed or corrupted, so it was NOT run.
If you are on a school or work network, it may be altering downloads; try
another network, or try again later.
    expected: $expected
    got:      $actual" 1
        fi
    fi

    # --- run it ---------------------------------------------------------------
    # install.py handles Ctrl+C itself (prints a message, exits 130). Keep our
    # INT trap a no-op meanwhile (a trap, not '', so Python still gets SIGINT).
    trap ':' INT
    # With `curl | bash`, stdin is the script itself, so give the installer the
    # terminal for its yes/no questions when there is one.
    if (exec </dev/tty) 2>/dev/null; then
        "$PY" -I "$installer" "$@" </dev/tty
    else
        "$PY" -I "$installer" "$@"
    fi
    rc=$?
    case "$rc" in
        126) die "Could not start Python ($PY): permission denied." 126 ;;
        127) die "Could not start Python ($PY): it was not found or is broken." 127 ;;
    esac
    exit "$rc"
}

main "$@"
