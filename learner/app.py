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
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
from pydantic import BaseModel, ValidationError

from . import config as config_module
from . import checkers, engine, sequencer, stats, store, tutor
from .config import Config
from .llm import LLMError, LLMProvider, make_provider
from .models import ANSWER_FORMATS, SQL_DIALECTS, Card, CardIn, Collection, CollectionIn, describe_errors

PACKAGE = Path(__file__).parent
LOCAL_HOSTS = ("127.0.0.1", "localhost")
NEW_COLLECTION = "new"  # the card form's "+ New collection…" option
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


def parse_tier_labels(text: str) -> dict[int, str]:
    """One per line: '1: Foundations'."""
    labels = {}
    for line in _lines(text):
        match = re.fullmatch(r"(\d+)\s*[:=]\s*(.+)", line)
        if not match:
            raise ValueError(f"Tier labels: couldn't read “{line}”. Use one per line, like “1: Foundations”.")
        labels[int(match[1])] = match[2].strip()
    return labels


def create_app(cfg: Config, provider: LLMProvider | None = None) -> FastAPI:
    """`provider` defaults to the one configured in `cfg` (None when LLM features are off)."""
    if provider is None:
        provider = make_provider(cfg)
    db_path = Path(cfg.db_path).expanduser()
    with closing(store.connect(db_path)) as conn:
        store.migrate(conn)
        if not store.list_collections(conn, include_archived=True):
            for path in sorted((PACKAGE / "starter").glob("*.json")):
                store.import_collection(conn, json.loads(path.read_text()))

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
        unlocked = engine.unlocked_through(cards, progress, collection.max_unlocked_tier, cfg.unlock_pct)
        if unlocked > collection.max_unlocked_tier:
            store.raise_unlocked_tier(conn, collection.id, unlocked)
        order = sequencer.build(cards, store.list_analyses(conn, collection.id))
        return Snapshot(collection, cards, attempts, progress, engine.tier_summaries(cards, progress, unlocked), order)

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
    def dashboard(request: Request):
        with db() as conn:
            snapshots = [snapshot(conn, c) for c in store.list_collections(conn)]
            archived = [c for c in store.list_collections(conn, include_archived=True) if c.archived]
        items = [(s, engine.default_tier(s.tiers)) for s in snapshots]
        return page(request, "dashboard.html", items=items, archived=archived)

    # --- study ---

    @app.get("/study/{collection_id}/{tier}")
    def study_start(request: Request, collection_id: int, tier: int, confirm: bool = False):
        with db() as conn:
            snap = snapshot(conn, load_collection(conn, collection_id))
        summary = snap.tier(tier)
        if summary is None:
            raise HTTPException(404, "That tier has no cards")
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
        if result not in ("right", "wrong"):
            raise HTTPException(400, "Bad answer")
        right = result == "right"
        hints_used = min(max(_int(form.get("max_hint_level")) or 0, 0), tutor.MAX_HINT_LEVEL)
        llm_verdict = form.get("llm_verdict") if form.get("llm_verdict") in tutor.VERDICTS else None
        with db() as conn:
            card = load_card(conn, _int(form.get("card_id")) or 0)
            if card.collection_id != collection_id:
                raise HTTPException(400, "Card is not in this collection")
            snap = snapshot(conn, load_collection(conn, collection_id))
            if engine.session_state(snap.cards, snap.attempts, session_id, tier, cfg, snap.order).next_card_id != card.id:
                # Not the card this session is showing: a repeat submit. Don't record it twice.
                return redirect(f"/study/{collection_id}/{tier}/{session_id}")
            store.add_attempt(
                conn, card.id, session_id, engine.outcome_for(right, hints_used),
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

    def card_and_dialect(card_id: int) -> tuple[Card, str]:
        with db() as conn:
            card = load_card(conn, card_id)
            return card, load_collection(conn, card.collection_id).sql_dialect

    @app.post("/grade")
    def grade_answer(body: TutorRequest):
        card, dialect = card_and_dialect(body.card_id)
        if provider is None:
            return {"error": "No LLM is configured."}
        if not body.answer.strip():
            return {"error": "Nothing to grade."}
        try:
            grade = tutor.grade(provider, card, body.answer, dialect)
        except LLMError as exc:
            return {"error": str(exc)}
        return {
            "verdict": grade.verdict, "right": grade.right, "feedback": grade.feedback,
            "rubric_hits": grade.rubric_hits, "rubric_misses": grade.rubric_misses,
            "equivalent_approach": grade.equivalent_approach,
        }

    @app.post("/hint")
    def give_hint(body: TutorRequest):
        card, dialect = card_and_dialect(body.card_id)
        if provider is None:
            return {"error": "No LLM is configured."}
        earlier = body.earlier[: tutor.MAX_HINT_LEVEL]
        level = body.level or len(earlier) + 1
        try:
            return {"hint": tutor.hint(provider, card, body.answer, level, earlier, dialect), "level": level}
        except LLMError as exc:
            return {"error": str(exc)}

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
        return page(
            request, "browse.html", rows=rows, collections=collections, tags=sorted(tags),
            filters={"collection_id": collection_id, "tier": tier, "status": status, "tag": tag, "archived": show_archived},
        )

    def card_page(request, conn, form, errors=(), card=None):
        progress = None
        if card is not None:
            snap = snapshot(conn, load_collection(conn, card.collection_id))
            progress = snap.progress.get(card.id)
        return page(
            request, "card_form.html", form=form, errors=list(errors), card=card, progress=progress,
            collections=store.list_collections(conn),
        )

    @app.get("/cards/new")
    def card_new(request: Request, collection_id: str = ""):
        with db() as conn:
            return card_page(request, conn, {"collection_id": collection_id, "tier": "1", "answer_format": "text"})

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
            try:
                data = card_from_form(form)
            except ValidationError as exc:
                return card_page(request, conn, form, describe_errors(exc), card)
            if form.get("collection_id") == NEW_COLLECTION:
                name = (form.get("new_collection") or "").strip()
                if not name:
                    return card_page(request, conn, form, ["Give the new collection a name."], card)
                if store.find_collection(conn, name):
                    return card_page(request, conn, form, [f"“{name}” already exists: choose it from the list."], card)
                form["collection_id"] = str(store.save_collection(conn, name, "mysql", {}))
            collection_id = _int(form.get("collection_id"))
            if collection_id is None or store.get_collection(conn, collection_id) is None:
                return card_page(request, conn, form, ["Choose a collection."], card)
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
        return redirect(f"/cards/{card_id}")

    @app.post("/cards/{card_id:int}/reset")
    def card_reset(card_id: int):
        with db() as conn:
            load_card(conn, card_id)
            store.reset_card_progress(conn, card_id)
        return redirect(f"/cards/{card_id}?reset=1")

    # --- collections ---

    def collection_page(request, form, errors=(), collection=None):
        return page(request, "collection_form.html", form=form, errors=list(errors), collection=collection)

    @app.get("/collections/new")
    def collection_new(request: Request):
        return collection_page(request, {"sql_dialect": "mysql", "tier_labels": "1: Foundations\n2: Intermediate\n3: Advanced"})

    @app.get("/collections/{collection_id}")
    def collection_edit(request: Request, collection_id: int):
        with db() as conn:
            c = load_collection(conn, collection_id)
        labels = "\n".join(f"{k}: {v}" for k, v in sorted(c.tier_labels.items()))
        return collection_page(request, {"name": c.name, "sql_dialect": c.sql_dialect, "tier_labels": labels}, collection=c)

    async def save_collection_form(request: Request, collection_id: int | None):
        form = dict(await request.form())
        with db() as conn:
            collection = load_collection(conn, collection_id) if collection_id else None
            try:
                labels = parse_tier_labels(form.get("tier_labels", ""))
                data = CollectionIn(collection=form.get("name", ""), sql_dialect=form.get("sql_dialect", "mysql"), tier_labels=labels)
                saved_id = store.save_collection(conn, data.collection, data.sql_dialect, data.tier_labels, collection_id)
            except ValueError as exc:  # includes pydantic's ValidationError
                errors = describe_errors(exc) if isinstance(exc, ValidationError) else [str(exc)]
                return collection_page(request, form, errors, collection)
            except sqlite3.IntegrityError:
                return collection_page(request, form, ["A collection with that name already exists."], collection)
        return redirect(f"/collections/{saved_id}?saved=1")

    @app.post("/collections/new")
    async def collection_create(request: Request):
        return await save_collection_form(request, None)

    @app.post("/collections/{collection_id}")
    async def collection_update(request: Request, collection_id: int):
        return await save_collection_form(request, collection_id)

    @app.post("/collections/{collection_id}/archive")
    def collection_archive(collection_id: int):
        with db() as conn:
            c = load_collection(conn, collection_id)
            store.set_collection_archived(conn, collection_id, not c.archived)
        return redirect(f"/collections/{collection_id}")

    @app.get("/collections/{collection_id}/export")
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

    # --- card order (sequencing) ---

    @app.get("/collections/{collection_id}/order")
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

    @app.post("/collections/{collection_id}/order/rebuild")
    def order_rebuild(collection_id: int):  # plain `def`: one slow AI call per tier
        if provider is None:
            return redirect(f"/collections/{collection_id}/order?error=" + quote("Ordering needs AI, and none is set up. See the note above."))
        with db() as conn:
            errors = rebuild_order(conn, load_collection(conn, collection_id))
        query = "&".join(f"error={quote(e)}" for e in errors)
        return redirect(f"/collections/{collection_id}/order?rebuilt=1" + (f"&{query}" if query else ""))

    # --- import ---

    @app.get("/import")
    def import_form(request: Request):
        return page(request, "import.html", result=None, text="")

    @app.post("/import")
    async def import_run(request: Request):
        form = await request.form()
        upload = form.get("file")
        text = form.get("text", "")
        if upload is not None and getattr(upload, "filename", ""):
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
        return page(request, "import.html", result=result, text=text if result.errors else "")

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
