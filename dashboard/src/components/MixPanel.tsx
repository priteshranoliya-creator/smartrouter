import { ChartView, config } from "../charts";
import { shortModel, usd } from "../format";
import type { Stats, Theme } from "../types";
import { Card } from "./Card";
import { IconPie } from "./icons";

export function MixPanel({ stats, theme }: { stats: Stats | null; theme: Theme }) {
  const models = stats ? stats.by_model : [];
  return (
    <Card id="panel-mix" title="Model mix" subtitle="Which models your money went to" icon={<IconPie />}>
      <ChartView
        tall
        theme={theme}
        label="Spend per model"
        empty={models.length === 0}
        emptyText="No requests in this window"
        build={(c) =>
          config<"doughnut">({
            type: "doughnut",
            data: {
              labels: models.map((m) => shortModel(m.model)),
              datasets: [
                {
                  data: models.map((m) => m.spend_usd),
                  backgroundColor: models.map((_, i) => c.series[(i + 1) % c.series.length] as string),
                  borderColor: c.border,
                  borderWidth: 2,
                },
              ],
            },
            options: {
              responsive: true,
              maintainAspectRatio: false,
              cutout: "62%",
              plugins: {
                legend: { position: "right", labels: { color: c.fg, boxWidth: 12, usePointStyle: true } },
                tooltip: { callbacks: { label: (ctx) => ctx.label + ": " + usd(ctx.parsed) } },
              },
            },
          })
        }
      />
    </Card>
  );
}
