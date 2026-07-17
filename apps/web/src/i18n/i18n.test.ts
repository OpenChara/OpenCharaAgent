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
  // The personal-website feature (homepage tab + the two wake/create module
  // toggles replacing the embodiment grid) added 6 (tab-home, home-iframe-title,
  // mod-roleplay, mod-roleplay-hint, mod-website, mod-website-hint), then the
  // homepage "open full page" button added home-open-full → 560. The settings
  // module toggles' "applies on next start" hint (mod-next-start) → 561. The
  // board's 全部启动/全部关闭 (board-start-all, board-stop-all) → 563. The chat
  // right-panel consolidation (merge 技能/愿望/记忆 into one Profile tab, move visual
  // editing to the deck card editor + gateway config to the Gateways page) added
  // p-profile and removed p-visuals + vis-session-note → 562. The new-gateway
  // redesign (one modal with 角色 + 网关 selectors, replacing the chara popover)
  // added gw-new-title, gw-pick-chara-hint, gw-done → 565. Surfacing the QQ
  // (OneBot v11) + Telegram gateways in the deck added 7 (gw-qq-label, gw-qq-note,
  // gw-f-access-token, gw-telegram-label, gw-tg-note, gw-f-tg-api-base,
  // gw-h-tg-api-base) → 572. The status pane's per-chara model + reasoning editor
  // (model Select → /model, reasoning seg → /reasoning) added p-model-scope-note → 573.
  // The visuals brief editor + un-locked art style added 13 (vis-brief-title/sub/
  // edit/hide/loading/rebuild/appearance/style/palette/world/theme + vis-matte-hint/
  // vis-matte-cta) → 587. Per-slot download + overwrite-on-regenerate warning added
  // 2 (vis-download, vis-regen-overwrite) → 589. The restored visuals pipeline
  // (keyvisual + stickers generation, async polling, identity anchor) added 3
  // (vis-kind-stickers, vis-anchor-hint, vis-gen-progress) → 592. The runtime
  // sandbox⇄admin isolation toggle in chara settings added 2 (p-sandbox-sub,
  // iso-admin-confirm) → 594. Then the wake sheet's isolation picker became a plain
  // on/off 沙盒 switch (like the other wake toggles): removed 5 (wake-iso, iso-dir,
  // iso-sandbox, iso-dir-d, iso-sandbox-d), added wake-iso-sub → 590.
  // The visuals editor's "write the brief first" gate added vis-need-brief → 591.
  // A descriptive post-generation toast added vis-gen-done + vis-gen-done-fallback → 593.
  // The matte pane's 选择/已选择 toggle + deps-repair path added 4 (matte-select,
  // matte-selected, matte-deps-missing, matte-deps-fix) → 597.
  // The visual editor's matte-skipped notice added vis-matte-skipped → 598.
  // The per-kind 额外提示词 input added vis-extra-ph → 601 (count was 600). The
  // candidate-gallery UI added vis-cut, vis-cutting, vis-cand-pick → 604. The sticker
  // rework (grid picker, per-image rename/delete, raw-sheet re-slice) added 5
  // (vis-generate-more, vis-grid-label, vis-rename, vis-sheet-label, vis-reslice) → 609.
  // The mockup-v2 visuals restructure (recipe bar + master/detail kind selector +
  // stage/rail) added 3 (vis-recipe-need, vis-recipe-gen, vis-cand-title) → 612.
  // Editing a running chara (live card edit + activation badges + apply + wake
  // in-flight) added 9 (cv-live-edit-note, cv-zone-next-start, cv-zone-next-turn,
  // cv-apply-pending, cv-apply-now, cv-applied, cv-done, wake-generating,
  // wake-inflight-q) → 621. The card asset library tab (素材) added 3 (cv-tab-assets,
  // cv-assets-note, cv-assets-empty) → 624; the any-format rework added 2
  // (cv-asset-download, cv-asset-toobig) → 626. Dropped the aspiration profile hint
  // (polaris-hint — the 理想 pane shows the value or the empty/CTA line, no preamble) → 625.
  // The structured world-book editor added 11 (wb-add, wb-gen, wb-expand, wb-empty,
  // wb-reorder, wb-type-tip, wb-key-ph, wb-key-del, wb-del, wb-content-ph, wb-gen-empty) → 636.
  // The Settings · 关于 update + changelog pane (coupled to GitHub Releases) added 18
  // (upd-current, upd-ch-dev, upd-ch-wheel, upd-check, upd-checking, upd-available,
  // upd-uptodate, upd-apply, upd-applying, upd-done, upd-restart, upd-failed,
  // upd-changelog, upd-installed, upd-no-notes, upd-behind, upd-nudge, upd-nudge-view) → 654.
  // World-book reorder moved from drag to up/down buttons (touch/keyboard-friendly):
  // dropped wb-reorder, added wb-move-up + wb-move-down → 655.
  // The model picker's offline/cached-list notice (model-list-stale) → 656.
  // Removed the hardcoded recommended-model quick-picks (model-note-flash/pro) → 654.
  // The force-sandbox distribution lock notice (p-sandbox-locked) → 655.
  // Surfacing the Discord + Slack gateways in the deck added 16 (gw-f-owner-id,
  // gw-h-owner-id, gw-f-channel-id, gw-h-channel-id, gw-discord-label, gw-discord-blurb,
  // gw-discord-note, gw-f-discord-token, gw-h-discord-token, gw-slack-label,
  // gw-slack-blurb, gw-slack-note, gw-f-slack-bot-token, gw-h-slack-bot-token,
  // gw-f-slack-app-token, gw-h-slack-app-token) → 671.
  // The Market section (nav-market + 13 market-* keys) → 694.
  // + 3 create-flow paste-import keys (create-card-detected / -import-faithful / -import-done) → 697.
  // + 2 drag-drop keys (create-drop-hint / create-importing) → 699.
  // + 9 Market v2 keys (sort tabs, filters, load-more, results, example, source link) → 708.
  // + 6 Market filter-panel keys (filters button, group headers, nsfw note, clear) → 714.
  // − 3 dead tab keys removed with the Skills "coming soon" tab (characters/skills/soon) → 711.
  // − 4 keys removed with the redundant image-gen section in the Providers pane
  //   (prov-image-section, prov-image-desc, img-active, img-key-missing-row;
  //   img-key-ready kept — still used by TaskModels) → 707.
  // + 3 chat-timezone keys (set-timezone, set-timezone-sub, tz-auto).
  // (+3 more from concurrent work landed alongside, not itemized above) → 713.
  // Chara restart/delete: removed the old clear-context (-4: reset-confirm, p-reset,
  // resetting, reset-done) and added restart + soft-delete keys (+9: p-restart,
  // p-restart-sub, restarting, restart-done, p-delete, p-delete-sub, deleting,
  // delete-done, delete-confirm) → 718.
  // − 1 (st-arrived "· 你来了 ·") removed: attach injects no presence marker, so the
  //   client draws no "arrived" divider either (the .arrived CSS stays for rejoin-gap) → 717.
  // + vis-ref-del-q (the "delete this reference? irreversible" confirm) → 718.
  // + market-tag-add (manual tag entry in the market filter panel) → 719.
  // The chat backdrop/sprite prefs UI (10: p-backdrop, p-backdrop-sub, p-veil,
  // p-veil-sub, p-sprite-pos, p-sprite-opacity, sprite-off/left/center/right)
  // + the composer's send-button label (composer-send — the stop button now
  // coexists with send while streaming) → 730.
  // + preview-you (the mobile conversation list's "你: / You:" prefix on a
  //   user-authored last message) → 731.
  it("preserves the full key set from the source dict (731 keys)", () => {
    expect(Object.keys(I18N).length).toBe(732);
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
