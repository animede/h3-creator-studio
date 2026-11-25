// 「使い方」の窓: 右上のボタンで開く、動かせて大きさを変えられる窓。中身は docs/USAGE.md(/api/help)を、小さな Markdown 変換で描く。
// いまのタブの節から表示し、左の目次(## の見出し)で飛べる。窓の位置と大きさは localStorage に覚える。
const HELP_GITHUB = `https://github.com/animede/h3-creator-studio/blob/main/docs/${LANG === "en" ? "USAGE.en.md" : "USAGE.md"}`;
// 各タブの節の見出し(説明書の見出しと突き合わせる文字なので訳さない。英語の説明書は HELP_TAB_HEADING_EN)
const HELP_TAB_HEADING = { image: "画像タブ", video: "動画タブ", music: "音楽タブ", mv: "MV(ミュージックビデオ)タブ", short: "ショート動画タブ", edit: "編集タブ" };      // i18n-skip 説明書の見出し
const HELP_TAB_HEADING_EN = { image: "Image tab", video: "Video tab", music: "Music tab", mv: "Music Video tab", short: "Short Video tab", edit: "Edit tab" };
const HELP_KEY = "yuzu.help.win";
const HELP_NARROW = 560;      // この幅より狭いとき、目次は上のプルダウンにする

// ---- Markdown 変換(ライブラリなし。説明書に使う範囲だけ) ----
// 見出し・段落・箇条書き(- * と 1.、入れ子は 1 段)・表・コードブロック・引用・水平線、
// 行の中は **太字** *斜体* `コード` [リンク](url) ![画像](path)。HTML は先に全部エスケープする。

const helpEsc = (s) => s.replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");

// 見出しの id: GitHub と同じ作り方(小文字・文字と数字と空白と - と _ 以外を除き、空白は -。日本語はそのまま)
function helpSlug(text) {
  return text.replace(/\[([^\]]*)\]\([^)]*\)/g, "$1").toLowerCase().replace(/[^\p{L}\p{N}\p{M}\s_-]/gu, "").trim().replace(/\s/g, "-");
}

