/* The right-side chat panel — tabbed: status | profile | settings. Ported from
 * chat.js renderStatusPane / renderSettingsPane; the PROFILE tab consolidates what
 * were three separate tabs — 愿望(wishes) · 技能(skills) · 记忆(memory) — into one
 * scrollable pane (in that order). Visual editing moved to the deck card editor
 * (it can now edit a living chara's locked card); gateway config moved to the
 * Gateways overview page. */

import { useEffect, useState } from "react";
import { useT, type TKey } from "../../i18n";
import { useHubApi, useHubState } from "../../state/hub";
import { useNavigate } from "../../hooks/useHashRoute";
import type { BoardSession } from "../../state/hub";
import { deckToast } from "../ui/deckToast";
import { rpcErrText } from "../../lib/status";
import { providerOf } from "../../lib/format";
import { Select, type SelectOption } from "../settings/Select";
import { Segmented } from "../ui/Segmented";
import { readVisualPrefs, writeVisualPrefs, type VisualPrefs, type SpritePos } from "../../lib/visual";
import type { ModelInfo } from "../deck/types";
import type { CharaStream, Snapshot } from "../../hooks/useCharaStream";
import { TasksSection, type TaskItem } from "./TasksSection";
import { CollapsibleSection } from "./CollapsibleSection";

const REASONING = ["off", "low", "medium", "high"] as const;

type PanelTab = "status" | "profile" | "settings";

const TABS: { key: PanelTab; label: string }[] = [
  { key: "status", label: "pg-status" },
  { key: "profile", label: "p-profile" },
  { key: "settings", label: "pg-settings" },
];

export function ChatPanel({
  stream,
  name,
  onClose,
}: {
  stream: CharaStream;
  name: string;
  onClose?: () => void;
}) {
  const t = useT();
  const [tab, setTab] = useState<PanelTab>("status");
  return (
    <aside className="panel">
      {/* Mobile-only header: on a phone the panel is a full-screen page, so it needs its
          own back button (the chat header underneath is covered). Hidden on desktop. */}
      <div className="panel-mhead">
        <button className="back" onClick={onClose} aria-label="Back">
          ‹
        </button>
        <b>{name}</b>
      </div>
      <div className="panel-tabs" role="tablist">
        {TABS.map((tb) => (
          <button
            type="button"
            role="tab"
            aria-selected={tab === tb.key}
            key={tb.key}
            className={tab === tb.key ? "on" : ""}
            onClick={() => setTab(tb.key)}
          >
            {t(tb.label)}
          </button>
        ))}
      </div>
      <div className="panel-panes">
        <div className="panel-pane on">
          {tab === "status" && <StatusPane stream={stream} name={name} onTab={setTab} />}
          {tab === "profile" && <ProfilePane stream={stream} name={name} />}
          {tab === "settings" && <SettingsPane stream={stream} name={name} />}
        </div>
      </div>
    </aside>
  );
}

/* ---- a compact panel row (chat.js prow) ---- */
function Prow({
  label,
  sub,
  val,
  bar,
  switchOn,
  onSwitch,
  onClick,
  chev,
  dot,
  cls,
}: {
  label: string;
  sub?: string;
  val?: React.ReactNode;
  bar?: number;
  switchOn?: boolean;
  onSwitch?: () => void;
  onClick?: () => void;
  chev?: boolean;
  dot?: string;
  cls?: string;
}) {
  const clickable = !!(onClick || onSwitch);
  // Keyboard/AT parity for the clickable rows: role=button + Enter/Space activate
  // (a switch-only row toggles its switch — same as the pointer affordance).
  const activate = onClick || onSwitch;
  return (
    <div
      className={`prow${clickable ? " click" : ""}${cls ? " " + cls : ""}`}
      onClick={onClick}
      role={clickable ? "button" : undefined}
      tabIndex={clickable ? 0 : undefined}
      onKeyDown={
        clickable
          ? (e) => {
              if (e.target !== e.currentTarget) return; // inner switch handles its own keys
              if (e.key === "Enter" || e.key === " ") {
                e.preventDefault();
                activate?.();
              }
            }
          : undefined
      }
    >
      <div className="pmain">
        <span className="plbl">{label}</span>
        {sub && <span className="psub">{sub}</span>}
        {bar !== undefined && (
          <div className="pbar">
            <i className={bar > 85 ? "hot" : ""} style={{ width: `${Math.max(0, Math.min(100, bar))}%` }} />
          </div>
        )}
      </div>
      {dot && <span className={`pdot ${dot}`} />}
      {val !== undefined && <span className="pval">{val}</span>}
      {onSwitch && (
        <button
          className={`switch${switchOn ? " on" : ""}`}
          onClick={(e) => {
            e.stopPropagation();
            onSwitch();
          }}
        />
      )}
      {chev && <span className="chev">›</span>}
    </div>
  );
}

