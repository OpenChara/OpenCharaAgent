import { describe, it, expect } from "vitest";
import {
  buildSaveConfig,
  requiredFilled,
  parseAllowed,
  allowedToString,
  platformEnabled,
  anyEnabled,
  togglePlatform,
  type MessagingConfig,
} from "./gatewayModel";

describe("parseAllowed", () => {
  it("splits on ASCII and CJK commas, trims, drops blanks", () => {
    expect(parseAllowed("a, b，c ,  ,d")).toEqual(["a", "b", "c", "d"]);
  });
  it("empty string → empty list", () => {
    expect(parseAllowed("")).toEqual([]);
    expect(parseAllowed("  ,  ，")).toEqual([]);
  });
});

describe("allowedToString", () => {
  it("joins an array; non-array → empty", () => {
    expect(allowedToString({ allowed_senders: ["x", "y"] })).toBe("x, y");
    expect(allowedToString({ allowed_senders: "nope" } as MessagingConfig)).toBe("");
    expect(allowedToString({})).toBe("");
  });
});

describe("requiredFilled", () => {
  it("weixin always reads configured (its login lives in weixin_state.json)", () => {
    // chat.js: required.length===0 → Object.keys(a).length>0 || plat==="weixin".
    expect(requiredFilled({}, "weixin")).toBe(true);
    expect(requiredFilled({ adapters: { weixin: {} } }, "weixin")).toBe(true);
    expect(requiredFilled({ adapters: { weixin: { base_url: "x" } } }, "weixin")).toBe(true);
  });
  it("qq reads configured once its required url is filled", () => {
    expect(requiredFilled({ adapters: { qq: { url: "ws://x" } } }, "qq")).toBe(true);
    expect(requiredFilled({ adapters: { qq: {} } }, "qq")).toBe(false);
  });
  it("telegram reads configured once its required bot_token is filled", () => {
    expect(requiredFilled({ adapters: { telegram: { bot_token: "t" } } }, "telegram")).toBe(true);
    expect(requiredFilled({ adapters: { telegram: {} } }, "telegram")).toBe(false);
  });
  it("unknown platform → false", () => {
    expect(requiredFilled({ adapters: { slack: { url: "x" } } }, "slack")).toBe(false);
  });
});

describe("platformEnabled (per-platform on-state, with legacy inherit)", () => {
  it("reads the platform's own enabled flag when present", () => {
    expect(platformEnabled({ enabled: false, adapters: { qq: { enabled: true } } }, "qq")).toBe(true);
    expect(platformEnabled({ enabled: true, adapters: { qq: { enabled: false } } }, "qq")).toBe(false);
  });
  it("inherits the top-level enabled when the platform flag is ABSENT (legacy)", () => {
    expect(platformEnabled({ enabled: true, adapters: { qq: {} } }, "qq")).toBe(true);
    expect(platformEnabled({ enabled: false, adapters: { qq: {} } }, "qq")).toBe(false);
    // missing adapter block also inherits the top-level
    expect(platformEnabled({ enabled: true, adapters: {} }, "qq")).toBe(true);
    expect(platformEnabled({ enabled: true }, "qq")).toBe(true);
  });
  it("an explicit null flag inherits like absent", () => {
    expect(platformEnabled({ enabled: true, adapters: { qq: { enabled: null } } } as MessagingConfig, "qq")).toBe(true);
  });
});

describe("anyEnabled (top-level = any platform effectively on)", () => {
  it("true when any configured platform is effectively on", () => {
    expect(anyEnabled({ enabled: false, adapters: { qq: { enabled: false }, telegram: { enabled: true } } })).toBe(true);
  });
  it("false when every configured platform is off", () => {
    expect(anyEnabled({ enabled: false, adapters: { qq: { enabled: false }, telegram: { enabled: false } } })).toBe(false);
  });
  it("honours the legacy inherit for flags that are absent", () => {
    expect(anyEnabled({ enabled: true, adapters: { qq: {}, telegram: {} } })).toBe(true);
    expect(anyEnabled({ enabled: false, adapters: { qq: {}, telegram: {} } })).toBe(false);
  });
  it("no configured platforms → false", () => {
    expect(anyEnabled({ enabled: true, adapters: {} })).toBe(false);
    expect(anyEnabled({ enabled: true })).toBe(false);
  });
});

