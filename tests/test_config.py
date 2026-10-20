import pytest

from learner import config
from learner.config import ConfigError


def write(tmp_path, text):
    p = tmp_path / "learner.toml"
    p.write_text(text)
    return p


def test_pace_sets_presets_and_explicit_values_win(tmp_path):
    cfg = config.load(write(tmp_path, '[study]\npace = "fast"\nunlock_pct = 0.6\n'))
    assert (cfg.known_streak, cfg.new_per_session, cfg.unlock_pct) == (1, 10, 0.6)
    assert config.load(write(tmp_path, '[study]\npace = "slow"\n')).known_streak == 3


def test_no_pace_keeps_defaults_and_bad_pace_is_rejected(tmp_path):
    assert config.load(write(tmp_path, "")).known_streak == 2
    with pytest.raises(ConfigError):
        config.load(write(tmp_path, '[study]\npace = "turbo"\n'))
