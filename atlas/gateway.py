"""Permission gateway for every connector request.

Each request carries an egress purpose and a context. The gateway checks, before any socket is
opened:

1. the mode (Offline refuses everything and counts the attempt);
2. the static policy row for the connector (host, method, path, body rule);
3. the purpose rule for the context (live ingestion only in live runs; target CI reads bound to
   a durable publication record; correction reads and writes bound to an incident and an
   authorisation).

Refused requests are recorded and reported to containment as allow-list violations. Connector
reads are retried once on a transient error; correction writes are never retried. Correction
writes are serialised per pull request, journalled before the write, refused when the live body
no longer matches the expected hash, and held for a human when the outcome is uncertain.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping
from urllib.parse import parse_qs, urlsplit

from atlas.connectors.project import SourceSet
from atlas.contracts import EgressPurpose, Mode, Role, RunMode, TaskState


GRAPHQL_OPERATIONS = frozenset(
    {
        "IssueSnapshot",
        "IssueTimeline",
        "IssueComments",
        "IssueSearch",
        "PullRequestSnapshot",
        "PullRequestTimeline",
        "ContentEditsProbe",
        "RateLimit",
        "ComponentChurn",
    }
)

OSV_QUERY_KEYS = {"package", "version"}
OSV_PACKAGE_KEYS = {"name", "ecosystem"}

CONDITIONAL_HEADERS = frozenset({"If-None-Match", "If-Modified-Since"})

POST_PUBLICATION_STATES = frozenset(
    {TaskState.PUBLISHED, TaskState.AWAITING_VALIDATION, TaskState.ACCEPTED, TaskState.FAILED}
)


class GatewayRefused(PermissionError):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class CorrectionHeld(RuntimeError):
    """The outcome of a correction write is uncertain; it is held for a human."""


@dataclass(frozen=True)
class PublicationRecord:
    destination_repo: str
    action_id: str
    published_commit: str
    pr_number: int | None
    task_state: TaskState


@dataclass(frozen=True)
class CorrectionAuthorisation:
    authorisation_id: str
    incident_id: str
    destination_repo: str
    pr_number: int
    expected_body_sha256: str
    replacement_text: str
    expires_at: datetime
    created_by: Role

    @property
    def replacement_sha256(self) -> str:
        return sha256_text(self.replacement_text)


@dataclass(frozen=True)
class RequestContext:
    purpose: EgressPurpose
    run_mode: RunMode = RunMode.LIVE
    frozen_evaluation: bool = False
    role: Role | None = None
    publication: PublicationRecord | None = None
    incident_id: str | None = None
    incident_publication: PublicationRecord | None = None
    authorisation: CorrectionAuthorisation | None = None
    action_security: tuple[str, str, str] | None = None  # (destination, action_id, revision)


@dataclass
class Response:
    status: int
    body: Any
    headers: Mapping[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class Attempt:
    connector: str
    purpose: str
    method: str
    host: str
    path: str
    allowed: bool
    reason: str
    at: str


Transport = Callable[[str, str, Mapping[str, str], Any], Response]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# GraphQL operation check
# ---------------------------------------------------------------------------


def _strip_graphql(document: str) -> str:
    out = []
    i = 0
    n = len(document)
    while i < n:
        ch = document[i]
        if ch == "#":
            while i < n and document[i] != "\n":
                i += 1
            continue
        if document.startswith('"""', i):
            end = document.find('"""', i + 3)
            i = n if end == -1 else end + 3
            out.append('""')
            continue
        if ch == '"':
            i += 1
            while i < n and document[i] != '"':
                i += 2 if document[i] == "\\" else 1
            i += 1
            out.append('""')
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def graphql_operations(document: str) -> list[tuple[str, str | None]]:
    """Top-level definitions as (type, name). Shorthand ``{ ... }`` is a query."""
    text = _strip_graphql(document)
    defs: list[tuple[str, str | None]] = []
    pending: tuple[str, str | None] | None = None
    depth = 0
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        if depth > 0:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
            i += 1
            continue
        if ch.isalpha() or ch == "_":
            word = re.match(r"[A-Za-z_][A-Za-z0-9_]*", text[i:]).group(0)
            i += len(word)
            if word in {"query", "mutation", "subscription", "fragment"}:
                nm = re.match(r"\s*([A-Za-z_][A-Za-z0-9_]*)", text[i:])
                name = None
                if nm and nm.group(1) not in {"on"}:
                    name = nm.group(1)
                    i += nm.end()
                pending = (word, name)
            continue
        if ch == "(":
            close = text.find(")", i)
            i = n if close == -1 else close + 1
            continue
        if ch == "{":
            defs.append(pending or ("query", None))
            pending = None
            depth = 1
        i += 1
    if pending is not None:
        defs.append(pending)
    return defs


