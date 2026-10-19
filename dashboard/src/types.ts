// Response shapes of the Python API (api/dashboard.py, telemetry/stats.py, eval/report.py).

export type Mode = "off" | "shadow" | "active";
export type WindowName = "15m" | "1h" | "24h" | "7d" | "all";
export type Theme = "dark" | "light";

export interface ModelStats {
  model: string;
  turns: number;
  requests: number;
  spend_usd: number;
  input_tokens: number;
  cache_read_tokens: number;
  cache_write_tokens: number;
  cache_hit_ratio: number;
}

export interface TimeseriesPoint {
  bucket: string;
  spend_usd: number;
  without_router_usd: number;
  with_router_usd: number;
  cache_write_tokens: number;
}

export interface SwitchLedgerRow {
  turn_id: number;
  session_id: string;
  seq: number;
  created_at: string;
  from_model: string | null;
  to_model: string;
  est_switch_tax_usd: number | null;
  est_saving_usd: number | null;
  realised_saving_usd: number;
}

export interface Stats {
  window: WindowName;
  since: string | null;
  sessions: number;
  turns: number;
  requests: number;
  spend_usd: number;
  without_router_usd: number;
  with_router_usd: number;
  saving_usd: number;
  cache_hit_ratio: number | null;
  switches: number;
  decisions_by_rule: Record<string, number>;
  timeouts: number;
  low_confidence: number;
  engine_latency_ms: { p50: number | null; p95: number | null };
  tier_probability_histogram: number[];
  by_model: ModelStats[];
  timeseries: TimeseriesPoint[];
  switch_ledger: SwitchLedgerRow[];
}

export interface SessionSummary {
  id: string;
  project_path: string;
  started_at: string;
  last_request_at: string | null;
  current_model: string | null;
  state: string;
  turns: number;
  requests: number;
  cost_usd: number;
  without_router_usd: number;
  with_router_usd: number;
  saving_usd: number;
}

export interface RequestJson {
  id: number | null;
  turn_id: number | null;
  session_id: string | null;
  kind: "main" | "background" | "compaction" | "subagent" | "eval";
  requested_model: string;
  sent_model: string;
  provider: string;
  input_tokens: number | null;
  output_tokens: number | null;
  cache_read_tokens: number | null;
  cache_write_5m_tokens: number | null;
  cache_write_1h_tokens: number | null;
  cost_usd: number;
  counterfactual_cost_usd: number | null;
  latency_ms: number | null;
  ttft_ms: number | null;
  status: number | null;
  error: string | null;
  created_at: string;
  without_router_usd: number;
  with_router_usd: number;
}

export interface QuestionAnswer {
  choice: string;
  probabilities: Record<string, number>;
}

export interface DecisionJson {
  engine: string;
  answers: Record<string, QuestionAnswer | null>;
  engine_latency_ms: number | null;
  desired_model: string | null;
  chosen_model: string;
  rule: string;
  mode: string;
  est_switch_tax_usd: number | null;
  est_saving_usd: number | null;
  tier_probability: number | null;
}

export interface TurnJson {
  id: number;
  seq: number;
  prompt: string | null;
  override: string | null;
  created_at: string;
  decision: DecisionJson | null;
  requests: RequestJson[];
}

export interface EvalRun {
  id: number;
  dataset_version: string;
  arms: string[];
  judge_model: string;
  started_at: string;
  finished_at: string | null;
}

export interface ArmSummary {
  arm: string;
  n: number;
  scored: number;
  errors: number;
  mean_score: number | null;
  pass_rate: number | null;
  total_cost_usd: number;
  mean_cost_usd: number | null;
  mean_latency_ms: number | null;
  rejudged: number;
  judge_agreement: number | null;
}

export interface EvalRunListItem extends EvalRun {
  n_items: number;
  summary: ArmSummary[];
}

export interface QualityCostPoint {
  arm: string;
  total_cost_usd: number;
  mean_score: number;
}

export interface Routing {
  tiers: string[];
  n: number;
  accuracy: number | null;
  /** [expected][predicted] */
  confusion: Record<string, Record<string, number>>;
}

export interface Agreement {
  n: number;
  rate: number | null;
}

export type Verdict = "hit" | "miss" | "wrong";

export interface JudgePoint {
  index: number;
  verdict: Verdict;
  reason: string;
}

export interface Rubric {
  judge?: { model: string; score: number; pass: boolean; points: JudgePoint[] };
  rejudge?: { model: string; pass?: boolean; score?: number; points?: JudgePoint[]; error?: string };
}

export interface ItemArm {
  model_used: string;
  answer: string;
  score: number | null;
  passed: boolean | null;
  rubric: Rubric | null;
  cost_usd: number | null;
  latency_ms: number | null;
  error: string | null;
}

export interface ItemRow {
  item_id: string;
  expected_tier: string | null;
  predicted_tier: string | null;
  arms: Record<string, ItemArm>;
}

export interface EvalRunDetail {
  run: EvalRun;
  arms: ArmSummary[];
  quality_cost: QualityCostPoint[];
  routing: Routing;
  agreement: Agreement;
  items: ItemRow[];
}
