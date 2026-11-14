// Every user action that changes something gets its own Idempotency-Key. If the outcome is
// uncertain (no response), "Check result" retries with the SAME key and payload, so the server
// returns the stored outcome instead of doing the work twice. A deliberate new action gets a new key.

export function newKey(): string {
  return crypto.randomUUID();
}

export interface PendingAction<P> {
  key: string;
  payload: P;
}

export function startAction<P>(payload: P): PendingAction<P> {
  return { key: newKey(), payload };
}

/** A retry keeps the key and the payload exactly. */
export function retryAction<P>(action: PendingAction<P>): PendingAction<P> {
  return { key: action.key, payload: action.payload };
}
