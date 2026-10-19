import { useCallback, useEffect, useRef, useState } from "preact/hooks";
import * as api from "./api";
import type { EvalRunListItem, Mode, SessionSummary, Stats, Theme, TurnJson, WindowName } from "./types";

export const FEED_SESSIONS = 10;
const FEED_DEBOUNCE_MS = 300;
const STATS_DEBOUNCE_MS = 500;
const RECONNECT_MS = 2000;
const EVENT_TYPES = ["turn", "decision", "request", "compaction"];
const THEME_KEY = "smartrouter-theme";

export function useTheme(): [Theme, () => void] {
  const [theme, setTheme] = useState<Theme>(() =>
    document.documentElement.dataset["theme"] === "light" ? "light" : "dark",
  );
  const toggle = () => {
    const next: Theme = theme === "dark" ? "light" : "dark";
    document.documentElement.dataset["theme"] = next;
    try {
      localStorage.setItem(THEME_KEY, next);
    } catch {
      /* storage unavailable */
    }
    setTheme(next);
  };
  return [theme, toggle];
}

function useDebounced(fn: () => void, wait: number): () => void {
  const latest = useRef(fn);
  latest.current = fn;
  const timer = useRef<number | undefined>(undefined);
  useEffect(() => () => window.clearTimeout(timer.current), []);
  return useCallback(() => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => latest.current(), wait);
  }, [wait]);
}

export function useStats(windowName: WindowName): { stats: Stats | null; refresh: () => void; schedule: () => void } {
  const [stats, setStats] = useState<Stats | null>(null);
  const current = useRef(windowName);
  current.current = windowName;
  const refresh = useCallback(() => {
    const asked = current.current;
    api
      .fetchStats(asked)
      .then((next) => {
        if (asked === current.current) setStats(next);
      })
      .catch(() => {
        /* keep the last good render */
      });
  }, []);
  const schedule = useDebounced(refresh, STATS_DEBOUNCE_MS);
  useEffect(refresh, [windowName, refresh]);
  return { stats, refresh, schedule };
}

export interface Feed {
  sessions: Map<string, SessionSummary>;
  turns: Map<string, TurnJson[]>;
}

/** The newest sessions' turns. `mark` queues a refresh of one session (null: all recent), flushed after a pause. */
export function useFeed(): { feed: Feed; markSession: (id: string | null) => void } {
  const [feed, setFeed] = useState<Feed>({ sessions: new Map(), turns: new Map() });
  const pending = useRef({ ids: new Set<string>(), all: true });

  const flush = useCallback(async () => {
    const { ids, all } = pending.current;
    const targets = [...ids];
    pending.current = { ids: new Set(), all: false };
    try {
      const list = await api.fetchSessions();
      const wanted = all ? list.slice(0, FEED_SESSIONS).map((s) => s.id) : targets;
      const fetched = await Promise.all(
        wanted.map(async (id): Promise<[string, TurnJson[] | null]> => {
          try {
            return [id, await api.fetchTurns(id)];
          } catch {
            return [id, null];
          }
        }),
      );
      setFeed((prev) => {
        const turns = new Map(prev.turns);
        for (const [id, rows] of fetched) {
          if (rows === null) turns.delete(id);
          else turns.set(id, rows);
        }
        return { sessions: new Map(list.map((s) => [s.id, s])), turns };
      });
    } catch {
      /* next event retries */
    }
  }, []);

  const schedule = useDebounced(() => void flush(), FEED_DEBOUNCE_MS);
  const markSession = useCallback(
    (id: string | null) => {
      if (id === null) pending.current.all = true;
      else pending.current.ids.add(id);
      schedule();
    },
    [schedule],
  );
  useEffect(() => {
    void flush();
  }, [flush]);
  return { feed, markSession };
}

/** Subscribes to /api/stream and reconnects after a closed connection. Returns whether it is connected. */
export function useStream(onOpen: () => void, onEvent: (sessionId: string | null) => void): boolean {
  const [live, setLive] = useState(false);
  const handlers = useRef({ onOpen, onEvent });
  handlers.current = { onOpen, onEvent };

  useEffect(() => {
    let source: EventSource | null = null;
    let retry: number | undefined;
    let stopped = false;

    const connect = () => {
      const next = new EventSource("/api/stream");
      source = next;
      next.onopen = () => {
        setLive(true);
        handlers.current.onOpen();
      };
      next.onerror = () => {
        setLive(false);
        if (next.readyState === EventSource.CLOSED && !stopped) retry = window.setTimeout(connect, RECONNECT_MS);
      };
      const onMessage = (message: MessageEvent<string>) => {
        let sessionId: string | null = null;
        try {
          const data: unknown = JSON.parse(message.data);
          if (data !== null && typeof data === "object" && "session_id" in data && data.session_id) {
            sessionId = String(data.session_id);
          }
        } catch {
          /* payload unreadable, refresh everything */
        }
        handlers.current.onEvent(sessionId);
      };
      for (const type of EVENT_TYPES) next.addEventListener(type, onMessage as EventListener);
    };

    connect();
    return () => {
      stopped = true;
      window.clearTimeout(retry);
      source?.close();
    };
  }, []);
  return live;
}

export function useMode(): { mode: Mode | null; message: string | null; change: (mode: Mode) => void } {
  const [mode, setMode] = useState<Mode | null>(null);
  const [message, setMessage] = useState<string | null>(null);
  useEffect(() => {
    api
      .fetchMode()
      .then(setMode)
      .catch(() => setMode(null));
  }, []);
  const change = (next: Mode) => {
    setMessage(null);
    api
      .putMode(next)
      .then(setMode)
      .catch((e: unknown) => setMessage(e instanceof Error ? e.message : String(e)));
  };
  return { mode, message, change };
}

export function useEvalRuns(): { runs: EvalRunListItem[] | null; reload: () => void } {
  const [runs, setRuns] = useState<EvalRunListItem[] | null>(null);
  const reload = useCallback(() => {
    api
      .fetchEvalRuns()
      .then(setRuns)
      .catch(() => setRuns((prev) => prev ?? []));
  }, []);
  useEffect(reload, [reload]);
  return { runs, reload };
}
