// Decides how the UI identifies the user, once, when the page loads.
//
// - The server answers GET /api/session: sign-in mode. The session gives the user, their projects
//   with a role in each, and the CSRF token for writes.
// - The server answers 401: sign-in mode, but there is no session; go to the sign-in page.
// - The server has no such route (404): the demo role header mode.

import { IdentityStore, headerIdentity, sessionIdentity, type SessionInfo } from "./identity";

export type Bootstrap =
  | { kind: "ready"; identity: IdentityStore }
  | { kind: "redirect"; to: string };

export async function bootstrapIdentity(
  fetchImpl: typeof fetch = (...a) => fetch(...a),
  navigate?: (url: string) => void,
): Promise<Bootstrap> {
  let res: Response;
  try {
    res = await fetchImpl("/api/session", {
      credentials: "same-origin",
      cache: "no-store",
      headers: { Accept: "application/json" },
    });
  } catch {
    return { kind: "ready", identity: new IdentityStore(headerIdentity, { role: "pm" }) };
  }
  if (res.status === 401) return { kind: "redirect", to: "/login" };
  if (!res.ok) return { kind: "ready", identity: new IdentityStore(headerIdentity, { role: "pm" }) };
  const session = (await res.json()) as SessionInfo;
  return { kind: "ready", identity: IdentityStore.fromSession(sessionIdentity(navigate), session) };
}

/** Signs out: the server ends the session and every live stream of it, then the sign-in page shows. */
export async function signOut(
  identity: IdentityStore,
  fetchImpl: typeof fetch = (...a) => fetch(...a),
  navigate: (url: string) => void = (url) => window.location.assign(url),
): Promise<void> {
  try {
    await fetchImpl("/logout", {
      method: "POST",
      headers: identity.headers(true),
      credentials: "same-origin",
      cache: "no-store",
      redirect: "manual",
    });
  } finally {
    navigate("/login");
  }
}
