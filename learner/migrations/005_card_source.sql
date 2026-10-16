-- Where an imported card came from (e.g. "anki:<note guid>:<template>"), so re-importing an updated
-- deck updates the card instead of adding a duplicate (DESIGN.md §12.3).
ALTER TABLE card ADD COLUMN source_ref TEXT;
CREATE INDEX card_by_source ON card(source_ref);
