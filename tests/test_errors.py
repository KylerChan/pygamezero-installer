"""Error handling: disk space, repairs, network, privileges, file locks, crashes."""

import collections
import contextlib
import errno
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
from helpers import LINUX, MAC, WINDOWS, fake_info, load_installer  # noqa: E402

inst = load_installer()
POSIX = os.name != "nt"
IS_ROOT = POSIX and os.geteuid() == 0
DiskUsage = collections.namedtuple("DiskUsage", "total used free")
MB = 1024 * 1024


class TempDirTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pgz errors ü ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        # Never really sleep between retries.
        patcher = mock.patch.object(inst.time, "sleep")
        self.sleep = patcher.start()
        self.addCleanup(patcher.stop)

    def layout(self, os_info=LINUX, **env):
        base = {"PGZERO_HOME": os.path.join(self.tmp, "pg zero"),
                "PGZERO_BIN_DIR": os.path.join(self.tmp, "my bin")}
        base.update(env)
        return inst.Layout(os_info, env=base)

    def env(self):
        return {"PGZERO_HOME": os.path.join(self.tmp, "pg zero"),
                "PGZERO_BIN_DIR": os.path.join(self.tmp, "bin dir"),
                "HOME": self.tmp, "SHELL": "/bin/bash"}

    def run_main(self, argv, env=None, unset=()):
        env = self.env() if env is None else env
        out = io.StringIO()
        with mock.patch.dict(os.environ, env), contextlib.redirect_stdout(out), \
                mock.patch.object(inst, "fallback_log_dir", return_value=self.tmp):
            for var in unset:
                os.environ.pop(var, None)
            code = inst.main(argv)
        return code, out.getvalue()

    def patches(self, **overrides):
        venv = fake_info((3, 13, 1), in_venv=True)
        defaults = {
            "detect_os": mock.patch.object(inst, "detect_os", return_value=LINUX),
            "check_existing_venv": mock.patch.object(inst, "check_existing_venv",
                                                     return_value=(None, None)),
            "select_python": mock.patch.object(inst, "select_python",
                                               return_value=(fake_info(), [])),
            "create_venv": mock.patch.object(inst, "create_venv",
                                             return_value=(venv, "created")),
            "pip_install": mock.patch.object(inst, "pip_install", return_value=""),
            "install_shim": mock.patch.object(inst, "install_shim", return_value=("ok", "x")),
            "ensure_unix_path": mock.patch.object(inst, "ensure_unix_path",
                                                  return_value=([], [])),
            "verify": mock.patch.object(inst, "verify"),
            "installed_versions": mock.patch.object(
                inst, "installed_versions", return_value={"pygame": "2.6.1", "pgzero": "1.2.1"}),
            "geteuid": mock.patch.object(inst.os, "geteuid", return_value=1000, create=True),
        }
        defaults.update(overrides)
        stack = contextlib.ExitStack()
        mocks = {k: stack.enter_context(p) for k, p in defaults.items()}
        return stack, mocks


def oserror(code=None, winerror=None, filename="x"):
    exc = OSError(code or errno.EIO, os.strerror(code) if code else "error", filename)
    if winerror is not None:
        exc.winerror = winerror
    return exc


# --------------------------------------------------------------------------
# 1. Disk space
# --------------------------------------------------------------------------

class DiskSpaceTests(TempDirTestCase):
    def test_preflight_refuses_when_disk_is_nearly_full(self):
        layout = self.layout()
        with mock.patch.object(inst.shutil, "disk_usage", return_value=DiskUsage(0, 0, 120 * MB)):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.check_disk_space(layout)
        self.assertEqual(ctx.exception.code, inst.EXIT_ENV)
        self.assertIn("500 MB", ctx.exception.message)
        self.assertIn("only 120 MB", ctx.exception.message)

    def test_preflight_passes_with_enough_space(self):
        with mock.patch.object(inst.shutil, "disk_usage",
                               return_value=DiskUsage(0, 0, 2000 * MB)):
            inst.check_disk_space(self.layout())

    def test_free_space_checks_nearest_existing_folder(self):
        missing = os.path.join(self.tmp, "a", "b", "c")
        with mock.patch.object(inst.shutil, "disk_usage",
                               return_value=DiskUsage(0, 0, 7 * MB)) as usage:
            self.assertEqual(inst.free_space_mb(missing), 7)
        self.assertEqual(usage.call_args[0][0], self.tmp)

    def test_unknown_free_space_does_not_block(self):
        with mock.patch.object(inst.shutil, "disk_usage", side_effect=OSError("nope")):
            self.assertIsNone(inst.free_space_mb(self.tmp))
            inst.check_disk_space(self.layout())

    def test_pip_disk_full(self):
        for out in ("ERROR: Could not install packages due to an OSError: "
                    "[Errno 28] No space left on device",
                    "OSError: [WinError 112] There is not enough space on the disk"):
            code, why = inst.classify_pip_failure(out)
            self.assertEqual(code, inst.EXIT_ENV)
            self.assertIn("disk is full", why)

    def test_venv_creation_disk_full(self):
        layout = self.layout()
        inst.prepare_home(layout)
        with mock.patch.object(inst, "run",
                               return_value=(1, "OSError: [Errno 28] No space left on device")):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.create_venv(layout, fake_info(), LINUX)
        self.assertEqual(ctx.exception.code, inst.EXIT_ENV)
        self.assertIn("disk is full", ctx.exception.message)

    def test_marker_write_disk_full(self):
        layout = self.layout()
        inst.prepare_home(layout)
        with mock.patch.object(inst.json, "dump", side_effect=oserror(errno.ENOSPC)):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.write_marker(layout, {})
        self.assertIn("disk is full", ctx.exception.message)

    def test_main_reports_disk_full_from_anywhere(self):
        stack, m = self.patches(install_shim=mock.patch.object(
            inst, "install_shim", side_effect=oserror(errno.ENOSPC, filename="/x/pgzrun")))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_ENV, out)
        self.assertIn("disk is full", out)
        self.assertNotIn("Something unexpected", out)

    def test_main_stops_before_creating_anything(self):
        stack, m = self.patches(disk=mock.patch.object(
            inst.shutil, "disk_usage", return_value=DiskUsage(0, 0, 10 * MB)))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_ENV)
        self.assertIn("not enough free disk space", out)
        m["create_venv"].assert_not_called()