// 画像の path: docs/img/ のもの(説明書からの相対 img/ も)は、配信の /docs/img/ に直す
function helpImgSrc(p) {
  const q = p.replace(/^(\.\.?\/)+/, "");
  if (/^https?:\/\//.test(p) || p.startsWith("/")) return p;
  if (q.startsWith("docs/img/")) return "/" + q;
  if (q.startsWith("img/")) return "/docs/" + q;
  return p;
}

function helpInline(raw) {
  const codes = [];
  let s = helpEsc(raw).replace(/`([^`]+)`/g, (_, c) => { codes.push(`<code>${c}</code>`); return `\u0000${codes.length - 1}\u0000`; });      // コードの中は、他の記法を効かせない
  s = s.replace(/!\[([^\]]*)\]\(([^)\s]+)\)/g, (_, alt, p) => `<img src="${helpImgSrc(p)}" alt="${alt}" loading="lazy">`);
  s = s.replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, (_, t, u) => {
    if (/^https?:\/\//.test(u)) return `<a href="${u}" target="_blank" rel="noopener noreferrer">${t}</a>`;
    if (u.startsWith("#")) return `<a href="${u}" data-anchor>${t}</a>`;
    return t;      // ほかの相対リンクや危険な形(javascript: など)は、文字だけにする
  });
  s = s.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>").replace(/(^|[^*])\*([^*\s][^*]*)\*(?!\*)/g, "$1<em>$2</em>");
  return s.replace(/\u0000(\d+)\u0000/g, (_, i) => codes[+i]);
}

const HELP_LIST = /^(\s*)([-*]|\d+\.)\s+(.*)$/;
const HELP_SEP = /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)*\|?\s*$/;
const helpCells = (line) => line.trim().replace(/^\|/, "").replace(/\|$/, "").split(/(?<!\\)\|/).map((c) => c.trim().replace(/\\\|/g, "|"));

function helpList(items) {
  let html = "", top = null, sub = null, open = false;      // top = 外側の ul/ol、sub = 入れ子の ul/ol
  for (const it of items) {
    const tag = it.ordered ? "ol" : "ul";
    if (it.level === 0 || !open) {
      if (sub) { html += `</${sub}>`; sub = null; }
      if (open) html += "</li>";
      if (top !== tag) { if (top) html += `</${top}>`; html += `<${tag}>`; top = tag; }
      html += `<li>${helpInline(it.text)}`;
      open = true;
    } else {
      if (sub !== tag) { if (sub) html += `</${sub}>`; html += `<${tag}>`; sub = tag; }
      html += `<li>${helpInline(it.text)}</li>`;
    }
  }
  return html + (sub ? `</${sub}>` : "") + (open ? "</li>" : "") + (top ? `</${top}>` : "");
}

function renderMarkdown(md) {
  const lines = md.replace(/\r\n?/g, "\n").split("\n"), out = [], ids = {};
  const blockStart = (l, nx) => /^(#{1,4}\s|```|>|\s*([-*]|\d+\.)\s|(-{3,}|\*{3,})\s*$)/.test(l) || (l.trim().startsWith("|") && nx !== undefined && HELP_SEP.test(nx));
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    let m;
    if (!l.trim()) { i++; continue; }
    if (l.startsWith("```")) {      // コードブロック: 閉じの ``` まで(HTML はエスケープ)
      const buf = [];
      for (i++; i < lines.length && !lines[i].startsWith("```"); i++) buf.push(lines[i]);
      i++;
      out.push(`<pre><code>${helpEsc(buf.join("\n"))}</code></pre>`);
    } else if ((m = /^(#{1,4})\s+(.*?)\s*$/.exec(l))) {
      let id = helpSlug(m[2]) || "section";
      if (ids[id] !== undefined) id += "-" + (++ids[id]); else ids[id] = 0;      // 同じ見出しは -1 -2 …
      out.push(`<h${m[1].length} id="${id}">${helpInline(m[2])}</h${m[1].length}>`);
      i++;
    } else if (/^(-{3,}|\*{3,})\s*$/.test(l)) { out.push("<hr>"); i++; }
    else if (l.startsWith(">")) {
      const buf = [];
      for (; i < lines.length && lines[i].startsWith(">"); i++) buf.push(lines[i].replace(/^>\s?/, ""));
      out.push(`<blockquote>${helpInline(buf.join(" "))}</blockquote>`);
    } else if (l.trim().startsWith("|") && HELP_SEP.test(lines[i + 1] || "x")) {
      const head = helpCells(l), rows = [];
      for (i += 2; i < lines.length && lines[i].trim().startsWith("|"); i++) rows.push(helpCells(lines[i]));
      const td = (tag, cs) => `<tr>${cs.map((c) => `<${tag}>${helpInline(c)}</${tag}>`).join("")}</tr>`;
      out.push(`<div class="help-tbl"><table><thead>${td("th", head)}</thead><tbody>${rows.map((r) => td("td", r)).join("")}</tbody></table></div>`);
    } else if (HELP_LIST.test(l)) {
      const items = [];
      while (i < lines.length) {
        const lm = HELP_LIST.exec(lines[i]);
        if (lm) { items.push({ level: lm[1].length >= 2 ? 1 : 0, ordered: /\d/.test(lm[2]), text: lm[3] }); i++; }
        else if (items.length && /^\s+\S/.test(lines[i]) && !blockStart(lines[i])) { items[items.length - 1].text += " " + lines[i].trim(); i++; }      // 字下げの続き行
        else if (!lines[i].trim() && HELP_LIST.test(lines[i + 1] || "")) i++;      // 項目の間の空行
        else break;
      }
      out.push(helpList(items));
    } else {      // 段落: 空行か、ほかの記法が始まるまで(改行は空白 1 つに)
      const buf = [l.trim()];
      for (i++; i < lines.length && lines[i].trim() && !blockStart(lines[i], lines[i + 1]); i++) buf.push(lines[i].trim());
      out.push(`<p>${helpInline(buf.join(" "))}</p>`);
    }
  }
  return out.join("\n");
}

const HELP_LOGIC = { renderMarkdown, helpSlug };      // node での検査用(web/help.js を偽の DOM で読み込む)

