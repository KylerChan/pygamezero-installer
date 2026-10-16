"""Error-handling tests for the PowerShell bootstrapper (install.ps1).

These run install.ps1 under PowerShell 7 (`pwsh`) with stub programs on PATH
(python, py, winget) and unreachable download URLs, so failures can be
simulated on any OS. Set PGZ_PWSH to choose the pwsh binary. Tests that need
the real internet skip themselves when it is not available.
"""

import hashlib
import http.server
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import threading
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from helpers import ROOT  # noqa: E402

SCRIPT = os.path.join(ROOT, "install.ps1")
IS_WINDOWS = os.name == "nt"
FALLBACK_PWSH = ("/private/tmp/claude-501/-Users-kylerchan-Desktop-coding-cca/"
                 "92ae8cc2-47d0-45ad-8a13-cbe75f547889/scratchpad/pwsh-dl/ps/pwsh")


def find_pwsh():
    for candidate in (os.environ.get("PGZ_PWSH"), shutil.which("pwsh"), FALLBACK_PWSH):
        if candidate and os.path.isfile(candidate) and os.access(candidate, os.X_OK):
            return candidate
    return None


PWSH = find_pwsh()
RELEASE_BASE = "https://kylerchan.github.io/pygamezero-installer/releases/v1.0.0"
# The fingerprint of install.py *as released in v1.0.0*. The script's own pin
# tracks the working copy of install.py, which differs between releases.
RELEASE_INSTALL_PY_SHA256 = "c2ac724fa82e1cab3f687f3477947aac18ad8be2dcc15dc1d3ae3c013b186337"
MISSING_BASE = "https://kylerchan.github.io/pygamezero-installer/releases/no-such-tag"
HTML_BASE = "https://kylerchan.github.io/pygamezero-installer/index.html?path="  # + "/install.py" -> the HTML home page


def ps_quote(text):
    return "'" + text.replace("'", "''") + "'"


