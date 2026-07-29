import TrafficLight from "./TrafficLight";
import KpiTile from "./KpiTile";
import { KPI_ORDER, type TickerRow } from "../lib/types";

export default function TickerCard({ row }: { row: TickerRow }) {
  return (
    <div className="card">
      <div className="card-head">
        <span className="ticker">{row.ticker}</span>
        <span className="composite">
          <TrafficLight light={row.compositeLight} /> {row.composite != null ? `${row.composite}/10` : "N/A"}
        </span>
      </div>
      <p className="coverage">{row.coverage} available</p>
      <p className="summary">{row.summary}</p>
      <div className="kpi-list">
        {KPI_ORDER.map(([key, name]) => (
          <KpiTile key={key} name={name} kpi={row.kpis[key]} />
        ))}
      </div>
      <p className="generated">Generated {row.generatedAt} NZT</p>
    </div>
  );
}
