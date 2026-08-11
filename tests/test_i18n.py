from __future__ import annotations

import unittest

from arss.i18n import MESSAGES, Translator, assert_catalogue_parity


class TranslationTest(unittest.TestCase):
    def test_catalogues_have_exact_parity(self) -> None:
        assert_catalogue_parity()

    def test_every_message_formats_without_arguments_when_it_has_no_fields(self) -> None:
        for messages in MESSAGES.values():
            for message in messages.values():
                if "{" not in message:
                    self.assertIsInstance(message.format(), str)

    def test_explicit_language_is_used(self) -> None:
        self.assertEqual("Nastavení", Translator("cs")("settings"))
        self.assertEqual("Settings", Translator("en")("settings"))

    def test_settings_mnemonics_are_present_and_unique(self) -> None:
        keys = (
            "language_mnemonic",
            "rss_interval_mnemonic",
            "podcast_interval_mnemonic",
        )
        for language, messages in MESSAGES.items():
            mnemonics: list[str] = []
            for key in keys:
                label = messages[key]
                marker = label.find("_")
                self.assertGreaterEqual(marker, 0, f"{language}:{key}")
                self.assertLess(marker + 1, len(label), f"{language}:{key}")
                mnemonics.append(label[marker + 1].casefold())
            self.assertEqual(len(keys), len(set(mnemonics)), language)


if __name__ == "__main__":
    unittest.main()
