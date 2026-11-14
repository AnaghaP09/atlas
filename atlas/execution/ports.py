"""Interfaces the execution layer needs from other components.

The definitions live in ``atlas.contracts``; this module re-exports them for the execution code.
Security scans come from the security connector, gate decisions from the security gate, the
authoritative verification generation from the run service, diff reviews from the verifier, remote
writes from the publication gateway and post-publication evidence from the validation store.
"""

from atlas.contracts import (  # noqa: F401
    DiffReviewer,
    EvidenceRecord,
    PublicationAPI,
    PullRequest,
    RepoIdentity,
    ReviewResult,
    RunState,
    SecurityDecision,
    SecurityGate,
    SecurityScanner,
    ValidationEvidence,
)
