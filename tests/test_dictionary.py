"""The personal dictionary: records, the setting they are kept in step with,
and what learning may add to them."""

import json

from dikte import config as cfg
from dikte import dictionary
from tests.support import DikteTest


class Migration(DikteTest):
    def test_untouched_text_is_passed_on_exactly_as_it_was(self):
        """No file yet: the hint and the glossary are the stored text itself,
        line breaks and all, so a user who never opens the dictionary sees
        nothing change."""
        conf = self.config(transcribe_prompt="Paraşüt\nOpenFrame, Dikte  ")
        self.assertEqual(conf.dictionary_hint(), "Paraşüt\nOpenFrame, Dikte  ")
        self.assertEqual(conf.dictionary_glossary(), "Paraşüt\nOpenFrame, Dikte")
        self.assertFalse(dictionary.path().exists())

    def test_the_text_becomes_one_record_per_line_or_comma(self):
        conf = self.config(transcribe_prompt="Paraşüt, OpenFrame\n\nYusuf İpek")
        found = dictionary.records(conf)
        self.assertEqual([r["term"] for r in found],
                         ["Paraşüt", "OpenFrame", "Yusuf İpek"])
        self.assertEqual({r["source"] for r in found}, {dictionary.MANUAL})

    def test_rendered_back_it_reads_as_the_same_list(self):
        text = "Paraşüt, OpenFrame, Yusuf İpek"
        conf = self.config(transcribe_prompt=text)
        self.assertEqual(dictionary.render_hint(dictionary.records(conf)), text)

    def test_a_repeated_term_is_kept_once(self):
        conf = self.config(transcribe_prompt="Dikte, dikte, DIKTE")
        self.assertEqual([r["term"] for r in dictionary.records(conf)], ["Dikte"])


