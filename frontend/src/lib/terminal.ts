"use client";

import { useCallback, useEffect, useRef, useState } from "react";

export async function terminalFetch<T>(
  path: string,
  signal?: AbortSignal,
  init?: RequestInit,
): Promise<T> {
  const response = await fetch(`/api${path}`, {
    ...init,
    signal,
    cache: "no-store",
    headers: { "Content-Type": "application/json", ...init?.headers },
  });
  if (response.status === 401) {
    window.location.assign("/login");
    throw new Error("Session expired. Sign in again.");
  }
  const value = await response.json().catch(() => null);
  if (!response.ok) {
    const detail = value?.detail;
    throw new Error(
      typeof detail === "string"
        ? detail
        : Array.isArray(detail)
          ? detail
              .map(
                (x: { loc?: string[]; msg?: string }) =>
                  `${x.loc?.slice(1).join(".") || "Setting"}: ${x.msg || "invalid"}`,
              )
              .join(" · ")
          : "Data unavailable. Retained values may be stale.",
    );
  }
  return value as T;
}

/** One request at a time, hidden-tab pause and bounded exponential reconnect. */
export function useLive<T>(path: string | null, interval = 15000) {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [revision, setRevision] = useState(0);
  const [received, setReceived] = useState<string | null>(null);
  const last = useRef(0);
  const previousPath = useRef<string | null>(null);
  const refresh = useCallback(() => setRevision((x) => x + 1), []);
  useEffect(() => {
    if (!path) {
      setLoading(false);
      return;
    }
    let stopped = false,
      running = false,
      failures = 0;
    let timer: ReturnType<typeof setTimeout>;
    let controller: AbortController | null = null;
    if (previousPath.current !== path) {
      setData(null);
      setError("");
      previousPath.current = path;
    }
    setLoading(true);
    const update = async () => {
      if (stopped || running) return;
      if (document.visibilityState === "hidden") {
        timer = setTimeout(update, interval);
        return;
      }
      running = true;
      controller = new AbortController();
      const requestController = controller;
      const deadline = setTimeout(() => requestController.abort(), 12000);
      try {
        const value = await terminalFetch<T>(path, requestController.signal);
        if (!stopped) {
          setData(value);
          setError("");
          failures = 0;
          last.current = performance.now();
          setReceived(new Date().toISOString());
        }
      } catch (reason) {
        if (!stopped) {
          failures += 1;
          setError(reason instanceof Error ? reason.message : "Connection unavailable");
        }
      } finally {
        clearTimeout(deadline);
        running = false;
        if (!stopped) {
          setLoading(false);
          timer = setTimeout(update, Math.min(60000, interval * 2 ** Math.min(failures, 2)));
        }
      }
    };
    const visible = () => {
      if (document.visibilityState === "visible" && !running) {
        clearTimeout(timer);
        void update();
      }
    };
    void update();
    document.addEventListener("visibilitychange", visible);
    return () => {
      stopped = true;
      controller?.abort();
      clearTimeout(timer);
      document.removeEventListener("visibilitychange", visible);
    };
  }, [path, interval, revision]);
  return {
    data: previousPath.current === path ? data : null,
    error,
    loading,
    received,
    refresh,
    stale: !!error || (!!last.current && performance.now() - last.current > interval * 3),
  };
}

export function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function asRecords(value: unknown): Record<string, unknown>[] {
  return Array.isArray(value) ? value.map(asRecord) : [];
}
export function amount(value: unknown): string | null {
  return typeof value === "string" || typeof value === "number" ? String(value) : null;
}
export function text(value: unknown, fallback = "Unavailable"): string {
  return typeof value === "string" || typeof value === "number" ? String(value) : fallback;
}