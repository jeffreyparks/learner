"""FastAPI app: routes and templates only. Study logic lives in engine.py, data access in store.py."""

from __future__ import annotations

import argparse
import json
import re
import secrets
import sqlite3
import threading
import webbrowser
from urllib.parse import quote, urlsplit
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from pydantic import BaseModel, ValidationError

from . import config as config_module
from . import anki, checkers, engine, media, organize, sequencer, stats, store, tiers, tutor
from .config import Config
from .llm import LLMError, LLMProvider, make_provider
from .models import ANSWER_FORMATS, SQL_DIALECTS, Card, CardIn, Collection, CollectionIn, describe_errors

PACKAGE = Path(__file__).parent
LOCAL_HOSTS = ("127.0.0.1", "localhost")
NEW_COLLECTION = "new"  # the card form's "+ New collection…" option
NEW_TIER = "new"  # the card form's "+ New tier at the end" option
_markdown = MarkdownIt("commonmark", {"html": False}).enable("table")


def render_markdown(text: str) -> Markup:
    return Markup(_markdown.render(text or ""))


def plain_title(text: str, length: int = 90) -> str:
    """A one-line title from a markdown prompt, for lists: first line, markdown markers removed."""
    line = next((l for l in (text or "").splitlines() if l.strip()), "")
    line = re.sub(r"^\s*(?:#+|>)\s*", "", line)  # heading / quote markers
    line = line.replace("`", "").replace("**", "").strip()  # keep underscores: they're in identifiers
    return line if len(line) <= length else line[: length - 1].rstrip() + "…"


def render_answer(card: Card) -> Markup:
    if card.answer_format == "text":
        return render_markdown(card.reference_answer)
    return Markup(f'<pre><code class="language-{card.answer_format}">{escape(card.reference_answer)}</code></pre>')


class CheckRequest(BaseModel):
    format: str
    code: str
    dialect: str | None = None


class TutorRequest(BaseModel):
    card_id: int
    answer: str = ""
    level: int | None = None  # hints only
    earlier: list[str] = []  # hints only: the hints already shown, so the next one goes further


@dataclass
class Snapshot:
    """A collection's active cards, their attempts, and the progress calculated from them."""

    collection: Collection
    cards: list[Card]
    attempts: list
    progress: dict[int, engine.Progress]
    tiers: list[engine.TierSummary]
    order: sequencer.CardSequence

    def tier(self, tier: int) -> engine.TierSummary | None:
        return next((t for t in self.tiers if t.tier == tier), None)

    def next_tier(self, tier: int) -> engine.TierSummary | None:
        return next((t for t in self.tiers if t.tier > tier), None)


def _int(value: str | None) -> int | None:
    try:
        return int(value) if value not in (None, "") else None
    except ValueError:
        return None


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def card_from_form(form) -> CardIn:
    """Build a CardIn from the card form. References are one per line: 'Title | URL' (or just a URL)."""
    references = []
    for line in _lines(form.get("references", "")):
        title, sep, url = line.rpartition("|")
        references.append({"title": title.strip(), "url": url.strip()} if sep else {"title": line, "url": line})
    return CardIn(
        tier=form.get("tier", ""),
        answer_format=form.get("answer_format", "text"),
        prompt=form.get("prompt", ""),
        reference_answer=form.get("reference_answer", ""),
        rubric=_lines(form.get("rubric", "")),
        references=references,
        tags=[t.strip() for t in form.get("tags", "").split(",") if t.strip()],
    )


def concepts_to_form(analysis) -> dict[str, str]:
    if analysis is None:
        return {"concepts_introduced": "", "concepts_required": "", "concepts_locked": ""}
    return {
        "concepts_introduced": ", ".join(analysis.introduced),
        "concepts_required": ", ".join(analysis.required),
        "concepts_locked": "1" if analysis.manual else "",
    }


def card_to_form(card: Card) -> dict[str, str]:
    return {
        "collection_id": str(card.collection_id),
        "tier": str(card.tier),
        "answer_format": card.answer_format,
        "prompt": card.prompt,
        "reference_answer": card.reference_answer,
        "rubric": "\n".join(card.rubric),
        "references": "\n".join(f"{r.title} | {r.url}" for r in card.references),
        "tags": ", ".join(card.tags),
    }


def seed_starter(conn: sqlite3.Connection) -> None:
    for path in sorted((PACKAGE / "starter").glob("*.json")):
        store.import_collection(conn, json.loads(path.read_text()))


