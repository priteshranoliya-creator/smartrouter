import type { ComponentChildren, RefObject } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import type { Mode, View } from "../types";
import { IconDashboard, IconLogo, IconSearch, IconTarget } from "./icons";

interface SidebarProps {
  searchRef: RefObject<HTMLInputElement>;
  onFilter: (value: string) => void;
  view: View;
  onNavigate: (view: View, id: string) => void;
  counts: { feed: number; ledger: number; mix: number; quality: number; evalRuns: number };
  mode: Mode | null;
  modeMessage: string | null;
  onMode: (mode: Mode) => void;
}

const MODES: Mode[] = ["off", "shadow", "active"];

interface NavDef {
  key: string;
  view: View;
  id: string;
  label: string;
  count: keyof SidebarProps["counts"] | null;
}

/** Panels in page order for each view, so the active pill follows the order the page reads in. */
const VIEW_NAV: NavDef[] = [
  { key: "overview", view: "overview", id: "top", label: "Overview", count: null },
  { key: "technical", view: "technical", id: "top", label: "Technical details", count: null },
];
const OVERVIEW_PANELS: NavDef[] = [
  { key: "spend", view: "overview", id: "panel-spend", label: "Spend vs baseline", count: null },
  { key: "feed", view: "overview", id: "panel-feed", label: "Live turn feed", count: "feed" },
  { key: "ledger", view: "overview", id: "panel-ledger", label: "Switch ledger", count: "ledger" },
  { key: "mix", view: "overview", id: "panel-mix", label: "Model mix", count: "mix" },
  { key: "quality", view: "overview", id: "panel-quality", label: "Decision quality", count: "quality" },
  { key: "eval", view: "overview", id: "panel-eval", label: "Eval runs", count: "evalRuns" },
];
const TECH_PANELS: NavDef[] = [
  { key: "counters", view: "technical", id: "panel-counters", label: "Raw counters", count: null },
  { key: "models", view: "technical", id: "panel-models", label: "Per-model counts", count: null },
  { key: "cache", view: "technical", id: "panel-cache", label: "Cache health", count: null },
  { key: "engine", view: "technical", id: "panel-engine", label: "Decision engine detail", count: null },
];

const ALL_NAV = [...VIEW_NAV, ...OVERVIEW_PANELS, ...TECH_PANELS];
const COUNT_IDS: Record<string, string> = {
  feed: "nav-feed",
  ledger: "nav-ledger",
  mix: "nav-mix",
  quality: "nav-quality",
  evalRuns: "nav-eval",
};
const SPY_LINE_RATIO = 0.3;
const SCROLL_SETTLE_MS = 150;
const BOTTOM_SLACK_PX = 2;

/**
 * The panel the spy line has reached last, in page order. A gap between cards keeps the previous panel
 * (the last one whose top is above the line), the last panel wins at the bottom of the page, and the
 * view's own item is only active at the very top.
 */
function spyPanel(panels: NavDef[], viewKey: string): string {
  if (window.scrollY <= 0) return viewKey;
  const atBottom = window.innerHeight + window.scrollY >= document.documentElement.scrollHeight - BOTTOM_SLACK_PX;
  if (atBottom && panels.length > 0) return panels[panels.length - 1]!.key;
  const line = window.innerHeight * SPY_LINE_RATIO;
  // The lowest row that reached the line; in a two-card row the left card (first in page order) wins.
  let reached: string | null = null;
  let reachedTop = -Infinity;
  for (const n of panels) {
    const box = document.getElementById(n.id)?.getBoundingClientRect();
    if (box !== undefined && box.top <= line && box.top > reachedTop + 1) {
      reached = n.key;
      reachedTop = box.top;
    }
  }
  return reached ?? panels[0]?.key ?? viewKey;
}

/** The nav item whose section is under the sticky bar. Clicking selects at once, scrolling follows the page. */
function useActiveNav(view: View): [string, (key: string) => void] {
  const [active, setActive] = useState<string>(view);
  const current = useRef(active);
  const holdUntil = useRef(0);
  const viewRef = useRef(view);
  viewRef.current = view;
  const set = (key: string) => {
    current.current = key;
    setActive(key);
  };
  const select = (key: string) => {
    holdUntil.current = Date.now() + SCROLL_SETTLE_MS;
    set(key);
  };
  useEffect(() => {
    const item = ALL_NAV.find((n) => n.key === current.current);
    if (!item || item.view !== view) set(view);
  }, [view]);
  useEffect(() => {
    const onScroll = () => {
      if (viewRef.current === "changelog") return;
      if (Date.now() < holdUntil.current) {
        holdUntil.current = Date.now() + SCROLL_SETTLE_MS;
        return;
      }
      const panels = viewRef.current === "overview" ? OVERVIEW_PANELS : TECH_PANELS;
      const next = spyPanel(panels, viewRef.current);
      if (next !== current.current) set(next);
    };
    document.addEventListener("scroll", onScroll, { passive: true });
    return () => document.removeEventListener("scroll", onScroll);
  }, []);
  return [active, select];
}

