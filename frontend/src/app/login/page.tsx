import { Brand } from "@/components/brand";
import { LoginForm } from "./form";
import { ShieldCheck } from "lucide-react";
import { safeReturnPath } from "@/lib/return-path";
export default async function LoginPage({
  searchParams,
}: {
  searchParams: Promise<{ reason?: string; next?: string }>;
}) {
  const { reason, next } = await searchParams;
  return (
    <main className="login-page">
      <div className="login-content">
        <Brand lockup />
        <span className="eyebrow">PRIVATE WORKSPACE</span>
        <h1>Open your terminal.</h1>
        <p>Sign in to your arbitrage intelligence workspace.</p>
        {reason === "unavailable" && (
          <div className="notice" role="alert">
            The backend is unavailable. Restore the backend and database, then sign in again.
          </div>
        )}
        <LoginForm returnTo={safeReturnPath(next) || "/overview"} />
        <div className="login-note">
          <ShieldCheck size={17} />
          <span>Paper trading environment. Real exchange orders are disabled.</span>
        </div>
      </div>
      <footer>
        QUANTIX <span>Market intelligence · Paper only</span>
      </footer>
    </main>
  );
}
