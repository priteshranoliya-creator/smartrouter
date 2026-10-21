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
import { CountersPanel, EnginePanel, ModelTablePanel } from "./components/TechPanels";
import { Topbar } from "./components/Topbar";
import { useEvalRuns, useFeed, useMode, useStats, useStream, useTheme } from "./hooks";
import type { View, WindowName } from "./types";

export function App() {
  const [windowName, setWindowName] = useState<WindowName>("all");
  const [filter, setFilter] = useState("");
  const [view, setView] = useState<View>("overview");
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

  const navigate = (next: View, id: string) => {
    setView(next);
    const scroll = () => {
      const target = document.getElementById(id);
      if (id === "top" || !target) window.scrollTo({ top: 0 });
      else target.scrollIntoView({ block: "start" });
    };
    requestAnimationFrame(() => requestAnimationFrame(scroll));
  };

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
    feed: visibleFeed(items, filter).length,
    ledger: stats ? stats.switch_ledger.length : 0,
    mix: stats ? stats.by_model.length : 0,
    quality: stats ? Object.values(stats.decisions_by_rule).reduce((n, c) => n + c, 0) : 0,
    evalRuns: runs ? runs.length : 0,
  };

  return (
    <div class="shell">
      <Sidebar searchRef={searchRef} onFilter={setFilter} view={view} onNavigate={navigate} counts={counts} mode={mode} modeMessage={message} onMode={change} />
      <div class="main" id="top">
        <Topbar live={live} window={windowName} onWindow={setWindowName} view={view} onView={(v) => navigate(v, "top")} theme={theme} onToggleTheme={toggleTheme} />
        <main class="content">
          {stats === null ? (
            <p class="empty page-loading">Loading</p>
          ) : view === "overview" ? (
            <>
              <Hero stats={stats} mode={mode} />
              <div class="grid">
                <SpendPanel stats={stats} theme={theme} />
                <FeedPanel items={items} filter={filter} />
                <LedgerPanel stats={stats} />
                <MixPanel stats={stats} theme={theme} />
                <QualityPanel stats={stats} />
                <EvalPanel runs={runs} reload={reload} theme={theme} />
              </div>
            </>
          ) : (
            <>
              <div class="page-intro">
                <h1>Technical details</h1>
                <p class="lede">The numbers behind the overview. You do not need these to judge whether the router works.</p>
              </div>
              <div class="grid">
                <CountersPanel stats={stats} engine={engine} />
                <ModelTablePanel stats={stats} />
                <CachePanel stats={stats} theme={theme} />
                <EnginePanel stats={stats} theme={theme} />
              </div>
            </>
          )}
        </main>
      </div>
    </div>
  );
}
