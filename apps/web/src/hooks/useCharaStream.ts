/* useCharaStream — the Chat view's engine. Owns a CharaClient + a StreamModel and
 * ports the chat.js ChatController lifecycle (open/attach/restore, the streaming
 * event dispatch, work-state + life-state, send/interrupt/command/snapshot, the
 * send-anytime queue, super-chat read-flush) into a React hook.
 *
 * The StreamModel mutates its `items` array in place (the load-bearing in-place
 * text accumulation). React can't see those mutations, so every mutating call is
 * followed by a `bump()` that increments a version counter; the hook re-exposes
 * the (same) array reference under a new version so consumers re-render. The view
 * renders `model.items` keyed by item id.
 *
 * Idle is server-side only — this hook never calls idle (rpc.ts has no idle()).
 */

import { useCallback, useEffect, useReducer, useRef, useState } from "react";
import { CharaClient } from "../rpc";
import type { ProtocolEvent } from "../protocol";
import { useT, type TFn } from "../i18n";
import { errMsg, lifeWord, type LifeSnapshot } from "../lib/status";
import { StreamModel, type StreamItem, type UserAttachment } from "../components/chat/streamModel";
import { ChatSession } from "./chatSession";

/** A staged/sent attachment: local preview + the raw-base64 wire payload. */
export interface StagedAttachment extends UserAttachment {
  /** raw base64, no `data:` prefix — the `data` wire field. */
  data: string;
}

/** The transient "working" indicator above the composer (chat.js work-status). */
export type WorkPhase = "idle" | "generate" | "think" | "tool";
export interface WorkState {
  active: boolean;
  phase: WorkPhase;
  thinkTokens: number;
  toolName: string;
}

export interface Snapshot {
  model?: string;
  net_on?: boolean;
  rest_until?: number;
  sandbox_root?: string;
  workspace_root?: string;
  /** Inline avatar data-URI (no fetch needed) — render directly as <img src>. */
  avatar_uri?: string;
  /** `/asset?p=…` URLs — wrap with assetUrl() to append the auth token. */
  sprite_url?: string;
  bg_url?: string;
  keyvisual_url?: string;
  [k: string]: unknown;
}

export interface CharaStream {
  /** the live item list (re-referenced on each bump). */
  items: StreamItem[];
  charName: string;
  connected: boolean;
  streaming: boolean;
  /** true once attach() resolved (history restored). */
  ready: boolean;
  work: WorkState;
  statusWord: string;
  /** composer placeholder (resting vs normal). */
  resting: boolean;
  snapshot: Snapshot | null;
  error: string | null;

  /** send a user turn (or queue it if busy). text and/or attachments. */
  send: (text: string, atts: StagedAttachment[]) => void;
  /** run a slash command; returns its reply text (or null). */
  runCommand: (line: string, quiet?: boolean) => Promise<string | null>;
  interrupt: () => void;
  /** refresh the right-panel snapshot on demand. */
  refreshSnapshot: () => Promise<Snapshot | null>;
  permissionReply: (id: string, granted: boolean) => void;
  clarifyReply: (id: string, answer: string) => void;
  /** surface a transient inline note (errors, hints) as a system line. */
  note: (msg: string) => void;
  /** the raw client (snapshot/command for panel tabs that need bespoke calls). */
  client: CharaClient;
}

