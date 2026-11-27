// 管理役の画面: メモリ計・エンジンの状態表示(折りたたみ可)。タブ・生成物・ギャラリーは gallery.js / image.js。
const STATE_LABEL = { stopped: t("停止"), starting: t("起動中"), ready: t("準備完了"), unloaded: t("退避中"), stopping: t("停止中"), error: t("エラー") };
const LANE_LABEL = { h3: "H3", h3image: "H3-Image", ace: "ACE", cpu: "CPU" };      // レーン(エンジン)の短い名前
const rows = {};          // エンジン名 -> {el, parts}
const openLogs = new Set();
let timer = null;
let partsCollapsed = false;       // エンジンパネルの折りたたみ(利用者が触るまでは自動)
let partsTouched = false;
let lastStatus = null;
let doneCounter = null;           // 終わったジョブの数。変わったら、自分が追っていないジョブ(API や別のブラウザ)も含めて一覧を読み直す
let actError = null;              // 画面の操作(起動・停止・準備・LLM の停止)が POST の段階で断られたときの失敗 {error, at, opId}。/api/status の operation に
                                  // 新しい操作(別の id)が出るか ACT_ERROR_MS 経つまで、operation の行より優先して出す(2 秒おきの描き直しで消えないように)
const ACT_ERROR_MS = 10000;

const $ = (id) => document.getElementById(id);
const gib = (mib) => (mib / 1024).toFixed(1);

function setGauge(id, used, total, usedText) {
  const g = $(id);
  const pct = total ? Math.min(100, (used / total) * 100) : 0;
  const bar = g.querySelector(".bar i");
  bar.style.width = pct + "%";
  bar.classList.toggle("hi", pct >= 90);
  g.querySelector(".g-num").textContent = usedText;
}

function el(tag, cls, text) {
  const e = document.createElement(tag);
  if (cls) e.className = cls;
  if (text !== undefined) e.textContent = text;
  return e;
}

async function post(url) {
  const r = await fetch(url, { method: "POST", headers: langHeaders() });
  if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || r.statusText);
  return r.json();
}

function buildRow(name, c) {
  const row = el("div", "row");
  const parts = {
    label: el("span", "label", c.label), badge: el("span", "badge"), tag: el("span", "tag", "GPU"),
    ext: el("span", "tag ext", t("外部")), port: el("span", "meta"), rss: el("span", "meta"),
    btns: el("span", "btns"), log: el("pre", "log"),
  };
  parts.log.hidden = true;
  const mk = (text, fn) => { const b = el("button", "", text); b.onclick = fn; parts.btns.append(b); return b; };
  parts.bStart = mk(t("起動"), () => act(name, "start"));
  parts.bStop = mk(LANG === "en" ? "Stop" : "停止", () => act(name, "stop"));      // i18n-skip 状態の「停止」(Stopped)と同じ鍵になるので、ボタンだけ別に
  parts.bEnsure = mk(t("準備"), () => act(name, "ensure"));
  mk(t("ログ"), async () => {
    if (openLogs.has(name)) { openLogs.delete(name); parts.log.hidden = true; }
    else { openLogs.add(name); parts.log.hidden = false; await refreshLog(name); }
  });
  row.append(parts.label, parts.badge, parts.tag, parts.ext, parts.port, parts.rss, parts.btns, parts.log);
  $("rows").append(row);
  rows[name] = parts;
}

// POST が断られた(409 など)か通信に失敗した: 操作(operation)は作られていないので、いまの operation の id を覚えて、それが変わるまで失敗を出し続ける
function failAct(e) {
  const op = lastStatus ? lastStatus.operation : null;
  actError = { error: String(e.message || e), at: Date.now(), opId: op ? op.id : null };
  showOp(op);
}

async function act(name, action) {
  try { await post(`/api/components/${name}/${action}`); } catch (e) { failAct(e); }
  poll();
}

async function refreshLog(name) {
  const r = await fetch(`/api/components/${name}/log?lines=100`, { headers: langHeaders() }).then((x) => x.json());
  const pre = rows[name].log;
  const atEnd = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 8;
  pre.textContent = r.log || t("(ログはまだありません)");
  if (atEnd) pre.scrollTop = pre.scrollHeight;
}

function showOp(op) {
  if (actError && (Date.now() - actError.at > ACT_ERROR_MS || (op ? op.id : null) !== actError.opId)) actError = null;   // 新しい操作が始まったか、時間が経った
  if (actError) op = { steps: [], error: actError.error };      // 画面の操作の失敗を、operation の行より優先して出す
  const line = $("opline");
  line.replaceChildren();
  line.classList.toggle("err", !!(op && op.error));
  if (!op) return;
  const last = op.steps.length ? op.steps[op.steps.length - 1].msg : "";
  line.append(document.createTextNode(op.error ? t("失敗: {error}", { error: op.error }) : last));
  if (op.error && op.log_tail) line.append(el("pre", "", op.log_tail));
}

