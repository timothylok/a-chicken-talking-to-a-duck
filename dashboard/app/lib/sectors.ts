import fs from "fs";
import path from "path";

// dashboard/data/sectors.json is written by ops/stock_risk_dashboard.py in
// the same daily run as latest.json and baked in at build time.
export interface SectorRow {
  etf: string;
  name: string;
  group: "Cyclical" | "Defensive";
  rs1m: number | null;
  rs3m: number | null;
  rs6m: number | null;
  favoured: boolean;
  call: "Overweight" | "Neutral" | "Underweight";
}

export interface SectorSnapshot {
  generatedAt: string;
  regime: string | null;
  regimeBasis: string;
  curve10y3m: number | null;
  sectors: SectorRow[];
}

const DATA_PATH = path.join(process.cwd(), "data", "sectors.json");

export function loadSectors(): SectorSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as SectorSnapshot;
  } catch {
    return null;
  }
}
