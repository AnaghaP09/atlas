// Data sets ("snapshots" inside the API) shown with plain names: what, as of when, how many issues.
import { useApi } from "../app/context";
import type { SnapshotItem } from "../screens/Home";

export function datasetLine(s: SnapshotItem): string {
  const parts = [s.title ?? "Data set"];
  if (s.as_of_label) parts.push(s.as_of_label);
  if (s.display_kind === "release" && typeof s.open_issues === "number") parts.push(`${s.open_issues} open issues`);
  if (s.display_kind === "case" && typeof s.linked_issues === "number") parts.push(`${s.linked_issues} linked issues`);
  return parts.join(" · ");
}

/** Data sets for normal lists and pickers: test copies (names disguised) are left out. */
export function visibleDatasets(list: SnapshotItem[]): SnapshotItem[] {
  return list.filter((s) => !s.is_test_copy && !(s.kind ?? "").endsWith("-aliased"));
}

export const DATASET_GROUPS: { kind: string; title: string }[] = [
  { kind: "release", title: "Releases" },
  { kind: "case", title: "Example cases" },
  { kind: "live", title: "Live (current data)" },
];

export function kindOf(s: SnapshotItem): string {
  if (s.display_kind) return s.display_kind;
  const k = s.kind ?? "";
  return k.startsWith("releases") ? "release" : k.startsWith("cases") ? "case" : "live";
}

/** A data set name looked up by id, for places that only hold the id (runs, reports). */
export function DatasetName({ id }: { id: string | null | undefined }) {
  const snaps = useApi<{ snapshots: SnapshotItem[] }>("/api/snapshots");
  if (!id) return <>—</>;
  const s = snaps.data?.snapshots.find((x) => x.snapshot_id === id);
  return <span title={s ? undefined : "Data set"}>{s ? datasetLine(s) : "Data set"}</span>;
}

/** A grouped picker: "Which point in time?" */
export function DatasetPicker({ value, onChange, list, label = "Which point in time?" }: { value: string; onChange: (v: string) => void; list: SnapshotItem[]; label?: string }) {
  const shown = visibleDatasets(list);
  return (
    <label>
      {label}
      <select value={value} onChange={(e) => onChange(e.target.value)} aria-label={label}>
        <option value="">Choose…</option>
        {DATASET_GROUPS.map((g) => {
          const items = shown.filter((s) => kindOf(s) === g.kind);
          if (!items.length) return null;
          return (
            <optgroup key={g.kind} label={g.title}>
              {items.map((s) => (
                <option key={s.snapshot_id} value={s.snapshot_id}>{datasetLine(s)}</option>
              ))}
            </optgroup>
          );
        })}
      </select>
    </label>
  );
}
