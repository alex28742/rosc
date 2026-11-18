# Worker definitions

`ruleset.yaml` pins the universal Worker Role Rules and Worker Execution Protocol (`rules/`) and the Worker Artifact Tool
(`tools/rosc_run_tool.py`) by `version`, SHA-256 and size; they are versioned and reviewed with the ROSC code (no separate
rules-approval workflow).  Nothing is loaded into a Worker CLI from this directory: ROSC Core verifies the listed
bytes/digests at Run launch and publishes an exact copy in that Run's offline package; the Worker reads the rules from
there with its native Read tool (the CLI's own hooks record that read) and runs the package's copy of the tool.

The Run manifest's `operative_stages` states what ROSC supports for that Run.  Since 0.2.0 (P054B) newly created packages
make E0-E6 operative: E0 receive/acknowledge, then - after ROSC Core accepted the ACK and sent the separate execution
line - E1-E6 with every Plan/Journal/Checkpoint/test/Report/terminal artifact written only by the pinned tool.  Packages
created under 0.1.0 stay E0-only (E1-E6 are not operative there) and are never rewritten or upgraded.  A stage that a
package does not list in `operative_stages` is not operative for that Run.

No Tool right or Core authority is granted by any file in this directory.
