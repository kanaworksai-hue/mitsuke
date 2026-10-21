// Mitsuke web app — plain JS, no build step. Talks to the local server (server.py) only.
"use strict";

window.__errors = []; // read by browser test drivers
addEventListener("error", (e) => window.__errors.push(String(e.message)));
addEventListener("unhandledrejection", (e) => window.__errors.push(String(e.reason && e.reason.stack || e.reason)));

const $ = (s, r = document) => r.querySelector(s);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const el = (html) => { const t = document.createElement("template"); t.innerHTML = html.trim(); return t.content.firstChild; };
const fmtTime = (t) => { t = Math.floor(t || 0); const h = Math.floor(t / 3600), m = Math.floor(t % 3600 / 60), s = t % 60; return (h ? `${h}:${String(m).padStart(2, "0")}` : `${m}`) + ":" + String(s).padStart(2, "0"); };
const fmtBytes = (b) => b >= 1 << 30 ? (b / (1 << 30)).toFixed(1) + " GB" : b >= 1 << 20 ? (b / (1 << 20)).toFixed(1) + " MB" : Math.max(1, Math.round(b / 1024)) + " KB";
const num = (n) => (n ?? 0).toLocaleString();
const debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
// Enter that only confirms an IME candidate (Japanese / Chinese input) must not submit; Safari reports it as keyCode 229
const composing = (e) => e.isComposing || e.keyCode === 229;

const ICON = {
  img: '<svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><rect x="3" y="4" width="18" height="16" rx="2"/><circle cx="9" cy="10" r="2"/><path d="m21 16-5-5-9 9"/></svg>',
  similar: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="11" height="11" rx="2"/><rect x="10" y="10" width="11" height="11" rx="2"/></svg>',
  board: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="8" height="10" rx="1"/><rect x="13" y="3" width="8" height="6" rx="1"/><rect x="3" y="15" width="8" height="6" rx="1"/><rect x="13" y="11" width="8" height="10" rx="1"/></svg>',
  folder: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M3 6a1 1 0 0 1 1-1h5l2 2h9a1 1 0 0 1 1 1v10a1 1 0 0 1-1 1H4a1 1 0 0 1-1-1z"/></svg>',
  finder: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><path d="M14 3h7v7M10 14 21 3M19 14v5a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7a2 2 0 0 1 2-2h5"/></svg>',
  search: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/></svg>',
  all: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="1.8"><rect x="3" y="3" width="7" height="7" rx="1"/><rect x="14" y="3" width="7" height="7" rx="1"/><rect x="3" y="14" width="7" height="7" rx="1"/><rect x="14" y="14" width="7" height="7" rx="1"/></svg>',
  plus: '<svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><path d="M12 5v14M5 12h14"/></svg>',
  play: '<svg viewBox="0 0 24 24" width="11" height="11" fill="currentColor"><path d="M7 4v16l13-8z"/></svg>',
  sun: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
  moon: '<svg viewBox="0 0 24 24" width="17" height="17" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linejoin="round"><path d="M20 14.5A8 8 0 0 1 9.5 4a8 8 0 1 0 10.5 10.5z"/></svg>',
  grid: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><rect x="3" y="3" width="8" height="8" rx="1"/><rect x="13" y="3" width="8" height="8" rx="1"/><rect x="3" y="13" width="8" height="8" rx="1"/><rect x="13" y="13" width="8" height="8" rx="1"/></svg>',
  rows: '<svg viewBox="0 0 24 24" width="15" height="15" fill="currentColor"><rect x="3" y="4" width="11" height="7" rx="1"/><rect x="16" y="4" width="5" height="7" rx="1"/><rect x="3" y="13" width="6" height="7" rx="1"/><rect x="11" y="13" width="10" height="7" rx="1"/></svg>',
};

// ---- state ------------------------------------------------------------------------------------

const QS = new URLSearchParams(location.search); // ?lang= &theme= override without saving (links, screenshots)
const S = {
  lang: QS.get("lang") || localStorage.getItem("mitsuke.lang") || ({ zh: "zh", ja: "ja" }[navigator.language.slice(0, 2)] || "en"),
  theme: QS.get("theme") || localStorage.getItem("mitsuke.theme") || "dark",
  layout: localStorage.getItem("mitsuke.layout") || "grid",
  size: +(localStorage.getItem("mitsuke.size") || 180),
  view: "all", // all | search | board
  text: "", chips: [], weight: 0.5, kind: "all", root: null, board: null,
  results: [], total: null, ms: null, sel: -1, loading: false, exhausted: false, req: 0,
  status: null, folders: [], boards: [], lastBoard: +(localStorage.getItem("mitsuke.lastBoard") || 0) || null,
  loupe: false,
};
const KINDS = [["all", null], ["photo", ["photo"]], ["raw", ["raw"]], ["psd", ["psd"]], ["hdr", ["hdr"]], ["video", ["video"]]];

function t(key, vars) {
  let s = (I18N[S.lang] && I18N[S.lang][key]) ?? I18N.en[key] ?? key;
  if (vars) for (const [k, v] of Object.entries(vars)) s = s.replaceAll(`{${k}}`, v);
  return s;
}

// ---- server -----------------------------------------------------------------------------------

async function api(method, path, body, opts = {}) {
  const headers = { "X-Mitsuke": "1", ...(opts.headers || {}) };
  let data = body;
  if (body !== undefined && !(body instanceof ArrayBuffer) && !(body instanceof Blob)) {
    headers["Content-Type"] = "application/json";
    data = JSON.stringify(body);
  }
  const r = await fetch(path, { method, headers, body: data });
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw Object.assign(new Error(j.error || r.statusText), { status: r.status });
  return j;
}

// ---- URL state (so a search is a link, and Back works) -----------------------------------------

const hasQuery = () => !!(S.text.trim() || S.chips.length);

