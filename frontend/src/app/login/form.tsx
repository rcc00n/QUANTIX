"use client";
import { useState, type FormEvent } from "react";
import { useRouter } from "next/navigation";
import { ArrowRight, LoaderCircle } from "lucide-react";
import { Button } from "@/components/ui/button";
export function LoginForm({ returnTo = "/overview" }: { returnTo?: string }) {
  const router = useRouter();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  async function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setError("");
    const data = new FormData(event.currentTarget);
    try {
      const response = await fetch("/api/auth/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ username: data.get("username"), password: data.get("password") }),
      });
      if (!response.ok) {
        const payload = await response.json();
        throw new Error(typeof payload.detail === "string" ? payload.detail : "Sign in failed");
      }
      router.replace(returnTo);
      router.refresh();
    } catch (cause) {
      setError(cause instanceof Error ? cause.message : "Unable to reach the backend");
      setBusy(false);
    }
  }
  return (
    <form onSubmit={submit} className="login-form">
      <label htmlFor="username">Username</label>
      <input
        id="username"
        name="username"
        autoComplete="username"
        required
        maxLength={80}
        placeholder="Your username"
      />
      <label htmlFor="password">Password</label>
      <input
        id="password"
        name="password"
        type="password"
        autoComplete="current-password"
        required
        maxLength={256}
        placeholder="Your password"
      />
      {error && (
        <p className="form-error" role="alert">
          {error}
        </p>
      )}
      <Button type="submit" disabled={busy}>
        {busy ? (
          <>
            <LoaderCircle className="spin" size={17} /> Signing in…
          </>
        ) : (
          <>
            Enter workspace <ArrowRight size={17} />
          </>
        )}
      </Button>
    </form>
  );
}
