#!/usr/bin/env bash
# Pygame Zero Installer: macOS / Linux bootstrapper.
#
#   curl -fsSL https://raw.githubusercontent.com/KylerChan/pygamezero-installer/v1.0.0/install.sh | bash
#
# This script only finds a Python 3 to run install.py with. It downloads
# install.py from a fixed release, checks its SHA-256 hash, and runs it.
# Everything else (choosing a compatible Python, the virtual environment,
# Pygame, Pygame Zero, PATH) happens in install.py.
#
# Pass options after `bash -s --`, for example:
#   curl -fsSL .../install.sh | bash -s -- --uninstall
#
# The values below are written by tools/release.py. Do not edit by hand.
PGZ_REPO="KylerChan/pygamezero-installer"
PGZ_VERSION="v1.0.0"
PGZ_INSTALL_PY_SHA256="d15b2c0ce6fd5a2faf068b65bd7b78dc3c36c8b18962ed0915c37b7c08237911"

# Everything is inside main(), so a partially downloaded script does nothing.
main() {
    set -u

    say() { printf '%s\n' "$*"; }
    die() { printf '\n%s\n' "$1" >&2; exit "${2:-1}"; }

    # --- find a Python 3 that can run install.py (any 3.6+) -------------------
    find_python() {
        for name in python3 python; do
            candidate=$(command -v "$name" 2>/dev/null) || continue
            # macOS: /usr/bin/python3 is a stub that pops up an install dialog
            # unless the Command Line Tools are installed. Don't trigger it.
            if [ "$(uname -s)" = "Darwin" ] && [ "$candidate" = "/usr/bin/python3" ] \
                && ! xcode-select -p >/dev/null 2>&1; then
                continue
            fi
            if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 6) else 1)' \
                >/dev/null 2>&1; then
                printf '%s' "$candidate"
                return 0
            fi
        done
        return 1
    }

    no_python_help() {
        say ""
        say "Python 3 was not found on this computer."
        say "Pygame Zero needs Python 3.9 to 3.13 (3.13 is recommended)."
        say ""
        case "$(uname -s)" in
            Darwin)
                if command -v brew >/dev/null 2>&1; then
                    say "Install it with Homebrew:"
                    say "    brew install python@3.13"
                else
                    say "Download and run the macOS installer from:"
                    say "    https://www.python.org/downloads/macos/"
                fi
                ;;
            Linux)
                say "Install it with your package manager, for example:"
                if command -v apt-get >/dev/null 2>&1; then
                    say "    sudo apt update && sudo apt install python3 python3-venv"
                elif command -v dnf >/dev/null 2>&1; then
                    say "    sudo dnf install python3"
                elif command -v pacman >/dev/null 2>&1; then
                    say "    sudo pacman -S python"
                elif command -v zypper >/dev/null 2>&1; then
                    say "    sudo zypper install python3"
                else
                    say "    (use your distribution's package manager to install python3)"
                fi
                ;;
        esac
        say ""
        say "Then open a new terminal and run this installer again."
    }

    case "$(uname -s)" in
        Darwin|Linux) ;;
        *) die "This script is for macOS and Linux. On Windows, use install.ps1 in PowerShell." 2 ;;
    esac

    PY=$(find_python) || { no_python_help; exit 3; }

    # --- get install.py --------------------------------------------------------
    # Running from a downloaded copy (bash install.sh)? Use the install.py next
    # to it, so people can inspect both files before running them.
    script_dir=""
    if [ -n "${BASH_SOURCE:-}" ] && [ -f "${BASH_SOURCE[0]:-}" ]; then
        script_dir=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
    fi

    tmpdir=""
    cleanup() { if [ -n "$tmpdir" ]; then rm -rf "$tmpdir"; fi; }
    trap cleanup EXIT

    if [ -n "$script_dir" ] && [ -f "$script_dir/install.py" ]; then
        installer="$script_dir/install.py"
        say "Using local installer: $installer"
    else
        base="${PGZ_INSTALLER_BASE_URL:-https://raw.githubusercontent.com/$PGZ_REPO/$PGZ_VERSION}"
        case "$base" in
            https://*) ;;
            *) die "Refusing to download over an insecure connection: $base" 1 ;;
        esac
        tmpdir=$(mktemp -d 2>/dev/null || mktemp -d -t pgzero) || die "Could not create a temporary folder." 1
        installer="$tmpdir/install.py"
        url="$base/install.py"
        if command -v curl >/dev/null 2>&1; then
            curl -fsSL --proto '=https' --tlsv1.2 --retry 3 -o "$installer" "$url"
        elif command -v wget >/dev/null 2>&1; then
            wget -q --https-only -O "$installer" "$url"
        else
            die "Neither curl nor wget is available to download the installer." 1
        fi || die "Could not download $url
Check your internet connection and that the link is correct, then try again." 6

        # Integrity check: the hash of install.py is pinned in this script.
        # PGZ_EXPECTED_SHA256 can override it when testing your own build.
        expected="${PGZ_EXPECTED_SHA256:-$PGZ_INSTALL_PY_SHA256}"
        actual=$("$PY" -c 'import hashlib, sys; print(hashlib.sha256(open(sys.argv[1], "rb").read()).hexdigest())' "$installer") \
            || die "Could not check the downloaded installer." 1
        if [ "$actual" != "$expected" ]; then
            die "The downloaded installer did not match its expected fingerprint (SHA-256).
It may have been changed or corrupted, so it was NOT run.
    expected: $expected
    got:      $actual" 1
        fi
    fi

    # --- run it ---------------------------------------------------------------
    # With `curl | bash`, stdin is the script itself, so give the installer the
    # terminal for its yes/no questions when there is one.
    if (exec </dev/tty) 2>/dev/null; then
        "$PY" -I "$installer" "$@" </dev/tty
    else
        "$PY" -I "$installer" "$@"
    fi
}

main "$@"
