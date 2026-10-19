import type { ComponentChildren, RefObject } from "preact";
import { useEffect, useRef, useState } from "preact/hooks";
import type { Mode } from "../types";
import { IconDashboard, IconLogo, IconSearch, IconSwitch, IconUsers } from "./icons";

interface SidebarProps {
  searchRef: RefObject<HTMLInputElement>;
  onFilter: (value: string) => void;
  counts: { sessions: number; switches: number; feed: number; ledger: number; mix: number; quality: number; evalRuns: number };
  mode: Mode | null;
  modeMessage: string | null;
  onMode: (mode: Mode) => void;
}

const MODES: Mode[] = ["off", "shadow", "active"];

/** Panel anchors repeat across the two groups, so the pill follows the item's key and not its href. */
const MAIN_NAV = [
  { key: "dashboard", href: "#top", label: "Dashboard", icon: <IconDashboard />, count: null },
  { key: "sessions", href: "#panel-feed", label: "Sessions", icon: <IconUsers />, count: "sessions" },
  { key: "switches", href: "#panel-ledger", label: "Switches", icon: <IconSwitch />, count: "switches" },
] as const;

const PANEL_NAV = [
  { key: "feed", href: "#panel-feed", label: "Live turn feed", count: "feed" },
  { key: "spend", href: "#panel-spend", label: "Spend vs baseline", count: null },
  { key: "ledger", href: "#panel-ledger", label: "Switch ledger", count: "ledger" },
  { key: "cache", href: "#panel-cache", label: "Cache health", count: null },
  { key: "mix", href: "#panel-mix", label: "Model mix", count: "mix" },
  { key: "quality", href: "#panel-quality", label: "Decision quality", count: "quality" },
  { key: "eval", href: "#panel-eval", label: "Eval runs", count: "evalRuns" },
] as const;

const ALL_NAV = [...MAIN_NAV, ...PANEL_NAV];
const COUNT_IDS: Record<string, string> = {
  sessions: "nav-sessions",
  switches: "nav-switches",
  feed: "nav-feed",
  ledger: "nav-ledger",
  mix: "nav-mix",
  quality: "nav-quality",
  evalRuns: "nav-eval",
};
const SPY_LINE_RATIO = 0.3;
const SCROLL_SETTLE_MS = 150;

function targetOf(href: string): HTMLElement | null {
  return document.getElementById(href.slice(1));
}

/** The nav item whose section is under the sticky bar. Clicking selects at once, scrolling follows the page. */
function useActiveNav(): [string, (key: string) => void] {
  const [active, setActive] = useState<string>(() => ALL_NAV.find((n) => n.href === location.hash && n.href !== "#top")?.key ?? "dashboard");
  const current = useRef(active);
  const holdUntil = useRef(0);
  const select = (key: string) => {
    current.current = key;
    holdUntil.current = Date.now() + SCROLL_SETTLE_MS;
    setActive(key);
  };
  useEffect(() => {
    const spans = (href: string) => {
      const box = targetOf(href)?.getBoundingClientRect();
      const line = window.innerHeight * SPY_LINE_RATIO;
      return box !== undefined && box.top <= line && box.bottom > line;
    };
    const onScroll = () => {
      if (Date.now() < holdUntil.current) {
        holdUntil.current = Date.now() + SCROLL_SETTLE_MS;
        return;
      }
      const item = ALL_NAV.find((n) => n.key === current.current);
      if (item && item.href !== "#top" && spans(item.href)) return;
      const next = PANEL_NAV.find((n) => spans(n.href))?.key ?? "dashboard";
      if (next !== current.current) {
        current.current = next;
        setActive(next);
      }
    };
    document.addEventListener("scroll", onScroll, { passive: true });
    return () => document.removeEventListener("scroll", onScroll);
  }, []);
  return [active, select];
}

function NavLink(props: { href: string; active: boolean; onSelect: () => void; children: ComponentChildren }) {
  return (
    <a
      class={"nav-item" + (props.active ? " active" : "")}
      href={props.href}
      aria-current={props.active ? "location" : undefined}
      onClick={props.onSelect}
    >
      {props.children}
    </a>
  );
}

export function Sidebar({ searchRef, onFilter, counts, mode, modeMessage, onMode }: SidebarProps) {
  const [active, select] = useActiveNav();
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
        {MAIN_NAV.map((n) => (
          <NavLink key={n.key} href={n.href} active={active === n.key} onSelect={() => select(n.key)}>
            {n.icon}
            <span>{n.label}</span>
            {n.count && (
              <span class="count" id={COUNT_IDS[n.count]}>
                {counts[n.count]}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

      <div class="section-label">Panels</div>
      <nav class="nav small" aria-label="Panels">
        {PANEL_NAV.map((n) => (
          <NavLink key={n.key} href={n.href} active={active === n.key} onSelect={() => select(n.key)}>
            <span>{n.label}</span>
            {n.count && (
              <span class="count" id={COUNT_IDS[n.count]}>
                {counts[n.count]}
              </span>
            )}
          </NavLink>
        ))}
      </nav>

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
