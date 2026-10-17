"""Which program is in front, on each of the four systems, with the system
itself faked at the one place each backend asks it."""

import ctypes
import os
import subprocess
import sys
from unittest import mock

from dikte import frontapp
from tests.support import DikteTest


def finished(stdout, code=0):
    return subprocess.CompletedProcess([], code, stdout=stdout, stderr="")


class Chooser(DikteTest):
    def backend(self, platform, **env):
        with mock.patch.object(sys, "platform", platform), \
                mock.patch.dict(os.environ, env, clear=True):
            return frontapp.backend()

    def test_each_system_has_its_own(self):
        self.assertIs(self.backend("darwin"), frontapp._macos)
        self.assertIs(self.backend("win32"), frontapp._windows)
        self.assertIs(self.backend("linux", XDG_SESSION_TYPE="x11"), frontapp._x11)
        self.assertIs(self.backend("linux", DISPLAY=":0"), frontapp._x11)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   DISPLAY=":0", XDG_CURRENT_DESKTOP="KDE"),
                      frontapp._kde_wayland)
        self.assertIs(self.backend("linux", WAYLAND_DISPLAY="wayland-0",
                                   XDG_CURRENT_DESKTOP="GNOME"),
                      frontapp._unknown)

    def test_a_failing_backend_is_an_unknown_program(self):
        with mock.patch.object(frontapp, "backend",
                               return_value=mock.Mock(side_effect=OSError)):
            self.assertEqual(frontapp.program_name(), "")

    def test_names_come_back_bare_and_lower_case(self):
        with mock.patch.object(frontapp, "backend",
                               return_value=lambda pid: r"C:\Apps\Slack.EXE"):
            self.assertEqual(frontapp.program_name(), "slack")


class X11(DikteTest):
    def answers(self, *replies):
        return mock.patch.object(frontapp.subprocess, "run",
                                 side_effect=[finished(r) for r in replies])

    def test_the_active_windows_process_names_the_program(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x3a00007\n",
                             "_NET_WM_PID(CARDINAL) = 4242\n"
                             'WM_CLASS(STRING) = "Navigator", "firefox"\n') as run, \
                mock.patch.object(frontapp, "_process_name", return_value="thunderbird"):
            self.assertEqual(frontapp._x11(), "thunderbird")
        self.assertEqual(run.call_args.args[0][:3], ["xprop", "-id", "0x3a00007"])

    def test_without_a_pid_the_window_class_will_do(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x1\n",
                             'WM_CLASS(STRING) = "Navigator", "firefox"\n'):
            self.assertEqual(frontapp._x11(), "firefox")

    def test_no_active_window_or_no_xprop_is_unknown(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                self.answers("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x0\n"):
            self.assertEqual(frontapp._x11(), "")
        with mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(frontapp._x11(), "")

    def test_a_hung_xprop_is_unknown_rather_than_a_hang(self):
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run",
                                  side_effect=subprocess.TimeoutExpired("xprop", 0.5)):
            self.assertEqual(frontapp._x11(), "")


class KDEWayland(DikteTest):
    def test_kdotool_asks_kwin(self):
        replies = [finished("{1234-abcd}\n"), finished("777\n")]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies), \
                mock.patch.object(frontapp, "_process_name", return_value="code"):
            self.assertEqual(frontapp._kde_wayland(), "code")

    def test_without_kdotool_it_is_unknown(self):
        with mock.patch.object(frontapp.shutil, "which", return_value=None):
            self.assertEqual(frontapp._kde_wayland(), "")


class Windows(DikteTest):
    def api(self, window=0x10, pid=55, image="C:\\Program Files\\Mozilla Thunderbird\\thunderbird.exe"):
        user32, kernel32 = mock.Mock(), mock.Mock()
        user32.GetForegroundWindow.return_value = window

        def thread_process(_window, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = pid
            return 1

        user32.GetWindowThreadProcessId.side_effect = thread_process
        kernel32.OpenProcess.return_value = 0x99

        def image_name(_handle, _flags, buffer, _size):
            buffer.value = image
            return 1

        kernel32.QueryFullProcessImageNameW.side_effect = image_name
        return user32, kernel32

    def test_the_foreground_windows_image_names_the_program(self):
        user32, kernel32 = self.api()
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(frontapp.program_name(), "thunderbird")
        kernel32.CloseHandle.assert_called_once_with(0x99)

    def test_no_foreground_window_is_unknown(self):
        user32, kernel32 = self.api(window=0)
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)):
            self.assertEqual(frontapp._windows(), "")

    def test_no_user32_at_all_is_unknown(self):
        with mock.patch.object(frontapp, "_win_api", side_effect=OSError):
            self.assertEqual(frontapp._windows(), "")


class MacOS(DikteTest):
    def test_no_appkit_is_unknown(self):
        from dikte import mac_window
        with mock.patch.object(mac_window, "_appkit", side_effect=OSError):
            self.assertEqual(frontapp._macos(4242), "")

    def test_no_front_application_is_unknown(self):
        from dikte import mac_window
        with mock.patch.object(mac_window, "frontmost_pid", return_value=None):
            self.assertEqual(frontapp._macos(None), "")
