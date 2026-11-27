// 制作(MV タブ・ショート動画タブ共通)の右側の画面と操作: 制作の進み・シーンの行・最終の動画・やり直し・結合し直し・履歴・削除。
// 2 つのタブは同じ台帳の制作(kind = mv / short)を、同じ形の API(/api/mv と /api/short)で操作するので、画面の仕組みは 1 つにして、
// 違い(API の入口・タブ名・メタ行・シーンの行の補足・結合し直しの設定)だけを makeProjectPanel の設定で渡す。
// 入力欄(左側)は各タブの js(mv.js / short.js)が持つ。ここは「作った後」だけ。

// 画角 × 画質の寸法(サーバの SIZES、docs/SPEC.md の表と同じ。実際の寸法はサーバが決める)と、画質の説明。画角 custom は幅・高さの手動入力
// 人物・画像の説明に使ってよい台帳のプロンプト(サーバの mv.description_prompt と同じ規則)。画像タブの edit(参照画像を直したもの)の
// プロンプトは「周囲を広げる」のような直し方の指示で、見た目の説明ではないので使わない("" を返す。説明は「画像から作る」・制作の中で画像を見て作る)
function descPrompt(item) {
  const p = (item && item.params) || {};
  if (p.kind === "h3image.edit" || (Array.isArray(p.references) && p.references.length)) return "";
  return String(p.prompt || "").trim();
}

const VIDEO_SIZES = {
  "16:9": { low: [512, 288], fast: [768, 448], standard: [1024, 576], high: [1344, 768] },
  "9:16": { low: [288, 512], fast: [448, 768], standard: [576, 1024], high: [768, 1344] },
  "1:1": { low: [384, 384], fast: [512, 512], standard: [768, 768], high: [960, 960] },
  "4:5": { low: [416, 512], fast: [512, 640], standard: [640, 800], high: [768, 960] },
  "5:4": { low: [512, 416], fast: [640, 512], standard: [800, 640], high: [960, 768] },
};
const VIDEO_QUALITY_NOTE = { low: t("低 VRAM(6GB 以下)向け。長辺 512 の小さな画"), fast: t("6〜12GB の GPU 向け。いちばん速い"), standard: t("16GB 以上の GPU 向け"), high: t("24GB 以上の GPU 向け。時間がかかります") };
const VIDEO_CUSTOM = { min: 256, max: 1344, step: 32 };
// 画角と画質の欄から寸法を出す(手動入力なら幅・高さの欄。32 の倍数に丸める)
function videoSize(aspect, quality, cw, ch) {
  if (aspect !== "custom") return VIDEO_SIZES[aspect][quality];
  const r = (v) => Math.min(VIDEO_CUSTOM.max, Math.max(VIDEO_CUSTOM.min, Math.round((+v || 0) / VIDEO_CUSTOM.step) * VIDEO_CUSTOM.step));
  return [r(cw), r(ch)];
}

// JSON の API を呼ぶ(失敗は、サーバの日本語の理由を載せた Error。HTTP の状態は .status に入れる)。
// 理由が無い失敗(入力の形の誤り 422 以外。サーバの落ち・中継の HTML など)は、通信の失敗として状態の番号を出す
async function apiJson(url, method = "GET", body) {
  const r = await fetch(url, { method, headers: langHeaders(body ? { "Content-Type": "application/json" } : undefined), body: body ? JSON.stringify(body) : undefined });
  const js = await r.json().catch(() => ({}));
  if (!r.ok) {
    const err = new Error(typeof js.detail === "string" ? js.detail : r.status === 422 ? t("入力を確認してください") : t("通信に失敗しました({status})", { status: r.status }));
    err.status = r.status;
    throw err;
  }
  return js;
}

