"""Find and remove junk files from well-known safe locations.

Only files inside the listed folders are touched. Files that are in use are
skipped, symlinks/junctions are never followed, and temp folders keep anything
modified recently so running programs aren't disturbed.
"""

import ctypes
import glob
import os
import time
from ctypes import wintypes
from dataclasses import dataclass, field

LOCAL = os.environ.get("LOCALAPPDATA", "")
WINDIR = os.environ.get("WINDIR", r"C:\Windows")


@dataclass
class Target:
    key: str
    label: str
    description: str
    paths: list = field(default_factory=list)
    min_age_hours: float = 0
    needs_admin: bool = False
    recycle_bin: bool = False


def _chromium_caches(user_data: str) -> list[str]:
    paths = []
    for profile in glob.glob(os.path.join(user_data, "*")):
        for sub in (r"Cache\Cache_Data", "Code Cache", "GPUCache"):
            p = os.path.join(profile, sub)
            if os.path.isdir(p):
                paths.append(p)
    return paths


def browser_caches() -> dict[str, tuple[str, list[str]]]:
    """Browser name -> (its process name, cache folders)."""
    out = {}
    for name, exe, rel in (("Chrome", "chrome.exe", r"Google\Chrome\User Data"),
                           ("Edge", "msedge.exe", r"Microsoft\Edge\User Data"),
                           ("Brave", "brave.exe", r"BraveSoftware\Brave-Browser\User Data")):
        out[name] = (exe, _chromium_caches(os.path.join(LOCAL, rel)))
    out["Firefox"] = ("firefox.exe", glob.glob(os.path.join(LOCAL, r"Mozilla\Firefox\Profiles\*\cache2")))
    return out


def targets() -> list[Target]:
    browsers = [p for _, paths in browser_caches().values() for p in paths]

    return [
        Target("user_temp", "Your temporary files",
               "Leftovers from installers and apps (older than 1 day).",
               [os.environ.get("TEMP", os.path.join(LOCAL, "Temp"))], min_age_hours=24),
        Target("win_temp", "Windows temporary files",
               "System temp folder (older than 1 day). Needs admin for most files.",
               [os.path.join(WINDIR, "Temp")], min_age_hours=24, needs_admin=True),
        Target("browser", "Browser caches",
               "Chrome, Edge, Brave and Firefox caches. Close browsers first; you stay logged in.",
               browsers),
        Target("crash", "Crash dumps & error reports",
               "Memory dumps and reports left by crashed apps.",
               [os.path.join(LOCAL, "CrashDumps"),
                os.path.join(LOCAL, r"Microsoft\Windows\WER\ReportArchive"),
                os.path.join(LOCAL, r"Microsoft\Windows\WER\ReportQueue")]),
        Target("wu_cache", "Windows Update downloads",
               "Already-installed update files (older than 7 days). Needs admin.",
               [os.path.join(WINDIR, r"SoftwareDistribution\Download")],
               min_age_hours=24 * 7, needs_admin=True),
        Target("recycle", "Recycle Bin",
               "Permanently deletes everything in the Recycle Bin.", recycle_bin=True),
    ]


def _walk_files(root: str, min_age_hours: float):
    """Yield (path, size) for files under root older than min_age_hours."""
    cutoff = time.time() - min_age_hours * 3600
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            continue
        with it:
            for e in it:
                try:
                    if e.is_symlink() or e.is_junction():
                        continue
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    st = e.stat(follow_symlinks=False)
                except OSError:
                    continue
                if st.st_mtime <= cutoff:
                    yield e.path, st.st_size


# --- Recycle Bin via shell32 ---------------------------------------------------

class _SHQUERYRBINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("i64Size", ctypes.c_longlong),
                ("i64NumItems", ctypes.c_longlong)]


def _recycle_bin_size() -> tuple[int, int]:
    info = _SHQUERYRBINFO(ctypes.sizeof(_SHQUERYRBINFO), 0, 0)
    if ctypes.windll.shell32.SHQueryRecycleBinW(None, ctypes.byref(info)) != 0:
        return 0, 0
    return info.i64Size, info.i64NumItems


def _empty_recycle_bin() -> None:
    SHERB_NOCONFIRMATION, SHERB_NOPROGRESSUI, SHERB_NOSOUND = 0x1, 0x2, 0x4
    ctypes.windll.shell32.SHEmptyRecycleBinW(
        None, None, SHERB_NOCONFIRMATION | SHERB_NOPROGRESSUI | SHERB_NOSOUND)


# --- public API ----------------------------------------------------------------

def scan(target: Target) -> tuple[int, int]:
    """Returns (bytes, file_count) that would be cleaned."""
    if target.recycle_bin:
        return _recycle_bin_size()
    total = count = 0
    for root in target.paths:
        for _, size in _walk_files(root, target.min_age_hours):
            total += size
            count += 1
    return total, count


def clean(target: Target) -> tuple[int, int, int]:
    """Deletes the target's files. Returns (bytes_freed, files_deleted, files_skipped)."""
    if target.recycle_bin:
        size, count = _recycle_bin_size()
        _empty_recycle_bin()
        left, _ = _recycle_bin_size()
        return size - left, count, 0
    freed = deleted = skipped = 0
    for root in target.paths:
        for path, size in list(_walk_files(root, target.min_age_hours)):
            try:
                os.remove(path)
                freed += size
                deleted += 1
            except OSError:
                skipped += 1  # in use or no permission
        _remove_empty_dirs(root)
    return freed, deleted, skipped


def _remove_empty_dirs(root: str) -> None:
    """Remove empty sub-folders (never root itself)."""
    for d, _, _ in sorted(os.walk(root, topdown=False), key=lambda t: -len(t[0])):
        if os.path.normcase(d) == os.path.normcase(root) or os.path.islink(d):
            continue
        try:
            os.rmdir(d)  # only succeeds when empty
        except OSError:
            pass
