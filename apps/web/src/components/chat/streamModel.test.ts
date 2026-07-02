import { describe, it, expect, beforeEach } from "vitest";
import {
  StreamModel,
  summarizeToolTally,
  toolBucket,
  speakTextsFromMessage,
  type StreamItem,
  type TextItem,
  type ThinkItem,
  type ToolGroupItem,
} from "./streamModel";

/* The stream accumulator is the load-bearing behavior ported from chat.js. These
   tests drive the model with decoded-event-equivalent calls and assert the
   resulting item list — the same-type-append / type-change-close contract, the
   tool-group fold + tally, the super-chat flag, and finalize. */

const kinds = (m: StreamModel): string[] => m.items.map((i) => i.kind);
function only<T extends StreamItem>(m: StreamModel, kind: string): T[] {
  return m.items.filter((i) => i.kind === kind) as T[];
}

describe("StreamModel — same-type append vs type-change close", () => {
  let m: StreamModel;
  beforeEach(() => {
    m = new StreamModel();
  });

  it("appends consecutive say deltas into ONE item", () => {
    m.pushText("Hel", "say");
    m.pushText("lo ", "say");
    m.pushText("world", "say");
    const says = only<TextItem>(m, "say");
    expect(says).toHaveLength(1);
    expect(says[0].raw).toBe("Hello world");
  });

  it("renders muse identically to say (the channel is a backend forwarding hint)", () => {
    // The say|muse channel only tells the backend gateway whether to forward a
    // turn externally; on the desktop there's no gateway, so muse must render
    // like say. A muse delta accumulates into a normal `say` item with no
    // channel-based visual or structural difference.
    m.pushText("self-work thought", "muse");
    expect(kinds(m)).toEqual(["say"]);
    expect((m.items[0] as TextItem).raw).toBe("self-work thought");
  });

  it("does NOT structurally break on a channel switch within a turn", () => {
    // The channel is no longer a display distinction, so a say → muse → say
    // switch carries no structural break: like any same-kind run, the deltas
    // accumulate into ONE say item. Separate self-work TURNS stay separate
    // because the turn boundary (finalize / closeCurrent) closes the item — see
    // the next test — NOT because of the channel.
    m.pushText("a", "say");
    m.pushText("b", "muse");
    m.pushText("c", "say");
    expect(kinds(m)).toEqual(["say"]);
    expect((m.items[0] as TextItem).raw).toBe("abc");
  });

  it("keeps successive muse turns as separate messages across a turn boundary", () => {
    m.pushText("turn one", "muse");
    m.finalize();
    m.pushText("turn two", "muse");
    expect(kinds(m)).toEqual(["say", "say"]);
    expect((m.items[0] as TextItem).raw).toBe("turn one");
    expect((m.items[1] as TextItem).raw).toBe("turn two");
  });

  it("appends consecutive muse deltas into ONE say item", () => {
    m.pushText("first ", "muse");
    m.pushText("second", "muse");
    expect(kinds(m)).toEqual(["say"]);
    expect((m.items[0] as TextItem).raw).toBe("first second");
  });

  it("accumulates think deltas into a single streaming think block", () => {
    m.pushThink("rea");
    m.pushThink("son");
    const think = only<ThinkItem>(m, "think");
    expect(think).toHaveLength(1);
    expect(think[0].raw).toBe("reason");
    expect(think[0].streaming).toBe(true);
    expect(think[0].tokens).toBeGreaterThan(0);
  });

  it("reuses the turn's think block across an interleaved tool call", () => {
    m.pushThink("first");
    m.pushToolStart("read_file", "", 0);
    m.pushToolEnd("read_file", true, 0.2, "ok", 0);
    m.pushThink(" more");
    const think = only<ThinkItem>(m, "think");
    expect(think).toHaveLength(1);
    expect(think[0].raw).toBe("first more");
  });
});