/** → true if the URL changed. Each search gets its own history entry; `replace` while a search is still being typed. */
function writeHash(replace = false) {
  const p = new URLSearchParams();
  if (S.text.trim()) p.set("q", S.text);
  const img = S.chips.filter((c) => !c.upload).map((c) => c.board ? `b${c.board}` : c.shot ? `s${c.shot}` : `f${c.file}`);
  if (img.length) p.set("img", img.join(","));
  if (img.length && S.text.trim() && S.weight !== 0.5) p.set("w", S.weight);
  if (S.kind !== "all") p.set("kind", S.kind);
  if (S.root) p.set("root", S.root);
  if (S.board) p.set("board", S.board);
  const h = p.toString();
  if (("#" + h) === location.hash || (h === "" && !location.hash)) return false;
  history[replace ? "replaceState" : "pushState"](null, "", h ? "#" + h : location.pathname + location.search);
  return true;
}

async function readHash() {
  typingEntry = false;
  const p = new URLSearchParams(location.hash.slice(1));
  S.text = p.get("q") || "";
  S.kind = p.get("kind") || "all";
  S.root = p.get("root") || null;
  S.board = +p.get("board") || null;
  S.weight = p.has("w") ? +p.get("w") : 0.5;
  S.chips = [];
  for (const tok of (p.get("img") || "").split(",").filter(Boolean)) {
    const id = +tok.slice(1);
    if (tok[0] === "f") {
      try { const it = await api("GET", `/api/item/${id}`); S.chips.push({ file: id, thumb: it.thumb, label: it.name }); } catch {}
    } else if (tok[0] === "s") {
      try { const s = await api("GET", `/api/shot/${id}`); S.chips.push({ shot: id, file: s.file, thumb: s.thumb, label: s.name, time: s.time }); } catch {}
    } else if (tok[0] === "b") {
      try { S.chips.push(boardChip(await api("GET", `/api/boards/${id}`))); } catch {}
    }
  }
  S.view = S.board ? "board" : hasQuery() ? "search" : "all";
}

// ---- results ----------------------------------------------------------------------------------

function kindsParam() { return (KINDS.find((k) => k[0] === S.kind) || [])[1] || null; }

/** more: next page. soft: refresh in place (new files indexed) — keeps the grid, the scroll position and the selection. */
async function load(more = false, soft = false) {
  const req = ++S.req;
  const keep = soft ? S.results[S.sel] : null;
  if (!more) { S.exhausted = false; if (!soft) { S.results = []; S.sel = -1; S.total = null; } }
  if (S.exhausted) return;
  S.loading = true;
  renderCrumb();
  if (!more && !soft) renderResults();
  const offset = more ? S.results.length : 0, k = 120;
  let kept = false; // the selected item is still there: leave the inspector (and a playing video) alone
  try {
    let r;
    if (S.view === "board") {
      r = more ? { results: [] } : await api("GET", `/api/boards/${S.board}`);
      S.boardName = r.name;
      r.results = r.results.filter((h) => S.kind === "all" || matchesKind(h));
      r.total = r.results.length;
    } else if (S.view === "search") {
      const images = S.chips.flatMap((c) => c.board ? c.items : [c.upload ? { upload: c.upload } : c.shot ? { shot: c.shot } : { file: c.file }]);
      r = await api("POST", "/api/search", { text: S.text, images, text_weight: S.weight, kinds: kindsParam(), root: S.root, k, offset });
    } else {
      r = await api("POST", "/api/browse", { kinds: kindsParam(), root: S.root, k, offset });
    }
    if (req !== S.req) return;
    S.results = more ? S.results.concat(r.results) : r.results;
    S.ms = r.ms ?? null;
    if (r.total != null) S.total = r.total;
    S.exhausted = r.results.length < k || S.view === "board" || (S.view === "search" && S.results.length >= 480);
    if (S.view === "search") S.total = S.exhausted ? S.results.length : null;
    if (!more) {
      const i = keep ? S.results.findIndex((h) => h.id === keep.id && (h.shot || 0) === (keep.shot || 0)) : -1;
      kept = i >= 0;
      S.sel = kept ? i : S.results.length ? 0 : -1;
    }
  } catch (e) {
    if (req === S.req) { toast(e.message); kept = soft; }
  } finally {
    if (req === S.req) {
      S.loading = false; renderCrumb(); renderSide(); renderResults(!more && !soft);
      if (!more && !kept) renderInspector();
    }
  }
}

function matchesKind(h) {
  const ks = kindsParam();
  if (!ks) return true;
  const map = { photo: ["jpeg", "heic", "png", "webp", "tiff", "avif"], hdr: ["exr", "hdr"] };
  return ks.flatMap((k) => map[k] || [k]).includes(h.kind);
}

const search = debounce(() => go(true), 450);
let typingEntry = false; // the current history entry is a search still being typed: update it instead of stacking more

function go(typing = false) {
  S.view = S.board && !hasQuery() ? "board" : hasQuery() ? "search" : "all";
  if (S.view === "search") S.board = null;
  const changed = writeHash(typing && typingEntry);
  if (!typing) typingEntry = false; else if (changed) typingEntry = true;
  renderComposer();
  renderSide();
  load();
}

// ---- rendering: composer ----------------------------------------------------------------------

function renderComposer() {
  const chips = $("#chips");
  chips.innerHTML = "";
  S.chips.forEach((c, i) => {
    const lab = c.board ? t("chip_board", { name: c.label, n: c.items.length }) : c.upload ? t("chip_dropped") : c.shot ? t("chip_shot", { t: fmtTime(c.time || 0) }) : t("chip_like");
    const ch = el(`<span class="chip" title="${esc(c.label || "")}">${c.thumb ? `<img src="${esc(c.thumb)}">` : ""}<span>${esc(lab)}</span><button title="${esc(t("remove"))}">×</button></span>`);
    ch.querySelector("button").onclick = () => { S.chips.splice(i, 1); go(); };
    chips.append(ch);
  });
  const q = $("#q");
  if (q.value !== S.text) q.value = S.text;
  q.placeholder = S.chips.length ? t("ph_more") : t("ph_search");
  $("#clear").hidden = !(S.text || S.chips.length);
  const mix = S.chips.length > 0 && S.text.trim().length > 0;
  $("#mix").hidden = !mix;
  $("#weight").value = Math.round(S.weight * 100);
  $("#mixval").textContent = t("mix_val", { i: Math.round((1 - S.weight) * 100), t: Math.round(S.weight * 100) });
}

