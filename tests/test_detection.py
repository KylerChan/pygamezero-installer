"""Operating system detection and Python interpreter selection."""

import json
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import LINUX, MAC, WINDOWS, fake_info, load_installer  # noqa: E402

inst = load_installer()


class NormalizeArchTests(unittest.TestCase):
    def test_aliases(self):
        for raw, want in [("x86_64", "x86_64"), ("AMD64", "x86_64"), ("arm64", "arm64"),
                          ("aarch64", "arm64"), ("ARM64", "arm64"), ("i686", "x86"),
                          ("", "unknown")]:
            self.assertEqual(inst.normalize_arch(raw), want, raw)


class DetectOsTests(unittest.TestCase):
    def test_windows_11_by_build_number(self):
        with mock.patch.object(inst.platform, "system", return_value="Windows"), \
                mock.patch.object(inst.platform, "version", return_value="10.0.22631"), \
                mock.patch.object(inst, "_windows_native_machine", return_value="ARM64"):
            info = inst.detect_os()
        self.assertEqual(info["name"], "Windows 11")
        self.assertEqual(info["arch"], "arm64")

    def test_windows_10(self):
        with mock.patch.object(inst.platform, "system", return_value="Windows"), \
                mock.patch.object(inst.platform, "version", return_value="10.0.19045"), \
                mock.patch.object(inst.platform, "release", return_value="10"), \
                mock.patch.object(inst, "_windows_native_machine", return_value="AMD64"):
            info = inst.detect_os()
        self.assertEqual(info["name"], "Windows 10")
        self.assertEqual(info["arch"], "x86_64")

    def test_macos_apple_silicon_even_under_rosetta(self):
        with mock.patch.object(inst.platform, "system", return_value="Darwin"), \
                mock.patch.object(inst.platform, "machine", return_value="x86_64"), \
                mock.patch.object(inst.platform, "mac_ver", return_value=("15.1", "", "")), \
                mock.patch.object(inst, "_mac_is_apple_silicon", return_value=True):
            info = inst.detect_os()
        self.assertEqual(info["arch"], "arm64")
        self.assertEqual(info["name"], "macOS 15.1 (Apple Silicon)")

    def test_macos_intel(self):
        with mock.patch.object(inst.platform, "system", return_value="Darwin"), \
                mock.patch.object(inst.platform, "mac_ver", return_value=("13.6", "", "")), \
                mock.patch.object(inst, "_mac_is_apple_silicon", return_value=False):
            info = inst.detect_os()
        self.assertEqual(info["arch"], "x86_64")
        self.assertIn("Intel", info["name"])

    def test_linux_distro_and_musl(self):
        rel = {"ID": "alpine", "PRETTY_NAME": "Alpine Linux v3.20"}
        with mock.patch.object(inst.platform, "system", return_value="Linux"), \
                mock.patch.object(inst.platform, "machine", return_value="aarch64"), \
                mock.patch.object(inst, "_read_os_release", return_value=rel), \
                mock.patch.object(inst, "_is_musl", return_value=True):
            info = inst.detect_os()
        self.assertEqual(info["name"], "Alpine Linux v3.20")
        self.assertEqual(info["arch"], "arm64")
        self.assertTrue(info["musl"])

    def test_read_os_release_parses_quotes(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", delete=False, suffix="os-release") as fh:
            fh.write('# comment\nID=ubuntu\nID_LIKE=debian\nPRETTY_NAME="Ubuntu 24.04 LTS"\n')
        try:
            rel = inst._read_os_release(fh.name)
        finally:
            os.remove(fh.name)
        self.assertEqual(rel["PRETTY_NAME"], "Ubuntu 24.04 LTS")
        self.assertEqual(rel["ID_LIKE"], "debian")

    def test_real_host_is_detected(self):
        info = inst.detect_os()
        self.assertIn(info["system"], ("Windows", "Darwin", "Linux"))
        self.assertNotEqual(info["arch"], "unknown")
        inst.check_platform_supported(info)  # CI hosts must all be supported


class PlatformSupportTests(unittest.TestCase):
    def test_unsupported_system(self):
        with self.assertRaises(inst.InstallError) as ctx:
            inst.check_platform_supported(dict(LINUX, system="FreeBSD"))
        self.assertEqual(ctx.exception.code, inst.EXIT_UNSUPPORTED_PLATFORM)

    def test_musl_rejected(self):
        with self.assertRaises(inst.InstallError) as ctx:
            inst.check_platform_supported(dict(LINUX, musl=True))
        self.assertIn("musl", ctx.exception.message)

    def test_supported(self):
        for os_info in (LINUX, MAC, WINDOWS, dict(LINUX, arch="arm64")):
            inst.check_platform_supported(os_info)


class AssessTests(unittest.TestCase):
    def check(self, info, os_info=LINUX):
        return inst.assess(info, os_info)

    def test_supported_versions(self):
        for minor in range(inst.SUPPORTED_MIN[1], inst.SUPPORTED_MAX[1] + 1):
            ok, reason = self.check(fake_info((3, minor, 0)))
            self.assertTrue(ok, reason)

    def test_too_old_and_too_new(self):
        self.assertIn("too old", self.check(fake_info((3, 8, 10)))[1])
        ok, reason = self.check(fake_info((3, 14, 0)))
        self.assertFalse(ok)
        self.assertIn("too new", reason)

    def test_rejections(self):
        cases = [
            (fake_info(implementation="PyPy"), "not CPython"),
            (fake_info(free_threaded=True), "free-threaded"),
            (fake_info(in_venv=True), "another virtual environment"),
            (fake_info(has_ensurepip=False), "venv"),
            (fake_info(has_venv=False), "venv"),
        ]
        for info, needle in cases:
            ok, reason = self.check(info)
            self.assertFalse(ok)
            self.assertIn(needle, reason)

    def test_windows_arm64_python_rejected_but_x64_ok(self):
        self.assertFalse(self.check(fake_info(platform="win-arm64"), WINDOWS)[0])
        self.assertTrue(self.check(fake_info(platform="win-amd64"), WINDOWS)[0])


class PyLauncherParsingTests(unittest.TestCase):
    def test_new_and_old_formats_with_spaces(self):
        out = (" -V:3.13 *        C:\\Users\\Ana Lee\\AppData\\Local\\Programs\\Python\\Python313\\python.exe\n"
               " -V:3.12          C:\\Program Files\\Python312\\python.exe\n"
               " -3.11-64         C:\\Python311\\python.exe\n"
               "Installed Pythons found by py Launcher for Windows\n")
        self.assertEqual(inst.parse_py_launcher_list(out), [
            "C:\\Users\\Ana Lee\\AppData\\Local\\Programs\\Python\\Python313\\python.exe",
            "C:\\Program Files\\Python312\\python.exe",
            "C:\\Python311\\python.exe",
        ])


class SelectPythonTests(unittest.TestCase):
    def fake_runner(self, table):
        def runner(cmd, **kwargs):
            info = table.get(cmd[0])
            if info is None:
                return subprocess.CompletedProcess(cmd, 1, b"", b"not found")
            return subprocess.CompletedProcess(cmd, 0, json.dumps(info).encode(), b"")
        return runner

    def test_prefers_recommended_and_skips_incompatible(self):
        table = {
            "/a/python3.15": fake_info((3, 15, 0), "/a/python3.15"),
            "/a/python3.12": fake_info((3, 12, 7), "/a/python3.12"),
            "/a/python3.13": fake_info((3, 13, 2), "/a/python3.13"),
            "/a/broken": None,
        }
        with mock.patch.object(inst, "candidate_commands", return_value=list(table)):
            chosen, rejected = inst.select_python(LINUX, runner=self.fake_runner(table))
        self.assertEqual(chosen["path"], "/a/python3.13")
        self.assertEqual([i["path"] for i, _ in rejected], ["/a/python3.15"])

    def test_handles_duplicates_of_same_interpreter(self):
        same = fake_info((3, 12, 1), "/real/python3.12")
        table = {"/a/python3": same, "/a/python3.12": same}
        with mock.patch.object(inst, "candidate_commands", return_value=list(table)):
            chosen, rejected = inst.select_python(LINUX, runner=self.fake_runner(table))
        self.assertEqual(chosen["executable"], "/real/python3.12")
        self.assertEqual(rejected, [])

    def test_no_python_at_all(self):
        with mock.patch.object(inst, "candidate_commands", return_value=[]):
            chosen, rejected = inst.select_python(LINUX, runner=self.fake_runner({}))
        self.assertIsNone(chosen)
        self.assertEqual(rejected, [])

    def test_explicit_python_that_does_not_run(self):
        chosen, rejected = inst.select_python(LINUX, explicit="/nope/python",
                                              runner=self.fake_runner({}))
        self.assertIsNone(chosen)
        self.assertIn("could not be run", rejected[0][1])

    def test_newest_patch_wins_within_minor(self):
        table = {"/a": fake_info((3, 12, 1), "/a"), "/b": fake_info((3, 12, 9), "/b")}
        with mock.patch.object(inst, "candidate_commands", return_value=list(table)):
            chosen, _ = inst.select_python(LINUX, runner=self.fake_runner(table))
        self.assertEqual(chosen["path"], "/b")

    def test_real_probe_of_current_interpreter(self):
        info = inst.probe(sys.executable)
        self.assertIsNotNone(info)
        self.assertEqual(tuple(info["version"]), tuple(sys.version_info[:3]))
        self.assertIn(info["bits"], (32, 64))


@unittest.skipIf(os.name == "nt", "POSIX candidate search")
class CandidateCommandTests(unittest.TestCase):
    def test_macos_skips_stub_python_without_command_line_tools(self):
        with mock.patch.object(inst, "_which_all",
                               side_effect=lambda n: ["/usr/bin/python3"] if n == "python3" else []), \
                mock.patch.object(inst, "_mac_has_command_line_tools", return_value=False), \
                mock.patch.object(inst.os.path, "isfile", return_value=True):
            cands = inst.candidate_commands(MAC)
        self.assertNotIn("/usr/bin/python3", cands)

    def test_macos_keeps_system_python_with_command_line_tools(self):
        with mock.patch.object(inst, "_which_all",
                               side_effect=lambda n: ["/usr/bin/python3"] if n == "python3" else []), \
                mock.patch.object(inst, "_mac_has_command_line_tools", return_value=True), \
                mock.patch.object(inst.os.path, "isfile", return_value=True):
            cands = inst.candidate_commands(MAC)
        self.assertIn("/usr/bin/python3", cands)

    def test_versioned_names_searched_newest_first(self):
        calls = []
        with mock.patch.object(inst, "_which_all", side_effect=lambda n: calls.append(n) or []):
            inst.candidate_commands(LINUX)
        self.assertLess(calls.index("python3.13"), calls.index("python3.9"))
        self.assertIn("python3", calls)


class InstallHelpTests(unittest.TestCase):
    def test_linux_missing_venv_gives_exact_package(self):
        text = inst.python_install_help(LINUX, [(fake_info((3, 12, 3), has_ensurepip=False),
                                                 "virtual environment support (venv/ensurepip) is missing")])
        self.assertIn("sudo apt update && sudo apt install python3.12-venv", text)

    def test_windows_arm_suggests_x64(self):
        text = inst.python_install_help(dict(WINDOWS, arch="arm64"), [])
        self.assertIn("--architecture x64", text)
        self.assertIn("--scope user", text)

    def test_mac_and_fedora(self):
        self.assertIn("brew install python@3.13", inst.python_install_help(MAC, []))
        fedora = dict(LINUX, distro_id="fedora", distro_like="")
        self.assertIn("sudo dnf install python3.13", inst.python_install_help(fedora, []))

    def test_unknown_linux_mentions_uv_fallback(self):
        text = inst.python_install_help(dict(LINUX, distro_id="gentoo", distro_like=""), [])
        self.assertIn("uv python install 3.13", text)


if __name__ == "__main__":
    unittest.main()
