import TrafficLight from "./TrafficLight";
import type { Kpi } from "../lib/types";

export default function KpiTile({ name, kpi }: { name: string; kpi: Kpi | undefined }) {
  const title = kpi ? `${name}: ${kpi.detail}` : `${name}: N/A`;
  return (
    <div className="kpi-row" title={title}>
      <TrafficLight light={kpi?.light ?? null} />
      <span className="kpi-name">{name}</span>
      <span className="kpi-score">{kpi?.score != null ? kpi.score.toFixed(2) : "N/A"}</span>
    </div>
  );
}
