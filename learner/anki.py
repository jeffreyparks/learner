"""Read Anki deck exports (.apkg) and turn them into Learner cards (DESIGN.md §12.3).

A deck file may come from anywhere, so reading is defensive: the archive is size-capped, only the
members Anki defines are read, nothing is extracted to disk or used as a file path, and Anki's
database is opened in memory, read-only, with untrusted-schema protections on.

Both export formats are supported:
- Anki 2.1.50+ (`collection.anki21b`): zstd-compressed database; note types, fields and templates
  live in their own tables, with settings encoded as protobuf (read by the tiny decoder below).
- Older Anki (`collection.anki21` / `collection.anki2`): note types and decks are JSON in `col`.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import zipfile
from collections import defaultdict
from collections.abc import Callable
from dataclasses import dataclass, field

import zstandard
from bs4 import BeautifulSoup
from markdownify import markdownify
from pydantic import ValidationError

from . import media, store, tiers
from .models import Card, CardIn

MAX_UNPACKED_BYTES = 500_000_000  # the whole deck, all members, once decompressed
PLACEHOLDER = "Please update to the latest Anki version"


class AnkiError(ValueError):
    """The file can't be imported; the message says why and what to do."""


@dataclass(frozen=True)
class AnkiCard:
    source_ref: str  # "anki:<note guid>:<template ord>", stable across exports
    deck: tuple[str, ...]  # ("SQL", "Joins")
    note_type: str
    prompt_html: str
    answer_html: str
    tags: tuple[str, ...]


@dataclass
class AnkiDeck:
    format: str
    cards: list[AnkiCard] = field(default_factory=list)
    media: dict[str, bytes] = field(default_factory=dict)  # file name as used in the cards' HTML → bytes
    warnings: list[str] = field(default_factory=list)


# --- protobuf: just enough to read Anki's settings blobs ---


def _varint(buf: bytes, i: int) -> tuple[int, int]:
    shift = result = 0
    while True:
        if i >= len(buf):
            raise AnkiError("The deck's settings are damaged (truncated number).")
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, i
        shift += 7


def proto_fields(buf: bytes) -> dict[int, list]:
    """Field number → values (ints for varints, bytes for everything else)."""
    out: dict[int, list] = defaultdict(list)
    i = 0
    while i < len(buf):
        key, i = _varint(buf, i)
        number, wire = key >> 3, key & 7
        if wire == 0:
            value, i = _varint(buf, i)
        elif wire == 1:
            value, i = buf[i:i + 8], i + 8
        elif wire == 2:
            length, i = _varint(buf, i)
            value, i = buf[i:i + length], i + length
        elif wire == 5:
            value, i = buf[i:i + 4], i + 4
        else:
            raise AnkiError("The deck's settings use an encoding this importer doesn't understand.")
        out[number].append(value)
    return out


def _text(values: list, default: str = "") -> str:
    return values[0].decode("utf-8", errors="replace") if values else default


# --- reading the archive ---


def _zstd(data: bytes, limit: int) -> bytes:
    reader = zstandard.ZstdDecompressor().stream_reader(io.BytesIO(data))
    chunks, total = [], 0
    while chunk := reader.read(1 << 20):
        total += len(chunk)
        if total > limit:
            raise AnkiError("The deck is too large to import (over 500 MB unpacked).")
        chunks.append(chunk)
    return b"".join(chunks)


@dataclass
class _NoteType:
    name: str
    fields: list[str]
    templates: dict[int, tuple[str, str]]  # ord → (question format, answer format)
    cloze: bool


