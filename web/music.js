// 音楽タブ: ACE-Step の簡単モード(ジャンル・テーマ・歌詞・言語・長さ)と、AI が作った曲のプランの表示。
(() => {   // 他のタブと名前がぶつからないよう、タブごとに閉じる
  const GENRES = ["J-POP", "Rock", "Ballad", "Anime", "Hip Hop", "R&B", "Enka", "EDM", "Reggaeton", "K-POP", "City Pop", "Jazz"];
  const PLAN_LINES = 8;           // プランの歌詞は、先頭の数行だけ見せる
  let genre = "J-POP";

  const mu = (id) => $("mu-" + id);

  function showMsg(text) {
    mu("msg").hidden = !text;
    mu("msg").textContent = text || "";
  }

  // ---- ジャンル ----

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

  // インストのときは、歌詞とボーカルは使わない
  mu("inst").onchange = () => {
    const inst = mu("inst").checked;
    mu("lyrics").disabled = mu("vocal").disabled = inst;
    mu("lyrics").placeholder = inst ? "インストなので歌詞はありません" : "自分の歌詞を使うときだけ書きます";
  };

  // ---- 曲のプラン(生成中に、AI が作ったテーマ・キャプション・歌詞を見せる) ----

  function showPlan(job) {
    const plan = job.result && job.result.plan;
    if (!plan) return;
    mu("plan").hidden = false;
    mu("plan-theme").textContent = plan.theme;
    mu("plan-cap").textContent = plan.caption;
    const lines = (plan.lyrics || "").split("\n").filter((l) => l.trim());
    mu("plan-lyrics").textContent = plan.lyrics === "[Instrumental]" ? "(インスト)"
      : lines.slice(0, PLAN_LINES).join("\n") + (lines.length > PLAN_LINES ? "\n…" : "");
  }

  // ---- 生成 ----

  function setBusy(busy) {
    mu("go").disabled = busy;
    mu("stop").disabled = !busy;
  }

  function generate() {
    showMsg("");
    mu("plan").hidden = true;
    tracker.submit("ace.simple", {
      genre, theme: mu("theme").value.trim(), lyrics: mu("inst").checked ? "" : mu("lyrics").value.trim(),
      language: mu("lang").value, mood: mu("mood").value, vocal: mu("inst").checked ? "" : mu("vocal").value,
      instrumental: mu("inst").checked, duration: +mu("dur").value,
    });
  }

  const tracker = createTracker("music", { setBusy, showMsg, onJob: showPlan });
  mu("go").onclick = generate;
  mu("stop").onclick = () => { mu("stop").disabled = true; tracker.stop(); };
  tracker.resume();
})();
