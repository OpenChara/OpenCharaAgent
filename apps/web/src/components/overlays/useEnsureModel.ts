/* useEnsureModel — the model gate shared by the create/builtin/wake entry points,
 * mirroring app.js ensureModel(). If a text key is set, the action runs straight
 * away. If not, instead of EJECTING to Settings and discarding what the user
 * wanted (the old behaviour), it opens the in-flow ModelGate overlay, which asks
 * for the key and RESUMES the action via onReady — keeping the inspiration→living-
 * chara path unbroken for a brand-new user. */

import { useCallback } from "react";
import { useHubState } from "../../state/hub";
import { useOverlay } from "../../state/overlay";

export function useEnsureModel(): (action: () => void) => void {
  const { snapshot } = useHubState();
  const overlay = useOverlay();
  const defaults = (snapshot?.defaults as { has_key?: boolean; base_url?: string }) || {};
  const ready = !!(defaults.has_key && defaults.base_url);
  return useCallback(
    (action: () => void) => {
      if (ready) action();
      else overlay.open({ kind: "model-gate", onReady: action });
    },
    [ready, overlay],
  );
}