describe("buildSaveConfig (field merge + per-platform enabled + derived top-level)", () => {
  const base = {
    plat: "weixin",
    enabled: true,
    allowedText: "alice, bob",
  };

  it("folds the platform's enabled into its adapter block; omits unchanged fields", () => {
    const cfg = buildSaveConfig({
      ...base,
      current: { base_url: "https://x", bot_type: "3" },
      initial: { base_url: "https://x", bot_type: "3" },
    });
    expect(cfg.adapters).toEqual({ weixin: { enabled: true } });
    expect(cfg.allowed_senders).toEqual(["alice", "bob"]);
  });

  it("sends only the changed field plus the platform enabled", () => {
    const cfg = buildSaveConfig({
      ...base,
      current: { base_url: "https://new", bot_type: "3" },
      initial: { base_url: "https://old", bot_type: "3" },
    });
    expect(cfg.adapters).toEqual({ weixin: { base_url: "https://new", enabled: true } });
  });

  it("a cleared field becomes an explicit null (delete)", () => {
    const cfg = buildSaveConfig({
      ...base,
      current: { base_url: "" },
      initial: { base_url: "https://old" },
    });
    expect(cfg.adapters!.weixin).toEqual({ base_url: null, enabled: true });
  });

  it("trims field values before comparing/sending", () => {
    const cfg = buildSaveConfig({
      ...base,
      current: { bot_type: "  4  " },
      initial: { bot_type: "3" },
    });
    expect(cfg.adapters!.weixin).toEqual({ bot_type: "4", enabled: true });
  });

  it("ignores fields not present in current (not rendered)", () => {
    const cfg = buildSaveConfig({
      ...base,
      current: {},
      initial: { base_url: "https://old" },
    });
    expect(cfg.adapters).toEqual({ weixin: { enabled: true } });
  });

  it("carries a known platform's changed field (qq url)", () => {
    const cfg = buildSaveConfig({
      plat: "qq",
      enabled: true,
      allowedText: "",
      current: { url: "ws://x" },
      initial: {},
    });
    expect(cfg.adapters).toEqual({ qq: { url: "ws://x", enabled: true } });
  });

  it("unknown platform → just the enabled flag in its block", () => {
    const cfg = buildSaveConfig({
      plat: "slack",
      enabled: false,
      allowedText: "",
      current: { url: "ws://x" },
      initial: {},
    });
    expect(cfg.adapters).toEqual({ slack: { enabled: false } });
  });

  it("derives top-level enabled = anyEnabled AFTER applying this platform's new value", () => {
    // qq currently off, telegram off → turning qq ON makes the top-level ON.
    const onCfg = buildSaveConfig({
      plat: "qq",
      enabled: true,
      allowedText: "",
      current: {},
      initial: {},
      cfg: { enabled: false, adapters: { qq: { enabled: false }, telegram: { enabled: false } } },
    });
    expect(onCfg.enabled).toBe(true);

    // qq currently on, telegram on → turning qq OFF leaves telegram on → top-level stays ON.
    const stillOn = buildSaveConfig({
      plat: "qq",
      enabled: false,
      allowedText: "",
      current: {},
      initial: {},
      cfg: { enabled: true, adapters: { qq: { enabled: true }, telegram: { enabled: true } } },
    });
    expect(stillOn.enabled).toBe(true);

    // last platform off → top-level OFF.
    const allOff = buildSaveConfig({
      plat: "qq",
      enabled: false,
      allowedText: "",
      current: {},
      initial: {},
      cfg: { enabled: true, adapters: { qq: { enabled: true }, telegram: { enabled: false } } },
    });
    expect(allOff.enabled).toBe(false);
  });

  it("without cfg, top-level reflects just this platform's new value", () => {
    expect(buildSaveConfig({ ...base, enabled: true, current: {}, initial: {} }).enabled).toBe(true);
    expect(buildSaveConfig({ ...base, enabled: false, current: {}, initial: {} }).enabled).toBe(false);
  });
});

describe("togglePlatform reconcile", () => {
  // The backend re-derives the authoritative top-level `enabled`; the start/stop
  // reconcile must follow THAT, not the frontend's own recompute, so they can't drift.
  function fakeHub(savedEnabled: boolean) {
    const calls: string[] = [];
    const hub = {
      call: async <T,>(method: string): Promise<T> => {
        calls.push(method);
        return (method === "messaging.save"
          ? { config: { enabled: savedEnabled } }
          : {}) as T;
      },
    };
    return { hub, calls };
  }

  it("follows the backend's returned enabled (stop) even when the local recompute says on", async () => {
    const { hub, calls } = fakeHub(false); // backend persisted OFF...
    await togglePlatform({ hub, name: "x", plat: "qq", next: true, // ...while local would say ON
                           cfg: { adapters: { qq: {} } } });
    expect(calls).toContain("gateway.stop");
    expect(calls).not.toContain("gateway.start");
  });

  it("follows the backend's returned enabled (start) when it says on", async () => {
    const { hub, calls } = fakeHub(true);
    await togglePlatform({ hub, name: "x", plat: "qq", next: true, cfg: { adapters: { qq: {} } } });
    expect(calls).toContain("gateway.start");
    expect(calls).not.toContain("gateway.stop");
  });
});
