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