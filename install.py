#!/usr/bin/env python3
"""Pygame Zero Installer.

Installs Pygame and Pygame Zero into an isolated virtual environment and puts
a `pgzrun` command on the user's PATH. Works on Windows, macOS and Linux.

This file is the shared installer used by install.sh and install.ps1, but it
can also be run directly:

    python3 install.py              # install (or repair) everything
    python3 install.py --uninstall  # remove everything this installer created

It only uses the Python standard library and must stay runnable on Python 3.6+
because the bootstrappers launch it with whatever Python 3 they can find. The
Python used for the game environment is chosen separately (see SUPPORTED_*).
"""

import argparse
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

INSTALLER_VERSION = "1.0.0"

# Pinned, tested package versions. pygame 2.6.1 publishes binary wheels for
# CPython 3.6-3.13 only; on 3.14+ pip falls back to compiling pygame from
# source, which fails on student machines. pgzero 1.2.1 requires pygame~=2.0.
PYGAME_SPEC = "pygame==2.6.1"
PGZERO_SPEC = "pgzero==1.2.1"
SUPPORTED_MIN = (3, 9)
SUPPORTED_MAX = (3, 13)
RECOMMENDED = (3, 13)

MARKER_FILE = "pygamezero-installer.json"
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
                cands = [c for c in cands if os.path.realpath(c) != "/usr/bin/python3"]
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


def probe(executable, runner=subprocess.run):
    """Ask an interpreter about itself. Returns a dict, or None if unusable."""
    try:
        proc = runner([executable, "-I", "-c", PROBE_CODE], stdout=subprocess.PIPE,
                      stderr=subprocess.PIPE, timeout=30, env=clean_env())
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
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
        info = probe(path, runner=runner)
        if info is None:
            if explicit:
                rejected.append(({"path": path, "version": [0, 0, 0]}, "it could not be run"))
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


def permission_error(path, exc):
    return InstallError(
        "Permission denied while writing to:\n    %s\n(%s)\n"
        "Make sure you own this folder and that no game or editor is using it, "
        "then run the installer again. Do not run the installer with sudo or as "
        "administrator." % (path, exc), EXIT_PERMISSION)


def read_marker(layout):
    try:
        with open(layout.marker, encoding="utf-8") as fh:
            data = json.load(fh)
        if data.get("installer") == "pygamezero-installer":
            return data
    except (IOError, OSError, ValueError):
        pass
    return None


def write_marker(layout, data):
    payload = {"installer": "pygamezero-installer", "installer_version": INSTALLER_VERSION}
    payload.update(data)
    tmp = layout.marker + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, layout.marker)


# --------------------------------------------------------------------------
# Step 3: virtual environment
# --------------------------------------------------------------------------

def run(cmd, timeout=None, env=None):
    """Run a command, capturing output. Returns (returncode, combined_output)."""
    try:
        proc = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                              timeout=timeout, env=env or clean_env())
    except subprocess.TimeoutExpired as exc:
        out = exc.output.decode("utf-8", "replace") if exc.output else ""
        return 124, out + "\n(timed out after %s seconds)" % timeout
    except OSError as exc:
        return 127, str(exc)
    return proc.returncode, proc.stdout.decode("utf-8", "replace")


def prepare_home(layout):
    """Create the install folder, refusing to take over a folder we don't own."""
    try:
        if os.path.isdir(layout.home) and os.listdir(layout.home) and read_marker(layout) is None:
            raise InstallError(
                "The folder\n    %s\nalready exists and was not created by this installer, "
                "so it will not be touched. Move it away or set PGZERO_HOME to a "
                "different folder." % layout.home, EXIT_ENV)
        if not os.path.isdir(layout.home):
            os.makedirs(layout.home)
        if read_marker(layout) is None:
            write_marker(layout, {})
    except InstallError:
        raise
    except OSError as exc:
        if _is_permission_error(exc):
            raise permission_error(layout.home, exc)
        raise InstallError("Could not create %s: %s" % (layout.home, exc), EXIT_ENV)


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

    if sys.version_info >= (3, 12):
        shutil.rmtree(path, onexc=retry)
    else:
        shutil.rmtree(path, onerror=lambda func, p, info: retry(func, p, info[1]))


def create_venv(layout, python_info, os_info, recreate=False):
    """Create or reuse the venv. Returns (venv_info, action_description)."""
    current = existing_venv_ok(layout, os_info)
    if current is not None and not recreate:
        return current, "reusing existing environment"
    try:
        if os.path.exists(layout.venv):
            remove_tree(layout.venv, layout)
    except OSError as exc:
        if _is_permission_error(exc):
            raise permission_error(layout.venv, exc)
        raise InstallError("Could not remove the old environment at %s: %s" % (layout.venv, exc),
                           EXIT_ENV)
    code, out = run([python_info["path"], "-m", "venv", layout.venv], timeout=300)
    if code != 0:
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
    info = probe(layout.venv_python)
    if info is None:
        raise InstallError("The new virtual environment at %s does not work." % layout.venv,
                           EXIT_ENV)
    return info, "created with Python %s" % version_str(info["version"])


