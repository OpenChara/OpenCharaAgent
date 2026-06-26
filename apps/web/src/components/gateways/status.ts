/* Gateway status/label helpers — React-side ports of app.js:430 gwPlatLabel /
 * 436 gwStatusBits. The platform registry is the deck's single surfaced platform
 * (weixin) today; unknown platforms fall back to their raw id, matching the
 * vanilla GW_PLATFORMS lookup. */

import type { TFn } from "../../i18n";
import { GW_PLATFORMS } from "./gatewayModel";

export function gwPlatLabel(t: TFn, platform: string | null | undefined): string {
  if (!platform) return t("gw-none");
  // One source of truth for platform → label: the GW_PLATFORMS registry (which
  // carries every surfaced platform incl. discord/slack). A platform missing
  // from it falls back to its raw id.
  const spec = GW_PLATFORMS[platform];
  return spec ? t(spec.label) : platform;
}

export interface GwBits {
  text: string;
  cls: "ok" | "warn" | "";
}

export function gwStatusBits(t: TFn, gw: { state?: string } | null | undefined): GwBits {
  const st = (gw && gw.state) || "stopped";
  return {
    text: st === "running" ? t("gw-running") : st === "needs_login" ? t("gw-needs-login") : t("gw-stopped"),
    cls: st === "running" ? "ok" : st === "needs_login" ? "warn" : "",
  };
}