class Records(DikteTest):
    def stored(self, conf, entries, loaded=()):
        return dictionary.store(conf, [dictionary.entry(*e) for e in entries],
                                list(loaded))

    def test_a_spelling_reaches_the_hint_and_the_glossary_differently(self):
        conf = self.config()
        self.stored(conf, [("kube control", "kubectl"), ("Paraşüt",)])
        self.assertEqual(conf.dictionary_hint(), "kubectl, Paraşüt")
        self.assertEqual(conf.dictionary_glossary(), "kube control -> kubectl, Paraşüt")
        self.assertIn("kube control -> kubectl", conf.cleanup_prompt())
        # The setting carries the hint, for an older version to read.
        self.assertEqual(self.read_config_file()["transcribe_prompt"],
                         "kubectl, Paraşüt")

    def test_an_edit_from_the_command_line_wins_over_the_file(self):
        conf = self.config()
        self.stored(conf, [("kube control", "kubectl"), ("Paraşüt",)])
        conf["transcribe_prompt"] = "kubectl, OpenFrame"
        found = dictionary.records(conf)
        # The piece that did not change keeps what it knew about itself.
        self.assertEqual([(r["term"], r["spelling"]) for r in found],
                         [("kube control", "kubectl"), ("OpenFrame", "")])

    def test_a_save_keeps_what_was_learned_while_the_window_was_open(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        dictionary._write(loaded + [dictionary.entry("Claude Code", source="auto")],
                          {}, conf["transcribe_prompt"])
        merged = self.stored(conf, [("Paraşüt",), ("OpenFrame",)], loaded)
        self.assertEqual([r["term"] for r in merged],
                         ["Paraşüt", "OpenFrame", "Claude Code"])

    def test_what_is_learned_while_config_json_is_written_is_kept_too(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",)])
        pending = dictionary.save(conf, loaded + [dictionary.entry("OpenFrame")], loaded)
        dictionary._write(loaded + [dictionary.entry("Claude", source="auto")],
                          {"Grafana": 2}, "Paraşüt")
        merged = dictionary.write(pending)
        self.assertEqual([r["term"] for r in merged], ["Paraşüt", "OpenFrame", "Claude"])
        stored = json.loads(dictionary.path().read_text(encoding="utf-8"))
        self.assertEqual(stored["pending"], {"Grafana": 2})

    def test_a_deletion_in_the_window_is_a_deletion(self):
        conf = self.config()
        loaded = self.stored(conf, [("Paraşüt",), ("OpenFrame",)])
        merged = self.stored(conf, [("OpenFrame",)], loaded)
        self.assertEqual([r["term"] for r in merged], ["OpenFrame"])

    def test_a_broken_file_falls_back_to_the_setting(self):
        conf = self.config(transcribe_prompt="Paraşüt")
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text("{not json", encoding="utf-8")
        self.assertEqual(conf.dictionary_hint(), "Paraşüt")

    def test_malformed_records_are_dropped_not_fatal(self):
        conf = self.config()
        cfg.CONFIG_DIR.mkdir(parents=True, exist_ok=True)
        dictionary.path().write_text(json.dumps({
            "entries": [{"term": "Dikte"}, "junk", {"term": ""}, {"no": 1}],
            "pending": {"x": "many"}, "synced": "",
        }), encoding="utf-8")
        self.assertEqual([r["term"] for r in dictionary.records(conf)], ["Dikte"])

    def test_clearing_the_learned_ones_keeps_the_typed_ones(self):
        kept = dictionary.clear_auto([dictionary.entry("A b"),
                                      dictionary.entry("Claude", source="auto")])
        self.assertEqual([r["term"] for r in kept], ["A b"])


class Learning(DikteTest):
    def learn(self, conf, text, times=1, raw=None, language="en"):
        added = []
        for _ in range(times):
            added += dictionary.learn(conf, raw if raw is not None else text,
                                      text, language)
        return added

    def test_a_name_mid_sentence_is_found_as_a_run(self):
        self.assertEqual(
            dictionary.candidates("We moved it to Claude Code yesterday. "
                                  "Then OpenFrame broke."),
            ["Claude Code", "OpenFrame"])

    def test_sentence_starts_days_and_acronyms_are_not_names(self):
        self.assertEqual(
            dictionary.candidates("Today I call the API on Monday. Hello there."),
            [])

    def test_a_turkish_suffix_is_left_off(self):
        self.assertEqual(dictionary.candidates("Bunu Yusuf'un bilgisayarında yaptım."),
                         ["Yusuf"])

    def test_three_dictations_make_an_entry(self):
        conf = self.config()
        text = "I pushed the fix to OpenFrame this morning."
        self.assertEqual(self.learn(conf, text, times=2), [])
        added = self.learn(conf, text)
        self.assertEqual([(r["term"], r["source"]) for r in added],
                         [("OpenFrame", "auto")])
        self.assertIn("OpenFrame", conf.dictionary_hint())
        # Seen again, it is already known.
        self.assertEqual(self.learn(conf, text, times=3), [])

    def test_a_word_the_cleanup_made_up_is_not_learned(self):
        conf = self.config()
        added = self.learn(conf, "I asked Claude about it.", times=3,
                           raw="I asked cloud about it.")
        self.assertEqual(added, [])

    def test_german_capitals_say_nothing(self):
        conf = self.config()
        self.assertEqual(self.learn(conf, "Ich habe den Hund gesehen.", 3,
                                    language="de"), [])

    def test_a_term_already_in_the_dictionary_is_not_added_again(self):
        conf = self.config(transcribe_prompt="openframe")
        self.assertEqual(self.learn(conf, "It runs on OpenFrame now.", 3), [])

    def test_learning_leaves_config_json_alone(self):
        """It runs on the dictation thread; the settings file is not its to
        write, and the dictionary file keeps the two in step."""
        conf = self.config(transcribe_prompt="Paraşüt")
        self.learn(conf, "It runs on OpenFrame now.", 3)
        self.assertFalse(cfg.CONFIG_FILE.exists())
        self.assertEqual(conf.dictionary_hint(), "Paraşüt, OpenFrame")