describe("StreamModel — tool groups", () => {
  let m: StreamModel;
  beforeEach(() => {
    m = new StreamModel();
  });

  it("folds a run of tool calls into ONE tool-group with a tally", () => {
    m.pushToolStart("read_file", "a.txt", 0);
    m.pushToolEnd("read_file", true, 0.1, "read", 0);
    m.pushToolStart("terminal", "ls", 1);
    m.pushToolEnd("terminal", true, 0.3, "done", 1);
    const groups = only<ToolGroupItem>(m, "tool-group");
    expect(groups).toHaveLength(1);
    expect(groups[0].chips).toHaveLength(2);
    expect(groups[0].tally).toEqual({ read_file: 1, terminal: 1 });
    expect(groups[0].fails).toBe(0);
  });

  it("breaks the group when text interrupts, then starts a new group", () => {
    m.pushToolStart("read_file", "", 0);
    m.pushToolEnd("read_file", true, 0.1, "", 0);
    m.pushText("between", "say");
    m.pushToolStart("terminal", "", 1);
    m.pushToolEnd("terminal", true, 0.1, "", 1);
    expect(kinds(m)).toEqual(["tool-group", "say", "tool-group"]);
  });

  it("counts a failed tool_end in fails", () => {
    m.pushToolStart("patch", "", 0);
    m.pushToolEnd("patch", false, 0.1, "boom", 0);
    const g = only<ToolGroupItem>(m, "tool-group")[0];
    expect(g.fails).toBe(1);
    expect(g.chips[0].ok).toBe(false);
  });

  it("handles an orphan tool_end (no matching start)", () => {
    m.pushToolEnd("web_search", true, 0.5, "found", 7);
    const g = only<ToolGroupItem>(m, "tool-group")[0];
    expect(g.chips).toHaveLength(1);
    expect(g.tally).toEqual({ web_search: 1 });
  });
});

describe("StreamModel — super-chat (speak tool)", () => {
  it("turns the say text after a speak tool_end into a super bubble", () => {
    const m = new StreamModel();
    m.pushToolStart("speak", "", 0);
    m.pushToolEnd("speak", true, 0.1, "spoke", 0);
    m.pushText("hey, look at this", "say");
    const supers = m.items.filter((i) => i.kind === "super");
    expect(supers).toHaveLength(1);
    expect((supers[0] as TextItem).raw).toBe("hey, look at this");
  });

  it("does NOT super-flag when the speak tool failed", () => {
    const m = new StreamModel();
    m.pushToolStart("speak", "", 0);
    m.pushToolEnd("speak", false, 0.1, "", 0);
    m.pushText("plain", "say");
    expect(m.items.some((i) => i.kind === "super")).toBe(false);
    expect(m.items.some((i) => i.kind === "say")).toBe(true);
  });

  it("stamps a super bubble with a ts (the view derives read/unread from it)", () => {
    const m = new StreamModel();
    m.pushToolStart("speak", "", 0);
    m.pushToolEnd("speak", true, 0, "", 0);
    m.pushText("ping", "say");
    const sup = m.items.find((i) => i.kind === "super") as TextItem;
    // read/unread is no longer stored on the item — only the ts the view compares
    // against its own watermark (Chat.tsx superReadTs).
    expect(sup.ts).toBeGreaterThan(0);
    expect("unread" in sup).toBe(false);
  });
});

describe("StreamModel — finalize + system + user", () => {
  it("finalize stops all streaming think blocks", () => {
    const m = new StreamModel();
    m.pushThink("x");
    m.finalize();
    expect((m.items[0] as ThinkItem).streaming).toBe(false);
  });

  it("a notice / system line breaks the current text item", () => {
    const m = new StreamModel();
    m.pushText("a", "say");
    m.pushNotice("retrying");
    m.pushText("b", "say");
    expect(kinds(m)).toEqual(["say", "system", "say"]);
  });

  it("pushUser returns an id and removeItem drops it (queue flush)", () => {
    const m = new StreamModel();
    const id = m.pushUser("queued msg", [], { queued: true });
    expect(m.items.some((i) => i.id === id)).toBe(true);
    m.removeItem(id);
    expect(m.items.some((i) => i.id === id)).toBe(false);
  });

  it("resolveAsk removes the matching permission/clarify box", () => {
    const m = new StreamModel();
    m.pushPermission("p1", "shell", "run ls?");
    m.pushClarify("c1", "which?", ["a", "b"]);
    m.resolveAsk("p1");
    expect(m.items.map((i) => i.kind)).toEqual(["clarify"]);
  });
});

