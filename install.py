#!/usr/bin/env python3
"""Pygame Zero Installer.

Installs Pygame and Pygame Zero into an isolated virtual environment and puts
a `pgzrun` command on the user's PATH. Works on Windows, macOS and Linux.

This file is the shared installer used by install.sh and install.ps1, but it
can also be run directly:

    python3 install.py              # install (or repair) everything
    python3 install.py --uninstall  # remove everything this installer created

A full log of each run is written to install.log in the install folder (or
to the temporary folder if that is not possible); failures print its path.

It only uses the Python standard library and must stay runnable on Python 3.6+
because the bootstrappers launch it with whatever Python 3 they can find. The
Python used for the game environment is chosen separately (see SUPPORTED_*).
"""

import argparse
import errno
import glob
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import traceback

INSTALLER_VERSION = "1.1.0"

# Pinned, tested package versions. pygame 2.6.1 publishes binary wheels for
# CPython 3.6-3.13 only; on 3.14+ pip falls back to compiling pygame from
# source, which fails on student machines. pgzero 1.2.1 requires pygame~=2.0.
PYGAME_SPEC = "pygame==2.6.1"
PGZERO_SPEC = "pgzero==1.2.1"
SUPPORTED_MIN = (3, 9)
SUPPORTED_MAX = (3, 13)
RECOMMENDED = (3, 13)

MARKER_FILE = "pygamezero-installer.json"
LOG_FILE = "install.log"
LOG_MAX_BYTES = 1024 * 1024
ISSUES_URL = "https://github.com/KylerChan/pygamezero-installer/issues"
SHIM_MARKER = "managed-by: pygamezero-installer"
RC_BEGIN = "# >>> pygamezero-installer >>>"
RC_END = "# <<< pygamezero-installer <<<"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_UNSUPPORTED_PLATFORM = 2
EXIT_NO_PYTHON = 3
EXIT_ENV = 4
EXIT_PACKAGE = 5
EXIT_NETWORK = 6
EXIT_PERMISSION = 7
EXIT_VERIFY = 8

TOTAL_STEPS = 7

# Free space needed on the install drive: pip's download plus the unpacked
# environment (pygame, pgzero, numpy) is roughly 300 MB at its peak.
REQUIRED_FREE_MB = 500
REQUIRED_FREE_MB_REUSE = 100
PIP_TIMEOUT = 900
VERIFY_TIMEOUT = 120
NETWORK_RETRY_DELAY = 5
# Pauses between attempts when Windows reports a file as in use (a running
# game, an editor, or antivirus scanning a freshly written file).
LOCK_RETRY_DELAYS = (0.5, 1, 2, 3)
# Some older Windows tools cut PATH at this length.
WINDOWS_PATH_WARN_LENGTH = 2047

# Printed by an interpreter we probe. Must run on any Python 3.
PROBE_CODE = r"""
import json, platform, struct, sys, sysconfig
try:
    import venv
    has_venv = True
except Exception:
    has_venv = False
try:
    import ensurepip
    has_ensurepip = True
except Exception:
    has_ensurepip = False
print(json.dumps({
    "version": list(sys.version_info[:3]),
    "implementation": platform.python_implementation(),
    "executable": sys.executable,
    "bits": struct.calcsize("P") * 8,
    "platform": sysconfig.get_platform(),
    "free_threaded": bool(sysconfig.get_config_var("Py_GIL_DISABLED")),
    "in_venv": sys.prefix != getattr(sys, "base_prefix", sys.prefix),
    "has_venv": has_venv,
    "has_ensurepip": has_ensurepip,
}))
"""

# Tiny Pygame Zero game used to prove the install works end to end. It runs
# headless (SDL dummy drivers), draws a few frames and exits.
SMOKE_GAME = """\
import sys
WIDTH = 160
HEIGHT = 120
frames = 0

def draw():
    screen.fill((40, 120, 200))
    screen.draw.text("ok", (10, 10))

def update():
    global frames
    frames += 1
    if frames >= 3:
        print("PGZERO_SMOKE_OK")
        sys.exit(0)
"""
SMOKE_TOKEN = "PGZERO_SMOKE_OK"


class InstallError(Exception):
    """A failure with a beginner-friendly message and an exit code."""

    def __init__(self, message, code=EXIT_ERROR, details=None):
        Exception.__init__(self, message)
        self.message = message
        self.code = code
        self.details = details


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

class Console(object):
    def __init__(self, stream=None):
        self.stream = stream or sys.stdout
        self.color = (
            hasattr(self.stream, "isatty")
            and self.stream.isatty()
            and os.environ.get("NO_COLOR") is None
            and os.environ.get("TERM") != "dumb"
        )
        if self.color and os.name == "nt":
            self.color = _enable_windows_ansi()

    def _paint(self, text, code):
        if self.color:
            return "\033[%sm%s\033[0m" % (code, text)
        return text

    def write(self, text=""):
        try:
            self.stream.write(text + "\n")
        except UnicodeEncodeError:
            enc = getattr(self.stream, "encoding", None) or "ascii"
            self.stream.write(text.encode(enc, "replace").decode(enc) + "\n")
        self.stream.flush()

    def step(self, n, label):
        try:
            self.stream.write("[%d/%d] %s... " % (n, TOTAL_STEPS, label))
        except UnicodeEncodeError:
            self.stream.write("[%d/%d] ... " % (n, TOTAL_STEPS))
        self.stream.flush()

    def ok(self, text="OK"):
        self.write(self._paint(text, "32"))

    def fail(self, text="FAILED"):
        self.write(self._paint(text, "31"))

    def warn(self, text):
        self.write(self._paint("Warning: ", "33") + text)

    def bold(self, text):
        return self._paint(text, "1")


def _enable_windows_ansi():
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.GetStdHandle(-11)
        mode = ctypes.c_uint32()
        if not kernel32.GetConsoleMode(handle, ctypes.byref(mode)):
            return False
        return bool(kernel32.SetConsoleMode(handle, mode.value | 0x0004))
    except Exception:
        return False


def tail(text, lines=25):
    parts = (text or "").strip().splitlines()
    return "\n".join(parts[-lines:])


class InstallLog(object):
    """Full details of a run (commands, pip output, errors) for a teacher to read.

    Lines are kept in memory until open() picks a file, so nothing is lost
    when a failure happens before the install folder exists.
    """

    def __init__(self):
        self.path = None
        self.pending = []

    def write(self, text):
        line = text if text.endswith("\n") else text + "\n"
        if self.path is None:
            if len(self.pending) < 2000:
                self.pending.append(line)
            return
        try:
            with open(self.path, "a", encoding="utf-8", errors="replace") as fh:
                fh.write(line)
        except (OSError, ValueError):
            pass

    def open(self, folder):
        """Start writing to <folder>/install.log. Returns True on success."""
        path = os.path.join(folder, LOG_FILE)
        try:
            if os.path.islink(path):
                return False
            mode = "a"
            if os.path.isfile(path) and os.path.getsize(path) > LOG_MAX_BYTES:
                mode = "w"
            with open(path, mode, encoding="utf-8", errors="replace") as fh:
                fh.write("".join(self.pending))
        except (OSError, ValueError):
            return False
        self.path, self.pending = path, []
        return True

    def ensure_open(self):
        """Make sure the log is on disk somewhere. Returns its path or None."""
        if self.path is None:
            try:
                # An unpredictable name: the temp folder may be shared.
                fd, path = tempfile.mkstemp(prefix="pygamezero-install-", suffix=".log",
                                            dir=fallback_log_dir())
                with os.fdopen(fd, "w", encoding="utf-8", errors="replace") as fh:
                    fh.write("".join(self.pending))
                self.path, self.pending = path, []
            except (OSError, ValueError):
                return None
        return self.path


def fallback_log_dir():
    return tempfile.gettempdir()


LOG = InstallLog()


# --------------------------------------------------------------------------
# Step 1: operating system detection
# --------------------------------------------------------------------------

def _read_os_release(path="/etc/os-release"):
    info = {}
    try:
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if "=" in line and not line.startswith("#"):
                    key, _, value = line.partition("=")
                    info[key] = value.strip().strip('"').strip("'")
    except (IOError, OSError):
        pass
    return info


def _is_musl():
    return bool(glob.glob("/lib/ld-musl-*")) or bool(glob.glob("/usr/lib/ld-musl-*"))


def _mac_is_apple_silicon():
    try:
        out = subprocess.check_output(
            ["sysctl", "-n", "hw.optional.arm64"], stderr=subprocess.DEVNULL
        )
        return out.strip() == b"1"
    except (OSError, subprocess.CalledProcessError):
        return platform.machine() == "arm64"


def _windows_native_machine():
    """Return the real CPU architecture, even inside an emulated process."""
    try:
        import ctypes

        process_machine = ctypes.c_ushort()
        native_machine = ctypes.c_ushort()
        kernel32 = ctypes.windll.kernel32
        if kernel32.IsWow64Process2(
            kernel32.GetCurrentProcess(),
            ctypes.byref(process_machine),
            ctypes.byref(native_machine),
        ):
            return {0xAA64: "ARM64", 0x8664: "AMD64", 0x014C: "x86"}.get(
                native_machine.value, platform.machine()
            )
    except Exception:
        pass
    return os.environ.get("PROCESSOR_ARCHITEW6432") or platform.machine()


def normalize_arch(machine):
    m = (machine or "").lower()
    if m in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if m in ("arm64", "aarch64", "armv8l"):
        return "arm64"
    if m in ("i386", "i686", "x86"):
        return "x86"
    return m or "unknown"


