import { useEffect, useState } from "react";

/** True on a phone-width viewport (≤680px — the one breakpoint mobile.css uses).
 *  Reactive: re-renders on resize / orientation change. Kept in sync with the
 *  `@media (max-width:680px)` rules so JS layout decisions never disagree with CSS. */
export const MOBILE_QUERY = "(max-width: 680px)";

export function isMobileViewport(): boolean {
  return typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches;
}

export function useIsMobile(): boolean {
  const [mobile, setMobile] = useState(isMobileViewport);
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const on = () => setMobile(mq.matches);
    mq.addEventListener("change", on);
    return () => mq.removeEventListener("change", on);
  }, []);
  return mobile;
}
