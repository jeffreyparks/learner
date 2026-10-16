# Learner — Design Outline

A single-user, self-hosted study app inspired by Anki, built for **fast technical review**. It handles free-text answers **and** code answers (Python, SQL, …), and uses an LLM to sequence material, give hints, and grade responses.

> Status: Phases 0–4 built (adaptive prerequisite refresh is still on the To-Do list). The reasoning behind each decision is in [DECISIONS.md](DECISIONS.md); later ideas are in §13.

---

## 1. Goals & Non-Goals

**Goals**
- One user, one machine. Start with a single command, open in the browser.
- Useful from the first run: a starter collection is loaded automatically.
- Content is easy to create and edit, in the app or by bulk JSON import.
- Two answer modes: **free text** and **code** (syntax highlighting + syntax checking, feels like an editor).
- One study flow for every card: write an answer, submit, and the LLM grades it (or you mark yourself when no LLM is configured).
- Organize by **Collection → Tier → Card**, with each card linking out to the right documentation.
- Cards don't need a hand-maintained order. The LLM works out the learning sequence.
- Built for **fast review sessions**. Progress is measured by correct answers across sessions, not calendar days.
- **Every LLM feature is optional.** Without an API key the app is still a working flashcard tool.

**Non-Goals (for now)**
- Multi-user, accounts, auth, sync, cloud hosting.
- Mobile app.
- **Running code.** Code answers are checked for syntax only (see §8 and §13).
- Multi-part cards. There's one prompt per card.

---

## 2. Design Principles

1. **One process, one database file, one language.** Python end to end, with no Node toolchain and no services to babysit.
2. **Small modules with narrow interfaces** (see §9). Each one can be tested and replaced on its own.
3. **The study engine is pure logic.** Given cards and attempts, it calculates statuses and the next queue. No I/O, no LLM, no cached state. That makes it easy to test.
4. **The attempt log is the only record of progress.** Status, streaks, and the refresher order are all calculated from it, so nothing can drift out of step. The one deliberate exception is `max_unlocked_tier` (§5.6), which is stored because working out "was this tier ever unlocked" by replaying history isn't worth it.
5. **The LLM advises; the user decides.** LLM output (grades, concept tags, order) can always be overridden, and an override is never overwritten by the LLM.
6. **One config file.** Every setting lives in `learner.toml`, with defaults in `config.py`. The API key comes from the standard `ANTHROPIC_API_KEY` environment variable (or `ANTHROPIC_AUTH_TOKEN`).
7. **Prompts are text files, not code.** Tuning the LLM means editing a file in `prompts/`.
8. **Add answer formats by registration, not by branching.** A new language is one entry in a table (§8).

---

## 3. Core Concepts & Hierarchy

```
Collection        e.g. "Python", "SQL", "Machine Learning"
 └─ Tier          1, 2, 3 … (numeric), optional label per collection, e.g. 1 = "Foundations"
     └─ Card      one prompt with a reference answer
```

- **Tiers** are numeric with optional display labels. Numbers keep the logic simple, and labels keep it readable.
- **Archiving** a collection hides it from the dashboard and keeps its history, the same as archiving a card. There's no hard delete from the UI.
- **Tier gating is the only lock in the app, and it's soft.** If you start a tier before it's unlocked, the app warns you and lets you continue.
- **Within a tier**, there's no manual ordering. The concept graph (§6) decides which new card comes next.

---

## 4. Card Model

| Field | Notes |
|---|---|
| `id` | Integer primary key. |
| `collection_id`, `tier` | Placement. A card can be moved freely. |
| `answer_format` | `text` \| `python` \| `sql` (one field drives the editor, the checker, and grading; see §8) |
| `prompt` | Markdown. Can include code blocks and tables. |
| `reference_answer` | The canonical answer. |
| `rubric` *(optional)* | Key points a correct answer must hit. Gives the grader something concrete to check. |
| `references` *(optional)* | A list of `{title, url}` links to outside sources, such as the docs page for `pandas.merge` or the MySQL manual page on `LEFT JOIN`. Usually one or two. |
| `tags` *(optional)* | Free-form. |
| `archived` | Archiving a card removes it from study but keeps its history. There's no hard delete from the UI. |
| `progress_reset_at` | Set by "Reset progress." Attempts before this time are ignored when calculating progress. History is kept. |

