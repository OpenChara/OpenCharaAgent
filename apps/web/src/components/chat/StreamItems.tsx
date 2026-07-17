/* The stream item renderers — one component per StreamItem kind, mirroring the
 * DOM that chat.js built (char-msg / think-block / tool-group / sys-note /
 * user-msg + the inline permission & clarify boxes). A file the chara surfaces
 * via a MEDIA:<path> marker is rendered inline inside its say message
 * (MediaInline), not as a separate item. The CSS class names are reused 1:1 from
 * front/web/style.css (ported in global.css).
 *
 * The say|muse channel is a backend messaging-gateway forwarding hint, not a
 * display distinction — on the desktop muse renders identically to say. */

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useT, type TFn } from "../../i18n";
import { assetUrl } from "../../rpc";
import { glyphOf, paletteClass } from "../../lib/format";
import { assertNever } from "../../lib/exhaustive";
import { splitOutbound, assetPathFor, type MediaMarker } from "../../lib/media";
import {
  chipLabel,
  summarizeToolTally,
  type StreamItem,
  type ToolGroupItem,
  type TextItem,
  type ThinkItem,
  type SystemItem,
  type UserItem,
  type PermissionItem,
  type ClarifyItem,
} from "./streamModel";

/** A small avatar for the chara's messages: the snapshot's inline avatar data-URI
 *  when present, falling back to the palette+letter glyph when absent. */
function Avatar({ name, avatarUri }: { name: string; avatarUri?: string }) {
  if (avatarUri) {
    return (
      <div className="avatar-s">
        <img src={avatarUri} alt="" loading="lazy" decoding="async" />
      </div>
    );
  }
  return (
    <div className={`avatar-s ${paletteClass(name)}`}>
      <span className="glyph-txt">{glyphOf(name)}</span>
    </div>
  );
}

