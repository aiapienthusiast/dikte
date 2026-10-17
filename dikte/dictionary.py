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
dictionary sees no change. When the list is edited, the migration stays
lossless: every piece is its own record, duplicates included, and each record
remembers how many line breaks stood before it, so the text renders back with
the lines it was typed in.
"""

import copy
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
# Candidates still being counted. Past this the ones not seen for longest are
# forgotten, whatever their count, so the file cannot grow without bound and a
# word seen for the first time is always counted.
PENDING_LIMIT = 200
# Learned words the user deleted, which are not learned again.
REJECTED_LIMIT = 500
# How much of the dictionary the transcription model is told, in characters.
# Whisper reads at most 224 tokens of prompt and keeps the end of a longer one;
# 600 characters stays inside that even for text that tokenises badly. The
# typed entries go first, so learning can never push them out. The cleanup
# glossary has no such limit and carries everything.
HINT_BUDGET = 600

_LOCK = threading.Lock()


def path():
    """Read from cfg each time, so a test that moves the config moves this too."""
    return cfg.CONFIG_DIR / "dictionary.json"


def pieces(text):
    """The pieces of a free text dictionary, one per line or comma, each with
    the number of line breaks that stood before it."""
    out, breaks = [], 0
    for line in (text or "").splitlines():
        for piece in line.split(","):
            piece = piece.strip()
            if piece:
                out.append((piece, breaks if out else 0))
                breaks = 0
        breaks += 1
    return out


def split(text):
    return [piece for piece, _ in pieces(text)]


def entry(term, spelling="", source=MANUAL, breaks=0):
    """A clean record, or None when there is no term to keep."""
    term = " ".join(str(term or "").replace(",", " ").split())
    spelling = " ".join(str(spelling or "").replace(",", " ").split())
    if not term:
        return None
    if spelling == term:
        spelling = ""
    if isinstance(breaks, bool) or not isinstance(breaks, int) or breaks < 0:
        breaks = 0
    return {"term": term, "spelling": spelling,
            "source": AUTO if source == AUTO else MANUAL, "breaks": min(breaks, 9)}


def hint_of(record):
    """What the transcription model is told: the spelling to aim for."""
    return record["spelling"] or record["term"]


def gloss_of(record):
    """What the cleanup model is told. An arrow, the way the cleanup prompt's
    own examples show a word before and after."""
    if record["spelling"]:
        return f"{record['term']} -> {record['spelling']}"
    return record["term"]


def _render(records, show):
    out = []
    for record in records:
        if out:
            out.append("\n" * record.get("breaks", 0) or ", ")
        out.append(show(record))
    return "".join(out)


def render_hint(records):
    """Every record, laid out the way the text was typed: what the setting
    holds."""
    return _render(records, hint_of)


def render_glossary(records):
    return _render(records, gloss_of)


def budget_hint(records, budget=HINT_BUDGET):
    """The transcription hint: the typed records, then the learned ones, cut
    at a record boundary once the budget is reached."""
    ordered = ([r for r in records if r["source"] != AUTO]
               + [dict(r, breaks=0) for r in records if r["source"] == AUTO])
    kept = []
    for record in ordered:
        if kept and len(_render(kept + [record], hint_of)) > budget:
            break
        kept.append(record)
    return _render(kept, hint_of)


def _key(text):
    """Without regard to case, and with Turkish's dotted capital I folded to a
    plain i rather than to an i and a combining dot."""
    return text.casefold().replace("i̇", "i")


def merge(edited, extra):
    """`edited` as it is, then each of `extra` whose term it does not have."""
    seen = {_key(r["term"]) for r in edited}
    out = list(edited)
    for record in extra:
        if _key(record["term"]) not in seen:
            seen.add(_key(record["term"]))
            out.append(record)
    return out


def _clean_records(raw):
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw:
        if isinstance(item, dict):
            record = entry(item.get("term"), item.get("spelling"),
                           item.get("source"), item.get("breaks", 0))
            if record is not None:
                out.append(record)
    return out


def _clean_pending(raw):
    """{term: [count, last seen]}. A bare count is an older file's."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for term, value in raw.items():
        if isinstance(value, int) and not isinstance(value, bool):
            value = [value, 0]
        if (isinstance(term, str) and isinstance(value, list) and len(value) == 2
                and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
                and value[0] > 0):
            out[term] = list(value)
    return out


