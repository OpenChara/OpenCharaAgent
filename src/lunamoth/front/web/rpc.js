/* JSON-RPC over WebSocket: HubClient (board-level, auto-reconnect) and
   CharaClient (one living chat — the existing per-session gateway protocol). */
"use strict";

const BOOT = (() => {
  const params = new URLSearchParams(location.hash.slice(1));
  return {
    token: params.get("token") || "",
    wsPort: params.get("ws") || location.port,
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
    if (frame.method) { // notification (event / hello / permission_ask)
      if (this.onEvent) this.onEvent(frame.method, frame.params || {});
      return;
    }
    const p = this.pending.get(frame.id);
    if (!p) return;
    this.pending.delete(frame.id);
    if (frame.error) {
      const err = new Error(frame.error.message || "rpc error");
      err.code = frame.error.code;
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
    this.onClose = null;
    this.streaming = false;
    this.sock.onEvent = (method, params) => {
      if (method === "event" && this.onProtocolEvent) this.onProtocolEvent(params);
      else if (method === "permission_ask" && this.onPermissionAsk) this.onPermissionAsk(params);
    };
  }

  async connect() {
    await this.sock.connect();
    this.sock.onClose = (ev) => { if (this.onClose) this.onClose(ev); };
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

  send(text) { return this._stream("send", { text }); }
  idle() { return this._stream("idle", {}); }
  interrupt() { return this.sock.call("interrupt", {}, 10000); }
  command(line) { return this.sock.call("command", { line }, 60000); }
  snapshot() { return this.sock.call("snapshot", {}, 20000); }
  permissionReply(id, granted) { return this.sock.call("permission_reply", { id, granted }, 10000); }
  detach() { return this.sock.call("detach", {}, 5000); }
  close() { this.sock.close(); }
  get open() { return this.sock.open; }
}
