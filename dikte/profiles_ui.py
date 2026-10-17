"""The two editors behind the dictionary and the writing profiles in Settings.

Kept out of settings_ui.py, which is long enough, and built the way the rest of
that window expects: each editor holds its own working copy, is read into the
form by load() and written back by store() when the window's Apply runs, and
reports what it holds through values() so the window can tell that something
is unsaved. The boxes inside an editor are marked as the editor's own (owned()),
because a search field or the box showing the selected profile changes without
anything having been edited.
"""

import copy
import itertools
import threading

from PyQt6.QtCore import QObject, Qt, QTimer, pyqtSignal
from PyQt6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QListWidget, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget,
)

from . import dictionary
from . import frontapp
from . import profiles
from .i18n import t

_OWNED = "dikte_editor"

# What each preference value is called on screen.
STYLE_LABELS = {
    "formality": ("Formality", {"casual": "Casual", "neutral": "Neutral",
                                "formal": "Formal"}),
    "sentences": ("Sentences", {"short": "Short", "long": "Longer"}),
    "punctuation": ("Punctuation", {"minimal": "Light",
                                    "expressive": "Expressive"}),
    "brevity": ("Brevity", {"concise": "Concise",
                            "complete": "Keep the full phrasing"}),
}


class _Relay(QObject):
    """Where a program lookup reports back. One for the whole application and
    never deleted, so a thread finishing after its editor has gone emits on
    something that still exists; the editor's own connection goes with it."""

    found = pyqtSignal(object, str)


_RELAY = []
# Every question asked, numbered across all editors: an address can be reused
# by a new editor, a number from this never is.
_QUESTIONS = itertools.count(1)


def _relay():
    if not _RELAY:
        _RELAY.append(_Relay())
    return _RELAY[0]


def owned(widget):
    """Whether a box belongs to one of these editors rather than to the form."""
    while widget is not None:
        if widget.property(_OWNED):
            return True
        widget = widget.parentWidget()
    return False


def _label(text):
    label = QLabel(text)
    label.setWordWrap(True)
    label.setTextFormat(Qt.TextFormat.PlainText)
    return label


def _muted(text=""):
    label = _label(text)
    label.setObjectName("muted")
    return label


def _row(*widgets):
    row = QHBoxLayout()
    for widget in widgets:
        row.addWidget(widget)
    row.addStretch()
    return row


def style_summary(style):
    """"Formality: Casual, Sentences: Short", for a set of preferences."""
    parts = []
    for key, (title, values) in STYLE_LABELS.items():
        if key in style:
            parts.append(t("{name}: {value}", name=t(title),
                           value=t(values[style[key]])))
    return ", ".join(parts)


# --- the dictionary ---------------------------------------------------------

class EntryDialog(QDialog):
    """One dictionary record: the word, and how it should be written."""

    def __init__(self, parent=None, term="", spelling=""):
        super().__init__(parent)
        self.setWindowTitle(t("Dictionary entry"))
        form = QFormLayout(self)
        self.term = QLineEdit(term)
        self.term.setPlaceholderText(t("e.g. dikte"))
        self.spelling = QLineEdit(spelling)
        self.spelling.setPlaceholderText(t("Optional, e.g. Dikte"))
        form.addRow(t("Word or expression"), self.term)
        form.addRow(t("Preferred spelling"), self.spelling)
        form.addRow(_muted(t(
            "Leave the spelling empty when the word is already written the way "
            "you want it. With one, the cleanup model is told to write the "
            "word that way.")))
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok
                                   | QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        form.addRow(buttons)


