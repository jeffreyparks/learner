-- Images on cards (DESIGN.md §12.1): stored in the database, one row per distinct image.
CREATE TABLE media (
    sha256     TEXT    PRIMARY KEY,
    mime       TEXT    NOT NULL CHECK (mime IN ('image/jpeg', 'image/png', 'image/gif', 'image/webp')),
    bytes      BLOB    NOT NULL,
    filename   TEXT    NOT NULL DEFAULT '',
    alt        TEXT    NOT NULL DEFAULT '',
    created_at TEXT    NOT NULL
);

-- Collection groups (DESIGN.md §12.2): names become unique per group, so the table is rebuilt
-- (SQLite can't drop a UNIQUE constraint in place). The runner turns foreign keys off around this.
CREATE TABLE collection_new (
    id                INTEGER PRIMARY KEY,
    group_name        TEXT    NOT NULL DEFAULT '',  -- '' = ungrouped
    name              TEXT    NOT NULL,
    tier_labels_json  TEXT    NOT NULL DEFAULT '{}',
    sql_dialect       TEXT    NOT NULL DEFAULT 'mysql',
    max_unlocked_tier INTEGER NOT NULL DEFAULT 1,
    archived          INTEGER NOT NULL DEFAULT 0,
    created_at        TEXT    NOT NULL,
    UNIQUE (group_name, name)
);
INSERT INTO collection_new (id, name, tier_labels_json, sql_dialect, max_unlocked_tier, archived, created_at)
    SELECT id, name, tier_labels_json, sql_dialect, max_unlocked_tier, archived, created_at FROM collection;
DROP TABLE collection;
ALTER TABLE collection_new RENAME TO collection;