# --------------------------------------------------------------------------
# 2, 3, 9. Interrupted installs, broken environments, missing pip
# --------------------------------------------------------------------------

class RepairTests(TempDirTestCase):
    def make_venv(self, complete):
        layout = self.layout()
        inst.prepare_home(layout)
        os.makedirs(os.path.dirname(layout.venv_python))
        with open(layout.venv_python, "w") as fh:
            fh.write("")
        data = {"complete": True} if complete else {}
        inst.write_marker(layout, data)
        return layout

    def check(self, layout, pip_ok=True, imports_ok=True):
        venv = fake_info(in_venv=True)
        with mock.patch.object(inst, "existing_venv_ok", return_value=venv), \
                mock.patch.object(inst, "pip_works", return_value=pip_ok), \
                mock.patch.object(inst, "packages_import", return_value=imports_ok):
            return inst.check_existing_venv(layout, LINUX)

    def test_healthy_environment_is_reused(self):
        info, problem = self.check(self.make_venv(complete=True))
        self.assertIsNotNone(info)
        self.assertIsNone(problem)

    def test_interrupted_install_is_detected(self):
        info, problem = self.check(self.make_venv(complete=False))
        self.assertIn("did not finish", problem)

    def test_broken_pip_is_detected(self):
        _, problem = self.check(self.make_venv(complete=True), pip_ok=False)
        self.assertIn("pip", problem)

    def test_broken_packages_are_detected(self):
        _, problem = self.check(self.make_venv(complete=True), imports_ok=False)
        self.assertIn("Pygame", problem)

    def test_unusable_environment_is_detected(self):
        layout = self.make_venv(complete=True)
        with mock.patch.object(inst, "existing_venv_ok", return_value=None):
            info, problem = inst.check_existing_venv(layout, LINUX)
        self.assertIsNone(info)
        self.assertIn("no longer works", problem)

    def test_no_environment_means_nothing_to_repair(self):
        self.assertEqual(inst.check_existing_venv(self.layout(), LINUX), (None, None))

    def test_main_rebuilds_once_with_a_real_base_python(self):
        venv = fake_info(in_venv=True, path="/venv/python")
        stack, m = self.patches(check_existing_venv=mock.patch.object(
            inst, "check_existing_venv",
            return_value=(venv, "the previous installation did not finish")))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_OK, out)
        m["select_python"].assert_called_once()
        self.assertEqual(m["create_venv"].call_count, 1)
        self.assertTrue(m["create_venv"].call_args[1]["recreate"])
        # The base interpreter, never the broken environment's own python.
        self.assertEqual(m["create_venv"].call_args[0][1]["path"], "/usr/bin/python3.13")
        self.assertIn("repaired: the previous installation did not finish", out)

    def test_ctrl_c_mid_pip_exits_130_and_next_run_repairs(self):
        def pip(layout, spec, upgrade_pip=False):
            if spec == inst.PYGAME_SPEC:
                raise KeyboardInterrupt
            return ""
        stack, m = self.patches(pip_install=mock.patch.object(inst, "pip_install",
                                                              side_effect=pip))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, 130)
        self.assertIn("Cancelled", out)
        self.assertNotIn("Traceback", out)
        layout = inst.Layout(LINUX, self.env())
        self.assertFalse(inst.read_marker(layout).get("complete"))
        os.makedirs(layout.venv)
        with mock.patch.object(inst, "existing_venv_ok", return_value=fake_info(in_venv=True)):
            self.assertIn("did not finish", inst.check_existing_venv(layout, LINUX)[1])

    def test_leftovers_of_an_interrupted_first_run_are_not_foreign(self):
        layout = self.layout()
        os.makedirs(layout.home)
        for name in (inst.MARKER_FILE + ".tmp", inst.LOG_FILE):
            with open(os.path.join(layout.home, name), "w") as fh:
                fh.write("{")
        inst.prepare_home(layout)
        self.assertIsNotNone(inst.read_marker(layout))

    def test_marker_that_is_not_an_object_is_ignored(self):
        layout = self.layout()
        os.makedirs(layout.home)
        with open(layout.marker, "w") as fh:
            fh.write("[1, 2]")
        self.assertIsNone(inst.read_marker(layout))