# The last file read, by its modification time and size: each dictation asks
# for the hint and the glossary, and a learning run reads it again, so this
# saves reading and parsing the same few kilobytes three times over.
_cache = {"stamp": None, "data": None}


def _read_file():
    """The stored state, or None when there is no file to read.

    A file that cannot be parsed counts as no file: the setting still holds
    the rendered text, which is the dictionary minus what was learned since
    it was last saved. A copy every time, so a caller may change it."""
    try:
        status = os.stat(path())
    except OSError:
        return None
    stamp = (str(path()), status.st_ino, status.st_mtime_ns, status.st_size)
    if _cache["stamp"] != stamp:
        try:
            with open(path(), encoding="utf-8") as fh:
                data = json.load(fh)
        except (OSError, ValueError):
            data = None
        if isinstance(data, dict):
            synced = data.get("synced")
            rejected = data.get("rejected")
            clock = data.get("clock")
            data = {
                "entries": _clean_records(data.get("entries")),
                "pending": _clean_pending(data.get("pending")),
                "synced": synced if isinstance(synced, str) else "",
                "rejected": ([x for x in rejected if isinstance(x, str)][-REJECTED_LIMIT:]
                             if isinstance(rejected, list) else []),
                "clock": clock if isinstance(clock, int) and not isinstance(clock, bool) else 0,
            }
        else:
            data = None
        _cache["stamp"], _cache["data"] = stamp, data
    return copy.deepcopy(_cache["data"])


def _reconcile(records, text, synced):
    """The records rebuilt from a setting edited outside the dictionary.

    Each piece takes the first record not yet used whose hint it is, so a
    spelling or an Auto mark survives an edit that did not touch it, and two
    records sharing a spelling stay two. A learned record the setting never
    held (learning does not write config.json) is kept too: an edit to a text
    it was never in cannot have meant to remove it."""
    available = list(records)
    rebuilt = []
    for piece, breaks in pieces(text):
        for i, record in enumerate(available):
            if hint_of(record) == piece:
                rebuilt.append(dict(available.pop(i), breaks=breaks))
                break
        else:
            rebuilt.append(entry(piece, breaks=breaks))
    before = set(split(synced))
    rebuilt += [r for r in available
                if r["source"] == AUTO and hint_of(r) not in before]
    return rebuilt


def _text(conf):
    value = conf["transcribe_prompt"]
    return value if isinstance(value, str) else ""


def _state(conf, stored=None):
    """(records, stored, exists), agreeing with the setting as it is now."""
    stored = _read_file() if stored is None else stored
    text = _text(conf)
    if stored is None:
        return [entry(p, breaks=b) for p, b in pieces(text)], None, False
    if text.strip() != stored["synced"].strip():
        return _reconcile(stored["entries"], text, stored["synced"]), stored, True
    return stored["entries"], stored, True


def records(conf):
    """Every record, in the order they are listed."""
    return list(_state(conf)[0])


def hint(conf):
    """The transcription hint. The stored text itself until there is a file."""
    found, _, exists = _state(conf)
    if not exists:
        return _text(conf)
    return budget_hint(found)


def glossary(conf):
    """The cleanup glossary, stripped the way the setting always was."""
    found, _, exists = _state(conf)
    if not exists:
        return _text(conf).strip()
    return render_glossary(found)


