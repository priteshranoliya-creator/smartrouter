import { ChartView, axes, config } from "../charts";
import { bucketLabel, isHourly, shortModel } from "../format";
import type { Stats, Theme } from "../types";
import { Card } from "./Card";
import { IconDatabase } from "./icons";

export function CachePanel({ stats, theme }: { stats: Stats | null; theme: Theme }) {
  const models = stats ? stats.by_model : [];
  const series = stats ? stats.timeseries : [];
  const hourly = isHourly(stats ? stats.window : "all");
  return (
    <Card id="panel-cache" title="Cache health" subtitle="Prompt cache reads and writes" icon={<IconDatabase />}>
      <p class="sub-label">Cache hit ratio per model</p>
      <ChartView
        theme={theme}
        label="Cache hit ratio per model"
        empty={models.length === 0}
        emptyText="No requests in this window"
        build={(c) =>
          config<"bar">({
            type: "bar",
            data: {
              labels: models.map((m) => shortModel(m.model)),
              datasets: [
                {
                  label: "Cache hit",
                  data: models.map((m) => m.cache_hit_ratio * 100),
                  backgroundColor: c.series[1] as string,
                  borderRadius: 6,
                  maxBarThickness: 48,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              plugins: { legend: { display: false }, tooltip: { callbacks: { label: (ctx) => (ctx.parsed.y as number).toFixed(0) + "%" } } },
              scales: axes(c, { max: 100, ticks: { color: c.muted, callback: (v: string | number) => v + "%" } }),
            },
          })
        }
      />
      <p class="sub-label">Cache write tokens over time</p>
      <ChartView
        theme={theme}
        label="Cache write tokens over time"
        empty={!series.some((p) => p.cache_write_tokens > 0)}
        emptyText="No cache writes in this window"
        build={(c) =>
          config<"bar">({
            type: "bar",
            data: {
              labels: series.map((p) => bucketLabel(p.bucket, hourly)),
              datasets: [
                {
                  label: "Cache write tokens",
                  data: series.map((p) => p.cache_write_tokens),
                  backgroundColor: c.series[2] as string,
                  borderRadius: 6,
                  maxBarThickness: 48,
                },
              ],
            },
            options: { responsive: true, maintainAspectRatio: false, plugins: { legend: { display: false } }, scales: axes(c, {}) },
          })
        }
      />
    </Card>
  );
}