class CreateVenvTests(TempDirTestCase):
    def setUp(self):
        TempDirTestCase.setUp(self)
        self.lay = self.layout()
        inst.prepare_home(self.lay)

    def create(self, run_results, pip_results, probe_result=None):
        probe_result = probe_result or fake_info(in_venv=True)
        with mock.patch.object(inst, "existing_venv_ok", return_value=None), \
                mock.patch.object(inst, "run", side_effect=run_results) as run, \
                mock.patch.object(inst, "probe", return_value=probe_result), \
                mock.patch.object(inst, "pip_works", side_effect=pip_results):
            result = inst.create_venv(self.lay, fake_info(), LINUX)
        return result, run

    def test_missing_pip_is_repaired_with_ensurepip(self):
        (info, action), run = self.create([(0, ""), (0, "")], [False, True])
        self.assertIn("ensurepip", run.call_args_list[1][0][0])
        self.assertIn("created", action)

    def test_missing_pip_that_cannot_be_repaired(self):
        with self.assertRaises(inst.InstallError) as ctx:
            self.create([(0, ""), (1, "ensurepip failed")], [False, False])
        self.assertEqual(ctx.exception.code, inst.EXIT_ENV)
        self.assertIn("pip", ctx.exception.message)
        self.assertIn("python3.13-venv", ctx.exception.message)

    def test_missing_pip_on_mac_suggests_reinstalling_python(self):
        with mock.patch.object(inst, "existing_venv_ok", return_value=None), \
                mock.patch.object(inst, "run", return_value=(0, "")), \
                mock.patch.object(inst, "probe", return_value=fake_info(in_venv=True)), \
                mock.patch.object(inst, "pip_works", return_value=False):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.create_venv(self.lay, fake_info(), MAC)
        self.assertIn("python.org", ctx.exception.message)

    def test_venv_timeout(self):
        with mock.patch.object(inst, "existing_venv_ok", return_value=None), \
                mock.patch.object(inst, "run", return_value=(124, "(timed out after 300 seconds)")):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.create_venv(self.lay, fake_info(), LINUX)
        self.assertIn("more than 5 minutes", ctx.exception.message)

    def test_new_python_that_hangs(self):
        def fake_probe(path, runner=None, errors=None):
            errors.append("it did not respond within 30 seconds")
            return None
        with mock.patch.object(inst, "existing_venv_ok", return_value=None), \
                mock.patch.object(inst, "run", return_value=(0, "")), \
                mock.patch.object(inst, "probe", side_effect=fake_probe):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.create_venv(self.lay, fake_info(), LINUX)
        self.assertIn("did not respond", ctx.exception.message)
        self.assertIn("Antivirus", ctx.exception.message)

    def test_probe_timeout_is_reported(self):
        def runner(cmd, **kwargs):
            raise subprocess.TimeoutExpired(cmd, 30)
        errors = []
        self.assertIsNone(inst.probe("/slow/python", runner=runner, errors=errors))
        self.assertIn("30 seconds", errors[0])
        _, rejected = inst.select_python(LINUX, explicit="/slow/python", runner=runner)
        self.assertIn("did not respond", rejected[0][1])


# --------------------------------------------------------------------------
# 4. Network
# --------------------------------------------------------------------------

