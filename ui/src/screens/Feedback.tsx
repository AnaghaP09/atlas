import { useState } from "react";
import { useAction } from "../app/context";
import { ActionResult, Section } from "../components/common";

export function Feedback() {
  const [form, setForm] = useState({ report_id: "", item_id: "", kind: "override", rationale: "" });
  const send = useAction<typeof form, unknown>("/api/feedback");
  return (
    <Section title="Record a decision">
      <p>
        Record when you overrule a report item, or accept a piece of technical debt on purpose. Disagreements become new test
        cases for ATLAS.
      </p>
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void send.run(form);
        }}
      >
        <label>Report <input value={form.report_id} onChange={(e) => setForm({ ...form, report_id: e.target.value })} /></label>
        <label>Item <input value={form.item_id} onChange={(e) => setForm({ ...form, item_id: e.target.value })} /></label>
        <label>
          Kind
          <select value={form.kind} onChange={(e) => setForm({ ...form, kind: e.target.value })}>
            <option value="override">I overrule this item</option>
            <option value="debt_acceptance">We accept this debt</option>
          </select>
        </label>
        <label>Why <textarea value={form.rationale} onChange={(e) => setForm({ ...form, rationale: e.target.value })} /></label>
        <button type="submit" disabled={!form.report_id || !form.rationale}>Record</button>
      </form>
      <ActionResult state={send.state} retry={() => void send.retrySame()} />
    </Section>
  );
}
