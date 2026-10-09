"""Build a single self-contained HTML file of the dashboard (data and d3 v7.9.0 inlined, works offline).

Output: dashboard/nifty_seasonality_dashboard.html -- open it in any browser.
"""
import csv
import json
from pathlib import Path

HERE = Path(__file__).parent
D = HERE / "data" / "dashboard"

data = {
    "month_stats": json.loads((D / "month_stats.json").read_text()),
    "summary": json.loads((D / "summary.json").read_text()),
    "monthly_returns": list(csv.DictReader((D / "monthly_returns.csv").open())),
}
page = (HERE / "dashboard" / "index.html").read_text()

HEAD = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>NIFTY Monthly Seasonality</title>
<style>
:root {
  --font-anthropic-sans: system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --font-anthropic-serif: Georgia, "Times New Roman", serif;
  --cds-font-size-caption: 12px; --cds-font-size-body: 14px; --cds-font-size-heading: 16px; --cds-font-size-title: 28px;
  --cds-font-weight-medium: 600;
  --cds-gap-xs: 4px; --cds-gap-sm: 8px; --cds-gap-md: 16px; --cds-gap-lg: 24px;
  --cds-pad-xs: 4px; --cds-pad-sm: 8px; --cds-pad-lg: 20px;
  --cds-radius: 8px; --cds-dur-slow: 300ms;
  --color-bg: #f9f9f7; --color-panel: #fcfcfb; --color-fg: #0b0b0b; --color-fg-muted: #6b6a66;
  --color-border-line: #e1e0d9; --color-bad: #d03b3b;
  --cds-surface-popover: #ffffff; --cds-border: #d6d5ce; --cds-shadow-popover: 0 4px 16px rgba(0,0,0,.12);
  --cds-text-secondary: #52514e;
  --cds-chart-grid: #e8e7e1; --cds-chart-axis: #c3c2b7; --cds-chart-reference: #898781;
  --cds-chart-muted: #c3c2b7; --cds-chart-status-critical: #d03b3b;
  --series-up: #2a78d6; --series-down: #e34948;
  color-scheme: light;
}
@media (prefers-color-scheme: dark) {
  :root {
    --color-bg: #0d0d0d; --color-panel: #1a1a19; --color-fg: #ffffff; --color-fg-muted: #a8a79f;
    --color-border-line: #2c2c2a; --cds-surface-popover: #232322; --cds-border: #383835;
    --cds-shadow-popover: 0 4px 16px rgba(0,0,0,.5); --cds-text-secondary: #c3c2b7;
    --cds-chart-grid: #2c2c2a; --cds-chart-axis: #383835; --cds-chart-reference: #898781;
    --series-up: #3987e5; --series-down: #e66767;
    color-scheme: dark;
  }
}
body { margin: 0; background: var(--color-bg); color: var(--color-fg); font-family: var(--font-anthropic-sans); font-size: 14px; }
#dash-root { max-width: 1280px; margin-inline: auto; padding: 24px 16px 40px; }
.dash-skeleton { color: transparent !important; background: var(--color-border-line); border-radius: 4px; }
</style>
<script>__D3__</script>
<script>
// Minimal stand-in for the claude.ai dashboard runtime: data is inlined below.
window.DASH_DATA = __DATA__;
window.dash = {
  colors: ['var(--series-up)', 'var(--series-up)', 'var(--series-up)', 'var(--series-up)',
           'var(--series-up)', 'var(--series-up)', 'var(--series-up)', 'var(--series-down)'],
  data(id) { const d = window.DASH_DATA[id]; return d ? { status: 'ok', data: d, columns: Object.keys(d[0] || {}) } : { status: 'missing', data: [] }; },
  onData(fn) {
    const run = () => fn();
    if (document.readyState === 'loading') document.addEventListener('DOMContentLoaded', run); else run();
    let w = window.innerWidth, t;
    window.addEventListener('resize', () => { clearTimeout(t); t = setTimeout(() => { if (window.innerWidth !== w) { w = window.innerWidth; run(); } }, 150); });
    window.matchMedia('(prefers-color-scheme: dark)').addEventListener('change', run);
  },
};
</script>
</head>
<body>
<div id="dash-root">
"""

d3_src = (HERE / "vendor" / "d3-7.9.0.min.js").read_text().replace("</script", "<\\/script")
out = HEAD.replace("__DATA__", json.dumps(data, separators=(",", ":"))).replace("__D3__", d3_src) + page + "\n</div>\n</body>\n</html>\n"
dest = HERE / "dashboard" / "nifty_seasonality_dashboard.html"
dest.write_text(out)
print(dest, len(out), "bytes")
