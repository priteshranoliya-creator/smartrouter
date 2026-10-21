import { ms } from "../format";
import type { Stats } from "../types";
import { Card } from "./Card";
import { IconTarget } from "./icons";

export function QualityPanel({ stats }: { stats: Stats | null }) {
  const total = stats ? Object.values(stats.decisions_by_rule).reduce((n, c) => n + c, 0) : 0;
  const low = stats ? stats.low_confidence : 0;
  const timeouts = stats ? stats.timeouts : 0;
  const errors = stats ? (stats.decisions_by_rule["decision_error"] ?? 0) : 0;
  const verdict =
    !stats || total === 0
      ? "No decisions in this window yet."
      : `${low} of ${total} decisions were low confidence, ${timeouts} timed out and ${errors} failed with an engine error.`;
  return (
    <Card
      id="panel-quality"
      wide
      title="Decision quality"
      subtitle="Is the engine that picks the model fast and sure enough to trust?"
      icon={<IconTarget />}
    >
      <div class="kpis four">
        <div title="95 out of 100 answered decisions arrive faster than this (timeouts and errors have no latency)">
          <span>Slowest typical answer</span>
          <b id="q-p95">{ms(stats ? stats.engine_latency_ms.p95 : null)}</b>
        </div>
        <div title="Decisions where the engine was not confident in its answer">
          <span>Unsure decisions</span>
          <b id="q-low">{low}</b>
        </div>
        <div title="Decisions the engine did not answer in time">
          <span>Timeouts</span>
          <b id="q-timeouts">{timeouts}</b>
        </div>
        <div title="Decisions where the engine call failed (not a timeout); the router fell back like a timeout">
          <span>Engine errors</span>
          <b id="q-errors">{errors}</b>
        </div>
      </div>
      <p class="sub-label">{verdict}</p>
    </Card>
  );
}
