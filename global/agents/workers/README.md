# Worker definitions

`ruleset.yaml` pins the universal Worker Role Rules and Worker Execution Protocol (`rules/`) by `version`, SHA-256 and
size; they are versioned and reviewed with the ROSC code (no separate rules-approval workflow).  Nothing is loaded into a
Worker CLI from this directory: ROSC Core verifies the listed bytes/digests at Run launch and publishes an exact copy in that Run's
offline package; the Worker reads it from there with its native Read tool, and the CLI's own hooks record that read.
The Run manifest's `operative_stages` states what ROSC supports: P054A makes only E0 (receive/acknowledge) operative;
stages E1-E6 are described but not operative until their ROSC artifacts exist (P054B).

No Tool right or Core authority is granted by any file in this directory.