**References** appear on the answer side of the card, alongside the reference answer and the verdict. They open in a new tab. You edit them like any other field, in the card form or in JSON.

**Storage: everything in one SQLite file** (`learner.db`).
- Cards are created and edited in the app's editor, or **bulk-imported from JSON** (§11).
- Backup means copying one file, or exporting to JSON.
- **Editing a card keeps its progress.** If you change a card enough that the old progress no longer applies, use "Reset progress."

---

## 5. Study Flow & Progress (the "when")

Built for quick sessions, so **nothing depends on elapsed days**. Progress comes from how you answer, across separate sessions.

### 5.1 One study flow
Every card, whatever its format, goes the same way:

1. Read the prompt and **write your answer**: a text box, or the code editor for code cards. Writing it out beats reciting it to yourself. Optional hints are available (§7.1).
2. **Submit** (`Ctrl+Enter`). The reference answer and references appear immediately.
3. **With an LLM configured**, its verdict and feedback follow a few seconds later. Accept it (`Enter`) or override it (`1` right / `2` wrong).
4. **Otherwise you mark yourself** (`1` / `2`). That's the case when there's no API key, when the LLM call fails, or when you submit with the box empty (nothing to grade, which gives you a fast "just show me" path).

### 5.2 Attempt outcomes
Every attempt ends with exactly one outcome, whoever grades it:

| Outcome | When | Effect on streak |
|---|---|---|
| `correct` | Right, no hints used | +1 (see §5.4) |
| `assisted` | Right, but used **any** hint | unchanged |
| `incorrect` | Wrong or partially right | reset to 0 |

### 5.3 Card status
Calculated from the card's attempts since its last `progress_reset_at`:

| Status | Meaning |
|---|---|
| `new` | No attempts. |
| `learning` | Attempted, streak below the `known` threshold. |
| `known` | Streak ≥ `known_streak` (default **2**). |

A wrong answer on a `known` card resets the streak, so the card drops back to `learning`.

### 5.4 One step forward per session
**Only a card's first attempt in a session can raise its streak.** Later attempts in the same session (misses that come back, refreshers) can still reset the streak if wrong, but they never raise it.

So "2 in a row" means right in **two separate sessions**. There's no calendar, but a card has to survive a break before it's `known`. Short-term memory alone doesn't count.

### 5.5 Session
A session covers **one collection and one tier**. By default that's the highest unlocked tier, and you can pick another one.

Queue, in priority order, up to `session_size` (default **20**):
1. **Misses from this session.** They come back about 3 cards later, for practice only (§5.4).
2. **`learning` cards**, weakest first, judged by accuracy over their last 5 attempts.
3. **`new` cards** in concept-graph order (§6), up to `new_per_session` (default **5**).
4. **Refresher:** `known` cards from this tier and lower ones, **least recently seen first**, up to `refresher_pct` of the session (default **10%**). This is the only resurfacing mechanism; nothing is time-based.

### 5.6 Tier unlock & completion
- **Unlock:** Tier N+1 unlocks when at least `unlock_pct` (default **80%**) of Tier N's active cards are `known`.
- **Sticky:** once a tier is unlocked, it stays unlocked, even if later misses drop the lower tier below the threshold.
- **Soft:** before it unlocks, you can still start it after a warning.
- **Tier complete:** when every active card in a tier is `known`, the dashboard and end-of-session screen say so and suggest the next tier. A session on a completed tier is a short refresher-only pass.

---

## 6. LLM-Driven Sequencing (the "what next")

**Problem:** cards build on each other. Hand-maintaining an order is brittle, especially when inserting a card between two existing ones.

**Approach: a concept dependency graph, not a sequence number.**

1. **One batch call per tier.** When you click "Rebuild order" (offered right after an import, and on the Card order page), the LLM sees every card in the tier at once, plus the concepts already introduced in lower tiers. For each card it returns:
   - `concepts_introduced`: what this card teaches (e.g. `list-comprehension`)
   - `concepts_required`: what you need to know first (e.g. `for-loop`, `list-literal`)
   - `difficulty`: a rough 1–5 score, used only to break ties.

   Seeing everything together keeps concept names consistent. Even a 500-card tier fits comfortably in one call.
