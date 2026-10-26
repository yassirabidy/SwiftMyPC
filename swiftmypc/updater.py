"""Self-update from GitHub releases.

A release must contain SwiftMyPC.exe and SwiftMyPC.exe.sha256; the download is
only used if its SHA-256 matches. Windows can't overwrite a running .exe but
can rename it, so the update renames the running copy to .old, moves the new
one into place and restarts. The next start deletes the .old file.
"""

import hashlib
import json
import os
import re
import time
import urllib.request
from dataclasses import dataclass

from . import __version__, installer

REPO = "yassirabidy/SwiftMyPC"
RELEASES_PAGE = f"https://github.com/{REPO}/releases/latest"
_DEFAULT_API = f"https://api.github.com/repos/{REPO}/releases/latest"
# Tests may point the updater at a local server, and only at a local one.
_override = os.environ.get("SWIFTMYPC_UPDATE_API", "")
API = _override if _override.startswith("http://127.0.0.1:") else _DEFAULT_API


@dataclass
class Update:
    version: str
    notes: str
    exe_url: str
    sha_url: str
    size: int


def _version_tuple(text: str) -> tuple:
    return tuple(int(n) for n in re.findall(r"\d+", text)[:3])


def _get(url: str, timeout: float = 20):
    req = urllib.request.Request(url, headers={"User-Agent": f"SwiftMyPC/{__version__}",
                                               "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=timeout)


def check() -> Update | None:
    """Return the newer release, or None if this is the latest (or it has no checksum)."""
    with _get(API) as r:
        data = json.load(r)
    tag = data.get("tag_name", "")
    if not tag or _version_tuple(tag) <= _version_tuple(__version__):
        return None
    assets = {a.get("name"): a for a in data.get("assets", [])}
    exe, sha = assets.get(installer.EXE_NAME), assets.get(installer.EXE_NAME + ".sha256")
    if not exe or not sha:
        return None  # never install something we can't verify
    return Update(tag.lstrip("vV"), data.get("body") or "", exe["browser_download_url"],
                  sha["browser_download_url"], exe.get("size") or 0)


def can_self_update() -> bool:
    return installer.running_installed()


def download(update: Update, progress=None) -> str:
    """Download next to the installed exe and verify it. Returns the new file's path."""
    with _get(update.sha_url) as r:
        expected = r.read().decode("ascii", "replace").split()[0].strip().lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        raise ValueError("The release's checksum file is invalid. Nothing was changed.")
    target = os.path.join(installer.install_dir(), "SwiftMyPC.new.exe")
    digest, done = hashlib.sha256(), 0
    try:
        with _get(update.exe_url, timeout=60) as r, open(target, "wb") as f:
            total = int(r.headers.get("Content-Length") or update.size or 0)
            while chunk := r.read(1 << 16):
                f.write(chunk)
                digest.update(chunk)
                done += len(chunk)
                if progress:
                    progress(done, total)
    except BaseException:
        _remove(target)
        raise
    if digest.hexdigest() != expected:
        _remove(target)
        raise ValueError("The download didn't pass the safety check (SHA-256 mismatch). Nothing was changed.")
    return target


def apply(new_path: str, version: str) -> None:
    """Swap the installed exe for new_path. The caller restarts the app afterwards."""
    current = installer.installed_exe()
    old = current + ".old"
    _remove(old)
    try:
        # Renaming is allowed while the app runs, but antivirus scanning a freshly
        # written .exe can hold it for a few seconds, so retry for a while.
        _retry(lambda: os.replace(current, old))
        try:
            _retry(lambda: os.replace(new_path, current))
        except OSError:
            os.replace(old, current)  # put the working version back
            raise
    except OSError:
        _remove(new_path)
        raise
    installer.register(version)


def _retry(fn, seconds: float = 20):
    deadline = time.time() + seconds
    while True:
        try:
            return fn()
        except OSError:
            if time.time() > deadline:
                raise
            time.sleep(0.5)


def cleanup_after_update() -> None:
    old = installer.installed_exe() + ".old"
    for _ in range(10):
        if not os.path.exists(old) or _remove(old):
            return
        time.sleep(0.5)               # the previous version may still be exiting


def _remove(path: str) -> bool:
    try:
        os.remove(path)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        return False