# --------------------------------------------------------------------------
# Steps 4-5: packages
# --------------------------------------------------------------------------

NETWORK_PATTERNS = (
    "NewConnectionError", "Failed to establish a new connection", "ConnectionError",
    "Temporary failure in name resolution", "Name or service not known",
    "nodename nor servname", "getaddrinfo failed", "Read timed out", "ConnectTimeoutError",
    "Network is unreachable", "No route to host", "Could not fetch URL",
    "Connection reset", "Connection refused", "ProxyError", "SSLError",
    "CERTIFICATE_VERIFY_FAILED", "There was a problem confirming the ssl certificate",
)
PERMISSION_PATTERNS = ("Permission denied", "[Errno 13]", "Access is denied", "EACCES",
                       "[WinError 5]")


def classify_pip_failure(output):
    """Map pip output to (exit_code, explanation)."""
    if any(p in output for p in NETWORK_PATTERNS):
        return EXIT_NETWORK, (
            "Could not download packages from the internet (PyPI). Check that you are "
            "online. On a school network a proxy or firewall may block pypi.org; ask "
            "your teacher or IT team, or try another network.")
    if any(p in output for p in PERMISSION_PATTERNS):
        return EXIT_PERMISSION, (
            "Permission denied while installing packages. Close any running games or "
            "editors that use Pygame and run the installer again.")
    if "No space left on device" in output or "[Errno 28]" in output:
        return EXIT_PACKAGE, "Your disk is full. Free up some space and try again."
    if "No matching distribution" in output or "Could not find a version" in output:
        return EXIT_PACKAGE, (
            "No ready-made package was found for this Python and computer. "
            "This usually means the Python version or CPU type is not supported.")
    return EXIT_PACKAGE, "pip could not install the package."


def pip_install(layout, spec, upgrade_pip=False):
    """Install one requirement. Raises InstallError on failure."""
    base = [layout.venv_python, "-m", "pip", "install", "--disable-pip-version-check",
            "--no-input", "--progress-bar", "off"]
    if upgrade_pip:
        cmd = base + ["--upgrade", "pip"]
    else:
        # Binary wheels only: never try to compile Pygame from source.
        cmd = base + ["--only-binary=:all:", spec]
    code, out = run(cmd, timeout=900)
    if code != 0:
        exit_code, why = classify_pip_failure(out)
        # pip reports an unreachable index as "no versions found" when it does
        # not retry, which would wrongly blame the Python version.
        if exit_code == EXIT_PACKAGE and "No matching distribution" in out \
                and not index_reachable():
            exit_code, why = classify_pip_failure("Could not fetch URL")
        raise InstallError(why, exit_code, details=tail(out))
    return out


def index_reachable(timeout=15):
    """Can we reach the package index (PyPI, or PIP_INDEX_URL if set)?"""
    try:
        from urllib.request import urlopen
        base = os.environ.get("PIP_INDEX_URL") or "https://pypi.org/simple"
        with urlopen(base.rstrip("/") + "/pygame/", timeout=timeout) as resp:
            return resp.status < 400
    except Exception:
        return False


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
        if _is_permission_error(exc):
            raise permission_error(layout.bin_dir, exc)
        raise InstallError("Could not create %s: %s" % (layout.shim, exc), EXIT_ENV)


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
    shutil.copyfile(layout.venv_pgzrun, layout.shim)
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
    """Add or refresh our PATH block. Returns True if the file changed."""
    try:
        with open(rc_path, encoding="utf-8") as fh:
            original = fh.read()
    except (IOError, OSError):
        original = ""
    if block in original:
        return False
    base = strip_rc_block(original) if RC_BEGIN in original else original
    if base and not base.endswith("\n"):
        base += "\n"
    new = base + ("\n" if base else "") + block
    parent = os.path.dirname(rc_path)
    if parent and not os.path.isdir(parent):
        os.makedirs(parent)
    with open(rc_path, "w", encoding="utf-8") as fh:
        fh.write(new)
    return True


def ensure_unix_path(layout, env=None, system=None):
    """Make sure new terminals have bin_dir on PATH. Returns list of messages."""
    env = os.environ if env is None else env
    home = env.get("HOME") or os.path.expanduser("~")
    changed = []
    for rc in shell_rc_files(env, system):
        if update_rc_file(rc, rc_block(layout.bin_dir, rc, home)):
            changed.append(rc)
    return changed


def ensure_windows_path(bin_dir, winreg_module=None):
    """Append bin_dir to the user's PATH in the registry. Returns True if changed."""
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

