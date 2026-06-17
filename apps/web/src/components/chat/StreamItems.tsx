/* The stream item renderers — one component per StreamItem kind, mirroring the
 * DOM that chat.js built (char-msg / muse-msg / think-block / tool-group /
 * attachment / sys-note / user-msg + the inline permission & clarify boxes). The
 * CSS class names are reused 1:1 from front/web/style.css (ported in global.css).
 */

import { useState } from "react";
import ReactMarkdown from "react-markdown";
import remarkGfm from "remark-gfm";
import { useT, type TFn } from "../../i18n";
import { assetUrl } from "../../rpc";
import { glyphOf, paletteClass } from "../../lib/format";
import { assertNever } from "../../lib/exhaustive";
import {
  chipLabel,
  summarizeToolTally,
  type StreamItem,
  type ToolGroupItem,
  type TextItem,
  type ThinkItem,
  type AttachmentItem,
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

/** Markdown for say/super; plain text for muse/think (chat.js closeCurrent). */
function Markdown({ text }: { text: string }) {
  return <ReactMarkdown remarkPlugins={[remarkGfm]}>{text}</ReactMarkdown>;
}

function SayMessage({
  item,
  charName,
  superReadTs,
  avatarUri,
}: {
  item: TextItem;
  charName: string;
  superReadTs: number;
  avatarUri?: string;
}) {
  const t = useT();
  const isSuper = item.kind === "super";
  const read = isSuper && item.ts !== undefined && item.ts <= superReadTs;
  return (
    <div className={`char-msg${isSuper ? " super-chat" : ""}${read ? " read" : ""}`}>
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
        <div className="text">
          <Markdown text={item.raw} />
        </div>
      </div>
    </div>
  );
}

function MuseMessage({ item }: { item: TextItem }) {
  const t = useT();
  return (
    <div className="muse-msg">
      <div className="muse-label">{t("muse-label")}</div>
      <div className="muse-text">{item.raw}</div>
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

function AttachmentCard({ item, charName, avatarUri }: { item: AttachmentItem; charName: string; avatarUri?: string }) {
  const t = useT();
  const [broken, setBroken] = useState(false);
  const isImage = (item.mime || "").startsWith("image/");
  const name = item.name || (isImage ? "image" : "file");
  const media = isImage ? (
    broken ? (
      <div className="att-missing">{t("att-img-missing")}</div>
    ) : (
      <div className="wp-img">
        <img alt={name} loading="lazy" decoding="async" src={assetUrl(item.url)} onError={() => setBroken(true)} />
      </div>
    )
  ) : (
    <div className="artifact">
      <div className="thumb">{(name.split(".").pop() || "").toUpperCase().slice(0, 4) || "FILE"}</div>
      <div className="meta">
        <b>{name}</b>
        <span>{item.mime || t("att-file")}</span>
      </div>
      <div className="acts">
        <a className="go" href={assetUrl(item.url)} target="_blank" rel="noopener" download={name}>
          {t("att-open")}
        </a>
      </div>
    </div>
  );
  const cap = item.caption ? <div className="attach-cap">{item.caption}</div> : null;
  if (item.channel === "muse") {
    return (
      <div className="muse-msg">
        <div className="muse-label">{t("muse-label")}</div>
        <div className="muse-text">
          {media}
          {cap}
        </div>
      </div>
    );
  }
  return (
    <div className="char-msg">
      <Avatar name={charName} avatarUri={avatarUri} />
      <div className="body">
        <div className="name">{charName}</div>
        {media}
        {cap}
      </div>
    </div>
  );
}

function SystemLine({ item }: { item: SystemItem }) {
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
  superReadTs,
  technical,
  avatarUri,
  onPermission,
  onClarify,
}: {
  item: StreamItem;
  charName: string;
  superReadTs: number;
  technical: boolean;
  /** the chara's inline avatar data-URI (snapshot.avatar_uri); glyph fallback when absent. */
  avatarUri?: string;
  onPermission: (id: string, granted: boolean) => void;
  onClarify: (id: string, answer: string) => void;
}) {
  const t = useT();
  switch (item.kind) {
    case "user":
      return <UserMessage item={item} t={t} />;
    case "say":
    case "super":
      return <SayMessage item={item} charName={charName} superReadTs={superReadTs} avatarUri={avatarUri} />;
    case "muse":
      return <MuseMessage item={item} />;
    case "think":
      return <ThinkBlock item={item} />;
    case "tool-group":
      return <ToolGroup item={item} technical={technical} />;
    case "attachment":
      return <AttachmentCard item={item} charName={charName} avatarUri={avatarUri} />;
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
