export type TrafficLight = "Green" | "Amber" | "Red";

export interface Kpi {
  score: number | null;
  light: TrafficLight | null;
  detail: string;
}

// Matches ops/stock_risk_dashboard.py's KPI_ORDER exactly.
export const KPI_ORDER: [string, string][] = [
  ["kpi1", "Fundamental Health"],
  ["kpi2", "Earnings Quality & Surprise Stability"],
  ["kpi3", "Revenue & Margin Trajectory"],
  ["kpi4", "Balance Sheet & Debt Maturity Risk"],
  ["kpi5", "Cash Flow & Dividend Coverage"],
  ["kpi6", "Valuation vs History & Peers"],
  ["kpi7", "Technical Trend & Momentum"],
  ["kpi8", "Volatility & Event Risk"],
  ["kpi9", "Market & Peer-Relative Pressure (approx.)"],
  ["kpi10", "Red Flags & Accounting Risk"],
];

export interface TickerRow {
  ticker: string;
  date: string;
  composite: number | null;
  compositeLight: TrafficLight | null;
  coverage: string;
  kpis: Record<string, Kpi>;
  summary: string;
  generatedAt: string;
}