describe("StreamModel — restored history", () => {
  it("renders user/assistant/think/tool from a restored transcript", () => {
    const m = new StreamModel();
    m.renderRestored([
      { role: "user", content: "hi" },
      {
        role: "assistant",
        content: "hello there",
        reasoning_content: "thinking…",
        tool_calls: [{ id: "t1", function: { name: "read_file", arguments: "{}" } }],
      },
      { role: "tool", content: "file body", tool_call_id: "t1" },
      {
        role: "assistant",
        content: "",
        tool_calls: [{ id: "s1", function: { name: "speak", arguments: '{"text":"important!"}' } }],
      },
    ]);
    expect(m.items.some((i) => i.kind === "user")).toBe(true);
    expect(m.items.some((i) => i.kind === "think")).toBe(true);
    expect(m.items.some((i) => i.kind === "say")).toBe(true);
    const g = m.items.find((i) => i.kind === "tool-group") as ToolGroupItem;
    expect(g.chips[0].name).toBe("read_file");
    expect(g.chips[0].summary).toContain("file body");
    const sup = m.items.find((i) => i.kind === "super") as TextItem;
    expect(sup.raw).toBe("important!");
  });

  it("renders a compaction-boundary divider for a summary row, keeping the raw turns", () => {
    const m = new StreamModel();
    m.renderRestored([
      { role: "user", content: "old q" },
      { role: "assistant", content: "old a" },
      { role: "system", content: "…summary text…", kind: "summary" },
      { role: "user", content: "new q" },
    ]);
    const divider = m.items.find((i) => i.kind === "system" && i.cls === "compacted");
    expect(divider).toBeTruthy();
    // the raw turns around the boundary are still shown in full
    expect(m.items.filter((i) => i.kind === "user").length).toBe(2);
    // the summary TEXT itself is never rendered as content
    expect(m.items.some((i) => i.kind === "system" && i.text.includes("summary text"))).toBe(false);
  });

  it("coalesces adjacent compaction boundaries into one divider", () => {
    const m = new StreamModel();
    m.renderRestored([
      { role: "system", content: "s1", kind: "summary" },
      { role: "system", content: "s2", kind: "summary" },
    ]);
    expect(m.items.filter((i) => i.kind === "system" && i.cls === "compacted").length).toBe(1);
  });

  it("does NOT leak pendingSuper past restore — the first live say stays a plain say", () => {
    // Regression: restoring a speak tool_call set the live pendingSuper flag (which
    // nothing in the restore path consumes — the restored super renders via the
    // isSuper arg), so the chara's FIRST live reply after re-entering the chat
    // mis-rendered as a ⚡super-chat.
    const m = new StreamModel();
    m.renderRestored([
      { role: "user", content: "hi" },
      {
        role: "assistant",
        content: "",
        tool_calls: [{ id: "s1", function: { name: "speak", arguments: '{"text":"restored super"}' } }],
      },
    ]);
    expect((m.items.find((i) => i.kind === "super") as TextItem).raw).toBe("restored super");
    m.pushText("a normal live reply", "say");
    expect(m.items[m.items.length - 1].kind).toBe("say");
  });
});

describe("tool tally helpers", () => {
  const t = (key: string, vars?: Record<string, string | number>) =>
    vars ? `${key}(${Object.values(vars).join(",")})` : key;

  it("buckets tool names to verbs", () => {
    expect(toolBucket("read_file")).toBe("tools-read");
    expect(toolBucket("terminal")).toBe("tools-ran");
    expect(toolBucket("browser_click")).toBe("tools-browsed");
    expect(toolBucket("web_search")).toBe("tools-web");
    expect(toolBucket("nonsense")).toBeNull();
  });

  it("summarizes a tally with bucketed + unbucketed names + fails", () => {
    const s = summarizeToolTally(t, { read_file: 1, list_files: 1, custom_tool: 2 }, 1);
    expect(s).toContain("tools-read(2)");
    expect(s).toContain("tools-used(custom_tool,2)");
    expect(s).toContain("tools-failed(1)");
  });

  it("extracts speak text from a message", () => {
    expect(
      speakTextsFromMessage({ tool_calls: [{ function: { name: "speak", arguments: '{"text":"hey"}' } }] }),
    ).toEqual(["hey"]);
    expect(speakTextsFromMessage({ tool_calls: [{ function: { name: "read_file", arguments: "{}" } }] })).toEqual([]);
  });
});
