/* JSON-RPC over WebSocket: HubClient (board-level, auto-reconnect) and
   CharaClient (one living chat — the existing per-session gateway protocol). */
"use strict";

/* The CLI prints …/#token=X&ws=Y. We claim those once into sessionStorage and
   hand the hash over to the router (#/chara/<name>…), so refresh/back work. */
const BOOT = (() => {
  const params = new URLSearchParams(location.hash.slice(1));
  let token = params.get("token") || "";
  let wsPort = params.get("ws") || "";
  if (token) {
    try {
      sessionStorage.setItem("lm-boot", JSON.stringify({ token, ws: wsPort }));
    } catch (e) { /* private mode: keep in memory only */ }
    history.replaceState(null, "", "#/");
  } else {
    try {
      const saved = JSON.parse(sessionStorage.getItem("lm-boot") || "null");
      if (saved) { token = saved.token || ""; wsPort = saved.ws || ""; }
    } catch (e) { /* corrupt */ }
  }
  return {
    token,
    wsPort: wsPort || location.port,
    host: location.hostname || "127.0.0.1",
  };
})();

function wsUrl(path) {
  return `ws://${BOOT.host}:${BOOT.wsPort}${path}?token=${encodeURIComponent(BOOT.token)}`;
}

class RpcSocket {
  constructor(path) {
    this.path = path;
    this.ws = null;
    this.nextId = 1;
    this.pending = new Map(); // id -> {resolve, reject}
    this.onEvent = null;      // (method, params) for notifications
    this.onOpen = null;
    this.onClose = null;
  }

  connect() {
    return new Promise((resolve, reject) => {
      const ws = new WebSocket(wsUrl(this.path));
      this.ws = ws;
      let settled = false;
      ws.onopen = () => { settled = true; if (this.onOpen) this.onOpen(); resolve(); };
      ws.onmessage = (ev) => this._onFrame(ev.data);
      ws.onerror = () => { if (!settled) { settled = true; reject(new Error("ws error")); } };
      ws.onclose = (ev) => {
        for (const p of this.pending.values()) p.reject(new Error("connection closed"));
        this.pending.clear();
        if (!settled) { settled = true; reject(new Error(ev.reason || "closed")); }
        if (this.onClose) this.onClose(ev);
      };
    });
  }

  _onFrame(raw) {
    let frame;
    try { frame = JSON.parse(raw); } catch (e) { return; }
    if (frame.method) { // notification (event / hello / permission_ask / life.state)
      if (this.onEvent) this.onEvent(frame.method, frame.params || {}, frame);
      return;
    }
    const p = this.pending.get(frame.id);
    if (!p) return;
    this.pending.delete(frame.id);
    if (frame.error) {
      const err = new Error(frame.error.message || "rpc error");
      err.code = frame.error.code;
      err.data = frame.error.data || null;
      p.reject(err);
    } else {
      p.resolve(frame.result);
    }
  }

  call(method, params, timeoutMs) {
    return new Promise((resolve, reject) => {
      if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
        reject(new Error("not connected"));
        return;
      }
      const id = this.nextId++;
      this.pending.set(id, { resolve, reject });
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

  notify(method, params) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify({ jsonrpc: "2.0", method, params: params || {} }));
    }
  }

  close() {
    if (this.ws) { try { this.ws.close(); } catch (e) { /* gone */ } }
    this.ws = null;
  }

  get open() { return !!this.ws && this.ws.readyState === WebSocket.OPEN; }
}

/* Board-level connection; reconnects forever with backoff. */
class HubClient {
  constructor() {
    this.sock = new RpcSocket("/hub");
    this.onReady = null;
    this.onDown = null;
    this._backoff = 500;
    this._stopped = false;
  }

  async start() {
    for (;;) {
      if (this._stopped) return;
      try {
        await this.sock.connect();
        this._backoff = 500;
        if (this.onReady) this.onReady();
        await new Promise((res) => { this.sock.onClose = res; });
        if (this.onDown) this.onDown();
      } catch (e) {
        if (this.onDown) this.onDown();
      }
      await new Promise((res) => setTimeout(res, this._backoff));
      this._backoff = Math.min(this._backoff * 2, 8000);
    }
  }

