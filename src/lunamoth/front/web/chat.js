/* LunaMoth Desktop renderer — the chara page: chat stream, the persistent
   right panel (status zone + settings), works / terminal sibling pages,
   the avatar editor and the per-chara gateway card.
   Idle driving is SERVER-SIDE only (supervisor) — this file never calls idle. */
"use strict";

/* ============================ AVATAR EDITOR ============================
   点头像即编辑：三条路径 — AI 重新生成（等待后端）、改主题色（纯前端）、
   直接改 SVG。保存写回卡册原卡 extensions.lunamoth。 */
function recolorSvg(svg, newColor) {
  const counts = {};
  const re = /(?:fill|stroke)\s*=\s*["'](#[0-9a-fA-F]{3,8})["']/g;
  let m;
  while ((m = re.exec(svg))) {
    const c = m[1].toLowerCase();
    counts[c] = (counts[c] || 0) + 1;
  }
  const dom = Object.entries(counts).sort((a, b) => b[1] - a[1])[0];
  if (!dom) return svg;
  return svg.replaceAll(new RegExp(dom[0].replace("#", "#"), "gi"), newColor);
}

async function openAvatarEditor(deckCard) {
  let full;
  try {
    full = await hub.call("card.read", { path: deckCard.path }, 20000);
  } catch (e) { toast(rpcErrText(e), true); return; }
  if (!full.raw || !full.raw.data) { toast("PNG cards: avatar editing needs a JSON card", true); return; }
  const data = full.raw.data;
  const ext0 = (data.extensions && data.extensions.lunamoth) || {};
  const work = {
    svg: String(ext0.avatar_svg || ""),
    color: /^#[0-9a-fA-F]{6}$/.test(String(ext0.theme_color || "")) ? String(ext0.theme_color).toUpperCase() : "#5B9FD4",
  };

  const preview = el("div", { class: "av-preview" });
  function refresh() {
    preview.innerHTML = "";
    preview.style.cssText = `--card-theme:${work.color}`;
    if (safeSvgForPreview(work.svg)) preview.appendChild(el("img", { src: dataUriSvg(work.svg), alt: "" }));
    else preview.appendChild(document.createTextNode(glyphOf(full.name)));
  }
  refresh();

  const svgText = el("textarea", { class: "svg-edit", placeholder: "<svg viewBox=\"0 0 64 64\" …" });
  svgText.value = work.svg;
  svgText.addEventListener("input", () => { work.svg = svgText.value; refresh(); });

  const colorInput = el("input", { type: "color", value: work.color });
  colorInput.addEventListener("input", () => {
    const next = colorInput.value.toUpperCase();
    if (work.svg) {
      work.svg = recolorSvg(work.svg, next);
      svgText.value = work.svg;
    }
    work.color = next;
    refresh();
  });

  const saveBtn = el("button", { class: "btn primary" }, t("save"));
  if (deckCard.builtin) saveBtn.disabled = true;
  saveBtn.addEventListener("click", async () => {
    saveBtn.disabled = true;
    try {
      const ext = data.extensions = data.extensions || {};
      const lm = ext.lunamoth = ext.lunamoth || {};
      lm.avatar_svg = work.svg;
      lm.theme_color = work.color;
      await hub.call("card.save", { data: full.raw, path: deckCard.path }, 20000);
      toast(t("saved"));
      closeModal();
      await refreshHub();
      if (state.chat) state.chat.refreshIdentity();
    } catch (e) {
      saveBtn.disabled = false;
      toast(rpcErrText(e), true);
    }
  });

  openModal(el("div", null,
    el("h2", null, `${full.name} · ${t("av-title")}`),
    deckCard.builtin ? el("div", { class: "av-note amber", style: "margin-bottom:12px" }, t("av-builtin-note")) : null,
    (!deckCard.builtin && deckCard.frozen) ? el("div", { class: "av-note", style: "margin-bottom:12px" },
      t("av-frozen-note", { names: (deckCard.used_by || []).join("、") })) : null,
    el("div", { class: "av-top" }, preview,
      el("div", null,
        el("div", { class: "av-sec" },
          el("h4", null, t("av-ai")),
          el("button", { class: "btn soft", disabled: "" }, "✦ " + t("av-ai")),
          el("div", { class: "av-note", style: "margin-top:5px" }, t("av-ai-wait"))),
        el("div", { class: "av-sec" },
          el("h4", null, t("av-color")),
          colorInput))),
    el("div", { class: "av-sec" },
      el("h4", null, t("av-svg")),
      svgText),
    el("div", { class: "acts", style: "margin-top:14px" },
      el("button", { class: "btn text", onclick: closeModal }, t("cancel")),
      el("div", { class: "grow" }),
      saveBtn)), true);
}

/* ============================ GATEWAY CARD（per-chara） ============================ */
const GW_PLATFORMS = {
  wecom: {
    label: "WeCom 企业微信",
    blurb: "gw-wecom-blurb",
    steps: ["gw-wecom-s1", "gw-wecom-s2", "gw-wecom-s3"],
    required: [["corp_id", "CorpID", false], ["secret", "Secret", true], ["agent_id", "AgentId", false],
               ["token", "Token", true], ["encoding_aes_key", "EncodingAESKey", true]],
    recommended: [["to_user", "to_user", false]],
    advanced: [["host", "host", false], ["port", "port (8128)", false], ["path", "path", false], ["api_base", "api_base", false]],
  },
  weixin: {
    label: "微信 · iLink",
    blurb: "gw-weixin-blurb",
    qr: true,
    note: "gw-weixin-note",
    required: [],
    recommended: [],
    advanced: [["base_url", "base_url", false], ["bot_type", "bot_type (3)", false],
               ["long_poll_timeout_ms", "long_poll_timeout_ms", false], ["api_timeout_ms", "api_timeout_ms", false]],
  },
  qq: {
    label: "QQ · OneBot",
    blurb: "gw-qq-blurb",
    steps: ["gw-qq-s1", "gw-qq-s2", "gw-qq-s3"],
    required: [["url", "ws://… (forward WebSocket)", false], ["peer_id", "", false]],
    recommended: [["access_token", "access_token", true]],
    advanced: [],
  },
};

async function openGatewayModal(name, displayName) {
  const shown = displayName || ((state.chat && state.chat.name === name) ? state.chat.charName : name);
  let cfg = null;          // messaging.get result (null = backend RPC missing)
  let backendMissing = false;
  try {
    cfg = await hub.call("messaging.get", { name }, 15000);
  } catch (e) {
    if (e && e.code === -32601) backendMissing = true;
    else if (e && e.message !== "not connected") backendMissing = true;
  }
  let gwStatus = { state: "stopped", platform: "", detail: "" };
  try { gwStatus = await hub.call("gateway.status", { name }, 15000); } catch (e) { /* keep */ }

  const adapters = (cfg && cfg.adapters) || {};
  const platKeys = Object.keys(GW_PLATFORMS);
  let plat = platKeys.find((k) => adapters[k]) || "wecom";
  let enabled = !!(cfg && cfg.enabled);
  const inputs = {};      // field -> input element (per render)
  let allowedInput = null;

  const root = el("div", null);

  function requiredFilled(p) {
    const spec = GW_PLATFORMS[p];
    const a = adapters[p] || {};
    return spec.required.length === 0
      ? Object.keys(a).length > 0 || p === "weixin"
      : spec.required.every(([f]) => String(a[f] ?? "").length > 0);
  }

  function chips() {
    const run = gwStatus && gwStatus.state === "running";
    return el("div", { class: "gw-chips" },
      el("span", { class: "gw-chip " + (enabled ? "ok" : "") }, enabled ? t("gw-enabled") : t("gw-disabled")),
      el("span", { class: "gw-chip " + (requiredFilled(plat) ? "ok" : "warn") },
        requiredFilled(plat) ? t("gw-configured") : t("gw-needs-setup")),
      el("span", { class: "gw-chip " + (run ? "ok" : "") }, run ? t("gw-running") : t("gw-stopped")));
  }

  function fieldRow(plat2, f, label, secret, why) {
    const a = adapters[plat2] || {};
    const input = el("input", {
      value: a[f] !== undefined && a[f] !== null ? String(a[f]) : "",
      placeholder: secret ? "••••" : "",
      type: secret ? "password" : "text",
    });
    if (backendMissing) input.disabled = true;
    inputs[f] = input;
    return el("div", { class: "gw-field" },
      el("label", null, label || f),
      why ? el("div", { class: "why" }, why) : null,
      input,
      secret ? el("div", { class: "why" }, t("gw-secret-keep")) : null);
  }

  function render() {
    root.innerHTML = "";
    for (const k of Object.keys(inputs)) delete inputs[k];
    const spec = GW_PLATFORMS[plat];
    root.appendChild(el("h2", null, t("gw-title", { name: shown })));
    root.appendChild(el("div", { class: "sub" }, t("gw-sub")));
    root.appendChild(el("div", { class: "gw-plats" },
      ...platKeys.map((k) => el("button", { class: k === plat ? "on" : "", onclick: () => { plat = k; render(); } },
        GW_PLATFORMS[k].label))));
    if (backendMissing) root.appendChild(el("div", { class: "gw-banner" }, t("gw-wait-backend")));
    root.appendChild(chips());
    root.appendChild(el("div", { class: "gw-blurb" }, t(spec.blurb)));

    if (spec.steps) {
      root.appendChild(el("div", { class: "gw-sec" },
        el("h4", null, t("gw-creds")),
        el("ol", { class: "gw-steps" }, ...spec.steps.map((s) => el("li", null, t(s))))));
    }
    if (spec.qr) {
      root.appendChild(el("div", { class: "gw-qr-box" }, t("gw-weixin-qr")));
      root.appendChild(el("div", { class: "gw-blurb" }, t(spec.note)));
    }
    if (spec.required.length) {
      root.appendChild(el("div", { class: "gw-sec" },
        el("h4", null, t("gw-required")),
        ...spec.required.map(([f, label, secret]) =>
          fieldRow(plat, f, f === "peer_id" ? t("gw-f-peer-id") : label, secret))));
    }
    const recSec = el("div", { class: "gw-sec" }, el("h4", null, t("gw-recommended")));
    for (const [f, label, secret] of spec.recommended) recSec.appendChild(fieldRow(plat, f, label, secret));
    // allowed_senders 顶层，带安全理由
    allowedInput = el("input", { value: (cfg && Array.isArray(cfg.allowed_senders) ? cfg.allowed_senders.join(", ") : "") });
    if (backendMissing) allowedInput.disabled = true;
    recSec.appendChild(el("div", { class: "gw-field" },
      el("label", null, t("gw-f-allowed")),
      el("div", { class: "why" }, t("gw-allowed-why")),
      allowedInput));
    root.appendChild(recSec);

    if (spec.advanced.length) {
      root.appendChild(el("details", { class: "gw-adv" },
        el("summary", null, `${t("gw-advanced")} (${spec.advanced.length})`),
        ...spec.advanced.map(([f, label, secret]) => fieldRow(plat, f, label, secret))));
    }

    const enableSwitch = el("button", { class: "switch" + (enabled ? " on" : ""), onclick: () => {
      enabled = !enabled;
      enableSwitch.classList.toggle("on", enabled);
    } });
    if (backendMissing) enableSwitch.disabled = true;
    const run = gwStatus && gwStatus.state === "running";
    const runBtn = el("button", { class: "btn soft", onclick: async () => {
      runBtn.disabled = true;
      try {
        gwStatus = await hub.call(run ? "gateway.stop" : "gateway.start", { name }, 30000);
        render();
      } catch (e) { runBtn.disabled = false; toast(rpcErrText(e), true); }
    } }, run ? t("gw-stop") : t("gw-start"));
    const saveBtn = el("button", { class: "btn primary", onclick: async () => {
      saveBtn.disabled = true;
      try {
        const fields = {};
        const spec2 = GW_PLATFORMS[plat];
        for (const [f, , secret] of [...spec2.required, ...spec2.recommended, ...spec2.advanced]) {
          const v = inputs[f] ? inputs[f].value.trim() : "";
          if (secret && (v === "" || v === "••••")) continue; // 留空/掩码 = 不修改
          if (v !== "") fields[f] = v;
        }
        const config = {
          enabled,
          allowed_senders: allowedInput.value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
          adapters: { [plat]: fields },
        };
        cfg = await hub.call("messaging.save", { name, config }, 20000);
        toast(t("saved"));
        render();
      } catch (e) {
        saveBtn.disabled = false;
        toast(rpcErrText(e), true);
      }
    } }, t("gw-save"));
    if (backendMissing) saveBtn.disabled = true;

    root.appendChild(el("div", { class: "gw-foot" },
      enableSwitch, el("span", { class: "enable-lbl" }, enabled ? t("gw-enabled") : t("gw-disabled")),
      el("div", { class: "grow" }), runBtn, saveBtn));
  }
  render();
  openModal(root, true);
}

/* ============================ CHAT CONTROLLER ============================ */
function lifeAttr(life) {
  if (!life || !life.state) return "";
  if (life.state === "idle_countdown") return "working"; // 机制不是情绪：与 working 同一 register
  return life.state;
}

class ChatController {
  constructor(name, opts) {
    this.name = name;
    this.opts = opts;
    this.client = new CharaClient(name);
    this.charName = name;
    this.deckCard = cardForSession(name);
    this.mode = "live";
    this.showThinking = false;
    this.disposed = false;
    this.cur = { kind: null, node: null, textNode: null };
    this.toolChips = null;
    this.activeTools = new Map();
    this.pendingSuper = false;
    this.turnThink = null;
    this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
    this.life = null;
    this.snap = null;
    this.superReadTs = 0;
    this.pendingSupers = [];   // {node, ts} bright bubbles awaiting the fade
    this.page = "chat";
    this.works = null;
    this.worksFilter = "all";
    this.lifeTimer = null;
    this.snapTimer = null;
    this.worksTimer = null;
    this.sbTimer = null;
    this.sessionStart = Date.now();
    this._panelSig = "";
    try {
      this.thinkExpanded = localStorage.getItem("lm-chat-thinking-expanded") === "1";
    } catch (e) {
      this.thinkExpanded = false;
    }
    try {
      this.worksSeen = Number(localStorage.getItem(`lm-works-seen:${name}`) || 0);
    } catch (e) { this.worksSeen = 0; }
    const entry = (state.hub && state.hub.sessions.find((s) => s.name === name)) || null;
    if (entry) this.charName = entry.char_name;
  }

  entry() {
    return (state.hub && state.hub.sessions.find((s) => s.name === this.name)) || null;
  }

  /* ---- identity: avatar + accent from the deck card behind the session ---- */
  refreshIdentity() {
    this.deckCard = cardForSession(this.name);
    const card = this.deckCard;
    const root = $("chat-root");
    root.style.setProperty("--chara-accent", card && card.theme_color ? card.theme_color : "var(--accent)");
    const btn = $("chat-avatar");
    btn.querySelectorAll("img,.glyph-txt").forEach((n) => n.remove());
    btn.classList.remove("p-0", "p-1", "p-2", "p-3", "p-4", "p-5");
    if (card && card.avatar_svg) {
      btn.style.cssText = card.theme_color ? `--card-theme:${card.theme_color}` : "";
      btn.insertBefore(el("img", { src: dataUriSvg(card.avatar_svg), alt: "" }), btn.firstChild);
    } else if (card && card.theme_color) {
      btn.style.cssText = `--card-theme:${card.theme_color}`;
      btn.insertBefore(el("span", { class: "glyph-txt" }, glyphOf(this.charName)), btn.firstChild);
    } else {
      btn.style.cssText = "";
      btn.classList.add(paletteClass(this.charName));
      btn.insertBefore(el("span", { class: "glyph-txt" }, glyphOf(this.charName)), btn.firstChild);
    }
    const empty = $("stream-inner").querySelector(".chat-empty");
    if (empty) {
      const old = empty.querySelector(".avatar-s");
      if (old) old.replaceWith(this.bigAvatar());
    }
  }

  msgAvatar() {
    return avatarNode(this.charName, this.deckCard, "avatar-s");
  }
  bigAvatar() {
    return avatarNode(this.charName, this.deckCard, "avatar-s");
  }

  /* ---- lifecycle ---- */
  async open(page) {
    $("stream-inner").innerHTML = "";
    this.setWorkState(false);
    $("chat-name").textContent = this.charName;
    $("chat-statusword").textContent = t("st-connecting");
    $("chat-dot").className = "mini-dot off";
    $("composer-input").placeholder = t("composer-ph", { name: this.charName });
    $("chat-root").removeAttribute("data-life");
    this.refreshIdentity();
    this.bindUI();
    this.showPage(page || "chat");
    this.renderTerm();
    this.startSessionTimer();
    try {
      await this.client.connect();
      this.client.onProtocolEvent = (ev) => this.onEvent(ev);
      this.client.onPermissionAsk = (p) => this.onPermission(p);
      this.client.onLifeState = (p) => this.onLifeState(p);
      this.client.onRejoinGap = () => {
        this.client.clearRejoin();
        $("stream-inner").innerHTML = "";
      };
      this.client.onClose = (ev) => {
        if (!this.disposed) {
          this.note((ev && ev.reason) || t("conn-lost"));
          $("chat-dot").className = "mini-dot off";
        }
      };
      try {
        const r = await hub.call("superchat.read", { name: this.name, ts: 0 }, 10000);
        this.superReadTs = Number(r && r.read_ts) || 0;
      } catch (e) { /* keep 0 */ }
      const info = await this.client.attach();
      if (this.disposed) return;
      this.charName = info.char_name || this.charName;
      this.mode = info.mode || "live";
      this.showThinking = !!info.show_thinking;
      $("chat-name").textContent = this.charName;
      $("chat-dot").className = "mini-dot";
      this.refreshIdentity();
      this.renderRestored(info.restored || []);
      this.flushSuperReads();
      this.refreshSnapshot().then(() => {
        // attach ≠ 唤醒：resting 时保持沉睡氛围，不宣称"它知道你来了"
        const resting = this.snap && this.snap.rest_until * 1000 > Date.now();
        const hasOpening = info.opening && info.opening !== "none" && info.opening_text;
        if (!resting && !hasOpening) this.systemLine(t("st-arrived"), "arrived");
        this.maybeEmptyState();
      });
      this.renderPanelMain();
      this.snapTimer = setInterval(() => { if (!document.hidden) this.refreshSnapshot(); }, 6000);
      this.worksTimer = setInterval(() => { if (!document.hidden) this.pollWorks(); }, 45000);
      this.pollWorks();
      if (this.opts.netOn) await this.command("/net on", true);
      await this.handleOpening(info);
    } catch (e) {
      if (!this.disposed) this.note(e.message);
    }
  }

  dispose() {
    this.disposed = true;
    if (this._visHandler) document.removeEventListener("visibilitychange", this._visHandler);
    clearInterval(this.lifeTimer);
    clearInterval(this.snapTimer);
    clearInterval(this.worksTimer);
    clearInterval(this.sbTimer);
    $("sb-timer").textContent = "";
    $("chat-root").removeAttribute("data-life");
    const c = this.client;
    (async () => {
      try { if (c.streaming) await c.interrupt(); } catch (e) { /* gone */ }
      try { await c.detach(); } catch (e) { /* gone */ }
      c.close();
    })();
    setTimeout(refreshHub, 600);
  }

  startSessionTimer() {
    const tick = () => {
      const s = Math.floor((Date.now() - this.sessionStart) / 1000);
      const mm = Math.floor((s % 3600) / 60).toString().padStart(2, "0");
      const ss = (s % 60).toString().padStart(2, "0");
      $("sb-timer").textContent = s >= 3600 ? `${Math.floor(s / 3600)}:${mm}:${ss}` : `${mm}:${ss}`;
    };
    tick();
    this.sbTimer = setInterval(tick, 1000);
  }

  /* ---- 对话|作品|终端：常驻不卸载，display 切换 ---- */
  showPage(page) {
    this.page = page;
    document.querySelectorAll("#chat-tabs span").forEach((s2) =>
      s2.classList.toggle("on", s2.dataset.page === page));
    document.querySelectorAll(".chat-page").forEach((p) =>
      p.classList.toggle("on", p.id === `page-${page}`));
    if (page === "works") {
      this.renderWorks();
      this.markWorksSeen();
    }
  }

  hashFor(page) {
    const base = `#/chara/${encodeURIComponent(this.name)}`;
    return page === "chat" ? base : `${base}/${page}`;
  }

  /* ---- opening decision tree (AttachInfo) ---- */
  async handleOpening(info) {
    if (info.opening === "greeting" && info.opening_text) {
      this.appendCharText(info.opening_text);
      this.finalize();
      try { await this.client.sock.call("greet", { text: info.opening_text }, 10000); } catch (e) { /* older server */ }
    } else if (info.opening === "arrival" && info.opening_text) {
      await this.runStream(() => this.client.sock.call("event", { text: info.opening_text }));
    } else if (info.opening === "probe" && info.opening_text) {
      await this.runStream(() => this.client.send(info.opening_text));
    }
  }

  /* ---- empty chat = the card's brand moment ---- */
  maybeEmptyState() {
    const inner = $("stream-inner");
    if (inner.children.length > 1) return;
    const onlyArrived = inner.children.length === 1 && inner.firstChild.classList.contains("arrived");
    if (inner.children.length === 0 || onlyArrived) {
      const card = this.deckCard;
      const tagline = (card && card.tagline) || "";
      const av = this.bigAvatar();
      inner.insertBefore(el("div", { class: "chat-empty" },
        av, el("b", null, this.charName),
        tagline ? el("div", { class: "tagline" }, tagline) : null), inner.firstChild);
    }
  }
  clearEmpty() {
    const node = $("stream-inner").querySelector(".chat-empty");
    if (node) node.remove();
  }

  /* ---- restored history ---- */
  renderRestored(messages) {
    const inner = $("stream-inner");
    for (const m of messages.slice(-80)) {
      if (!m) continue;
      const content = typeof m.content === "string" ? m.content : "";
      const hasText = content.trim().length > 0;
      if (m.role === "user") {
        if (!hasText) continue;
        inner.appendChild(el("div", { class: "user-msg" }, el("div", { class: "bubble" }, m.content)));
      } else if (m.role === "system") {
        if (hasText && m.kind !== "summary") this.systemLine(content);
      } else if (m.role === "assistant") {
        if (m.kind === "think") {
          if (hasText) {
            this.appendMuseText(content);
            this.closeCurrent();
          }
          continue;
        }
        if (hasText) {
          this.appendCharText(content);
          this.closeCurrent();
        }
        for (const speak of speakTextsFromMessage(m)) {
          this.appendCharText(speak, { superChat: true, ts: m.ts || Date.now() / 1000 });
          this.closeCurrent();
        }
      }
    }
    this.scrollDown(true);
  }

  /* ---- streaming protocol events ---- */
  onEvent(ev) {
    if (!ev || this.disposed) return;
    if (ev.type === "text") {
      this.setWorkState(true, "generate");
      const isSuper = ev.channel === "say" && this.pendingSuper;
      if (ev.channel === "say") this.pendingSuper = false;
      if (ev.channel === "muse") {
        this.appendMuseText(ev.text);
      } else {
        this.appendCharText(ev.text, { superChat: isSuper });
        // Electron shell: collect say-channel text for a system notification.
        this.pendingNotify = (this.pendingNotify || "") + ev.text;
      }
      this.setStatusWord(t("st-creating"));
    } else if (ev.type === "think") {
      this.appendThinking(ev.text);
    } else if (ev.type === "tool_start") {
      this.showToolStart(ev);
    } else if (ev.type === "tool_end") {
      this.showToolEnd(ev);
    } else if (ev.type === "notice") {
      this.note(ev.text || ev.kind);
    }
    this.scrollDown();
  }

  appendCharText(text, opts) {
    this.clearEmpty();
    const kind = opts && opts.superChat ? "super" : "say";
    const ts = opts && opts.ts ? Number(opts.ts) : Date.now() / 1000;
    if (this.cur.kind !== kind) {
      this.closeCurrent();
      this.breakToolGroup();
      const textDiv = el("div", { class: "text" });
      const nameLine = el("div", { class: "name" }, this.charName);
      if (kind === "super") nameLine.appendChild(superBadge());
      else if (isTechnical()) nameLine.appendChild(el("span", { class: "chan-badge" }, t("channel-say")));
      const node = el("div", { class: "char-msg" + (kind === "super" ? " super-chat" : "") },
        this.msgAvatar(),
        el("div", { class: "body" }, nameLine, textDiv));
      if (kind === "super") {
        node.dataset.speakTs = String(ts);
        if (ts <= this.superReadTs) node.classList.add("read"); // 淡的看过了
        else this.pendingSupers.push({ node, ts });             // 亮的没看过
      }
      $("stream-inner").appendChild(node);
      this.cur = { kind, node, textNode: textDiv, raw: "" };
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.textNode.textContent = this.cur.raw;
  }

  /* 已读 = 淡化：页面可见地渲染过 → superchat.read → 整体淡下去 */
  flushSuperReads() {
    if (document.visibilityState !== "visible") return;
    const pend = this.pendingSupers.splice(0);
    if (!pend.length) return;
    const maxTs = Math.max(...pend.map((p) => p.ts));
    setTimeout(() => {
      hub.call("superchat.read", { name: this.name, ts: maxTs }, 10000)
        .then((r) => {
          this.superReadTs = Math.max(this.superReadTs, Number(r && r.read_ts) || maxTs);
          for (const p of pend) p.node.classList.add("read");
          refreshHub();
        })
        .catch(() => { this.pendingSupers.push(...pend); });
    }, 1600);
  }

  appendMuseText(text) {
    this.clearEmpty();
    if (this.cur.kind !== "muse") {
      this.closeCurrent();
      this.breakToolGroup();
      const textDiv = el("div", { class: "muse-text" });
      const node = el("div", { class: "muse-msg" },
        el("div", { class: "muse-label" }, t("muse-label")),
        textDiv);
      $("stream-inner").appendChild(node);
      this.cur = { kind: "muse", node, textNode: textDiv, raw: "" };
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.textNode.textContent = this.cur.raw;
  }

  appendThinking(text) {
    this.clearEmpty();
    if (this.cur.kind !== "think") {
      this.closeCurrent();
      this.breakToolGroup();
      if (this.turnThink && this.turnThink.node && this.turnThink.node.isConnected) {
        this.cur = this.turnThink;
      } else {
        const head = el("button", { class: "think-head streaming" });
        const body = el("div", { class: "think-body" });
        const node = el("div", { class: "think-block streaming" }, head, body);
        head.onclick = () => this.toggleThinkingExpanded();
        $("stream-inner").appendChild(node);
        this.cur = { kind: "think", node, head, body, raw: "", tokens: 0 };
        this.turnThink = this.cur;
      }
    }
    this.cur.raw = (this.cur.raw || "") + text;
    this.cur.tokens = this.cur.raw ? Math.max(1, estimateTokens(this.cur.raw)) : 0;
    this.cur.node.dataset.tokens = String(this.cur.tokens);
    this.cur.body.textContent = this.cur.raw;
    this.applyThinkState(this.cur.node, true);
    this.setWorkState(true, "think", { thinkTokens: this.cur.tokens });
  }

  closeCurrent() {
    if ((this.cur.kind === "say" || this.cur.kind === "super") && this.cur.raw) {
      this.cur.textNode.innerHTML = mdRender(this.cur.raw);
    }
    this.cur = { kind: null, node: null, textNode: null };
  }

  applyThinkState(node, streaming) {
    if (!node) return;
    const tokens = Number(node.dataset.tokens || 0) || 0;
    const head = node.querySelector(".think-head");
    const body = node.querySelector(".think-body");
    node.classList.toggle("streaming", !!streaming);
    if (head) {
      head.classList.toggle("streaming", !!streaming);
      head.textContent = streaming
        ? `✶ ${t("thinking-live", { n: tokens })}`
        : `${t("thinking-done", { n: tokens })} ${this.thinkExpanded ? "▾" : "▸"}`;
    }
    if (body) body.style.display = this.thinkExpanded ? "block" : "none";
  }

  toggleThinkingExpanded() {
    this.thinkExpanded = !this.thinkExpanded;
    try { localStorage.setItem("lm-chat-thinking-expanded", this.thinkExpanded ? "1" : "0"); } catch (e) { /* ok */ }
    this.updateThinkingBlocks();
  }

  updateThinkingBlocks() {
    $("stream-inner").querySelectorAll(".think-block").forEach((node) =>
      this.applyThinkState(node, node.classList.contains("streaming")));
  }

  finalizeThinkingBlocks() {
    $("stream-inner").querySelectorAll(".think-block.streaming").forEach((node) => {
      node.classList.remove("streaming");
      this.applyThinkState(node, false);
    });
  }

  ensureToolGroup() {
    if (this.toolChips && this.toolChips.isConnected) return this.toolChips;
    this.closeCurrent();
    const node = el("div", { class: "tool-chip-line" });
    $("stream-inner").appendChild(node);
    this.toolChips = node;
    return node;
  }

  breakToolGroup() {
    this.toolChips = null;
  }

  toolKey(ev) {
    return `${Number(ev.index || 0)}:${ev.name || "?"}`;
  }

  showToolStart(ev) {
    this.clearEmpty();
    this.closeCurrent();
    const name = ev.name || "?";
    const group = this.ensureToolGroup();
    const detail = el("div", { class: "tool-detail" }, isTechnical() ? (ev.preview || "") : "");
    const button = el("button", { class: "tool-chip running" },
      el("span", { class: "spin" }), el("span", null, `⚙ ${name}`));
    const item = el("div", { class: "tool-chip-item" }, button, detail);
    button.onclick = () => item.classList.toggle("open");
    group.appendChild(item);
    this.activeTools.set(this.toolKey(ev), { item, button, detail, name });
    this.setWorkState(true, "tool", { toolName: name });
    this.setStatusWord(t("st-creating"));
  }

  showToolEnd(ev) {
    const name = ev.name || "?";
    const key = this.toolKey(ev);
    let rec = this.activeTools.get(key);
    if (!rec) {
      const group = this.ensureToolGroup();
      const detail = el("div", { class: "tool-detail" });
      const button = el("button", { class: "tool-chip" });
      const item = el("div", { class: "tool-chip-item" }, button, detail);
      button.onclick = () => item.classList.toggle("open");
      group.appendChild(item);
      rec = { item, button, detail, name };
    }
    const ok = ev.ok !== false;
    rec.button.className = "tool-chip " + (ok ? "ok" : "err");
    rec.button.textContent = `⚙ ${name} ${ok ? "✓" : "✗"} · ${durationText(ev.duration)}`;
    rec.detail.textContent = ev.summary || t("tool-no-summary");
    rec.item.classList.toggle("has-detail", !!(ev.summary || "").trim());
    this.activeTools.delete(key);
    if (name === "speak" && ok) this.pendingSuper = true;
    if (this.activeTools.size) {
      const next = this.activeTools.values().next().value;
      this.setWorkState(true, "tool", { toolName: next ? next.name : "" });
    } else {
      this.setWorkState(true, "generate");
    }
  }

  systemLine(text, cls) {
    if (!text) return;
    this.closeCurrent();
    this.breakToolGroup();
    $("stream-inner").appendChild(el("div", { class: "sys-note" + (cls ? " " + cls : "") }, String(text).slice(0, 240)));
  }

  setWorkState(active, phase, detail) {
    const node = $("work-status");
    if (!node) return;
    if (!active) {
      this.work = { active: false, phase: "idle", thinkTokens: 0, toolName: "" };
      node.hidden = true;
      node.textContent = "";
      node.className = "work-status";
      return;
    }
    this.work = {
      active: true,
      phase: phase || this.work.phase || "generate",
      thinkTokens: detail && "thinkTokens" in detail ? detail.thinkTokens : (this.work.thinkTokens || 0),
      toolName: detail && "toolName" in detail ? detail.toolName : (this.work.toolName || ""),
    };
    node.hidden = false;
    node.className = "work-status " + this.work.phase;
    if (this.work.phase === "think") {
      node.textContent = t("work-thinking", { n: this.work.thinkTokens || 0 });
    } else if (this.work.phase === "tool") {
      node.textContent = t("work-tool", { name: this.work.toolName || "tool" });
    } else {
      node.textContent = t("work-generating");
    }
  }

  finalize() {
    this.closeCurrent();
    this.finalizeThinkingBlocks();
    this.activeTools.clear();
    this.breakToolGroup();
    this.pendingSuper = false;
    this.turnThink = null;
    this.setWorkState(false);
    this.flushSuperReads();
    // Electron shell: surface what was said while the window wasn't watched.
    if (this.pendingNotify && window.lunamothNative && !document.hasFocus())
      window.lunamothNative.notify(this.charName, this.pendingNotify.trim().slice(0, 200));
    this.pendingNotify = "";
    this.renderLifeState();
    this.scrollDown();
    if (this.page !== "works") this.pollWorks();
  }

  note(text) {
    if (!text) return;
    this.systemLine(text);
  }

  scrollDown(force) {
    const sc = $("stream");
    const nearBottom = sc.scrollHeight - sc.scrollTop - sc.clientHeight < 160;
    if (force || nearBottom) sc.scrollTop = sc.scrollHeight;
  }

  setStatusWord(word) { $("chat-statusword").textContent = word || ""; }

  /* ---- driving turns ---- */
  async runStream(fn) {
    this.setSending(true);
    this.turnThink = null;
    this.setWorkState(true, "generate");
    try {
      await fn();
    } catch (e) {
      if (!this.disposed) this.note(e.message);
    } finally {
      if (!this.disposed) {
        this.finalize();
        this.setSending(false);
        this.refreshSnapshot();
      }
    }
  }

  async sendUser(text) {
    this.clearEmpty();
    $("stream-inner").appendChild(el("div", { class: "user-msg" }, el("div", { class: "bubble" }, text)));
    this.scrollDown(true);
    await this.runStream(() => this.client.send(text));
  }

  /* ---- mood layer：状态即氛围（waiting 进度条 / working 流光 / resting 灯暗） ---- */
  onLifeState(life) {
    this.life = life || null;
    this.renderLifeState();
    if (!this.lifeTimer) this.lifeTimer = setInterval(() => this.renderLifeState(), 1000);
  }

  renderLifeState() {
    const life = this.life;
    const root = $("chat-root");
    if (!life) return;
    if (this.client.streaming) {
      root.setAttribute("data-life", "working");
      this.renderEngageBar(null);
      return;
    }
    root.setAttribute("data-life", lifeAttr(life));
    this.setStatusWord(lifeText(life));
    const resting = life.state === "resting";
    $("composer-input").placeholder = resting
      ? t("composer-resting-ph")
      : t("composer-ph", { name: this.charName });
    this.renderEngageBar(life.state === "waiting" ? life : null);
    if (isTechnical() && life.next_cycle_at) {
      const rowVal = $("p-next-cycle-val");
      if (rowVal) rowVal.textContent = t("next-cycle-at", { time: fmtClock(life.next_cycle_at) });
    }
  }

  renderEngageBar(life) {
    const bar = $("engage-bar");
    if (!life || !life.engaged_until) { bar.hidden = true; return; }
    const quiet = (this.snap && this.snap.quiet) || 300;
    const left = life.engaged_until - Date.now() / 1000;
    if (left <= 0) { bar.hidden = true; return; }
    bar.hidden = false;
    const pct = Math.max(0, Math.min(100, (left / quiet) * 100));
    bar.firstElementChild.style.width = pct + "%";
  }

  async command(line, quiet) {
    try {
      const reply = await this.client.command(line);
      if (!quiet && reply && reply.text) this.note(reply.text);
      this.refreshSnapshot();
      return reply;
    } catch (e) {
      if (!quiet) this.note(e.message);
      return null;
    }
  }

  /* ---- snapshot -> header + panel ---- */
  async refreshSnapshot() {
    if (!this.client.open || this.client.streaming) return;
    let snap;
    try { snap = await this.client.snapshot(); } catch (e) { return; }
    if (this.disposed) return;
    this.snap = snap;
    this.mode = snap.mode || this.mode;
    this.showThinking = !!snap.show_thinking;
    $("net-btn").style.display = snap.net_on ? "none" : "flex";
    $("net-btn").title = t("net-off-tip");
    if (!this.life && snap.rest_until && snap.rest_until * 1000 > Date.now()) {
      $("chat-root").setAttribute("data-life", "resting");
      this.setStatusWord(t("life-resting-until", { time: fmtClock(snap.rest_until) }));
      $("composer-input").placeholder = t("composer-resting-ph");
    }
    this.renderPanelMain();
  }

  onHubState() {
    this.renderPanelMain();
  }

  onDisplayModeChanged() {
    this._panelSig = "";
    this.renderPanelMain();
  }

  /* ============================ RIGHT PANEL ============================ */
  prow(opts) {
    // opts: {label, sub, val, valNode, click, switchOn, onSwitch, dot, chev, cls, tidy}
    const row = el("div", { class: "prow" + (opts.click || opts.onSwitch ? " click" : "") + (opts.cls ? " " + opts.cls : "") });
    const main = el("div", { class: "pmain" }, el("span", { class: "plbl" }, opts.label));
    if (opts.sub) main.appendChild(el("span", { class: "psub" }, opts.sub));
    if (opts.bar !== undefined) {
      const pct = Math.max(0, Math.min(100, opts.bar));
      main.appendChild(el("div", { class: "pbar" }, el("i", { class: pct > 85 ? "hot" : "", style: `width:${pct}%` })));
    }
    row.appendChild(main);
    if (opts.dot) row.appendChild(el("span", { class: "pdot " + opts.dot }));
    if (opts.valNode) row.appendChild(opts.valNode);
    else if (opts.val !== undefined) row.appendChild(el("span", { class: "pval" }, opts.val));
    if (opts.tidy) row.appendChild(el("button", { class: "tidy-link", onclick: (ev) => { ev.stopPropagation(); opts.tidy(); } }, t("p-tidy")));
    if (opts.onSwitch) {
      const sw = el("button", { class: "switch" + (opts.switchOn ? " on" : ""), onclick: (ev) => { ev.stopPropagation(); opts.onSwitch(); } });
      row.appendChild(sw);
    }
    if (opts.chev) row.appendChild(el("span", { class: "chev" }, "›"));
    if (opts.click) row.addEventListener("click", opts.click);
    return row;
  }

  renderPanelMain() {
    if (this.disposed) return;
    const snap = this.snap;
    const entry = this.entry();
    const pane = $("panel-main");
    const sig = JSON.stringify([
      snap && [snap.model, snap.reasoning, snap.context_tokens, snap.memory_chars, snap.net_on,
               snap.mode, snap.show_thinking, snap.isolation, snap.quiet, snap.patience, snap.embodiment],
      entry && entry.gateway && [entry.gateway.state, entry.gateway.platform],
      getLangCode(), isTechnical(),
    ]);
    if (sig === this._panelSig) return;
    this._panelSig = sig;
    pane.innerHTML = "";
    if (!snap) return;

    // —— 状态区：高频、温和、一眼可读，点击即改 ——
    const st = el("div", { class: "pgroup" }, el("h5", null, t("pg-status")));
    st.appendChild(this.prow({
      label: t("p-model"),
      valNode: el("span", { class: "pval" }, el("code", null, snap.model || "—")),
      chev: true,
      click: (ev) => this.openModelPopover(ev),
    }));
    st.appendChild(this.prow({
      label: t("p-effort"),
      val: t("eff-" + (snap.reasoning || "medium")) + (snap.reasoning_supported ? "" : " ⌀"),
      chev: true,
      click: (ev) => this.openModelPopover(ev),
    }));
    const pctCtx = snap.context_max ? Math.round(100 * snap.context_tokens / snap.context_max) : 0;
    st.appendChild(this.prow({
      label: t("p-context"),
      bar: pctCtx,
      val: `${(snap.context_tokens / 1000).toFixed(1)}k / ${(snap.context_max / 1000).toFixed(0)}k · ${pctCtx}%`,
      tidy: () => this.command("/compact"),
    }));
    const pctMem = snap.memory_max ? Math.round(100 * snap.memory_chars / snap.memory_max) : 0;
    st.appendChild(this.prow({
      label: t("p-memory"),
      bar: pctMem,
      val: `${snap.memory_chars} / ${snap.memory_max}`,
      chev: true,
      click: () => this.openPanelPage("memory"),
    }));
    st.appendChild(this.prow({
      label: t("p-sandbox"),
      valNode: el("span", { class: "pval" }, `${isoGlyph(snap.isolation)} `,
        el("code", null, (snap.workspace_root || "").split("/").slice(-2).join("/"))),
      click: () => hub.call("open.path", { path: snap.workspace_root || snap.sandbox_root }).catch((e) => toast(e.message, true)),
    }));
    st.appendChild(this.prow({
      label: t("p-net"), sub: t("p-net-sub"),
      switchOn: !!snap.net_on,
      onSwitch: () => this.command(snap.net_on ? "/net off" : "/net on", true),
    }));
    st.appendChild(this.prow({
      label: t("p-autonomy"), sub: t("p-autonomy-sub"),
      switchOn: this.mode === "live",
      onSwitch: async () => {
        this.mode = this.mode === "live" ? "chat" : "live";
        await this.command(`/mode ${this.mode}`, true);
      },
    }));
    st.appendChild(this.prow({
      label: t("p-thinking"), sub: t("p-thinking-sub"),
      switchOn: !!this.showThinking,
      onSwitch: async () => {
        this.showThinking = !this.showThinking;
        await this.command(`/thinking ${this.showThinking ? "on" : "off"}`, true);
      },
    }));
    const gw = entry && entry.gateway;
    st.appendChild(this.prow({
      label: t("p-gateway"),
      dot: gw && gw.state === "running" ? "live" : "",
      val: gw && gw.state === "running" ? t("gw-running") : t("gw-stopped"),
      chev: true,
      click: () => openGatewayModal(this.name),
    }));
    if (isTechnical() && this.life && this.life.next_cycle_at) {
      const row = this.prow({ label: t("p-next-cycle"), val: "" });
      row.querySelector(".pval").id = "p-next-cycle-val";
      row.querySelector(".pval").textContent = t("next-cycle-at", { time: fmtClock(this.life.next_cycle_at) });
      st.appendChild(row);
    }
    pane.appendChild(st);

    // —— 设置选单：凡有 /command 的能力，这里都有入口 ——
    const menu = el("div", { class: "pgroup" }, el("h5", null, t("pg-settings")));
    menu.appendChild(this.prow({ label: t("p-menu-rhythm"), chev: true, click: () => this.openPanelPage("rhythm") }));
    menu.appendChild(this.prow({ label: t("p-abilities"), chev: true, click: () => this.openPanelPage("abilities") }));
    menu.appendChild(this.prow({ label: t("p-memory"), chev: true, click: () => this.openPanelPage("memory") }));
    menu.appendChild(this.prow({ label: t("p-gateway"), chev: true, click: () => openGatewayModal(this.name) }));
    menu.appendChild(this.prow({
      label: t("p-quiet-act"),
      click: () => { this.command("/quiet 600", false); },
    }));
    pane.appendChild(menu);

    // —— 危险区：reset 在最底部 ——
    const danger = el("div", { class: "pgroup" });
    danger.appendChild(this.prow({
      label: t("p-reset"), cls: "danger click",
      click: () => {
        if (confirm(t("reset-confirm"))) {
          this.command("/reset").then(() => { $("stream-inner").innerHTML = ""; this.maybeEmptyState(); });
        }
      },
    }));
    pane.appendChild(danger);
  }

  openPanelPage(which) {
    $("panel-main").classList.remove("on");
    const page = $("panel-page");
    page.classList.add("on");
    const body = $("panel-page-body");
    body.innerHTML = "";
    const titles = { rhythm: t("p-menu-rhythm"), abilities: t("p-abilities"), memory: t("p-memory") };
    $("panel-page-title").textContent = titles[which] || "";
    if (which === "rhythm") this.renderRhythmPage(body);
    if (which === "abilities") this.renderAbilitiesPage(body);
    if (which === "memory") this.renderMemoryPage(body);
  }
  closePanelPage() {
    $("panel-page").classList.remove("on");
    $("panel-main").classList.add("on");
    this._panelSig = "";
    this.renderPanelMain();
  }

  numField(body, labelKey, whyKey, value, onSave) {
    const input = el("input", { type: "number", value: String(Math.round(value)) });
    const btn = el("button", { class: "btn soft", onclick: async () => {
      btn.disabled = true;
      await onSave(input.value.trim());
      btn.disabled = false;
    } }, t("save"));
    body.appendChild(el("div", { class: "pfield" },
      el("label", null, t(labelKey)),
      el("div", { class: "why" }, t(whyKey)),
      el("div", { class: "ctl" }, input, btn)));
  }

  renderRhythmPage(body) {
    const snap = this.snap || {};
    // quiet =「等你多久」；patience =「它自己生活的节拍」（owner 的文案语义）
    this.numField(body, "p-quiet", "p-quiet-sub", snap.quiet || 300,
      (v) => this.command(`/quiet ${v}`, false));
    this.numField(body, "p-patience", "p-patience-sub", snap.patience || 600,
      (v) => this.command(`/patience ${v}`, false));
    // Embodiment is chosen at wake and never hot-swapped (identity-layer
    // switches would rebuild the stable prefix and destroy the prompt cache).
    const stance = snap.embodiment === "actor" ? "actor" : "literal";
    body.appendChild(el("div", { class: "pfield" },
      el("label", null, t("p-embodiment") + " · " + stance),
      el("div", { class: "why" }, t("emb-" + stance)),
      el("div", { class: "why" }, t("p-embodiment-wake"))));
  }

  async renderAbilitiesPage(body) {
    const snap = this.snap || {};
    let pack = "";
    const deckCard = this.deckCard;
    if (deckCard) {
      try {
        const full = await hub.call("card.read", { path: deckCard.path }, 20000);
        const ext = full.extensions && full.extensions.lunamoth;
        if (ext && ext.toolpack) pack = String(ext.toolpack);
      } catch (e) { /* fine */ }
    }
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("p-toolpack")),
      el("div", { class: "tool-chips" },
        el("span", { class: "chip" }, pack || "sandbox"))));
    const skillsReply = await this.command("/skills", true);
    if (skillsReply && skillsReply.text) {
      body.appendChild(el("div", { class: "dsec" },
        el("h4", null, "Skills"),
        el("div", { class: "memory-text" }, skillsReply.text.slice(0, 2000))));
    }
    let extras = null;
    try { extras = await hub.call("chara.extras", { name: this.name }, 20000); } catch (e) { /* */ }
    const goals = (extras && extras.goals && (Array.isArray(extras.goals) ? extras.goals : extras.goals.goals)) || [];
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("p-goals")),
      ...(goals.length
        ? goals.slice(0, 12).map((g) => el("div", { class: "goal" }, el("i"), el("span", null,
            typeof g === "string" ? g : (g.text || g.title || JSON.stringify(g)).slice(0, 120))))
        : [el("div", { class: "placeholder-pane" }, t("d-empty-goals"))])));
  }

  async renderMemoryPage(body) {
    let extras = null;
    try { extras = await hub.call("chara.extras", { name: this.name }, 20000); } catch (e) { return; }
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-mem-own")),
      el("div", { class: "memory-text" }, extras.memory || t("d-empty-mem"))));
    body.appendChild(el("div", { class: "dsec" },
      el("h4", null, t("d-mem-user")),
      el("div", { class: "memory-text" }, extras.user_memory || t("d-empty-mem"))));
  }

  openModelPopover(ev) {
    closePopovers();
    const snap = this.snap || {};
    const pop = el("div", { class: "popover" });
    pop.appendChild(el("h4", null, t("p-model")));
    pop.appendChild(el("div", { class: "model-id" }, snap.model || "—"));
    pop.appendChild(el("div", { class: "dim-note" }, t("p-hotswap-wait")));
    pop.appendChild(el("h4", { style: "margin-top:12px" }, t("p-effort")));
    const seg = el("div", { class: "seg" });
    for (const lvl of ["off", "low", "medium", "high"]) {
      seg.appendChild(el("span", { class: (snap.reasoning || "medium") === lvl ? "on" : "", onclick: async (e2) => {
        await this.command(`/reasoning ${lvl}`, true);
        seg.querySelectorAll("span").forEach((s2) => s2.classList.toggle("on", s2 === e2.target));
      } }, t("eff-" + lvl)));
    }
    pop.appendChild(seg);
    if (!snap.reasoning_supported) pop.appendChild(el("div", { class: "dim-note" }, t("p-effort-ignored")));
    const rect = ev.currentTarget.getBoundingClientRect();
    pop.style.top = Math.min(rect.top, innerHeight - 240) + "px";
    pop.style.left = Math.max(8, rect.left - 296) + "px";
    document.body.appendChild(pop);
    setTimeout(() => document.addEventListener("click", function close(e2) {
      if (!pop.contains(e2.target)) { pop.remove(); document.removeEventListener("click", close); }
    }), 0);
  }

  /* ============================ WORKS PAGE ============================ */
  async pollWorks() {
    if (this.disposed) return;
    let works;
    try { works = await hub.call("works.list", { name: this.name }, 20000); } catch (e) { return; }
    if (this.disposed) return;
    this.works = works;
    const maxM = works.length ? Math.max(...works.map((w) => w.mtime)) : 0;
    if (this.page === "works") {
      this.renderWorksList();
      this.markWorksSeen();
    } else {
      $("works-unread").classList.toggle("show", maxM > this.worksSeen);
    }
  }

  markWorksSeen() {
    const maxM = this.works && this.works.length ? Math.max(...this.works.map((w) => w.mtime)) : 0;
    this.worksSeen = Math.max(this.worksSeen, maxM);
    try { localStorage.setItem(`lm-works-seen:${this.name}`, String(this.worksSeen)); } catch (e) { /* ok */ }
    $("works-unread").classList.remove("show");
  }

  worksGroup(kind) {
    if (kind === "image") return "img";
    if (kind === "text" || kind === "code") return "text";
    return "other";
  }

  async renderWorks() {
    if (!this.works) await this.pollWorks();
    this.renderWorksList();
  }

  renderWorksList() {
    const works = this.works || [];
    const chips = $("works-chips");
    const listEl = $("works-list");
    const counts = { all: works.length, img: 0, text: 0, other: 0 };
    for (const w of works) counts[this.worksGroup(w.kind)]++;
    chips.innerHTML = "";
    for (const [key, labelKey] of [["all", "works-all"], ["img", "works-img"], ["text", "works-text"], ["other", "works-other"]]) {
      chips.appendChild(el("button", {
        class: "fchip" + (this.worksFilter === key ? " on" : ""),
        onclick: () => { this.worksFilter = key; this.renderWorksList(); },
      }, `${t(labelKey)} ${counts[key]}`));
    }
    listEl.innerHTML = "";
    const filtered = this.worksFilter === "all" ? works : works.filter((w) => this.worksGroup(w.kind) === this.worksFilter);
    if (!filtered.length) {
      listEl.appendChild(el("div", { class: "works-empty" }, t("works-empty")));
    } else {
      listEl.appendChild(el("div", { class: "works-note" }, t("works-preview-wait")));
      let lastDay = "";
      const icons = { image: "▣", web: "❖", audio: "♪", text: "≣", code: "⌨", file: "▢" };
      for (const w of filtered) {
        const day = new Date(w.mtime * 1000).toLocaleDateString();
        if (day !== lastDay) {
          lastDay = day;
          const today = new Date().toLocaleDateString();
          const yest = new Date(Date.now() - 86400000).toLocaleDateString();
          listEl.appendChild(el("div", { class: "day-label" }, day === today ? t("today") : day === yest ? t("yesterday") : day));
        }
        listEl.appendChild(el("div", { class: "work-row", onclick: () => hub.call("works.open", { path: w.path }).catch((e) => toast(e.message, true)) },
          el("div", { class: "wicon" }, icons[w.kind] || "▢"),
          el("div", { class: "winfo" },
            el("b", null, w.name),
            el("span", { class: "wrel" }, w.rel || "")),
          el("div", { class: "wmeta" },
            el("span", null, fmtSize(w.size)),
            el("span", null, new Date(w.mtime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }))),
          el("button", { class: "reveal", title: "Finder", onclick: (ev) => {
            ev.stopPropagation();
            hub.call("works.open", { path: w.path, reveal: true }).catch((e) => toast(e.message, true));
          } }, "⌖")));
      }
    }
    listEl.appendChild(el("button", { class: "drawer-foot-link", onclick: () => {
      if (this.snap) hub.call("open.path", { path: this.snap.sandbox_root }).catch((e) => toast(e.message, true));
    } }, t("open-sandbox")));
  }

  /* ============================ TERMINAL PAGE（等待 PTY 后端的占位形态） ============================ */
  renderTerm() {
    const body = $("term-body");
    body.innerHTML = "";
    const path = (this.snap && this.snap.sandbox_root) ||
      ((state.hub && this.entry()) ? "" : "");
    const code = el("code", null, path || "…");
    const copyBtn = el("button", { class: "btn soft", onclick: async () => {
      try {
        await navigator.clipboard.writeText(code.textContent);
        toast(t("copied-path"));
      } catch (e) { /* clipboard denied */ }
    } }, t("copy"));
    body.appendChild(el("div", { class: "term-note" }, t("term-wait")));
    body.appendChild(el("div", { class: "term-path" }, code, copyBtn));
    body.appendChild(el("div", { class: "term-note" }, t("term-note")));
    this._termCode = code;
  }

  /* ---- permission requests, inline ---- */
  onPermission(p) {
    const box = el("div", { class: "sec", style: "max-width:430px;margin-left:40px" },
      el("h3", null, `🔐 ${p.kind}`),
      el("div", { class: "memory-text" }, p.reason || p.detail || ""),
      el("div", { class: "acts", style: "margin-top:10px" },
        el("button", { class: "btn soft", onclick: () => { this.client.permissionReply(p.id, false).catch(() => {}); box.remove(); } }, "✗"),
        el("div", { class: "grow" }),
        el("button", { class: "btn primary", onclick: () => { this.client.permissionReply(p.id, true).catch(() => {}); box.remove(); } }, "✓")));
    $("stream-inner").appendChild(box);
    this.scrollDown();
  }

  /* ---- composer & UI bindings ---- */
  setSending(streaming) {
    $("send-btn").textContent = streaming ? "■" : "↑";
    $("send-btn").className = streaming ? "stop" : "send";
  }

  bindUI() {
    const input = $("composer-input");
    input.value = "";
    input.oninput = () => {
      input.style.height = "auto";
      input.style.height = Math.min(input.scrollHeight, 130) + "px";
    };
    input.onkeydown = (ev) => {
      if (ev.key === "Enter" && !ev.shiftKey && !ev.isComposing) {
        ev.preventDefault();
        this.submit();
      }
    };
    $("send-btn").onclick = () => {
      if (this.client.streaming) this.client.interrupt().catch(() => {});
      else this.submit();
    };
    $("chat-back").onclick = () => navTo("#/");
    $("chat-avatar").onclick = () => {
      if (this.deckCard) openAvatarEditor(this.deckCard);
    };
    $("chat-tabs").onclick = (ev) => {
      const s2 = ev.target.closest("span[data-page]");
      if (s2) navTo(this.hashFor(s2.dataset.page));
    };
    $("panel-btn").onclick = () => {
      const collapsed = $("panel").classList.toggle("collapsed");
      $("panel-btn").classList.toggle("on", !collapsed);
      try { localStorage.setItem("lm-panel-open", collapsed ? "0" : "1"); } catch (e) { /* ok */ }
    };
    const panelOpen = localStorage.getItem("lm-panel-open") !== "0";
    $("panel").classList.toggle("collapsed", !panelOpen);
    $("panel-btn").classList.toggle("on", panelOpen);
    $("panel-page-back").onclick = () => this.closePanelPage();
    $("net-btn").onclick = () => this.command("/net on");
    document.addEventListener("visibilitychange", this._visHandler = () => {
      if (document.visibilityState === "visible") this.flushSuperReads();
    });
  }

  async submit() {
    const input = $("composer-input");
    const text = input.value.trim();
    if (!text || this.client.streaming) return;
    input.value = "";
    input.style.height = "auto";
    if (text.startsWith("/")) {
      const reply = await this.command(text);
      if (reply && reply.text) this.note(reply.text);
      return;
    }
    await this.sendUser(text);
  }
}