def detect_os():
    """Return a dict describing the host: system, arch, pretty name, notes."""
    system = platform.system()
    info = {"system": system, "arch": normalize_arch(platform.machine()),
            "name": system, "musl": False, "distro_id": "", "distro_like": ""}
    if system == "Windows":
        info["arch"] = normalize_arch(_windows_native_machine())
        build = 0
        try:
            build = int(platform.version().split(".")[2])
        except (IndexError, ValueError):
            pass
        info["name"] = "Windows 11" if build >= 22000 else "Windows %s" % platform.release()
    elif system == "Darwin":
        info["arch"] = "arm64" if _mac_is_apple_silicon() else "x86_64"
        ver = platform.mac_ver()[0]
        chip = "Apple Silicon" if info["arch"] == "arm64" else "Intel"
        info["name"] = "macOS %s (%s)" % (ver, chip) if ver else "macOS (%s)" % chip
    elif system == "Linux":
        rel = _read_os_release()
        info["distro_id"] = rel.get("ID", "")
        info["distro_like"] = rel.get("ID_LIKE", "")
        info["name"] = rel.get("PRETTY_NAME") or "Linux"
        info["musl"] = _is_musl()
    return info


def check_platform_supported(os_info):
    system, arch = os_info["system"], os_info["arch"]
    if system not in ("Windows", "Darwin", "Linux"):
        raise InstallError(
            "This installer supports Windows, macOS and Linux, but this computer "
            "is running %s." % system, EXIT_UNSUPPORTED_PLATFORM)
    if system == "Linux" and os_info["musl"]:
        raise InstallError(
            "This Linux system uses the musl C library (for example Alpine Linux). "
            "Pygame does not publish ready-made packages for it, so it cannot be "
            "installed automatically. Use a mainstream distribution such as Ubuntu, "
            "Debian or Fedora.", EXIT_UNSUPPORTED_PLATFORM)
    if system == "Linux" and arch not in ("x86_64", "arm64", "x86"):
        raise InstallError(
            "Pygame does not publish ready-made packages for the %s processor "
            "architecture on Linux." % arch, EXIT_UNSUPPORTED_PLATFORM)


# --------------------------------------------------------------------------
# Step 2: Python discovery and selection
# --------------------------------------------------------------------------

def version_str(v):
    return ".".join(str(x) for x in v)


def _mac_has_command_line_tools():
    """/usr/bin/python3 on macOS pops up an install dialog without the CLT."""
    try:
        return subprocess.call(["xcode-select", "-p"], stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL) == 0
    except OSError:
        return False


def _which_all(name, path=None):
    """Every match of `name` on PATH, not just the first one."""
    found = []
    path = os.environ.get("PATH", "") if path is None else path
    exts = [""]
    if os.name == "nt":
        exts = [e.lower() for e in os.environ.get("PATHEXT", ".EXE").split(";") if e]
    for d in path.split(os.pathsep):
        if not d:
            continue
        for ext in exts:
            p = os.path.join(d, name + ext)
            if os.path.isfile(p) and os.access(p, os.X_OK):
                found.append(p)
    return found


def parse_py_launcher_list(output):
    """Extract interpreter paths from `py -0p` output (old and new formats)."""
    paths = []
    for line in output.splitlines():
        m = re.search(r"([A-Za-z]:\\.*?\.exe)\s*$", line.strip())
        if m:
            paths.append(m.group(1))
    return paths


def candidate_commands(os_info, env=None):
    """Interpreter executables worth probing, most likely first."""
    env = os.environ if env is None else env
    cands = []
    minors = range(SUPPORTED_MAX[1] + 3, SUPPORTED_MIN[1] - 1, -1)
    system = os_info["system"]

    if system == "Windows":
        for launcher in _which_all("py"):
            try:
                out = subprocess.check_output([launcher, "-0p"], stderr=subprocess.STDOUT,
                                              timeout=20)
                cands.extend(parse_py_launcher_list(out.decode("utf-8", "replace")))
            except (OSError, subprocess.SubprocessError):
                pass
        local = env.get("LOCALAPPDATA", "")
        patterns = []
        if local:
            patterns += [os.path.join(local, "Programs", "Python", "Python3*", "python.exe"),
                         os.path.join(local, "Python", "pythoncore-3.*", "python.exe")]
        for var in ("ProgramFiles", "ProgramFiles(x86)"):
            if env.get(var):
                patterns.append(os.path.join(env[var], "Python3*", "python.exe"))
        for pattern in patterns:
            cands.extend(sorted(glob.glob(pattern), reverse=True))
        for name in ("python", "python3"):
            cands.extend(_which_all(name))
    else:
        for minor in minors:
            cands.extend(_which_all("python3.%d" % minor))
        cands.extend(_which_all("python3"))
        cands.extend(_which_all("python"))
        if system == "Darwin":
            for minor in minors:
                cands.append("/Library/Frameworks/Python.framework/Versions/3.%d/bin/python3" % minor)
                cands.append("/opt/homebrew/bin/python3.%d" % minor)
                cands.append("/usr/local/bin/python3.%d" % minor)
            if not _mac_has_command_line_tools():
                stub = "/usr/bin/python3"
                cands = [c for c in cands if c != stub and os.path.realpath(c) != stub]
    cands.append(sys.executable)

    seen, unique = set(), []
    for c in cands:
        if not c or not os.path.isfile(c):
            continue
        key = os.path.normcase(os.path.realpath(c))
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def clean_env(extra=None):
    """Environment for child Pythons, without variables that break venvs."""
    env = dict(os.environ)
    for var in ("PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "PIP_USER",
                "PIP_TARGET", "PIP_PREFIX", "PIP_REQUIRE_VIRTUALENV", "VIRTUAL_ENV",
                "__PYVENV_LAUNCHER__", "CONDA_PREFIX"):
        env.pop(var, None)
    env["PYTHONIOENCODING"] = "utf-8"
    if extra:
        env.update(extra)
    return env


def probe(executable, runner=subprocess.run, errors=None):
    """Ask an interpreter about itself. Returns a dict, or None if unusable.

    If `errors` is a list, a short reason is appended to it on failure.
    """
    errors = [] if errors is None else errors
    try:
        proc = runner([executable, "-I", "-c", PROBE_CODE], stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, timeout=30, env=clean_env())
    except subprocess.TimeoutExpired:
        errors.append("it did not respond within 30 seconds")
        LOG.write("probe %s: timed out" % executable)
        return None
    except (OSError, subprocess.SubprocessError) as exc:
        errors.append("it could not be run (%s)" % exc)
        return None
    if proc.returncode != 0:
        errors.append("it could not be run (it exited with an error)")
        LOG.write("probe %s failed:\n%s" % (executable, (proc.stderr or b"").decode(
            "utf-8", "replace")))
        return None
    try:
        out = proc.stdout.decode("utf-8", "replace").strip().splitlines()[-1]
        data = json.loads(out)
    except (ValueError, IndexError):
        return None
    data["path"] = executable
    return data


def assess(info, os_info):
    """Return (usable, reason). `reason` explains why an interpreter is skipped."""
    v = tuple(info["version"][:2])
    if info.get("implementation") != "CPython":
        return False, "%s is not CPython" % info.get("implementation")
    if v < SUPPORTED_MIN:
        return False, "Python %s is too old (need %s to %s)" % (
            version_str(v), version_str(SUPPORTED_MIN), version_str(SUPPORTED_MAX))
    if v > SUPPORTED_MAX:
        return False, "Python %s is too new: Pygame does not support it yet (need %s to %s)" % (
            version_str(v), version_str(SUPPORTED_MIN), version_str(SUPPORTED_MAX))
    if info.get("free_threaded"):
        return False, "free-threaded Python builds are not supported by Pygame"
    if info.get("in_venv"):
        return False, "it belongs to another virtual environment"
    plat = info.get("platform", "")
    if os_info["system"] == "Windows" and "arm64" in plat:
        return False, "Pygame has no packages for ARM64 Windows Python; use the 64-bit (x64) Python"
    if not info.get("has_venv") or not info.get("has_ensurepip"):
        return False, "virtual environment support (venv/ensurepip) is missing"
    return True, ""


def rank_key(info):
    v = tuple(info["version"][:3])
    return (v[:2] == RECOMMENDED, v, info.get("bits", 64) == 64)


def select_python(os_info, explicit=None, runner=subprocess.run):
    """Return (chosen_info_or_None, list_of_(info, reason)_rejections)."""
    paths = [explicit] if explicit else candidate_commands(os_info)
    good, rejected, seen = [], [], set()
    for path in paths:
        errors = []
        info = probe(path, runner=runner, errors=errors)
        if info is None:
            if explicit:
                reason = errors[0] if errors else "it could not be run"
                rejected.append(({"path": path, "version": [0, 0, 0]}, reason))
            continue
        key = os.path.normcase(os.path.realpath(info.get("executable") or path))
        if key in seen:
            continue
        seen.add(key)
        usable, reason = assess(info, os_info)
        if usable:
            good.append(info)
        else:
            rejected.append((info, reason))
    if not good:
        return None, rejected
    good.sort(key=rank_key, reverse=True)
    return good[0], rejected