  call(method, params, timeoutMs) { return this.sock.call(method, params, timeoutMs); }
}

/* One living chat. Owns the stream lifecycle:
   attach -> AttachInfo; send/idle stream `event` notifications until their
   response lands; command/snapshot are plain calls. */
class CharaClient {
  constructor(name) {
    this.name = name;
    this.sock = new RpcSocket(`/chara/${encodeURIComponent(name)}`);
    this.onProtocolEvent = null; // (ev) protocol event dicts
    this.onPermissionAsk = null; // ({id, kind, reason, detail, wait_seconds})
    this.onPeerMessage = null;  // ({text, source, sender}) inbound from another channel (WeChat)
    this.onTurnEnd = null;      // ({kind, interrupted}) a turn ended (incl. ones the app didn't drive)
    this.onLifeState = null;    // supervisor life.state
    this.onRejoinGap = null;    // () => fresh attach
    this.onClose = null;
    this.streaming = false;
    this.lastSeq = Number(localStorage.getItem(`lm-last-seq:${name}`) || 0) || 0;
    this.rejoinGap = false;
    this.sock.onEvent = (method, params, frame) => {
      if (frame && Number.isFinite(Number(frame.seq))) {
        this.lastSeq = Math.max(this.lastSeq, Number(frame.seq));
        try { localStorage.setItem(`lm-last-seq:${this.name}`, String(this.lastSeq)); } catch (e) { /* private */ }
      }
      if (method === "event" && this.onProtocolEvent) this.onProtocolEvent(params);
      else if (method === "permission_ask" && this.onPermissionAsk) this.onPermissionAsk(params);
      else if (method === "clarify_ask" && this.onClarifyAsk) this.onClarifyAsk(params);
      else if (method === "peer_message" && this.onPeerMessage) this.onPeerMessage(params);
      else if (method === "turn_end" && this.onTurnEnd) this.onTurnEnd(params);
      else if (method === "life.state" && this.onLifeState) this.onLifeState(params);
      else if (method === "rejoin.gap") {
        this.rejoinGap = true;
        if (this.onRejoinGap) this.onRejoinGap();
      }
    };
  }

  async connect() {
    await this.sock.connect();
    this.rejoinGap = false;
    this.sock.onClose = (ev) => { if (this.onClose) this.onClose(ev); };
    this.sock.notify("rejoin", { last_seq: this.lastSeq });
  }

  clearRejoin() {
    this.lastSeq = 0;
    this.rejoinGap = false;
    try { localStorage.removeItem(`lm-last-seq:${this.name}`); } catch (e) { /* private */ }
  }

  attach() { return this.sock.call("attach", { present: true }, 120000); }

  async _stream(method, params) {
    this.streaming = true;
    try {
      return await this.sock.call(method, params); // resolves when the turn ends
    } finally {
      this.streaming = false;
    }
  }

  // attachments (optional): [{name, mime, size, data:<base64, no data: prefix>}]
  send(text, attachments) {
    const params = { text };
    if (attachments && attachments.length) params.attachments = attachments;
    return this._stream("send", params);
  }
  // No idle() here by design: idle driving is SERVER-SIDE only (supervisor.py).
  // The web renderer renders life.state and must never drive an idle turn.
  interrupt() { return this.sock.call("interrupt", {}, 10000); }
  command(line) { return this.sock.call("command", { line }, 60000); }
  snapshot() { return this.sock.call("snapshot", {}, 20000); }
  permissionReply(id, granted) { return this.sock.call("permission_reply", { id, granted }, 10000); }
  clarifyReply(id, answer) { return this.sock.call("clarify_reply", { id, answer }, 10000); }
  detach() { return this.sock.call("detach", {}, 5000); }
  close() { this.sock.close(); }
  get open() { return this.sock.open; }
}