// ---- rendering: sidebar -----------------------------------------------------------------------

function renderSide() {
  const side = $("#side");
  const st = S.status || {};
  const parts = [];
  parts.push(`<h4>${esc(t("library"))}</h4>`);
  if (hasQuery()) parts.push(`<button class="it ${S.view === "search" ? "on" : ""}" data-go="search">${ICON.search}<span class="nm">${esc(t("results"))}</span><span class="n">${S.view === "search" ? num(S.results.length) : ""}</span></button>`);
  parts.push(`<button class="it ${S.view === "all" && !S.root ? "on" : ""}" data-go="all">${ICON.all}<span class="nm">${esc(t("all_images"))}</span><span class="n">${num(st.indexed)}</span></button>`);
  parts.push(`<h4>${esc(t("folders"))}<button data-act="addfolder" title="${esc(t("add_folder"))}">+</button></h4>`);
  for (const f of S.folders) {
    parts.push(`<button class="it ${S.root === f.path ? "on" : ""} ${f.online ? "" : "offline"}" data-root="${esc(f.path)}" title="${esc(f.path)}">${ICON.folder}<span class="nm">${esc(f.name)}</span><span class="n">${f.online ? num(f.ok) : esc(t("offline"))}</span><span class="x" data-rmfolder="${f.id}" title="${esc(t("remove_folder"))}">×</span></button>`);
    for (const c of f.children.slice(0, 40)) parts.push(`<button class="it sub ${S.root === c.path ? "on" : ""}" data-root="${esc(c.path)}" title="${esc(c.path)}">${ICON.folder}<span class="nm">${esc(c.name)}</span><span class="n">${num(c.count)}</span></button>`);
  }
  if (S.addingFolder) parts.push(`<div class="inline"><input id="folderpath" placeholder="${esc(t("folder_ph"))}"><button class="btn" data-act="pick">${esc(t("choose"))}</button></div>`);
  parts.push(`<h4>${esc(t("boards"))}<button data-act="newboard" title="${esc(t("new_board"))}">+</button></h4>`);
  if (S.newBoard) parts.push(`<div class="inline"><input id="boardname" placeholder="${esc(t("board_ph"))}"></div>`);
  for (const b of S.boards) parts.push(`<button class="it ${S.view === "board" && S.board === b.id ? "on" : ""}" data-board="${b.id}">${ICON.board}<span class="nm">${esc(b.name)}</span><span class="n">${num(b.count)}</span></button>`);
  if (!S.boards.length && !S.newBoard) parts.push(`<div class="it add" style="font-size:12px">${esc(t("boards_hint"))}</div>`);
  // the sidebar re-renders on every search and status change: keep what is being typed in its inline inputs
  const typed = ["folderpath", "boardname"].map((id) => [id, $("#" + id)?.value, document.activeElement?.id === id]);
  side.innerHTML = parts.join("");
  for (const [id, v, f] of typed) { const n = $("#" + id); if (n && v != null) { n.value = v; if (f) n.focus(); } }
  side.querySelectorAll("[data-go]").forEach((b) => b.onclick = () => {
    if (b.dataset.go === "all") { S.text = ""; S.chips = []; S.root = null; S.board = null; }
    go();
  });
  side.querySelectorAll("[data-root]").forEach((b) => b.onclick = (e) => {
    if (e.target.dataset.rmfolder) return removeFolder(+e.target.dataset.rmfolder, b.dataset.root);
    S.root = S.root === b.dataset.root ? null : b.dataset.root; S.board = null; go();
  });
  side.querySelectorAll("[data-board]").forEach((b) => {
    b.onclick = () => { S.board = +b.dataset.board; S.text = ""; S.chips = []; S.root = null; go(); };
    b.ondragover = (e) => { e.preventDefault(); b.classList.add("drop"); };
    b.ondragleave = () => b.classList.remove("drop");
    b.ondrop = (e) => { e.preventDefault(); b.classList.remove("drop"); const it = dragItem(e); if (it) addToBoard(+b.dataset.board, [it]); };
  });
  const af = side.querySelector('[data-act="addfolder"]'); if (af) af.onclick = () => { S.addingFolder = !S.addingFolder; renderSide(); $("#folderpath")?.focus(); };
  const pk = side.querySelector('[data-act="pick"]'); if (pk) pk.onclick = pickFolder;
  const fp = $("#folderpath"); if (fp) fp.onkeydown = (e) => { if (e.key === "Enter" && !composing(e)) addFolder(fp.value); if (e.key === "Escape") { S.addingFolder = false; renderSide(); } };
  const nb = side.querySelector('[data-act="newboard"]'); if (nb) nb.onclick = () => { S.newBoard = true; renderSide(); $("#boardname").focus(); };
  const bn = $("#boardname"); if (bn) {
    bn.onkeydown = async (e) => {
      if (e.key === "Escape") { S.newBoard = false; renderSide(); }
      if (e.key === "Enter" && !composing(e)) {
        S.newBoard = false;
        if (!bn.value.trim()) return renderSide();
        try {
          const b = await api("POST", "/api/boards", { name: bn.value });
          S.lastBoard = b.id; localStorage.setItem("mitsuke.lastBoard", b.id);
        } catch (er) { toast(er.message); }
        await refreshBoards(); renderSide();
      }
    };
    bn.onblur = () => { if (S.newBoard && bn.isConnected) { S.newBoard = false; renderSide(); } };
  }
}

// ---- rendering: results -----------------------------------------------------------------------

