import { describe, it, expect, beforeEach } from "vitest";
import { I18N } from "./strings";
import { translate, normalizeLang, detectInitialLang, type Lang } from "./index";

describe("I18N strings map", () => {
  // Pin the count: the source dict (front/web/i18n.js) had 445 keys; the password-
  // login overlay (§4b) added 8 (login-*) → 453; the merged R9/R10/R11 features
  // (visuals/keys+image/matte, from origin/main) added 44 → 497; retiring the old
  // SVG-avatar-gen + dual-theme pipeline (AvatarControls/AvatarEditor) removed 18
  // (av-ai*, av-color*, av-title, av-image, av-png-note, av-builtin-note,
  // sec-visual, visual-after-wake) → 479; the rejoin-gap notice (visible
  // reconnect-gap) added 1 → 480; the safety batch (card-deleted, undo, restored,
  // discard-edits-q, vis-del-q, vis-deleting) added 6 → 486; the interaction batch
  // (save-failed, resetting, reset-done, open-failed) added 4 → 490; the in-flow
  // model gate (gate-title/sub/key-ph/openrouter-note/advanced/continue) added 6 → 496;
  // onboarding delight (vis-need-key, vis-need-key-cta, chat-say-hi) added 3 → 499;
  // unifying the Keys settings surface (#5) added 5 (model-label, keys-edit,
  // keys-set, keys-saved, set-keys) → 504; the matte models cleanup (#4) added 1
  // (matte-shared-note) → 505; the in-session visuals editor (#1b) added 3
  // (vis-kind-keyvisual, p-visuals, vis-session-note) → 508; dropping the
  // muse-label (the say|muse channel is a backend forwarding hint, not a desktop
  // display distinction — muse now renders identically to say) removed 1 → 507.
  // Deferring card import (removed the Import overlay + file .json/.png path; paste a
  // foreign card's JSON into the create box instead) removed 3 (import, btn-import,
  // imported) → 504. The model-picker refactor (Providers pane = key library,
  // 模型 pane = picker) added 5 (model-pane-sub, model-other, model-other-note,
  // model-other-ph, model-no-key) → 509; the picker redesign added 2 note keys
  // (model-note-flash, model-note-pro) → 511.
  // Multi-provider image generation (Ark / DashScope / OpenAI / OpenRouter) added 7
  // (img-active, img-key-ready, img-key-missing-row, prov-image-ark-note,
  // img-unset, img-key-missing, img-prov-hint) → 555. (prov-openai-desc already
  // existed; the OpenAI provider preset reuses it.) Then unifying the image key on
  // the provider keyring (no legacy single image_api_key input) dropped 2
  // (prov-image-ark-note, image-key-ph) → 553.
  it("preserves the full key set from the source dict (553 keys)", () => {
    expect(Object.keys(I18N).length).toBe(553);
  });

  it("every value is a [zh, en] string tuple", () => {
    for (const [key, pair] of Object.entries(I18N)) {
      expect(Array.isArray(pair), `${key} is a tuple`).toBe(true);
      expect(pair.length, `${key} has 2 entries`).toBe(2);
      expect(typeof pair[0], `${key}[zh] is string`).toBe("string");
      expect(typeof pair[1], `${key}[en] is string`).toBe("string");
    }
  });

  // Spot-check several keys match the source verbatim (zh + en).
  it("carries representative keys verbatim", () => {
    expect(I18N["nav-charas"]).toEqual(["角色", "Characters"]);
    expect(I18N["nav-settings"]).toEqual(["设置", "Settings"]);
    expect(I18N["ago-min"]).toEqual(["分钟前", "min ago"]);
    expect(I18N["life-idle-next"]).toEqual([
      "空闲 · 约 {n} 分钟后自发一次",
      "Idle · next self-paced turn in ~{n} min",
    ]);
    expect(I18N["composer-ph"]).toEqual(["对{name}说点什么…", "Say something to {name}…"]);
    expect(I18N["conn-lost"]).toEqual(["连接断开，正在重连…", "Connection lost — reconnecting…"]);
  });

  it("keeps {var} placeholders intact for interpolated keys", () => {
    expect(I18N["wake-title"][0]).toContain("{name}");
    expect(I18N["wake-title"][1]).toContain("{name}");
    expect(I18N["key-update-apply"][0]).toContain("{n}");
  });
});

describe("translate() — the pure t() core", () => {
  it("picks zh vs en by lang", () => {
    expect(translate("zh", "nav-charas")).toBe("角色");
    expect(translate("en", "nav-charas")).toBe("Characters");
  });

  it("falls back to the raw key when missing", () => {
    expect(translate("zh", "no-such-key")).toBe("no-such-key");
    expect(translate("en", "no-such-key")).toBe("no-such-key");
  });

  it("substitutes a single {var}", () => {
    expect(translate("en", "composer-ph", { name: "Quinn" })).toBe("Say something to Quinn…");
    expect(translate("zh", "composer-ph", { name: "小Q" })).toBe("对小Q说点什么…");
  });

  it("substitutes numeric vars and coerces to string", () => {
    expect(translate("en", "ago-min")).toBe("min ago");
    expect(translate("en", "key-update-apply", { n: 3 })).toBe("Update 3 characters");
    expect(translate("zh", "key-update-apply", { n: 3 })).toBe("更新 3 个角色");
  });

  it("replaces every occurrence of a repeated {var} (replaceAll semantics)", () => {
    // del-step1-ph is exactly "{name}" in both languages → full replacement.
    expect(translate("en", "del-step1-ph", { name: "Ada" })).toBe("Ada");
  });

  it("leaves unknown placeholders untouched", () => {
    expect(translate("en", "composer-ph", { other: "x" })).toBe("Say something to {name}…");
  });
});

describe("normalizeLang", () => {
  it("maps en → en and everything else → zh", () => {
    expect(normalizeLang("en")).toBe("en");
    expect(normalizeLang("zh")).toBe("zh");
    expect(normalizeLang("zh-CN")).toBe("zh");
    expect(normalizeLang("fr")).toBe("zh");
    expect(normalizeLang(null)).toBe("zh");
    expect(normalizeLang(undefined)).toBe("zh");
  });
});

describe("detectInitialLang — saved choice then navigator", () => {
  beforeEach(() => {
    localStorage.clear();
  });

  it("prefers a saved lm-lang", () => {
    localStorage.setItem("lm-lang", "en");
    expect(detectInitialLang()).toBe("en");
    localStorage.setItem("lm-lang", "zh");
    expect(detectInitialLang()).toBe("zh");
  });

  it("falls back to navigator.language (zh* → zh, else en)", () => {
    localStorage.removeItem("lm-lang");
    const langs: Array<[string, Lang]> = [
      ["zh-CN", "zh"],
      ["zh", "zh"],
      ["en-US", "en"],
      ["fr-FR", "en"],
    ];
    for (const [nav, expected] of langs) {
      Object.defineProperty(navigator, "language", { value: nav, configurable: true });
      expect(detectInitialLang(), nav).toBe(expected);
    }
  });
});
