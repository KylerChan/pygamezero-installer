#!/usr/bin/env python3
"""Write the GitHub repo, release tag and install.py SHA-256 into the bootstrappers.

    python tools/release.py --repo yourname/pygamezero-installer --version v1.0.0
    python tools/release.py            # refresh the hash only (keeps repo/version)
    python tools/release.py --check    # exit 1 if the stamped hash is stale

install.sh and install.ps1 download install.py from
https://raw.githubusercontent.com/<repo>/<version>/install.py and refuse to run
it unless its hash matches, so run this, commit, then create the tag.
"""

import argparse
import hashlib
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# (file, repo pattern, version pattern, hash pattern); each has one group to replace.
FIELDS = {
    "install.sh": (r'^PGZ_REPO="([^"]*)"', r'^PGZ_VERSION="([^"]*)"',
                   r'^PGZ_INSTALL_PY_SHA256="([^"]*)"'),
    "install.ps1": (r'^\$PgzRepo = "([^"]*)"', r'^\$PgzVersion = "([^"]*)"',
                    r'^\$PgzInstallPySha256 = "([^"]*)"'),
}


RAW_URL = re.compile(r"https://raw\.githubusercontent\.com/[^/\s]+/[^/\s]+/[^/\s]+/(install\.(?:sh|ps1|py))")


def update_urls(text, repo, version):
    return RAW_URL.sub(lambda m: "https://raw.githubusercontent.com/%s/%s/%s" % (
        repo, version, m.group(1)), text)


def install_py_sha256(root=ROOT):
    # Hash the exact bytes; .gitattributes keeps line endings stable.
    with open(os.path.join(root, "install.py"), "rb") as fh:
        return hashlib.sha256(fh.read()).hexdigest()


def read_field(text, pattern):
    m = re.search(pattern, text, re.M)
    if not m:
        raise SystemExit("pattern not found: %s" % pattern)
    return m.group(1)


def replace_field(text, pattern, value):
    m = re.search(pattern, text, re.M)
    if not m:
        raise SystemExit("pattern not found: %s" % pattern)
    return text[:m.start(1)] + value + text[m.end(1):]


def stamped_values(root=ROOT):
    values = {}
    for name, patterns in FIELDS.items():
        with open(os.path.join(root, name), encoding="utf-8") as fh:
            text = fh.read()
        values[name] = tuple(read_field(text, p) for p in patterns)
    return values


def main(argv=None):
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--repo", help="GitHub owner/name, e.g. myschool/pygamezero-installer")
    p.add_argument("--version", help="release tag, e.g. v1.0.0")
    p.add_argument("--check", action="store_true", help="only verify the stamped hash")
    args = p.parse_args(argv)

    digest = install_py_sha256()
    if args.check:
        stale = [n for n, (_, _, h) in stamped_values().items() if h != digest]
        if stale:
            print("Stale install.py hash in: %s\nRun: python tools/release.py" % ", ".join(stale))
            return 1
        print("Bootstrapper hashes match install.py (%s)" % digest)
        return 0

    if args.repo and not re.match(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", args.repo):
        raise SystemExit("--repo must look like owner/name")
    if args.version and not re.match(r"^[A-Za-z0-9_.-]+$", args.version):
        raise SystemExit("--version must be a plain tag name like v1.0.0")

    for name, (repo_pat, ver_pat, hash_pat) in FIELDS.items():
        path = os.path.join(ROOT, name)
        with open(path, encoding="utf-8", newline="") as fh:
            text = fh.read()
        if args.repo:
            text = replace_field(text, repo_pat, args.repo)
        if args.version:
            text = replace_field(text, ver_pat, args.version)
        text = replace_field(text, hash_pat, digest)
        repo, version = read_field(text, repo_pat), read_field(text, ver_pat)
        text = update_urls(text, repo, version)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(text)
        print("updated %s" % name)
    readme = os.path.join(ROOT, "README.md")
    if os.path.exists(readme):
        with open(readme, encoding="utf-8", newline="") as fh:
            text = fh.read()
        with open(readme, "w", encoding="utf-8", newline="") as fh:
            fh.write(update_urls(text, repo, version))
        print("updated README.md")
    print("install.py sha256: %s" % digest)
    return 0


if __name__ == "__main__":
    sys.exit(main())