function StatusPane({ stream, name, onTab }: { stream: CharaStream; name: string; onTab: (t: PanelTab) => void }) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const { snapshot: hubSnap } = useHubState();
  const snap = stream.snapshot as Snapshot | null;
  // Autonomy (自主运行) — the SAME on/off switch as the board, reading the SAME
  // variable: the roster entry's `paused` (push-refreshed on life.state). Sourcing
  // both views from one place is why inner and outer can never disagree. on = mode
  // live (autonomous); off = mode chat (replies only); it never kills the chat.
  // Optimistic: flip at once, reconcile when the roster agrees, revert on failure.
  // The hub RPC is keyed by SESSION name (the route `name`), never the card's
  // display name (stream.charName) — those differ and would target the wrong session.
  const entry = (hubSnap?.sessions as BoardSession[] | undefined)?.find((s) => s.name === name);
  const rosterLive = entry ? !entry.paused : String(snap?.mode || "live") === "live";
  const [livePending, setLivePending] = useState<boolean | null>(null);
  useEffect(() => {
    if (livePending !== null && rosterLive === livePending) setLivePending(null);
  }, [rosterLive, livePending]);
  if (!snap) return <div className="placeholder-pane">{t("st-connecting")}</div>;
  const num = (v: unknown) => Number(v) || 0;
  const ctxMax = num(snap.context_max);
  const ctxTok = num(snap.context_tokens);
  const pctCtx = ctxMax ? Math.round((100 * ctxTok) / ctxMax) : 0;
  const memMax = num(snap.memory_max);
  const memCh = num(snap.memory_chars);
  const pctMem = memMax ? Math.round((100 * memCh) / memMax) : 0;
  const liveOn = livePending ?? rosterLive;
  const toggleLive = () => {
    if (livePending !== null) return; // a flip is already in flight (matches Board's guard)
    const next = !liveOn;
    setLivePending(next); // optimistic flip
    hub
      .call("chara.set_autonomy", { name, on: next }, 30000)
      .then(() => refresh()) // pull the roster so the board + header dot agree at once
      .catch((e) => {
        setLivePending(null); // failed → revert to the real state
        deckToast(rpcErrText(t, e as { message?: string }), true);
      });
  };

  return (
    <div className="pgroup">
      <Prow label={t("p-autonomy")} sub={t("p-autonomy-sub")} switchOn={liveOn} onSwitch={toggleLive} />
      <ModelEffort stream={stream} snap={snap} />
      <div className="ctx-sec">
        <div className="ctx-sec-label">{t("p-context")}</div>
        <div className="ctx-big">
          <div className={`ctx-ring${pctCtx >= 75 ? " hot" : ""}`} style={{ ["--p" as string]: String(pctCtx) }} />
          <div className="ctx-nums">
            <b>{pctCtx}%</b>
            <div>
              {(ctxTok / 1000).toFixed(1)}k / {(ctxMax / 1000).toFixed(0)}k tokens
            </div>
          </div>
        </div>
      </div>
      <Prow
        label={t("p-memory")}
        bar={pctMem}
        val={`${memCh} / ${memMax}`}
        chev
        onClick={() => onTab("profile")}
      />
    </div>
  );
}

/* Per-chara model + reasoning — the same controls as Settings · 模型, scoped to THIS
 * chara through the live /model + /reasoning commands. /model is a session hot-swap
 * (a restart returns to the configured default — the scope note says so); /reasoning
 * persists. Reasoning is greyed when the route ignores it (snapshot.reasoning_supported).
 * Both are optimistic: reflect the choice at once, reconcile when the snapshot agrees,
 * revert + toast on failure. */