class NetworkTests(TempDirTestCase):
    OFFLINE = ("WARNING: Retrying ... 'NewConnectionError(... [Errno 8] nodename nor servname "
               "provided, or not known)'\nERROR: No matching distribution found for pygame")
    SSL = ("Could not fetch URL https://pypi.org/simple/pygame/: There was a problem confirming "
           "the ssl certificate: HTTPSConnectionPool(host='pypi.org', port=443): "
           "(Caused by SSLError(SSLCertVerificationError(1, '[SSL: CERTIFICATE_VERIFY_FAILED] "
           "certificate verify failed: self-signed certificate in certificate chain')))")
    PROXY = ("WARNING: Retrying ... ProxyError('Cannot connect to proxy.', "
             "OSError('Tunnel connection failed: 407 Proxy Authentication Required'))")
    TIMEOUT = ("WARNING: Retrying ... ReadTimeoutError(\"HTTPSConnectionPool(host='files."
               "pythonhosted.org', port=443): Read timed out. (read timeout=15)\")")
    HASHES = ("ERROR: THESE PACKAGES DO NOT MATCH THE HASHES FROM THE REQUIREMENTS FILE.\n"
              "    pygame==2.6.1 from https://files.pythonhosted.org/...:\n"
              "        Expected sha256 abc\n             Got        def")

    def test_kinds(self):
        self.assertEqual(inst.network_kind(self.OFFLINE), "offline")
        self.assertEqual(inst.network_kind(self.SSL), "ssl")
        self.assertEqual(inst.network_kind(self.PROXY), "proxy")
        self.assertEqual(inst.network_kind(self.TIMEOUT), "timeout")
        self.assertEqual(inst.network_kind("Temporary failure in name resolution"), "offline")
        self.assertEqual(inst.network_kind("[WinError 10060] A connection attempt failed"),
                         "timeout")
        self.assertIsNone(inst.network_kind("ERROR: No matching distribution found"))

    def test_each_kind_has_its_own_message(self):
        messages = {}
        for name in ("OFFLINE", "SSL", "PROXY", "TIMEOUT"):
            code, why = inst.classify_pip_failure(getattr(self, name))
            self.assertEqual(code, inst.EXIT_NETWORK, name)
            messages[name] = why
        self.assertEqual(len(set(messages.values())), 4)
        self.assertIn("online", messages["OFFLINE"])
        self.assertIn("IT team", messages["SSL"])
        self.assertIn("another network", messages["SSL"])
        self.assertIn("username and password", messages["PROXY"])
        self.assertIn("timed out", messages["TIMEOUT"])

    def test_proxy_message_names_the_configured_variable(self):
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://user:secret@proxy:3128"}):
            why = inst.network_help("proxy", "ProxyError")
        self.assertIn("HTTPS_PROXY", why)
        self.assertNotIn("secret", why)

    def pip(self, results):
        layout = self.layout()
        with mock.patch.object(inst, "run", side_effect=results) as run, \
                mock.patch.object(inst, "index_problem", return_value="offline"):
            try:
                inst.pip_install(layout, inst.PYGAME_SPEC)
                error = None
            except inst.InstallError as exc:
                error = exc
        return error, run

    def test_transient_failure_is_retried_once_after_a_pause(self):
        error, run = self.pip([(1, self.TIMEOUT), (0, "Successfully installed")])
        self.assertIsNone(error)
        self.assertEqual(run.call_count, 2)
        self.sleep.assert_called_once_with(inst.NETWORK_RETRY_DELAY)

    def test_persistent_offline_gives_up_after_one_retry(self):
        error, run = self.pip([(1, self.OFFLINE), (1, self.OFFLINE)])
        self.assertEqual(run.call_count, 2)
        self.assertEqual(error.code, inst.EXIT_NETWORK)
        self.assertIn("online", error.message)

    def test_certificate_errors_are_not_retried(self):
        error, run = self.pip([(1, self.SSL)])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(error.code, inst.EXIT_NETWORK)
        self.assertIn("certificate", error.message)

    def test_corrupted_download_is_retried_without_cache(self):
        error, run = self.pip([(1, self.HASHES), (0, "ok")])
        self.assertIsNone(error)
        self.assertNotIn("--no-cache-dir", run.call_args_list[0][0][0])
        retry_cmd = run.call_args_list[1][0][0]
        self.assertIn("--no-cache-dir", retry_cmd)
        self.assertEqual(retry_cmd[-1], inst.PYGAME_SPEC)
        self.sleep.assert_not_called()

    def test_corrupted_download_twice(self):
        error, run = self.pip([(1, self.HASHES), (1, "zipfile.BadZipFile: File is not a zip file")])
        self.assertEqual(error.code, inst.EXIT_NETWORK)
        self.assertIn("damaged", error.message)

    def test_pip_process_timeout(self):
        error, run = self.pip([(124, "Collecting pygame\n(timed out after 900 seconds)")])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(error.code, inst.EXIT_NETWORK)
        self.assertIn("15 minutes", error.message)

    def test_package_errors_are_not_retried(self):
        error, run = self.pip([(1, "ERROR: something odd")])
        self.assertEqual(run.call_count, 1)
        self.assertEqual(error.code, inst.EXIT_PACKAGE)

    def test_pip_upgrade_network_failure_stops_install(self):
        def pip(layout, spec, upgrade_pip=False):
            raise inst.InstallError("offline", inst.EXIT_NETWORK)
        stack, m = self.patches(pip_install=mock.patch.object(inst, "pip_install",
                                                              side_effect=pip))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_NETWORK)
        self.assertIn("[3/7] Preparing environment... FAILED", out)


class OtherPipFailureTests(unittest.TestCase):
    def test_file_in_use(self):
        code, why = inst.classify_pip_failure(
            "ERROR: Could not install packages due to an OSError: [WinError 32] The process "
            "cannot access the file because it is being used by another process")
        self.assertEqual(code, inst.EXIT_PERMISSION)
        self.assertIn("Close any running games", why)

    def test_long_paths_on_windows(self):
        code, why = inst.classify_pip_failure("OSError: [WinError 206] The filename or "
                                              "extension is too long")
        self.assertEqual(code, inst.EXIT_ENV)
        self.assertIn("PGZERO_HOME", why)

    def test_missing_pip(self):
        code, why = inst.classify_pip_failure("/venv/bin/python: No module named pip")
        self.assertEqual(code, inst.EXIT_ENV)
        self.assertIn("--recreate", why)


