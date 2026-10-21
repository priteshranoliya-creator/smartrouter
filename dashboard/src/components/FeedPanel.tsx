import { useState } from "preact/hooks";
import { basename, clock, mainRequest, routingAnswer, shortModel, snippet, usd } from "../format";
import type { SessionSummary, TurnJson } from "../types";
import { Card } from "./Card";
import { IconActivity } from "./icons";

const FEED_LIMIT = 50;

export interface FeedItem {
  turn: TurnJson;
  session: SessionSummary | undefined;
}

/** All fed turns, newest first, filtered by the sidebar search. */
export function visibleFeed(items: FeedItem[], filter: string): FeedItem[] {
  const needle = filter.trim().toLowerCase();
  return items
    .filter((i) => !needle || (i.turn.prompt || "").toLowerCase().includes(needle))
    .slice(0, FEED_LIMIT);
}

function FeedRow({ item, detail }: { item: FeedItem; detail: boolean }) {
  const { turn, session } = item;
  const decision = turn.decision;
  const req = mainRequest(turn);
  const tokens = turn.requests.reduce(
    (n, r) =>
      n +
      (r.input_tokens || 0) +
      (r.output_tokens || 0) +
      (r.cache_read_tokens || 0) +
      (r.cache_write_5m_tokens || 0) +
      (r.cache_write_1h_tokens || 0),
    0,
  );
  const answer = routingAnswer(decision);
  const ran = req ? shortModel(req.sent_model) : null;
  const pick = decision ? shortModel(decision.chosen_model) : null;
  // Same test as the switches count and the ledger: the router's pick against the model the turn asked for.
  const asked = req ? shortModel(req.requested_model) : null;
  const same = asked !== null && pick !== null && asked === pick;
  const applied = !same && ran !== null && pick !== null && ran === pick;
  return (
    <div class="feed-row">
      <div class="f-time">{clock(turn.created_at)}</div>
      <div class="f-prompt">
        <span class="proj">{basename(session && session.project_path)}</span>
        <span class="prompt-text">{snippet(turn.prompt, 80)}</span>
      </div>
      <div class="f-meta">
        {ran && <span>{"Ran " + ran}</span>}
        {pick && <span>{"Router would pick " + pick}</span>}
        {decision && <span class={"badge " + (same ? "" : "switch")}>{same ? "same" : applied ? "switched" : "would switch"}</span>}
        {!decision && <span class="badge">no decision</span>}
        {detail && answer && <span>{"engine answer " + answer}</span>}
        {detail && decision && <span class="badge">{decision.rule}</span>}
        {detail && <span>{tokens.toLocaleString() + " tok"}</span>}
      </div>
      <div class="f-cost" title={turn.requests.length + " request(s) in this turn"}>
        <b>{req ? usd(req.cost_usd) : "n/a"}</b>
        <span>{"alt " + (req ? usd(req.counterfactual_cost_usd) : "n/a")}</span>
      </div>
    </div>
  );
}

interface FeedPanelProps {
  items: FeedItem[];
  filter: string;
}

export function FeedPanel({ items, filter }: FeedPanelProps) {
  const visible = visibleFeed(items, filter);
  const needle = filter.trim();
  const [detail, setDetail] = useState(false);
  return (
    <Card
      id="panel-feed"
      title="Live turn feed"
      subtitle="Each recent prompt: what ran, what the router would have picked, and the cost"
      icon={<IconActivity />}
      meta={
        <span class="card-actions">
          <span class="card-meta" id="feed-meta">
            {needle ? visible.length + " of " + items.length : items.length + " turns"}
          </span>
          <button type="button" class={"chip-btn" + (detail ? " on" : "")} id="feed-detail" aria-pressed={detail} onClick={() => setDetail(!detail)}>
            Technical detail
          </button>
        </span>
      }
    >
      <div class="feed" id="feed">
        {visible.map((item) => (
          <FeedRow key={item.turn.id} item={item} detail={detail} />
        ))}
      </div>
      <p class="empty" id="feed-empty" hidden={visible.length > 0}>
        {needle && items.length ? "No turns match the filter" : "No turns yet"}
      </p>
    </Card>
  );
}
