"""Answer-format registry: each format names its editor language and (optionally) a syntax checker.

Adding a language means adding one entry to FORMATS (and, if it has an editor mode, one line in
static/editor.js). Checkers report syntax problems only; they never decide whether an answer is right.
"""

from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Callable
from dataclasses import asdict, dataclass

import sqlglot
from sqlglot.errors import ParseError, TokenError


@dataclass(frozen=True)
class Diagnostic:
    """A problem in the answer. Lines and columns are 1-based; the end column is exclusive."""

    line: int
    col: int
    end_line: int
    end_col: int
    message: str
    severity: str = "error"  # "error" | "warning"

    def to_dict(self) -> dict:
        return asdict(self)


# --- Python: ruff ---

# Syntax errors are always reported. On top of those, a few pyflakes rules, minus the ones that are
# noise for snippets (which routinely use names like `df` defined elsewhere, or assign without using).
RUFF_SELECT = "F"
RUFF_IGNORE = "F401,F811,F821,F841"


def check_python(code: str, dialect: str | None = None) -> list[Diagnostic]:
    from ruff.__main__ import find_ruff_bin

    result = subprocess.run(
        [
            find_ruff_bin(), "check", "-", "--isolated", "--no-cache", "--output-format=json",
            f"--select={RUFF_SELECT}", f"--ignore={RUFF_IGNORE}", "--stdin-filename=answer.py",
        ],
        input=code, capture_output=True, text=True, timeout=10,
    )
    diagnostics = []
    for item in json.loads(result.stdout or "[]"):
        start, end = item["location"], item["end_location"]
        syntax = item["code"] in (None, "invalid-syntax", "E999")
        diagnostics.append(
            Diagnostic(
                line=start["row"], col=start["column"], end_line=end["row"], end_col=end["column"],
                message=item["message"] if syntax else f"{item['message']} ({item['code']})",
                severity="error" if syntax else "warning",
            )
        )
    return diagnostics


# --- SQL: sqlglot ---

_TOKEN_REPR = re.compile(r"<Token token_type: [^,]+, text: (.*?), line: \d+.*?>")


def check_sql(code: str, dialect: str | None = None) -> list[Diagnostic]:
    """sqlglot is lenient: it catches most syntax errors, not all. The grader reads the code too."""
    try:
        sqlglot.parse(code, read=dialect or "mysql")
    except ParseError as exc:
        diagnostics = []
        for err in exc.errors:
            line, col = err.get("line") or 1, err.get("col") or 1
            width = max(1, len(err.get("highlight") or ""))
            message = _TOKEN_REPR.sub(lambda m: f"“{m[1]}”", err.get("description") or str(exc))
            diagnostics.append(Diagnostic(line, max(1, col - width + 1), line, col + 1, message))
        return diagnostics
    except TokenError as exc:
        return [Diagnostic(1, 1, 1, 2, f"Couldn't read the SQL (check quotes and brackets): {exc}")]
    return []


# --- registry ---


@dataclass(frozen=True)
class AnswerFormat:
    name: str
    label: str
    editor: str | None  # CodeMirror language in static/editor.js; None = plain textarea
    check: Callable[[str, str | None], list[Diagnostic]] | None


FORMATS: dict[str, AnswerFormat] = {
    f.name: f
    for f in (
        AnswerFormat("text", "Text", None, None),
        AnswerFormat("python", "Python", "python", check_python),
        AnswerFormat("sql", "SQL", "sql", check_sql),
    )
}


def check(answer_format: str, code: str, dialect: str | None = None) -> list[Diagnostic]:
    fmt = FORMATS.get(answer_format)
    if fmt is None or fmt.check is None or not code.strip():
        return []
    return fmt.check(code, dialect)
