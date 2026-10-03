import type { FactorSnapshot } from "../lib/factors";

const pp = (v: number | null) => (v == null ? "N/A" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`);
const tone = (v: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");
const CALL = { "In favour": "Overweight", "Out of favour": "Underweight", Mixed: "Neutral" } as const;

export default function FactorTable({ snapshot }: { snapshot: FactorSnapshot }) {
  return (
    <section className="sectors">
      <h2>Factor performance</h2>
      <p className="tagline">
        Factor ETFs&apos; returns in percentage points vs SPY. In favour = beating SPY over both 3 and 6 months, out of
        favour = lagging both. A fixed rule, not a recommendation.
      </p>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              <th>Factor</th>
              <th>1m</th>
              <th>3m</th>
              <th>6m</th>
              <th>12m</th>
              <th>Status</th>
            </tr>
          </thead>
          <tbody>
            {snapshot.factors.map((f) => (
              <tr key={f.etf}>
                <td>
                  <span className="etf">{f.etf}</span> {f.name}
                  <span className="group">{f.note}</span>
                </td>
                {[f.rs1m, f.rs3m, f.rs6m, f.rs12m].map((v, i) => (
                  <td key={i} className={`num ${tone(v)}`}>{pp(v)}</td>
                ))}
                <td className={`call ${CALL[f.status]}`}>{f.status}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <ul className="episode-notes">
        {snapshot.rotation.map((r) => (
          <li key={r.pair}>
            <strong>{r.pair}:</strong> {r.leader} leading by {Math.abs(r.spread6m).toFixed(1)}pp over 6 months.
          </li>
        ))}
      </ul>
      <p className="generated">Generated {snapshot.generatedAt} NZT</p>
    </section>
  );
}