interface KeyRow { label: string; provider: string; base_url: string; has_key: boolean; active: boolean }

function ModelEffort({ stream, snap }: { stream: CharaStream; snap: Snapshot }) {
  const t = useT();
  const { hub } = useHubApi();
  const [models, setModels] = useState<ModelInfo[]>([]);
  const [keys, setKeys] = useState<KeyRow[]>([]);
  useEffect(() => {
    let on = true;
    hub.call<{ models?: ModelInfo[] }>("models.list", {}, 30000).then((r) => on && setModels(Array.isArray(r?.models) ? r.models : [])).catch(() => {});
    hub.call<KeyRow[]>("keys.list", {}, 15000).then((k) => on && setKeys(Array.isArray(k) ? k : [])).catch(() => {});
    return () => {
      on = false;
    };
  }, [hub]);

  // Provider — which SAVED key this chara is on, matched by its endpoint
  // (base_url, then provider name). Switching it runs /provider, which swaps the
  // chara's provider live and persists it (the key itself stays in the keyring).
  const norm = (u: string) => u.trim().replace(/\/+$/, "").toLowerCase();
  const liveBase = norm(String(snap.base_url || ""));
  const liveProv = String(snap.provider || "").toLowerCase();
  const activeKey =
    keys.find((k) => k.base_url && norm(k.base_url) === liveBase) ||
    keys.find((k) => (k.provider || "").toLowerCase() === liveProv);
  const activeLabel = activeKey?.label || "";
  const [provPending, setProvPending] = useState<string | null>(null);
  useEffect(() => {
    if (provPending !== null && activeLabel === provPending) setProvPending(null);
  }, [activeLabel, provPending]);
  const providerVal = provPending ?? activeLabel;
  const providerOptions: SelectOption[] = keys
    .filter((k) => k.has_key)
    .map((k) => ({ value: k.label, label: k.label, note: k.provider || undefined }));
  const swapProvider = (label: string) => {
    if (!label || label === activeLabel) return;
    setProvPending(label); // optimistic
    void stream.runCommand(`/provider ${label}`).then((r) => {
      if (r === null) {
        setProvPending(null);
        deckToast(t("save-failed"), true);
      }
    });
  };

  const liveModel = String(snap.model || "");
  const [modelPending, setModelPending] = useState<string | null>(null);
  useEffect(() => {
    if (modelPending !== null && liveModel === modelPending) setModelPending(null);
  }, [liveModel, modelPending]);
  const model = modelPending ?? liveModel;

  const liveReason = snap.reasoning ? String(snap.reasoning) : "medium";
  const [reasonPending, setReasonPending] = useState<string | null>(null);
  useEffect(() => {
    if (reasonPending !== null && liveReason === reasonPending) setReasonPending(null);
  }, [liveReason, reasonPending]);
  const reason = reasonPending ?? liveReason;
  const reasonSupported = snap.reasoning_supported !== false;

  const modelOptions: SelectOption[] = models
    .map((m) => m.id)
    .sort((a, b) => a.localeCompare(b))
    .map((id) => ({ value: id, label: id, group: providerOf(id) }));

  const swapModel = (id: string) => {
    const v = id.trim();
    if (!v || v === liveModel) return;
    setModelPending(v); // optimistic
    void stream.runCommand(`/model ${v}`).then((r) => {
      if (r === null) {
        setModelPending(null);
        deckToast(t("save-failed"), true);
      }
    });
  };
  const setReason = (r: string) => {
    if (r === liveReason) return;
    setReasonPending(r); // optimistic
    void stream.runCommand(`/reasoning ${r}`).then((res) => {
      if (res === null) {
        setReasonPending(null);
        deckToast(t("save-failed"), true);
      }
    });
  };

  return (
    <div className="model-boxes" style={{ margin: "4px 0 2px" }}>
      {providerOptions.length > 0 && (
        <label className="model-box">
          <span className="mb-lbl">{t("provider")}</span>
          <Select
            value={providerVal}
            options={providerOptions}
            onChange={swapProvider}
            placeholder={String(snap.provider || t("provider"))}
          />
        </label>
      )}
      <label className="model-box">
        <span className="mb-lbl">{t("p-model")}</span>
        <Select value={model} options={modelOptions} onChange={swapModel} placeholder={t("p-model")} search allowCustom />
      </label>
      <div className="model-box">
        <span className="mb-lbl" id="p-effort-lbl">{t("p-effort")}</span>
        <Segmented
          ariaLabelledby="p-effort-lbl"
          value={reason}
          options={REASONING.map((r) => ({ value: r, label: t(("eff-" + r) as TKey) }))}
          onChange={(r) => setReason(r)}
        />
        {!reasonSupported && <span className="reason-hint">{t("p-effort-ignored")}</span>}
      </div>
      <div className="av-note">{t("p-model-scope-note")}</div>
    </div>
  );
}

