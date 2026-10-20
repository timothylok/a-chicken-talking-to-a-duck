import fs from "fs";
import path from "path";
import type { TrafficLight } from "./types";

// dashboard/data/ai-bubble.json is written locally by ops/ai_bubble.py
// (called from the daily risk dashboard run) and baked in at build time.
export interface BubbleMetric {
  id: string;
  name: string;
  value: number | null;
  display: string;
  asOf: string | null;
  source: string;
  note: string;
  stale: boolean;
  lo: number;
  hi: number;
  score: number | null;
  light: TrafficLight | null;
}

export interface BubbleGauge {
  id: string;
  name: string;
  score: number | null;
  light: TrafficLight | null;
  metrics: BubbleMetric[];
}

export interface AiBubble {
  generatedAt: string;
  composite: number | null;
  compositeLight: TrafficLight | null;
  coverage: string;
  gauges: BubbleGauge[];
  context: {
    spy?: { asOf: string; mag7Weight: number; top10Weight: number; hhi: number; holdings: number; source: string; stale?: boolean };
    headlines?: { today: number; avg7: number; source: string; stale?: boolean };
    vix?: { level: number | null; percentile10y: number | null; regime: string | null; asOf: string | null; source: string } | null;
  };
}

const DATA_PATH = path.join(process.cwd(), "data", "ai-bubble.json");

export function loadAiBubble(): AiBubble | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as AiBubble;
  } catch {
    return null;
  }
}