# --------------------------------------------------------------------------
# 5. Privileges and active environments
# --------------------------------------------------------------------------

class PrivilegeTests(TempDirTestCase):
    def test_root_is_refused(self):
        with mock.patch.object(inst.os, "geteuid", return_value=0, create=True):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.check_privileges(LINUX, env={"SUDO_USER": "sam"})
        self.assertEqual(ctx.exception.code, inst.EXIT_PERMISSION)
        self.assertIn("with sudo (you are logged in as sam)", ctx.exception.message)
        self.assertIn("--allow-root", ctx.exception.message)

    def test_allow_root(self):
        with mock.patch.object(inst.os, "geteuid", return_value=0, create=True):
            notes = inst.check_privileges(MAC, allow_root=True, env={})
        self.assertEqual(notes[0][0], "warn")
        self.assertIn("root", notes[0][1])

    def test_normal_user_has_no_notes(self):
        with mock.patch.object(inst.os, "geteuid", return_value=501, create=True):
            self.assertEqual(inst.check_privileges(MAC, env={}), [])

    def test_windows_administrator_is_only_warned(self):
        with mock.patch.object(inst, "_windows_is_admin", return_value=True):
            notes = inst.check_privileges(WINDOWS, env={"USERNAME": "sam"})
        self.assertEqual(notes[0][0], "warn")
        self.assertIn("Administrator", notes[0][1])
        self.assertIn("sam", notes[0][1])

    def test_active_environments_are_noted(self):
        with mock.patch.object(inst.os, "geteuid", return_value=501, create=True):
            conda = inst.check_privileges(MAC, env={"CONDA_PREFIX": "/opt/anaconda3"})
            venv = inst.check_privileges(LINUX, env={"VIRTUAL_ENV": "/home/s/proj/.venv"})
        self.assertEqual(conda, [("note", conda[0][1])])
        self.assertIn("conda environment", conda[0][1])
        self.assertIn("virtual environment", venv[0][1])
        self.assertIn("fine", venv[0][1])

    def test_main_refuses_root_before_touching_anything(self):
        stack, m = self.patches(geteuid=mock.patch.object(inst.os, "geteuid", return_value=0,
                                                          create=True))
        with stack:
            code, out = self.run_main([], dict(self.env(), SUDO_USER="sam"))
        self.assertEqual(code, inst.EXIT_PERMISSION)
        self.assertIn("WITHOUT sudo", out)
        m["create_venv"].assert_not_called()
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pg zero")))

    def test_main_with_allow_root_continues(self):
        stack, m = self.patches(geteuid=mock.patch.object(inst.os, "geteuid", return_value=0,
                                                          create=True))
        with stack:
            code, out = self.run_main(["--allow-root"])
        self.assertEqual(code, inst.EXIT_OK, out)

    def test_main_notes_conda(self):
        stack, m = self.patches()
        with stack:
            code, out = self.run_main([], dict(self.env(), CONDA_PREFIX="/opt/conda"),
                                      unset=("VIRTUAL_ENV",))
        self.assertEqual(code, inst.EXIT_OK, out)
        self.assertIn("Note: a conda environment is active", out)

    def test_root_owned_folder_hint(self):
        home = os.path.realpath(self.tmp)
        target = os.path.join(home, ".local", "share", "pygamezero")
        os.makedirs(target)
        real_lstat = os.lstat

        def lstat(p):
            st = real_lstat(p)
            if p == target:
                return os.stat_result((st.st_mode, st.st_ino, st.st_dev, st.st_nlink, 0,
                                       st.st_gid, st.st_size, 0, 0, 0))
            return st
        with mock.patch.object(inst.os.path, "expanduser", return_value=home), \
                mock.patch.object(inst.os, "geteuid", return_value=501, create=True), \
                mock.patch.object(inst.os, "lstat", side_effect=lstat), \
                mock.patch.object(inst.os, "name", "posix"):
            hint = inst._root_owned_hint(os.path.join(target, "venv"))
        self.assertIn("sudo chown -R", hint)
        self.assertIn("pygamezero", hint)


# --------------------------------------------------------------------------
# 6. File locks
# --------------------------------------------------------------------------

