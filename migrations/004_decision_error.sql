-- decision_error: the engine failed (tdd-changes #34). SQLite cannot change a CHECK, so rebuild the table.
CREATE TABLE decisions_new (
    turn_id INTEGER PRIMARY KEY REFERENCES turns(id),
    engine TEXT NOT NULL,
    answers TEXT NOT NULL,
    engine_latency_ms REAL,
    desired_model TEXT,
    chosen_model TEXT NOT NULL,
    rule TEXT NOT NULL CHECK (rule IN (
        'override', 'low_confidence', 'same_tier', 'free_window',
        'upgrade', 'cooldown', 'payback', 'decision_timeout', 'decision_error'
    )),
    mode TEXT NOT NULL CHECK (mode IN ('off', 'shadow', 'active')),
    est_switch_tax_usd REAL,
    est_saving_usd REAL
);
INSERT INTO decisions_new SELECT * FROM decisions;
DROP TABLE decisions;
ALTER TABLE decisions_new RENAME TO decisions;
