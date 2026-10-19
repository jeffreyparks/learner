#!/usr/bin/env python3
"""Check a Learner collection JSON file before importing it. Standard library only.

    python validate.py cards.json        # or: cat cards.json | python validate.py -

Prints OK and a summary, or one line per problem (exit status 1). Errors are things Learner's
importer would reject; warnings are things it accepts but that make weaker cards.
"""

from __future__ import annotations

import ast
import base64
import hashlib
import json
import re
import sys

ANSWER_FORMATS = ("text", "python", "sql")
SQL_DIALECTS = ("mysql", "postgres", "sqlite", "snowflake", "bigquery", "tsql", "duckdb")
COLLECTION_FIELDS = {"collection", "group", "sql_dialect", "tier_labels", "cards", "media"}
CARD_FIELDS = {"id", "tier", "answer_format", "prompt", "reference_answer", "rubric", "references", "tags", "archived"}
MEDIA_FIELDS = {"mime", "data", "filename", "alt"}
IMAGE_MAGIC = (b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a")
MEDIA_REF = re.compile(r"/media/([0-9a-f]{64})")


def nonempty_str(value) -> bool:
    return isinstance(value, str) and bool(value.strip())


def is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def validate(data) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ["The file must be one JSON object (a collection), not a list or value."], []

    for key in sorted(set(data) - COLLECTION_FIELDS):
        errors.append(f"{key}: unknown field (allowed: {', '.join(sorted(COLLECTION_FIELDS))})")
    if not nonempty_str(data.get("collection")):
        errors.append("collection: required, a non-empty string")
    if "group" in data and not isinstance(data["group"], str):
        errors.append("group: must be a string")
    if "sql_dialect" in data and data["sql_dialect"] not in SQL_DIALECTS:
        errors.append(f"sql_dialect: must be one of {', '.join(SQL_DIALECTS)}")

    labels = data.get("tier_labels", {})
    label_tiers: set[int] = set()
    if not isinstance(labels, dict):
        errors.append('tier_labels: must be an object like {"1": "Foundations"}')
    else:
        for key, value in labels.items():
            if not re.fullmatch(r"[1-9][0-9]*", str(key)):
                errors.append(f"tier_labels {key}: keys must be tier numbers like \"1\"")
            else:
                label_tiers.add(int(key))
            if not isinstance(value, str):
                errors.append(f"tier_labels {key}: label must be a string")

    cards = data.get("cards", [])
    if not isinstance(cards, list):
        errors.append("cards: must be a list")
        cards = []
    elif not cards:
        warnings.append("cards: the collection has no cards")

    prompts: dict[str, int] = {}
    tiers_used: set[int] = set()
    for n, card in enumerate(cards, start=1):
        where = f"Card {n}"
        if not isinstance(card, dict):
            errors.append(f"{where}: must be an object")
            continue
        for key in sorted(set(card) - CARD_FIELDS):
            errors.append(f"{where}, {key}: unknown field (allowed: {', '.join(sorted(CARD_FIELDS - {'id', 'archived'}))})")
        if "id" in card:
            warnings.append(f"{where}, id: only exported cards have ids; leave it out for new cards")
        tier = card.get("tier")
        if not is_int(tier) or tier < 1:
            errors.append(f"{where}, tier: required, a whole number of 1 or more")
        elif not card.get("archived"):
            tiers_used.add(tier)
        fmt = card.get("answer_format", "text")
        if fmt not in ANSWER_FORMATS:
            errors.append(f"{where}, answer_format: must be one of {', '.join(ANSWER_FORMATS)}")
        for key in ("prompt", "reference_answer"):
            if not nonempty_str(card.get(key)):
                errors.append(f"{where}, {key}: required, a non-empty string")
        for key in ("rubric", "tags"):
            if key in card and not (isinstance(card[key], list) and all(isinstance(x, str) for x in card[key])):
                errors.append(f"{where}, {key}: must be a list of strings")
        if "archived" in card and not isinstance(card["archived"], bool):
            errors.append(f"{where}, archived: must be true or false")
        refs = card.get("references", [])
        if not isinstance(refs, list):
            errors.append(f"{where}, references: must be a list")
            refs = []
        for r, ref in enumerate(refs, start=1):
            if not isinstance(ref, dict):
                errors.append(f"{where}, references {r}: must be an object with title and url")
                continue
            for key in sorted(set(ref) - {"title", "url"}):
                errors.append(f"{where}, references {r}, {key}: unknown field (allowed: title, url)")
            if not nonempty_str(ref.get("title")):
                errors.append(f"{where}, references {r}, title: required, a non-empty string")
            url = ref.get("url")
            if not isinstance(url, str) or not url.strip().startswith(("http://", "https://")) or " " in url.strip():
                errors.append(f"{where}, references {r}, url: must be a web address starting with http:// or https://")

        # Quality checks: the importer accepts these, but the cards grade worse.
        rubric = card.get("rubric", [])
        if isinstance(rubric, list) and not 2 <= len(rubric) <= 4:
            warnings.append(f"{where}, rubric: aim for 2-4 key points (has {len(rubric)})")
        answer = card.get("reference_answer")
        if isinstance(answer, str) and fmt in ("python", "sql"):
            if answer.lstrip().startswith("```"):
                warnings.append(f"{where}, reference_answer: {fmt} answers should be bare code, without ``` fences")
            elif fmt == "python":
                try:
                    ast.parse(answer)
                except SyntaxError as exc:
                    warnings.append(f"{where}, reference_answer: Python syntax error on line {exc.lineno}: {exc.msg}")
        prompt = card.get("prompt")
        if nonempty_str(prompt):
            key = " ".join(prompt.lower().split())
            if key in prompts:
                warnings.append(f"{where}, prompt: same as card {prompts[key]}")
            prompts.setdefault(key, n)

    if tiers_used:
        expected = set(range(1, max(tiers_used) + 1))
        if tiers_used != expected:
            gaps = ", ".join(str(t) for t in sorted(expected - tiers_used))
            warnings.append(f"tiers: no cards in tier {gaps}; Learner renumbers tiers to close gaps")
        for tier in sorted(tiers_used - label_tiers):
            warnings.append(f"tier_labels: tier {tier} has no label")

    media = data.get("media", {})
    if not isinstance(media, dict):
        errors.append("media: must be an object keyed by sha256")
        media = {}
    for sha, item in media.items():
        where = f"media {sha[:12]}"
        if not re.fullmatch(r"[0-9a-f]{64}", sha):
            errors.append(f"{where}: key must be the image's hex SHA-256")
        if not isinstance(item, dict):
            errors.append(f"{where}: must be an object with mime and data")
            continue
        for key in sorted(set(item) - MEDIA_FIELDS):
            errors.append(f"{where}, {key}: unknown field (allowed: {', '.join(sorted(MEDIA_FIELDS))})")
        for key in ("mime", "data"):
            if not isinstance(item.get(key), str):
                errors.append(f"{where}, {key}: required, a string")
        try:
            raw = base64.b64decode(item.get("data", ""), validate=True)
        except (ValueError, TypeError):
            errors.append(f"{where}: the image data is not valid base64")
            continue
        if hashlib.sha256(raw).hexdigest() != sha:
            errors.append(f"{where}: the data doesn't match its fingerprint")
        if not (raw.startswith(IMAGE_MAGIC) or (raw[:4] == b"RIFF" and raw[8:12] == b"WEBP")):
            errors.append(f"{where}: not a JPEG, PNG, GIF or WebP image")
    texts = [c.get(k, "") for c in cards if isinstance(c, dict) for k in ("prompt", "reference_answer")]
    for sha in sorted({m for t in texts if isinstance(t, str) for m in MEDIA_REF.findall(t)} - set(media)):
        warnings.append(f"media: cards reference /media/{sha[:12]}… but the file doesn't include it")

    return errors, warnings


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__.strip(), file=sys.stderr)
        return 2
    try:
        text = sys.stdin.read() if argv[1] == "-" else open(argv[1], encoding="utf-8").read()
        data = json.loads(text)
    except OSError as exc:
        print(f"ERROR: can't read {argv[1]}: {exc.strerror}")
        return 1
    except json.JSONDecodeError as exc:
        print(f"ERROR: not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})")
        return 1
    errors, warnings = validate(data)
    for line in errors:
        print(f"ERROR: {line}")
    for line in warnings:
        print(f"WARNING: {line}")
    if errors:
        return 1
    cards = data.get("cards", [])
    tiers = sorted({c["tier"] for c in cards})
    print(f"OK: {data['collection']!r}, {len(cards)} cards across {len(tiers)} tier(s)")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