def check_graphql(body: Any) -> str | None:
    """Return a refusal reason, or None if the document is one allow-listed query."""
    if not isinstance(body, Mapping) or not isinstance(body.get("query"), str):
        return "GraphQL body must be an object with a query string"
    extra = set(body) - {"query", "variables", "operationName"}
    if extra:
        return "unexpected GraphQL body fields"
    try:
        defs = graphql_operations(body["query"])
    except Exception:  # pragma: no cover - defensive
        return "GraphQL document could not be parsed"
    operations = [d for d in defs if d[0] != "fragment"]
    if len(operations) != 1:
        return "GraphQL document must contain exactly one operation"
    kind, name = operations[0]
    if kind != "query":
        return f"GraphQL {kind} operations are not allowed"
    if name not in GRAPHQL_OPERATIONS:
        return "GraphQL operation name is not on the allow-list"
    if body.get("operationName") not in (None, name):
        return "operationName does not match the operation"
    return None


def check_osv(path: str, body: Any) -> str | None:
    def one(q: Any) -> str | None:
        if not isinstance(q, Mapping) or set(q) - OSV_QUERY_KEYS or "package" not in q:
            return "OSV query may contain only package and version"
        pkg = q["package"]
        if not isinstance(pkg, Mapping) or set(pkg) - OSV_PACKAGE_KEYS or "name" not in pkg:
            return "OSV package may contain only name and ecosystem"
        if "version" in q and not isinstance(q["version"], str):
            return "OSV version must be a string"
        return None

    if path == "/v1/query":
        return one(body)
    if path == "/v1/querybatch":
        if not isinstance(body, Mapping) or set(body) != {"queries"} or not isinstance(body["queries"], list):
            return "OSV batch body must be {queries: [...]}"
        for q in body["queries"]:
            reason = one(q)
            if reason:
                return reason
        return None
    return "unknown OSV route"


# ---------------------------------------------------------------------------
# Static policy table
# ---------------------------------------------------------------------------

_TARGET_CI_PATHS = [
    re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs$"),
    re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run>\d+)$"),
    re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run>\d+)/jobs$"),
    re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/actions/runs/(?P<run>\d+)/artifacts$"),
    re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/commits/(?P<sha>[0-9a-f]{7,40})/check-runs$"),
]
_ACTIONS_PATH = re.compile(
    r"^/repos/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)/actions/(runs|runs/\d+|runs/\d+/jobs|jobs/\d+|jobs/\d+/logs)$"
)
_PR_PATH = re.compile(r"^/repos/(?P<owner>[^/]+)/(?P<repo>[^/]+)/pulls/(?P<number>\d+)$")
_REPO_PATH = re.compile(r"^/repos/(?P<owner>[A-Za-z0-9_.-]+)/(?P<repo>[A-Za-z0-9_.-]+)(/[A-Za-z0-9._~!$&'()*+,;=:@%/-]*)?$")
_SAFE_PATH = re.compile(r"^[A-Za-z0-9._~/-]+$")
_LISTING_KEYS = frozenset({"prefix", "delimiter", "marker", "max-keys"})
NO_SOURCES = SourceSet()
_OSV_VULN = re.compile(r"^/v1/vulns/[A-Za-z0-9._-]+$")


def _under(path: str, prefix: str) -> bool:
    prefix = prefix.rstrip("/")
    return bool(prefix) and path.startswith(prefix + "/") and ".." not in path and bool(_SAFE_PATH.match(path))


