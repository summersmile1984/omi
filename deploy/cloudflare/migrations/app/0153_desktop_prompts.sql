-- Global operator-authored prompts; this table never stores user responses.
CREATE TABLE IF NOT EXISTS cf_desktop_prompts (
    id TEXT PRIMARY KEY,
    active INTEGER NOT NULL DEFAULT 1 CHECK (active IN (0, 1)),
    document_json TEXT NOT NULL CHECK (json_valid(document_json))
);

CREATE INDEX IF NOT EXISTS cf_desktop_prompts_active ON cf_desktop_prompts(active, id);