def read_apkg(data: bytes) -> AnkiDeck:
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as exc:
        raise AnkiError("That isn't an Anki deck file (.apkg).") from exc
    if sum(i.file_size for i in archive.infolist()) > MAX_UNPACKED_BYTES:
        raise AnkiError("The deck is too large to import (over 500 MB unpacked).")
    names = set(archive.namelist())

    if "collection.anki21b" in names:
        deck = AnkiDeck("Anki 2.1.50 or later")
        db = _zstd(archive.read("collection.anki21b"), MAX_UNPACKED_BYTES)
        new_schema, compressed_media = True, True
    elif "collection.anki21" in names or "collection.anki2" in names:
        deck = AnkiDeck("older Anki")
        db = archive.read("collection.anki21" if "collection.anki21" in names else "collection.anki2")
        new_schema, compressed_media = False, False
    else:
        raise AnkiError("That file has no Anki collection inside. Export it from Anki as a deck package (.apkg).")

    db = bytearray(db)
    if db[:16] == b"SQLite format 3\x00" and db[18:20] == b"\x02\x02":
        db[18:20] = b"\x01\x01"  # saved in WAL mode, which an in-memory copy can't use: switch to rollback mode
    conn = sqlite3.connect(":memory:")
    try:
        conn.deserialize(bytes(db))
    except sqlite3.DatabaseError as exc:
        raise AnkiError("The deck's database couldn't be read.") from exc
    conn.execute("PRAGMA trusted_schema = OFF")  # don't run anything a crafted schema might define
    conn.execute("PRAGMA query_only = ON")
    conn.create_collation("unicase", lambda a, b: (a.casefold() > b.casefold()) - (a.casefold() < b.casefold()))
    try:
        note_types, deck_names = _load_new(conn) if new_schema else _load_legacy(conn)
        _load_cards(conn, deck, note_types, deck_names)
    except (sqlite3.DatabaseError, KeyError, ValueError) as exc:
        if isinstance(exc, AnkiError):
            raise
        raise AnkiError(f"The deck's contents couldn't be read ({exc}).") from exc
    finally:
        conn.close()

    if not new_schema and any(PLACEHOLDER in c.prompt_html for c in deck.cards):
        raise AnkiError("This export only contains Anki's “please update” placeholder. "
                        "Re-export it from Anki 2.1.50 or later, or tick “Support older Anki versions”.")
    deck.media = _load_media(archive, compressed_media, deck)
    return deck


def _load_new(conn: sqlite3.Connection) -> tuple[dict[int, _NoteType], dict[int, tuple[str, ...]]]:
    fields: dict[int, list[tuple[int, str]]] = defaultdict(list)
    for ntid, ord_, name in conn.execute("SELECT ntid, ord, name FROM fields"):
        fields[ntid].append((ord_, name))
    templates: dict[int, dict[int, tuple[str, str]]] = defaultdict(dict)
    for ntid, ord_, config in conn.execute("SELECT ntid, ord, config FROM templates"):
        cfg = proto_fields(config)
        templates[ntid][ord_] = (_text(cfg.get(1, [])), _text(cfg.get(2, [])))
    note_types = {}
    for ntid, name, config in conn.execute("SELECT id, name, config FROM notetypes"):
        kind = proto_fields(config).get(1, [0])[0]  # NotetypeConfig.kind: 0 normal, 1 cloze
        note_types[ntid] = _NoteType(name, [n for _, n in sorted(fields[ntid])], templates[ntid], kind == 1)
    decks = {did: tuple(name.split("\x1f")) for did, name in conn.execute("SELECT id, name FROM decks")}
    return note_types, decks


def _load_legacy(conn: sqlite3.Connection) -> tuple[dict[int, _NoteType], dict[int, tuple[str, ...]]]:
    models_json, decks_json = conn.execute("SELECT models, decks FROM col").fetchone()
    note_types = {}
    for ntid, m in json.loads(models_json).items():
        fields = [f["name"] for f in sorted(m["flds"], key=lambda f: f["ord"])]
        templates = {t["ord"]: (t["qfmt"], t["afmt"]) for t in m["tmpls"]}
        note_types[int(ntid)] = _NoteType(m["name"], fields, templates, m.get("type") == 1)
    decks = {int(did): tuple(d["name"].split("::")) for did, d in json.loads(decks_json).items()}
    return note_types, decks


