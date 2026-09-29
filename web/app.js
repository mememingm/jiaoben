const state = {
  jobId: null,
  rows: [],
  querySummary: null,
  filteredRows: [],
  selectedIndex: null,
  selectedInventoryIds: new Set(),
  batch: null,
  activationJobId: null,
  activationResults: [],
  activationPollTimer: null,
  pollTimer: null,
  loadingHistory: false,
};

const $ = (id) => document.getElementById(id);
const form = $("query-form");
const username = $("username");
const password = $("password");
const baseUrl = $("base-url");
const iccidField = $("iccid-field");
const iccids = $("iccids");
const queryButton = $("query-button");
const cancelButton = $("cancel-button");
const loadHistoryButton = $("load-history");
const historyPanel = $("history-panel");
const closeHistoryButton = $("close-history");
const historyList = $("history-list");
const historyOverlay = $("history-overlay");
const progressPanel = $("progress-panel");
const progressBar = $("progress-bar");
const progressMessage = $("progress-message");
const progressPercent = $("progress-percent");
const resultBody = $("result-body");
const search = $("search");
const statusFilter = $("status-filter");
const exportIccids = $("export-iccids");
const detailContent = $("detail-content");
const detailLabel = $("detail-label");
const historyAccount = $("history-account");
const batchTarget = $("batch-target");
const selectReleasedButton = $("select-released-button");
const clearSelectionButton = $("clear-selection-button");
const createBatchButton = $("create-batch-button");
const batchSelection = $("batch-selection");
const batchMessage = $("batch-message");
const batchResult = $("batch-result");

document.querySelectorAll('input[name="scope"]').forEach((radio) => {
  radio.addEventListener("change", () => {
    const selected = radio.value === "selected";
    iccidField.hidden = !selected;
    iccids.disabled = !selected;
    if (selected) iccids.focus();
  });
});
search.addEventListener("input", renderRows);
statusFilter.addEventListener("change", renderRows);
exportIccids.addEventListener("click", exportCurrentIccids);
form.addEventListener("submit", startQuery);
cancelButton.addEventListener("click", cancelQuery);
loadHistoryButton.addEventListener("click", loadLatestHistory);
closeHistoryButton.addEventListener("click", closeHistory);
historyOverlay.addEventListener("click", closeHistory);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !historyPanel.hidden) closeHistory();
});
batchTarget.addEventListener("input", updateBatchControls);
selectReleasedButton.addEventListener("click", selectReleasedRows);
clearSelectionButton.addEventListener("click", clearSelection);
createBatchButton.addEventListener("click", createWriteBatch);

function setFormMessage(message = "") {
  $("form-message").textContent = message;
}

function selectedScope() {
  return document.querySelector('input[name="scope"]:checked').value;
}

async function startQuery(event) {
  event.preventDefault();
  if (state.jobId || state.loadingHistory) return;
  setFormMessage("");
  const scope = selectedScope();
  const values = iccids.value.split(/[\s,;，；]+/).map((value) => value.trim()).filter(Boolean);
  if (scope === "selected" && values.length === 0) {
    setFormMessage("请输入至少一个 ICCID，或切换为全部 eSIM。 ");
    iccids.focus();
    return;
  }
  setBusy(true);
  state.rows = [];
  state.querySummary = null;
  state.selectedIndex = null;
  state.selectedInventoryIds.clear();
  state.batch = null;
  state.activationJobId = null;
  state.activationResults = [];
  renderBatchResult();
  setHistoryAccount("");
  renderRows();
  setProgress(0, "正在创建查询任务……");
  try {
    const response = await fetch("/api/jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
        base_url: baseUrl.value.trim(),
        scope,
        iccids: values,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法创建查询任务。");
    state.jobId = data.job_id;
    pollJob();
  } catch (error) {
    setBusy(false);
    setProgress(0, "查询未开始");
    setFormMessage(error.message);
  }
}

async function pollJob() {
  if (!state.jobId) return;
  try {
    const response = await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "查询任务不存在。");
    setProgress(data.progress || 0, data.message || "正在处理……");
    $("status-message").textContent = data.message || "正在处理……";
    if (data.state === "done") {
      state.rows = Array.isArray(data.rows) ? data.rows : [];
      state.querySummary = normalizeQuerySummary(data.profile_query_summary, state.rows);
      setHistoryAccount(data.account);
      $("output-paths").textContent = outputLabel(data.json_path, data.csv_path);
      finishQuery(queryCompletionMessage(state.querySummary), false);
      return;
    }
    if (data.state === "failed") {
      finishQuery(data.error || "查询失败。", true);
      return;
    }
    if (data.state === "cancelled") {
      finishQuery("查询已取消。", true);
      return;
    }
    state.pollTimer = window.setTimeout(pollJob, 350);
  } catch (error) {
    finishQuery(error.message, true);
  }
}