interface Extras {
  polaris?: string;
  memory?: string;
  user_memory?: string;
  tasks?: { active: TaskItem[]; done: TaskItem[] };
}

/* The PROFILE tab — 愿望 · 技能 · 记忆 in one scrollable pane. The three were once
 * three tabs; consolidating them also collapses the two duplicate chara.extras
 * fetches (wishes + memory) into one. Skills still come from the live agent via
 * /skills. Order is deliberate: wishes first (what it's reaching for), then skills
 * (what it can do), then memory (what it's holding). */
function ProfilePane({ stream, name }: { stream: CharaStream; name: string }) {
  const t = useT();
  const { hub } = useHubApi();
  const [ex, setEx] = useState<Extras | null>(null);
  const [skills, setSkills] = useState<string | null>(null);
  // Polaris + tasks are DISPLAY-ONLY here — the chara owns its tasks (set via the
  // `task` tool); the aspiration is changed only with the `/aspiration` command.

  useEffect(() => {
    let on = true;
    hub
      .call<Extras>("chara.extras", { name }, 20000)
      .then((r) => on && setEx(r))
      .catch(() => on && setEx({}));
    return () => {
      on = false;
    };
  }, [hub, name]);

  useEffect(() => {
    let on = true;
    (async () => {
      const reply = await stream.runCommand("/skills", true);
      if (on) setSkills(reply || "");
    })();
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);

  return (
    <div className="profile-pane">
      <section className="dsec">
        <h4>{t("p-polaris")}</h4>
        {ex === null ? (
          <div className="placeholder-pane">…</div>
        ) : ex.polaris ? (
          <div className="memory-text">{ex.polaris}</div>
        ) : (
          <div className="placeholder-pane">{t("polaris-empty")}</div>
        )}
      </section>

      <CollapsibleSection title={t("p-tasks")}>
        <TasksSection tasks={ex?.tasks} loading={ex === null} />
      </CollapsibleSection>

      <CollapsibleSection title={t("p-skills")}>
        {skills === null ? (
          <div className="placeholder-pane">…</div>
        ) : skills ? (
          <div className="memory-text">{skills.slice(0, 2000)}</div>
        ) : (
          <div className="placeholder-pane">{t("d-empty-skills")}</div>
        )}
      </CollapsibleSection>

      <CollapsibleSection title={t("p-memory")} defaultOpen={false}>
        {ex === null ? (
          <div className="placeholder-pane">…</div>
        ) : (
          <>
            <div className="av-note">{t("d-mem-own")}</div>
            <div className="memory-text">{ex.memory || t("d-empty-mem")}</div>
            <div className="av-note" style={{ marginTop: 10 }}>{t("d-mem-user")}</div>
            <div className="memory-text">{ex.user_memory || t("d-empty-mem")}</div>
          </>
        )}
      </CollapsibleSection>
    </div>
  );
}

function SettingsPane({ stream, name }: { stream: CharaStream; name: string }) {
  const t = useT();
  const { hub, refresh } = useHubApi();
  const nav = useNavigate();
  // Distribution lock (server LUNAMOTH_FORCE_SANDBOX): the board state reports it; the
  // sandbox toggle below is then shown ON, greyed, and non-interactive.
  const { snapshot: hubSnap } = useHubState();
  const forceSandbox = !!(hubSnap as { force_sandbox?: boolean } | null)?.force_sandbox;
  const snap = (stream.snapshot as Snapshot | null) || {};
  const quiet = Number(snap.quiet) || 300;
  const patience = Number(snap.patience) || 3600;
  const [restarting, setRestarting] = useState(false);
  const [deleting, setDeleting] = useState(false);

  // 网络 — instant (the /net command hits the live agent immediately).
  const snapNet = !!snap.net_on;
  const [netPending, setNetPending] = useState<boolean | null>(null);
  useEffect(() => {
    if (netPending !== null && snapNet === netPending) setNetPending(null);
  }, [snapNet, netPending]);
  const netOn = netPending ?? snapNet;
  const toggleNet = () => {
    const next = !netOn;
    setNetPending(next);
    void stream.runCommand(next ? "/net on" : "/net off").then((r) => {
      if (r === null) setNetPending(null);
    });
  };

  // 网站 + 强化角色扮演 — prompt MODULES: the change is written now but applies on
  // the NEXT start (a module rides the cache-stable prefix). The toggle reflects
  // the DESIRED state (sticky once touched); a hint shows while it differs from
  // what's live. set_modules is a hub RPC (name-keyed), not a chat command.
  const activeSite = !!snap.website;
  const activeRp = snap.embodiment === "actor";
  const [siteWant, setSiteWant] = useState<boolean | null>(null);
  const [rpWant, setRpWant] = useState<boolean | null>(null);
  // Reconcile (the netPending pattern above): sticky is only for the WAITING gap —
  // once the authoritative snapshot agrees (the chara restarted with the change
  // applied, or it changed in place, e.g. from another window), the desired value
  // yields to the live one; and switching charas must never carry a stale toggle.
  useEffect(() => {
    if (siteWant !== null && activeSite === siteWant) setSiteWant(null);
  }, [activeSite, siteWant]);
  useEffect(() => {
    if (rpWant !== null && activeRp === rpWant) setRpWant(null);
  }, [activeRp, rpWant]);
  useEffect(() => {
    setSiteWant(null);
    setRpWant(null);
  }, [name]);
  const siteOn = siteWant ?? activeSite;
  const rpOn = rpWant ?? activeRp;
  const setModule = (mod: "website" | "force_roleplay", next: boolean) => {
    const setLocal = mod === "website" ? setSiteWant : setRpWant;
    setLocal(next); // optimistic, sticky (it's the pending next-start value)
    hub.call("session.set_modules", { name, [mod]: next }, 30000).catch((e) => {
      setLocal(null); // revert to the live value
      deckToast(rpcErrText(t, e as { message?: string }), true);
    });
  };

  // 沙盒 — OS isolation: sandbox (jailed, default) ⇄ admin (full machine). The jail
  // backend is pinned at the chara's process launch, so a change applies on the NEXT
  // start (never hot-swapped, like the modules). snap.isolation is the LIVE value;
  // turning the sandbox OFF (→ admin) grants full read/write of this computer, so it
  // is confirm-gated. Re-enabling it (→ sandbox) tightens, no confirm.
  const activeSandbox = String(snap.isolation || "sandbox") !== "admin";
  const [isoWant, setIsoWant] = useState<boolean | null>(null);
  // Same reconcile as the modules above: live value catches up → drop the sticky
  // desire; chara switch → never show the previous chara's pending toggle.
  useEffect(() => {
    if (isoWant !== null && activeSandbox === isoWant) setIsoWant(null);
  }, [activeSandbox, isoWant]);
  useEffect(() => {
    setIsoWant(null);
  }, [name]);
  const sandboxOn = isoWant ?? activeSandbox;
  const setSandbox = (next: boolean) => {
    if (!next && !confirm(t("iso-admin-confirm"))) return;
    setIsoWant(next); // optimistic, sticky (the pending next-start value)
    hub.call("chara.set_isolation", { name, isolation: next ? "sandbox" : "admin" }, 30000).catch((e) => {
      setIsoWant(null); // revert to the live value
      deckToast(rpcErrText(t, e as { message?: string }), true);
    });
  };

  // Restart — bounce the chara's process: unblocks a stuck one (e.g. wedged in a tool)
  // AND applies any pending card edits (the fresh process re-reads the frozen card;
  // history is restored). Harmless, no confirm. The same mechanism as 立即应用.
  const doRestart = async () => {
    if (restarting) return;
    setRestarting(true);
    try {
      await hub.call("chara.restart", { name }, 60000);
      deckToast(t("restart-done"));
    } catch (e) {
      deckToast(rpcErrText(t, e as { message?: string }), true);
    } finally {
      setRestarting(false);
    }
  };

  // Delete — soft: the chara leaves awakened (off the board + its locked card off the
  // deck) and returns to not-awakened (the deck template stays, re-wakeable). Data is
  // moved to ~/.trash (recoverable), not erased. Confirm-gated; on success leave the
  // now-dead chat for the board.
  const doDelete = async () => {
    if (deleting || !confirm(t("delete-confirm", { name }))) return;
    setDeleting(true);
    try {
      await hub.call("session.delete", { name, confirm: name }, 60000);
      await refresh();
      deckToast(t("delete-done", { name }));
      nav("#/");
    } catch (e) {
      setDeleting(false);
      deckToast(rpcErrText(t, e as { message?: string }), true);
    }
  };
  return (
    <div>
      <NumField labelKey="p-quiet" whyKey="p-quiet-sub" value={quiet} onSave={(v) => stream.runCommand(`/quiet ${v}`)} />
      <NumField
        labelKey="p-patience"
        whyKey="p-patience-sub"
        value={patience}
        onSave={(v) => stream.runCommand(`/patience ${v}`)}
      />
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("mod-website")}</label>
        <div className="why">{t("mod-website-hint")}</div>
        <div className="ctl">
          <button className={"switch" + (siteOn ? " on" : "")} onClick={() => setModule("website", !siteOn)} />
          {siteOn !== activeSite && <span className="fact-hint">{t("mod-next-start")}</span>}
        </div>
      </div>
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("p-net")}</label>
        <div className="why">{t("p-net-sub")}</div>
        <div className="ctl">
          <button className={"switch" + (netOn ? " on" : "")} onClick={toggleNet} />
        </div>
      </div>
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("p-sandbox")}</label>
        <div className="why">{t("p-sandbox-sub")}</div>
        <div className="ctl">
          <button
            className={"switch" + (forceSandbox || sandboxOn ? " on" : "") + (forceSandbox ? " locked" : "")}
            disabled={forceSandbox}
            aria-disabled={forceSandbox}
            onClick={() => !forceSandbox && setSandbox(!sandboxOn)}
          />
          {forceSandbox
            ? <span className="fact-hint">{t("p-sandbox-locked")}</span>
            : sandboxOn !== activeSandbox && <span className="fact-hint">{t("mod-next-start")}</span>}
        </div>
      </div>
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("mod-roleplay")}</label>
        <div className="why">{t("mod-roleplay-hint")}</div>
        <div className="ctl">
          <button className={"switch" + (rpOn ? " on" : "")} onClick={() => setModule("force_roleplay", !rpOn)} />
          {rpOn !== activeRp && <span className="fact-hint">{t("mod-next-start")}</span>}
        </div>
      </div>
      <VisualPrefsFields charaName={stream.charName} />
      <div className="pgroup" style={{ marginTop: 22 }}>
        <div
          className={"prow click" + (restarting ? " busy" : "")}
          role="button"
          tabIndex={0}
          onClick={() => void doRestart()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              void doRestart();
            }
          }}
        >
          <div className="pmain">
            <span className="plbl">{restarting ? t("restarting") : t("p-restart")}</span>
            <span className="psub">{t("p-restart-sub")}</span>
          </div>
          {restarting && <span className="spin" />}
        </div>
        <div
          className={"prow danger click" + (deleting ? " busy" : "")}
          role="button"
          tabIndex={0}
          onClick={() => void doDelete()}
          onKeyDown={(e) => {
            if (e.key === "Enter" || e.key === " ") {
              e.preventDefault();
              void doDelete();
            }
          }}
        >
          <div className="pmain">
            <span className="plbl">{deleting ? t("deleting") : t("p-delete")}</span>
            <span className="psub">{t("p-delete-sub")}</span>
          </div>
          {deleting && <span className="spin" />}
        </div>
      </div>
    </div>
  );
}