export function useCharaStream(name: string): CharaStream {
  const t = useT();
  const tRef = useRef<TFn>(t);
  tRef.current = t;

  // The model + client live for the lifetime of this mounted chat (per name).
  const modelRef = useRef<StreamModel | null>(null);
  if (!modelRef.current) modelRef.current = new StreamModel();
  const clientRef = useRef<CharaClient | null>(null);

  const [, bumpTick] = useReducer((n: number) => n + 1, 0);
  const bump = useCallback(() => bumpTick(), []);

  const [charName, setCharName] = useState(name);
  const [connected, setConnected] = useState(false);
  const [streaming, setStreaming] = useState(false);
  const [ready, setReady] = useState(false);
  const [work, setWork] = useState<WorkState>({ active: false, phase: "idle", thinkTokens: 0, toolName: "" });
  const [statusWord, setStatusWord] = useState("");
  const [resting, setResting] = useState(false);
  const [snapshot, setSnapshot] = useState<Snapshot | null>(null);
  const [error, setError] = useState<string | null>(null);

  // mutable turn bookkeeping (refs so callbacks see live values)
  const lifeRef = useRef<LifeSnapshot | null>(null);
  const lastLifeWordRef = useRef("");
  const appTurnRef = useRef(false);
  const queueRef = useRef<{ text: string; atts: StagedAttachment[]; id: string }[]>([]);
  const disposedRef = useRef(false);

  /* ---- work-state slot (chat.js setWorkState/setStatusWord) ---- */
  const applyStatusWord = useCallback((word: string) => {
    lastLifeWordRef.current = word || "";
    setWork((w) => {
      if (w.active) return w; // an active turn owns the slot
      setStatusWord(word || "");
      return w;
    });
  }, []);

  const setWorkState = useCallback(
    (active: boolean, phase?: WorkPhase, detail?: { thinkTokens?: number; toolName?: string }) => {
      if (!active) {
        setWork({ active: false, phase: "idle", thinkTokens: 0, toolName: "" });
        setStatusWord(lastLifeWordRef.current || "");
        return;
      }
      setWork((prev) => ({
        active: true,
        phase: phase || prev.phase || "generate",
        thinkTokens: detail && "thinkTokens" in detail ? detail.thinkTokens! : prev.thinkTokens,
        toolName: detail && "toolName" in detail ? detail.toolName! : prev.toolName,
      }));
    },
    [],
  );

  /* ---- life-state (chat.js renderLifeState/lifeWord) ---- */
  const renderLifeState = useCallback(() => {
    const life = lifeRef.current;
    if (clientRef.current?.streaming) return;
    if (!life) return;
    applyStatusWord(lifeWord(tRef.current, life));
    setResting(life.state === "resting");
  }, [applyStatusWord]);

  /* ---- finalize a turn (chat.js finalize) ---- */
  const finalize = useCallback(() => {
    modelRef.current!.finalize();
    bump();
    setWorkState(false);
    renderLifeState();
  }, [bump, setWorkState, renderLifeState]);

  /* ---- snapshot → header/panel ---- */
  const refreshSnapshot = useCallback(async (): Promise<Snapshot | null> => {
    const c = clientRef.current;
    if (!c || !c.open || c.streaming) return null;
    let snap: Snapshot;
    try {
      snap = await c.snapshot<Snapshot>();
    } catch {
      return null;
    }
    if (disposedRef.current) return null;
    setSnapshot(snap);
    if (!lifeRef.current && snap.rest_until && snap.rest_until * 1000 > Date.now()) {
      setResting(true);
      applyStatusWord(tRef.current("life-resting-until", { time: fmtClock(snap.rest_until) }));
    }
    return snap;
  }, [applyStatusWord]);

  /* ---- protocol event dispatch (chat.js onEvent) ---- */
  const onEvent = useCallback(
    (ev: ProtocolEvent) => {
      if (disposedRef.current) return;
      const m = modelRef.current!;
      if (ev.type === "text") {
        setWorkState(true, "generate");
        m.pushText(ev.text, ev.channel);
        applyStatusWord(tRef.current("st-creating"));
      } else if (ev.type === "think") {
        m.pushThink(ev.text);
        const last = m.items[m.items.length - 1];
        const tokens = last && last.kind === "think" ? last.tokens : 0;
        setWorkState(true, "think", { thinkTokens: tokens });
      } else if (ev.type === "tool_start") {
        m.pushToolStart(ev.name, ev.preview, ev.index);
        setWorkState(true, "tool", { toolName: ev.name });
        applyStatusWord(tRef.current("st-creating"));
      } else if (ev.type === "tool_end") {
        m.pushToolEnd(ev.name, ev.ok, ev.duration, ev.summary, ev.index);
        // hand the indicator back to whatever is next (chat.js showToolEnd tail)
        setWorkState(true, "generate");
      } else if (ev.type === "notice") {
        m.pushNotice(ev.text || ev.kind);
      }
      // Files are no longer a separate event: the chara writes a MEDIA:<path>
      // marker in its say text, which SayMessage extracts and renders inline.
      bump();
    },
    [applyStatusWord, setWorkState, bump],
  );

  /* ---- driving a turn (chat.js runStream) ---- */
  const runStream = useCallback(
    async (fn: () => Promise<unknown>) => {
      setStreaming(true);
      appTurnRef.current = true;
      setWorkState(true, "generate");
      try {
        await fn();
      } catch (e) {
        if (!disposedRef.current) modelRef.current!.systemLine(errMsg(e));
      } finally {
        if (!disposedRef.current) {
          finalize();
          setStreaming(false);
          void refreshSnapshot();
        }
        appTurnRef.current = false;
      }
      flushQueue();
    },
    // flushQueue is declared below with a ref to avoid a cycle
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [finalize, refreshSnapshot, setWorkState],
  );

  const sendUser = useCallback(
    async (text: string, atts: StagedAttachment[]) => {
      const m = modelRef.current!;
      m.pushUser(text, atts);
      bump();
      const wire = atts.map((a) => ({ name: a.name, mime: a.mime, size: a.size, data: a.data }));
      await runStream(() => clientRef.current!.send(text, wire));
    },
    [bump, runStream],
  );

  const flushQueueRef = useRef<() => void>(() => {});
  const flushQueue = useCallback(() => {
    if (disposedRef.current || !queueRef.current.length) return;
    if (clientRef.current?.streaming || appTurnRef.current) return;
    const item = queueRef.current.shift()!;
    modelRef.current!.removeItem(item.id);
    bump();
    void sendUser(item.text, item.atts);
  }, [bump, sendUser]);
  flushQueueRef.current = flushQueue;

  /* ---- public send: queue if busy (chat.js submit/queueMessage) ---- */
  const send = useCallback(
    (text: string, atts: StagedAttachment[]) => {
      if (!text && !atts.length) return;
      const busy = clientRef.current?.streaming || appTurnRef.current;
      if (busy) {
        const id = modelRef.current!.pushUser(text, atts, { queued: true });
        bump();
        queueRef.current.push({ text, atts, id });
      } else {
        void sendUser(text, atts);
      }
    },
    [bump, sendUser],
  );

  const runCommand = useCallback(async (line: string, quiet?: boolean): Promise<string | null> => {
    const c = clientRef.current;
    if (!c) return null;
    try {
      const reply = await c.command<{ text?: string }>(line);
      const text = reply && reply.text ? reply.text : null;
      if (!quiet && text) {
        modelRef.current!.systemLine(text);
        bump();
      }
      void refreshSnapshot();
      return text;
    } catch (e) {
      if (!quiet) {
        modelRef.current!.systemLine(errMsg(e));
        bump();
      }
      return null;
    }
  }, [bump, refreshSnapshot]);

  const interrupt = useCallback(() => {
    clientRef.current?.interrupt().catch(() => {});
  }, []);

  const note = useCallback(
    (msg: string) => {
      if (!msg) return;
      modelRef.current!.systemLine(msg);
      bump();
    },
    [bump],
  );

  const permissionReply = useCallback(
    (id: string, granted: boolean) => {
      clientRef.current?.permissionReply(id, granted).catch(() => {});
      modelRef.current!.resolveAsk(id);
      bump();
    },
    [bump],
  );
  const clarifyReply = useCallback(
    (id: string, answer: string) => {
      clientRef.current?.clarifyReply(id, answer).catch(() => {});
      modelRef.current!.resolveAsk(id);
      bump();
    },
    [bump],
  );

  /* ---- the connect/attach lifecycle (chat.js open) — one ChatSession per name ----
     The lifecycle (connect → wire → attach → restore → snapshot → opening, plus
     the life/snapshot timers and teardown) lives in ChatSession so it's unit-
     testable and owns its own timers; this effect only resets per-name React
     state and bridges the hook's callbacks/refs in via `deps`. */
  useEffect(() => {
    disposedRef.current = false;
    const model = new StreamModel();
    modelRef.current = model;
    setCharName(name);
    setConnected(false);
    setReady(false);
    setStreaming(false);
    setError(null);
    setSnapshot(null);
    lifeRef.current = null;
    queueRef.current = [];
    applyStatusWord(t("st-connecting"));
    bump();

    const session = new ChatSession(name, (n) => new CharaClient(n), {
      t,
      model,
      bump,
      isDisposed: () => disposedRef.current,
      isAppTurn: () => appTurnRef.current,
      setConnected,
      setCharName,
      setReady,
      setError,
      setLife: (life) => {
        lifeRef.current = life;
      },
      onEvent,
      renderLifeState,
      finalize,
      flushQueue: () => flushQueueRef.current(),
      refreshSnapshot,
      runStream,
    });
    clientRef.current = session.client;
    void session.start();

    return () => {
      disposedRef.current = true;
      session.dispose();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  return {
    items: modelRef.current.items,
    charName,
    connected,
    streaming,
    ready,
    work,
    statusWord,
    resting,
    snapshot,
    error,
    send,
    runCommand,
    interrupt,
    refreshSnapshot,
    permissionReply,
    clarifyReply,
    note,
    client: clientRef.current!,
  };
}

/** Local HH:MM (mirror of lib.format.fmtClock — avoids a cyclic import here). */
function fmtClock(epoch: number): string {
  return new Date(epoch * 1000).toLocaleTimeString([], {
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}
