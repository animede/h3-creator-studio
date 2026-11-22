// 音楽タブ: ACE-Step の「簡単」(ジャンル・テーマ・歌詞・キャプション)と「おまかせ」(一言から)、詳細設定、生成中のプランの表示。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const GENRES = ["J-POP", "Rock", "Ballad", "Anime", "Hip Hop", "R&B", "Enka", "EDM", "Reggaeton", "K-POP", "City Pop", "Jazz"];
  const MOODS = [["bright and happy", "😊 明るい"], ["bittersweet and sad", "😢 切ない"], ["intense and powerful", "🔥 激しい"],
    ["calm and gentle", "🍃 穏やか"], ["mysterious and fantastical", "✨ 神秘的"], ["romantic and tender", "💕 ロマンチック"],
    ["playful and fun", "🎉 楽しい"], ["melancholic and dark", "🌙 メランコリック"], ["epic and grand", "🏔️ 壮大"], ["cool and stylish", "😎 クール"]];
  const VOCALS = [["female vocal", "👩 女性"], ["male vocal", "👨 男性"], ["powerful vocal", "🎤 力強い"],
    ["breathy whisper vocal", "🌬️ ウィスパー"], ["falsetto vocal", "🎵 ファルセット"], ["vocal harmonies", "👥 ハーモニー"]];
  const DEFAULT_NEGATIVE = "low quality, noisy, distorted, muddy, clipping, off-key, out of tune, amateur, poorly mixed";
  const PLAN_LINES = 8;           // プランの歌詞は、先頭の数行だけ見せる
  const MODEL_KEY = "studio.ace.model";
  let mode = "simple";
  let genre = "J-POP";
  let mood = "", vocal = "";
  let lastSeed = null;            // 前回の曲のシード(「同じ骨格で作り直す」に使う)
  let captionEdited = false;      // キャプションを利用者が書き換えた(AI が入れた文は、次の生成で作り直す)
  let fillPlan = false;           // 生成を押した直後: 届いたプランの中身を、テーマ・歌詞・キャプション欄に入れる
  let defaultModel = "";          // ACE の既定のモデル名(/api/music/models の default_model)。分かるまでは空

  const mu = (id) => $("mu-" + id);
  const val = (id) => mu(id).value.trim();
  const inst = () => mu("inst").checked;

  function showMsg(text) {
    mu("msg").hidden = !text;
    mu("msg").textContent = text || "";
  }

  // ---- 作り方(簡単 / おまかせ) ----

  function setMode(m) {
    mode = m;
    document.querySelectorAll("#mu-modes button").forEach((b) => b.classList.toggle("active", b.dataset.mode === m));
    showMsg("");
    render();
  }
  document.querySelectorAll("#mu-modes button").forEach((b) => { b.onclick = () => setMode(b.dataset.mode); });

  function render() {          // 作り方と「楽器のみ」に応じて、欄の出し入れをする
    const simple = mode === "simple";
    mu("omakase-box").hidden = simple;
    mu("simple-box").hidden = mu("simple-box2").hidden = !simple;
    // インストのときは、歌詞とボーカルは使わない
    const noVocal = inst();
    mu("lyrics").disabled = noVocal;
    mu("lyrics").placeholder = noVocal ? "インストなので歌詞はありません" : "自分の歌詞を使うときだけ書きます";
    mu("vocals").classList.toggle("off", noVocal);
    mu("regen").hidden = !(simple && !noVocal && val("lyrics"));
  }
  mu("inst").onchange = render;
  mu("lyrics").oninput = render;

  // ---- ジャンル・ムード・ボーカルのチップ ----

  for (const g of GENRES) {
    const b = el("button", "chip", g);
    b.type = "button";
    b.dataset.genre = g;
    b.onclick = () => { genre = g; markGenre(); };
    mu("genres").append(b);
  }
  function markGenre() {
    mu("genres").querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b.dataset.genre === genre));
  }
  markGenre();

  // 1 つだけ選べて、もう一度押すと外れるチップ。get() は今の値、set(v) は選び直し
  function singleChips(box, items, get, set) {
    const mark = () => box.querySelectorAll(".chip").forEach((b) => b.classList.toggle("active", b.dataset.v === get()));
    for (const [v, label] of items) {
      const b = el("button", "chip", label);
      b.type = "button";
      b.dataset.v = v;
      b.onclick = () => { set(get() === v ? "" : v); mark(); };
      box.append(b);
    }
  }
  singleChips(mu("moods"), MOODS, () => mood, (v) => { mood = v; });
  singleChips(mu("vocals"), VOCALS, () => vocal, (v) => { vocal = v; });

  // ---- 「作る」ボタン(管理役の LLM): テーマ / 歌詞 / キャプション ----

  const MAKERS = ["theme", "lyrics", "caption"];
  let making = false;
  let busy = false;

  function updateButtons() {
    for (const k of MAKERS) mu(k + "-go").disabled = making || busy;
    mu("lyrics-go").disabled = making || busy || inst();
  }

  async function llm(name, body, label) {
    showMsg("");
    making = true;
    updateButtons();
    const btn = mu(name + "-go"), text = btn.textContent;
    btn.textContent = "作っています…";
    try {
      const r = await fetch("/api/music/" + name, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
      const js = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : "作れませんでした");
      return js;
    } catch (e) {
      showMsg(`${label}を作れませんでした: ${e.message || e}`);
      return null;
    } finally {
      making = false;
      btn.textContent = text;
      updateButtons();
    }
  }

  const base = () => ({ genre, language: mu("lang").value });

  mu("theme-go").onclick = async () => {
    const js = await llm("theme", base(), "テーマ");
    if (js) mu("theme").value = js.theme;
  };
  mu("lyrics-go").onclick = async () => {
    const js = await llm("lyrics", { ...base(), theme: val("theme"), mood, vocal }, "歌詞");
    if (!js) return;
    if (!val("theme")) mu("theme").value = js.theme;      // テーマが空だったときは、一緒に作ったテーマを入れる
    mu("lyrics").value = js.lyrics;
    mu("lyrics-note").textContent = `AI の推奨の長さ: 約 ${js.recommended_duration} 秒`;
    render();
  };
  mu("caption-go").onclick = async () => {
    const js = await llm("caption", { ...base(), theme: val("theme"), lyrics: inst() ? "" : val("lyrics"), mood, vocal: inst() ? "" : vocal,
      thinking: mu("thinking").checked, instrumental: inst() }, "キャプション");
    if (js) { mu("caption").value = js.caption; captionEdited = true; }     // 作ったものは「手で書いた」扱い(生成で作り直さない)
  };
  mu("theme-clear").onclick = () => { mu("theme").value = ""; };
  mu("lyrics-clear").onclick = () => { mu("lyrics").value = ""; mu("lyrics-note").textContent = ""; render(); };
  mu("caption-clear").onclick = () => { mu("caption").value = ""; captionEdited = false; };
  mu("caption").oninput = () => { captionEdited = true; };

  // ---- 詳細 ----

  mu("random").onchange = () => { mu("seed").disabled = mu("random").checked; };
  mu("lastseed").onclick = () => { mu("seed").value = lastSeed; mu("random").checked = false; mu("seed").disabled = false; };
  mu("neg").value = DEFAULT_NEGATIVE;

  // モデル名なし(「ACE の既定」)は、既定のモデル名が分かればそれで見る。分からなければ turbo とみなす
  const isTurbo = (m) => { const n = m || defaultModel; return !n || n.toLowerCase().includes("turbo"); };

  // モデルを選んだら STEP を合わせる(turbo 8、それ以外 50)。SFT は 4 曲を選べない(VRAM)
  function onModel() {
    const m = mu("model").value;
    mu("steps").value = isTurbo(m) ? "8" : "50";
    const sft = (m || defaultModel).toLowerCase().includes("sft");
    for (const o of mu("batch").options) o.disabled = sft && +o.value > 2;
    if (sft && +mu("batch").value > 2) mu("batch").value = "2";
  }
  mu("model").onchange = () => {
    onModel();
    try { localStorage.setItem(MODEL_KEY, mu("model").value); } catch (e) { /* 保存できなくても動作は同じ */ }
  };

  // ACE のモデル一覧(動いていなければ「ACE の既定」だけ)
  async function loadModels() {
    const sel = mu("model"), keep = sel.value, known = defaultModel;
    let js = { models: [] };
    try { js = await fetch("/api/music/models").then((r) => r.json()); } catch (e) { /* 管理役がまだ起きていない */ }
    defaultModel = js.default_model || "";
    sel.replaceChildren(new Option("ACE の既定", ""));
    const sorted = [...js.models].sort((a, b) => (b.is_loaded - a.is_loaded) || (isTurbo(b.name) - isTurbo(a.name)) || a.name.localeCompare(b.name));
    for (const m of sorted) {
      const o = new Option(`${m.name}${m.is_loaded ? "  ✓読込済" : "  (未読込。選ぶと読み込みます)"}${m.vram_ok ? "" : "  (VRAM 不足)"}`, m.name);
      o.disabled = !m.vram_ok;
      sel.append(o);
    }
    let saved = "";
    try { saved = localStorage.getItem(MODEL_KEY) || ""; } catch (e) { /* 無ければ既定 */ }
    const ok = (v) => v && [...sel.options].some((o) => o.value === v && !o.disabled);
    sel.value = [keep, saved, js.default_model].find(ok) || "";      // いま選んでいるもの → 前回のもの → ACE の既定
    mu("model-note").textContent = js.note ? `${js.note}(既定のモデルで作ります)` : "選んだモデルが読み込まれていなければ、生成の前に自動で読み込みます(数十秒)";
    if (sel.value !== keep || (!sel.value && defaultModel !== known)) onModel();      // 「ACE の既定」のままでも、既定のモデルが分かった(変わった)なら STEP を合わせ直す
  }
  mu("adv").addEventListener("toggle", () => { if (mu("adv").open) loadModels(); });

  const advanced = () => ({
    model: mu("model").value || undefined, steps: +mu("steps").value, thinking: mu("thinking").checked, batch_size: +mu("batch").value,
    seed: mu("random").checked || mu("seed").value === "" ? undefined : +mu("seed").value,
    negative_prompt: val("neg"), shift: mu("shift").value, infer_method: mu("infer").value,
  });

  // ---- 曲のプラン(生成中に、AI が作ったテーマ・キャプション・歌詞を見せる) ----

  function showPlan(job) {
    const plan = job.result && job.result.plan;
    if (!plan) return;
    mu("plan").hidden = false;
    mu("plan-head").textContent = job.kind === "ace.omakase" ? "おまかせ" : "曲のプラン";
    mu("plan-theme").textContent = plan.theme;
    mu("plan-cap").textContent = plan.caption;
    const lines = (plan.lyrics || "").split("\n").filter((l) => l.trim());
    mu("plan-lyrics").textContent = !lines.length || plan.lyrics === "[Instrumental]" ? (lines.length ? "(インスト)" : "")
      : lines.slice(0, PLAN_LINES).join("\n") + (lines.length > PLAN_LINES ? "\n…" : "");
    if (fillPlan && job.kind === "ace.simple") {     // 作ったものを欄に残す(次は、それを使う / 歌詞だけ新しくする、が選べる)
      fillPlan = false;
      if (!val("theme")) mu("theme").value = plan.theme;
      if (!val("lyrics") && !inst() && plan.lyrics) mu("lyrics").value = plan.lyrics;
      if (!captionEdited && plan.caption) mu("caption").value = plan.caption;
      render();
    }
  }

  function onJob(job) {
    showPlan(job);
    if (["completed", "failed", "cancelled"].includes(job.status)) {
      if (job.status === "completed") rememberSeed(job);
      loadModels();        // 読み込み済みのモデルが変わったかもしれない
    }
  }

  function rememberSeed(job) {
    const seed = job.result && job.result.seed;
    if (seed === undefined || seed === null) return;
    lastSeed = seed;
    mu("lastseed").disabled = false;
  }

  // ---- 生成 ----

  function setBusy(b) {
    busy = b;
    mu("go").disabled = mu("regen").disabled = b;
    mu("stop").disabled = !b;
    updateButtons();
    render();
  }

  function generate() {
    showMsg("");
    mu("plan").hidden = true;
    if (mode === "omakase") {
      if (!val("query")) { showMsg("どんな曲か、一言入力してください"); mu("query").focus(); return; }
      tracker.submit("ace.omakase", { query: val("query"), language: mu("lang").value, instrumental: inst(), duration: +mu("dur").value, ...advanced() });
      return;
    }
    fillPlan = true;
    tracker.submit("ace.simple", {
      genre, theme: val("theme"), lyrics: inst() ? "" : val("lyrics"), caption: captionEdited ? val("caption") : "",
      caption_auto: mu("caption-auto").checked, language: mu("lang").value, mood, vocal: inst() ? "" : vocal,
      instrumental: inst(), duration: +mu("dur").value, ...advanced(),
    });
  }

  // 歌詞を消して、テーマはそのままで(歌詞とキャプションを作り直して)生成する
  function regenerate() {
    mu("lyrics").value = "";
    mu("lyrics-note").textContent = "";
    if (!captionEdited) mu("caption").value = "";
    render();
    generate();
  }

  const tracker = createTracker("music", { setBusy, showMsg, onJob });
  mu("go").onclick = generate;
  mu("regen").onclick = regenerate;
  mu("stop").onclick = () => { mu("stop").disabled = true; tracker.stop(); };
  tracker.resume();
  render();
  // 前回のシード: 直近に完了した曲から拾う(ページを開き直しても「同じ骨格で作り直す」が使える)
  fetch("/api/jobs?tab=music&limit=20").then((r) => r.json()).then((jobs) => {
    const done = jobs.find((j) => j.status === "completed" && j.result && j.result.seed != null);
    if (done) rememberSeed(done);
  }).catch(() => {});
  loadModels();
})();
