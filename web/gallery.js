// 生成物の共有部分: ギャラリー(全タブ共通)・履歴・生成物ビューア・タブ切り替え・ドラッグ元。
const OUT_MIME = "application/x-studio-output";     // ドラッグするデータ: 生成物の ID
const KIND_LABEL = { image: "画像", video: "動画", audio: "音楽" };
const TAB_KIND = { image: "image", video: "video", music: "audio" };

const studio = {
  tab: "image",
  items: [],                 // /api/gallery の結果(新しい順)
  viewing: {},               // タブ -> 生成物ビューアに出している ID(未指定なら、そのタブの最新)
  galleryKind: "all",
  actions: {},               // タブ -> (生成物) => [{label, run}]  生成物ビューアに出すボタン(各タブの js が入れる)
  own: {},                   // タブ -> {history(grid), open(生成物)}  右側の生成物ビューアと履歴を自前で描くタブ(MV タブ)。open はギャラリーでクリックされたとき
  on: { tab: [], items: [] },   // 各タブの js が入れる: タブが変わったとき / 一覧が更新されたとき
};

const byId = (id) => studio.items.find((i) => i.id === id);
const fmtSec = (s) => (s >= 60 ? `${Math.floor(s / 60)}分${Math.round(s % 60)}秒` : `${s.toFixed(1)}秒`);
const fmtDur = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

async function loadGallery() {
  try {
    studio.items = await fetch("/api/gallery?limit=500").then((r) => r.json());
  } catch (e) { return; }
  renderHistory();
  renderGallery();
  renderViewer();
  studio.on.items.forEach((f) => f());
}

// ---- ドラッグ元 ----

function makeDraggable(node, id) {
  node.draggable = true;
  node.addEventListener("dragstart", (e) => {
    e.dataTransfer.setData(OUT_MIME, id);
    e.dataTransfer.setData("text/plain", id);
    e.dataTransfer.effectAllowed = "copy";
    node.classList.add("dragging");
  });
  node.addEventListener("dragend", () => node.classList.remove("dragging"));
}

function thumbNode(item, onClick, onDelete) {
  const t = el("div", "thumb");
  t.title = (item.kind === "audio" && item.params && item.params.theme) || `${item.filename || ""}${item.width ? `  ${item.width}×${item.height}` : ""}`;
  if (item.thumb_url) {
    const img = el("img");
    img.src = item.thumb_url;
    img.alt = item.filename || "";
    img.loading = "lazy";
    img.draggable = false;          // ドラッグ元は外側の div にそろえる
    t.append(img);
  } else {
    t.append(el("div", "nothumb " + item.kind, item.kind === "audio" ? "🎵" : item.kind === "video" ? "▶" : KIND_LABEL[item.kind] || item.kind));
  }
  if (item.tab === "upload") t.append(el("span", "tbadge top", "取込"));
  if (item.kind !== "image") {     // 動画は ▶ と長さ、音声は 🔊 と長さ
    const d = item.duration_s ? " " + fmtDur(item.duration_s) : "";
    t.append(el("span", "tbadge", (item.kind === "video" ? "▶" : "🎵") + d));
  }
  t.onclick = () => onClick(item);
  if (onDelete) {
    const x = el("button", "tdel", "×");
    x.title = "削除";
    x.onclick = (e) => { e.stopPropagation(); onDelete(item); };
    t.append(x);
  }
  makeDraggable(t, item.id);
  return t;
}

// ---- 履歴(このタブの生成物) ----

function tabItems() { return studio.items.filter((i) => i.tab === studio.tab); }

function renderHistory() {
  const grid = $("hist-grid");
  const own = studio.own[studio.tab];
  if (own) { own.history(grid); return; }
  const list = tabItems();
  const shown = currentItem();
  grid.replaceChildren();
  if (!list.length) { grid.append(el("div", "placeholder", "このタブで作ったものがここに並びます")); return; }
  for (const it of list) {
    const t = thumbNode(it, (x) => { studio.viewing[studio.tab] = x.id; renderHistory(); renderViewer(); });
    t.classList.toggle("sel", !!shown && shown.id === it.id);
    grid.append(t);
  }
}

// ---- 生成物ビューア ----

// 音楽のカード: タイトル(テーマ)・キャプション・プレーヤー・歌詞
function audioCard(item) {
  const p = item.params || {};
  const box = el("div", "audiocard");
  box.append(el("div", "aicon", "🎵"), el("div", "atitle", p.theme || item.filename || ""));
  if (p.caption) box.append(el("div", "acap", p.caption));
  const a = el("audio");
  a.controls = true;
  a.src = item.url;
  box.append(a);
  if (p.lyrics && p.lyrics !== "[Instrumental]") {
    const d = el("details", "alyrics");
    d.append(el("summary", "", "歌詞"), el("pre", "", p.lyrics));
    box.append(d);
  }
  return box;
}