def python_install_help(os_info, rejected):
    """Actionable instructions for getting a compatible Python."""
    rec = version_str(RECOMMENDED)
    lines = []
    missing_venv = [i for i, r in rejected if "venv" in r]
    if missing_venv and os_info["system"] == "Linux":
        v = version_str(missing_venv[0]["version"][:2])
        lines.append("Python %s is installed but its virtual environment support is missing." % v)
        lines.append("Install it with your package manager, then run this installer again:")
        lines.extend("    " + c for c in linux_venv_commands(os_info, v))
        return "\n".join(lines)

    lines.append("Pygame needs Python %s to %s. Python %s is recommended." % (
        version_str(SUPPORTED_MIN), version_str(SUPPORTED_MAX), rec))
    system = os_info["system"]
    if system == "Windows":
        arch = " --architecture x64" if os_info["arch"] == "arm64" else ""
        lines += [
            "Install it (no administrator rights needed) with:",
            "    winget install --id Python.Python.%s -e --scope user%s" % (rec, arch),
            "or download the Windows installer (64-bit) from:",
            "    https://www.python.org/downloads/windows/",
        ]
    elif system == "Darwin":
        lines += [
            "Install it with Homebrew (https://brew.sh):",
            "    brew install python@%s" % rec,
            "or download the macOS installer from:",
            "    https://www.python.org/downloads/macos/",
        ]
    else:
        lines += ["Install it with your package manager, for example:"]
        lines.extend("    " + c for c in linux_python_commands(os_info, rec))
        lines += [
            "If your distribution only offers a newer Python, you can install",
            "Python %s just for your user (no sudo) with uv (https://docs.astral.sh/uv/):" % rec,
            "    uv python install %s" % rec,
            "and then run:  python3 install.py --python \"$(uv python find %s)\"" % rec,
        ]
    lines.append("Then open a new terminal and run this installer again.")
    return "\n".join(lines)


def _linux_family(os_info):
    ids = (os_info.get("distro_id", "") + " " + os_info.get("distro_like", "")).lower().split()
    for family in ("debian", "ubuntu", "fedora", "rhel", "arch", "suse", "opensuse"):
        if family in ids:
            return {"ubuntu": "debian", "rhel": "fedora", "opensuse": "suse"}.get(family, family)
    return ""


def linux_python_commands(os_info, v):
    family = _linux_family(os_info)
    if family == "debian":
        return ["sudo apt update && sudo apt install python%s python%s-venv" % (v, v)]
    if family == "fedora":
        return ["sudo dnf install python%s" % v]
    if family == "arch":
        return ["sudo pacman -S python"]
    if family == "suse":
        return ["sudo zypper install python%s" % v.replace(".", "")]
    return ["(use your distribution's package manager to install Python %s)" % v]


def linux_venv_commands(os_info, v):
    family = _linux_family(os_info)
    if family == "debian":
        return ["sudo apt update && sudo apt install python%s-venv" % v]
    if family == "fedora":
        return ["sudo dnf install python%s" % v]
    if family == "suse":
        return ["sudo zypper install python%s" % v.replace(".", "")]
    return ["(install the 'venv' module for Python %s with your package manager)" % v]


def offer_python_install(os_info, console, assume_yes=False, stdin=None):
    """Offer a user-level install via winget/Homebrew. Returns True if it ran."""
    rec = version_str(RECOMMENDED)
    if os_info["system"] == "Windows" and shutil.which("winget"):
        cmd = ["winget", "install", "--id", "Python.Python.%s" % rec, "-e",
               "--scope", "user", "--source", "winget",
               "--accept-package-agreements", "--accept-source-agreements"]
        if os_info["arch"] == "arm64":
            cmd += ["--architecture", "x64"]
        what = "Python %s from python.org using winget (for your user only)" % rec
    elif os_info["system"] == "Darwin" and shutil.which("brew"):
        cmd = ["brew", "install", "python@%s" % rec]
        what = "Python %s using Homebrew" % rec
    else:
        return False

    stdin = stdin or sys.stdin
    if not assume_yes:
        if not (hasattr(stdin, "isatty") and stdin.isatty()):
            return False
        console.write("")
        console.write("I can install %s now by running:" % what)
        console.write("    " + " ".join(cmd))
        console.stream.write("Install it now? [y/N] ")
        console.stream.flush()
        answer = stdin.readline().strip().lower()
        if answer not in ("y", "yes"):
            return False
    console.write("Running: " + " ".join(cmd))
    try:
        return subprocess.call(cmd) == 0
    except OSError:
        return False


def refresh_windows_path():
    """Pick up PATH changes made by an installer we just ran (Windows only)."""
    try:
        import winreg
    except ImportError:
        return
    parts = []
    for root, sub in ((winreg.HKEY_LOCAL_MACHINE,
                       r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment"),
                      (winreg.HKEY_CURRENT_USER, "Environment")):
        try:
            with winreg.OpenKey(root, sub) as key:
                value, _ = winreg.QueryValueEx(key, "Path")
                parts.append(os.path.expandvars(value))
        except OSError:
            pass
    if parts:
        os.environ["PATH"] = os.pathsep.join(parts + [os.environ.get("PATH", "")])


# --------------------------------------------------------------------------
# Layout: where things live
# --------------------------------------------------------------------------

class Layout(object):
    def __init__(self, os_info, env=None):
        env = os.environ if env is None else env
        self.windows = os_info["system"] == "Windows"
        home = os.path.expanduser("~")
        if self.windows:
            needs_home = not (env.get("PGZERO_HOME") or env.get("LOCALAPPDATA"))
        else:
            needs_home = not (env.get("PGZERO_HOME") or env.get("XDG_DATA_HOME")) or \
                not env.get("PGZERO_BIN_DIR")
        if needs_home and not os.path.isabs(home):
            raise InstallError(
                "Could not work out where your home folder is (%s is not set).\n"
                "Choose a folder you can write to, for example:\n    %s\n"
                "and then run the installer again in the same window." % (
                    "USERPROFILE" if self.windows else "HOME",
                    '$env:PGZERO_HOME = "C:\\pgzero"' if self.windows
                    else "export PGZERO_HOME=/path/to/folder PGZERO_BIN_DIR=/path/to/folder/bin"),
                EXIT_ENV)
        if env.get("PGZERO_HOME"):
            self.home = os.path.abspath(os.path.expanduser(env["PGZERO_HOME"]))
        elif self.windows:
            base = env.get("LOCALAPPDATA") or os.path.join(home, "AppData", "Local")
            self.home = os.path.join(base, "pygamezero")
        else:
            base = env.get("XDG_DATA_HOME") or os.path.join(home, ".local", "share")
            self.home = os.path.join(base, "pygamezero")
        self.venv = os.path.join(self.home, "venv")
        self.marker = os.path.join(self.home, MARKER_FILE)
        if env.get("PGZERO_BIN_DIR"):
            self.bin_dir = os.path.abspath(os.path.expanduser(env["PGZERO_BIN_DIR"]))
        elif self.windows:
            self.bin_dir = os.path.join(self.home, "bin")
        else:
            self.bin_dir = os.path.join(home, ".local", "bin")
        scripts = "Scripts" if self.windows else "bin"
        exe = ".exe" if self.windows else ""
        self.venv_python = os.path.join(self.venv, scripts, "python" + exe)
        self.venv_pgzrun = os.path.join(self.venv, scripts, "pgzrun" + exe)
        self.shim = os.path.join(self.bin_dir, "pgzrun" + exe)


def _is_permission_error(exc):
    return isinstance(exc, PermissionError) or getattr(exc, "errno", None) in (1, 13, 30)


def _is_disk_full_error(exc):
    return getattr(exc, "errno", None) in (errno.ENOSPC, getattr(errno, "EDQUOT", -1)) or \
        getattr(exc, "winerror", None) in (39, 112)


def _is_lock_error(exc):
    """A file is in use by another program (Windows) or busy."""
    return getattr(exc, "winerror", None) in (32, 33) or getattr(exc, "errno", None) == errno.EBUSY


def _is_retryable_lock(exc):
    # Antivirus scanners briefly lock new files and Windows reports that as
    # "access denied" (WinError 5), so that is worth a few retries too.
    return _is_lock_error(exc) or (os.name == "nt" and getattr(exc, "winerror", None) == 5)


def retry_if_locked(func, *args):
    """Call func(*args), retrying a few times while a file is in use."""
    for delay in LOCK_RETRY_DELAYS + (None,):
        try:
            return func(*args)
        except OSError as exc:
            if delay is None or not _is_retryable_lock(exc):
                raise
            LOG.write("%s(%r) failed (%s); retrying in %ss" % (
                getattr(func, "__name__", func), args, exc, delay))
            time.sleep(delay)


DISK_FULL_PATTERNS = ("No space left on device", "[Errno 28]", "ENOSPC", "Disk quota exceeded",
                      "[WinError 112]", "There is not enough space on the disk")
LOCK_PATTERNS = ("[WinError 32]", "being used by another process", "[WinError 33]",
                 "Device or resource busy")


def disk_full_error(path, details=None):
    return InstallError(
        "Your disk is full, so the installer could not save files in:\n    %s\n"
        "Pygame Zero needs about %d MB of free space. Delete some files you no longer "
        "need (and empty the Trash or Recycle Bin), then run the installer again."
        % (path, REQUIRED_FREE_MB), EXIT_ENV, details=details)


def file_in_use_error(path, details=None):
    return InstallError(
        "Another program is using files in:\n    %s\n"
        "Close any running games, Python windows and editors (VS Code, Thonny, Mu...), "
        "wait a few seconds, then run the installer again. If it keeps happening, "
        "restart the computer (antivirus software can also hold files open)." % path,
        EXIT_PERMISSION, details=details)


def _root_owned_hint(path):
    """If an earlier `sudo` run left root-owned files in the user's home, say how to fix it."""
    if os.name == "nt" or not hasattr(os, "geteuid") or os.geteuid() == 0:
        return ""
    home = os.path.realpath(os.path.expanduser("~"))
    p = os.path.abspath(path)
    while not os.path.lexists(p) and os.path.dirname(p) != p:
        p = os.path.dirname(p)
    try:
        owned_by_root = os.lstat(p).st_uid == 0
    except OSError:
        return ""
    real = os.path.realpath(p)
    if not owned_by_root or real == home or not real.startswith(home.rstrip(os.sep) + os.sep):
        return ""
    return ("\nThis folder belongs to the administrator (root) account, probably because "
            "the installer was run with sudo before. Give it back to your account with:\n"
            "    sudo chown -R \"$(whoami)\" %s" % shlex.quote(p))


def permission_error(path, exc):
    return InstallError(
        "Permission denied while writing to:\n    %s\n(%s)\n"
        "Make sure you own this folder and that no game or editor is using it, "
        "then run the installer again. Do not run the installer with sudo or as "
        "administrator.%s\nIf you cannot write to your home folder (some school "
        "computers), set PGZERO_HOME to a folder you can write to and run the "
        "installer again." % (path, exc, _root_owned_hint(path)), EXIT_PERMISSION)


def friendly_os_error(path, exc):
    """Turn a disk-full, file-in-use or permission OSError into an InstallError."""
    if _is_disk_full_error(exc):
        return disk_full_error(path, details=str(exc))
    if _is_lock_error(exc):
        return file_in_use_error(path, details=str(exc))
    if _is_permission_error(exc):
        return permission_error(path, exc)
    return None


def read_marker(layout):
    try:
        with open(layout.marker, encoding="utf-8") as fh:
            data = json.load(fh)
        if isinstance(data, dict) and data.get("installer") == "pygamezero-installer":
            return data
    except (IOError, OSError, ValueError):
        pass
    return None


def write_marker(layout, data):
    payload = {"installer": "pygamezero-installer", "installer_version": INSTALLER_VERSION}
    payload.update(data)
    tmp = layout.marker + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        retry_if_locked(os.replace, tmp, layout.marker)
    except OSError as exc:
        raise friendly_os_error(layout.home, exc) or InstallError(
            "Could not save %s: %s" % (layout.marker, exc), EXIT_ENV)


# --------------------------------------------------------------------------
# Step 3: virtual environment
# --------------------------------------------------------------------------

def run(cmd, timeout=None, env=None):
    """Run a command, capturing output. Returns (returncode, combined_output).

    Timeouts return 124 and a missing program 127. Everything goes to the log.
    """
    LOG.write("$ " + " ".join(shlex.quote(str(c)) for c in cmd))
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env or clean_env())
        code, out = proc.returncode, proc.stdout.decode("utf-8", "replace")
    except subprocess.TimeoutExpired as exc:
        out = exc.output.decode("utf-8", "replace") if exc.output else ""
        code, out = 124, out + "\n(timed out after %s seconds)" % timeout
    except OSError as exc:
        code, out = 127, str(exc)
    LOG.write(out)
    LOG.write("(exit code %s)" % code)
    return code, out


