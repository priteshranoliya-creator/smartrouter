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

const LABEL_GAP = 10;
const LABEL_HEIGHT = 14;

interface LabelBox {
  arm: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

const overlaps = (a: LabelBox, b: LabelBox) => a.x < b.x + b.w && b.x < a.x + a.w && a.y < b.y + b.h && b.y < a.y + a.h;

/** Arms with near-equal cost and score land on one spot, so a label that hits another label or marker steps down until it is clear. */
const armLabels: Plugin<"scatter"> = {
  id: "armLabels",
  afterDatasetsDraw(chart) {
    const { ctx, chartArea } = chart;
    ctx.save();
    ctx.font = "12px Inter, system-ui, sans-serif";
    ctx.fillStyle = getComputedStyle(chart.canvas).getPropertyValue("--fg").trim();
    ctx.textBaseline = "middle";
    const placed: LabelBox[] = [];
    const points = chart.data.datasets.flatMap((dataset, index) =>
      chart.isDatasetVisible(index)
        ? chart.getDatasetMeta(index).data.map((element, i) => ({
            element,
            point: dataset.data[i] as unknown as ArmPoint,
            radius: (element.options as { radius?: number }).radius ?? 6,
          }))
        : [],
    );
    const markers = points.map(({ element, point, radius }): LabelBox => ({ arm: point.arm, x: element.x - radius, y: element.y - radius, w: 2 * radius, h: 2 * radius }));
    points.sort((a, b) => a.element.y - b.element.y || a.element.x - b.element.x);
    for (const { element, point } of points) {
      const w = ctx.measureText(point.arm).width;
      const fits = (side: "right" | "left") => (side === "right" ? element.x + LABEL_GAP + w <= chartArea.right : element.x - LABEL_GAP - w >= chartArea.left);
      const at = (side: "right" | "left", y: number): LabelBox => ({
        arm: point.arm,
        x: side === "right" ? element.x + LABEL_GAP : element.x - LABEL_GAP - w,
        y,
        w,
        h: LABEL_HEIGHT,
      });
      const blocked = (box: LabelBox) => placed.some((other) => overlaps(box, other)) || markers.some((m) => m.arm !== point.arm && overlaps(box, m));
      const home = element.y - LABEL_HEIGHT / 2;
      const preferred = fits("right") ? "right" : "left";
      const flipped = preferred === "right" ? "left" : "right";
      let box = at(preferred, home);
      if (blocked(box) && fits(flipped) && !blocked(at(flipped, home))) box = at(flipped, home);
      while (blocked(box) && box.y < chartArea.bottom - LABEL_HEIGHT) box = at(box.x > element.x ? "right" : "left", box.y + LABEL_HEIGHT);
      placed.push(box);
      const onLeft = box.x < element.x;
      ctx.textAlign = onLeft ? "right" : "left";
      ctx.fillText(point.arm, onLeft ? box.x + w : box.x, box.y + LABEL_HEIGHT / 2);
    }
    ctx.restore();
    chart.canvas.dataset["labelBoxes"] = JSON.stringify(placed);
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
