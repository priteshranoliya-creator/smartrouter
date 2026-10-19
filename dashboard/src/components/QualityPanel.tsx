import { ChartView, axes, config } from "../charts";
import { ms } from "../format";
import type { Stats, Theme } from "../types";
import { Card } from "./Card";
import { IconTarget } from "./icons";

export function QualityPanel({ stats, theme }: { stats: Stats | null; theme: Theme }) {
  const bins = stats ? stats.tier_probability_histogram : [];
  const rules = stats ? Object.entries(stats.decisions_by_rule).sort((a, b) => b[1] - a[1]) : [];
  return (
    <Card
      id="panel-quality"
      title="Decision quality"
      subtitle="Engine latency, confidence and rules fired"
      icon={<IconTarget />}
    >
      <div class="kpis four">
        <div>
          <span>Latency p50</span>
          <b id="q-p50">{ms(stats ? stats.engine_latency_ms.p50 : null)}</b>
        </div>
        <div>
          <span>Latency p95</span>
          <b id="q-p95">{ms(stats ? stats.engine_latency_ms.p95 : null)}</b>
        </div>
        <div>
          <span>Low confidence</span>
          <b id="q-low">{stats ? stats.low_confidence : 0}</b>
        </div>
        <div>
          <span>Timeouts</span>
          <b id="q-timeouts">{stats ? stats.timeouts : 0}</b>
        </div>
      </div>
      <p class="sub-label">Tier probability of the chosen answer</p>
      <ChartView
        theme={theme}
        label="Tier probability histogram"
        empty={!bins.some((n) => n > 0)}
        emptyText="No decisions in this window"
        build={(c) =>
          config<"bar">({
            type: "bar",
            data: {
              labels: bins.map((_, i) => i * 10 + "–" + (i + 1) * 10 + "%"),
              datasets: [{ label: "Decisions", data: bins, backgroundColor: c.series[3] as string, borderRadius: 4 }],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: { legend: { display: false } },
              scales: {
                ...axes(c, { ticks: { color: c.muted, precision: 0 } }),
                x: { ...axes(c, {}).x, ticks: { color: c.muted, maxRotation: 0, autoSkip: true, maxTicksLimit: 5 } },
              },
            },
          })
        }
      />
      <div class="badges" id="q-rules">
        {rules.map(([rule, count]) => (
          <span class="badge" key={rule}>
            {rule}
            <b>{count}</b>
          </span>
        ))}
      </div>
    </Card>
  );
}
