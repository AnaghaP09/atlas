// View types for the parts of the API this UI reads. The generated `schema.ts` lists every route;
// most responses are open objects there, so the fields the screens use are named here.

export type DataMode = "REAL" | "SYNTHETIC" | "STAGED" | "UNEVALUATED";

export interface Envelope {
  data_mode?: DataMode;
  environment?: string;
  server_time?: string;
  restricted_items?: number;
  status?: string;
  reason?: string;
}

export interface Health extends Envelope {
  status: string;
  environment: string;
  server: string;
  mode: "connected" | "offline";
  identity: string;
  components: Record<string, string>;
  notifier?: { notifier: string; dead_letters: number; paused: number };
  staged: string[];
  unevaluated: string[];
}

export interface TriggerParameter {
  name: string;
  type: string;
  required: boolean;
}

export interface ManualTrigger {
  trigger_id: string;
  title: string;
  method: string;
  path: string;
  allowed_roles: string[];
  parameters: TriggerParameter[];
  availability: "available" | "disabled" | string;
  reason?: string | null;
}

export interface Catalog extends Envelope {
  manual: ManualTrigger[];
  automatic: Schedule[];
}

export interface Schedule {
  schedule_id: string;
  kind: string;
  state: "enabled" | "paused" | "disabled" | string;
  reason?: string | null;
  last_run?: string | null;
  last_event_id?: string | null;
  next_run?: string | null;
}

export interface StartedBy {
  kind: "schedule" | "watcher" | "person" | string;
  role?: string | null;
  watcher_id?: string | null;
}

export interface ReportLink {
  report_id: string;
  report_type: string;
  report_version: number;
  superseded?: boolean;
}

export interface RunView extends Envelope {
  run_id: string;
  admission_status: string;
  hold_reason?: string | null;
  trigger: Record<string, unknown>;
  started_by: StartedBy;
  snapshot_id: string;
  project_id?: string | null;
  authoritative_generation: number | null;
  superseded_generations: number[];
  run_status: string;
  error?: string | null;
  cost?: unknown;
  reports: ReportLink[];
  /** Plain words for the run, for example "Release close · KubeVirt at release v1.9.0". */
  title?: string;
  kind?: string;
  kind_label?: string;
  subject?: string;
  dataset_title?: string | null;
  started_at?: string | null;
}

export interface Claim {
  id: string;
  text: string;
  kind?: string;
  evidence_ids?: string[];
  verdict?: string;
}

export interface ReportView extends Envelope {
  report_id: string;
  run_id: string;
  report_type: string;
  authoritative_generation: number;
  generation: number;
  superseded_by: number | null;
  report_version: number;
  correction_version: number;
  corrections: { text?: string; correction_id?: string }[];
  markdown: string;
  report: Record<string, unknown> & { claims?: Claim[] };
  withheld: number;
}

export interface EvidenceRecord {
  record_id?: string;
  source?: string;
  source_id?: string;
  revision?: string;
  provenance?: string;
  observed_at?: string;
  valid_from?: string;
  content?: unknown;
  text?: string;
  [key: string]: unknown;
}

export interface QueueEntry {
  entry_id?: string;
  trigger?: string;
  reason?: string;
  run_id?: string | null;
  created_at?: string;
  [key: string]: unknown;
}

export interface Notification {
  notification_id: string;
  event_type: string;
  severity: "info" | "warning" | "high" | "critical" | string;
  kind?: "alert" | "reminder" | "decision" | string;
  message: string;
  run_id?: string | null;
  task_id?: string | null;
  created_at?: string;
  state?: string;
  acknowledged?: boolean;
  resolved?: boolean;
}

export interface Approval {
  approval_id: string;
  task_id: string;
  kind: "A" | "B" | string;
  state: string;
  destination_repository?: string;
  base_ref?: string;
  publication_ref?: string;
  base_commit?: string;
  verification_generation?: number;
  security_decision_id?: string;
  security_valid_until?: string;
  diff_hash?: string;
  artefact_hash?: string;
  diff?: string;
  expires_at?: string;
  [key: string]: unknown;
}

export interface TaskView extends Envelope {
  task_id: string;
  action_id?: string;
  state: string;
  action?: string;
  run_id?: string;
  support_status?: string | null;
  checks?: Record<string, string>;
  security?: { state?: string; valid_until?: string } | null;
  publication?: { pr_url?: string; head_commit?: string } | null;
  ci?: unknown;
  behavioural_check?: unknown;
  adjudication?: unknown;
  [key: string]: unknown;
}

export interface Incident {
  incident_id: string;
  switch_id: string;
  state: string;
  trigger_event?: string;
  tripped_at?: string;
  required_prerequisites?: string[];
  recorded_prerequisites?: { kind: string; reference?: string }[];
  corrections?: Correction[];
}

export interface Correction {
  correction_id: string;
  state: "held" | "completed" | "pending" | string;
  reason?: string | null;
}

export interface ContainmentView extends Envelope {
  switches: { switch_id: string; state: string; title?: string; incident?: Incident | null }[];
}

export interface Gate {
  gate_id: string;
  status: "passed" | "failed" | "unevaluated" | string;
  summary?: string;
  proxy_note?: string;
  pooled?: { recall?: number; precision?: number };
  per_close?: { close: string; recall?: number; precision?: number }[];
}

export interface Evaluation extends Envelope {
  evaluation_id: string;
  completed_at?: string;
  gates: Gate[];
  proxy_limitation?: string;
}

export interface GraphView extends Envelope {
  snapshot_id: string;
  nodes: { id: string; kind: string; label?: string }[];
  edges: { source: string; target: string; kind: string }[];
}

export interface ManifestCall {
  method: string;
  path: string;
  role?: string;
  body?: unknown;
  expect_status?: number[];
}

export interface ManifestRow {
  row_id: string;
  title: string;
  shows: string;
  data_mode: DataMode;
  server: "normal" | "scenario" | string;
  calls: ManifestCall[];
}

export interface DemoManifest extends Envelope {
  manifest: { title: string; description: string; rows: ManifestRow[] };
  variables: Record<string, string>;
  this_server: string;
}

export interface ScorecardRow {
  task_class: string;
  data_mode?: DataMode;
  net_contribution?: number | "unavailable" | null;
  range?: [number, number] | null;
  [key: string]: unknown;
}
