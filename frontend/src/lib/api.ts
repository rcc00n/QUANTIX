import "server-only";
import { cache } from "react";
import { cookies, headers } from "next/headers";
import { redirect } from "next/navigation";
import { safeReturnPath } from "@/lib/return-path";

export type User = { id: string; username: string; mode: "paper" };
export type SystemStatus = {
  mode: "paper";
  stage: number;
  database: string;
  schema_revision: string;
  services: Record<string, string>;
  counts: { exchanges: number; opportunities: number; paper_sessions: number };
};
export type SystemEvent = {
  id: string;
  created_at: string;
  event_type: string;
  severity: string;
  message: string;
};

export async function backendFetch(path: string): Promise<Response> {
  return fetch(`${process.env.BACKEND_URL || "http://127.0.0.1:8000"}/api/v1${path}`, {
    headers: { Cookie: (await cookies()).toString() },
    cache: "no-store",
    signal: AbortSignal.timeout(5000),
  });
}
export const requireUser = cache(async (): Promise<User> => {
  const destination = safeReturnPath((await headers()).get("x-quantix-return-path"));
  const login = (unavailable = false): never => {
    const query = new URLSearchParams();
    if (unavailable) query.set("reason", "unavailable");
    // Ordinary entry keeps its established URL; linked evidence resumes after login.
    if (
      destination &&
      destination !== "/overview" &&
      new URL(destination, "https://quantix.invalid").search
    )
      query.set("next", destination);
    redirect(`/login${query.size ? `?${query}` : ""}`);
  };
  let response: Response;
  try {
    response = await backendFetch("/auth/me");
  } catch {
    return login(true);
  }
  if (response.status === 401) login();
  if (!response.ok) login(true);
  return response.json() as Promise<User>;
});
export async function getStatus(): Promise<SystemStatus | null> {
  try {
    const response = await backendFetch("/system/status");
    return response.ok ? response.json() : null;
  } catch {
    return null;
  }
}
export async function getEvents(): Promise<SystemEvent[]> {
  try {
    const response = await backendFetch("/system/events");
    return response.ok ? response.json() : [];
  } catch {
    return [];
  }
}
