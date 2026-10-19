-- "Show answer" on the study screen (DESIGN.md §5.2): a new `revealed` outcome that leaves the
-- score alone. SQLite can't change a CHECK constraint in place, so the table is rebuilt.
CREATE TABLE attempt_new (
    id             INTEGER PRIMARY KEY,
    card_id        INTEGER NOT NULL REFERENCES card(id),
    session_id     TEXT    NOT NULL,
    ts             TEXT    NOT NULL,
    answer         TEXT,
    max_hint_level INTEGER NOT NULL DEFAULT 0,
    llm_verdict    TEXT,
    llm_feedback   TEXT,
    outcome        TEXT    NOT NULL CHECK (outcome IN ('correct', 'assisted', 'incorrect', 'revealed')),
    overridden     INTEGER NOT NULL DEFAULT 0,
    duration_ms    INTEGER
);
INSERT INTO attempt_new (id, card_id, session_id, ts, answer, max_hint_level, llm_verdict, llm_feedback, outcome, overridden, duration_ms)
    SELECT id, card_id, session_id, ts, answer, max_hint_level, llm_verdict, llm_feedback, outcome, overridden, duration_ms FROM attempt;
DROP TABLE attempt;
ALTER TABLE attempt_new RENAME TO attempt;
CREATE INDEX attempt_by_card ON attempt(card_id, id);
CREATE INDEX attempt_by_session ON attempt(session_id);
