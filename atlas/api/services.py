"""Everything the HTTP API talks to, gathered in one container.

``Services`` is built once per server. The data and execution layers enter through the ports in
``atlas.api.ports``; until they are connected the server answers their endpoints with a clear 503.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping

from atlas.api.identity import HeaderIdentity, IdentityResolver
from atlas.projects import ProjectRegistry
from atlas.api.idempotency import IdempotencyStore
from atlas.api.ports import Unwired
from atlas.api.streams import StreamHub
from atlas.contracts import Mode
from atlas.hitl import HumanQueue
from atlas.notify import NotificationService
from atlas.projection import AuthorizationProjection, principal_for
from atlas.reports import EventProducer
from atlas.scheduler import AdmissionStore, Dispatcher, Scheduler
from atlas.services.disposition import FeedbackStore
from eval.store import EvaluationStore

ROOT = Path(__file__).resolve().parents[2]
MANIFEST_PATH = ROOT / "demo" / "manifest.json"


class ModeState:
    """Connected or Offline, changed only through the API by the audit role."""

    def __init__(self, mode: Mode = Mode.CONNECTED) -> None:
        self._mode = mode
        self._lock = threading.Lock()

    def get(self) -> Mode:
        return self._mode

    def set(self, mode: Mode) -> None:
        with self._lock:
            self._mode = mode


class ManualAudit:
    """Append-only record of every manual trigger and control action."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._db = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False)
        self._db.execute("CREATE TABLE IF NOT EXISTS audit (id TEXT PRIMARY KEY, at TEXT, role TEXT, action TEXT, "
                         "subject TEXT, parameters TEXT)")
        self._lock = threading.Lock()

    def record(self, role: str, action: str, subject: str, parameters: Mapping[str, Any]) -> str:
        rid = f"aud-{uuid.uuid4().hex[:10]}"
        with self._lock:
            self._db.execute("INSERT INTO audit VALUES (?,?,?,?,?,?)",
                             (rid, datetime.now(timezone.utc).isoformat(), role, action, subject,
                              json.dumps(dict(parameters), default=str, sort_keys=True)))
        return rid

    def entries(self, subject: str | None = None) -> list[dict[str, Any]]:
        q = "SELECT id, at, role, action, subject, parameters FROM audit" + (" WHERE subject = ?" if subject else "") + " ORDER BY at"
        rows = self._db.execute(q, (subject,) if subject else ()).fetchall()
        return [dict(zip(("id", "at", "role", "action", "subject", "parameters"), r)) | {"parameters": json.loads(r[5])}
                for r in rows]

    def update(self, *_: Any, **__: Any) -> None:
        raise PermissionError("the audit record is append-only")


class NotifyingNotifier:
    """Wraps the notification service so every new notification also reaches the live stream."""

    def __init__(self, inner: NotificationService, hub: StreamHub) -> None:
        self.inner = inner
        self.hub = hub

    def emit(self, event_type: str, subject_id: str, payload_refs: Any, required_scopes: frozenset[str],
             content: Mapping[str, Any] | None = None, project_id: str = "") -> str:
        before = {r["id"] for r in self.inner.records()}
        nid = self.inner.emit(event_type, subject_id, payload_refs, required_scopes, content, project_id=project_id)
        for rec in self.inner.records():
            if rec["id"] not in before:
                self.hub.on_notification(rec)
        return nid

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)


@dataclass
class ScenarioState:
    """Only on the scenario server: fake sources, clock offset, spend and injected faults."""

    events: list[dict[str, Any]] = field(default_factory=list)
    faults: dict[str, dict[str, Any]] = field(default_factory=dict)
    clock_offset_hours: float = 0.0
    spend_usd: float | None = None