2. **Out-of-date detection.** A card is *unsequenced* if it has no analysis or was edited after its last analysis. Unsequenced cards go to the end of the order, and the tier shows a banner: "Order out of date. Rebuild?" Nothing calls the LLM in the background.
3. **Graph.** Concepts are shared across the whole collection, but ordering is applied **within a tier**. Within a tier, a card that introduces a concept comes before the cards that require it. Topologically sort it, breaking ties by difficulty, then by creation date.
4. **Insertion is free.** Add a card, rebuild, and it slots in where its prerequisites are. Nothing gets renumbered.
5. **"Ready" new cards.** A new card is *ready* when each required concept is either introduced in a lower tier, or introduced by **at least one** `known` card in this tier. The queue prefers ready cards in graph order. If none are ready, it takes the next card in graph order anyway. This is a preference, not a lock.
6. **Manual override.** The card form has "Teaches" and "Needs first" fields. Typing in them marks the analysis as `manual`; a rebuild shows those concepts to the LLM as fixed input and never overwrites them. Unticking "Keep my concepts" hands the card back to the LLM at the next rebuild.
7. **Warnings, shown in the sequence list:**
   - **Cycle:** broken automatically by difficulty and flagged.
   - **Missing concept:** required but never introduced, e.g. "3 cards require `window-functions` but nothing teaches it."
   - **Tier mismatch:** a card requires a concept that's only introduced in a *higher* tier, so it's probably in the wrong tier.

**Before Phase 3** (or with no API key), new cards come in creation order.

---

## 7. LLM Tutor: Hints & Grading

### 7.1 Hint ladder (graduated, no spoilers)
| Level | What it gives |
|---|---|
| 1. Nudge | Points to the right area: "Think about what happens to rows with NULL in a JOIN." |
| 2. Concept | Names the tool or concept: "Look at `LEFT JOIN` + `COALESCE`." |
| 3. Partial | A skeleton or the first step, with blanks. |

- **Any hint makes a right answer `assisted`.** After level 3 there are no more hints; to give up, submit and mark it wrong.
- Hints are always generated by the LLM, using the fast model. There are no hint fields on the card to write or maintain. With no LLM configured, there are no hints.
- Hints are **aware of your current attempt**, e.g. "your WHERE clause is filtering out the rows you need," and of the hints already given, so they don't repeat.

### 7.2 Grading (every card, when an LLM is configured)
- **Input:** prompt, reference answer, rubric, your answer, and (for code) syntax diagnostics.
- **Output (structured JSON):**
  ```json
  {
    "verdict": "correct | partially_correct | incorrect",
    "rubric_hits": ["..."], "rubric_misses": ["..."],
    "equivalent_approach": true,
    "feedback": "Short explanation of what was right/wrong."
  }
  ```
- `correct` maps to `correct` or `assisted`, depending on whether you used a hint. `partially_correct` and `incorrect` map to `incorrect`, and the feedback explains the gap.
- **Free text:** graded on meaning against the rubric, not wording.
- **Code:** graded by reading it, not running it. The grader judges whether the code would do the job. An alternative approach with the same effect (e.g. a CTE instead of a subquery, or `dict.get` instead of `try/except`) is marked correct, with an optional note on style. A syntax error is reported but doesn't automatically fail the answer; the grader decides.
- **Speed:** on submit, the reference answer appears immediately and the verdict follows a few seconds later. You can mark yourself (`1`/`2`) without waiting.
- **Blank answers aren't sent to the LLM.** There's nothing to grade, so you mark yourself.
- You can always **override the verdict**. Overrides are logged.

### 7.3 Prompts
Each LLM task has one prompt file in `prompts/`: `hint.md`, `grade.md`, `sequence.md`. They're plain text with a few placeholders filled in at call time. Changing how the tutor behaves means editing a file, not the code.

### 7.4 Grader test set
Every override is a real example of the grader getting it wrong. `uv run learner-eval export` adds them to a test set (`evals/grader_cases.jsonl`), together with an equal number of recent answers where you agreed with the grader, so a change that fixes mistakes but breaks good verdicts shows up too. Cases already in the file are kept as they are.

