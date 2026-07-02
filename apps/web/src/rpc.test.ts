import { describe, it, expect, vi } from "vitest";
import { CharaClient, RpcSocket, wsUrl } from "./rpc";

describe("wsUrl", () => {
  it("uses ws:// on a plain-http page (jsdom default origin)", () => {
    expect(wsUrl("/hub").startsWith("ws://")).toBe(true);
  });
  it("passes the path through verbatim (CharaClient pre-encodes it) and appends ?token=", () => {
    // wsUrl encodes the TOKEN, not the path — the caller (CharaClient) already
    // did encodeURIComponent(name) before building the /chara/<name> path.
    const url = wsUrl("/chara/%E5%B0%8FQ");
    expect(url).toContain("/chara/%E5%B0%8FQ?token=");
  });
});

describe("RpcSocket._onFrame routing", () => {
  it("routes a notification (has .method) to onEvent", () => {
    const s = new RpcSocket("/x");
    const seen: Array<[string, Record<string, unknown>]> = [];
    s.onEvent = (m, p) => seen.push([m, p]);
    s._onFrame(JSON.stringify({ jsonrpc: "2.0", method: "event", params: { type: "text" } }));
    expect(seen).toEqual([["event", { type: "text" }]]);
  });

  it("resolves a pending call by id", async () => {
    const s = new RpcSocket("/x");
    const resolve = vi.fn();
    const reject = vi.fn();
    s.pending.set(7, { resolve, reject });
    s._onFrame(JSON.stringify({ jsonrpc: "2.0", id: 7, result: 42 }));
    expect(resolve).toHaveBeenCalledWith(42);
    expect(s.pending.has(7)).toBe(false);
  });

  it("rejects a pending call on an error frame, carrying code", () => {
    const s = new RpcSocket("/x");
    let err: (Error & { code?: number }) | null = null;
    s.pending.set(3, { resolve: () => {}, reject: (e) => (err = e as Error & { code?: number }) });
    s._onFrame(JSON.stringify({ jsonrpc: "2.0", id: 3, error: { code: -32011, message: "busy" } }));
    expect(err).not.toBeNull();
    expect(err!.code).toBe(-32011);
    expect(err!.message).toBe("busy");
  });

  it("drops a response with no matching pending id", () => {
    const s = new RpcSocket("/x");
    expect(() => s._onFrame(JSON.stringify({ id: 999, result: 1 }))).not.toThrow();
  });

  it("ignores malformed JSON frames", () => {
    const s = new RpcSocket("/x");
    expect(() => s._onFrame("{not json")).not.toThrow();
  });
});

describe("RpcSocket.call guards", () => {
  it("rejects when not connected", async () => {
    const s = new RpcSocket("/x");
    await expect(s.call("ping")).rejects.toThrow("not connected");
  });
});

describe("CharaClient seq / rejoin bookkeeping", () => {
  it("starts with no rejoin anchor and ignores stale persisted seqs", () => {
    localStorage.setItem("lm-last-seq:quinn", "123"); // the retired cross-visit key
    const c = new CharaClient("quinn");
    expect(c.lastSeq).toBe(-1);
    expect(c.hasRejoinAnchor).toBe(false);
    expect(localStorage.getItem("lm-last-seq:quinn")).toBeNull(); // cleaned up
  });

  it("advances lastSeq on NOTIFICATION frames", () => {
    const c = new CharaClient("quinn");
    c.sock._onFrame(JSON.stringify({ jsonrpc: "2.0", method: "turn_end", params: {}, seq: 5 }));
    expect(c.lastSeq).toBe(5);
    expect(c.hasRejoinAnchor).toBe(true);
    // never regresses
    c.sock._onFrame(JSON.stringify({ jsonrpc: "2.0", method: "turn_end", params: {}, seq: 3 }));
    expect(c.lastSeq).toBe(5);
  });

  it("advances lastSeq on RESPONSE frames too (the attach response anchors a fresh session)", () => {
    const c = new CharaClient("quinn");
    c.sock._onFrame(JSON.stringify({ jsonrpc: "2.0", id: 1, result: { ok: true }, seq: 42 }));
    expect(c.lastSeq).toBe(42);
    expect(c.hasRejoinAnchor).toBe(true);
  });

  it("connect() sends NO rejoin (fresh attach restores history; replay would duplicate)", async () => {
    const c = new CharaClient("quinn");
    c.sock.connect = vi.fn().mockResolvedValue(undefined);
    c.sock.notify = vi.fn();
    await c.connect();
    expect(c.sock.notify).not.toHaveBeenCalled();
  });

  it("reconnect() replays in place from lastSeq, and refuses without an anchor", async () => {
    const c = new CharaClient("quinn");
    c.sock.connect = vi.fn().mockResolvedValue(undefined);
    c.sock.notify = vi.fn();
    await expect(c.reconnect()).rejects.toThrow("no rejoin anchor");
    c.sock._onFrame(JSON.stringify({ jsonrpc: "2.0", method: "turn_end", params: {}, seq: 7 }));
    await c.reconnect();
    expect(c.sock.notify).toHaveBeenCalledWith("rejoin", { last_seq: 7 });
  });

  it("clearRejoin() drops the anchor (gap → the caller re-attaches from scratch)", () => {
    const c = new CharaClient("quinn");
    c.sock._onFrame(JSON.stringify({ jsonrpc: "2.0", method: "x", params: {}, seq: 9 }));
    c.clearRejoin();
    expect(c.lastSeq).toBe(-1);
    expect(c.hasRejoinAnchor).toBe(false);
  });
});
