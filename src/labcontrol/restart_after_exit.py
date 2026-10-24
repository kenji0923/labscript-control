"""Inherited desktop-session helper: start once, only after the old PID exits."""
import base64
import ctypes
import json
import os
import subprocess
import sys
import time


def main():
    pid = int(sys.argv[1])
    argv = json.loads(base64.b64decode(sys.argv[2]))
    if os.name == 'nt':
        from ctypes import wintypes
        kernel = ctypes.WinDLL('kernel32', use_last_error=True)
        kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel.OpenProcess.restype = wintypes.HANDLE
        kernel.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
        kernel.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel.OpenProcess(0x00100000, False, pid)
        if not handle:
            raise RuntimeError('Cannot obtain old process handle; do not launch a duplicate')
        try:
            if kernel.WaitForSingleObject(handle, 120000) != 0:
                raise RuntimeError('Old process did not exit within 120 s')
        finally:
            kernel.CloseHandle(handle)
    else:
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.2)
        else:
            raise RuntimeError('Old process did not exit')
    subprocess.Popen(argv, close_fds=True)


if __name__ == '__main__':
    main()
