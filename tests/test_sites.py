"""Profiles chosen by the site in a browser tab: matching, the title readers on
each system, the keyword proposed from a title, and the editor's Add site."""

import ctypes
import subprocess
import sys
from unittest import mock

from PyQt6.QtWidgets import QApplication

from dikte import config as cfg
from dikte import frontapp
from dikte import profiles
from dikte import profiles_ui
from tests.support import DikteTest
from tests.test_worker import ChainBase

_app = QApplication.instance() or QApplication([])

EM, EN = "\u2014", "\u2013"


def finished(stdout):
    return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")


class Matching(DikteTest):
    def setUp(self):
        super().setUp()
        self.conf = self.config(profiles=[
            {"name": "Chat", "sites": ["WhatsApp"], "apps": ["slack"]},
            {"name": "Mail", "sites": ["Gmail", "Outlook"], "apps": ["thunderbird"]},
            {"name": "Browsing", "apps": ["firefox"]},
        ])

    def choose(self, program, title):
        return profiles.choose(self.conf, program, True, title)

    def test_a_site_wins_then_the_program_then_default(self):
        self.assertEqual(self.choose("firefox", f"(2) WhatsApp {EM} Mozilla Firefox").name,
                         "Chat")
        self.assertEqual(self.choose("firefox", f"News {EM} Mozilla Firefox").name,
                         "Browsing")
        self.assertEqual(self.choose("google-chrome", "News - Google Chrome").name, "")
        self.assertEqual(self.choose("thunderbird", "").name, "Mail")

    def test_the_matched_keyword_is_the_site_and_nothing_else(self):
        choice = self.choose("firefox", f"Inbox - Gmail {EM} Mozilla Firefox")
        self.assertEqual((choice.name, choice.site), ("Mail", "Gmail"))
        self.assertNotIn("Inbox", repr(choice))

    def test_only_a_browser_title_counts(self):
        """A document called Gmail in an editor is not the site."""
        self.assertEqual(self.choose("code", "Gmail notes.md - Code").name, "")

    def test_browser_forms(self):
        for name in ("firefox", "Navigator", "org.mozilla.firefox", "zen",
                     "app.zen_browser.zen", "Google-chrome", "chromium-browser",
                     "brave-browser", "com.brave.Browser", "vivaldi-stable",
                     "msedge.exe", "microsoft-edge", "librewolf", "floorp", "waterfox"):
            with self.subTest(name=name):
                self.assertTrue(profiles.is_browser(name))
        for name in ("thunderbird", "code", "org.kde.konsole", ""):
            with self.subTest(name=name):
                self.assertFalse(profiles.is_browser(name))

    def test_keywords_fold_and_match_whole_words(self):
        self.assertTrue(profiles.site_in("gmail", "Inbox - GMAIL"))
        self.assertTrue(profiles.site_in("İzmir", "izmir haberleri - Chromium"))
        self.assertTrue(profiles.site_in("Google Docs", "Report - Google Docs"))
        self.assertFalse(profiles.site_in("Mail", "Inbox - Gmail"))
        self.assertFalse(profiles.site_in("", "anything"))

    def test_sites_turn_on_the_lookup_and_the_title(self):
        conf = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp"]}])
        self.assertTrue(profiles.wants_detection(conf))
        self.assertTrue(profiles.wants_titles(conf))
        conf["profile_by_app"] = False
        self.assertFalse(profiles.wants_titles(conf))
        self.assertEqual(profiles.choose(conf, "firefox", True, "WhatsApp").name, "")

    def test_sites_are_cleaned(self):
        conf = self.config(profiles=[{"name": "A", "sites": ["Gmail", "gmail", 3,
                                                            "  You  Tube "]}])
        self.assertEqual(profiles.named_profiles(conf)[0]["sites"], ["Gmail", "You Tube"])

    def test_a_retry_keeps_its_site(self):
        choice = profiles.again(self.conf, {"profile": "Chat", "site": "WhatsApp",
                                            "program": "firefox"})
        self.assertEqual((choice.name, choice.site), ("Chat", "WhatsApp"))


class Proposal(DikteTest):
    def test_typical_titles(self):
        for title, expected in (
            (f"Inbox (3) - you@example.com - Gmail {EM} Mozilla Firefox", "Gmail"),
            ("Never Gonna Give You Up - YouTube - Google Chrome", "YouTube"),
            (f"(2) WhatsApp {EM} Zen Browser", "WhatsApp"),
            (f"Dikte | Notion {EM} Mozilla Firefox", "Notion"),
            (f"Pull requests {EN} GitHub - Brave", "GitHub"),
            ("web.whatsapp.com - Chromium", "whatsapp"),
            ("Knowledge base - Confluence - Microsoft Edge", "Confluence"),
            ("WhatsApp", "WhatsApp"),
            ("", ""),
        ):
            with self.subTest(title=title):
                self.assertEqual(profiles.propose_site(title), expected)


