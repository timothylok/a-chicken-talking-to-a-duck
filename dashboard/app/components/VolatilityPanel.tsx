import type { VolatilitySnapshot } from "../lib/volatility";

const num = (v: number | null, d = 1) => (v == null ? "N/A" : v.toFixed(d));

export default function VolatilityPanel({ snapshot: v }: { snapshot: VolatilitySnapshot }) {
  return (
    <section className="sectors">
      <h2>Volatility regime</h2>
      <p className="tagline">
        Regime: <strong>{v.regime ?? "N/A"}</strong> -- VIX {num(v.vix, 2)}, {num(v.vixPercentile10y, 0)}th percentile
        of its 10-year range{v.vixRange10y ? ` (${v.vixRange10y[0]}-${v.vixRange10y[1]})` : ""}; term structure in{" "}
        {v.shape?.toLowerCase() ?? "N/A"} (VIX / VIX3M {num(v.vixToVix3m, 2)}). Bands: under the 25th percentile
        calm, 25th-75th normal, above elevated; backwardation reads as stressed at any level.
      </p>
      <p className="tagline">{v.trades} Textbook pairings for this regime, not recommendations.</p>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              {v.termStructure.map((t) => (
                <th key={t.index} className="num">{t.tenor}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            <tr>
              {v.termStructure.map((t) => (
                <td key={t.index} className="num">{num(t.level, 2)}</td>
              ))}
            </tr>
          </tbody>
        </table>
      </div>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              <th>Index</th>
              <th className="num">Implied</th>
              <th className="num">Realized (20d)</th>
              <th className="num">Implied - realized</th>
            </tr>
          </thead>
          <tbody>
            {v.indices.map((i) => (
              <tr key={i.etf}>
                <td>
                  <span className="etf">{i.etf}</span> {i.name}
                  <span className="group">{i.implied_index ? `implied from ${i.implied_index}` : "no free implied-vol index"}</span>
                </td>
                <td className="num">{num(i.implied)}</td>
                <td className="num">{num(i.realized_20d)}</td>
                <td className="num">{i.spread == null ? "N/A" : `${i.spread > 0 ? "+" : ""}${i.spread.toFixed(1)}`}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="generated">Generated {v.generatedAt} NZT. Annualized %, from Cboe indices via Yahoo.</p>
    </section>
  );
}
