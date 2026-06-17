/* The right-side chat panel — tabbed: status | skills | wishes(愿望) | memory |
 * gateway | settings. Ported from chat.js renderStatusPane / renderSkillsPage /
 * renderGoalsPage / renderMemoryPage / renderSettingsPane / renderGatewayPane.
 *
 * The STATUS tab is the priority (the live snapshot the owner reads at a glance);
 * skills/wishes/memory/settings are functional via command()/hub extras; the
 * GATEWAY tab (WeChat QR login + adapter config + enable/disable) lives in
 * GatewayPane. */

import { useEffect, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { GatewayPane } from "./GatewayPane";
import type { CharaStream, Snapshot } from "../../hooks/useCharaStream";

type PanelTab = "status" | "skills" | "goals" | "memory" | "gateway" | "settings";

const TABS: { key: PanelTab; label: string }[] = [
  { key: "status", label: "pg-status" },
  { key: "skills", label: "p-skills" },
  { key: "goals", label: "p-goals" },
  { key: "memory", label: "p-memory" },
  { key: "gateway", label: "p-gateway" },
  { key: "settings", label: "pg-settings" },
];

export function ChatPanel({ stream, name }: { stream: CharaStream; name: string }) {
  const t = useT();
  const [tab, setTab] = useState<PanelTab>("status");
  return (
    <aside className="panel">
      <div className="panel-tabs">
        {TABS.map((tb) => (
          <span key={tb.key} className={tab === tb.key ? "on" : ""} onClick={() => setTab(tb.key)}>
            {t(tb.label)}
          </span>
        ))}
      </div>
      <div className="panel-panes">
        <div className="panel-pane on">
          {tab === "status" && <StatusPane stream={stream} onTab={setTab} />}
          {tab === "skills" && <SkillsPane stream={stream} name={name} />}
          {tab === "goals" && <WishesPane name={name} />}
          {tab === "memory" && <MemoryPane name={name} />}
          {tab === "gateway" && <GatewayPane name={name} />}
          {tab === "settings" && <SettingsPane stream={stream} />}
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
  return (
    <div
      className={`prow${clickable ? " click" : ""}${cls ? " " + cls : ""}`}
      onClick={onClick}
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

function StatusPane({ stream, onTab }: { stream: CharaStream; onTab: (t: PanelTab) => void }) {
  const t = useT();
  const snap = stream.snapshot as Snapshot | null;
  if (!snap) return <div className="placeholder-pane">{t("st-connecting")}</div>;
  const num = (v: unknown) => Number(v) || 0;
  const ctxMax = num(snap.context_max);
  const ctxTok = num(snap.context_tokens);
  const pctCtx = ctxMax ? Math.round((100 * ctxTok) / ctxMax) : 0;
  const memMax = num(snap.memory_max);
  const memCh = num(snap.memory_chars);
  const pctMem = memMax ? Math.round((100 * memCh) / memMax) : 0;
  const netOn = !!snap.net_on;

  return (
    <div className="pgroup">
      <Prow label={t("p-model")} val={<code>{String(snap.model || "—")}</code>} chev />
      <Prow
        label={t("p-effort")}
        val={t("eff-" + (snap.reasoning ? String(snap.reasoning) : "medium")) + (snap.reasoning_supported ? "" : " ⌀")}
        chev
      />
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
        onClick={() => onTab("memory")}
      />
      <Prow
        label={t("p-net")}
        sub={t("p-net-sub")}
        switchOn={netOn}
        onSwitch={() => void stream.runCommand(netOn ? "/net off" : "/net on", true)}
      />
      <Prow
        label={t("p-gateway")}
        val={t("gw-stopped")}
        chev
        onClick={() => onTab("gateway")}
      />
    </div>
  );
}

function SkillsPane({ stream, name }: { stream: CharaStream; name: string }) {
  const t = useT();
  const { hub } = useHubApi();
  const [pack, setPack] = useState("");
  const [skills, setSkills] = useState<string | null>(null);
  useEffect(() => {
    let on = true;
    (async () => {
      try {
        const card = await hub.call<{ extensions?: { lunamoth?: { toolpack?: string } } }>(
          "card.read",
          { name },
          20000,
        );
        const p = card?.extensions?.lunamoth?.toolpack;
        if (on && p) setPack(String(p));
      } catch {
        /* fine */
      }
      const reply = await stream.runCommand("/skills", true);
      if (on) setSkills(reply || "");
    })();
    return () => {
      on = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [name]);
  return (
    <div>
      <div className="dsec">
        <h4>{t("p-toolpack")}</h4>
        <div className="tool-chips">
          <span className="chip">{pack || "sandbox"}</span>
        </div>
      </div>
      <div className="dsec">
        <h4>Skills</h4>
        {skills ? (
          <div className="memory-text">{skills.slice(0, 2000)}</div>
        ) : (
          <div className="placeholder-pane">{t("d-empty-skills")}</div>
        )}
      </div>
    </div>
  );
}

interface Extras {
  goals?: unknown;
  memory?: string;
  user_memory?: string;
}

function WishesPane({ name }: { name: string }) {
  const t = useT();
  const { hub } = useHubApi();
  const [goals, setGoals] = useState<{ text: string; status: string }[] | null>(null);
  useEffect(() => {
    let on = true;
    (async () => {
      try {
        const ex = await hub.call<Extras>("chara.extras", { name }, 20000);
        const raw = ex?.goals;
        const list = Array.isArray(raw)
          ? raw
          : raw && typeof raw === "object" && Array.isArray((raw as { goals?: unknown }).goals)
            ? (raw as { goals: unknown[] }).goals
            : [];
        const norm = list.map((g) => {
          if (typeof g === "string") return { text: g, status: "active" };
          const o = g as { text?: string; title?: string; status?: string };
          return { text: o.text || o.title || JSON.stringify(g), status: o.status || "active" };
        });
        if (on) setGoals(norm);
      } catch {
        if (on) setGoals([]);
      }
    })();
    return () => {
      on = false;
    };
  }, [hub, name]);
  if (goals === null) return <div className="placeholder-pane">…</div>;
  if (!goals.length) return <div className="placeholder-pane">{t("d-empty-goals")}</div>;
  const rank: Record<string, number> = { active: 0, done: 1, dropped: 2 };
  const ordered = [...goals].sort((a, b) => (rank[a.status] ?? 0) - (rank[b.status] ?? 0));
  return (
    <div className="dsec">
      {ordered.slice(0, 30).map((g, i) => (
        <div key={i} className={`goal goal-${g.status}`}>
          <i />
          <span>{g.text.slice(0, 200)}</span>
          {g.status !== "active" && <span className={`goal-badge ${g.status}`}>{t("goal-" + g.status)}</span>}
        </div>
      ))}
    </div>
  );
}

function MemoryPane({ name }: { name: string }) {
  const t = useT();
  const { hub } = useHubApi();
  const [ex, setEx] = useState<Extras | null>(null);
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
  if (!ex) return <div className="placeholder-pane">…</div>;
  return (
    <div>
      <div className="dsec">
        <h4>{t("d-mem-own")}</h4>
        <div className="memory-text">{ex.memory || t("d-empty-mem")}</div>
      </div>
      <div className="dsec">
        <h4>{t("d-mem-user")}</h4>
        <div className="memory-text">{ex.user_memory || t("d-empty-mem")}</div>
      </div>
    </div>
  );
}

function SettingsPane({ stream }: { stream: CharaStream }) {
  const t = useT();
  const snap = (stream.snapshot as Snapshot | null) || {};
  const quiet = Number(snap.quiet) || 300;
  const patience = Number(snap.patience) || 600;
  const emb = snap.embodiment === "actor" ? "actor" : "literal";
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
        <label>{t("p-embodiment")}</label>
        <div className="why">{t("emb-" + emb)}</div>
        <div className="ctl">
          <span className="fact">{emb}</span>
          <span className="fact-hint">{t("emb-fact-hint")}</span>
        </div>
      </div>
      <div className="pgroup" style={{ marginTop: 22 }}>
        <div
          className="prow danger click"
          onClick={() => {
            if (confirm(t("reset-confirm"))) void stream.runCommand("/reset");
          }}
        >
          <div className="pmain">
            <span className="plbl">{t("p-reset")}</span>
          </div>
        </div>
      </div>
    </div>
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
            setBusy(true);
            await onSave(v.trim());
            setBusy(false);
          }}
        >
          {t("save")}
        </button>
      </div>
    </div>
  );
}
