"""Synthetic-scenario tests for read-path telemetry; no network, no DOUTLAB access."""
from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from scripts.agent_sync import artifacts, state as sync_state
from scripts.read_path import cli, ledger, trace

DOCS = {
    "docs/README.md": "# Docs\n[workflow](development_workflow.md)\n[ctx](ctx/README.md)\n[other](other/README.md)\n",
    "docs/development_workflow.md": "# Workflow\n[home](README.md)\n",
    "docs/ctx/README.md": "# Ctx\n[contract](contract.md)\n[arch](../architecture.md)\n",
    "docs/ctx/contract.md": "# Contract\n",
    "docs/architecture.md": "# Arch\n",
    "docs/other/README.md": "# Other\n[x](unrelated.md)\n",
    "docs/other/unrelated.md": "# Unrelated\n",
    "docs/orphan.md": "# Orphan, linked from nowhere\n",
}


class Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        for rel, text in DOCS.items():
            (self.root / rel).parent.mkdir(parents=True, exist_ok=True)
            (self.root / rel).write_text(text)
        self.runs = self.root / ".agent-telemetry" / "read-path"
        self.ledger = self.root / ".agent-telemetry" / "read-path-state.json"

    def start(self, mode="COLD", owners=("docs/ctx/README.md",), run_id="p1__run__r01"):
        return trace.init_run(
            repo_root=self.root, runs_dir=self.runs, run_id=run_id, prompt_id="1",
            report=f"{run_id}.md", context_mode=mode, owners=list(owners), head="a" * 40, branch="b",
        )

    def read(self, file, path, category="OWNER", **kw):
        return trace.record_read(repo_root=self.root, file=file, state_path=self.ledger, path=path, category=category, **kw)

    def analysis(self, file):
        return trace.analyze(repo_root=self.root, file=file)


