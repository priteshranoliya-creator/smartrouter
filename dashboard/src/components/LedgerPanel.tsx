import { useState } from "preact/hooks";
import { shortModel, usd } from "../format";
import type { Stats } from "../types";
import { Card } from "./Card";
import { IconSwitch } from "./icons";

export function LedgerPanel({ stats }: { stats: Stats | null }) {
  const [estimates, setEstimates] = useState(false);
  const rows = stats ? [...stats.switch_ledger].sort((x, y) => y.created_at.localeCompare(x.created_at)) : [];
  return (
    <Card
      id="panel-ledger"
      title="Switch ledger"
      subtitle="Turns where the router would have picked a different model, and what that was worth"
      icon={<IconSwitch />}
      meta={
        <button type="button" class={"chip-btn" + (estimates ? " on" : "")} id="ledger-estimates" aria-pressed={estimates} onClick={() => setEstimates(!estimates)}>
          Show estimates
        </button>
      }
    >
      <div class="table-wrap">
        <table id="ledger-table" hidden={rows.length === 0}>
          <thead>
            <tr>
              <th>Turn</th>
              <th>Switch</th>
              {estimates && <th class="num" title="Estimated cost of switching: the cache has to be rebuilt">Est switch cost</th>}
              {estimates && <th class="num" title="Estimated saving before the switch cost">Est saving</th>}
              <th class="num" title="Saving actually measured on this turn">Realised saving</th>
            </tr>
          </thead>
          <tbody id="ledger-body">
            {rows.map((row) => (
              <tr key={row.turn_id}>
                <td>{"#" + row.seq + " " + row.session_id.slice(0, 6)}</td>
                <td>
                  <span>{shortModel(row.from_model)}</span>
                  <span class="arrow"> → </span>
                  <span>{shortModel(row.to_model)}</span>
                </td>
                {estimates && <td class="num">{usd(row.est_switch_tax_usd)}</td>}
                {estimates && <td class="num">{usd(row.est_saving_usd)}</td>}
                <td class={"num " + (row.realised_saving_usd > 0 ? "pos" : row.realised_saving_usd < 0 ? "neg" : "")}>
                  {usd(row.realised_saving_usd)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p class="empty" id="ledger-empty" hidden={rows.length > 0}>
        No switches yet
      </p>
    </Card>
  );
}
