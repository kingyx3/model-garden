"""Credential-free checks for the language fallback rule in knowledge search and for look-alike control text (#239, #235)."""
from __future__ import annotations

import pathlib
import sys
import unittest

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import test_coworker_pause as paused  # noqa: E402
import test_knowledge_permissions as knowledge_tests  # noqa: E402
import test_unanswered as unanswered_tests  # noqa: E402

K = knowledge_tests.K
TODAY = knowledge_tests.TODAY
pause = paused.pause
NEUTRAL = "Only an administrator can pause or resume me, in a direct message."


class FallbackTests(unittest.TestCase):
    state = knowledge_tests.LocaleTests.state

    def test_an_uncovered_language_names_the_covered_ones_and_forbids_presenting_a_translation_as_approved(self):
        state = self.state(("english", {"locale": "en-SG"}), ("chinese", {"locale": "zh"}))
        result = K.search_selected(state, "opening hours", today=TODAY, locale="ms")
        self.assertEqual((result["documents"], result["reason"]), ([], "source-other-locale"))
        self.assertEqual(result["availableLocales"], ["en-SG", "zh"])
        self.assertIn("Do not translate", result["localeGuidance"])
        self.assertIn("offer a message or a person", result["localeGuidance"])

    def test_nothing_extra_is_added_when_a_source_answers_or_no_language_was_asked_or_the_miss_is_not_about_language(self):
        covered = self.state(("english", {"locale": "en"}), ("neutral", {}))  # an unlabelled source matches every request
        found = K.search_selected(covered, "opening hours", today=TODAY, locale="ms")
        self.assertTrue(found["documents"])
        self.assertNotIn("availableLocales", found)
        english = self.state(("english", {"locale": "en"}))
        self.assertNotIn("availableLocales", K.search_selected(english, "opening hours", today=TODAY))
        self.assertNotIn("availableLocales", K.search_selected(english, "zzzz qqqq nothing", today=TODAY, locale="en"))  # right language, no match

    def test_the_names_are_declared_tags_only_unique_and_sorted(self):
        state = self.state(("a", {"locale": "zh"}), ("b", {"locale": "en"}), ("c", {"locale": "zh"}))
        self.assertEqual(K.search_selected(state, "opening hours", today=TODAY, locale="ta")["availableLocales"], ["en", "zh"])


class BoundaryTests(unittest.TestCase):
    """The sidecar returns the guidance and notes the miss with its reason."""
    setUp = paused.BoundaryTests.setUp
    store = unanswered_tests.BoundaryTests.store

    def test_a_search_in_an_uncovered_language_is_guided_and_noted_with_the_reason(self):
        desired = self.runtime.desired_state
        for item in desired["knowledge"]:
            item["spec"]["locale"] = "en"
        result = self.runtime.kb_search(query="opening hours", locale="ms")["result"]
        self.assertEqual((result["documents"], result["availableLocales"]), ([], ["en"]))
        groups = self.store().list()
        self.assertEqual((len(groups), groups[0]["reason"]), (1, "source-other-locale"))


class LookAlikeCommandTests(unittest.TestCase):
    setUp, command, respond, say = (paused.ChatTests.setUp, paused.ChatTests.command, paused.ChatTests.respond, paused.ChatTests.say)

    def test_look_alike_spellings_from_a_stranger_never_pause_or_resume(self):
        for words in ("pause　coworker", "PAUSE COWORKER now", "/pause coworker", "pause coworker please"):
            self.assertEqual(self.say(words, user="USTRANGER"), NEUTRAL, words)
        for words in ("ｐａｕｓｅ ｃｏｗｏｒｋｅｒ", "暂停 coworker", "pause co​worker", "pausa coworker"):
            self.assertEqual(self.say(words, user="USTRANGER"), "the coworker answered", words)  # not a command: ordinary words for the coworker
        self.assertFalse(pause.PauseStore(self.root).is_paused())

    def test_an_administrator_is_not_paused_by_confusable_text_either(self):
        for words in ("ｐａｕｓｅ ｃｏｗｏｒｋｅｒ", "pause co​worker"):
            self.say(words)
        self.assertFalse(pause.PauseStore(self.root).is_paused())


if __name__ == "__main__":
    unittest.main()