async function cancelQuery() {
  if (!state.jobId) return;
  cancelButton.disabled = true;
  try {
    await fetch(`/api/jobs/${encodeURIComponent(state.jobId)}/cancel`, { method: "POST" });
  } catch (_error) {
    $("status-message").textContent = "正在停止查询……";
  }
}

async function loadLatestHistory() {
  if (state.jobId || state.loadingHistory) return;
  setHistoryOpen(true);
  await loadHistoryIndex();
}

function setHistoryOpen(open) {
  historyPanel.hidden = !open;
  historyOverlay.hidden = !open;
  loadHistoryButton.setAttribute("aria-expanded", String(open));
  if (open) closeHistoryButton.focus();
}

function closeHistory() {
  if (historyPanel.hidden) return;
  setHistoryOpen(false);
  loadHistoryButton.focus();
}

async function loadHistoryIndex() {
  historyList.replaceChildren();
  const loading = document.createElement("div");
  loading.className = "history-empty";
  loading.textContent = "正在读取记录……";
  historyList.append(loading);
  try {
    const response = await fetch("/api/history", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取查询记录。");
    renderHistoryList(Array.isArray(data.items) ? data.items : []);
  } catch (error) {
    historyList.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = error.message;
    historyList.append(empty);
  }
}

function renderHistoryList(items) {
  historyList.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("div");
    empty.className = "history-empty";
    empty.textContent = "还没有保存的查询记录。";
    historyList.append(empty);
    return;
  }
  items.forEach((item) => {
    const card = document.createElement("article");
    card.className = "history-item";
    const info = document.createElement("div");
    const title = document.createElement("strong");
    title.textContent = formatHistoryTime(item.queried_at) || item.filename;
    const meta = document.createElement("span");
    const statuses = Object.entries(item.statuses || {}).map(([key, value]) => `${key}: ${value}`).join(" · ");
    meta.textContent = `${item.account || "未知账号"} · ${item.count || 0} 条${statuses ? ` · ${statuses}` : ""}`;
    const summary = normalizeQuerySummary(item.profile_query_summary, []);
    const quality = document.createElement("span");
    quality.className = `history-quality ${summary.complete ? "is-complete" : "is-incomplete"}`;
    quality.textContent = `完整度 ${formatRate(summary.success_rate)} · 成功 ${summary.succeeded}/${summary.total}`;
    info.append(title, meta, quality);
    const actions = document.createElement("div");
    actions.className = "history-actions";
    const load = document.createElement("button");
    load.className = "button button-secondary";
    load.type = "button";
    load.textContent = "加载";
    load.addEventListener("click", () => loadHistory(item.filename));
    const remove = document.createElement("button");
    remove.className = "button button-danger";
    remove.type = "button";
    remove.textContent = "删除";
    remove.addEventListener("click", () => deleteHistory(item.filename, title.textContent));
    actions.append(load, remove);
    card.append(info, actions);
    historyList.append(card);
  });
}

