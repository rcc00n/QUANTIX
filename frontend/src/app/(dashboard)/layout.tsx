import { requireUser } from "@/lib/api";
import { Sidebar, Logout } from "@/components/navigation";
import { ShieldCheck } from "lucide-react";
export const dynamic = "force-dynamic";
export default async function DashboardLayout({ children }: { children: React.ReactNode }) {
  const user = await requireUser();
  return (
    <div className="application">
      <a className="skip-link" href="#workspace">
        Skip to workspace
      </a>
      <Sidebar />
      <div className="main-column">
        <header className="topbar">
          <div className="topbar-context">
            Workspace <span>/</span> <strong>Intelligence terminal</strong>
          </div>
          <div className="topbar-actions">
            <span className="mode-tag">
              <ShieldCheck size={14} /> PAPER ONLY
            </span>
            <span className="username">{user.username}</span>
            <Logout />
          </div>
        </header>
        <main id="workspace" className="workspace">
          {children}
        </main>
        <footer className="workspace-footer">
          <span>QUANTIX</span>
          <span>All financial activity is simulated</span>
        </footer>
      </div>
    </div>
  );
}
