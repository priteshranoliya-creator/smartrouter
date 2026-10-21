import type { JSX } from "preact";
import { pct, usd } from "../format";
import type { Mode, Stats } from "../types";
import { IconActivity, IconMessage, IconSwitch, IconTimer } from "./icons";

interface HeroProps {
  stats: Stats | null;
  mode: Mode | null;
}

function headline(s: Stats | null, mode: Mode | null): [string, string] {
  if (!s) return ["Loading", " "];
  if (!s.requests) return ["Waiting for the first routed turn", " "];
  if (mode === "off") return ["Routing is off", "recording " + usd(s.without_router_usd) + " of Claude Code traffic"];
  const active = mode === "active";
  const verb = s.saving_usd >= 0 ? (active ? "saved " : "would have saved ") : active ? "cost " : "would have cost ";
  const amount = usd(Math.abs(s.saving_usd)) + (s.saving_usd >= 0 ? "" : " more");
  return ["Routing " + verb + amount, "on " + usd(s.without_router_usd) + " of Claude Code traffic"];
}

export function Hero({ stats, mode }: HeroProps) {
  const [first, second] = headline(stats, mode);
  const label = mode || "unknown";
  const behaviour =
    label === "active"
      ? " applies the router's choice to each request."
      : label === "off"
        ? " passes every request straight through."
        : " records what the router would do and never changes a request.";
  const savingPct = stats && stats.without_router_usd > 0 ? pct(stats.saving_usd / stats.without_router_usd).replace(/^-0%$/, "0%") : "n/a";
  const tiles: [string, string, string | number, string, string][] = [
    ["tile-turns", "Turns", stats ? stats.turns : 0, "message", "Prompts you sent to Claude Code in this window"],
    ["tile-saving", "Saving", savingPct, "activity", "Share of the without-router cost that routing would save"],
    ["tile-switches", "Switches", stats ? stats.switches : 0, "switch", "Turns where the router would pick a different model than the one that ran"],
    ["tile-timeouts", "Timeouts", stats ? stats.timeouts : 0, "timer", "Times the decision engine took too long and the router fell back. Engine failures are counted separately under Decision quality"],
  ];
  const icons: Record<string, JSX.Element> = {
    message: <IconMessage />,
    activity: <IconActivity />,
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
          {behaviour + " "}
          <b>Prompts never leave this machine except to the decision engine.</b>
        </p>
      </div>
      <div class="tiles" id="tiles">
        {tiles.map(([id, name, value, icon, tip]) => (
          <div class="tile" key={id} title={tip}>
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
