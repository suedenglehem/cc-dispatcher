-- 0001_init.sql — initial schema (PRD §6 data model)

CREATE TABLE IF NOT EXISTS schema_version (
    v          INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS projects (
    id                 TEXT PRIMARY KEY,
    repo_path          TEXT NOT NULL UNIQUE,
    integration_path   TEXT NOT NULL,
    integration_ref    TEXT NOT NULL DEFAULT 'main',
    mycelium_project   TEXT NOT NULL DEFAULT '',
    mode               TEXT NOT NULL DEFAULT 'single'
                       CHECK (mode IN ('single','team')),
    drain_policy       TEXT CHECK (drain_policy IS NULL OR drain_policy IN ('drain','cancel')),
    mode_changed_at    TEXT,
    primary_session_id TEXT,
    updated_at         TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS resource_groups (
    id              TEXT PRIMARY KEY,
    max_concurrency INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS backends (
    id                TEXT PRIMARY KEY,
    endpoint          TEXT NOT NULL,
    openai_base_url   TEXT,
    model_id          TEXT NOT NULL,
    model_label       TEXT,
    credential_env    TEXT NOT NULL DEFAULT '{}',
    enabled           INTEGER NOT NULL DEFAULT 1,
    is_primary        INTEGER NOT NULL DEFAULT 0,
    manually_reserved INTEGER NOT NULL DEFAULT 0,
    reserved_note     TEXT,
    health            TEXT NOT NULL DEFAULT 'unknown'
                      CHECK (health IN ('unknown','online','offline')),
    probed_at         TEXT,
    ctx_max           INTEGER,
    concurrency       INTEGER NOT NULL DEFAULT 1,
    resource_group    TEXT REFERENCES resource_groups(id),
    notes             TEXT
);

CREATE TABLE IF NOT EXISTS worker_profiles (
    id                 TEXT PRIMARY KEY,
    adapter            TEXT NOT NULL CHECK (adapter IN ('claude','cline')),
    executable         TEXT NOT NULL,
    args               TEXT NOT NULL DEFAULT '[]',
    env_overrides      TEXT NOT NULL DEFAULT '{}',
    backend_ids        TEXT NOT NULL DEFAULT '[]',
    allowed_tools      TEXT NOT NULL DEFAULT '[]',
    deadline_s         INTEGER NOT NULL DEFAULT 1800,
    max_attempts       INTEGER NOT NULL DEFAULT 2,
    output_limit_chars INTEGER NOT NULL DEFAULT 8000,
    allow_test_repair  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS jobs (
    id                    TEXT PRIMARY KEY,
    project_id            TEXT NOT NULL REFERENCES projects(id),
    idempotency_key       TEXT,
    objective             TEXT NOT NULL,
    acceptance_criteria   TEXT NOT NULL DEFAULT '[]',
    base_commit           TEXT NOT NULL,
    allowed_scope         TEXT NOT NULL DEFAULT '[]',
    relevant_files        TEXT NOT NULL DEFAULT '[]',
    contracts_text        TEXT NOT NULL DEFAULT '',
    decisions_text        TEXT NOT NULL DEFAULT '',
    exclusions_text       TEXT NOT NULL DEFAULT '',
    worker_policy         TEXT NOT NULL DEFAULT '',
    memory_queries        TEXT NOT NULL DEFAULT '[]',
    dependencies          TEXT NOT NULL DEFAULT '[]',
    profile_id            TEXT NOT NULL REFERENCES worker_profiles(id),
    backend_id            TEXT REFERENCES backends(id),
    status                TEXT NOT NULL DEFAULT 'queued'
                          CHECK (status IN ('queued','paused','starting','running','succeeded','failed','cancelled','blocked','interrupted')),
    status_detail         TEXT,
    memory_required       INTEGER NOT NULL DEFAULT 0,
    recovery_from_attempt TEXT,
    recovery_context      TEXT,
    created_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at            TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    started_at            TEXT,
    finished_at           TEXT,
    deadline_start_at     TEXT,
    deadline_expires_at   TEXT,
    attempt_count         INTEGER NOT NULL DEFAULT 0,
    result_json           TEXT,
    failure_class         TEXT,
    integrated_commit     TEXT,
    integration_disposition TEXT NOT NULL DEFAULT 'pending'
                          CHECK (integration_disposition IN ('pending','accepted','rejected','superseded')),
    scope_violations      TEXT NOT NULL DEFAULT '[]',
    mycelium_project      TEXT NOT NULL DEFAULT '',
    UNIQUE (project_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_jobs_project_status ON jobs(project_id, status, created_at);

CREATE TABLE IF NOT EXISTS attempts (
    id                    TEXT PRIMARY KEY,
    job_id                TEXT NOT NULL REFERENCES jobs(id),
    attempt_no            INTEGER NOT NULL,
    status                TEXT NOT NULL DEFAULT 'starting'
                          CHECK (status IN ('starting','running','succeeded','failed','cancelled','interrupted')),
    pid                   INTEGER,
    pgid                  INTEGER,
    start_identity        TEXT,
    backend_id            TEXT NOT NULL REFERENCES backends(id),
    worktree_path         TEXT NOT NULL,
    branch                TEXT NOT NULL,
    started_at            TEXT,
    ended_at              TEXT,
    exit_code             INTEGER,
    failure_class         TEXT,
    stream_log_path       TEXT,
    checkpoint_path       TEXT,
    result_path           TEXT,
    diff_path             TEXT,
    recovery_from         TEXT,
    recovery_context      TEXT,
    usage_json            TEXT,
    UNIQUE (job_id, attempt_no)
);
CREATE INDEX IF NOT EXISTS idx_attempts_job ON attempts(job_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_attempts_status ON attempts(status);

CREATE TABLE IF NOT EXISTS reservations (
    attempt_id  TEXT NOT NULL REFERENCES attempts(id),
    resource_id TEXT NOT NULL,
    kind        TEXT NOT NULL CHECK (kind IN ('backend','group')),
    status      TEXT NOT NULL DEFAULT 'active'
                CHECK (status IN ('active','released','quarantined')),
    acquired_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    released_at TEXT,
    PRIMARY KEY (attempt_id, resource_id, kind)
);
CREATE INDEX IF NOT EXISTS idx_reservations_resource ON reservations(resource_id, status);

CREATE TABLE IF NOT EXISTS events (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    subject_type TEXT NOT NULL,
    subject_id   TEXT NOT NULL,
    event        TEXT NOT NULL,
    detail_json  TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_events_subject ON events(subject_type, subject_id, id);
CREATE INDEX IF NOT EXISTS idx_events_ts ON events(ts);