/** Markdown for say/super; plain text for think (chat.js closeCurrent). */
function Markdown({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>;
}

/** One inline file the chara surfaced via a MEDIA: marker — an image embeds, any
 *  other file is offered as a download. The path is resolved to a same-origin
 *  /asset URL (the server re-validates it against the session sandbox). */
function MediaInline({
  marker,
  sandboxRoot,
  workspaceRoot,
}: {
  marker: MediaMarker;
  sandboxRoot: string;
  workspaceRoot: string;
}) {
  const t = useT();
  const [broken, setBroken] = useState(false);
  const src = assetUrl(assetPathFor(marker.path, sandboxRoot, workspaceRoot));
  const name = marker.path.split("/").pop() || (marker.isImage ? "image" : "file");
  if (marker.isImage) {
    return broken ? (
      <div className="att-missing">{t("att-img-missing")}</div>
    ) : (
      <div className="wp-img">
        <img alt={name} loading="lazy" decoding="async" src={src} onError={() => setBroken(true)} />
      </div>
    );
  }
  return (
    <div className="artifact">
      <div className="thumb">{(name.split(".").pop() || "").toUpperCase().slice(0, 4) || "FILE"}</div>
      <div className="meta">
        <b>{name}</b>
        <span>{t("att-file")}</span>
      </div>
      <div className="acts">
        <a className="go" href={src} target="_blank" rel="noopener" download={name}>
          {t("att-open")}
        </a>
      </div>
    </div>
  );
}

function SayMessage({
  item,
  charName,
  avatarUri,
  sandboxRoot,
  workspaceRoot,
}: {
  item: TextItem;
  charName: string;
  avatarUri?: string;
  sandboxRoot: string;
  workspaceRoot: string;
}) {
  const t = useT();
  const isSuper = item.kind === "super";
  // Extract MEDIA: markers from the accumulated text (hermes shape): prose renders
  // as markdown, a marker renders as an inline image / download, in order.
  const segments = splitOutbound(item.raw);
  return (
    <div className={`char-msg${isSuper ? " super-chat" : ""}`}>
      <Avatar name={charName} avatarUri={avatarUri} />
      <div className="body">
        <div className="name">
          {charName}
          {isSuper && (
            <span className="super-badge" title={t("superchat-tip")}>
              ⚡ Super Chat
            </span>
          )}
        </div>
        {segments.map((seg, i) =>
          seg.kind === "text" ? (
            <div className="text" key={i}>
              <Markdown text={seg.text} />
            </div>
          ) : (
            <MediaInline key={i} marker={seg} sandboxRoot={sandboxRoot} workspaceRoot={workspaceRoot} />
          ),
        )}
      </div>
    </div>
  );
}

function ThinkBlock({ item }: { item: ThinkItem }) {
  const t = useT();
  const [open, setOpen] = useState(() => {
    try {
      return localStorage.getItem("lm-chat-thinking-expanded") === "1";
    } catch {
      return false;
    }
  });
  const toggle = () => {
    setOpen((v) => {
      const next = !v;
      try {
        localStorage.setItem("lm-chat-thinking-expanded", next ? "1" : "0");
      } catch {
        /* ok */
      }
      return next;
    });
  };
  const head = item.streaming
    ? `✶ ${t("thinking-live", { n: item.tokens })}`
    : `${t("thinking-done", { n: item.tokens })} ${open ? "▾" : "▸"}`;
  return (
    <div className={`think-block${item.streaming ? " streaming" : ""}`} data-tokens={item.tokens}>
      <button className={`think-head${item.streaming ? " streaming" : ""}`} onClick={toggle}>
        {head}
      </button>
      <div className="think-body" style={{ display: open ? "block" : "none" }}>
        {item.raw}
      </div>
    </div>
  );
}

function ToolGroup({ item, technical }: { item: ToolGroupItem; technical: boolean }) {
  const t = useT();
  const [collapsed, setCollapsed] = useState(true);
  const summary = "⚙ " + summarizeToolTally(t, item.tally, item.fails);
  return (
    <div className={`tool-group${collapsed ? " collapsed" : ""}`}>
      <button className="tool-group-summary" onClick={() => setCollapsed((c) => !c)}>
        {summary}
      </button>
      <div className="tool-chip-line">
        {item.chips.map((chip) => (
          <ToolChipRow key={chip.key} chip={chip} technical={technical} />
        ))}
      </div>
    </div>
  );
}

function ToolChipRow({ chip, technical }: { chip: ToolGroupItem["chips"][number]; technical: boolean }) {
  const t = useT();
  const [open, setOpen] = useState(false);
  const cls = chip.running ? "running" : chip.ok ? "ok" : "err";
  const detail = chip.running ? (technical ? chip.preview : "") : chip.summary || t("tool-no-summary");
  const hasDetail = !!detail.trim();
  return (
    <div className={`tool-chip-item${hasDetail ? " has-detail" : ""}${open ? " open" : ""}`}>
      <button className={`tool-chip ${cls}`} onClick={() => setOpen((o) => !o)}>
        {chip.running && <span className="spin" />}
        <span>{chipLabel(chip)}</span>
      </button>
      {hasDetail && <div className="tool-detail">{detail}</div>}
    </div>
  );
}


/** A centered WeChat-style time separator between messages (today→time, 昨天→昨天 HH:MM,
 *  older→date). The label is computed by the caller in the viewer's chosen timezone. */
export function TimeSeparator({ label }: { label: string }) {
  return <div className="time-sep">{label}</div>;
}

function SystemLine({ item }: { item: SystemItem }) {
  const t = useT();
  // The compaction-boundary divider carries no text of its own — it renders a fixed
  // i18n marker telling the reader the chara's verbatim memory above here is condensed.
  if (item.cls === "compacted") {
    return <div className="sys-note compacted">{t("compacted-here")}</div>;
  }
  return <div className={`sys-note${item.cls ? " " + item.cls : ""}`}>{item.text}</div>;
}

function UserMessage({ item, t }: { item: UserItem; t: TFn }) {
  return (
    <div className={`user-msg${item.queued ? " queued" : ""}`}>
      {item.atts.length > 0 && (
        <div className="att-row">
          {item.atts.map((a, i) =>
            a.isImage ? (
              <img key={i} className="att-thumb" src={a.url} alt={a.name} loading="lazy" decoding="async" />
            ) : (
              <div key={i} className="att-file">
                📄 {a.name}
              </div>
            ),
          )}
        </div>
      )}
      {item.text && <div className="bubble">{item.text}</div>}
      {item.queued && <div className="via-tag">{t("queued-hint")}</div>}
      {item.via && (
        <div className="via-tag">
          {t("via-tag")} {item.via}
        </div>
      )}
    </div>
  );
}

function PermissionBox({
  item,
  onReply,
}: {
  item: PermissionItem;
  onReply: (id: string, granted: boolean) => void;
}) {
  return (
    <div className="sec" style={{ maxWidth: 430, marginLeft: 40 }}>
      <h3>🔐 {item.title}</h3>
      <div className="memory-text">{item.reason}</div>
      <div className="acts" style={{ marginTop: 10 }}>
        <button className="btn soft" onClick={() => onReply(item.askId, false)}>
          ✗
        </button>
        <div className="grow" />
        <button className="btn primary" onClick={() => onReply(item.askId, true)}>
          ✓
        </button>
      </div>
    </div>
  );
}

function ClarifyBox({
  item,
  onReply,
}: {
  item: ClarifyItem;
  onReply: (id: string, answer: string) => void;
}) {
  const t = useT();
  const [other, setOther] = useState("");
  return (
    <div className="sec" style={{ maxWidth: 430, marginLeft: 40 }}>
      <h3>❓ {item.question}</h3>
      <div className="acts" style={{ marginTop: 10, flexWrap: "wrap", gap: 6 }}>
        {item.choices.map((c) => (
          <button key={c} className="btn soft" onClick={() => onReply(item.askId, c)}>
            {c}
          </button>
        ))}
      </div>
      <div className="acts" style={{ marginTop: 8, gap: 6 }}>
        <input
          className="clarify-other"
          type="text"
          placeholder={t("clarify-other")}
          value={other}
          onChange={(e) => setOther(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && other.trim()) onReply(item.askId, other.trim());
          }}
        />
        <button className="btn primary" onClick={() => other.trim() && onReply(item.askId, other.trim())}>
          →
        </button>
      </div>
    </div>
  );
}