# Files that may be left in a fresh install folder by an interrupted first run.
IGNORABLE_FILES = (LOG_FILE, MARKER_FILE + ".tmp")


def prepare_home(layout):
    """Create the install folder, refusing to take over a folder we don't own."""
    try:
        if os.path.isdir(layout.home) and read_marker(layout) is None and \
                [n for n in os.listdir(layout.home) if n not in IGNORABLE_FILES]:
            raise InstallError(
                "The folder\n    %s\nalready exists and was not created by this installer, "
                "so it will not be touched. Move it away or set PGZERO_HOME to a "
                "different folder. (If it is left over from an earlier Pygame Zero "
                "installation, you can delete it and run the installer again.)"
                % layout.home, EXIT_ENV)
        if not os.path.isdir(layout.home):
            os.makedirs(layout.home)
        if read_marker(layout) is None:
            write_marker(layout, {})
    except InstallError:
        raise
    except OSError as exc:
        raise friendly_os_error(layout.home, exc) or InstallError(
            "Could not create %s: %s\nIf this folder is on a drive that is not available, "
            "set PGZERO_HOME to a folder you can write to and run the installer again."
            % (layout.home, exc), EXIT_ENV)


def free_space_mb(path):
    """Free space (MB) on the drive that holds `path`, or None if unknown."""
    p = os.path.abspath(path)
    while not os.path.isdir(p):
        parent = os.path.dirname(p)
        if parent == p:
            return None
        p = parent
    try:
        return shutil.disk_usage(p).free // (1024 * 1024)
    except OSError:
        return None


def check_disk_space(layout, needed_mb=REQUIRED_FREE_MB):
    free = free_space_mb(layout.home)
    LOG.write("free space near %s: %s MB (need %s MB)" % (layout.home, free, needed_mb))
    if free is not None and free < needed_mb:
        raise InstallError(
            "There is not enough free disk space. Pygame Zero needs about %d MB, but only "
            "%d MB is free on the drive that holds:\n    %s\n"
            "Delete some files you no longer need (and empty the Trash or Recycle Bin), "
            "then run the installer again." % (needed_mb, free, layout.home), EXIT_ENV)


def existing_venv_ok(layout, os_info):
    if not os.path.isfile(layout.venv_python):
        return None
    info = probe(layout.venv_python)
    if info is None or not info.get("in_venv"):
        return None
    v = tuple(info["version"][:2])
    if not (SUPPORTED_MIN <= v <= SUPPORTED_MAX):
        return None
    return info


def pip_works(layout):
    code, _ = run([layout.venv_python, "-m", "pip", "--version"], timeout=120)
    return code == 0


def packages_import(layout):
    code, _ = run([layout.venv_python, "-c", "import pygame, pgzero"], timeout=VERIFY_TIMEOUT,
                  env=clean_env({"PYGAME_HIDE_SUPPORT_PROMPT": "1"}))
    return code == 0


def check_existing_venv(layout, os_info):
    """Look at an environment left by an earlier run.

    Returns (venv_info_or_None, problem_or_None). A problem means the
    environment must be rebuilt: an earlier run was interrupted or failed, or
    pip or Pygame inside it is broken.
    """
    if not os.path.isdir(layout.venv):
        return None, None
    marker = read_marker(layout)
    if marker is None:
        return None, None
    info = existing_venv_ok(layout, os_info)
    if info is None:
        return None, "the existing environment no longer works"
    if not marker.get("complete"):
        return info, "the previous installation did not finish"
    if not pip_works(layout):
        return info, "pip in the existing environment is broken"
    if not packages_import(layout):
        return info, "Pygame in the existing environment is broken"
    return info, None


def remove_tree(path, layout):
    """Delete a folder, but only inside an install folder that is ours."""
    real_home = os.path.normcase(os.path.realpath(layout.home))
    real = os.path.normcase(os.path.realpath(path))
    try:
        inside = os.path.commonpath([real_home, real]) == real_home and real != real_home
    except ValueError:  # different drives on Windows
        inside = False
    if read_marker(layout) is None or not inside:
        raise InstallError("Refusing to delete %s: it is not managed by this installer." % path,
                           EXIT_ENV)

    def retry(func, p, exc):
        # Windows marks some files read-only; clear the flag and retry once.
        try:
            os.chmod(p, 0o700)
            func(p)
        except OSError:
            raise exc

    def remove():
        if not os.path.exists(path):
            return
        if sys.version_info >= (3, 12):
            shutil.rmtree(path, onexc=retry)
        else:
            shutil.rmtree(path, onerror=lambda func, p, info: retry(func, p, info[1]))

    retry_if_locked(remove)


def pip_missing_help(os_info, python_info):
    lines = ["The environment was created, but pip (the tool that downloads Python "
             "packages) is missing from it."]
    if os_info["system"] == "Linux":
        v = version_str(python_info["version"][:2])
        lines.append("Install virtual environment support, then run this installer again:")
        lines.extend("    " + c for c in linux_venv_commands(os_info, v))
    else:
        lines.append("Your Python installation looks incomplete. Reinstall Python %s from "
                     "https://www.python.org/downloads/ (keep the \"pip\" option ticked), "
                     "then run this installer again." % version_str(python_info["version"][:2]))
    return "\n".join(lines)


def create_venv(layout, python_info, os_info, recreate=False):
    """Create or reuse the venv. Returns (venv_info, action_description)."""
    current = existing_venv_ok(layout, os_info)
    if current is not None and not recreate:
        return current, "reusing existing environment"
    try:
        if os.path.exists(layout.venv):
            remove_tree(layout.venv, layout)
    except OSError as exc:
        raise friendly_os_error(layout.venv, exc) or InstallError(
            "Could not remove the old environment at %s: %s" % (layout.venv, exc), EXIT_ENV)
    code, out = run([python_info["path"], "-m", "venv", layout.venv], timeout=300)
    if code != 0:
        if code == 124:
            raise InstallError(
                "Creating the environment took more than 5 minutes and was stopped. Antivirus "
                "software scanning the new files can cause this. Wait a minute, then run the "
                "installer again.", EXIT_ENV, details=tail(out))
        if any(p in out for p in DISK_FULL_PATTERNS):
            raise disk_full_error(layout.venv, details=tail(out))
        if any(p in out for p in LOCK_PATTERNS):
            raise file_in_use_error(layout.venv, details=tail(out))
        if "Permission denied" in out or "Errno 13" in out:
            raise InstallError(
                "Permission denied while creating the environment in\n    %s" % layout.venv,
                EXIT_PERMISSION, details=tail(out))
        hint = ""
        if os_info["system"] == "Linux" and ("ensurepip" in out or "venv" in out):
            v = version_str(python_info["version"][:2])
            hint = "\nInstall virtual environment support, then run this installer again:\n" + \
                "\n".join("    " + c for c in linux_venv_commands(os_info, v))
        raise InstallError("Could not create the virtual environment." + hint, EXIT_ENV,
                           details=tail(out))
    errors = []
    info = probe(layout.venv_python, errors=errors)
    if info is None:
        why = errors[0] if errors else "it could not be run"
        hint = ""
        if "respond" in why:
            hint = (" Antivirus software scanning new files can cause this. Wait a minute, "
                    "then run the installer again.")
        raise InstallError("The new virtual environment at %s does not work: %s.%s"
                           % (layout.venv, why, hint), EXIT_ENV)
    if not pip_works(layout):
        # ensurepip can fail quietly (or be stripped by a Linux distribution).
        run([layout.venv_python, "-m", "ensurepip", "--upgrade", "--default-pip"], timeout=300)
        if not pip_works(layout):
            raise InstallError(pip_missing_help(os_info, python_info), EXIT_ENV)
    return info, "created with Python %s" % version_str(info["version"])


