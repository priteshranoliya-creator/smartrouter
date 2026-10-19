import { ChartView, axes, config } from "../charts";
import { bucketLabel, cumulative, isHourly, usd } from "../format";
import type { Stats, Theme } from "../types";
import { Card } from "./Card";
import { IconTrend } from "./icons";

export function SpendPanel({ stats, theme }: { stats: Stats | null; theme: Theme }) {
  const series = stats ? stats.timeseries : [];
  return (
    <Card
      id="panel-spend"
      highlight
      title="Spend vs baseline"
      subtitle="Cumulative cost with and without routing"
      icon={<IconTrend />}
      meta={
        <span class="card-meta" id="spend-saving">
          {stats && stats.requests ? stats.requests + " requests" : ""}
        </span>
      }
    >
      <div class="kpis">
        <div>
          <span>Without router</span>
          <b id="spend-without">{usd(stats ? stats.without_router_usd : 0)}</b>
        </div>
        <div>
          <span>With router</span>
          <b id="spend-with">{usd(stats ? stats.with_router_usd : 0)}</b>
        </div>
        <div>
          <span>Saving</span>
          <b id="spend-delta">{usd(stats ? stats.saving_usd : 0)}</b>
        </div>
      </div>
      <ChartView
        tall
        theme={theme}
        label="Cumulative spend with and without the router"
        empty={series.length === 0}
        emptyText="No spend in this window"
        build={(c) =>
          config<"line">({
            type: "line",
            data: {
              labels: series.map((p) => bucketLabel(p.bucket, isHourly(stats ? stats.window : "all"))),
              datasets: [
                {
                  label: "Without router",
                  data: cumulative(series.map((p) => p.without_router_usd)),
                  borderColor: c.muted,
                  backgroundColor: c.muted,
                  borderDash: [5, 4],
                  pointRadius: 2,
                  tension: 0.25,
                },
                {
                  label: "With router",
                  data: cumulative(series.map((p) => p.with_router_usd)),
                  borderColor: c.accent,
                  backgroundColor: c.accent,
                  pointRadius: 2,
                  tension: 0.25,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              interaction: { mode: "index", intersect: false },
              plugins: {
                legend: { labels: { color: c.fg, boxWidth: 12, usePointStyle: true } },
                tooltip: { callbacks: { label: (ctx) => ctx.dataset.label + ": " + usd(ctx.parsed.y) } },
              },
              scales: axes(c, { ticks: { color: c.muted, callback: (v: string | number) => usd(Number(v)) } }),
            },
          })
        }
      />
    </Card>
  );
}
