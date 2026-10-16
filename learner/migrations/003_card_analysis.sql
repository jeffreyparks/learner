-- Concept analysis per card, used to order new cards within a tier (DESIGN.md §6).
CREATE TABLE card_analysis (
    card_id                  INTEGER PRIMARY KEY REFERENCES card(id),
    concepts_introduced_json TEXT    NOT NULL DEFAULT '[]',
    concepts_required_json   TEXT    NOT NULL DEFAULT '[]',
    difficulty               INTEGER NOT NULL DEFAULT 3 CHECK (difficulty BETWEEN 1 AND 5),
    manual                   INTEGER NOT NULL DEFAULT 0,  -- edited by you: the AI never overwrites it
    analyzed_at              TEXT    NOT NULL
);
