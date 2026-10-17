"""The personal dictionary: names and terms, one record each.

It used to be one free text box, the `transcribe_prompt` setting, handed as it
was to the transcription model as a hint and to the cleanup model as a
glossary. It is still both of those. What changed is that the text is now
rendered from records, so that one entry can be found, corrected or removed,
and so that an entry Dikte learned on its own can be told apart from one the
user typed.

The records live in dictionary.json next to config.json rather than inside it.
Learning adds to them from the dictation thread, and config.json is written
whole by the settings window and by the command line from another process; a
file of its own, behind a lock of its own, is what keeps a learned entry from
being written over by a save that never knew about it.

`transcribe_prompt` is kept, holding the rendered text. An older version, or
`dikte config set transcribe_prompt`, keeps working on it, and the file
remembers which text it last agreed with (`synced`). When the setting no longer
matches, somebody edited it outside the dictionary, and the setting wins: its
pieces become the records, keeping whatever an unchanged piece already knew
about itself.

Until the file exists nothing is rendered at all: the hint and the glossary are
the stored text exactly as before, byte for byte. A user who never opens the
dictionary sees no change.
"""

import json
import os
import re
import threading

from . import config as cfg

MANUAL, AUTO = "manual", "auto"

# How many separate dictations a term has to survive before it is learned. One
# sighting is a coincidence, and a misheard word repeats less often than a real
# name does.
LEARN_AFTER = 3
# Candidates still being counted. Past this the least seen are forgotten, so
# the file cannot grow without bound from words that never come back.
PENDING_LIMIT = 200

_LOCK = threading.Lock()


def path():
    """Read from cfg each time, so a test that moves the config moves this too."""
    return cfg.CONFIG_DIR / "dictionary.json"


def split(text):
    """The pieces of a free text dictionary: one per line or comma.

    The pieces keep their order and their words, which is what makes the
    migration lossless: rendered back, they read as the same list with commas
    between them."""
    out = []
    for line in (text or "").splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                out.append(piece)
    return out


def entry(term, spelling="", source=MANUAL):
    """A clean record, or None when there is no term to keep."""
    term = " ".join(str(term or "").replace(",", " ").split())
    spelling = " ".join(str(spelling or "").replace(",", " ").split())
    if not term:
        return None
    if spelling == term:
        spelling = ""
    return {"term": term, "spelling": spelling,
            "source": AUTO if source == AUTO else MANUAL}


def hint_of(record):
    """What the transcription model is told: the spelling to aim for."""
    return record["spelling"] or record["term"]


def gloss_of(record):
    """What the cleanup model is told. An arrow, the way the cleanup prompt's
    own examples show a word before and after."""
    if record["spelling"]:
        return f"{record['term']} -> {record['spelling']}"
    return record["term"]


def render_hint(records):
    return ", ".join(hint_of(r) for r in records)


def render_glossary(records):
    return ", ".join(gloss_of(r) for r in records)


def _key(text):
    return text.casefold()


def dedupe(records):
    """First of each term wins, compared without regard to case."""
    seen, out = set(), []
    for record in records:
        if record is None:
            continue
        key = _key(record["term"])
        if key in seen:
            continue
        seen.add(key)
        out.append(record)
    return out