def _load_cards(conn, deck: AnkiDeck, note_types: dict[int, _NoteType], deck_names) -> None:
    skipped: dict[str, int] = defaultdict(int)
    rows = conn.execute(
        "SELECT c.ord, c.did, n.guid, n.mid, n.flds, n.tags FROM cards c JOIN notes n ON n.id = c.nid ORDER BY c.did, n.id, c.ord"
    )
    for ord_, did, guid, mid, flds, tags in rows:
        nt = note_types.get(mid)
        if nt is None:
            skipped["unknown note type"] += 1
            continue
        values = dict(zip(nt.fields, flds.split("\x1f")))
        template = nt.templates.get(0 if nt.cloze else ord_)
        if template is None or "image-occlusion" in template[0] or "Image Occlusion" in nt.name:
            skipped[f"“{nt.name}” cards (not supported)"] += 1
            continue
        cloze = ord_ + 1 if nt.cloze else None
        prompt = render_template(template[0], values, cloze, answer_side=False)
        answer = answer_part(render_template(template[1], values, cloze, answer_side=True))
        if not plain(prompt) or not plain(answer):
            skipped["cards with an empty front or back"] += 1
            continue
        deck.cards.append(AnkiCard(
            source_ref=f"anki:{guid}:{ord_}",
            deck=deck_names.get(did, ("Imported",)),
            note_type=nt.name,
            prompt_html=prompt,
            answer_html=answer,
            # "null" is what some Markdown-to-Anki tools write for "no tags".
            tags=tuple(t for t in tags.split() if t.lower() != "null"),
        ))
    for reason, count in skipped.items():
        deck.warnings.append(f"Skipped {count} {reason.replace('cards', 'card', 1) if count == 1 else reason}.")


def _load_media(archive: zipfile.ZipFile, compressed: bool, deck: AnkiDeck) -> dict[str, bytes]:
    """File name (as written in the cards' HTML) → bytes. Zip member names come from Anki's index only."""
    if "media" not in archive.namelist():
        return {}
    raw = archive.read("media")
    entries: list[tuple[str, str]] = []  # (zip member, file name)
    if compressed:
        index = _zstd(raw, MAX_UNPACKED_BYTES)
        for position, entry in enumerate(proto_fields(index).get(1, [])):
            e = proto_fields(entry)
            member = str(e[255][0]) if 255 in e else str(position)
            entries.append((member, _text(e.get(1, []))))
    else:
        try:
            entries = [(k, v) for k, v in json.loads(raw or b"{}").items()]
        except ValueError:
            deck.warnings.append("The deck's media index couldn't be read; images were skipped.")
            return {}
    names = set(archive.namelist())
    media = {}
    for member, name in entries:
        if member not in names or not name or "/" in name or "\\" in name:
            continue
        data = archive.read(member)
        media[name] = _zstd(data, MAX_UNPACKED_BYTES) if compressed else data
    return media


# --- Anki's card templates (the subset decks actually use) ---

SECTION = re.compile(r"\{\{([#^])\s*([^}]+?)\s*\}\}(.*?)\{\{/\s*\2\s*\}\}", re.S)
REPLACEMENT = re.compile(r"\{\{([^#^/][^}]*?)\}\}")
CLOZE = re.compile(r"\{\{c(\d+)::(.*?)(?:::(.*?))?\}\}", re.S)
ANSWER_RULE = re.compile(r"<hr\s+id\s*=\s*[\"']?answer[\"']?\s*/?>", re.I)


def render_template(template: str, fields: dict[str, str], cloze: int | None, answer_side: bool) -> str:
    def section(m: re.Match) -> str:
        filled = bool(plain(fields.get(m[2].strip(), "")))
        return m[3] if filled == (m[1] == "#") else ""

    previous = None
    while previous != template:  # sections can nest
        previous, template = template, SECTION.sub(section, template)

    def replace(m: re.Match) -> str:
        *filters, name = [p.strip() for p in m[1].split(":")]
        if name == "FrontSide" or any(f.startswith("tts") for f in filters):
            return ""  # the question is shown separately; text-to-speech has nothing to show
        value = fields.get(name, "")
        if "cloze" in filters:
            return render_cloze(value, cloze, answer_side)
        if "type" in filters:
            return value if answer_side else ""  # Anki shows a typing box here; ours is always there
        return value

    return REPLACEMENT.sub(replace, template)


