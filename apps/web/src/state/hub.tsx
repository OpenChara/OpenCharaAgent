import {
  createContext,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";
import { HubClient } from "../rpc";
import type { SessionSnapshot } from "../lib/status";

/* The board-level connection + roster snapshot, shared by every view.
   The HubClient reconnects forever; we re-fetch hub.state on (re)connect and
   whenever the hub pushes a notification (life.state etc.), so the board stays
   live without polling. Mirrors app.js's `state.hub` + refreshHub(). */

export interface BoardSession extends SessionSnapshot {
  name: string;
  char_name: string;
  mode: string;
  lang: string;
  speaks?: string[];
}

/** hub.state response — only the fields the board reads are typed; the rest is
 *  tolerated (forward-compat with the Python HubDispatcher). */
export interface HubSnapshot {
  sessions: BoardSession[];
  [k: string]: unknown;
}

interface HubContextValue {
  hub: HubClient;
  connected: boolean;
  snapshot: HubSnapshot | null;
  refresh: () => Promise<void>;
}

/** The STABLE half — `hub` (one instance) + `refresh` (a stable callback). Its
 *  value never changes after mount, so subscribing to it costs zero re-renders.
 *  Components that only CALL the hub (the ~14 RPC-only consumers) use this. */
interface HubApi {
  hub: HubClient;
  refresh: () => Promise<void>;
}
/** The CHANGING half — connection + roster snapshot, updated on every hub push. */
interface HubStateValue {
  connected: boolean;
  snapshot: HubSnapshot | null;
}

const HubApiContext = createContext<HubApi | null>(null);
const HubStateContext = createContext<HubStateValue | null>(null);

export function HubProvider({ children }: { children: ReactNode }) {
  const hub = useMemo(() => new HubClient(), []);
  const [connected, setConnected] = useState(false);
  const [snapshot, setSnapshot] = useState<HubSnapshot | null>(null);
  const refreshing = useRef(false);
  const refreshAgain = useRef(false);

  const refresh = useMemo(
    () => {
      // Coalesce: if a push arrives while a refresh is in flight, don't DROP it
      // (that left the roster stale until the next push) — flag a trailing re-run
      // so we always converge on the latest hub.state.
      const run = async (): Promise<void> => {
        if (refreshing.current) {
          refreshAgain.current = true;
          return;
        }
        refreshing.current = true;
        try {
          const snap = await hub.call<HubSnapshot>("hub.state", {}, 20000);
          setSnapshot(snap);
        } catch {
          /* a failed refresh leaves the last good snapshot; reconnect drives the next */
        } finally {
          refreshing.current = false;
        }
        if (refreshAgain.current) {
          refreshAgain.current = false;
          await run();
        }
      };
      return run;
    },
    [hub],
  );

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | null = null;
    const debouncedRefresh = () => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => void refresh(), 150);
    };
    hub.onReady = () => {
      setConnected(true);
      void refresh();
    };
    hub.onDown = () => setConnected(false);
    // Board-relevant pushes (life.state, hello, …) → re-fetch the roster.
    hub.sock.onEvent = () => debouncedRefresh();
    void hub.start();
    return () => {
      if (timer) clearTimeout(timer);
      hub.stop();
    };
  }, [hub, refresh]);

  // Stable: only changes if hub/refresh identity changes (they don't, post-mount).
  const api = useMemo<HubApi>(() => ({ hub, refresh }), [hub, refresh]);
  // Changing: a new object whenever connection or snapshot updates.
  const state = useMemo<HubStateValue>(() => ({ connected, snapshot }), [connected, snapshot]);
  return (
    <HubApiContext.Provider value={api}>
      <HubStateContext.Provider value={state}>{children}</HubStateContext.Provider>
    </HubApiContext.Provider>
  );
}

/** Subscribe to the STABLE hub API ({hub, refresh}) — no re-render on hub pushes.
 *  Use this in components that only call the hub and never read the snapshot. */
export function useHubApi(): HubApi {
  const ctx = useContext(HubApiContext);
  if (!ctx) throw new Error("useHubApi must be used within a HubProvider");
  return ctx;
}

/** Subscribe to the CHANGING hub state ({connected, snapshot}) — re-renders on
 *  every push. Use this when you only need connection/snapshot, not the API. */
export function useHubState(): HubStateValue {
  const ctx = useContext(HubStateContext);
  if (!ctx) throw new Error("useHubState must be used within a HubProvider");
  return ctx;
}

/** The combined view ({hub, connected, snapshot, refresh}) for consumers that
 *  need both the API and the snapshot. Re-renders on push (it reads state). */
export function useHub(): HubContextValue {
  return { ...useHubApi(), ...useHubState() };
}