@unittest.skipUnless(PWSH, "pwsh (PowerShell 7) not found; set PGZ_PWSH")
class Ps1TestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.class_tmp = tempfile.mkdtemp(prefix="pgz ps1 home ")
        cls.home = os.path.join(cls.class_tmp, "home dir")
        os.makedirs(cls.home)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.class_tmp, ignore_errors=True)

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pgz ps1 test ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "stub bin")
        os.makedirs(self.bin)
        self.path_dirs = [self.bin]
        self.args_file = os.path.join(self.tmp, "python args.txt")

    # -- stubs ---------------------------------------------------------------

    def make_stub(self, name, sh_body, cmd_body, directory=None):
        directory = directory or self.bin
        os.makedirs(directory, exist_ok=True)
        if IS_WINDOWS:
            path = os.path.join(directory, name + ".cmd")
            with open(path, "w", newline="\r\n") as f:
                f.write("@echo off\n" + cmd_body)
        else:
            path = os.path.join(directory, name)
            with open(path, "w") as f:
                f.write("#!/bin/sh\n" + sh_body)
            os.chmod(path, 0o755)
        return path

    def python_stub(self, name="python3", version="3.13", exit_code=0, directory=None):
        """A fake Python: answers the probe, records install.py args, exits."""
        directory = directory or self.bin
        path = os.path.join(directory, name + (".cmd" if IS_WINDOWS else ""))
        sh = ('if [ "$1" = "-c" ]; then echo "%s|%s"; exit 0; fi\n'
              'for a in "$@"; do echo "$a" >> "%s"; done\n'
              'exit %d\n') % (version, path, self.args_file, exit_code)
        cmd = ('if "%%~1"=="-c" (echo %s^|%s& exit /b 0)\n'
               ':loop\nif "%%~1"=="" goto done\necho %%~1>>"%s"\nshift\ngoto loop\n'
               ':done\nexit /b %d\n') % (version, path, self.args_file, exit_code)
        return self.make_stub(name, sh, cmd, directory)

    def recorded_args(self):
        if not os.path.exists(self.args_file):
            return None
        with open(self.args_file) as f:
            return [line.strip() for line in f if line.strip()]

    # -- running -------------------------------------------------------------

    def env(self, extra=None):
        env = {k: v for k, v in os.environ.items()
               if not k.startswith("PGZ") and k not in ("PYTHONHOME", "PYTHONPATH")}
        env["PATH"] = os.pathsep.join(self.path_dirs)
        env["HOME"] = self.home
        env["PGZERO_HOME"] = os.path.join(self.tmp, "pgz home")
        if not IS_WINDOWS:
            env.pop("LOCALAPPDATA", None)
            env.pop("ProgramFiles", None)
        env.update(extra or {})
        return env

    def copy_script(self, with_local_installer):
        app = os.path.join(self.tmp, "app dir")
        os.makedirs(app, exist_ok=True)
        dest = os.path.join(app, "install.ps1")
        shutil.copy(SCRIPT, dest)
        if with_local_installer:
            with open(os.path.join(app, "install.py"), "w") as f:
                f.write("raise SystemExit('stub python should run instead')\n")
        return dest

    def run_file(self, args=(), env=None, local=True, timeout=120):
        script = self.copy_script(local)
        cmd = [PWSH, "-NoProfile", "-NonInteractive", "-File", script] + list(args)
        proc = subprocess.run(cmd, env=self.env(env), capture_output=True, text=True,
                              timeout=timeout, cwd=self.tmp)
        proc.output = proc.stdout + proc.stderr
        return proc

    def run_iex(self, env=None, prelude="", timeout=120):
        script = self.copy_script(with_local_installer=False)
        command = ('%s Get-Content -Raw -LiteralPath %s | Invoke-Expression; '
                   'Write-Host "after=$LASTEXITCODE"') % (prelude, ps_quote(script))
        cmd = [PWSH, "-NoProfile", "-NonInteractive", "-Command", command]
        proc = subprocess.run(cmd, env=self.env(env), capture_output=True, text=True,
                              timeout=timeout, cwd=self.tmp)
        proc.output = proc.stdout + proc.stderr
        return proc

    def assertAfter(self, proc, code):
        match = re.search(r"after=(-?\d+)", proc.stdout)
        self.assertIsNotNone(match, "script ended the session early:\n" + proc.output)
        self.assertEqual(int(match.group(1)), code, proc.output)

    def assertFriendly(self, proc):
        # No raw PowerShell error records for students.
        for marker in ("CategoryInfo", "FullyQualifiedErrorId", "At line:", "InvocationInfo"):
            self.assertNotIn(marker, proc.output)

    def skip_if_offline(self, proc):
        if re.search(r"could not find|network could not be reached|Could not connect|took too long",
                     proc.output):
            self.skipTest("no internet connection")


class StaticTests(Ps1TestCase):
    def test_parses_without_errors(self):
        cmd = ('$t = $null; $e = $null; '
               '[void][System.Management.Automation.Language.Parser]::ParseFile(%s, [ref]$t, [ref]$e); '
               '$e | ForEach-Object { $_.ToString() }; "errors=$($e.Count)"') % ps_quote(SCRIPT)
        out = subprocess.run([PWSH, "-NoProfile", "-Command", cmd], capture_output=True,
                             text=True, timeout=60).stdout
        self.assertIn("errors=0", out)

    def test_preflight_is_ps2_and_clm_safe(self):
        with open(SCRIPT) as f:
            text = f.read()
        text.encode("ascii")  # PS 5.1 reads BOM-less files as ANSI
        preflight = text.split("# Finding Python")[0]
        code_lines = [l for l in preflight.splitlines() if not l.lstrip().startswith("#")]
        self.assertFalse([l for l in code_lines if "::" in l or ".GetType(" in l],
                         "no .NET calls before the language-mode check")
        body = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        for pattern in (r"\s-(in|notin|shl|shr)\s", r"\?\?", r"&&", r"\|\|",
                        r"^\s*class\s", r"-Parallel"):
            self.assertIsNone(re.search(pattern, body, re.M), pattern)

    def test_release_lines_keep_their_format(self):
        with open(SCRIPT) as f:
            text = f.read()
        self.assertRegex(text, r'(?m)^\$PgzSite = "[^"]+"$')
        self.assertRegex(text, r'(?m)^\$PgzVersion = "[^"]+"$')
        self.assertRegex(text, r'(?m)^\$PgzInstallPySha256 = "[0-9a-f]{64}"$')


