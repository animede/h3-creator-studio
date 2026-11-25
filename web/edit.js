// 編集タブ: ギャラリーの動画・画像・曲を、GPU を使わず ffmpeg だけでつなぎ・切り・色を変え・音を差し替える簡易編集(docs/SPEC.md 第 4 版)。
// 左 = クリップの列・区間・操作、右上 = プレビュー動画と目盛り・書き出した動画、右下 = 編集プロジェクトの一覧(ギャラリーに切り替え可)。
// 操作のたびに、いまの state(view.state)から新しい state をこの画面で組み立てて PUT /api/edit/{id}/state に送り、続けて POST /api/edit/{id}/preview。
// サーバは検証するだけ(分ける・削除・挿入はここでやる)。ジョブ(プレビュー・書き出し)が走っている間だけ、2 秒おきに view を読み直す。

// ---- state の組み立て(DOM に触れない純粋な関数。tests/test_web_edit.py が node で動かして確かめる) ----
// 時間はすべて「プロジェクトの秒」: クリップをつないだだけの時間軸(境目の効果で縮む前)。state の形は docs/SPEC.md 第 4 版 §1。
const EDIT_LOGIC = (() => {
  const EPS = 0.0005;
  const r3 = (x) => Math.round(x * 1000) / 1000;
  const floor3 = (x) => Math.floor(x * 1000 + 1e-6) / 1000;
  const clone = (x) => JSON.parse(JSON.stringify(x));
  const clipLen = (c) => (c.kind === "image" ? Number(c.duration != null ? c.duration : c.out - c.in) : c.out - c.in);

  function timeline(clips) {
    let t = 0;
    return clips.map((c, i) => { const start = t; t = r3(t + clipLen(c)); return { index: i, start, end: t }; });
  }
  function total(clips) {
    const tl = timeline(clips);
    return tl.length ? tl[tl.length - 1].end : 0;
  }

  // クリップ c の、先頭から a 秒〜b 秒の部分(同じ生成物の in / out だけ変えた新しいクリップ)
  function piece(c, a, b) {
    const n = { ...c };
    if (c.kind === "image") { n.in = 0; n.out = r3(b - a); n.duration = n.out; }
    else { n.in = r3(c.in + a); n.out = r3(c.in + b); if ("duration" in c) n.duration = r3(n.out - n.in); }
    return n;
  }

  // 時刻 t で、そこを含むクリップを 2 つに分ける(境目の効果は、分けたところには付かない = cut)。戻り: { state, at: t より前のクリップの数 }
  function splitAt(state, t) {
    const s = clone(state), tl = timeline(s.clips);
    for (let i = 0; i < tl.length; i++) {
      if (t > tl[i].start + EPS && t < tl[i].end - EPS) {
        const c = s.clips[i], off = r3(t - tl[i].start);
        s.clips.splice(i, 1, piece(c, 0, off), piece(c, off, clipLen(c)));
        s.transitions = (s.transitions || []).map((x) => (x.after >= i ? { ...x, after: x.after + 1 } : x));
        return { state: s, at: i + 1 };
      }
    }
    return { state: s, at: tl.filter((x) => x.end <= t + EPS).length };
  }

  // 区間 [a, b] を削除して前後をつなぐ。かかるクリップは分けて、区間の外の部分だけを残す。
  // 残る境目の効果は付け替え(区間の中や、区間の端に触れた境目は消えて cut)、効果の区間(秒)は削った分だけ詰める。
  function deleteRange(state, a, b) {
    const tot = total(state.clips);
    a = Math.max(0, r3(a));
    b = Math.min(tot, r3(b));
    if (!(b - a > EPS)) throw new Error("区間の終了は、開始より後にしてください");
    const s = clone(state), tl = timeline(s.clips), out = [], map = {};
    tl.forEach((x, i) => {
      const c = s.clips[i];
      if (a - x.start > EPS) out.push(piece(c, 0, Math.min(x.end, a) - x.start));
      if (x.end - Math.max(x.start, b) > EPS) out.push(piece(c, Math.max(x.start, b) - x.start, x.end - x.start));
      if (x.end < a - EPS || x.end > b + EPS) map[i] = out.length - 1;      // 後ろの端がそのまま残る境目
    });
    s.clips = out;
    s.transitions = (s.transitions || []).filter((x) => x.after in map).map((x) => ({ ...x, after: map[x.after] }));
    const d = b - a, mp = (t) => (t <= a ? t : t >= b ? t - d : a);
    s.effects = (s.effects || []).map((e) => (e.start == null || e.end == null ? e : { ...e, start: r3(mp(e.start)), end: r3(mp(e.end)) }))
      .filter((e) => e.start == null || e.end == null || e.end - e.start > EPS);
    return s;
  }

  // 時刻 t にクリップ clip を挿入する(t を含むクリップは分ける)。t の位置の境目の効果は消え(cut)、後ろの境目は 1 つずれ、効果の区間は挿入した分だけ後ろへずれる
  function insertAt(state, t, clip) {
    const sp = splitAt(state, Math.max(0, Math.min(t, total(state.clips)))), s = sp.state, b = sp.at, len = clipLen(clip);
    const tl = timeline(s.clips);
    const p = b < tl.length ? tl[b].start : total(s.clips);
    s.clips.splice(b, 0, clip);
    s.transitions = (s.transitions || []).filter((x) => x.after !== b - 1).map((x) => (x.after >= b ? { ...x, after: x.after + 1 } : x));
    s.effects = (s.effects || []).map((e) => (e.start == null || e.end == null ? e
      : { ...e, start: r3(e.start >= p ? e.start + len : e.start), end: r3(e.end > p ? e.end + len : e.end) }));
    return s;
  }

  // 落とした生成物(probe の答え: kind / length_s)から、末尾に足すクリップ。画像は imgSec 秒
  function newClip(src, imgSec) {
    if (src.kind === "image") { const d = r3(imgSec); return { id: src.id, kind: "image", in: 0, out: d, duration: d }; }
    return { id: src.id, kind: "video", in: 0, out: floor3(src.length_s) };
  }

  // i 番目のクリップの in / out(画像は duration)を書き換える。範囲の誤りは Error(日本語)
  function setClip(state, i, patch, length) {
    const s = clone(state), c = s.clips[i];
    if (c.kind === "image") {
      const d = r3(patch.duration);
      if (!(d >= 0.1)) throw new Error("画像の秒数は 0.1 以上にしてください");
      c.in = 0; c.out = d; c.duration = d;
    } else {
      const a = r3(patch.in != null ? patch.in : c.in), b = r3(patch.out != null ? patch.out : c.out);
      if (!(a >= 0)) throw new Error("in は 0 以上にしてください");
      if (!(b - a >= 0.05)) throw new Error("out は、in より後にしてください");
      if (length != null && b > length + EPS) throw new Error(`out は、元の長さ(${r3(length)} 秒)までです`);
      c.in = a; c.out = b;
      if ("duration" in c) c.duration = r3(b - a);
    }
    return s;
  }

  // i 番目と、隣(dir = -1 で前、+1 で後)を入れ替える。境目の効果は位置に付いたまま
  function moveClip(state, i, dir) {
    const j = i + dir;
    if (j < 0 || j >= state.clips.length) return state;
    const s = clone(state);
    [s.clips[i], s.clips[j]] = [s.clips[j], s.clips[i]];
    return s;
  }

  // 境目 after の効果を type / 秒数にする(cut は書かない)。秒数は両側のクリップの半分(0.1 秒刻みで切り捨て)までに収める。0.5 秒に満たなければ Error
  function setTransition(state, after, type, duration) {
    const s = clone(state);
    s.transitions = (s.transitions || []).filter((x) => x.after !== after);
    if (type !== "cut") {
      const half = Math.floor(Math.min(clipLen(s.clips[after]), clipLen(s.clips[after + 1])) / 2 * 10 + 1e-6) / 10;
      const d = Math.min(duration, half);
      if (!(d >= 0.5)) throw new Error("前後のクリップが短くて、境目の効果を付けられません(両方とも 1 秒以上が要ります)");
      s.transitions.push({ after, type, duration: r3(d) });
      s.transitions.sort((x, y) => x.after - y.after);
    }
    return s;
  }

  // 操作のあとの整え: 範囲から外れた境目の効果・短すぎて付けられなくなった効果を外し、効果の区間を全体の長さに収める(収まらない・空になったものは外す)
  function fixup(state) {
    const s = clone(state), n = s.clips.length, tot = total(s.clips);
    s.transitions = (s.transitions || []).filter((x) => x.type !== "cut" && x.after >= 0 && x.after < n - 1).flatMap((x) => {
      const half = Math.floor(Math.min(clipLen(s.clips[x.after]), clipLen(s.clips[x.after + 1])) / 2 * 10 + 1e-6) / 10;
      const d = Math.min(x.duration, half);
      return d >= 0.5 ? [{ ...x, duration: r3(d) }] : [];
    });
    s.effects = (s.effects || []).map((e) => (e.start == null || e.end == null ? e : { ...e, start: Math.min(e.start, tot), end: Math.min(e.end, tot) }))
      .filter((e) => e.start == null || e.end == null || e.end - e.start > EPS);
    s.fade_in = Math.min(s.fade_in || 0, tot);
    s.fade_out = Math.min(s.fade_out || 0, tot);
    return s;
  }

  // 書き出しの時間軸(境目の効果で縮んだ後): 各クリップの始まり。crossfade / fadeblack は xfade なので、その秒数だけ後ろのクリップが前に食い込む
  function outStarts(state) {
    const tl = timeline(state.clips), starts = [];
    let shrink = 0;
    tl.forEach((x, i) => {
      if (i > 0) { const t = (state.transitions || []).find((y) => y.after === i - 1 && y.type !== "cut"); if (t) shrink += t.duration; }
      starts.push(r3(x.start - shrink));
    });
    return { tl, starts };
  }

  // プロジェクトの秒 t -> 書き出し(プレビュー)の秒。isEnd は、区間の終わりの側(クリップの境目では前のクリップの終わりに寄せる)
  function tlToOut(state, t, isEnd = false) {
    const { tl, starts } = outStarts(state);
    if (!tl.length) return 0;
    let j = 0;
    if (isEnd) { j = tl.findIndex((x) => t <= x.end + EPS); if (j < 0) j = tl.length - 1; }
    else { for (let k = 0; k < tl.length; k++) if (tl[k].start <= t + EPS) j = k; }
    return Math.max(0, starts[j] + Math.min(t - tl[j].start, tl[j].end - tl[j].start));
  }

  // 書き出し(プレビュー)の秒 p -> プロジェクトの秒(境目の食い込みの中は、後ろのクリップ側)
  function outToTl(state, p) {
    const { tl, starts } = outStarts(state);
    if (!tl.length) return 0;
    let j = 0;
    for (let k = 0; k < tl.length; k++) if (starts[k] <= p + EPS) j = k;
    return r3(tl[j].start + Math.max(0, Math.min(p - starts[j], tl[j].end - tl[j].start)));
  }

  return { r3, clone, clipLen, timeline, total, splitAt, deleteRange, insertAt, newClip, setClip, moveClip, setTransition, fixup, outStarts, tlToOut, outToTl };
})();

