/* Overlay context — the single host for the Track-C overlays (first-run,
 * create-flow, builtin carousel, avatar editor, import). The vanilla app.js kept
 * each overlay as a fixed DOM node toggled by class; in the SPA any view opens one
 * imperatively via useOverlay().open(kind, props) and the OverlayHost (mounted in
 * App.tsx) renders exactly one at a time. close() dismisses it.
 *
 * Keeping this a context (rather than module-global like deckToast) lets overlays
 * re-open each other — e.g. the create-flow's "wake now?" step hands off to the
 * deck's WakeSheet, and the builtin picker routes a pick through the wake flow —
 * while staying inside React's tree so they read i18n/hub the same way as views. */

import {
  createContext,
  useCallback,
  useContext,
  useMemo,
  useState,
  type ReactNode,
} from "react";
import type { DeckCard } from "../components/deck/types";

/** The overlays the host can show, with their per-kind props. */
export type OverlayState =
  | { kind: "firstrun" }
  | { kind: "create" }
  | { kind: "builtin" }
  | { kind: "import" }
  // The in-flow model-key step; onReady resumes the create/wake the user started.
  | { kind: "model-gate"; onReady: () => void }
  // The 2-step wake sheet lives in components/deck but is opened as an overlay so
  // create-flow / builtin picker can hand off to it (app.js openWakeSheet).
  | { kind: "wake"; card: DeckCard }
  | null;

interface OverlayContextValue {
  state: OverlayState;
  open: (state: NonNullable<OverlayState>) => void;
  close: () => void;
}

const OverlayContext = createContext<OverlayContextValue | null>(null);

export function OverlayProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<OverlayState>(null);
  const open = useCallback((next: NonNullable<OverlayState>) => setState(next), []);
  const close = useCallback(() => setState(null), []);
  const value = useMemo(() => ({ state, open, close }), [state, open, close]);
  return <OverlayContext.Provider value={value}>{children}</OverlayContext.Provider>;
}

export function useOverlay(): OverlayContextValue {
  const ctx = useContext(OverlayContext);
  if (!ctx) throw new Error("useOverlay must be used within an OverlayProvider");
  return ctx;
}