class Readers(DikteTest):
    def test_x11_asks_for_the_title_in_the_same_call(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "Navigator", "firefox"\n'
                            '_NET_WM_NAME(UTF8_STRING) = "Inbox \\"new\\" - Gmail '
                            f'{EM} Mozilla Firefox"\n'
                            'WM_NAME(STRING) = "fallback"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run, \
                mock.patch.object(frontapp, "backend", return_value=frontapp._x11):
            program, title = frontapp.front(None, profiles.is_browser)
        self.assertEqual((program, title),
                         ("firefox", f'Inbox "new" - Gmail {EM} Mozilla Firefox'))
        self.assertEqual(run.call_count, 2)
        self.assertIn("_NET_WM_NAME", run.call_args.args[0])

    def test_x11_falls_back_to_wm_name(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "x", "chromium"\n'
                            'WM_NAME(STRING) = "YouTube - Chromium"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies):
            self.assertEqual(frontapp._x11(title_for=profiles.is_browser),
                             ("chromium", "YouTube - Chromium"))

    def test_x11_keeps_no_title_for_a_program_that_is_not_a_browser(self):
        replies = [finished("_NET_ACTIVE_WINDOW(WINDOW): window id # 0x2\n"),
                   finished('WM_CLASS(STRING) = "x", "code"\n'
                            '_NET_WM_NAME(UTF8_STRING) = "secret.txt - Code"\n')]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/xprop"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies):
            self.assertEqual(frontapp._x11(title_for=profiles.is_browser), ("code", ""))

    def test_kde_reads_the_title_with_one_more_call_and_only_for_a_browser(self):
        replies = [finished("{w}\n"), finished("org.mozilla.firefox\n"),
                   finished(f"WhatsApp {EM} Mozilla Firefox\n")]
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run", side_effect=replies) as run:
            self.assertEqual(frontapp._kde_wayland(title_for=profiles.is_browser),
                             ("org.mozilla.firefox", f"WhatsApp {EM} Mozilla Firefox"))
        self.assertEqual(run.call_args.args[0][:2], ["kdotool", "getwindowname"])
        with mock.patch.object(frontapp.shutil, "which", return_value="/usr/bin/kdotool"), \
                mock.patch.object(frontapp.subprocess, "run",
                                  side_effect=[finished("{w}\n"),
                                               finished("org.kde.konsole\n")]) as run:
            self.assertEqual(frontapp._kde_wayland(title_for=profiles.is_browser),
                             ("org.kde.konsole", ""))
        self.assertEqual(run.call_count, 2)

    def test_windows_reads_the_foreground_windows_text(self):
        user32, kernel32 = mock.Mock(), mock.Mock()
        user32.GetForegroundWindow.return_value = 0x10

        def thread_process(_window, pointer):
            ctypes.cast(pointer, ctypes.POINTER(ctypes.c_ulong)).contents.value = 7
            return 1

        def image(_handle, _flags, buffer, _size):
            buffer.value = "C:\\Program Files\\Google\\Chrome\\chrome.exe"
            return 1

        def text(_window, buffer, _size):
            buffer.value = "Gmail - Google Chrome"
            return len(buffer.value)

        user32.GetWindowThreadProcessId.side_effect = thread_process
        user32.GetWindowTextLengthW.return_value = 21
        user32.GetWindowTextW.side_effect = text
        kernel32.OpenProcess.return_value = 0x99
        kernel32.QueryFullProcessImageNameW.side_effect = image
        with mock.patch.object(frontapp, "_win_api", return_value=(user32, kernel32)), \
                mock.patch.object(sys, "platform", "win32"):
            self.assertEqual(frontapp.front(None, profiles.is_browser),
                             ("chrome", "Gmail - Google Chrome"))

    def test_macos_reads_no_title(self):
        with mock.patch.object(frontapp, "_macos_name", return_value="Firefox"):
            self.assertEqual(frontapp._macos(42, title_for=profiles.is_browser),
                             ("Firefox", ""))
            self.assertEqual(frontapp._macos(42), "Firefox")

    def test_the_lookup_reads_titles_only_when_a_profile_lists_a_site(self):
        from dikte import app
        apps_only = self.config(profiles=[{"name": "A", "apps": ["firefox"]}])
        with mock.patch.object(app.frontapp, "front") as front, \
                mock.patch.object(app.frontapp, "program_name", return_value="firefox"):
            app._ProfileLookup(apps_only, None).result()
        front.assert_not_called()
        with_sites = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp"]}])
        with mock.patch.object(app.frontapp, "front",
                               return_value=("firefox", "(1) WhatsApp")):
            choice = app._ProfileLookup(with_sites, None).result()
        self.assertEqual((choice.name, choice.site), ("Chat", "WhatsApp"))


class TitleNeverStored(ChainBase):
    def test_the_history_keeps_the_site_and_not_the_title(self):
        self.conf["profiles"] = [{"name": "Chat", "sites": ["WhatsApp"]}]
        title = f"(4) WhatsApp {EM} Private chat with Ayşe {EM} Mozilla Firefox"
        choice = profiles.choose(self.conf, "firefox", True, title)
        self.run_chain(profile=choice)
        stored = cfg.HISTORY_FILE.read_text(encoding="utf-8")
        row = cfg.read_history()[0]
        self.assertEqual((row["profile"], row["site"], row["program"]),
                         ("Chat", "WhatsApp", "firefox"))
        self.assertNotIn("Ayşe", stored)
        self.assertNotIn("Private chat", stored)


class Editor(DikteTest):
    def editor(self, conf):
        editor = profiles_ui.ProfilesEditor()
        self.addCleanup(editor.deleteLater)
        editor.load(conf)
        editor.select(1)
        return editor

    def test_a_keyword_can_be_typed(self):
        conf = self.config(profiles=[{"name": "Chat"}])
        editor = self.editor(conf)
        editor.site_edit.setText("WhatsApp")
        editor._add_typed_site()
        self.assertFalse(editor.add_site("whatsapp"))
        editor.store(conf)
        self.assertEqual(conf["profiles"][0]["sites"], ["WhatsApp"])

    def test_add_site_counts_down_reads_and_proposes(self):
        conf = self.config(profiles=[{"name": "Chat"}])
        editor = self.editor(conf)
        with mock.patch.object(profiles_ui.threading, "Thread") as thread, \
                mock.patch.object(frontapp, "front",
                                  return_value=("firefox", f"(2) WhatsApp {EM} Mozilla Firefox")):
            editor.start_site_countdown()
            self.assertIn("Switch to the tab… 3", editor.site_status.text())
            self.assertFalse(editor.read_site_button.isEnabled())
            editor._tick()
            self.assertIn("2", editor.site_status.text())
            editor._tick()
            editor._tick()
            # Read off the main thread.
            thread.return_value.start.assert_called_once_with()
            thread.call_args.kwargs["target"]()
        QApplication.processEvents()
        self.assertEqual(editor.current()["sites"], ["WhatsApp"])
        self.assertIn("firefox", editor.site_status.text())
        self.assertIn("Mozilla Firefox", editor.site_status.text())
        self.assertTrue(editor.read_site_button.isEnabled())
        editor.store(conf)
        self.assertNotIn("Mozilla", repr(conf["profiles"]))

    def test_a_program_that_is_not_a_browser_is_said_so(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        editor._site_question = 5
        editor._site_target = editor.current()
        editor._titled(5, "code", "")
        self.assertIn("not a browser", editor.site_status.text())
        self.assertEqual(editor.current()["sites"], [])

    def test_the_default_profile_has_no_sites(self):
        editor = self.editor(self.config(profiles=[{"name": "Chat"}]))
        editor.select(0)
        self.assertFalse(editor.read_site_button.isEnabled())
        self.assertFalse(editor.add_site("WhatsApp"))

    def test_remove_site(self):
        conf = self.config(profiles=[{"name": "Chat", "sites": ["WhatsApp", "Gmail"]}])
        editor = self.editor(conf)
        editor.sites.item(0).setSelected(True)
        editor.remove_selected_sites()
        self.assertEqual(editor.current()["sites"], ["Gmail"])

    def test_the_tab_says_what_is_read_and_where_it_is_not(self):
        with mock.patch.object(frontapp, "backend", return_value=frontapp._macos):
            self.assertIn("not available on macOS", profiles_ui.ProfilesEditor.sites_text())
        with mock.patch.object(frontapp, "backend", return_value=frontapp._x11):
            self.assertIn("never saved", profiles_ui.ProfilesEditor.sites_text())
