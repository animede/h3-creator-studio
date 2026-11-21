// 動画タブ: 作り方(T2VA / FL2VA / Ref2VA / 静止画)・ドロップ欄・音声合成(TTS)・ジョブの投入。
(() => {   // 画像タブと名前がぶつからないよう、タブごとに閉じる
  const MAX = { image: 9, video: 3, audio: 3 };
  const KIND = { image: "画像", video: "動画", audio: "音声" };
  const ICON = { video: "▶", audio: "🔊" };
  const TAG = { t2va: "テキストから動画", fl2va: "先頭フレームから動画", ref2va: "参照から動画" };
  const refs = [];                  // 参照(生成物のレコード)。順番が意味を持つ
  const frames = { first: null, last: null };
  let mode = "t2va";

  const vd = (id) => $("vd-" + id);
  const hasRefs = () => mode === "ref2va" || mode === "still";
  const count = (kind) => refs.filter((r) => r.kind === kind).length;

  function showMsg(text) {
    vd("msg").hidden = !text;
    vd("msg").textContent = text || "";
  }

  // ---- 作り方・長さ・シード ----

  function setMode(m) {
    mode = m;
    document.querySelectorAll("#vd-modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    showMsg("");
    render();
  }
  document.querySelectorAll("#vd-modes button").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

  vd("secs").oninput = render;
  vd("match").onchange = render;
  vd("random").onchange = () => { vd("seed").disabled = vd("random").checked; };

  function render() {
    vd("frames").hidden = mode !== "fl2va";
    vd("refs-fld").hidden = !hasRefs();
    vd("refs-label").textContent = mode === "still" ? "参照(任意。あれば参照から静止画、なければテキストから)" : "参照(画像・動画・音声、順番に意味があります)";
    vd("mode-tag").textContent = mode === "still" ? (refs.length ? "参照から静止画" : "テキストから静止画") : TAG[mode];
    vd("secs-fld").hidden = mode === "still";
    vd("enh").disabled = mode === "still";       // 動画向けの強化なので、静止画では使わない(画像タブの強化を使う)
    vd("enh").title = mode === "still" ? "静止画のプロンプトは、画像タブの「プロンプト強化」で書き直せます" : "MiniMax 公式のプロンプトの書き方に沿って書き直します(管理役の LLM を使います)";
    const matchable = mode === "ref2va" && count("audio") === 1;      // 音声が 1 つだけなら、その長さに合わせられる
    vd("match-fld").hidden = !matchable;
    const match = matchable && vd("match").checked;
    vd("secs").disabled = match;
    vd("secs-val").textContent = match ? "音声の長さ" : `${vd("secs").value} 秒`;
    vd("vocal-fld").hidden = !(mode === "ref2va" && count("audio") > 0);
    renderRefs();
    renderFrame("first");
    renderFrame("last");
  }

  // ---- 先頭・最後のフレーム(画像 1 枚ずつ) ----

  function renderFrame(which) {
    const box = vd(which), item = frames[which];
    const body = box.querySelector(".frame-body");
    body.replaceChildren();
    if (!item) { body.append(el("div", "refs-empty", "ここに画像をドロップ")); return; }
    const c = el("div", "frame-item");
    const img = el("img");
    img.src = item.thumb_url || item.url;
    img.title = item.filename || "";
    img.draggable = false;
    const x = el("button", "rdel", "×");
    x.title = "外す";
    x.onclick = () => { frames[which] = null; renderFrame(which); };
    c.append(img, x);
    makeDraggable(c, item.id);
    body.append(c);
  }

  function setFrame(which, item) {
    if (item.kind !== "image") { showMsg("フレームにできるのは画像だけです"); return false; }
    frames[which] = item;
    showMsg("");
    renderFrame(which);
    return true;
  }

  async function uploadFrame(which, files) {
    try {
      const recs = await uploadFiles(files, vd(which));
      const img = recs.find((r) => r.kind === "image");
      if (img) setFrame(which, img); else showMsg("画像ではないファイルはフレームにできません(ギャラリーには保存しました)");
    } catch (e) { showMsg(`取り込めませんでした: ${e.message || e}`); }
  }

  for (const which of ["first", "last"]) {
    wireDrop(vd(which), { active: () => studio.tab === "video" && mode === "fl2va", msg: showMsg,
      onItem: (it) => setFrame(which, it), onFiles: (files) => uploadFrame(which, files) });
  }

  // ---- 参照(画像・動画・音声の順序つきリスト) ----

  function refNode(r, i) {
    const c = el("div", "ref " + r.kind);
    c.title = `${KIND[r.kind]}: ${r.filename || ""}`;
    if (r.thumb_url || r.kind === "image") {
      const img = el("img");
      img.src = r.thumb_url || r.url;
      img.draggable = false;
      c.append(img);
    } else {
      c.append(el("div", "nothumb " + r.kind, ICON[r.kind]));
    }
    c.append(el("span", "num", String(i + 1)), el("span", "kbadge", KIND[r.kind]));
    const x = el("button", "rdel", "×");
    x.title = "外す";
    x.onclick = () => { refs.splice(i, 1); render(); };
    c.append(x);
    for (const [d, label, text] of [[-1, "前へ", "‹"], [1, "後ろへ", "›"]]) {     // 順番を入れ替える
      if (i + d < 0 || i + d >= refs.length) continue;
      const m = el("button", "mv " + (d < 0 ? "l" : "r"), text);
      m.title = `順番を${label}`;
      m.onclick = () => { [refs[i], refs[i + d]] = [refs[i + d], refs[i]]; render(); };
      c.append(m);
    }
    makeDraggable(c, r.id);
    return c;
  }

  function renderRefs() {
    const list = vd("refs-list");
    list.replaceChildren(...refs.map(refNode));
    vd("refs-empty").hidden = refs.length > 0;
    const note = vd("refs-count");
    note.replaceChildren();
    for (const k of ["image", "video", "audio"]) {
      const n = count(k);
      note.append(el("span", n >= MAX[k] ? "cnt full" : "cnt", `${KIND[k]} ${n}/${MAX[k]}`));
    }
  }

  function addRef(item) {
    if (!MAX[item.kind]) { showMsg("参照にできるのは画像・動画・音声です"); return false; }
    if (refs.some((r) => r.id === item.id)) { showMsg("それはすでに参照に入っています"); return false; }
    if (count(item.kind) >= MAX[item.kind]) { showMsg(`参照の${KIND[item.kind]}は最大 ${MAX[item.kind]} 個です`); return false; }
    const firstAudio = item.kind === "audio" && refs.length === 0;     // 音声だけから始めるときは、音声の長さに合わせるのが普通
    refs.push(item);
    if (firstAudio) vd("match").checked = true;
    showMsg("");
    render();
    return true;
  }

  async function uploadRefs(files) {
    try {
      for (const rec of await uploadFiles(files, vd("refs"))) addRef(rec);
    } catch (e) { showMsg(`取り込めませんでした: ${e.message || e}`); }
  }

  wireDrop(vd("refs"), { zone: vd("refs-fld"), active: () => studio.tab === "video" && hasRefs(), msg: showMsg, onItem: addRef, onFiles: uploadRefs });

  studio.on.items.push(() => {       // ギャラリーから消されたものは入力欄から外す
    let changed = false;
    for (let i = refs.length - 1; i >= 0; i--) if (!byId(refs[i].id)) { refs.splice(i, 1); changed = true; }
    for (const w of ["first", "last"]) if (frames[w] && !byId(frames[w].id)) { frames[w] = null; changed = true; }
    if (changed) render();
  });

  // 生成物ビューアのボタン
  studio.actions.video = (item) => [
    ...(item.kind === "image" ? [{ label: "先頭フレームに使う", run: () => { setTab("video"); setMode("fl2va"); setFrame("first", item); } }] : []),
    { label: "参照に使う", run: () => { setTab("video"); if (!hasRefs()) setMode("ref2va"); addRef(item); } },
  ];

  // 音楽タブの曲は、動画の参照(Ref2VA / Vocal Lock の音声)に使える
  studio.actions.music = (item) => (item.kind === "audio"
    ? [{ label: "動画の参照に使う", run: () => { setTab("video"); if (!hasRefs()) setMode("ref2va"); addRef(item); } }] : []);

  // ---- 音声を作る(TTS) ----

  let speakersLoaded = false;

  async function loadSpeakers() {
    const sel = vd("tts-speaker");
    vd("tts-note").textContent = "話者を読み込んでいます(AivisSpeech が止まっていれば起動します)…";
    try {
      const r = await fetch("/api/tts/speakers");
      if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
      const list = await r.json();
      sel.replaceChildren(...list.map((s) => { const o = el("option", "", s.label); o.value = s.style_id; return o; }));
      let saved = null;
      try { saved = localStorage.getItem("tts-style"); } catch (e) { /* 保存できなくても動く */ }
      if (saved && list.some((s) => String(s.style_id) === saved)) sel.value = saved;
      speakersLoaded = list.length > 0;
      vd("tts-note").textContent = speakersLoaded ? "" : "話者が見つかりません";
    } catch (e) {
      vd("tts-note").textContent = `話者を取得できません: ${e.message || e}`;
    }
  }

  vd("tts").addEventListener("toggle", () => { if (vd("tts").open && !speakersLoaded) loadSpeakers(); });
  vd("tts-speaker").onchange = () => { try { localStorage.setItem("tts-style", vd("tts-speaker").value); } catch (e) { /* 保存できなくても動く */ } };
  vd("tts-speed").oninput = () => { vd("tts-speed-val").textContent = (+vd("tts-speed").value).toFixed(1); };

  vd("tts-go").onclick = async () => {
    const text = vd("tts-text").value.trim();
    const note = vd("tts-note");
    if (!text) { note.textContent = "読み上げる文を入れてください"; return; }
    if (!vd("tts-speaker").value) { note.textContent = "話者を選んでください"; return; }
    vd("tts-go").disabled = true;
    note.textContent = "音声を作っています…";
    try {
      const r = await fetch("/api/tts", { method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ text, style_id: +vd("tts-speaker").value, speed: +vd("tts-speed").value }) });
      const js = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : "入力を確認してください");
      await loadGallery();                       // 先に台帳へ載せる(載っていない参照は外される)
      vd("tts-audio").src = js.url;
      vd("tts-audio").hidden = false;
      note.textContent = mode === "ref2va" && addRef(js) ? "音声を作って、参照に加えました" : "音声を作りました(ギャラリーにあります)";
    } catch (e) {
      note.textContent = `作れませんでした: ${e.message || e}`;
    } finally {
      vd("tts-go").disabled = false;
    }
  };

  vd("tts-text").addEventListener("input", () => {
    if (vd("tts-text").value.length > 1900) vd("tts-note").textContent = `${vd("tts-text").value.length} / 2000 文字`;
  });

  // ---- 生成 ----

  function setBusy(busy) {
    vd("go").disabled = busy;
    vd("stop").disabled = !busy;
  }

  function generate() {
    const prompt = vd("prompt").value.trim();
    if (!prompt) { showMsg("プロンプトを入力してください"); vd("prompt").focus(); return; }
    const params = { prompt, resolution: vd("res").value,
      seed: vd("random").checked || vd("seed").value === "" ? null : parseInt(vd("seed").value, 10) };
    let kind;
    if (mode === "still") {
      kind = refs.length ? "h3.ref2i" : "h3.t2i";
      if (refs.length) params.references = refs.map((r) => r.id);
    } else {
      params.seconds = +vd("secs").value;
      if (mode === "t2va") kind = "h3.t2va";
      else if (mode === "fl2va") {
        if (!frames.first && !frames.last) { showMsg("先頭フレームか最後のフレームの画像を入れてください"); return; }
        kind = "h3.fl2va";
        params.first_image = frames.first ? frames.first.id : null;
        params.last_image = frames.last ? frames.last.id : null;
      } else {
        if (!refs.length) { showMsg("参照を 1 つ以上入れてください(画像・動画・音声)"); return; }
        kind = "h3.ref2va";
        params.references = refs.map((r) => r.id);
        if (!vd("match-fld").hidden && vd("match").checked) params.seconds = 0;
        params.vocal_lock = !vd("vocal-fld").hidden && vd("vocal").checked;
      }
    }
    showMsg("");
    tracker.submit(kind, params);
  }

  const tracker = createTracker("video", { setBusy, showMsg });
  vd("go").onclick = generate;
  vd("stop").onclick = () => { vd("stop").disabled = true; tracker.stop(); };
  vd("prompt").addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey) && !vd("go").disabled) generate(); });

  wireEnhance({ tab: "video", ta: vd("prompt"), btn: vd("enh"), undo: vd("enh-undo"), note: vd("enh-note"), showMsg,
    body: () => ({ task: mode === "still" ? "t2va" : mode, seconds: +vd("secs").value }) });

  tracker.resume();
  render();
})();
