"""Error handling tests for install.sh (the macOS / Linux bootstrapper).

Each test runs install.sh in "remote" mode (piped into bash, like
`curl ... | bash`) with a tiny PATH made of stub curl / wget / python3 scripts,
so every download failure can be simulated without a network.
"""

import hashlib
import os
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(ROOT, "install.sh")
BASH = shutil.which("bash")

CURL_STUB = r"""#!/bin/sh
echo "curl $*" >> "$STUB_LOG"
out=""; w=""
while [ $# -gt 0 ]; do
    case "$1" in
        -o) out=$2; shift ;;
        -w) w=1; shift ;;
    esac
    shift
done
if [ -n "${STUB_SLEEP:-}" ]; then exec /bin/sleep "$STUB_SLEEP"; fi
if [ -n "$out" ] && [ -n "${STUB_BODY:-}" ]; then /bin/cat "$STUB_BODY" > "$out"; fi
if [ -n "$w" ]; then
    if [ "${STUB_RC:-0}" = 0 ]; then printf '%s' "${STUB_HTTP_CODE:-200}"
    else printf '%s' "${STUB_HTTP_CODE:-000}"; fi
fi
if [ -n "${STUB_MSG:-}" ]; then echo "$STUB_MSG" >&2; fi
exit "${STUB_RC:-0}"
"""

WGET_STUB = r"""#!/bin/sh
if [ "$1" = "--help" ]; then echo "  --https-only   only follow secure HTTPS links"; exit 0; fi
echo "wget $*" >> "$STUB_LOG"
out=""
while [ $# -gt 0 ]; do
    case "$1" in -O) out=$2; shift ;; esac
    shift
done
if [ -n "$out" ] && [ -n "${STUB_BODY:-}" ]; then /bin/cat "$STUB_BODY" > "$out"; fi
if [ -n "${STUB_MSG:-}" ]; then printf '%s\n' "$STUB_MSG" >&2; fi
exit "${STUB_RC:-0}"
"""

SLEEP_STUB = '#!/bin/sh\necho "sleep $*" >> "$STUB_LOG"\nexit 0\n'

FIXTURE = 'import sys\nprint("FIXTURE_RAN " + " ".join(sys.argv[1:]))\nsys.exit({code})\n'


def write_exec(path, text):
    with open(path, "w") as fh:
        fh.write(text)
    os.chmod(path, 0o755)


