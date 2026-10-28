-- DocRenew persistent memory schema (SQLite is the authoritative store).
-- Every user-owned table carries user_id; every query in the code is scoped by it.
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS users (
  user_id      TEXT PRIMARY KEY,
  display_name TEXT NOT NULL,
  created_at   TEXT NOT NULL
);

-- SOURCE KNOWLEDGE: every loaded file, versioned by SHA-256 so a file is loaded once.
CREATE TABLE IF NOT EXISTS sources (
  source_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id   TEXT NOT NULL REFERENCES users(user_id),
  kind      TEXT NOT NULL CHECK (kind IN ('documents_csv','requirements')),
  path      TEXT NOT NULL,
  sha256    TEXT NOT NULL,
  version   INTEGER NOT NULL,
  status    TEXT NOT NULL CHECK (status IN ('current','superseded')),
  loaded_at TEXT NOT NULL
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_sources_hash ON sources(user_id, kind, sha256);

CREATE TABLE IF NOT EXISTS requirement_chunks (
  chunk_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          TEXT NOT NULL REFERENCES users(user_id),
  source_id        INTEGER NOT NULL REFERENCES sources(source_id),
  doc_type         TEXT NOT NULL,
  heading          TEXT NOT NULL,
  lead_days        INTEGER,
  body             TEXT NOT NULL,
  steps_json       TEXT NOT NULL,        -- [{"step_no":1,"text":"...","line":12}]
  quarantined_json TEXT NOT NULL,        -- lines removed by the injection guard
  line_start       INTEGER NOT NULL,
  line_end         INTEGER NOT NULL
);

-- LONG-TERM MEMORY: documents with versioned expiry dates (history is never overwritten).
CREATE TABLE IF NOT EXISTS documents (
  doc_id     TEXT PRIMARY KEY,           -- stable id: <user_id>-<doc_type>
  user_id    TEXT NOT NULL REFERENCES users(user_id),
  doc_type   TEXT NOT NULL,
  label      TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS document_versions (
  version_id    INTEGER PRIMARY KEY AUTOINCREMENT,
  doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  user_id       TEXT NOT NULL REFERENCES users(user_id),
  expiry_date   TEXT NOT NULL,
  origin        TEXT NOT NULL CHECK (origin IN ('csv_import','user_correction')),
  source_id     INTEGER REFERENCES sources(source_id),
  status        TEXT NOT NULL CHECK (status IN ('current','superseded')),
  reason        TEXT,
  recorded_at   TEXT NOT NULL,
  superseded_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_one_current_version ON document_versions(doc_id) WHERE status = 'current';

-- WORKING STATE: bounded checkpoint of a running review (resume after interruption).
CREATE TABLE IF NOT EXISTS review_cycles (
  cycle_id       INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id        TEXT NOT NULL REFERENCES users(user_id),
  sim_date       TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT,
  status         TEXT NOT NULL CHECK (status IN ('running','interrupted','completed')),
  provider       TEXT NOT NULL,
  memory_enabled INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS working_state (
  cycle_id   INTEGER PRIMARY KEY REFERENCES review_cycles(cycle_id),
  user_id    TEXT NOT NULL REFERENCES users(user_id),
  state_json TEXT NOT NULL,
  updated_at TEXT NOT NULL
);

-- What the agent last learned about each document (lead time and where it came from).
CREATE TABLE IF NOT EXISTS doc_state (
  doc_id        TEXT PRIMARY KEY REFERENCES documents(doc_id),
  user_id       TEXT NOT NULL REFERENCES users(user_id),
  lead_days     INTEGER,
  lead_source   TEXT,
  evidence_ref  TEXT,
  last_stage    TEXT,
  last_cycle_id INTEGER,
  updated_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS checklist_items (
  item_id          INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id          TEXT NOT NULL REFERENCES users(user_id),
  doc_id           TEXT NOT NULL REFERENCES documents(doc_id),
  step_no          INTEGER NOT NULL,
  text             TEXT NOT NULL,
  status           TEXT NOT NULL DEFAULT 'todo' CHECK (status IN ('todo','done','skipped')),
  evidence_ref     TEXT NOT NULL,
  created_cycle_id INTEGER,
  created_at       TEXT NOT NULL,
  updated_at       TEXT NOT NULL,
  UNIQUE (doc_id, step_no)               -- idempotent writes: a step exists once
);

-- Reminder DRAFTS ONLY. The system has no send capability.
CREATE TABLE IF NOT EXISTS reminders (
  reminder_id   INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id       TEXT NOT NULL REFERENCES users(user_id),
  doc_id        TEXT NOT NULL REFERENCES documents(doc_id),
  version_id    INTEGER NOT NULL REFERENCES document_versions(version_id),
  cycle_id      INTEGER,
  stage         TEXT NOT NULL CHECK (stage IN ('upcoming','start_now','overdue_start','expired')),
  dedupe_key    TEXT NOT NULL UNIQUE,    -- user|doc|version|stage
  subject       TEXT NOT NULL,
  body          TEXT NOT NULL,           -- agent suggestion
  edited_body   TEXT,                    -- user edit (kept separate from the suggestion)
  status        TEXT NOT NULL CHECK (status IN ('draft_unsent','approved','edited','rejected','superseded')),
  created_at    TEXT NOT NULL,
  decided_at    TEXT,
  decision_note TEXT
);

CREATE TABLE IF NOT EXISTS questions (
  question_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT NOT NULL REFERENCES users(user_id),
  doc_id      TEXT NOT NULL REFERENCES documents(doc_id),
  cycle_id    INTEGER,
  source_id   INTEGER,                   -- requirements source version when asked
  kind        TEXT NOT NULL CHECK (kind IN ('missing_requirements','missing_lead_days','conflicting_lead_days','agent_stalled')),
  text        TEXT NOT NULL,
  status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open','answered')),
  answer      TEXT,
  created_at  TEXT NOT NULL,
  answered_at TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_open_question ON questions(doc_id, kind) WHERE status = 'open';

CREATE TABLE IF NOT EXISTS preferences (
  user_id    TEXT NOT NULL REFERENCES users(user_id),
  key        TEXT NOT NULL,
  value      TEXT NOT NULL,
  source     TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  PRIMARY KEY (user_id, key)
);

CREATE TABLE IF NOT EXISTS decisions (   -- every human decision, kept apart from agent suggestions
  decision_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id     TEXT NOT NULL REFERENCES users(user_id),
  target_type TEXT NOT NULL,
  target_id   TEXT NOT NULL,
  decision    TEXT NOT NULL,
  note        TEXT,
  decided_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS episode_summaries (
  episode_id INTEGER PRIMARY KEY AUTOINCREMENT,
  user_id    TEXT NOT NULL REFERENCES users(user_id),
  cycle_id   INTEGER NOT NULL,
  doc_id     TEXT NOT NULL,
  summary    TEXT NOT NULL,
  created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS action_log (
  log_id         INTEGER PRIMARY KEY AUTOINCREMENT,
  ts             TEXT NOT NULL,
  sim_date       TEXT,
  pid            INTEGER NOT NULL,
  user_id        TEXT,
  cycle_id       INTEGER,
  event_type     TEXT NOT NULL CHECK (event_type IN ('tool_call','memory_read','memory_write','llm_call','decision','guard','error','cycle')),
  tool           TEXT,
  input_json     TEXT,
  output_summary TEXT,
  outcome        TEXT NOT NULL,
  latency_ms     REAL,
  idem_key       TEXT
);