def render_cloze(text: str, number: int | None, answer_side: bool) -> str:
    def gap(m: re.Match) -> str:
        if number is None or int(m[1]) != number:
            return m[2]
        if answer_side:
            return f"<b>{m[2]}</b>"
        return f"[{m[3]}]" if m[3] else "[…]"

    return CLOZE.sub(gap, text)


def answer_part(html: str) -> str:
    """Anki answer templates usually repeat the question above an <hr id=answer>; keep what follows."""
    parts = ANSWER_RULE.split(html, maxsplit=1)
    return parts[1] if len(parts) == 2 else html


def plain(html: str) -> str:
    return BeautifulSoup(html or "", "html.parser").get_text(" ", strip=True)


# --- HTML → Markdown ---


def to_markdown(html: str, image_url: Callable[[str], str | None] = lambda name: None) -> str:
    """Convert a card side to Markdown. `image_url` maps an image file name to its stored URL (or None)."""
    soup = BeautifulSoup(html or "", "html.parser")
    for tag in soup(["script", "style"]):
        tag.decompose()
    for img in soup.find_all("img"):
        src = img.get("src", "")
        url = image_url(src)
        if url:
            img["src"] = url
            img["alt"] = img.get("alt") or src.rsplit(".", 1)[0]
        else:
            img.replace_with(f"[image: {src}]" if src else "")
    md = markdownify(
        str(soup), heading_style="ATX", bullets="-", strip=["span", "div", "font"],
        code_language_callback=lambda el: next(
            (c.removeprefix("language-") for c in (el.get("class") or []) if c.startswith("language-")), None
        ),
    )
    md = md.replace("\xa0", " ")
    md = re.sub(r"[ \t]+\n", "\n", md)
    return re.sub(r"\n{3,}", "\n\n", md).strip()


def collection_for(deck: tuple[str, ...]) -> tuple[str, str]:
    """Deck path → (group, collection name). `SQL::Joins` → ("SQL", "Joins"); `A::B::C` → ("A", "B › C")."""
    parts = [p.strip() for p in deck if p.strip()] or ["Imported"]
    return ("", parts[0]) if len(parts) == 1 else (parts[0], " › ".join(parts[1:]))


# --- importing into Learner ---

PARENT_ITSELF = "(cards in the deck itself)"


@dataclass
class Subdeck:
    label: str  # "Joins", or "Basics › Strings" for deeper decks, or PARENT_ITSELF
    count: int
    prompts: list[str]  # a few example prompts, as plain text (for you and for the AI's ordering)


@dataclass
class ParentDeck:
    """A top-level Anki deck and what's under it."""

    name: str
    subdecks: list[Subdeck]  # in Anki's order (parent's own cards first, then alphabetical)


@dataclass
class DeckChoice:
    """How to bring one top-level deck in (chosen on the preview, DESIGN.md §12.3).

    "tiers": one collection named `collection` (in `group`), each subdeck a tier per `tiers`.
    "collections": each subdeck its own collection, grouped under the deck's name.
    """

    mode: str
    collection: str
    group: str
    tiers: dict[str, int]  # subdeck label → tier


def parent_decks(deck: AnkiDeck) -> list[ParentDeck]:
    by_parent: dict[str, dict[str, list[AnkiCard]]] = defaultdict(lambda: defaultdict(list))
    for card in deck.cards:
        top, *rest = [p.strip() for p in card.deck if p.strip()] or ["Imported"]
        by_parent[top][" › ".join(rest) or PARENT_ITSELF].append(card)
    parents = []
    for name in sorted(by_parent, key=str.casefold):
        subs = by_parent[name]
        order = sorted(subs, key=lambda label: (label != PARENT_ITSELF, label.casefold()))
        parents.append(ParentDeck(name, [Subdeck(label, len(subs[label]), [plain(c.prompt_html)[:160] for c in subs[label][:3]])
                                         for label in order]))
    return parents


