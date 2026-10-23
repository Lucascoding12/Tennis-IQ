// Tennis IQ — single-page app. Views: h2h, billy, player, rankings, data.
const chosen = { p1: null, p2: null };
let courtView = "surfaces12";
let timer = null;           // per-view auto refresh
let routeToken = 0;         // ignore responses from views we've left

const $ = (s, el = document) => el.querySelector(s);
const out = () => $("#out");
const esc = (s) => String(s ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
const rec = (r) => `${r.won}-${r.lost}`;
const pct = (r) => (r.pct == null ? "—" : `${r.pct}%`);
const last = (name) => esc(String(name).split(" ").slice(-1)[0]);
const COURT_NAME = { outdoor_hard: "Outdoor Hard", indoor_hard: "Indoor Hard", clay: "Clay", grass: "Grass", other: "Other" };
const COURTS = ["outdoor_hard", "indoor_hard", "clay", "grass"];
const ago = (ts) => {
  if (!ts) return "never";
  const s = Math.round(Date.now() / 1000 - ts);
  return s < 60 ? "just now" : s < 3600 ? `${Math.round(s / 60)} min ago` : `${Math.round(s / 3600)} h ago`;
};
const plink = (id, name) => (id ? `<a class="plink" href="#/player/${id}">${esc(name)}</a>` : esc(name));

// Changing models or forcing a data refresh needs the admin password (set on the server).
let adminToken = (() => { try { return localStorage.getItem("tiqAdmin") || ""; } catch { return ""; } })();

async function api(url, opts = {}, retried = false) {
  const res = await fetch(url, { ...opts, headers: { ...(opts.headers || {}), ...(adminToken ? { "X-Admin-Token": adminToken } : {}) } });
  const data = await res.json().catch(() => ({}));
  if (res.status === 401 && !retried) {
    const pw = prompt("Admin password (needed to change models):");
    if (pw) {
      adminToken = pw;
      try { localStorage.setItem("tiqAdmin", pw); } catch { /* private mode */ }
      return api(url, opts, true);
    }
  }
  if (!res.ok) throw new Error(data.detail || res.statusText);
  return data;
}
function every(ms, fn) {
  clearTimeout(timer);
  timer = setTimeout(async () => { await fn(); }, ms);
}

// ================================================================ router
function route() {
  clearTimeout(timer);
  routeToken++;
  const [path, query] = (location.hash.slice(1) || "/h2h").split("?");
  const parts = path.split("/").filter(Boolean);
  let tab = parts[0] || "h2h";
  if (tab === "mymodel") tab = "billy";
  document.querySelectorAll("nav a").forEach((a) => a.classList.toggle("on", a.dataset.tab === tab));
  $("#search").hidden = tab !== "h2h";
  const qs = new URLSearchParams(query || "");
  if (tab === "h2h") return viewH2H(qs);
  if (tab === "player") return viewPlayer(parts[1]);
  if (tab === "rankings") return viewRankings();
  if (tab === "data") return viewData();
  if (tab === "billy") return viewMyModel();
  return viewH2H(qs);
}
window.addEventListener("hashchange", route);

// ================================================================ shared bits
function photo(p, cls = "photo") {
  const initials = p.name.split(" ").map((w) => w[0]).slice(0, 2).join("");
  return `<img class="${cls}" src="/img/${p.id}" alt="${esc(p.name)}"
    onerror="this.outerHTML='<div class=&quot;${cls} fallback&quot;>${esc(initials)}</div>'">`;
}

function lineChart(points, { w = 760, h = 200, fmt = (v) => Math.round(v) } = {}) {
  if (points.length < 2) return `<p class="empty">Not enough data yet.</p>`;
  const xs = points.map((p) => new Date(p[0]).getTime()), ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs), x1 = Math.max(...xs);
  let y0 = Math.min(...ys), y1 = Math.max(...ys);
  const pad = (y1 - y0) * 0.1 || 10; y0 -= pad; y1 += pad;
  const L = 44, R = 10, T = 10, B = 24;
  const X = (x) => L + ((x - x0) / (x1 - x0 || 1)) * (w - L - R);
  const Y = (y) => T + (1 - (y - y0) / (y1 - y0)) * (h - T - B);
  const d = points.map((p, i) => `${i ? "L" : "M"}${X(xs[i]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join("");
  const ticks = [0, 0.5, 1].map((t) => y0 + t * (y1 - y0));
  const years = [];
  for (let y = new Date(x0).getFullYear() + 1; y <= new Date(x1).getFullYear(); y++) years.push(y);
  return `<svg viewBox="0 0 ${w} ${h}">
    ${ticks.map((t) => `<line x1="${L}" x2="${w - R}" y1="${Y(t)}" y2="${Y(t)}" stroke="#222"/><text x="${L - 6}" y="${Y(t) + 4}" text-anchor="end">${fmt(t)}</text>`).join("")}
    ${years.map((y) => { const x = X(new Date(`${y}-01-01`).getTime()); return `<line x1="${x}" x2="${x}" y1="${T}" y2="${h - B}" stroke="#1a1a1a"/><text x="${x}" y="${h - 6}" text-anchor="middle">${y}</text>`; }).join("")}
    <path d="${d}" fill="none" stroke="#22c55e" stroke-width="2"/>
    <circle cx="${X(xs.at(-1))}" cy="${Y(ys.at(-1))}" r="4" fill="#22c55e"/>
  </svg>`;
}

// ================================================================ H2H
document.querySelectorAll(".pick").forEach((pick) => {
  const slot = pick.dataset.slot;
  const input = $("input", pick);
  const list = $(".suggest", pick);
  let t, items = [], active = -1;
  const choose = (p) => {
    chosen[slot] = p;
    input.value = p.name;
    list.innerHTML = "";
    if (chosen.p1 && chosen.p2) goH2H();
  };
  const render = () => {
    list.innerHTML = items.map((p, i) =>
      `<li class="${i === active ? "active" : ""}" data-i="${i}"><span>${esc(p.name)}</span><small>#${p.rank} · ${esc(p.country)}</small></li>`).join("");
  };
  input.addEventListener("input", () => {
    chosen[slot] = null;
    clearTimeout(t);
    t = setTimeout(async () => {
      const q = input.value.trim();
      items = q.length < 2 ? [] : await api(`/api/players?q=${encodeURIComponent(q)}`);
      active = items.length ? 0 : -1;
      render();
    }, 150);
  });
  input.addEventListener("keydown", (e) => {
    if (e.key === "ArrowDown" || e.key === "ArrowUp") {
      e.preventDefault();
      if (!items.length) return;
      active = (active + (e.key === "ArrowDown" ? 1 : -1) + items.length) % items.length;
      render();
    } else if (e.key === "Enter") {
      if (items[active] && !chosen[slot]) choose(items[active]);
      else if (chosen.p1 && chosen.p2) goH2H();
    } else if (e.key === "Escape") list.innerHTML = "";
  });
  list.addEventListener("mousedown", (e) => {
    const li = e.target.closest("li");
    if (li) choose(items[+li.dataset.i]);
  });
  input.addEventListener("blur", () => setTimeout(() => (list.innerHTML = ""), 100));
});
$("#go").addEventListener("click", () => {
  if (chosen.p1 && chosen.p2) goH2H();
  else out().innerHTML = `<p class="msg">Pick both players from the dropdown first.</p>`;
});
$("#swap").addEventListener("click", () => {
  [chosen.p1, chosen.p2] = [chosen.p2, chosen.p1];
  if (chosen.p1 && chosen.p2) goH2H();
});
function goH2H() {
  const { p1, p2 } = chosen;
  location.hash = `#/h2h?p1=${p1.id}&p2=${p2.id}&n1=${encodeURIComponent(p1.name)}&n2=${encodeURIComponent(p2.name)}`;
}

