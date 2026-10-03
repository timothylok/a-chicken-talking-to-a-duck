import TickerCard from "./components/TickerCard";
import SectorTable from "./components/SectorTable";
import { loadDashboardData } from "./lib/data";
import { loadSectors } from "./lib/sectors";
import ReversionTable from "./components/ReversionTable";
import { loadReversion } from "./lib/reversion";
import VolatilityPanel from "./components/VolatilityPanel";
import { loadVolatility } from "./lib/volatility";

export default function Dashboard() {
  const rows = loadDashboardData();
  const sectors = loadSectors();
  const reversion = loadReversion();
  const volatility = loadVolatility();

  if (rows.length === 0) {
    return (
      <main>
        <a className="back-link" href="https://a-chicken-talking-to-a-duck.vercel.app/">← Home</a>
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
      <a className="back-link" href="https://a-chicken-talking-to-a-duck.vercel.app/">← Home</a>
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
      {sectors && <SectorTable snapshot={sectors} />}
      {reversion && <ReversionTable snapshot={reversion} />}
      {volatility && <VolatilityPanel snapshot={volatility} />}
      <footer>
        KPI 9 ("Market &amp; Sector-Relative Pressure") blends each stock&apos;s 3-month return vs SPY with its
        sector ETF&apos;s. The sector regime is read from market prices and the Treasury yield curve only -- no
        economic data feed.
      </footer>
    </main>
  );
}
