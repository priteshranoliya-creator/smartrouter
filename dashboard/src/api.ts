import type {
  EvalRunDetail,
  EvalRunListItem,
  Mode,
  SessionSummary,
  Stats,
  TurnJson,
  WindowName,
} from "./types";

async function getJSON<T>(url: string): Promise<T> {
  const res = await fetch(url, { headers: { accept: "application/json" } });
  const body: unknown = await res.json().catch(() => null);
  if (!res.ok) {
    const message = body !== null && typeof body === "object" && "error" in body ? String(body.error) : "";
    throw new Error(message || res.statusText);
  }
  return body as T;
}

export const fetchStats = (window: WindowName) =>
  getJSON<Stats>("/api/stats?window=" + encodeURIComponent(window));

export const fetchSessions = () => getJSON<SessionSummary[]>("/api/sessions");

export const fetchTurns = (sessionId: string) =>
  getJSON<TurnJson[]>("/api/sessions/" + encodeURIComponent(sessionId) + "/turns");

export const fetchMode = async (): Promise<Mode> => (await getJSON<{ mode: Mode }>("/api/mode")).mode;

export const fetchEvalRuns = () => getJSON<EvalRunListItem[]>("/api/eval/runs");

export const fetchEvalRun = (id: number) => getJSON<EvalRunDetail>("/api/eval/runs/" + id);

/** Resolves to the new mode, or rejects with the server's message. */
export async function putMode(mode: Mode): Promise<Mode> {
  const res = await fetch("/api/mode", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ mode }),
  });
  const body: { mode?: Mode; error?: string } = await res.json().catch(() => ({}));
  if (!res.ok || body.mode === undefined) throw new Error(body.error || "Mode change failed (" + res.status + ")");
  return body.mode;
}