async function loadHistory(filename) {
  if (state.jobId || state.loadingHistory) return;
  state.loadingHistory = true;
  loadHistoryButton.disabled = true;
  queryButton.disabled = true;
  setFormMessage("");
  setProgress(0, "正在读取上次保存的结果……");
  try {
    const endpoint = filename ? `/api/history/${encodeURIComponent(filename)}` : "/api/history/latest";
    const response = await fetch(endpoint, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "无法读取历史记录。");

    state.rows = Array.isArray(data.records) ? data.records : [];
    state.querySummary = normalizeQuerySummary(data.profile_query_summary, state.rows);
    state.selectedIndex = null;
    state.selectedInventoryIds.clear();
    state.batch = null;
    state.activationJobId = null;
    state.activationResults = [];
    setHistoryAccount(data.account);
    search.value = "";
    statusFilter.value = "all";
    detailLabel.textContent = "未选择记录";
    detailContent.textContent = "单击一条记录查看平台原始字段。";
    renderSummary();
    renderRows();
    renderBatchResult();
    const paths = outputLabel(data.json_path, data.csv_path);
    const historyName = data.history_filename ? `历史记录：${data.history_filename}` : "历史记录";
    $("output-paths").textContent = paths ? `${historyName} · ${paths}` : historyName;
    setProgress(100, "已加载上次结果");
    const queriedAt = formatHistoryTime(data.queried_at);
    const accountLabel = data.account || "未知账号（旧记录）";
    const loadedLabel = queriedAt
      ? `已加载查询记录（账号：${accountLabel}；查询时间：${queriedAt}）。`
      : `已加载查询记录（账号：${accountLabel}）。`;
    $("status-message").textContent = state.querySummary.complete
      ? loadedLabel
      : `${loadedLabel} 结果不完整，不能用于核对激活数量。`;
    closeHistory();
  } catch (error) {
    setProgress(0, "历史记录未加载");
    setFormMessage(error.message === "还没有保存的历史记录。"
      ? "还没有保存的历史记录，请先查询一次。"
      : error.message);
    $("status-message").textContent = "历史记录未加载。";
  } finally {
    state.loadingHistory = false;
    loadHistoryButton.disabled = false;
    queryButton.disabled = Boolean(state.jobId);
    updateBatchControls();
  }
}

async function deleteHistory(filename, label) {
  if (!window.confirm(`确定删除查询记录“${label}”吗？对应 JSON 和 CSV 都会删除。`)) return;
  try {
    const response = await fetch(`/api/history/${encodeURIComponent(filename)}`, { method: "DELETE" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "删除失败。");
    await loadHistoryIndex();
    $("status-message").textContent = "查询记录已删除。";
  } catch (error) {
    setFormMessage(error.message);
  }
}

function finishQuery(message, isError) {
  if (state.pollTimer) window.clearTimeout(state.pollTimer);
  state.pollTimer = null;
  setBusy(false);
  $("status-message").textContent = message;
  if (isError) setFormMessage(message);
  if (!isError) renderSummary();
  renderRows();
}

function setBusy(busy) {
  state.jobId = busy ? state.jobId : null;
  queryButton.disabled = busy || state.loadingHistory;
  cancelButton.disabled = !busy;
  loadHistoryButton.disabled = busy;
  username.disabled = busy;
  password.disabled = busy;
  baseUrl.disabled = busy;
  document.querySelectorAll('input[name="scope"]').forEach((radio) => { radio.disabled = busy; });
  iccids.disabled = busy || selectedScope() !== "selected";
  updateBatchControls();
  updateActivationControls();
}

function setProgress(percent, message) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  progressPanel.hidden = false;
  progressBar.style.width = `${value}%`;
  progressPercent.textContent = `${Math.round(value)}%`;
  progressMessage.textContent = message;
}

function deriveQuerySummary(rows) {
  const total = rows.length;
  let succeeded = 0;
  let retriedRecords = 0;
  let recoveredAfterRetry = 0;
  rows.forEach((row) => {
    const success = row.esimProfileStatusQueryStatus === "ok"
      || (row.esimProfileStatusQueryStatus !== "failed"
        && Object.prototype.hasOwnProperty.call(row, "esimProfileStatus"));
    if (success) succeeded += 1;
    const attempts = Number.isInteger(row.esimProfileStatusQueryAttempts)
      ? row.esimProfileStatusQueryAttempts : 1;
    if (attempts > 1) {
      retriedRecords += 1;
      if (success) recoveredAfterRetry += 1;
    }
  });
  const failed = total - succeeded;
  return {
    total,
    succeeded,
    failed,
    success_rate: total ? Math.round((succeeded / total * 100) * 10) / 10 : 100,
    complete: failed === 0,
    retried_records: retriedRecords,
    recovered_after_retry: recoveredAfterRetry,
    failure_reasons: rows.reduce((reasons, row) => {
      if (row.esimProfileStatusQueryStatus !== "failed") return reasons;
      const reason = String(row.esimProfileStatusQueryError || "未知失败原因");
      reasons[reason] = (reasons[reason] || 0) + 1;
      return reasons;
    }, {}),
  };
}

