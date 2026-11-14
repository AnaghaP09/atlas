// One item opened through an access grant: always read-only, no action buttons.
import { ApiError } from "../api/client";
import { useApi, useIdentity } from "../app/context";
import { LockedChip, ReadOnlyBanner, typeLabel } from "../components/access";
import { ErrorView, Loading, Section } from "../components/common";
import { Markdown } from "../lib/markdown";
import type { ReportView } from "../api/types";
import { ReportBody } from "./Reports";
import type { AccessRequest } from "./AccessRequests";

interface ObjectBody {
  label?: string;
  via_grant?: boolean;
  read_only?: boolean;
  grant_expires_at?: string | null;
  markdown?: string;
  data_mode?: string;
  [key: string]: unknown;
}

const META = new Set(["label", "via_grant", "read_only", "grant_expires_at", "markdown", "data_mode", "environment", "server_time", "access", "object_type", "object_id", "restricted_items", "json", "report"]);

function Facts({ value }: { value: Record<string, unknown> }) {
  const rows = Object.entries(value).filter(([k, v]) => !META.has(k) && v !== null && typeof v !== "object");
  if (!rows.length) return null;
  return (
    <dl className="facts">
      {rows.map(([k, v]) => (
        <div key={k} className="fact-row">
          <dt>{k.replace(/_/g, " ").replace(/^./, (c) => c.toUpperCase())}</dt>
          <dd>{String(v)}</dd>
        </div>
      ))}
    </dl>
  );
}

export function ObjectView({ objectType, objectId }: { objectType: string; objectId: string }) {
  const { principal } = useIdentity();
  const data = useApi<ObjectBody>(`/api/objects/${encodeURIComponent(objectType)}/${encodeURIComponent(objectId)}`);
  const mine = useApi<{ requests: AccessRequest[] }>("/api/access-requests", { box: "mine" });
  if (data.isLoading) return <Loading what="the item" />;
  if (data.error instanceof ApiError && data.error.status === 403)
    return (
      <Section title={typeLabel(objectType)} icon="lock">
        <LockedChip objectType={objectType} objectId={objectId} />
      </Section>
    );
  if (data.error) return <ErrorView error={data.error} />;
  const body = data.data ?? {};
  const req = (mine.data?.requests ?? []).find((r) => r.object_type === objectType && r.object_id === objectId && r.status === "granted");
  const owner = (req as (AccessRequest & { owner_role?: string }) | undefined)?.owner_role ?? null;
  const inner = Object.values(body).find((v) => v && typeof v === "object" && !Array.isArray(v)) as Record<string, unknown> | undefined;
  return (
    <Section title={body.label ?? typeLabel(objectType)} icon="lock" mode={body.data_mode}>
      {body.via_grant ? (
        <ReadOnlyBanner
          access={{ mode: "read_only", expires_at: body.grant_expires_at ?? null, automatic: !!req?.grant?.automatic, owning_role: owner }}
          myRole={principal?.role ?? null}
        />
      ) : null}
      {objectType === "report" && body.report && typeof body.report === "object" && typeof body.markdown === "string" ? (
        <ReportBody r={body as unknown as ReportView} />
      ) : typeof body.markdown === "string" ? (
        <div className="report"><Markdown source={body.markdown} /></div>
      ) : (
        <>
          <Facts value={body} />
          {inner ? <Facts value={inner} /> : null}
        </>
      )}
    </Section>
  );
}
