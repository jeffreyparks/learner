"""Grader test set (DESIGN.md §7.4): check that a change to the grading prompt or model actually helps.

    uv run learner-eval export          # add your graded answers to evals/grader_cases.jsonl
    uv run learner-eval run             # re-grade every case with the current prompt and model
    uv run learner-eval run --model claude-sonnet-5-5   # ...or try another grading model

A case is an answer the AI graded, plus your final call on it. Every answer where you overrode the
grader becomes a case (those are its known mistakes), and so does an equal number of recent answers
where you agreed (so a change that fixes mistakes but breaks good verdicts shows up too).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from contextlib import closing
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

from . import config as config_module
from . import store, tutor
from .engine import INCORRECT
from .llm import LLMError, LLMProvider, make_provider
from .models import Card

DEFAULT_CASES = Path("evals/grader_cases.jsonl")


def load_cases(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def export_cases(conn, path: Path) -> tuple[int, int]:
    """Append new cases to `path`; cases already there are kept as they are. Returns (overrides, agreed) added."""
    existing = {c["attempt_id"] for c in load_cases(path)}
    collections = {c.id: c for c in store.list_collections(conn, include_archived=True)}
    cards = {c.id: c for col in collections for c in store.list_cards(conn, col, include_archived=True)}
    graded = [r for r in store.list_attempt_records(conn) if r.llm_verdict and r.answer and r.card_id in cards]
    overrides = [r for r in graded if r.overridden]
    agreed = [r for r in graded if not r.overridden][-max(len(overrides), 1):] if overrides else []

    new_cases = []
    for kind, records in (("override", overrides), ("agreed", agreed)):
        for r in records:
            if r.id in existing:
                continue
            card = cards[r.card_id]
            new_cases.append({
                "attempt_id": r.id,
                "kind": kind,
                "expected": "wrong" if r.outcome == INCORRECT else "right",
                "original_verdict": r.llm_verdict,
                "answer": r.answer,
                "dialect": collections[card.collection_id].sql_dialect,
                "card": {
                    "id": card.id, "answer_format": card.answer_format, "prompt": card.prompt,
                    "reference_answer": card.reference_answer, "rubric": card.rubric,
                },
                "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            })
    if new_cases:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            for case in new_cases:
                f.write(json.dumps(case) + "\n")
    added = Counter(c["kind"] for c in new_cases)
    return added["override"], added["agreed"]


def case_card(case: dict) -> Card:
    c = case["card"]
    return Card(
        id=c["id"], collection_id=0, tier=1, answer_format=c["answer_format"], prompt=c["prompt"],
        reference_answer=c["reference_answer"], rubric=c.get("rubric", []), references=[], tags=[],
        archived=False, progress_reset_at=None, created_at="", updated_at="",
    )


def run_cases(provider: LLMProvider, cases: list[dict], out=sys.stdout) -> dict:
    """Re-grade every case. Returns the tallies; prints a readable report to `out`."""
    tally: Counter = Counter()
    misses = []
    for case in cases:
        key = (case["kind"], case["card"]["answer_format"])
        try:
            grade = tutor.grade(provider, case_card(case), case["answer"], case.get("dialect", "mysql"))
        except LLMError as exc:
            tally["errors"] += 1
            misses.append((case, f"error: {exc}"))
            continue
        agrees = grade.right == (case["expected"] == "right")
        tally[(*key, "agree" if agrees else "disagree")] += 1
        if not agrees:
            misses.append((case, f"{grade.verdict}: {grade.feedback}"))

    def line(label: str, kinds=("override", "agreed"), formats=None) -> str:
        agree = sum(n for k, n in tally.items() if isinstance(k, tuple) and k[0] in kinds
                    and (formats is None or k[1] in formats) and k[2] == "agree")
        total = sum(n for k, n in tally.items() if isinstance(k, tuple) and k[0] in kinds
                    and (formats is None or k[1] in formats))
        return f"  {label:<34} {agree}/{total}" + (f"  ({agree / total:.0%})" if total else "")

    formats = sorted({k[1] for k in tally if isinstance(k, tuple)})
    print("Grader agreement with your calls", file=out)
    print(line("All cases"), file=out)
    print(line("Your past overrides (were wrong)", ("override",)), file=out)
    print(line("Answers you agreed with", ("agreed",)), file=out)
    for fmt in formats:
        print(line(f"Format: {fmt}", formats=(fmt,)), file=out)
    if tally["errors"]:
        print(f"  Errors: {tally['errors']}", file=out)
    if misses:
        print("\nStill disagreeing:", file=out)
        for case, got in misses:
            print(f"  attempt {case['attempt_id']} [{case['card']['answer_format']}] you said {case['expected']}; "
                  f"grader said {got[:120]}", file=out)
    return dict(tally)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="learner-eval", description="Build and run the AI grader test set.")
    parser.add_argument("--config", help="path to learner.toml")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES, help=f"test set file (default {DEFAULT_CASES})")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("export", help="add your graded answers to the test set")
    run = sub.add_parser("run", help="re-grade every case and report agreement")
    run.add_argument("--model", help="grade with this model instead of the configured one")
    run.add_argument("--yes", action="store_true", help="don't ask before making API calls")
    args = parser.parse_args(argv)

    cfg = config_module.load(args.config)
    if args.command == "export":
        with closing(store.connect(Path(cfg.db_path).expanduser())) as conn:
            store.migrate(conn)
            overrides, agreed = export_cases(conn, args.cases)
        total = len(load_cases(args.cases))
        print(f"Added {overrides} override case(s) and {agreed} agreed case(s). {args.cases} now has {total} case(s).")
        return

    cases = load_cases(args.cases)
    if not cases:
        sys.exit(f"No cases in {args.cases}. Run `learner-eval export` first.")
    if args.model:
        cfg = replace(cfg, grade_model=args.model)
    provider = make_provider(cfg)
    if provider is None:
        sys.exit("No AI provider is configured (set ANTHROPIC_API_KEY, or provider in learner.toml).")
    if not args.yes:
        answer = input(f"Re-grade {len(cases)} case(s) with {cfg.grade_model}? This makes {len(cases)} API call(s). [y/N] ")
        if answer.strip().lower() not in ("y", "yes"):
            sys.exit("Cancelled.")
    run_cases(provider, cases)