def policy_check(
    connector: str, method: str, url: str, body: Any, ctx: RequestContext, sources: SourceSet = NO_SOURCES
) -> str | None:
    """Static policy row plus purpose rule. Returns a refusal reason or None.

    Upstream repositories and CI artefact locations come only from the configured projects;
    with no configuration every upstream read is refused.
    """
    parts = urlsplit(url)
    if parts.scheme != "https":
        return "only https is allowed"
    host = parts.hostname or ""
    path = parts.path
    method = method.upper()
    purpose = ctx.purpose

    if purpose in (EgressPurpose.MODEL_CALL, EgressPurpose.PUBLICATION, EgressPurpose.WEBHOOK_DELIVERY):
        return f"purpose {purpose} is not handled on connector routes"

    if purpose is EgressPurpose.LIVE_INGEST:
        if ctx.run_mode is not RunMode.LIVE or ctx.frozen_evaluation:
            return "live ingestion is refused in replay and frozen evaluation runs"
        if connector in ("issues", "git"):
            if host != "api.github.com":
                return "host not allowed for this connector"
            if method == "POST" and path == "/graphql":
                return check_graphql(body)
            m = _REPO_PATH.match(path)
            if method == "GET" and m and f"{m.group('owner')}/{m.group('repo')}".lower() in sources.repositories():
                return None
            return "method or path not allowed for this connector"
        if connector == "ci":
            if method != "GET":
                return "only GET is allowed for CI artefacts"
            for rule_host, rule_prefix in sources.ci_artifact_rules() | sources.ci_view_rules():
                if host == rule_host and _under(path, rule_prefix):
                    return None
            # Folder listing of the project's own job folders (read-only, bounded by prefix).
            query = parse_qs(parts.query, keep_blank_values=True)
            for rule_host, bucket, prefixes in sources.ci_listing_rules():
                if host != rule_host or path.rstrip("/") != (bucket or "").rstrip("/"):
                    continue
                if set(query) - _LISTING_KEYS or query.get("delimiter") != ["/"]:
                    return "CI folder listing allows only prefix, delimiter, marker and max-keys"
                wanted = (query.get("prefix") or [""])[0]
                if _SAFE_PATH.match(wanted or "") and any(wanted.startswith(p) for p in prefixes if p):
                    return None
                return "CI folder listing prefix is not configured for this project"
            return "host or path not allowed for CI artefacts"
        if connector == "security":
            return _security_rule(method, host, path, body)
        if connector == "actions":
            # GitHub Actions CI of a configured upstream project: read-only run and job listings.
            if host != "api.github.com" or method != "GET":
                return "Actions reads are GET requests to api.github.com only"
            m = _ACTIONS_PATH.match(path)
            if m and f"{m.group('owner')}/{m.group('repo')}".lower() in sources.repositories():
                return None
            return "Actions route or repository not allowed"
        return "connector not allowed for live ingestion"

    if purpose is EgressPurpose.ACTION_SECURITY_INGEST:
        if connector != "security":
            return "action security ingestion is for the security connector only"
        if ctx.frozen_evaluation or ctx.action_security is None:
            return "action security ingestion needs a task context and is refused in frozen runs"
        return _security_rule(method, host, path, body)

    if purpose is EgressPurpose.TARGET_CI_READ:
        if connector != "target_ci" or method != "GET" or host != "api.github.com":
            return "target CI reads are GET requests to the bound repository only"
        if ctx.frozen_evaluation:
            return "target CI reads are refused in frozen evaluation runs"
        pub = ctx.publication
        if pub is None:
            return "no durable publication record"
        if pub.task_state not in POST_PUBLICATION_STATES:
            return "task is not in a post-publication state"
        for pattern in _TARGET_CI_PATHS:
            m = pattern.match(path)
            if not m:
                continue
            repo = f"{m.group('owner')}/{m.group('repo')}"
            if repo.lower() != pub.destination_repo.lower() or repo.lower() in sources.repositories():
                return "repository does not match the publication record"
            sha = m.groupdict().get("sha")
            if sha and not pub.published_commit.startswith(sha):
                return "commit does not match the published commit"
            head = parse_qs(parts.query).get("head_sha")
            if pattern is _TARGET_CI_PATHS[0]:
                if not head or head[0] != pub.published_commit:
                    return "workflow run listing must filter on the published commit"
            return None
        return "path not allowed for target CI reads"

    if purpose is EgressPurpose.CORRECTION_PREAUTH_READ:
        if method != "GET" or host != "api.github.com":
            return "pre-authorisation reads are GET only"
        m = _PR_PATH.match(path)
        if not m:
            return "pre-authorisation reads are limited to the pull request"
        if ctx.role is not Role.AUDIT or not ctx.incident_id or ctx.incident_publication is None:
            return "pre-authorisation reads need the audit role and an open incident"
        pub = ctx.incident_publication
        if f"{m.group('owner')}/{m.group('repo')}".lower() != pub.destination_repo.lower() or int(m.group("number")) != pub.pr_number:
            return "pull request does not match the incident's publication"
        return None

    if purpose in (EgressPurpose.CORRECTION_WRITE, EgressPurpose.REMEDIATION_VERIFY_READ):
        if host != "api.github.com":
            return "correction routes are on api.github.com only"
        m = _PR_PATH.match(path)
        if not m:
            return "corrections may only replace an existing pull request description"
        auth = ctx.authorisation
        if auth is None:
            return "no correction authorisation"
        if auth.created_by is not Role.AUDIT:
            return "correction authorisation was not created by the audit role"
        if f"{m.group('owner')}/{m.group('repo')}".lower() != auth.destination_repo.lower() or int(m.group("number")) != auth.pr_number:
            return "pull request does not match the authorisation"
        if purpose is EgressPurpose.REMEDIATION_VERIFY_READ:
            return None if method == "GET" else "verification reads are GET only"
        if method != "PATCH":
            return "corrections may only replace the description"
        if not isinstance(body, Mapping) or set(body) != {"body"} or not isinstance(body["body"], str):
            return "correction body may contain only the description"
        if sha256_text(body["body"]) != auth.replacement_sha256:
            return "description does not match the authorised replacement text"
        return None

    return "purpose not allowed"


