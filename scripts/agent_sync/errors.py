"""Typed failures for agent prompt/report sync.

The split matters for the local-success / remote-failure invariant
(``docs/agent_reporting.md``): a pre-flight/validation failure
(:class:`AgentSyncError`) must **never** be recorded as a transport failure or
mark an artifact ``failed`` -- the artifact was simply never eligible to be
transported. Anything else (auth/network/Drive API) is a real, retryable
transport failure the caller records as ``failed`` while leaving the local
artifact untouched.
"""

from __future__ import annotations


class AgentSyncError(Exception):
    """Base for pre-flight/validation failures that must not mark sync state
    ``failed`` (no transport was attempted against a valid artifact)."""


class SyncConfigError(AgentSyncError):
    """The sync configuration is missing or unusable (e.g. an unset Drive
    folder ID, or an invalid config file)."""


class PromptLookupError(AgentSyncError):
    """A Drive prompt-draft lookup could not be resolved unambiguously: zero
    matches, or more than one match. Always fails closed."""


class ReportValidationError(AgentSyncError):
    """A report is not eligible for transport: missing, untracked, not
    finalized, or mutated after finalization."""


class DriveFormatError(AgentSyncError):
    """A Drive draft exists but is not in the one canonical draft format
    (Markdown), so it cannot be frozen faithfully."""


class ReportPushError(Exception):
    """A real transport/reconciliation failure for ``reports-push`` (remote
    missing, remote mismatch, ambiguous match). The caller records it as a
    retryable ``failed`` sync state while leaving the local report untouched.

    Deliberately *not* an :class:`AgentSyncError`: it happens after a valid,
    finalized report was found, so it is transport state, not a pre-flight
    refusal.
    """

