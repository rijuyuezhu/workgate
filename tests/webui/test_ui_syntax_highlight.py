import json
import shutil
import subprocess
from pathlib import Path

import pytest

STATIC_ROOT = Path(__file__).parents[2] / "src" / "workgate" / "ui" / "static"


def test_syntax_highlighter_is_loaded_before_browser_app_and_uses_safe_dom() -> (
    None
):
    index = (STATIC_ROOT / "index.html").read_text(encoding="utf-8")
    script = (STATIC_ROOT / "syntax_highlight.js").read_text(encoding="utf-8")
    css = (STATIC_ROOT / "web.css").read_text(encoding="utf-8")

    assert index.index("assets/syntax_highlight.js") < index.index(
        "assets/web.js"
    )
    assert "textContent" in script
    assert "createTextNode" in script
    assert "innerHTML" not in script
    assert ".syntax-keyword" in css
    assert ".syntax-diff-add" in css


@pytest.mark.skipif(
    shutil.which("node") is None, reason="Node.js is unavailable"
)
def test_syntax_tokenizer_preserves_text_and_detects_languages() -> None:
    module = STATIC_ROOT / "syntax_highlight.js"
    source = 'const value = {"answer": 42}; // note\n'
    code = r"""
const syntax = require(process.argv[1]);
const source = JSON.parse(process.argv[2]);
const tokens = syntax.tokenize(source, "javascript");
const result = {
  joined: tokens.map((token) => token.text).join(""),
  types: tokens.map((token) => token.type),
  js: syntax.languageForPath("src/app.ts"),
  py: syntax.languageForPath("tool.py"),
  diff: syntax.languageForPath("change.patch"),
  json: syntax.languageForPath("unknown", "application/json"),
};
process.stdout.write(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", code, str(module), json.dumps(source)],
        check=True,
        capture_output=True,
        text=True,
    )
    result = json.loads(completed.stdout)

    assert result["joined"] == source
    assert "keyword" in result["types"]
    assert "string" in result["types"]
    assert "number" in result["types"]
    assert "comment" in result["types"]
    assert result["js"] == "javascript"
    assert result["py"] == "python"
    assert result["diff"] == "diff"
    assert result["json"] == "json"
