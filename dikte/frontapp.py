"""Which program is in front, by name, as far as this system will say.

Asked when a dictation starts, to pick the writing profile for it. Every answer
is best effort and "" means unknown, which the caller turns into the Default
profile and says so: a dictation is never worth failing over the question of
where it is going.

What each system offers, one function each and one chooser between them, the
way paste.py and audio.py are laid out:

  macOS     NSRunningApplication for the process id app.py already asks for,
            and the name of its executable
  Windows   the foreground window, its process, and the image file of that
            process, through user32 and kernel32
  X11       xprop: the active window from the root, then its process id, and
            the process name from /proc; the window class when there is no pid
  Wayland   no protocol tells a client which window has focus. KDE's kdotool
            can ask KWin, so it is used where it is installed; anywhere else
            the answer is unknown

The name is the program's own (thunderbird, slack, code, firefox), lower case
and without an .exe, which is what a profile lists. A browser is the browser:
which site is open in it is not asked.
"""

import ctypes
import os
import shutil
import subprocess
import sys

# Long enough for a busy X server, short enough that the key press does not
# visibly wait on it. Two calls at most on X11, one under KDE.
TIMEOUT = 0.5


def _clean(name):
    # Either separator, so a Windows path reads the same wherever this runs.
    name = (name or "").strip().replace("\\", "/").rsplit("/", 1)[-1].lower()
    if name.endswith(".exe"):
        name = name[:-4]
    return name


def _run(argv):
    try:
        res = subprocess.run(argv, capture_output=True, text=True,
                             timeout=TIMEOUT)
    except (OSError, subprocess.SubprocessError):
        return ""
    return res.stdout if res.returncode == 0 else ""


def _process_name(pid):
    """A Linux process's name, from /proc. The executable's base name is the
    better answer (comm is cut at 15 characters), with comm when it is not
    readable."""
    try:
        return os.path.basename(os.readlink(f"/proc/{int(pid)}/exe"))
    except (OSError, ValueError):
        pass
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


# --- macOS ------------------------------------------------------------------

def _macos(pid):
    from . import mac_window
    if not pid:
        pid = mac_window.frontmost_pid()
    if not pid:
        return ""
    try:
        api = mac_window._appkit()
        ask_text = api._as(ctypes.c_char_p)
        running = ctypes.c_void_p(api.ask_of_pid(
            ctypes.c_void_p(api.objc.objc_getClass(b"NSRunningApplication")),
            api.selector(b"runningApplicationWithProcessIdentifier:"),
            int(pid)))
        if not running:
            return ""
        url = ctypes.c_void_p(api.ask(running, api.selector(b"executableURL")))
        if not url:
            return ""
        component = ctypes.c_void_p(api.ask(url, api.selector(b"lastPathComponent")))
        if not component:
            return ""
        name = ask_text(component, api.selector(b"UTF8String"))
        return name.decode("utf-8", "replace") if name else ""
    except Exception:
        return ""


# --- Windows ----------------------------------------------------------------

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000


def _win_api():
    """user32 and kernel32 with the prototypes used here spelled out; the one
    function a test replaces to stand on Windows."""
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    user32.GetForegroundWindow.restype = ctypes.c_void_p
    user32.GetWindowThreadProcessId.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_ulong)]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _windows(_pid=None):
    try:
        user32, kernel32 = _win_api()
        window = user32.GetForegroundWindow()
        if not window:
            return ""
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(window, ctypes.byref(pid))
        if not pid.value:
            return ""
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, 0, pid.value)
        if not handle:
            return ""
        try:
            size = ctypes.c_ulong(1024)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer,
                                                       ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    except Exception:
        return ""


# --- Linux ------------------------------------------------------------------

def _x11(_pid=None):
    if not shutil.which("xprop"):
        return ""
    active = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    # "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007"
    window = active.rsplit("#", 1)[-1].strip().split(",")[0].strip()
    if not window.startswith("0x") or int(window, 16) == 0:
        return ""
    found = _run(["xprop", "-id", window, "_NET_WM_PID", "WM_CLASS"])
    pid, klass = "", ""
    for line in found.splitlines():
        if line.startswith("_NET_WM_PID") and "=" in line:
            pid = line.split("=", 1)[1].strip()
        elif line.startswith("WM_CLASS") and "=" in line:
            # 'WM_CLASS(STRING) = "Navigator", "firefox"': the second is the
            # class, which is the program; the first is one of its windows.
            parts = [p.strip().strip('"') for p in line.split("=", 1)[1].split(",")]
            klass = parts[-1] if parts else ""
    if pid.isdigit():
        name = _process_name(pid)
        if name:
            return name
    return klass


def _kde_wayland(_pid=None):
    """KWin through kdotool, the one Wayland compositor with a way to ask."""
    if not shutil.which("kdotool"):
        return ""
    window = _run(["kdotool", "getactivewindow"]).strip()
    if not window:
        return ""
    pid = _run(["kdotool", "getwindowpid", window]).strip()
    if pid.isdigit():
        name = _process_name(pid)
        if name:
            return name
    return _run(["kdotool", "getwindowclassname", window]).strip()


def _unknown(_pid=None):
    return ""


def backend():
    """The function that can answer on this session. Read every time, like
    paste.desktop(), so a test can say where it is standing."""
    if sys.platform == "darwin":
        return _macos
    if sys.platform == "win32":
        return _windows
    if os.environ.get("XDG_SESSION_TYPE") == "x11" or (
            os.environ.get("DISPLAY") and not os.environ.get("WAYLAND_DISPLAY")):
        return _x11
    if "kde" in os.environ.get("XDG_CURRENT_DESKTOP", "").lower():
        return _kde_wayland
    return _unknown


def program_name(pid=None):
    """The program in front, lower case and bare, or "" when it is unknown.

    `pid` is what macOS has already found out about the front, which saves
    asking twice; elsewhere it is not needed."""
    try:
        return _clean(backend()(pid))
    except Exception:
        return ""
