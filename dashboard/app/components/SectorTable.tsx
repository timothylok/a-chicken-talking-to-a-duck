import type { SectorSnapshot } from "../lib/sectors";

const pp = (v: number | null) => (v == null ? "N/A" : `${v > 0 ? "+" : ""}${v.toFixed(1)}`);
const tone = (v: number | null) => (v == null ? "" : v > 0 ? "pos" : v < 0 ? "neg" : "");

export default function SectorTable({ snapshot }: { snapshot: SectorSnapshot }) {
  return (
    <section className="sectors">
      <h2>Sector rotation</h2>
      <p className="tagline">
        Market-implied regime: <strong>{snapshot.regime ?? "N/A"}</strong> -- {snapshot.regimeBasis}. Returns are
        percentage points vs SPY. Overweight = the cycle playbook favours the sector in this regime and it leads SPY
        over 3 and 6 months; underweight = not favoured and lagging both. A fixed rule, not a forecast.
      </p>
      <div className="table-wrap">
        <table className="sector-table">
          <thead>
            <tr>
              <th>Sector</th>
              <th>1m</th>
              <th>3m</th>
              <th>6m</th>
              <th>Call</th>
            </tr>
          </thead>
          <tbody>
            {snapshot.sectors.map((s) => (
              <tr key={s.etf}>
                <td>
                  <span className="etf">{s.etf}</span> {s.name}
                  <span className="group">{s.group}{s.favoured ? " · favoured" : ""}</span>
                </td>
                {[s.rs1m, s.rs3m, s.rs6m].map((v, i) => (
                  <td key={i} className={`num ${tone(v)}`}>{pp(v)}</td>
                ))}
                <td className={`call ${s.call}`}>{s.call}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <p className="generated">Generated {snapshot.generatedAt} NZT</p>
    </section>
  );
}
