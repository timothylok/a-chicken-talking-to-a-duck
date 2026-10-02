import fs from "fs";
import path from "path";

// dashboard/data/ai-digest.json is written locally by ops/ai_digest.py and
// baked in at build time, same as latest.json for the risk page.
export interface DigestItem {
  title: string;
  source: string;
  url: string;
  summary: string;
  timestamp: string;
}

export interface AiDigest {
  updated_at: string;
  stale: string[];
  top_ai_news: DigestItem[];
  top_ai_hiccups: DigestItem[];
}

const DATA_PATH = path.join(process.cwd(), "data", "ai-digest.json");

export function loadAiDigest(): AiDigest | null {
  try {
    return JSON.parse(fs.readFileSync(DATA_PATH, "utf-8")) as AiDigest;
  } catch {
    return null;
  }
}
