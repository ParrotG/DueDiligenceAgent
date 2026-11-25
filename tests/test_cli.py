"""Test CLI validation and the browser representation of search results."""

import os
import subprocess
import sys
from unittest import TestCase

from google.genai import types

from due_diligence.preview import _render
from due_diligence.runtime import SearchResult


class CliTests(TestCase):
    def test_help_works_without_api_key(self):
        completed = self.run_cli("--help")
        self.assertEqual(completed.returncode, 0)
        self.assertIn("--trace", completed.stdout)

    def test_missing_key_is_reported_before_running_a_case(self):
        completed = self.run_cli("--prompt", "Investigate Example Holdings.")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("Set GEMINI_API_KEY or GOOGLE_API_KEY", completed.stderr)

    def test_search_preview_escapes_model_text_and_preserves_suggestions(self):
        search = SearchResult(
            text="Grounded <script>text</script>.",
            metadata=types.GroundingMetadata(
                search_entry_point=types.SearchEntryPoint(
                    rendered_content='<div class="suggestions">Try a search</div>'
                ),
                grounding_chunks=[types.GroundingChunk(
                    web=types.GroundingChunkWeb(uri="https://example.com/", title="Example")
                )],
            ),
        )
        html = _render([search]).decode()
        self.assertIn("Grounded &lt;script&gt;text&lt;/script&gt;.", html)
        self.assertIn('href="https://example.com/"', html)
        self.assertIn('<div class="suggestions">Try a search</div>', html)

    def run_cli(self, *args):
        env = os.environ.copy()
        for key in ("GEMINI_API_KEY", "GOOGLE_API_KEY"):
            env.pop(key, None)
        return subprocess.run(
            [sys.executable, "-m", "due_diligence.cli", *args],
            capture_output=True,
            text=True,
            env=env,
            timeout=15,
        )
