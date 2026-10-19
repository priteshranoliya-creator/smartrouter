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

function FeedRow({ item }: { item: FeedItem }) {
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
  return (
    <div class="feed-row">
      <div class="f-time">{clock(turn.created_at)}</div>
      <div class="f-prompt">
        <span class="proj">{basename(session && session.project_path)}</span>
        <span class="prompt-text">{snippet(turn.prompt, 80)}</span>
      </div>
      <div class="f-meta">
        {answer && <span>{answer}</span>}
        {decision && <span class="badge">{decision.rule}</span>}
        {decision && <span>{"chose " + shortModel(decision.chosen_model)}</span>}
        {req && <span>{"sent " + shortModel(req.sent_model)}</span>}
        <span>{tokens.toLocaleString() + " tok"}</span>
        {!decision && <span class="badge">no decision</span>}
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
  return (
    <Card
      id="panel-feed"
      title="Live turn feed"
      subtitle="Newest first, last 50 turns"
      icon={<IconActivity />}
      meta={
        <span class="card-meta" id="feed-meta">
          {needle ? visible.length + " of " + items.length : items.length + " turns"}
        </span>
      }
    >
      <div class="feed" id="feed">
        {visible.map((item) => (
          <FeedRow key={item.turn.id} item={item} />
        ))}
      </div>
      <p class="empty" id="feed-empty" hidden={visible.length > 0}>
        {needle && items.length ? "No turns match the filter" : "No turns yet"}
      </p>
    </Card>
  );
}
