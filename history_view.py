"""Builds a searchable, offline history page from history.jsonl and opens it."""
import json
import logging
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).parent
HISTORY = ROOT / "history.jsonl"
PAGE = ROOT / "history.html"

TEMPLATE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Local Flow History</title>
<style>
:root { --bg:#f6f6f4; --card:#fff; --text:#1d1d1f; --muted:#6e6e73; --line:#e5e5e2; --accent:#d93a2b; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#141414; --card:#1f1f1f; --text:#f2f2f2; --muted:#9a9a9f; --line:#2e2e2e; --accent:#ff5a4a; }
}
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--text);
       font: 15px/1.5 -apple-system, BlinkMacSystemFont, "SF Pro Text", sans-serif; }
header { position:sticky; top:0; background:var(--bg); padding:20px 16px 12px; border-bottom:1px solid var(--line); }
.wrap { max-width:760px; margin:0 auto; }
h1 { font-size:20px; margin:0 0 4px; }
.sub { color:var(--muted); font-size:13px; margin-bottom:12px; }
input { width:100%; padding:10px 12px; font-size:15px; border-radius:10px;
        border:1px solid var(--line); background:var(--card); color:var(--text); outline:none; }
input:focus { border-color:var(--accent); }
main { padding:8px 16px 40px; }
.day { color:var(--muted); font-size:12px; font-weight:600; text-transform:uppercase;
       letter-spacing:.04em; margin:20px 0 8px; }
.item { background:var(--card); border:1px solid var(--line); border-radius:12px;
        padding:12px 14px; margin-bottom:8px; }
.text { white-space:pre-wrap; word-wrap:break-word; }
.meta { display:flex; gap:12px; align-items:center; color:var(--muted); font-size:12px; margin-top:8px; flex-wrap:wrap; }
.meta .sp { flex:1; }
button { font:inherit; font-size:12px; padding:4px 10px; border-radius:7px; cursor:pointer;
         border:1px solid var(--line); background:transparent; color:var(--text); }
button:hover { border-color:var(--accent); color:var(--accent); }
.raw { display:none; margin-top:8px; padding-top:8px; border-top:1px dashed var(--line);
       color:var(--muted); font-size:13px; white-space:pre-wrap; }
.item.show-raw .raw { display:block; }
mark { background:color-mix(in srgb, var(--accent) 25%, transparent); color:inherit; border-radius:3px; }
.empty { color:var(--muted); text-align:center; padding:40px 0; }
</style></head><body>
<header><div class="wrap">
  <h1>Local Flow History</h1>
  <div class="sub" id="sub"></div>
  <input id="q" type="search" placeholder="Search your dictations…" autofocus>
</div></header>
<main><div class="wrap" id="list"></div></main>
<script>
const DATA = __DATA__;
const list = document.getElementById("list"), q = document.getElementById("q");
const words = DATA.reduce((n, d) => n + (d.text || "").split(/\\s+/).filter(Boolean).length, 0);
document.getElementById("sub").textContent =
  `${DATA.length} dictations · ${words.toLocaleString()} words · generated __GENERATED__`;
const esc = s => s.replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
function highlight(s, term) {
  const safe = esc(s);
  if (!term) return safe;
  const re = new RegExp(term.replace(/[.*+?^${}()|[\\]\\\\]/g, "\\\\$&"), "gi");
  return safe.replace(re, m => `<mark>${m}</mark>`);
}
function render() {
  const term = q.value.trim();
  const rows = DATA.filter(d => !term || (d.text + " " + (d.raw || "") + " " + (d.app || ""))
                   .toLowerCase().includes(term.toLowerCase()));
  if (!rows.length) { list.innerHTML = `<div class="empty">No dictations match “${esc(term)}”.</div>`; return; }
  let html = "", lastDay = "";
  for (const d of rows) {
    const dt = new Date(d.ts * 1000);
    const day = dt.toLocaleDateString(undefined, {weekday:"long", month:"short", day:"numeric", year:"numeric"});
    if (day !== lastDay) { html += `<div class="day">${day}</div>`; lastDay = day; }
    const time = dt.toLocaleTimeString(undefined, {hour:"numeric", minute:"2-digit"});
    const changed = d.raw && d.raw.trim() !== (d.text || "").trim();
    html += `<div class="item"><div class="text">${highlight(d.text || "", term)}</div>
      <div class="meta"><span>${time}</span>${d.app ? `<span>${esc(d.app)}</span>` : ""}
      <span>${(d.audio_s || 0).toFixed(0)}s speech</span><span class="sp"></span>
      ${changed ? `<button data-raw>Original</button>` : ""}<button data-copy>Copy</button></div>
      ${changed ? `<div class="raw">${highlight(d.raw, term)}</div>` : ""}</div>`;
  }
  list.innerHTML = html;
  list.querySelectorAll(".item").forEach((el, i) => {
    const d = rows[i];
    el.querySelector("[data-copy]").onclick = e => {
      navigator.clipboard.writeText(d.text || "").then(() => {
        e.target.textContent = "Copied"; setTimeout(() => e.target.textContent = "Copy", 1200);
      });
    };
    const r = el.querySelector("[data-raw]");
    if (r) r.onclick = () => el.classList.toggle("show-raw");
  });
}
q.addEventListener("input", render);
render();
</script></body></html>
"""


MAX_ROWS = 3000  # newest dictations shown; keeps the page fast after months of use


def build() -> Path:
    rows = []
    if HISTORY.exists():
        # bytes + "replace": a line torn by a crash mid-write must not break the page
        for line in HISTORY.read_bytes().decode("utf-8", "replace").splitlines():
            try:
                d = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(d, dict):
                continue
            d.setdefault("text", d.get("raw", ""))
            rows.append(d)
    rows.sort(key=lambda d: d.get("ts", 0), reverse=True)
    rows = rows[:MAX_ROWS]
    data = json.dumps(rows).replace("</", "<\\/")
    generated = time.strftime("%b %-d, %-I:%M %p")
    PAGE.write_text(TEMPLATE.replace("__DATA__", data).replace("__GENERATED__", generated))
    return PAGE


def open_history():
    """Build off the main thread (a big history would beachball the menu), then open."""
    import threading

    def run():
        try:
            subprocess.run(["open", str(build())])
        except Exception:
            logging.getLogger("local-flow").exception("building history page")

    threading.Thread(target=run, daemon=True).start()


if __name__ == "__main__":
    open_history()