function renderCrumb() {
  const c = $("#crumb");
  let title, extra = "";
  if (S.view === "board") {
    title = `${t("board")}: <b>${esc(S.boardName || "")}</b>`;
    extra = `<span class="sp"></span><button data-b="like">${esc(t("search_like_board"))}</button><button data-b="copy">${esc(t("copy_paths"))}</button><button data-b="rename">${esc(t("rename"))}</button><button data-b="delete">${esc(t("delete"))}</button>`;
  } else if (S.view === "search") {
    const nimg = S.chips.reduce((a, c) => a + (c.board ? c.items.length : 1), 0);
    const what = [nimg ? esc(t("crumb_images", { n: nimg })) : "", S.text.trim() ? esc(t("quote", { q: S.text.trim() })) : ""].filter(Boolean).join(" + ");
    title = `<b>${what}</b>`;
  } else title = `<b>${esc(S.root ? S.root.split("/").pop() : t("all_images"))}</b>`;
  const n = S.loading && !S.results.length ? t("searching") : S.total != null ? t("n_items", { n: num(S.total) }) : t("n_items_plus", { n: num(S.results.length) });
  const filt = [S.kind !== "all" ? t("kind_" + S.kind) : "", S.root && S.view !== "all" ? S.root.split("/").pop() : ""].filter(Boolean).join(" · ");
  c.innerHTML = `${title}<span>${esc(n)}${filt ? " · " + esc(filt) : ""}${S.ms != null && S.view === "search" ? ` · ${S.ms} ms` : ""}</span>${extra || '<span class="sp"></span>'}`;
  c.querySelectorAll("[data-b]").forEach((b) => b.onclick = () => boardAction(b.dataset.b));
}

function cardHTML(h, i) {
  const badge = h.kind === "video" ? `<span class="badge">${ICON.play} ${h.time != null ? fmtTime(h.time) : fmtTime(h.duration)}</span>` : "";
  const cap = S.view === "search" ? (S.chips.length && S.text ? h.score.toFixed(1) : h.score.toFixed(2)) : h.kind.toUpperCase();
  return `<div class="card ${i === S.sel ? "sel" : ""}" data-i="${i}" draggable="true">
    <div class="im"><img loading="lazy" src="${esc(h.thumb)}" alt=""></div>${badge}
    <div class="acts"><button data-a="similar" title="${esc(t("similar"))} (S)">${ICON.similar}</button>${S.view === "board"
      ? `<button data-a="unboard" title="${esc(t("remove_from_board"))} (Delete)">×</button>`
      : `<button data-a="board" title="${esc(t("to_board"))} (B)">${ICON.board}</button>`}</div>
    <div class="cap"><span>${esc(h.name)}</span><span>${esc(cap)}</span></div></div>`;
}

