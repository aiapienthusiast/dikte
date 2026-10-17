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
and without an .exe, which is what a profile lists.

For a browser, the window title can be read as well, so that a profile can be
chosen by the site in the tab: front() takes a `title_for` test and reads the
title only for a program it says yes to. Each backend does that in at most one
more call: X11 asks for the title in the request it already makes, KDE adds
one kdotool call, Windows one GetWindowTextW. macOS would need the Screen
Recording permission for other programs' titles, so it reads none. A title is
returned to the caller and nothing else: it is never logged or kept here.
"""

import ctypes
import os
import shutil
import subprocess
import sys

# Each call's limit: long enough for a busy X server. Two calls at most on
# X11 and three under KDE, all on a thread of their own (see app.py).
TIMEOUT = 0.5


def _clean(name):
    # Either separator, so a Windows path reads the same wherever this runs.
    name = _undeleted((name or "").strip())
    name = name.replace("\\", "/").rsplit("/", 1)[-1].lower()
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


_DELETED = " (deleted)"


def _undeleted(name):
    """What /proc says of a program upgraded while it ran: "thunderbird
    (deleted)", the file it was started from being gone."""
    return name[:-len(_DELETED)] if name.endswith(_DELETED) else name


def _process_name(pid):
    """A Linux process's name, from /proc: the executable's base name, or comm
    (cut at 15 characters) when that is not readable. Only the fallback: a
    window's class names the program better, since this says "electron",
    "python3.14" or "java" for whatever runs on one of those."""
    try:
        return _undeleted(os.path.basename(os.readlink(f"/proc/{int(pid)}/exe")))
    except (OSError, ValueError):
        pass
    try:
        with open(f"/proc/{int(pid)}/comm", encoding="utf-8") as fh:
            return fh.read().strip()
    except (OSError, ValueError):
        return ""


# --- macOS ------------------------------------------------------------------

def _macos(pid, title_for=None):
    """The name only: no title on macOS (see the module docstring)."""
    name = _macos_name(pid)
    return name if title_for is None else (name, "")


def _macos_name(pid):
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
    user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
    user32.GetWindowTextW.argtypes = [ctypes.c_void_p, ctypes.c_wchar_p, ctypes.c_int]
    kernel32.OpenProcess.restype = ctypes.c_void_p
    kernel32.OpenProcess.argtypes = [ctypes.c_ulong, ctypes.c_int, ctypes.c_ulong]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        ctypes.c_void_p, ctypes.c_ulong, ctypes.c_wchar_p,
        ctypes.POINTER(ctypes.c_ulong)]
    kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
    return user32, kernel32


def _windows(_pid=None, title_for=None):
    name, title = "", ""
    try:
        user32, kernel32 = _win_api()
        window = user32.GetForegroundWindow()
        if window:
            name = _win_image(user32, kernel32, window)
            if name and title_for is not None and title_for(_clean(name)):
                length = user32.GetWindowTextLengthW(window)
                if length > 0:
                    buffer = ctypes.create_unicode_buffer(length + 1)
                    user32.GetWindowTextW(window, buffer, length + 1)
                    title = buffer.value
    except Exception:
        pass
    return name if title_for is None else (name, title)


def _win_image(user32, kernel32, window):
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


# --- Linux ------------------------------------------------------------------

def _xprop_string(value):
    """A string as xprop prints one: in quotes, with backslash escapes, and
    with bytes it would not print as octal escapes of UTF-8."""
    value = value.strip()
    if len(value) < 2 or value[0] != '"' or value[-1] != '"':
        return ""
    out, i, inner = bytearray(), 0, value[1:-1]
    while i < len(inner):
        char = inner[i]
        if char == "\\" and i + 1 < len(inner):
            digits = inner[i + 1:i + 4]
            if len(digits) == 3 and all(c in "01234567" for c in digits):
                out.append(int(digits, 8) & 0xFF)
                i += 4
                continue
            out.extend(inner[i + 1].encode("utf-8"))
            i += 2
            continue
        out.extend(char.encode("utf-8"))
        i += 1
    return out.decode("utf-8", "replace")


def _x11(_pid=None, title_for=None):
    def answer(name, title=""):
        return name if title_for is None else (name, title)

    if not shutil.which("xprop"):
        return answer("")
    active = _run(["xprop", "-root", "_NET_ACTIVE_WINDOW"])
    # "_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007"
    window = active.rsplit("#", 1)[-1].strip().split(",")[0].strip()
    if not window.startswith("0x") or int(window, 16) == 0:
        return answer("")
    # The title rides along in the request already made, so it costs no
    # call of its own; it is only kept below for a program that wants it.
    wanted = ["_NET_WM_PID", "WM_CLASS"]
    if title_for is not None:
        wanted += ["_NET_WM_NAME", "WM_NAME"]
    found = _run(["xprop", "-id", window] + wanted)
    pid, klass, utf8_name, plain_name = "", "", "", ""
    for line in found.splitlines():
        if line.startswith("_NET_WM_PID") and "=" in line:
            pid = line.split("=", 1)[1].strip()
        elif line.startswith("WM_CLASS") and "=" in line:
            # 'WM_CLASS(STRING) = "Navigator", "firefox"': the second is the
            # class, which is the program; the first is one of its windows.
            parts = [p.strip().strip('"') for p in line.split("=", 1)[1].split(",")]
            klass = parts[-1] if parts else ""
        elif line.startswith("_NET_WM_NAME") and "=" in line:
            utf8_name = _xprop_string(line.split("=", 1)[1])
        elif line.startswith("WM_NAME") and "=" in line:
            plain_name = _xprop_string(line.split("=", 1)[1])
    # The class first: the process behind a window is often a runtime shared
    # by many programs, and a Flatpak's pid is not even one of this system's.
    name = klass or (_process_name(pid) if pid.isdigit() else "")
    title = ""
    if title_for is not None and name and title_for(_clean(name)):
        title = utf8_name or plain_name
    return answer(name, title)


def _kde_wayland(_pid=None, title_for=None):
    """KWin through kdotool, the one Wayland compositor with a way to ask.
    The window class first, for the same reason as under X11, and the process
    only when there is no class. The title is one more call, made only for a
    program that wants it."""
    def answer(name, title=""):
        return name if title_for is None else (name, title)

    if not shutil.which("kdotool"):
        return answer("")
    window = _run(["kdotool", "getactivewindow"]).strip()
    if not window:
        return answer("")
    name = _run(["kdotool", "getwindowclassname", window]).strip()
    if not name:
        pid = _run(["kdotool", "getwindowpid", window]).strip()
        name = _process_name(pid) if pid.isdigit() else ""
    title = ""
    if title_for is not None and name and title_for(_clean(name)):
        title = _run(["kdotool", "getwindowname", window]).strip()
    return answer(name, title)


def _unknown(_pid=None, title_for=None):
    return "" if title_for is None else ("", "")


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


def front(pid=None, title_for=None):
    """(program, title) of the window in front. The title is read only when
    `title_for(program)` says so, and is "" otherwise or when it cannot be."""
    if title_for is None:
        return program_name(pid), ""
    try:
        found = backend()(pid, title_for=title_for)
    except Exception:
        return "", ""
    if isinstance(found, str):
        found = (found, "")
    name, title = found
    return _clean(name), (title or "").strip()