(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const L = EDIT_LOGIC;
  const ed = (id) => $("ed-" + id);
  const KEY = "edit-project";                       // 最後に開いていた編集プロジェクト(localStorage。使えなくても動く)
  const POLL_MS = 2000;
  const FX = {
    mono: { label: "モノクロ" },
    saturation: { label: "彩度", min: 0, max: 3, step: 0.1, def: 1.5 },
    contrast: { label: "コントラスト", min: 0, max: 3, step: 0.1, def: 1.3 },
    brightness: { label: "明るさ", min: -1, max: 1, step: 0.05, def: 0.2 },
    fade_to_black: { label: "黒に落とす" },
    sepia: { label: "セピア" }, warm: { label: "暖色" }, cool: { label: "寒色" }, vintage: { label: "フィルム風(退色)" },
    vignette: { label: "周辺を暗く" }, negate: { label: "色を反転" }, blur: { label: "ぼかし" },
  };
  const TR_LABEL = { cut: "カット", crossfade: "クロスフェード", fadeblack: "黒を挟む" };
  const AUDIO_LABEL = { original: "元の音", music: "曲に差し替え", mix: "元の音 + 曲", none: "音なし" };
  const STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600];      // 目盛りの間隔(秒)の候補

  let projects = [];                // 編集プロジェクトの一覧(新しい順)
  let current = null;               // 選んでいるプロジェクトの ID
  let view = null;                  // その view(サーバの答えのまま)
  let timer = null;
  let working = false;              // 操作を送っている最中(この間は次の操作を受けない)
  let gen = 0;                      // 操作の世代。古い通信の答えで、新しい表示を上書きしない
  let insItem = null;               // 挿入待ちの動画・画像(生成物のレコード)
  const songs = {};                 // 曲の ID -> 生成物のレコード(ギャラリーに載る前の取り込みでも名前を出せるように)
  let opsSig = "", rulerSig = "", cardsSig = "", shownPreview = null, shownOutput = null, playerFor = null;
  const api = apiJson;              // JSON の API(web/project.js)

  const f = (x) => String(Math.round(x * 100) / 100);
  const num = (input) => parseFloat(input.value);
  const mmss = (s) => `${Math.floor(s / 60)}:${(s % 60).toFixed(1).padStart(4, "0")}`;
  const totalTl = () => (view ? L.total(view.state.clips) : 0);

  function showMsg(text) {
    ed("msg").hidden = !text;
    ed("msg").textContent = text || "";
  }
  const errText = (e) => String((e && e.message) || e);

  // ---- 一覧・選択 ----

  async function loadList() {
    try { projects = await api("/api/edit"); } catch (e) { return; }
    renderProjSelect();
    renderCards();
  }

  function remember(id) { try { if (id) localStorage.setItem(KEY, id); else localStorage.removeItem(KEY); } catch (e) { /* 保存できなくても動く */ } }

  async function refresh() {
    await loadList();
    if (current && !projects.some((p) => p.id === current)) { current = null; view = null; }
    if (!current && projects.length) {
      let saved = null;
      try { saved = localStorage.getItem(KEY); } catch (e) { /* 保存できなくても動く */ }
      current = (projects.find((p) => p.id === saved) || projects[0]).id;
    }
    if (!current) { setView(null); return; }
    if (!view || view.id !== current) await selectProject(current);
    else await reload();
  }

  // 開いているプロジェクトを、表示を消さずに読み直す(タブに戻ったとき。別のところで進んだジョブも拾う)
  async function reload() {
    const id = current;
    try {
      const v = await api(`/api/edit/${id}`);
      if (current === id && !working) setView(v);
    } catch (e) { /* 次に開いたとき */ }
  }

  async function selectProject(id) {
    current = id;
    remember(id);
    gen++;
    showMsg("");
    setView(null);
    renderProjSelect();
    try {
      const v = await api(`/api/edit/${id}`);
      if (current === id) setView(v);
    } catch (e) {
      showMsg(`開けませんでした: ${errText(e)}`);
      await loadList();
    }
  }

  let projSig = "";
  function renderProjSelect() {
    ed("rename").disabled = ed("del").disabled = !current;
    const sig = JSON.stringify([current, projects.map((p) => [p.id, p.name])]);
    if (sig === projSig) return;          // 開いている選択肢を、2 秒おきの更新で閉じない
    projSig = sig;
    const sel = ed("proj");
    sel.replaceChildren();
    if (!projects.length) {
      const o = el("option", "", "(編集プロジェクトがありません)");
      o.value = "";
      sel.append(o);
    }
    for (const p of projects) {
      const o = el("option", "", p.name || "無題");
      o.value = p.id;
      sel.append(o);
    }
    sel.value = current || "";
  }

  ed("proj").onchange = () => { if (ed("proj").value) selectProject(ed("proj").value); };

  ed("new").onclick = async () => {
    const name = prompt("編集プロジェクトの名前", "");
    if (name === null) return;
    try {
      const v = await api("/api/edit", "POST", { name: name.trim() || "無題" });
      current = v.id;
      remember(v.id);
      await loadList();
      setView(v);
    } catch (e) { showMsg(`作れませんでした: ${errText(e)}`); }
  };

  ed("rename").onclick = async () => {
    if (!view) return;
    const name = prompt("新しい名前", view.name || "");
    if (name === null || !name.trim()) return;
    try {
      const v = await api(`/api/edit/${view.id}`, "PATCH", { name: name.trim() });
      if (current === v.id) setView(v);
      await loadList();
    } catch (e) { showMsg(`名前を変えられませんでした: ${errText(e)}`); }
  };

  ed("del").onclick = () => { if (view) removeProjects([view.id]); };

  // 編集プロジェクトを消す(確認は 1 回。書き出した動画はギャラリーに残る)。消したら true
  async function removeProjects(ids) {
    const names = ids.map((id) => (projects.find((p) => p.id === id) || {}).name || id);
    if (!confirm(`編集プロジェクト${ids.length === 1 ? `「${names[0]}」` : ` ${ids.length} 件`}を削除しますか?\n書き出した動画はギャラリーに残ります。元のクリップも消えません。`)) return false;
    for (const id of ids) {
      try { await api(`/api/edit/${id}`, "DELETE"); } catch (e) { showMsg(`削除できませんでした: ${errText(e)}`); }
      if (current === id) { current = null; view = null; remember(null); }
    }
    await refresh();
    return true;
  }

  // ---- 操作の送信: state を作って PUT、続けてプレビュー ----

  // fn(id) は view を返す通信。終わったら view を入れ替え(undo / redo / state の変更)、previewAfter ならプレビューを作らせる
  async function work(fn, previewAfter = true) {
    if (!view || working) return;
    const id = view.id;
    working = true;
    gen++;
    showMsg("");
    renderFoot();
    try {
      const v = await fn(id);
      if (current !== id) return;
      setView(v);
      if (previewAfter && v.state.clips.length) {
        try { if (current === id) setView(await api(`/api/edit/${id}/preview`, "POST")); }
        catch (e) { showMsg(`プレビューを作れませんでした: ${errText(e)}`); }
      }
    } catch (e) {
      showMsg(errText(e));
      renderOps(true);          // 入力欄を、いまの state の値に戻す
    } finally {
      working = false;
      renderFoot();
      loadList();
    }
  }

  // build(view) は新しい state を返す(または throw)。整えてから送る
  const mutate = (build) => work(async (id) => api(`/api/edit/${id}/state`, "PUT", { state: L.fixup(await build(view)) }));

  const probe = (item) => api("/api/edit/probe", "POST", { id: item.id });

  // ---- 区間 ----

  // 開始・終了(秒)。誤りは Error
  function getRange() {
    const a = num(ed("start")), b = num(ed("end")), tot = totalTl();
    if (!(a >= 0) || !(b > a)) throw new Error("区間は、開始 < 終了(秒)で入れてください");
    if (a >= tot) throw new Error(`開始は、全体の長さ(${f(tot)} 秒)より前にしてください`);
    return [a, Math.min(b, tot)];
  }

  function renderRange(reset) {
    const tot = totalTl();
    let a = num(ed("start")), b = num(ed("end"));
    if (reset || !(a >= 0)) a = 0;
    if (a > tot) a = 0;
    if (reset || !(b > 0) || b > tot || b <= a) b = tot;
    ed("start").value = f(a);
    ed("end").value = f(b);
    ed("start").max = ed("end").max = f(tot);
    ed("range-note").textContent = view ? `全体 ${f(tot)} 秒(つないだだけの長さ)` : "";
    renderSel();
  }

  // 目盛りに、いまの区間(開始〜終了)を重ねる
  function renderSel() {
    const sel = ed("sel"), a = num(ed("start")), b = num(ed("end"));
    const dur = view ? view.duration : 0;
    if (!view || !dur || !(a >= 0) || !(b > a)) { sel.hidden = true; ed("sel-text").textContent = ""; return; }
    const x = L.tlToOut(view.state, a), y = L.tlToOut(view.state, Math.min(b, totalTl()), true);
    sel.hidden = false;
    sel.style.left = `${Math.min(100, x / dur * 100)}%`;
    sel.style.width = `${Math.max(0.3, Math.min(100 - x / dur * 100, (y - x) / dur * 100))}%`;
    ed("sel-text").textContent = `区間 ${f(a)}〜${f(b)} 秒`;
  }
  ed("start").oninput = ed("end").oninput = renderSel;
  ed("start").onchange = ed("end").onchange = renderSel;

  // プレビューの再生位置(書き出しの秒)を、プロジェクトの秒にして入れる
  function fromPlayer(field) {
    if (!view) return;
    ed(field).value = f(L.outToTl(view.state, ed("video").currentTime || 0));
    renderSel();
  }
  ed("from").onclick = ed("from2").onclick = () => fromPlayer("start");       // 左の欄と、右のプレビューの下の両方にある
  ed("to").onclick = ed("to2").onclick = () => fromPlayer("end");
  ed("all").onclick = () => { ed("start").value = "0"; ed("end").value = f(totalTl()); renderSel(); };

  // ---- 操作 ----

  ed("delrange").onclick = () => {
    let r;
    try { r = getRange(); } catch (e) { showMsg(errText(e)); return; }
    mutate((v) => L.deleteRange(v.state, r[0], r[1]));
  };

  // 生成物の種類を確かめて、動画・画像だけを受ける。受けたら true
  const isClipKind = (it) => it && (it.kind === "video" || it.kind === "image");

  function addItems(items) {
    const ok = items.filter(isClipKind);
    if (!ok.length) { showMsg("クリップには動画か画像を落としてください(曲は、下の「音声」の欄へ)"); return; }
    mutate(async (v) => {
      const s = L.clone(v.state);
      for (const it of ok) s.clips.push(L.newClip({ ...(await probe(it)), id: it.id }, 3));      // 画像は 3 秒(あとで行の欄で変えられる)
      return s;
    });
  }

  function setIns(item) {
    if (!isClipKind(item)) { showMsg("挿入には動画か画像を落としてください"); return; }
    insItem = item;
    showMsg("");
    renderIns();
  }

  function renderIns() {
    const body = ed("ins-body");
    body.replaceChildren();
    ed("ins-go").disabled = !insItem || !view || working;
    if (!insItem) { body.append(el("div", "refs-empty", "挿入する動画・画像をここにドロップ")); return; }
    const row = el("div", "edinsitem");
    const img = el("img");
    img.src = insItem.thumb_url || insItem.url;
    img.draggable = false;
    const x = el("button", "rdel", "×");
    x.type = "button";
    x.title = "外す";
    x.onclick = () => { insItem = null; renderIns(); };
    row.append(img, el("span", "edname", insItem.filename || insItem.id), x);
    body.append(row);
  }

  ed("ins-go").onclick = () => {
    if (!insItem) return;
    const item = insItem, at = num(ed("start")), sec = num(ed("ins-sec"));
    if (!(at >= 0)) { showMsg("挿入する位置を、開始(秒)に入れてください"); return; }
    if (item.kind === "image" && !(sec >= 0.5)) { showMsg("画像の秒数は 0.5 以上にしてください"); return; }
    mutate(async (v) => {
      const clip = L.newClip({ ...(await probe(item)), id: item.id }, sec);
      const s = L.insertAt(v.state, at, clip);
      insItem = null;
      renderIns();
      return s;
    });
  };

  // 色調(fx: 見た目を選ぶ、値なし)と調整(adj: 値を決める)。どちらも同じ effects に積む
  function renderFxInput() {
    const type = ed("adj-type").value, d = FX[type];
    ed("adj-valwrap").hidden = !d.step;
    if (d.step) {
      const r = ed("adj-val");
      if (r.dataset.type !== type) { r.min = d.min; r.max = d.max; r.step = d.step; r.value = d.def; r.dataset.type = type; }
      ed("adj-valtext").textContent = f(num(r));
    }
    ed("adj-all").disabled = type === "fade_to_black" || !view || working;      // 黒に落とすのは、区間を決めて使う
    ed("fx-all").disabled = !view || working;
  }
  ed("adj-type").onchange = renderFxInput;
  ed("adj-val").oninput = renderFxInput;

  function applyFx(which, whole) {
    const type = (which === "fx" ? ed("fx-type") : ed("adj-type")).value, d = FX[type];
    let r = [null, null];
    if (!whole) { try { r = getRange(); } catch (e) { showMsg(errText(e)); return; } }
    mutate((v) => {
      const s = L.clone(v.state);
      s.effects = s.effects || [];
      s.effects.push({ type, value: d.step ? L.r3(num(ed("adj-val"))) : 1, start: r[0], end: r[1] });
      return s;
    });
  }
  ed("fx-apply").onclick = () => applyFx("fx", false);
  ed("fx-all").onclick = () => applyFx("fx", true);
  ed("adj-apply").onclick = () => applyFx("adj", false);
  ed("adj-all").onclick = () => applyFx("adj", true);

  const fxText = (e) => { const d = FX[e.type] || { label: e.type }; return `${d.label}${d.step ? ` ${f(e.value)}` : ""}  ${e.start == null ? "全体" : `${f(e.start)}〜${f(e.end)} 秒`}`; };

  function renderFxList() {
    const box = ed("fx-list");
    box.replaceChildren();
    const list = view ? view.state.effects || [] : [];
    if (!list.length) { box.append(el("div", "note", "色調はまだ付けていません")); return; }
    list.forEach((e, i) => {
      const row = el("div", "edfxrow");
      row.append(el("span", `edfxdot fx-${e.type}`), el("span", "edname", fxText(e)));
      const x = el("button", "rdel", "×");
      x.type = "button";
      x.title = "この色調を外す";
      x.onclick = () => mutate((v) => { const s = L.clone(v.state); s.effects.splice(i, 1); return s; });
      row.append(x);
      box.append(row);
    });
  }

  function renderTrans() {
    const box = ed("trans");
    box.replaceChildren();
    const n = view ? view.state.clips.length : 0;
    if (n < 2) { box.append(el("div", "note", "クリップが 2 つ以上になると、境目ごとに選べます")); return; }
    for (let i = 0; i < n - 1; i++) {
      const t = (view.state.transitions || []).find((x) => x.after === i && x.type !== "cut");
      const row = el("div", "edtrow");
      row.append(el("span", "edtlabel", `${i + 1} → ${i + 2}`));
      const sel = el("select");
      sel.setAttribute("aria-label", `${i + 1} 番目と ${i + 2} 番目の境目`);
      for (const [v, label] of Object.entries(TR_LABEL)) { const o = el("option", "", label); o.value = v; sel.append(o); }
      sel.value = t ? t.type : "cut";
      const sec = el("input");
      sec.type = "number"; sec.min = "0.5"; sec.max = "2"; sec.step = "0.1";
      sec.value = t ? f(t.duration) : "1";
      sec.disabled = !t;
      sec.className = "edsmall";
      sec.setAttribute("aria-label", "境目の秒数");
      const apply = () => mutate((v) => L.setTransition(v.state, i, sel.value, Math.min(2, Math.max(0.5, num(sec) || 1))));
      sel.onchange = apply;
      sec.onchange = apply;
      row.append(sel, sec, el("span", "edunit", "秒"));
      box.append(row);
    }
  }

  function setFade(key, input) {
    const x = num(input);
    if (!(x >= 0)) { showMsg("フェードの秒数は 0 以上にしてください"); renderOps(true); return; }
    mutate((v) => { const s = L.clone(v.state); s[key] = L.r3(x); return s; });
  }
  ed("fadein").onchange = () => setFade("fade_in", ed("fadein"));
  ed("fadeout").onchange = () => setFade("fade_out", ed("fadeout"));

  // ---- 音声 ----

  ed("audio").onchange = () => {
    const mode = ed("audio").value;
    mutate((v) => {
      const s = L.clone(v.state);
      s.audio = s.audio || { mode: "original", song_id: null, song_db: -14 };
      if ((mode === "music" || mode === "mix") && !s.audio.song_id) throw new Error("曲に差し替えるには、先に曲をドロップしてください(音楽タブの曲・ギャラリー・PC の音声ファイル)");
      s.audio.mode = mode;
      return s;
    });
  };

  function setSong(item) {
    if (!item || item.kind !== "audio") { showMsg("曲には音声を落としてください(音楽タブの曲、または音声ファイル)"); return; }
    songs[item.id] = item;
    mutate((v) => {
      const s = L.clone(v.state);
      s.audio = s.audio || { mode: "original", song_id: null, song_db: -14 };
      s.audio.song_id = item.id;
      if (s.audio.mode === "original" || s.audio.mode === "none") s.audio.mode = "music";      // 曲を落としたのだから、使う
      return s;
    });
  }

  function clearSong() {
    mutate((v) => {
      const s = L.clone(v.state);
      s.audio.song_id = null;
      if (s.audio.mode === "music" || s.audio.mode === "mix") s.audio.mode = "original";
      return s;
    });
  }

  function renderSong() {
    const body = ed("song-body");
    body.replaceChildren();
    const id = view && view.state.audio && view.state.audio.song_id;
    if (!id) { body.append(el("div", "refs-empty", "ここに曲をドロップ")); return; }
    const song = songs[id] || byId(id);
    const c = el("div", "songitem");
    const text = el("div", "stext");
    const p = (song && song.params) || {};
    text.append(el("div", "stitle", p.theme || (song && song.filename) || "曲"),
      el("div", "sinfo", [song && song.duration_s ? fmtDur(song.duration_s) : "", song && song.filename].filter(Boolean).join("  ")));
    const x = el("button", "rdel", "×");
    x.type = "button";
    x.title = "外す";
    x.onclick = clearSong;
    c.append(el("div", "aicon", "🎵"), text, x);
    body.append(c);
  }

  // ---- ドロップ ----

  // PC のファイルは、取り込んでからギャラリーの生成物として使う(種類が合わないものはギャラリーに残すだけ)
  async function upload(files, box, kinds, label, use) {
    try {
      const recs = (await uploadFiles(files, box)).filter((r) => kinds.includes(r.kind));
      if (!recs.length) { showMsg(`${label}のファイルではありません(ギャラリーには保存しました)`); return; }
      use(recs);
    } catch (e) { showMsg(`取り込めませんでした: ${errText(e)}`); }
  }

  const active = () => studio.tab === "edit" && !!view && !working;
  wireDrop(ed("clips-drop"), { active, msg: showMsg, onItem: (it) => addItems([it]), onFiles: (files) => upload(files, ed("clips-drop"), ["video", "image"], "動画・画像", addItems) });
  wireDrop(ed("ins-drop"), { active, msg: showMsg, onItem: setIns, onFiles: (files) => upload(files, ed("ins-drop"), ["video", "image"], "動画・画像", (recs) => setIns(recs[0])) });
  wireDrop(ed("song"), { active, msg: showMsg, onItem: setSong, onFiles: (files) => upload(files, ed("song"), ["audio"], "音声", (recs) => setSong(recs[0])) });

  // ---- クリップの列 ----

  function renderClips() {
    const box = ed("clips");
    box.replaceChildren();
    const clips = view ? view.clips : [];
    ed("clips-empty").hidden = clips.length > 0;
    if (!view) return;
    const { starts } = L.outStarts(view.state);
    clips.forEach((c, i) => {
      const sc = view.state.clips[i];
      const row = el("div", "edclip" + (c.missing ? " missing" : ""));
      const img = el("img");
      img.src = c.thumb_url || "";
      img.alt = c.filename || "";
      img.draggable = false;
      img.title = "クリックで、その位置を再生します";
      img.onclick = () => seekOut(starts[i]);
      const main = el("div", "edmain");
      const head = el("div", "edhead");
      const name = el("span", "edname", `${i + 1}. ${c.filename || c.id}`);
      name.title = c.filename || c.id;
      head.append(name, el("span", "edlen", c.missing ? "見つかりません" : `${f(L.clipLen(sc))} 秒${sc.kind === "video" && c.length_s ? `(元 ${f(c.length_s)} 秒)` : ""}`));
      const fields = el("div", "edfields");
      const mk = (label, value, min, title, max) => {
        const lab = el("label", "edfield");
        const inp = el("input");
        inp.type = "number"; inp.step = "0.1"; inp.min = String(min); inp.value = f(value);
        if (max != null) inp.max = String(max);
        inp.title = title;
        lab.append(el("span", "", label), inp);
        fields.append(lab);
        return inp;
      };
      if (sc.kind === "image") {
        const dur = mk("秒数", sc.duration != null ? sc.duration : sc.out, 0.1, "画像を出す秒数");
        dur.onchange = () => mutate((v) => L.setClip(v.state, i, { duration: num(dur) }, null));
      } else {
        const a = mk("in", sc.in, 0, "元の動画の、使い始めの秒", c.length_s);
        const b = mk("out", sc.out, 0, "元の動画の、使い終わりの秒", c.length_s);
        const go = () => mutate((v) => L.setClip(v.state, i, { in: num(a), out: num(b) }, c.length_s));
        a.onchange = go;
        b.onchange = go;
      }
      main.append(head, fields);
      const btns = el("div", "edbtns");
      const mkb = (text, title, fn, disabled) => {
        const b = el("button", "mini", text);
        b.type = "button";
        b.title = title;
        b.disabled = !!disabled;
        b.onclick = fn;
        btns.append(b);
      };
      mkb("▲", "前へ", () => mutate((v) => L.moveClip(v.state, i, -1)), i === 0);
      mkb("▼", "後ろへ", () => mutate((v) => L.moveClip(v.state, i, 1)), i === clips.length - 1);
      mkb("×", "このクリップを外す(元の動画は残ります)", () => {
        const tl = L.timeline(view.state.clips)[i];
        mutate((v) => L.deleteRange(v.state, tl.start, tl.end));
      });
      row.append(img, main, btns);
      box.append(row);
    });
  }

  // ---- プレビュー・目盛り ----

  function seekOut(p) {
    const v = ed("video");
    if (!v.getAttribute("src")) return;
    try { v.currentTime = Math.max(0, isFinite(v.duration) ? Math.min(p, v.duration) : p); } catch (e) { /* まだ読み込み前 */ }
  }

  function renderPlayer() {
    const vid = ed("video"), pv = view && view.preview, job = view && view.job;
    const url = pv ? pv.url : null;
    const pid = view ? view.id : null;
    if (playerFor !== pid) { playerFor = pid; shownPreview = null; vid.removeAttribute("src"); vid.load(); }      // 別のプロジェクト: 前のプレビューを外す
    if (url !== shownPreview) {
      const keep = shownPreview ? vid.currentTime || 0 : 0;        // 作り直したら、見ていた位置に戻す
      shownPreview = url;
      if (url) {
        vid.src = url;
        vid.addEventListener("loadedmetadata", () => { try { vid.currentTime = Math.min(keep, vid.duration || keep); } catch (e) { /* 戻せなくてもよい */ } }, { once: true });
      } else { vid.removeAttribute("src"); vid.load(); }
    }
    vid.hidden = !url;
    ed("empty").hidden = !!url;
    ed("empty-text").textContent = !view ? "編集プロジェクトを選ぶか、新規に作ってください"
      : !view.state.clips.length ? "クリップを追加すると、ここにプレビューが出ます" : job ? "プレビューを作っています…" : "プレビューがありません";
    const previewing = !!job && job.kind === "edit.preview";
    ed("stage").classList.toggle("busy", previewing);
    ed("wait").hidden = !job;
    if (job) {
      const pct = job.progress != null ? ` ${Math.round(job.progress * 100)}%` : "";
      const head = job.kind === "edit.export" ? "書き出し中" : "プレビューを作っています";
      ed("wait-text").textContent = `${head}${pct}${job.message ? " - " + job.message : ""}${job.error ? " - " + job.error : ""}`;
      const bar = ed("wait-bar");
      bar.classList.toggle("indet", job.progress == null);
      bar.querySelector("i").style.width = job.progress != null ? `${Math.round(job.progress * 100)}%` : "";
    }
    const stale = !!pv && pv.stale && !previewing;
    ed("stale").hidden = !(stale || (!url && view && view.state.clips.length && !job));
    ed("stale").firstChild.textContent = stale ? "プレビューは古い版です " : "プレビューがありません ";
    updateTime();
  }
  ed("regen").onclick = () => work(async (id) => api(`/api/edit/${id}/preview`, "POST"), false);

  // 目盛り: 幅 100% = 書き出しの長さ(view.duration)。クリップを交互の濃さで、境目を線で、効果の区間を細い帯で
  function renderRuler() {
    const segs = ed("segs"), fxs = ed("fxs"), ticks = ed("ticks");
    segs.replaceChildren();
    fxs.replaceChildren();
    ticks.replaceChildren();
    const dur = view ? view.duration : 0;
    ed("ruler").classList.toggle("empty", !dur);
    if (!view || !dur) return;
    const st = view.state, { tl, starts } = L.outStarts(st);
    const pct = (x) => `${Math.max(0, Math.min(100, x / dur * 100))}%`;
    tl.forEach((x, j) => {
      const len = x.end - x.start;
      const seg = el("div", "edseg " + (j % 2 ? "b" : "a"));
      seg.style.left = pct(starts[j]);
      seg.style.width = pct(Math.min(len, dur - starts[j]));
      const c = view.clips[j];
      seg.title = `${j + 1}. ${(c && c.filename) || ""}`;
      seg.append(el("span", "", String(j + 1)));
      segs.append(seg);
      if (j > 0) {
        const t = (st.transitions || []).find((y) => y.after === j - 1 && y.type !== "cut");
        const mark = el("div", "edjoin" + (t ? ` ${t.type}` : ""));
        mark.style.left = pct(starts[j]);
        if (t) mark.style.width = pct(t.duration);
        mark.title = `境目 ${j} → ${j + 1}: ${TR_LABEL[t ? t.type : "cut"]}${t ? ` ${f(t.duration)} 秒` : ""}`;
        segs.append(mark);
      }
    });
    const list = st.effects || [];
    fxs.style.height = `${Math.max(1, list.length) * 6}px`;
    list.forEach((e, k) => {
      const a = e.start == null ? 0 : L.tlToOut(st, e.start), b = e.end == null ? dur : L.tlToOut(st, e.end, true);
      const strip = el("div", `edfx fx-${e.type}`);
      strip.style.left = pct(a);
      strip.style.width = pct(Math.max(0.05, b - a));
      strip.style.top = `${k * 6}px`;
      strip.title = fxText(e);
      fxs.append(strip);
    });
    const step = STEPS.find((s) => dur / s <= 12) || STEPS[STEPS.length - 1];
    for (let t = 0; t <= dur + 1e-6; t += step) {
      const tick = el("div", "edtick");
      tick.style.left = pct(t);
      tick.append(el("span", "", step < 60 && dur < 60 ? `${t}s` : fmtDur(t)));
      ticks.append(tick);
    }
    renderSel();
  }

  ed("ruler").addEventListener("click", (e) => {
    if (!view || !view.duration) return;
    const r = ed("ruler").getBoundingClientRect();
    if (!r.width) return;
    seekOut(Math.max(0, Math.min(1, (e.clientX - r.left) / r.width)) * view.duration);
  });

  function updateTime() {
    const v = ed("video"), dur = view ? view.duration : 0;
    const cur = v.getAttribute("src") ? v.currentTime || 0 : 0;
    ed("play").style.left = dur ? `${Math.max(0, Math.min(100, cur / dur * 100))}%` : "0";
    const tl = view && view.state.clips.length ? L.outToTl(view.state, cur) : 0;
    ed("time").textContent = view ? `再生位置 ${mmss(cur)} / ${mmss(dur)}${dur ? `(プロジェクトの ${f(tl)} 秒)` : ""}` : "";
  }
  ed("video").addEventListener("timeupdate", updateTime);
  ed("video").addEventListener("seeked", updateTime);

  // 最後に書き出した動画
  function renderOutput() {
    const o = view && view.output;
    ed("out").hidden = !o;
    if (!o) { shownOutput = null; return; }
    if (shownOutput !== o.url) {
      shownOutput = o.url;
      ed("out-video").src = o.url;
      const n = el("span", "edoutname", o.filename || "");
      n.title = "ほかのタブへドラッグして使えます";
      makeDraggable(n, o.id);        // ふつうの動画として、他のタブへ落とせる
      ed("out-name").replaceChildren(n);
    }
    ed("out-info").textContent = [o.width ? `${o.width}×${o.height}` : "", o.duration_s ? fmtSec(o.duration_s) : ""].filter(Boolean).join("  ");
    ed("out-save").href = o.url;
    ed("out-save").download = o.filename || "";
  }

  // ---- 表示の更新 ----

  function renderFoot() {
    const v = view, job = v && v.job;
    ed("body").disabled = !v || working;
    ed("undo").disabled = !v || working || !v.can_undo;
    ed("redo").disabled = !v || working || !v.can_redo;
    ed("export").disabled = !v || working || !v.state.clips.length || !!(job && job.kind === "edit.export");
    ed("ins-go").disabled = !insItem || !v || working;
    ed("fx-all").disabled = !v || working;
    ed("adj-all").disabled = ed("adj-type").value === "fade_to_black" || !v || working;
    ed("intro").hidden = !!v;
  }

  // 左の操作欄(クリップ・効果・境目・フェード・音声)。state と素材が変わらない間(ジョブの 2 秒おきの更新)は描き直さない
  function renderOps(force) {
    const sig = view ? JSON.stringify([view.id, view.state, view.clips]) : "";
    if (!force && sig === opsSig) return;
    const fresh = !opsSig || !view || !opsSig.startsWith(`["${view.id}"`);
    opsSig = sig;
    renderClips();
    renderRange(fresh);
    renderFxList();
    renderTrans();
    renderFxInput();
    renderSong();
    renderIns();
    const st = view ? view.state : null;
    ed("fadein").value = st ? f(st.fade_in || 0) : "0";
    ed("fadeout").value = st ? f(st.fade_out || 0) : "0";
    ed("audio").value = st && st.audio ? st.audio.mode : "original";
  }

  function render() {
    renderOps(false);
    const sig = view ? JSON.stringify([view.id, view.state, view.duration, view.clips.map((c) => c.filename)]) : "";
    if (sig !== rulerSig) { rulerSig = sig; renderRuler(); }
    renderPlayer();
    renderOutput();
    renderFoot();
    renderProjSelect();
  }

  let lastJob = null;
  function setView(v) {
    const prev = lastJob;
    view = v;
    lastJob = v && v.job ? v.job.kind : null;
    if (!v) { opsSig = rulerSig = "\0"; }          // 空に描き直す(前のプロジェクトの表示を残さない)
    render();
    if (prev === "edit.export" && !lastJob) loadGallery();        // 書き出した動画がギャラリー(と履歴)に載った
    schedule();
  }

  // ジョブが走っている間だけ、2 秒おきに読み直す
  function schedule() {
    clearTimeout(timer);
    if (!view || !view.job || studio.tab !== "edit") return;
    const id = view.id;
    timer = setTimeout(async () => {
      const g = gen;
      try {
        const v = await api(`/api/edit/${id}`);
        if (g === gen && current === id && !working) setView(v);       // 操作の途中や、操作が済んだあとの古い答えでは、表示を戻さない
      } catch (e) { /* 次の周で再試行 */ }
      if (current === id && studio.tab === "edit") schedule();
    }, POLL_MS);
  }

  // ---- ボタン ----

  ed("undo").onclick = () => work(async (id) => api(`/api/edit/${id}/undo`, "POST"));
  ed("redo").onclick = () => work(async (id) => api(`/api/edit/${id}/redo`, "POST"));
  ed("export").onclick = () => work(async (id) => api(`/api/edit/${id}/export`, "POST"), false);

  // ---- 右下: 編集プロジェクトの一覧(履歴の欄。ギャラリーに切り替え可) ----

  function projectNode(p) {
    const t = el("div", "thumb");
    if (p.thumb_url) {
      const img = el("img");
      img.src = p.thumb_url;
      img.alt = p.name || "";
      img.loading = "lazy";
      img.draggable = false;
      t.append(img);
    } else {
      t.append(el("div", "nothumb video", "編集"));
    }
    t.append(el("span", "tbadge top", p.name || "無題"));
    t.append(el("span", "tbadge", `${p.duration ? fmtDur(p.duration) + " · " : ""}${p.clips || 0} クリップ`));
    t.classList.toggle("sel", !studio.select.on && p.id === current);
    if (studio.select.on) { pickable(t, p.id); return t; }          // 選択モード: クリックは選び直し(×もドラッグも無し)
    t.onclick = () => { if (p.id !== current) selectProject(p.id); };
    const x = el("button", "tdel", "×");
    x.title = "削除";
    x.onclick = (e) => { e.stopPropagation(); removeProjects([p.id]); };
    t.append(x);
    if (p.output && p.output.id) makeDraggable(t, p.output.id);      // 書き出した動画は、ふつうの動画として他のタブへ落とせる
    return t;
  }

  // 一覧が変わっていなければ描き直さない(ドラッグ中のカードを作り直さないように)。タブの切り替えなどでは force で描く
  function renderCards(force = false) {
    const grid = $("hist-grid");
    if (studio.tab !== "edit") return;
    const sig = JSON.stringify([current, projects.map((p) => [p.id, p.name, p.duration, p.clips, p.thumb_url, p.output && p.output.id])]);
    if (!force && sig === cardsSig) return;
    cardsSig = sig;
    grid.replaceChildren();
    if (!projects.length) grid.append(el("div", "placeholder", "編集プロジェクトがここに並びます"));
    for (const p of projects) grid.append(projectNode(p));
    renderSelectBar();
  }

  studio.own.edit = {
    history: () => renderCards(true),
    open: (item) => {          // ギャラリーで書き出した動画を選んだら、それを作った編集プロジェクトを開く
      const pid = item.params && item.params.project_id;
      if (pid && projects.some((p) => p.id === pid)) selectProject(pid);
    },
    ids: () => projects.map((p) => p.id),
    remove: removeProjects,
  };

  studio.on.tab.push((tab) => {
    if (tab === "edit") { refresh(); }
    else clearTimeout(timer);
  });
  studio.on.items.push(() => renderSong());          // 曲の名前(ギャラリーが読み直されたとき)

  renderProjSelect();
  renderFxInput();
  renderIns();
  renderSong();
})();
