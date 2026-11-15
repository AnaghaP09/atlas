// Decides how the UI identifies the user, once, when the page loads.
//
// - The server answers GET /api/session: sign-in mode. The session gives the user, their projects
//   with a role in each, and the CSRF token for writes.
// - The server answers 401: sign-in mode, but there is no session; go to the sign-in page.
// - The server answers that sign-in is off ({ sign_in: false }): the demo role header mode.
//   An older server has no such route (404), which means the same.

import { IdentityStore, ROLES, headerIdentity, sessionIdentity, type Role, type SessionInfo } from "./identity";
import { rememberDemoIdentity, storedDemoProject, storedDemoRole } from "../lib/theme";

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
    return { kind: "ready", identity: headerStore() };
  }
  if (res.status === 401) return { kind: "redirect", to: "/login" };
  if (!res.ok) return { kind: "ready", identity: headerStore() };
  const body = (await res.json()) as SessionInfo | { sign_in: false };
  if ("sign_in" in body && body.sign_in === false) return { kind: "ready", identity: headerStore() };
  const session = body as SessionInfo;
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

/** Header mode: start from the role and project picked last time, if they are still valid. */
function headerStore(): IdentityStore {
  const saved = storedDemoRole();
  const role = (ROLES as readonly string[]).includes(saved ?? "") ? (saved as Role) : "pm";
  const store = new IdentityStore(headerIdentity, { role }, storedDemoProject());
  store.subscribe((snap) => rememberDemoIdentity(snap.principal?.role, snap.projectId));
  return store;
}
