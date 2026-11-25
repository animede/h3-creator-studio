// ショート動画タブ: 物語・曲・セリフ・参照画像から、物語性のある短い動画を作る(H3 だけ。MV タブの仕組みが土台)。
// 左 = 入力、右上 = いまの制作(進み・最終の動画・シーン)、右下 = 制作の履歴。ジョブは管理役の Director がつなぐので、ここは /api/short を見て操作するだけ。
// 右側の画面(制作の進み・シーンの行・最終の動画・やり直し・結合し直し・履歴・削除)は MV タブと共通の仕組み(web/project.js の makeProjectPanel)。
// 時間軸は入力で決まる: セリフあり → 読み上げ(人物が口を合わせる。曲は BGM)/ 曲だけ → 曲 / どちらも無し → 指定の長さ。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const SIZES = VIDEO_SIZES, QUALITY_NOTE = VIDEO_QUALITY_NOTE;      // 画角 × 画質の寸法と、画質の説明(web/project.js。MV タブと共通)
  const CHAR_MAX = 600, IMAGES_MAX = 4;
  const LENGTH_MIN = 30, LENGTH_MAX = 3600, WARN_S = 300;      // 曲もセリフも無いときの長さの範囲(秒。サーバと同じ)と、警告を出す長さ(5 分)
  const SCENE_S = 7.5, SCENE_MIN_A4000 = 3.5;           // 警告の見積り: シーンの平均の長さ(秒)と、A4000 の高速で 1 シーンにかかる時間(分)
  const SPEECH_CHARS_PER_S = 5.5;                       // 読み上げの長さの目安(1 秒あたりの文字数。実際の長さは、作り始めて読み上げを作ってから決まる)
  const ROLE_LABEL = { person: t("人物"), scenery: t("景色・物") };
  const TIMELINE_LABEL = { speech: t("セリフ"), music: t("曲"), free: t("指定の長さ") };
  // 制作に出す「自動で入れた人物の説明」の出どころ(サーバの character_source。書いた説明 typed は出さない)
  const AUTO_HOW = { vision: t("画像を見て自動で作りました"), image: t("画像のプロンプトから入れました"),
    none: t("画像を読めなかったので、空のままです(演出は、画像の人物としか書きません)") };

  const sh = (id) => $("sh-" + id);
  let song = null;                         // 曲(生成物のレコード)
  let images = [];                         // 参照画像: [{ item(生成物のレコード), role("person" | "scenery") }]
  let tools = { available: true, hint: null };
  let creating = false, qualityTouched = false, charFor = null;      // charFor: 人物の説明を入れた(自動で入れた)画像の ID
  const api = apiJson;      // JSON の API(web/project.js)

  function showMsg(text) {
    sh("msg").hidden = !text;
    sh("msg").textContent = text || "";
  }

  const person = () => images.find((r) => r.role === "person") || null;
  const timeline = () => (sh("narr").value.trim() ? "speech" : song ? "music" : "free");

  // ---- 入力: 空(または自動で入れた文のまま)の欄にだけ、ドロップした生成物の中身を入れる(自分で書いた文は消さない) ----

  function fill(ta, text) {
    if (ta.value.trim() && ta.value !== ta.dataset.auto) return;
    ta.value = ta.dataset.auto = text || "";
  }

  // 自動で作った文を欄に入れる(自動で入れた文の印を付ける)。書いた文があれば、置き換えてよいか確かめる。入れたら true
  function putAuto(ta, text) {
    if (ta.value.trim() && ta.value !== ta.dataset.auto && !confirm(t("書いた内容を、自動で作った文に置き換えますか?"))) return false;
    ta.value = ta.dataset.auto = text;
    changed();
    return true;
  }

  const busy = { char: false, narr: false };
  const hasPrompt = (item) => !!(item && item.params && String(item.params.prompt || "").trim());      // 画像タブで作った画像か(台帳に画像のプロンプトがある)

  function setNote(which, text) {
    sh(which + "-note").hidden = !text;
    sh(which + "-note").textContent = text || "";
  }

  // 通信中の表示(くるくる・文言・経過時間)。戻り値は、終わらせる関数
  function startBusy(which, label) {
    const t0 = Date.now();
    const draw = () => {
      const sec = Math.round((Date.now() - t0) / 1000);
      sh(which + "-busy-text").textContent = t("{label}(経過 {time})", { label: label(), time: `${Math.floor(sec / 60)}:${String(sec % 60).padStart(2, "0")}` });
    };
    busy[which] = true;
    sh(which + "-busy").hidden = false;
    draw();
    const timer = setInterval(draw, 1000);
    updateAuto();
    return () => { clearInterval(timer); busy[which] = false; sh(which + "-busy").hidden = true; updateAuto(); };
  }

  // ボタン(材料があって、通信中でないときだけ)と、「空のまま作ると、自動で入れます」の案内
  function updateAuto() {
    const p = person();
    sh("char-go").disabled = !p || busy.char;
    sh("narr-go").disabled = !sh("story").value.trim() || busy.narr;
    sh("char-hint").hidden = !(p && !hasPrompt(p.item) && !sh("char").value.trim());
  }

  // ---- 入力: 曲 ----

  function renderSong() {
    const body = sh("song").querySelector(".frame-body");
    body.replaceChildren();
    if (!song) { body.append(el("div", "refs-empty", t("ここに曲をドロップ"))); return; }
    const c = el("div", "songitem");
    const text = el("div", "stext");
    const info = el("div", "sinfo", [song.duration_s ? fmtDur(song.duration_s) : "", song.params && song.params.genre, song.filename].filter(Boolean).join("  "));
    info.title = info.textContent;      // ジャンルが自由に書いた長い文のことがある(1 行に収め、全文は吹き出しに)
    text.append(el("div", "stitle", (song.params && song.params.theme) || song.filename || t("曲")), info);
    const a = el("audio");
    a.controls = true;
    a.src = song.url;
    text.append(a);
    const x = el("button", "rdel", "×");
    x.title = t("外す");
    x.onclick = clearSong;
    c.append(el("div", "aicon", "🎵"), text, x);
    makeDraggable(c, song.id);
    body.append(c);
  }

  function setSong(item) {
    if (item.kind !== "audio") { showMsg(t("曲には音声を落としてください(音楽タブの曲、または音声ファイル)")); return false; }
    song = item;
    showMsg("");
    renderSong();
    changed();
    return true;
  }

  function clearSong() {
    song = null;
    renderSong();
    changed();
  }

  // ---- 入力: 参照画像(0〜4 枚。1 枚ごとに役割: 人物 / 景色・物) ----

  function renderImages() {
    const list = sh("images-list");
    list.replaceChildren();
    sh("images-empty").hidden = images.length > 0;
    for (const r of images) {
      const row = el("div", "shimg");
      const img = el("img");
      img.src = r.item.thumb_url || r.item.url;
      img.title = r.item.filename || "";
      img.draggable = false;
      const name = el("div", "shname", r.item.filename || t("画像"));
      name.title = String((r.item.params && r.item.params.prompt) || r.item.filename || "");
      const role = el("select");
      role.setAttribute("aria-label", t("役割"));
      for (const [value, label] of Object.entries(ROLE_LABEL)) {
        const o = el("option", "", label);
        o.value = value;
        o.disabled = value === "person" && images.some((x) => x !== r && x.role === "person");      // 人物は 1 枚まで
        role.append(o);
      }
      role.value = r.role;
      role.onchange = () => { r.role = role.value; renderImages(); changed(); };
      const x = el("button", "rdel", "×");
      x.type = "button";
      x.title = t("外す");
      x.onclick = () => { images = images.filter((v) => v !== r); renderImages(); changed(); };      // 画像を見ている最中なら、その答えは使わない(describeImage が見分ける)
      row.append(img, name, role, x);
      list.append(row);
    }
  }

  // 人物の画像が変わったら、人物の説明の欄を出し入れして、画像のプロンプトを(空なら)入れる
  function syncPerson() {
    const p = person();
    sh("char-fld").hidden = !p;
    const id = p ? p.item.id : null;
    if (id === charFor) return;
    charFor = id;
    setNote("char", "");
    fill(sh("char"), p ? String((p.item.params && p.item.params.prompt) || "").trim().slice(0, CHAR_MAX) : "");
  }

  // role は、サンプルなど役割が決まっているときだけ渡す(人物は 1 枚までなので、2 枚目の人物は景色・物にする)
  function addImage(item, role) {
    if (item.kind !== "image") { showMsg(t("参照画像には画像を落としてください")); return false; }
    if (images.some((r) => r.item.id === item.id)) { showMsg(t("その画像はもう入っています")); return false; }
    if (images.length >= IMAGES_MAX) { showMsg(t("参照画像は {n} 枚までです", { n: IMAGES_MAX })); return false; }
    images.push({ item, role: (role || "person") === "person" && !person() ? "person" : "scenery" });      // 最初の 1 枚は人物、2 枚目からは景色・物(役割は選び直せる)
    showMsg("");
    renderImages();
    changed();
    return true;
  }

  // PC のファイルは、取り込んでからギャラリーの生成物として入れる(種類が合わないものはギャラリーに残すだけ)
  async function upload(files, box, kind, set) {
    try {
      const recs = (await uploadFiles(files, box)).filter((r) => r.kind === kind);
      if (!recs.length) showMsg(kind === "audio" ? t("音声のファイルではありません(ギャラリーには保存しました)") : t("画像のファイルではありません(ギャラリーには保存しました)"));
      for (const rec of recs) if (!set(rec)) break;
    } catch (e) { showMsg(t("取り込めませんでした: {error}", { error: e.message || e })); }
  }

  const active = () => studio.tab === "short";
  wireDrop(sh("song"), { zone: sh("song-fld"), active, msg: showMsg, onItem: setSong, onFiles: (files) => upload(files, sh("song"), "audio", setSong) });
  wireDrop(sh("images"), { active, msg: showMsg, onItem: addImage, onFiles: (files) => upload(files, sh("images"), "image", addImage) });

  // 消された曲・画像は入力欄から外す(消したとき(studio-deleted)と、一覧を読み直したらギャラリーに無かったとき)
  function dropGone(gone) {
    if (song && gone(song.id)) clearSong();
    if (images.some((r) => gone(r.item.id))) { images = images.filter((r) => !gone(r.item.id)); renderImages(); changed(); }
  }
  studio.on.items.push(() => dropGone(goneFromGallery));      // 一覧が全部のときだけ(web/gallery.js の goneFromGallery)
  document.addEventListener("studio-deleted", (e) => dropGone((id) => e.detail.ids.includes(id)));

  // ---- 入力: 自動の説明(画像から人物の説明、物語からナレーション) ----

  async function describeImage() {
    const p = person();
    if (!p || busy.char) return;
    const target = p.item;
    showMsg("");
    setNote("char", "");
    const stop = startBusy("char", () => t("画像を見ています…"));
    try {
      const js = await api("/api/mv/describe/image", "POST", { image_id: target.id });
      const now = person();
      if (!now || now.item !== target) return;       // 待っている間に、人物の画像が入れ替わった
      if (js.text && !putAuto(sh("char"), js.text)) return;
      setNote("char", js.source === "vision" ? t("画像を見て作りました。確かめて、直してから作れます") : js.note);
    } catch (e) {
      showMsg(t("人物の説明を作れませんでした: {error}", { error: e.message || e }));
    } finally {
      stop();
    }
  }
  sh("char-go").onclick = describeImage;

  async function describeNarration() {
    const story = sh("story").value.trim();
    if (!story || busy.narr) return;
    showMsg("");
    setNote("narr", "");
    const stop = startBusy("narr", () => t("ナレーションを考えています…"));
    try {
      const js = await api("/api/short/describe/narration", "POST", { story, song_id: song ? song.id : "" });
      if (!putAuto(sh("narr"), js.text)) return;
      setNote("narr", t("物語から作りました。確かめて、直してから作れます"));
    } catch (e) {
      showMsg(t("ナレーションを作れませんでした: {error}", { error: e.message || e }));
    } finally {
      stop();
    }
  }
  sh("narr-go").onclick = describeNarration;

  // ---- 入力: 話者(AivisSpeech。動画タブの TTS と同じ一覧。セリフを入れたときに読み込む) ----

  let speakers = null, speakersLoading = false, speakersFailed = false, pendingSpeaker = null;

  function setSpeakerNote(text) {
    sh("speaker-note").hidden = !text;
    sh("speaker-note").textContent = text || "";
  }

  async function loadSpeakers() {
    if (speakers || speakersLoading) return;
    speakersLoading = true;
    speakersFailed = false;
    setSpeakerNote(t("話者を読み込んでいます(AivisSpeech が止まっていれば起動します)…"));
    try {
      const r = await fetch("/api/tts/speakers", { headers: langHeaders() });
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const list = await r.json();
      if (!list.length) throw new Error(t("話者が見つかりません"));
      const sel = sh("speaker");
      const keep = sel.value;
      sel.replaceChildren(...list.map((s) => { const o = el("option", "", s.label); o.value = s.style_id; return o; }));
      speakers = list;
      let saved = null;
      try { saved = localStorage.getItem("tts-style"); } catch (e) { /* 保存できなくても動く */ }      // 動画タブの TTS と同じ選びを引き継ぐ
      const want = [pendingSpeaker, keep, saved].find((v) => v != null && v !== "" && list.some((s) => String(s.style_id) === String(v)));
      if (want != null) sel.value = String(want);
      setSpeakerNote(pendingSpeaker != null && String(sel.value) !== String(pendingSpeaker) ? t("プリセットの話者は、この環境にありません") : "");
      pendingSpeaker = null;
    } catch (e) {
      speakersFailed = true;
      setSpeakerNote(t("話者を取得できません: {error}(話者の欄を押すと読み直します)", { error: e.message || e }));
    } finally {
      speakersLoading = false;
    }
  }
  sh("speaker").addEventListener("focus", () => { speakersFailed = false; loadSpeakers(); });
  sh("speaker").addEventListener("mousedown", () => { if (!speakers) { speakersFailed = false; loadSpeakers(); } });
  sh("speaker").onchange = () => { try { localStorage.setItem("tts-style", sh("speaker").value); } catch (e) { /* 保存できなくても動く */ } };

  // ---- 入力: 長さ(時間軸)・警告 ----

  // 長さの見込み(秒): セリフ → 読み上げ(文字数からの目安)/ 曲 → 曲の長さ / どちらも無し → 指定の長さ
  function expectedSeconds() {
    const tl = timeline();
    if (tl === "free") { const n = +sh("length").value; return n > 0 ? n : null; }
    if (tl === "music") return song.duration_s || null;
    return Math.round(sh("narr").value.replace(/\s/g, "").length / SPEECH_CHARS_PER_S);
  }

  // 入力が変わったときに追従するエンジン: 物語の必須の案内・曲の BGM の案内・長さの欄と案内・5 分超の警告・ボタン
  function changed() {
    const tl = timeline();
    syncPerson();
    sh("story-req").textContent = tl === "free" ? t("(必須: 曲もセリフも無いので、これが元になります)") : t("(任意)");
    sh("bgm-note").hidden = !(tl === "speech" && song);
    sh("length-fld").hidden = tl !== "free";
    const sec = expectedSeconds();
    const note = sh("len-note");
    note.hidden = tl === "free";
    if (tl === "music") note.textContent = sec ? t("長さ: 曲の長さ({dur})", { dur: fmtDur(sec) }) : t("長さ: 曲の長さ");
    else if (tl === "speech") note.textContent = t("長さ: 読み上げの長さ(目安 約 {dur}。作り始めて読み上げを作ると決まります)", { dur: fmtDur(sec || 0) }) + (song ? t("。曲は BGM になります") : "");
    const warn = sh("len-warn");
    warn.hidden = !(sec && sec > WARN_S);
    if (!warn.hidden) {
      const hours = Math.round((sec / SCENE_S) * SCENE_MIN_A4000 / 6) / 10;      // シーン数 × 3.5 分 を時間に(小数 1 位)
      warn.textContent = (tl === "speech" ? t("読み上げは目安で約 {min} 分です。", { min: Math.round(sec / 60) }) : "") + t("5 分を超えます。A4000 の高速で 1 シーン約 3.5 分 → 約 {hours} 時間", { hours });
    }
    updateAuto();
    updateGo();
    if (sh("narr").value.trim() && !speakers && !speakersLoading && !speakersFailed) loadSpeakers();      // セリフを入れたら話者の一覧を読む
  }
  sh("story").addEventListener("input", changed);
  sh("narr").addEventListener("input", changed);
  sh("length").addEventListener("input", changed);
  sh("char").addEventListener("input", updateAuto);

  // ---- 入力: 画角・画質・シード ----

  const customSize = () => sh("aspect").value === "custom";
  function renderSize() {
    sh("custom").hidden = !customSize();
    const [w, h] = videoSize(sh("aspect").value, sh("quality").value, sh("cw").value, sh("ch").value);
    sh("size").textContent = `(${w}×${h})`;
    sh("qnote").textContent = customSize() ? t("幅・高さを直接指定(256〜1344、32 の倍数に丸めます)。画質の欄は使いません") : QUALITY_NOTE[sh("quality").value];
  }
  sh("aspect").onchange = renderSize;
  sh("cw").oninput = sh("ch").oninput = renderSize;
  sh("quality").onchange = () => { qualityTouched = true; renderSize(); };
  sh("random").onchange = () => { sh("seed").disabled = sh("random").checked; };

  // 既定の画質: VRAM が 16GiB 未満なら高速、それ以上なら標準(利用者が選んだ後は触らない)
  async function pickQuality() {
    try {
      const s = await fetch("/api/status", { headers: langHeaders() }).then((r) => r.json());
      if (!qualityTouched) sh("quality").value = s.gpu && s.gpu.total_mib >= 16 * 1024 ? "standard" : "fast";
    } catch (e) { /* GPU の様子が分からなければ高速のまま */ }
    renderSize();
  }

  // 曲だけの動画は MV と同じ分析(Demucs と librosa)を使う。その道具が無いときだけ、作れない
  function updateGo() {
    const need = timeline() === "music" && !tools.available;
    sh("go").disabled = creating || need;
    $("sh-tools").hidden = !need;
    $("sh-tools").textContent = need ? (tools.hint || t("曲の分析に必要な道具が見つかりません")) : "";
  }

  async function loadTools() {
    try { tools = await api("/api/mv/tools"); } catch (e) { return; }
    updateGo();
  }

  // ---- 作る ----

  async function generate() {
    const tl = timeline();
    const story = sh("story").value.trim(), narration = sh("narr").value.trim();
    if (!story && !song && !narration) { showMsg(t("物語・映像のイメージを書いてください(曲もセリフも無いときは必須です)")); return; }
    if (narration && !person()) { showMsg(t("セリフがあるときは、口を合わせる人物の画像を入れて、役割を「人物」にしてください")); return; }
    if (narration && !speakers) { speakersFailed = false; loadSpeakers(); showMsg(t("話者の一覧を読めていません。読み込んでから、話者を選んでください")); return; }      // 読めていなければ読み直す
    if (narration && !sh("speaker").value) { showMsg(t("セリフを読み上げる話者を選んでください")); return; }
    const min = +sh("min").value, max = +sh("max").value;
    if (!(min >= 5 && max <= 10 && min <= max)) { showMsg(t("シーンの長さは 5〜10 秒で、最短は最長以下にしてください")); return; }
    const length = +sh("length").value;
    if (tl === "free" && !(length >= LENGTH_MIN && length <= LENGTH_MAX)) { showMsg(t("長さは {min}〜{max} 秒で指定してください", { min: LENGTH_MIN, max: LENGTH_MAX })); return; }
    if (tl === "music" && !tools.available) { showMsg(tools.hint || t("曲の分析に必要な道具が見つかりません")); return; }
    const seed = sh("random").checked || sh("seed").value === "" ? null : parseInt(sh("seed").value, 10);
    showMsg("");
    creating = true;
    updateGo();
    try {
      const js = await api("/api/short", "POST", { story, song_id: song ? song.id : "", narration, speaker_id: narration ? +sh("speaker").value : null,
        images: images.map((r) => ({ id: r.item.id, role: r.role, description: "" })), length_s: tl === "free" ? length : null,
        character: person() ? sh("char").value.trim() : "", aspect: sh("aspect").value, quality: sh("quality").value,
        width: customSize() ? +sh("cw").value : null, height: customSize() ? +sh("ch").value : null, min_s: min, max_s: max, seed,
        background_text: sh("bgtext").value, final_audio: sh("final").value, subtitles: sh("subs").checked });
      await select(js.id);
    } catch (e) {
      showMsg(t("ショート動画を始められませんでした: {error}", { error: e.message || e }));
    } finally {
      creating = false;
      updateGo();
    }
  }
  sh("go").onclick = generate;

  // ---- プリセット(入力欄の値だけ。曲・参照画像・物語・セリフ・人物の説明は含めない。話者は AivisSpeech のスタイル ID) ----

  function getPresetValues() {
    return { aspect: sh("aspect").value, quality: sh("quality").value, ...(customSize() ? { width: +sh("cw").value, height: +sh("ch").value } : {}),
      min_s: +sh("min").value, max_s: +sh("max").value,
      seed: presetGetSeed(sh("seed"), sh("random")), background_text: sh("bgtext").value, final_audio: sh("final").value,
      length_s: +sh("length").value >= LENGTH_MIN && +sh("length").value <= LENGTH_MAX ? +sh("length").value : 60, speaker_id: sh("speaker").value === "" ? null : +sh("speaker").value,
      subtitles: sh("subs").checked };
  }

  // 書かれている項目だけ反映する(知らない項目は無視)。選べない値は入れず、そのままにした項目として知らせる
  function applyPresetValues(v) {
    const skipped = [];
    for (const [key, id, label] of [["aspect", "aspect", t("画角")], ["quality", "quality", t("画質")], ["final_audio", "final", t("最終の音声")]]) {
      if (key in v && !presetSet(sh(id), v[key])) skipped.push(label);      // change で寸法の表示が合う
    }
    for (const [key, id] of [["width", "cw"], ["height", "ch"]]) if (Number.isFinite(v[key])) presetSet(sh(id), v[key]);
    if ("background_text" in v && !presetSet(sh("bgtext"), v.background_text)) skipped.push(t("背景の文字"));
    if (typeof v.subtitles === "boolean") presetSet(sh("subs"), v.subtitles);
    for (const [key, id] of [["min_s", "min"], ["max_s", "max"]]) if (Number.isFinite(v[key])) presetSet(sh(id), v[key]);
    if (Number.isFinite(v.length_s) && v.length_s >= LENGTH_MIN && v.length_s <= LENGTH_MAX) presetSet(sh("length"), v.length_s);      // input で警告が合う
    if ("seed" in v) presetSetSeed(sh("seed"), sh("random"), v.seed);
    if (Number.isInteger(v.speaker_id)) {
      if (speakers) { if (!presetSet(sh("speaker"), v.speaker_id)) skipped.push(t("話者")); }
      else { pendingSpeaker = v.speaker_id; speakersFailed = false; loadSpeakers(); }      // 一覧を読んだら、その話者を選ぶ
    }
    return skipped;
  }
  wirePresets({ tab: "short", box: sh("presets"), get: getPresetValues, apply: applyPresetValues });

  // ---- サンプル(samples/<id>/ の入力欄の値と素材。読み込むと、いまの曲・参照画像・物語・セリフ・人物の説明は、サンプルのものに置き換わる。生成はしない) ----

  // values = サンプルの入力欄の値(プリセットと同じ項目 + story / narration / character)。assets = [{role: song | person | scenery, output(ギャラリーの生成物)}]
  function applySample(values, assets) {
    song = null;                              // 前の入力を引きずらない(サンプルの条件で作れるように)
    images = [];
    charFor = null;
    for (const [id, key] of [["story", "story"], ["narr", "narration"], ["char", "character"]]) {
      sh(id).dataset.auto = "";               // 自動で入れた文の印は外す(サンプルの文は、書いた文と同じに扱う)
      presetSet(sh(id), typeof values[key] === "string" ? values[key] : "");      // input で、時間軸・長さ・ボタンが追従する
    }
    const skipped = applyPresetValues(values);
    for (const a of assets) {
      if (a.role === "song") setSong(a.output);
      else addImage(a.output, a.role);        // IMAGES_MAX を超える分は、addImage が断る
    }
    if (typeof values.character === "string" && values.character) sh("char").value = values.character;      // 人物の画像のプロンプトより、サンプルの説明を優先する
    renderSong();
    renderImages();
    changed();
    showMsg(t("サンプルを読み込みました。「ショート動画を作る」で同じ条件で作れます") + (skipped.length ? t("(選べない値だったので、そのままにした項目: {items})", { items: skipped.join(t("、")) }) : ""));
  }
  wireSamples({ tab: "short", box: sh("samples"), apply: applySample });

  // ---- 右側: 制作の進み・シーン・最終の動画・履歴(MV タブと共通。web/project.js) ----

  function autoRows(v) {
    const src = (v.sources || {}).character, note = (v.source_notes || {}).character, how = AUTO_HOW[src] || "";
    return how || note ? [{ label: t("人物の説明"), how, text: how && src !== "none" ? v.params.character || "" : "", note: note || "" }] : [];
  }

  const panel = makeProjectPanel({
    tab: "short", base: "/api/short", noun: t("ショート動画"), root: $("short-project"), dialog: "proj", cardLabel: t("ショート"), outName: "short.mp4", finalAudio: true,
    subtitles: true,
    placeholder: t("ショート動画を作ると、ここに進み具合とシーンが並びます"), scenesPlaceholder: t("音声の準備(シーン割り)が終わると、シーンがここに並びます"),
    historyPlaceholder: t("作ったショート動画(制作)がここに並びます"),
    meta: (v, m) => {
      const a = v.analysis, pr = v.params;
      const dur = a && a.duration ? ` ${fmtDur(a.duration)}` : pr.timeline === "free" && pr.length_s ? ` ${fmtDur(pr.length_s)}` : "";
      return [m.pct, m.size, pr.timeline ? t("時間軸: {timeline}", { timeline: (TIMELINE_LABEL[pr.timeline] || pr.timeline) + dur }) + (pr.timeline === "speech" && pr.song_id ? t(" + 曲(BGM)") : "") : "", m.scenes,
        t("最終の音声: {mode}", { mode: pr.final_audio === "none" ? t("音声なし") : t("自動") }), pr.timeline === "speech" && pr.subtitles ? t("テロップあり") : "", m.elapsed];
    },
    autoRows,
    kindBadge: (v, s) => (v.params.timeline === "free" ? null : v.params.timeline === "speech" ? { text: t("セリフ"), title: t("読み上げに口を合わせます") }
      : { text: s.kind === "vocal" ? t("歌唱") : t("器楽"), title: t("歌っている割合 {pct}%", { pct: Math.round((s.vocal_ratio || 0) * 100) }) }),
    rowInfo: (v, s) => {
      const imgs = v.params.images || [];
      const refs = (s.refs || []).filter((i) => imgs[i]).map((i) => `${i + 1}: ${ROLE_LABEL[imgs[i].role] || imgs[i].role}`);
      return [s.place ? { label: t("場所"), text: s.place } : null, s.speech && s.text ? { label: t("セリフ"), text: s.text } : !s.speech && s.summary ? { label: t("場面"), text: s.summary } : null,
        refs.length ? { label: t("参照"), text: refs.join(t("、")) } : null].filter(Boolean);
    },
    onOpen: loadTools,
  });
  const select = panel.select;

  renderSong();
  renderImages();
  renderSize();
  changed();
  pickQuality();
})();