async function viewH2H(qs) {
  if (qs.get("p1") && qs.get("p2")) {
    chosen.p1 = { id: qs.get("p1"), name: qs.get("n1") || qs.get("p1") };
    chosen.p2 = { id: qs.get("p2"), name: qs.get("n2") || qs.get("p2") };
  }
  $('[data-slot="p1"] input').value = chosen.p1?.name || "";
  $('[data-slot="p2"] input').value = chosen.p2?.name || "";
  if (!chosen.p1 || !chosen.p2) {
    out().innerHTML = `<p class="msg">Enter two players to see their head-to-head, form, records by court and a points &amp; sets estimate.</p>`;
    return;
  }
  loadH2H(false);
}

async function loadH2H(silent) {
  const tok = routeToken;
  const { p1, p2 } = chosen;
  if (!silent) out().innerHTML = `<p class="msg"><span class="spinner"></span>Loading ${esc(p1.name)} vs ${esc(p2.name)}…</p>`;
  try {
    const data = await api(`/api/compare?p1=${p1.id}&p2=${p2.id}`);
    if (tok !== routeToken) return;
    data.updated = new Date();
    drawH2H(data);
  } catch (e) {
    if (!silent && tok === routeToken) out().innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
  }
  if (tok === routeToken) every(5 * 60000, () => loadH2H(true));
}

function playerCard(p, side) {
  return `<div class="player ${side}">
    ${photo(p)}
    <div>
      <div class="pname">${plink(p.id, p.name)}</div>
      <div class="pcountry">${esc(p.countryName || p.country || "")}${p.plays ? " · " + esc(p.plays) : ""}</div>
      <dl class="facts">
        <dt>ATP rank</dt><dd>${p.rank ? "#" + p.rank : "Unranked"}</dd>
        <dt>Career high</dt><dd>${p.highRank ? "#" + p.highRank : "—"}${p.highRankDate ? ` <span style="color:var(--muted);font-weight:400">(${p.highRankDate.slice(0, 4)})</span>` : ""}</dd>
        <dt>Age</dt><dd>${p.age ?? "—"}</dd>
        <dt>Turned pro</dt><dd>${p.proYear ?? "—"}</dd>
      </dl>
    </div>
  </div>`;
}

function sideCell(r, cls, best) {
  return `<div class="side ${cls}">
    <span class="rec ${best ? "best" : ""}">${rec(r)}</span>
    <span class="pct">${pct(r)}</span>
    <div class="mini"><i style="width:${r.pct ?? 0}%"></i></div>
  </div>`;
}
function compareRow(label, a, b) {
  const both = a.won + a.lost > 0 && b.won + b.lost > 0;
  return `<div class="row">${sideCell(a, "l", both && a.pct > b.pct)}<div class="label">${label}</div>${sideCell(b, "r", both && b.pct > a.pct)}</div>`;
}
const hm = (mins) => (mins ? `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, "0")}m` : "—");

function durationCard(d, a, b) {
  const du = d.durations || {};
  const side = (p, x, cls) => {
    if (!x) return `<div></div>`;
    const l = x.last || {};
    return `<div class="dur ${cls}">
      <div class="dur-name">${esc(p.name)}</div>
      <div class="big">${hm(l.minutes)}</div>
      <div class="muted">on court in their last match${l.tournament ? ` · ${esc(l.tournament)} ${esc(l.round || "")} · <span class="${l.result === "W" ? "up" : "down"}">${l.result || ""}</span> · ${l.date || ""}` : ""}</div>
      <div class="mid">${hm(x.recentMinutes)} <span class="muted">on court in the last 14 days (${x.recentMatches} match${x.recentMatches === 1 ? "" : "es"}${x.recentTimed < x.recentMatches ? `, ${x.recentMatches - x.recentTimed} untimed` : ""})</span></div>
    </div>`;
  };
  const lm = du.lastMeeting;
  return `<div class="card">
    <h2>Match length &amp; time on court</h2>
    <div class="durgrid">
      ${side(a, du[a.id], "l")}
      <div class="dur mid-col"><div class="dur-name">Against each other</div><div class="big">${hm(lm?.minutes)}</div><div class="muted">${lm ? `last meeting · ${esc(lm.tournament)} ${esc(lm.round || "")} ${lm.year}` : "never met"}</div></div>
      ${side(b, du[b.id], "r")}
    </div>
  </div>`;
}

function wlBlock(p, s) {
  const r = s.last12, total = r.won + r.lost;
  const w = total ? (100 * r.won) / total : 0;
  return `<div class="wl">
    <h3>${esc(p.name)} <span>${rec(r)}</span></h3>
    <div class="wlbar">
      ${total ? `<div class="w" style="width:${w}%">${w >= 12 ? `W ${w.toFixed(0)}%` : ""}</div>
      <div class="l" style="width:${100 - w}%">${100 - w >= 12 ? `L ${(100 - w).toFixed(0)}%` : ""}</div>` : `<div style="width:100%;background:#222;color:var(--muted)">No matches</div>`}
    </div>
    <div class="extra"><span>${new Date().getFullYear()} season: <b>${rec(s.ytd)}</b> (${pct(s.ytd)})</span><span>Career: <b>${rec(s.career)}</b></span></div>
  </div>`;
}

function matchList(ms) {
  return ms.length ? ms.map((m) => `<div class="m">
    <div class="res ${m.result}">${m.result}</div>
    <div>
      <div class="opp">${plink(m.opponentId, m.opponent)}<span class="rk">${m.opponentRankNow ? "#" + m.opponentRankNow : "NR"}</span></div>
      <div class="meta">${esc(m.tournament)} · ${esc(m.round)} · ${COURT_NAME[m.court] || esc(m.surface)} · ${m.date.slice(0, 7)}</div>
    </div>
    <div class="score">${esc(m.score)}</div>
  </div>`).join("") : `<p class="empty">No matches found.</p>`;
}
function last10Block(p, s) {
  return `<div>
    <h3><span>${esc(p.name)} <span class="l10rec">${s.last10.filter((m) => m.result === "W").length}-${s.last10.filter((m) => m.result === "L").length}</span></span>
      <span class="dots">${s.last10.map((m) => `<i class="${m.result}"></i>`).join("")}</span></h3>
    ${matchList(s.last10)}
  </div>`;
}

