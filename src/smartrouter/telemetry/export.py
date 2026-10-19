"""CSV export: one row per request, with the same spend-vs-baseline figures the dashboard shows."""

from __future__ import annotations

import csv
from collections.abc import Iterable
from typing import TextIO

from smartrouter.telemetry import stats
from smartrouter.telemetry.models import RequestRow

COLUMNS = (
    "created_at",
    "session_id",
    "turn_seq",
    "kind",
    "requested_model",
    "sent_model",
    "mode",
    "rule",
    "chosen_model",
    "input_tokens",
    "output_tokens",
    "cache_read_tokens",
    "cache_write_5m_tokens",
    "cache_write_1h_tokens",
    "cost_usd",
    "counterfactual_cost_usd",
    "without_router_usd",
    "with_router_usd",
)


def write_csv(rows: Iterable[RequestRow], out: TextIO) -> int:
    """Write the header and one line per request. Returns the number of requests written."""
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(COLUMNS)
    count = 0
    for row in rows:
        req = row.request
        without, with_router = stats.request_costs(row)
        values = {
            **{c: getattr(req, c) for c in COLUMNS if hasattr(req, c)},
            "turn_seq": row.turn_seq,
            "mode": row.mode,
            "rule": row.rule,
            "chosen_model": row.chosen_model,
            "without_router_usd": without,
            "with_router_usd": with_router,
        }
        writer.writerow("" if values[c] is None else values[c] for c in COLUMNS)
        count += 1
    return count