def _clean_records(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            out.append(entry(item.get("term"), item.get("spelling"),
                             item.get("source")))
    return dedupe(out)


def _clean_pending(raw):
    if not isinstance(raw, dict):
        return {}
    out = {}
    for term, count in raw.items():
        if isinstance(term, str) and isinstance(count, int) and count > 0:
            out[term] = count
    return out


def _read_file():
    """The stored state, or None when there is no file to read.

    A file that cannot be parsed counts as no file: the setting still holds
    the rendered text, which is the dictionary minus what was learned since
    it was last saved."""
    try:
        with open(path(), encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    synced = data.get("synced")
    return {
        "entries": _clean_records(data.get("entries")),
        "pending": _clean_pending(data.get("pending")),
        "synced": synced if isinstance(synced, str) else "",
    }


def _reconcile(records, text):
    """The records rebuilt from a setting edited outside the dictionary.

    A piece that is the hint of a record already known keeps that record, so
    a spelling or an Auto mark survives an edit that did not touch it."""
    known = {}
    for record in records:
        known.setdefault(hint_of(record), record)
    return dedupe([known.get(piece) or entry(piece) for piece in split(text)])


def _state(conf):
    """(records, pending, exists), agreeing with the setting as it is now."""
    stored = _read_file()
    text = conf["transcribe_prompt"] if isinstance(conf["transcribe_prompt"], str) else ""
    if stored is None:
        return dedupe([entry(p) for p in split(text)]), {}, False
    if text.strip() != stored["synced"].strip():
        return _reconcile(stored["entries"], text), stored["pending"], True
    return stored["entries"], stored["pending"], True


def records(conf):
    """Every record, in the order they are listed."""
    return list(_state(conf)[0])


def hint(conf):
    """The transcription hint. The stored text itself until there is a file."""
    found, _, exists = _state(conf)
    if not exists:
        text = conf["transcribe_prompt"]
        return text if isinstance(text, str) else ""
    return render_hint(found)


def glossary(conf):
    """The cleanup glossary, stripped the way the setting always was."""
    found, _, exists = _state(conf)
    if not exists:
        text = conf["transcribe_prompt"]
        return text.strip() if isinstance(text, str) else ""
    return render_glossary(found)


def _write(entries, pending, synced):
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = path()
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": entries, "pending": pending,
                   "synced": synced}, fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    cfg._replace_with_retry(tmp, target)


def _arrived(current, loaded):
    """Records on disk that the window never loaded: learned while it was open."""
    seen = {_key(r["term"]) for r in loaded}
    return [r for r in current if _key(r["term"]) not in seen]


def save(conf, edited, loaded):
    """Store what the settings window holds, and the setting with it.

    `loaded` is what the window was given when it read the dictionary. A record
    on disk now that was not there then was learned while the window was open,
    and is kept: the user never saw it, so leaving it out is not a deletion.

    The setting is written into `conf` here and saved by the caller with the
    rest of the window, before this file is: should that save fail, the file
    still agrees with the old setting, and nothing is lost either way. Returns
    what write() needs, the records as they are now among it."""
    with _LOCK:
        current, _, _ = _state(conf)
        merged = dedupe(list(edited) + _arrived(current, loaded))
        text = render_hint(merged)
        conf["transcribe_prompt"] = text
        return merged, list(loaded), text


def write(pending):
    """The second half of save(), once config.json is on disk.

    The file is read again under the lock, since learning may have added to it
    while config.json was being written, and what it added is kept the same
    way save() kept what arrived before."""
    merged, loaded, text = pending
    with _LOCK:
        stored = _read_file() or {"entries": [], "pending": {}}
        merged = dedupe(merged + _arrived(stored["entries"], loaded))
        _write(merged, stored["pending"], text)
        return merged


def store(conf, edited, loaded):
    """save() and write() for a caller with nothing else to save in between."""
    pending = save(conf, edited, loaded)
    conf.save()
    return write(pending)


def clear_auto(entries):
    return [r for r in entries if r["source"] != AUTO]


# --- learning ---------------------------------------------------------------

# Words that are written with a capital in the middle of a sentence without
# being anybody's name: the pronoun, the days and the months. Everything else a
# title-case word mid-sentence usually is, in English and in Turkish, is a name.
COMMON = set("""
i i'm i've i'll i'd
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october
november december
pazartesi salı çarşamba perşembe cuma cumartesi pazar
ocak şubat mart nisan mayıs haziran temmuz ağustos eylül ekim kasım aralık
""".split())

# German writes every noun with a capital, so the signal means nothing there.
_NOUN_CAPITALS = {"de", "lb"}

_WORD = re.compile(r"[^\W\d_][\w]*(?:[-.][\w]+)*", re.UNICODE)
_SENTENCE_END = set(".!?:;\n\"“”‘«»(")


def _titled(word):
    """Title case or camel case, with at least one lower case letter: a name.
    An acronym in capitals is left alone; those are mostly common words."""
    return (len(word) >= 3 and word[0].isupper()
            and any(c.islower() for c in word[1:]))


def candidates(text):
    """Names and terms in a text: runs of title-case words that do not start a
    sentence. "We moved it to Claude Code yesterday" gives "Claude Code"."""
    found, run = [], []
    previous_end = 0

    def close():
        if run:
            found.append(" ".join(run))
            run.clear()

    for match in _WORD.finditer(text):
        word = match.group(0)
        gap = text[previous_end:match.start()]
        previous_end = match.end()
        before = text[:match.start()].rstrip()
        starts = not before or before[-1] in _SENTENCE_END
        # A Turkish suffix hangs off a name after an apostrophe (Yusuf'un), and
        # the word pattern stops at the apostrophe, so the name arrives bare and
        # the suffix as a lower case word of its own that ends the run.
        if _titled(word) and word.casefold() not in COMMON and not starts:
            if run and gap.strip():
                close()
            run.append(word)
        else:
            close()
    close()
    return found


def learn(conf, raw, text, speech_language=""):
    """Count the names in one finished dictation, and learn the ones that have
    now been seen often enough. Returns the records added.

    Only a term that both the transcriber heard and the cleanup kept counts:
    the transcript and the cleaned text agree on it, which is what a real name
    looks like and a misheard word mostly does not. Each term counts once per
    dictation. The caller decides which dictations are eligible at all."""
    if (speech_language or "").lower()[:2] in _NOUN_CAPITALS:
        return []
    heard = {_key(w) for w in _WORD.findall(raw)}
    terms = []
    for term in candidates(text):
        if all(_key(w) in heard for w in term.split()) and term not in terms:
            terms.append(term)
    if not terms:
        return []
    with _LOCK:
        current, pending, exists = _state(conf)
        known = set()
        for record in current:
            known.add(_key(record["term"]))
            known.add(_key(hint_of(record)))
        added = []
        for term in terms:
            if _key(term) in known:
                continue
            count = pending.get(term, 0) + 1
            if count >= LEARN_AFTER:
                pending.pop(term, None)
                record = entry(term, source=AUTO)
                current.append(record)
                known.add(_key(term))
                added.append(record)
            else:
                pending[term] = count
        if len(pending) > PENDING_LIMIT:
            keep = sorted(pending.items(), key=lambda item: -item[1])[:PENDING_LIMIT]
            pending = dict(keep)
        # The setting is left as it is: this thread does not write config.json.
        # The file says which text it agrees with, so the two stay in step.
        text_now = conf["transcribe_prompt"] if isinstance(
            conf["transcribe_prompt"], str) else ""
        _write(current, pending, text_now)
        return added