class ColdRouting(Base):
    def test_cold_bootstrap_owner_dependency_chain_has_no_findings(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.read(f, "docs/development_workflow.md", "BOOTSTRAP")
        self.read(f, "docs/ctx/README.md", "OWNER")
        self.read(f, "docs/ctx/contract.md", "DEPENDENCY")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["verdict"], "NO-FINDINGS")
        self.assertEqual(a["coverage"], "3 full, 0 freshness-only reuse, 0 partial, 0 not-recorded of 3 obligations")
        self.assertEqual([c["via"] for c in a["chain"]], ["bootstrap", "bootstrap", "docs/README.md", "docs/ctx/README.md"])
        self.assertEqual(a["categories"]["DEPENDENCY"], 1)

    def test_conditional_contract_missed_and_irrelevant_not_flagged(self):
        f = self.start()
        trace.record_obligation(repo_root=self.root, file=f, path="docs/ctx/contract.md", trigger="ctx README: when task touches X")
        for p, c in (("docs/README.md", "BOOTSTRAP"), ("docs/development_workflow.md", "BOOTSTRAP"), ("docs/ctx/README.md", "OWNER")):
            self.read(f, p, c)
        trace.end_run(file=f, head="b" * 40)
        findings = self.analysis(f)["findings"]
        # Declared-but-unrecorded is "not-recorded", and the unrelated other/ context is never required.
        self.assertEqual(findings["not_recorded_obligations"], ["docs/ctx/contract.md"])
        self.assertNotIn("docs/other/README.md", findings["not_recorded_obligations"])
        self.assertEqual(findings["router_misses"], [])

    def test_detour_and_router_miss_then_recovery(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.read(f, "docs/development_workflow.md", "BOOTSTRAP")
        self.read(f, "docs/orphan.md", "DETOUR")  # found by search, no router link
        self.read(f, "docs/ctx/README.md", "OWNER")  # back on the documented route
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["findings"]["router_misses"], ["docs/orphan.md"])
        self.assertEqual(a["findings"]["detours"], 1)
        self.assertEqual(a["chain"][-1]["via"], "docs/README.md")
        self.assertEqual(a["verdict"], "ATTENTION")

    def test_repeats_justified_revisit_versus_potential_cycle(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.read(f, "docs/ctx/README.md", "OWNER")
        self.read(f, "docs/ctx/README.md", "OWNER", reason="re-check section after edit")
        self.read(f, "docs/README.md", "BOOTSTRAP")  # back, unexplained, after another doc
        trace.end_run(file=f, head="b" * 40)
        dups = {d["seq"]: d for d in self.analysis(f)["duplicates"]}
        self.assertTrue(dups[4]["justified"])
        self.assertFalse(dups[5]["justified"])
        self.assertEqual([c["seq"] for c in self.analysis(f)["findings"]["potential_cycles"]], [5])

    def test_changed_content_makes_a_repeat_justified(self):
        f = self.start()
        self.read(f, "docs/ctx/contract.md")
        (self.root / "docs/ctx/contract.md").write_text("# Contract v2\n")
        self.read(f, "docs/ctx/contract.md")
        self.assertTrue(self.analysis(f)["duplicates"][0]["justified"])

    def test_only_documentation_reads_are_loggable(self):
        f = self.start()
        for bad in ("scripts/read_path/cli.py", "docs/notes.txt", "../outside.md", "AGENTS.md"):
            with self.assertRaises(trace.TraceError, msg=bad):
                self.read(f, bad)


class WarmFreshness(Base):
    def test_reuse_requires_warm_and_matching_hash(self):
        cold = self.start("COLD")
        with self.assertRaises(trace.TraceError):
            trace.record_reuse(repo_root=self.root, file=cold, state_path=self.ledger, path="docs/ctx/README.md")

        self.read(cold, "docs/ctx/README.md")
        warm = self.start("WARM", run_id="p1__run2__r01")
        ev = trace.record_reuse(repo_root=self.root, file=warm, state_path=self.ledger, path="docs/ctx/README.md")
        self.assertEqual(ev["classification"], ledger.REUSED_VALID)
        self.assertFalse(ev["fresh_read_required"])

    def test_stale_unknown_missing_all_require_fresh_read_and_mention_never_promotes(self):
        cold = self.start("COLD")
        self.read(cold, "docs/ctx/README.md")
        (self.root / "docs/ctx/README.md").write_text("# changed\n")
        warm = self.start("WARM", run_id="w__r01")
        cases = {"docs/ctx/README.md": ledger.STALE, "docs/architecture.md": ledger.UNKNOWN}
        for path, expected in cases.items():
            ev = trace.record_reuse(repo_root=self.root, file=warm, state_path=self.ledger, path=path)
            self.assertEqual(ev["classification"], expected)
            self.assertTrue(ev["fresh_read_required"])
        # Re-classifying (a "mention") does not rewrite the ledger.
        self.assertEqual(ledger.classify(repo_root=self.root, rel_path="docs/ctx/README.md", state_path=self.ledger)[0], ledger.STALE)
        (self.root / "docs/ctx/contract.md").unlink()
        self.assertEqual(ledger.classify(repo_root=self.root, rel_path="docs/ctx/contract.md", state_path=self.ledger)[0], ledger.MISSING)
        self.read(warm, "docs/ctx/README.md")  # fresh read restores validity
        self.assertEqual(ledger.classify(repo_root=self.root, rel_path="docs/ctx/README.md", state_path=self.ledger)[0], ledger.REUSED_VALID)

    def test_deleted_document_is_failed_read_and_never_confirmed(self):
        f = self.start()
        self.read(f, "docs/ctx/contract.md")
        (self.root / "docs/ctx/contract.md").unlink()
        ev = self.read(f, "docs/ctx/contract.md")
        self.assertEqual(ev["outcome"], "failed")
        self.assertEqual(self.analysis(f)["findings"]["failed_reads"], ["docs/ctx/contract.md"])

    def test_corrupt_ledger_is_unknown_not_valid(self):
        self.ledger.parent.mkdir(parents=True, exist_ok=True)
        self.ledger.write_text("not json")
        self.assertEqual(ledger.classify(repo_root=self.root, rel_path="docs/README.md", state_path=self.ledger)[0], ledger.UNKNOWN)
        self.ledger.write_text(json.dumps({"documents": {"docs/README.md": {"content_id": "x", "read_at": "now"}}}))
        self.assertEqual(ledger.classify(repo_root=self.root, rel_path="docs/README.md", state_path=self.ledger)[0], ledger.UNKNOWN)


class TruthfulDegradation(Base):
    def test_missing_empty_and_corrupt_streams_never_pass(self):
        missing = self.runs / "nope.jsonl"
        self.assertEqual(self.analysis(missing)["verdict"], "UNAVAILABLE")
        self.runs.mkdir(parents=True)
        corrupt = self.runs / "bad.jsonl"
        corrupt.write_text("garbage\n{\"no\": \"type\"}\n[1]\n")
        a = self.analysis(corrupt)
        self.assertEqual(a["verdict"], "UNAVAILABLE")
        self.assertEqual(a["corrupt_lines"], 3)
        self.assertIn("UNAVAILABLE", cli.render_summary(a, "x"))

    def test_started_but_no_reads_and_unclosed_runs_are_not_clean(self):
        f = self.start()
        self.assertEqual(self.analysis(f)["verdict"], "UNAVAILABLE")
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.assertEqual(self.analysis(f)["verdict"], "INCOMPLETE")

    def test_partial_corruption_is_reported_and_survivors_used(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        with f.open("a") as h:
            h.write("{broken\n")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["corrupt_lines"], 1)
        self.assertEqual(a["reads"], 1)

    def test_run_is_never_overwritten_and_closed_run_rejects_events(self):
        f = self.start()
        with self.assertRaises(trace.TraceError):
            self.start()
        trace.end_run(file=f, head="b" * 40)
        with self.assertRaises(trace.TraceError):
            self.read(f, "docs/README.md", "BOOTSTRAP")


class ReportCompatibility(Base):
    def make_report(self, name="p1__run__r01", prompt_id="1"):
        text = artifacts.render_report_skeleton(
            prompt_id=prompt_id, title="t", prompt_snapshot="p.md", prompt_run=1, prompt_sha256="0" * 64,
            model="m", branch="b", base_commit="a" * 40,
        )
        path = self.root / f"{name}.md"
        path.write_text(text)
        return path

    def test_attach_keeps_standard_tail_valid_and_leaves_other_sections(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        trace.end_run(file=f, head="b" * 40)
        report = self.make_report()
        before = report.read_text()
        state_path = self.root / ".agent-telemetry" / "agent-sync" / "state.json"
        cli.attach_summary(report=report, repo_root=self.root, sync_state_path=state_path, run_file=f)
        after = report.read_text()
        self.assertEqual(after.split("## Telemetry")[0], before.split("## Telemetry")[0])
        self.assertIn("Read-path telemetry: enabled", after)
        self.assertNotIn("docs/README.md", after)  # summary only; no raw rows in the report
        self.assertEqual(artifacts.normalize_report_tail(after), after)

    def test_attach_refuses_finalized_report(self):
        f = self.start()
        report = self.make_report()
        state_path = self.root / ".agent-telemetry" / "agent-sync" / "state.json"
        sync_state.record_artifact(state_path, artifact_type="report", local_path="p1__run__r01.md", sha256="0" * 64, created_at="t")
        sync_state.mark_report_finalized(state_path, local_path="p1__run__r01.md", sha256="0" * 64, finalized_at="t")
        before = report.read_bytes()
        with self.assertRaises(trace.TraceError):
            cli.attach_summary(report=report, repo_root=self.root, sync_state_path=state_path, run_file=f)
        self.assertEqual(report.read_bytes(), before)

    def test_raw_events_hold_no_secrets_only_paths_hashes_and_labels(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        keys = {k for e in trace.read_events(f)[0] for k in e}
        self.assertLessEqual(keys, {"seq", "ts", "type", "schema", "run_id", "prompt_id", "report", "context_mode", "owners",
                                    "head", "branch", "path", "category", "outcome", "sha256", "bytes", "reason", "source"})

    def test_repo_gitignores_runtime_output_and_bootstrap_names_match(self):
        repo = trace.REPO_ROOT
        self.assertIn(".agent-telemetry/", (repo / ".gitignore").read_text().splitlines())
        agents = (repo / "AGENTS.md").read_text()
        self.assertIn("docs/README.md", agents)
        self.assertIn("docs/development_workflow.md", agents)
        for root in trace.BOOTSTRAP_ROOTS:
            self.assertTrue((repo / root).is_file())


class FollowUpRegressions(Base):
    """P005.1 follow-up: coverage honesty, residency, bootstrap files, attach identity, route certainty."""

    make_report = ReportCompatibility.make_report

    def full_bootstrap(self, f):
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.read(f, "docs/development_workflow.md", "BOOTSTRAP")

    # 1. partial is not full
    def test_partial_read_of_required_contract_is_not_counted_as_full(self):
        f = self.start()
        trace.record_obligation(repo_root=self.root, file=f, path="docs/ctx/contract.md", trigger="ctx README: when X")
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER")
        self.read(f, "docs/ctx/contract.md", "DEPENDENCY", outcome="partial")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["obligations"]["docs/ctx/contract.md"]["status"], "partial")
        self.assertEqual(a["coverage"], "3 full, 0 freshness-only reuse, 1 partial, 0 not-recorded of 4 obligations")
        self.assertEqual(a["findings"]["partial_obligations"], ["docs/ctx/contract.md"])
        self.assertEqual(a["verdict"], "ATTENTION")
        self.assertIn("partial-only obligations: 1", cli.render_summary(a, "raw"))

    def test_full_read_after_partial_upgrades_and_absent_stays_distinct(self):
        f = self.start()
        trace.record_obligation(repo_root=self.root, file=f, path="docs/ctx/contract.md", trigger="t")
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER", outcome="partial")
        self.read(f, "docs/ctx/README.md", "OWNER", reason="finish reading")
        a = self.analysis(f)
        self.assertEqual(a["obligations"]["docs/ctx/README.md"]["status"], "full")
        self.assertEqual(a["obligations"]["docs/ctx/contract.md"]["status"], "not-recorded")

    # 2. freshness is not residency
    def test_reuse_valid_is_separate_from_fresh_read_and_residency_is_never_verified(self):
        cold = self.start("COLD")
        self.read(cold, "docs/ctx/README.md")
        warm = self.start("WARM", run_id="w__r01")
        self.read(warm, "docs/README.md", "BOOTSTRAP")
        self.read(warm, "docs/development_workflow.md", "BOOTSTRAP")
        ev = trace.record_reuse(repo_root=self.root, file=warm, state_path=self.ledger, path="docs/ctx/README.md", residency_claimed=True)
        self.assertEqual(ev["classification"], ledger.REUSED_VALID)
        self.assertEqual((ev["residency"], ev["residency_verified"]), ("agent-claimed", False))
        trace.end_run(file=warm, head="b" * 40)
        a = self.analysis(warm)
        self.assertEqual(a["obligations"]["docs/ctx/README.md"]["status"], "reused-valid-claimed")
        self.assertEqual(a["reads"], 2)  # reuse is not a fresh read
        self.assertIn("1 freshness-only reuse", a["coverage"])
        self.assertIn("not that its text is in the agent's context", cli.render_summary(a, "raw"))

    def test_reuse_without_residency_claim_is_labelled_unclaimed(self):
        cold = self.start("COLD")
        self.read(cold, "docs/ctx/README.md")
        warm = self.start("WARM", run_id="w__r01")
        ev = trace.record_reuse(repo_root=self.root, file=warm, state_path=self.ledger, path="docs/ctx/README.md")
        self.assertEqual(ev["residency"], "not-claimed")
        self.assertEqual(self.analysis(warm)["obligations"]["docs/ctx/README.md"]["status"], "reused-valid-unclaimed")

    # 3. bootstrap files, and only those
    def test_agents_and_claude_register_as_bootstrap_only(self):
        (self.root / "AGENTS.md").write_text("# agents\n")
        (self.root / "CLAUDE.md").write_text("# claude\n")
        (self.root / "README.md").write_text("# not a docs read\n")
        f = self.start()
        self.assertEqual(self.read(f, "AGENTS.md", "BOOTSTRAP")["outcome"], "full")
        self.read(f, "CLAUDE.md", "BOOTSTRAP")
        with self.assertRaises(trace.TraceError):
            self.read(f, "AGENTS.md", "OWNER")
        for bad in ("README.md", "Makefile", "scripts/read_path/cli.py", "docs/../README.md", "AGENTS.md.bak"):
            with self.assertRaises(trace.TraceError, msg=bad):
                self.read(f, bad, "BOOTSTRAP")
        self.read(f, "docs/README.md", "BOOTSTRAP")
        self.read(f, "docs/development_workflow.md", "BOOTSTRAP")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual([c["route"] for c in a["chain"][:2]], ["bootstrap", "bootstrap"])

    # 4. attach identity and corrupt journals
    def test_attach_rejects_mismatched_run_report_or_prompt(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP")
        trace.end_run(file=f, head="b" * 40)
        state_path = self.root / ".agent-telemetry" / "agent-sync" / "state.json"
        good = self.make_report()
        other_name = self.make_report(name="p2__other__r01")
        other_prompt = self.make_report(name="p1__run__r01_x", prompt_id="9")
        for report in (other_name, other_prompt):
            before = report.read_bytes()
            with self.assertRaises(trace.TraceError):
                cli.attach_summary(report=report, repo_root=self.root, sync_state_path=state_path, run_file=f)
            self.assertEqual(report.read_bytes(), before)
        # Run header names a different prompt than the report.
        wrong = self.start(run_id="p1__run__r02")
        text = wrong.read_text().replace('"prompt_id": "1"', '"prompt_id": "7"')
        wrong.write_text(text)
        report2 = self.make_report(name="p1__run__r02")
        with self.assertRaises(trace.TraceError):
            cli.attach_summary(report=report2, repo_root=self.root, sync_state_path=state_path, run_file=wrong)
        self.assertEqual(cli.attach_summary(report=good, repo_root=self.root, sync_state_path=state_path, run_file=f), "p1__run__r01.md")

    def test_corrupt_journal_line_never_yields_no_findings(self):
        f = self.start()
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER")
        trace.end_run(file=f, head="b" * 40)
        self.assertEqual(self.analysis(f)["verdict"], "NO-FINDINGS")
        with f.open("a") as h:
            h.write("{truncated\n")
        a = self.analysis(f)
        self.assertEqual(a["verdict"], "INCONCLUSIVE")
        self.assertIn("cannot be NO-FINDINGS", cli.render_summary(a, "raw"))

    # 5. changed / partly-read routers
    def test_router_changed_after_read_makes_route_uncertain_not_a_miss_or_confirmed(self):
        f = self.start()
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER")
        # The router drops its link after being read; the later read cannot be confirmed or called a miss.
        (self.root / "docs/ctx/README.md").write_text("# Ctx rewritten, no links\n")
        self.read(f, "docs/ctx/contract.md", "DEPENDENCY")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        row = a["chain"][-1]
        self.assertEqual((row["route"], row["via"]), ("uncertain", None))
        self.assertEqual(a["findings"]["router_misses"], [])
        self.assertEqual(a["uncertainties"]["route_uncertain"], ["docs/ctx/contract.md"])
        self.assertEqual(a["verdict"], "INCONCLUSIVE")
        self.assertIn("Routing uncertain", cli.render_summary(a, "raw"))

    def test_router_changed_to_add_a_link_is_not_credited_retroactively(self):
        f = self.start()
        self.full_bootstrap(f)
        self.read(f, "docs/orphan.md", "DETOUR")  # nothing read links here
        # An edit that later links the orphan must not rewrite history...
        (self.root / "docs/README.md").write_text(DOCS["docs/README.md"] + "[o](orphan.md)\n")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["chain"][-1]["route"], "uncertain")  # README changed since read: not claimable as a miss
        self.assertEqual(a["findings"]["router_misses"], [])

    def test_partly_read_router_does_not_vouch_for_its_links(self):
        f = self.start()
        self.read(f, "docs/README.md", "BOOTSTRAP", outcome="partial")
        self.read(f, "docs/ctx/README.md", "OWNER")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual(a["chain"][-1]["route"], "uncertain")
        self.assertIsNone(a["obligations"]["docs/ctx/README.md"]["router_cited"])

    def test_unchanged_full_router_still_gives_confident_route_and_confident_miss(self):
        f = self.start()
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER")
        self.read(f, "docs/orphan.md", "DETOUR")
        trace.end_run(file=f, head="b" * 40)
        a = self.analysis(f)
        self.assertEqual((a["chain"][2]["route"], a["chain"][2]["via"]), ("verified", "docs/README.md"))
        self.assertEqual(a["findings"]["router_misses"], ["docs/orphan.md"])
        self.assertEqual(a["verdict"], "ATTENTION")

    def test_deleted_router_makes_route_uncertain(self):
        f = self.start()
        self.full_bootstrap(f)
        self.read(f, "docs/ctx/README.md", "OWNER")
        (self.root / "docs/ctx/README.md").unlink()
        self.read(f, "docs/ctx/contract.md", "DEPENDENCY")
        self.assertEqual(self.analysis(f)["chain"][-1]["route"], "uncertain")


if __name__ == "__main__":
    unittest.main()

