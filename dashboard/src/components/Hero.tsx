import type { JSX } from "preact";
import { pct, usd } from "../format";
import type { Mode, Stats } from "../types";
import { IconActivity, IconDatabase, IconMessage, IconSwitch, IconTimer, IconUser } from "./icons";

interface HeroProps {
  stats: Stats | null;
  mode: Mode | null;
  engine: string | null;
}

function headline(s: Stats | null, mode: Mode | null): [string, string] {
  if (!s || !s.requests) return ["Waiting for the first routed turn", ""];
  if (mode === "off") return ["Routing is off", "recording " + usd(s.without_router_usd) + " of Claude Code traffic"];
  const active = mode === "active";
  const verb = s.saving_usd >= 0 ? (active ? "saved " : "would have saved ") : active ? "cost " : "would have cost ";
  const amount = usd(Math.abs(s.saving_usd)) + (s.saving_usd >= 0 ? "" : " more");
  return ["Routing " + verb + amount, "on " + usd(s.without_router_usd) + " of Claude Code traffic"];
}

export function Hero({ stats, mode, engine }: HeroProps) {
  const [first, second] = headline(stats, mode);
  const label = mode || "unknown";
  const behaviour =
    label === "active"
      ? " applies the router's choice to each request."
      : label === "off"
        ? " passes every request straight through."
        : " records what the router would do and never changes a request.";
  const tiles: [string, string, string | number, string][] = [
    ["tile-sessions", "Sessions", stats ? stats.sessions : 0, "user"],
    ["tile-turns", "Turns", stats ? stats.turns : 0, "message"],
    ["tile-requests", "Requests", stats ? stats.requests : 0, "activity"],
    ["tile-cache", "Cache hit", stats ? pct(stats.cache_hit_ratio) : "0%", "database"],
    ["tile-switches", "Switches", stats ? stats.switches : 0, "switch"],
    ["tile-timeouts", "Timeouts", stats ? stats.timeouts : 0, "timer"],
  ];
  const icons: Record<string, JSX.Element> = {
    user: <IconUser />,
    message: <IconMessage />,
    activity: <IconActivity />,
    database: <IconDatabase />,
    switch: <IconSwitch />,
    timer: <IconTimer />,
  };
  return (
    <section class="hero">
      <div class="hero-text">
        <h1 id="headline">
          <span id="headline-a">{first}</span>
          <span id="headline-b">{second}</span>
        </h1>
        <p id="hero-sub" class="lede">
          <b>{label + " mode"}</b>
          {behaviour + (engine ? " Engine " + engine + "." : "") + " "}
          <b>Prompts never leave this machine except to the decision engine.</b>
        </p>
      </div>
      <div class="tiles" id="tiles">
        {tiles.map(([id, name, value, icon]) => (
          <div class="tile">
            {icons[icon]}
            <div>
              <b id={id}>{value}</b>
              <span>{name}</span>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}
