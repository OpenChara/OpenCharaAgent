/* JSON-RPC over WebSocket — a faithful TS port of front/web/rpc.js.
 * HubClient (board-level, auto-reconnect) + CharaClient (one living chat).
 *
 * TWO behavioral changes from the JS original: (1) wsUrl derives the scheme from
 * the page protocol (wss on https) so the same build works behind a TLS reverse
 * proxy; (2) the rejoin seq is SESSION-scoped, not persisted — a fresh mount
 * attaches with no replay (attach() restores history; rpc.js's cross-visit
 * localStorage seq made the server replay turns the restore already covered,
 * duplicating them), and `rejoin` replay is reserved for the in-place reconnect
 * of a live session (see CharaClient.reconnect). Everything else — id-matched
 * calls, forever-reconnect backoff, the callback set — is 1:1 with rpc.js. */

import { decodeEvent, type ProtocolEvent } from "./protocol";
import type { LifeSnapshot } from "./lib/status";

/* The CLI prints …/#token=X&ws=Y. Claim it once into sessionStorage and hand
   the hash to the router (#/chara/<name>…) so refresh/back work. */
export interface Boot {
  token: string;
  wsPort: string;
  host: string;
}

export const BOOT: Boot = (() => {
  const params = new URLSearchParams(location.hash.slice(1));
  let token = params.get("token") || "";
  let wsPort = params.get("ws") || "";
  if (token) {
    try {
      sessionStorage.setItem("lm-boot", JSON.stringify({ token, ws: wsPort }));
    } catch {
      /* private mode: keep in memory only */
    }
    history.replaceState(null, "", "#/");
  } else {
    try {
      const saved = JSON.parse(sessionStorage.getItem("lm-boot") || "null");
      if (saved) {
        token = saved.token || "";
        wsPort = saved.ws || "";
      }
    } catch {
      /* corrupt */
    }
  }
  return {
    token,
    wsPort: wsPort || location.port,
    host: location.hostname || "127.0.0.1",
  };
})();

export function wsUrl(path: string): string {
  const proto = location.protocol === "https:" ? "wss:" : "ws:";
  // Local/SSH-tunnel: HTTP + WS are on distinct ports, so BOOT.wsPort is set and
  // we target host:wsPort. Behind a reverse proxy the page is single-origin (the
  // bookmark omits &ws=), so wsPort is empty → target the page origin (no :port,
  // which would otherwise emit a malformed `wss://host:/path`) and let the proxy
  // path-route the upgrade to the backend WS port.
  const hostport = BOOT.wsPort ? `${BOOT.host}:${BOOT.wsPort}` : BOOT.host;
  return `${proto}//${hostport}${path}?token=${encodeURIComponent(BOOT.token)}`;
}

/* The SPA's token rides the URL hash (client-only), so the shell GET sets no auth
   cookie. Call this once at boot: GET /auth?token=… makes the server mint the
   SameSite auth cookie, so subsequent tokenless <img src="/asset?p=…">/attachment
   requests authenticate via the cookie (no token sprayed into every asset URL). */
export async function mintAuthCookie(): Promise<void> {
  if (!BOOT.token) return;
  try {
    await fetch(`/auth?token=${encodeURIComponent(BOOT.token)}`, {
      credentials: "same-origin",
      cache: "no-store",
    });
  } catch {
    /* offline / down — the WS reconnect path will retry the session */
  }
}

/* OPTIONAL password login (plan §4b) — the ALTERNATIVE auth path for a public
   bind reached without a #token= (the proxied bookmark). authInfo() asks the
   server whether login is offered (true only for a public bind with a configured
   password); login() POSTs the password and, on 204, the server has minted the
   SAME SameSite auth cookie the ?token= handshake sets — so the rest of the app
   proceeds unchanged. Both are no-ops for the local/loopback app (BOOT.token is
   present there, and authInfo() returns false). */
export async function authInfo(): Promise<{ login: boolean }> {
  try {
    const r = await fetch("/authinfo", { credentials: "same-origin", cache: "no-store" });
    if (!r.ok) return { login: false };
    const data = (await r.json()) as { login?: boolean };
    return { login: Boolean(data && data.login) };
  } catch {
    return { login: false };
  }
}

export type LoginResult = "ok" | "bad" | "throttled" | "error";

