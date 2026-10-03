import fs from "fs";
import path from "path";

// dashboard/data/drawdowns.json is written by ops/stock_risk_dashboard.py
// (drawdown_analogues) in the same daily run, baked in at build time.
export interface EpisodeResult {
  drawdown: number;
  isEstimate: boolean;
  peak?: string;
  trough?: string;
  recovered?: string | null;
  weeksToRecover?: number | null;
}

export interface Episode {
  id: string;
  name: string;
  start: string;
  end: string;
  driver: string;
  hedge: string;
}

export interface DrawdownSnapshot {
  generatedAt: string;
  episodes: Episode[];
  spy: Record<string, EpisodeResult | null>;
  rows: { ticker: string; beta: number | null; betaDays: number; episodes: Record<string, EpisodeResult | null> }[];
}

const DATA_PATH = path.join(process.cwd(), "data", "drawdowns.json");

export function loadDrawdowns(): DrawdownSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as DrawdownSnapshot;
  } catch {
    return null;
  }
}