/* 聊天背景 / 立绘 presentation prefs — LOCAL display preferences (this device
 * only), stored per-chara in localStorage and applied live by the open chat
 * (writeVisualPrefs fires VISUAL_PREFS_EVENT; ChatStreamPage re-reads). The
 * render path always existed — this is the missing write side. Keyed by
 * stream.charName: the SAME key ChatStreamPage reads with. Instant apply, no
 * API round-trip (pure local state — the binding rule's easiest case). */
const SPRITE_CHOICES: readonly SpritePos[] = ["off", "left", "center", "right"];

function VisualPrefsFields({ charaName }: { charaName: string }) {
  const t = useT();
  const [vp, setVp] = useState<VisualPrefs>(() => readVisualPrefs(charaName));
  useEffect(() => setVp(readVisualPrefs(charaName)), [charaName]);
  const patch = (p: Partial<VisualPrefs>) => {
    setVp((prev) => ({ ...prev, ...p })); // instant
    writeVisualPrefs(charaName, p); // persist + notify the open chat
  };
  return (
    <>
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("p-backdrop")}</label>
        <div className="why">{t("p-backdrop-sub")}</div>
        <div className="ctl">
          <button
            className={"switch" + (vp.bgOn ? " on" : "")}
            aria-label={t("p-backdrop")}
            aria-pressed={vp.bgOn}
            onClick={() => patch({ bgOn: !vp.bgOn })}
          />
        </div>
      </div>
      {vp.bgOn && (
        <div className="pfield">
          <label>{t("p-veil")}</label>
          <div className="why">{t("p-veil-sub")}</div>
          <div className="ctl">
            <input
              type="range"
              min={0}
              max={100}
              value={vp.veilOpacity}
              aria-label={t("p-veil")}
              onChange={(e) => patch({ veilOpacity: Number(e.target.value) })}
            />
            <span className="pref-val">{vp.veilOpacity}%</span>
          </div>
        </div>
      )}
      <div className="pfield" style={{ marginTop: 16 }}>
        <label>{t("p-sprite-pos")}</label>
        <div className="ctl">
          <Segmented
            ariaLabel={t("p-sprite-pos")}
            value={vp.spritePos}
            options={SPRITE_CHOICES.map((p) => ({ value: p, label: t(("sprite-" + p) as TKey) }))}
            onChange={(p) => patch({ spritePos: p })}
          />
        </div>
      </div>
      {vp.spritePos !== "off" && (
        <div className="pfield">
          <label>{t("p-sprite-opacity")}</label>
          <div className="ctl">
            <input
              type="range"
              min={0}
              max={100}
              value={vp.spriteOpacity}
              aria-label={t("p-sprite-opacity")}
              onChange={(e) => patch({ spriteOpacity: Number(e.target.value) })}
            />
            <span className="pref-val">{vp.spriteOpacity}%</span>
          </div>
        </div>
      )}
    </>
  );
}