def _security_rule(method: str, host: str, path: str, body: Any) -> str | None:
    if host != "api.osv.dev":
        return "host not allowed for the security connector"
    if method == "POST" and path in ("/v1/query", "/v1/querybatch"):
        return check_osv(path, body)
    if method == "GET" and _OSV_VULN.match(path):
        return None
    return "method or path not allowed for the security connector"


# ---------------------------------------------------------------------------
# Gateway
# ---------------------------------------------------------------------------


class CorrectionJournal:
    """Durable intents and leases for correction writes (one in flight per pull request)."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
        self._lock = threading.RLock()
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS intents (
                intent_id TEXT PRIMARY KEY, authorisation_id TEXT NOT NULL, pr TEXT NOT NULL,
                expected_body_sha256 TEXT NOT NULL, replacement_sha256 TEXT NOT NULL,
                state TEXT NOT NULL, reason TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS leases (pr TEXT PRIMARY KEY, holder TEXT NOT NULL, taken_at TEXT NOT NULL);
            """
        )

    def acquire(self, pr: str, holder: str) -> bool:
        with self._lock:
            try:
                self._conn.execute("INSERT INTO leases (pr, holder, taken_at) VALUES (?,?,?)", (pr, holder, _now()))
                return True
            except sqlite3.IntegrityError:
                return False

    def release(self, pr: str, holder: str) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM leases WHERE pr=? AND holder=?", (pr, holder))

    def intent_for(self, authorisation_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT intent_id, state, reason FROM intents WHERE authorisation_id=? ORDER BY created_at DESC LIMIT 1",
                (authorisation_id,),
            ).fetchone()
        return None if row is None else {"intent_id": row[0], "state": row[1], "reason": row[2]}

    def record(self, intent_id: str, auth: CorrectionAuthorisation, pr: str, state: str, reason: str | None = None) -> None:
        with self._lock:
            now = _now()
            self._conn.execute(
                "INSERT INTO intents (intent_id, authorisation_id, pr, expected_body_sha256, replacement_sha256, state, reason, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?) ON CONFLICT(intent_id) DO UPDATE SET state=excluded.state, reason=excluded.reason, updated_at=excluded.updated_at",
                (intent_id, auth.authorisation_id, pr, auth.expected_body_sha256, auth.replacement_sha256, state, reason, now, now),
            )

    def intents(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT intent_id, authorisation_id, pr, state, reason FROM intents ORDER BY created_at"
            ).fetchall()
        return [dict(zip(("intent_id", "authorisation_id", "pr", "state", "reason"), r)) for r in rows]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Gateway:
    """Checks, sends and records every connector request."""

    TRANSIENT = frozenset({408, 429, 500, 502, 503, 504})

    def __init__(
        self,
        transport: Transport,
        *,
        mode: Callable[[], Mode] = lambda: Mode.CONNECTED,
        on_violation: Callable[[str, Mapping[str, Any]], None] | None = None,
        tokens: Mapping[str, Callable[[], str]] | None = None,
        journal: CorrectionJournal | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
        sources: SourceSet = NO_SOURCES,
    ) -> None:
        self._transport = transport
        self.sources = sources
        self._mode = mode
        self._on_violation = on_violation or (lambda kind, detail: None)
        self._tokens = dict(tokens or {})
        self._journal = journal
        self._clock = clock
        self.attempts: list[Attempt] = []
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ bookkeeping

    def _record(self, connector: str, ctx: RequestContext, method: str, url: str, allowed: bool, reason: str) -> None:
        parts = urlsplit(url)
        with self._lock:
            self.attempts.append(
                Attempt(connector, str(ctx.purpose), method.upper(), parts.hostname or "", parts.path, allowed, reason, self._clock().isoformat())
            )

    def attempted_external(self) -> int:
        return len(self.attempts)

    def refused(self) -> list[Attempt]:
        return [a for a in self.attempts if not a.allowed]

    def _headers(self, connector: str, ctx: RequestContext) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": "atlas-backend"}
        if connector in ("issues", "git", "actions"):
            token_name = "github"
        elif connector in ("target_ci", "correction"):
            token_name = "publish"
        else:
            return headers
        supplier = self._tokens.get(token_name)
        token = supplier() if supplier else ""
        if token:
            headers["Authorization"] = f"Bearer {token}"
        return headers

    # ------------------------------------------------------------------ requests

    def request(
        self,
        connector: str,
        method: str,
        url: str,
        ctx: RequestContext,
        body: Any = None,
        *,
        conditional: Mapping[str, str] | None = None,
    ) -> Response:
        """A connector read. Refused requests raise GatewayRefused; transient errors retry once.

        ``conditional`` may carry only ``If-None-Match`` and ``If-Modified-Since`` (GET only); a
        ``304 Not Modified`` reply is returned as is, with no body.
        """
        extra = dict(conditional or {})
        if set(extra) - CONDITIONAL_HEADERS or (extra and method.upper() != "GET"):
            raise GatewayRefused("only If-None-Match and If-Modified-Since are allowed, on GET requests")
        if ctx.purpose is EgressPurpose.CORRECTION_WRITE:
            raise GatewayRefused("correction writes go through write_correction")
        if self._mode() is Mode.OFFLINE:
            self._record(connector, ctx, method, url, False, "offline mode refuses every outbound request")
            self._on_violation("offline_egress_attempt", {"connector": connector, "url": _redact_url(url)})
            raise GatewayRefused("offline mode refuses every outbound request")
        reason = policy_check(connector, method, url, body, ctx, self.sources)
        if reason is not None:
            self._record(connector, ctx, method, url, False, reason)
            self._on_violation("allow_list_violation", {"connector": connector, "reason": reason, "url": _redact_url(url)})
            raise GatewayRefused(reason)
        self._record(connector, ctx, method, url, True, "allowed")
        headers = self._headers(connector, ctx)
        headers.update(extra)
        try:
            response = self._transport(method.upper(), url, headers, body)
        except (ConnectionError, TimeoutError):
            response = None
        if response is None or response.status in self.TRANSIENT:
            self._record(connector, ctx, method, url, True, "retry after transient error")
            try:
                response = self._transport(method.upper(), url, headers, body)
            except (ConnectionError, TimeoutError) as exc:
                raise SourceUnavailable(f"{connector} unavailable after one retry") from exc
            if response.status in self.TRANSIENT:
                raise SourceUnavailable(f"{connector} unavailable after one retry")
        return response

    # ------------------------------------------------------------------ corrections

    def preauthorisation_read(self, ctx: RequestContext) -> tuple[str, str]:
        """Read the incident publication's PR description. Returns (body, sha256)."""
        pub = ctx.incident_publication
        if pub is None:
            raise GatewayRefused("no incident publication")
        url = f"https://api.github.com/repos/{pub.destination_repo}/pulls/{pub.pr_number}"
        resp = self.request("correction", "GET", url, ctx)
        text = (resp.body or {}).get("body") or ""
        return text, sha256_text(text)

    def write_correction(self, ctx: RequestContext) -> str:
        """Replace a PR description with the authorised text.

        Returns ``done`` or ``refused_stale``; raises CorrectionHeld when the outcome is
        uncertain. Never retries the write.
        """
        auth = ctx.authorisation
        if auth is None or ctx.purpose is not EgressPurpose.CORRECTION_WRITE:
            raise GatewayRefused("no correction authorisation")
        if self._journal is None:
            raise GatewayRefused("correction journal unavailable")
        if self._clock() >= auth.expires_at:
            self._record("correction", ctx, "PATCH", "https://api.github.com/", False, "authorisation expired")
            raise GatewayRefused("correction authorisation expired")
        pr = f"{auth.destination_repo}#{auth.pr_number}"
        url = f"https://api.github.com/repos/{auth.destination_repo}/pulls/{auth.pr_number}"
        existing = self._journal.intent_for(auth.authorisation_id)
        if existing and existing["state"] == "done":
            return "done"
        if existing and existing["state"] == "held":
            raise CorrectionHeld("correction is held for a human")
        holder = uuid.uuid4().hex
        if not self._journal.acquire(pr, holder):
            raise CorrectionHeld("another correction for this pull request is in flight")
        try:
            verify_ctx = RequestContext(purpose=EgressPurpose.REMEDIATION_VERIFY_READ, authorisation=auth)
            live = self.request("correction", "GET", url, verify_ctx)
            live_sha = sha256_text((live.body or {}).get("body") or "")
            if live_sha == auth.replacement_sha256:
                self._journal.record(holder, auth, pr, "done", "already in place")
                return "done"
            if live_sha != auth.expected_body_sha256:
                self._journal.record(holder, auth, pr, "refused_stale", "description changed since authorisation")
                return "refused_stale"
            body = {"body": auth.replacement_text}
            reason = policy_check("correction", "PATCH", url, body, ctx, self.sources)
            if reason is not None:
                self._record("correction", ctx, "PATCH", url, False, reason)
                self._on_violation("allow_list_violation", {"connector": "correction", "reason": reason})
                raise GatewayRefused(reason)
            if self._mode() is Mode.OFFLINE:
                self._record("correction", ctx, "PATCH", url, False, "offline mode refuses every outbound request")
                raise GatewayRefused("offline mode refuses every outbound request")
            self._journal.record(holder, auth, pr, "intent")
            self._record("correction", ctx, "PATCH", url, True, "allowed")
            try:
                response = self._transport("PATCH", url, self._headers("correction", ctx), body)
            except Exception:
                response = None
            if response is not None and 200 <= response.status < 300:
                self._journal.record(holder, auth, pr, "done")
                return "done"
            # Uncertain: reconcile by reading, never re-send.
            try:
                check = self.request("correction", "GET", url, verify_ctx)
                if sha256_text((check.body or {}).get("body") or "") == auth.replacement_sha256:
                    self._journal.record(holder, auth, pr, "done", "confirmed by reconciliation read")
                    return "done"
            except Exception:
                pass
            self._journal.record(holder, auth, pr, "held", "outcome uncertain; held for a human")
            raise CorrectionHeld("correction outcome uncertain; held for a human")
        finally:
            self._journal.release(pr, holder)

    def reconcile_correction(self, ctx: RequestContext) -> str:
        """Read-only reconciliation of a held correction. Never writes."""
        auth = ctx.authorisation
        if auth is None or self._journal is None:
            raise GatewayRefused("no correction authorisation")
        url = f"https://api.github.com/repos/{auth.destination_repo}/pulls/{auth.pr_number}"
        verify_ctx = RequestContext(purpose=EgressPurpose.REMEDIATION_VERIFY_READ, authorisation=auth)
        check = self.request("correction", "GET", url, verify_ctx)
        intent = self._journal.intent_for(auth.authorisation_id)
        if sha256_text((check.body or {}).get("body") or "") == auth.replacement_sha256:
            if intent:
                self._journal.record(intent["intent_id"], auth, f"{auth.destination_repo}#{auth.pr_number}", "done", "confirmed by reconciliation read")
            return "done"
        return intent["state"] if intent else "none"


class SourceUnavailable(ConnectionError):
    pass


def _redact_url(url: str) -> str:
    parts = urlsplit(url)
    return f"{parts.scheme}://{parts.hostname}{parts.path}"


def httpx_transport(timeout: float = 30.0) -> Transport:
    """Real transport using httpx (imported lazily so tests never need the network)."""
    import httpx

    client = httpx.Client(timeout=timeout, follow_redirects=False)

    def send(method: str, url: str, headers: Mapping[str, str], body: Any) -> Response:
        try:
            if body is None:
                r = client.request(method, url, headers=dict(headers))
            else:
                r = client.request(method, url, headers=dict(headers), json=body)
        except httpx.TimeoutException as exc:
            raise TimeoutError(str(exc)) from exc
        except httpx.TransportError as exc:
            raise ConnectionError(str(exc)) from exc
        ctype = r.headers.get("content-type", "")
        payload: Any = r.json() if "json" in ctype else r.text
        return Response(r.status_code, payload, dict(r.headers))

    return send
