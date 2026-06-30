import { describe, it, expect, vi, beforeEach, afterEach } from "vitest";
import { ChatSession, type ChatSessionDeps } from "./chatSession";
import type { CharaClient } from "../rpc";
import type { TFn } from "../i18n";
import { StreamModel } from "../components/chat/streamModel";

/* A minimal fake CharaClient: the methods ChatSession calls + the callback slots
   it assigns. Slots are nullable callables (ChatSession installs the real fns in
   wireCallbacks); cast through unknown — the session only touches this surface. */
type Cb = ((p?: unknown) => void) | null;
interface FakeClient {
  connect: ReturnType<typeof vi.fn>;
  attach: ReturnType<typeof vi.fn>;
  detach: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  clearRejoin: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  sock: { call: ReturnType<typeof vi.fn> };
  onProtocolEvent: Cb;
  onPermissionAsk: Cb;
  onClarifyAsk: Cb;
  onPeerMessage: Cb;
  onTurnEnd: Cb;
  onLifeState: Cb;
  onRejoinGap: Cb;
  onClose: Cb;
}
function makeFakeClient(over: Partial<FakeClient> = {}): FakeClient {
  return {
    connect: vi.fn().mockResolvedValue(undefined),
    attach: vi.fn().mockResolvedValue({ char_name: "Quinn", restored: [], opening: "none" }),
    detach: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    clearRejoin: vi.fn(),
    send: vi.fn().mockResolvedValue(undefined),
    sock: { call: vi.fn().mockResolvedValue(undefined) },
    onProtocolEvent: null,
    onPermissionAsk: null,
    onClarifyAsk: null,
    onPeerMessage: null,
    onTurnEnd: null,
    onLifeState: null,
    onRejoinGap: null,
    onClose: null,
    ...over,
  };
}

function makeDeps(model: StreamModel, over: Partial<ChatSessionDeps> = {}): ChatSessionDeps {
  return {
    t: ((k: string) => k) as unknown as TFn,
    model,
    bump: vi.fn(),
    isDisposed: () => false,
    isAppTurn: () => false,
    setConnected: vi.fn(),
    setCharName: vi.fn(),
    setReady: vi.fn(),
    setError: vi.fn(),
    setLife: vi.fn(),
    onEvent: vi.fn(),
    renderLifeState: vi.fn(),
    finalize: vi.fn(),
    flushQueue: vi.fn(),
    restoreQueue: vi.fn(),
    refreshSnapshot: vi.fn().mockResolvedValue(null),
    runStream: vi.fn().mockResolvedValue(undefined),
    ...over,
  };
}

const session = (name: string, fake: FakeClient, deps: ChatSessionDeps) =>
  new ChatSession(name, () => fake as unknown as CharaClient, deps);