@dataclass
class Services:
    environment: str
    host: str
    port: int
    allowed_hosts: frozenset[str]
    allowed_origins: frozenset[str]
    identity: IdentityResolver
    projection: Any
    hub: StreamHub
    events: Any
    notifier: Any
    queue: HumanQueue
    admissions: AdmissionStore
    dispatcher: Dispatcher
    scheduler: Scheduler
    feedback: FeedbackStore
    evaluations: EvaluationStore
    idempotency: IdempotencyStore
    audit: ManualAudit
    mode: ModeState
    orchestrator: Any = None
    snapshots: Any = field(default_factory=lambda: Unwired("the data layer"))
    tasks: Any = field(default_factory=lambda: Unwired("the execution layer"))
    containment: Any = field(default_factory=lambda: Unwired("the containment service"))
    destinations: list[dict[str, Any]] = field(default_factory=list)
    model_configured: bool = False
    spend: Any = None
    scenario: ScenarioState | None = None
    manifest_path: Path = MANIFEST_PATH
    run_in_background: Callable[[Callable[[], None]], None] = lambda fn: fn()
    candidates_dir: Path | None = None
    clock: Callable[[], datetime] = lambda: datetime.now(timezone.utc)
    staged: tuple[str, ...] = ("chat delivery (no workspace in the demo)", "offline self-hosted model (no GPU)",
                               "full upstream CI for the coding task")
    unevaluated: tuple[str, ...] = ()
    run_errors: dict[str, str] = field(default_factory=dict)
    config_presence: dict[str, bool] = field(default_factory=dict)
    ui_origins: frozenset[str] = frozenset()
    default_project_id: str | None = None
    demo_variables: dict[str, str] = field(default_factory=dict)
    projects: ProjectRegistry = field(default_factory=ProjectRegistry)
    feed: Any = None

    @property
    def is_scenario(self) -> bool:
        return self.environment == "scenario"


def build_core(*, environment: str = "normal", host: str = "127.0.0.1", port: int = 8000,
               data_dir: Path | None = None, identity: IdentityResolver | None = None,
               allowed_hosts: frozenset[str] | None = None, allowed_origins: frozenset[str] | None = None,
               ui_origins: frozenset[str] = frozenset(), mode: Mode = Mode.CONNECTED,
               notifier_kwargs: Mapping[str, Any] | None = None) -> Services:
    """Build the parts of the server that exist in this layer. Ports are wired afterwards.

    Host, port and the Host/Origin allow-lists come from configuration (``atlas.config.Settings``);
    the defaults here only mirror the configuration defaults for tests.
    """

    def db(name: str) -> str:
        if data_dir is None:
            return ":memory:"
        data_dir.mkdir(parents=True, exist_ok=True)
        return str(data_dir / f"{environment}-{name}.sqlite")

    projection = AuthorizationProjection()
    hub = StreamHub(projection)
    events = EventProducer(projection)
    events.subscribers.append(hub.on_run_event)
    mode_state = ModeState(mode)
    inner = NotificationService(projection=projection, principal_for=principal_for, mode=mode_state.get,
                                db_path=db("notifications"), **dict(notifier_kwargs or {}))
    notifier = NotifyingNotifier(inner, hub)
    admissions = AdmissionStore(db("admissions"))
    hosts = allowed_hosts or frozenset({f"{host}:{port}", f"localhost:{port}", f"[::1]:{port}"})
    origins = (allowed_origins or frozenset({f"http://{h}" for h in hosts})) | ui_origins
    services = Services(
        environment=environment, host=host, port=port, allowed_hosts=hosts, allowed_origins=origins,
        ui_origins=ui_origins,
        identity=identity or HeaderIdentity(), projection=projection, hub=hub, events=events, notifier=notifier,
        queue=HumanQueue(db("queue")), admissions=admissions,
        dispatcher=Dispatcher(admissions, lambda adm: None),
        scheduler=Scheduler(admissions, [], mode=mode_state.get),
        feedback=FeedbackStore(db("feedback")), evaluations=EvaluationStore(db("evaluations")),
        idempotency=IdempotencyStore(db("idempotency")), audit=ManualAudit(db("audit")), mode=mode_state,
        scenario=ScenarioState() if environment == "scenario" else None,
        candidates_dir=(data_dir / "candidates") if data_dir else None,
    )
    return services
