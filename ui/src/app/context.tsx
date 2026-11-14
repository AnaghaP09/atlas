import { QueryClient, QueryClientProvider, useQuery, useQueryClient } from "@tanstack/react-query";
import { createContext, useContext, useEffect, useMemo, useState, useSyncExternalStore, type ReactNode } from "react";
import { ApiError, StaleEpochError, createClient, type ApiClient } from "../api/client";
import { openStream, type LiveEvent } from "../api/stream";
import { IdentityStore, headerIdentity, type IdentitySnapshot, type Principal } from "../identity/identity";
import { startAction, type PendingAction } from "../lib/idempotency";

interface AppContextValue {
  identity: IdentityStore;
  client: ApiClient;
  serverId: string;
  /** The fetch the client uses; live streams use the same one. */
  fetchImpl?: typeof fetch;
}

const AppContext = createContext<AppContextValue | null>(null);

export function createQueryClient(): QueryClient {
  return new QueryClient({
    defaultOptions: {
      queries: {
        retry: (count, err) => !(err instanceof ApiError) && !(err instanceof StaleEpochError) && count < 2,
        refetchOnWindowFocus: false,
        staleTime: 5_000,
      },
      mutations: { retry: false },
    },
  });
}

export function AppProvider({
  children,
  identity: givenIdentity,
  queryClient: givenQueryClient,
  fetchImpl,
}: {
  children: ReactNode;
  identity?: IdentityStore;
  queryClient?: QueryClient;
  fetchImpl?: typeof fetch;
}) {
  const identity = useMemo(() => givenIdentity ?? new IdentityStore(headerIdentity, { role: "pm" }), [givenIdentity]);
  const queryClient = useMemo(() => givenQueryClient ?? createQueryClient(), [givenQueryClient]);
  const client = useMemo(() => createClient(identity, fetchImpl), [identity, fetchImpl]);
  const serverId = typeof window === "undefined" ? "server" : window.location.origin;

  // An identity transition clears everything that belonged to the old identity.
  useEffect(
    () =>
      identity.subscribe(() => {
        void queryClient.cancelQueries();
        queryClient.clear();
      }),
    [identity, queryClient],
  );

  return (
    <QueryClientProvider client={queryClient}>
      <AppContext.Provider value={{ identity, client, serverId, fetchImpl }}>{children}</AppContext.Provider>
    </QueryClientProvider>
  );
}

export function useApp(): AppContextValue {
  const ctx = useContext(AppContext);
  if (!ctx) throw new Error("useApp outside AppProvider");
  return ctx;
}

export function useIdentity(): IdentitySnapshot & { switchRole: (p: Principal) => void; switchProject: (id: string | null) => void } {
  const { identity } = useApp();
  const snap = useSyncExternalStore(
    (cb) => identity.subscribe(cb),
    () => identity.snapshot,
    () => identity.snapshot,
  );
  return {
    ...snap,
    switchRole: (principal) => identity.transition({ principal }),
    switchProject: (projectId) => identity.selectProject(projectId),
  };
}

/** Query keys always start with the server, role and project, so nothing crosses an identity. */
export function useScopedKey(...parts: unknown[]): unknown[] {
  const { serverId } = useApp();
  const { principal, projectId, epoch } = useIdentity();
  return [serverId, principal?.role ?? null, projectId, epoch, ...parts];
}

export function useApi<T>(path: string | null, query?: Record<string, string | number | boolean | null | undefined>) {
  const { client } = useApp();
  const key = useScopedKey(path, query ?? null);
  return useQuery<T, Error>({
    queryKey: key,
    queryFn: () => client.get<T>(path as string, query),
    enabled: path !== null,
  });
}

export type ActionState<R> =
  | { status: "idle" }
  | { status: "running" }
  | { status: "done"; result: R }
  | { status: "failed"; error: ApiError }
  | { status: "uncertain" };

/**
 * One user action with its own Idempotency-Key. "uncertain" means no answer came back; the
 * retry function repeats the request with the same key and payload.
 */
export function useAction<P, R>(path: string) {
  const { client, identity } = useApp();
  const [state, setState] = useState<ActionState<R>>({ status: "idle" });
  const [pending, setPending] = useState<PendingAction<P> | null>(null);

  async function send(action: PendingAction<P>) {
    const epoch = identity.snapshot.epoch;
    setPending(action);
    setState({ status: "running" });
    try {
      const result = await client.post<R>(path, action.payload, action.key);
      if (identity.isCurrent(epoch)) setState({ status: "done", result });
    } catch (err) {
      if (!identity.isCurrent(epoch) || err instanceof StaleEpochError) return;
      if (err instanceof ApiError) setState({ status: "failed", error: err });
      else setState({ status: "uncertain" });
    }
  }

  return {
    state,
    run: (payload: P) => send(startAction(payload)),
    retrySame: () => (pending ? send(pending) : undefined),
  };
}

/** Subscribes to a live stream for as long as the component is mounted and the identity holds. */
export function useLiveStream(path: string | null, onEvent: (e: LiveEvent) => void, onReset: () => void) {
  const { identity, fetchImpl } = useApp();
  const { epoch } = useIdentity();
  const [status, setStatus] = useState<"live" | "reconnecting" | "closed">("closed");
  useEffect(() => {
    if (!path) return;
    return openStream({ identity, path, fetchImpl, handlers: { onEvent, onReset, onStatus: setStatus } });
    // The handlers are read through refs by callers; reopening on every render is not wanted.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [path, identity, epoch, fetchImpl]);
  return status;
}

export function useInvalidateAll() {
  const qc = useQueryClient();
  return () => void qc.invalidateQueries();
}
