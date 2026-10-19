import type { Theme, WindowName } from "../types";
import { IconMoon, IconSun } from "./icons";

export const WINDOWS: WindowName[] = ["15m", "1h", "24h", "7d", "all"];

interface TopbarProps {
  live: boolean;
  window: WindowName;
  onWindow: (window: WindowName) => void;
  theme: Theme;
  onToggleTheme: () => void;
}

export function Topbar({ live, window, onWindow, onToggleTheme }: TopbarProps) {
  return (
    <header class="topbar">
      <h2>Dashboard</h2>
      <div class="topbar-right">
        <span class={"live" + (live ? "" : " off")} id="live" title={live ? "Stream connected" : "Stream disconnected"}>
          <i></i>
          <span id="live-label">{live ? "live" : "offline"}</span>
        </span>
        <div class="segmented compact" id="window-switch" role="group" aria-label="Time window">
          {WINDOWS.map((w) => (
            <button type="button" data-window={w} class={w === window ? "on" : ""} aria-pressed={w === window} onClick={() => onWindow(w)}>
              {w}
            </button>
          ))}
        </div>
        <button type="button" class="icon-btn" id="theme-toggle" aria-label="Toggle theme" title="Toggle theme" onClick={onToggleTheme}>
          <IconSun />
          <IconMoon />
        </button>
      </div>
    </header>
  );
}
