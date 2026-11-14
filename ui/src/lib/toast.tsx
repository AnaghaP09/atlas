// Short messages that confirm an action. They appear in one corner, read themselves to screen
// readers and leave on their own. Errors stay on the page next to the control; only results are toasts.
import { useSyncExternalStore } from "react";
import { Icon } from "../components/icons";

export type ToastTone = "good" | "warn" | "critical" | "info";

export interface Toast {
  id: number;
  text: string;
  tone: ToastTone;
}

let items: Toast[] = [];
let nextId = 1;
const listeners = new Set<() => void>();

function emit() {
  listeners.forEach((l) => l());
}

export function dismissToast(id: number) {
  items = items.filter((t) => t.id !== id);
  emit();
}

/** Show a toast. It leaves after a few seconds; critical ones stay until closed. */
export function toast(text: string, tone: ToastTone = "good", ms = 4500): number {
  const id = nextId++;
  items = [...items.slice(-3), { id, text, tone }];
  emit();
  if (tone !== "critical" && typeof window !== "undefined") window.setTimeout(() => dismissToast(id), ms);
  return id;
}

export function clearToasts() {
  items = [];
  emit();
}

function subscribe(l: () => void) {
  listeners.add(l);
  return () => listeners.delete(l);
}

export function Toaster() {
  const list = useSyncExternalStore(subscribe, () => items, () => items);
  return (
    <div className="toasts" role="region" aria-label="Messages">
      {list.map((t) => (
        <div key={t.id} className={`toast toast-${t.tone}`} role={t.tone === "critical" ? "alert" : "status"}>
          <Icon name={t.tone === "good" ? "ok" : t.tone === "info" ? "bell" : "alert"} size={18} />
          <span className="toast-text">{t.text}</span>
          <button type="button" className="toast-close" aria-label="Dismiss" onClick={() => dismissToast(t.id)}>
            <Icon name="close" size={14} />
          </button>
        </div>
      ))}
    </div>
  );
}
