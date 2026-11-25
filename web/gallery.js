// 生成物の共有部分: ギャラリー(全タブ共通)・履歴・生成物ビューア・タブ切り替え・ドラッグ元。
const OUT_MIME = "application/x-studio-output";     // ドラッグするデータ: 生成物の ID
const KIND_LABEL = { image: t("画像"), video: t("動画"), audio: t("音楽") };
const TAB_KIND = { image: "image", video: "video", music: "audio" };

const GALLERY_LIMIT = 500;   // 読む生成物の数(新しい順)。これより多ければ、古いものは studio.items に無い
const studio = {
  tab: "image",
  items: [],                 // /api/gallery の結果(新しい順。GALLERY_LIMIT まで)
  complete: false,           // items が全部か(GALLERY_LIMIT で切れていない)。切れていれば、items に無い生成物が消えたとは限らない
  viewing: {},               // タブ -> 生成物ビューアに出している ID(未指定なら、そのタブの最新)
  galleryKind: "all",
  actions: {},               // タブ -> (生成物) => [{label, run}]  生成物ビューアに出すボタン(各タブの js が入れる)
  own: {},                   // タブ -> {history(grid), open(生成物), ids(), remove(ids)}  右側の生成物ビューアと履歴を自前で描くタブ(MV・ショート動画・編集タブ)。open はギャラリーでクリックされたとき(true を返せばギャラリーは開いたまま)、ids と remove は選択モードで、履歴の ID の一覧と、選んだものの削除(消したら true)
  select: { on: false, ids: new Set() },     // 選択モード(履歴とギャラリーで共通): 選んでいる ID。MV・ショート動画・編集タブの履歴では制作(プロジェクト)の ID
  on: { tab: [], items: [] },   // 各タブの js が入れる: タブが変わったとき / 一覧が更新されたとき
};

const byId = (id) => studio.items.find((i) => i.id === id);
// 一覧を読み直したとき、入力欄に入れた生成物が消えたとみなすか(一覧が全部で、そこに無い)。切れた一覧では、古くて載っていないだけかもしれないので外さない
// (消したときは studio-deleted で外す)
const goneFromGallery = (id) => studio.complete && !byId(id);
const fmtSec = (s) => (s >= 60 ? t("{m}分{s}秒", { m: Math.floor(s / 60), s: Math.round(s % 60) }) : t("{s}秒", { s: s.toFixed(1) }));
const fmtDur = (s) => `${Math.floor(s / 60)}:${String(Math.round(s % 60)).padStart(2, "0")}`;

async function loadGallery() {
  try {
    const items = await fetch(`/api/gallery?limit=${GALLERY_LIMIT}`, { headers: langHeaders() }).then((r) => r.json());
    if (!Array.isArray(items)) return;
    studio.items = items;
    studio.complete = items.length < GALLERY_LIMIT;
  } catch (e) { return; }
  renderHistory();
  renderGallery();
  renderViewer();
  renderSelectBar();
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
  const th = el("div", "thumb");
  th.title = (item.kind === "audio" && item.params && item.params.theme) || `${item.filename || ""}${item.width ? `  ${item.width}×${item.height}` : ""}`;
  if (item.thumb_url) {
    const img = el("img");
    img.src = item.thumb_url;
    img.alt = item.filename || "";
    img.loading = "lazy";
    img.draggable = false;          // ドラッグ元は外側の div にそろえる
    th.append(img);
  } else {
    th.append(el("div", "nothumb " + item.kind, item.kind === "audio" ? "🎵" : item.kind === "video" ? "▶" : KIND_LABEL[item.kind] || item.kind));
  }
  if (item.tab === "upload") th.append(el("span", "tbadge top", t("取込")));
  if (item.kind !== "image") {     // 動画は ▶ と長さ、音声は 🔊 と長さ
    const d = item.duration_s ? " " + fmtDur(item.duration_s) : "";
    th.append(el("span", "tbadge", (item.kind === "video" ? "▶" : "🎵") + d));
  }
  if (studio.select.on) { pickable(th, item.id); return th; }      // 選択モード: クリックは選び直し(×もドラッグも無し)
  th.onclick = () => onClick(item);
  if (onDelete) {
    const x = el("button", "tdel", "×");
    x.title = t("削除");
    x.onclick = (e) => { e.stopPropagation(); onDelete(item); };
    th.append(x);
  }
  makeDraggable(th, item.id);
  return th;
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
  if (!list.length) { grid.append(el("div", "placeholder", t("このタブで作ったものがここに並びます"))); return; }
  for (const it of list) {
    const t = thumbNode(it, (x) => { studio.viewing[studio.tab] = x.id; renderHistory(); renderViewer(); }, deleteItem);
    t.classList.toggle("sel", !studio.select.on && !!shown && shown.id === it.id);
    grid.append(t);
  }
}

