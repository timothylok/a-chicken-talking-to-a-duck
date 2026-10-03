import fs from "fs";
import path from "path";

// dashboard/data/reversion.json is written by ops/stock_risk_dashboard.py
// (mean_reversion_scan) in the same daily run, baked in at build time.
export interface ReversionRow {
  ticker: string;
  pe: number | null;
  pe_5y_median: number | null;
  pe_dev: number | null;
  ev_ebitda: number | null;
  group_median_ev_ebitda: number | null;
  ev_dev: number | null;
  vs_sma200: number | null;
  rsi: number | null;
  revenue_growth: number | null;
  score: number;
  extremes: string[];
  verdict: string;
}

export interface ReversionSnapshot {
  generatedAt: string;
  universe: number;
  scored: number;
  top: ReversionRow[];
}

const DATA_PATH = path.join(process.cwd(), "data", "reversion.json");

export function loadReversion(): ReversionSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as ReversionSnapshot;
  } catch {
    return null;
  }
}
