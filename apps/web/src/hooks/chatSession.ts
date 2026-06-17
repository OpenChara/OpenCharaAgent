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
  refreshSnapshot: () => Promise<SessionSnapshot | null>;
  runStream: (fn: () => Promise<unknown>) => Promise<void>;
}

export type ClientFactory = (name: string) => CharaClient;

export const LIFE_TICK_MS = 1000;
export const SNAP_POLL_MS = 6000;

export class ChatSession {
  readonly client: CharaClient;
  private lifeTimer: ReturnType<typeof setInterval> | null = null;
  private snapTimer: ReturnType<typeof setInterval> | null = null;
  private ownDisposed = false;

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
      deps.setConnected(true);
      this.wireCallbacks();

      const info = await client.attach<AttachInfo>();
      if (this.dead) return;
      deps.setCharName(info.char_name || this.name);
      model.renderRestored(info.restored || []);
      deps.bump();
      deps.setReady(true);

      // attach ≠ wake: a resting chara stays asleep; only note arrival otherwise.
      const snap = await deps.refreshSnapshot();
      if (this.dead) return;
      const restingNow = !!(snap && snap.rest_until && snap.rest_until * 1000 > Date.now());
      const hasOpening = info.opening && info.opening !== "none" && !!info.opening_text;
      if (!restingNow && !hasOpening) {
        model.systemLine(deps.t("st-arrived"), "arrived");
        deps.bump();
      }
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

    client.onProtocolEvent = (ev) => deps.onEvent(ev);
    client.onPermissionAsk = (p) => {
      model.pushPermission(p.id, p.kind, p.reason);
      deps.bump();
    };
    client.onClarifyAsk = (p) => {
      model.pushClarify(p.id, p.question, p.choices);
      deps.bump();
    };
    client.onPeerMessage = (p) => {
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
      // We reconnected having missed events while disconnected — the live
      // transcript is now incomplete. Surface that instead of swallowing it (a
      // silent gap is exactly the failure the no-hidden-errors rule guards
      // against); the restored history is intact on re-attach.
      if (!this.dead) {
        model.systemLine(deps.t("rejoin-gap"), "arrived");
        deps.bump();
      }
      client.clearRejoin();
    };
    client.onClose = () => {
      if (!this.dead) {
        deps.setConnected(false);
        model.systemLine(deps.t("conn-lost"));
        deps.bump();
      }
    };
  }

  /** Idempotent teardown: clears BOTH timers (the session owns them, so this works
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