class ExitCodeTests(Ps1TestCase):
    def test_exit_codes_from_install_py_are_propagated(self):
        for code in (0, 5, 130):
            with self.subTest(code=code):
                self.python_stub(exit_code=code)
                proc = self.run_file()
                self.assertEqual(proc.returncode, code, proc.output)
                self.assertIn("Using local installer", proc.output)

    def test_options_are_forwarded(self):
        self.python_stub(exit_code=0)
        proc = self.run_file(["-Uninstall", "-Recreate", "-NoModifyPath", "-Yes"])
        self.assertEqual(proc.returncode, 0, proc.output)
        args = self.recorded_args()
        self.assertEqual(args[0], "-I")
        self.assertTrue(args[1].endswith("install.py"))
        self.assertEqual(args[2:], ["--uninstall", "--recreate", "--no-modify-path", "--yes"])

    def test_unexpected_python_exit_code_is_explained(self):
        self.python_stub(exit_code=77)
        proc = self.run_file()
        self.assertEqual(proc.returncode, 77, proc.output)
        self.assertIn("Python stopped unexpectedly (exit code 77", proc.output)


class PythonDiscoveryTests(Ps1TestCase):
    def test_unusable_pythons_are_skipped_and_listed(self):
        self.make_stub("py", 'echo "No suitable Python runtime found" >&2\nexit 103\n',
                       "echo No suitable Python runtime found 1>&2\nexit /b 103\n")
        self.python_stub(name="python", version="2.7")
        store = os.path.join(self.tmp, "WindowsApps")
        self.make_stub("python3", 'echo "Python was not found; run without arguments" >&2\nexit 49\n',
                       "echo Python was not found 1>&2\nexit /b 9009\n", directory=store)
        self.path_dirs.append(store)
        proc = self.run_file()
        self.assertEqual(proc.returncode, 3, proc.output)
        out = proc.output
        self.assertIn("Python 3 was not found", out)
        self.assertIn("py launcher", out)
        self.assertIn("no working Python 3 behind it", out)
        self.assertIn("Python 2.7 is too old", out)
        self.assertIn("Microsoft Store shortcut", out)
        self.assertIn("winget (the Windows package manager) is not available", out)
        self.assertIn("python.org", out)
        self.assertFriendly(proc)

    @unittest.skipIf(IS_WINDOWS, "uses a Unix script with a missing interpreter")
    def test_python_that_cannot_start_is_skipped(self):
        path = os.path.join(self.bin, "python3")
        with open(path, "w") as f:
            f.write("#!/nonexistent/interpreter\n")
        os.chmod(path, 0o755)
        proc = self.run_file()
        self.assertEqual(proc.returncode, 3, proc.output)
        self.assertIn("could not be started", proc.output)
        self.assertFriendly(proc)

    def test_working_python_wins_over_broken_ones(self):
        self.python_stub(name="python", version="2.7")
        self.python_stub(name="python3", exit_code=0)
        proc = self.run_file()
        self.assertEqual(proc.returncode, 0, proc.output)


