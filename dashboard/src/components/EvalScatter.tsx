import type { Plugin } from "chart.js";
import { ChartView, config } from "../charts";
import { usd } from "../format";
import type { QualityCostPoint, Theme } from "../types";

interface ArmPoint {
  x: number;
  y: number;
  arm: string;
}

const toPoint = (p: QualityCostPoint): ArmPoint => ({ x: p.total_cost_usd, y: p.mean_score, arm: p.arm });

const armLabels: Plugin<"scatter"> = {
  id: "armLabels",
  afterDatasetsDraw(chart) {
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.font = "12px Inter, system-ui, sans-serif";
    ctx.fillStyle = getComputedStyle(chart.canvas).getPropertyValue("--fg").trim();
    ctx.textBaseline = "middle";
    chart.data.datasets.forEach((dataset, index) => {
      chart.getDatasetMeta(index).data.forEach((element, i) => {
        const point = dataset.data[i] as unknown as ArmPoint;
        const left = element.x + 10 + ctx.measureText(point.arm).width > chartArea.right;
        ctx.textAlign = left ? "right" : "left";
        ctx.fillText(point.arm, element.x + (left ? -10 : 10), element.y);
      });
    });
    ctx.restore();
  },
};

/** One labelled point per arm: x is total cost, y is mean judge score. The router and oracle get their own marker. */
export function EvalScatter({ points, theme }: { points: QualityCostPoint[]; theme: Theme }) {
  return (
    <ChartView
      tall
      theme={theme}
      label="Mean judge score against total cost, one point per arm"
      empty={points.length === 0}
      emptyText="No scored arms in this run"
      build={(c) =>
        config<"scatter">({
          type: "scatter",
          data: {
            datasets: [
              {
                label: "Model arms",
                data: points.filter((p) => p.arm !== "router" && p.arm !== "oracle").map(toPoint),
                pointStyle: "circle",
                pointRadius: 6,
                backgroundColor: c.series[2] as string,
                borderColor: c.series[2] as string,
              },
              {
                label: "Router",
                data: points.filter((p) => p.arm === "router").map(toPoint),
                pointStyle: "rectRot",
                pointRadius: 9,
                backgroundColor: c.accent,
                borderColor: c.accent,
              },
              {
                label: "Oracle",
                data: points.filter((p) => p.arm === "oracle").map(toPoint),
                pointStyle: "triangle",
                pointRadius: 8,
                backgroundColor: c.warn,
                borderColor: c.warn,
              },
            ],
          },
          options: {
            responsive: true,
            maintainAspectRatio: false,
            plugins: {
              legend: { labels: { color: c.fg, boxWidth: 12, usePointStyle: true } },
              tooltip: {
                callbacks: {
                  label: (ctx) => {
                    const p = ctx.raw as ArmPoint;
                    return p.arm + ": score " + p.y.toFixed(2) + ", cost " + usd(p.x);
                  },
                },
              },
            },
            scales: {
              x: {
                type: "linear",
                beginAtZero: true,
                grace: "12%",
                title: { display: true, text: "Total cost (USD)", color: c.muted },
                ticks: { color: c.muted, callback: (v) => usd(Number(v)) },
                grid: { color: c.border },
                border: { color: c.border },
              },
              y: {
                type: "linear",
                min: 0,
                max: 1.05,
                title: { display: true, text: "Mean judge score", color: c.muted },
                ticks: { color: c.muted, includeBounds: false, callback: (v) => Number(v).toFixed(1) },
                grid: { color: c.border },
                border: { display: false },
              },
            },
          },
          plugins: [armLabels],
        })
      }
    />
  );
}
