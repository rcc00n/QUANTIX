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