"""Installer behaviour: failure handling, idempotency, PATH setup, real installs.

Set PGZ_INTEGRATION=1 to also run the real end-to-end install (downloads
Pygame and Pygame Zero from PyPI into a temporary folder).
"""

import contextlib
import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import LINUX, ROOT, WINDOWS, fake_info, load_installer  # noqa: E402

inst = load_installer()
REAL_PIP_INSTALL = inst.pip_install
POSIX = os.name != "nt"
IS_ROOT = POSIX and os.geteuid() == 0


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        # Spaces and non-ASCII characters on purpose.
        self.tmp = tempfile.mkdtemp(prefix="pgz test ü ")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def layout(self, os_info=LINUX, **env):
        base = {"PGZERO_HOME": os.path.join(self.tmp, "pg zero"),
                "PGZERO_BIN_DIR": os.path.join(self.tmp, "my bin")}
        base.update(env)
        return inst.Layout(os_info, env=base)


def run_main(argv, env):
    out = io.StringIO()
    with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out):
        code = inst.main(argv)
    return code, out.getvalue()


class PipFailureClassificationTests(unittest.TestCase):
    def test_network(self):
        out = ("WARNING: Retrying ... after connection broken by "
               "'NewConnectionError(... [Errno 8] nodename nor servname provided)'\n"
               "ERROR: Could not find a version that satisfies the requirement pygame==2.6.1")
        code, why = inst.classify_pip_failure(out)
        self.assertEqual(code, inst.EXIT_NETWORK)
        self.assertIn("online", why)

    def test_proxy_and_ssl(self):
        for text in ("ProxyError('Cannot connect to proxy.')",
                     "SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED]"):
            self.assertEqual(inst.classify_pip_failure(text)[0], inst.EXIT_NETWORK)

    def test_permission(self):
        code, _ = inst.classify_pip_failure("ERROR: Could not install packages due to an "
                                            "OSError: [Errno 13] Permission denied: '/x'")
        self.assertEqual(code, inst.EXIT_PERMISSION)
        self.assertEqual(inst.classify_pip_failure("[WinError 5] Access is denied")[0],
                         inst.EXIT_PERMISSION)

    def test_no_wheel(self):
        code, why = inst.classify_pip_failure(
            "ERROR: No matching distribution found for pygame==2.6.1")
        self.assertEqual(code, inst.EXIT_PACKAGE)
        self.assertIn("not supported", why)

    def test_unknown(self):
        self.assertEqual(inst.classify_pip_failure("something odd")[0], inst.EXIT_PACKAGE)


class PipInstallTests(TempDirTestCase):
    def test_uses_venv_python_binary_only_and_pinned_spec(self):
        layout = self.layout()
        with mock.patch.object(inst, "run", return_value=(0, "ok")) as run:
            inst.pip_install(layout, inst.PYGAME_SPEC)
        cmd = run.call_args[0][0]
        self.assertEqual(cmd[0], layout.venv_python)
        self.assertIn("--only-binary=:all:", cmd)
        self.assertEqual(cmd[-1], "pygame==2.6.1")
        self.assertNotIn("--break-system-packages", cmd)
        self.assertNotIn("--user", cmd)

    def test_failure_raises_with_details(self):
        layout = self.layout()
        with mock.patch.object(inst, "run", return_value=(1, "line\nConnection refused")):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.pip_install(layout, inst.PGZERO_SPEC)
        self.assertEqual(ctx.exception.code, inst.EXIT_NETWORK)
        self.assertIn("Connection refused", ctx.exception.details)

    def test_no_versions_found_is_network_when_index_unreachable(self):
        layout = self.layout()
        out = ("ERROR: Could not find a version that satisfies the requirement pygame==2.6.1 "
               "(from versions: none)\nERROR: No matching distribution found for pygame==2.6.1")
        with mock.patch.object(inst, "run", return_value=(1, out)):
            with mock.patch.object(inst, "index_reachable", return_value=False):
                with self.assertRaises(inst.InstallError) as ctx:
                    inst.pip_install(layout, inst.PYGAME_SPEC)
            self.assertEqual(ctx.exception.code, inst.EXIT_NETWORK)
            with mock.patch.object(inst, "index_reachable", return_value=True):
                with self.assertRaises(inst.InstallError) as ctx:
                    inst.pip_install(layout, inst.PYGAME_SPEC)
            self.assertEqual(ctx.exception.code, inst.EXIT_PACKAGE)

    def test_index_reachable_false_for_dead_index(self):
        with mock.patch.dict(os.environ, {"PIP_INDEX_URL": "https://127.0.0.1:9/simple"}):
            self.assertFalse(inst.index_reachable(timeout=3))

    def test_clean_env_drops_variables_that_break_venvs(self):
        with mock.patch.dict(os.environ, {"PIP_USER": "1", "PYTHONPATH": "/x",
                                          "PYTHONHOME": "/y"}):
            env = inst.clean_env()
        for var in ("PIP_USER", "PYTHONPATH", "PYTHONHOME"):
            self.assertNotIn(var, env)


