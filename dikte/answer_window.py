"""The window a question about selected text answers in.

Three parts kept apart on screen, the way they were kept apart in the prompt:
what was said, what it was said about, and what came back. Apply is there only
for an edit of a selection that can be pasted back; Copy is always there; and
Cancel, Escape or closing the window leave the original text exactly as it was.
Nothing is pasted until Apply is pressed.
"""

import html
import threading

from PyQt6.QtCore import Qt, pyqtSignal
from PyQt6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
                             QPushButton, QVBoxLayout)

from . import paste
from .i18n import t

# How much of the selection the tooltip shows. The whole of it went to the
# agent; this is only there to say which text that was.
PREVIEW_CHARS = 1500


def _tip(text):
    """A tooltip that shows `text` as typed: Qt reads tooltips as rich text."""
    return "<qt>" + html.escape(text) + "</qt>"


def _one_line(text, limit=90):
    text = " ".join(str(text).split())
    return text if len(text) <= limit else text[:limit - 1] + "…"


class AnswerWindow(QDialog):
    # Carried out on a thread of its own; this is how a failure comes back.
    apply_failed = pyqtSignal(str)
    copied = pyqtSignal(str)

    def __init__(self, instruction, answer, selected="", can_apply=False,
                 apply_fn=None, agent="", parent=None):
        super().__init__(parent)
        self._apply_fn = apply_fn
        self._thread = None
        self.setWindowTitle(t("Dikte: {name}", name=agent) if agent else "Dikte")
        self.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        self.resize(520, 360)

        layout = QVBoxLayout(self)
        self.instruction = QLabel(t("You said: {text}", text=_one_line(instruction)))
        # Plain text, always: a transcript or a selection that happens to look
        # like markup must not be rendered as such.
        self.instruction.setTextFormat(Qt.TextFormat.PlainText)
        self.instruction.setWordWrap(True)
        self.instruction.setToolTip(_tip(instruction))
        layout.addWidget(self.instruction)

        self.selection = QLabel(t("About the selection ({count} characters)",
                                  count=len(selected)))
        self.selection.setTextFormat(Qt.TextFormat.PlainText)
        preview = selected[:PREVIEW_CHARS] + ("…" if len(selected) > PREVIEW_CHARS else "")
        self.selection.setToolTip(_tip(preview))
        self.selection.setVisible(bool(selected))
        layout.addWidget(self.selection)

        self.answer = QPlainTextEdit()
        self.answer.setPlainText(answer)
        # An edit can be touched up before it goes back; an answer is read.
        self.answer.setReadOnly(not can_apply)
        layout.addWidget(self.answer, 1)

        row = QHBoxLayout()
        row.addStretch(1)
        self.apply_button = QPushButton(t("Apply"))
        self.apply_button.setToolTip(t(
            "Replaces the selected text in the window it came from. "
            "Nothing is pasted until you press this."))
        self.apply_button.clicked.connect(self.apply)
        self.apply_button.setVisible(bool(can_apply and apply_fn))
        self.copy_button = QPushButton(t("Copy"))
        self.copy_button.clicked.connect(self.copy)
        self.cancel_button = QPushButton(t("Cancel"))
        self.cancel_button.setToolTip(t("Closes this and leaves the original text alone."))
        self.cancel_button.clicked.connect(self.reject)
        for button in (self.apply_button, self.copy_button, self.cancel_button):
            row.addWidget(button)
        layout.addLayout(row)
        (self.apply_button if self.apply_button.isVisibleTo(self)
         else self.copy_button).setDefault(True)

    @property
    def can_apply(self):
        return self.apply_button.isVisibleTo(self)

    def text(self):
        return self.answer.toPlainText()

    def apply(self):
        """Hide, so the selection's window has the focus back, then paste."""
        if not self.can_apply:
            return
        text = self.text()
        self.hide()

        def work():
            try:
                self._apply_fn(text)
            except paste.PasteError as exc:
                self.apply_failed.emit(t("Copied, but pasting failed: {error}",
                                         error=exc))

        self._thread = threading.Thread(target=work, daemon=True)
        self._thread.start()
        self.accept()

    def copy(self):
        try:
            paste.copy(self.text())
        except paste.PasteError as exc:
            self.copy_button.setToolTip(_tip(str(exc)))
            self.copy_button.setText(t("Copy failed"))
            return
        self.copy_button.setText(t("Copied"))
        self.copied.emit(self.text())