class DictionaryEditor(QWidget):
    """The personal dictionary as a list of records, with learning's switch."""

    changed = pyqtSignal()

    FILTERS = (("All", ""), ("Auto-added", dictionary.AUTO),
               ("Manually added", dictionary.MANUAL))

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty(_OWNED, True)
        self._entries = []
        self._loaded = []
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(t(
            "Names and terms you say often. They go to the transcription model "
            "as a hint, and to the cleanup model as a glossary, so it can "
            "repair the ones that still come out wrong. Every profile uses "
            "the same dictionary, and so do audio files and meetings.")))

        self.search = QLineEdit()
        self.search.setPlaceholderText(t("Search"))
        self.search.setClearButtonEnabled(True)
        self.search.textChanged.connect(self._show)
        self.filter = QComboBox()
        for label, value in self.FILTERS:
            self.filter.addItem(t(label), value)
        self.filter.currentIndexChanged.connect(self._show)
        top = QHBoxLayout()
        top.addWidget(self.search, 1)
        top.addWidget(self.filter)
        layout.addLayout(top)

        self.table = QTableWidget(0, 3)
        self.table.setHorizontalHeaderLabels(
            [t("Word or expression"), t("Preferred spelling"), t("Added")])
        self.table.horizontalHeader().setSectionResizeMode(
            QHeaderView.ResizeMode.Stretch)
        self.table.verticalHeader().hide()
        self.table.setSelectionBehavior(QAbstractItemView.SelectionBehavior.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.table.setMinimumHeight(160)
        self.table.itemDoubleClicked.connect(lambda *_: self._edit())
        layout.addWidget(self.table, 1)

        add = QPushButton(t("Add…"))
        add.clicked.connect(self._add)
        edit = QPushButton(t("Edit…"))
        edit.clicked.connect(self._edit)
        delete = QPushButton(t("Delete"))
        delete.clicked.connect(self.delete_selected)
        self.clear_auto_button = QPushButton(t("Clear auto-learned"))
        self.clear_auto_button.clicked.connect(self.clear_auto)
        layout.addLayout(_row(add, edit, delete, self.clear_auto_button))

        self.learning = QCheckBox(t("Learn names and terms from my dictations"))
        self.learning.toggled.connect(lambda *_: self.changed.emit())
        layout.addWidget(self.learning)
        layout.addWidget(_muted(t(
            "Only ordinary dictations are read, and only once they have been "
            "cleaned up and finished without a warning: never meetings, files, "
            "the agent, translations, the clipboard or other programs. A name "
            "written with a capital in the middle of a sentence is added after "
            "it has come through three dictations unchanged, marked Auto; until "
            "then the names being counted are kept in the dictionary file. A "
            "learned name you delete is not learned again. Nothing is sent "
            "anywhere to do this.")))

    # ---- the form ---------------------------------------------------------

    def load(self, conf):
        self._entries = dictionary.records(conf)
        self._loaded = [dict(r) for r in self._entries]
        self._cleared = False
        self.learning.setChecked(bool(conf["dictionary_learning"]))
        self._show()

    def values(self):
        return [[(r["term"], r["spelling"], r["source"]) for r in self._entries],
                self.learning.isChecked()]

    def store(self, conf):
        """Put the edits into `conf`. Returns what commit() writes once the
        window has saved config.json, or None when the records are unchanged:
        an Apply that edited nothing leaves the dictionary as it was stored,
        down to the commas."""
        conf["dictionary_learning"] = self.learning.isChecked()
        # The names still being counted are words out of dictations, kept
        # only for learning: they go when learning is off, or when the user
        # cleared what it learned.
        forget = self._cleared or not self.learning.isChecked()
        if self._entries == self._loaded:
            return (None, forget)
        return (dictionary.save(conf, self._entries, self._loaded), forget)

    @staticmethod
    def commit(stored):
        if stored is None:
            return
        pending, forget = stored
        if pending is not None:
            dictionary.write(pending, forget)
        elif forget:
            dictionary.forget_pending()

    # ---- editing ----------------------------------------------------------

    def entries(self):
        return list(self._entries)

    def add_entry(self, term, spelling="", source=dictionary.MANUAL):
        """Add one record per comma-separated piece; False when nothing was
        added because each one is already there."""
        added = False
        for piece in dictionary.split(term) or []:
            record = dictionary.entry(piece, spelling, source)
            if record is None or self._index(record["term"]) is not None:
                continue
            self._entries.append(record)
            added = True
        if added:
            self._show()
            self.changed.emit()
        return added

    def edit_entry(self, index, term, spelling=""):
        """Correct a record. A record the user corrected is theirs now, so an
        Auto mark is taken off it."""
        record = dictionary.entry(term, spelling)
        if record is None or not 0 <= index < len(self._entries):
            return False
        old = self._entries[index]
        if (old["term"], old["spelling"]) == (record["term"], record["spelling"]):
            return True
        # Only a change of word can collide: two equal terms migrated from the
        # old text stay editable, each one on its own.
        renamed = dictionary.fold(record["term"]) != dictionary.fold(old["term"])
        if renamed and self._index(record["term"], skip=index) is not None:
            return False
        record["breaks"] = old.get("breaks", 0)
        self._entries[index] = record
        self._show()
        self.changed.emit()
        return True

    def delete_entries(self, indexes):
        """Remove records. A line break that stood before one moves on to the
        record after it, so deleting a word does not join two lines."""
        doomed = set(indexes)
        kept, carry = [], 0
        for i, record in enumerate(self._entries):
            if i in doomed:
                carry = max(carry, record.get("breaks", 0))
                continue
            if carry and kept:
                record = dict(record, breaks=max(record.get("breaks", 0), carry))
            carry = 0
            kept.append(record)
        if kept:
            kept[0] = dict(kept[0], breaks=0)
        self._entries = kept
        self._show()
        self.changed.emit()

    def delete_selected(self):
        self.delete_entries(self._selected())

    def clear_auto(self):
        if not any(r["source"] == dictionary.AUTO for r in self._entries):
            return
        self._entries = dictionary.clear_auto(self._entries)
        self._cleared = True
        self._show()
        self.changed.emit()

    def _index(self, term, skip=None):
        """The first other record with this term, folded the way the
        dictionary folds it (Turkish's dotted I included)."""
        key = dictionary.fold(term)
        for i, record in enumerate(self._entries):
            if i != skip and dictionary.fold(record["term"]) == key:
                return i
        return None

    def _selected(self):
        rows = {item.row() for item in self.table.selectedItems()}
        return sorted(self.table.item(row, 0).data(Qt.ItemDataRole.UserRole)
                      for row in rows)

    def _add(self):
        dialog = EntryDialog(self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.add_entry(dialog.term.text(), dialog.spelling.text())

    def _edit(self):
        chosen = self._selected()
        if not chosen:
            return
        record = self._entries[chosen[0]]
        dialog = EntryDialog(self, record["term"], record["spelling"])
        if dialog.exec() == QDialog.DialogCode.Accepted:
            if not self.edit_entry(chosen[0], dialog.term.text(),
                                   dialog.spelling.text()):
                QMessageBox.information(self, t("Dictionary"), t(
                    "That word is already in the dictionary."))

    def visible(self):
        """The records the search and the filter let through, as indexes."""
        needle = self.search.text().strip().casefold()
        wanted = self.filter.currentData() or ""
        out = []
        for i, record in enumerate(self._entries):
            if wanted and record["source"] != wanted:
                continue
            if needle and needle not in record["term"].casefold() \
                    and needle not in record["spelling"].casefold():
                continue
            out.append(i)
        return out

    def _show(self, *_):
        shown = self.visible()
        self.table.setRowCount(len(shown))
        for row, index in enumerate(shown):
            record = self._entries[index]
            cells = (record["term"], record["spelling"],
                     t("Auto") if record["source"] == dictionary.AUTO else t("Manual"))
            for column, text in enumerate(cells):
                item = QTableWidgetItem(text)
                item.setData(Qt.ItemDataRole.UserRole, index)
                self.table.setItem(row, column, item)
        self.clear_auto_button.setEnabled(
            any(r["source"] == dictionary.AUTO for r in self._entries))


# --- the profiles -----------------------------------------------------------

class ProfilesEditor(QWidget):
    """The Default profile, the named ones, and the switches over all of them."""

    changed = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setProperty(_OWNED, True)
        self._default = profiles.clean({}, named=False)
        self._named = []
        self._filling = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(_label(t(
            "A profile says how a dictation should read, and can translate it. "
            "The Default profile is used unless a named profile lists the "
            "program in front when the recording starts. Files, meetings and "
            "the agent are never changed by a profile.")))

        switches = QGroupBox(t("What profiles may do"))
        column = QVBoxLayout(switches)
        self.use_style = QCheckBox(t("Apply the writing style of the profile"))
        self.use_style.setToolTip(t(
            "Adds the profile's preferences to the dictation cleanup "
            "instruction. A cleanup instruction of your own replaces the "
            "built-in one, and the style is still added after it; switch this "
            "off to use your own instruction alone."))
        self.by_app = QCheckBox(t("Choose the profile by the program in front"))
        self.translation = QCheckBox(t("Translate when the profile has a target language"))
        self.translation.setToolTip(t(
            "The translation is made after the recording, by the cleanup "
            "model, before the text is pasted. It is not live. If it fails, "
            "the original transcript is copied and nothing is pasted."))
        self.style_learning = QCheckBox(t("Use the style learned from examples I paste in"))
        for box in (self.use_style, self.by_app, self.translation,
                    self.style_learning):
            box.toggled.connect(self._switched)
            column.addWidget(box)
        layout.addWidget(switches)

        choose = QFormLayout()
        self.override = QComboBox()
        self.override.currentIndexChanged.connect(self._edited)
        choose.addRow(t("Profile for every dictation"), self.override)
        layout.addLayout(choose)
        self.detection = _muted(self.detection_text())
        layout.addWidget(self.detection)
        self.find_button = QPushButton(t("Find the program name"))
        self.find_button.setToolTip(t(
            "Switch to the program within three seconds; its name is shown "
            "here, ready to be listed in a profile."))
        self.find_button.clicked.connect(self._find_program)
        self.found = _muted()
        layout.addLayout(_row(self.find_button, self.found))

        self.list = QListWidget()
        self.list.setMaximumHeight(120)
        self.list.currentRowChanged.connect(self._selected)
        layout.addWidget(self.list)
        add = QPushButton(t("Add profile…"))
        add.clicked.connect(self._add)
        self.rename_button = QPushButton(t("Rename…"))
        self.rename_button.clicked.connect(self._rename)
        self.remove_button = QPushButton(t("Remove"))
        self.remove_button.clicked.connect(self.remove_current)
        layout.addLayout(_row(add, self.rename_button, self.remove_button))

        self.editor = QGroupBox()
        form = QFormLayout(self.editor)
        self.apps = QLineEdit()
        self.apps.setPlaceholderText(t("e.g. thunderbird, outlook"))
        self.apps.setToolTip(t(
            "Program names, separated by commas, the way Find the program name "
            "shows them. A browser counts as one program whatever site is open "
            "in it."))
        self.apps.textEdited.connect(self._edited)
        self.apps_label = QLabel(t("Programs"))
        form.addRow(self.apps_label, self.apps)
        self.style_boxes = {}
        for key, (title, values) in STYLE_LABELS.items():
            box = QComboBox()
            box.addItem(t("No preference"), "")
            for value in profiles.STYLE[key]:
                box.addItem(t(values[value]), value)
            box.currentIndexChanged.connect(self._edited)
            self.style_boxes[key] = box
            form.addRow(t(title), box)
        self.instruction = QLineEdit()
        self.instruction.setPlaceholderText(t("e.g. Sign off with my first name"))
        self.instruction.textEdited.connect(self._edited)
        form.addRow(t("Also"), self.instruction)
        self.target = QComboBox()
        self.target.addItem(t("Do not translate"), "")
        for code, english, _turkish in profiles.LANGUAGES:
            self.target.addItem(t(english), code)
        self.target.currentIndexChanged.connect(self._edited)
        form.addRow(t("Translate into"), self.target)
        self.learned = _muted()
        self.reset_learned = QPushButton(t("Reset learned style"))
        self.reset_learned.clicked.connect(self.reset_learned_style)
        form.addRow(self.learned)
        form.addRow(self.reset_learned)
        layout.addWidget(self.editor)

        self.teach = QGroupBox(t("Learn the style from examples"))
        teach = QVBoxLayout(self.teach)
        teach.addWidget(_muted(t(
            "Paste something you wrote, or a dictation and the way you "
            "corrected it. It is read on this computer and not kept: only the "
            "preferences found in it are, shown above, and a choice you made "
            "yourself always wins over a learned one.")))
        self.examples = QPlainTextEdit()
        self.examples.setPlaceholderText(t("Examples of your own writing"))
        self.examples.setMaximumHeight(90)
        teach.addWidget(self.examples)
        self.before = QPlainTextEdit()
        self.before.setPlaceholderText(t("What Dikte wrote (optional)"))
        self.before.setMaximumHeight(60)
        self.after = QPlainTextEdit()
        self.after.setPlaceholderText(t("What you corrected it to (optional)"))
        self.after.setMaximumHeight(60)
        pair = QHBoxLayout()
        pair.addWidget(self.before)
        pair.addWidget(self.after)
        teach.addLayout(pair)
        self.learn_button = QPushButton(t("Learn from these"))
        self.learn_button.clicked.connect(self.learn_from_examples)
        self.learn_result = _muted()
        teach.addLayout(_row(self.learn_button, self.learn_result))
        layout.addWidget(self.teach)

        self._find_timer = QTimer(self)
        self._find_timer.setSingleShot(True)
        self._find_timer.timeout.connect(self._show_found)
        _relay().found.connect(self._found)
        self._asked = 0

    # ---- the form ---------------------------------------------------------

    def load(self, conf):
        self._filling = True
        self._default = profiles.default_profile(conf)
        self._named = profiles.named_profiles(conf)
        self.use_style.setChecked(bool(conf["profile_style"]))
        self.by_app.setChecked(bool(conf["profile_by_app"]))
        self.translation.setChecked(bool(conf["translation_enabled"]))
        self.style_learning.setChecked(bool(conf["style_learning"]))
        self._fill_list(0)
        self._fill_override(conf["profile_override"]
                            if isinstance(conf["profile_override"], str) else "")
        self._filling = False
        self._selected(self.list.currentRow())
        self._sync_switches()

    def values(self):
        # Copies: the window keeps the last saved values to compare with, and
        # the profiles are edited in place.
        return copy.deepcopy([self._default, self._named, self.use_style.isChecked(),
                self.by_app.isChecked(), self.translation.isChecked(),
                self.style_learning.isChecked(), self.override.currentData() or ""])

    def store(self, conf):
        # Only what differs: an Apply that touched no profile leaves them
        # stored the way they were, rather than rewritten in their clean form.
        if self._default != profiles.default_profile(conf):
            conf["profile_default"] = copy.deepcopy(self._default)
        if self._named != profiles.named_profiles(conf):
            conf["profiles"] = copy.deepcopy(self._named)
        conf["profile_style"] = self.use_style.isChecked()
        conf["profile_by_app"] = self.by_app.isChecked()
        conf["translation_enabled"] = self.translation.isChecked()
        conf["style_learning"] = self.style_learning.isChecked()
        conf["profile_override"] = self.override.currentData() or ""

    @staticmethod
    def detection_text():
        """What this session can tell about the program in front."""
        backend = frontapp.backend()
        if backend is frontapp._unknown:
            return t("This desktop does not say which program is in front, so "
                     "every dictation uses the Default profile unless one is "
                     "chosen above.")
        if backend is frontapp._kde_wayland and not frontapp.shutil.which("kdotool"):
            return t("On KDE Plasma under Wayland, per-application profiles need "
                     "kdotool installed.")
        return t("The program in front is looked up when a recording starts. "
                 "When it cannot be found out or no profile lists it, the Default "
                 "profile is used, and the history names the profile and the program.")

    # ---- the profiles -----------------------------------------------------

    def profile_names(self):
        return [p["name"] for p in self._named]

    def _taken(self, name, skip=None):
        """Whether a name would be confused with another profile, or with
        Default in either language."""
        key = name.casefold()
        if key in ("default", t("Default").casefold()):
            return True
        return any(n.casefold() == key for i, n in enumerate(self.profile_names())
                   if i != skip)

    def add_profile(self, name):
        name = " ".join((name or "").split())[:60]
        if not name or self._taken(name):
            return False
        self._named.append(profiles.clean({"name": name, "id": profiles.new_id()}))
        self._fill_list(len(self._named))
        self._fill_override(self.override.currentData() or "")
        self.changed.emit()
        return True

    def rename_current(self, name):
        index = self.list.currentRow() - 1
        name = " ".join((name or "").split())[:60]
        if index < 0 or not name:
            return False
        if self._taken(name, skip=index):
            return False
        old = self._named[index]["name"]
        self._named[index]["name"] = name
        chosen = self.override.currentData() or ""
        self._fill_list(index + 1)
        self._fill_override(name if chosen == old else chosen)
        self.changed.emit()
        return True

    def remove_current(self):
        index = self.list.currentRow() - 1
        if index < 0:
            return False
        self._named.pop(index)
        self._fill_list(min(index + 1, len(self._named)))
        self._fill_override(self.override.currentData() or "")
        self.changed.emit()
        return True

    def current(self):
        """The profile being edited: the Default one at row 0."""
        index = self.list.currentRow()
        if index <= 0 or index > len(self._named):
            return self._default
        return self._named[index - 1]

    def select(self, row):
        self.list.setCurrentRow(row)

    def reset_learned_style(self):
        profile = self.current()
        if profile["learned"]:
            profile["learned"] = {}
            self._selected(self.list.currentRow())
            self.changed.emit()

    def learn_from_examples(self):
        """Read the pasted text into the selected profile's learned style, then
        empty the boxes: the text was lent for this, not given to keep."""
        before = self.before.toPlainText().strip()
        after = self.after.toPlainText().strip()
        corrections = [(before, after)] if before and after else []
        found = profiles.learn_style([self.examples.toPlainText()], corrections)
        for box in (self.examples, self.before, self.after):
            box.clear()
        if not found:
            self.learn_result.setText(t(
                "Nothing clear enough to learn from that. A few sentences more "
                "would help."))
            return {}
        profile = self.current()
        profile["learned"] = dict(profile["learned"], **found)
        self.learn_result.setText(t("Learned: {preferences}",
                                    preferences=style_summary(found)))
        self._selected(self.list.currentRow())
        self.changed.emit()
        return found

    def _fill_list(self, row):
        with_signals = self.list.blockSignals(True)
        self.list.clear()
        self.list.addItem(t("Default"))
        for profile in self._named:
            programs = ", ".join(profile["apps"])
            self.list.addItem(f"{profile['name']}  ({programs})" if programs
                              else profile["name"])
        self.list.blockSignals(with_signals)
        self.list.setCurrentRow(max(0, min(row, len(self._named))))
        self._selected(self.list.currentRow())

    def _fill_override(self, chosen):
        with_signals = self.override.blockSignals(True)
        self.override.clear()
        self.override.addItem(t("Automatic, by the program in front"), "")
        for name in self.profile_names():
            self.override.addItem(name, name)
        index = self.override.findData(chosen) if chosen else 0
        self.override.setCurrentIndex(max(0, index))
        self.override.blockSignals(with_signals)

    def _selected(self, _row=None):
        profile = self.current()
        named = profile is not self._default
        self._filling = True
        self.editor.setTitle(profile["name"] if named else t("Default"))
        self.apps.setText(", ".join(profile.get("apps", [])))
        self.apps.setEnabled(named)
        self.apps_label.setEnabled(named)
        for key, box in self.style_boxes.items():
            index = box.findData(profile["style"].get(key, ""))
            box.setCurrentIndex(max(0, index))
        self.instruction.setText(profile["instruction"])
        self.target.setCurrentIndex(max(0, self.target.findData(profile["translate_to"])))
        learned = style_summary(profile["learned"])
        self.learned.setText(t("Learned from your examples: {preferences}",
                               preferences=learned) if learned else "")
        self.learned.setVisible(bool(learned))
        self.reset_learned.setVisible(bool(learned))
        self.rename_button.setEnabled(named)
        self.remove_button.setEnabled(named)
        self._filling = False

    def _edited(self, *_):
        if self._filling:
            return
        profile = self.current()
        if profile is not self._default:
            profile["apps"] = profiles.clean({"apps": self.apps.text()})["apps"]
            item = self.list.currentItem()
            if item is not None:
                programs = ", ".join(profile["apps"])
                item.setText(f"{profile['name']}  ({programs})" if programs
                             else profile["name"])
        profile["style"] = {key: box.currentData()
                            for key, box in self.style_boxes.items()
                            if box.currentData()}
        profile["instruction"] = self.instruction.text().strip()[:2000]
        profile["translate_to"] = self.target.currentData() or ""
        self.changed.emit()

    def _switched(self, *_):
        self._sync_switches()
        if not self._filling:
            self.changed.emit()

    def _sync_switches(self):
        for box in self.style_boxes.values():
            box.setEnabled(self.use_style.isChecked())
        self.instruction.setEnabled(self.use_style.isChecked())
        self.target.setEnabled(self.translation.isChecked())
        self.teach.setEnabled(self.style_learning.isChecked())

    def _add(self):
        name, ok = QInputDialog.getText(self, t("Add profile"), t("Name"))
        if ok and not self.add_profile(name):
            QMessageBox.information(self, t("Profiles"), t(
                "A profile needs a name of its own."))

    def _rename(self):
        profile = self.current()
        if profile is self._default:
            return
        name, ok = QInputDialog.getText(self, t("Rename profile"), t("Name"),
                                        text=profile["name"])
        if ok and not self.rename_current(name):
            QMessageBox.information(self, t("Profiles"), t(
                "A profile needs a name of its own."))

    def _find_program(self):
        self.found.setText(t("Switch to the program now…"))
        self._find_timer.start(3000)

    def _show_found(self):
        """Ask on a thread of its own: the asking is up to three subprocesses,
        and this is the main thread. The answer comes back through a signal."""
        self._asked = next(_QUESTIONS)
        asked, relay = self._asked, _relay()

        def work():
            relay.found.emit(asked, frontapp.program_name())

        threading.Thread(target=work, daemon=True).start()

    def _found(self, asked, name):
        # Only this editor's newest question: a slow earlier one must not land
        # on top of it, and another editor's is not this one's to show.
        if asked != self._asked:
            return
        self.found.setText(t("In front: {name}", name=name) if name
                           else t("The program in front could not be found out."))