function currentItem() {
  const id = studio.viewing[studio.tab];
  return (id && byId(id)) || tabItems()[0] || null;
}

function renderViewer() {
  if (studio.own[studio.tab]) return;
  const item = currentItem();
  const v = $("viewer"), meta = $("vmeta");
  meta.hidden = !item;
  if (!item) {
    if (v.dataset.id !== "none") { v.dataset.id = "none"; v.replaceChildren(el("div", "placeholder", "まだ生成物がありません")); }
    return;
  }
  if (v.dataset.id !== item.id) {       // 同じものを出し直さない(動画の再生が途切れないように)
    v.dataset.id = item.id;
    let m;
    if (item.kind === "audio") { m = audioCard(item); }
    else {
      if (item.kind === "video") { m = el("video"); m.controls = true; }
      else { m = el("img"); m.alt = item.filename || ""; makeDraggable(m, item.id); }
      m.src = item.url;
    }
    v.replaceChildren(m);
  }
  const p = item.params || {};
  $("vname").textContent = (item.kind === "audio" && p.theme) || item.filename || "";
  const info = [];
  if (item.width) info.push(`${item.width}×${item.height}`);
  if (item.duration_s) info.push(fmtSec(item.duration_s));
  if (item.kind === "audio") {
    if (p.bpm) info.push(`BPM ${p.bpm}`);
    if (p.key_scale) info.push(p.key_scale);
  }
  const sec = item.params && item.params.result && item.params.result.generation_seconds;
  if (sec) info.push(`生成 ${fmtSec(sec)}`);
  $("vinfo").textContent = info.join("  ");
  const save = $("vsave");
  save.href = item.url;
  save.download = item.filename || "";
  const acts = $("vacts");        // 「参照に使う」など、いまのタブごとのボタン
  acts.replaceChildren();
  for (const a of (studio.actions[studio.tab] ? studio.actions[studio.tab](item) : [])) {
    const b = el("button", "", a.label);
    b.onclick = a.run;
    acts.append(b);
  }
}

// ---- ギャラリー(右側 2 枚を 1 枚に切り替える) ----

function setGalleryView(on) {
  $("workspace").classList.toggle("show-gallery", on);
  $("gallery").hidden = !on;
  if (on) loadGallery();
}

function renderGallery() {
  const grid = $("gal-grid");
  grid.replaceChildren();
  const list = studio.items.filter((i) => studio.galleryKind === "all" || i.kind === studio.galleryKind);
  if (!list.length) { grid.append(el("div", "placeholder", "まだ何もありません")); return; }
  for (const it of list) {
    grid.append(thumbNode(it, (x) => {      // クリック: その生成物を、いまのタブの生成物ビューアで開く(MV タブは、その MV を作った制作を開く)
      const own = studio.own[studio.tab];
      if (own) { if (own.open) own.open(x); setGalleryView(false); return; }
      studio.viewing[studio.tab] = x.id;
      setGalleryView(false);
      renderHistory();
      renderViewer();
    }, deleteItem));
  }
}

async function deleteItem(item) {
  if (!confirm(`「${item.filename || item.id}」を削除しますか?\nファイルも消え、元に戻せません。`)) return;
  const r = await fetch(`/api/gallery/${item.id}`, { method: "DELETE" });
  if (!r.ok && r.status !== 404) { alert("削除できませんでした"); return; }
  for (const k of Object.keys(studio.viewing)) if (studio.viewing[k] === item.id) delete studio.viewing[k];
  await loadGallery();
}

$("to-gallery").onclick = () => setGalleryView(true);
$("from-gallery").onclick = () => setGalleryView(false);
document.querySelectorAll("#g-chips .chip").forEach((c) => c.addEventListener("click", () => {
  studio.galleryKind = c.dataset.kind;
  document.querySelectorAll("#g-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
  renderGallery();
}));

// ---- タブ ----

function setTab(tab) {
  studio.tab = tab;
  $("workspace").dataset.tab = tab;
  document.querySelectorAll(".tab").forEach((x) => x.classList.toggle("active", x.dataset.tab === tab));
  document.querySelectorAll(".tabpanel").forEach((p) => { p.hidden = p.id !== "tab-" + tab; });
  $("progress").hidden = true;      // 進捗は、そのタブのジョブがあるときだけ各タブの js が出す
  renderHistory();
  renderViewer();
  studio.on.tab.forEach((f) => f(tab));
}

// 画面の外にファイルを落としたとき、ブラウザがそのファイルを開いてしまうのを防ぐ
for (const ev of ["dragover", "drop"]) {
  document.addEventListener(ev, (e) => { if ([...(e.dataTransfer?.types || [])].includes("Files")) e.preventDefault(); });
}

loadGallery();