export async function login(password: string): Promise<LoginResult> {
  try {
    const r = await fetch("/login", {
      method: "POST",
      credentials: "same-origin",
      cache: "no-store",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password }),
    });
    if (r.status === 204) return "ok";
    if (r.status === 429) return "throttled";
    if (r.status === 401) return "bad";
    return "error";
  } catch {
    return "error";
  }
}

/** Append the token to a same-origin /asset or /upload URL — a belt-and-suspenders
 *  fallback for asset requests that race the boot cookie mint. */
export function assetUrl(url: string): string {
  if (!url || !BOOT.token) return url;
  if (!url.startsWith("/asset") && !url.startsWith("/upload")) return url;
  return `${url}${url.includes("?") ? "&" : "?"}token=${encodeURIComponent(BOOT.token)}`;
}

/** A token-authed same-origin /asset URL for a file at an absolute sandbox path. */
export function assetUrlForPath(absPath: string): string {
  return assetUrl(`/asset?p=${encodeURIComponent(absPath)}`);
}

/** True only in the Electron desktop shell (its preload exposes window.charaNative).
 *  Desktop-LOCAL actions (open-in-system, reveal-in-Finder) make sense ONLY there —
 *  in a browser they'd run on the SERVER machine, so the webui downloads/previews
 *  instead. The split the user asked for: webui vs. mac/desktop. */
export function isDesktopShell(): boolean {
  return typeof window !== "undefined" && !!(window as { charaNative?: unknown }).charaNative;
}

interface Pending {
  resolve: (v: unknown) => void;
  reject: (e: Error) => void;
}

interface RpcError extends Error {
  code?: number;
  data?: unknown;
}

type EventHandler = (method: string, params: Record<string, unknown>, frame: Record<string, unknown>) => void;

export class RpcSocket {
  path: string;
  ws: WebSocket | null = null;
  nextId = 1;
  pending = new Map<number, Pending>();
  onEvent: EventHandler | null = null;
  /** Fires for EVERY parsed frame (responses included) before dispatch. The chara
   *  transport stamps a monotonic `seq` on all frames it forwards, and the rejoin
   *  baseline must advance on responses too (the attach response is often the first
   *  stamped frame a fresh session sees). Hub frames carry no seq; the hub leaves
   *  this null. */
  onFrame: ((frame: Record<string, unknown>) => void) | null = null;
  onOpen: (() => void) | null = null;
  onClose: ((ev: CloseEvent) => void) | null = null;

  constructor(path: string) {
    this.path = path;
  }

  connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(wsUrl(this.path));
      this.ws = ws;
      let settled = false;
      ws.onopen = () => {
        settled = true;
        if (this.onOpen) this.onOpen();
        resolve();
      };
      ws.onmessage = (ev) => this._onFrame(ev.data);
      ws.onerror = () => {
        if (!settled) {
          settled = true;
          reject(new Error("ws error"));
        }
      };
      ws.onclose = (ev) => {
        for (const p of this.pending.values()) p.reject(new Error("connection closed"));
        this.pending.clear();
        if (!settled) {
          settled = true;
          reject(new Error(ev.reason || "closed"));
        }
        if (this.onClose) this.onClose(ev);
      };
    });
  }

  _onFrame(raw: string): void {
    let frame: Record<string, unknown>;
    try {
      frame = JSON.parse(raw);
    } catch {
      return;
    }
    if (this.onFrame) this.onFrame(frame);
    if (frame.method) {
      // notification (event / hello / permission_ask / life.state)
      if (this.onEvent) this.onEvent(String(frame.method), (frame.params as Record<string, unknown>) || {}, frame);
      return;
    }
    const id = frame.id as number;
    const p = this.pending.get(id);
    if (!p) return;
    this.pending.delete(id);
    if (frame.error) {
      const e = frame.error as { message?: string; code?: number; data?: unknown };
      const err: RpcError = new Error(e.message || "rpc error");
      err.code = e.code;
      err.data = e.data ?? null;
      p.reject(err);
    } else {
      p.resolve(frame.result);
    }
  }

  call<T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<T> {
    return new Promise<T>((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("not connected"));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, { resolve: resolve as (v: unknown) => void, reject });
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", id, method, params: params || {} }));
      if (timeoutMs) {
        setTimeout(() => {
          if (this.pending.has(id)) {
            this.pending.delete(id);
            reject(new Error("timeout"));
          }
        }, timeoutMs);
      }
    });
  }

  notify(method: string, params?: Record<string, unknown>): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", method, params: params || {} }));
    }
  }

  close(): void {
    if (this.ws) {
      try {
        this.ws.close();
      } catch {
        /* gone */
      }
    }
    this.ws = null;
  }

  get open(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }
}

