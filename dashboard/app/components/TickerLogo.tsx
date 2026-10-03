import fs from "fs";
import path from "path";

// Logos are bundled in public/logos/<TICKER>.png (fetched once, not
// hot-linked), so the static page makes no third-party requests. A ticker
// added to the watchlist without a logo file gets a lettered tile instead.
// The src carries the basePath by hand because a plain <img> doesn't get it.
// Logos drawn in white (Amazon's) need a dark tile to be visible at all.
const LIGHT_LOGOS = new Set(["AMZN"]);

export default function TickerLogo({ ticker }: { ticker: string }) {
  const file = path.join(process.cwd(), "public", "logos", `${ticker}.png`);
  return (
    <span className={LIGHT_LOGOS.has(ticker) ? "logo dark" : "logo"} aria-hidden="true">
      {fs.existsSync(file) ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img src={`/dashboard/logos/${ticker}.png`} alt="" width={28} height={28} />
      ) : (
        <span className="logo-letter">{ticker[0]}</span>
      )}
    </span>
  );
}
