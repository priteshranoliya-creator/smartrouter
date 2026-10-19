import {
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  Chart,
  DoughnutController,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  ScatterController,
  Tooltip,
} from "chart.js";
import type { ChartConfiguration, ChartOptions, ChartType } from "chart.js";
import { useEffect, useRef } from "preact/hooks";
import type { Theme } from "./types";

Chart.register(
  ArcElement,
  BarController,
  BarElement,
  CategoryScale,
  DoughnutController,
  Legend,
  LineController,
  LineElement,
  LinearScale,
  PointElement,
  ScatterController,
  Tooltip,
);
// Live data redraws charts often; animating every refresh flickers and leaves half-drawn frames.
Chart.defaults.animation = false;

export interface Palette {
  fg: string;
  muted: string;
  border: string;
  accent: string;
  warn: string;
  series: string[];
}

function cssVar(node: Element, name: string): string {
  return getComputedStyle(node).getPropertyValue(name).trim();
}

export function palette(node: Element): Palette {
  return {
    fg: cssVar(node, "--fg"),
    muted: cssVar(node, "--muted"),
    border: cssVar(node, "--border"),
    accent: cssVar(node, "--accent"),
    warn: cssVar(node, "--warn") || cssVar(node, "--c4"),
    series: ["--c1", "--c2", "--c3", "--c4", "--c5", "--c6"].map((name) => cssVar(node, name)),
  };
}

type Scales = NonNullable<ChartOptions<"bar">["scales"]>;

export function axes(c: Palette, extra: Record<string, unknown>): Scales {
  return {
    x: { ticks: { color: c.muted, maxRotation: 0, autoSkip: true }, grid: { display: false }, border: { color: c.border } },
    y: { beginAtZero: true, ticks: { color: c.muted }, grid: { color: c.border }, border: { display: false }, ...extra },
  };
}

/** Widen a chart-type-specific config to the shape ChartView passes around. */
export function config<T extends ChartType>(c: ChartConfiguration<T>): ChartConfiguration {
  return c as unknown as ChartConfiguration;
}

interface ChartProps {
  label: string;
  theme: Theme;
  empty: boolean;
  emptyText: string;
  tall?: boolean;
  build: (colors: Palette) => ChartConfiguration;
}

function ChartInner({ label, empty, emptyText, tall, build }: Omit<ChartProps, "theme">) {
  const canvas = useRef<HTMLCanvasElement>(null);
  const chart = useRef<Chart | null>(null);

  useEffect(() => {
    const node = canvas.current;
    if (!node) return;
    if (empty) {
      chart.current?.destroy();
      chart.current = null;
      return;
    }
    const next = build(palette(node));
    if (chart.current) {
      chart.current.data = next.data;
      chart.current.options = next.options ?? {};
      chart.current.update("none");
    } else {
      chart.current = new Chart(node, next);
    }
  });

  useEffect(
    () => () => {
      chart.current?.destroy();
      chart.current = null;
    },
    [],
  );

  return (
    <div class={"chart-wrap" + (tall ? " tall" : "")}>
      <canvas ref={canvas} aria-label={label} role="img" style={{ visibility: empty ? "hidden" : "visible" }} />
      <p class="empty" hidden={!empty}>
        {emptyText}
      </p>
    </div>
  );
}

/** A Chart.js canvas that is rebuilt from scratch when the theme changes, so it re-reads the CSS colors. */
export function ChartView(props: ChartProps) {
  const { theme, ...rest } = props;
  return <ChartInner key={theme} {...rest} />;
}
