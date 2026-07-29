import fs from "fs";
import path from "path";
import type { TickerRow } from "./types";

// dashboard/data/latest.json is written locally by
// ops/stock_risk_dashboard.py, then baked into the site by running
// `vercel --prod` from this directory -- fully static, no live Notion API
// call and no Notion token in Vercel env. Notion is still written to by
// the Python script (a human-browsable historical log, same role it plays
// for every other category), it's just no longer what this page reads.
const DATA_PATH = path.join(process.cwd(), "data", "latest.json");

export function loadDashboardData(): TickerRow[] {
  try {
    const raw = fs.readFileSync(DATA_PATH, "utf-8");
    return JSON.parse(raw) as TickerRow[];
  } catch {
    return [];
  }
}
