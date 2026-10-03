import fs from "fs";
import path from "path";

// dashboard/data/factors.json is written by ops/stock_risk_dashboard.py
// (factor_performance) in the same daily run, baked in at build time.
export interface FactorRow {
  etf: string;
  name: string;
  note: string;
  rs1m: number | null;
  rs3m: number | null;
  rs6m: number | null;
  rs12m: number | null;
  status: "In favour" | "Out of favour" | "Mixed";
}

export interface FactorSnapshot {
  generatedAt: string;
  factors: FactorRow[];
  rotation: { pair: string; spread6m: number; leader: string }[];
}

const DATA_PATH = path.join(process.cwd(), "data", "factors.json");

export function loadFactors(): FactorSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as FactorSnapshot;
  } catch {
    return null;
  }
}
