// ジョブの投入・追跡・進捗表示(画像タブ・動画タブで共通)。進捗ブロックは生成物パネルの上部に 1 つだけある。

// tab のジョブを扱う道具を作る。setBusy(bool) は 生成/中止 ボタンの切り替え、showMsg(text) は操作欄のメッセージ、
// onJob(job) は追跡中のジョブが更新されるたびに呼ばれる(音楽タブが曲のプランを出すのに使う)。
function createTracker(tab, { setBusy, showMsg, onJob = () => {} }) {
  let active = null;         // 追跡中のジョブ(進行中)
  let last = null;           // 直近に終わった失敗・中止のジョブ(メッセージを残す)
  let timer = null;

  async function submit(kind, params) {
    setBusy(true);
    try {
      const r = await fetch("/api/jobs", { method: "POST", headers: langHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ tab, kind, params }) });
      const js = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : t("入力を確認してください"));
      last = null;
      track({ id: js.id, status: "queued", progress: 0, message: t("順番を待っています"), wait_reason: t("順番を待っています"), steps: [], created: Date.now() / 1000 });
    } catch (e) {
      setBusy(false);
      showMsg(t("生成を始められませんでした: {error}", { error: e.message || e }));
    }
  }

  function track(job) {
    active = job;
    setBusy(true);
    render();
    clearTimeout(timer);
    timer = setTimeout(poll, 300);
  }

  async function poll() {
    const id = active && active.id;
    if (!id) return;
    try {
      const r = await fetch(`/api/jobs/${id}`, { headers: langHeaders() });
      if (r.status === 404) { active = null; setBusy(false); render(); return; }
      const job = await r.json();
      if (["completed", "failed", "cancelled"].includes(job.status)) { await finish(job); return; }
      active = job;
      render();
      onJob(job);
    } catch (e) { /* 一時的な通信失敗は次の周で再試行 */ }
    timer = setTimeout(poll, 1000);
  }

  async function finish(job) {
    onJob(job);
    active = null;
    setBusy(false);
    if (job.status === "completed") {
      last = null;
      await loadGallery();
      if (job.outputs.length) studio.viewing[tab] = job.outputs[0];
      renderHistory();
      renderViewer();
    } else {
      last = job;
    }
    render();
  }

  async function stop() {
    if (!active) return;
    try { await fetch(`/api/jobs/${active.id}/cancel`, { method: "POST", headers: langHeaders() }); } catch (e) { /* 次の周で状態が分かる */ }
  }

  const fmtElapsed = (created) => {
    const s = Math.max(0, Math.round(Date.now() / 1000 - created));
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  };

  // 進捗ブロック: 進行中のジョブか、直近の失敗・中止を出す(自分のタブが開いているときだけ触る)
  function render() {
    if (studio.tab !== tab) return;
    const j = active || last;
    const box = $("progress");
    box.hidden = !j;
    if (!j) return;
    const failed = j.status === "failed", cancelled = j.status === "cancelled";
    box.classList.toggle("err", failed);
    const pct = j.status === "running" ? Math.round((j.progress || 0) * 100) : 0;
    const bar = box.querySelector(".pbar");
    bar.hidden = failed || cancelled;
    bar.classList.toggle("indet", j.status !== "running" || pct === 0);
    bar.querySelector("i").style.width = j.status === "running" && pct > 0 ? pct + "%" : "";
    // 順番待ちのジョブは、待っている理由(順番 / GPU の空き待ち)を出す
    $("pmsg").textContent = failed ? t("失敗: {error}", { error: j.error || t("原因不明") }) : cancelled ? t("中止しました")
      : t("{msg}  (経過 {time})", { msg: (j.status === "queued" && j.wait_reason) || j.message || "", time: fmtElapsed(j.created) });
    const steps = $("psteps");
    steps.replaceChildren();
    if (j.status === "preparing") (j.steps || []).slice(-4, -1).forEach((t) => steps.append(el("li", "", t)));
    $("plog").hidden = !(failed && j.log_tail);
    $("plog").textContent = j.log_tail || "";
  }

  // ページを開き直したとき、進行中のジョブがあれば追跡を再開する(同時に複数あれば、走っているものを先に)
  async function resume() {
    try {
      const jobs = await fetch(`/api/jobs?tab=${tab}&limit=10`, { headers: langHeaders() }).then((r) => r.json());
      const run = jobs.find((j) => ["preparing", "running"].includes(j.status)) || jobs.find((j) => j.status === "queued");
      if (run) track(run);
    } catch (e) { /* 管理役がまだ起きていない */ }
  }

  studio.on.tab.push(render);
  return { submit, stop, render, resume };
}

// プロンプト強化: textarea の中身を /api/enhance の結果に置き換える。元の文は「元に戻す」(と、そのリンクの吹き出し)に残す。
//   opts: tab, ta(textarea), btn, undo(リンク), note(説明の文字), body()(tab 固有の追加項目), showMsg
function wireEnhance({ tab, ta, btn, undo, note, body = () => ({}), showMsg }) {
  let orig = null;
  undo.onclick = (e) => { e.preventDefault(); if (orig !== null) ta.value = orig; undo.hidden = true; note.textContent = ""; };
  btn.onclick = async () => {
    const text = ta.value.trim();
    if (!text) { showMsg(t("プロンプトを入力してください")); ta.focus(); return; }
    showMsg("");
    btn.disabled = true;
    undo.hidden = true;
    note.textContent = t("強化しています…(エンジンや LLM を起こすので、数分かかることがあります)");
    try {
      const r = await fetch("/api/enhance", { method: "POST", headers: langHeaders({ "Content-Type": "application/json" }), body: JSON.stringify({ tab, text, ...body() }) });
      const js = await r.json().catch(() => ({}));
      if (!r.ok) throw new Error(typeof js.detail === "string" ? js.detail : t("強化できませんでした"));
      orig = ta.value;
      ta.value = js.text;
      undo.hidden = false;
      undo.title = t("元の文: {text}", { text: orig });
      note.textContent = js.suggested_width ? t("推奨サイズ: {w}×{h}", { w: js.suggested_width, h: js.suggested_height }) : "";
    } catch (e) {
      note.textContent = "";
      showMsg(t("強化できませんでした: {error}", { error: e.message || e }));
    } finally {
      btn.disabled = false;
    }
  };
}
