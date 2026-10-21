import { ChartView, axes, config } from "../charts";
import { ms, pct, shortModel, usd } from "../format";
import type { Stats, Theme } from "../types";
import { Card } from "./Card";
import { IconActivity, IconPie, IconTarget } from "./icons";

export function CountersPanel({ stats, engine }: { stats: Stats | null; engine: string | null }) {
  return (
    <Card id="panel-counters" title="Raw counters" subtitle="Plain totals behind the headline numbers" icon={<IconActivity />}>
      <div class="kpis four">
        <div>
          <span>Sessions</span>
          <b id="tile-sessions">{stats ? stats.sessions : 0}</b>
        </div>
        <div>
          <span>Requests</span>
          <b id="tile-requests">{stats ? stats.requests : 0}</b>
        </div>
        <div>
          <span>Cache hit</span>
          <b id="tile-cache">{stats ? pct(stats.cache_hit_ratio) : "0%"}</b>
        </div>
        <div>
          <span>Engine</span>
          <b class="small-b" id="tile-engine">{engine ?? "n/a"}</b>
        </div>
      </div>
      <p class="sub-label">A turn is one prompt from you. A request is one call to the model, and a turn can make several.</p>
    </Card>
  );
}

export function ModelTablePanel({ stats }: { stats: Stats | null }) {
  const models = stats ? stats.by_model : [];
  return (
    <Card id="panel-models" title="Per-model counts" subtitle="Turns, requests and spend for each model that ran" icon={<IconPie />}>
      <div class="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Model</th>
              <th class="num">Turns</th>
              <th class="num">Requests</th>
              <th class="num">Spend</th>
            </tr>
          </thead>
          <tbody id="mix-body">
            {models.map((m) => (
              <tr key={m.model}>
                <td>{shortModel(m.model)}</td>
                <td class="num">{m.turns}</td>
                <td class="num">{m.requests}</td>
                <td class="num">{usd(m.spend_usd)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p class="empty" hidden={models.length > 0}>
        No requests in this window
      </p>
    </Card>
  );
}

export function EnginePanel({ stats, theme }: { stats: Stats | null; theme: Theme }) {
  const bins = stats ? stats.tier_probability_histogram : [];
  const rules = stats ? Object.entries(stats.decisions_by_rule).sort((a, b) => b[1] - a[1]) : [];
  return (
    <Card
      id="panel-engine"
      title="Decision engine detail"
      subtitle="How sure the engine was, and which routing rule made each call"
      icon={<IconTarget />}
    >
      <div class="kpis two">
        <div>
          <span>Typical response time (p50)</span>
          <b id="q-p50">{ms(stats ? stats.engine_latency_ms.p50 : null)}</b>
        </div>
      </div>
      <p class="sub-label">Engine confidence in its chosen answer (decisions per band)</p>
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
      <p class="sub-label">Routing rules that fired (rule name and count)</p>
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
