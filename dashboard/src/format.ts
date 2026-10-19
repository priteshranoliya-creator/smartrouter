import type { DecisionJson, RequestJson, TurnJson, WindowName } from "./types";

export function usd(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  const abs = Math.abs(value);
  const text = abs !== 0 && abs < 0.01 ? abs.toFixed(4) : abs.toFixed(2);
  return (value < 0 ? "-$" : "$") + text;
}

export function pct(ratio: number | null | undefined): string {
  return ratio === null || ratio === undefined ? "n/a" : (ratio * 100).toFixed(0) + "%";
}

export function ms(value: number | null | undefined): string {
  if (value === null || value === undefined) return "n/a";
  return value >= 1000 ? (value / 1000).toFixed(2) + " s" : value.toFixed(0) + " ms";
}

export function shortModel(model: string | null | undefined): string {
  return model ? model.replace(/^claude-/, "") : "n/a";
}

export function basename(path: string | null | undefined): string {
  if (!path) return "unknown";
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.length ? (parts[parts.length - 1] as string) : path;
}

export function snippet(text: string | null | undefined, max: number): string {
  const flat = (text || "").replace(/\s+/g, " ").trim();
  return flat.length > max ? flat.slice(0, max - 1) + "…" : flat;
}

export function clock(iso: string): string {
  const when = new Date(iso);
  if (Number.isNaN(when.getTime())) return "";
  const same = when.toDateString() === new Date().toDateString();
  const hms = when.toLocaleTimeString([], { hour12: false });
  return same ? hms : when.toLocaleDateString([], { month: "short", day: "numeric" }) + " " + hms.slice(0, 5);
}

export function bucketLabel(bucket: string, hourly: boolean): string {
  const when = new Date(bucket);
  return hourly
    ? when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", hour12: false })
    : when.toLocaleDateString([], { month: "short", day: "numeric" });
}

export function isHourly(windowName: WindowName): boolean {
  return windowName === "15m" || windowName === "1h" || windowName === "24h";
}

export function cumulative(series: number[]): number[] {
  let running = 0;
  return series.map((value) => (running += value));
}

export function mainRequest(turn: TurnJson): RequestJson | null {
  return turn.requests.find((r) => r.kind === "main") || turn.requests[0] || null;
}

export function routingAnswer(decision: DecisionJson | null): string | null {
  if (!decision) return null;
  const first = Object.values(decision.answers || {})[0];
  const choice = first ? first.choice : null;
  if (choice === null || choice === undefined) return null;
  return decision.tier_probability === null || decision.tier_probability === undefined
    ? String(choice)
    : choice + " " + pct(decision.tier_probability);
}