function drawH2H(d) {
  const a = d.p1.profile, b = d.p2.profile, sa = d.p1.stats, sb = d.p2.stats;
  const h1 = d.h2h.filter((m) => m.winner === a.id).length, h2 = d.h2h.length - h1;
  const courts = d.courts;

  out().innerHTML = `
  <div class="card">
    <div class="players">
      ${playerCard(a, "left")}
      <div class="h2hbig"><div class="nums">${h1} – ${h2}</div><div class="lbl">HEAD TO HEAD</div></div>
      ${playerCard(b, "right")}
    </div>
  </div>

  <div class="card">
    <h2>Record by court</h2>
    <div class="toggle">
      <button data-v="surfaces12" class="${courtView === "surfaces12" ? "on" : ""}">Last 12 months</button>
      <button data-v="surfacesCareer" class="${courtView === "surfacesCareer" ? "on" : ""}">Career</button>
    </div>
    <div class="split-names"><span>${esc(a.name)}</span><span>${esc(b.name)}</span></div>
    ${courts.map(([k, name]) => compareRow(name, sa[courtView][k], sb[courtView][k])).join("")}
  </div>

  ${durationCard(d, a, b)}

  <div class="card">
    <h2>Last 12 months — win / loss</h2>
    <div class="wlbars">${wlBlock(a, sa)}${wlBlock(b, sb)}</div>
  </div>

  <div class="card">
    <h2>Last 10 matches <span style="text-transform:none;letter-spacing:0">(opponent's current ATP rank)</span></h2>
    <div class="last10">${last10Block(a, sa)}${last10Block(b, sb)}</div>
  </div>

  <div class="card">
    <h2>Head-to-head meetings</h2>
    ${d.h2h.length ? d.h2h.map((m) => `<div class="meet">
      <span class="yr">${m.year}</span>
      <div><div>${esc(m.tournament)} · ${esc(m.round)}</div><div class="meta">${COURT_NAME[m.court] || esc(m.surface)}${m.level === "CH" ? " · Challenger" : ""}</div></div>
      <span class="who" style="color:var(--win)">${esc((m.winner === a.id ? a : b).name)}</span>
      <span class="score">${esc(m.score)}${m.note ? " " + esc(m.note) : ""}</span>
    </div>`).join("") : `<p class="empty">These players have never met.</p>`}
  </div>

  <div class="card" id="ptscard"><h2>Points &amp; sets estimate</h2><p class="msg"><span class="spinner"></span>Simulating the match point by point…</p></div>

  <p class="foot">Data: atptour.com · auto-refreshes every 5 min · last updated ${d.updated.toLocaleTimeString()}</p>`;
  loadPoints(a, b);

  out().querySelectorAll(".toggle button[data-v]").forEach((btn) =>
    btn.addEventListener("click", () => { courtView = btn.dataset.v; drawH2H(d); }));

}

// ---------------- points & sets estimate (bottom of H2H)
let ptsCourt = "outdoor_hard", ptsBo = 3;
async function loadPoints(a, b) {
  const box = $("#ptscard");
  if (!box) return;
  let e;
  try { e = await api(`/api/points?p1=${a.id}&p2=${b.id}&court=${ptsCourt}&bo=${ptsBo}`); }
  catch (err) { box.innerHTML = `<h2>Points &amp; sets estimate</h2><p class="msg err">${esc(err.message)}</p>`; return; }
  if (!$("#ptscard")) return;
  const P = e.points, S = e.sets, sl = e.scorelines, need = ptsBo === 3 ? 2 : 3;
  const maxH = Math.max(...P.hist);
  const bt = e.backtest;
  const setRows = Object.entries(S).map(([n, v]) => `<div class="setbar"><span>${n} sets</span><div class="track"><i style="width:${v}%"></i></div><b>${v}%</b></div>`).join("");
  const scoreRows = Object.entries(sl).sort((x, y) => y[1] - x[1]).map(([k, v]) => {
    const who = k[0] === "a" ? a : b;
    return `<div class="setbar"><span>${last(who.name)} ${k[1]}-${k[2]}</span><div class="track"><i style="width:${v}%"></i></div><b>${v}%</b></div>`;
  }).join("");
  box.innerHTML = `
    <h2>Points &amp; sets estimate</h2>
    <div class="toggle">
      ${COURTS.map((c) => `<button data-pc="${c}" class="${c === ptsCourt ? "on" : ""}">${COURT_NAME[c]}</button>`).join("")}
      <span style="width:10px"></span>
      ${[3, 5].map((n) => `<button data-bo="${n}" class="${n === ptsBo ? "on" : ""}">Best of ${n}</button>`).join("")}
    </div>
    <div class="pts-hero">
      <div><small>Total points</small><b>${P.median}</b><span>likely ${P.range80[0]}–${P.range80[1]}</span></div>
      <div><small>Total games</small><b>${e.games.median}</b><span>likely ${e.games.range80[0]}–${e.games.range80[1]}</span></div>
      <div><small>Most likely</small><b>${Object.entries(S).sort((x, y) => y[1] - x[1])[0][0]} sets</b><span>${Object.entries(S).sort((x, y) => y[1] - x[1])[0][1]}% chance</span></div>
    </div>
    <div class="grid2" style="margin-top:16px">
      <div><h3 class="bt-h">Number of sets</h3>${setRows}<h3 class="bt-h" style="margin-top:14px">Exact scoreline</h3>${scoreRows}</div>
      <div>
        <h3 class="bt-h">Where the point total lands</h3>
        <div class="hist">${P.hist.map((v, i) => `<div title="${P.histStart + i * 10}–${P.histStart + i * 10 + 9} points: ${(100 * v / P.hist.reduce((s, x) => s + x, 0)).toFixed(1)}%"><i style="height:${(100 * v) / maxH}%"></i></div>`).join("")}</div>
        <div class="hist-axis"><span>${P.histStart}</span><span>${P.histStart + P.hist.length * 10}</span></div>
        <h3 class="bt-h" style="margin-top:14px">Chance the total goes over…</h3>
        <div class="overs">${Object.entries(P.over).map(([l, v]) => `<div><small>${l}</small><b>${v}%</b></div>`).join("")}</div>
      </div>
    </div>
    <p class="muted" style="font-size:12px;margin:14px 0 0">How: ${last(a.name)} wins ${e.serve.a}% of points on serve, ${last(b.name)} ${e.serve.b}% (ATP serve & return stats, Barnett–Clarke method, tuned so the winner matches the ${e.winProb ?? "—"}% win prediction), then 6,000 point-by-point simulations.${e.calibrated ? " Corrected with the backtest below." : ptsBo === 5 ? " Best of 5 isn't backtested yet, so it's uncorrected and likely runs a little long." : ""}${!e.stats.a || !e.stats.b ? " One player has no ATP serve stats (Challenger-only), so tour averages are used for them." : ""}</p>
    ${bt ? `<details class="honest"><summary><b>How accurate is this?</b> Strict backtest on ${bt.matches} ${bt.test_year} matches</summary><ul>${bt.verdict.map((v) => `<li>${esc(v)}</li>`).join("")}</ul></details>` : `<p class="muted" style="font-size:12px">Backtest still running in the background…</p>`}`;
  box.querySelectorAll("[data-pc]").forEach((x) => x.addEventListener("click", () => { ptsCourt = x.dataset.pc; loadPoints(a, b); }));
  box.querySelectorAll("[data-bo]").forEach((x) => x.addEventListener("click", () => { ptsBo = +x.dataset.bo; loadPoints(a, b); }));
}