class FileLockTests(TempDirTestCase):
    def test_retries_until_the_file_is_released(self):
        func = mock.Mock(side_effect=[oserror(winerror=32), oserror(winerror=32), "done"])
        self.assertEqual(inst.retry_if_locked(func, "a"), "done")
        self.assertEqual(func.call_count, 3)
        self.assertEqual(self.sleep.call_count, 2)

    def test_gives_up_after_a_few_tries(self):
        func = mock.Mock(side_effect=oserror(winerror=32))
        with self.assertRaises(OSError):
            inst.retry_if_locked(func)
        self.assertEqual(func.call_count, len(inst.LOCK_RETRY_DELAYS) + 1)

    def test_other_errors_are_not_retried(self):
        func = mock.Mock(side_effect=oserror(errno.ENOENT))
        with self.assertRaises(OSError):
            inst.retry_if_locked(func)
        self.assertEqual(func.call_count, 1)

    def test_locked_file_message(self):
        error = inst.friendly_os_error("C:\\pgz\\venv", oserror(errno.EACCES, winerror=32))
        self.assertEqual(error.code, inst.EXIT_PERMISSION)
        self.assertIn("Close any running games", error.message)

    def test_remove_tree_retries_locked_files(self):
        layout = self.layout()
        inst.prepare_home(layout)
        os.makedirs(layout.venv)
        real_rmtree = shutil.rmtree
        calls = []

        def flaky(path, **kwargs):
            calls.append(path)
            if len(calls) == 1:
                raise oserror(errno.EACCES, winerror=32)
            real_rmtree(path, **kwargs)
        with mock.patch.object(inst.shutil, "rmtree", side_effect=flaky):
            inst.remove_tree(layout.venv, layout)
        self.assertEqual(len(calls), 2)
        self.assertFalse(os.path.exists(layout.venv))

    def test_create_venv_reports_locked_old_environment(self):
        layout = self.layout()
        inst.prepare_home(layout)
        os.makedirs(layout.venv)
        with mock.patch.object(inst, "existing_venv_ok", return_value=None), \
                mock.patch.object(inst.shutil, "rmtree",
                                  side_effect=oserror(errno.EACCES, winerror=32)):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.create_venv(layout, fake_info(), WINDOWS)
        self.assertIn("Close any running games", ctx.exception.message)

    def test_windows_shim_copy_retries(self):
        layout = self.layout(WINDOWS)
        os.makedirs(os.path.dirname(layout.venv_pgzrun))
        with open(layout.venv_pgzrun, "wb") as fh:
            fh.write(b"MZ")
        real_copy = shutil.copyfile
        attempts = []

        def flaky(src, dst):
            attempts.append(dst)
            if len(attempts) < 3:
                raise oserror(errno.EACCES, winerror=32)
            return real_copy(src, dst)
        with mock.patch.object(inst.shutil, "copyfile", side_effect=flaky):
            self.assertEqual(inst.install_shim(layout)[0], "ok")
        self.assertEqual(len(attempts), 3)


# --------------------------------------------------------------------------
# 7. Unexpected errors and the log file
# --------------------------------------------------------------------------

