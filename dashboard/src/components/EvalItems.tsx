import { Fragment } from "preact";
import { useState } from "preact/hooks";
import { ms, pct, shortModel, usd } from "../format";
import type { ArmSummary, ItemArm, ItemRow, JudgePoint } from "../types";
import { IconChevron } from "./icons";

function score(value: number | null): string {
  return value === null ? "–" : value.toFixed(2);
}

function ScoreCell({ arm }: { arm: ItemArm | undefined }) {
  if (!arm) return <td class="num">–</td>;
  if (arm.error) return <td class="num neg" title={arm.error}>error</td>;
  const tone = arm.passed === true ? "pos" : arm.passed === false ? "neg" : "";
  return <td class={"num " + tone}>{score(arm.score)}</td>;
}

function Points({ points }: { points: JudgePoint[] }) {
  return (
    <ul class="points">
      {points.map((p) => (
        <li key={p.index}>
          <span class={"badge verdict " + p.verdict}>{p.verdict}</span>
          <span class="muted">{"point " + p.index}</span>
          {p.reason && <span>{p.reason}</span>}
        </li>
      ))}
    </ul>
  );
}

function ArmCard({ name, arm }: { name: string; arm: ItemArm | undefined }) {
  if (!arm) {
    return (
      <div class="arm-card">
        <h4>{name}</h4>
        <p class="empty">No row for this arm</p>
      </div>
    );
  }
  const judge = arm.rubric && arm.rubric.judge;
  const rejudge = arm.rubric && arm.rubric.rejudge;
  return (
    <div class="arm-card">
      <h4>
        {name}
        <span class="muted">{shortModel(arm.model_used)}</span>
      </h4>
      <div class="arm-stats">
        <span class={"badge " + (arm.passed === true ? "pass" : arm.passed === false ? "fail" : "")}>
          {arm.passed === null ? "not judged" : arm.passed ? "pass" : "fail"}
        </span>
        <span>
          score <b>{score(arm.score)}</b>
        </span>
        <span>{usd(arm.cost_usd)}</span>
        <span>{ms(arm.latency_ms)}</span>
      </div>
      {arm.error && <p class="neg">{arm.error}</p>}
      {judge && Array.isArray(judge.points) && <Points points={judge.points} />}
      {rejudge && (
        <p class="muted">
          {"re-judge " + shortModel(rejudge.model) + ": " + (rejudge.error ? "error, " + rejudge.error : rejudge.pass ? "pass" : "fail")}
        </p>
      )}
      <pre class="answer">{arm.answer || "(no answer)"}</pre>
    </div>
  );
}

function ItemRows({ item, arms, open, onToggle }: { item: ItemRow; arms: string[]; open: boolean; onToggle: () => void }) {
  return (
    <Fragment>
      <tr class={"item-row" + (open ? " open" : "")} onClick={onToggle}>
        <td>
          <button type="button" class="row-toggle" aria-expanded={open} aria-label={"Show answers for " + item.item_id}>
            <span class="chev">
              <IconChevron />
            </span>
            {item.item_id}
          </button>
        </td>
        <td>{item.expected_tier || "–"}</td>
        <td class={item.predicted_tier && item.predicted_tier !== item.expected_tier ? "neg" : ""}>{item.predicted_tier || "–"}</td>
        {arms.map((name) => (
          <ScoreCell key={name} arm={item.arms[name]} />
        ))}
      </tr>
      {open && (
        <tr class="item-detail">
          <td colSpan={3 + arms.length}>
            <div class="arm-grid">
              {arms.map((name) => (
                <ArmCard key={name} name={name} arm={item.arms[name]} />
              ))}
            </div>
          </td>
        </tr>
      )}
    </Fragment>
  );
}

export function EvalItems({ items, arms }: { items: ItemRow[]; arms: string[] }) {
  const [open, setOpen] = useState<Set<string>>(new Set());
  const toggle = (id: string) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (!next.delete(id)) next.add(id);
      return next;
    });
  if (items.length === 0) return <p class="empty">No results stored for this run yet</p>;
  return (
    <div class="table-wrap">
      <table class="items">
        <thead>
          <tr>
            <th>Item</th>
            <th>Expected</th>
            <th>Router</th>
            {arms.map((a) => (
              <th class="num" key={a}>
                {a}
              </th>
            ))}
          </tr>
        </thead>
        <tbody>
          {items.map((item) => (
            <ItemRows key={item.item_id} item={item} arms={arms} open={open.has(item.item_id)} onToggle={() => toggle(item.item_id)} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

export function ArmsTable({ arms }: { arms: ArmSummary[] }) {
  return (
    <div class="table-wrap">
      <table class="arms">
        <thead>
          <tr>
            <th>Arm</th>
            <th class="num">n</th>
            <th class="num">Score</th>
            <th class="num">Pass</th>
            <th class="num">Total cost</th>
            <th class="num">Mean cost</th>
            <th class="num">Latency</th>
            <th class="num">Errors</th>
          </tr>
        </thead>
        <tbody>
          {arms.map((a) => (
            <tr key={a.arm}>
              <td>{a.arm}</td>
              <td class="num">{a.n}</td>
              <td class="num">{a.mean_score === null ? "n/a" : a.mean_score.toFixed(2)}</td>
              <td class="num">{pct(a.pass_rate)}</td>
              <td class="num">{usd(a.total_cost_usd)}</td>
              <td class="num">{usd(a.mean_cost_usd)}</td>
              <td class="num">{ms(a.mean_latency_ms)}</td>
              <td class={"num" + (a.errors ? " neg" : "")}>{a.errors}</td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}