// 設定(cfg):
//   tab, base(例 "/api/mv"), noun(例 "MV"), root(制作の画面の入れ物の要素), dialog(確認の小窓の ID の頭)
//   placeholder / scenesPlaceholder / historyPlaceholder(空のときの文), outName(保存するファイルの既定の名前), cardLabel(サムネイルが無いときの字)
//   meta(v, parts)       : メタ行の並び(文字列の配列)。parts = { pct, size, scenes, elapsed }。既定は parts の順
//   autoRows(v)          : 「自動で入れた説明」の行 [{label, how, text, note}](無ければ出さない)
//   kindBadge(v, s)      : シーンの種類の札 {text, title}(null なら出さない)。既定は歌唱 / 器楽
//   rowInfo(v, s)        : シーンの行の補足 [{label, text}](あれば行に出す)
//   finalAudio           : true なら、「結合し直す」の横に最終の音声(自動 / 音声なし)の選びを出して、結合し直しに渡す
//   subtitles            : true なら、「結合し直す」の横にテロップ(字幕)の印を出して(セリフの制作だけ)、結合し直しに渡す。最終の動画に SRT があればリンクを出す
//   onOpen()             : タブを開いたとき(制作の一覧を読む前)
// 戻り値: { select(id) } — 作った制作を開く
// MV の音声処理の道具の案内と「道具を入れる」ボタン(MV タブと、曲を使うときのショート動画タブ)。
// prefix: 要素の ID の頭(mv / sh)。need: 道具の案内を出すか。tools: /api/mv/tools の答え。reload(): 道具の様子を読み直す
// (導入中は 2 秒おきに呼ぶ)。ボタンは POST /api/mv/tools/install で、setup と同じ導入を裏で始める。
// 2 行目(<prefix>-tools-ref)は、参照モードに要る H3 のキャッシュがこの PC で無いときの案内(refCacheHint。作るコマンドは文の中。ボタンは無い)。
const toolsPollTimers = {};
const refCacheHint = (tools) => (tools && tools.ref_cache && tools.ref_cache.hint) || "";
function renderToolsBox(prefix, tools, need, fallback, reload) {
  const el = (id) => document.getElementById(prefix + "-tools" + id);
  const box = el(""), btn = el("-install"), prog = el("-progress"), ref = refCacheHint(tools);
  clearTimeout(toolsPollTimers[prefix]);
  if (tools.installing) toolsPollTimers[prefix] = setTimeout(reload, 2000);
  box.hidden = !need && !ref;
  el("-ref").hidden = !ref;
  el("-ref").textContent = ref;
  el("-main").hidden = !need;
  if (!need) return;
  el("-text").textContent = tools.error || tools.hint || fallback;
  btn.hidden = !tools.installable && !tools.installing;
  btn.disabled = !!tools.installing;
  prog.textContent = tools.installing ? t("入れています…") + (tools.progress_text ? " " + tools.progress_text : "") : "";
  if (!btn.dataset.bound) {
    btn.dataset.bound = "1";
    btn.addEventListener("click", async () => {
      btn.disabled = true;
      try { await apiJson("/api/mv/tools/install", "POST"); } catch (e) { el("-text").textContent = e.message; btn.disabled = false; return; }
      reload();
    });
  }
}

