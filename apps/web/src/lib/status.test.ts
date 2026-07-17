import { describe, it, expect } from "vitest";
import { translate, type TFn, type Lang } from "../i18n";
import {
  statusOf,
  lifeText,
  lifeWord,
  boardErrText,
  shortErr,
  errText,
  rpcErrText,
  type SessionSnapshot,
} from "./status";

const tFor = (lang: Lang): TFn => (key, vars) => translate(lang, key, vars);
const en = tFor("en");
const NOW = 1_000_000 * 1000; // fixed ms

describe("errText / rpcErrText", () => {
  it("maps error kinds to copy", () => {
    expect(errText(en, { kind: "auth" })).toBe("Invalid key or no access");
    expect(errText(en, { kind: "credit" })).toBe("Out of credit");
    expect(errText(en, { kind: "unknown" })).toBe("Could not reach the model");
    expect(errText(en, null)).toBe("Could not reach the model");
  });
  it("rpcErrText prefers data.kind, then message", () => {
    expect(rpcErrText(en, { data: { kind: "auth", detail: "401" } })).toBe(
      "Invalid key or no access · 401",
    );
    expect(rpcErrText(en, { message: "boom" })).toBe("boom");
    expect(rpcErrText(en, null)).toBe("Could not reach the model");
  });
});

describe("shortErr", () => {
  it("classifies a raw line by substring", () => {
    expect(shortErr(en, "insufficient balance")).toBe("Out of credit");
    expect(shortErr(en, "HTTP 401 Unauthorized")).toBe("Invalid key or no access");
    expect(shortErr(en, "connect ETIMEDOUT")).toBe("Network unreachable");
    expect(shortErr(en, "weird thing")).toBe("Could not reach the model");
  });
});

describe("boardErrText", () => {
  it("maps error_kind, then short-classifies the raw error", () => {
    expect(boardErrText(en, { error_kind: "auth" })).toBe(
      "Model key is invalid — update it in Settings",
    );
    expect(boardErrText(en, { error_kind: "credit" })).toBe("Out of credit");
    expect(boardErrText(en, { error: "401 nope" })).toBe("Invalid key or no access");
    expect(boardErrText(en, {})).toBe("Something went wrong");
  });
});

describe("lifeText", () => {
  it("renders each life state factually", () => {
    expect(lifeText(en, null, NOW)).toBe("");
    expect(lifeText(en, { state: "working" }, NOW)).toBe("Working on its own");
    expect(lifeText(en, { state: "waiting" }, NOW)).toBe("Waiting for you");
    expect(lifeText(en, { state: "resting" }, NOW)).toBe("Resting");
    expect(lifeText(en, { state: "anything" }, NOW)).toBe("Idle");
  });
  it("idle_countdown shows ~N min when next_cycle_at is ≥1 min out", () => {
    const next = NOW / 1000 + 300; // +5 min
    expect(lifeText(en, { state: "idle_countdown", next_cycle_at: next }, NOW)).toBe(
      "Idle · next self-paced turn in ~5 min",
    );
    expect(lifeText(en, { state: "idle_countdown", next_cycle_at: NOW / 1000 + 10 }, NOW)).toBe(
      "Idle",
    );
    expect(lifeText(en, { state: "idle_countdown" }, NOW)).toBe("Idle");
  });
  it("resting with rest_until shows the clock; backoff appends detail", () => {
    expect(lifeText(en, { state: "resting", rest_until: 1_700_000_000 }, NOW)).toMatch(
      /^Resting until \d{2}:\d{2}$/,
    );
    expect(lifeText(en, { state: "backoff", detail: "x5" }, NOW)).toBe("Idle backoff · x5");
    expect(lifeText(en, { state: "backoff" }, NOW)).toBe("Idle backoff");
  });
});

describe("lifeWord (chat header)", () => {
  it("adds the back-to-work countdown while waiting", () => {
    const until = NOW / 1000 + 180; // +3 min
    expect(lifeWord(en, { state: "waiting", engaged_until: until }, NOW)).toBe(
      "Waiting for you · back to its own work in ~3 min",
    );
  });
  it("falls back to lifeText when not waiting / window already elapsed", () => {
    expect(lifeWord(en, { state: "working" }, NOW)).toBe("Working on its own");
    // engaged_until at/before now → ceil ≤ 0 → no countdown, plain waiting line.
    expect(lifeWord(en, { state: "waiting", engaged_until: NOW / 1000 }, NOW)).toBe(
      "Waiting for you",
    );
    expect(lifeWord(en, { state: "waiting" }, NOW)).toBe("Waiting for you");
  });
});

describe("statusOf", () => {
  const cases: Array<[string, SessionSnapshot, string, string]> = [
    ["new", { status: "new" }, "off", "Not set up yet"],
    ["crashed", { status: "crashed", error: "kaboom" }, "err", "kaboom"],
    ["paused", { status: "running", paused: true }, "off", "Autonomy off"],
    ["auth error", { status: "running", error: "x", error_kind: "auth" }, "err", "Something went wrong"],
  ];
  it.each(cases)("%s", (_label, s, dot, line) => {
    const out = statusOf(en, s, NOW);
    expect(out.dot).toBe(dot);
    expect(out.line).toBe(line);
  });

  it("idle but autonomous (not paused) reads as on/live", () => {
    // on/off == autonomy, not process state: a non-paused chara is "on" even
    // when its child isn't resident, so the board shows it living its day.
    const out = statusOf(en, { status: "idle", last_active: NOW / 1000 - 600 }, NOW);
    expect(out.dot).toBe("live");
    expect(out.line).toBe("Living its own day");
  });

  it("never headlines the opening line — a greeting-only chara has no last_message", () => {
    // The backend excludes the card greeting from last_message (a chara that has
    // only greeted has no conversation yet), so the row arrives with
    // last_message null and must read as living its day, cls not "msg".
    const out = statusOf(en, { status: "running", last_message: null, life: { state: "working" } }, NOW);
    expect(out.line).toBe("Working on its own");
    expect(out.cls).toBe("");
  });

  it("falls through to the life line, then the living-its-day default", () => {
    expect(statusOf(en, { status: "running", life: { state: "working" } }, NOW).line).toBe(
      "Working on its own",
    );
    expect(statusOf(en, { status: "running" }, NOW).line).toBe("Living its own day");
  });

  it("headlines the last conversation message over life (chara side)", () => {
    const out = statusOf(
      en,
      {
        status: "running",
        last_message: { text: "hi from me", ts: 100, role: "chara" },
        life: { state: "working" },
      },
      NOW,
    );
    expect(out.line).toBe("hi from me");
    expect(out.cls).toBe("msg");
    expect(out.dot).toBe("live");
  });

  it("a user turn is a headline too (WeChat semantics: last message, either side)", () => {
    const out = statusOf(
      en,
      { status: "running", last_message: { text: "你在吗", ts: 200, role: "user" } },
      NOW,
    );
    expect(out.line).toBe("你在吗");
    expect(out.cls).toBe("msg");
  });

  it("a paused chara still shows its last message, but the dot is off", () => {
    const out = statusOf(
      en,
      { status: "running", paused: true, last_message: { text: "later", ts: 1, role: "chara" } },
      NOW,
    );
    expect(out.line).toBe("later");
    expect(out.dot).toBe("off");
  });
});
