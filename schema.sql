CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS facts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL,
    source_trust TEXT NOT NULL DEFAULT 'untrusted',
    confidence REAL NOT NULL DEFAULT 0.5,
    entity_tags TEXT NOT NULL DEFAULT '[]',
    valid_from TEXT,
    valid_until TEXT,
    conflict_flag INTEGER NOT NULL DEFAULT 0,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS audit_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    source TEXT NOT NULL,
    lane TEXT,
    provider TEXT,
    model TEXT,
    action TEXT NOT NULL,
    result TEXT,
    metadata TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS observe_stream (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp TEXT DEFAULT CURRENT_TIMESTAMP,
    event_type TEXT NOT NULL,
    lane TEXT,
    provider TEXT,
    model TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);