class HomeFolderSafetyTests(TempDirTestCase):
    def test_creates_marker_and_is_repeatable(self):
        layout = self.layout()
        inst.prepare_home(layout)
        inst.prepare_home(layout)
        self.assertIsNotNone(inst.read_marker(layout))

    def test_refuses_foreign_non_empty_folder(self):
        layout = self.layout()
        os.makedirs(layout.home)
        with open(os.path.join(layout.home, "homework.txt"), "w") as fh:
            fh.write("important")
        with self.assertRaises(inst.InstallError) as ctx:
            inst.prepare_home(layout)
        self.assertEqual(ctx.exception.code, inst.EXIT_ENV)
        self.assertTrue(os.path.exists(os.path.join(layout.home, "homework.txt")))

    def test_remove_tree_refuses_paths_outside_home(self):
        layout = self.layout()
        inst.prepare_home(layout)
        outside = os.path.join(self.tmp, "outside")
        os.makedirs(outside)
        with self.assertRaises(inst.InstallError):
            inst.remove_tree(outside, layout)
        with self.assertRaises(inst.InstallError):
            inst.remove_tree(layout.home, layout)
        self.assertTrue(os.path.isdir(outside))

    def test_remove_tree_refuses_without_marker(self):
        layout = self.layout()
        os.makedirs(layout.venv)
        with self.assertRaises(inst.InstallError):
            inst.remove_tree(layout.venv, layout)

    @unittest.skipUnless(POSIX and not IS_ROOT, "needs POSIX permissions as a normal user")
    def test_permission_error_is_reported(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        layout = inst.Layout(LINUX, env={"PGZERO_HOME": os.path.join(locked, "pgz")})
        with self.assertRaises(inst.InstallError) as ctx:
            inst.prepare_home(layout)
        self.assertEqual(ctx.exception.code, inst.EXIT_PERMISSION)
        self.assertIn("sudo", ctx.exception.message)


class LayoutTests(unittest.TestCase):
    def test_defaults(self):
        lin = inst.Layout(LINUX, env={"XDG_DATA_HOME": "/data"})
        self.assertEqual(lin.home, os.path.join("/data", "pygamezero"))
        self.assertTrue(lin.venv_python.endswith(os.path.join("venv", "bin", "python")))
        self.assertEqual(os.path.basename(lin.bin_dir), "bin")
        win = inst.Layout(WINDOWS, env={"LOCALAPPDATA": "C:\\Users\\a\\AppData\\Local"})
        self.assertTrue(win.venv_python.endswith("python.exe"))
        self.assertTrue(win.shim.endswith("pgzrun.exe"))
        self.assertIn("pygamezero", win.bin_dir)


@unittest.skipUnless(POSIX, "POSIX launcher script")
class UnixShimTests(TempDirTestCase):
    def test_create_then_unchanged(self):
        layout = self.layout()
        status, _ = inst.install_shim(layout)
        self.assertEqual(status, "ok")
        self.assertTrue(os.access(layout.shim, os.X_OK))
        self.assertEqual(inst.install_shim(layout)[0], "unchanged")

    def test_shim_quotes_paths_with_spaces_and_quotes(self):
        layout = self.layout(PGZERO_HOME=os.path.join(self.tmp, "it's here"))
        os.makedirs(os.path.dirname(layout.venv_python))
        fake_python = layout.venv_python
        with open(fake_python, "w") as fh:
            fh.write('#!/bin/sh\nprintf "%s|" "$@"\n')
        os.chmod(fake_python, 0o755)
        inst.install_shim(layout)
        out = subprocess.check_output([layout.shim, "my game.py", "--x"]).decode()
        self.assertEqual(out, "-m|pgzero|my game.py|--x|")

    def test_does_not_overwrite_foreign_pgzrun(self):
        layout = self.layout()
        os.makedirs(layout.bin_dir)
        with open(layout.shim, "w") as fh:
            fh.write("#!/bin/sh\necho someone else's pgzrun\n")
        status, msg = inst.install_shim(layout)
        self.assertEqual(status, "blocked")
        with open(layout.shim) as fh:
            self.assertIn("someone else", fh.read())

    def test_does_not_follow_symlinks(self):
        layout = self.layout()
        os.makedirs(layout.bin_dir)
        target = os.path.join(self.tmp, "real-pgzrun")
        with open(target, "w") as fh:
            fh.write("# %s\n" % inst.SHIM_MARKER)
        os.symlink(target, layout.shim)
        self.assertEqual(inst.install_shim(layout)[0], "blocked")


class WindowsShimTests(TempDirTestCase):
    def test_copies_launcher_and_respects_foreign_file(self):
        layout = self.layout(WINDOWS)
        os.makedirs(os.path.dirname(layout.venv_pgzrun))
        with open(layout.venv_pgzrun, "wb") as fh:
            fh.write(b"MZ fake launcher")
        self.assertEqual(inst.install_shim(layout)[0], "ok")
        self.assertEqual(inst.install_shim(layout)[0], "unchanged")

        other = self.layout(WINDOWS, PGZERO_BIN_DIR=os.path.join(self.tmp, "other bin"))
        os.makedirs(other.bin_dir)
        with open(other.shim, "wb") as fh:
            fh.write(b"not ours")
        self.assertEqual(inst.install_shim(other)[0], "blocked")


class ShellRcTests(TempDirTestCase):
    def test_rc_files_by_shell(self):
        home = self.tmp
        self.assertEqual(inst.shell_rc_files({"HOME": home, "SHELL": "/bin/zsh"}, "Darwin"),
                         [os.path.join(home, ".zshrc")])
        self.assertEqual(inst.shell_rc_files({"HOME": home, "SHELL": "/bin/bash"}, "Darwin"),
                         [os.path.join(home, ".bashrc"), os.path.join(home, ".bash_profile")])
        self.assertEqual(inst.shell_rc_files({"HOME": home, "SHELL": "/bin/bash"}, "Linux"),
                         [os.path.join(home, ".bashrc")])
        self.assertTrue(inst.shell_rc_files({"HOME": home, "SHELL": "/usr/bin/fish"}, "Linux")[0]
                        .endswith("pygamezero.fish"))
        self.assertEqual(inst.shell_rc_files({"HOME": home, "SHELL": "/bin/ksh"}, "Linux"),
                         [os.path.join(home, ".profile")])

    def test_update_is_idempotent_and_preserves_user_content(self):
        rc = os.path.join(self.tmp, ".bashrc")
        with open(rc, "w") as fh:
            fh.write("alias ll='ls -l'")  # no trailing newline
        block = inst.rc_block("/home/x/.local/bin", rc, "/home/x")
        self.assertTrue(inst.update_rc_file(rc, block))
        self.assertFalse(inst.update_rc_file(rc, block))
        with open(rc) as fh:
            text = fh.read()
        self.assertTrue(text.startswith("alias ll='ls -l'\n"))
        self.assertEqual(text.count(inst.RC_BEGIN), 1)
        self.assertIn("$HOME/.local/bin", text)

        # A changed bin folder replaces the old block instead of adding another.
        self.assertTrue(inst.update_rc_file(rc, inst.rc_block("/opt/pgz bin", rc, "/home/x")))
        with open(rc) as fh:
            text = fh.read()
        self.assertEqual(text.count(inst.RC_BEGIN), 1)
        self.assertIn("/opt/pgz bin", text)

        self.assertEqual(inst.strip_rc_block(text), "alias ll='ls -l'\n")
        self.assertEqual(inst.strip_rc_block(block), "")

    @unittest.skipUnless(POSIX and shutil.which("sh"), "needs sh")
    def test_block_really_puts_odd_paths_on_path(self):
        home = os.path.join(self.tmp, "home")
        bin_dir = os.path.join(home, 'we"ird $dir `x`', "bin")
        rc = os.path.join(self.tmp, ".profile")
        inst.update_rc_file(rc, inst.rc_block(bin_dir, rc, home))
        inst.update_rc_file(rc, inst.rc_block(bin_dir, rc, home))
        out = subprocess.check_output(
            ["sh", "-c", '. "$1"; . "$1"; printf %s "$PATH"', "sh", rc],
            env={"HOME": home, "PATH": "/usr/bin:/bin"}).decode()
        self.assertEqual(out.split(":").count(bin_dir), 1)

    @unittest.skipUnless(POSIX, "POSIX shells")
    def test_ensure_unix_path_writes_rc_in_home(self):
        layout = self.layout()
        env = {"HOME": self.tmp, "SHELL": "/bin/zsh"}
        changed = inst.ensure_unix_path(layout, env=env, system="Darwin")
        self.assertEqual(changed, [os.path.join(self.tmp, ".zshrc")])
        self.assertEqual(inst.ensure_unix_path(layout, env=env, system="Darwin"), [])

    def test_path_contains(self):
        sep = os.pathsep
        self.assertTrue(inst.path_contains(sep.join(["/a", "/b/c/"]), "/b/c"))
        self.assertFalse(inst.path_contains(sep.join(["/a", "/b"]), "/b/c"))


class FakeWinreg(object):
    """Just enough of the winreg module for the PATH functions."""
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ, KEY_WRITE = 1, 2
    REG_SZ, REG_EXPAND_SZ, REG_DWORD = 1, 2, 4

    def __init__(self, value=None, kind=2):
        self.store = {} if value is None else {"Path": (value, kind)}

    @contextlib.contextmanager
    def OpenKey(self, root, sub, reserved=0, access=0):
        yield self

    def QueryValueEx(self, key, name):
        if name not in self.store:
            raise OSError("missing")
        return self.store[name]

    def SetValueEx(self, key, name, reserved, kind, value):
        self.store[name] = (value, kind)


class WindowsPathTests(unittest.TestCase):
    def setUp(self):
        patcher = mock.patch.object(inst, "_broadcast_environment_change")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.path_sep = mock.patch.object(inst.os, "pathsep", ";")

    def test_append_preserves_type_and_is_idempotent(self):
        reg = FakeWinreg("%USERPROFILE%\\bin;C:\\Tools", FakeWinreg.REG_EXPAND_SZ)
        with self.path_sep:
            self.assertTrue(inst.ensure_windows_path("C:\\Users\\A B\\pgz\\bin", reg))
            self.assertFalse(inst.ensure_windows_path("C:\\Users\\A B\\pgz\\bin", reg))
        value, kind = reg.store["Path"]
        self.assertEqual(value, "%USERPROFILE%\\bin;C:\\Tools;C:\\Users\\A B\\pgz\\bin")
        self.assertEqual(kind, FakeWinreg.REG_EXPAND_SZ)

    def test_creates_missing_value_and_removes_cleanly(self):
        reg = FakeWinreg()
        with self.path_sep:
            inst.ensure_windows_path("C:\\pgz\\bin", reg)
            self.assertEqual(reg.store["Path"][0], "C:\\pgz\\bin")
            reg.store["Path"] = ("C:\\Tools;C:\\pgz\\bin", FakeWinreg.REG_SZ)
            self.assertTrue(inst.remove_windows_path("C:\\pgz\\bin", reg))
        self.assertEqual(reg.store["Path"], ("C:\\Tools", FakeWinreg.REG_SZ))

    def test_refuses_unexpected_value_type(self):
        reg = FakeWinreg(5, FakeWinreg.REG_DWORD)
        with self.assertRaises(inst.InstallError):
            inst.ensure_windows_path("C:\\pgz\\bin", reg)


class MissingLibraryTests(unittest.TestCase):
    OUT = ("NotImplementedError: mixer module not available (ImportError: "
           "libgthread-2.0.so.0: cannot open shared object file: No such file or directory)")

    def test_debian_gets_exact_package(self):
        text = inst.missing_library_help(self.OUT, LINUX)
        self.assertIn("libgthread-2.0.so.0", text)
        self.assertIn("sudo apt install libglib2.0-0", text)

    def test_unknown_library_and_distro(self):
        text = inst.missing_library_help("libfoo.so.3: cannot open shared object file",
                                         dict(LINUX, distro_id="gentoo", distro_like=""))
        self.assertIn("libfoo.so.3", text)
        self.assertNotIn("sudo", text)

    def test_other_errors_are_not_library_errors(self):
        self.assertIsNone(inst.missing_library_help("ModuleNotFoundError: pgzero", LINUX))


class OfferPythonInstallTests(unittest.TestCase):
    def test_never_installs_without_a_yes(self):
        console = inst.Console(io.StringIO())
        with mock.patch.object(inst.shutil, "which", return_value="/usr/local/bin/brew"), \
                mock.patch.object(inst.subprocess, "call") as call:
            # Not interactive: no prompt, no install.
            self.assertFalse(inst.offer_python_install(
                dict(LINUX, system="Darwin"), console, stdin=io.StringIO("y\n")))
            # Interactive but the user says no.
            tty = io.StringIO("n\n")
            tty.isatty = lambda: True
            self.assertFalse(inst.offer_python_install(dict(LINUX, system="Darwin"),
                                                       console, stdin=tty))
        call.assert_not_called()

    def test_yes_runs_user_scope_winget(self):
        console = inst.Console(io.StringIO())
        tty = io.StringIO("y\n")
        tty.isatty = lambda: True
        with mock.patch.object(inst.shutil, "which", return_value="winget"), \
                mock.patch.object(inst.subprocess, "call", return_value=0) as call:
            self.assertTrue(inst.offer_python_install(dict(WINDOWS, arch="arm64"),
                                                      console, stdin=tty))
        cmd = call.call_args[0][0]
        self.assertIn("--scope", cmd)
        self.assertEqual(cmd[cmd.index("--scope") + 1], "user")
        self.assertEqual(cmd[-2:], ["--architecture", "x64"])

    def test_linux_never_offers_sudo(self):
        console = inst.Console(io.StringIO())
        self.assertFalse(inst.offer_python_install(LINUX, console, assume_yes=True))


class MainFlowTests(TempDirTestCase):
    """Drive main() end to end with the slow/external parts replaced."""

    def env(self):
        return {"PGZERO_HOME": os.path.join(self.tmp, "pg zero"),
                "PGZERO_BIN_DIR": os.path.join(self.tmp, "bin dir"),
                "HOME": self.tmp, "SHELL": "/bin/bash"}

    def patches(self, **overrides):
        venv = fake_info((3, 13, 1), in_venv=True)
        defaults = {
            "detect_os": mock.patch.object(inst, "detect_os", return_value=LINUX),
            "existing_venv_ok": mock.patch.object(inst, "existing_venv_ok", return_value=None),
            "select_python": mock.patch.object(inst, "select_python",
                                               return_value=(fake_info(), [])),
            "create_venv": mock.patch.object(inst, "create_venv",
                                             return_value=(venv, "created")),
            "pip_install": mock.patch.object(inst, "pip_install", return_value=""),
            "install_shim": mock.patch.object(inst, "install_shim", return_value=("ok", "x")),
            "ensure_unix_path": mock.patch.object(inst, "ensure_unix_path", return_value=[]),
            "verify": mock.patch.object(inst, "verify"),
            "installed_versions": mock.patch.object(
                inst, "installed_versions", return_value={"pygame": "2.6.1", "pgzero": "1.2.1"}),
        }
        defaults.update(overrides)
        stack = contextlib.ExitStack()
        mocks = {k: stack.enter_context(p) for k, p in defaults.items()}
        return stack, mocks

    def test_success_message_only_after_verification(self):
        stack, m = self.patches()
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_OK, out)
        self.assertIn("[7/7] Verifying installation... OK", out)
        self.assertIn("Installation complete!", out)
        self.assertIn("pgzrun game.py", out)
        self.assertTrue(inst.read_marker(inst.Layout(LINUX, self.env()))["complete"])

    def test_pygame_installed_before_pgzero(self):
        stack, m = self.patches()
        with stack:
            run_main([], self.env())
        specs = [c[0][1] for c in m["pip_install"].call_args_list]
        self.assertEqual(specs, [None, inst.PYGAME_SPEC, inst.PGZERO_SPEC])

    def test_missing_python(self):
        stack, m = self.patches(
            select_python=mock.patch.object(inst, "select_python", return_value=(None, [])),
            offer=mock.patch.object(inst, "offer_python_install", return_value=False))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_NO_PYTHON)
        self.assertIn("Python 3.9 to 3.13", out)
        self.assertIn("sudo apt", out)
        self.assertNotIn("Installation complete", out)
        m["create_venv"].assert_not_called()

    def test_only_too_new_python(self):
        rejected = [(fake_info((3, 14, 0)), "Python 3.14 is too new: Pygame does not support it yet")]
        stack, m = self.patches(
            select_python=mock.patch.object(inst, "select_python", return_value=(None, rejected)),
            offer=mock.patch.object(inst, "offer_python_install", return_value=False))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_NO_PYTHON)
        self.assertIn("too new", out)

    def test_missing_venv_support(self):
        rejected = [(fake_info((3, 12, 3), has_ensurepip=False),
                     "virtual environment support (venv/ensurepip) is missing")]
        stack, m = self.patches(
            select_python=mock.patch.object(inst, "select_python", return_value=(None, rejected)),
            offer=mock.patch.object(inst, "offer_python_install", return_value=False))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_NO_PYTHON)
        self.assertIn("python3.12-venv", out)

    def test_python_installed_on_offer_then_continues(self):
        stack, m = self.patches(
            select_python=mock.patch.object(inst, "select_python",
                                            side_effect=[(None, []), (fake_info(), [])]),
            offer=mock.patch.object(inst, "offer_python_install", return_value=True))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_OK, out)

    def test_failed_pgzero_install_reports_partial(self):
        def pip(layout, spec, upgrade_pip=False):
            if spec == inst.PGZERO_SPEC:
                raise inst.InstallError("pip could not install the package.", inst.EXIT_PACKAGE,
                                        details="ERROR: boom")
            return ""
        stack, m = self.patches(pip_install=mock.patch.object(inst, "pip_install",
                                                              side_effect=pip))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_PACKAGE)
        self.assertIn("[5/7] Installing Pygame Zero... FAILED", out)
        self.assertIn("Partial installation: pygame==2.6.1 installed, pgzero==1.2.1 did not", out)
        self.assertIn("ERROR: boom", out)
        self.assertNotIn("Installation complete", out)
        m["verify"].assert_not_called()

    def test_network_failure_from_real_pip_output(self):
        def fake_run(cmd, timeout=None, env=None):
            return 1, "WARNING: Retrying... NewConnectionError: Failed to establish a new connection"
        stack, m = self.patches()
        with stack, mock.patch.object(inst, "pip_install", REAL_PIP_INSTALL), \
                mock.patch.object(inst, "run", side_effect=fake_run):
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_NETWORK)
        self.assertIn("pypi.org", out)
        self.assertNotIn("Installation complete", out)

    def test_pip_upgrade_failure_is_not_fatal(self):
        def pip(layout, spec, upgrade_pip=False):
            if upgrade_pip:
                raise inst.InstallError("old pip", inst.EXIT_PACKAGE)
            return ""
        stack, m = self.patches(pip_install=mock.patch.object(inst, "pip_install",
                                                              side_effect=pip))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_OK, out)
        self.assertIn("could not upgrade pip", out)

    def test_verification_failure(self):
        stack, m = self.patches(verify=mock.patch.object(
            inst, "verify", side_effect=inst.InstallError("A test game could not be run.",
                                                          inst.EXIT_VERIFY)))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_VERIFY)
        self.assertNotIn("Installation complete", out)

    def test_blocked_shim_points_to_full_path(self):
        stack, m = self.patches(install_shim=mock.patch.object(
            inst, "install_shim", return_value=("blocked", "exists")))
        with stack:
            code, out = run_main([], self.env())
        self.assertEqual(code, inst.EXIT_OK)
        self.assertIn("Another program called pgzrun", out)
        launcher = m["verify"].call_args[0][1]
        self.assertTrue(launcher.endswith(os.path.join("venv", "bin", "pgzrun")))

    def test_no_modify_path_prints_instructions(self):
        stack, m = self.patches()
        with stack:
            code, out = run_main(["--no-modify-path"], self.env())
        self.assertEqual(code, inst.EXIT_OK)
        self.assertIn("Add this folder to your PATH", out)
        m["ensure_unix_path"].assert_not_called()

    @unittest.skipUnless(POSIX and not IS_ROOT, "needs POSIX permissions as a normal user")
    def test_permission_error_exit_code(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        env = dict(self.env(), PGZERO_HOME=os.path.join(locked, "pgz"))
        stack, m = self.patches()
        with stack:
            code, out = run_main([], env)
        self.assertEqual(code, inst.EXIT_PERMISSION)
        self.assertIn("Permission denied", out)


class BootstrapperIntegrityTests(unittest.TestCase):
    def test_pinned_hash_matches_install_py(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import release
        finally:
            sys.path.pop(0)
        digest = release.install_py_sha256()
        for name, (repo, version, sha) in release.stamped_values().items():
            self.assertEqual(sha, digest, "%s has a stale hash; run tools/release.py" % name)

    def test_versions_agree(self):
        sys.path.insert(0, os.path.join(ROOT, "tools"))
        try:
            import release
        finally:
            sys.path.pop(0)
        for name, (repo, version, sha) in release.stamped_values().items():
            self.assertEqual(version.lstrip("v"), inst.INSTALLER_VERSION, name)


@unittest.skipUnless(POSIX and shutil.which("bash"), "install.sh needs bash")
class ShellBootstrapperTests(TempDirTestCase):
    def run_sh(self, env, stdin_script=False):
        script = os.path.join(ROOT, "install.sh")
        bash = shutil.which("bash")
        if stdin_script:  # like `curl ... | bash`
            with open(script, "rb") as fh:
                return subprocess.run([bash], input=fh.read(), stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, env=env, timeout=60)
        return subprocess.run([bash, script], stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env, timeout=60)

    def test_missing_python_exits_3_with_instructions(self):
        bindir = os.path.join(self.tmp, "bin")
        os.makedirs(bindir)
        for tool in ("uname", "dirname", "mktemp", "rm", "cat"):
            path = shutil.which(tool)
            if path:
                os.symlink(path, os.path.join(bindir, tool))
        proc = self.run_sh({"HOME": self.tmp, "PATH": bindir})
        out = proc.stdout.decode()
        self.assertEqual(proc.returncode, 3, out)
        self.assertIn("Python 3 was not found", out)

    def test_refuses_plain_http(self):
        env = dict(os.environ, HOME=self.tmp, PGZ_INSTALLER_BASE_URL="http://example.com")
        proc = self.run_sh(env, stdin_script=True)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("insecure", proc.stdout.decode())

    def test_syntax(self):
        subprocess.check_call(["bash", "-n", os.path.join(ROOT, "install.sh")])


@unittest.skipUnless(os.environ.get("PGZ_INTEGRATION") == "1",
                     "set PGZ_INTEGRATION=1 to run the real install (needs internet)")
class RealInstallTests(TempDirTestCase):
    """Real install into a folder whose path has spaces and non-ASCII characters."""

    def test_fresh_repeat_and_uninstall(self):
        home = os.path.join(self.tmp, "home ü")
        os.makedirs(home)
        env = dict(os.environ, HOME=home, USERPROFILE=home, SHELL="/bin/bash",
                   PGZERO_HOME=os.path.join(home, "Pygame Zero ü"),
                   PGZERO_BIN_DIR=os.path.join(home, "bin dir"))
        args = [sys.executable, os.path.join(ROOT, "install.py")]
        if not POSIX:
            args.append("--no-modify-path")  # never touch a developer's registry here

        for attempt in ("fresh", "repeat"):
            proc = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  env=env, timeout=1200)
            out = proc.stdout.decode("utf-8", "replace")
            self.assertEqual(proc.returncode, 0, "%s install failed:\n%s" % (attempt, out))
            self.assertIn("Installation complete!", out)

        layout = inst.Layout(inst.detect_os(), env=env)
        check = subprocess.run([layout.venv_python, "-c",
                                "import pygame, pgzero; print(pygame.version.ver, pgzero.__version__)"],
                               stdout=subprocess.PIPE, timeout=120)
        self.assertEqual(check.stdout.decode().split()[-2:], ["2.6.1", "1.2.1"])
        self.assertTrue(os.path.isfile(layout.shim))

        # The launcher runs a real game from a folder with spaces.
        game_dir = os.path.join(self.tmp, "my games")
        os.makedirs(game_dir)
        with open(os.path.join(game_dir, "game.py"), "w") as fh:
            fh.write(inst.SMOKE_GAME)
        game_env = dict(env, SDL_VIDEODRIVER="dummy", SDL_AUDIODRIVER="dummy")
        proc = subprocess.run([layout.shim, "game.py"], cwd=game_dir, env=game_env,
                              stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=120)
        self.assertIn(inst.SMOKE_TOKEN, proc.stdout.decode("utf-8", "replace"))

        if POSIX:
            with open(os.path.join(home, ".bashrc")) as fh:
                self.assertEqual(fh.read().count(inst.RC_BEGIN), 1)

        proc = subprocess.run(args + ["--uninstall"], env=env, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, timeout=300)
        self.assertEqual(proc.returncode, 0, proc.stdout.decode("utf-8", "replace"))
        self.assertFalse(os.path.exists(layout.home))
        self.assertFalse(os.path.exists(layout.shim))


if __name__ == "__main__":
    unittest.main()