function normalizeQuerySummary(value, rows) {
  const fallback = deriveQuerySummary(rows);
  if (!value || typeof value !== "object") return fallback;
  const total = Number.isFinite(Number(value.total)) ? Number(value.total) : fallback.total;
  const succeeded = Number.isFinite(Number(value.succeeded)) ? Number(value.succeeded) : fallback.succeeded;
  const failed = Number.isFinite(Number(value.failed)) ? Number(value.failed) : Math.max(0, total - succeeded);
  const rateValue = Number(value.success_rate);
  return {
    total,
    succeeded,
    failed,
    success_rate: Number.isFinite(rateValue) ? rateValue : (total ? succeeded / total * 100 : 100),
    complete: failed === 0 && succeeded === total,
    retried_records: Number(value.retried_records) || 0,
    recovered_after_retry: Number(value.recovered_after_retry) || 0,
    failure_reasons: value.failure_reasons && typeof value.failure_reasons === "object"
      ? value.failure_reasons : fallback.failure_reasons,
  };
}

function formatRate(value) {
  const rounded = Math.round((Number(value) || 0) * 10) / 10;
  return `${rounded.toFixed(rounded % 1 === 0 ? 0 : 1)}%`;
}

function queryCompletionMessage(summary) {
  if (summary.complete) return `查询完整：成功 ${summary.succeeded}/${summary.total}，结果已保存。`;
  return `查询结束但结果不完整：成功 ${summary.succeeded}/${summary.total}，失败 ${summary.failed}。不能用于核对激活数量。`;
}

function renderSummary() {
  $("metric-total").textContent = state.rows.length;
  $("metric-installed").textContent = state.rows.filter((row) => row.esimProfileStatus === "INSTALLED").length;
  $("metric-released").textContent = state.rows.filter((row) => row.esimProfileStatus === "RELEASED").length;
  $("metric-failed").textContent = state.rows.filter((row) => row.esimProfileStatusQueryStatus === "failed").length;
  const summary = normalizeQuerySummary(state.querySummary, state.rows);
  state.querySummary = summary;
  const quality = $("query-quality");
  quality.hidden = summary.total === 0;
  quality.className = `query-quality ${summary.complete ? "query-quality-complete" : "query-quality-incomplete"}`;
  $("query-quality-title").textContent = summary.complete
    ? "查询完整，可用于状态核对"
    : "结果不完整，不能用于核对激活数量";
  const retryText = summary.retried_records > 0
    ? ` · 重试 ${summary.retried_records} 条，恢复 ${summary.recovered_after_retry} 条`
    : "";
  const topFailure = Object.entries(summary.failure_reasons || {})
    .sort((left, right) => Number(right[1]) - Number(left[1]))[0];
  const failureText = topFailure ? ` · 主要失败：${topFailure[0]} × ${topFailure[1]}` : "";
  $("query-quality-detail").textContent = `成功 ${summary.succeeded}/${summary.total} · 失败 ${summary.failed}${retryText}${failureText}`;
  $("query-quality-rate").textContent = `完整度 ${formatRate(summary.success_rate)}`;
  updateBatchControls();
}

function renderRows() {
  const needle = search.value.trim().toLowerCase();
  const selected = statusFilter.value;
  state.filteredRows = state.rows.filter((row) => {
    const matchesSearch = !needle || JSON.stringify(row).toLowerCase().includes(needle);
    const matchesFilter = selected === "all"
      || (selected === "failed" && row.esimProfileStatusQueryStatus === "failed")
      || row.esimProfileStatus === selected;
    return matchesSearch && matchesFilter;
  });
  resultBody.replaceChildren();
  if (state.filteredRows.length === 0) {
    const tr = document.createElement("tr");
    tr.className = "empty-row";
    const td = document.createElement("td");
    td.colSpan = 6;
    td.innerHTML = '<div class="empty-state"><span class="empty-glyph">◌</span><strong>没有匹配记录</strong><span>调整筛选条件后再查看。</span></div>';
    tr.append(td);
    resultBody.append(tr);
  } else {
    state.filteredRows.forEach((row, index) => resultBody.append(createRow(row, index)));
  }
  $("record-count").textContent = `${state.filteredRows.length} / ${state.rows.length} 条`;
  exportIccids.disabled = state.filteredRows.length === 0;
  updateBatchControls();
}

