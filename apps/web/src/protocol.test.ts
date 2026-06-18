import { describe, it, expect } from "vitest";
import { decodeEvent, PROTOCOL_VERSION } from "./protocol";

describe("protocol.decodeEvent", () => {
  it("pins PROTOCOL_VERSION to the Python codec (1)", () => {
    expect(PROTOCOL_VERSION).toBe(1);
  });

  it("decodes a say text delta with channel default", () => {
    expect(decodeEvent({ type: "text", text: "hi" })).toEqual({
      type: "text",
      text: "hi",
      channel: "say",
    });
  });

  it("honors the muse channel", () => {
    expect(decodeEvent({ type: "text", text: "x", channel: "muse" })).toEqual({
      type: "text",
      text: "x",
      channel: "muse",
    });
  });

  it("fills dataclass field defaults for a sparse tool_end", () => {
    expect(decodeEvent({ type: "tool_end", name: "speak" })).toEqual({
      type: "tool_end",
      name: "speak",
      ok: true,
      duration: 0,
      summary: "",
      index: 0,
    });
  });

  it("decodes ok:false on tool_end", () => {
    const ev = decodeEvent({ type: "tool_end", name: "patch", ok: false }) as {
      ok: boolean;
    };
    expect(ev.ok).toBe(false);
  });

  it("drops the retired attachment event (files now ride MEDIA: markers in say text)", () => {
    expect(decodeEvent({ type: "attachment", url: "/asset?p=a.png", mime: "image/png" })).toBeNull();
  });

  it("ignores unknown fields", () => {
    const ev = decodeEvent({ type: "notice", kind: "retry", text: "x", extra: 1 });
    expect(ev).toEqual({ type: "notice", kind: "retry", text: "x" });
  });

  it("tolerates an unknown type by returning null (forward-compat)", () => {
    expect(decodeEvent({ type: "future_event", foo: 1 })).toBeNull();
  });
});
