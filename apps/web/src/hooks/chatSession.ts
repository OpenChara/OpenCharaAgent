/* ChatSession — the connect/attach/stream lifecycle of one living chat, lifted
 * OUT of the useCharaStream effect so it can be (a) unit-tested with a fake client
 * and (b) own its timers + disposed flag, which closes a teardown race the inline
 * effect had: lifeTimer was created inside onLifeState and renderLifeState never
 * checked disposed, so a life.state push arriving DURING teardown could start an
 * uncleared 1s interval after cleanup already ran. Here the session owns both
 * timers and guards every async hop on `dead`, so dispose() always clears them.
 *
 * Everything that touches React/model is a `deps` bridge — the coupling is real
 * (the lifecycle genuinely drives status, the model, the work slot, snapshots), so
 * it's made EXPLICIT here instead of implicit in a 110-line closure. */

import { CharaClient } from "../rpc";
import type { ProtocolEvent } from "../protocol";
import type { TFn } from "../i18n";
import { errMsg, type LifeSnapshot } from "../lib/status";
import { StreamModel, type RestoredMessage } from "../components/chat/streamModel";

export interface AttachInfo {
  char_name?: string;
  restored?: RestoredMessage[];
  opening?: string;
  opening_text?: string;
}

export interface SessionSnapshot {
  rest_until?: number;
  [k: string]: unknown;
}

/** The bridges the lifecycle needs to talk back to the hook (React state + the
 *  shared StreamModel + the turn-driving callbacks). All are stable across a
 *  session's life. */
export interface ChatSessionDeps {
  t: TFn;
  model: StreamModel;
  bump: () => void;
  /** the hook's shared disposed flag (other hook callbacks read it too). */
  isDisposed: () => boolean;
  /** true while the app is driving a turn it started (vs self-work/gateway). */
  isAppTurn: () => boolean;
  setConnected: (b: boolean) => void;
  setCharName: (s: string) => void;
  setReady: (b: boolean) => void;
  setError: (msg: string) => void;
  setLife: (life: LifeSnapshot) => void;
  onEvent: (ev: ProtocolEvent) => void;
  renderLifeState: () => void;
  finalize: () => void;
  flushQueue: () => void;
  /** restore + flush any queue persisted from a previous visit (after restore renders). */
  restoreQueue: () => void;
  refreshSnapshot: () => Promise<SessionSnapshot | null>;
  runStream: (fn: () => Promise<unknown>) => Promise<void>;
  /** Tear this session down and mount a fresh one (fresh model + attach/restore).
   *  The recovery path when an in-place resume is impossible: no rejoin anchor, or
   *  the server declared a replay gap — a clean re-attach restores full history. */
  requestRestart: () => void;
}

export type ClientFactory = (name: string) => CharaClient;

export const LIFE_TICK_MS = 1000;
export const SNAP_POLL_MS = 6000;
export const RECONNECT_BASE_MS = 1000;
export const RECONNECT_MAX_MS = 8000;

export class ChatSession {
  readonly client: CharaClient;
  private lifeTimer: ReturnType<typeof setInterval> | null = null;
  private snapTimer: ReturnType<typeof setInterval> | null = null;
  private reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  private reconnectBackoff = RECONNECT_BASE_MS;
  private ownDisposed = false;
  /** true once attach() resolved — the precondition for an in-place resume. */
  private attached = false;
  /** live-socket flag, so a FAILED reconnect attempt's close event doesn't append
   *  another "connection lost" line — only a real connected→down transition does. */
  private connectedNow = false;
  // Live frames are buffered until the restored history is rendered, so a turn that
  // was already in flight when we (re)attached can't render ABOVE the history.
  private restored = false;
  private pendingEvents: ProtocolEvent[] = [];

  constructor(
    private readonly name: string,
    makeClient: ClientFactory,
    private readonly deps: ChatSessionDeps,
  ) {
    this.client = makeClient(name);
  }

  /** Dead = this session disposed, or the hook tore down. Every async hop checks it
   *  so a late-resolving connect/attach can't mutate after unmount. */
  private get dead(): boolean {
    return this.ownDisposed || this.deps.isDisposed();
  }

  async start(): Promise<void> {
    const { deps } = this;
    const { model } = deps;
    const client = this.client;
    try {
      await client.connect();
      if (this.dead) return;
      this.connectedNow = true;
      deps.setConnected(true);
      this.wireCallbacks();

      const info = await client.attach<AttachInfo>();
      if (this.dead) return;
      this.attached = true;
      deps.setCharName(info.char_name || this.name);
      model.renderRestored(info.restored || []);
      deps.bump();
      deps.setReady(true);

      // History is down — now flush any live frames buffered during attach, in order,
      // so an in-flight turn's thinking renders at the END (where it belongs) and is
      // visible, instead of stranded above the history. A non-empty buffer also means
      // a turn is in flight (so the restored queue must wait for it, not send now).
      const hadLiveTurn = this.pendingEvents.length > 0;
      this.restored = true;
      for (const ev of this.pendingEvents) deps.onEvent(ev);
      this.pendingEvents = [];

      // A message queued on a previous visit (sent while busy, then the user left):
      // re-draw its bubble after the history, then send it only if the chara is idle —
      // if a turn is in flight, its end (onTurnEnd) flushes the queue.
      deps.restoreQueue();
      if (!hadLiveTurn) deps.flushQueue();

      // attach ≠ wake AND attach injects nothing: presence was removed 2026-06-18
      // (no enter/leave marker, no `user_present` fact), so re-entering a chat draws
      // NO local "arrived" divider either — it is a pure connection event. We still
      // refresh the snapshot (it drives the panel + the resting placeholder).
      await deps.refreshSnapshot();
      if (this.dead) return;
      if (!this.snapTimer) {
        this.snapTimer = setInterval(() => {
          if (!document.hidden) void deps.refreshSnapshot();
        }, SNAP_POLL_MS);
      }
      await handleOpening(client, model, info, deps);
    } catch (e) {
      if (!this.dead) {
        const msg = errMsg(e);
        deps.setError(msg);
        model.systemLine(msg);
        deps.bump();
      }
    }
  }

