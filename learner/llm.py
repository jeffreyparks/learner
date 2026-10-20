"""LLM providers behind one small interface. The tutor (tutor.py) only ever calls `complete`.

Any failure raises LLMError, and callers fall back to letting you mark yourself: a session never
blocks on the LLM.
"""

from __future__ import annotations

import base64
import json
import os
import re
import time
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Protocol

import anthropic

from .config import Config

TASKS = ("grade", "hint", "sequence", "draft")
AUTHORING_TASKS = ("sequence", "draft")  # occasional, longer calls on the stronger model


class LLMError(Exception):
    """The LLM couldn't produce a usable answer. The message is shown to the user."""


# An image to show the model: (mime type, bytes).
ImageInput = tuple[str, bytes]


class LLMProvider(Protocol):
    name: str

    def complete(self, task: str, system: str, user: str, schema: dict, images: Sequence[ImageInput] = ()) -> dict:
        """Return a dict matching the JSON `schema`, or raise LLMError. `images` go before the text."""
        ...


def user_content(user: str, images: Sequence[ImageInput]) -> str | list[dict]:
    """The user turn: plain text, or the images (in order) followed by the text."""
    if not images:
        return user
    blocks = [
        {"type": "image", "source": {"type": "base64", "media_type": mime, "data": base64.b64encode(data).decode("ascii")}}
        for mime, data in images
    ]
    return blocks + [{"type": "text", "text": user}]


class ClaudeProvider:
    name = "Claude"

    # Authoring calls (ordering a tier, drafting cards) are long: give them room, and stream so
    # long outputs don't time out.
    AUTHORING_TIMEOUT = 600.0
    AUTHORING_MAX_TOKENS = 64000

    def __init__(self, cfg: Config, client: anthropic.Anthropic | None = None):
        self.models = {"grade": cfg.grade_model, "hint": cfg.hint_model, "ask": cfg.ask_model,
                       "sequence": cfg.sequence_model, "draft": cfg.draft_model}
        self.effort = cfg.effort
        self.authoring_effort = {"sequence": cfg.sequence_effort, "draft": cfg.draft_effort}
        # One retry: a slow answer is worse than falling back to marking yourself.
        self.client = client or anthropic.Anthropic(timeout=cfg.timeout, max_retries=1)

    def complete(self, task: str, system: str, user: str, schema: dict, images: Sequence[ImageInput] = ()) -> dict:
        try:
            response = self._authoring(task, system, user, schema) if task in AUTHORING_TASKS else self.client.messages.create(
                model=self.models[task],
                max_tokens=8000,
                system=system,
                messages=[{"role": "user", "content": user_content(user, images)}],
                output_config={"effort": self.effort, "format": {"type": "json_schema", "schema": schema}},
            )
        except anthropic.AuthenticationError as exc:
            raise LLMError("The API key was rejected. Check ANTHROPIC_API_KEY.") from exc
        except anthropic.RateLimitError as exc:
            raise LLMError("Rate limited by the API. Try again in a moment.") from exc
        except anthropic.APITimeoutError as exc:
            raise LLMError("The model took too long to answer.") from exc
        except anthropic.APIConnectionError as exc:
            raise LLMError("Couldn't reach the Claude API. Check your connection.") from exc
        except anthropic.APIStatusError as exc:
            raise LLMError(f"The Claude API returned an error ({exc.status_code}).") from exc
        except anthropic.AnthropicError as exc:  # e.g. no credentials found at request time
            raise LLMError(f"Claude isn't available: {exc}") from exc

        if response.stop_reason in ("refusal", "max_tokens"):
            raise LLMError(f"The model stopped before answering ({response.stop_reason}).")
        text = next((b.text for b in response.content if b.type == "text"), None)
        try:
            return json.loads(text or "")
        except json.JSONDecodeError as exc:
            raise LLMError("The model's reply couldn't be read.") from exc

    def _authoring(self, task: str, system: str, user: str, schema: dict):
        # Server-side fallback: if the model declines (e.g. a safety classifier misfires on a
        # security-flavoured card), the API retries on Anthropic's recommended fallback model.
        # Only some models accept it (Haiku doesn't), so it's used here, not for grading.
        with self.client.beta.messages.stream(
            model=self.models[task],
            max_tokens=self.AUTHORING_MAX_TOKENS,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.authoring_effort[task], "format": {"type": "json_schema", "schema": schema}},
            betas=["server-side-fallback-2026-07-01"],
            fallbacks="default",
            timeout=self.AUTHORING_TIMEOUT,
        ) as stream:
            return stream.get_final_message()