def create_app(cfg: Config, provider: LLMProvider | None = None) -> FastAPI:
    """`provider` defaults to the one configured in `cfg` (None when LLM features are off)."""
    if provider is None:
        provider = make_provider(cfg)
    db_path = Path(cfg.db_path).expanduser()
    with closing(store.connect(db_path)) as conn:
        store.migrate(conn)
        if not store.list_collections(conn, include_archived=True):
            seed_starter(conn)
        tiers.tidy_all(conn)  # every collection's tiers are 1…N with a label entry each (DESIGN.md §12.5)

    app = FastAPI(title="Learner", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        """The app has no login, so only this machine's browser tabs may use it. Reject other Host
        names (DNS rebinding) and writes sent from other websites (cross-site form posts), which
        could otherwise change your cards or spend API credits."""
        if urlsplit(f"//{request.headers.get('host', '')}").hostname not in LOCAL_HOSTS:
            return PlainTextResponse("Learner only answers on 127.0.0.1 or localhost.", status_code=403)
        origin = request.headers.get("origin")
        if request.method not in ("GET", "HEAD") and origin and urlsplit(origin).hostname not in LOCAL_HOSTS:
            return PlainTextResponse("Cross-site requests aren't allowed.", status_code=403)
        return await call_next(request)
    app.mount("/static", StaticFiles(directory=PACKAGE / "static"), name="static")
    templates = Jinja2Templates(directory=PACKAGE / "templates")
    templates.env.filters["md"] = render_markdown
    templates.env.filters["card_title"] = plain_title
    templates.env.globals.update(
        render_answer=render_answer,
        ANSWER_FORMATS=ANSWER_FORMATS,
        ai_grading=provider is not None,
        MAX_HINT_LEVEL=tutor.MAX_HINT_LEVEL,
        SQL_DIALECTS=SQL_DIALECTS,
        NEW_COLLECTION=NEW_COLLECTION,
        NEW_TIER=NEW_TIER,
        cfg=cfg,
        ai_off_by_choice=cfg.provider == "off",
    )

    def db():
        return closing(store.connect(db_path))

    def page(request: Request, name: str, **context):
        return templates.TemplateResponse(request, name, context)

    def redirect(url: str) -> RedirectResponse:
        return RedirectResponse(url, status_code=303)

    def snapshot(conn: sqlite3.Connection, collection: Collection) -> Snapshot:
        """Calculate progress and persist any new (sticky) tier unlock."""
        cards = store.list_cards(conn, collection.id)
        attempts = store.list_attempts(conn, collection.id)
        progress = engine.progress_map(cards, attempts, cfg.known_streak)
        all_tiers = sorted(collection.tier_labels)
        unlocked = engine.unlocked_through(cards, progress, collection.max_unlocked_tier, cfg.unlock_pct, all_tiers)
        if unlocked > collection.max_unlocked_tier:
            store.raise_unlocked_tier(conn, collection.id, unlocked)
        order = sequencer.build(cards, store.list_analyses(conn, collection.id))
        summaries = engine.tier_summaries(cards, progress, unlocked, all_tiers)
        return Snapshot(collection, cards, attempts, progress, summaries, order)

    def load_collection(conn, collection_id: int) -> Collection:
        collection = store.get_collection(conn, collection_id)
        if collection is None:
            raise HTTPException(404, "Collection not found")
        return collection

    def load_card(conn, card_id: int) -> Card:
        card = store.get_card(conn, card_id)
        if card is None:
            raise HTTPException(404, "Card not found")
        return card

    # --- dashboard ---

    @app.get("/")
    def dashboard(request: Request, reset: str = ""):
        with db() as conn:
            snapshots = [snapshot(conn, c) for c in store.list_collections(conn)]
            archived = [c for c in store.list_collections(conn, include_archived=True) if c.archived]
        groups: dict[str, list] = {}  # ungrouped ("") first, then groups A–Z (list_collections orders them)
        for s in snapshots:
            groups.setdefault(s.collection.group, []).append((s, engine.default_tier(s.tiers)))
        return page(request, "dashboard.html", groups=list(groups.items()), archived=archived, reset=reset)

    # --- study ---

    @app.get("/study/{collection_id}/{tier}")
    def study_start(request: Request, collection_id: int, tier: int, confirm: bool = False):
        with db() as conn:
            snap = snapshot(conn, load_collection(conn, collection_id))
        summary = snap.tier(tier)
        if summary is None or not summary.total:  # missing or empty: nothing to study
            return redirect("/")
        if not summary.unlocked and not confirm:
            previous = max((t for t in snap.tiers if t.tier < tier), key=lambda t: t.tier, default=None)
            return page(request, "study_locked.html", snap=snap, tier=summary, previous=previous)
        session_id = secrets.token_urlsafe(8)
        return redirect(f"/study/{collection_id}/{tier}/{session_id}")

    @app.get("/study/{collection_id}/{tier}/{session_id}")
    def study_card(request: Request, collection_id: int, tier: int, session_id: str):
        with db() as conn:
            snap = snapshot(conn, load_collection(conn, collection_id))
        state = engine.session_state(snap.cards, snap.attempts, session_id, tier, cfg, snap.order)
        if state.next_card_id is None:
            return redirect(f"/study/{collection_id}/{tier}/{session_id}/summary")
        card = next(c for c in snap.cards if c.id == state.next_card_id)
        return page(
            request, "study.html", snap=snap, tier=tier, session_id=session_id,
            card=card, state=state, progress=snap.progress[card.id],
        )

    @app.post("/study/{collection_id}/{tier}/{session_id}")
    async def study_answer(request: Request, collection_id: int, tier: int, session_id: str):
        form = await request.form()
        result = form.get("result")
        if result not in ("right", "wrong", "revealed"):
            raise HTTPException(400, "Bad answer")
        right = result == "right"
        hints_used = min(max(_int(form.get("max_hint_level")) or 0, 0), tutor.MAX_HINT_LEVEL)
        llm_verdict = form.get("llm_verdict") if form.get("llm_verdict") in tutor.VERDICTS and result != "revealed" else None
        with db() as conn:
            card = load_card(conn, _int(form.get("card_id")) or 0)
            if card.collection_id != collection_id:
                raise HTTPException(400, "Card is not in this collection")
            snap = snapshot(conn, load_collection(conn, collection_id))
            if engine.session_state(snap.cards, snap.attempts, session_id, tier, cfg, snap.order).next_card_id != card.id:
                # Not the card this session is showing: a repeat submit. Don't record it twice.
                return redirect(f"/study/{collection_id}/{tier}/{session_id}")
            store.add_attempt(
                conn, card.id, session_id, engine.REVEALED if result == "revealed" else engine.outcome_for(right, hints_used),
                answer=form.get("answer") or None,
                duration_ms=_int(form.get("duration_ms")),
                max_hint_level=hints_used,
                llm_verdict=llm_verdict,
                llm_feedback=(form.get("llm_feedback") or None) if llm_verdict else None,
                overridden=llm_verdict is not None and (llm_verdict == "correct") != right,
            )
            snapshot(conn, load_collection(conn, collection_id))  # persists any tier unlock
        return redirect(f"/study/{collection_id}/{tier}/{session_id}")

    @app.get("/study/{collection_id}/{tier}/{session_id}/summary")
    def study_summary(request: Request, collection_id: int, tier: int, session_id: str):
        with db() as conn:
            snap = snapshot(conn, load_collection(conn, collection_id))
        summary = engine.session_summary(snap.cards, snap.attempts, session_id, cfg.known_streak)
        return page(
            request, "study_done.html", snap=snap, summary=summary,
            tier=snap.tier(tier), next_tier=snap.next_tier(tier),
        )

    # --- the LLM tutor (called by the study screen) ---

    # Plain `def` handlers: FastAPI runs them on a worker thread, so a slow LLM call never
    # blocks the rest of the app (syntax checks, other pages). The same goes for /check below.

    def card_and_dialect(card_id: int) -> tuple[Card, str, list[tuple[str, bytes]]]:
        """The card, its collection's SQL dialect, and the images it shows (for the AI to see)."""
        with db() as conn:
            card = load_card(conn, card_id)
            images = [(i.mime, i.data) for i in media.images_for(conn, card.prompt, card.reference_answer)]
            return card, load_collection(conn, card.collection_id).sql_dialect, images

    @app.post("/grade")
    def grade_answer(body: TutorRequest):
        card, dialect, images = card_and_dialect(body.card_id)
        if provider is None:
            return {"error": "No LLM is configured."}
        if not body.answer.strip():
            return {"error": "Nothing to grade."}
        try:
            grade = tutor.grade(provider, card, body.answer, dialect, images)
        except LLMError as exc:
            return {"error": str(exc)}
        return {
            "verdict": grade.verdict, "right": grade.right, "feedback": grade.feedback,
            "rubric_hits": grade.rubric_hits, "rubric_misses": grade.rubric_misses,
            "equivalent_approach": grade.equivalent_approach,
        }

    @app.post("/hint")
    def give_hint(body: TutorRequest):
        card, dialect, images = card_and_dialect(body.card_id)
        if provider is None:
            return {"error": "No LLM is configured."}
        earlier = body.earlier[: tutor.MAX_HINT_LEVEL]
        level = body.level or len(earlier) + 1
        try:
            return {"hint": tutor.hint(provider, card, body.answer, level, earlier, dialect, images), "level": level}
        except LLMError as exc:
            return {"error": str(exc)}

    # --- images (DESIGN.md §12.1) ---

    max_image_bytes = int(cfg.max_image_mb * 1_000_000)

    @app.post("/media")
    async def media_upload(request: Request):
        """Store an uploaded or pasted image; returns the Markdown to insert into the card."""
        form = await request.form()
        upload = form.get("file")
        if upload is None or not hasattr(upload, "read"):
            return JSONResponse({"error": "No image received."}, status_code=400)
        data = await upload.read(max_image_bytes + 1)
        alt = (form.get("alt") or "").strip().replace("]", ")").replace("\n", " ")
        try:
            with db() as conn:
                image = media.save_image(conn, data, getattr(upload, "filename", "") or "", alt, max_image_bytes)
        except media.MediaError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return {"url": image.url, "markdown": f"![{alt or 'image'}]({image.url})", "sha256": image.sha256}

    @app.get("/media/{sha256}")
    def media_get(sha256: str):
        if not re.fullmatch(r"[0-9a-f]{64}", sha256):
            raise HTTPException(404, "Image not found")
        with db() as conn:
            image = media.get_image(conn, sha256)
        if image is None:
            raise HTTPException(404, "Image not found")
        # Content never changes for a given fingerprint, so browsers can cache it for good.
        return Response(image.data, media_type=image.mime, headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "X-Content-Type-Options": "nosniff",
        })

    # --- syntax checking (called by the code editor as you type) ---

    @app.post("/check")
    def check_answer(body: CheckRequest):  # plain `def`: ruff runs as a subprocess
        if body.format not in ANSWER_FORMATS:
            raise HTTPException(400, "Unknown answer format")
        dialect = body.dialect if body.dialect in SQL_DIALECTS else "mysql"
        return {"diagnostics": [d.to_dict() for d in checkers.check(body.format, body.code, dialect)]}

    # --- browse & cards ---

    @app.get("/browse")
    def browse(request: Request):
        q = request.query_params
        collection_id, tier = _int(q.get("collection_id")), _int(q.get("tier"))
        status, tag, show_archived = q.get("status", ""), q.get("tag", ""), q.get("archived") == "1"
        rows, tags = [], set()
        with db() as conn:
            collections = store.list_collections(conn)
            for collection in collections:
                if collection_id and collection.id != collection_id:
                    continue
                snap = snapshot(conn, collection)
                for card in store.list_cards(conn, collection.id, include_archived=show_archived):
                    card_status = "archived" if card.archived else snap.progress[card.id].status
                    tags.update(card.tags)
                    if (tier and card.tier != tier) or (status and card_status != status) or (tag and tag not in card.tags):
                        continue
                    rows.append((collection, card, card_status))
        tier_choices = {c.id: [[t, c.tier_label(t)] for t in sorted(c.tier_labels)] for c in collections}
        return page(
            request, "browse.html", rows=rows, collections=collections, tags=sorted(tags), tier_choices=tier_choices,
            moved=q.get("moved"), back=str(request.url.query),
            filters={"collection_id": collection_id, "tier": tier, "status": status, "tag": tag, "archived": show_archived},
        )

    @app.post("/cards/move")
    async def cards_move(request: Request):
        """Bulk "Move selected to…" from the Cards page: any collection, any tier (or a new one at the end)."""
        form = await request.form()
        collection_id = _int(form.get("to_collection")) or 0
        card_ids = [i for v in form.getlist("card_ids") if (i := _int(v)) is not None]
        back = form.get("back") or ""
        with db() as conn:
            load_collection(conn, collection_id)
            to = tiers.count(conn, collection_id) + 1 if form.get("to") == NEW_TIER else (_int(form.get("to")) or 0)
            try:
                moved = tiers.move_cards(conn, card_ids, collection_id, to) if card_ids else 0
            except tiers.TierError:
                moved = 0
        query = "&".join(p for p in back.split("&") if not p.startswith("moved="))
        return redirect(f"/browse?{query}&moved={moved}")

    def card_page(request, conn, form, errors=(), card=None):
        progress = None
        if card is not None:
            snap = snapshot(conn, load_collection(conn, card.collection_id))
            progress = snap.progress.get(card.id)
        collections = store.list_collections(conn)
        # Each collection's real tiers, for the Tier dropdown (rebuilt in the browser when the collection changes).
        tier_choices = {c.id: [[t, c.tier_label(t)] for t in sorted(c.tier_labels)] for c in collections}
        return page(
            request, "card_form.html", form=form, errors=list(errors), card=card, progress=progress,
            collections=collections, groups=store.list_groups(conn), tier_choices=tier_choices,
        )

    @app.get("/cards/new")
    def card_new(request: Request, collection_id: str = "", tier: str = ""):
        with db() as conn:
            return card_page(request, conn, {"collection_id": collection_id, "tier": tier or "1", "answer_format": "text"})

    @app.get("/cards/{card_id:int}")
    def card_edit(request: Request, card_id: int):
        with db() as conn:
            card = load_card(conn, card_id)
            analysis = store.list_analyses(conn, card.collection_id).get(card.id)
            return card_page(request, conn, card_to_form(card) | concepts_to_form(analysis), card=card)

    def save_concepts(conn, card_id: int, form: dict) -> None:
        """Concepts you type in are kept as yours (`manual`): a rebuild never overwrites them.
        Unticking "Keep my concepts" hands the card back to the AI at the next rebuild."""
        introduced = sequencer.concept_list(form.get("concepts_introduced", "").split(","))
        required = sequencer.concept_list(form.get("concepts_required", "").split(","))
        current = store.list_analyses(conn, load_card(conn, card_id).collection_id).get(card_id)
        changed = (introduced, required) != ((current.introduced, current.required) if current else ([], []))
        if changed:
            store.save_analysis(conn, card_id, introduced, required,
                                current.difficulty if current else 3, manual=True)
        elif current is not None and current.manual != bool(form.get("concepts_locked")):
            store.set_analysis_manual(conn, card_id, bool(form.get("concepts_locked")))

    async def save_card_form(request: Request, card_id: int | None):
        form = dict(await request.form())
        with db() as conn:
            card = load_card(conn, card_id) if card_id else None
            new_tier = form.get("tier") == NEW_TIER
            try:
                data = card_from_form(form | ({"tier": "1"} if new_tier else {}))
            except ValidationError as exc:
                return card_page(request, conn, form, describe_errors(exc), card)
            if form.get("collection_id") == NEW_COLLECTION:
                name = (form.get("new_collection") or "").strip()
                group = (form.get("new_collection_group") or "").strip()
                if not name:
                    return card_page(request, conn, form, ["Give the new collection a name."], card)
                if store.find_collection(conn, name, group):
                    return card_page(request, conn, form, [f"“{name}” already exists: choose it from the list."], card)
                form["collection_id"] = str(store.save_collection(conn, name, "mysql", {}, group=group))
            collection_id = _int(form.get("collection_id"))
            if collection_id is None or store.get_collection(conn, collection_id) is None:
                return card_page(request, conn, form, ["Choose a collection."], card)
            # Only real tiers (or a new one at the end) can be chosen, so labels never fall out of step.
            tier_count = tiers.count(conn, collection_id)
            if new_tier or tier_count == 0:
                data = data.model_copy(update={"tier": tiers.add(conn, collection_id)})
            elif data.tier > tier_count:
                return card_page(request, conn, form, ["Choose one of the collection's tiers."], card)
            if card is not None:
                data = data.model_copy(update={"archived": card.archived})
            saved_id, _ = store.save_card(conn, collection_id, data, card_id)
            save_concepts(conn, saved_id, form)
        return redirect(f"/cards/{saved_id}?saved=1")

    @app.post("/cards/new")
    async def card_create(request: Request):
        return await save_card_form(request, None)

    @app.post("/cards/{card_id:int}")
    async def card_update(request: Request, card_id: int):
        return await save_card_form(request, card_id)

    @app.post("/cards/{card_id:int}/archive")
    def card_archive(card_id: int):
        with db() as conn:
            card = load_card(conn, card_id)
            store.set_card_archived(conn, card_id, not card.archived)
            # Back from the archive into a tier that was deleted meanwhile? It goes to the last tier.
            moved = card.archived and tiers.place_unarchived(conn, card_id)
        return redirect(f"/cards/{card_id}" + ("?moved_tier=1" if moved else ""))

    @app.post("/cards/{card_id:int}/reset")
    def card_reset(card_id: int):
        with db() as conn:
            load_card(conn, card_id)
            store.reset_card_progress(conn, card_id)
        return redirect(f"/cards/{card_id}?reset=1")

    # --- collections ---

    def collection_page(request, form, errors=(), collection=None):
        with db() as conn:
            groups = store.list_groups(conn)
            snap = snapshot(conn, load_collection(conn, collection.id)) if collection else None
        return page(request, "collection_form.html", form=form, errors=list(errors), collection=collection, groups=groups,
                    snap=snap, notice=request.query_params.get("tiers"))

    @app.get("/collections/new")
    def collection_new(request: Request):
        return collection_page(request, {"sql_dialect": "mysql", "tiers": "Foundations\nIntermediate\nAdvanced"})

    @app.get("/collections/{collection_id:int}")
    def collection_edit(request: Request, collection_id: int):
        with db() as conn:
            c = load_collection(conn, collection_id)
        return collection_page(request, {"name": c.name, "group": c.group, "sql_dialect": c.sql_dialect}, collection=c)

    async def save_collection_form(request: Request, collection_id: int | None):
        form = dict(await request.form())
        with db() as conn:
            collection = load_collection(conn, collection_id) if collection_id else None
            try:
                # Tiers are set here only when creating; after that, the Tiers panel owns them.
                labels = collection.tier_labels if collection else {
                    i: line for i, line in enumerate(_lines(form.get("tiers", "")), start=1)}
                data = CollectionIn(collection=form.get("name", ""), group=form.get("group", ""),
                                    sql_dialect=form.get("sql_dialect", "mysql"), tier_labels=labels)
                saved_id = store.save_collection(conn, data.collection, data.sql_dialect, data.tier_labels,
                                                 collection_id, group=data.group)
            except ValueError as exc:  # includes pydantic's ValidationError
                errors = describe_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
                return collection_page(request, form, errors, collection)
            except sqlite3.IntegrityError:
                return collection_page(request, form, ["A collection with that name already exists in that group."], collection)
        return redirect(f"/collections/{saved_id}?saved=1")

    @app.post("/collections/new")
    async def collection_create(request: Request):
        return await save_collection_form(request, None)

    @app.post("/collections/{collection_id:int}")
    async def collection_update(request: Request, collection_id: int):
        return await save_collection_form(request, collection_id)

    @app.post("/collections/{collection_id:int}/tiers")
    async def collection_tiers(request: Request, collection_id: int):
        """The Tiers panel: labels are saved with every action, then the action (op) runs."""
        form = await request.form()
        op, _, arg = (form.get("op") or "save").partition(":")
        tier = _int(arg)
        with db() as conn:
            load_collection(conn, collection_id)
            labels = {t: form.get(f"label_{t}", "") for t in range(1, tiers.count(conn, collection_id) + 1)}
            tiers.set_labels(conn, collection_id, labels)
            try:
                if op == "up":
                    tiers.swap(conn, collection_id, tier, tier - 1)
                    message = f"Moved tier {tier} up."
                elif op == "down":
                    tiers.swap(conn, collection_id, tier, tier + 1)
                    message = f"Moved tier {tier} down."
                elif op == "move":
                    to = _int(form.get(f"move_to_{tier}"))
                    moved = tiers.move_tier_cards(conn, collection_id, tier, to)
                    message = f"Moved {moved} card{'' if moved == 1 else 's'} from tier {tier} to tier {to}."
                elif op == "delete":
                    archived = tiers.delete(conn, collection_id, tier)
                    message = f"Deleted tier {tier}" + (f" and archived its {archived} card{'' if archived == 1 else 's'}." if archived else ".")
                elif op == "add":
                    new = tiers.add(conn, collection_id, form.get("new_label", ""))
                    message = f"Added tier {new}."
                else:
                    message = "Tier names saved."
            except tiers.TierError as exc:
                message = str(exc)
        return redirect(f"/collections/{collection_id}?tiers={quote(message)}#tiers")

    @app.post("/collections/{collection_id:int}/archive")
    def collection_archive(collection_id: int):
        with db() as conn:
            c = load_collection(conn, collection_id)
            store.set_collection_archived(conn, collection_id, not c.archived)
        return redirect(f"/collections/{collection_id}")

    @app.get("/collections/{collection_id:int}/export")
    def collection_export(collection_id: int):
        with db() as conn:
            c = load_collection(conn, collection_id)
            data = store.export_collection(conn, collection_id)
        filename = re.sub(r"[^a-z0-9]+", "-", c.name.lower()).strip("-") or "collection"
        return JSONResponse(data, headers={"Content-Disposition": f'attachment; filename="{filename}.json"'})

    # --- authoring assistant: draft cards from your notes ---

    def draft_page(request, collection_id: int | None, form: dict, drafts=(), errors=(), saved=()):
        with db() as conn:
            collections = store.list_collections(conn)
        return page(request, "draft.html", collections=collections, collection_id=collection_id, form=form,
                    drafts=list(drafts), errors=list(errors), saved=list(saved), max_drafts=tutor.MAX_DRAFTS)

    @app.get("/cards/draft")
    def draft_start(request: Request, collection_id: str = "", concept: str = "", tier: str = ""):
        notes = f"Write a card that teaches “{concept}”." if concept else ""
        return draft_page(request, _int(collection_id),
                          {"notes": notes, "count": "1" if concept else "3", "tier": tier, "concept": concept})

    @app.post("/cards/draft")
    def draft_generate(  # plain `def`: drafting is a slow AI call
        request: Request, collection_id: str = Form(""), notes: str = Form(""), count: str = Form("3"),
        tier: str = Form(""), concept: str = Form(""),
    ):
        form = {"notes": notes, "count": count, "tier": tier, "concept": concept}
        cid = _int(collection_id)
        if provider is None:
            return draft_page(request, cid, form, errors=["Drafting needs AI, and none is set up. See the note above."])
        if cid is None or not notes.strip():
            return draft_page(request, cid, form, errors=["Choose a collection and add some notes."])
        with db() as conn:
            collection = load_collection(conn, cid)
            cards = store.list_cards(conn, cid)
            analyses = store.list_analyses(conn, cid)
        try:
            drafts = tutor.draft_cards(provider, collection, cards, analyses, notes, _int(count) or 3, _int(tier), concept)
        except LLMError as exc:
            return draft_page(request, cid, form, errors=[str(exc)])
        return draft_page(request, cid, form, drafts=drafts)

    @app.post("/cards/draft/save")
    async def draft_save(request: Request):
        """Save the drafts you ticked. Suggested links are only kept if you ticked them too."""
        form = await request.form()
        cid = _int(form.get("collection_id"))
        saved, errors = [], []
        with db() as conn:
            load_collection(conn, cid or 0)
            for key in form:
                if not key.startswith("keep_"):
                    continue
                n = key.removeprefix("keep_")
                draft = json.loads(form.get(f"draft_{n}", "{}"))
                card_data = draft.get("card", {}) | {"references": draft.get("references", []) if form.get(f"refs_{n}") else []}
                try:
                    card = CardIn.model_validate(card_data)
                except ValidationError as exc:
                    errors += [f"Draft {int(n) + 1}: {e}" for e in describe_errors(exc)]
                    continue
                card_id, _ = store.save_card(conn, cid, card)
                if draft.get("introduced") or draft.get("required"):  # the AI's concepts: a rebuild may refine them
                    store.save_analysis(conn, card_id, draft.get("introduced", []), draft.get("required", []))
                saved.append(store.get_card(conn, card_id))
        if not saved and not errors:
            errors = ["Tick at least one draft to save."]
        return draft_page(request, cid, {"notes": "", "count": "3", "tier": "", "concept": ""}, errors=errors, saved=saved)

    # --- stats ---

    @app.get("/stats")
    def stats_page(request: Request, collection_id: str = ""):
        selected = _int(collection_id)
        with db() as conn:
            collections = store.list_collections(conn)
            chosen = [c for c in collections if selected in (None, c.id)]
            cards, progress, analyses, records = [], {}, {}, []
            for c in chosen:
                snap = snapshot(conn, c)
                cards += snap.cards
                progress |= snap.progress
                analyses |= snap.order.analyses
                records += store.list_attempt_records(conn, c.id)
            every_card = {card.id: card for c in chosen for card in store.list_cards(conn, c.id, include_archived=True)}
        days = stats.daily(records)
        return page(
            request, "stats.html", collections=collections, selected=selected,
            totals=stats.totals(records), days=days, max_day=max((d.attempts for d in days), default=0),
            grader=stats.grader_agreement(records, every_card),
            weak_concepts=stats.weakest_concepts(cards, analyses, progress),
            weak_cards=stats.weakest_cards(cards, progress), progress=progress,
        )

    # --- combine and re-tier collections (DESIGN.md §12.4) ---

    def organize_page(request, ids: list[int], name: str, group: str, mapping, warnings=(), errors=(), result=None):
        with db() as conn:
            collections = store.list_collections(conn)
            rows = organize.rows_for(conn, ids)
        ordered = sorted(rows, key=lambda r: (mapping[r.key].tier, rows.index(r))) if mapping else rows
        return page(request, "organize.html", collections=collections, ids=ids, rows=ordered, target_name=name, group=group,
                    mapping=mapping, warnings=list(warnings), errors=list(errors), result=result,
                    tier_options=range(1, 21))

    def chosen_ids(values) -> list[int]:
        return list(dict.fromkeys(i for v in values if (i := _int(v)) is not None))

    def mapping_from_form(form, rows: list[organize.Row], defaults: dict[str, organize.Mapping]) -> dict[str, organize.Mapping]:
        return {
            r.key: organize.Mapping(max(1, _int(form.get(f"tier_{r.key}")) or defaults[r.key].tier),
                                    (form.get(f"label_{r.key}") if form.get(f"label_{r.key}") is not None else defaults[r.key].label).strip())
            for r in rows
        }

    @app.get("/collections/organize")
    def organize_start(request: Request):
        ids = chosen_ids(request.query_params.getlist("ids"))
        if not ids:
            return organize_page(request, [], "", "", {})
        with db() as conn:
            chosen = [c for i in ids if (c := store.get_collection(conn, i))]
            rows = organize.rows_for(conn, ids)
        if not rows:
            return organize_page(request, [], "", "", {}, errors=["Those collections have no cards."])
        name = organize.suggested_name(chosen)
        group = chosen[0].group if len({c.group for c in chosen}) == 1 else ""
        return organize_page(request, ids, name, group, organize.default_mapping(rows, name))

    @app.post("/collections/organize/preview")
    async def organize_preview(request: Request):
        """Re-show with your edits; with suggest=1, ask the AI for a study order first."""
        form = await request.form()
        ids = chosen_ids(form.getlist("ids"))
        name, group = (form.get("name") or "").strip(), (form.get("group") or "").strip()
        with db() as conn:
            rows = organize.rows_for(conn, ids)
        mapping = mapping_from_form(form, rows, organize.default_mapping(rows, name))
        warnings = []
        if form.get("suggest"):
            if provider is None:
                warnings.append("Suggesting an order needs AI, and none is set up.")
            else:
                try:
                    labels = await run_in_threadpool(
                        tutor.order_subdecks, provider, name or "Combined collection", [(r.label, r.prompts) for r in rows])
                    by_label = {r.label: r.key for r in rows}
                    suggested = organize.default_mapping(rows, name, [by_label[label] for label in labels])
                    mapping = {k: organize.Mapping(suggested[k].tier, mapping[k].label) for k in mapping}
                except LLMError as exc:
                    warnings.append(f"Couldn't suggest an order ({exc}).")
        return organize_page(request, ids, name, group, mapping, warnings)

    @app.post("/collections/organize/apply")
    async def organize_apply(request: Request):
        form = await request.form()
        ids = chosen_ids(form.getlist("ids"))
        name, group = (form.get("name") or "").strip(), (form.get("group") or "").strip()
        with db() as conn:
            rows = organize.rows_for(conn, ids)
            mapping = mapping_from_form(form, rows, organize.default_mapping(rows, name))
            try:
                result = organize.apply(conn, rows, mapping, name, group)
            except organize.OrganizeError as exc:
                return organize_page(request, ids, name, group, mapping, errors=[str(exc)])
            target = store.get_collection(conn, result.collection_id)
        return organize_page(request, [], "", "", {}, result=(result, target))

    # --- AI re-tiering of one collection (DESIGN.md §12.6) ---

    def retier_page(request, conn, collection: Collection, labels: list[str], assignment: dict[int, int], errors=()):
        cards = store.list_cards(conn, collection.id)
        attempted = {a.card_id for a in store.list_attempts(conn, collection.id)}
        return page(request, "retier.html", collection=collection, cards=cards, labels=labels, assignment=assignment,
                    attempted=attempted, errors=list(errors))

    @app.post("/collections/{collection_id:int}/retier")
    def retier_suggest(request: Request, collection_id: int):  # plain `def`: one slow AI call
        with db() as conn:
            collection = load_collection(conn, collection_id)
            cards = store.list_cards(conn, collection_id)
            if provider is None or not cards:
                reason = "Sorting into tiers needs AI, and none is set up." if provider is None else "This collection has no cards."
                return redirect(f"/collections/{collection_id}?tiers={quote(reason)}#tiers")
            try:
                suggestion = tutor.suggest_tiers(provider, collection.name, cards)
            except LLMError as exc:
                return redirect(f"/collections/{collection_id}?tiers={quote(f'Couldn’t suggest tiers ({exc}).')}#tiers")
            return retier_page(request, conn, collection, suggestion.labels, suggestion.tiers)

    @app.post("/collections/{collection_id:int}/retier/apply")
    async def retier_apply(request: Request, collection_id: int):
        form = await request.form()
        labels, n = [], 1
        while f"label_{n}" in form:
            labels.append(str(form[f"label_{n}"]).strip())
            n += 1
        with db() as conn:
            collection = load_collection(conn, collection_id)
            cards = store.list_cards(conn, collection_id)
            assignment = {c.id: _int(form.get(f"tier_{c.id}")) or 0 for c in cards}
            try:
                changed = tiers.retier(conn, collection_id, labels, assignment)
            except tiers.TierError as exc:
                return retier_page(request, conn, collection, labels, assignment, [str(exc)])
            count = tiers.count(conn, collection_id)
        message = f"Sorted into {count} tier{'' if count == 1 else 's'}; {changed} card{'' if changed == 1 else 's'} changed tier."
        return redirect(f"/collections/{collection_id}?tiers={quote(message)}#tiers")

    # --- card order (sequencing) ---

    @app.get("/collections/{collection_id:int}/order")
    def order_page(request: Request, collection_id: int):
        with db() as conn:
            snap = snapshot(conn, load_collection(conn, collection_id))
        by_id = {c.id: c for c in snap.cards}
        q = request.query_params
        return page(
            request, "order.html", snap=snap, by_id=by_id,
            rebuilt=q.get("rebuilt"), errors=[e for e in q.getlist("error")],
        )

    def rebuild_order(conn, collection: Collection) -> list[str]:
        """Analyze every tier, lowest first, so each tier sees the concepts taught below it.
        Returns problems to show; tiers that fail keep their previous analysis."""
        errors = []
        for tier in sorted({c.tier for c in store.list_cards(conn, collection.id)}):
            cards = [c for c in store.list_cards(conn, collection.id) if c.tier == tier]
            analyses = store.list_analyses(conn, collection.id)
            all_cards = {c.id: c for c in store.list_cards(conn, collection.id)}
            lower = sequencer.concept_list(
                [n for cid, a in analyses.items() if cid in all_cards and all_cards[cid].tier < tier for n in a.introduced]
            )
            existing = sequencer.concept_list([n for a in analyses.values() for n in a.introduced + a.required])
            fixed = {c.id: analyses[c.id] for c in cards if c.id in analyses and analyses[c.id].manual}
            try:
                result = tutor.analyze_tier(provider, collection.name, tier, cards, fixed, lower, existing)
            except LLMError as exc:
                errors.append(f"{collection.tier_label(tier)}: {exc}")
                continue
            for card_id, concepts in result.items():
                store.save_analysis(conn, card_id, concepts.introduced, concepts.required, concepts.difficulty)
            skipped = len(cards) - len(fixed) - len(result)
            if skipped:
                errors.append(f"{collection.tier_label(tier)}: the AI skipped {skipped} card(s); they stay at the end.")
        return errors

    @app.post("/collections/{collection_id:int}/order/rebuild")
    def order_rebuild(collection_id: int):  # plain `def`: one slow AI call per tier
        if provider is None:
            return redirect(f"/collections/{collection_id}/order?error=" + quote("Ordering needs AI, and none is set up. See the note above."))
        with db() as conn:
            errors = rebuild_order(conn, load_collection(conn, collection_id))
        query = "&".join(f"error={quote(e)}" for e in errors)
        return redirect(f"/collections/{collection_id}/order?rebuilt=1" + (f"&{query}" if query else ""))

    # --- import ---

    # Anki decks are read from the imports/ folder next to the database (uploads are saved there too).
    imports_dir = db_path.resolve().parent / "imports"

    def anki_files() -> list[str]:
        return sorted(p.name for p in imports_dir.glob("*.apkg") if p.is_file()) if imports_dir.is_dir() else []

    def anki_path(name: str) -> Path:
        """Only plain .apkg file names inside imports/ (never a path someone typed into the URL)."""
        if not name or name != Path(name).name or not name.lower().endswith(".apkg"):
            raise HTTPException(404, "Deck not found")
        path = imports_dir / name
        if not path.is_file():
            raise HTTPException(404, "Deck not found")
        return path

    @app.post("/reset")
    async def reset_everything(request: Request):
        """Back to the two starter collections. A backup of the current database is saved first."""
        form = await request.form()
        if str(form.get("confirm", "")).strip().lower() != "reset":
            return page(request, "import.html", result=None, text="", anki_files=anki_files(),
                        reset_error="Type reset in the box to confirm.")
        with db() as conn:
            saved = store.backup(conn, db_path.resolve().parent / "backups")
            store.wipe(conn)
            seed_starter(conn)
            tiers.tidy_all(conn)
        return redirect(f"/?reset={quote(saved.name)}")

    @app.get("/import")
    def import_form(request: Request):
        return page(request, "import.html", result=None, text="", anki_files=anki_files())

    @app.post("/import")
    async def import_run(request: Request):
        form = await request.form()
        upload = form.get("file")
        text = form.get("text", "")
        filename = getattr(upload, "filename", "") or ""
        if filename.lower().endswith(".apkg"):  # an Anki deck: keep it in imports/ and show the preview
            safe = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(filename).name)[:120]
            imports_dir.mkdir(exist_ok=True)
            (imports_dir / safe).write_bytes(await upload.read())
            return redirect(f"/import/anki?file={quote(safe)}")
        if upload is not None and filename:
            text = (await upload.read()).decode("utf-8", errors="replace")
        result = store.ImportResult()
        if not text.strip():
            result.errors = ["Choose a JSON file or paste its contents."]
        else:
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                result.errors = [f"Not valid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno})"]
            else:
                with db() as conn:
                    result = store.import_collection(conn, data)
                    if result.collection_id:
                        tiers.tidy(conn, result.collection_id)
        return page(request, "import.html", result=result, text=text if result.errors else "", anki_files=anki_files())

    def read_deck(name: str) -> anki.AnkiDeck:
        return anki.read_apkg(anki_path(name).read_bytes())

    def choices_from_form(form, parents: list[anki.ParentDeck], defaults: list[anki.DeckChoice]) -> list[anki.DeckChoice]:
        """The layout you chose on the preview (fields mode_i, collection_i, group_i, tier_i_j)."""
        choices = []
        for i, (parent, default) in enumerate(zip(parents, defaults)):
            mode = form.get(f"mode_{i}") if form.get(f"mode_{i}") in ("tiers", "collections") else default.mode
            tiers = {sub.label: max(1, _int(form.get(f"tier_{i}_{j}")) or default.tiers[sub.label])
                     for j, sub in enumerate(parent.subdecks)}
            choices.append(anki.DeckChoice(mode, (form.get(f"collection_{i}") or default.collection).strip(),
                                           (form.get(f"group_{i}") or "").strip(), tiers))
        return choices

    def anki_page(request, file, deck, choices, warnings=(), errors=(), done=False, plans=None):
        if plans is None:
            with db() as conn:
                plans = anki.plan_import(conn, deck, choices)
        parents = anki.parent_decks(deck)
        # Show each deck's subdecks in tier order; `j` keeps the form field tied to the subdeck.
        rows = [sorted(enumerate(p.subdecks), key=lambda js: (c.tiers[js[1].label], js[0])) for p, c in zip(parents, choices)]
        return page(request, "anki.html", file=file, deck=deck, parents=list(zip(parents, choices, rows)), plans=plans,
                    warnings=list(warnings), errors=list(errors), done=done, tier_options=range(1, 13), parent_itself=anki.PARENT_ITSELF)

    def anki_error(request, file, exc):
        return page(request, "anki.html", file=file, deck=None, parents=[], plans=[], warnings=[], errors=[str(exc)], done=False)

    @app.get("/import/anki")
    def anki_preview(request: Request, file: str = ""):  # plain `def`: reading a deck can take a moment
        try:
            deck = read_deck(file)
        except anki.AnkiError as exc:
            return anki_error(request, file, exc)
        with db() as conn:
            choices = anki.default_choices(conn, deck)
        return anki_page(request, file, deck, choices, deck.warnings)

    @app.post("/import/anki/preview")
    async def anki_repreview(request: Request, file: str = ""):
        """Re-plan with your edits; with suggest=1, ask the AI for a study order first."""
        form = await request.form()
        try:
            deck = read_deck(file)
        except anki.AnkiError as exc:
            return anki_error(request, file, exc)
        parents = anki.parent_decks(deck)
        with db() as conn:
            defaults = anki.default_choices(conn, deck)
        choices = choices_from_form(form, parents, defaults)
        warnings = list(deck.warnings)
        if form.get("suggest"):
            if provider is None:
                warnings.append("Suggesting an order needs AI, and none is set up.")
            else:
                for parent, choice in zip(parents, choices):
                    if len(parent.subdecks) < 2:
                        continue
                    try:
                        order = await run_in_threadpool(tutor.order_subdecks, provider, parent.name,
                                                        [(s.label, s.prompts) for s in parent.subdecks])
                    except LLMError as exc:
                        warnings.append(f"{parent.name}: couldn't suggest an order ({exc})")
                        continue
                    choice.tiers = {label: i for i, label in enumerate(order, start=1)}
                    choice.mode = "tiers"
        return anki_page(request, file, deck, choices, warnings)

    @app.post("/import/anki")
    async def anki_import(request: Request, file: str = ""):
        form = await request.form()
        try:
            deck = read_deck(file)
        except anki.AnkiError as exc:
            return anki_error(request, file, exc)
        parents = anki.parent_decks(deck)
        with db() as conn:
            choices = choices_from_form(form, parents, anki.default_choices(conn, deck))
            results, warnings = anki.apply_import(conn, deck, choices, max_image_bytes)
        return anki_page(request, file, deck, choices, warnings, done=True, plans=results)

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Learner on 127.0.0.1.")
    parser.add_argument("--config", help="path to learner.toml (default: ./learner.toml if present)")
    parser.add_argument("--port", type=int, help="override the configured port")
    parser.add_argument("--no-browser", action="store_true", help="don't open a browser tab")
    args = parser.parse_args()

    env_file = Path(args.config).parent / ".env" if args.config else Path(".env")
    config_module.load_env_file(env_file)
    cfg = config_module.load(args.config)
    port = args.port or cfg.port
    url = f"http://127.0.0.1:{port}"
    provider = make_provider(cfg)
    app = create_app(cfg, provider)
    print(f"Learner running at {url}  (database: {Path(cfg.db_path).expanduser().resolve()})")
    if provider is not None:
        print(f"AI: on ({provider.name})")
    elif cfg.provider == "off":
        print('AI: off (provider = "off" in learner.toml)')
    else:
        print(f"AI: off. No API key found. Add ANTHROPIC_API_KEY=... to {env_file.resolve()} and restart.")
    if cfg.open_browser and not args.no_browser:
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