# --------------------------------------------------------------------------
# Steps 4-5: packages
# --------------------------------------------------------------------------

# Network problems, most specific first. Matched case-insensitively.
NETWORK_KINDS = (
    ("ssl", ("CERTIFICATE_VERIFY_FAILED", "SSLCertVerificationError",
             "certificate verify failed", "There was a problem confirming the ssl certificate",
             "self signed certificate", "self-signed certificate",
             "unable to get local issuer certificate")),
    ("proxy", ("ProxyError", "Cannot connect to proxy", "Proxy Authentication Required",
               "Tunnel connection failed")),
    ("ssl", ("SSLError", "SSL: ", "EOF occurred in violation of protocol")),
    ("timeout", ("Read timed out", "ConnectTimeoutError", "Connection timed out", "timed out",
                 "TimeoutError", "IncompleteRead", "[WinError 10060]")),
    ("offline", ("NewConnectionError", "Failed to establish a new connection",
                 "Temporary failure in name resolution", "Name or service not known",
                 "nodename nor servname", "getaddrinfo failed", "No address associated",
                 "Network is unreachable", "No route to host", "Could not fetch URL",
                 "Connection reset", "Connection refused", "Connection aborted",
                 "RemoteDisconnected", "ConnectionError", "[WinError 11001]",
                 "[WinError 10051]", "[WinError 10054]", "[WinError 10061]")),
)
# Worth one automatic retry: flaky Wi-Fi often works a few seconds later.
TRANSIENT_NETWORK_KINDS = ("timeout", "offline")
CORRUPT_DOWNLOAD_PATTERNS = ("THESE PACKAGES DO NOT MATCH THE HASHES", "BadZipFile",
                             "File is not a zip file", "is not a valid wheel",
                             "Bad CRC-32", "invalid distribution")
PERMISSION_PATTERNS = ("Permission denied", "[Errno 13]", "Access is denied", "EACCES",
                       "[WinError 5]")
LONG_PATH_PATTERNS = ("[WinError 206]", "The filename or extension is too long",
                      "Windows Long Path support")


def network_kind(output):
    """Return "ssl", "proxy", "timeout", "offline" or None for pip output."""
    low = output.lower()
    for kind, patterns in NETWORK_KINDS:
        if any(p.lower() in low for p in patterns):
            return kind
    return None


def network_help(kind, output=""):
    if kind == "ssl":
        return (
            "Could not make a secure connection to the Python package website (pypi.org): "
            "its security certificate was rejected. This usually happens on school or work "
            "networks that inspect internet traffic. Ask your teacher or IT team to allow "
            "pypi.org and files.pythonhosted.org, or try another network (for example at "
            "home or a phone hotspot).")
    if kind == "proxy":
        lines = ["Could not download packages through your network's proxy server."]
        if "407" in output or "Proxy Authentication Required" in output:
            lines.append("The proxy needs a username and password.")
        configured = [v for v in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
                                  "ALL_PROXY", "all_proxy") if os.environ.get(v)]
        if configured:
            lines.append("Your computer is set to use the proxy in %s; check that it is "
                         "correct, or unset it if you are not on that network any more."
                         % configured[0])
        lines.append("Ask your teacher or IT team for the right proxy settings, or try "
                     "another network.")
        return " ".join(lines)
    if kind == "timeout":
        return (
            "The connection to the Python package website (pypi.org) timed out or dropped "
            "out, even after trying again. Your internet connection may be slow or "
            "unstable. Run the installer again, ideally on a faster network.")
    return (
        "Could not download packages from the internet (pypi.org). Check that you are "
        "online: can you open https://pypi.org in a web browser? On a school network "
        "a proxy or firewall may block pypi.org; ask your teacher or IT team, or try "
        "another network.")


def classify_pip_failure(output):
    """Map pip output to (exit_code, explanation)."""
    if "No module named pip" in output:
        return EXIT_ENV, ("pip (the tool that downloads Python packages) is missing from the "
                          "environment. Run the installer again with --recreate to rebuild it.")
    if any(p in output for p in DISK_FULL_PATTERNS):
        return EXIT_ENV, (
            "Your disk is full, so the packages could not be installed. Pygame Zero needs "
            "about %d MB of free space. Delete some files you no longer need (and empty the "
            "Trash or Recycle Bin), then run the installer again." % REQUIRED_FREE_MB)
    if any(p in output for p in LOCK_PATTERNS):
        return EXIT_PERMISSION, (
            "Another program is using Pygame's files. Close any running games, Python "
            "windows and editors (VS Code, Thonny, Mu...), wait a few seconds, then run "
            "the installer again.")
    if any(p in output for p in LONG_PATH_PATTERNS):
        return EXIT_ENV, (
            "A file path was too long for Windows. Install into a shorter folder, for "
            "example:\n    $env:PGZERO_HOME = \"C:\\pgzero\"\nand then run the installer "
            "again in the same window.")
    if any(p in output for p in CORRUPT_DOWNLOAD_PATTERNS):
        return EXIT_NETWORK, (
            "The downloaded package files were damaged, even after downloading them again. "
            "This is usually caused by an unreliable internet connection or a network "
            "filter that changes downloads. Try again, ideally on another network.")
    kind = network_kind(output)
    if kind:
        return EXIT_NETWORK, network_help(kind, output)
    if any(p in output for p in PERMISSION_PATTERNS):
        return EXIT_PERMISSION, (
            "Permission denied while installing packages. Close any running games or "
            "editors that use Pygame and run the installer again.")
    if "No matching distribution" in output or "Could not find a version" in output:
        return EXIT_PACKAGE, (
            "No ready-made package was found for this Python and computer. "
            "This usually means the Python version or CPU type is not supported.")
    return EXIT_PACKAGE, "pip could not install the package."


