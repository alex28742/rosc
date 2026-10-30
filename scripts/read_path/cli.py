"""``python3 -m scripts.read_path.cli`` -- record and summarize a run's doc read path."""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

from scripts.agent_sync import state as sync_state

from . import trace

SECTION_MARKER = "## Telemetry\n\n"


def _git(*args: str) -> str:
    out = subprocess.run(["git", "-C", str(trace.REPO_ROOT), *args], capture_output=True, text=True)
    return out.stdout.strip() if out.returncode == 0 else "unknown"


def render_summary(analysis: dict, raw_path: str) -> str:
    """Concise Telemetry-section text for a report; never the raw log."""

    if analysis.get("verdict") == "UNAVAILABLE" and "run_id" not in analysis:
        return f"Read-path telemetry UNAVAILABLE: {analysis.get('reason')}. Raw: `{raw_path}`. No routing claim is made."
    f = analysis["findings"]
    c = analysis["categories"]
    lines = [
        f"Read-path telemetry: enabled. Context mode: {analysis['context_mode']}. Verdict: {analysis['verdict']}.",
        f"Raw events (local, gitignored): `{raw_path}`",
        f"Reads {analysis['reads']} (BOOTSTRAP {c['BOOTSTRAP']}, OWNER {c['OWNER']}, "
        f"DEPENDENCY {c['DEPENDENCY']}, DETOUR {c['DETOUR']}); reuse events {analysis['reuses']} "
        "(hash match shows the file is unchanged, not that its text is in the agent's context).",
        f"Coverage: {analysis['coverage']}. Evidence: {analysis['confidence']}.",
        f"Router-miss candidates: {len(f['router_misses'])}; unexplained repeats: {len(f['unexplained_repeats'])}; "
        f"potential cycles: {len(f['potential_cycles'])}; partial-only obligations: {len(f['partial_obligations'])}; "
        f"not-recorded obligations: {len(f['not_recorded_obligations'])} "
        f"(not recorded is not proof of a skipped read); failed reads: {len(f['failed_reads'])}.",
    ]
    u = analysis["uncertainties"]
    if u["route_uncertain"] or u["obligation_citation_uncertain"]:
        lines.append(
            f"Routing uncertain for {len(u['route_uncertain'])} read(s) and {len(u['obligation_citation_uncertain'])} "
            "obligation citation(s): a router changed since it was read or was only partly read."
        )
    if analysis["corrupt_lines"]:
        lines.append(f"Warning: {analysis['corrupt_lines']} unreadable raw line(s) ignored; verdict cannot be NO-FINDINGS.")
    return "\n".join(lines)


def _run(args) -> Path:
    return trace.run_path(args.run)


def cmd_init(args) -> int:
    report = Path(args.report)
    if not report.is_file():
        print(f"ERROR: report not found: {args.report}", file=sys.stderr)
        return 2
    rel_report = str(report.resolve().relative_to(trace.REPO_ROOT.resolve()))
    file = trace.init_run(
        repo_root=trace.REPO_ROOT, runs_dir=trace.RUNS_DIR, run_id=report.stem, prompt_id=args.prompt,
        report=rel_report, context_mode=args.mode, owners=args.owner or [],
        head=_git("rev-parse", "HEAD"), branch=_git("branch", "--show-current"),
    )
    print(f"READ-PATH RUN {report.stem} STARTED | mode={args.mode} | raw={file.relative_to(trace.REPO_ROOT)}")
    return 0


def cmd_read(args) -> int:
    event = trace.record_read(
        repo_root=trace.REPO_ROOT, file=_run(args), state_path=trace.LEDGER_PATH,
        path=args.path, category=args.category, outcome=args.outcome, reason=args.reason,
    )
    print(f"READ #{event['seq']} {event['outcome']} {event['category']} {event['path']}")
    return 0 if event["outcome"] != "failed" else 1


def cmd_reuse(args) -> int:
    event = trace.record_reuse(repo_root=trace.REPO_ROOT, file=_run(args), state_path=trace.LEDGER_PATH, path=args.path, note=args.note, residency_claimed=args.residency_claimed)
    print(f"REUSE {event['classification'].upper()} {event['path']}" + (" | fresh read required" if event["fresh_read_required"] else ""))
    return 0 if not event["fresh_read_required"] else 1


