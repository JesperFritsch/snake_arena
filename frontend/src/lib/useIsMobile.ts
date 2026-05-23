import { useEffect, useState } from "react";

// Must match the editor's mobile breakpoint in index.css.
const MOBILE_QUERY = "(max-width: 760px)";

/** True when the viewport is at or below the mobile breakpoint. Lets callers
 *  render only the layout they need, so stateful components (e.g. the match
 *  WebSocket) aren't mounted twice by CSS-hidden duplicate trees. */
export function useIsMobile(): boolean {
  const [isMobile, setIsMobile] = useState(
    () => typeof window !== "undefined" && window.matchMedia(MOBILE_QUERY).matches,
  );
  useEffect(() => {
    const mq = window.matchMedia(MOBILE_QUERY);
    const onChange = () => setIsMobile(mq.matches);
    mq.addEventListener("change", onChange);
    return () => mq.removeEventListener("change", onChange);
  }, []);
  return isMobile;
}
