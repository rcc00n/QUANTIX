import type { Metadata } from "next";
import "./globals.css";
export const metadata: Metadata = {
  title: "QUANTIX",
  description: "Paper-only cryptocurrency arbitrage intelligence workspace",
  icons: { icon: "/brand/quantix-icon-32.png", apple: "/brand/quantix-icon-192.png" },
  robots: { index: false, follow: false },
};
export default function RootLayout({ children }: Readonly<{ children: React.ReactNode }>) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
