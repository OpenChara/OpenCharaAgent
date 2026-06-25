/* Settings — the 模型/通用/网关/高级/关于 pane stack, faithful to index.html
 * #view-settings + app.js setupPane (1451) + the settings interactions (1643).
 * The general pane (theme + UI language + tool display) stays live; the model
 * pane is the full provider/key setup; advanced reveals the data path; about
 * shows the version (hub.state).
 *
 * Binding UI rule: every seg control flips immediately (optimistic + persisted);
 * theme/lang persist to the backend (defaults.set) best-effort; the model pane
 * carries its own working states. */

import { useEffect, useState } from "react";
import { navTo } from "../hooks/useHashRoute";
import { useIsMobile } from "../hooks/useIsMobile";
import { useT, useLang } from "../i18n";
import { useHub } from "../state/hub";
import { Segmented } from "../components/ui/Segmented";
import { applyTheme, currentThemePref, type ThemePref } from "../theme";
import { ModelPane } from "../components/settings/ModelPane";
import { KeysPane } from "../components/settings/KeysPane";
import { UpdatePane } from "../components/settings/UpdatePane";
import { deckToast } from "../components/ui/deckToast";
import { rpcErrText } from "../lib/status";

type Pane = "model" | "keys" | "general" | "gateway" | "advanced" | "about";
type Display = "product" | "technical";

const PANES: ReadonlyArray<readonly [Pane, string]> = [
  ["model", "set-model"],
  ["keys", "set-keys"],
  ["general", "set-general"],
  ["gateway", "set-gateway"],
  ["advanced", "set-advanced"],
  ["about", "set-about"],
] as const;

// Deep-link the pane via the hash sub-segment (`#/settings/keys`), so e.g. the
// Model pane's "add a key" prompt can jump straight to the Providers pane.
function paneFromHash(): Pane {
  const seg = (location.hash.split("/")[2] || "") as Pane;
  return PANES.some(([k]) => k === seg) ? seg : "model";
}

// The hash sub-segment, but ONLY when it names a real pane — so a bare or malformed
// `#/settings(/bogus)` collapses to "" (the mobile MENU), never a detail view of the
// fallback pane.
function segFromHash(): string {
  const seg = location.hash.split("/")[2] || "";
  return PANES.some(([k]) => k === seg) ? seg : "";
}

function currentDisplay(): Display {
  try {
    return localStorage.getItem("lm-display") === "technical" ? "technical" : "product";
  } catch {
    return "product";
  }
}

export function Settings() {
  const t = useT();
  const { lang, setLang } = useLang();
  const { hub, snapshot } = useHub();
  const isMobile = useIsMobile();
  const [pane, setPane] = useState<Pane>(paneFromHash);
  // The hash sub-segment, tracked so mobile can show a drill-down: bare `#/settings`
  // = the section MENU on a phone (desktop still defaults to the model pane).
  const [seg, setSeg] = useState(segFromHash);
  useEffect(() => {
    const on = () => {
      setPane(paneFromHash());
      setSeg(segFromHash());
    };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  // Mobile is a 2-level drill-down: "menu" (the section list) → "detail" (one pane,
  // with a back button). Desktop keeps the side-nav + pane side by side.
  const mview = isMobile ? (seg ? "detail" : "menu") : "";
  const [theme, setTheme] = useState<ThemePref>(currentThemePref());
  const [display, setDisplay] = useState<Display>(currentDisplay());

  const home = String((snapshot?.home as string) || "");
  const version = String((snapshot?.version as string) || "");

  const pickTheme = (p: ThemePref) => {
    applyTheme(p); // optimistic + persisted to localStorage at once
    setTheme(p);
    hub.call("defaults.set", { ui_theme: p }).catch(() => {});
  };

  const pickLang = (code: "zh" | "en") => {
    setLang(code); // optimistic + persisted + reactive re-render
    if (hub.sock.open) hub.call("defaults.set", { ui_lang: code }).catch(() => {});
  };

  const pickDisplay = (d: Display) => {
    setDisplay(d); // optimistic
    try {
      localStorage.setItem("lm-display", d);
    } catch {
      /* private */
    }
    document.body.classList.toggle("technical", d === "technical");
  };

  const revealHome = () => {
    if (!home) return;
    hub.call("open.path", { path: home, reveal: true }).catch((e) =>
      deckToast(rpcErrText(t, e as { message?: string }), true),
    );
  };

  return (
    <div className="view active" id="view-settings">
      <div className="toolbar">
        <h1>{t("nav-settings")}</h1>
      </div>
      <div className="settings-root" data-mview={mview || undefined}>
        <nav className="settings-nav">
          {PANES.map(([key, label]) => (
            <button
              key={key}
              className={pane === key ? "on" : ""}
              onClick={() => { setPane(key); navTo(`#/settings/${key}`); }}
            >
              {t(label)}
            </button>
          ))}
        </nav>
        <div className="settings-body">
          {isMobile && seg && (
            <button className="m-back" onClick={() => navTo("#/settings")}>
              ‹ {t("nav-settings")}
            </button>
          )}
          {pane === "model" && <ModelPane />}

          {/* #5 — the unified Keys surface: BOTH the saved text/provider keys
              and the global image-gen key/model, one visual language. */}
          {pane === "keys" && <KeysPane />}

          {pane === "general" && (
            <div className="settings-pane on">
              <h2>{t("set-general")}</h2>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-appearance")}</span>
                </div>
                <Segmented
                  ariaLabel={t("set-appearance")}
                  value={theme}
                  options={(["system", "light", "dark"] as ThemePref[]).map((p) => ({
                    value: p, label: t(p === "system" ? "th-system" : p === "light" ? "th-light" : "th-dark"),
                  }))}
                  onChange={pickTheme}
                />
              </div>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-uilang")}</span>
                  <small>{t("set-uilang-sub")}</small>
                </div>
                <Segmented
                  ariaLabel={t("set-uilang")}
                  value={lang}
                  options={[{ value: "zh", label: "中文" }, { value: "en", label: "English" }]}
                  onChange={pickLang}
                />
              </div>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-display")}</span>
                  <small>{t("disp-sub")}</small>
                </div>
                <Segmented
                  ariaLabel={t("set-display")}
                  value={display}
                  options={(["product", "technical"] as Display[]).map((d) => ({
                    value: d, label: t(d === "product" ? "disp-product" : "disp-technical"),
                  }))}
                  onChange={pickDisplay}
                />
              </div>
            </div>
          )}

          {pane === "gateway" && (
            <div className="settings-pane on">
              <h2>{t("set-gateway")}</h2>
              <div className="placeholder-pane">{t("set-gw-sub")}</div>
            </div>
          )}

          {pane === "advanced" && (
            <div className="settings-pane on">
              <h2>{t("set-advanced")}</h2>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-data")}</span>
                  <small>{home}</small>
                </div>
                <button className="btn soft" onClick={revealHome}>
                  {t("set-reveal")}
                </button>
              </div>
            </div>
          )}

          {pane === "about" && (
            <div className="settings-pane on">
              <h2>LunaMoth</h2>
              <div className="about-block">
                <div>{version ? `LunaMoth v${version}` : "LunaMoth"}</div>
                <div>{t("about-text")}</div>
              </div>
              <UpdatePane />
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
