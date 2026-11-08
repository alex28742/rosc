"""Offline, read-only summary of an Architect latency trace (P024).

    .venv/bin/python3 -m scripts.architect_latency_summary <path/to/architect-latency.jsonl> [--json]

Reads the JSONL written by ``ROSC_ARCHITECT_LATENCY_TRACE=1``; touches nothing else (no database, no network, no write).
It prints per-turn phase DURATIONS (measured spans) separately from the inter-event INTERVAL (monotonic difference inside
one process ``origin``), flags turns without a terminal event as ``incomplete`` and states what is NOT measured.  Medians
and spread are over complete turns only and are descriptive, not a root-cause verdict.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

NOT_MEASURED = ("time to first token", "model token-generation time", "number/duration of LLM steps", "compaction",
                "client paint / first-authentic-message time")
# disjoint top-level phases of one delivery; nested spans (prompt/wait/lookup/probe/context) are reported separately
TOP = ("queue_wait_ms", "lock_wait_ms", "prepare_ms", "ask_ms", "commit_ms")


def load(path: Path) -> list[dict]:
    events = []
    with path.open(encoding="ascii", errors="replace") as handle:
        for number, line in enumerate(handle, 1):
            try:
                record = json.loads(line)
            except ValueError:
                print(f"warning: line {number} is not valid JSON (partial write?) and was skipped", file=sys.stderr)
                continue
            if isinstance(record, dict) and isinstance(record.get("event"), str):
                events.append(record)
    return events


def _first(events: list[dict], name: str, **match) -> dict | None:
    for e in events:
        if e["event"] == name and all(e.get(k) == v for k, v in match.items()):
            return e
    return None


def turns(events: list[dict]) -> dict[str, dict]:
    by_turn: dict[str, list[dict]] = defaultdict(list)
    for e in events:
        if e.get("turn_id"):
            by_turn[e["turn_id"]].append(e)
    out = {}
    for turn_id, evs in by_turn.items():
        deliver = [e for e in evs if e.get("phase") in (None, "intake", "deliver", "tool")]
        row: dict = {"turn_id": turn_id, "events": len(evs)}
        pick = lambda name, **m: _first(deliver, name, **m)  # noqa: E731
        for key, name in (("queue_wait_ms", "worker_started"), ("lock_wait_ms", "turn_lock_acquired"),
                          ("prepare_ms", "prepare"), ("ask_ms", "ask_finished"), ("commit_ms", "commit")):
            e = pick(name)
            if e is not None and "duration_ms" in e:
                row[key] = e["duration_ms"]
        for key, name in (("probe_ms", "probe"), ("context_build_ms", "context_built"), ("prompt_admit_ms", "prompt_admitted"),
                          ("wait_ms", "wait_returned"), ("lookup_ms", "lookup")):
            e = pick(name)
            if e is not None and "duration_ms" in e:
                row[key] = e["duration_ms"]
        ctx = pick("context_built")
        if ctx:
            for k in ("total_chars", "identity_chars", "rules_chars", "tool_guidance_chars", "recap_chars",
                      "recap_messages", "framing_chars", "owner_chars"):
                if k in ctx:
                    row[k] = ctx[k]
        lk = pick("lookup")
        if lk:
            row.update(lookup_requests=lk.get("requests"), lookup_messages=lk.get("messages"),
                       lookup_fallbacks=lk.get("fallbacks"), lookup_bytes=lk.get("bytes"))
        wait = pick("wait_returned")
        if wait:
            row["wait_outcome"] = wait.get("outcome")
        tools = [e for e in evs if e["event"] == "tool_call"]
        row["tools"] = len(tools)
        row["tools_verified"] = sum(1 for e in tools if e.get("correlation") == "verified")
        row["tool_ms_total"] = round(sum(e.get("duration_ms", 0) for e in tools), 3)
        fin = pick("turn_finished")
        state = [e for e in deliver if e["event"] == "delivery_state"]
        row["final_state"] = (fin or {}).get("state") or (state[-1].get("state") if state else None)
        row["outcome"] = (fin or {}).get("outcome")
        row["complete"] = fin is not None
        if fin is not None and "duration_ms" in fin:
            row["total_ms"] = fin["duration_ms"]
            known = sum(row.get(k, 0) for k in TOP)
            row["unaccounted_ms"] = round(max(fin["duration_ms"] - known, 0.0), 3)
        recon = [e for e in evs if e["event"] == "reconcile_finished"]
        row["reconciles"] = [{"outcome": e.get("outcome"), "duration_ms": e.get("duration_ms")} for e in recon]
        out[turn_id] = row
    return out


def spread(values: list[float]) -> dict:
    values = sorted(values)
    if not values:
        return {}
    p90 = values[min(len(values) - 1, int(round(0.9 * (len(values) - 1))))]
    return {"n": len(values), "min": values[0], "median": round(statistics.median(values), 3), "p90": p90,
            "max": values[-1]}


def summarize(events: list[dict]) -> dict:
    rows = turns(events)
    complete = [r for r in rows.values() if r["complete"]]
    keys = (*TOP, "total_ms", "unaccounted_ms", "probe_ms", "context_build_ms", "prompt_admit_ms", "wait_ms",
            "lookup_ms", "total_chars", "tool_ms_total")
    return {"turns": len(rows), "complete": len(complete), "incomplete": len(rows) - len(complete),
            "rows": list(rows.values()),
            "spread_complete": {k: spread([r[k] for r in complete if k in r]) for k in keys},
            "not_measured": list(NOT_MEASURED),
            "note": "durations are measured spans; unaccounted_ms = total_ms - sum(top-level phases) is an interval, "
                    "not a duration; wait_ms is time until OpenCode's agent loop was idle, not model inference"}


def render(summary: dict) -> str:
    lines = [f"turns={summary['turns']} complete={summary['complete']} incomplete={summary['incomplete']}", ""]
    header = ("turn", "state", "total", "queue", "lock", "prep", "ask", "prompt", "wait", "lookup", "commit", "tools",
              "chars")
    lines.append(" ".join(f"{h:>9}" for h in header))
    for r in summary["rows"]:
        cells = [r["turn_id"][-8:], (r.get("final_state") or "?") + ("" if r["complete"] else "*"),
                 r.get("total_ms"), r.get("queue_wait_ms"), r.get("lock_wait_ms"), r.get("prepare_ms"),
                 r.get("ask_ms"), r.get("prompt_admit_ms"), r.get("wait_ms"), r.get("lookup_ms"), r.get("commit_ms"),
                 f"{r.get('tools', 0)}({r.get('tools_verified', 0)})", r.get("total_chars")]
        lines.append(" ".join(f"{('-' if c is None else (round(c, 1) if isinstance(c, float) else c))!s:>9}" for c in cells))
    lines += ["", "(ms; * = incomplete: no terminal event, never counted as success)", "", "spread over complete turns:"]
    for key, stats in summary["spread_complete"].items():
        if stats:
            lines.append(f"  {key:18} n={stats['n']} min={stats['min']} median={stats['median']} p90={stats['p90']} max={stats['max']}")
    lines += ["", "NOT MEASURED: " + "; ".join(summary["not_measured"]), summary["note"]]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("path", type=Path)
    parser.add_argument("--json", action="store_true", help="print machine-readable JSON")
    args = parser.parse_args(argv)
    summary = summarize(load(args.path))
    print(json.dumps(summary, indent=2) if args.json else render(summary))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
