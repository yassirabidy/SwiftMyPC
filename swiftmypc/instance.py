"""Only one SwiftMyPC at a time; starting a second one shows the first.
The installer/uninstaller can also ask the running copy to quit."""

import ctypes
import time

from .installer import SANDBOX_SUFFIX


class SingleInstance:
    MUTEX = f"Local\\SwiftMyPC{SANDBOX_SUFFIX}.Instance"
    EVENT = f"Local\\SwiftMyPC{SANDBOX_SUFFIX}.Show"
    QUIT_EVENT = f"Local\\SwiftMyPC{SANDBOX_SUFFIX}.Quit"

    def __init__(self):
        k = self.kernel32 = ctypes.windll.kernel32
        k.CreateMutexW.restype = k.CreateEventW.restype = ctypes.c_void_p
        k.OpenEventW.restype = k.OpenMutexW.restype = ctypes.c_void_p
        self.mutex = self.event = self.quit_event = None

    def acquire(self) -> bool:
        k = self.kernel32
        self.mutex = k.CreateMutexW(None, False, self.MUTEX)
        if k.GetLastError() in (183, 5):  # already exists / owned by an elevated copy
            k.CloseHandle(ctypes.c_void_p(self.mutex))
            self.mutex = None
            return False
        self.event = k.CreateEventW(None, False, False, self.EVENT)
        self.quit_event = k.CreateEventW(None, False, False, self.QUIT_EVENT)
        return True

    def signal_existing(self) -> bool:
        ev = self.kernel32.OpenEventW(0x0002, False, self.EVENT)  # EVENT_MODIFY_STATE
        if not ev:
            return False
        self.kernel32.SetEvent(ctypes.c_void_p(ev))
        self.kernel32.CloseHandle(ctypes.c_void_p(ev))
        return True

    def other_running(self) -> bool:
        h = self.kernel32.OpenMutexW(0x00100000, False, self.MUTEX)  # SYNCHRONIZE
        if h:
            self.kernel32.CloseHandle(ctypes.c_void_p(h))
            return True
        return self.kernel32.GetLastError() == 5  # exists, but owned by an elevated copy

    def ask_other_to_quit(self, timeout: float = 30.0) -> bool:
        """Ask the running copy to quit and wait until it has. False if it didn't."""
        ev = self.kernel32.OpenEventW(0x0002, False, self.QUIT_EVENT)
        if not ev:
            return not self.other_running()
        self.kernel32.SetEvent(ctypes.c_void_p(ev))
        self.kernel32.CloseHandle(ctypes.c_void_p(ev))
        deadline = time.time() + timeout
        while time.time() < deadline:
            if not self.other_running():
                time.sleep(1.5)  # let the process finish exiting so its .exe is unlocked
                return True
            time.sleep(0.2)
        return False

    def _signaled(self, handle) -> bool:
        return bool(handle) and self.kernel32.WaitForSingleObject(ctypes.c_void_p(handle), 0) == 0

    def show_requested(self) -> bool:
        return self._signaled(self.event)

    def quit_requested(self) -> bool:
        return self._signaled(self.quit_event)

    def release(self):
        for h in (self.mutex, self.event, self.quit_event):
            if h:
                self.kernel32.CloseHandle(ctypes.c_void_p(h))
        self.mutex = self.event = self.quit_event = None