// 状態バーの 2 行目: レーンごとの実行中のジョブ(種類と進み)と順番待ちの数。share のときは、GPU に載っているエンジンと利用枠も出す
function renderLanes(s) {
  const share = s.gpu_mode === "share";
  const items = [el("span", "lane", share ? t("GPU: 共有(複数のエンジンを同時に載せる)") : t("GPU: エンジン 1 つずつ"))];
  for (const [name, l] of Object.entries(s.lanes || {})) {
    const jobs = l.running_jobs || [];
    if (!jobs.length && !l.queued) continue;
    const parts = jobs.map((j) => j.kind.split(".").pop() + " " + (j.status === "running" ? Math.round((j.progress || 0) * 100) + "%" : t("準備中")));
    if (l.queued) parts.push(t("待ち {n}", { n: l.queued }));
    items.push(el("span", "lane run", `${LANE_LABEL[name] || name}: ${parts.join(" / ")}`));
  }
  if (share) for (const r of s.residents || []) items.push(el("span", "lane res" + (r.busy ? " busy" : ""), (r.busy ? t("{name} 利用枠 {gib} GiB(仕事中)", { name: LANE_LABEL[r.name] || r.name, gib: gib(r.budget_mib) }) : t("{name} 利用枠 {gib} GiB", { name: LANE_LABEL[r.name] || r.name, gib: gib(r.budget_mib) }))));
  $("lanes").replaceChildren(...items);
}

function render(s) {
  const gpu = s.gpu;
  if (gpu) setGauge("g-vram", gpu.used_mib, gpu.total_mib, `${gib(gpu.used_mib)} / ${gib(gpu.total_mib)} GiB`);
  else setGauge("g-vram", 0, 0, t("GPU なし"));
  // GPU が 2 枚以上ある PC: 管理役が数えている(エンジンを寄せている)GPU を小さく出す
  const multi = !!(gpu && gpu.count > 1);
  $("g-gpu").hidden = !multi;
  if (multi) $("g-gpu").textContent = t("使用 GPU: {index} {name}", { index: gpu.index, name: gpu.name });
  $("g-shared").hidden = s.shared_gpu_mib == null;
  if (s.shared_gpu_mib != null) {      // Windows の共有 GPU メモリ。上限はサーバが知っていればそれ(ふつう CPU メモリの半分)、無ければ 4GiB で目安にする
    const total = s.shared_gpu_total_mib || 4096;
    setGauge("g-shared", s.shared_gpu_mib, total, `${gib(s.shared_gpu_mib)} / ${gib(total)} GiB`);
  }
  if (s.ram) setGauge("g-ram", s.ram.used_mib, s.ram.total_mib, `${gib(s.ram.used_mib)} / ${gib(s.ram.total_mib)} GiB`);
  const w = $("warning");
  w.hidden = !s.warning;
  w.textContent = s.warning || "";
  renderLanes(s);

  for (const [name, c] of Object.entries(s.components)) {
    if (!rows[name]) buildRow(name, c);
    const r = rows[name];
    r.badge.textContent = STATE_LABEL[c.state] || c.state;
    r.badge.className = "badge " + c.state;
    r.tag.hidden = !c.gpu_resident;
    r.tag.textContent = c.budget_mib ? `GPU ${gib(c.budget_mib)} GiB` : "GPU";      // share のときは利用枠つき
    r.ext.hidden = !c.external;
    r.port.textContent = `:${c.port}`;
    r.rss.textContent = c.rss_mib != null ? `RSS ${gib(c.rss_mib)} GiB` : "";
    r.rss.title = c.error || "";
    const running = ["starting", "ready", "unloaded"].includes(c.state);
    r.bStart.disabled = s.busy || running;
    r.bStop.disabled = s.busy || c.state === "stopped" || c.external || c.busy;       // 仕事中のエンジンは止めない
    r.bEnsure.disabled = s.busy;
    if (openLogs.has(name)) refreshLog(name);
  }
  if (s.llm) renderLlm(s.llm);
  showOp(s.operation);
  updateParts(s);
}

// 同梱 LLM(llama-server)の行: 状態(停止 / 起動中 / GPU で準備完了 / CPU で準備完了)・RAM・停止ボタン。止めても、次に LLM を使うときに起動し直す
const LLM_STATE = { stopped: t("停止"), starting: t("起動中"), "ready-gpu": t("準備完了(GPU)"), "ready-cpu": t("準備完了(CPU)") };
// LLM の初回の取得(llama-server・モデル・投影ファイル)の進み具合。黙って数 GB 取得していて、止まったように見えた(2026-11-28)
const LLM_DOWNLOAD = { binary: t("LLM の実行ファイル(llama-server)"), model: t("LLM のモデル(Gemma 4 E4B、約 4.4GB)"),
  mmproj: t("画像・音声を読む投影ファイル(約 0.56GB)") };
