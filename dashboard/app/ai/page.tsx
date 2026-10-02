import type { Metadata } from "next";
import { loadAiDigest, type AiDigest, type DigestItem } from "../lib/aiDigest";

export const metadata: Metadata = {
  title: "AI Digest",
  description: "Daily top AI news and hiccups, each linked to its source.",
};

const SECTIONS: [keyof AiDigest, string][] = [
  ["top_ai_news", "Top AI news"],
  ["top_ai_hiccups", "AI hiccups"],
];

const nzt = (iso: string) =>
  new Date(iso).toLocaleString("en-NZ", {
    timeZone: "Pacific/Auckland",
    day: "numeric",
    month: "short",
    hour: "2-digit",
    minute: "2-digit",
  });

export default function AiDigestPage() {
  const digest = loadAiDigest();

  return (
    <main>
      <a className="back-link" href="https://a-chicken-talking-to-a-duck.vercel.app/">← Home</a>
      <h1>AI Digest</h1>
      {!digest ? (
        <div className="error">
          No digest yet -- run <code>python ops/ai_digest.py</code> locally to generate and publish it.
        </div>
      ) : (
        <>
          <p className="tagline">
            Updated {nzt(digest.updated_at)} NZT. Picked from Google News and the OpenAI/Anthropic status
            pages; where an item has more than a headline, a one-line summary of that text by Cloudflare Workers AI -- open the
            source before relying on any of it.
          </p>
          {SECTIONS.map(([key, label]) => {
            const items = digest[key] as DigestItem[];
            return (
              <section key={key} className="digest-section">
                <h2>
                  {label}
                  {digest.stale.includes(key) && <span className="stale"> -- source unavailable, from last run</span>}
                </h2>
                {items.length === 0 ? (
                  <p className="tagline">Nothing today.</p>
                ) : (
                  <ul className="digest-list">
                    {items.map((item) => (
                      <li key={item.url} className="card">
                        <a href={item.url} target="_blank" rel="noopener noreferrer" className="digest-title">
                          {item.title}
                        </a>
                        {item.summary && <p className="digest-summary">{item.summary}</p>}
                        <p className="digest-meta">
                          {item.source} · {nzt(item.timestamp)}
                        </p>
                      </li>
                    ))}
                  </ul>
                )}
              </section>
            );
          })}
        </>
      )}
    </main>
  );
}
