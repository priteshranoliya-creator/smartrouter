import { useEffect, useState } from "preact/hooks";
import * as api from "../api";
import { pct } from "../format";
import type { EvalRunDetail, EvalRunListItem, Theme } from "../types";
import { Card } from "./Card";
import { ConfusionMatrix } from "./ConfusionMatrix";
import { ArmsTable, EvalItems } from "./EvalItems";
import { EvalScatter } from "./EvalScatter";
import { IconFlask } from "./icons";

interface EvalPanelProps {
  runs: EvalRunListItem[] | null;
  reload: () => void;
  theme: Theme;
}

type Detail = { id: number; run: EvalRunDetail } | { id: number; error: string };

function started(iso: string): string {
  const when = new Date(iso);
  return Number.isNaN(when.getTime()) ? iso : when.toLocaleString([], { dateStyle: "medium", timeStyle: "short" });
}

export function EvalPanel({ runs, reload, theme }: EvalPanelProps) {
  const [picked, setPicked] = useState<number | null>(null);
  const [detail, setDetail] = useState<Detail | null>(null);
  const [tick, setTick] = useState(0);
  const list = runs ?? [];
  const selected = list.find((r) => r.id === picked) ?? list[0] ?? null;
  const selectedId = selected ? selected.id : null;

  useEffect(() => {
    if (selectedId === null) return;
    let stale = false;
    api
      .fetchEvalRun(selectedId)
      .then((run) => !stale && setDetail({ id: selectedId, run }))
      .catch((e: unknown) => !stale && setDetail({ id: selectedId, error: e instanceof Error ? e.message : String(e) }));
    return () => {
      stale = true;
    };
  }, [selectedId, tick]);

  const refresh = () => {
    reload();
    setTick((n) => n + 1);
  };
  const current = detail && detail.id === selectedId ? detail : null;

  return (
    <Card
      id="panel-eval"
      wide
      title="Eval runs"
      subtitle="Golden set answered by every arm, scored by the judge"
      icon={<IconFlask />}
      meta={
        list.length > 0 ? (
          <span class="eval-controls">
            <label class="select-label">
              <span class="muted">Run</span>
              <select
                id="eval-run"
                aria-label="Eval run"
                value={selectedId ?? ""}
                onChange={(event) => setPicked(Number(event.currentTarget.value))}
              >
                {list.map((r, i) => (
                  <option key={r.id} value={r.id}>
                    {"#" + r.id + (i === 0 ? " (latest)" : "") + " · " + r.n_items + " items · " + started(r.started_at)}
                  </option>
                ))}
              </select>
            </label>
            <button type="button" class="icon-btn small" aria-label="Refresh eval runs" title="Refresh" onClick={refresh}>
              ↻
            </button>
          </span>
        ) : null
      }
    >
      {runs === null && <p class="empty">Loading eval runs</p>}
      {runs !== null && list.length === 0 && (
        <p class="empty" id="eval-empty">
          No eval runs yet. Start one with <code>router eval run</code>, then <code>router eval report</code> writes the same numbers to a file.
        </p>
      )}
      {selected && !current && <p class="empty">Loading run #{selected.id}</p>}
      {current && "error" in current && <p class="empty neg">{"Could not load run #" + current.id + ": " + current.error}</p>}
      {current && "run" in current && <RunView detail={current.run} theme={theme} />}
    </Card>
  );
}

function RunView({ detail, theme }: { detail: EvalRunDetail; theme: Theme }) {
  const { run, routing, agreement } = detail;
  const arms = detail.arms.map((a) => a.arm);
  return (
    <>
      <div class="badges">
        <span class="badge">
          dataset <b>{run.dataset_version}</b>
        </span>
        <span class="badge">
          judge <b>{run.judge_model}</b>
        </span>
        <span class="badge">{run.finished_at ? "finished" : "not finished"}</span>
      </div>
      <div class="eval-top">
        <div class="eval-col">
          <p class="sub-label">Quality vs cost</p>
          <EvalScatter points={detail.quality_cost} theme={theme} />
        </div>
        <div class="eval-col">
          <p class="sub-label">Router tier against golden tier</p>
          <ConfusionMatrix routing={routing} />
          <div class="kpis two">
            <div>
              <span>Routing accuracy</span>
              <b id="eval-accuracy">{pct(routing.accuracy)}</b>
            </div>
            <div>
              <span>Judge agreement</span>
              <b id="eval-agreement">{pct(agreement.rate)}</b>
              <span>{agreement.n ? "over " + agreement.n + " re-judged rows" : "no re-judged rows"}</span>
            </div>
          </div>
        </div>
      </div>
      <ArmsTable arms={detail.arms} />
      <p class="sub-label">Items (click a row to compare the arms' answers)</p>
      <EvalItems items={detail.items} arms={arms} />
    </>
  );
}