def default_choices(conn: sqlite3.Connection, deck: AnkiDeck, order: dict[str, list[str]] | None = None) -> list[DeckChoice]:
    """Tiers by default: one tier per subdeck, in `order` (e.g. the AI's suggestion) or Anki's order.
    If a same-named collection already holds other cards, suggest "<name> (Anki)" instead of mixing them."""
    refs = {c.source_ref for c in deck.cards}
    choices = []
    for parent in parent_decks(deck):
        name = parent.name
        existing = store.find_collection(conn, name)
        if existing and any(c.source_ref not in refs for c in store.list_cards(conn, existing.id, include_archived=True)):
            name = f"{parent.name} (Anki)"
        labels = [s.label for s in parent.subdecks]
        ranked = [l for l in (order or {}).get(parent.name, []) if l in labels]
        ranked += [l for l in labels if l not in ranked]
        choices.append(DeckChoice("tiers", name, "", {label: i for i, label in enumerate(ranked, start=1)}))
    return choices


def _target(card: AnkiCard, parents: dict[str, DeckChoice]) -> tuple[str, str, int | None, str]:
    """(group, collection, tier or None, subdeck label) for a card under the chosen layout."""
    top, *rest = [p.strip() for p in card.deck if p.strip()] or ["Imported"]
    label = " › ".join(rest) or PARENT_ITSELF
    choice = parents.get(top)
    if choice is None or choice.mode == "collections":
        group, name = collection_for(card.deck)
        return group, name, None, label
    return choice.group.strip(), choice.collection.strip() or top, max(1, choice.tiers.get(label, 1)), label


@dataclass
class CollectionPlan:
    group: str
    name: str
    exists: bool
    collection_id: int | None = None
    new: int = 0
    updated: int = 0
    unchanged: int = 0
    tier_labels: dict[int, str] = field(default_factory=dict)
    samples: list[tuple[str, str]] = field(default_factory=list)  # (prompt, answer) Markdown

    @property
    def display_name(self) -> str:
        return f"{self.group} › {self.name}" if self.group else self.name


def _card_in(card: AnkiCard, existing: Card | None, image_url: Callable[[str], str | None], tier: int | None) -> CardIn:
    """The card as Learner stores it. Anki's prompt, answer and tags are the source of truth. On
    re-import, what you changed locally (format, key points, links, archived) is kept, and so is the
    tier, unless the import assigns tiers from subdecks (an explicit choice on the preview)."""
    keep = {"tier": 1, "answer_format": "text", "rubric": [], "references": [], "archived": False}
    if existing is not None:
        keep = {"tier": existing.tier, "answer_format": existing.answer_format, "rubric": existing.rubric,
                "references": [{"title": r.title, "url": r.url} for r in existing.references],
                "archived": existing.archived}
    if tier is not None:
        keep["tier"] = tier
    return CardIn(prompt=to_markdown(card.prompt_html, image_url), reference_answer=to_markdown(card.answer_html, image_url),
                  tags=list(card.tags), **keep)


def _preview_url(deck: AnkiDeck) -> Callable[[str], str | None]:
    """Where each image will live once imported (its fingerprint), without storing anything yet."""
    def url(name: str) -> str | None:
        data = deck.media.get(name)
        return f"/media/{hashlib.sha256(data).hexdigest()}" if data and media.detect_mime(data) else None
    return url


