import type { ReversionSnapshot } from "../lib/reversion";

const pct = (v: number | null) => (v == null ? "N/A" : `${v > 0 ? "+" : ""}${v.toFixed(0)}%`);
const tone = (v: number | null) => (v == null ? "" : v > 0 ? "neg" : v < 0 ? "pos" : "");

export default function ReversionTable({ snapshot }: { snapshot: ReversionSnapshot }) {
  return (
    <section className="sectors">
      <h2>Mean reversion scan</h2>
      <p className="tagline">
        The {snapshot.top.length} of {snapshot.scored} scanned stocks (watchlist + peers) furthest from their own
        norms: P/E vs its 5-year median, EV/EBITDA vs its peer group, price vs the 200-day average, and RSI. Negative
        score = cheap or oversold, positive = rich or overbought. A cheap stock is flagged as a possible value trap
        when its revenue is shrinking, or when its low P/E comes from earnings growing far faster than revenue
        (often a one-off gain). A fixed rule, not a recommendation. EV/EBITDA vs peers needs 3+ peers with positive
        EBITDA, otherwise N/A.
      </p>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              <th>Stock</th>
              <th>Score</th>
              <th>P/E vs 5y</th>
              <th>EV/EBITDA vs peers</th>
              <th>vs 200-day</th>
              <th>RSI</th>
            </tr>
          </thead>
          <tbody>
            {snapshot.top.map((r) => (
              <tr key={r.ticker}>
                <td>
                  <span className="etf">{r.ticker}</span>
                  <span className="group">{r.verdict}</span>
                </td>
                <td className={`num ${tone(r.score)}`}>{`${r.score > 0 ? "+" : ""}${r.score.toFixed(2)}`}</td>
                <td className={`num ${tone(r.pe_dev)}`}>{pct(r.pe_dev)}</td>
                <td className={`num ${tone(r.ev_dev)}`}>{pct(r.ev_dev)}</td>
                <td className={`num ${tone(r.vs_sma200)}`}>{pct(r.vs_sma200)}</td>
                <td className="num">{r.rsi == null ? "N/A" : r.rsi.toFixed(0)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="generated">Generated {snapshot.generatedAt} NZT</p>
    </section>
  );
}