def pip_install(layout, spec, upgrade_pip=False):
    """Install one requirement. Raises InstallError on failure.

    A transient network failure is retried once after a short pause, and a
    corrupted download is retried once without pip's cache.
    """
    base = [layout.venv_python, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--progress-bar", "off"]
    if upgrade_pip:
        target = ["--upgrade", "pip"]
    else:
        # Binary wheels only: never try to compile Pygame from source.
        target = ["--only-binary=:all:", spec]
    extra = []
    retried = False
    while True:
        code, out = run(base + extra + target, timeout=PIP_TIMEOUT)
        if code == 0:
            return out
        if code == 124:
            raise InstallError(
                "Downloading and installing %s took more than %d minutes, so it was "
                "stopped. Your internet connection may be very slow. Run the installer "
                "again, ideally on a faster network." % (spec or "pip", PIP_TIMEOUT // 60),
                EXIT_NETWORK, details=tail(out))
        if retried or "No module named pip" in out:
            break
        retried = True
        if any(p in out for p in CORRUPT_DOWNLOAD_PATTERNS):
            LOG.write("download looks corrupted; retrying without pip's cache")
            extra = ["--no-cache-dir"]
        elif network_kind(out) in TRANSIENT_NETWORK_KINDS:
            LOG.write("network problem; retrying in %s seconds" % NETWORK_RETRY_DELAY)
            time.sleep(NETWORK_RETRY_DELAY)
        else:
            break
    exit_code, why = classify_pip_failure(out)
    # pip reports an unreachable index as "no versions found" when it does
    # not retry, which would wrongly blame the Python version.
    if exit_code == EXIT_PACKAGE and "No matching distribution" in out:
        kind = index_problem()
        if kind:
            exit_code, why = EXIT_NETWORK, network_help(kind, out)
    raise InstallError(why, exit_code, details=tail(out))


def index_problem(timeout=15):
    """Try the package index (PyPI, or PIP_INDEX_URL if set) directly.

    Returns None if it answers, else a network kind for network_help().
    """
    try:
        from urllib.request import build_opener, getproxies, proxy_bypass
        from urllib.parse import urlparse
        base = os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple"
        url = base.rstrip("/") + "/pygame/"
        # A fresh opener, so current proxy settings are used.
        with build_opener().open(url, timeout=timeout) as resp:
            if resp.status < 400:
                return None
            return "offline"
    except Exception as exc:
        LOG.write("package index check failed: %r" % exc)
        kind = network_kind(repr(exc)) or "offline"
        try:
            host = urlparse(url).hostname or ""
            via_proxy = bool(getproxies().get(urlparse(url).scheme)) and not proxy_bypass(host)
        except Exception:
            via_proxy = False
        if via_proxy and kind == "offline":
            return "proxy"
        return kind


def installed_versions(layout):
    code, out = run([layout.venv_python, "-c",
                     "import json, importlib.metadata as m\n"
                     "r = {}\n"
                     "for n in ('pygame', 'pgzero', 'numpy'):\n"
                     "    try:\n"
                     "        r[n] = m.version(n)\n"
                     "    except Exception:\n"
                     "        r[n] = None\n"
                     "print(json.dumps(r))"], timeout=120)
    if code != 0:
        return {}
    try:
        return json.loads(out.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return {}


# --------------------------------------------------------------------------
# Step 6: the `pgzrun` command on PATH
# --------------------------------------------------------------------------

def unix_shim_text(layout):
    return ("#!/bin/sh\n"
            "# %s\n"
            "# Runs Pygame Zero from %s\n"
            "# Re-run the installer to repair, or run it with --uninstall to remove.\n"
            "exec %s -m pgzero \"$@\"\n") % (SHIM_MARKER, layout.venv,
                                            shlex.quote(layout.venv_python))


def shim_is_ours(path):
    if os.path.islink(path):
        return False
    try:
        with open(path, "rb") as fh:
            head = fh.read(4096)
    except (IOError, OSError):
        return False
    return SHIM_MARKER.encode() in head


def install_shim(layout):
    """Create the pgzrun launcher. Returns (status, message).

    status is "ok", "unchanged" or "blocked" (a foreign pgzrun exists).
    """
    try:
        if not os.path.isdir(layout.bin_dir):
            os.makedirs(layout.bin_dir)
        if layout.windows:
            return _install_windows_shim(layout)
        text = unix_shim_text(layout)
        if os.path.lexists(layout.shim):
            if not shim_is_ours(layout.shim):
                return "blocked", (
                    "%s already exists and was not created by this installer, so it was "
                    "left untouched." % layout.shim)
            with open(layout.shim) as fh:
                if fh.read() == text:
                    return "unchanged", layout.shim
        tmp = layout.shim + ".tmp"
        with open(tmp, "w") as fh:
            fh.write(text)
        os.chmod(tmp, 0o755)
        os.replace(tmp, layout.shim)
        return "ok", layout.shim
    except OSError as exc:
        raise friendly_os_error(layout.bin_dir, exc) or InstallError(
            "Could not create %s: %s" % (layout.shim, exc), EXIT_ENV)


def _install_windows_shim(layout):
    # The pip-generated pgzrun.exe embeds the absolute path of the venv's
    # python.exe, so a copy works from any folder. Our bin folder is private
    # to this installer, but we still record what we put there.
    manifest = os.path.join(layout.bin_dir, MARKER_FILE)
    if os.path.exists(layout.shim) and not os.path.exists(manifest):
        return "blocked", ("%s already exists and was not created by this installer, so it "
                           "was left untouched." % layout.shim)
    if os.path.exists(layout.shim):
        with open(layout.shim, "rb") as a, open(layout.venv_pgzrun, "rb") as b:
            if a.read() == b.read():
                return "unchanged", layout.shim
    retry_if_locked(shutil.copyfile, layout.venv_pgzrun, layout.shim)
    with open(manifest, "w", encoding="utf-8") as fh:
        json.dump({"installer": "pygamezero-installer", "files": ["pgzrun.exe"]}, fh)
    return "ok", layout.shim


def path_contains(path_value, directory):
    target = os.path.normcase(os.path.normpath(directory))
    for entry in path_value.split(os.pathsep):
        entry = entry.strip().strip('"')
        if entry and os.path.normcase(os.path.normpath(os.path.expandvars(
                os.path.expanduser(entry)))) == target:
            return True
    return False


def shell_rc_files(env=None, system=None):
    """Startup files to update for the user's login shell."""
    env = os.environ if env is None else env
    system = system or platform.system()
    home = env.get("HOME") or os.path.expanduser("~")
    shell = os.path.basename(env.get("SHELL", "")) or ("zsh" if system == "Darwin" else "bash")
    if shell == "zsh":
        return [os.path.join(env.get("ZDOTDIR") or home, ".zshrc")]
    if shell == "bash":
        files = [os.path.join(home, ".bashrc")]
        if system == "Darwin":
            files.append(os.path.join(home, ".bash_profile"))
        return files
    if shell == "fish":
        return [os.path.join(home, ".config", "fish", "conf.d", "pygamezero.fish")]
    return [os.path.join(home, ".profile")]


def rc_block(bin_dir, rc_path, home):
    if rc_path.endswith(".fish"):
        q = shlex.quote(bin_dir)
        body = "if not contains -- %s $PATH\n    set -gx PATH %s $PATH\nend" % (q, q)
    else:
        prefix, rest = "", bin_dir
        if home and bin_dir.startswith(home.rstrip("/") + "/"):
            prefix, rest = "$HOME", bin_dir[len(home.rstrip("/")):]
        # Inside double quotes only $HOME may expand; escape everything special.
        shown = prefix + re.sub(r'(["`$\\])', r"\\\1", rest)
        body = ('case ":$PATH:" in\n'
                '    *":%s:"*) ;;\n'
                '    *) export PATH="%s:$PATH" ;;\n'
                'esac') % (shown, shown)
    return "%s\n%s\n%s\n" % (RC_BEGIN, body, RC_END)


def strip_rc_block(text):
    pattern = re.compile(r"\n?" + re.escape(RC_BEGIN) + r".*?" + re.escape(RC_END) + r"\n?",
                         re.S)
    result = pattern.sub("\n", text)
    return result.rstrip("\n") + "\n" if result.strip() else ""


def update_rc_file(rc_path, block):
    """Add or refresh our PATH block. Returns True if the file changed.

    Raises UnicodeDecodeError if the file is not UTF-8 and OSError if it may
    not be changed (read-only, or a broken symbolic link).
    """
    # Write through symbolic links (dotfile managers) instead of replacing them.
    real = os.path.realpath(rc_path)
    if os.path.islink(rc_path) and not os.path.exists(real):
        raise OSError(errno.ENOENT, "it is a symbolic link to a missing file", rc_path)
    try:
        with open(real, "rb") as fh:
            original = fh.read().decode("utf-8")
        exists = True
    except FileNotFoundError:
        original, exists = "", False
    if block in original:
        return False
    if exists and not os.access(real, os.W_OK):
        raise OSError(errno.EACCES, "the file is read-only", rc_path)
    base = strip_rc_block(original) if RC_BEGIN in original else original
    if base and not base.endswith("\n"):
        base += "\n"
    new = base + ("\n" if base else "") + block
    write_rc_text(rc_path, new)
    return True


def write_rc_text(rc_path, text):
    """Replace a startup file's contents without ever leaving it half-written."""
    real = os.path.realpath(rc_path)
    parent = os.path.dirname(real)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    tmp = real + ".pygamezero-tmp"
    try:
        with open(tmp, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        if os.path.exists(real):
            shutil.copymode(real, tmp)
        os.replace(tmp, real)
    finally:
        if os.path.exists(tmp):
            os.remove(tmp)


def ensure_unix_path(layout, env=None, system=None):
    """Make sure new terminals have bin_dir on PATH.

    Returns (changed_files, [(skipped_file, reason), ...]).
    """
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    changed, skipped = [], []
    for rc in shell_rc_files(env, system):
        try:
            if update_rc_file(rc, rc_block(layout.bin_dir, rc, home)):
                changed.append(rc)
        except UnicodeDecodeError:
            skipped.append((rc, "it is not a UTF-8 text file"))
        except OSError as exc:
            skipped.append((rc, exc.strerror or str(exc)))
    return changed, skipped


def ensure_windows_path(bin_dir, winreg_module=None, warnings=None):
    """Append bin_dir to the user's PATH in the registry. Returns True if changed.

    Never shortens PATH; adds a note to `warnings` if it gets very long.
    """
    if winreg_module is None:
        import winreg as winreg_module
    wr = winreg_module
    with wr.OpenKey(wr.HKEY_CURRENT_USER, "Environment", 0,
                    wr.KEY_READ | wr.KEY_WRITE) as key:
        try:
            value, kind = wr.QueryValueEx(key, "Path")
        except OSError:
            value, kind = "", wr.REG_EXPAND_SZ
        if kind not in (wr.REG_SZ, wr.REG_EXPAND_SZ):
            raise InstallError("Your user PATH has an unexpected format; not changing it.",
                               EXIT_ENV)
        if path_contains(value, bin_dir):
            return False
        new = (value.rstrip(";") + ";" + bin_dir) if value else bin_dir
        wr.SetValueEx(key, "Path", 0, kind, new)
    if len(new) > WINDOWS_PATH_WARN_LENGTH and warnings is not None:
        warnings.append(
            "your user PATH is now %d characters long. Some older Windows tools only "
            "read the first %d characters; if programs stop being found, remove PATH "
            "entries you no longer need (search Windows for \"Edit environment variables "
            "for your account\")." % (len(new), WINDOWS_PATH_WARN_LENGTH))
    _broadcast_environment_change()
    return True


def remove_windows_path(bin_dir, winreg_module=None):
    if winreg_module is None:
        import winreg as winreg_module
    wr = winreg_module
    with wr.OpenKey(wr.HKEY_CURRENT_USER, "Environment", 0,
                    wr.KEY_READ | wr.KEY_WRITE) as key:
        try:
            value, kind = wr.QueryValueEx(key, "Path")
        except OSError:
            return False
        target = os.path.normcase(os.path.normpath(bin_dir))
        parts = [p for p in value.split(";") if p]
        keep = [p for p in parts
                if os.path.normcase(os.path.normpath(os.path.expandvars(p))) != target]
        if len(keep) == len(parts):
            return False
        wr.SetValueEx(key, "Path", 0, kind, ";".join(keep))
    _broadcast_environment_change()
    return True


def _broadcast_environment_change():
    """Tell Explorer that environment variables changed, so new terminals see it."""
    try:
        import ctypes
        from ctypes import wintypes

        result = wintypes.DWORD()
        ctypes.windll.user32.SendMessageTimeoutW(
            0xFFFF, 0x001A, 0, "Environment", 0x0002, 5000, ctypes.byref(result))
    except Exception:
        pass


# --------------------------------------------------------------------------
# Step 7: verification
# --------------------------------------------------------------------------

# System libraries Pygame's Linux wheels expect the OS to provide, mapped to
# the package that contains them. Desktop installs have these; minimal
# server/container images may not.
LINUX_LIBRARY_PACKAGES = {
    "libgthread-2.0": {"debian": "libglib2.0-0", "fedora": "glib2", "arch": "glib2",
                       "suse": "libgthread-2_0-0"},
    "libglib-2.0": {"debian": "libglib2.0-0", "fedora": "glib2", "arch": "glib2",
                    "suse": "libglib-2_0-0"},
}


def missing_library_help(output, os_info=None):
    """Explain a missing shared library (Linux), or return None."""
    m = re.search(r"([\w.+-]+\.so(?:\.\d+)*): cannot open shared object file", output or "")
    if not m:
        return None
    lib = m.group(1)
    os_info = os_info or detect_os()
    family = _linux_family(os_info)
    package = LINUX_LIBRARY_PACKAGES.get(lib.split(".so")[0], {}).get(family)
    command = {"debian": "sudo apt install %s", "fedora": "sudo dnf install %s",
               "arch": "sudo pacman -S %s", "suse": "sudo zypper install %s"}.get(family)
    lines = ["Pygame needs the system library %s, which is not installed." % lib]
    if package and command:
        lines += ["Install it with:", "    " + command % package]
    else:
        lines.append("Install the package that provides %s with your package manager." % lib)
    lines.append("Then run this installer again.")
    return "\n".join(lines)


def verify_timeout_error(what, out):
    return InstallError(
        "%s did not finish within %d seconds, so it was stopped. Everything was "
        "installed, but this check could not complete. This can happen the first time on "
        "a slow or busy computer, or while antivirus software scans the new files. Wait "
        "a minute, then run the installer again." % (what, VERIFY_TIMEOUT), EXIT_VERIFY,
        details=tail(out))


def verify(layout, launcher):
    """Prove imports work and a real Pygame Zero game runs. Raises on failure."""
    code, out = run([layout.venv_python, "-c",
                     "import pygame, pgzero, pgzero.runner; "
                     "print(pygame.version.ver, pgzero.__version__)"],
                    timeout=VERIFY_TIMEOUT)
    if code == 124:
        raise verify_timeout_error("Loading Pygame and Pygame Zero", out)
    if code != 0:
        raise InstallError(missing_library_help(out) or
                           "Pygame or Pygame Zero cannot be imported.", EXIT_VERIFY,
                           details=tail(out))
    if not os.path.isfile(layout.venv_pgzrun):
        raise InstallError("The pgzrun program is missing from %s." % layout.venv_pgzrun,
                           EXIT_VERIFY)
    try:
        workdir = tempfile.mkdtemp(prefix="pgzero-check-")
    except OSError:  # temp folder full or not writable
        workdir = tempfile.mkdtemp(prefix="pgzero-check-", dir=layout.home)
    try:
        game = os.path.join(workdir, "smoke_game.py")
        with open(game, "w", encoding="utf-8") as fh:
            fh.write(SMOKE_GAME)
        env = clean_env({"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
                         "PYGAME_HIDE_SUPPORT_PROMPT": "1"})
        code, out = run([launcher, game], timeout=VERIFY_TIMEOUT, env=env)
        if code == 124:
            raise verify_timeout_error("The test game", out)
        if code != 0 or SMOKE_TOKEN not in out:
            raise InstallError(missing_library_help(out) or
                               "A test game could not be run with pgzrun.", EXIT_VERIFY,
                               details=tail(out))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def _windows_is_admin():
    try:
        import ctypes
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def check_privileges(os_info, allow_root=False, env=None):
    """Refuse to run as root on macOS/Linux. Returns [(kind, text)] notes to show."""
    env = os.environ if env is None else env
    notes = []
    if os_info["system"] == "Windows":
        if _windows_is_admin():
            notes.append(("warn", (
                "you are running the installer as Administrator. That is not needed: "
                "Pygame Zero is installed only for the account you are logged in as (%s). "
                "If that is not your normal account, run the installer again from a "
                "normal terminal." % (env.get("USERNAME") or "this user"))))
    elif hasattr(os, "geteuid") and os.geteuid() == 0:
        if not allow_root:
            how = ""
            if env.get("SUDO_USER"):
                how = " with sudo (you are logged in as %s)" % env["SUDO_USER"]
            raise InstallError(
                "The installer was started as the administrator (root) account%s.\n"
                "That would install Pygame Zero for root instead of for you: the files would "
                "go into root's home folder and belong to root, so `pgzrun` would not work "
                "from your normal account.\n"
                "Run it again WITHOUT sudo, as your normal user. It never needs "
                "administrator rights.\n"
                "(To install for the root account on purpose, for example in a container, "
                "add --allow-root.)" % how, EXIT_PERMISSION)
        notes.append(("warn", "installing for the root account because --allow-root was "
                              "given."))
    active = env.get("VIRTUAL_ENV") or env.get("CONDA_PREFIX")
    if active:
        what = "conda environment" if env.get("CONDA_PREFIX") and not env.get(
            "VIRTUAL_ENV") else "virtual environment"
        notes.append(("note", "a %s is active (%s). That is fine: the installer ignores it "
                              "and creates its own separate environment." % (what, active)))
    return notes


def log_environment(os_info, layout):
    """Facts that help someone diagnose a failure. No environment dump, no secrets."""
    proxies = sorted(v for v in os.environ if v.lower() in (
        "http_proxy", "https_proxy", "all_proxy", "no_proxy"))
    pip_vars = sorted(v for v in os.environ if v.startswith("PIP_"))
    lines = [
        "system: %s, %s (%s)" % (os_info["name"], os_info["arch"], platform.platform()),
        "running with: Python %s at %s" % (platform.python_version(), sys.executable),
        "install folder: %s" % layout.home,
        "environment: %s" % layout.venv,
        "pgzrun folder: %s (on PATH: %s)" % (
            layout.bin_dir, path_contains(os.environ.get("PATH", ""), layout.bin_dir)),
        "free space: %s MB" % free_space_mb(layout.home),
        "shell: %s" % (os.environ.get("SHELL") or os.environ.get("ComSpec") or "?"),
        "active venv/conda: %s" % bool(os.environ.get("VIRTUAL_ENV")
                                       or os.environ.get("CONDA_PREFIX")),
        # Names only: values can contain passwords.
        "proxy variables set: %s" % (", ".join(proxies) or "none"),
        "pip variables set: %s" % (", ".join(pip_vars) or "none"),
    ]
    if hasattr(os, "geteuid"):
        lines.append("root: %s, sudo: %s" % (os.geteuid() == 0, bool(os.environ.get("SUDO_USER"))))
    LOG.write("\n".join(lines))


def install(args, console):
    console.write(console.bold("Pygame Zero Installer") + " (v%s)" % INSTALLER_VERSION)
    console.write("")

    console.step(1, "Detecting operating system")
    os_info = detect_os()
    check_platform_supported(os_info)
    console.ok("%s, %s" % (os_info["name"], os_info["arch"]))
    notes = check_privileges(os_info, args.allow_root)
    layout = Layout(os_info)
    if read_marker(layout) is not None:
        LOG.open(layout.home)
    log_environment(os_info, layout)
    for kind, text in notes:
        if kind == "warn":
            console.warn(text)
        else:
            console.write("Note: " + text)

    console.step(2, "Checking Python")
    reuse, problem = (None, None) if args.recreate else check_existing_venv(layout, os_info)
    if problem:
        LOG.write("existing environment needs repair: %s" % problem)
    if reuse is not None and problem is None and not args.python:
        chosen = reuse
        console.ok("Python %s (existing environment)" % version_str(chosen["version"]))
    else:
        chosen, rejected = select_python(os_info, explicit=args.python)
        if chosen is None:
            console.fail("no compatible Python found")
            for info, reason in rejected:
                console.write("    skipped %s: %s" % (info.get("path"), reason))
            if not args.python and offer_python_install(os_info, console, args.yes):
                refresh_windows_path()
                chosen, rejected = select_python(os_info)
            if chosen is None:
                raise InstallError(python_install_help(os_info, rejected), EXIT_NO_PYTHON)
            console.step(2, "Checking Python")
        console.ok("Python %s (%s)" % (version_str(chosen["version"]), chosen["path"]))
    LOG.write("chosen Python: %s %s" % (version_str(chosen["version"]), chosen["path"]))

    console.step(3, "Preparing environment")
    rebuild = args.recreate or bool(problem)
    try:
        check_disk_space(layout, REQUIRED_FREE_MB_REUSE if reuse is not None and not rebuild
                         else REQUIRED_FREE_MB)
        prepare_home(layout)
        if LOG.path is None:
            LOG.open(layout.home)
        venv_info, action = create_venv(layout, chosen, os_info, recreate=rebuild)
        if problem:
            action = "repaired: %s" % problem
        # Written without "complete": an interrupted run is repaired next time.
        write_marker(layout, {"python": version_str(venv_info["version"]),
                              "venv": layout.venv, "bin_dir": layout.bin_dir})
        # Keeping pip current avoids old pips that cannot read modern wheel tags.
        pip_warning = None
        try:
            pip_install(layout, None, upgrade_pip=True)
        except InstallError as exc:
            if exc.code != EXIT_PACKAGE:
                raise
            pip_warning = "could not upgrade pip; continuing with the existing version."
    except InstallError:
        console.fail()
        raise
    console.ok("OK (%s)" % action)
    if pip_warning:
        console.warn(pip_warning)

    done = []
    for n, label, spec in ((4, "Installing Pygame", PYGAME_SPEC),
                           (5, "Installing Pygame Zero", PGZERO_SPEC)):
        console.step(n, label)
        try:
            pip_install(layout, spec)
        except InstallError as exc:
            console.fail()
            if done:
                exc.message += "\n\nPartial installation: %s installed, %s did not." % (
                    ", ".join(done), spec)
            raise
        done.append(spec)
        console.ok("OK (%s)" % spec.replace("==", " "))

    console.step(6, "Adding the pgzrun command")
    try:
        status, msg = install_shim(layout)
    except InstallError:
        console.fail()
        raise
    path_notes = []
    warnings = []
    if status == "blocked":
        console.fail("SKIPPED")
        console.warn(msg)
        launcher = layout.venv_pgzrun
    else:
        launcher = layout.shim
        on_path_now = path_contains(os.environ.get("PATH", ""), layout.bin_dir)
        if args.no_modify_path:
            if not on_path_now:
                path_notes.append("manual")
        elif layout.windows:
            try:
                if ensure_windows_path(layout.bin_dir, warnings=warnings):
                    path_notes.append("windows")
            except (OSError, ImportError) as exc:
                warnings.append("could not update your PATH automatically (%s)." % exc)
                path_notes.append("manual")
        else:
            changed, skipped = ensure_unix_path(layout)
            for rc, reason in skipped:
                warnings.append("did not change %s: %s." % (rc, reason))
            if changed:
                path_notes.append("rc:" + ", ".join(changed))
            elif skipped:
                path_notes.append("manual")
            elif not on_path_now:
                path_notes.append("rc-existing")
        console.ok("OK (%s)" % layout.shim)
    for text in warnings:
        console.warn(text)

    console.step(7, "Verifying installation")
    try:
        verify(layout, launcher)
    except InstallError:
        console.fail()
        raise
    versions = installed_versions(layout)
    write_marker(layout, {"python": version_str(venv_info["version"]), "venv": layout.venv,
                          "bin_dir": layout.bin_dir, "packages": versions,
                          "complete": True})
    console.ok("OK (pygame %s, pgzero %s)" % (versions.get("pygame"), versions.get("pgzero")))
    LOG.write("installation complete")

    print_success(console, layout, status, path_notes)
    return EXIT_OK


def print_success(console, layout, shim_status, path_notes):
    console.write("")
    console.write(console.bold("Installation complete!"))
    console.write("")
    if shim_status == "blocked":
        console.write("Another program called pgzrun is already installed, so use the full path:")
        console.write("    %s game.py" % _display_cmd(layout.venv_pgzrun))
    else:
        current = shutil.which("pgzrun")
        if current and os.path.normcase(os.path.realpath(current)) != \
                os.path.normcase(os.path.realpath(layout.shim)):
            console.warn("a different pgzrun (%s) comes first on your PATH. If `pgzrun` "
                         "misbehaves, use the full path: %s" % (current, layout.shim))
        if any(n.startswith("rc:") or n in ("windows", "rc-existing") for n in path_notes):
            console.write("Open a NEW terminal window first, so it can find pgzrun.")
        if "manual" in path_notes:
            console.write("Add this folder to your PATH so the pgzrun command works:")
            console.write("    %s" % layout.bin_dir)
            if not layout.windows:
                console.write("for example by adding this line to your shell startup file "
                              "(such as ~/.zshrc or ~/.bashrc):")
                console.write('    export PATH="%s:$PATH"' % layout.bin_dir.replace('"', '\\"'))
            console.write("or use the full path instead of pgzrun:")
            console.write("    %s game.py" % _display_cmd(layout.shim))
        console.write("")
        console.write("Run your first game:")
        console.write("    pgzrun game.py")
    console.write("")
    console.write("Python for your editor (VS Code, Thonny, Mu...):")
    console.write("    %s" % layout.venv_python)


def _display_cmd(path):
    if " " in path:
        return '& "%s"' % path if os.name == "nt" else shlex.quote(path)
    return path


def uninstall(args, console):
    console.write(console.bold("Pygame Zero Installer: uninstall"))
    os_info = detect_os()
    layout = Layout(os_info)
    removed = []
    try:
        if layout.windows:
            manifest = os.path.join(layout.bin_dir, MARKER_FILE)
            if os.path.exists(manifest):
                for name in ("pgzrun.exe", MARKER_FILE):
                    p = os.path.join(layout.bin_dir, name)
                    if os.path.exists(p):
                        retry_if_locked(os.remove, p)
                        removed.append(p)
                try:
                    if remove_windows_path(layout.bin_dir):
                        removed.append("PATH entry %s" % layout.bin_dir)
                except (OSError, ImportError):
                    pass
                try:
                    os.rmdir(layout.bin_dir)
                except OSError:
                    pass
        else:
            if os.path.lexists(layout.shim) and shim_is_ours(layout.shim):
                os.remove(layout.shim)
                removed.append(layout.shim)
            elif os.path.lexists(layout.shim):
                console.warn("left %s alone: it was not created by this installer." % layout.shim)
            for rc in shell_rc_files():
                try:
                    with open(rc, encoding="utf-8") as fh:
                        text = fh.read()
                except ValueError:
                    console.warn("could not read %s (it is not a UTF-8 text file); remove the "
                                 "lines between '%s' and '%s' yourself if they are there."
                                 % (rc, RC_BEGIN, RC_END))
                    continue
                except (IOError, OSError):
                    continue
                if RC_BEGIN in text:
                    try:
                        if rc.endswith("pygamezero.fish"):
                            os.remove(rc)
                        else:
                            write_rc_text(rc, strip_rc_block(text))
                        removed.append("PATH lines in %s" % rc)
                    except OSError as exc:
                        console.warn("could not change %s (%s); remove the lines between "
                                     "'%s' and '%s' yourself." % (rc, exc, RC_BEGIN, RC_END))
        if read_marker(layout) is not None:
            retry_if_locked(shutil.rmtree, layout.home)
            removed.append(layout.home)
        elif os.path.exists(layout.home):
            console.warn("left %s alone: it was not created by this installer." % layout.home)
    except OSError as exc:
        raise friendly_os_error(getattr(exc, "filename", None) or layout.home, exc) or \
            InstallError("Could not remove %s: %s" % (layout.home, exc), EXIT_ENV)
    if removed:
        console.write("Removed:")
        for item in removed:
            console.write("    " + item)
    else:
        console.write("Nothing to remove: Pygame Zero was not installed by this installer.")
    console.write("Your other Python installations were not changed.")
    return EXIT_OK


def parse_args(argv):
    p = argparse.ArgumentParser(
        prog="install.py",
        description="Install Pygame and Pygame Zero into an isolated environment.")
    p.add_argument("--python", metavar="PATH",
                   help="use this Python interpreter instead of searching for one")
    p.add_argument("--recreate", action="store_true",
                   help="delete and rebuild the environment from scratch")
    p.add_argument("--no-modify-path", action="store_true",
                   help="do not change PATH; print instructions instead")
    p.add_argument("--yes", action="store_true",
                   help="accept the offer to install Python with winget/Homebrew")
    p.add_argument("--uninstall", action="store_true",
                   help="remove everything this installer created")
    p.add_argument("--allow-root", action="store_true",
                   help="allow installing for the root account (macOS/Linux)")
    p.add_argument("--version", action="version", version=INSTALLER_VERSION)
    return p.parse_args(argv)


def report_failure(console, args, exc):
    LOG.write("FAILED (exit code %s): %s" % (exc.code, exc.message))
    if exc.details:
        LOG.write(exc.details)
    console.write("")
    console.write(console._paint("Installation failed.", "31")
                  if not args.uninstall else "Uninstall failed.")
    console.write(exc.message)
    if exc.details:
        console.write("")
        console.write("Details:")
        for line in exc.details.splitlines():
            console.write("    " + line)
    console.write("")
    console.write("Your other Python installations were not changed. "
                  "You can safely run the installer again.")
    print_log_path(console)
    return exc.code


def print_log_path(console):
    path = LOG.ensure_open()
    if path:
        console.write("A full log was saved to:")
        console.write("    %s" % path)
        console.write("If you ask your teacher for help, send them this file.")


def main(argv=None):
    global LOG
    argv = sys.argv[1:] if argv is None else argv
    args = parse_args(argv)
    console = Console()
    LOG = InstallLog()
    LOG.write("==== Pygame Zero Installer v%s, %s, arguments: %s ====" % (
        INSTALLER_VERSION, time.strftime("%Y-%m-%d %H:%M:%S"), " ".join(argv) or "(none)"))
    try:
        if args.uninstall:
            return uninstall(args, console)
        return install(args, console)
    except InstallError as exc:
        return report_failure(console, args, exc)
    except KeyboardInterrupt:
        LOG.write("cancelled with Ctrl+C")
        console.write("")
        console.write("Cancelled. Nothing is broken: run the installer again at any time "
                      "and it will finish (or repair) the installation.")
        return 130
    except Exception as exc:
        LOG.write(traceback.format_exc())
        if isinstance(exc, OSError):
            friendly = friendly_os_error(getattr(exc, "filename", None) or "(unknown)", exc)
            if friendly is not None:
                return report_failure(console, args, friendly)
        console.write("")
        console.write(console._paint("Something unexpected went wrong.", "31"))
        console.write("This is a problem in the installer, not something you did.")
        console.write("    %s: %s" % (type(exc).__name__, exc))
        console.write("You can safely run the installer again.")
        print_log_path(console)
        console.write("Please also report it at %s" % ISSUES_URL)
        return EXIT_ERROR


if __name__ == "__main__":
    sys.exit(main())