def _walk(conn, deck: AnkiDeck, choices: list[DeckChoice], image_url, write: bool):
    by_parent = {p.name: c for p, c in zip(parent_decks(deck), choices)}
    plans: dict[tuple[str, str], CollectionPlan] = {}
    warnings: list[str] = []
    moved_from: set[int] = set()  # collections that imported cards left (e.g. after switching to tiers)
    for card in deck.cards:
        group, name, tier, label = _target(card, by_parent)
        if (group, name) not in plans:
            found = store.find_collection(conn, name, group)
            cid = found.id if found else (store.save_collection(conn, name, "mysql", {}, group=group) if write else None)
            plans[(group, name)] = CollectionPlan(group, name, found is not None, cid)
        plan = plans[(group, name)]
        if tier is not None:
            shown = "General" if label == PARENT_ITSELF else label  # the parent deck's own cards
            plan.tier_labels[tier] = shown if tier not in plan.tier_labels or plan.tier_labels[tier] == shown \
                else f"{plan.tier_labels[tier]} + {shown}"
        existing = store.find_card_by_source(conn, card.source_ref)
        try:
            incoming = _card_in(card, existing, image_url, tier)
        except ValidationError:
            warnings.append(f"Skipped a card in {plan.display_name} that was empty after conversion.")
            continue
        if write:
            if existing is not None and existing.collection_id != plan.collection_id:
                moved_from.add(existing.collection_id)
            card_id, changed = store.save_card(conn, plan.collection_id, incoming, existing.id if existing else None)
            if existing is None:
                store.set_card_source(conn, card_id, card.source_ref)
        else:
            changed = existing is None or (
                existing.prompt, existing.reference_answer, existing.tags, existing.collection_id, existing.tier
            ) != (incoming.prompt, incoming.reference_answer, incoming.tags, plan.collection_id, incoming.tier)
        if existing is None:
            plan.new += 1
        elif changed:
            plan.updated += 1
        else:
            plan.unchanged += 1
        if len(plan.samples) < 2:
            plan.samples.append((incoming.prompt, incoming.reference_answer))
    if write:  # collections an earlier import filled and this one emptied are archived, like a combine (§12.4)
        targets = {p.collection_id for p in plans.values()}
        for cid in moved_from - targets:
            if not store.list_cards(conn, cid, include_archived=True):
                store.set_collection_archived(conn, cid, True)
                warnings.append(f"Archived “{store.get_collection(conn, cid).display_name}”, now empty.")
    if write:  # subdeck names become tier labels (existing labels for other tiers are kept)
        for plan in plans.values():
            if plan.tier_labels:
                current = store.get_collection(conn, plan.collection_id)
                store.save_collection(conn, current.name, current.sql_dialect, current.tier_labels | plan.tier_labels,
                                      plan.collection_id, group=current.group)
    return sorted(plans.values(), key=lambda p: (p.group, p.name)), warnings


def plan_import(conn: sqlite3.Connection, deck: AnkiDeck, choices: list[DeckChoice]) -> list[CollectionPlan]:
    """What an import would do, per target collection. Writes nothing."""
    return _walk(conn, deck, choices, _preview_url(deck), write=False)[0]


def apply_import(
    conn: sqlite3.Connection, deck: AnkiDeck, choices: list[DeckChoice], max_image_bytes: int
) -> tuple[list[CollectionPlan], list[str]]:
    """Import the deck with the chosen layout: collections are created as needed, cards added or updated."""
    warnings = list(deck.warnings)
    stored: dict[str, str | None] = {}

    def image_url(name: str) -> str | None:
        if name not in stored:
            data = deck.media.get(name)
            try:
                stored[name] = media.save_image(conn, data, name, name.rsplit(".", 1)[0], max_image_bytes).url if data else None
            except media.MediaError as exc:
                warnings.append(f"Image “{name}” wasn't imported: {exc}")
                stored[name] = None
            if data is None:
                warnings.append(f"Image “{name}” is used by a card but missing from the deck.")
        return stored[name]

    plans, more = _walk(conn, deck, choices, image_url, write=True)
    for plan in plans:
        tiers.tidy(conn, plan.collection_id)
    return plans, list(dict.fromkeys(warnings + more))
