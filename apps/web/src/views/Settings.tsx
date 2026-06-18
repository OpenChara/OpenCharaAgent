/* Settings — the 模型/通用/网关/高级/关于 pane stack, faithful to index.html
 * #view-settings + app.js setupPane (1451) + the settings interactions (1643).
 * The general pane (theme + UI language + tool display) stays live; the model
 * pane is the full provider/key setup; advanced reveals the data path; about
 * shows the version (hub.state).
 *
 * Binding UI rule: every seg control flips immediately (optimistic + persisted);
 * theme/lang persist to the backend (defaults.set) best-effort; the model pane
 * carries its own working states. */

import { useState } from "react";
import { useT, useLang } from "../i18n";
import { useHub } from "../state/hub";
import { applyTheme, currentThemePref, type ThemePref } from "../theme";
import { ModelPane } from "../components/settings/ModelPane";
import { KeysPane } from "../components/settings/KeysPane";
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
  const [pane, setPane] = useState<Pane>("model");
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
      <div className="settings-root">
        <nav className="settings-nav">
          {PANES.map(([key, label]) => (
            <button key={key} className={pane === key ? "on" : ""} onClick={() => setPane(key)}>
              {t(label)}
            </button>
          ))}
        </nav>
        <div className="settings-body">
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
                <div className="seg">
                  {(["system", "light", "dark"] as ThemePref[]).map((p) => (
                    <span key={p} className={theme === p ? "on" : ""} onClick={() => pickTheme(p)}>
                      {t(p === "system" ? "th-system" : p === "light" ? "th-light" : "th-dark")}
                    </span>
                  ))}
                </div>
              </div>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-uilang")}</span>
                  <small>{t("set-uilang-sub")}</small>
                </div>
                <div className="seg">
                  <span className={lang === "zh" ? "on" : ""} onClick={() => pickLang("zh")}>中文</span>
                  <span className={lang === "en" ? "on" : ""} onClick={() => pickLang("en")}>English</span>
                </div>
              </div>
              <div className="set-row">
                <div className="lbl">
                  <span>{t("set-display")}</span>
                  <small>{t("disp-sub")}</small>
                </div>
                <div className="seg">
                  {(["product", "technical"] as Display[]).map((d) => (
                    <span key={d} className={display === d ? "on" : ""} onClick={() => pickDisplay(d)}>
                      {t(d === "product" ? "disp-product" : "disp-technical")}
                    </span>
                  ))}
                </div>
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
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