function renderResults(scrollTop = false) {
  const box = $("#results");
  if (scrollTop) box.scrollTop = 0;
  if (!S.results.length) {
    box.innerHTML = emptyHTML();
    bindEmpty();
    return;
  }
  document.documentElement.style.setProperty("--cell-size", S.size + "px");
  let html;
  if (S.layout === "rows") {
    const W = box.clientWidth - 32, H = S.size * 1.1, G = 8;
    let row = [], ar = 0, out = [];
    S.results.forEach((h, i) => {
      const a = Math.min(Math.max((h.width || 1) / (h.height || 1), 0.3), 4);
      row.push([h, i, a]); ar += a;
      if (ar * H + G * (row.length - 1) >= W) {
        const rh = (W - G * (row.length - 1)) / ar;
        out.push(`<div class="row" style="height:${rh}px">${row.map(([h, i, a]) => cardHTML(h, i).replace('class="card', `style="width:${rh * a}px" class="card`)).join("")}</div>`);
        row = []; ar = 0;
      }
    });
    if (row.length) out.push(`<div class="row" style="height:${H}px">${row.map(([h, i, a]) => cardHTML(h, i).replace('class="card', `style="width:${H * a}px" class="card`)).join("")}</div>`);
    html = `<div class="rows">${out.join("")}</div>`;
  } else html = `<div class="grid">${S.results.map(cardHTML).join("")}</div>`;
  box.innerHTML = html + (S.exhausted ? "" : `<div class="more">${esc(t("loading_more"))}</div>`);
  box.querySelectorAll(".card").forEach((c) => {
    const i = +c.dataset.i;
    c.onclick = (e) => {
      const a = e.target.closest("[data-a]")?.dataset.a;
      if (a === "similar") return similar(S.results[i]);
      if (a === "board") return boardMenu(S.results[i], e);
      if (a === "unboard") return removeFromBoard(S.results[i]);
      select(i);
    };
    c.ondblclick = () => { select(i); openLoupe(); };
    c.ondragstart = (e) => {
      const h = S.results[i];
      e.dataTransfer.setData("application/x-mitsuke", JSON.stringify({ file: h.id, shot: h.shot || 0, thumb: h.thumb, name: h.name, time: h.time }));
      // dragging out to Finder / other apps gets the original file
      e.dataTransfer.setData("DownloadURL", `application/octet-stream:${h.name}:${location.origin}/api/view/${h.id}`);
      e.dataTransfer.effectAllowed = "copy";
    };
  });
}

function emptyHTML() {
  const st = S.status;
  if (S.loading) return "";
  if (st && !st.roots) return `<div class="empty"><h2>${esc(t("welcome"))}</h2><p>${esc(t("welcome_sub"))}</p>
    <div class="addrow"><input id="firstfolder" placeholder="${esc(t("folder_ph"))}"><button class="btn" data-act="pick">${esc(t("choose"))}</button><button class="btn pri" data-act="add">${esc(t("add"))}</button></div>
    <p style="font-size:12px">${esc(t("private_note"))}</p></div>`;
  const ix = st?.indexer;
  if (ix && ix.state === "indexing" && !st.indexed) return `<div class="empty"><h2>${esc(t("indexing_first"))}</h2><p>${esc(t("indexing_n", { done: num(ix.done), total: num(ix.total) }))}</p><div class="progress"><i style="width:${ix.total ? 100 * ix.done / ix.total : 0}%"></i></div></div>`;
  if (S.view === "board") return `<div class="empty"><h2>${esc(t("board_empty"))}</h2><p>${esc(t("board_empty_sub"))}</p></div>`;
  return `<div class="empty"><h2>${esc(t("no_results"))}</h2><p>${esc(t("no_results_sub"))}</p></div>`;
}

function bindEmpty() {
  const inp = $("#firstfolder");
  if (!inp) return;
  inp.onkeydown = (e) => { if (e.key === "Enter" && !composing(e)) addFolder(inp.value); };
  $('[data-act="add"]').onclick = () => addFolder(inp.value);
  $('#results [data-act="pick"]').onclick = pickFolder;
}

function select(i, scroll = false) {
  if (i < 0 || i >= S.results.length) return;
  const prev = $(`#results .card[data-i="${S.sel}"]`);
  prev?.classList.remove("sel");
  S.sel = i;
  const c = $(`#results .card[data-i="${i}"]`);
  c?.classList.add("sel");
  if (scroll) c?.scrollIntoView({ block: "nearest" });
  renderInspector();
  if (S.loupe) renderLoupe();
}

// ---- inspector --------------------------------------------------------------------------------

let inspectReq = 0;
async function renderInspector() {
  const box = $("#inspector");
  const h = S.results[S.sel];
  if (!h) { box.innerHTML = `<div class="empty" style="margin:40px 0">${esc(t("nothing_selected"))}</div>`; return; }
  const my = ++inspectReq;
  const isVideo = h.kind === "video";
  const media = isVideo
    ? `<video src="/api/view/${h.id}#t=${h.time || 0}" muted controls preload="metadata" playsinline></video>`
    : `<img src="${esc(h.thumb)}">`;
  box.innerHTML = `<div class="pv">${media}</div><h3>${esc(h.name)}</h3><div class="path">${esc(h.dir)}</div>
    <dl id="meta"><dt>${esc(t("size"))}</dt><dd>${h.width || "?"} × ${h.height || "?"}</dd><dt>${esc(t("type"))}</dt><dd>${esc(h.kind.toUpperCase())}</dd>
    ${isVideo ? `<dt>${esc(t("duration"))}</dt><dd>${fmtTime(h.duration)}</dd>` : ""}${h.time != null ? `<dt>${esc(t("best_shot"))}</dt><dd>${fmtTime(h.t0)}–${fmtTime(h.t1)}</dd>` : ""}
    ${S.view === "search" ? `<dt>${esc(t("score"))}</dt><dd>${h.score.toFixed(3)}</dd>` : ""}</dl>
    <div class="acts2"><button class="btn pri" data-a="similar">${ICON.similar} ${esc(t("similar"))}</button><button class="btn" data-a="board">${ICON.board} ${esc(t("to_board"))}</button>
    <button class="btn" data-a="reveal">${ICON.finder} ${esc(t("finder"))}</button><button class="btn" data-a="copy">${esc(t("copy_path"))}</button></div>
    <div class="likebut">${ICON.search}<input id="likebut" placeholder="${esc(t("like_but"))}"></div>
    ${isVideo ? `<h4>${esc(t("shots"))}</h4><div class="shots" id="shots"></div>` : ""}
    <h4>${esc(t("similar_images"))}</h4><div class="mini" id="mini"></div>`;
  box.querySelector('[data-a="similar"]').onclick = () => similar(h);
  box.querySelector('[data-a="board"]').onclick = (e) => boardMenu(h, e);
  box.querySelector('[data-a="reveal"]').onclick = () => api("POST", `/api/reveal/${h.id}`).catch((e) => toast(e.message));
  box.querySelector('[data-a="copy"]').onclick = () => copyText(h.path);
  const lb = $("#likebut");
  lb.onkeydown = (e) => {
    if (e.key === "Enter" && !composing(e) && lb.value.trim()) {
      S.chips = [chipFor(h)]; S.text = lb.value.trim(); S.weight = 0.5; go();
    }
  };
  if (isVideo) {
    const v = box.querySelector("video");
    v.addEventListener("loadedmetadata", () => { if (h.time != null) v.currentTime = h.time; }, { once: true });
  }
  // details + neighbours
  const [item, sim] = await Promise.all([
    api("GET", `/api/item/${h.id}`).catch(() => null),
    api("POST", "/api/search", { images: [h.shot ? { shot: h.shot } : { file: h.id }], k: 8 }).catch(() => ({ results: [] })),
  ]);
  if (my !== inspectReq) return;
  if (item) {
    const meta = $("#meta");
    meta.insertAdjacentHTML("beforeend", `${item.bytes ? `<dt>${esc(t("file_size"))}</dt><dd>${fmtBytes(item.bytes)}</dd>` : ""}${item.mtime ? `<dt>${esc(t("modified"))}</dt><dd>${new Date(item.mtime * 1000).toLocaleString(S.lang)}</dd>` : ""}`);
    if (isVideo && item.shots) {
      const sh = $("#shots");
      sh.innerHTML = item.shots.map((s) => `<button data-t="${s.time}" data-shot="${s.shot}" class="${s.shot === h.shot ? "on" : ""}" title="${fmtTime(s.t0)}–${fmtTime(s.t1)}"><img loading="lazy" src="${esc(s.thumb)}"><span>${fmtTime(s.t0)}</span></button>`).join("");
      sh.querySelectorAll("button").forEach((b) => {
        b.onclick = () => { const v = box.querySelector("video"); v.currentTime = +b.dataset.t; sh.querySelectorAll("button").forEach((x) => x.classList.toggle("on", x === b)); };
        b.ondblclick = () => { S.chips = [{ shot: +b.dataset.shot, file: h.id, thumb: b.querySelector("img").src, label: h.name, time: +b.dataset.t }]; S.text = ""; go(); };
      });
    }
  }
  const mini = $("#mini");
  mini.innerHTML = sim.results.map((s, i) => `<img data-i="${i}" src="${esc(s.thumb)}" title="${esc(s.name)}">`).join("");
  mini.querySelectorAll("img").forEach((im) => im.onclick = () => similar(h));
}

function boardChip(b) {
  return { board: b.id, label: b.name, thumb: b.results[0]?.thumb, items: b.results.map((h) => h.shot ? { shot: h.shot } : { file: h.id }) };
}
function chipFor(h) { return h.shot ? { shot: h.shot, file: h.id, thumb: h.thumb, label: h.name, time: h.time } : { file: h.id, thumb: h.thumb, label: h.name }; }
function similar(h) { S.chips = [chipFor(h)]; S.text = ""; go(); }

// ---- loupe ------------------------------------------------------------------------------------

function openLoupe() { if (S.results[S.sel]) { S.loupe = true; renderLoupe(); } }
function closeLoupe() { S.loupe = false; const l = $("#loupe"); l.querySelector("video")?.pause(); l.hidden = true; l.innerHTML = ""; }
function renderLoupe() {
  const l = $("#loupe"), h = S.results[S.sel];
  if (!h) return closeLoupe();
  l.hidden = false;
  const media = h.kind === "video"
    ? `<video src="/api/view/${h.id}#t=${h.time || 0}" controls autoplay muted playsinline></video>`
    : `<img src="${esc(h.thumb)}" data-full="/api/view/${h.id}">`;
  const from = Math.max(0, S.sel - 12);
  l.innerHTML = `<div class="top"><b>${esc(h.name)}</b><span>${h.width || "?"} × ${h.height || "?"} · ${esc(h.kind.toUpperCase())}</span><span class="sp"></span><span>${S.sel + 1} / ${num(S.results.length)}</span><button class="btn" data-a="close">Esc</button></div>
    <div class="stage">${media}<button class="nav" style="left:0" data-a="prev">‹</button><button class="nav" style="right:0" data-a="next">›</button></div>
    <div class="film">${S.results.slice(from, from + 40).map((x, j) => `<img data-i="${from + j}" class="${from + j === S.sel ? "on" : ""}" src="${esc(x.thumb)}">`).join("")}</div>`;
  const img = l.querySelector("img[data-full]");
  if (img) { const full = new Image(); full.onload = () => { if (img.isConnected) img.src = full.src; }; full.src = img.dataset.full; }
  l.querySelector('[data-a="close"]').onclick = closeLoupe;
  l.querySelector('[data-a="prev"]').onclick = () => select(S.sel - 1, true);
  l.querySelector('[data-a="next"]').onclick = () => select(S.sel + 1, true);
  l.querySelectorAll(".film img").forEach((im) => im.onclick = () => select(+im.dataset.i, true));
  l.querySelector(".film img.on")?.scrollIntoView({ inline: "center", block: "nearest" });
}

// ---- boards -----------------------------------------------------------------------------------

async function refreshBoards() {
  const b = (await api("GET", "/api/boards")).boards;
  if (JSON.stringify(b) !== JSON.stringify(S.boards)) { S.boards = b; renderSide(); } // also boards made by the CLI / an agent
}

async function removeFromBoard(h) {
  const sel = S.sel;
  try {
    await api("DELETE", `/api/boards/${S.board}/items`, { items: [{ file: h.id, shot: h.shot || 0 }] });
    await refreshBoards();
    await load();
    select(Math.min(sel, S.results.length - 1));
  } catch (e) { toast(e.message); }
}

async function addToBoard(bid, items) {
  try {
    await api("POST", `/api/boards/${bid}/items`, { items });
    S.lastBoard = bid; localStorage.setItem("mitsuke.lastBoard", bid);
    const b = S.boards.find((x) => x.id === bid);
    toast(t("added_to", { name: b ? b.name : "" }));
    await refreshBoards();
    if (S.view === "board" && S.board === bid) load();
  } catch (e) { toast(e.message); }
}

function boardMenu(h, ev) {
  document.querySelector(".menu")?.remove();
  const m = el(`<div class="menu">${S.boards.map((b) => `<button data-b="${b.id}">${ICON.board} ${esc(b.name)}</button>`).join("")}<button data-b="new">${ICON.plus} ${esc(t("new_board"))}…</button></div>`);
  const r = (ev?.target?.closest("button") || ev?.target || document.body).getBoundingClientRect();
  m.style.left = Math.min(r.left, innerWidth - 220) + "px";
  m.style.top = Math.min(r.bottom + 4, innerHeight - 40 * (S.boards.length + 2)) + "px";
  document.body.append(m);
  const item = { file: h.id, shot: h.shot || 0 };
  m.querySelectorAll("button").forEach((b) => b.onclick = async () => {
    m.remove();
    if (b.dataset.b === "new") {
      const name = prompt(t("board_ph"));
      if (!name) return;
      const nb = await api("POST", "/api/boards", { name });
      await refreshBoards();
      return addToBoard(nb.id, [item]);
    }
    addToBoard(+b.dataset.b, [item]);
  });
  setTimeout(() => document.addEventListener("click", () => m.remove(), { once: true }), 0);
}

async function boardAction(a) {
  const bid = S.board;
  if (a === "like") {
    S.chips = [boardChip({ id: bid, name: S.boardName, results: S.results })]; S.text = ""; S.board = null; return go();
  }
  if (a === "copy") return copyText(S.results.map((h) => h.path).join("\n"));
  if (a === "rename") {
    const name = prompt(t("rename"), S.boardName);
    if (name) { await api("PATCH", `/api/boards/${bid}`, { name }); await refreshBoards(); load(); }
  }
  if (a === "delete" && confirm(t("delete_board_q", { name: S.boardName }))) {
    await api("DELETE", `/api/boards/${bid}`); S.board = null; await refreshBoards(); go();
  }
}

function dragItem(e) {
  try { const d = JSON.parse(e.dataTransfer.getData("application/x-mitsuke")); return { file: d.file, shot: d.shot || 0 }; } catch { return null; }
}

// ---- folders ----------------------------------------------------------------------------------

async function refreshFolders() {
  const f = (await api("GET", "/api/folders")).folders;
  if (JSON.stringify(f) !== JSON.stringify(S.folders)) { S.folders = f; renderSide(); } // e.g. a folder went offline
}

async function addFolder(path) {
  if (!path || !path.trim()) return;
  try {
    await api("POST", "/api/folders", { path });
    S.addingFolder = false;
    toast(t("folder_added"));
    await refreshStatus();
    await refreshFolders();
    if (!S.results.length) load();
  } catch (e) {
    // the server's messages are English: show the common ones in the UI language
    toast(/^not a folder/.test(e.message) ? t("not_folder", { path: path.trim() }) : / overlaps with /.test(e.message) ? t("folder_overlap") : e.message);
  }
}

async function pickFolder() {
  try { const r = await api("POST", "/api/folders/pick"); if (r.path) addFolder(r.path); } catch (e) {
    if (e.status !== 501) return toast(e.message);
    toast(t("type_path")); // no folder dialog (not a Mac, or MITSUKE_NO_DESKTOP)
    if (!$("#firstfolder")) { S.addingFolder = true; renderSide(); }
    ($("#firstfolder") || $("#folderpath"))?.focus();
  }
}

async function removeFolder(id, path) {
  if (!confirm(t("remove_folder_q", { path }))) return;
  await api("DELETE", `/api/folders/${id}`);
  if (S.root && (S.root === path || S.root.startsWith(path + "/"))) S.root = null;
  await refreshStatus(); await refreshFolders(); go();
}

// ---- image queries: drop, paste, file picker --------------------------------------------------

async function uploadFile(file) {
  try {
    toast(t("reading_image"));
    const r = await api("POST", "/api/upload", await file.arrayBuffer(), { headers: { "X-Filename": encodeURIComponent(file.name || "pasted.png"), "Content-Type": "application/octet-stream" } });
    S.chips.push({ upload: r.upload, thumb: r.thumb, label: file.name || "" });
    hideToast();
    go();
  } catch (e) { toast(e.status === 415 ? t("not_image") : e.message); }
}

function setupDrop() {
  let depth = 0;
  const dz = $("#dropzone"), comp = $("#composer");
  const isFiles = (e) => [...(e.dataTransfer?.types || [])].includes("Files");
  addEventListener("dragenter", (e) => { if (isFiles(e)) { depth++; dz.hidden = false; } });
  addEventListener("dragleave", (e) => { if (isFiles(e) && --depth <= 0) { depth = 0; dz.hidden = true; } });
  addEventListener("dragover", (e) => { if (isFiles(e) || [...e.dataTransfer.types].includes("application/x-mitsuke")) e.preventDefault(); });
  addEventListener("drop", (e) => {
    depth = 0; dz.hidden = true;
    if (e.defaultPrevented) return;
    const f = [...(e.dataTransfer?.files || [])][0];
    if (f) { e.preventDefault(); uploadFile(f); }
  });
  comp.addEventListener("dragover", (e) => { e.preventDefault(); comp.classList.add("dragover"); });
  comp.addEventListener("dragleave", () => comp.classList.remove("dragover"));
  comp.addEventListener("drop", (e) => {
    comp.classList.remove("dragover");
    const raw = e.dataTransfer.getData("application/x-mitsuke");
    if (raw) { e.preventDefault(); const d = JSON.parse(raw); S.chips.push(d.shot ? { shot: d.shot, file: d.file, thumb: d.thumb, label: d.name, time: d.time } : { file: d.file, thumb: d.thumb, label: d.name }); go(); }
  });
  addEventListener("paste", (e) => {
    const f = [...(e.clipboardData?.files || [])].find((x) => x.type.startsWith("image/"));
    if (f) { e.preventDefault(); uploadFile(f); }
  });
  $("#imgbtn").onclick = () => $("#file").click();
  $("#file").onchange = (e) => { const f = e.target.files[0]; if (f) uploadFile(f); e.target.value = ""; };
}

// ---- status -----------------------------------------------------------------------------------

let lastIndexed = null;
async function refreshStatus() {
  try { S.status = await api("GET", "/api/status"); } catch { S.status = null; }
  renderStatus();
  const st = S.status;
  if (st) { refreshFolders().catch(() => {}); refreshBoards().catch(() => {}); }
  // new files indexed: refresh the browse view in place, unless the user has scrolled past the first page
  if (st && lastIndexed !== null && st.indexed !== lastIndexed && S.view === "all" && !S.loading && S.results.length <= 120) load(false, true);
  if (st) lastIndexed = st.indexed;
}

function renderStatus() {
  const st = S.status, box = $("#status");
  if (!st) { box.innerHTML = `<span><span class="dot err"></span>${esc(t("server_down"))}</span>`; return; }
  const ix = st.indexer || {};
  let s;
  if (ix.state === "indexing") s = `<span><span class="dot busy"></span>${esc(t("st_indexing", { done: num(ix.done), total: num(ix.total) }))}<span class="bar"><i style="width:${ix.total ? 100 * ix.done / ix.total : 0}%"></i></span>${ix.eta_s != null ? esc(t("st_eta", { t: fmtTime(ix.eta_s) })) : ""}</span>`;
  else if (ix.state === "scanning") s = `<span><span class="dot busy"></span>${esc(t("st_scanning"))}</span>`;
  else if (ix.state === "external") s = `<span><span class="dot busy"></span>${esc(t("st_external"))}</span>`;
  else if (ix.state === "error") s = `<span><span class="dot err"></span>${esc(ix.message || "error")}</span>`;
  else s = `<span><span class="dot"></span>${esc(t("st_ready", { n: num(st.indexed) }))}</span>`;
  const watch = ix.state === "watching" ? `<span>${esc(t("st_watching", { n: st.roots }))}</span>` : "";
  const errs = st.errors ? `<span>${esc(t("st_errors", { n: num(st.errors) }))}</span>` : "";
  box.innerHTML = `${s}${watch}${errs}<span>EmbeddingGemma 2 · ${esc(t("local_only"))}</span>
    <span class="keys"><kbd>/</kbd> ${esc(t("k_search"))} <kbd>Space</kbd> ${esc(t("k_preview"))} <kbd>S</kbd> ${esc(t("similar"))} <kbd>B</kbd> ${esc(t("k_board"))} <kbd>R</kbd> ${esc(t("finder"))}</span>`;
}

// ---- misc -------------------------------------------------------------------------------------

let toastTimer;
function toast(msg) { const tt = $("#toast"); tt.textContent = msg; tt.hidden = false; clearTimeout(toastTimer); toastTimer = setTimeout(hideToast, 3500); }
function hideToast() { $("#toast").hidden = true; }
function copyText(s) { navigator.clipboard.writeText(s).then(() => toast(t("copied")), () => toast(s)); }

function columns() {
  const cards = [...document.querySelectorAll("#results .card")];
  if (cards.length < 2) return 1;
  const top = cards[0].getBoundingClientRect().top;
  const n = cards.findIndex((c) => c.getBoundingClientRect().top > top + 2);
  return n < 0 ? cards.length : n;
}

function applyChrome() {
  document.documentElement.dataset.theme = S.theme;
  document.documentElement.lang = { zh: "zh-Hans", ja: "ja", en: "en" }[S.lang];
  $("#lang").value = S.lang;
  $("#theme").innerHTML = S.theme === "dark" ? ICON.sun : ICON.moon;
  $("#theme").title = t("theme");
  $("#imgbtn").innerHTML = ICON.img; $("#imgbtn").title = t("img_tip");
  $("#size").value = S.size;
  $("#kinds").innerHTML = KINDS.map(([k]) => `<button data-k="${k}" class="${S.kind === k ? "on" : ""}">${esc(t("kind_" + k))}</button>`).join("");
  $("#kinds").querySelectorAll("button").forEach((b) => b.onclick = () => { S.kind = b.dataset.k; applyChrome(); go(); });
  $("#layout").innerHTML = `<button data-l="grid" class="${S.layout === "grid" ? "on" : ""}" title="${esc(t("layout_grid"))}">${ICON.grid}</button><button data-l="rows" class="${S.layout === "rows" ? "on" : ""}" title="${esc(t("layout_rows"))}">${ICON.rows}</button>`;
  $("#layout").querySelectorAll("button").forEach((b) => b.onclick = () => { S.layout = b.dataset.l; localStorage.setItem("mitsuke.layout", S.layout); applyChrome(); renderResults(); });
  document.querySelectorAll("[data-t]").forEach((n) => { if (n.dataset.t && I18N.en[n.dataset.t]) n.textContent = t(n.dataset.t); });
  document.title = "Mitsuke 見つけ";
}

function setupInputs() {
  const q = $("#q"), comp = $("#composer");
  q.addEventListener("focus", () => comp.classList.add("focus"));
  q.addEventListener("blur", () => comp.classList.remove("focus"));
  q.addEventListener("input", (e) => { S.text = q.value; if (!e.isComposing) search(); });
  q.addEventListener("compositionend", () => { S.text = q.value; search(); });
  q.addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !composing(e)) { e.preventDefault(); S.text = q.value; go(); q.blur(); }
    if (e.key === "Escape") { q.blur(); }
    if (e.key === "Backspace" && !q.value && S.chips.length) { S.chips.pop(); go(); }
  });
  $("#clear").onclick = () => { S.text = ""; S.chips = []; go(); };
  $("#weight").addEventListener("input", (e) => { S.weight = +e.target.value / 100; renderComposer(); search(); });
  $("#size").addEventListener("input", (e) => { S.size = +e.target.value; localStorage.setItem("mitsuke.size", S.size); renderResults(); select(S.sel); });
  $("#lang").onchange = (e) => { S.lang = e.target.value; localStorage.setItem("mitsuke.lang", S.lang); applyChrome(); renderAll(); };
  $("#theme").onclick = () => { S.theme = S.theme === "dark" ? "light" : "dark"; localStorage.setItem("mitsuke.theme", S.theme); applyChrome(); };
  $("#results").addEventListener("scroll", (e) => {
    const b = e.target;
    if (!S.loading && !S.exhausted && b.scrollTop + b.clientHeight > b.scrollHeight - 600) load(true);
  });
  addEventListener("resize", debounce(() => { if (S.layout === "rows") renderResults(); }, 150));
  addEventListener("hashchange", async () => { await readHash(); applyChrome(); renderAll(); load(); });

  addEventListener("keydown", (e) => {
    const typing = e.target.matches?.("input, textarea, select");
    if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") { e.preventDefault(); q.focus(); q.select(); return; }
    if (typing) return;
    if (e.key === "/") { e.preventDefault(); q.focus(); q.select(); return; }
    if (e.key === "Escape") { if (S.loupe) closeLoupe(); else if (S.text || S.chips.length) { S.text = ""; S.chips = []; go(); } return; }
    if (e.key === "Enter" && e.target.closest?.("button, a")) return; // a focused button: let Enter press it
    if (e.key === " " || e.key === "Enter") { e.preventDefault(); S.loupe ? closeLoupe() : openLoupe(); return; }
    const cols = S.loupe ? 1 : columns();
    const mv = { ArrowRight: 1, ArrowLeft: -1, ArrowDown: S.loupe ? 1 : cols, ArrowUp: S.loupe ? -1 : -cols }[e.key];
    if (mv) { e.preventDefault(); select(Math.max(0, Math.min(S.results.length - 1, S.sel + mv)), true); return; }
    const h = S.results[S.sel];
    if (!h || e.metaKey || e.ctrlKey) return;
    if (e.key === "s" || e.key === "S") { closeLoupe(); similar(h); }
    if (e.key === "b" || e.key === "B") { if (S.lastBoard && S.boards.some((b) => b.id === S.lastBoard)) addToBoard(S.lastBoard, [{ file: h.id, shot: h.shot || 0 }]); else boardMenu(h, { target: $("#inspector .acts2") || document.body }); }
    if ((e.key === "Delete" || e.key === "Backspace") && S.view === "board") { e.preventDefault(); removeFromBoard(h); }
    if (e.key === "r" || e.key === "R") api("POST", `/api/reveal/${h.id}`).catch((er) => toast(er.message));
  });
}

function renderAll() { renderComposer(); renderSide(); renderCrumb(); renderResults(); renderInspector(); renderStatus(); }

async function main() {
  applyChrome();
  setupInputs();
  setupDrop();
  await readHash();
  await refreshStatus();
  await Promise.all([refreshFolders().catch(() => {}), refreshBoards().catch(() => {})]);
  renderAll();
  await load();
  if (QS.has("sel")) select(+QS.get("sel"));
  if (QS.has("loupe")) openLoupe();
  document.body.dataset.ready = "1";
  const tick = async () => { await refreshStatus(); const busy = ["indexing", "scanning", "starting"].includes(S.status?.indexer?.state); setTimeout(tick, busy ? 1500 : 5000); };
  setTimeout(tick, 1500);
}

main();