function NumField({
  labelKey,
  whyKey,
  value,
  onSave,
}: {
  labelKey: string;
  whyKey: string;
  value: number;
  onSave: (v: string) => Promise<unknown> | void;
}) {
  const t = useT();
  const [v, setV] = useState(String(Math.round(value)));
  const [busy, setBusy] = useState(false);
  useEffect(() => setV(String(Math.round(value))), [value]);
  return (
    <div className="pfield">
      <label>{t(labelKey)}</label>
      <div className="why">{t(whyKey)}</div>
      <div className="ctl">
        <input type="number" value={v} onChange={(e) => setV(e.target.value)} />
        <button
          className="btn soft"
          disabled={busy}
          onClick={async () => {
            // Validate client-side (it's a number field) so the only remaining
            // failure is a thrown call → runCommand returns null. Then: confirm at
            // the control on success, revert + surface on failure (no silent save).
            const n = Number(v.trim());
            if (!Number.isFinite(n) || n < 0) {
              deckToast(t("save-failed"), true);
              return;
            }
            setBusy(true);
            const r = await onSave(String(Math.round(n)));
            setBusy(false);
            if (r === null) {
              setV(String(Math.round(value)));
              deckToast(t("save-failed"), true);
            } else {
              deckToast(t("saved"));
            }
          }}
        >
          {t("save")}
        </button>
      </div>
    </div>
  );
}
