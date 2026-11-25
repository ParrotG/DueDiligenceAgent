"""Test local secret loading without network calls."""

import os
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

from due_diligence.config import load_settings, make_client


class ConfigTests(TestCase):
    def test_local_key_is_used_without_exporting_it(self):
        with TemporaryDirectory() as directory:
            dotfile = Path(directory) / ".env.local"
            dotfile.write_text("GEMINI_API_KEY=local-key\n", encoding="utf-8")
            with patch.dict(os.environ, {"GEMINI_API_KEY": "inherited-key"}):
                with patch("due_diligence.config.genai.Client") as client:
                    make_client(load_settings(), dotfile)
                    self.assertEqual(client.call_args.kwargs["api_key"], "local-key")
                    self.assertEqual(os.environ["GEMINI_API_KEY"], "inherited-key")

    def test_process_key_is_ignored_if_local_file_is_missing(self):
        with TemporaryDirectory() as directory:
            with patch.dict(os.environ, {"GEMINI_API_KEY": "inherited-key"}):
                with self.assertRaisesRegex(ValueError, r"\.env\.local"):
                    make_client(load_settings(), Path(directory) / ".env.local")
