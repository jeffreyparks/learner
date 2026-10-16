# Learner

A single-user study app for fast technical review: free-text and code cards, organized as
Collection → Tier → Card, with progress measured by correct answers across sessions.
See [DESIGN.md](DESIGN.md) for the design and [DECISIONS.md](DECISIONS.md) for the reasoning.

## Run

```bash
uv run learner
```

Opens http://127.0.0.1:8765. On first run, the starter SQL and Python collections are loaded.
Settings: copy `learner.example.toml` to `learner.toml` and edit. Your data lives in `learner.db`.

## LLM grading and hints

Create a file named `.env` in the project folder with your key, and Claude grades every answer,
offers hints, orders cards and drafts new ones:

```
ANTHROPIC_API_KEY=your-key
```

The app reads `.env` each time it starts (it's git-ignored, so it's never committed). An exported
`ANTHROPIC_API_KEY` or an `ant auth login` profile works too. The startup line says whether AI is on. Without credentials, you mark your own answers. To try the flow without
an API key, set `provider = "fake"` under `[llm]` in `learner.toml` for canned answers.

## Card order

New cards are introduced so that a card teaching a concept comes before the cards that need it.
After importing cards (or adding several), open **Card order** on the dashboard and click
**Rebuild order**: the AI reads each tier and works out the concepts. You can also set a card's
concepts yourself in the card form.

## Drafting cards, stats, and checking the grader

- **Draft with AI** (top bar): paste notes or a topic and get draft cards to keep or discard.
- **Stats** (top bar): progress over time, weakest concepts, and how often you disagree with the AI grader.
- **Grader test set:** after you've overridden some verdicts, collect them and re-test the grader:

```bash
uv run learner-eval export
```

```bash
uv run learner-eval run
```

## Study keys

- `Ctrl+Enter` (or `Space` outside the answer box): submit your answer
- `Alt+H`: ask for a hint (up to three, each more specific)
- After submitting: `Enter` accepts the grader's verdict, `1` marks it right, `2` marks it wrong

## Test

```bash
uv run pytest
```
