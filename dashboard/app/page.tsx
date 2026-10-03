import TickerCard from "./components/TickerCard";
import TickerLogo from "./components/TickerLogo";
import TrafficLight from "./components/TrafficLight";
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
  // Highest composite (lowest risk) first; unscored tickers last, alphabetically.
  const ranked = [...rows].sort(
    (a, b) => (b.composite ?? -1) - (a.composite ?? -1) || a.ticker.localeCompare(b.ticker),
  );
  const rankOf = (r: (typeof rows)[number]) => (r.composite != null ? ranked.indexOf(r) + 1 : null);

  return (
    <main>
      <a className="back-link" href="https://a-chicken-talking-to-a-duck.vercel.app/">← Home</a>
      <h1>Mag 7 Risk Dashboard</h1>
      <p className="tagline">
        10 KPIs scored from SEC/Yahoo data by a local pipeline; only the one-paragraph summaries are written by
        Cloudflare Workers AI. Ranked by composite score, higher = lower risk. Static snapshot published {latest} NZT.
      </p>
      <ol className="ranking">
        {ranked.map((row) => (
          <li key={row.ticker}>
            <a href={`#${row.ticker}`}>
              <span className="rank">{rankOf(row) != null ? `#${rankOf(row)}` : "--"}</span>
              <TickerLogo ticker={row.ticker} />
              <span className="ticker">{row.ticker}</span>
              <span className="rank-score">
                <TrafficLight light={row.compositeLight} />
                {row.composite != null ? row.composite.toFixed(1) : "N/A"}
              </span>
            </a>
          </li>
        ))}
      </ol>
      <div className="grid">
        {ranked.map((row) => (
          <TickerCard key={row.ticker} row={row} rank={rankOf(row)} />
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
