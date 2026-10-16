# Learner — Decision Log

Why the design in [DESIGN.md](DESIGN.md) is the way it is. Numbered entries are never renumbered; later entries can supersede earlier ones.

**Round 1**

| # | Question | Decision |
|---|---|---|
| 1 | Tier naming | Numeric tiers + optional labels per collection. |
| 2 | Content storage | **SQLite only**, with JSON import/export. |
| 3 | Tier gating | **Soft:** warn, allow override. |
| 4 | Code execution | **Not now.** Syntax checking only. |
| 5 | LLM provider | **Claude** first, behind a swappable `LLMProvider`. |
| 6 | Card granularity | **One prompt per card.** |
| 7 | Mastery / unlock | **No day-based thresholds.** Attempt streaks + % of tier known. |
| 8 | Default thresholds | **2 in a row** for `known`, **80%** for tier unlock. Configurable (global since #35). |
| 9 | Resurfacing `known` cards | **In-session refresher sample only.** |
| 10 | Default SQL dialect | **MySQL.** Configurable per collection. |

**Round 2 (consistency review)**

| # | Change | Why |
|---|---|---|
| 11 | Merged `type` + `language` into one `answer_format` field | They always moved together. |
| 12 | Three explicit attempt outcomes: `correct` / `assisted` / `incorrect` | The hint rules and the `known` definition contradicted each other. |
| 13 | Dropped the card-level `locked` status; concept order is a preference, not a lock | Tier gating is the only lock, so there are fewer states and no dead ends. |
| 14 | Prerequisite needs **any one** `known` card that introduces it | "Every card that introduces it" was wrong. |
| 15 | Concept graph spans the collection; ordering applies within a tier; added tier-mismatch warning | Cross-tier prerequisites weren't handled. |
| 16 | Dropped `pin` and `concept` tables, aliases, `score`, prompt caching, global `settings` table | Each was unneeded complexity. |
| 17 | Sequence view is an ordered list, not a graph drawing | Much simpler to build and just as useful. |
| 18 | JSON import/export moved into Phase 0 | Without it, an empty app has to be filled by hand. |
| 19 | Archive instead of delete; edits keep progress, with a reset button | Protects history. |
| 20 | Added `FakeProvider` and graceful LLM fallback | Makes tests and offline use possible. |
| 21 | Ruff limited to syntax + a few rules | Snippets would otherwise be full of false warnings. |

**Round 3 (second review)**

| # | Change | Why |
|---|---|---|
| 22 | **Quick** and **Practice** study modes, with a per-card default and a per-session override | Typing and LLM grading every card is too slow for fast review. |
| 23 | Only a card's first attempt in a session can raise its streak | Otherwise a card could become `known` within minutes from short-term memory. |
| 24 | Tier unlocks are sticky (`max_unlocked_tier`) | A few misses shouldn't lock a tier you're already working in. |
| 25 | Concept analysis in batch per tier *(further simplified in #36)* | Fewer calls, consistent concept names, better ordering. |
| 26 | **Frontend: Jinja2 + HTMX + plain JS + CodeMirror from CDN; no Node** | Keeps the whole app in Python with no build step. |
| 27 | Refresher picks least recently seen `known` cards | Better coverage than random, still not day-based. |
| 28 | "Tier complete" message and next-tier suggestion | Avoids refresher-only sessions with no direction. |
| 29 | Grader test set built from overrides | A way to check that grading-prompt changes actually help. |
| 30 | Migrations as numbered SQL files + `PRAGMA user_version` | Simplest thing that handles schema changes. |
| 31 | Hint ladder of 3 levels plus "Give up"; restored the tutor section (§7) | The Round 2 rewrite had accidentally dropped it. |

**Round 4 (simplicity, modularity, ease of use and maintenance)**

| # | Change | Why |
|---|---|---|
| 32 | Dropped the `card_progress` table; progress is calculated from `attempt`, and reset uses `progress_reset_at` | One record of progress, nothing to keep in sync, history preserved. |
| 33 | **Any hint** makes a right answer `assisted`; only wrong or "Give up" is `incorrect` | Removes the special level-3 rule. |
| 34 | Hints are LLM-only; dropped the card's hint fields | One less thing to write per card. |
| 35 | One `learner.toml` + `ANTHROPIC_API_KEY`; study settings are global; collections keep only name, tier labels, dialect; **no Settings screen** | One place to look; one fewer screen to build. |
| 36 | Sequencing is batch-only, with an out-of-date banner; dropped per-save analysis, chunking, and content hashes | One code path instead of three. |
| 37 | Starter collections in `starter/`, imported on first run | Useful immediately, and a working example of the JSON format. |
| 38 | LLM prompts live as files in `prompts/` | Tune behavior without touching code. |
| 39 | Added the project layout (§9.3) | Makes the module boundaries concrete. |
| 40 | Added `references` (list of `{title, url}`) to each card, shown on the answer side | Every card links to the right documentation. |

**Round 5 (final pre-build review)**

| # | Change | Why |
|---|---|---|
| 41 | Dropped the `session` table; each attempt carries a `session_id` | The per-session streak rule only needs the ID; everything else about a session can be worked out from its attempts. |
| 42 | `max_unlocked_tier` named as the one deliberate exception to "the attempt log is the only record of progress" | Replaying history to decide whether a tier was ever unlocked is needless complexity. |
| 43 | Server listens on `127.0.0.1` only | It holds an API key and has no login. |
| 44 | Collections can be archived, like cards | Consistent rule: hidden from the dashboard, history kept, no hard delete. |
| 45 | Decision log moved from DESIGN.md to DECISIONS.md | DESIGN.md stays a clean description of the current design. |

**Round 6 (after trying Phase 0)**

| # | Change | Why |
|---|---|---|
| 46 | Removed the per-session mode override (supersedes part of #22); every card is always studied in its own mode | Choosing a mode before each session was an unnecessary step. |

**Round 7 (Phase 1)**

| # | Change | Why |
|---|---|---|
| 47 | CodeMirror ships as one pre-built file in the repo, instead of loading from a CDN (amends #26) | Separate CDN modules can load two copies of CodeMirror's core and break the editor; one file is reliable and works offline. Node is needed only to upgrade it. |
| 48 | No HTMX for now; plain forms plus a `fetch` to `/check` | Nothing in Phases 0–1 needed it. Revisit for Phase 2's verdicts. |
| 49 | The answer-format registry in `checkers.py` is the single source of `ANSWER_FORMATS` | Adding a language really is one entry. |

**Round 8 (after trying Phase 1)**

| # | Change | Why |
|---|---|---|
| 50 | Every card has an answer box, Quick cards included; the modes now differ only in who grades | Writing an answer out tests recall better than reciting it to yourself. |

**Round 9 (before Phase 2)**

| # | Change | Why |
|---|---|---|
| 51 | **One study flow for all cards; Quick/Practice modes removed** (supersedes #22, #46, #50). The LLM grades every card when configured; otherwise you mark yourself | With an answer box on every card, the modes differed only in who grades. Removing them drops a card field, a UI concept, and branching logic. |
| 52 | Blank answers skip the LLM and go to self-marking | Nothing to grade, and it keeps a fast "just show me" path. |
| 53 | Grading and hints use Claude Haiku 5.5 at low effort; sequencing uses Claude Sonnet 5.5. Per-format grading models only if the grader test set shows a need | Grading runs on every card, so speed matters; tiers should follow evidence, not guesses. |
| 54 | Dropped the explicit "Give up" button | Submitting and marking yourself wrong does the same thing. |

**Round 10 (Phase 2 review)**

| # | Change | Why |
|---|---|---|
| 55 | Grading and hints are plain `fetch` calls; HTMX stays out (resolves #48) | The study screen's needs are small and already met. |
| 56 | Requests must be addressed to 127.0.0.1/localhost, and writes from other origins are refused | With no login, any website could otherwise post forms to the app (edit cards, spend API credits), including via DNS rebinding. |
| 57 | The server only records an answer for the card the session is currently showing | A held-down `1` key, the back button or a refresh could record the same answer several times and inflate progress. |
| 58 | `/check`, `/grade` and `/hint` are plain (threaded) handlers | Each does slow, blocking work (a ruff subprocess, an API call) that would otherwise freeze every other request. |

**Round 11 (Phase 3)**

| # | Change | Why |
|---|---|---|
| 59 | Rebuilding the order is always a click ("Order these cards now" after an import, "Rebuild order" on the Card order page), never automatic | It takes about a minute per tier; an import or app start shouldn't silently wait on it. |
| 60 | Sequencing calls Sonnet 5.5 with streaming and the server-side refusal fallback (`fallbacks: "default"`), at `sequence_effort` (default medium) | A tier's analysis can be long, and a misfiring safety classifier on one card shouldn't fail the whole tier. Haiku has no server-side fallback, so grading doesn't use it. |
| 61 | Concepts you type in the card form are kept (`manual`) until you untick "Keep my concepts" | Clear ownership: the AI fills in, you override, and an override is never lost. |
| 62 | Ready new cards come first, then the rest in graph order | A new card waits behind independent material until what it builds on is known, without ever blocking a session. |

**Round 12 (Phase 4)**

| # | Change | Why |
|---|---|---|
| 63 | The grader test set includes agreed answers as well as overrides, in equal numbers | Overrides alone only show fixes; agreed answers catch regressions. |
| 64 | `learner-eval run --model` compares grading models on your own cases | Turns the per-format model question (#53) into a measurement. |
| 65 | Drafting uses Sonnet 5.5 (`draft_model`, `draft_effort`) with streaming and the server-side fallback, like sequencing | Card quality matters more than speed here, and it runs occasionally. |
| 66 | Drafts are reviewed before saving: you tick cards to keep, and suggested links are saved only if you tick them separately | AI-suggested URLs can be wrong; nothing the AI writes is saved without a decision from you. |
| 67 | Drafted cards keep the AI's concepts as a normal (not manual) analysis | They're ordered right away, and the next rebuild can still refine them. |
| 68 | Stats show one chart (answers per day, single series) and otherwise tables and tiles | Most of these numbers are read, not compared visually; one chart avoids dual axes and colour legends. |
| 69 | Adaptive prerequisite refresh stays on the To-Do list | Better decided once real use (the weakest-concepts table) shows misses clustering on prerequisites. |

**Round 13 (Phase 5 design: images and Anki import)**

| # | Change | Why |
|---|---|---|
| 70 | Images in both prompts and reference answers; raster formats only (no SVG) | Diagrams and charts are common in technical material; SVG can carry scripts. |
| 71 | Images stored inside `learner.db`, deduplicated by fingerprint | Keeps the one-file principle (#2) for backup. |
| 72 | The AI grader and hints see card images | Otherwise image-based questions can't be graded. |
| 73 | Collections get an optional group label (one level), with names unique per group | Supports e.g. a Data science group holding Experiments, Forecasting, MMM and Probability, without a full hierarchy. |
| 74 | Anki subdecks become separate collections (grouped under the parent deck), never tiers | Matches how you want to organise subjects. |
| 75 | Anki review history is not imported; everything starts as `new` | Anki's schedule doesn't map onto session streaks, and a fresh start is simpler. |
| 76 | Anki import previews before writing, and re-imports update by Anki note id | Shared decks vary a lot; you see the mapping first, and updated decks don't duplicate. |
| 77 | `imports/` is the drop folder for decks; its contents are git-ignored | Decks can be large or personal. |

**Round 14 (Phase 5: images and groups built)**

| # | Change | Why |
|---|---|---|
| 78 | Default image limit 4 MB (`max_image_mb`), image type detected from the file's bytes | Stays inside the AI API's image limits; a renamed file can't pass as an image. |
| 79 | The migration runner turns foreign keys off around migrations and checks them afterwards | Changing the collection name rule required rebuilding a table, which SQLite only allows that way. |
| 80 | Image tools appear on text fields only (prompt, and the reference answer of text cards) | Images don't belong inside code answers. |

**Round 15 (Phase 5: Anki import built)**

| # | Change | Why |
|---|---|---|
| 81 | Read Anki's database in memory with the untrusted-schema protection on; read its protobuf settings with a tiny built-in decoder | Decks can come from anywhere; avoiding Anki's own (large) package keeps dependencies small. Adds `zstandard`, `markdownify` (with BeautifulSoup). |
| 82 | Follow each note type's own card templates instead of special-casing "Basic"/"Cloze" | Custom note types (like Yanki's) and reversed / type-in / optional-reverse cards work without extra code. |
| 83 | On re-import, Anki's prompt, answer and tags win; your tier, answer format, key points, links and archived state are kept | The deck stays the source for content; local study choices aren't lost. |
| 84 | Uploaded decks are saved into `imports/`; the preview and import refer to files there by plain name only | One place for decks, and no file paths ever come from the browser. |
| 85 | AI-suggested key points for imported cards moved to the To-Do list | Grading already works against the reference answer; better to see real use first. |

**Round 16 (Anki subdecks as tiers)**

| # | Change | Why |
|---|---|---|
| 86 | Anki subdecks become **tiers** of one collection by default (supersedes #74); "separate collections" stays as a per-deck option | Matches how you want subjects organised: one SQL collection stepping up through its subdecks. |
| 87 | Tier order is shown and editable on the preview, starting from Anki's order; an AI button suggests a foundations-first order | Anki's alphabetical order says nothing about difficulty, and a guess is better confirmed than silently applied. |
| 88 | If a same-named collection already has other cards, the import defaults to "<name> (Anki)" | Avoids mixing an Anki deck into, say, the starter SQL collection by accident. |
| 89 | Tiers from the preview apply on every import (overriding local tiers), unlike other local edits | It's an explicit choice made at import time; it also lets you switch layouts later. |

**Round 17 (combine and re-tier collections)**

| # | Change | Why |
|---|---|---|
| 90 | One "Combine or re-tier" tool maps every (collection, tier) row to a tier of a target collection, reusing the import's controls and AI ordering | Same mental model as the Anki import; merging, reordering and re-tiering are all the same operation. |
| 91 | The target must be new or one of the chosen collections | Prevents accidentally pouring cards into an unrelated collection. |
| 92 | Moving cards doesn't touch their content or `updated_at` | Progress, history and concept analyses stay valid; no forced re-sequencing. |
| 93 | Emptied source collections are archived, not deleted | Consistent with "no hard delete"; easy to undo by hand. |

**Round 18 (managing tiers)**

| # | Change | Why |
|---|---|---|
| 94 | Tiers are always 1…N with one label entry each; only `tiers.py` changes them, in one transaction, followed by a tidy step (also at startup and after imports/combines) | Free-text labels drifted out of step with the cards; one owner plus an enforced rule makes that impossible. |
| 95 | Tier labels are no longer free text after creation; the Tiers panel (rename, ↑/↓, move cards, delete, add) owns them | One place, one set of rules. |
| 96 | Deleting a tier archives its cards; they're parked beyond the last tier and go to the last tier if unarchived | Your choice: archive, not move. Parking prevents phantom tiers. |
| 97 | Empty tiers are allowed and never block unlocking | Lets you set up tiers before adding cards, without locking later tiers. |
| 98 | Unlocked tiers stay unlocked through reorganising | Reorganising should never take away access. |
| 99 | Card form tier = dropdown of real tiers + "new tier"; bulk "Move selected to" on the Cards page | Card-level moves can't create unlabelled tiers. |
| 100 | No drag and drop | Tiers are few (buttons suffice); bulk moves are faster with checkboxes; less JavaScript to maintain. |
| 101 | "Combine or re-tier" renamed "Combine collections"; single-collection hand edits live in the Tiers panel | Avoids two places doing the same job. |