def verify(layout, launcher):
    """Prove imports work and a real Pygame Zero game runs. Raises on failure."""
    code, out = run([layout.venv_python, "-c",
                     "import pygame, pgzero, pgzero.runner; "
                     "print(pygame.version.ver, pgzero.__version__)"],
                    timeout=120)
    if code != 0:
        raise InstallError("Pygame or Pygame Zero cannot be imported.", EXIT_VERIFY,
                           details=tail(out))
    if not os.path.isfile(layout.venv_pgzrun):
        raise InstallError("The pgzrun program is missing from %s." % layout.venv_pgzrun,
                           EXIT_VERIFY)
    workdir = tempfile.mkdtemp(prefix="pgzero-check-")
    try:
        game = os.path.join(workdir, "smoke_game.py")
        with open(game, "w", encoding="utf-8") as fh:
            fh.write(SMOKE_GAME)
        env = clean_env({"SDL_VIDEODRIVER": "dummy", "SDL_AUDIODRIVER": "dummy",
                         "PYGAME_HIDE_SUPPORT_PROMPT": "1"})
        code, out = run([launcher, game], timeout=120, env=env)
        if code != 0 or SMOKE_TOKEN not in out:
            raise InstallError("A test game could not be run with pgzrun.", EXIT_VERIFY,
                               details=tail(out))
    finally:
        shutil.rmtree(workdir, ignore_errors=True)


# --------------------------------------------------------------------------
# Orchestration
# --------------------------------------------------------------------------

def install(args, console):
    console.write(console.bold("Pygame Zero Installer") + " (v%s)" % INSTALLER_VERSION)
    console.write("")

    console.step(1, "Detecting operating system")
    os_info = detect_os()
    check_platform_supported(os_info)
    console.ok("%s, %s" % (os_info["name"], os_info["arch"]))
    layout = Layout(os_info)

    console.step(2, "Checking Python")
    reuse = None if args.recreate else existing_venv_ok(layout, os_info)
    if reuse is not None and read_marker(layout) is not None and not args.python:
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

    console.step(3, "Preparing environment")
    prepare_home(layout)
    venv_info, action = create_venv(layout, chosen, os_info, recreate=args.recreate)
    write_marker(layout, {"python": version_str(venv_info["version"]),
                          "venv": layout.venv, "bin_dir": layout.bin_dir})
    # Keeping pip current avoids old pips that cannot read modern wheel tags.
    pip_warning = None
    try:
        pip_install(layout, None, upgrade_pip=True)
    except InstallError as exc:
        if exc.code == EXIT_NETWORK:
            console.fail()
            raise
        pip_warning = "could not upgrade pip; continuing with the existing version."
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
    status, msg = install_shim(layout)
    path_notes = []
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
                if ensure_windows_path(layout.bin_dir):
                    path_notes.append("windows")
            except (OSError, ImportError) as exc:
                console.warn("could not update your PATH automatically (%s)." % exc)
                path_notes.append("manual")
        else:
            try:
                changed = ensure_unix_path(layout)
            except OSError as exc:
                console.warn("could not update your shell startup file (%s)." % exc)
                changed = None
                path_notes.append("manual")
            if changed:
                path_notes.append("rc:" + ", ".join(changed))
            elif changed is not None and not on_path_now:
                path_notes.append("rc-existing")
        console.ok("OK (%s)" % layout.shim)

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
    if layout.windows:
        manifest = os.path.join(layout.bin_dir, MARKER_FILE)
        if os.path.exists(manifest):
            for name in ("pgzrun.exe", MARKER_FILE):
                p = os.path.join(layout.bin_dir, name)
                if os.path.exists(p):
                    os.remove(p)
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
            except (IOError, OSError):
                continue
            if RC_BEGIN in text:
                if rc.endswith("pygamezero.fish"):
                    os.remove(rc)
                else:
                    with open(rc, "w", encoding="utf-8") as fh:
                        fh.write(strip_rc_block(text))
                removed.append("PATH lines in %s" % rc)
    if read_marker(layout) is not None:
        try:
            shutil.rmtree(layout.home)
        except OSError as exc:
            if _is_permission_error(exc):
                raise permission_error(layout.home, exc)
            raise
        removed.append(layout.home)
    elif os.path.exists(layout.home):
        console.warn("left %s alone: it was not created by this installer." % layout.home)
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
    p.add_argument("--version", action="version", version=INSTALLER_VERSION)
    return p.parse_args(argv)


def main(argv=None):
    args = parse_args(sys.argv[1:] if argv is None else argv)
    console = Console()
    try:
        if args.uninstall:
            return uninstall(args, console)
        return install(args, console)
    except InstallError as exc:
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
        return exc.code
    except KeyboardInterrupt:
        console.write("")
        console.write("Cancelled. You can safely run the installer again.")
        return 130


if __name__ == "__main__":
    sys.exit(main())
