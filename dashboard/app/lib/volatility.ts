import fs from "fs";
import path from "path";

// dashboard/data/volatility.json is written by ops/stock_risk_dashboard.py
// (volatility_regime) in the same daily run, baked in at build time.
export interface VolIndex {
  etf: string;
  name: string;
  implied_index: string | null;
  realized_20d: number | null;
  implied: number | null;
  spread: number | null;
}

export interface VolatilitySnapshot {
  generatedAt: string;
  vix: number | null;
  vixPercentile10y: number | null;
  vixRange10y: [number, number] | null;
  termStructure: { index: string; tenor: string; level: number | null }[];
  vixToVix3m: number | null;
  shape: "Contango" | "Backwardation" | null;
  indices: VolIndex[];
  regime: "Calm" | "Normal" | "Elevated" | "Stressed" | null;
  trades: string;
}

const DATA_PATH = path.join(process.cwd(), "data", "volatility.json");

export function loadVolatility(): VolatilitySnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as VolatilitySnapshot;
  } catch {
    return null;
  }
}
