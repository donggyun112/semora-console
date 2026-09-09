"""Every Korean string the page can show has an English one.

The catalog lives in the browser and the strings live in Python, so nothing in the
server would notice a message added without a translation: the page would quietly draw
Korean inside an English console. This walks the modules that produce operator-facing
text, hands each string to the real catalog through node, and fails on the ones that
came back unchanged.
"""

from __future__ import annotations

import ast
import json
import shutil
import subprocess
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[1] / "src" / "console"
I18N = SRC / "static" / "i18n.mjs"

# Text the model reads, not text the operator reads. Translating it would change what
# the agent was told, which is a different decision from translating the console.
MODEL_FACING = {
    "SYSTEM_PROMPT_KO",
    "SYSTEM_PROMPT_EN",
    "POLICY_NOTICE",
    "DROP_NOTICE",
    "LOG_HINT",
    "UNTRUSTED_MARK",
}

# The two shapes a runtime message interpolates: a tool name, or a count.
SLOT_SAMPLES = ("charge_card", "2")


def _has_hangul(text: str) -> bool:
    return any("가" <= ch <= "힣" for ch in text)


def _docstrings(tree: ast.AST) -> set[int]:
    """ids() of the string constants that are docstrings, which nobody displays."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Module | ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef):
            continue
        first = node.body[0] if node.body else None
        if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
            found.add(id(first.value))
    return found


def _model_facing(tree: ast.AST) -> set[int]:
    """ids() of the constants assigned to a name the model reads rather than the page."""
    found = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Assign):
            continue
        names = {t.id for t in node.targets if isinstance(t, ast.Name)}
        if names & MODEL_FACING:
            for child in ast.walk(node.value):
                if isinstance(child, ast.Constant):
                    found.add(id(child))
    return found


def _korean_strings(path: Path) -> list[tuple[str, ...]]:
    """Operator-facing Korean in one module, as groups of acceptable renderings.

    A plain literal is a group of one. An f-string is a group of its samples: the slot
    is a tool name in one message and a count in another, and only the catalog knows
    which, so translating either rendering settles it.
    """
    tree = ast.parse(path.read_text())
    skip = _docstrings(tree) | _model_facing(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.JoinedStr):
            # Its literal halves are not strings anyone displays on their own.
            skip |= {id(part) for part in node.values}
    groups: set[tuple[str, ...]] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in skip and _has_hangul(node.value):
                groups.add((node.value,))
        elif isinstance(node, ast.JoinedStr) and id(node) not in skip:
            rendered = tuple(
                "".join(
                    part.value if isinstance(part, ast.Constant) else sample
                    for part in node.values
                )
                for sample in SLOT_SAMPLES
            )
            if _has_hangul(rendered[0]):
                groups.add(rendered)
    return sorted(groups)


def _catalog_misses(groups: list[tuple[str, ...]], tmp_path: Path) -> list[str]:
    """The groups the English catalog leaves untranslated in every rendering."""
    payload = tmp_path / "strings.json"
    payload.write_text(json.dumps([list(g) for g in groups], ensure_ascii=False))
    script = tmp_path / "check.mjs"
    script.write_text(
        f'import {{ readFileSync }} from "node:fs";\n'
        f'import {{ setLang, t }} from "{I18N.as_posix()}";\n'
        'setLang("en");\n'
        f'const wanted = JSON.parse(readFileSync("{payload.as_posix()}", "utf8"));\n'
        "const missed = wanted.filter((g) => g.every((s) => t(s) === s));\n"
        "console.log(JSON.stringify(missed.map((g) => g[0])));\n"
    )
    out = subprocess.run(
        ["node", str(script)], capture_output=True, text=True, check=True
    )
    return json.loads(out.stdout)


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the catalog")
@pytest.mark.parametrize("module", ["units.py", "dormancy.py", "server.py"])
def test_server_korean_is_translated(module: str, tmp_path: Path) -> None:
    strings = _korean_strings(SRC / module)
    assert strings, f"{module} produces no operator-facing Korean; check the walker"
    missing = _catalog_misses(strings, tmp_path)
    assert not missing, f"no English for {module}: {missing}"


def test_every_scenario_has_an_english_half() -> None:
    """Scenario copy is the server's, not the catalog's, because the prompt is sent.

    The page shows the prompt as the user message in the thread, so English on screen
    has to be the English the model was given.
    """
    from console.scenarios import SCENARIOS

    for scenario in SCENARIOS:
        english = scenario.get("en")
        assert english, f"{scenario['id']} has no English half"
        for field in ("title", "risk", "prompt"):
            value = english.get(field)
            assert value, f"{scenario['id']} has no English {field}"
            assert not _has_hangul(value), f"{scenario['id']} English {field} is Korean"


@pytest.mark.skipif(shutil.which("node") is None, reason="node runs the catalog")
def test_page_korean_is_translated(tmp_path: Path) -> None:
    """The Korean the page passes to `t` and `tf` itself, and the markup it ships with."""
    import re

    strings: set[str] = set()
    for name in ("app.js", "reducer.mjs"):
        source = (SRC / "static" / name).read_text()
        for call in re.findall(r'\bt[f]?\(\s*"((?:[^"\\]|\\.)*)"', source):
            if _has_hangul(call):
                strings.add(call)
    markup = (SRC / "static" / "index.html").read_text()
    for tag in re.findall(r"data-i18n>\s*([^<]+?)\s*<", markup):
        if _has_hangul(tag):
            strings.add(tag)
    for label in re.findall(r'data-i18n-label aria-label="([^"]+)"', markup):
        if _has_hangul(label):
            strings.add(label)

    missing = _catalog_misses([(s,) for s in sorted(strings)], tmp_path)
    assert not missing, f"no English for the page's own strings: {missing}"
