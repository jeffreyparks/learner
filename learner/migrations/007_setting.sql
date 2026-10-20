-- Small key/value settings changed from the UI (currently just `pace`, DESIGN.md §5.7).
CREATE TABLE setting (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