function makeProjectPanel(cfg) {
  const STATE_LABEL = { analyzing: t("分析中"), scripting: t("演出を作成中"), generating: t("シーンを生成中"), assembling: t("結合中"), paused: t("一時停止"),
    completed: t("完成"), failed: t("失敗"), cancelled: t("中止") };
  const SCENE_LABEL = { pending: t("待機"), running: t("生成中"), done: t("完了"), failed: t("失敗") };
  const JOB_LABEL = { queued: t("順番待ち"), preparing: t("準備中"), running: t("生成中") };
  const ACTIVE = ["analyzing", "scripting", "generating", "assembling"];
  const POLL_MS = 2000;
  const FINAL_AUDIO = [["auto", t("自動")], ["none", t("音声なし")]];
  const dlg = (id) => $(`${cfg.dialog}-${id}`);

  let projects = [];                       // 制作の一覧(新しい順)
  let current = null;                      // 選んでいる制作の ID
  let view = null;                         // その制作の詳細
  let timer = null;

  const isActive = (p) => ACTIVE.includes(p.state) || !!p.job_id;
  const pct = (p) => Math.round((p || 0) * 100);
  const fmtElapsed = (created) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - created));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  // ---- 制作の一覧・選択・更新 ----

  async function loadList() {
    try { projects = await apiJson(`${cfg.base}?limit=100`); } catch (e) { /* 次の周で再試行 */ }
  }

  // 詳細を読む。待っている間に別の制作を選んだ・もっと新しい読み込み(や操作の答え)があれば、古い答えは捨てる(loadSeq が世代)
  let loadSeq = 0;
  async function loadView() {
    const id = current, my = ++loadSeq;
    if (!id) { view = null; return; }
    try {
      const r = await fetch(`${cfg.base}/${id}`, { headers: langHeaders() });
      const fresh = () => my === loadSeq && id === current;
      if (!fresh()) return;
      if (r.status === 404) { current = null; view = null; }
      else if (r.ok) { const v = await r.json(); if (fresh()) view = v; }
    } catch (e) { /* 一時的な通信失敗は次の周で */ }
  }

  async function refresh() {
    await loadList();
    await loadView();
    if (!current && projects.length) {      // まだ何も選んでいなければ、動いている(無ければ最新の)制作を開く
      current = (projects.find(isActive) || projects[0]).id;
      await loadView();
    }
    renderProject();
    renderCards();
  }

  // 動いている制作がある間は 2 秒おきに読む(このタブを開いているときだけ)
  function schedule() {
    clearTimeout(timer);
    if (studio.tab !== cfg.tab) return;
    if ((view && isActive(view)) || projects.some(isActive)) timer = setTimeout(async () => { await refresh(); schedule(); }, POLL_MS);
  }

  async function select(id) {
    current = id;
    view = null;
    renderProject();
    await refresh();
    schedule();
  }

  async function act(path, method = "POST", body) {
    ui.actionErr.hidden = true;
    try {
      const v = await apiJson(path, method, body);
      loadSeq++;                 // 先に出ていた読み込みの答え(操作の前の状態)で、上書きしない
      if (v && v.id === current) { view = v; renderProject(); }
    } catch (e) {
      ui.actionErr.hidden = false;
      ui.actionErr.textContent = t("できませんでした: {error}", { error: e.message || e });
    }
    await refresh();
    schedule();
  }

  // 確認の小窓(index.html の <dialog>)。outs は消す制作にある完成した動画の数で、あれば「完成した動画も削除する」を選べる。やめたら null、続けるなら { out: 動画も消すか }
  function askDelete(text, outs) {
    const d = dlg("del");
    dlg("del-text").textContent = text;
    dlg("del-out").checked = false;
    dlg("del-out-fld").hidden = !outs;
    dlg("del-out-text").textContent = t("完成した {noun} も削除する({n} 本)", { noun: cfg.noun, n: outs });
    dlg("del-note").textContent = t("チェックしないと、完成した {noun} はギャラリーに残ります", { noun: cfg.noun });
    d.returnValue = "";
    return new Promise((resolve) => {
      d.addEventListener("close", () => resolve(d.returnValue === "ok" ? { out: dlg("del-out").checked } : null), { once: true });      // Esc で閉じても close になる
      d.showModal();
    });
  }

  // その制作の完成した動画の ID。サーバが消す対象と同じで、結合し直す前の版(生成物の params.project_id がその制作のもの)も入る
  const outputsOf = (p) => [...studio.items.filter((i) => i.params && i.params.project_id === p.id).map((i) => i.id), ...(p.output ? [p.output.id] : [])];

  // 制作を消す(1 件でも、まとめてでも)。完成した動画は、選べばいっしょに消す。消したら true、やめたら false
  async function removeProjects(ids) {
    const list = ids.map((id) => projects.find((p) => p.id === id) || { id, title: cfg.noun });
    const what = list.length === 1 ? t("「{title}」の制作を削除しますか?\nシーンのクリップも消えます。", { title: list[0].title }) : t("{n} 件の制作を削除しますか?\nシーンのクリップも消えます。", { n: list.length });
    const ask = await askDelete(what, new Set(list.flatMap(outputsOf)).size);
    if (!ask) return false;
    const gone = [], refused = [], failed = [];
    for (const p of list) {
      try {
        const js = await apiJson(`${cfg.base}/${p.id}${ask.out ? "?with_output=1" : ""}`, "DELETE");
        gone.push(...(js.deleted || []), ...(js.missing || []));
        refused.push(...(js.refused || []));
      } catch (e) { failed.push(`${p.title}: ${e.message || e}`); }
    }
    if (list.some((p) => p.id === current)) { current = null; view = null; }
    await refresh();
    if (gone.length) await forget(gone);
    schedule();
    const notes = [];
    if (failed.length) notes.push(t("{n} 件の制作は削除できませんでした。", { n: failed.length }) + "\n" + failed.join("\n"));
    if (refused.length) notes.push(refusedText(refused, t("使っているものがあるので、完成した {noun} の一部は残しました。", { noun: cfg.noun })));
    if (notes.length) alert(notes.join("\n\n"));
    return true;
  }

  // ---- 右上: いまの制作 ----

  const ui = { id: null, rows: new Map() };      // 組み立て済みの画面の要素(同じ制作は、組み立て直さず中身だけ更新する。動画の再生が途切れないように)

  function button(text, onclick, title) {
    const b = el("button", "", text);
    b.type = "button";
    b.onclick = onclick;
    if (title) b.title = title;
    return b;
  }

  function buildProject(v) {
    const root = cfg.root;
    root.replaceChildren();
    ui.id = v ? v.id : null;
    ui.rows = new Map();
    ui.count = -1;
    if (!v) {
      root.append(el("div", "placeholder", cfg.placeholder));
      return;
    }
    ui.name = el("b", "mvname");
    ui.badge = el("span", "badge");
    ui.msg = el("div", "mvmsg");
    ui.bar = el("div", "pbar mvbar");
    ui.bar.append(el("i"));
    ui.meta = el("div", "mvmeta");
    ui.err = el("div", "mverr");
    ui.err.hidden = true;
    ui.actionErr = el("div", "mverr");
    ui.actionErr.hidden = true;
    ui.auto = el("div", "mvauto");
    ui.auto.hidden = true;
    ui.autoSig = "";
    const base = `${cfg.base}/${v.id}`;
    ui.btns = {
      pause: button(t("一時停止"), () => act(`${base}/pause`), t("走っている仕事は終わるまで続け、順番待ちの仕事は取り下げて止めます")),
      resume: button(t("再開"), () => act(`${base}/resume`), t("できているところから続けます")),
      cancel: button(t("中止"), () => act(`${base}/cancel`), t("走っている仕事を止めます(あとで再開できます)")),
      reassemble: button(t("結合し直す"), () => act(`${base}/reassemble`, "POST", cfg.finalAudio || cfg.subtitles
        ? { ...(cfg.finalAudio ? { final_audio: ui.audio.value } : {}), ...(cfg.subtitles && !ui.subs.hidden ? { subtitles: ui.subs.firstChild.checked } : {}) } : undefined),
        t("いまのクリップをつなぎ直して、新しい {noun} を作ります", { noun: cfg.noun })),
      remove: button(t("削除"), () => removeProjects([v.id]), t("この制作とクリップを消します(完成した {noun} も消すかは、確認で選べます)", { noun: cfg.noun })),
    };
    const head = el("div", "mvhead");
    const title = el("div", "mvtitle");
    title.append(ui.name, ui.badge);
    const btns = el("div", "mvbtns");
    if (cfg.finalAudio) {      // 結合し直すときの最終の音声(制作の設定。結合し直すと書き換わる)
      ui.audio = el("select");
      ui.audio.setAttribute("aria-label", t("最終の音声"));
      ui.audio.title = t("最終の音声: 自動 = 読み上げ・曲・H3 の音から決めます / 音声なし = 映像だけの MP4");
      for (const [value, label] of FINAL_AUDIO) { const o = el("option", "", t("音声: {label}", { label })); o.value = value; ui.audio.append(o); }
      ui.audioTouched = false;
      ui.audio.onchange = () => { ui.audioTouched = true; };
    }
    if (cfg.subtitles) {       // 結合し直すときのテロップ(セリフの制作だけ。制作の設定。結合し直すと書き換わる)
      const box = el("input");
      box.type = "checkbox";
      ui.subs = el("label", "chk");
      ui.subs.append(box, t("テロップ"));
      ui.subs.title = t("セリフのテロップ(字幕)を焼き込む");
      ui.subsTouched = false;
      box.onchange = () => { ui.subsTouched = true; };
    }
    const b = ui.btns;
    btns.append(b.pause, b.resume, b.cancel, ...(ui.audio ? [ui.audio] : []), ...(ui.subs ? [ui.subs] : []), b.reassemble, b.remove);
    head.append(title, ui.msg, ui.bar, ui.meta, ui.err, ui.actionErr, ui.auto, btns);

    ui.final = el("div", "mvfinal");
    ui.final.hidden = true;
    ui.video = el("video");
    ui.video.controls = true;
    ui.video.preload = "metadata";
    ui.fnote = el("span", "mvnote", t("古い版です。結合し直すと更新されます"));
    ui.fnote.hidden = true;
    ui.finfo = el("span");
    ui.save = el("a", "btn", t("保存"));
    ui.save.download = "";
    ui.srt = el("a", "btn", "SRT");
    ui.srt.title = t("テロップの SRT(投稿サイトの字幕用)");
    ui.srt.hidden = true;
    const fbar = el("div", "fbar");
    fbar.append(ui.save, ui.srt, ui.finfo, ui.fnote);
    ui.final.append(ui.video, fbar);

    ui.scenes = el("div", "mvscenes");
    root.append(head, ui.final, ui.scenes);
  }

  function buildRow(v, s) {
    const row = { index: s.index, edit: false };
    row.el = el("div", "mvscene");
    const head = el("div", "mvs-head");
    row.status = el("span", "badge");
    row.kind = el("span", "badge");
    row.time = el("span", "mvs-time");
    row.meta = el("span", "mvs-meta");
    head.append(el("b", "", t("シーン {n}", { n: s.index })), row.time, row.kind, row.status, row.meta);
    const body = el("div", "mvs-body");
    row.video = el("video", "mvs-video");
    row.video.controls = true;
    row.video.preload = "none";
    row.video.hidden = true;
    row.none = el("div", "mvs-none");
    const actions = el("div", "mvs-actions");
    row.retry = button(LANG === "en" ? t("やり直す(シーン)") : "やり直す", () => retry(row, false), t("別の seed で作り直します(全シーンができると、自動で結合し直します)"));      // i18n-skip: 編集の「やり直す」(Redo)と英語を分ける(シーンは Retry)
    row.editBtn = button(t("プロンプトを編集"), () => toggleEdit(row), t("演出(H3 に渡すプロンプト)と seed を直して、そのシーンだけ作り直します"));
    actions.append(row.retry, row.editBtn);
    body.append(row.video, row.none, actions);
    row.takes = el("details", "mvs-takes");
    row.takes.hidden = true;
    const takeHeader = el("summary", "", t("テイクを比較"));
    const takeActions = el("div", "inline");
    row.takeSelect = el("select");
    row.takeSelect.setAttribute("aria-label", t("比較するテイク"));
    row.takeSelect.addEventListener("change", () => updateTakePreview(row));
    row.takeButton = button(t("このテイクを採用"), () => {
      if (!view || !row.takeSelect.value) return;
      act(cfg.base + "/" + view.id + "/scenes/" + row.index + "/takes/" + encodeURIComponent(row.takeSelect.value) + "/select");
    });
    takeActions.append(row.takeSelect, row.takeButton);
    row.takeVideo = el("video", "mvs-video");
    row.takeVideo.controls = true;
    row.takeVideo.preload = "none";
    row.takes.append(takeHeader, takeActions, row.takeVideo);
    row.editor = el("div", "mvs-edit");
    row.editor.hidden = true;
    row.ta = el("textarea");
    row.ta.maxLength = 8000;
    row.seed = el("input");
    row.seed.type = "number";
    row.seed.min = "0";
    row.seed.placeholder = "seed";
    row.seed.setAttribute("aria-label", "seed");
    const inline = el("div", "inline");
    inline.append(row.seed, button(t("この内容でやり直す"), () => retry(row, true)), button(t("閉じる"), () => toggleEdit(row)));
    row.editor.append(row.ta, inline);
    row.err = el("div", "mvs-err");
    row.err.hidden = true;
    row.el.append(head);
    if (cfg.rowInfo) {      // 場所・セリフなど、種類ごとの補足
      row.info = el("div", "mvs-info");
      row.info.hidden = true;
      row.infoSig = "";
      row.el.append(row.info);
    }
    row.el.append(body, row.takes, row.editor, row.err);
    return row;
  }

  function toggleEdit(row) {
    row.edit = !row.edit;
    row.editor.hidden = !row.edit;
    const s = view && view.scenes.find((x) => x.index === row.index);
    if (row.edit && s) { row.ta.value = s.prompt || ""; row.seed.value = s.seed == null ? "" : s.seed; }
  }

  function retry(row, edited) {
    const s = view && view.scenes.find((x) => x.index === row.index);
    if (!s) return;
    let body;
    if (edited) {      // 書き換えた分だけ送る(プロンプトが同じなら送らない)
      const prompt = row.ta.value.trim();
      body = { prompt: prompt !== (s.prompt || "") ? prompt : null, seed: row.seed.value === "" ? null : parseInt(row.seed.value, 10) };
      row.edit = false;
      row.editor.hidden = true;
    }
    act(`${cfg.base}/${view.id}/scenes/${row.index}/retry`, "POST", body);
  }

  const defaultKind = (v, s) => ({ text: s.kind === "vocal" ? t("歌唱") : t("器楽"), title: t("歌っている割合 {pct}%", { pct: Math.round((s.vocal_ratio || 0) * 100) }) });

  function updateTakePreview(row) {
    const take = row.takeChoices.find((item) => item.id === row.takeSelect.value);
    row.takeButton.disabled = !take || !row.canSelectTake;
    row.takeVideo.hidden = !take;
    if (!take && row.takeVideo.dataset.src) {
      row.takeVideo.removeAttribute("src");
      row.takeVideo.removeAttribute("poster");
      row.takeVideo.load();
      delete row.takeVideo.dataset.src;
    }
    if (take && row.takeVideo.dataset.src !== take.clip_url) {
      row.takeVideo.dataset.src = take.clip_url;
      row.takeVideo.poster = take.thumb_url || "";
      row.takeVideo.src = take.clip_url;
    }
  }

  function updateTakes(row, v, s) {
    const choices = (s.takes || []).map((take, i) => ({ ...take, number: i + 1 }))
      .filter((take) => take.id !== s.active_take_id || s.status !== "done");
    row.takeChoices = choices;
    row.canSelectTake = !v.job_id && v.state !== "assembling";
    row.takes.hidden = !choices.length;
    const sig = JSON.stringify(choices);
    if (sig !== row.takeSig) {
      const selected = row.takeSelect.value;
      row.takeSig = sig;
      row.takeSelect.replaceChildren(...choices.map((take) => {
        const option = el("option", "", t("テイク {n}", { n: take.number }) + (take.seed == null ? "" : " · seed " + take.seed));
        option.value = take.id;
        return option;
      }));
      row.takeSelect.value = choices.some((take) => take.id === selected) ? selected : choices.at(-1)?.id || "";
    }
    updateTakePreview(row);
  }

  function updateRow(row, v, s) {
    row.el.className = "mvscene " + s.status + (v.params.height > v.params.width ? " tall" : "");      // 縦長の動画は、小さい動画も少し高く
    row.time.textContent = t("{start}–{end}({sec} 秒)", { start: fmtDur(s.start), end: fmtDur(s.end), sec: s.duration.toFixed(1) });
    const kind = (cfg.kindBadge || defaultKind)(v, s);
    row.kind.hidden = !kind;
    if (kind) {
      row.kind.className = "badge " + s.kind;
      row.kind.textContent = kind.text;
      row.kind.title = kind.title || "";
    }
    if (row.info) {
      const lines = cfg.rowInfo(v, s) || [];
      const sig = JSON.stringify(lines);
      if (sig !== row.infoSig) {      // 2 秒おきの更新で、変わっていなければ組み立て直さない
        row.infoSig = sig;
        row.info.hidden = !lines.length;
        row.info.replaceChildren(...lines.map((l) => {
          const d = el("div", "mvs-info-row");
          d.append(el("b", "", l.label), l.text);
          return d;
        }));
      }
    }
    row.status.className = "badge " + s.status;
    row.status.textContent = s.status === "running" && s.job_status ? JOB_LABEL[s.job_status] || SCENE_LABEL.running : SCENE_LABEL[s.status] || s.status;
    row.meta.textContent = [s.seed != null ? `seed ${s.seed}` : "", s.elapsed_s ? t("生成 {time}", { time: fmtSec(s.elapsed_s) }) : "", s.peak_vram_gb ? `VRAM ${(+s.peak_vram_gb).toFixed(1)}GB` : ""]
      .filter(Boolean).join(" · ");
    row.err.hidden = !s.error;
    row.err.textContent = s.error || "";
    row.el.style.setProperty("--ar", `${v.params.width} / ${v.params.height}`);       // 小さい動画の縦横比(縦長の動画は縦長に)
    if (s.clip_url) {
      row.video.hidden = false;
      row.none.hidden = true;
      if (row.video.dataset.src !== s.clip_url) { row.video.dataset.src = s.clip_url; row.video.poster = s.thumb_url || ""; row.video.src = s.clip_url; }
    } else {
      row.video.hidden = true;
      row.none.hidden = false;
      row.none.textContent = s.status === "running" ? t("生成中…") : s.status === "failed" ? t("失敗") : s.prompt ? t("未生成") : t("演出待ち");
      if (row.video.dataset.src) { row.video.removeAttribute("src"); row.video.load(); delete row.video.dataset.src; }
    }
    const idle = !!s.prompt && s.status !== "running" && v.state !== "assembling";
    row.retry.disabled = row.editBtn.disabled = !idle;
    updateTakes(row, v, s);
  }

  // 自動で入れた説明を、どれが自動かが分かるように出す。書いた説明は出さない
  function renderAuto(v) {
    const rows = cfg.autoRows ? cfg.autoRows(v) : [];
    const sig = JSON.stringify(rows);
    if (sig === ui.autoSig) return;      // 2 秒おきの更新で、変わっていなければ組み立て直さない
    ui.autoSig = sig;
    ui.auto.hidden = !rows.length;
    ui.auto.replaceChildren(...rows.map((r) => {
      const row = el("div", "mvauto-row");
      const head = el("div");
      head.append(el("b", "", r.label), el("span", "mvauto-how", r.how));
      row.append(head);
      if (r.text) row.append(el("div", "mvauto-text", r.text));
      if (r.note) row.append(el("div", "mvauto-note", r.note));
      return row;
    }));
  }

  function renderProject() {
    const v = view;
    if ((v ? v.id : null) !== ui.id) buildProject(v);
    if (!v) return;
    ui.name.textContent = v.title;
    ui.badge.className = "badge " + v.state;
    ui.badge.textContent = STATE_LABEL[v.state] || v.state;
    const live = ACTIVE.includes(v.state);
    ui.msg.textContent = v.message || "";
    const p = pct(v.progress);
    ui.bar.querySelector("i").style.width = p + "%";
    ui.bar.classList.toggle("err", v.state === "failed");
    ui.bar.classList.toggle("ok", v.state === "completed");
    ui.bar.title = `${p}%`;
    const pr = v.params;
    const parts = { pct: `${p}%`, size: `${pr.width}×${pr.height}(${pr.aspect})`,
      scenes: v.scenes.length ? t("{done} / {total} シーン", { done: v.scenes.filter((s) => s.status === "done").length, total: v.scenes.length }) : "", elapsed: live ? t("経過 {time}", { time: fmtElapsed(v.created_at) }) : "" };
    ui.meta.textContent = (cfg.meta ? cfg.meta(v, parts) : [parts.pct, parts.size, parts.scenes, parts.elapsed]).filter(Boolean).join("  ");
    renderAuto(v);
    if (v.error) { ui.err.hidden = false; ui.err.textContent = t("失敗: {error}", { error: v.error }); }
    else if (ui.err.textContent.startsWith(t("失敗: {error}", { error: "" }))) { ui.err.hidden = true; ui.err.textContent = ""; }
    const done = v.scenes.length > 0 && v.scenes.every((s) => s.status === "done");
    const b = ui.btns;
    b.pause.hidden = !live;
    b.resume.hidden = !["paused", "failed", "cancelled"].includes(v.state);
    b.cancel.hidden = !(live || v.state === "paused");
    b.reassemble.hidden = !(done && !v.job_id && ["completed", "paused", "failed", "cancelled"].includes(v.state));
    if (ui.audio) {
      ui.audio.hidden = b.reassemble.hidden;
      if (!ui.audioTouched) ui.audio.value = pr.final_audio === "none" ? "none" : "auto";      // 触るまでは、この制作の設定に合わせる
    }
    if (ui.subs) {
      ui.subs.hidden = b.reassemble.hidden || pr.timeline !== "speech";
      if (!ui.subsTouched) ui.subs.firstChild.checked = !!pr.subtitles;
    }
    // 最終の動画
    ui.final.hidden = !v.output;
    if (v.output) {
      if (ui.video.dataset.src !== v.output.url) {
        ui.video.dataset.src = v.output.url;
        ui.video.poster = v.output.thumb_url || "";
        ui.video.src = v.output.url;
      }
      ui.save.href = v.output.url;
      ui.save.download = v.output.filename || cfg.outName;
      const srt = v.output.params && v.output.params.srt;
      ui.srt.hidden = !srt;
      if (srt) { ui.srt.href = v.output.url.replace(/[^/]+$/, srt); ui.srt.download = srt; }
      ui.finfo.textContent = [v.output.width ? `${v.output.width}×${v.output.height}` : "", v.output.duration_s ? fmtSec(v.output.duration_s) : ""].filter(Boolean).join("  ");
      ui.fnote.hidden = v.assembled;
    }
    // シーン
    if (ui.count !== v.scenes.length) {      // シーンの数が変わったときだけ、行を組み立て直す
      ui.count = v.scenes.length;
      ui.rows = new Map(v.scenes.map((s) => [s.index, buildRow(v, s)]));
      ui.scenes.replaceChildren(...(v.scenes.length ? [...ui.rows.values()].map((r) => r.el) : [el("div", "placeholder", cfg.scenesPlaceholder)]));
    }
    for (const s of v.scenes) { const row = ui.rows.get(s.index); if (row) updateRow(row, v, s); }
  }

  // ---- 右下: 制作の履歴 ----

  function projectNode(p) {
    const card = el("div", "thumb mvcard");
    card.title = `${p.title}\n${p.message || ""}`;
    keyClickable(card, p.title);      // キーボードでも開ける(web/gallery.js)
    if (p.thumb_url) {
      const img = el("img");
      img.src = p.thumb_url;
      img.alt = p.title;
      img.loading = "lazy";
      img.draggable = false;
      card.append(img);
    } else {
      card.append(el("div", "nothumb video", cfg.cardLabel));
    }
    card.append(el("span", "tbadge top", p.title));
    card.append(el("span", "tbadge state", `${STATE_LABEL[p.state] || p.state}${ACTIVE.includes(p.state) ? ` ${pct(p.progress)}%` : ""}`));
    card.classList.toggle("sel", !studio.select.on && p.id === current);
    if (studio.select.on) { pickable(card, p.id); return card; }      // 選択モード: クリックは選び直し(×もドラッグも無し)
    card.onclick = () => select(p.id);
    const x = el("button", "tdel", "×");
    x.title = t("削除");
    x.onclick = (e) => { e.stopPropagation(); removeProjects([p.id]); };
    card.append(x);
    if (p.output) makeDraggable(card, p.output.id);      // 完成した動画は、ふつうの動画として他のタブへ落とせる
    return card;
  }

  // 一覧が変わっていなければ描き直さない(2 秒おきの更新で、ドラッグ中のカードを作り直さないように)。タブの切り替えなどでは force で描く
  let cardsSig = "";
  function renderCards(force = false) {
    const grid = $("hist-grid");
    if (studio.tab !== cfg.tab) return;
    const sig = JSON.stringify([current, projects.map((p) => [p.id, p.state, pct(p.progress), p.title, p.thumb_url, p.output && p.output.id])]);
    if (!force && sig === cardsSig) return;
    cardsSig = sig;
    grid.replaceChildren();
    if (!projects.length) grid.append(el("div", "placeholder", cfg.historyPlaceholder));
    for (const p of projects) grid.append(projectNode(p));
    renderSelectBar();       // 選んでいた制作が消えていたら、数を合わせる
  }

  // 生成物ビューアと履歴を、この画面で自前に描く(gallery.js の studio.own)。ギャラリーで動画を選んだら、それを作った制作を開く
  studio.own[cfg.tab] = {
    history: () => renderCards(true),
    open: (item) => {
      const pid = item.params && item.params.project_id;
      if (pid && projects.some((p) => p.id === pid)) select(pid);
    },
    ids: () => projects.map((p) => p.id),
    remove: removeProjects,
  };

  studio.on.tab.push((tab) => {
    if (tab === cfg.tab) { if (cfg.onOpen) cfg.onOpen(); refresh().then(schedule); }
    else clearTimeout(timer);
  });

  buildProject(null);
  return { select };
}