// ---- 生成物ビューア ----

// 音楽のカード: スペクトラム(再生中に動く。viz.js)・タイトル(テーマ)・キャプション・プレーヤー・歌詞。色合いは、自作のジャンルが持つ雰囲気(viz)、無ければ曲のムード、それも無ければキャプションの言葉、それも無ければジャンルで決まる
function audioCard(item) {
  const p = item.params || {};
  const box = el("div", "audiocard");
  const a = el("audio");
  a.controls = true;
  a.src = item.url;
  const head = el("div", "ahead");       // アイコンとタイトルは 1 行に(スペクトラムの高さを確保する)
  head.append(el("div", "aicon", "🎵"), el("div", "atitle", p.theme || item.filename || ""));
  box.append(vizBox(a, { viz: p.viz, mood: p.mood, genre: p.genre, caption: p.caption }), head);
  if (p.caption) box.append(el("div", "acap", p.caption));
  box.append(a);
  if (p.lyrics && p.lyrics !== "[Instrumental]") {
    const d = el("details", "alyrics");
    d.append(el("summary", "", t("歌詞")), el("pre", "", p.lyrics));
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
    if (v.dataset.id !== "none") { v.dataset.id = "none"; detachVisualizers(v); v.replaceChildren(el("div", "placeholder", t("まだ生成物がありません"))); }
    return;
  }
  if (v.dataset.id !== item.id) {       // 同じものを出し直さない(動画の再生が途切れないように)
    v.dataset.id = item.id;
    detachVisualizers(v);                // 外す音楽のカードのスペクトラム(ループと音の経路)を片付ける
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
  if (sec) info.push(t("生成 {time}", { time: fmtSec(sec) }));
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
  if (studio.select.on) setSelecting(false);      // 選んだものは、履歴とギャラリーの間で持ち越さない
  $("workspace").classList.toggle("show-gallery", on);
  $("gallery").hidden = !on;
  if (on) loadGallery();
}

const galleryItems = () => studio.items.filter((i) => studio.galleryKind === "all" || i.kind === studio.galleryKind);

function renderGallery() {
  const grid = $("gal-grid");
  grid.replaceChildren();
  const list = galleryItems();
  if (!list.length) { grid.append(el("div", "placeholder", t("まだ何もありません"))); return; }
  for (const it of list) {
    grid.append(thumbNode(it, (x) => {      // クリック: その生成物を、いまのタブの生成物ビューアで開く(MV タブは、その MV を作った制作を開く)
      const own = studio.own[studio.tab];
      if (own) { const keep = own.open ? own.open(x) : false; if (!keep) setGalleryView(false); return; }      // open が true を返したら、ギャラリーは開いたまま(編集タブ: 続けてクリップを選べる)
      studio.viewing[studio.tab] = x.id;
      setGalleryView(false);
      renderHistory();
      renderViewer();
    }, deleteItem));
  }
}

// ---- 削除(1 件でも、まとめてでも) ----

// 生成物をサーバに消してもらう(1 回の通信)。まだ要るもの(使っているジョブや MV の制作があるもの)は断られるので、その理由を 1 回だけ知らせる
async function removeOutputs(items) {
  let js;
  try {
    const r = await fetch("/api/gallery/delete", { method: "POST", headers: langHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ ids: items.map((i) => i.id) }) });
    js = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : t("入力を確認してください"));
  } catch (e) { alert(t("削除できませんでした: {error}", { error: e.message || e })); return; }
  await forget([...js.deleted, ...js.missing]);
  if (js.refused.length) alert(refusedText(js.refused));
}

// 断られた生成物を、理由ごとにまとめた文(1 回の alert で足りるように)
function refusedText(refused, head = t("{n} 件は削除できませんでした。", { n: refused.length })) {
  const by = new Map();
  for (const f of refused) {
    const it = byId(f.id);
    by.set(f.reason, [...(by.get(f.reason) || []), (it && (it.filename || it.id)) || f.id]);
  }
  return `${head}\n` + [...by].map(([reason, names]) => t("・{reason}\n  {names}", { reason, names: names.slice(0, 5).join(t("、")) + (names.length > 5 ? t(" ほか {n} 件", { n: names.length - 5 }) : "") })).join("\n");
}

