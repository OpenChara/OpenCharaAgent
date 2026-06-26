/* Classify a file for the works preview: which native browser surface (if any) can
 * show it in place. MUST stay in step with the supervisor's inline-asset lane
 * (http.py `_ASSET_MIME` for images + `_ASSET_INLINE_MIME` for audio/video/pdf) — a
 * kind we render as <audio>/<video>/<iframe> only plays if the server serves it
 * inline rather than forcing a download. Everything else falls back to text/download. */

const IMAGE = new Set([".png", ".jpg", ".jpeg", ".gif", ".webp"]);
const AUDIO = new Set([".wav", ".ogg", ".oga", ".opus", ".mp3", ".m4a", ".aac", ".flac"]);
const VIDEO = new Set([".mp4", ".m4v", ".webm", ".mov", ".ogv"]);

export type MediaKind = "image" | "audio" | "video" | "pdf" | "other";

export function extOf(name: string): string {
  const i = name.lastIndexOf(".");
  return i >= 0 ? name.slice(i).toLowerCase() : "";
}

/** The native-preview kind for a filename, or "other" (text/binary → read/download). */
export function mediaKind(name: string): MediaKind {
  const ext = extOf(name);
  if (IMAGE.has(ext)) return "image";
  if (AUDIO.has(ext)) return "audio";
  if (VIDEO.has(ext)) return "video";
  if (ext === ".pdf") return "pdf";
  return "other";
}