class FakeProvider:
    """Canned, deterministic answers: for tests, and for trying the flow without an API key.

    Grades by word overlap with the reference answer. `responses` can script any task's reply
    (a dict, or a function of the user message); `fail` makes every call raise LLMError.
    """

    name = "Fake (canned answers)"

    def __init__(self, responses: dict[str, dict | Callable[[str], dict]] | None = None,
                 fail: bool = False, delay: float = 0.0):
        self.responses = responses or {}
        self.fail = fail
        self.delay = delay
        self.calls: list[tuple[str, str]] = []
        self.images: list[list[ImageInput]] = []  # the images each call received

    def complete(self, task: str, system: str, user: str, schema: dict, images: Sequence[ImageInput] = ()) -> dict:
        self.calls.append((task, user))
        self.images.append(list(images))
        if self.delay:
            time.sleep(self.delay)
        if self.fail:
            raise LLMError("The fake provider was told to fail.")
        if task in self.responses:
            reply = self.responses[task]
            return reply(user) if callable(reply) else reply
        if task == "grade":
            return self._grade(user)
        if task == "hint":
            level = _tag(user, "hint_level") or "1"
            return {"hint": f"(Fake hint, level {level}) Re-read the prompt and think about the key idea it's testing."}
        if task == "ask":
            return {"reply": f"(Fake tutor, {_tag(user, 'phase')}) Here's how I'd think about it: start from what the prompt is really testing."}
        if task == "sequence" and "<subdecks>" in user:  # ordering an Anki deck's subdecks
            names = re.findall(r'<subdeck name="([^"]*)"', user)
            return {"order": list(reversed(names))}
        if task == "sequence" and "Return the tier names" in system:  # sorting a collection into tiers
            return self._retier(user)
        if task == "sequence":
            return self._sequence(user)
        if task == "draft":
            return self._draft(user)
        raise LLMError(f"The fake provider has no answer for {task!r}.")

    @staticmethod
    def _grade(user: str) -> dict:
        words = lambda s: set(re.findall(r"[a-z0-9_]+", s.lower()))
        reference, answer = words(_tag(user, "reference_answer")), words(_tag(user, "learner_answer"))
        overlap = len(reference & answer) / len(reference) if reference else 0.0
        verdict = "correct" if overlap >= 0.6 else "partially_correct" if overlap >= 0.3 else "incorrect"
        return {
            "verdict": verdict,
            "feedback": f"(Fake grader) Your answer shares {overlap:.0%} of the reference answer's words.",
            "rubric_hits": [],
            "rubric_misses": [],
            "equivalent_approach": False,
        }


    @staticmethod
    def _retier(user: str) -> dict:
        """Three tiers of equal size, in card id order."""
        ids = sorted(int(i) for i in re.findall(r'<card id="(\d+)"', user))
        return {
            "tiers": ["Fundamentals", "Intermediate", "Advanced"],
            "cards": [{"card_id": cid, "tier": 1 + min(2, 3 * n // len(ids))} for n, cid in enumerate(ids)],
        }

    @staticmethod
    def _sequence(user: str) -> dict:
        """Use each card's tags as its concepts: a tag is introduced by the first card that has it
        (in id order) and required by the later ones. Crude, but it yields a real graph to look at."""
        cards = re.findall(r'<card id="(\d+)"[^>]*tags="([^"]*)"[^>]*>(.*?)</card>', user, re.S)
        seen: set[str] = set()
        out = []
        for card_id, tags, body in sorted(cards, key=lambda c: int(c[0])):
            names = [t.strip() for t in tags.split(",") if t.strip()]
            out.append({
                "card_id": int(card_id),
                "concepts_introduced": [t for t in names if t not in seen],
                "concepts_required": [t for t in names if t in seen],
                "difficulty": min(5, 1 + len(_tag(body, "reference_answer")) // 80),
            })
            seen.update(names)
        return {"cards": out}


    @staticmethod
    def _draft(user: str) -> dict:
        """Echo the notes back as simple text cards, so the drafting flow can be tried without an API key."""
        count = int(re.search(r'count="(\d+)"', user)[1])
        notes = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", _tag(user, "notes")) if s.strip()] or ["(no notes)"]
        tier = re.search(r'tier="(\d*)"', user)[1] or "1"
        return {"cards": [
            {
                "tier": int(tier), "answer_format": "text",
                "prompt": f"(Fake draft {i + 1}) Explain in your own words: {notes[i % len(notes)]}",
                "reference_answer": notes[i % len(notes)], "rubric": ["Restates the idea accurately"],
                "references": [{"title": "Python docs", "url": "https://docs.python.org/3/"}],
                "tags": ["drafted"], "concepts_introduced": [f"draft-idea-{i + 1}"], "concepts_required": [],
            }
            for i in range(count)
        ]}


def _tag(text: str, name: str) -> str:
    match = re.search(rf"<{name}>\n?(.*?)\n?</{name}>", text, re.S)
    return match[1] if match else ""


def has_claude_credentials() -> bool:
    """An API key, an auth token, or a saved `ant auth login` profile (the SDK resolves all three)."""
    if any(os.environ.get(v) for v in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_PROFILE")):
        return True
    return (Path.home() / ".config" / "anthropic").is_dir()


def make_provider(cfg: Config) -> LLMProvider | None:
    """The configured provider, or None when LLM features are off or Claude has no credentials."""
    if cfg.provider == "off":
        return None
    if cfg.provider == "fake":
        return FakeProvider()
    if not has_claude_credentials():
        return None
    try:
        return ClaudeProvider(cfg)
    except anthropic.AnthropicError:
        return None
