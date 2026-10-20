-- A collection can override the global pace (DESIGN.md §5.7). '' = use the global pace.
ALTER TABLE collection ADD COLUMN pace TEXT NOT NULL DEFAULT '';
