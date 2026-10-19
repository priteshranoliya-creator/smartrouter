import type { RefObject } from "preact";
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

export function Sidebar({ searchRef, onFilter, counts, mode, modeMessage, onMode }: SidebarProps) {
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
        <a class="nav-item active" href="#top">
          <IconDashboard />
          <span>Dashboard</span>
        </a>
        <a class="nav-item" href="#panel-feed">
          <IconUsers />
          <span>Sessions</span>
          <span class="count" id="nav-sessions">
            {counts.sessions}
          </span>
        </a>
        <a class="nav-item" href="#panel-ledger">
          <IconSwitch />
          <span>Switches</span>
          <span class="count" id="nav-switches">
            {counts.switches}
          </span>
        </a>
      </nav>

      <div class="section-label">Panels</div>
      <nav class="nav small" aria-label="Panels">
        <a class="nav-item" href="#panel-feed">
          <span>Live turn feed</span>
          <span class="count" id="nav-feed">
            {counts.feed}
          </span>
        </a>
        <a class="nav-item" href="#panel-spend">
          <span>Spend vs baseline</span>
        </a>
        <a class="nav-item" href="#panel-ledger">
          <span>Switch ledger</span>
          <span class="count" id="nav-ledger">
            {counts.ledger}
          </span>
        </a>
        <a class="nav-item" href="#panel-cache">
          <span>Cache health</span>
        </a>
        <a class="nav-item" href="#panel-mix">
          <span>Model mix</span>
          <span class="count" id="nav-mix">
            {counts.mix}
          </span>
        </a>
        <a class="nav-item" href="#panel-quality">
          <span>Decision quality</span>
          <span class="count" id="nav-quality">
            {counts.quality}
          </span>
        </a>
        <a class="nav-item" href="#panel-eval">
          <span>Eval runs</span>
          <span class="count" id="nav-eval">
            {counts.evalRuns}
          </span>
        </a>
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
                disabled
                aria-disabled="true"
                title="Available after the T6.1 go/no-go review"
              >
                {m}
              </button>
            ) : (
              <button type="button" data-mode={m} class={mode === m ? "on" : ""} onClick={() => onMode(m)}>
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