function llmDownloadText(d) {
  const what = LLM_DOWNLOAD[d.what] || d.what;
  const gb = (b) => (b / 1e9).toFixed(2);
  return d.total ? t("{what}をダウンロードしています(初回だけ): {done} / {total} GB({pct}%)", { what, done: gb(d.done), total: gb(d.total), pct: Math.floor(d.done * 100 / d.total) })
    : t("{what}をダウンロードしています(初回だけ): {done} GB", { what, done: gb(d.done) });
}
let llmRow = null;
function renderLlm(l) {
  if (!llmRow) {
    const row = el("div", "row");
    llmRow = { label: el("span", "label", t("同梱 LLM")), badge: el("span", "badge"), ext: el("span", "tag ext", t("外部")),
      port: el("span", "meta"), rss: el("span", "meta"), dl: el("span", "meta"), btns: el("span", "btns") };
    llmRow.bStop = el("button", "", LANG === "en" ? "Stop" : "停止");      // i18n-skip エンジンの行と同じ(状態の「停止」と鍵を分ける)
    llmRow.bStop.title = t("止めても、次に LLM を使うときに自動で起動し直します");
    llmRow.bStop.onclick = async () => {
      try { await post("/api/llm/stop"); } catch (e) { failAct(e); }
      poll();
    };
    llmRow.btns.append(llmRow.bStop);
    row.append(llmRow.label, llmRow.badge, llmRow.ext, llmRow.port, llmRow.rss, llmRow.dl, llmRow.btns);
    $("rows").append(row);
  }
  const state = l.state || "stopped";
  llmRow.badge.textContent = l.download ? t("ダウンロード中") : LLM_STATE[state] || state;
  llmRow.badge.className = "badge " + (l.download ? "starting" : state.startsWith("ready") ? "ready" : state);
  llmRow.dl.textContent = l.download ? llmDownloadText(l.download) : "";
  llmRow.ext.hidden = !l.external;
  llmRow.port.textContent = l.port ? `:${l.port}` : "";
  llmRow.rss.textContent = l.rss_mib != null ? `RSS ${gib(l.rss_mib)} GiB` : "";
  llmRow.rss.title = l.error || "";
  llmRow.bStop.disabled = !l.can_stop || state === "stopped" || state === "starting" || l.in_use > 0;
}

// エンジンパネル: 準備が整ったら自動で畳み、見出しに 1 行で状況を出す(利用者が開閉したら以後は自動にしない)
function summarize(s) {
  const comps = Object.values(s.components);
  const res = comps.filter((c) => c.gpu_resident);       // share では複数載っている
  const op = s.operation && !s.operation.done ? s.operation : null;
  let text = res.length ? `GPU: ${res.map((c) => `${c.label}(${STATE_LABEL[c.state] || c.state})`).join(t("、"))}` : t("GPU: 空き");
  const starting = comps.find((c) => c.state === "starting" || c.state === "stopping");
  if (op && op.steps.length) text += " - " + op.steps[op.steps.length - 1].msg;
  else if (starting) text += ` - ${starting.label}: ${STATE_LABEL[starting.state]}`;
  if (s.llm && s.llm.download) text += " - " + llmDownloadText(s.llm.download);      // 畳んでいても見えるように
  return { text, error: !!(s.operation && s.operation.error) || comps.some((c) => c.state === "error") };
}

function updateParts(s) {
  lastStatus = s;
  if (s.version) $("app-version").textContent = "v" + s.version;      // 版(問題の報告のときに分かるように)
  const sum = summarize(s);
  $("parts-summary").textContent = sum.text;
  $("parts-summary").classList.toggle("err", sum.error);
  if (!partsTouched) partsCollapsed = !sum.error && Object.values(s.components).some((c) => c.state === "ready");
  $("parts-body").hidden = partsCollapsed;
  $("parts-head").setAttribute("aria-expanded", String(!partsCollapsed));
  $("parts-head").querySelector(".caret").textContent = partsCollapsed ? "▸" : "▾";
}

function toggleParts() { partsTouched = true; partsCollapsed = !partsCollapsed; if (lastStatus) updateParts(lastStatus); }
$("parts-head").addEventListener("click", toggleParts);
$("parts-head").addEventListener("keydown", (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggleParts(); } });

async function poll() {
  clearTimeout(timer);
  let busy = false;
  try {
    const s = await fetch("/api/status", { headers: langHeaders() }).then((r) => r.json());
    render(s);
    busy = s.busy || Object.values(s.lanes || {}).some((l) => (l.running_jobs || []).length || l.queued);     // 仕事が走っている間は 1 秒おき
    if (s.jobs_done_counter != null) {
      if (doneCounter !== null && s.jobs_done_counter !== doneCounter) loadGallery();   // 履歴・ビューアも更新される
      doneCounter = s.jobs_done_counter;
    }
  } catch (e) {
    $("warning").hidden = false;
    $("warning").textContent = t("管理役に接続できません");
  }
  timer = setTimeout(poll, busy ? 1000 : 2000);
}

document.querySelectorAll(".tab").forEach((b) => b.addEventListener("click", () => setTab(b.dataset.tab)));   // setTab は gallery.js

poll();
