CREATE TABLE sessions (
    id TEXT PRIMARY KEY,
    project_path TEXT NOT NULL,
    started_at TEXT NOT NULL,
    current_model TEXT,
    state TEXT NOT NULL DEFAULT 'active' CHECK (state IN ('active', 'compacting', 'ended')),
    last_request_at TEXT,
    last_switch_turn INTEGER
);

CREATE TABLE session_models (
    session_id TEXT NOT NULL REFERENCES sessions(id),
    model TEXT NOT NULL,
    last_used_at TEXT NOT NULL,
    prefix_tokens INTEGER,
    PRIMARY KEY (session_id, model)
);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL REFERENCES sessions(id),
    seq INTEGER NOT NULL,
    prompt_id TEXT,
    prompt TEXT NOT NULL,
    override TEXT CHECK (override IS NULL OR override IN ('opus', 'sonnet', 'haiku', 'auto')),
    created_at TEXT NOT NULL,
    UNIQUE (session_id, seq)
);

CREATE TABLE decisions (
    turn_id INTEGER PRIMARY KEY REFERENCES turns(id),
    engine TEXT NOT NULL,
    answers TEXT NOT NULL,
    engine_latency_ms REAL,
    desired_model TEXT,
    chosen_model TEXT NOT NULL,
    rule TEXT NOT NULL CHECK (rule IN (
        'override', 'low_confidence', 'same_tier', 'free_window',
        'upgrade', 'cooldown', 'payback', 'decision_timeout'
    )),
    mode TEXT NOT NULL CHECK (mode IN ('off', 'shadow', 'active')),
    est_switch_tax_usd REAL,
    est_saving_usd REAL
);

CREATE TABLE requests (
    id INTEGER PRIMARY KEY,
    turn_id INTEGER REFERENCES turns(id),
    session_id TEXT,
    kind TEXT NOT NULL CHECK (kind IN ('main', 'background', 'compaction', 'subagent', 'eval')),
    requested_model TEXT NOT NULL,
    sent_model TEXT NOT NULL,
    provider TEXT NOT NULL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_5m_tokens INTEGER,
    cache_write_1h_tokens INTEGER,
    cost_usd REAL NOT NULL,
    counterfactual_cost_usd REAL,
    latency_ms REAL,
    ttft_ms REAL,
    status INTEGER,
    error TEXT,
    created_at TEXT NOT NULL
);

CREATE TABLE compactions (
    id INTEGER PRIMARY KEY,
    session_id TEXT NOT NULL,
    trigger TEXT NOT NULL CHECK (trigger IN ('manual', 'auto')),
    prefix_tokens_before INTEGER,
    created_at TEXT NOT NULL
);

CREATE TABLE eval_runs (
    id INTEGER PRIMARY KEY,
    dataset_version TEXT NOT NULL,
    arms TEXT NOT NULL,
    judge_model TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT
);

CREATE TABLE eval_results (
    id INTEGER PRIMARY KEY,
    run_id INTEGER NOT NULL REFERENCES eval_runs(id),
    item_id TEXT NOT NULL,
    arm TEXT NOT NULL,
    model_used TEXT NOT NULL,
    answer TEXT NOT NULL,
    judge_score REAL CHECK (judge_score IS NULL OR judge_score BETWEEN 0 AND 1),
    rubric_hits TEXT,
    cost_usd REAL,
    latency_ms REAL,
    expected_tier TEXT,
    predicted_tier TEXT
);

CREATE INDEX idx_requests_session_created ON requests(session_id, created_at);
CREATE INDEX idx_requests_turn ON requests(turn_id);
CREATE INDEX idx_turns_session_seq ON turns(session_id, seq);
CREATE INDEX idx_eval_results_run_arm ON eval_results(run_id, arm);