// ---- 窓 ----
(() => {
  const win = $("help-win"), head = $("help-head"), content = $("help-content"), toc = $("help-toc"), sel = $("help-toc-sel"), btn = $("help-btn");
  let loaded = null;      // {md, mtime}: 一度読んだ説明(開き直したとき、mtime が変わっていたら読み直す)
  let headings = [];      // 目次の ## の見出し [{h, a}]

  const load = () => { try { return JSON.parse(localStorage.getItem(HELP_KEY)) || {}; } catch (e) { return {}; } };
  const save = (o) => { try { localStorage.setItem(HELP_KEY, JSON.stringify(o)); } catch (e) { /* 覚えられなくても動く */ } };
  const clamp = (v, lo, hi) => Math.max(lo, Math.min(v, Math.max(lo, hi)));

  // 位置と大きさ: 保存があればそれ、無ければ右側に 760px × 画面の 70%。いつも画面の中に収める
  function place(g) {
    const vw = window.innerWidth, vh = window.innerHeight;
    const w = clamp(g.w || 760, 360, vw - 8), h = clamp(g.h || Math.round(vh * 0.7), 300, vh - 8);
    win.style.width = w + "px"; win.style.height = h + "px";
    win.style.left = clamp(g.x ?? vw - w - 16, 0, vw - w) + "px";
    win.style.top = clamp(g.y ?? 64, 0, vh - h) + "px";
  }
  const geom = () => { const r = win.getBoundingClientRect(); return { x: Math.round(r.left), y: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) }; };

  // タイトルバーのドラッグ(窓の外へ出さない)
  head.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest("button, a")) return;
    const r = win.getBoundingClientRect(), dx = e.clientX - r.left, dy = e.clientY - r.top;
    head.setPointerCapture(e.pointerId);
    const move = (ev) => {
      win.style.left = clamp(ev.clientX - dx, 0, window.innerWidth - r.width) + "px";
      win.style.top = clamp(ev.clientY - dy, 0, window.innerHeight - r.height) + "px";
    };
    const up = () => { head.removeEventListener("pointermove", move); head.removeEventListener("pointerup", up); head.removeEventListener("pointercancel", up); save(geom()); };
    head.addEventListener("pointermove", move); head.addEventListener("pointerup", up); head.addEventListener("pointercancel", up);
  });

  // 大きさが変わったら(右下の角のドラッグ)、狭いときの目次の形を切り替えて、大きさを覚える
  let saveTimer = null;
  new ResizeObserver(() => {
    if (win.hidden) return;
    win.classList.toggle("narrow", win.clientWidth < HELP_NARROW);
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => save(geom()), 300);
  }).observe(win);
  window.addEventListener("resize", () => { if (!win.hidden) place(geom()); });

  function scrollToHeading(h, smooth) {
    const top = h.getBoundingClientRect().top - content.getBoundingClientRect().top + content.scrollTop - 8;
    content.scrollTo({ top, behavior: smooth ? "smooth" : "auto" });
  }

  // 本文を描いて、目次(## だけ)を作る
  function render(md) {
    content.innerHTML = renderMarkdown(md);
    headings = [...content.querySelectorAll("h2")].map((h) => {
      const a = el("a", "", h.textContent);
      a.href = "#" + h.id;
      a.onclick = (e) => { e.preventDefault(); scrollToHeading(h, true); };
      return { h, a };
    });
    toc.replaceChildren(...headings.map((x) => x.a));
    sel.replaceChildren(...headings.map((x) => { const o = el("option", "", x.h.textContent); o.value = x.h.id; return o; }));
    content.scrollTop = 0;
  }

  // 本文のスクロールに合わせて、目次の「いま読んでいる節」を強調する
  function markCurrent() {
    const limit = content.getBoundingClientRect().top + 24;
    let cur = headings[0];
    for (const x of headings) if (x.h.getBoundingClientRect().top <= limit) cur = x;
    headings.forEach((x) => x.a.classList.toggle("cur", x === cur));
    if (cur) { sel.value = cur.h.id; if (!win.classList.contains("narrow")) cur.a.scrollIntoView({ block: "nearest" }); }
  }
  let spy = 0;
  content.addEventListener("scroll", () => {
    if (!spy) spy = requestAnimationFrame(() => { spy = 0; markCurrent(); });
  });
  sel.addEventListener("change", () => { const x = headings.find((y) => y.h.id === sel.value); if (x) scrollToHeading(x.h, true); });

  // 本文の中の #リンクは、窓の中で飛ぶ
  content.addEventListener("click", (e) => {
    const a = e.target.closest("a[data-anchor]");
    if (!a) return;
    e.preventDefault();
    const id = decodeURIComponent(a.getAttribute("href").slice(1));
    const h = [...content.querySelectorAll("[id]")].find((x) => x.id === id || x.id === helpSlug(id));
    if (h) scrollToHeading(h, true);
  });

  // 説明を取りに行く。開くたびに確かめ、mtime が同じなら描き直さない
  async function fetchHelp() {
    const r = await fetch(`/api/help?lang=${LANG}`, { headers: langHeaders() });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
    const j = await r.json();
    if (loaded && loaded.mtime === j.mtime) return false;
    loaded = { md: j.markdown, mtime: j.mtime };
    render(j.markdown);
    return true;
  }

  // いまのタブの節へ(見出しが ## の「画像タブ」などで始まるもの)
  function toTabSection() {
    const want = (LANG === "en" ? HELP_TAB_HEADING_EN : HELP_TAB_HEADING)[typeof studio !== "undefined" ? studio.tab : "image"];
    const x = want && headings.find((y) => y.h.textContent.trim().startsWith(want));
    if (x) scrollToHeading(x.h, false);
  }

  async function open() {
    win.hidden = false;
    place(load());
    win.classList.toggle("narrow", win.clientWidth < HELP_NARROW);
    btn.setAttribute("aria-expanded", "true"); btn.classList.add("on");
    if (!loaded) content.replaceChildren(el("p", "help-note", t("読み込み中…")));
    try {
      await fetchHelp();
      toTabSection();
      markCurrent();
    } catch (e) {
      content.replaceChildren(el("p", "help-note err", t("使い方を読み込めません: {error}", { error: e.message || e })));
      toc.replaceChildren(); sel.replaceChildren(); headings = []; loaded = null;
    }
  }
  function close() {
    if (win.hidden) return;
    save(geom());
    win.hidden = true;
    btn.setAttribute("aria-expanded", "false"); btn.classList.remove("on");
  }

  btn.addEventListener("click", () => (win.hidden ? open() : close()));
  $("help-close").addEventListener("click", close);
  $("help-gh").href = HELP_GITHUB;
  document.addEventListener("keydown", (e) => {
    if (e.key !== "Escape" || win.hidden || document.querySelector("dialog[open]")) return;
    const t = e.target;
    if (t && t.closest && t.closest("input, textarea, select") && !win.contains(t)) return;      // 入力中の Esc は、その入力のもの
    close();
  });
})();
