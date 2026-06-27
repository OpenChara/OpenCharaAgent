import { useSyncExternalStore, useCallback } from "react";

/* Minimal hash router — mirrors app.js route() (#/ board, #/deck, #/gateways,
   #/settings, #/chara/<name>(/works|/term)?). Hash routing keeps the server
   free of any SPA-fallback list and survives any mount path. */

export type ViewName = "board" | "deck" | "market" | "gateways" | "settings" | "chat";
export type ChatSub = "chat" | "works" | "term" | "home";

export interface Route {
  view: ViewName;
  name: string | null; // chara name when view === "chat"
  sub: ChatSub;
}

const CHARA_RE = /^#\/chara\/([^/]+)(?:\/(works|term|home))?$/;

export function parseHash(hash: string): Route {
  const m = CHARA_RE.exec(hash);
  if (m) {
    return { view: "chat", name: decodeURIComponent(m[1]), sub: (m[2] as ChatSub) || "chat" };
  }
  const seg = hash.replace(/^#\//, "").split("/")[0];
  const view: ViewName =
    seg === "deck" || seg === "market" || seg === "gateways" || seg === "settings" ? seg : "board";
  return { view, name: null, sub: "chat" };
}

function subscribe(cb: () => void): () => void {
  window.addEventListener("hashchange", cb);
  return () => window.removeEventListener("hashchange", cb);
}

export function navTo(hash: string): void {
  if (location.hash !== hash) location.hash = hash;
}

export function useHashRoute(): Route {
  const hash = useSyncExternalStore(
    subscribe,
    () => location.hash || "#/",
    () => "#/",
  );
  return parseHash(hash);
}

export function useNavigate(): (hash: string) => void {
  return useCallback((hash: string) => navTo(hash), []);
}
