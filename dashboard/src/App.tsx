import { useEffect, useMemo, useRef, useState } from "preact/hooks";
import { CachePanel } from "./components/CachePanel";
import { EvalPanel } from "./components/EvalPanel";
import { FeedPanel, visibleFeed } from "./components/FeedPanel";
import type { FeedItem } from "./components/FeedPanel";
import { Hero } from "./components/Hero";
import { LedgerPanel } from "./components/LedgerPanel";
import { MixPanel } from "./components/MixPanel";
import { QualityPanel } from "./components/QualityPanel";
import { Sidebar } from "./components/Sidebar";
import { SpendPanel } from "./components/SpendPanel";
import { Topbar } from "./components/Topbar";
import { useEvalRuns, useFeed, useMode, useStats, useStream, useTheme } from "./hooks";
import type { WindowName } from "./types";

export function App() {
  const [windowName, setWindowName] = useState<WindowName>("all");
  const [filter, setFilter] = useState("");
  const [theme, toggleTheme] = useTheme();
  const { stats, schedule: scheduleStats } = useStats(windowName);
  const { feed, markSession } = useFeed();
  const { mode, message, change } = useMode();
  const { runs, reload } = useEvalRuns();
  const searchRef = useRef<HTMLInputElement>(null);

  const live = useStream(
    () => {
      markSession(null);
      scheduleStats();
    },
    (sessionId) => {
      markSession(sessionId);
      scheduleStats();
    },
  );

  useEffect(() => {
    const onKey = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        searchRef.current?.focus();
        searchRef.current?.select();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, []);

  const items = useMemo(() => {
    const all: FeedItem[] = [];
    for (const [sessionId, turns] of feed.turns) {
      const session = feed.sessions.get(sessionId);
      for (const turn of turns) all.push({ turn, session });
    }
    return all.sort((a, b) => b.turn.created_at.localeCompare(a.turn.created_at));
  }, [feed]);
  const engine = items.find((i) => i.turn.decision && i.turn.decision.engine)?.turn.decision?.engine ?? null;

  const counts = {
    sessions: stats ? stats.sessions : 0,
    switches: stats ? stats.switches : 0,
    feed: visibleFeed(items, filter).length,
    ledger: stats ? stats.switch_ledger.length : 0,
    mix: stats ? stats.by_model.length : 0,
    quality: stats ? Object.values(stats.decisions_by_rule).reduce((n, c) => n + c, 0) : 0,
    evalRuns: runs ? runs.length : 0,
  };

  return (
    <div class="shell">
      <Sidebar searchRef={searchRef} onFilter={setFilter} counts={counts} mode={mode} modeMessage={message} onMode={change} />
      <div class="main" id="top">
        <Topbar live={live} window={windowName} onWindow={setWindowName} theme={theme} onToggleTheme={toggleTheme} />
        <main class="content">
          <Hero stats={stats} mode={mode} engine={engine} />
          <div class="section-label">Panels</div>
          <div class="grid">
            <SpendPanel stats={stats} theme={theme} />
            <FeedPanel items={items} filter={filter} />
            <LedgerPanel stats={stats} />
            <CachePanel stats={stats} theme={theme} />
            <MixPanel stats={stats} theme={theme} />
            <QualityPanel stats={stats} theme={theme} />
            <EvalPanel runs={runs} reload={reload} theme={theme} />
          </div>
        </main>
      </div>
    </div>
  );
}