class CrashAndLogTests(TempDirTestCase):
    def test_unexpected_error_is_logged_with_traceback(self):
        stack, m = self.patches(install_shim=mock.patch.object(
            inst, "install_shim", side_effect=RuntimeError("kaboom")))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_ERROR)
        self.assertIn("Something unexpected went wrong", out)
        self.assertIn("RuntimeError: kaboom", out)
        self.assertIn(inst.HELP_URL, out)
        self.assertNotIn("Traceback", out)
        log = os.path.join(self.tmp, "pg zero", inst.LOG_FILE)
        self.assertIn(log, out)
        with open(log, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Traceback (most recent call last)", text)
        self.assertIn("system: Ubuntu 24.04", text)
        self.assertIn("install folder:", text)

    def test_log_has_no_secret_values(self):
        stack, m = self.patches(verify=mock.patch.object(
            inst, "verify", side_effect=inst.InstallError("bad", inst.EXIT_VERIFY)))
        env = dict(self.env(), HTTPS_PROXY="http://u:hunter2@proxy:8080", PIP_INDEX_URL=
                   "https://u:hunter2@mirror/simple", GITHUB_TOKEN="ghp_hunter2")
        with stack:
            code, out = self.run_main([], env)
        self.assertEqual(code, inst.EXIT_VERIFY)
        with open(os.path.join(self.tmp, "pg zero", inst.LOG_FILE), encoding="utf-8") as fh:
            text = fh.read()
        self.assertNotIn("hunter2", text)
        self.assertIn("HTTPS_PROXY", text)

    def test_every_failure_prints_the_log_path(self):
        stack, m = self.patches(pip_install=mock.patch.object(
            inst, "pip_install", side_effect=inst.InstallError("nope", inst.EXIT_PACKAGE,
                                                               details="ERROR: nope")))
        with stack:
            code, out = self.run_main([])
        self.assertIn("A full log was saved to:", out)
        self.assertIn("send them this file", out)

    def test_failure_before_install_folder_logs_to_temp(self):
        stack, m = self.patches(
            select_python=mock.patch.object(inst, "select_python", return_value=(None, [])),
            offer=mock.patch.object(inst, "offer_python_install", return_value=False))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_NO_PYTHON)
        logs = [n for n in os.listdir(self.tmp) if n.startswith("pygamezero-install-")]
        self.assertEqual(len(logs), 1)
        self.assertIn(os.path.join(self.tmp, logs[0]), out)
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "pg zero")))

    def test_command_output_goes_to_the_log(self):
        log = inst.InstallLog()
        log.open(self.tmp)
        with mock.patch.object(inst, "LOG", log):
            inst.run([sys.executable, "-c", "print('Collecting pygame==2.6.1')"], timeout=60)
        with open(log.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("Collecting pygame==2.6.1", text)
        self.assertIn("(exit code 0)", text)

    def test_log_buffers_until_opened_and_never_crashes(self):
        log = inst.InstallLog()
        log.write("early line")
        self.assertTrue(log.open(self.tmp))
        log.write("late line")
        with open(log.path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertIn("early line\nlate line\n", text)
        self.assertFalse(inst.InstallLog().open(os.path.join(self.tmp, "missing", "dir")))

    def test_huge_old_log_is_replaced(self):
        path = os.path.join(self.tmp, inst.LOG_FILE)
        with open(path, "w") as fh:
            fh.write("x" * (inst.LOG_MAX_BYTES + 10))
        log = inst.InstallLog()
        log.write("fresh")
        log.open(self.tmp)
        self.assertLess(os.path.getsize(path), 100)

    def test_temp_fallback_failure_is_quiet(self):
        log = inst.InstallLog()
        with mock.patch.object(inst, "fallback_log_dir",
                               return_value=os.path.join(self.tmp, "missing")):
            self.assertIsNone(log.ensure_open())


# --------------------------------------------------------------------------
# 8. Robustness: home folder, shell startup files, Windows PATH, timeouts
# --------------------------------------------------------------------------

class HomeFolderTests(TempDirTestCase):
    def test_unknown_home_folder(self):
        with mock.patch.object(inst.os.path, "expanduser", side_effect=lambda p: p):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.Layout(LINUX, env={})
            self.assertIn("PGZERO_HOME", ctx.exception.message)
            with self.assertRaises(inst.InstallError):
                inst.Layout(WINDOWS, env={})
            # Explicit folders make the home folder unnecessary.
            inst.Layout(LINUX, env={"PGZERO_HOME": "/x/pgz", "PGZERO_BIN_DIR": "/x/bin"})
            inst.Layout(WINDOWS, env={"PGZERO_HOME": "C:\\pgz"})

    @unittest.skipUnless(POSIX and not IS_ROOT, "needs POSIX permissions as a normal user")
    def test_unwritable_home_suggests_pgzero_home(self):
        locked = os.path.join(self.tmp, "locked")
        os.makedirs(locked)
        os.chmod(locked, stat.S_IRUSR | stat.S_IXUSR)
        self.addCleanup(os.chmod, locked, stat.S_IRWXU)
        layout = inst.Layout(LINUX, env={"PGZERO_HOME": os.path.join(locked, "pgz")})
        with self.assertRaises(inst.InstallError) as ctx:
            inst.prepare_home(layout)
        self.assertEqual(ctx.exception.code, inst.EXIT_PERMISSION)
        self.assertIn("PGZERO_HOME", ctx.exception.message)


class RcFileTests(TempDirTestCase):
    def block(self, rc):
        return inst.rc_block("/home/x/.local/bin", rc, "/home/x")

    def test_non_utf8_file_is_skipped_untouched(self):
        rc = os.path.join(self.tmp, ".zshrc")
        original = "# caf\xe9 settings\nalias ll='ls -l'\n".encode("latin-1")
        with open(rc, "wb") as fh:
            fh.write(original)
        layout = self.layout()
        changed, skipped = inst.ensure_unix_path(layout, env={"HOME": self.tmp,
                                                              "SHELL": "/bin/zsh"})
        self.assertEqual(changed, [])
        self.assertEqual(skipped[0][0], rc)
        self.assertIn("UTF-8", skipped[0][1])
        with open(rc, "rb") as fh:
            self.assertEqual(fh.read(), original)

    @unittest.skipUnless(POSIX and not IS_ROOT, "needs POSIX permissions as a normal user")
    def test_read_only_file_is_skipped(self):
        rc = os.path.join(self.tmp, ".bashrc")
        with open(rc, "w") as fh:
            fh.write("export A=1\n")
        os.chmod(rc, stat.S_IRUSR)
        self.addCleanup(os.chmod, rc, stat.S_IRWXU)
        with self.assertRaises(OSError) as ctx:
            inst.update_rc_file(rc, self.block(rc))
        self.assertIn("read-only", str(ctx.exception))
        with open(rc) as fh:
            self.assertEqual(fh.read(), "export A=1\n")

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_symlinked_rc_file_is_updated_through_the_link(self):
        dotfiles = os.path.join(self.tmp, "dotfiles")
        os.makedirs(dotfiles)
        target = os.path.join(dotfiles, "zshrc")
        with open(target, "w") as fh:
            fh.write("export A=1\n")
        os.chmod(target, 0o640)
        rc = os.path.join(self.tmp, ".zshrc")
        os.symlink(target, rc)
        self.assertTrue(inst.update_rc_file(rc, self.block(rc)))
        self.assertTrue(os.path.islink(rc))
        with open(target) as fh:
            self.assertIn(inst.RC_BEGIN, fh.read())
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o640)
        self.assertEqual(os.listdir(dotfiles), ["zshrc"])

    @unittest.skipUnless(POSIX, "symbolic links")
    def test_broken_symlink_is_skipped(self):
        rc = os.path.join(self.tmp, ".zshrc")
        os.symlink(os.path.join(self.tmp, "gone", "zshrc"), rc)
        with self.assertRaises(OSError):
            inst.update_rc_file(rc, self.block(rc))
        self.assertFalse(os.path.exists(os.path.join(self.tmp, "gone")))

    def test_write_failure_leaves_original_intact(self):
        rc = os.path.join(self.tmp, ".bashrc")
        with open(rc, "w") as fh:
            fh.write("export A=1\n")
        with mock.patch.object(inst.os, "replace", side_effect=oserror(errno.ENOSPC)):
            with self.assertRaises(OSError):
                inst.update_rc_file(rc, self.block(rc))
        with open(rc) as fh:
            self.assertEqual(fh.read(), "export A=1\n")
        self.assertEqual(os.listdir(self.tmp), [".bashrc"])

    def test_main_prints_manual_instructions_for_skipped_files(self):
        rc = os.path.join(self.tmp, ".bashrc")
        stack, m = self.patches(ensure_unix_path=mock.patch.object(
            inst, "ensure_unix_path", return_value=([], [(rc, "it is not a UTF-8 text file")])))
        with stack:
            code, out = self.run_main([])
        self.assertEqual(code, inst.EXIT_OK, out)
        self.assertIn("did not change %s: it is not a UTF-8 text file" % rc, out)
        self.assertIn("Add this folder to your PATH", out)
        self.assertIn('export PATH="', out)

    def test_uninstall_survives_non_utf8_rc(self):
        home = os.path.join(self.tmp, "home")
        os.makedirs(home)
        with open(os.path.join(home, ".bashrc"), "wb") as fh:
            fh.write(b"\xff\xfe junk")
        env = dict(self.env(), HOME=home)
        with mock.patch.object(inst, "detect_os", return_value=LINUX):
            code, out = self.run_main(["--uninstall"], env)
        self.assertEqual(code, inst.EXIT_OK, out)
        self.assertIn("not a UTF-8 text file", out)


class WindowsPathLengthTests(unittest.TestCase):
    def test_long_path_warns_but_is_not_truncated(self):
        sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
        from test_installer import FakeWinreg
        long_value = ";".join("C:\\Tools\\tool%03d\\bin" % i for i in range(110))
        reg = FakeWinreg(long_value, FakeWinreg.REG_EXPAND_SZ)
        warnings = []
        with mock.patch.object(inst, "_broadcast_environment_change"), \
                mock.patch.object(inst.os, "pathsep", ";"):
            self.assertTrue(inst.ensure_windows_path("C:\\pgz\\bin", reg, warnings=warnings))
        self.assertEqual(reg.store["Path"][0], long_value + ";C:\\pgz\\bin")
        self.assertEqual(len(warnings), 1)
        self.assertIn("characters long", warnings[0])

    def test_short_path_has_no_warning(self):
        from test_installer import FakeWinreg
        reg = FakeWinreg("C:\\Tools")
        warnings = []
        with mock.patch.object(inst, "_broadcast_environment_change"), \
                mock.patch.object(inst.os, "pathsep", ";"):
            inst.ensure_windows_path("C:\\pgz\\bin", reg, warnings=warnings)
        self.assertEqual(warnings, [])


class VerifyTimeoutTests(TempDirTestCase):
    def test_import_check_timeout(self):
        layout = self.layout()
        with mock.patch.object(inst, "run", return_value=(124, "(timed out after 120 seconds)")):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.verify(layout, layout.shim)
        self.assertEqual(ctx.exception.code, inst.EXIT_VERIFY)
        self.assertIn("did not finish within 120 seconds", ctx.exception.message)

    def test_test_game_timeout(self):
        layout = self.layout()
        os.makedirs(os.path.dirname(layout.venv_pgzrun))
        with open(layout.venv_pgzrun, "w") as fh:
            fh.write("")
        with mock.patch.object(inst, "run", side_effect=[(0, "2.6.1 1.2.1"),
                                                         (124, "(timed out)")]):
            with self.assertRaises(inst.InstallError) as ctx:
                inst.verify(layout, layout.shim)
        self.assertIn("The test game did not finish", ctx.exception.message)
        self.assertIn("antivirus", ctx.exception.message)

    def test_run_reports_timeouts(self):
        code, out = inst.run([sys.executable, "-c", "import time; time.sleep(5)"], timeout=0.5)
        self.assertEqual(code, 124)
        self.assertIn("timed out", out)


if __name__ == "__main__":
    unittest.main()