// ================================================================ PLAYER
async function viewPlayer(id) {
  const tok = routeToken;
  out().innerHTML = `<p class="msg"><span class="spinner"></span>Loading player…</p>`;
  let d;
  try { d = await api(`/api/player/${id}`); } catch (e) {
    if (tok === routeToken) out().innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    return;
  }
  if (tok !== routeToken) return;
  const p = d.profile, s = d.stats, e = d.elo || {};
  const stat = (label, v) => `<div class="stat"><small>${label}</small><b>${v ?? "—"}</b></div>`;
  out().innerHTML = `
  <div class="card">
    <div class="phero">
      ${photo(p)}
      <div style="min-width:220px">
        <div class="pname">${esc(p.name)}</div>
        <div class="pcountry">${esc(p.countryName || "")} · ${esc(p.plays || "")}${p.backhand ? ", " + esc(p.backhand) + " backhand" : ""}${p.heightCm ? ` · ${p.heightCm} cm` : ""}</div>
        <button class="btn-sm" id="cmp">Compare with…</button>
      </div>
      <div class="statgrid">
        ${stat("ATP rank", p.rank ? "#" + p.rank : "NR")}
        ${stat("Career high", p.highRank ? `#${p.highRank}` : null)}
        ${stat("Elo", e.elo)}
        ${stat("Age", p.age)}
        ${stat("Turned pro", p.proYear)}
        ${stat("Titles", p.titles)}
        ${stat("12-mo record", rec(s.last12))}
        ${stat(`${new Date().getFullYear()} record`, rec(s.ytd))}
      </div>
    </div>
  </div>

  <div class="card chart">
    <h2>Elo rating — last 3 years</h2>
    ${lineChart(d.eloHistory)}
  </div>

  <div class="grid2">
    <div class="card">
      <h2>By court — last 12 months / career / Elo</h2>
      <table>
        <tr><th>Court</th><th class="num">12 mo</th><th class="num">Career</th><th class="num">Elo</th></tr>
        ${COURTS.map((c) => `<tr><td>${COURT_NAME[c]}</td>
          <td class="num">${rec(s.surfaces12[c])} <span class="muted">${pct(s.surfaces12[c])}</span></td>
          <td class="num">${rec(s.surfacesCareer[c])} <span class="muted">${pct(s.surfacesCareer[c])}</span></td>
          <td class="num">${e[c] ?? "—"}</td></tr>`).join("")}
      </table>
    </div>
    <div class="card">
      <h2>Season by season</h2>
      <table>
        <tr><th>Year</th><th class="num">W-L</th><th class="num">Win %</th><th style="width:40%"></th></tr>
        ${d.years.map((y) => { const t = y.won + y.lost, w = t ? (100 * y.won) / t : 0; return `<tr><td>${y.year}</td><td class="num">${y.won}-${y.lost}</td><td class="num">${w.toFixed(0)}%</td>
          <td><div class="wlbar" style="height:10px"><div class="w" style="width:${w}%"></div><div class="l" style="width:${100 - w}%"></div></div></td></tr>`; }).join("")}
      </table>
    </div>
  </div>

  <div class="card">
    <h2>Recent matches</h2>
    ${matchList(d.recent)}
  </div>`;
  $("#cmp").addEventListener("click", () => {
    chosen.p1 = { id: p.id, name: p.name };
    chosen.p2 = null;
    location.hash = "#/h2h";
    setTimeout(() => $('[data-slot="p2"] input').focus(), 50);
  });
}

// ================================================================ RANKINGS
let rankSort = { key: "rank", dir: 1 }, rankFilter = "";
async function viewRankings() {
  const tok = routeToken;
  out().innerHTML = `<p class="msg"><span class="spinner"></span>Loading rankings…</p>`;
  let d;
  try { d = await api("/api/rankings"); } catch (e) {
    if (tok === routeToken) out().innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    return;
  }
  if (tok !== routeToken) return;
  const val = (r, k) => k === "elo" ? r.elo ?? -1 : k === "rec" ? (r.won12 ?? 0) / Math.max(1, (r.won12 ?? 0) + (r.lost12 ?? 0)) :
    COURTS.includes(k) ? r.surfaceElo?.[k] ?? -1 : k === "name" ? r.name : r[k];
  const draw = () => {
    const rows = d.rows.filter((r) => !rankFilter || r.name.toLowerCase().includes(rankFilter) || r.country.toLowerCase().includes(rankFilter))
      .sort((x, y) => { const a = val(x, rankSort.key), b = val(y, rankSort.key); return (a > b ? 1 : a < b ? -1 : 0) * rankSort.dir; });
    const th = (k, label, cls = "") => `<th class="${cls}" data-k="${k}">${label}${rankSort.key === k ? (rankSort.dir > 0 ? " ▲" : " ▼") : ""}</th>`;
    $("#rtable").innerHTML = `<table>
      <tr>${th("rank", "ATP")}${th("name", "Player")}${th("points", "Points", "num")}${th("elo", "Elo", "num")}${th("eloRank", "Elo rank", "num")}
        ${COURTS.map((c) => th(c, COURT_NAME[c].replace("Outdoor ", "").replace("Indoor Hard", "Indoor"), "num")).join("")}${th("rec", "12-mo W-L", "num")}</tr>
      ${rows.map((r) => `<tr>
        <td>${r.rank} ${r.move ? `<span class="${r.move > 0 ? "up" : "down"}" style="font-size:11px">${r.move > 0 ? "▲" : "▼"}${Math.abs(r.move)}</span>` : ""}</td>
        <td><img class="thumb" loading="lazy" src="/img/${r.id}" onerror="this.style.visibility='hidden'">${plink(r.id, r.name)} <span class="muted" style="font-size:12px">${esc(r.country)}</span></td>
        <td class="num">${r.points.toLocaleString()}</td>
        <td class="num"><b>${r.elo ?? "—"}</b></td>
        <td class="num">${r.eloRank ?? "—"}</td>
        ${COURTS.map((c) => `<td class="num">${r.surfaceElo?.[c] ?? "—"}</td>`).join("")}
        <td class="num">${r.won12 != null ? `${r.won12}-${r.lost12}` : "—"}</td>
      </tr>`).join("")}
    </table>`;
    $("#rtable").querySelectorAll("th").forEach((h) => h.addEventListener("click", () => {
      const k = h.dataset.k;
      rankSort = { key: k, dir: rankSort.key === k ? -rankSort.dir : (["rank", "eloRank", "name"].includes(k) ? 1 : -1) };
      draw();
    }));
  };
  out().innerHTML = `
    <div class="pagehead">
      <div><h1>Rankings</h1><p>Official ATP ranking next to Tennis IQ Elo (overall and per court). Click a column to sort.</p></div>
      <div class="pick" style="flex:0 1 260px"><input id="rfilter" placeholder="Filter by name or country" value="${esc(rankFilter)}"></div>
    </div>
    <div class="card tablewrap" id="rtable"></div>`;
  $("#rfilter").addEventListener("input", (e) => { rankFilter = e.target.value.toLowerCase(); draw(); });
  draw();
  every(30 * 60000, viewRankings);
}

// ================================================================ DATA & MODELS
let labOptions = null, selectedModel = null;
async function viewData() {
  const tok = routeToken;
  if (!out().querySelector(".data-page")) out().innerHTML = `<p class="msg"><span class="spinner"></span>Loading data…</p>`;
  let st, models, gr;
  try {
    [st, models, labOptions, gr] = await Promise.all([api("/api/data/status"), api("/api/lab/models"), labOptions || api("/api/lab/options"), api("/api/games/report")]);
  } catch (e) {
    if (tok === routeToken) out().innerHTML = `<p class="msg err">${esc(e.message)}</p>`;
    return;
  }
  if (tok !== routeToken) return;
  const busy = st.engine.phase !== "idle";
  if (out().querySelector(".data-page")) {
    // Already drawn: only refresh the live parts so the model form isn't wiped
    $("#dstatus").innerHTML = statusHTML(st);
    $("#models").innerHTML = modelsHTML(models);
    bindModels(models);
  } else {
    drawData(st, models, gr);
  }
  every(busy ? 3000 : 30000, viewData);
}

function statusHTML(st) {
  const e = st.engine, ds = st.dataset;
  const busy = e.phase !== "idle";
  const crawlPct = e.crawl_total ? (100 * e.crawl_done) / e.crawl_total : 100;
  return `<div style="display:flex;justify-content:space-between;gap:12px;flex-wrap:wrap;align-items:center">
      <div>${busy ? `<span class="spinner"></span><b>${esc(e.phase)}</b>` : `<span class="pill green">UP TO DATE</span>`}
        <span class="muted" style="margin-left:8px">Last update ${ago(e.last_update)} · auto-updates every ${st.everyMinutes} min</span>
        ${e.error ? `<div class="err" style="margin-top:6px">${esc(e.error)}</div>` : ""}</div>
      <div style="display:flex;gap:8px"><button class="btn-sm btn-ghost" id="refresh">Update now</button>
        <a href="/api/data/download.csv"><button class="btn-sm btn-ghost">Download CSV</button></a></div>
    </div>
    ${busy && e.crawl_total ? `<div class="progress"><i style="width:${crawlPct}%"></i></div><p class="muted" style="font-size:12px">${e.crawl_done} / ${e.crawl_total} player histories downloaded</p>` : ""}
    ${ds.ready ? `<div class="kpis" style="margin-top:14px">
      <div class="stat"><small>Matches</small><b>${ds.matches.toLocaleString()}</b></div>
      <div class="stat"><small>Players</small><b>${ds.players.toLocaleString()}</b></div>
      <div class="stat"><small>Full careers crawled</small><b>${ds.crawled}</b></div>
      <div class="stat"><small>Date range</small><b style="font-size:15px">${ds.from.slice(0, 4)} – ${ds.to}</b></div>
    </div>` : ""}`;
}

function gamesReportHTML(r) {
  if (!r || !r.games) return "";
  const g = r.games, z = r.sets;
  return `<div class="card">
    <h2>Games &amp; sets model: strict backtest</h2>
    <p class="muted" style="margin-top:0">Trained ${r.years.train.join("–")} (${r.rows.train.toLocaleString()} best-of-3 matches), tuned on ${r.years.validation}, scored once on ${r.years.test} (${r.rows.test.toLocaleString()} matches it never saw).</p>
    <div class="kpis">
      <div class="stat"><small>Games: avg miss</small><b>${g.mae}</b> <span class="muted">vs ${g.baseline_mae} guessing avg</span></div>
      <div class="stat"><small>Within 3 games</small><b>${g.within3}%</b> <span class="muted">vs ${g.within3_baseline}%</span></div>
      <div class="stat"><small>3rd set AUC</small><b>${z.auc}</b> <span class="muted">0.5 = coin flip</span></div>
      <div class="stat"><small>3rd set log loss</small><b>${z.log_loss}</b> <span class="muted">vs ${z.baseline_log_loss}</span></div>
    </div>
    <h3 class="bt-h" style="margin-top:14px">Over/under vs a fair per-match line</h3>
    <table><tr><th>Bet when model is ≥ this far from line</th><th class="num">Bets</th><th class="num">Model hit</th><th class="num">Always-under hit</th><th class="num">Real skill</th></tr>
      ${g.over_under.map((o) => `<tr><td>${o.edge} games</td><td class="num">${o.bets.toLocaleString()}</td><td class="num">${o.hit ?? "—"}%</td><td class="num">${o.always_under ?? "—"}%</td>
        <td class="num ${o.hit - o.always_under > 0 ? "up" : "down"}">${o.hit != null ? (o.hit - o.always_under >= 0 ? "+" : "") + (o.hit - o.always_under).toFixed(1) : "—"}</td></tr>`).join("")}</table>
    <div class="honest"><b>Verdict</b><ul>${r.verdict.map((v) => `<li>${esc(v)}</li>`).join("")}</ul></div>
  </div>`;
}

function drawData(st, models, gr) {
  const ds = st.dataset, o = labOptions;
  const years = ds.ready ? Object.entries(ds.byYear) : [];
  const maxY = Math.max(1, ...years.map(([, v]) => v));
  out().innerHTML = `<div class="data-page">
  <div class="pagehead"><div><h1>Data &amp; Models</h1>
    <p>Training data: every singles match from the full ATP careers of the top ${st.crawlSize} players (tour, Challenger, ITF, Davis Cup), crawled from atptour.com.
    Each row stores the features <i>as they were before the match</i>, so models never see the future.</p></div></div>

  <div class="card" id="dstatus">${statusHTML(st)}</div>

  ${ds.ready ? `<div class="grid2">
    <div class="card">
      <h2>How predictable is each court?</h2>
      <table><tr><th>Court</th><th class="num">Matches</th><th class="num">Higher rank wins</th><th class="num">Higher Elo wins</th></tr>
      ${ds.byCourt.map((c) => `<tr><td>${COURT_NAME[c.court]}</td><td class="num">${c.matches.toLocaleString()}</td><td class="num">${c.favWin ?? "—"}%</td><td class="num">${c.eloWin}%</td></tr>`).join("")}</table>
    </div>
    <div class="card chart">
      <h2>Matches per year</h2>
      <svg viewBox="0 0 520 170">${years.map(([y, v], i) => {
        const w = 500 / years.length, h = (130 * v) / maxY;
        return `<rect x="${10 + i * w}" y="${140 - h}" width="${w - 2}" height="${h}" fill="${+y === new Date().getFullYear() ? "#22c55e" : "#444"}"><title>${y}: ${v}</title></rect>
          ${i % 4 === 0 ? `<text x="${10 + i * w + w / 2}" y="158" text-anchor="middle">${y}</text>` : ""}`;
      }).join("")}</svg>
    </div>
  </div>` : ""}

  <div class="card">
    <h2>Build a model</h2>
    <div class="form">
      <label>Name<input id="m-name" value="My model ${models.models.length + 1}"></label>
      <label>Model type<select id="m-type">${Object.keys(o.types).map((t) => `<option value="${t}">${t.replace("_", " ")}</option>`).join("")}</select></label>
      <label>Train from year<input id="m-from" type="number" value="2010" min="2000"></label>
      <label>Train through year<input id="m-to" type="number" placeholder="last year (auto)"></label>
      <label>Matches to use<select id="m-levels"><option value="tour_ch">Tour + Challenger</option><option value="tour">Tour level only</option><option value="all">Everything (incl. ITF)</option></select></label>
      <label>Min. prior matches<input id="m-min" type="number" value="10" min="0"></label>
      <span id="m-params" style="display:contents"></span>
    </div>
    <p class="typehint" id="m-hint"></p>
    <div class="feats" id="m-feats">${Object.entries(o.features).map(([k, desc]) => `<label><input type="checkbox" value="${k}" ${o.default.features.includes(k) ? "checked" : ""}><span>${k}<small>${esc(desc)}</small></span></label>`).join("")}</div>
    <div style="display:flex;gap:16px;align-items:center;flex-wrap:wrap">
      <button id="m-train">Train model</button>
      <label class="muted" style="font-size:13px"><input type="checkbox" id="m-auto" checked style="accent-color:var(--win)"> Auto-retrain when new matches arrive</label>
      <label class="muted" style="font-size:13px"><input type="checkbox" id="m-act" style="accent-color:var(--win)"> Make active after training</label>
      <span id="m-msg" class="muted"></span>
    </div>
  </div>

  <div class="card"><h2>Your models</h2><div id="models">${modelsHTML(models)}</div></div>

  ${gamesReportHTML(gr)}

  ${ds.ready ? `<div class="card tablewrap"><h2>Latest matches in the dataset</h2>
    <table><tr><th>Date</th><th>Event</th><th>Court</th><th>Rd</th><th>Winner</th><th>Loser</th><th>Score</th></tr>
    ${ds.recent.map((r) => { const p1won = r.winner === r.p1; const W = p1won ? r.p1_name : r.p2_name, L = p1won ? r.p2_name : r.p1_name;
      return `<tr><td>${r.date}</td><td>${esc(r.event)} <span class="muted">${esc(r.level)}</span></td><td>${COURT_NAME[r.court]}</td><td>${esc(r.round)}</td><td style="color:var(--win)">${esc(W)}</td><td>${esc(L)}</td><td class="muted">${esc(r.score)}</td></tr>`; }).join("")}
    </table></div>` : ""}
  </div>`;

  const typeSel = $("#m-type");
  const PARAMS = {
    logistic: [["C", "Regularization C", 1.0]],
    gboost: [["max_depth", "Max depth", 4], ["learning_rate", "Learning rate", 0.05], ["max_iter", "Trees", 300]],
    random_forest: [["n_estimators", "Trees", 200], ["max_depth", "Max depth", 10]],
    mlp: [["hidden", "Hidden units", 32]],
    elo: [],
  };
  const syncType = () => {
    const t = typeSel.value;
    $("#m-hint").textContent = o.types[t];
    $("#m-params").innerHTML = PARAMS[t].map(([k, l, v]) => `<label>${l}<input data-p="${k}" type="number" step="any" value="${v}"></label>`).join("");
    $("#m-feats").style.opacity = t === "elo" ? 0.35 : 1;
  };
  typeSel.addEventListener("change", syncType);
  syncType();
  $("#m-train").addEventListener("click", trainModel);
  bindModels(models);
}

async function trainModel() {
  const btn = $("#m-train"), msg = $("#m-msg");
  const cfg = {
    name: $("#m-name").value.trim() || "Untitled",
    type: $("#m-type").value,
    features: [...document.querySelectorAll("#m-feats input:checked")].map((i) => i.value),
    train_from: +$("#m-from").value || 2010,
    train_to: $("#m-to").value ? +$("#m-to").value : null,
    levels: $("#m-levels").value,
    min_matches: +$("#m-min").value || 0,
    params: Object.fromEntries([...document.querySelectorAll("[data-p]")].map((i) => [i.dataset.p, +i.value])),
    auto_retrain: $("#m-auto").checked,
    activate: $("#m-act").checked,
  };
  btn.disabled = true;
  msg.innerHTML = `<span class="spinner"></span>Training ${esc(cfg.name)}… (boosting / forests / neural nets can take up to a minute)`;
  try {
    const m = await api("/api/lab/train", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(cfg) });
    msg.innerHTML = `<span style="color:var(--win)">✓ ${esc(m.name)}: ${m.metrics.accuracy}% accuracy on ${m.n_test.toLocaleString()} unseen matches</span>`;
    selectedModel = m.slug;
    const models = await api("/api/lab/models");
    $("#models").innerHTML = modelsHTML(models);
    bindModels(models);
  } catch (e) {
    msg.innerHTML = `<span class="err">${esc(e.message)}</span>`;
  }
  btn.disabled = false;
}

function modelsHTML(models) {
  if (!models.models.length) return `<p class="empty">No models yet — the first ones train automatically once the dataset is built.</p>`;
  const sel = models.models.find((m) => m.slug === selectedModel) || models.models.find((m) => m.slug === models.active) || models.models[0];
  selectedModel = sel.slug;
  const best = Math.min(...models.models.map((m) => m.metrics.log_loss));
  return `<div class="tablewrap"><table class="models">
    <tr><th>Model</th><th>Type</th><th class="num">Accuracy</th><th class="num">vs rank favourite</th><th class="num">Log loss</th><th class="num">Brier</th><th class="num">AUC</th><th>Trained / tested on</th><th></th></tr>
    ${models.models.map((m) => `<tr data-slug="${m.slug}" class="${m.slug === sel.slug ? "sel" : ""}" style="cursor:pointer">
      <td><b>${esc(m.name)}</b> ${m.config?.auto_retrain ? `<span class="pill" title="Retrains automatically">AUTO</span>` : ""}</td>
      <td>${esc(m.type.replace("_", " "))}</td>
      <td class="num"><b>${m.metrics.accuracy}%</b></td>
      <td class="num ${m.metrics.accuracy >= m.metrics.favorite_accuracy ? "up" : "down"}">${(m.metrics.accuracy - m.metrics.favorite_accuracy >= 0 ? "+" : "") + (m.metrics.accuracy - m.metrics.favorite_accuracy).toFixed(1)}</td>
      <td class="num" style="${m.metrics.log_loss === best ? "color:var(--win);font-weight:700" : ""}">${m.metrics.log_loss}</td>
      <td class="num">${m.metrics.brier}</td>
      <td class="num">${m.metrics.auc}</td>
      <td class="muted">${m.train_years.join("–")} / ${m.test_years[0] === m.test_years[1] ? m.test_years[0] : m.test_years.join("–")} · ${m.n_train.toLocaleString()} / ${m.n_test.toLocaleString()}</td>
      <td>${m.slug === models.active ? `<span class="pill green">ACTIVE</span>` : `<button class="btn-sm btn-ghost" data-act="${m.slug}">Use</button>`}
        <button class="btn-sm btn-ghost" data-del="${m.slug}" title="Delete">✕</button></td>
    </tr>`).join("")}
  </table></div>
  <p class="muted" style="font-size:12px">The <b>active</b> model powers Billy's Model and the points &amp; sets estimate. Lower log loss = better-calibrated probabilities (the best metric for betting-style use); accuracy = % of winners picked.</p>
  ${modelDetail(sel)}`;
}

function modelDetail(m) {
  const imp = Object.entries(m.importance || {}).sort((a, b) => Math.abs(b[1]) - Math.abs(a[1]));
  const maxI = Math.max(1e-9, ...imp.map(([, v]) => Math.abs(v)));
  const cal = m.calibration || [];
  const S = 220, P = 30;
  const X = (v) => P + v * (S - P - 8), Y = (v) => S - P - v * (S - P - 8);
  return `<div class="grid2" style="margin-top:16px">
    <div>
      <h3 style="margin:0 0 10px;font-size:15px">${esc(m.name)} — what drives it</h3>
      ${imp.length ? `<div class="bars">${imp.map(([k, v]) => `<div class="b"><span>${k}</span>
        <div class="track"><i class="${v < 0 ? "neg" : ""}" style="left:0;width:${(100 * Math.abs(v)) / maxI}%"></i></div>
        <span class="num muted">${v.toFixed(3)}</span></div>`).join("")}</div>
        <p class="muted" style="font-size:12px">${m.type === "logistic" ? "Standardized coefficients (red = pushes the other way)." : m.type === "random_forest" ? "Impurity-based importance." : "Permutation importance: how much worse the log loss gets when a feature is shuffled."}</p>`
        : `<p class="muted">Elo-only model: a single input.</p>`}
      <h3 style="margin:16px 0 8px;font-size:15px">Accuracy by court (test set)</h3>
      <div class="kpis">${Object.entries(m.by_court || {}).map(([c, v]) => `<div class="stat"><small>${COURT_NAME[c]}</small><b>${v}%</b></div>`).join("")}</div>
    </div>
    <div class="chart">
      <h3 style="margin:0 0 10px;font-size:15px">Calibration — when it says X%, does it happen X% of the time?</h3>
      <svg viewBox="0 0 ${S} ${S}" style="max-width:360px">
        <line x1="${X(0)}" y1="${Y(0)}" x2="${X(1)}" y2="${Y(1)}" stroke="#333" stroke-dasharray="4"/>
        ${[0, 0.5, 1].map((t) => `<text x="${X(t)}" y="${S - 12}" text-anchor="middle">${t * 100}%</text><text x="${P - 4}" y="${Y(t) + 4}" text-anchor="end">${t * 100}%</text>`).join("")}
        <path d="${cal.map((c, i) => `${i ? "L" : "M"}${X(c.pred)},${Y(c.actual)}`).join("")}" fill="none" stroke="#22c55e" stroke-width="2"/>
        ${cal.map((c) => `<circle cx="${X(c.pred)}" cy="${Y(c.actual)}" r="3.5" fill="#22c55e"><title>predicted ${(100 * c.pred).toFixed(0)}% → actual ${(100 * c.actual).toFixed(0)}% (${c.n} matches)</title></circle>`).join("")}
      </svg>
      <p class="muted" style="font-size:12px">Closer to the dashed line = more trustworthy probabilities.</p>
    </div>
  </div>`;
}

function bindModels(models) {
  document.querySelectorAll("[data-slug]").forEach((tr) => tr.addEventListener("click", (e) => {
    if (e.target.closest("button")) return;
    selectedModel = tr.dataset.slug;
    $("#models").innerHTML = modelsHTML(models);
    bindModels(models);
  }));
  document.querySelectorAll("[data-act]").forEach((b) => b.addEventListener("click", async () => {
    const m = await api(`/api/lab/models/${b.dataset.act}/activate`, { method: "POST" });
    $("#models").innerHTML = modelsHTML(m); bindModels(m);
  }));
  document.querySelectorAll("[data-del]").forEach((b) => b.addEventListener("click", async () => {
    if (!confirm("Delete this model?")) return;
    const m = await api(`/api/lab/models/${b.dataset.del}`, { method: "DELETE" });
    $("#models").innerHTML = modelsHTML(m); bindModels(m);
  }));
  const r = $("#refresh");
  if (r && !r.dataset.bound) {
    r.dataset.bound = 1;
    r.addEventListener("click", async () => { await api("/api/data/refresh", { method: "POST" }); setTimeout(viewData, 800); });
  }
}

// ================================================================ MY MODEL (Billy's checklist)
async function viewMyModel() {
  const tok = routeToken;
  if (!out().querySelector(".mymodel-page")) out().innerHTML = `<p class="msg"><span class="spinner"></span>Scanning today's and tomorrow's ATP + Challenger matches…</p>`;
  let d;
  try { d = await api("/api/system"); } catch (e) {
    if (tok === routeToken) out().innerHTML = `<p class="msg"><span class="spinner"></span>${esc(e.message)} (retrying automatically)</p>`;
    return every(10000, viewMyModel);
  }
  if (tok !== routeToken) return;
  const bt = d.backtest;
  const fav = (c) => c.match.status === "finished" && c.match.winner
    ? ((c.match.winner === "home") === (c.fav.name === c.match.home)) : null;
  const done = d.passing.map(fav).filter((x) => x !== null);

  const up = d.passing.filter((c) => c.match.status !== "finished");
  out().innerHTML = `<div class="mymodel-page">
    <div class="pagehead"><div><h1>Billy's Model</h1>
      <p>Upcoming ATP + Challenger matches that pass every test, then near misses that failed one test (marked ✕). Updates every 2 minutes.</p></div></div>
    <div class="testline">
      <span>Rank gap ≥ ${d.rules.gap}</span><span>Last 10 ≥ 8/10 <em>(7/10 OK)</em></span><span>Better on this court this season</span>
    </div>
    ${up.length ? `<div class="cards">${up.map(reportCard).join("")}</div>`
      : `<p class="msg">No upcoming matches pass all the tests right now. Checks again every 2 minutes.</p>`}
  </div>`;
  every(120000, viewMyModel);
}

const mark = (ch) => ch.ok ? (ch.warn ? `<span class="warn">⚠</span>` : `<span class="up">✓</span>`) : (ch.warn ? `<span class="warn">⚠</span>` : `<span class="down">✗</span>`);
const h2hHref = (c) => `#/h2h?p1=${c.fav.id}&p2=${c.dog.id}&n1=${encodeURIComponent(c.fav.name)}&n2=${encodeURIComponent(c.dog.name)}`;

function reportCard(c) {
  const m = c.match, sm = c.summary || {};
  const won = m.status === "finished" && m.winner ? ((m.winner === "home") === (c.fav.name === m.home)) : null;
  const when = m.status === "finished" ? "Final" : m.status === "inprogress" ? "LIVE"
    : m.start ? new Date(m.start * 1000).toLocaleString([], { weekday: "short", hour: "numeric", minute: "2-digit" }) : "TBA";
  const [gapCk, formCk, courtCk] = c.checks;
  const state = (ch) => !ch ? "na" : ch.ok ? (ch.warn ? "warn" : "ok") : (ch.warn ? "warn" : "bad");
  const icon = { ok: "✓", warn: "!", bad: "✕", na: "–" };
  const tile = (ch, value, label) => `<div class="tile t-${state(ch)}"><i>${icon[state(ch)]}</i><b>${value}</b><small>${label}</small></div>`;
  const courtName = COURT_NAME[m.court].replace("Outdoor ", "");
  const dots = (arr) => `<span class="dots">${(arr || []).map((r) => `<i class="${r}"></i>`).join("")}</span>`;
  const rec = (r) => r ? `${r.won}-${r.lost}` : "—";
  const prob = c.model;
  const warnings = [];
  if (sm.favL10 === 7) warnings.push("Only 7/10 recent form");
  if (formCk && !formCk.ok) warnings.push(`Near miss: only ${sm.favL10}/10 recent form (test needs 7+)`);
  if (courtCk && !courtCk.ok) warnings.push(sm.favCourt == null || sm.dogCourt == null
    ? `Near miss: no ${courtName.toLowerCase()} record to compare`
    : `Near miss: ${last(c.dog.name)} has the better ${courtName.toLowerCase()} record (${sm.dogCourt}% vs ${sm.favCourt}%)`);
  if (sm.courtWindow === "last12") warnings.push(`Few ${courtName.toLowerCase()} matches this season, used last 12 months`);
  const h = c.history;

  return `<div class="rc ${won === true ? "rc-won" : won === false ? "rc-lost" : ""}">
    ${won !== null ? `<div class="rc-result ${won ? "w" : "l"}">${won ? "✓ PICK WON" : "✕ PICK LOST"}</div>` : ""}
    <div class="rc-meta">
      <span class="grade g-${(c.grade || "C").replace("+", "p")}">${c.grade || ""}</span>
      <span>${m.status === "inprogress" ? `<span class="pill live">LIVE</span>` : esc(when)}</span>
      <span class="dotsep">·</span><span>${esc(m.tour)}</span>
      <span class="dotsep">·</span><span class="ell">${esc(m.tournament.split(",")[0])}</span>
      <span class="court-badge c-${m.court}">${courtName}</span>
    </div>

    <div class="rc-pick">
      ${photo({ id: c.fav.id, name: c.fav.name }, "rc-photo")}
      <div class="rc-who">
        <small>PICK</small>
        <a class="plink rc-name" href="#/player/${c.fav.id}">${esc(c.fav.name)}</a>
        <span class="muted">#${c.fav.rank || "NR"} vs <a class="plink" href="#/player/${c.dog.id}">${esc(c.dog.name)}</a> #${c.dog.rank || "NR"}</span>
      </div>
      ${prob != null ? `<div class="rc-prob"><b>${Math.round(prob)}%</b><small>to win</small></div>` : ""}
    </div>
    ${prob != null ? `<div class="rc-bar"><i style="width:${prob}%"></i></div>` : ""}

    <div class="tiles">
      ${tile(gapCk, c.gap, "rank gap")}
      ${tile(formCk, `${sm.favL10 ?? "—"}/10`, "last 10")}
      ${tile(courtCk, sm.favCourt != null ? `${sm.favCourt}%` : "—", `${courtName.toLowerCase()} vs ${sm.dogCourt ?? "—"}%`)}
    </div>

    ${warnings.length ? `<div class="rc-warn">! ${warnings.map(esc).join(" · ")}</div>` : ""}

    <details class="rc-more">
      <summary>Details</summary>
      <div class="rc-detail">
        <div class="dl-row"><span>Last 10</span><span>${last(c.fav.name)} <b>${sm.favL10}-${(sm.favL10n || 0) - (sm.favL10 || 0)}</b> ${dots(formCk?.fav10)}</span></div>
        <div class="dl-row"><span></span><span>${last(c.dog.name)} <b>${sm.dogL10}-${(sm.dogL10n || 0) - (sm.dogL10 || 0)}</b> ${dots(formCk?.dog10)}</span></div>
        <div class="dl-row"><span>${courtName} record</span><span>${last(c.fav.name)} <b>${rec(sm.favCourtRec)}</b> · ${last(c.dog.name)} <b>${rec(sm.dogCourtRec)}</b> <span class="muted">(${sm.courtWindow === "season" ? "this season" : "last 12 months"})</span></span></div>
        ${h ? `<div class="dl-row"><span>History</span><span>Picks like this won <b>${h.form.win}%</b> since 2015 · break-even odds <b>${h.form.fair_odds}</b> (${americanOdds(h.form.fair_odds)})</span></div>` : ""}
        <a class="plink" href="${h2hHref(c)}">Open full head-to-head →</a>
      </div>
    </details>
  </div>`;
}

function btTable(rows, label = "") {
  return `<table><tr><th>${label}</th><th class="num">Matches</th><th class="num">Fav won</th><th class="num">95% range</th><th class="num">Break-even odds</th></tr>
    ${rows.filter((r) => r.n).map((r) => `<tr><td>${esc(COURT_NAME[r.key] || r.key || r.step)}</td><td class="num">${r.n.toLocaleString()}</td>
      <td class="num"><b>${r.win}%</b></td><td class="num muted">${r.lo}–${r.hi}%</td><td class="num">${r.fair_odds}</td></tr>`).join("")}</table>`;
}

function backtestHTML(bt) {
  const f = bt.funnel, form = bt.form, mdl = bt.model;
  const sys = f[3], gapOnly = f[1];
  const lowModel = mdl.find((r) => r.key === "<60%"), highModel = mdl.find((r) => r.key === "80–90%");
  const years = bt.years.filter((y) => y.n);
  return `<div class="card">
    <h2>Backtest: your system, ${bt.from}–${bt.to}</h2>
    <div class="honest" style="margin-top:0">
      <b>The honest read</b>
      <ul>
        <li>Matches that pass your checklist: the favourite won <b>${sys.win}%</b> of ${sys.n.toLocaleString()} times, vs <b>${gapOnly.win}%</b> for a ranking gap alone. The filters add about ${(sys.win - gapOnly.win).toFixed(1)} points.</li>
        <li>7/10 vs 8+/10 barely matters: <b>${form[1].win}%</b> vs <b>${form[0].win}%</b>. The court check matters more: 8+/10 favourites who <i>fail</i> it win only <b>${form[2].win}%</b>.</li>
        <li>The model cross-check is the strongest filter of all: when it gives the favourite under 60%, they win just <b>${lowModel?.win}%</b>; at 80–90% they win <b>${highModel?.win}%</b>. Skip the ones the model doesn't back.</li>
        <li>A high win rate is not profit. At ${sys.win}% you need odds better than <b>${sys.fair_odds}</b> (about ${americanOdds(sys.fair_odds)}) just to break even, and these favourites are often priced shorter than that. Compare the break-even odds column with the odds you're actually getting.</li>
      </ul>
    </div>
    <div class="grid2" style="margin-top:14px">
      <div><h3 class="bt-h">How the funnel narrows</h3>${btTable(f.map((r) => ({ ...r, key: r.step })))}</div>
      <div><h3 class="bt-h">Last-10 form &amp; court check</h3>${btTable(form)}</div>
      <div><h3 class="bt-h">Model cross-check (Elo, as of match day)</h3>${btTable(mdl, "Model gives fav")}</div>
      <div><h3 class="bt-h">Ranking gap</h3>${btTable(bt.gap, "Gap")}</div>
      <div><h3 class="bt-h">Tour vs Challenger</h3>${btTable(bt.tour)}</div>
      <div><h3 class="bt-h">Court type</h3>${btTable(bt.court)}</div>
    </div>
    <h3 class="bt-h" style="margin-top:16px">Favourite win % by year (system matches)</h3>
    <div class="yearbars">${years.map((y) => `<div title="${y.key}: ${y.win}% of ${y.n}"><i style="height:${Math.max(0, (y.win - 50) * 2)}%"></i><span>${y.win}</span><small>${y.key.slice(2)}</small></div>`).join("")}</div>
  </div>`;
}

function americanOdds(dec) {
  if (!dec) return "—";
  return dec >= 2 ? `+${Math.round((dec - 1) * 100)}` : `-${Math.round(100 / (dec - 1))}`;
}

route();
