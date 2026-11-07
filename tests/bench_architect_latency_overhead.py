"""P024 offline micro-benchmark (NOT a test: the name is deliberately not ``test_*``; run it explicitly).

    .venv/bin/python3 -m unittest discover -s tests -p 'bench_architect_latency_overhead.py' -v

Measures the ROSC-side cost of the diagnostics with the fake OpenCode (no model, no provider): (1) one ``emit`` enabled vs
disabled; (2) a whole Owner message -> delivered turn with tracing alternately OFF and ON in the SAME process/session.
The fake answers instantly, so (2) is the worst case for relative overhead.  Numbers are machine-specific.
"""
from __future__ import annotations

import json
import os
import statistics
import tempfile
import time
import unittest
from pathlib import Path

import test_p022_architect_multistep_reply as p22
from rosc.architect_latency import DISABLED, LatencyTrace

TURNS = int(os.environ.get("P024_BENCH_TURNS", "60"))
EMITS = 20000


def stats(values: list[float]) -> str:
    values = sorted(values)
    return (f"n={len(values)} median={statistics.median(values):.3f} p90={values[int(0.9 * (len(values) - 1))]:.3f} "
            f"min={values[0]:.3f} max={values[-1]:.3f} stdev={statistics.pstdev(values):.3f}")


class EmitCost(unittest.TestCase):
    def test_emit_cost(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            on = LatencyTrace(Path(tmp) / "d")
            for label, trace in (("disabled", DISABLED), ("enabled", on)):
                runs = []
                for _ in range(5):
                    start = time.perf_counter()
                    for _ in range(EMITS):
                        trace.emit("probe", duration_ms=1.25, outcome="ok", project_id=1, chars=100)
                    runs.append((time.perf_counter() - start) / EMITS * 1e6)
                print(f"\nemit {label}: microseconds/event {stats(runs)}")


class TurnCost(p22.ContextCase):
    def test_turn_cost_off_vs_on(self) -> None:
        self.activate()
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        on = LatencyTrace(Path(tmp.name) / "d")

        def switch(trace: LatencyTrace) -> None:
            self.architect._trace = self.host._trace = self.runtime._diag = trace

        (self.bin / "mode").write_text("ok")
        samples: dict[str, list[float]] = {"off": [], "on": []}
        for index in range(TURNS * 2):
            label = "on" if index % 2 else "off"
            switch(on if label == "on" else DISABLED)
            start = time.perf_counter()
            sent = self.client.post("/api/projects/P0001/conversation/messages",
                                    json={"client_key": f"bench-{index:06d}", "body": "x"})
            self.assertEqual(sent.status_code, 201)
            self.architect.drain()
            samples[label].append((time.perf_counter() - start) * 1000.0)
        switch(DISABLED)
        print("\nturn (ms) tracing OFF:", stats(samples["off"]))
        print("turn (ms) tracing ON :", stats(samples["on"]))
        print(f"median difference ON-OFF: {statistics.median(samples['on']) - statistics.median(samples['off']):.3f} ms")
        print("trace file bytes:", on.path.stat().st_size, "events:", len(on.path.read_text().splitlines()))


if __name__ == "__main__":
    unittest.main()