// 消えた生成物の後始末: ビューアで見ていたものは外し(そのタブの、次に新しいものか、無ければ空の表示になる)、各タブの入力欄の参照も外させて、一覧を読み直す
async function forget(ids) {
  for (const k of Object.keys(studio.viewing)) if (ids.includes(studio.viewing[k])) delete studio.viewing[k];
  document.dispatchEvent(new CustomEvent("studio-deleted", { detail: { ids } }));
  await loadGallery();
}

async function deleteItem(item) {
  if (!confirm(t("「{name}」を削除しますか?\nファイルも消え、元に戻せません。", { name: item.filename || item.id }))) return;
  await removeOutputs([item]);
}

// 選んだものをまとめて消す(確認は 1 回)。消したら true、やめたら false
async function deleteItems(items) {
  if (!confirm(t("{n} 件を削除しますか?\nファイルも消え、元に戻せません。", { n: items.length }))) return false;
  await removeOutputs(items);
  return true;
}

$("vdel").onclick = () => { const item = currentItem(); if (item) deleteItem(item); };

// ---- 選択モード(履歴・ギャラリー共通): サムネイルを選んで、まとめて消す ----

// サムネイルを選べるようにする: クリックで選び直し、選んでいるものに ✓ を出す。ドラッグ元にはしない(MV タブの制作のカードも使う)
function pickable(node, id) {
  node.classList.add("picking");
  node.classList.toggle("picked", studio.select.ids.has(id));
  node.append(el("span", "tcheck", "✓"));
  node.onclick = () => {
    const ids = studio.select.ids;
    if (!ids.delete(id)) ids.add(id);
    node.classList.toggle("picked", ids.has(id));
    renderSelectBar();
  };
}

// いま見えているグリッドの ID(ギャラリー、または履歴。MV タブの履歴は制作の ID)
function shownIds() {
  const own = studio.own[studio.tab];
  if ($("gallery").hidden && own) return own.ids();
  return ($("gallery").hidden ? tabItems() : galleryItems()).map((i) => i.id);
}

const pickedIds = () => shownIds().filter((id) => studio.select.ids.has(id));       // 選んでいるもののうち、いま見えているもの(絞り込みで隠れたものは数えない)

// 見出しの「選択」と、選択モードの「N 件を選択 / すべて選択 / 削除 / やめる」の切り替え
function renderSelectBar() {
  const on = studio.select.on, n = pickedIds().length;
  document.querySelectorAll(".seltoggle").forEach((b) => { b.hidden = on; });
  document.querySelectorAll(".selbar").forEach((bar) => {
    bar.hidden = !on;
    bar.querySelector(".selcount").textContent = t("{n} 件を選択", { n });
    bar.querySelector(".seldel").disabled = !n;
  });
}

function setSelecting(on) {
  studio.select.on = on;
  studio.select.ids.clear();
  renderSelectBar();
  renderHistory();
  renderGallery();
}

async function deleteSelected() {
  const ids = pickedIds(), own = studio.own[studio.tab];
  if (!ids.length) return;
  const done = $("gallery").hidden && own ? await own.remove(ids) : await deleteItems(ids.map(byId));
  if (done) setSelecting(false);
}

document.querySelectorAll(".seltoggle").forEach((b) => { b.onclick = () => setSelecting(true); });
document.querySelectorAll(".selend").forEach((b) => { b.onclick = () => setSelecting(false); });
document.querySelectorAll(".seldel").forEach((b) => { b.onclick = deleteSelected; });
document.querySelectorAll(".selall").forEach((b) => {
  b.onclick = () => {
    shownIds().forEach((id) => studio.select.ids.add(id));
    renderHistory();
    renderGallery();
    renderSelectBar();
  };
});

$("to-gallery").onclick = () => setGalleryView(true);
$("from-gallery").onclick = () => setGalleryView(false);
document.querySelectorAll("#g-chips .chip").forEach((c) => c.addEventListener("click", () => {
  studio.galleryKind = c.dataset.kind;
  document.querySelectorAll("#g-chips .chip").forEach((x) => x.classList.toggle("active", x === c));
  renderGallery();
  renderSelectBar();      // 絞り込みで見えなくなったものは、選んだ数に入れない
}));

// ---- タブ ----

function setTab(tab) {
  studio.tab = tab;
  if (studio.select.on) setSelecting(false);      // 選択モードは、タブを越えて続けない
  $("workspace").dataset.tab = tab;
  document.querySelectorAll(".tab").forEach((x) => { x.classList.toggle("active", x.dataset.tab === tab); x.setAttribute("aria-selected", String(x.dataset.tab === tab)); });
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