@unittest.skipIf(os.name == "nt" or not BASH, "install.sh tests need a POSIX system with bash")
class BootstrapShTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pgz bootstrap ")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.bin = os.path.join(self.tmp, "stub bin")
        self.tmpdir = os.path.join(self.tmp, "tmp dir")
        os.makedirs(self.bin)
        os.makedirs(self.tmpdir)
        for tool in ("uname", "mktemp", "rm", "dirname"):
            path = shutil.which(tool)
            if path:
                os.symlink(path, os.path.join(self.bin, tool))
        write_exec(os.path.join(self.bin, "sleep"), SLEEP_STUB)
        self.log = os.path.join(self.tmp, "calls.log")
        self.body = os.path.join(self.tmp, "body file")

    # --- helpers ------------------------------------------------------------
    def real_python(self, name="python3"):
        write_exec(os.path.join(self.bin, name),
                   '#!/bin/sh\nexec "%s" "$@"\n' % sys.executable)

    def stub(self, name, text):
        write_exec(os.path.join(self.bin, name), text)

    def set_body(self, data):
        with open(self.body, "wb") as fh:
            fh.write(data)

    def fixture(self, code=0):
        """Make the 'download' a tiny install.py; return its SHA-256."""
        data = FIXTURE.format(code=code).encode()
        self.set_body(data)
        return hashlib.sha256(data).hexdigest()

    def env(self, **extra):
        env = {"HOME": self.tmp, "PATH": self.bin, "TMPDIR": self.tmpdir,
               "STUB_LOG": self.log, "STUB_BODY": self.body, "LC_ALL": "C"}
        env.update(extra)
        return env

    def run_remote(self, env, args=(), shell=None):
        """Pipe install.sh into bash, like `curl ... | bash -s -- ARGS`."""
        with open(SCRIPT, "rb") as fh:
            script = fh.read()
        cmd = [shell or BASH] + (["-s", "--"] + list(args) if args else [])
        # A new session means no /dev/tty, so the installer reads stdin.
        proc = subprocess.run(cmd, input=script, stdout=subprocess.PIPE,
                              stderr=subprocess.STDOUT, env=env, cwd=self.tmp,
                              timeout=60, start_new_session=True)
        return proc.returncode, proc.stdout.decode("utf-8", "replace")

    def calls(self, prefix):
        if not os.path.exists(self.log):
            return 0
        with open(self.log) as fh:
            return sum(1 for line in fh if line.startswith(prefix))

    def assert_tmp_clean(self):
        self.assertEqual(os.listdir(self.tmpdir), [], "temporary folder was not removed")

    # --- not bash -------------------------------------------------------------
    def test_non_bash_shells_are_told_to_use_bash(self):
        shells = [shutil.which(s) for s in ("dash", "zsh", "sh") if shutil.which(s)]
        # macOS /bin/sh is bash in POSIX mode, which runs the script fine.
        shells = [s for s in shells if subprocess.run(
            [s, "-c", '[ -z "${BASH_VERSION:-}" ]']).returncode == 0]
        if not shells:
            self.skipTest("no non-bash shell available")
        # Sandboxed HOME and a PATH without Python: even if the check were
        # missing, nothing real could be installed.
        alone = os.path.join(self.tmp, "only script")
        os.makedirs(alone)
        shutil.copy(SCRIPT, alone)
        for shell in shells:
            with self.subTest(shell=shell, mode="piped"):
                code, out = self.run_remote(self.env(), shell=shell)
                self.assertEqual(code, 1, out)
                self.assertIn("needs bash", out)
                self.assertIn("| bash", out)
            with self.subTest(shell=shell, mode="file"):
                proc = subprocess.run([shell, "install.sh"], stdout=subprocess.PIPE,
                                      stderr=subprocess.STDOUT, env=self.env(),
                                      cwd=alone, timeout=60)
                out = proc.stdout.decode()
                self.assertEqual(proc.returncode, 1, out)
                self.assertIn("bash install.sh", out)

    def test_syntax_with_system_bash(self):
        subprocess.check_call([BASH, "-n", SCRIPT])
        if os.path.exists("/bin/bash"):
            subprocess.check_call(["/bin/bash", "-n", SCRIPT])

    # --- curl failures ----------------------------------------------------------
    CURL_CASES = [
        # curl exit, HTTP code, our exit, text shown, download attempts
        (6, None, 6, "DNS lookup failed", 3),
        (7, None, 6, "Could not connect", 3),
        (28, None, 6, "timed out", 3),
        (35, None, 6, "school or work networks that inspect HTTPS", 3),
        (60, None, 6, "school or work networks that inspect HTTPS", 1),
        (22, "404", 6, "this version was removed", 1),
        (56, "404", 6, "this version was removed", 1),  # newer curl over HTTP/2
        (22, "503", 6, "having problems (error 503)", 3),
        (22, "403", 6, "refused the request (error 403)", 1),
        (5, None, 6, "proxy", 1),
        (97, None, 6, "proxy", 1),
        (18, None, 6, "interrupted", 3),
        (56, None, 6, "interrupted", 3),
        (23, None, 1, "disk may be full", 1),
        (1, None, 6, "insecure", 1),
        (99, None, 6, "Could not download the installer", 1),
    ]

    def test_curl_failures_are_explained(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        self.fixture()
        for rc, http, want_exit, text, attempts in self.CURL_CASES:
            with self.subTest(curl_exit=rc, http=http):
                if os.path.exists(self.log):
                    os.remove(self.log)
                extra = {"STUB_RC": str(rc), "STUB_MSG": "curl: (%d) simulated" % rc}
                if http:
                    extra["STUB_HTTP_CODE"] = http
                code, out = self.run_remote(self.env(**extra))
                self.assertEqual(code, want_exit, out)
                self.assertIn(text, out)
                self.assertIn("curl: (%d) simulated" % rc, out)
                self.assertEqual(self.calls("curl "), attempts, out)
                self.assertNotIn("FIXTURE_RAN", out)
                self.assert_tmp_clean()

    def test_curl_is_called_with_timeouts_and_https_only(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        self.run_remote(self.env(STUB_RC="6"))
        with open(self.log) as fh:
            line = fh.readline()
        for flag in ("--connect-timeout", "--max-time", "--proto =https", "-f"):
            self.assertIn(flag, line)

    def test_transient_failure_then_success(self):
        self.real_python()
        sha = self.fixture()
        flaky = CURL_STUB.replace(
            'exit "${STUB_RC:-0}"',
            '[ "$(/usr/bin/wc -l < "$STUB_LOG")" -ge 2 ] && exit 0\nexit 7')
        flaky = flaky.replace('if [ -n "$w" ]; then', 'if false; then')
        self.stub("curl", flaky)
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=sha))
        self.assertEqual(code, 0, out)
        self.assertIn("Trying again", out)
        self.assertIn("FIXTURE_RAN", out)

    # --- wget ---------------------------------------------------------------------
    def test_wget_failures_are_explained(self):
        self.real_python()
        self.stub("wget", WGET_STUB)
        self.fixture()
        cases = [
            (4, "", "Could not connect", 3),
            (5, "", "inspect HTTPS", 1),
            (8, "https://x/install.py:\n2025-01-01 10:00:00 ERROR 404: Not Found.",
             "this version was removed", 1),
            (3, "", "disk may be full", 1),
        ]
        for rc, msg, text, attempts in cases:
            with self.subTest(wget_exit=rc):
                if os.path.exists(self.log):
                    os.remove(self.log)
                code, out = self.run_remote(self.env(STUB_RC=str(rc), STUB_MSG=msg))
                self.assertIn(text, out)
                self.assertEqual(self.calls("wget "), attempts, out)
                self.assertNotEqual(code, 0)
                self.assert_tmp_clean()

    def test_wget_success(self):
        self.real_python()
        self.stub("wget", WGET_STUB)
        sha = self.fixture()
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=sha))
        self.assertEqual(code, 0, out)
        self.assertIn("FIXTURE_RAN", out)

    def test_no_curl_or_wget(self):
        self.real_python()
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 1, out)
        self.assertIn("neither was found", out)

    # --- bad downloads -------------------------------------------------------------
    def test_empty_download(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        self.set_body(b"")
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 6, out)
        self.assertIn("was empty", out)
        self.assertIn("log in to the Wi-Fi", out)
        self.assertNotIn("fingerprint", out)
        self.assert_tmp_clean()

    def test_html_download_is_a_captive_portal(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        self.set_body(b"\n\n<!DOCTYPE html>\n<html><head><title>Login</title></head></html>\n")
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 6, out)
        self.assertIn("web page was downloaded", out)
        self.assertIn("log in to the Wi-Fi", out)
        self.assertNotIn("fingerprint", out)

    def test_hash_mismatch_is_not_run(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        self.fixture()
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256="0" * 64))
        self.assertEqual(code, 1, out)
        self.assertIn("NOT run", out)
        self.assertNotIn("FIXTURE_RAN", out)
        self.assert_tmp_clean()

    def test_temp_folder_cannot_be_created(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        os.remove(os.path.join(self.bin, "mktemp"))
        self.stub("mktemp", "#!/bin/sh\necho 'mktemp: No space left on device' >&2\nexit 1\n")
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 1, out)
        self.assertIn("Could not create a temporary folder", out)
        self.assertEqual(self.calls("curl "), 0)

    def test_unwritable_tmpdir_falls_back(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        sha = self.fixture()
        env = self.env(PGZ_EXPECTED_SHA256=sha, TMPDIR=os.path.join(self.tmp, "missing"))
        code, out = self.run_remote(env)
        self.assertEqual(code, 0, out)
        self.assertIn("FIXTURE_RAN", out)

    # --- Python candidates ------------------------------------------------------------
    def make_broken_pythons(self):
        self.stub("python3", "#!/bin/sh\necho 'pyenv: python3: command not found' >&2\nexit 127\n")
        self.stub("python", "#!/bin/sh\nprintf 2.7\nexit 1\n")
        noexec = os.path.join(self.tmp, "not executable")
        with open(noexec, "w") as fh:
            fh.write("")
        os.chmod(noexec, 0o644)
        self.stub("python3.13", "#!%s\n" % noexec)  # exec fails: permission denied

    def test_broken_pythons_are_skipped(self):
        self.make_broken_pythons()
        self.real_python("python3.12")
        self.stub("curl", CURL_STUB)
        sha = self.fixture()
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=sha))
        self.assertEqual(code, 0, out)
        self.assertIn("FIXTURE_RAN", out)

    def test_no_working_python_lists_what_was_skipped(self):
        self.make_broken_pythons()
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 3, out)
        self.assertIn("No working Python 3", out)
        self.assertIn("pyenv: python3: command not found", out)
        self.assertIn("Python 2.7 is too old", out)
        # macOS says "permission denied", Linux "required file not found";
        # either way the interpreter must be listed as skipped.
        self.assertIn("python3.13 (", out)
        self.assertIn("Python 3.9 to 3.13", out)

    def test_no_python_at_all(self):
        code, out = self.run_remote(self.env())
        self.assertEqual(code, 3, out)
        self.assertIn("Python 3 was not found", out)

    # --- running install.py --------------------------------------------------------------
    def test_exit_codes_and_arguments_are_passed_through(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        for want in (0, 5, 8, 130):
            with self.subTest(exit=want):
                sha = self.fixture(want)
                code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=sha),
                                            args=["--yes", "two words"])
                self.assertEqual(code, want, out)
                self.assertIn("FIXTURE_RAN --yes two words", out)
                self.assert_tmp_clean()

    def test_installer_killed_by_ctrl_c_gives_130(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        data = b"import os, signal\nsignal.signal(signal.SIGINT, signal.SIG_DFL)\nos.kill(os.getpid(), signal.SIGINT)\n"
        self.set_body(data)
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=hashlib.sha256(data).hexdigest()))
        self.assertEqual(code, 130, out)

    def test_python_that_cannot_start_the_installer(self):
        # Works for the version check and hash, then fails like a missing binary.
        self.stub("python3", '#!/bin/sh\n[ "$1" = "-I" ] && exit 127\nexec "%s" "$@"\n'
                  % sys.executable)
        self.stub("curl", CURL_STUB)
        sha = self.fixture()
        code, out = self.run_remote(self.env(PGZ_EXPECTED_SHA256=sha))
        self.assertEqual(code, 127, out)
        self.assertIn("Could not start Python", out)

    def test_ctrl_c_during_download(self):
        self.real_python()
        self.stub("curl", CURL_STUB)
        with open(SCRIPT, "rb") as fh:
            script = fh.read()
        proc = subprocess.Popen([BASH], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, env=self.env(STUB_SLEEP="30"),
                                cwd=self.tmp, start_new_session=True)
        proc.stdin.write(script)
        proc.stdin.close()
        deadline = time.time() + 20
        while self.calls("curl ") == 0 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.2)
        os.killpg(proc.pid, signal.SIGINT)
        with proc.stdout:
            out = proc.stdout.read().decode()
        code = proc.wait(timeout=20)
        self.assertEqual(code, 130, out)
        self.assertIn("Cancelled. You can safely run the installer again.", out)
        self.assert_tmp_clean()


if __name__ == "__main__":
    unittest.main()
