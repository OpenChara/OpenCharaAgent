/* The works sub-page — the chara's artifact list (works.list) with a kind filter
 * and an in-app preview (works.read). Ported from chat.js pollWorks/renderWorksList/
 * openWorkPreview. Reveal-in-Finder + open-in-system go through works.open. */

import { useEffect, useMemo, useState } from "react";
import { useT } from "../../i18n";
import { useHubApi } from "../../state/hub";
import { fmtSize } from "../../lib/format";

interface Work {
  name: string;
  rel: string;
  path: string;
  kind: string;
  size: number;
  mtime: number;
}

type Filter = "all" | "img" | "text" | "other";

function group(kind: string): Exclude<Filter, "all"> {
  if (kind === "image") return "img";
  if (kind === "text" || kind === "code") return "text";
  return "other";
}

const ICONS: Record<string, string> = { image: "▣", web: "❖", audio: "♪", text: "≣", code: "⌨", file: "▢" };

export function ChatWorks({ name, sandboxRoot }: { name: string; sandboxRoot?: string }) {
  const t = useT();
  const { hub } = useHubApi();
  const [works, setWorks] = useState<Work[]>([]);
  const [filter, setFilter] = useState<Filter>("all");
  const [preview, setPreview] = useState<{ work: Work; body: PreviewBody } | null>(null);

  useEffect(() => {
    let on = true;
    const load = async () => {
      try {
        const r = await hub.call<Work[]>("works.list", { name }, 20000);
        if (on) setWorks(r || []);
      } catch {
        /* keep */
      }
    };
    void load();
    const timer = setInterval(() => {
      if (!document.hidden) void load();
    }, 45000);
    return () => {
      on = false;
      clearInterval(timer);
    };
  }, [hub, name]);

  const counts = useMemo(() => {
    const c = { all: works.length, img: 0, text: 0, other: 0 };
    for (const w of works) c[group(w.kind)]++;
    return c;
  }, [works]);

  const filtered = filter === "all" ? works : works.filter((w) => group(w.kind) === filter);

  const openPreview = async (w: Work) => {
    try {
      const r = await hub.call<PreviewRead>("works.read", { name, rel: w.rel }, 30000);
      setPreview({ work: w, body: toBody(r, t) });
    } catch {
      /* toast skipped in MVP */
    }
  };

  return (
    <div className="chat-page on" id="page-works">
      <div className="works-head" id="works-chips">
        {(
          [
            ["all", "works-all"],
            ["img", "works-img"],
            ["text", "works-text"],
            ["other", "works-other"],
          ] as [Filter, string][]
        ).map(([key, label]) => (
          <button
            key={key}
            className={`fchip${filter === key ? " on" : ""}`}
            onClick={() => setFilter(key)}
          >
            {t(label)} {counts[key]}
          </button>
        ))}
      </div>
      <div className="works-body" id="works-list">
        {filtered.length === 0 ? (
          <div className="works-empty">{t("works-empty")}</div>
        ) : (
          <WorksList works={filtered} t={t} hub={hub} onOpen={openPreview} />
        )}
        <button
          className="drawer-foot-link"
          onClick={() => {
            if (sandboxRoot) hub.call("open.path", { path: sandboxRoot }).catch(() => {});
          }}
        >
          {t("open-sandbox")}
        </button>
      </div>
      {preview && <WorkPreview preview={preview} hub={hub} onClose={() => setPreview(null)} />}
    </div>
  );
}

function WorksList({
  works,
  t,
  hub,
  onOpen,
}: {
  works: Work[];
  t: ReturnType<typeof useT>;
  hub: ReturnType<typeof useHubApi>["hub"];
  onOpen: (w: Work) => void;
}) {
  const rows: React.ReactNode[] = [];
  let lastDay = "";
  const today = new Date().toLocaleDateString();
  const yest = new Date(Date.now() - 86400000).toLocaleDateString();
  for (const w of works) {
    const day = new Date(w.mtime * 1000).toLocaleDateString();
    if (day !== lastDay) {
      lastDay = day;
      rows.push(
        <div key={`d-${day}`} className="day-label">
          {day === today ? t("today") : day === yest ? t("yesterday") : day}
        </div>,
      );
    }
    rows.push(
      <div key={w.path} className="work-row" onClick={() => onOpen(w)}>
        <div className="wicon">{ICONS[w.kind] || "▢"}</div>
        <div className="winfo">
          <b>{w.name}</b>
          <span className="wrel">{w.rel || ""}</span>
        </div>
        <div className="wmeta">
          <span>{fmtSize(w.size)}</span>
          <span>{new Date(w.mtime * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}</span>
        </div>
        <button
          className="reveal"
          title="Finder"
          onClick={(e) => {
            e.stopPropagation();
            hub.call("works.open", { path: w.path, reveal: true }).catch(() => {});
          }}
        >
          ⌖
        </button>
      </div>,
    );
  }
  return <>{rows}</>;
}

interface PreviewRead {
  kind?: string;
  data_uri?: string;
  content?: string;
  truncated?: boolean;
  size?: number;
}
type PreviewBody =
  | { type: "image"; src: string }
  | { type: "text"; content: string; truncated: boolean }
  | { type: "note"; text: string };

function toBody(r: PreviewRead, t: ReturnType<typeof useT>): PreviewBody {
  if (r.kind === "image" && r.data_uri) return { type: "image", src: r.data_uri };
  if (r.kind === "text") return { type: "text", content: r.content || "", truncated: !!r.truncated };
  return { type: "note", text: r.kind === "image" && r.truncated ? t("wp-too-big") : t("wp-binary") };
}

function WorkPreview({
  preview,
  hub,
  onClose,
}: {
  preview: { work: Work; body: PreviewBody };
  hub: ReturnType<typeof useHubApi>["hub"];
  onClose: () => void;
}) {
  const t = useT();
  const { work, body } = preview;
  return (
    <div className="overlay open" onClick={onClose}>
      <div className="modal" onClick={(e) => e.stopPropagation()}>
        <div className="wp-head">
          <b>{work.name}</b>
          <span className="wp-meta">
            {work.rel || ""} · {fmtSize(work.size)}
          </span>
        </div>
        {body.type === "image" && (
          <div className="wp-img">
            <img src={body.src} alt={work.name} />
          </div>
        )}
        {body.type === "text" && (
          <div>
            <pre className="wp-pre">{body.content}</pre>
            {body.truncated && <div className="wp-note">{t("wp-truncated")}</div>}
          </div>
        )}
        {body.type === "note" && <div className="wp-note">{body.text}</div>}
        <div className="acts" style={{ marginTop: 14 }}>
          <button className="btn text" onClick={onClose}>
            {t("cancel")}
          </button>
          <div className="grow" />
          <button className="btn soft" onClick={() => hub.call("works.open", { path: work.path }).catch(() => {})}>
            {t("wp-open-system")}
          </button>
        </div>
      </div>
    </div>
  );
}
