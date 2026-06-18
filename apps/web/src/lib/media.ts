/**
 * Outbound media-marker parsing for the web — the TypeScript twin of
 * `protocol/media.py` (and the analog of hermes's `ui-tui/markdown.tsx`).
 *
 * A chara surfaces a file by writing a `MEDIA:<path>` line in its reply; the
 * backend passes the text through untouched (hermes shape), and each rich surface
 * extracts the marker at render time. Here that means: split the accumulated say
 * text into prose runs (markdown-rendered) and media markers (rendered inline as an
 * image / download), and turn a workspace-relative path into a same-origin
 * `/asset?p=<abspath>` URL the desktop server serves (the server re-validates the
 * path against the session sandbox, so this mapping is convenience, not trust).
 *
 * The deliverable-extension set and image set are kept in lockstep with
 * `protocol/media.py` (duplicated across the language boundary the same way hermes
 * duplicates base.py ↔ markdown.tsx); the comparison audit checks they match.
 */

// Mirror protocol/media.py MEDIA_DELIVERY_EXTS (character-for-character).
export const MEDIA_DELIVERY_EXTS: ReadonlySet<string> = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
  ".mp4", ".mov", ".avi", ".mkv", ".webm",
  ".mp3", ".wav", ".ogg", ".opus", ".m4a", ".flac",
  ".pdf", ".docx", ".doc", ".odt", ".rtf", ".txt", ".md", ".epub",
  ".xlsx", ".xls", ".ods", ".csv", ".tsv", ".json", ".xml", ".yaml", ".yml",
  ".pptx", ".ppt", ".odp", ".key",
  ".zip", ".tar", ".gz", ".tgz", ".bz2", ".xz", ".7z", ".rar", ".apk", ".ipa",
  ".html", ".htm",
]);

// Mirror protocol/media.py IMAGE_EXTS — embedded inline (vs offered as download).
export const IMAGE_EXTS: ReadonlySet<string> = new Set([
  ".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tiff", ".svg",
]);

// A whole line that is just a MEDIA: marker (quote/backtick-tolerant). Like hermes
// markdown.tsx's MEDIA_LINE_RE, line-anchored so a mid-sentence "MEDIA:" is prose.
const MEDIA_LINE_RE = /^\s*[`"']?MEDIA:\s*(.+?)[`"']?\s*$/;

function extOf(path: string): string {
  const dot = path.lastIndexOf(".");
  return dot === -1 ? "" : path.slice(dot).toLowerCase();
}

export interface MediaMarker {
  path: string;
  isImage: boolean;
}

/** Parse one line as a deliverable MEDIA: marker, or null. Gated on the shared
 *  deliverable-extension set so a marker means the same thing on every surface. */
export function parseMediaLine(line: string): MediaMarker | null {
  const m = line.match(MEDIA_LINE_RE);
  if (!m) return null;
  let path = m[1].trim();
  // Unwrap a symmetric surrounding quote pair, then strip leading quote chars and
  // trailing quote/punctuation residue — mirrors protocol/media.py._strip_quotes
  // (lstrip("`\"'") + rstrip("`\"',.;:)}]")) so web and messaging agree on the path.
  if (path.length >= 2 && path[0] === path[path.length - 1] && "`\"'".includes(path[0])) {
    path = path.slice(1, -1).trim();
  }
  path = path.replace(/^[`"']+/, "").replace(/[`"',.;:)}\]]+$/, "");
  const ext = extOf(path);
  if (!path || !MEDIA_DELIVERY_EXTS.has(ext)) return null;
  return { path, isImage: IMAGE_EXTS.has(ext) };
}

export type Segment =
  | { kind: "text"; text: string }
  | { kind: "media"; path: string; isImage: boolean };

/** Split accumulated say text into ordered prose / media segments. Operates on the
 *  COMPLETE text (the web re-renders the whole message as it streams, like hermes's
 *  TUI re-render) — no streaming buffer needed. Consecutive prose lines coalesce
 *  into one text segment so markdown (lists, code fences) renders intact. */
export function splitOutbound(text: string): Segment[] {
  const segments: Segment[] = [];
  let buf: string[] = [];
  const flush = () => {
    if (buf.length) {
      const joined = buf.join("\n");
      if (joined.trim()) segments.push({ kind: "text", text: joined });
      buf = [];
    }
  };
  for (const line of text.split("\n")) {
    const marker = parseMediaLine(line);
    if (marker) {
      flush();
      segments.push({ kind: "media", ...marker });
    } else {
      buf.push(line);
    }
  }
  flush();
  return segments;
}

/** True when the text holds at least one deliverable MEDIA: marker line. */
export function hasMediaMarker(text: string): boolean {
  return text.split("\n").some((l) => parseMediaLine(l) !== null);
}

function joinPath(...parts: string[]): string {
  return parts
    .map((p, i) => (i === 0 ? p.replace(/\/+$/, "") : p.replace(/^\/+|\/+$/g, "")))
    .filter(Boolean)
    .join("/");
}

/** Turn a chara-emitted relative path into a `/asset?p=<abspath>` URL, mirroring
 *  the sandbox's resolve_readable mapping: a leading `assets/` component maps to the
 *  read-only assets shelf (`<sandboxRoot>/assets/…`); everything else is
 *  workspace-relative (`<workspaceRoot>/…`). The server re-validates the resolved
 *  path against the session roots, so a wrong/spoofed path is rejected there. */
export function assetPathFor(path: string, sandboxRoot: string, workspaceRoot: string): string {
  let abs: string;
  if (/^[/~]|^[A-Za-z]:[/\\]/.test(path)) {
    abs = path; // already absolute (the server boundary check still applies)
  } else {
    const parts = path.split("/");
    abs = parts[0] === "assets"
      ? joinPath(sandboxRoot, "assets", ...parts.slice(1))
      : joinPath(workspaceRoot, path);
  }
  return `/asset?p=${encodeURIComponent(abs)}`;
}
