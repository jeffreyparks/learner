# Learner

Inspired by Anki, built for spaced-repetition study of any subject including technical material: Python, SQL, machine learning, that sort of thing. 

A few things I wanted that Anki doesn't really do:

- **Real answers, not flip-and-recite.** Every card has an answer box, and code cards get a proper code editor with syntax checking. Writing it out tells me a lot more than saying it in my head.
- **An AI tutor.** It grades what I wrote (including a different approach that gets to the same result), and gives me hints when I'm stuck without just handing over the answer.
- **A sensible order without me managing one.** Within a tier, ideas build on each other. I don't want to hand-number cards, so the AI works out which cards teach what and introduces them in order.
- **Fast review, not a calendar.** Progress comes from getting things right across separate sessions, not from waiting days between reviews.

Everything is organized as **Collection → Tier → Card**, so each subject area is separate, and each collection steps up from foundations to harder material. If you want the full design, it's in [DESIGN.md](DESIGN.md), and the reasoning behind each choice is in [DECISIONS.md](DECISIONS.md).

## Running it

```bash
uv run learner
```

That opens http://127.0.0.1:8765. The first time it runs, it loads a starter SQL collection and a starter Python collection so there's something to study right away.

All my cards and progress live in one file, `learner.db`. If I want to change any settings, I copy `learner.example.toml` to `learner.toml` and edit it.

## Turning on the AI

The AI grades every answer, gives hints, orders cards and drafts new ones. To turn it on, I put my API key in a file called `.env` in the project folder:

```
ANTHROPIC_API_KEY=your-key
```

The app reads `.env` every time it starts, so I don't have to export the key in each new terminal. The file is git-ignored, so it never gets committed. An exported `ANTHROPIC_API_KEY`, or an `ant auth login` profile, works too.

When the app starts, it prints whether AI is on. Without a key, everything still works; I just mark my own answers.

If I want to try the whole flow without a key, I set `provider = "fake"` under `[llm]` in `learner.toml`, and it gives canned answers instead.

## Images and groups

Cards can have images (JPEG, PNG, GIF or WebP) in the question or the answer. In the card form I just paste one, drag it in, or click **Add image**. It asks for a short description, which the AI grader and screen readers use, and the AI grader sees the images too. They're stored inside `learner.db`, so backing up is still one file.

Collections can sit in a **group**, like a "Data science" group holding Experiments, Forecasting, MMM and Probability. I set the group in the collection form (or when making a new collection from the card form), and the dashboard shows each group as a heading.

## Importing from Anki

I export a deck from Anki (`.apkg`) and either drop it in the `imports/` folder or upload it on the **Import** page. It shows me a preview first: which collections it'll create and a couple of sample cards from each. Then I click Import.

By default each deck becomes one collection and its subdecks become tiers (`SQL::General Concepts` → tier 1, `SQL::Functions and Operators` → tier 2, and so on). The preview lets me set each subdeck's tier, and **Suggest tier order with AI** puts them in a sensible foundations-first order for me. If I'd rather have each subdeck as its own collection, I can pick that per deck instead. Everything starts fresh as new cards; Anki's review history doesn't come over. Basic, reversed and cloze cards all work, images included. If I import an updated version of the same deck later, it updates the cards instead of duplicating them, and keeps anything I changed here (tier, answer format, key points).

## Tiers, and combining collections

Each collection's **Edit** page has a **Tiers** panel: I can rename tiers, move them up or down, move all of a tier's cards into another tier, add an empty tier, or delete one (its cards get archived, not lost). Tier numbers and names always stay in step with the cards. To move individual cards, I filter **Cards** to one collection, tick the ones I want, and use **Move selected to**. The card form's Tier is a dropdown of the real tiers.

**Combine collections** on the dashboard lets me fold several collections into one, each becoming a tier, the same way subdecks do on import. For example, Causal Inference, Causal Inference Fundamentals, Causal Inference DAGs, Causal Inference Potential Outcomes and Experiments become one Causal Inference collection: tier 1 basics, tier 2 fundamentals, and so on. I can set the tiers myself or ask the AI for an order. Picking a single collection gets me an AI-suggested order for its tiers. Progress and history come along, and anything left empty is archived, not deleted.

## Card order

New cards get introduced so that a card teaching a concept comes before the cards that depend on it.

After I import cards, or add a bunch, I open **Card order** from the dashboard and click **Rebuild order**. The AI reads each tier and works out the concepts. If I disagree with it, I can set a card's concepts myself in the card form, and a rebuild will leave those alone.

## Drafting cards, stats, and keeping the grader honest

- **Draft with AI** (top bar): I paste notes or a topic and get draft cards back. I keep the ones I like and toss the rest.
- **Stats** (top bar): how I'm doing over time, which concepts I'm weakest on, and how often I've disagreed with the AI grader.
- **Grader test set:** whenever I overrule the grader, that gets saved. Once I've got a few, I collect them and re-test the grader to see if a prompt or model change actually made it better:

```bash
uv run learner-eval export
```

```bash
uv run learner-eval run
```

## Keys while studying

- `Ctrl+Enter` (or `Space` when I'm not in the answer box): submit my answer
- `Alt+H`: ask for a hint. I get up to three, each one more specific.
- `Alt+A` (the red **Show answer** button): skip straight to the reference answer to study it. No grading, and no effect on my score either way; the card just comes back later in the session for a real try.
- After submitting: `Enter` accepts the grader's verdict, `1` marks it right, `2` marks it wrong

## Running the tests

```bash
uv run pytest
```
