import { describe, it, expect, vi } from "vitest";
import { markSuperchatRead, nextReadTs, type ReadCaller } from "./superchat";

const hubOf = (impl: (attempt: number) => Promise<unknown>): ReadCaller & { calls: number } => {
  const hub = {
    calls: 0,
    call<T>(): Promise<T> {
      hub.calls += 1;
      return impl(hub.calls) as Promise<T>;
    },
  };
  return hub;
};

const noSleep = () => Promise.resolve();

describe("nextReadTs — the read watermark only advances", () => {
  it("adopts the server ts when it is ahead", () => {
    expect(nextReadTs(10, 20, 30)).toBe(30);
  });

  it("keeps the previous watermark when the server is behind", () => {
    expect(nextReadTs(50, 20, 30)).toBe(50);
  });

  it("falls back to the attempted ts on a missing/invalid server value", () => {
    expect(nextReadTs(0, 20, undefined)).toBe(20);
    expect(nextReadTs(0, 20, NaN)).toBe(20);
    expect(nextReadTs(0, 20, 0)).toBe(20);
    expect(nextReadTs(0, 20, "junk")).toBe(20);
  });
});

describe("markSuperchatRead — persist with retry, degrade to local-read", () => {
  it("resolves the server read_ts on first success", async () => {
    const hub = hubOf(() => Promise.resolve({ read_ts: 123.5 }));
    await expect(markSuperchatRead(hub, "quinn", 100, { sleep: noSleep })).resolves.toBe(123.5);
    expect(hub.calls).toBe(1);
  });

  it("falls back to the attempted ts when the server omits read_ts", async () => {
    const hub = hubOf(() => Promise.resolve({}));
    await expect(markSuperchatRead(hub, "quinn", 100, { sleep: noSleep })).resolves.toBe(100);
  });

  it("retries a transient failure and then succeeds", async () => {
    const hub = hubOf((n) => (n < 3 ? Promise.reject(new Error("net")) : Promise.resolve({ read_ts: 42 })));
    await expect(markSuperchatRead(hub, "quinn", 40, { retries: 2, sleep: noSleep })).resolves.toBe(42);
    expect(hub.calls).toBe(3);
  });

  it("resolves null (never rejects) when every attempt fails", async () => {
    const hub = hubOf(() => Promise.reject(new Error("down")));
    await expect(markSuperchatRead(hub, "quinn", 40, { retries: 2, sleep: noSleep })).resolves.toBeNull();
    expect(hub.calls).toBe(3);
  });

  it("stops retrying once the signal aborts", async () => {
    const signal = { aborted: false };
    const hub = hubOf(() => {
      signal.aborted = true; // abort after the first (failing) attempt
      return Promise.reject(new Error("down"));
    });
    await expect(markSuperchatRead(hub, "quinn", 40, { retries: 5, signal, sleep: noSleep })).resolves.toBeNull();
    expect(hub.calls).toBe(1);
  });

  it("waits delayMs between attempts", async () => {
    const sleep = vi.fn(() => Promise.resolve());
    const hub = hubOf((n) => (n === 1 ? Promise.reject(new Error("net")) : Promise.resolve({ read_ts: 7 })));
    await markSuperchatRead(hub, "quinn", 5, { retries: 1, delayMs: 1234, sleep });
    expect(sleep).toHaveBeenCalledWith(1234);
  });
});
