"use client";

import { useEffect, useState } from "react";
import { age } from "@/lib/market-data";

/** Advance a server-observed age using monotonic browser elapsed time, never Date.now. */
export function MarketAge({
  seconds,
  generatedAt,
  trusted = true,
}: {
  seconds: number | null;
  generatedAt?: string;
  trusted?: boolean;
}) {
  const [elapsed, setElapsed] = useState(0);
  useEffect(() => {
    const start = performance.now();
    setElapsed(0);
    const timer = setInterval(() => {
      if (document.visibilityState === "visible") setElapsed((performance.now() - start) / 1000);
    }, 1000);
    return () => clearInterval(timer);
  }, [seconds, generatedAt]);
  return (
    <span title="Collector receipt age at status generation + browser monotonic elapsed; network transit is not included">
      {trusted
        ? age(
            seconds !== null && Number.isFinite(seconds) && seconds >= 0 ? seconds + elapsed : null,
          )
        : "Clock untrusted"}
    </span>
  );
}
