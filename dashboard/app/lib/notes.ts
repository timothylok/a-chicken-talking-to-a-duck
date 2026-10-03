import fs from "fs";
import path from "path";

// dashboard/data/notes.json is written by ops/stock_risk_dashboard.py
// (research_notes) in the same daily run, baked in at build time.
export interface ResearchNote {
  ticker: string;
  composite: number;
  thesis: string;
  key_points: string[];
  risks: string[];
  valuation: string;
  action: "Buy" | "Hold" | "Sell";
  conviction: "Low" | "Medium" | "High";
  bull: string[];
  bear: string[];
  debate_verdict: string;
  premortem: { reason: string; probability: number; warning_sign: string }[];
  // Numbers the model wrote that are not in its fact sheet (kept after a retry).
  unverified: number[];
}

export interface NotesSnapshot {
  generatedAt: string;
  model: string;
  notes: ResearchNote[];
}

const DATA_PATH = path.join(process.cwd(), "data", "notes.json");

export function loadNotes(): NotesSnapshot | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as NotesSnapshot;
  } catch {
    return null;
  }
}
