import { useEffect, useState } from "preact/hooks";
import * as api from "../api";
import { pct, usd } from "../format";
import type { EvalRunDetail, EvalRunListItem, Theme } from "../types";
import { Card } from "./Card";
import { ConfusionMatrix } from "./ConfusionMatrix";
import { Disclosure } from "./Disclosure";
import { ArmsExtra, ArmsTable, EvalItems } from "./EvalItems";
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
      subtitle="A fixed test set answered by every model and graded by a judge, to show whether the router picks well"
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
  const router = detail.arms.find((a) => a.arm === "router");
  const oracle = detail.arms.find((a) => a.arm === "oracle");
  const compare =
    router && oracle && router.mean_score !== null && oracle.mean_score !== null
      ? `The router scored ${router.mean_score.toFixed(2)} for ${usd(router.total_cost_usd)}. The oracle (always the best model) scored ${oracle.mean_score.toFixed(2)} for ${usd(oracle.total_cost_usd)}.`
      : null;
  return (
    <>
      <div class="kpis">
        <div title="How often the router picked the same model tier as the golden answer">
          <span>Routing accuracy</span>
          <b id="eval-accuracy">{pct(routing.accuracy)}</b>
          <span>{"over " + routing.n + " questions"}</span>
        </div>
        <div title="How often a second judge agreed with the first">
          <span>Judge agreement</span>
          <b id="eval-agreement">{pct(agreement.rate)}</b>
          <span>{agreement.n ? "over " + agreement.n + " re-judged rows" : "no re-judged rows"}</span>
        </div>
        <div>
          <span>Run</span>
          <b class="small-b">{run.finished_at ? "finished" : "not finished"}</b>
        </div>
      </div>
      {compare && <p class="sub-label">{compare}</p>}
      <ArmsTable arms={detail.arms} />
      <Disclosure summary="Charts" hint="quality against cost, and router tier against golden tier">
        <div class="badges">
          <span class="badge">
            dataset <b>{run.dataset_version}</b>
          </span>
          <span class="badge">
            judge <b>{run.judge_model}</b>
          </span>
        </div>
        <div class="eval-top">
          <div class="eval-col">
            <p class="sub-label">Quality vs cost</p>
            <EvalScatter points={detail.quality_cost} theme={theme} />
          </div>
          <div class="eval-col">
            <p class="sub-label">Router tier against golden tier</p>
            <ConfusionMatrix routing={routing} />
          </div>
        </div>
        <p class="sub-label">More model statistics</p>
        <ArmsExtra arms={detail.arms} />
      </Disclosure>
      <Disclosure summary="Per-question results" hint="ids such as g-0007 are test-set question ids">
        <p class="sub-label">Click a row to compare each model's answer to that question.</p>
        <EvalItems items={detail.items} arms={arms} />
      </Disclosure>
    </>
  );
}
