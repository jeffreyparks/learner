"""Settings: defaults live here, overridden by learner.toml (see learner.example.toml)."""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, fields, replace
from pathlib import Path


class ConfigError(ValueError):
    pass


# One word instead of several knobs (DESIGN.md §5.7). Each preset sets these study settings;
# anything also set explicitly in learner.toml wins over the preset.
PACES: dict[str, dict[str, float | int]] = {
    "slow": dict(unlock_pct=0.90, known_streak=3, new_per_session=3, refresher_pct=0.20, assisted_credit=0.0),
    "medium": dict(unlock_pct=0.70, known_streak=2, new_per_session=6, refresher_pct=0.10, assisted_credit=0.5),
    "fast": dict(unlock_pct=0.50, known_streak=1, new_per_session=10, refresher_pct=0.05, assisted_credit=0.5),
}


@dataclass(frozen=True)
class Config:
    # [app]
    db_path: str = "learner.db"
    port: int = 8765
    open_browser: bool = True
    max_image_mb: float = 4.0  # per image; the API also limits image size, so keep this modest
    # [study]
    pace: str = ""  # "" (the plain defaults below) | "slow" | "medium" | "fast"
    assisted_credit: float = 0.0  # streak credit for a right answer after a hint (0 = none, 0.5 = half a step)
    session_size: int = 20
    new_per_session: int = 5
    refresher_pct: float = 0.10
    known_streak: int = 2
    unlock_pct: float = 0.80
    requeue_gap: int = 3
    max_attempts_per_card: int = 3
    pinned: tuple[str, ...] = ()  # pace settings set explicitly in learner.toml; a pace choice never overrides these
    # [llm]
    provider: str = "claude"  # "claude" | "fake" (canned answers, for demos and tests) | "off"
    grade_model: str = "claude-haiku-5-5"
    hint_model: str = "claude-haiku-5-5"
    sequence_model: str = "claude-sonnet-5-5"
    draft_model: str = "claude-sonnet-5-5"  # drafts new cards from your notes
    effort: str = "low"  # for grading and hints, which run on every card
    sequence_effort: str = "medium"  # ordering runs rarely, as a batch, so it can think harder
    draft_effort: str = "medium"
    timeout: float = 30.0

    def __post_init__(self) -> None:
        for name in ("session_size", "known_streak", "max_attempts_per_card", "port"):
            if getattr(self, name) < 1:
                raise ConfigError(f"{name} must be at least 1")
        for name in ("new_per_session", "requeue_gap"):
            if getattr(self, name) < 0:
                raise ConfigError(f"{name} must not be negative")
        if self.pace and self.pace not in PACES:
            raise ConfigError('pace must be "slow", "medium" or "fast"')
        for name in ("refresher_pct", "unlock_pct", "assisted_credit"):
            if not 0 <= getattr(self, name) <= 1:
                raise ConfigError(f"{name} must be between 0 and 1")
        if not 0 < self.max_image_mb <= 20:
            raise ConfigError("max_image_mb must be more than 0 and at most 20")
        if self.provider not in ("claude", "fake", "off"):
            raise ConfigError('provider must be "claude", "fake" or "off"')
        for name in ("effort", "sequence_effort", "draft_effort"):
            if getattr(self, name) not in ("low", "medium", "high", "xhigh", "max"):
                raise ConfigError(f"{name} must be low, medium, high, xhigh or max")


PACE_KEYS = tuple(PACES["medium"])


def with_pace(cfg: Config, pace: str) -> Config:
    """`cfg` with a pace applied ("" = the plain defaults). Settings pinned in learner.toml keep their values."""
    if pace and pace not in PACES:
        raise ConfigError('pace must be "slow", "medium" or "fast"')
    plain = Config()
    preset = PACES.get(pace, {})
    values = {k: preset.get(k, getattr(plain, k)) for k in PACE_KEYS if k not in cfg.pinned}
    return replace(cfg, pace=pace, **values)


SECTIONS = {
    "app": ("db_path", "port", "open_browser", "max_image_mb"),
    "study": (
        "pace",
        "assisted_credit",
        "session_size",
        "new_per_session",
        "refresher_pct",
        "known_streak",
        "unlock_pct",
        "requeue_gap",
        "max_attempts_per_card",
    ),
    "llm": (
        "provider", "grade_model", "hint_model", "sequence_model", "draft_model",
        "effort", "sequence_effort", "draft_effort", "timeout",
    ),
}


def load_env_file(path: str | Path = ".env") -> list[str]:
    """Read KEY=VALUE lines from a .env file into the environment, so secrets like ANTHROPIC_API_KEY
    survive restarts without living in learner.toml. Variables already set win. Returns the names set."""
    path = Path(path)
    if not path.is_file():
        return []
    loaded = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.removeprefix("export ").partition("=")
        key, value = key.strip(), value.strip().strip("'\"")
        if key and value and key not in os.environ:
            os.environ[key] = value
            loaded.append(key)
    return loaded


def load(path: str | Path | None = None) -> Config:
    """Load settings from `path`, $LEARNER_CONFIG, or ./learner.toml. A missing default file means all defaults."""
    explicit = path is not None or "LEARNER_CONFIG" in os.environ
    path = Path(path or os.environ.get("LEARNER_CONFIG", "learner.toml"))
    if not path.exists():
        if explicit:
            raise ConfigError(f"Config file not found: {path}")
        return Config()

    raw = tomllib.loads(path.read_text())
    types = {f.name: type(getattr(Config(), f.name)) for f in fields(Config)}
    values: dict[str, object] = {}
    for section, settings in raw.items():
        if section not in SECTIONS or not isinstance(settings, dict):
            raise ConfigError(f"{path}: unknown section [{section}]")
        for key, value in settings.items():
            if key not in SECTIONS[section]:
                raise ConfigError(f"{path}: unknown setting {section}.{key}")
            expected = types[key]
            if expected is float and isinstance(value, int) and not isinstance(value, bool):
                value = float(value)
            if not isinstance(value, expected) or (expected is int and isinstance(value, bool)):
                raise ConfigError(f"{path}: {section}.{key} should be a {expected.__name__}")
            values[key] = value
    pace = values.get("pace", "")
    if pace and pace not in PACES:
        raise ConfigError(f'{path}: study.pace must be "slow", "medium" or "fast"')
    pinned = tuple(k for k in values if k in PACE_KEYS)
    return replace(Config(), **{**PACES.get(pace, {}), **values}, pinned=pinned)
