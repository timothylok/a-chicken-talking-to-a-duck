import type { Metadata } from "next";
import "./globals.css";

export const metadata: Metadata = {
  title: "Mag 7 Risk Dashboard",
  description: "10-KPI composite risk score per ticker, computed locally and read from Notion.",
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="en">
      <body>{children}</body>
    </html>
  );
}
