CREATE TABLE collection (
    id                INTEGER PRIMARY KEY,
    name              TEXT    NOT NULL UNIQUE,
    tier_labels_json  TEXT    NOT NULL DEFAULT '{}',
    sql_dialect       TEXT    NOT NULL DEFAULT 'mysql',
    max_unlocked_tier INTEGER NOT NULL DEFAULT 1,  -- only ever increases (sticky unlock)
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL
);

CREATE TABLE card (
    id                INTEGER PRIMARY KEY,
    collection_id     INTEGER NOT NULL REFERENCES collection(id),
    tier              INTEGER NOT NULL CHECK (tier >= 1),
    answer_format     TEXT    NOT NULL CHECK (answer_format IN ('text', 'python', 'sql')),
    default_mode      TEXT    NOT NULL DEFAULT 'quick' CHECK (default_mode IN ('quick', 'practice')),
    prompt            TEXT    NOT NULL,
    reference_answer  TEXT    NOT NULL,
    rubric_json       TEXT    NOT NULL DEFAULT '[]',
    references_json   TEXT    NOT NULL DEFAULT '[]',
    tags_json         TEXT    NOT NULL DEFAULT '[]',
    archived          INTEGER NOT NULL DEFAULT 0,
    progress_reset_at TEXT,
    created_at        TEXT    NOT NULL,
    updated_at        TEXT    NOT NULL
);
CREATE INDEX card_by_collection ON card(collection_id, tier);

-- The only record of progress: status and streaks are calculated from this table.
CREATE TABLE attempt (
    id             INTEGER PRIMARY KEY,
    card_id        INTEGER NOT NULL REFERENCES card(id),
    session_id     TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    mode           TEXT    NOT NULL CHECK (mode IN ('quick', 'practice')),
    answer         TEXT,
    max_hint_level INTEGER NOT NULL DEFAULT 0,
    llm_verdict    TEXT,
    llm_feedback   TEXT,
    outcome        TEXT    NOT NULL CHECK (outcome IN ('correct', 'assisted', 'incorrect')),
    overridden     INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER
);
CREATE INDEX attempt_by_card ON attempt(card_id, id);
CREATE INDEX attempt_by_session ON attempt(session_id);
