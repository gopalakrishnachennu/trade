// Shared nav + live health pill for every dashboard page. Token stays in the URL query;
// writes send it in the X-Dashboard-Token header (a link alone can never change anything).
(function () {
  const T = new URLSearchParams(location.search).get("token") || "";
  const Q = "token=" + encodeURIComponent(T);
  const PM = (window.PM = window.PM || {});
  PM.T = T; PM.Q = Q;
  PM.esc = (s) => String(s == null ? "" : s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  PM.get = async (path) => { const r = await fetch(path + (path.includes("?") ? "&" : "?") + Q, { cache: "no-store" }); if (!r.ok) throw new Error(r.status + " " + (await r.text()).slice(0, 200)); return r.json(); };
  PM.send = async (method, path, body) => {
    const r = await fetch(path, { method, headers: { "Content-Type": "application/json", "X-Dashboard-Token": T }, body: body == null ? undefined : JSON.stringify(body) });
    let j = {}; try { j = await r.json(); } catch (e) {}
    if (!r.ok) throw new Error(j.error || (j.errors && JSON.stringify(j.errors)) || r.status);
    return j;
  };
  PM.ago = (ts) => { if (!ts) return "—"; const s = Date.now() / 1000 - ts; if (s < 0) return "now"; if (s < 90) return s.toFixed(0) + "s ago"; if (s < 5400) return (s / 60).toFixed(0) + "m ago"; if (s < 172800) return (s / 3600).toFixed(1) + "h ago"; return (s / 86400).toFixed(1) + "d ago"; };
  PM.et = (ts) => ts ? new Date(ts * 1000).toLocaleString("en-US", { timeZone: "America/New_York", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", second: "2-digit" }) + " ET" : "—";
  PM.usd = (x, d = 2) => x == null || isNaN(x) ? "—" : (x < 0 ? "−$" : "$") + Math.abs(x).toFixed(d);
  PM.susd = (x) => x == null || isNaN(x) ? "—" : (x >= 0 ? "+$" : "−$") + Math.abs(x).toFixed(2);
  PM.pct = (x, d = 1) => x == null || isNaN(x) ? "—" : (x * 100).toFixed(d) + "%";
  PM.bytes = (b) => b == null ? "—" : b > 1e9 ? (b / 1e9).toFixed(2) + " GB" : b > 1e6 ? (b / 1e6).toFixed(1) + " MB" : (b / 1e3).toFixed(0) + " KB";

  const pages = [["/", "TERMINAL"], ["/health", "HEALTH"], ["/strategies", "STRATEGIES"], ["/patterns", "PATTERNS"], ["/attribution", "LOSSES"], ["/rules-lab", "RULES LAB"], ["/integrations", "INTEGRATIONS"], ["/archives", "ARCHIVES"], ["/rules", "RULES"], ["/api/report/daily", "DAILY REPORT"], ["/api/export/trades.csv", "CSV"]];
  function mount() {
    if (document.getElementById("pm-nav")) return;
    const nav = document.createElement("div");
    nav.id = "pm-nav";
    nav.innerHTML = `<span class="brand">PM US · PAPER</span>` +
      pages.map(([p, l]) => `<a href="${p}?${Q}" class="${location.pathname === p ? "cur" : ""}">${l}</a>`).join("") +
      `<span class="hp"><span class="mut">health</span><span id="pm-health" class="st st-DEGRADED" title="loading">…</span><span id="pm-health-why" class="mut"></span></span>`;
    document.body.insertBefore(nav, document.body.firstChild);
    document.getElementById("pm-health").onclick = () => (location.href = "/health?" + Q);
    poll();
    setInterval(poll, 5000);
  }
  async function poll() {
    const el = document.getElementById("pm-health"), why = document.getElementById("pm-health-why");
    try {
      const d = await PM.get("/api/health");
      el.className = "st st-" + d.status; el.textContent = d.status;
      const bad = d.items.filter((i) => i.status !== "OK");
      why.textContent = d.halt && d.halt.halted ? `HALT: ${d.halt.reason} (${d.halt.component})` : bad.length ? bad.map((i) => `${i.name} ${i.status}`).slice(0, 4).join(" · ") + (bad.length > 4 ? ` +${bad.length - 4}` : "") : "all components OK";
      el.title = why.textContent;
      PM.lastHealth = d;
      document.dispatchEvent(new CustomEvent("pm-health", { detail: d }));
    } catch (e) { el.className = "st st-FAILED"; el.textContent = "UNREACHABLE"; why.textContent = String(e).slice(0, 80); }
  }
  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", mount); else mount();
})();