/** Render one stream item by kind. */
export function StreamItemView({
  item,
  charName,
  technical = false,
  avatarUri,
  sandboxRoot,
  workspaceRoot,
  onPermission,
  onClarify,
}: {
  item: StreamItem;
  charName: string;
  /** show raw tool-call previews (Settings · 显示 = Technical; Chat mirrors body.technical). */
  technical?: boolean;
  /** the chara's inline avatar data-URI (snapshot.avatar_uri); glyph fallback when absent. */
  avatarUri?: string;
  /** session roots (snapshot) used to resolve a MEDIA: marker's relative path. */
  sandboxRoot: string;
  workspaceRoot: string;
  onPermission: (id: string, granted: boolean) => void;
  onClarify: (id: string, answer: string) => void;
}) {
  const t = useT();
  switch (item.kind) {
    case "user":
      return <UserMessage item={item} t={t} />;
    case "say":
    case "super":
      return (
        <SayMessage
          item={item}
          charName={charName}
          avatarUri={avatarUri}
          sandboxRoot={sandboxRoot}
          workspaceRoot={workspaceRoot}
        />
      );
    case "think":
      return <ThinkBlock item={item} />;
    case "tool-group":
      return <ToolGroup item={item} technical={technical} />;
    case "system":
      return <SystemLine item={item} />;
    case "permission":
      return <PermissionBox item={item} onReply={onPermission} />;
    case "clarify":
      return <ClarifyBox item={item} onReply={onClarify} />;
    default:
      // Compile-time exhaustiveness: a new StreamItem kind that isn't handled
      // above stops `item` being `never` and fails this call's typecheck.
      return assertNever(item);
  }
}
