/* useCharaStream — the pre-ready send seam. A message sent BEFORE attach completes
 * must ride the send queue (bubble drawn at once, delivered right after attach),
 * never hit client.send — which would reject with a raw "not connected" error. */
import { describe, it, expect, vi, beforeEach } from "vitest";
import { renderHook, act, waitFor } from "@testing-library/react";
import type { ReactNode } from "react";
import { I18nProvider } from "../i18n";
import { useCharaStream } from "./useCharaStream";

/* The hook constructs its CharaClient via `new CharaClient(name)`; mock the module
 * so every construction hands back the test-controlled fake (a constructor that
 * returns an object yields that object — must be a `function`, not an arrow). */
let currentFake: FakeClient;
vi.mock("../rpc", () => ({
  CharaClient: function CharaClient() {
    return currentFake;
  },
}));

interface FakeClient {
  open: boolean;
  streaming: boolean;
  hasRejoinAnchor: boolean;
  connect: ReturnType<typeof vi.fn>;
  reconnect: ReturnType<typeof vi.fn>;
  attach: ReturnType<typeof vi.fn>;
  detach: ReturnType<typeof vi.fn>;
  close: ReturnType<typeof vi.fn>;
  clearRejoin: ReturnType<typeof vi.fn>;
  send: ReturnType<typeof vi.fn>;
  snapshot: ReturnType<typeof vi.fn>;
  interrupt: ReturnType<typeof vi.fn>;
  sock: { call: ReturnType<typeof vi.fn> };
  /** resolve the (deliberately pending) attach() — the test controls WHEN. */
  finishAttach: (info: unknown) => void;
}

function makeFake(): FakeClient {
  let resolveAttach!: (v: unknown) => void;
  return {
    open: true,
    streaming: false,
    hasRejoinAnchor: true,
    connect: vi.fn().mockResolvedValue(undefined),
    reconnect: vi.fn().mockResolvedValue(undefined),
    attach: vi.fn(
      () =>
        new Promise((r) => {
          resolveAttach = r;
        }),
    ),
    detach: vi.fn().mockResolvedValue(undefined),
    close: vi.fn(),
    clearRejoin: vi.fn(),
    send: vi.fn().mockResolvedValue(undefined),
    snapshot: vi.fn().mockResolvedValue({}),
    interrupt: vi.fn().mockResolvedValue(undefined),
    sock: { call: vi.fn().mockResolvedValue(undefined) },
    finishAttach: (info) => resolveAttach(info),
  };
}

const wrapper = ({ children }: { children: ReactNode }) => (
  <I18nProvider initialLang="en">{children}</I18nProvider>
);

const ATTACHED = { char_name: "Quinn", restored: [], opening: "none" };

describe("useCharaStream pre-ready send", () => {
  beforeEach(() => localStorage.clear());

  it("queues a send made before attach completes, then delivers it once ready", async () => {
    currentFake = makeFake();
    const { result, unmount } = renderHook(() => useCharaStream("quinn"), { wrapper });
    await waitFor(() => expect(currentFake.attach).toHaveBeenCalled());

    // attach still in flight: the send must queue (bubble drawn), NOT hit the wire
    act(() => result.current.send("hello", []));
    expect(currentFake.send).not.toHaveBeenCalled();
    const queued = result.current.items.filter((i) => i.kind === "user");
    expect(queued).toHaveLength(1);
    expect(result.current.error).toBeNull(); // no raw "not connected" surfaced

    await act(async () => currentFake.finishAttach(ATTACHED));
    await waitFor(() => expect(currentFake.send).toHaveBeenCalledWith("hello", []));
    // exactly ONE user bubble survives the queue → flush handoff (no duplicate)
    await waitFor(() =>
      expect(result.current.items.filter((i) => i.kind === "user")).toHaveLength(1),
    );
    unmount();
  });

  it("keeps the ready-path behavior: an idle post-attach send goes straight out", async () => {
    currentFake = makeFake();
    const { result, unmount } = renderHook(() => useCharaStream("quinn"), { wrapper });
    await waitFor(() => expect(currentFake.attach).toHaveBeenCalled());
    await act(async () => currentFake.finishAttach(ATTACHED));
    await waitFor(() => expect(result.current.ready).toBe(true));

    act(() => result.current.send("hi", []));
    await waitFor(() => expect(currentFake.send).toHaveBeenCalledWith("hi", []));
    unmount();
  });

  it("a pre-ready send does not clobber a queue persisted from a previous visit", async () => {
    // A message queued on an earlier visit sits in localStorage; a new pre-ready
    // send must MERGE with it (older first), not overwrite it.
    localStorage.setItem("lm-queue:quinn", JSON.stringify([{ text: "older" }]));
    currentFake = makeFake();
    const { result, unmount } = renderHook(() => useCharaStream("quinn"), { wrapper });
    await waitFor(() => expect(currentFake.attach).toHaveBeenCalled());

    act(() => result.current.send("newer", []));
    await act(async () => currentFake.finishAttach(ATTACHED));

    // both deliver, persisted (older) first
    await waitFor(() => expect(currentFake.send).toHaveBeenCalledTimes(2));
    expect(currentFake.send.mock.calls[0][0]).toBe("older");
    expect(currentFake.send.mock.calls[1][0]).toBe("newer");
    unmount();
  });
});