class WingetTests(Ps1TestCase):
    def winget_stub(self, exit_code, creates_python=False):
        marker = os.path.join(self.tmp, "winget ran.txt")
        sh = 'echo ran > "%s"\n' % marker
        cmd = 'echo ran> "%s"\n' % marker
        if creates_python:
            target = os.path.join(self.tmp, "python template")
            os.makedirs(target)
            stub = self.python_stub(name="python3", exit_code=0, directory=target)
            dest = os.path.join(self.bin, os.path.basename(stub))
            # The stub reports its own path, so rewrite it for the new location.
            with open(stub) as f:
                content = f.read().replace(stub, dest)
            with open(stub, "w") as f:
                f.write(content)
            sh += '/bin/cp "%s" "%s"\n' % (stub, dest)
            cmd += 'copy "%s" "%s" >nul\n' % (stub, dest)
        sh += "exit %d\n" % exit_code
        cmd += "exit /b %d\n" % exit_code
        self.make_stub("winget", sh, cmd)
        return marker

    def test_noninteractive_window_is_treated_as_no(self):
        marker = self.winget_stub(0)
        proc = self.run_file()
        self.assertEqual(proc.returncode, 3, proc.output)
        self.assertIn("cannot ask questions", proc.output)
        self.assertIn("winget install --id Python.Python.3.13", proc.output)
        self.assertFalse(os.path.exists(marker), "winget must not run without consent")
        self.assertFriendly(proc)

    def test_winget_failure_shows_exit_code(self):
        self.winget_stub(1)
        proc = self.run_file(["-Yes"])
        self.assertEqual(proc.returncode, 3, proc.output)
        self.assertIn("winget could not install Python (exit code 1, 0x00000001)", proc.output)
        self.assertIn("python.org", proc.output)

    @unittest.skipIf(os.name == "nt", "the script reloads PATH from the real Windows registry, "
                     "which finds the runner's real Pythons instead of the stub")
    def test_python_installed_by_winget_is_used(self):
        self.winget_stub(0, creates_python=True)
        proc = self.run_file(["-Yes"])
        self.assertEqual(proc.returncode, 0, proc.output)
        self.assertIn("--yes", self.recorded_args())

    @unittest.skipIf(os.name == "nt", "the script reloads PATH from the real Windows registry, "
                     "which finds the runner's real Pythons instead of the stub")
    def test_python_installed_but_not_visible_yet(self):
        self.winget_stub(0)
        proc = self.run_file(["-Yes"])
        self.assertEqual(proc.returncode, 3, proc.output)
        self.assertIn("cannot see it yet", proc.output)
        self.assertIn("open a new PowerShell window", proc.output)


