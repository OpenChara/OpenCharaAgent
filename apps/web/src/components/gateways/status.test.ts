import { describe, it, expect } from "vitest";
import { gwPlatLabel, gwStatusBits } from "./status";
import type { TFn } from "../../i18n";

/* A stub translator: echoes the key (and notes vars) so the tests assert on the
   key choice, not on copy. Mirrors how the lib/* tests stub `t`. */
const t: TFn = (key, vars) => (vars ? `${String(key)}:${JSON.stringify(vars)}` : String(key));

describe("gwPlatLabel", () => {
  it("maps a known platform to its i18n label key", () => {
    expect(gwPlatLabel(t, "weixin")).toBe("gw-weixin-label");
    expect(gwPlatLabel(t, "qq")).toBe("gw-qq-label");
    expect(gwPlatLabel(t, "telegram")).toBe("gw-telegram-label");
    expect(gwPlatLabel(t, "discord")).toBe("gw-discord-label");
    expect(gwPlatLabel(t, "slack")).toBe("gw-slack-label");
  });
  it("falls back to gw-none when no platform", () => {
    expect(gwPlatLabel(t, "")).toBe("gw-none");
    expect(gwPlatLabel(t, null)).toBe("gw-none");
    expect(gwPlatLabel(t, undefined)).toBe("gw-none");
  });
  it("returns the raw id for an unknown platform", () => {
    expect(gwPlatLabel(t, "myspace")).toBe("myspace");
  });
});

describe("gwStatusBits", () => {
  it("running → ok", () => {
    expect(gwStatusBits(t, { state: "running" })).toEqual({ text: "gw-running", cls: "ok" });
  });
  it("needs_login → warn", () => {
    expect(gwStatusBits(t, { state: "needs_login" })).toEqual({ text: "gw-needs-login", cls: "warn" });
  });
  it("stopped / missing → no class", () => {
    expect(gwStatusBits(t, { state: "stopped" })).toEqual({ text: "gw-stopped", cls: "" });
    expect(gwStatusBits(t, null)).toEqual({ text: "gw-stopped", cls: "" });
    expect(gwStatusBits(t, {})).toEqual({ text: "gw-stopped", cls: "" });
  });
});