function exportCurrentIccids() {
  const values = [...new Set(state.filteredRows.map((row) => String(row.iccid || "").trim()))]
    .filter((value) => /^[0-9]{18,22}$/.test(value));
  if (values.length === 0) {
    setFormMessage("当前列表没有可导出的 ICCID。 ");
    return;
  }
  const stamp = new Date().toISOString().replace(/[-:]/g, "").replace(/\.\d{3}Z$/, "Z");
  const blob = new Blob([`${values.join("\n")}\n`], { type: "text/plain;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `esim-iccids-${stamp}.txt`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
  $("status-message").textContent = `已导出当前列表 ${values.length} 个 ICCID。`;
}

function createRow(row, index) {
  const tr = document.createElement("tr");
  if (state.selectedIndex === index) tr.classList.add("selected");
  const raw = row.esimProfileStatus;
  const failed = row.esimProfileStatusQueryStatus === "failed";
  const statusCell = failed ? chip("查询失败", "failed") : chip(profileLabel(raw), profileClass(raw));
  const selectionTd = document.createElement("td");
  selectionTd.className = "selection-cell";
  const selectable = isReleasedRow(row);
  const checkbox = document.createElement("input");
  checkbox.type = "checkbox";
  checkbox.className = "row-selector";
  checkbox.checked = selectable && state.selectedInventoryIds.has(row.id);
  checkbox.disabled = !selectable;
  checkbox.setAttribute("aria-label", `选择 ${row.iccid || "此卡"} 加入写卡批次`);
  checkbox.addEventListener("click", (event) => event.stopPropagation());
  checkbox.addEventListener("change", () => {
    if (checkbox.checked) state.selectedInventoryIds.add(row.id);
    else state.selectedInventoryIds.delete(row.id);
    updateBatchControls();
  });
  selectionTd.append(checkbox);
  tr.append(selectionTd);
  const values = [row.iccid || "", row.status || "", null,
    row.esimProfileStatusUpdatedAt || "", failed ? "失败" : (row.esimProfileStatusQueryStatus === "ok" ? "成功" : "")];
  values.forEach((value, cellIndex) => {
    const td = document.createElement("td");
    if (cellIndex === 2) td.append(statusCell);
    else {
      td.textContent = value;
      if (cellIndex === 4) td.className = failed ? "query-failed" : "query-ok";
    }
    tr.append(td);
  });
  tr.addEventListener("click", () => {
    state.selectedIndex = index;
    detailLabel.textContent = row.iccid || "未命名记录";
    detailContent.textContent = JSON.stringify(row, null, 2);
    renderRows();
  });
  return tr;
}

function isReleasedRow(row) {
  return row && row.esimProfileStatus === "RELEASED"
    && row.esimProfileStatusQueryStatus === "ok"
    && Number.isInteger(row.id) && row.id > 0;
}

function releasedRows() {
  return state.rows.filter(isReleasedRow);
}

function updateBatchControls() {
  const parsed = Number.parseInt(batchTarget.value, 10);
  const target = Math.max(1, Math.min(200, Number.isFinite(parsed) ? parsed : 200));
  if (document.activeElement !== batchTarget) batchTarget.value = String(target);
  const availableRows = releasedRows();
  const availableIds = new Set(availableRows.map((row) => row.id));
  const selected = [...state.selectedInventoryIds].filter((id) => availableIds.has(id)).length;
  batchSelection.textContent = `已选择 ${selected} 张；可用 RELEASED ${availableRows.length} 张；目标数量仅用于自动选择，批次上限 200 张。`;
  const locked = Boolean(state.jobId) || state.loadingHistory;
  batchTarget.disabled = locked;
  selectReleasedButton.disabled = locked || availableRows.length === 0;
  clearSelectionButton.disabled = locked || selected === 0;
  createBatchButton.disabled = locked || selected === 0;
}

function selectReleasedRows() {
  const parsed = Number.parseInt(batchTarget.value, 10);
  const target = Math.max(1, Math.min(200, Number.isFinite(parsed) ? parsed : 200));
  batchTarget.value = String(target);
  const candidates = releasedRows().slice(0, target);
  state.selectedInventoryIds = new Set(candidates.map((row) => row.id));
  renderRows();
  $("status-message").textContent = `已自动选择 ${candidates.length} 张 RELEASED 卡。`;
}

function clearSelection() {
  state.selectedInventoryIds.clear();
  renderRows();
  $("status-message").textContent = "已清除写卡批次选择。";
}

async function createWriteBatch() {
  const selectedRows = state.rows.filter((row) => state.selectedInventoryIds.has(row.id));
  if (selectedRows.length === 0) {
    batchMessage.textContent = "请先选择 RELEASED 卡。";
    return;
  }
  if (selectedRows.length > 200) {
    batchMessage.textContent = "单个批次最多 200 张卡。";
    return;
  }
  batchMessage.textContent = "正在保存写卡准备清单……";
  createBatchButton.disabled = true;
  try {
    const response = await fetch("/api/write-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ count: selectedRows.length, rows: selectedRows }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "写卡批次创建失败。");
    state.batch = data;
    state.activationJobId = null;
    state.activationResults = [];
    renderBatchResult();
    batchMessage.textContent = `批次已创建：${data.batch_id}。当前未获取二维码、未读取 LPA、未写卡。`;
    $("status-message").textContent = "写卡准备批次已保存。";
  } catch (error) {
    batchMessage.textContent = error.message;
  } finally {
    updateBatchControls();
  }
}

function renderBatchResult() {
  batchResult.replaceChildren();
  if (!state.batch) {
    batchResult.hidden = true;
    return;
  }
  batchResult.hidden = false;
  const title = document.createElement("strong");
  title.textContent = `${state.batch.batch_id} · ${state.batch.count} 张`;
  const note = document.createElement("span");
  note.textContent = "阶段：等待一次性获取 LPA 和二维码";
  const link = document.createElement("a");
  link.href = state.batch.csv_download || "#";
  link.textContent = "下载 CSV 清单";
  link.download = "";
  const button = document.createElement("button");
  button.id = "fetch-profile-data-button";
  button.type = "button";
  button.className = "button button-caution";
  button.textContent = "一次性获取 LPA + 二维码";
  button.title = "每张卡最多请求一次；超时不重试";
  button.addEventListener("click", startActivationData);
  const progress = document.createElement("div");
  progress.id = "activation-progress";
  progress.className = "activation-progress";
  progress.hidden = true;
  progress.innerHTML = '<div class="progress-line"><span id="activation-progress-message">准备获取</span><strong id="activation-progress-percent">0%</strong></div><div class="progress-track"><div id="activation-progress-bar" class="progress-bar"></div></div>';
  const results = document.createElement("div");
  results.id = "activation-results";
  results.className = "activation-results";
  batchResult.append(title, note, link, button, progress, results);
  renderActivationResults(state.activationResults);
  updateActivationControls();
}

function updateActivationControls() {
  const button = $("fetch-profile-data-button");
  if (!button) return;
  const locked = Boolean(state.jobId) || state.loadingHistory || Boolean(state.activationJobId);
  const alreadyStarted = state.batch && state.batch.qr_operations && state.batch.qr_operations !== "none";
  button.disabled = locked || alreadyStarted;
}

async function startActivationData() {
  if (!state.batch || state.activationJobId) return;
  if (!username.value.trim() || !password.value) {
    batchMessage.textContent = "获取 LPA 需要填写后台账号和密码。";
    return;
  }
  const confirmed = window.confirm(
    "这会对批次中的每张卡各请求一次二维码接口，并可能消耗查看次数。\n\n超时或网络中断不会重试，结果未知时会停止后续卡。确认继续吗？"
  );
  if (!confirmed) return;
  state.batch.qr_operations = "started";
  updateActivationControls();
  batchMessage.textContent = "正在创建一次性激活数据任务……";
  try {
    const response = await fetch("/api/activation-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        batch_id: state.batch.batch_id,
        username: username.value.trim(),
        password: password.value,
        base_url: baseUrl.value.trim(),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "激活数据任务创建失败。");
    state.activationJobId = data.job_id;
    pollActivationJob();
  } catch (error) {
    batchMessage.textContent = error.message;
    updateActivationControls();
  }
}

async function pollActivationJob() {
  if (!state.activationJobId) return;
  try {
    const response = await fetch(`/api/activation-jobs/${encodeURIComponent(state.activationJobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "激活数据任务不存在。");
    setActivationProgress(data.progress || 0, data.message || "正在获取激活数据……");
    if (data.state === "done") {
      state.activationResults = Array.isArray(data.results) ? data.results : [];
      renderActivationResults(state.activationResults);
      batchMessage.textContent = "激活数据任务完成；LPA 只保存在当前页面任务内存中。";
      updateActivationControls();
      return;
    }
    if (data.state === "failed" || data.state === "cancelled") {
      batchMessage.textContent = data.error || data.message || "激活数据任务未完成。";
      updateActivationControls();
      return;
    }
    state.activationPollTimer = window.setTimeout(pollActivationJob, 500);
  } catch (error) {
    batchMessage.textContent = `${error.message} 正在等待任务状态，未重新请求二维码。`;
    updateActivationControls();
    state.activationPollTimer = window.setTimeout(pollActivationJob, 1500);
  }
}

function setActivationProgress(percent, message) {
  const panel = $("activation-progress");
  const bar = $("activation-progress-bar");
  const label = $("activation-progress-message");
  const valueLabel = $("activation-progress-percent");
  if (!panel || !bar || !label || !valueLabel) return;
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  panel.hidden = false;
  bar.style.width = `${value}%`;
  valueLabel.textContent = `${Math.round(value)}%`;
  label.textContent = message;
}

function renderActivationResults(results) {
  const container = $("activation-results");
  if (!container) return;
  container.replaceChildren();
  if (!Array.isArray(results) || results.length === 0) return;
  const heading = document.createElement("strong");
  heading.textContent = `激活数据结果：${results.filter((item) => item.state === "ready").length} 张已生成二维码`;
  container.append(heading);
  results.forEach((result) => {
    const item = document.createElement("details");
    item.className = `activation-item activation-${result.state || "unknown"}`;
    const summary = document.createElement("summary");
    summary.textContent = `${result.sequence || "-"}. ${result.iccid || "未知 ICCID"} · ${activationStateLabel(result.state)}`;
    item.append(summary);
    if (result.state === "ready") {
      const code = document.createElement("code");
      code.className = "lpa-value";
      code.textContent = result.activation_code || "LPA 未返回";
      const qrLink = document.createElement("a");
      qrLink.href = result.qr_url || "#";
      qrLink.target = "_blank";
      qrLink.rel = "noreferrer";
      qrLink.textContent = "打开二维码 PNG";
      const image = document.createElement("img");
      image.className = "qr-preview";
      image.loading = "lazy";
      image.alt = `${result.iccid || "此卡"} 的 eSIM 二维码`;
      image.src = result.qr_url || "";
      item.append(code, qrLink, image);
    } else {
      const error = document.createElement("span");
      error.textContent = result.error || "未获取成功，未重试。";
      item.append(error);
    }
    container.append(item);
  });
}

function activationStateLabel(stateValue) {
  if (stateValue === "ready") return "已生成二维码";
  if (stateValue === "unknown") return "结果未知，已停止";
  if (stateValue === "skipped") return "已跳过";
  return "获取失败";
}

function chip(label, kind) {
  const span = document.createElement("span");
  span.className = `status-chip status-${kind}`;
  span.textContent = label;
  return span;
}

function profileLabel(raw) {
  if (raw === "INSTALLED") return "已安装（INSTALLED）";
  if (raw === "RELEASED") return "已释放待下载（RELEASED）";
  return raw ? `原始值：${raw}` : "未返回状态";
}

function profileClass(raw) {
  if (raw === "INSTALLED") return "installed";
  if (raw === "RELEASED") return "released";
  return "raw";
}

function outputLabel(jsonPath, csvPath) {
  const name = (value) => value ? value.split(/[\\/]/).pop() : "";
  const values = [name(jsonPath), name(csvPath)].filter(Boolean);
  return values.length ? `已导出：${values.join("、")}` : "";
}

function setHistoryAccount(account) {
  const value = String(account || "").trim();
  historyAccount.textContent = value ? `账号：${value}` : "";
  historyAccount.hidden = !value;
}

function formatHistoryTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value);
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "medium",
  }).format(date);
}

renderSummary();
renderRows();
