"""Shared test helpers: load install.py as a module without running it."""

import importlib.util
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_installer():
    spec = importlib.util.spec_from_file_location("pgz_install", os.path.join(ROOT, "install.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def fake_info(version=(3, 13, 1), path="/usr/bin/python3.13", **overrides):
    info = {
        "version": list(version),
        "implementation": "CPython",
        "executable": path,
        "path": path,
        "bits": 64,
        "platform": "linux-x86_64",
        "free_threaded": False,
        "in_venv": False,
        "has_venv": True,
        "has_ensurepip": True,
    }
    info.update(overrides)
    return info


LINUX = {"system": "Linux", "arch": "x86_64", "name": "Ubuntu 24.04", "musl": False,
         "distro_id": "ubuntu", "distro_like": "debian"}
MAC = {"system": "Darwin", "arch": "arm64", "name": "macOS 15 (Apple Silicon)", "musl": False,
       "distro_id": "", "distro_like": ""}
WINDOWS = {"system": "Windows", "arch": "x86_64", "name": "Windows 11", "musl": False,
           "distro_id": "", "distro_like": ""}