class DownloadTests(Ps1TestCase):
    def setUp(self):
        super().setUp()
        self.python_stub(exit_code=5)

    def test_connection_refused_is_retried_then_explained(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": "https://127.0.0.1:9"}, local=False)
        self.assertEqual(proc.returncode, 6, proc.output)
        self.assertIn("trying again in 2 seconds (attempt 3 of 3)", proc.output)
        self.assertIn("Could not connect to 127.0.0.1", proc.output)
        self.assertIsNone(self.recorded_args())
        self.assertFriendly(proc)

    def test_unknown_host_is_explained(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": "https://nonexistent.invalid"},
                             local=False)
        self.assertEqual(proc.returncode, 6, proc.output)
        self.assertIn("could not find nonexistent.invalid", proc.output)
        self.assertIn("internet connection", proc.output)
        self.assertFriendly(proc)

    def test_insecure_url_is_refused(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": "http://example.com"}, local=False)
        self.assertEqual(proc.returncode, 1, proc.output)
        self.assertIn("Refusing to download over an insecure connection", proc.output)

    @unittest.skipUnless(shutil.which("openssl"), "openssl not found")
    def test_untrusted_certificate_is_explained(self):
        cert = os.path.join(self.tmp, "cert.pem")
        key = os.path.join(self.tmp, "key.pem")
        subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                        "-subj", "/CN=127.0.0.1", "-keyout", key, "-out", cert],
                       check=True, capture_output=True)
        server = http.server.HTTPServer(("127.0.0.1", 0), http.server.SimpleHTTPRequestHandler)
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(cert, key)
        server.socket = context.wrap_socket(server.socket, server_side=True)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        base = "https://127.0.0.1:%d" % server.server_address[1]
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": base}, local=False)
        self.assertEqual(proc.returncode, 6, proc.output)
        self.assertIn("secure (HTTPS) connection", proc.output)
        self.assertIn("try a different network", proc.output)
        self.assertNotIn("trying again", proc.output)
        self.assertFriendly(proc)

    def test_missing_release_gives_404_message(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": MISSING_BASE}, local=False)
        self.skip_if_offline(proc)
        self.assertEqual(proc.returncode, 6, proc.output)
        self.assertIn("was not found there (HTTP 404)", proc.output)
        self.assertNotIn("trying again", proc.output)

    def test_web_page_instead_of_installer(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": HTML_BASE}, local=False)
        self.skip_if_offline(proc)
        self.assertEqual(proc.returncode, 6, proc.output)
        self.assertIn("returned a web page instead of the installer", proc.output)
        self.assertIn("NOT run", proc.output)
        self.assertNotIn("fingerprint", proc.output)

    def test_hash_mismatch_is_not_run(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": RELEASE_BASE,
                                  "PGZ_EXPECTED_SHA256": "0" * 64}, local=False)
        self.skip_if_offline(proc)
        self.assertEqual(proc.returncode, 1, proc.output)
        self.assertIn("did not match its expected fingerprint", proc.output)
        self.assertIsNone(self.recorded_args())

    def test_verified_download_runs_python(self):
        proc = self.run_file(env={"PGZ_INSTALLER_BASE_URL": RELEASE_BASE,
                                  "PGZ_EXPECTED_SHA256": RELEASE_INSTALL_PY_SHA256},
                             local=False)
        self.skip_if_offline(proc)
        self.assertEqual(proc.returncode, 5, proc.output)
        installer = self.recorded_args()[1]
        self.assertFalse(os.path.exists(installer), "temporary download must be cleaned up")


class IexModeTests(Ps1TestCase):
    """`irm | iex` must never close the window, so the session must continue."""

    def test_no_python_keeps_session_open(self):
        proc = self.run_iex()
        self.assertAfter(proc, 3)
        self.assertIn("Python 3 was not found", proc.output)
        self.assertFriendly(proc)

    def test_download_failure_keeps_session_open(self):
        self.python_stub(exit_code=0)
        proc = self.run_iex(env={"PGZ_INSTALLER_BASE_URL": "https://nonexistent.invalid"})
        self.assertAfter(proc, 6)
        self.assertFriendly(proc)

    def test_exit_code_from_downloaded_installer(self):
        self.python_stub(exit_code=5)
        proc = self.run_iex(env={"PGZ_INSTALLER_BASE_URL": RELEASE_BASE,
                                 "PGZ_EXPECTED_SHA256": RELEASE_INSTALL_PY_SHA256})
        self.skip_if_offline(proc)
        self.assertAfter(proc, 5)

    def test_unexpected_error_is_short_and_keeps_session_open(self):
        # An alias beats the real cmdlet, so Find-Python hits a terminating error.
        proc = self.run_iex(prelude="function Fail-Now { throw 'simulated failure' }; "
                                    "Set-Alias Get-Command Fail-Now;")
        self.assertAfter(proc, 1)
        self.assertIn("something unexpected went wrong", proc.output)
        self.assertIn("simulated failure", proc.output)
        self.assertFriendly(proc)

    def test_constrained_language_mode_is_detected(self):
        self.python_stub(exit_code=0)
        proc = self.run_iex(prelude="$ExecutionContext.SessionState.LanguageMode = 'ConstrainedLanguage';")
        self.assertAfter(proc, 7)
        self.assertIn("'ConstrainedLanguage' mode", proc.output)
        self.assertIn("py install.py", proc.output)
        self.assertIsNone(self.recorded_args())
        self.assertFriendly(proc)


class FunctionTests(Ps1TestCase):
    """Calls the script's helper functions directly (loaded from its syntax tree)."""

    LOADER = r"""
$ErrorActionPreference = 'Stop'
$PgzSite = 'https://kylerchan.github.io/pygamezero-installer'
$tokens = $null; $errors = $null
$ast = [System.Management.Automation.Language.Parser]::ParseFile(%s, [ref]$tokens, [ref]$errors)
foreach ($f in $ast.FindAll({ param($n) $n -is [System.Management.Automation.Language.FunctionDefinitionAst] }, $false)) {
    . ([scriptblock]::Create($f.Extent.Text))
}
function Out-Result($Name, $Value) { Write-Output ("RESULT " + (@{ name = $Name; value = $Value } | ConvertTo-Json -Compress)) }
function Kind($Exception) { $p = Get-PgzDownloadProblem ([pscustomobject]@{ Exception = $Exception }); "$($p.Kind)|$($p.Status)|$($p.Retry)" }
"""

    def run_functions(self, body):
        script = self.LOADER % ps_quote(SCRIPT) + body
        path = os.path.join(self.tmp, "functions test.ps1")
        with open(path, "w") as f:
            f.write(script)
        proc = subprocess.run([PWSH, "-NoProfile", "-NonInteractive", "-File", path],
                              capture_output=True, text=True, timeout=120)
        results = {}
        for line in proc.stdout.splitlines():
            if line.startswith("RESULT "):
                item = json.loads(line[len("RESULT "):])
                results[item["name"]] = item["value"]
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        return results

    def test_download_error_classification(self):
        body = r"""
Out-Result dns (Kind ([System.Net.WebException]::new('x', [System.Net.WebExceptionStatus]::NameResolutionFailure)))
Out-Result wtimeout (Kind ([System.Net.WebException]::new('x', [System.Net.WebExceptionStatus]::Timeout)))
Out-Result trust (Kind ([System.Net.WebException]::new('x', [System.Net.WebExceptionStatus]::TrustFailure)))
Out-Result proxyname (Kind ([System.Net.WebException]::new('x', [System.Net.WebExceptionStatus]::ProxyNameResolutionFailure)))
# HttpClient exception types only exist by default in PowerShell 7.
if ($PSVersionTable.PSVersion.Major -ge 6) {
    Out-Result auth (Kind ([System.Net.Http.HttpRequestException]::new('The SSL connection could not be established', [System.Security.Authentication.AuthenticationException]::new('remote certificate is invalid'))))
    Out-Result canceled (Kind ([System.Threading.Tasks.TaskCanceledException]::new('The request was canceled due to the configured HttpClient.Timeout of 30 seconds elapsing.')))
    Out-Result refused (Kind ([System.Net.Http.HttpRequestException]::new('Connection refused', [System.Net.Sockets.SocketException]::new([int][System.Net.Sockets.SocketError]::ConnectionRefused))))
    Out-Result s404 (Kind ([System.Net.Http.HttpRequestException]::new('x', $null, [System.Net.HttpStatusCode]::NotFound)))
    Out-Result s407 (Kind ([System.Net.Http.HttpRequestException]::new('x', $null, [System.Net.HttpStatusCode]::ProxyAuthenticationRequired)))
    Out-Result s503 (Kind ([System.Net.Http.HttpRequestException]::new('x', $null, [System.Net.HttpStatusCode]::ServiceUnavailable)))
    Out-Result s403 (Kind ([System.Net.Http.HttpRequestException]::new('x', $null, [System.Net.HttpStatusCode]::Forbidden)))
    Out-Result s400 (Kind ([System.Net.Http.HttpRequestException]::new('x', $null, [System.Net.HttpStatusCode]::BadRequest)))
}
# Windows PowerShell 5.1 shape: WebException.Response.StatusCode
$ps51 = [pscustomobject]@{ Message = 'The remote server returned an error: (407) Proxy Authentication Required.'; InnerException = $null; Response = [pscustomobject]@{ StatusCode = [System.Net.HttpStatusCode]::ProxyAuthenticationRequired } }
Out-Result ps51_407 (Kind $ps51)
Out-Result msg404 (Kind ([System.Exception]::new('The remote server returned an error: (404) Not Found.')))
$disk = [System.IO.IOException]::new('There is not enough space on the disk.', -2147024784)
Out-Result disk (Kind $disk)
Out-Result other (Kind ([System.Exception]::new('something odd')))
"""
        r = self.run_functions(body)
        self.assertEqual(r["dns"], "dns||True")
        self.assertEqual(r["wtimeout"], "timeout||True")
        self.assertEqual(r["trust"], "tls||False")
        self.assertEqual(r["proxyname"], "proxy||False")
        if "auth" in r:  # PowerShell 7 only
            self.assertEqual(r["auth"], "tls||False")
            self.assertEqual(r["canceled"], "timeout||True")
            self.assertEqual(r["refused"], "connect||True")
            self.assertEqual(r["s404"], "notfound|404|False")
            self.assertEqual(r["s407"], "proxy|407|False")
            self.assertEqual(r["s503"], "server|503|True")
            self.assertEqual(r["s403"], "blocked|403|False")
            self.assertEqual(r["s400"], "http|400|False")
        self.assertEqual(r["ps51_407"], "proxy|407|False")
        self.assertEqual(r["msg404"], "notfound|404|False")
        self.assertEqual(r["disk"], "disk||False")
        self.assertEqual(r["other"], "other||False")

    def test_download_content_checks(self):
        empty = os.path.join(self.tmp, "empty.py")
        open(empty, "w").close()
        html = os.path.join(self.tmp, "portal.py")
        with open(html, "w") as f:
            f.write("\n  <!DOCTYPE html>\n<html><head><title>Sign in to Wi-Fi</title></head>\n")
        body = ("Out-Result empty (Get-PgzDownloadContentProblem %s)\n"
                "Out-Result html (Get-PgzDownloadContentProblem %s)\n"
                "Out-Result real (Get-PgzDownloadContentProblem %s)\n"
                "Out-Result missing (Get-PgzDownloadContentProblem %s)\n") % (
            ps_quote(empty), ps_quote(html), ps_quote(os.path.join(ROOT, "install.py")),
            ps_quote(os.path.join(self.tmp, "nope.py")))
        r = self.run_functions(body)
        self.assertEqual(r, {"empty": "empty", "html": "html", "real": "", "missing": "empty"})

    def test_sha256_with_and_without_get_filehash(self):
        target = os.path.join(self.tmp, "data file.bin")
        with open(target, "wb") as f:
            f.write(os.urandom(5000))
        with open(target, "rb") as f:
            expected = hashlib.sha256(f.read()).hexdigest()
        body = ("Out-Result normal (Get-PgzSha256 %s)\n"
                "function Get-FileHash { throw 'not available' }\n"
                "Out-Result fallback (Get-PgzSha256 %s)\n") % (ps_quote(target), ps_quote(target))
        r = self.run_functions(body)
        self.assertEqual(r["normal"], expected)
        self.assertEqual(r["fallback"], expected)

    def test_winget_hints(self):
        body = ("Out-Result nopkg (Get-PgzWingetHint -1978335212)\n"
                "Out-Result policy (Get-PgzWingetHint -1978334961)\n"
                "Out-Result gpo (Get-PgzWingetHint -1978335174)\n"
                "Out-Result unknown (Get-PgzWingetHint 1)\n")
        r = self.run_functions(body)
        self.assertIn("No packages found", r["nopkg"])
        self.assertIn("do not allow installing apps", r["policy"])
        self.assertIn("Group Policy", r["gpo"])
        self.assertIsNone(r["unknown"])


if __name__ == "__main__":
    unittest.main()
