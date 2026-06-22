/* The composer — textarea + attach menu + slash-command palette + send/stop.
 *
 * Two pop-ups float above the bar, sharing one quiet card style (.composer-pop):
 *   - the `+` opens an ATTACH menu (Files / Folder / Images / Paste image / URL),
 *   - typing `/` opens a COMMAND palette (filter + arrow/enter/tab autocomplete).
 *
 * Optimistic UI (CLAUDE.md binding): the send button flips to ■ the instant a turn
 * is in flight; an empty box + ■ click interrupts; a slash command runs as a control
 * line; busy + text stages the message. Attachments stage as chips, read to base64.
 */

import { useEffect, useMemo, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { FileText, FolderOpen, Image as ImageIcon, ClipboardPaste, Link2 } from "lucide-react";
import { useT, useLang } from "../../i18n";
import { errMsg } from "../../lib/status";
import type { StagedAttachment } from "../../hooks/useCharaStream";
import { readAttachment, humanSize, ATTACH_MAX_BYTES, ATTACH_ACCEPT_ALL } from "./attachments";

/* The slash commands, mirrored from core/commands.py (the ONE registry). Help is
 * bilingual — these are power-user controls, so the palette speaks the UI language.
 * Kept here (not an RPC) so the palette is instant + offline; update alongside the
 * backend registry. */
type Cmd = { name: string; usage: string; en: string; zh: string };
const COMMANDS: Cmd[] = [
  { name: "aspiration", usage: "/aspiration [text | clear]", en: "the chara's lifelong ideal (yours to set)", zh: "角色一生追寻的理想（你来设定）" },
  { name: "mode", usage: "/mode live|chat", en: "live: keeps creating while you watch; chat: replies only", zh: "live：边看边自主创作；chat：只回应你" },
  { name: "model", usage: "/model <id>", en: "session-scoped model hot-swap (empty: show current)", zh: "本会话临时换模型（留空查看当前）" },
  { name: "provider", usage: "/provider <label>", en: "switch this chara to a saved provider key", zh: "切换到已保存的供应商 key" },
  { name: "reasoning", usage: "/reasoning off|low|medium|high", en: "reasoning effort (default medium)", zh: "推理强度（默认 medium）" },
  { name: "thinking", usage: "/thinking on|off", en: "show the thinking text (default: ✶ indicator only)", zh: "显示思考过程（默认只显示 ✶）" },
  { name: "net", usage: "/net on|off", en: "terminal network access", zh: "终端网络访问开关" },
  { name: "allow-dir", usage: "/allow-dir <path>", en: "extra writable path (sandbox)", zh: "额外可写目录（沙盒）" },
  { name: "quiet", usage: "/quiet <seconds>", en: "silence before it resumes its own work (default 300)", zh: "静默多久后回到自我工作（默认 300）" },
  { name: "patience", usage: "/patience <seconds>", en: "base seconds between spontaneous cycles", zh: "自发循环之间的基础间隔（秒）" },
  { name: "steps", usage: "/steps <n>", en: "max tool-call iterations per turn (default 80)", zh: "每轮最多工具调用次数（默认 80）" },
  { name: "status", usage: "/status", en: "environment + context size", zh: "环境与上下文用量" },
  { name: "memory", usage: "/memory", en: "the durable memory document", zh: "长期记忆文档" },
  { name: "memory_path", usage: "/memory_path", en: "where the memory lives on disk", zh: "记忆文件在磁盘上的位置" },
  { name: "files", usage: "/files", en: "sandbox file listing", zh: "沙盒文件列表" },
  { name: "workspace", usage: "/workspace", en: "workspace file listing", zh: "工作区文件列表" },
  { name: "read", usage: "/read <file>", en: "read a sandbox file", zh: "读取一个沙盒文件" },
  { name: "wread", usage: "/wread <file>", en: "read a workspace file", zh: "读取一个工作区文件" },
  { name: "write", usage: "/write <file> <text>", en: "write a sandbox file", zh: "写入一个沙盒文件" },
  { name: "skills", usage: "/skills", en: "skill index (the chara writes its own)", zh: "技能索引（角色自己编写）" },
  { name: "mcp", usage: "/mcp", en: "configured MCP tool servers", zh: "已配置的 MCP 工具服务器" },
  { name: "logs", usage: "/logs", en: "recent audit events", zh: "最近的审计事件" },
  { name: "compact", usage: "/compact", en: "fold older turns into a summary now", zh: "立即把旧对话折叠成摘要" },
  { name: "reset", usage: "/reset", en: "zero session context (new transcript epoch)", zh: "清空会话上下文（新的记录纪元）" },
  { name: "help", usage: "/help", en: "this list", zh: "这个列表" },
];

export function Composer({
  charName,
  streaming,
  resting,
  statusSlot,
  onSend,
  onInterrupt,
  onCommand,
  onError,
}: {
  charName: string;
  streaming: boolean;
  resting: boolean;
  /** the work-status line, rendered at the top of the composer-wrap (chat.js). */
  statusSlot?: ReactNode;
  onSend: (text: string, atts: StagedAttachment[]) => void;
  onInterrupt: () => void;
  onCommand: (line: string) => void;
  onError: (msg: string) => void;
}) {
  const t = useT();
  const { lang } = useLang();
  // Persist the unsent text draft per-chara so leaving the chat doesn't throw away
  // a half-typed message — restored on return. (Attachments are blobs, not persisted.)
  const draftKey = (n: string) => `lm-composer-draft:${n}`;
  const readDraft = (n: string): string => {
    try {
      return localStorage.getItem(draftKey(n)) || "";
    } catch {
      return "";
    }
  };
  const [text, setText] = useState(() => readDraft(charName));
  const [staged, setStaged] = useState<StagedAttachment[]>([]);
  useEffect(() => {
    setText(readDraft(charName));
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [charName]);
  useEffect(() => {
    try {
      if (text) localStorage.setItem(draftKey(charName), text);
      else localStorage.removeItem(draftKey(charName));
    } catch {
      /* private mode — ignore */
    }
  }, [text, charName]);
  const [stopping, setStopping] = useState(false);
  const [menuOpen, setMenuOpen] = useState(false);
  const [sel, setSel] = useState(0); // highlighted slash-command index
  const [slashOff, setSlashOff] = useState(false); // Esc dismissed the palette
  const fileRef = useRef<HTMLInputElement>(null);
  const taRef = useRef<HTMLTextAreaElement>(null);
  const wrapRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);

  // The slash palette: open while the box is exactly `/<partial>` (no space yet),
  // not streaming, not Esc-dismissed. A space (= entering args) closes it.
  const slashQuery = !streaming && !slashOff && /^\/[^\s]*$/.test(text) ? text.slice(1).toLowerCase() : null;
  const matches = useMemo(
    () => (slashQuery === null ? [] : COMMANDS.filter((c) => c.name.startsWith(slashQuery))),
    [slashQuery],
  );
  const paletteOpen = matches.length > 0;
  useEffect(() => { setSel(0); }, [slashQuery]);
  useEffect(() => { if (!/^\//.test(text)) setSlashOff(false); }, [text]); // re-arm once they leave `/`
  // Keep the keyboard-highlighted row in view on a long (24-command) list.
  useEffect(() => {
    if (paletteOpen) (listRef.current?.children[sel] as HTMLElement | undefined)?.scrollIntoView({ block: "nearest" });
  }, [sel, paletteOpen]);

  // Close the attach menu on an outside click / Esc.
  useEffect(() => {
    if (!menuOpen) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setMenuOpen(false);
    };
    document.addEventListener("mousedown", onDown);
    return () => document.removeEventListener("mousedown", onDown);
  }, [menuOpen]);

  const focus = () => requestAnimationFrame(() => taRef.current?.focus());

  const stageFiles = async (list: FileList | File[] | null) => {
    for (const f of Array.from(list || [])) {
      if (!f) continue;
      if (f.size > ATTACH_MAX_BYTES) {
        onError(t("attach-too-big", { name: f.name || t("attach-file") }));
        continue;
      }
      try {
        const att = await readAttachment(f);
        setStaged((prev) => [...prev, att]);
      } catch (e) {
        onError(errMsg(e));
      }
    }
  };

  const pick = (accept: string, directory = false) => {
    const el = fileRef.current;
    if (!el) return;
    el.accept = accept;
    if (directory) el.setAttribute("webkitdirectory", "");
    else el.removeAttribute("webkitdirectory");
    el.click();
    setMenuOpen(false);
  };

  const pasteImage = async () => {
    setMenuOpen(false);
    try {
      const items = await navigator.clipboard.read();
      for (const it of items) {
        const type = it.types.find((ty) => ty.startsWith("image/"));
        if (!type) continue;
        const blob = await it.getType(type);
        await stageFiles([new File([blob], `pasted.${type.split("/")[1] || "png"}`, { type })]);
        return;
      }
      onError(t("attach-no-clip-image"));
    } catch {
      onError(t("attach-clip-denied"));
    }
  };

  const addUrl = async () => {
    setMenuOpen(false);
    const url = window.prompt(t("attach-url-prompt"))?.trim();
    if (!url) return;
    try {
      const res = await fetch(url);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const blob = await res.blob();
      const name = decodeURIComponent(url.split("/").pop()?.split("?")[0] || "download") || "download";
      await stageFiles([new File([blob], name, { type: blob.type || "application/octet-stream" })]);
    } catch (e) {
      onError(t("attach-url-failed", { err: errMsg(e) }));
    }
  };

  const unstage = (att: StagedAttachment) => setStaged((prev) => prev.filter((a) => a !== att));

  const chooseCmd = (c: Cmd) => {
    setText("/" + c.name + " "); // the trailing space closes the palette; user edits args / hits Enter
    setSlashOff(false);
    focus();
  };

  const submit = () => {
    const trimmed = text.trim();
    const hasAttach = staged.length > 0;
    if (!trimmed && !hasAttach) return;
    if (trimmed.startsWith("/") && !hasAttach) {
      if (streaming) {
        onError(t("busy-cmd"));
        return;
      }
      setText("");
      onCommand(trimmed);
      return;
    }
    const atts = staged;
    setText("");
    setStaged([]);
    onSend(trimmed, atts);
  };

  const onSendClick = () => {
    const hasText = text.trim().length > 0;
    if (streaming && !hasText) {
      setStopping(true);
      onInterrupt();
    } else {
      submit();
    }
  };

  const onKeyDown = (ev: KeyboardEvent<HTMLTextAreaElement>) => {
    if (paletteOpen) {
      if (ev.key === "ArrowDown") { ev.preventDefault(); setSel((i) => (i + 1) % matches.length); return; }
      if (ev.key === "ArrowUp") { ev.preventDefault(); setSel((i) => (i - 1 + matches.length) % matches.length); return; }
      if (ev.key === "Tab" || (ev.key === "Enter" && !ev.shiftKey && !ev.nativeEvent.isComposing)) {
        ev.preventDefault();
        chooseCmd(matches[Math.min(sel, matches.length - 1)]); // autocomplete; a 2nd Enter sends
        return;
      }
      if (ev.key === "Escape") { ev.preventDefault(); setSlashOff(true); return; }
    }
    if (ev.key === "Enter" && !ev.shiftKey && !ev.nativeEvent.isComposing) {
      ev.preventDefault();
      submit();
    }
  };

  const showStop = streaming && text.trim().length === 0;

  return (
    <div className="composer-wrap" ref={wrapRef}>
      {statusSlot}

      {paletteOpen && (
        <div className="composer-pop slash-pop" role="listbox">
          <div className="pop-head">{t("slash-head")}</div>
          <div className="pop-list" ref={listRef}>
            {matches.map((c, i) => (
              <button
                key={c.name}
                type="button"
                role="option"
                aria-selected={i === sel}
                className={"slash-row" + (i === sel ? " on" : "")}
                onMouseEnter={() => setSel(i)}
                onClick={() => chooseCmd(c)}
              >
                <span className="slash-name">/{c.name}</span>
                <span className="slash-desc">{lang === "zh" ? c.zh : c.en}</span>
              </button>
            ))}
          </div>
        </div>
      )}

      {menuOpen && (
        <div className="composer-pop attach-pop">
          <div className="pop-head">{t("attach-head")}</div>
          <button type="button" className="pop-row" onClick={() => pick(ATTACH_ACCEPT_ALL)}>
            <FileText size={16} /> {t("attach-files")}
          </button>
          <button type="button" className="pop-row" onClick={() => pick(ATTACH_ACCEPT_ALL, true)}>
            <FolderOpen size={16} /> {t("attach-folder")}
          </button>
          <button type="button" className="pop-row" onClick={() => pick("image/*")}>
            <ImageIcon size={16} /> {t("attach-images")}
          </button>
          <button type="button" className="pop-row" onClick={() => void pasteImage()}>
            <ClipboardPaste size={16} /> {t("attach-paste")}
          </button>
          <button type="button" className="pop-row" onClick={() => void addUrl()}>
            <Link2 size={16} /> {t("attach-url")}
          </button>
          <div className="pop-div" />
          <div className="pop-tip">{t("attach-tip")}</div>
        </div>
      )}

      {staged.length > 0 && (
        <div className="attach-stage">
          {staged.map((att, i) =>
            att.isImage ? (
              <div key={i} className="attach-chip" title={att.name}>
                <img className="thumb" src={att.url} alt={att.name} loading="lazy" decoding="async" />
                <button className="rm" title={t("attach-remove")} onClick={() => unstage(att)}>×</button>
              </div>
            ) : (
              <div key={i} className="attach-chip file" title={att.name}>
                <span className="ficon">📄</span>
                <div className="meta">
                  <span className="fname">{att.name}</span>
                  <span className="fsize">{humanSize(att.size)}</span>
                </div>
                <button className="rm" title={t("attach-remove")} onClick={() => unstage(att)}>×</button>
              </div>
            ),
          )}
        </div>
      )}

      <div className="composer">
        <input
          ref={fileRef}
          type="file"
          multiple
          accept={ATTACH_ACCEPT_ALL}
          hidden
          onChange={(e) => {
            void stageFiles(e.target.files);
            e.target.value = "";
          }}
        />
        <button
          className={"attach-btn" + (menuOpen ? " on" : "")}
          title={t("attach-add")}
          aria-label={t("attach-add")}
          aria-expanded={menuOpen}
          onClick={() => setMenuOpen((v) => !v)}
        >
          +
        </button>
        <textarea
          ref={taRef}
          rows={1}
          value={text}
          placeholder={resting ? t("composer-resting-ph") : t("composer-ph", { name: charName })}
          onChange={(e) => {
            setText(e.target.value);
            const el = e.target;
            el.style.height = "auto";
            el.style.height = Math.min(el.scrollHeight, 130) + "px";
          }}
          onKeyDown={onKeyDown}
        />
        <button
          className={showStop ? `stop${stopping ? " stopping" : ""}` : "send"}
          disabled={stopping && showStop}
          onClick={onSendClick}
        >
          {showStop ? "■" : "↑"}
        </button>
      </div>
    </div>
  );
}
