/* Superchat read-state — the client side of `superchat.read`.
 *
 * The chat treats "opened" as "read" (点进去就是已读). The server write is what
 * clears the board's unread mark, but the CHAT's own read watermark must never
 * fail closed: a dropped `superchat.read` used to leave superReadTs at 0 and
 * render the entire history unread forever. So the view sets the LOCAL
 * watermark optimistically and uses this helper to persist it server-side with
 * a few retries; total failure degrades to "read locally" (null), never to
 * "all unread". */

export interface ReadCaller {
  call<T = unknown>(method: string, params?: Record<string, unknown>, timeoutMs?: number): Promise<T>;
}

/** Fold a server-confirmed read_ts into the local watermark. The watermark only
 *  ever advances; a missing/invalid server value falls back to the attempted ts. */
export function nextReadTs(prev: number, attempted: number, server?: unknown): number {
  const s = Number(server);
  return Math.max(prev, Number.isFinite(s) && s > 0 ? s : attempted);
}

const sleepMs = (ms: number) => new Promise<void>((res) => setTimeout(res, ms));

/** Persist the read watermark server-side, retrying transient failures.
 *  Resolves the server's read_ts (or the attempted ts if the server omits it)
 *  on success; resolves null when every attempt failed or the caller aborted —
 *  it NEVER rejects, so the caller's optimistic local watermark always stands. */
export async function markSuperchatRead(
  hub: ReadCaller,
  name: string,
  ts: number,
  opts: {
    retries?: number;
    delayMs?: number;
    /** cooperative cancel — set aborted=true (e.g. on unmount) to stop retrying. */
    signal?: { aborted: boolean };
    sleep?: (ms: number) => Promise<void>;
  } = {},
): Promise<number | null> {
  const { retries = 2, delayMs = 4000, signal, sleep = sleepMs } = opts;
  for (let attempt = 0; attempt <= retries; attempt++) {
    if (signal?.aborted) return null;
    try {
      const r = await hub.call<{ read_ts?: number }>("superchat.read", { name, ts }, 10000);
      const server = Number(r?.read_ts);
      return Number.isFinite(server) && server > 0 ? server : ts;
    } catch {
      if (attempt === retries || signal?.aborted) return null;
      await sleep(delayMs);
    }
  }
  return null;
}
