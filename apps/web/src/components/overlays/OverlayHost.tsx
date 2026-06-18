/* OverlayHost — renders the one active overlay from the overlay context. Mounted
 * once in App.tsx so any view can open an overlay via useOverlay().open(...). One
 * at a time (the vanilla app's overlays were mutually exclusive fixed layers). */

import { useOverlay } from "../../state/overlay";
import { WakeSheet } from "../deck/WakeSheet";
import { FirstRun } from "./FirstRun";
import { CreateFlow } from "./CreateFlow";
import { BuiltinPicker } from "./BuiltinPicker";
import { ModelGate } from "./ModelGate";

export function OverlayHost() {
  const { state, close } = useOverlay();
  if (!state) return null;
  switch (state.kind) {
    case "firstrun":
      return <FirstRun onClose={close} />;
    case "create":
      return <CreateFlow onClose={close} />;
    case "builtin":
      return <BuiltinPicker onClose={close} />;
    case "model-gate":
      return <ModelGate onClose={close} onReady={state.onReady} />;
    case "wake":
      return <WakeSheet card={state.card} onClose={close} />;
  }
}
