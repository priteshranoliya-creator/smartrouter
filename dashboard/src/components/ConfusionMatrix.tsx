import { pct } from "../format";
import type { Routing } from "../types";

const HEAT_MAX = 55;

/** Rows are the golden tier, columns the router's. Shade is the share of the row; the diagonal is green, errors red. */
export function ConfusionMatrix({ routing }: { routing: Routing }) {
  if (routing.n === 0) return <p class="empty">No router predictions in this run</p>;
  return (
    <div class="table-wrap">
      <table class="confusion">
        <caption class="sub-label">
          {"Routing accuracy " + pct(routing.accuracy) + " over " + routing.n + " items"}
        </caption>
        <thead>
          <tr>
            <th>expected \ router</th>
            {routing.tiers.map((t) => (
              <th class="num" key={t}>
                {t}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {routing.tiers.map((expected) => {
            const row = routing.confusion[expected] || {};
            const total = routing.tiers.reduce((n, t) => n + (row[t] || 0), 0);
            return (
              <tr key={expected}>
                <th scope="row">{expected}</th>
                {routing.tiers.map((predicted) => {
                  const count = row[predicted] || 0;
                  const share = total ? count / total : 0;
                  const colour = predicted === expected ? "var(--accent)" : "var(--bad)";
                  return (
                    <td
                      class="num heat"
                      key={predicted}
                      title={count + " of " + total + " (" + pct(share) + ")"}
                      style={{ background: "color-mix(in srgb, " + colour + " " + Math.round(share * HEAT_MAX) + "%, transparent)" }}
                    >
                      {count}
                    </td>
                  );
                })}
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
