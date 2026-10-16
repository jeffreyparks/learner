-- One study flow for every card (DECISIONS.md #51): cards no longer have a mode, and attempts
-- record who graded them via llm_verdict / overridden instead.
ALTER TABLE card DROP COLUMN default_mode;
ALTER TABLE attempt DROP COLUMN mode;