`uv run learner-eval run` re-grades every case with the current `grade.md` and model and reports agreement with your calls: overall, on past overrides, on agreed answers, and per format. `--model` tries a different grading model, which is how the per-format model question (§7.5) gets answered with evidence. It asks before making API calls.

### 7.5 Provider, models & cost
- **Start with Claude.** All LLM calls go through one small `LLMProvider` interface, so another provider can be plugged in later with its own API key.
- The model is configurable per task in `learner.toml`:
  - Hints and grading → the fast model (Claude Haiku 5.5), at low effort. These run on every card, so speed matters most.
  - Sequencing → the stronger model (Claude Sonnet 5.5). It runs rarely, as a batch.
- **Model tiers are evidence-driven.** If the grader test set (§7.4) shows the fast model struggling on, say, code cards, the next step is a per-format grading model (e.g. Sonnet for SQL and Python). That's a small config addition, made only when the overrides justify it.
- Grading and hints use structured output, so responses always parse.
- **Cost:** grading a 20-card session is roughly 30k tokens, well under a cent on Haiku. Cost doesn't drive any design choices.

---

## 8. Answer Formats & Code Checking

Each answer format is one entry in a small registry. Adding a language means adding an entry and, optionally, a checker.

| Format | Editor | Checker | Notes |
|---|---|---|---|
| `text` | Plain textarea | none | |
| `python` | CodeMirror 6, Python mode | `ruff`, limited to syntax errors + a few pyflakes rules | Undefined-name, unused-import, unused-variable and redefinition warnings are **off**, since snippets routinely use variables like `df` that are defined elsewhere. Other pyflakes findings show as warnings. |
| `sql` | CodeMirror 6, SQL mode | `sqlglot` parse, using the collection's dialect (**MySQL by default**) | `sqlglot` is lenient, so it catches most syntax errors but not all. The grader reads the code too. |

- Diagnostics run on the server (`POST /check`), triggered about 500 ms after you stop typing, and show as squiggles plus a gutter marker.
- The same editor is used for the answer box on code cards (both modes) and for the reference answer in the card form. Revealed reference answers are syntax-highlighted.
- They're passed to the grader as evidence. They never decide the outcome on their own.
- **No code execution** (see §13).

---

## 9. Architecture

### 9.1 Modules

```
┌───────────────────────────── browser (localhost) ─────────────────────────────┐
│  Server-rendered pages (Jinja2) + a little plain JS + bundled CodeMirror 6    │
└──────────────────────────────────┬────────────────────────────────────────────┘
                                   │ HTML fragments / JSON over HTTP
┌──────────────────────────────────▼────────────────────────────────────────────┐
│  FastAPI app (single process): routes + templates only                        │
│                                                                               │
│   config     learner.toml + defaults                                          │
│   store      ←→ learner.db     SQLite access; migrations; JSON import/export  │
│   engine     (pure)            outcomes, streaks, statuses, queue, unlocks    │
│   sequencer  (pure)            concept graph, ordering, readiness, warnings   │
│   tutor                        hints, grading, sequencing calls; prompts/     │
│     └─ llm                     LLMProvider: ClaudeProvider · FakeProvider     │
│   checkers                     answer-format registry: ruff, sqlglot          │
└───────────────────────────────────────────────────────────────────────────────┘
```

- **Frontend without a runtime build step.** Pages are rendered on the server with Jinja2 and use plain forms. Two small JS modules handle the rest: `editor.js` (CodeMirror setup, calling `/check`) and `app.js` (study keys, card form). Grading and hints are plain `fetch` calls to `/grade` and `/hint`; HTMX turned out to be unnecessary.
- **CodeMirror is one pre-built file** in `static/vendor/codemirror.js`, kept in the repo. Bundling guarantees a single copy of CodeMirror's core (loading its packages separately from a CDN can produce two, which breaks the editor) and works offline. Node is needed only to upgrade it: `cd tools/codemirror && npm install && npm run build`.
- **`LLMProvider`** has one method, roughly `complete(system, user, schema) → dict`. `ClaudeProvider` is the real one. `FakeProvider` returns canned responses for tests and demos.
- **Graceful failure:** if an LLM call errors or times out, that card falls back to "flip and mark yourself." A session never blocks on the LLM.
- **Schema changes:** numbered SQL files (`migrations/001_init.sql`, `002_….sql`) applied at startup, tracked with SQLite's built-in `PRAGMA user_version`. No migration framework.
- **First run:** if the database is empty, the starter collections in `starter/` are imported automatically.
- **Run:** `uv run learner` starts the server and opens the browser. It listens on `127.0.0.1` only, since it holds your API key and has no login. It also refuses requests addressed to any other host name and writes sent from other websites, so a web page you visit can't change your cards or spend API credits through it.
- **One attempt per card shown:** an answer is only recorded for the card the session is currently showing, so a held-down key, the back button or a refresh can't record it twice.