/* Board-level connection; reconnects forever with backoff. */
export class HubClient {
  sock: RpcSocket;
  onReady: (() => void) | null = null;
  onDown: (() => void) | null = null;
  private _backoff = 500;
  private _stopped = false;

  constructor() {
    this.sock = new RpcSocket("/hub");
  }

  async start(): Promise<void> {
    for (;;) {
      if (this._stopped) return;
      try {
        await this.sock.connect();
        this._backoff = 500;
        if (this.onReady) this.onReady();
        await new Promise<void>((res) => {
          this.sock.onClose = () => res();
        });
        if (this.onDown) this.onDown();
      } catch {
        if (this.onDown) this.onDown();
      }
      await new Promise((res) => setTimeout(res, this._backoff));
      this._backoff = Math.min(this._backoff * 2, 8000);
    }
  }

  stop(): void {
    this._stopped = true;
    this.sock.close();
  }

  call<T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<T> {
    return this.sock.call<T>(method, params, timeoutMs);
  }
}

/* The CharaClient notification payloads, decoded ONCE at the wire boundary (the
   same discipline as decodeEvent) so handlers receive typed values instead of
   re-validating a Record<string, unknown> with ad-hoc String()/casts. */
export interface PermissionAsk {
  id: string;
  kind: string;
  reason: string;
}
export interface ClarifyAsk {
  id: string;
  question: string;
  choices: string[];
}
export interface PeerMessage {
  text: string;
  source: string;
}

const str = (v: unknown): string => (v == null ? "" : String(v));
const optNum = (v: unknown): number | undefined => {
  if (v == null) return undefined;
  const n = Number(v);
  return Number.isFinite(n) ? n : undefined;
};

export function decodePermissionAsk(p: Record<string, unknown>): PermissionAsk {
  return { id: str(p.id), kind: str(p.kind), reason: str(p.reason ?? p.detail) };
}
export function decodeClarifyAsk(p: Record<string, unknown>): ClarifyAsk {
  return {
    id: str(p.id),
    question: str(p.question),
    choices: Array.isArray(p.choices) ? p.choices.map(str) : [],
  };
}
export function decodePeerMessage(p: Record<string, unknown>): PeerMessage {
  return { text: str(p.text), source: str(p.source) };
}
export function decodeLifeState(p: Record<string, unknown>): LifeSnapshot {
  // All fields optional; coerce so a malformed wire value can't masquerade as the
  // wrong type (the `p as LifeSnapshot` this replaces did no checking).
  return {
    state: p.state == null ? undefined : str(p.state),
    next_cycle_at: optNum(p.next_cycle_at),
    rest_until: optNum(p.rest_until),
    engaged_until: optNum(p.engaged_until),
    detail: p.detail == null ? undefined : str(p.detail),
  };
}

/* One living chat. attach -> AttachInfo; send streams `event` notifications
   until the turn's response lands; command/snapshot are plain calls. */
export class CharaClient {
  name: string;
  sock: RpcSocket;
  onProtocolEvent: ((ev: ProtocolEvent) => void) | null = null;
  onPermissionAsk: ((p: PermissionAsk) => void) | null = null;
  onClarifyAsk: ((p: ClarifyAsk) => void) | null = null;
  onPeerMessage: ((p: PeerMessage) => void) | null = null;
  onTurnEnd: (() => void) | null = null;
  onLifeState: ((p: LifeSnapshot) => void) | null = null;
  onRejoinGap: (() => void) | null = null;
  onClose: ((ev: CloseEvent) => void) | null = null;
  streaming = false;
  /** Highest server seq seen on this client's frames (responses + notifications).
   *  -1 = no baseline yet. Deliberately NOT persisted: attach() restores the full
   *  transcript tail on a fresh mount, so replaying ring frames from a previous
   *  page visit would render every completed turn twice (the old localStorage
   *  `lm-last-seq` did exactly that). Replay is only for an in-place reconnect of
   *  THIS live session — see reconnect(). */
  lastSeq = -1;
  rejoinGap = false;

