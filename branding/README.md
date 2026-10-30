# cashfloor branding

`icon.svg` is the master (512 viewBox): a blue tile, a white balance line
that dips to a flat amber **floor** and recovers, and a red marker at the
floor — the same elements as the dashboard chart.

`icon-mono.svg` is the single-colour mark (uses `currentColor`) for inline
use next to the wordmark and for monochrome contexts.

PNGs are rendered from `icon.svg` at 16/32/48/64/128/180/192/256/512;
`favicon.ico` bundles 16/32/48.  Copies the web app serves live in
`src/cashfloor/web/static/` together with `manifest.webmanifest`
(so "Install as app" in Chrome/Edge gets the icon and standalone window).

Re-render after editing the SVG with Playwright (see git history) or any
SVG rasteriser; keep `src/cashfloor/web/static/` in sync.