function NavLink(props: { item: NavDef; active: boolean; onGo: () => void; count?: number; children?: ComponentChildren }) {
  const { item } = props;
  return (
    <a
      class={"nav-item" + (props.active ? " active" : "")}
      href={"#" + item.id}
      aria-current={props.active ? "location" : undefined}
      onClick={(event) => {
        event.preventDefault();
        props.onGo();
      }}
    >
      {props.children}
      <span>{item.label}</span>
      {item.count && (
        <span class="count" id={COUNT_IDS[item.count]}>
          {props.count}
        </span>
      )}
    </a>
  );
}

export function Sidebar({ searchRef, onFilter, view, onNavigate, counts, mode, modeMessage, onMode }: SidebarProps) {
  const [active, select] = useActiveNav(view);
  return (
    <aside class="sidebar" id="sidebar">
      <div class="brand">
        <span class="logo" aria-hidden="true">
          <IconLogo />
        </span>
        <span class="brand-name">
          smartrouter<span class="muted">.local</span>
        </span>
      </div>

      <label class="search">
        <IconSearch />
        <input
          id="search"
          ref={searchRef}
          type="search"
          placeholder="Filter prompts"
          autocomplete="off"
          aria-label="Filter the live feed by prompt text"
          onInput={(event) => onFilter(event.currentTarget.value)}
        />
        <kbd>Ctrl K</kbd>
      </label>

      <nav class="nav" aria-label="Main">
        {VIEW_NAV.map((n) => (
          <NavLink
            key={n.key}
            item={n}
            active={active === n.key}
            onGo={() => {
              select(n.key);
              onNavigate(n.view, n.id);
            }}
          >
            {n.key === "overview" ? <IconDashboard /> : <IconTarget />}
          </NavLink>
        ))}
      </nav>

      {view !== "changelog" && (
        <>
          <div class="section-label">{view === "overview" ? "Overview panels" : "Technical panels"}</div>
          <nav class="nav small" aria-label="Panels">
            {(view === "overview" ? OVERVIEW_PANELS : TECH_PANELS).map((n) => (
              <NavLink
                key={n.key}
                item={n}
                active={active === n.key}
                count={n.count ? counts[n.count] : undefined}
                onGo={() => {
                  select(n.key);
                  onNavigate(n.view, n.id);
                }}
              />
            ))}
          </nav>
        </>
      )}

      <div class="section-label">Resources</div>
      <nav class="nav small" aria-label="Resources">
        <a class="nav-item" href="/healthz" target="_blank" rel="noopener">
          <span>Health</span>
        </a>
        <a
          class="nav-item"
          href="https://github.com/priteshranoliya-creator/smartrouter/blob/main/docs/tdd-changes.md"
          target="_blank"
          rel="noopener"
        >
          <span>Design changes</span>
        </a>
        <button
          type="button"
          class={"nav-item" + (view === "changelog" ? " active" : "")}
          id="nav-changelog"
          aria-current={view === "changelog" ? "location" : undefined}
          onClick={() => onNavigate("changelog", "top")}
        >
          <span>Changelog</span>
        </button>
      </nav>

      <div class="sidebar-foot">
        <div class="section-label flush">Mode</div>
        <div class="segmented" id="mode-switch" role="group" aria-label="Routing mode">
          {MODES.map((m) =>
            m === "active" ? (
              <button
                type="button"
                data-mode={m}
                class={mode === m ? "on" : ""}
                aria-pressed={mode === m}
                disabled
                aria-disabled="true"
                title="Available after the T6.1 go/no-go review"
              >
                {m}
              </button>
            ) : (
              <button type="button" data-mode={m} class={mode === m ? "on" : ""} aria-pressed={mode === m} onClick={() => onMode(m)}>
                {m}
              </button>
            ),
          )}
        </div>
        <p class="mode-msg" id="mode-msg" role="status" hidden={modeMessage === null}>
          {modeMessage}
        </p>
      </div>
    </aside>
  );
}