  constructor(name: string) {
    this.name = name;
    this.sock = new RpcSocket(`/chara/${encodeURIComponent(name)}`);
    try {
      localStorage.removeItem(`lm-last-seq:${name}`); // retired cross-visit persistence
    } catch {
      /* private */
    }
    this.sock.onFrame = (frame) => {
      const seq = Number(frame.seq);
      if (Number.isFinite(seq) && seq > 0) this.lastSeq = Math.max(this.lastSeq, seq);
    };
    this.sock.onEvent = (method, params) => {
      if (method === "event" && this.onProtocolEvent) {
        const ev = decodeEvent(params);
        if (ev) this.onProtocolEvent(ev);
      } else if (method === "permission_ask" && this.onPermissionAsk) this.onPermissionAsk(decodePermissionAsk(params));
      else if (method === "clarify_ask" && this.onClarifyAsk) this.onClarifyAsk(decodeClarifyAsk(params));
      else if (method === "peer_message" && this.onPeerMessage) this.onPeerMessage(decodePeerMessage(params));
      else if (method === "turn_end" && this.onTurnEnd) this.onTurnEnd();
      else if (method === "life.state" && this.onLifeState) this.onLifeState(decodeLifeState(params));
      else if (method === "rejoin.gap") {
        this.rejoinGap = true;
        if (this.onRejoinGap) this.onRejoinGap();
      }
    };
  }

  /** Open the socket for a FRESH session. No `rejoin` is sent: the first real
   *  frame (attach) joins the driver with NO replay, because attach() itself
   *  restores the transcript tail — a replay on top of that duplicates turns. */
  async connect(): Promise<void> {
    await this.sock.connect();
    this.rejoinGap = false;
    this.sock.onClose = (ev) => {
      if (this.onClose) this.onClose(ev);
    };
  }

  /** True when this client has a replay anchor: a stamped frame was seen on a
   *  previous connection of THIS session, so `rejoin` can resume in place. */
  get hasRejoinAnchor(): boolean {
    return this.lastSeq >= 0;
  }

  /** In-place reconnect of a LIVE session: reopen the socket and ask the server
   *  to replay only the frames missed since lastSeq. Requires hasRejoinAnchor —
   *  without one, rejoin(0) would replay the child's whole ring and duplicate
   *  history; the caller falls back to a full re-attach instead. */
  async reconnect(): Promise<void> {
    if (!this.hasRejoinAnchor) throw new Error("no rejoin anchor");
    await this.connect();
    this.sock.notify("rejoin", { last_seq: this.lastSeq });
  }

  clearRejoin(): void {
    this.lastSeq = -1;
    this.rejoinGap = false;
  }

  attach<T = unknown>(): Promise<T> {
    return this.sock.call<T>("attach", { present: true }, 120000);
  }

  private async _stream<T = unknown>(method: string, params: Record<string, unknown>): Promise<T> {
    this.streaming = true;
    try {
      return await this.sock.call<T>(method, params); // resolves when the turn ends
    } finally {
      this.streaming = false;
    }
  }

  // attachments (optional): [{name, mime, size, data:<base64, no data: prefix>}]
  send<T = unknown>(text: string, attachments?: unknown[]): Promise<T> {
    const params: Record<string, unknown> = { text };
    if (attachments && attachments.length) params.attachments = attachments;
    return this._stream<T>("send", params);
  }
  // No idle() by design: idle driving is SERVER-SIDE only (supervisor.py).
  interrupt<T = unknown>(): Promise<T> {
    return this.sock.call<T>("interrupt", {}, 10000);
  }
  command<T = unknown>(line: string): Promise<T> {
    return this.sock.call<T>("command", { line }, 60000);
  }
  snapshot<T = unknown>(): Promise<T> {
    return this.sock.call<T>("snapshot", {}, 20000);
  }
  permissionReply<T = unknown>(id: string, granted: boolean): Promise<T> {
    return this.sock.call<T>("permission_reply", { id, granted }, 10000);
  }
  clarifyReply<T = unknown>(id: string, answer: string): Promise<T> {
    return this.sock.call<T>("clarify_reply", { id, answer }, 10000);
  }
  detach<T = unknown>(): Promise<T> {
    return this.sock.call<T>("detach", {}, 5000);
  }
  close(): void {
    this.sock.close();
  }
  get open(): boolean {
    return this.sock.open;
  }
}