def _write(entries, pending, synced, rejected=(), clock=0):
    # Forgotten first: two writes inside one tick of the file system clock can
    # leave the same time and size behind, and only this process writes here.
    _cache["stamp"] = None
    cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    target = path()
    tmp = target.with_suffix(".json.tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"version": 1, "entries": entries, "pending": pending,
                   "synced": synced, "rejected": list(rejected)[-REJECTED_LIMIT:],
                   "clock": clock},
                  fh, ensure_ascii=False, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.chmod(tmp, 0o600)
    cfg._replace_with_retry(tmp, target)


def _empty():
    return {"entries": [], "pending": {}, "synced": "", "rejected": [], "clock": 0}


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
    what write() needs."""
    with _LOCK:
        current, _, _ = _state(conf)
        merged = merge(list(edited), _arrived(current, loaded))
        text = render_hint(merged)
        conf["transcribe_prompt"] = text
        return merged, list(loaded), text


def write(pending, forget=False):
    """The second half of save(), once config.json is on disk.

    The file is read again under the lock, since learning may have added to it
    while config.json was being written, and what it added is kept the same
    way save() kept what arrived before. `forget` empties the names still
    being counted."""
    merged, loaded, text = pending
    with _LOCK:
        stored = _read_file() or _empty()
        merged = merge(merged, _arrived(stored["entries"], loaded))
        # A learned record the user took out is remembered as refused, so the
        # same word is not learned straight back three dictations later.
        kept = {_key(r["term"]) for r in merged}
        rejected = list(stored["rejected"])
        for record in loaded:
            key = _key(record["term"])
            if record["source"] == AUTO and key not in kept and key not in rejected:
                rejected.append(key)
        _write(merged, {} if forget else stored["pending"], text, rejected,
               stored["clock"])
        return merged


def store(conf, edited, loaded, forget=False):
    """save() and write() for a caller with nothing else to save in between."""
    pending = save(conf, edited, loaded)
    conf.save()
    return write(pending, forget)


def forget_pending():
    """Drop every name still being counted. They are words out of dictations,
    kept only for learning, and go when learning or the history does."""
    with _LOCK:
        stored = _read_file()
        if stored is None or not stored["pending"]:
            return
        _write(stored["entries"], {}, stored["synced"], stored["rejected"],
               stored["clock"])


def clear_auto(entries):
    return [r for r in entries if r["source"] != AUTO]


# --- learning ---------------------------------------------------------------

# Words that are written with a capital in the middle of a sentence without
# being anybody's name: the pronoun, the days, the months, and the languages
# and nationalities, which English capitalises and a Turkish speaker mixing
# languages often does too. Everything else a title-case word mid-sentence
# usually is, in English and in Turkish, is a name.
COMMON = set("""
i i'm i've i'll i'd
monday tuesday wednesday thursday friday saturday sunday
january february march april may june july august september october
november december
pazartesi salı çarşamba perşembe cuma cumartesi pazar
ocak şubat mart nisan mayıs haziran temmuz ağustos eylül ekim kasım aralık
english british american turkish german french spanish italian portuguese
dutch polish russian ukrainian arabic persian kurdish greek chinese japanese
korean hindi european african asian
ingilizce türkçe almanca fransızca ispanyolca italyanca portekizce felemenkçe
lehçe rusça ukraynaca arapça farsça kürtçe yunanca çince japonca korece
ingiliz amerikan amerikalı türk alman fransız ispanyol italyan rus arap
kürt yunan japon koreli çinli avrupalı
""".split())
COMMON = {_key(w) for w in COMMON}

# German writes every noun with a capital, so the signal means nothing there.
_NOUN_CAPITALS = {"de", "lb"}

_WORD = re.compile(r"[^\W\d_][\w]*(?:[-.][\w]+)*", re.UNICODE)
# What can stand before the first word of a sentence, a line or a list item.
_SENTENCE_END = set(".!?:;\n\"“”‘«»(-*•")


def _titled(word):
    """Title case or camel case, with at least one lower case letter: a name.
    An acronym in capitals is left alone; those are mostly common words."""
    return (len(word) >= 3 and word[0].isupper()
            and any(c.islower() for c in word[1:]))


def runs(text):
    """Runs of title-case words, each with whether it began a sentence.

    A run that begins a sentence is kept whole rather than losing its first
    word: "Claude Code is great" is the run "Claude Code" at a start, not the
    name "Code"."""
    found, run = [], []
    run_starts = False
    previous_end = 0

    def close():
        if run:
            found.append((" ".join(run), run_starts))
            if run_starts and len(run) > 1:
                # The first word may only be capitalised for starting the
                # sentence ("Bunu Yusuf'un"), so the rest is offered too, as
                # a start as well: it counts only once seen mid-sentence.
                found.append((" ".join(run[1:]), True))
            run.clear()

    for match in _WORD.finditer(text):
        word = match.group(0)
        # Spaces and tabs only: a line break is itself a start. Read from the
        # gap since the last word, so the text is walked once.
        gap = text[previous_end:match.start()]
        trimmed = gap.rstrip(" \t")
        starts = (trimmed[-1] in _SENTENCE_END) if trimmed else previous_end == 0
        previous_end = match.end()
        # A Turkish suffix hangs off a name after an apostrophe (Yusuf'un), and
        # the word pattern stops at the apostrophe, so the name arrives bare and
        # the suffix as a lower case word of its own that ends the run.
        if _titled(word) and _key(word) not in COMMON:
            if run and gap.strip():
                close()
            if not run:
                run_starts = starts
            run.append(word)
        else:
            close()
    close()
    return found


def candidates(text):
    """Names and terms in a text: runs of title-case words that do not start a
    sentence. "We moved it to Claude Code yesterday" gives "Claude Code"."""
    return [term for term, starts in runs(text) if not starts]


def learn(conf, raw, text, speech_language=""):
    """Count the names in one finished dictation, and learn the ones that have
    now been seen often enough. Returns the records added.

    Only a term that both the transcriber heard and the cleanup kept counts:
    the transcript and the cleaned text agree on it, which is what a real name
    looks like and a misheard word mostly does not. Each term counts once per
    dictation. A run at the start of a sentence counts only once the same run
    has been seen in the middle of one, here or in an earlier dictation. The
    caller decides which dictations are eligible at all."""
    if (speech_language or "").lower()[:2] in _NOUN_CAPITALS:
        return []
    heard = {_key(w) for w in _WORD.findall(raw)}
    found = [(term, starts) for term, starts in runs(text)
             if all(_key(w) in heard for w in term.split())]
    if not found:
        return []
    with _LOCK:
        current, stored, _ = _state(conf)
        stored = stored or _empty()
        pending, rejected = stored["pending"], stored["rejected"]
        clock = stored["clock"] + 1
        middle = {term for term, starts in found if not starts}
        terms = []
        for term, starts in found:
            if starts and term not in middle and term not in pending:
                continue
            if term not in terms:
                terms.append(term)
        if not terms:
            return []
        known = set(rejected)
        for record in current:
            known.add(_key(record["term"]))
            known.add(_key(hint_of(record)))
        added = []
        for term in terms:
            if _key(term) in known:
                continue
            count = pending.get(term, [0, 0])[0] + 1
            if count >= LEARN_AFTER:
                pending.pop(term, None)
                record = entry(term, source=AUTO)
                current.append(record)
                known.add(_key(term))
                added.append(record)
            else:
                pending[term] = [count, clock]
        if len(pending) > PENDING_LIMIT:
            # Least recently seen out first, whatever the count.
            recent = sorted(pending.items(), key=lambda item: -item[1][1])
            pending = dict(recent[:PENDING_LIMIT])
        # The setting is left as it is: this thread does not write config.json.
        # The file says which text it agrees with, so the two stay in step.
        _write(current, pending, _text(conf), rejected, clock)
        return added