### 9.2 Configuration
| Where | What |
|---|---|
| `ANTHROPIC_API_KEY` env var | The API key. Unset means LLM features are off. |
| `learner.toml` | Everything else: LLM provider (`claude`, `fake` for demos/tests, or `off`), model per task, effort, timeout, DB path, port, `session_size`, `new_per_session`, `refresher_pct`, `known_streak`, `unlock_pct`. |
| Collection edit form | Only what belongs to a collection: name, tier labels, SQL dialect. |

- Defaults live in `config.py`. A missing `learner.toml` just means "all defaults."
- The repo ships a `learner.example.toml` with every setting listed and commented.
- Study thresholds are global, not per collection. Per-collection overrides can be added later if ever needed.

### 9.3 Project layout
```
learner/
├── pyproject.toml
├── learner.example.toml       # every setting, commented
├── learner/
│   ├── app.py                 # FastAPI routes
│   ├── config.py              # defaults + learner.toml loader
│   ├── models.py              # shared data types + JSON import schema
│   ├── store.py               # SQLite access, import/export
│   ├── engine.py              # pure: outcomes, status, queue, unlocks
│   ├── sequencer.py           # pure: concept graph, order, warnings
│   ├── tutor.py               # hint / grade / sequence calls
│   ├── llm.py                 # LLMProvider, ClaudeProvider, FakeProvider
│   ├── checkers.py            # answer-format registry
│   ├── migrations/            # 001_init.sql, …
│   ├── prompts/               # hint.md, grade.md, sequence.md
│   ├── templates/             # Jinja2 pages
│   ├── static/                # app.js, editor.js, app.css, vendor/codemirror.js
│   └── starter/               # sql.json, python.json
├── tools/codemirror/          # how vendor/codemirror.js is built (upgrade-time only)
├── tests/                     # engine + sequencer unit tests, FakeProvider
└── evals/                     # grader test set + runner script
```

*Considered and rejected:* Svelte/React SPA (a second toolchain and build step for one interactive screen), Streamlit/Gradio (poor code-editor experience and keyboard flow), Electron/Tauri (adds complexity for no single-user benefit), a migration framework like Alembic (overkill for one SQLite file), per-collection study settings and a Settings screen (global settings cover it).

---

## 10. Screens

1. **Dashboard:** collections with per-tier progress (% known, unlocked or not, "complete" badge), and a "Study" button.
2. **Study:** prompt → answer (text box or code editor) → `Alt+H` hint → `Ctrl+Enter` submit → reference answer + references immediately, verdict + feedback when ready → `Enter` accept, or `1`/`2` to mark right/wrong yourself → next.
   - End screen: session summary, plus "Tier complete, try Tier N+1" when it applies.
3. **Browse / Edit:** filter by collection, tier, tag, or status. Edit cards (form with markdown preview, the code editor for code fields, and a references list), edit or archive collections (name, tier labels, SQL dialect), and import/export JSON.
4. **Card order** (linked from each collection on the dashboard): an **ordered list per tier** (not a graph drawing), showing each card's "teaches" and "needs" concepts, its status, and any warnings from §6.7. Shows the out-of-date banner and the "Rebuild order" button. The dashboard flags collections with cards still to order.

5. **Draft with AI:** paste notes, code or a topic, pick a collection, how many (up to 5) and optionally a tier. The AI (Sonnet, `draft_model`) drafts cards in the collection's style, with key points, concepts and suggested doc links. You tick which to keep; suggested links are only saved if you tick "I checked these links". Missing-concept warnings on the Card order page link here with the concept filled in.
6. **Stats:** answers, % right, hint use and typical time per card; answers per day over 30 days (a bar chart with hover details and a table view); AI grader agreement per format, split into "too harsh" and "too lenient"; weakest concepts and cards to work on. Filterable by collection.
7. **New collections** can also be created from the card form ("+ New collection…" in the collection list).