def cmd_obligation(args) -> int:
    event = trace.record_obligation(repo_root=trace.REPO_ROOT, file=_run(args), path=args.path, trigger=args.trigger)
    print(f"OBLIGATION #{event['seq']} {event['path']}")
    return 0


def cmd_end(args) -> int:
    trace.end_run(file=_run(args), head=_git("rev-parse", "HEAD"))
    print(f"READ-PATH RUN {args.run} ENDED")
    return 0


def cmd_summary(args) -> int:
    file = _run(args)
    print(render_summary(trace.analyze(repo_root=trace.REPO_ROOT, file=file), str(file.relative_to(trace.REPO_ROOT))))
    return 0


def attach_summary(*, report: Path, repo_root: Path, sync_state_path: Path, run_file: Path) -> str:
    """Replace a *draft* report's Telemetry body with the summary; refuse finalized reports."""

    report = report.resolve()
    local = str(report.relative_to(repo_root.resolve()))
    if sync_state.finalized_at(sync_state_path, local):
        raise trace.TraceError("report is finalized and immutable; telemetry was not attached")
    text = report.read_text(encoding="utf-8")
    start = next((e for e in trace.read_events(run_file)[0] if e.get("type") == "run_start"), None)
    if start is None:
        raise trace.TraceError("run has no readable run_start; telemetry was not attached")
    match = re.search(r"(?m)^- Prompt ID: (\S+)\s*$", text)
    expected = {"run_id": report.stem, "report": local, "prompt_id": match.group(1) if match else None}
    if run_file.stem != report.stem:
        raise trace.TraceError(f"run file {run_file.stem!r} does not belong to report {report.stem!r}")
    for key, want in expected.items():
        if start.get(key) != want:
            raise trace.TraceError(f"run {key} {start.get(key)!r} does not match report ({want!r}); telemetry was not attached")
    head, marker, _ = text.rpartition(SECTION_MARKER)
    if not marker:
        raise trace.TraceError("report has no '## Telemetry' section")
    summary = render_summary(trace.analyze(repo_root=repo_root, file=run_file), str(run_file.relative_to(repo_root)))
    report.write_text(head + marker + summary + "\n", encoding="utf-8")
    return local


def cmd_attach(args) -> int:
    local = attach_summary(
        report=Path(args.report), repo_root=trace.REPO_ROOT, run_file=_run(args),
        sync_state_path=trace.TELEMETRY_ROOT / "agent-sync" / "state.json",
    )
    print(f"ATTACHED telemetry summary to {local}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python3 -m scripts.read_path.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    p = sub.add_parser("init", help="Start a run (before the first docs/ read of a COLD run).")
    p.add_argument("--prompt", required=True)
    p.add_argument("--report", required=True, help="The run's managed report draft (single source of run metadata).")
    p.add_argument("--mode", required=True, choices=trace.CONTEXT_MODES)
    p.add_argument("--owner", action="append", help="Owning router README(s) required for this task; repeatable.")
    p.set_defaults(func=cmd_init)
    for name, fn, helptext in (("read", cmd_read, "Record a documentation read just performed."),
                               ("reuse", cmd_reuse, "WARM only: log reuse of resident context."),
                               ("obligation", cmd_obligation, "Declare a router-triggered required document."),
                               ("end", cmd_end, "Close the run."), ("summary", cmd_summary, "Print the report summary."),
                               ("attach", cmd_attach, "Write the summary into a DRAFT report's Telemetry section.")):
        q = sub.add_parser(name, help=helptext)
        q.add_argument("--run", required=True)
        q.set_defaults(func=fn)
        if name == "read":
            q.add_argument("path")
            q.add_argument("--category", required=True, choices=trace.CATEGORIES)
            q.add_argument("--outcome", default="full", choices=trace.OUTCOMES)
            q.add_argument("--reason", help="Why a repeat read is justified (e.g. re-check after edit).")
        elif name == "reuse":
            q.add_argument("path")
            q.add_argument("--note")
            q.add_argument("--residency-claimed", action="store_true",
                           help="Agent asserts the text is still in its active context (unverifiable).")
        elif name == "obligation":
            q.add_argument("path")
            q.add_argument("--trigger", required=True, help="The router line that requires this document.")
        elif name == "attach":
            q.add_argument("--report", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args)
    except trace.TraceError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())

