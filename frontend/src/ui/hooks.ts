import { useEffect, useState } from "react";

/** The clock, for "for 12 s" texts. */
export function useNow(ms = 1000): number {
  const [now, set] = useState(Date.now);
  useEffect(() => {
    const t = setInterval(() => set(Date.now()), ms);
    return () => clearInterval(t);
  }, [ms]);
  return now;
}
