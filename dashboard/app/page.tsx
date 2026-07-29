import TickerCard from "./components/TickerCard";
import { loadDashboardData } from "./lib/data";

export default function Dashboard() {
  const rows = loadDashboardData();

  if (rows.length === 0) {
    return (
      <main>
        <h1>Mag 7 Risk Dashboard</h1>
        <div className="error">
          No dashboard data yet -- run <code>python ops/stock_risk_dashboard.py</code> locally, then{" "}
          <code>vercel --prod</code> from this directory to publish it.
        </div>
      </main>
    );
  }

  const latest = rows.reduce((max, r) => (r.generatedAt > max ? r.generatedAt : max), rows[0].generatedAt);

  return (
    <main>
      <h1>Mag 7 Risk Dashboard</h1>
      <p className="tagline">
        10 KPIs, computed by a local pipeline from SEC/Yahoo data (no cloud AI). Static snapshot published
        {" "}{latest} NZT -- republish by running the generator locally, then <code>vercel --prod</code>.
      </p>
      <div className="grid">
        {rows.map((row) => (
          <TickerCard key={row.ticker} row={row} />
        ))}
      </div>
      <footer>
        KPI 9 ("Market &amp; Peer-Relative Pressure") is an approximation built from relative strength vs
        SPY and peer-multiple spread -- not real sector or macro data.
      </footer>
    </main>
  );
}
