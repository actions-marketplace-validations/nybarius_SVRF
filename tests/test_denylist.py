"""The tree and the commit messages carry none of a list of terms that do not belong in
this repository (names and vocabulary of the private project it was extracted from).

The terms are stored rot13-encoded so this file does not itself contain them.
Run `python3 tests/test_denylist.py --show` to print the decoded list.
"""

from __future__ import annotations

import codecs
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ENCODED = [
    "vafgvghgvbany", "fpujrvmrezrgubq", "uvonev", "fpuhjnyq", "tevzzre", "yhatr", "fgrvare",
    "nhgubevgl_rssrpg", "svyvat_rssrpg", "c_if_ac", "zrgbalz", "/ubzr/fgrcu", "vapvqrapr",
    "frcnengbe", "pbafhzrq ernq", "pneevre", "betnavfz", "fjnez-anzr", "arkhfcyyp", "dhvpxfgebyy",
    "fgntrunaq", "oebjfrebf",
]
TERMS = [codecs.decode(t, "rot13") for t in ENCODED]
SKIP_DIRS = {".git", ".lake", "__pycache__", ".mypy_cache", ".pytest_cache", "build", "dist", ".venv"}


def files() -> list[Path]:
    out = []
    for path in ROOT.rglob("*"):
        if any(part in SKIP_DIRS or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts):
            continue
        if path.is_file():
            out.append(path)
    return out


PATTERNS = [(t, re.compile(r"(?<![a-z])" + re.escape(t))) for t in TERMS]


def hits(text: str) -> list[str]:
    """Terms found at the start of a word (so a term also matches its longer forms)."""
    lowered = text.lower()
    return [t for t, pattern in PATTERNS if pattern.search(lowered)]


class Denylist(unittest.TestCase):
    def test_the_scanner_finds_every_term_in_any_case_and_skips_word_interiors(self):
        for term in TERMS:
            self.assertEqual(hits(f"see {term.upper()}s here"), [term])
        self.assertEqual(hits("take the p" + TERMS[5] + " into multiple" + TERMS[13] + "s"), [])

    def test_stdlib_json_options_are_code_and_private_prose_still_refuses(self):
        term = TERMS[13]
        option = term + "s"
        expressions = [
            "import json\njson.dumps({}, " + option + "=(\",\", \":\"))",
            "import json as wire\nwire.JSONEncoder(" + option + "=(\",\", \":\"))",
            "from json import dumps as encode\nencode({}, " + option + "=(\",\", \":\"))",
        ]
        for source in expressions:
            with self.subTest(source=source):
                self.assertEqual(hits(source), [])
                self.assertEqual(hits(source + "\n# private " + term), [term])
                self.assertEqual(hits(source + "\nvalue = " + repr(term)), [term])
        for source in [
            "custom({}, " + option + "=(\",\", \":\"))",
            "import fake as json\njson.dumps({}, " + option + "=(\",\", \":\"))",
            "import json\njson = custom\njson.dumps({}, " + option + "=(\",\", \":\"))",
            "import json\ndef run(json):\n    json.dumps({}, " + option + "=(\",\", \":\"))",
            "import json\njson.dumps(" + repr(term) + ")",
        ]:
            with self.subTest(source=source):
                self.assertEqual(hits(source), [term])

    def test_no_file_carries_a_denied_term(self):
        found = {}
        for path in files():
            try:
                text = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                continue
            terms = hits(text) + hits(str(path.relative_to(ROOT)))
            if terms:
                found[str(path.relative_to(ROOT))] = terms
        self.assertEqual(found, {})

    def test_no_commit_message_carries_a_denied_term(self):
        done = subprocess.run(["git", "-C", str(ROOT), "log", "--format=%H%n%an%n%ae%n%B%n--"],
                              capture_output=True, text=True)
        if done.returncode != 0:
            self.skipTest("not a git checkout")
        self.assertEqual(hits(done.stdout), [])


if __name__ == "__main__":
    if "--show" in sys.argv:
        print("\n".join(TERMS))
    else:
        unittest.main()
