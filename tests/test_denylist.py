"""The current tree carries none of a list of terms that do not belong in
this repository (names and vocabulary of the private project it was extracted from).

The terms are stored rot13-encoded so this file does not itself contain them.
Run `python3 tests/test_denylist.py --show` to print the decoded list.
"""

from __future__ import annotations

import ast
import codecs
import inspect
import json
import re
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


def _without_stdlib_options(text: str) -> str:
    """Exclude only verified standard JSON option identifiers, never their values."""
    try:
        tree = ast.parse(text)
    except (SyntaxError, ValueError):
        return text
    bindings, invalid = {}, set()
    for node in tree.body:
        if isinstance(node, ast.Import):
            for alias in node.names:
                name = alias.asname or alias.name.split(".")[0]
                if name in bindings or alias.name != "json":
                    invalid.add(name)
                else:
                    bindings[name] = "module"
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                name = alias.asname or alias.name
                if (name in bindings or node.module != "json" or node.level
                        or alias.name not in ("dumps", "JSONEncoder")):
                    invalid.add(name)
                else:
                    bindings[name] = alias.name
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)) and node not in tree.body:
            invalid.update(alias.asname or alias.name.split(".")[0] for alias in node.names)
        if isinstance(node, ast.ImportFrom) and any(alias.name == "*" for alias in node.names):
            invalid.update(bindings)
        if isinstance(node, ast.ExceptHandler) and node.name:
            invalid.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            invalid.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            invalid.add(node.rest)
        if isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            invalid.add(node.id)
        elif isinstance(node, ast.arg):
            invalid.add(node.arg)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            invalid.add(node.name)
        elif isinstance(node, ast.Attribute) and isinstance(node.ctx, (ast.Store, ast.Del)):
            root = node.value
            while isinstance(root, ast.Attribute):
                root = root.value
            if isinstance(root, ast.Name):
                invalid.add(root.id)
    bindings = {name: target for name, target in bindings.items() if name not in invalid}
    signatures = {name: inspect.signature(getattr(json, name)).parameters
                  for name in ("dumps", "JSONEncoder")}
    lines = [bytearray(line.encode()) for line in text.splitlines(keepends=True)]
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = None
        if isinstance(node.func, ast.Name):
            target = bindings.get(node.func.id)
        elif (isinstance(node.func, ast.Attribute) and isinstance(node.func.value, ast.Name)
              and bindings.get(node.func.value.id) == "module"):
            target = node.func.attr
        if target not in signatures:
            continue
        for option in node.keywords:
            if option.arg in signatures[target]:
                begin, size = option.col_offset, len(option.arg.encode())
                lines[option.lineno - 1][begin:begin + size] = b" " * size
    return b"".join(lines).decode()


def hits(text: str) -> list[str]:
    """Terms found at the start of a word (so a term also matches its longer forms)."""
    lowered = _without_stdlib_options(text).lower()
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

    def test_nested_import_exception_and_pattern_bindings_are_not_verified_json(self):
        term = TERMS[13]
        option = term + "s"
        for source in [
            "import json\ndef run():\n    import fake as json\n    json.dumps({}, " + option + "=(1, 2))",
            "from json import dumps as encode\ndef run():\n    from fake import encode\n    encode({}, " + option + "=(1, 2))",
            "import json\ntry: pass\nexcept Exception as json:\n    json.dumps({}, " + option + "=(1, 2))",
            "import json\nmatch value:\n    case json:\n        json.dumps({}, " + option + "=(1, 2))",
            "import json\nfrom fake import *\njson.dumps({}, " + option + "=(1, 2))",
            "import json as foreign\nimport foreign.submodule\nforeign.dumps({}, " + option + "=(1, 2))",
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


if __name__ == "__main__":
    if "--show" in sys.argv:
        print("\n".join(TERMS))
    else:
        unittest.main()
