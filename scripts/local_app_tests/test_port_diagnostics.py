"""Read-only occupied-port diagnostics (P015 amendment C). Simulated /proc trees plus the real current process."""
from __future__ import annotations

import os
import socket
import tempfile
import unittest
from pathlib import Path

from scripts import local_app as la

PORT = 8010
TCP_HEADER = "  sl  local_address rem_address   st tx_queue rx_queue tr tm->when retrnsmt   uid  timeout inode\n"


def tcp_line(port: int, inode: str, state: str = "0A", address: str = "0100007F") -> str:
    return (f"   0: {address}:{port:04X} 00000000:0000 {state} 00000000:00000000 00:00000000 00000000  1000 0 "
            f"{inode} 1 0000000000000000 100 0 0 10 0\n")


class FakeProc:
    def __init__(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        (self.root / "net").mkdir()
        (self.root / "net" / "tcp").write_text(TCP_HEADER)
        (self.root / "net" / "tcp6").write_text(TCP_HEADER)

    def listen(self, port: int, inode: str, **kw) -> None:
        with open(self.root / "net" / "tcp", "a") as handle:
            handle.write(tcp_line(port, inode, **kw))

    def process(self, pid: int, comm: str, argv: list[str], inode: str | None = None, readable: bool = True) -> None:
        folder = self.root / str(pid)
        (folder / "fd").mkdir(parents=True)
        (folder / "comm").write_text(comm + "\n")
        (folder / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")
        if inode is not None:
            os.symlink(f"socket:[{inode}]", folder / "fd" / "3")
        if not readable:
            os.chmod(folder / "fd", 0)

    def close(self) -> None:
        for fd in self.root.glob("*/fd"):
            os.chmod(fd, 0o700)
        self.tmp.cleanup()


class ListenerDiagnosticsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = FakeProc()
        self.addCleanup(self.proc.close)

    def test_foreign_listener_is_identified_as_unknown_without_leaking_argv(self) -> None:
        self.proc.listen(PORT, "555")
        self.proc.process(4242, "python3", ["python3", "-m", "http.server", "--token=SECRET-VALUE"], inode="555")
        text = la.listener_diagnostics(PORT, self.proc.root)
        self.assertIn("pid 4242", text)
        self.assertIn("identity UNKNOWN", text)
        self.assertNotIn("SECRET-VALUE", text)
        self.assertNotIn("http.server", text)

    def test_old_rosc_backend_is_only_said_to_resemble_one(self) -> None:
        self.proc.listen(PORT, "777")
        self.proc.process(4300, "python", ["/x/python", "-m", "uvicorn", "rosc.app:app", "--port", str(PORT)], inode="777")
        text = la.listener_diagnostics(PORT, self.proc.root)
        self.assertIn("resembles a ROSC backend", text)
        self.assertIn("NOT verified", text)
        self.assertNotIn("owned", text.replace("not own", ""))

    def test_inaccessible_process_keeps_an_honest_manual_inspection_note(self) -> None:
        if os.geteuid() == 0:
            self.skipTest("root can read every fd table")
        self.proc.listen(PORT, "888")
        self.proc.process(4400, "other", ["other"], inode="888", readable=False)
        text = la.listener_diagnostics(PORT, self.proc.root)
        self.assertIn("not visible to this user", text)
        self.assertIn("inspect it manually", text)
        self.assertNotIn("pid 4400", text)

    def test_other_ports_and_non_listening_sockets_are_ignored(self) -> None:
        self.proc.listen(PORT + 1, "1")
        self.proc.listen(PORT, "2", state="01")  # ESTABLISHED, not LISTEN
        self.proc.process(4500, "x", ["x"], inode="1")
        self.assertEqual(la.listener_diagnostics(PORT, self.proc.root), "")

    def test_missing_proc_returns_nothing_and_never_raises(self) -> None:
        self.assertEqual(la.listener_diagnostics(PORT, self.proc.root / "absent"), "")

    def test_real_proc_finds_the_current_process_listener(self) -> None:
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            sock.listen(1)
            text = la.listener_diagnostics(sock.getsockname()[1])
        self.assertIn(f"pid {os.getpid()}", text)

    def test_message_includes_diagnostics_and_still_refuses_to_touch_anything(self) -> None:
        message = la.occupied_message("backend", PORT, "\n  Listener: pid 1")
        self.assertIn("already in use by a process this launcher does not own", message)
        self.assertIn("It was not touched", message)
        self.assertIn("Listener: pid 1", message)
        self.assertEqual(la.occupied_message("backend", PORT).count("Listener"), 0)


if __name__ == "__main__":
    unittest.main()