  private wireCallbacks(): void {
    const { deps } = this;
    const { model } = deps;
    const client = this.client;

    // ALL callbacks guard on `dead`: dispose() closes the socket ASYNCHRONOUSLY
    // (detach first), so a late frame can still arrive on a just-disposed session's
    // socket — e.g. across an epoch restart, where the successor session shares the
    // hook's model refs. Without the guard it would write into the successor's model.
    client.onProtocolEvent = (ev) => {
      if (this.dead) return;
      // Until the restored history is laid down, a live frame (a turn already in
      // flight when we attached) must be HELD — rendering it now would push it above
      // the history. Flushed in order right after renderRestored (see start()).
      if (!this.restored) {
        this.pendingEvents.push(ev);
        return;
      }
      deps.onEvent(ev);
    };
    client.onPermissionAsk = (p) => {
      if (this.dead) return;
      model.pushPermission(p.id, p.kind, p.reason);
      deps.bump();
    };
    client.onClarifyAsk = (p) => {
      if (this.dead) return;
      model.pushClarify(p.id, p.question, p.choices);
      deps.bump();
    };
    client.onPeerMessage = (p) => {
      if (this.dead) return;
      if (!p.text) return;
      model.pushUser(p.text, [], { via: p.source || undefined });
      deps.bump();
    };
    client.onTurnEnd = () => {
      if (this.dead) return;
      // a turn the app didn't drive (self-work / gateway) just ended
      if (!deps.isAppTurn()) deps.finalize();
      deps.flushQueue();
    };
    client.onLifeState = (p) => {
      deps.setLife(p); // already a decoded, coerced LifeSnapshot
      deps.renderLifeState();
      // Timer OWNED by the session + guarded on `dead`: a life.state push during
      // teardown can't leave an interval running past dispose().
      if (!this.dead && !this.lifeTimer) {
        this.lifeTimer = setInterval(() => {
          if (this.dead) return;
          deps.renderLifeState();
        }, LIFE_TICK_MS);
      }
    };
    client.onRejoinGap = () => {
      // The server's replay ring can't bridge the outage (or the child restarted):
      // resuming in place would leave a silent hole in the live transcript. A
      // clean remount re-attaches and restores the FULL history instead, so
      // nothing is missing and nothing renders twice.
      client.clearRejoin();
      if (!this.dead) deps.requestRestart();
    };
    client.onClose = () => {
      if (this.dead) return;
      if (this.connectedNow) {
        this.connectedNow = false;
        deps.setConnected(false);
        model.systemLine(deps.t("conn-lost")); // "…reconnecting…" — the loop below delivers on it
        deps.bump();
      }
      this.scheduleReconnect();
    };
  }

  /** Auto-reconnect with backoff (HubClient's forever-reconnect, scoped to this
   *  session). An attached session with a rejoin anchor resumes IN PLACE: the
   *  server replays only the frames missed while the socket was down, so nothing
   *  renders twice. Without an anchor (attach never completed) a seamless resume
   *  is impossible — remount for a clean attach+restore instead. */
  private scheduleReconnect(): void {
    if (this.dead || this.reconnectTimer) return;
    const delay = this.reconnectBackoff;
    this.reconnectBackoff = Math.min(this.reconnectBackoff * 2, RECONNECT_MAX_MS);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      void this.tryReconnect();
    }, delay);
  }

  private async tryReconnect(): Promise<void> {
    if (this.dead) return;
    if (!this.attached || !this.client.hasRejoinAnchor) {
      this.deps.requestRestart();
      return;
    }
    try {
      await this.client.reconnect();
    } catch {
      if (!this.dead) this.scheduleReconnect();
      return;
    }
    if (this.dead) return;
    this.reconnectBackoff = RECONNECT_BASE_MS;
    this.connectedNow = true;
    this.deps.setConnected(true);
    void this.deps.refreshSnapshot();
  }

  /** Idempotent teardown: clears ALL timers (the session owns them, so this works
   *  regardless of how far start() got), then detaches + closes. Never throws. */
  dispose(): void {
    this.ownDisposed = true;
    if (this.lifeTimer) {
      clearInterval(this.lifeTimer);
      this.lifeTimer = null;
    }
    if (this.snapTimer) {
      clearInterval(this.snapTimer);
      this.snapTimer = null;
    }
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    // Leaving the chat is a pure presence fact — detach, never interrupt.
    void (async () => {
      try {
        await this.client.detach();
      } catch {
        /* gone */
      }
      this.client.close();
    })();
  }
}

/* The opening decision tree (chat.js handleOpening). */
async function handleOpening(
  client: CharaClient,
  model: StreamModel,
  info: AttachInfo,
  deps: ChatSessionDeps,
): Promise<void> {
  const text = info.opening_text || "";
  if (info.opening === "greeting" && text) {
    model.pushText(text, "say");
    model.closeCurrent();
    deps.finalize();
    deps.bump();
    try {
      await client.sock.call("greet", { text }, 10000);
    } catch {
      /* older server */
    }
  } else if (info.opening === "arrival" && text) {
    await deps.runStream(() => client.sock.call("event", { text }));
  } else if (info.opening === "probe" && text) {
    await deps.runStream(() => client.send(text));
  }
}