describe("ChatSession", () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => vi.useRealTimers());

  it("connects, wires callbacks, attaches, restores, marks ready", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient();
    await session("quinn", fake, deps).start();

    expect(fake.connect).toHaveBeenCalled();
    expect(deps.setConnected).toHaveBeenCalledWith(true);
    expect(fake.attach).toHaveBeenCalled();
    expect(deps.setCharName).toHaveBeenCalledWith("Quinn");
    expect(deps.setReady).toHaveBeenCalledWith(true);
    expect(typeof fake.onLifeState).toBe("function"); // callbacks got wired
    expect(typeof fake.onClose).toBe("function");
  });

  it("holds live frames that arrive during attach, flushing them AFTER restore", async () => {
    // Regression: a turn already in flight when we (re)enter streams a think frame
    // WHILE attach is awaiting. It must be buffered until the restored history is
    // down, then delivered — otherwise it renders above the history (the bug where
    // in-progress thinking jumped to the top and looked missing).
    const model = new StreamModel();
    const order: string[] = [];
    const deps = makeDeps(model, {
      setReady: vi.fn(() => order.push("ready")),
      onEvent: vi.fn(() => order.push("event")),
    });
    const fake: FakeClient = makeFakeClient({
      attach: vi.fn().mockImplementation(async () => {
        fake.onProtocolEvent?.({ type: "think", text: "…" }); // arrives mid-attach
        return { char_name: "Quinn", restored: [], opening: "none" };
      }),
    });
    await session("quinn", fake, deps).start();

    expect(order).toEqual(["ready", "event"]); // event held until after restore/ready
    expect(deps.onEvent).toHaveBeenCalledWith({ type: "think", text: "…" });
  });

  it("falls back to the requested name when attach omits char_name", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient({ attach: vi.fn().mockResolvedValue({ restored: [], opening: "none" }) });
    await session("quinn", fake, deps).start();
    expect(deps.setCharName).toHaveBeenCalledWith("quinn");
  });

  it("life.state starts a 1s tick, and dispose() clears it (no tick after teardown)", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient();
    const s = session("quinn", fake, deps);
    await s.start();

    // a life.state push opens the recurring tick
    fake.onLifeState!({ state: "live" });
    (deps.renderLifeState as ReturnType<typeof vi.fn>).mockClear();
    vi.advanceTimersByTime(1000);
    expect(deps.renderLifeState).toHaveBeenCalledTimes(1);

    // dispose must stop it — the race this extraction fixes
    s.dispose();
    (deps.renderLifeState as ReturnType<typeof vi.fn>).mockClear();
    vi.advanceTimersByTime(5000);
    expect(deps.renderLifeState).not.toHaveBeenCalled();
  });

  it("a teardown DURING connect aborts start before attach (no leak)", async () => {
    const model = new StreamModel();
    let disposed = false;
    const deps = makeDeps(model, { isDisposed: () => disposed });
    let resolveConnect!: () => void;
    const fake = makeFakeClient({
      connect: vi.fn(() => new Promise<void>((r) => (resolveConnect = r))),
    });
    const p = session("quinn", fake, deps).start();
    disposed = true; // the hook unmounted mid-connect
    resolveConnect();
    await p;

    expect(deps.setConnected).not.toHaveBeenCalled();
    expect(fake.attach).not.toHaveBeenCalled();
  });

  it("a late life.state after dispose never starts a timer", async () => {
    const model = new StreamModel();
    let disposed = false;
    const deps = makeDeps(model, { isDisposed: () => disposed });
    const fake = makeFakeClient();
    const s = session("quinn", fake, deps);
    await s.start();
    s.dispose();
    disposed = true;

    fake.onLifeState!({ state: "live" }); // arrives during socket teardown
    (deps.renderLifeState as ReturnType<typeof vi.fn>).mockClear();
    vi.advanceTimersByTime(5000);
    expect(deps.renderLifeState).not.toHaveBeenCalled();
  });

  it("onRejoinGap surfaces a notice and clears the gap", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient();
    await session("quinn", fake, deps).start();

    fake.onRejoinGap!();
    expect(model.items.some((i) => i.kind === "system")).toBe(true);
    expect(fake.clearRejoin).toHaveBeenCalled();
  });

  it("onClose marks disconnected + drops a system line", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient();
    await session("quinn", fake, deps).start();
    (deps.setConnected as ReturnType<typeof vi.fn>).mockClear();

    fake.onClose!();
    expect(deps.setConnected).toHaveBeenCalledWith(false);
    expect(model.items.some((i) => i.kind === "system")).toBe(true);
  });

  it("dispose() detaches + closes the client", async () => {
    const model = new StreamModel();
    const deps = makeDeps(model);
    const fake = makeFakeClient();
    const s = session("quinn", fake, deps);
    await s.start();
    s.dispose();
    await Promise.resolve(); // let the detach microtask run
    expect(fake.detach).toHaveBeenCalled();
    expect(fake.close).toHaveBeenCalled();
  });
});
