import type { Theme, View, WindowName } from "../types";
import { IconMoon, IconSun } from "./icons";

export const WINDOWS: WindowName[] = ["15m", "1h", "24h", "7d", "all"];
const VIEWS: [View, string][] = [
  ["overview", "Overview"],
  ["technical", "Technical details"],
];

interface TopbarProps {
  live: boolean;
  window: WindowName;
  onWindow: (window: WindowName) => void;
  view: View;
  onView: (view: View) => void;
  theme: Theme;
  onToggleTheme: () => void;
}

export function Topbar({ live, window, onWindow, view, onView, onToggleTheme }: TopbarProps) {
  return (
    <header class="topbar">
      <div class="tabs" role="tablist" aria-label="Dashboard view">
        {VIEWS.map(([key, label]) => (
          <button
            type="button"
            role="tab"
            id={"tab-" + key}
            class={"tab" + (view === key ? " on" : "")}
            aria-selected={view === key}
            onClick={() => onView(key)}
          >
            {label}
          </button>
        ))}
      </div>
      <div class="topbar-right">
        <span class={"live" + (live ? "" : " off")} id="live" title={live ? "Stream connected" : "Stream disconnected"}>
          <i></i>
          <span id="live-label">{live ? "live" : "offline"}</span>
        </span>
        {view !== "changelog" && (
          <div class="segmented compact" id="window-switch" role="group" aria-label="Time window">
            {WINDOWS.map((w) => (
              <button type="button" data-window={w} class={w === window ? "on" : ""} aria-pressed={w === window} onClick={() => onWindow(w)}>
                {w}
              </button>
            ))}
          </div>
        )}
        <button type="button" class="icon-btn" id="theme-toggle" aria-label="Toggle theme" title="Toggle theme" onClick={onToggleTheme}>
          <IconSun />
          <IconMoon />
        </button>
      </div>
    </header>
  );
}
