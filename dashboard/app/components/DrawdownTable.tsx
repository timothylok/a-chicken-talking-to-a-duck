import TickerLogo from "./TickerLogo";
import type { DrawdownSnapshot, EpisodeResult } from "../lib/drawdowns";

function Cell({ r }: { r: EpisodeResult | null | undefined }) {
  if (!r) return <td className="num">N/A</td>;
  const recovery = r.isEstimate
    ? "estimate"
    : r.weeksToRecover == null
      ? "not recovered"
      : `back in ${r.weeksToRecover}w`;
  return (
    <td className={`num neg${r.isEstimate ? " est" : ""}`}>
      {r.isEstimate ? "~" : ""}
      {r.drawdown.toFixed(0)}%<span className="group">{recovery}</span>
    </td>
  );
}

export default function DrawdownTable({ snapshot }: { snapshot: DrawdownSnapshot }) {
  return (
    <section className="sectors">
      <h2>Drawdown analogues</h2>
      <p className="tagline">
        Each stock&apos;s actual peak-to-trough fall in four sell-offs, and the weeks from the bottom back to that
        peak, from weekly closes (split- but not dividend-adjusted). A stock not yet listed shows an estimate
        (~) from its 2-year beta to SPY. History, not a forecast.
      </p>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              <th>Stock</th>
              {snapshot.episodes.map((ep) => (
                <th key={ep.id} className="num">{ep.name}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              <td>
                <span className="etf">SPY</span>
                <span className="group">S&amp;P 500</span>
              </td>
              {snapshot.episodes.map((ep) => (
                <Cell key={ep.id} r={snapshot.spy[ep.id]} />
              ))}
            </tr>
            {snapshot.rows.map((row) => (
              <tr key={row.ticker}>
                <td>
                  <span className="ticker-id">
                    <TickerLogo ticker={row.ticker} />
                    <span className="etf">{row.ticker}</span>
                  </span>
                  <span className="group">
                    beta {row.beta == null ? "N/A" : row.beta.toFixed(2)}
                    {row.betaDays < 250 ? ` (${row.betaDays} days)` : ""}
                  </span>
                </td>
                {snapshot.episodes.map((ep) => (
                  <Cell key={ep.id} r={row.episodes[ep.id]} />
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="episode-notes">
        {snapshot.episodes.map((ep) => (
          <li key={ep.id}>
            <strong>{ep.name}.</strong> {ep.driver} <span className="tagline">Hedge that worked: {ep.hedge}</span>
          </li>
        ))}
      </ul>
      <p className="generated">Generated {snapshot.generatedAt} NZT</p>
    </section>
  );
}