---

## 11. Data (SQLite) — sketch

- `collection(id, name, tier_labels_json, sql_dialect='mysql', max_unlocked_tier=1, archived)`
  - `max_unlocked_tier` only ever increases, which is what makes unlocks sticky.
- `card(id, collection_id, tier, answer_format, prompt, reference_answer, rubric, references_json, tags, archived, progress_reset_at, created_at, updated_at)`
- `card_analysis(card_id, concepts_required_json, concepts_introduced_json, difficulty, manual, analyzed_at)`
  - A card is unsequenced when there's no row, or `card.updated_at > analyzed_at`.
- `attempt(id, card_id, session_id, ts, answer, max_hint_level, llm_verdict, llm_feedback, outcome, overridden, duration_ms)`
  - `llm_verdict` is empty when you marked yourself; `overridden` is set when you disagreed with the LLM.
  - `session_id` is a random ID generated when a session starts. There's no session table: a session's tier, mode, and summary are worked out from its attempts.

There is no progress table. Status, streak, and last-seen time are calculated from `attempt` (§5.3).

**JSON import/export** (one file per collection; this is also the format of the starter collections):
```json
{
  "collection": "SQL",
  "sql_dialect": "mysql",
  "tier_labels": {"1": "Foundations", "2": "Intermediate"},
  "cards": [
    {"tier": 1, "answer_format": "sql",
     "prompt": "Return each customer's name and their order count, including customers with no orders.",
     "reference_answer": "SELECT c.name, COUNT(o.id) FROM customers c LEFT JOIN orders o ON o.customer_id = c.id GROUP BY c.id, c.name;",
     "rubric": ["LEFT JOIN so customers without orders appear", "COUNT a column from orders, not *"],
     "references": [{"title": "MySQL: JOIN clause", "url": "https://dev.mysql.com/doc/refman/8.0/en/join.html"}]}
  ]
}
```
- Import validates the file and reports every problem with its card's position, so a bad file is easy to fix.
- Import upserts cards by `id` when present and creates them when absent.
- This format is also the easiest way to have an LLM draft a starter deck outside the app.

---

## 12. Phased Plan

| Phase | Scope | Usable result |
|---|---|---|
| **0. Skeleton** | Config, migrations, store, engine (outcomes, per-session streak rule, statuses, queue, sticky unlock, tier complete), JSON import/export, starter collections, card + collection editing (incl. references), study loop with self-marking | A working flashcard app, useful on first run |
| **1. Code feel** | CodeMirror, answer-format registry, ruff + sqlglot checking | Code cards feel like an editor |
| **2. LLM tutor** | `LLMProvider` (Claude + Fake), prompt files, grading for every card, hint ladder, override, graceful fallback | AI grading and hints |
| **3. Sequencing** | Batch concept analysis, graph ordering, readiness, out-of-date detection, sequence list + warnings, manual concept edits | Smart ordering of new cards |
| **4. Polish** | Stats, grader test set (`learner-eval`), authoring assistant, new collection from the card form | Insight into progress and grading; faster authoring |

Phase 0 is usable on its own, and each later phase adds to it.

---

## 13. To-Do / Later

- [ ] **Code execution.** SQL: run user and reference queries against an author-provided schema + sample data in in-memory DuckDB/SQLite and compare result sets. Python: run author-provided asserts in a sandboxed subprocess with a timeout. Results would feed the grader as evidence.
- [ ] **Optional day-based retention mode** (FSRS) for long-term spacing.
- [ ] **Link checker:** a command that flags dead reference URLs.
- [ ] **Adaptive refresh:** after a miss, pull that card's prerequisites forward into the session. Now possible with the concept graph; worth doing once real use shows misses clustering on prerequisites (the Stats page's weakest concepts will show it).
- [ ] **Per-collection setting overrides**, if global settings ever prove too coarse.
- [ ] **Additional LLM providers** (OpenAI, local via Ollama, …).
- [ ] **Multi-part cards.**
- [ ] **Anki import.**
