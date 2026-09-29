const state = {
  jobId: null,
  rows: [],
  querySummary: null,
  filteredRows: [],
  selectedIndex: null,
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
$("prepare-installation").addEventListener("click", () => {
  if (state.jobId || state.loadingHistory) return;
  const rows = state.filteredRows.filter((row) => row.status === "USED" && row.esimProfileStatus === "RELEASED" && !isQueryFailure(row));
  if (!rows.length || rows.length > 200) {
    setFormMessage("请筛选 1—200 张 USED + RELEASED 卡；也可直接进入手动写入资料页粘贴 ICCID。");
    return;
  }
  sessionStorage.setItem("installation-iccids", JSON.stringify(rows.map((row) => row.iccid)));
  location.href = "/installation.html";
});
form.addEventListener("submit", startQuery);
cancelButton.addEventListener("click", cancelQuery);
loadHistoryButton.addEventListener("click", loadLatestHistory);
closeHistoryButton.addEventListener("click", closeHistory);
historyOverlay.addEventListener("click", closeHistory);
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !historyPanel.hidden) closeHistory();
});

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
    setFormMessage("请输入至少一个 ICCID，或切换为全部已开卡 eSIM。");
    iccids.focus();
    return;
  }
  setBusy(true);
  state.rows = [];
  state.querySummary = null;
  state.selectedIndex = null;
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
      state.querySummary = deriveQuerySummary(openedRows(state.rows));
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
    meta.textContent = `${item.account || "未知账号"} · USED ${item.count || 0} 张${statuses ? ` · ${statuses}` : ""}`;
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
    state.querySummary = deriveQuerySummary(openedRows(state.rows));
    state.selectedIndex = null;
    setHistoryAccount(data.account);
    search.value = "";
    statusFilter.value = "all";
    detailLabel.textContent = "未选择记录";
    detailContent.textContent = "单击一条记录查看原始接口字段。";
    renderSummary();
    renderRows();
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
      : `${loadedLabel} 结果不完整，不能用于核对写入状态。`;
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
}

function setProgress(percent, message) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  progressPanel.hidden = false;
  progressBar.style.width = `${value}%`;
  progressPercent.textContent = `${Math.round(value)}%`;
  progressMessage.textContent = message;
}

function openedRows(rows = state.rows) {
  return rows.filter((row) => row && row.status === "USED");
}

function isQueryFailure(row) {
  return row.esimProfileStatusQueryStatus === "failed"
    || !Object.prototype.hasOwnProperty.call(row, "esimProfileStatus");
}

function isProfileIssue(row) {
  return !isQueryFailure(row)
    && !["INSTALLED", "RELEASED"].includes(row.esimProfileStatus);
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
  if (summary.complete) return `USED 卡 Profile 查询完整：成功 ${summary.succeeded}/${summary.total}，结果已保存。`;
  return `查询结束但结果不完整：成功 ${summary.succeeded}/${summary.total}，失败 ${summary.failed}。不能用于核对写入状态。`;
}

function renderSummary() {
  const rows = openedRows();
  $("metric-total").textContent = rows.length;
  $("metric-installed").textContent = rows.filter((row) => row.esimProfileStatus === "INSTALLED").length;
  $("metric-released").textContent = rows.filter((row) => row.esimProfileStatus === "RELEASED").length;
  $("metric-error").textContent = rows.filter(isProfileIssue).length;
  $("metric-failed").textContent = rows.filter(isQueryFailure).length;
  const summary = deriveQuerySummary(rows);
  state.querySummary = summary;
  const quality = $("query-quality");
  quality.hidden = summary.total === 0;
  quality.className = `query-quality ${summary.complete ? "query-quality-complete" : "query-quality-incomplete"}`;
  $("query-quality-title").textContent = summary.complete
    ? "USED 卡 Profile 查询完整，可用于核对写入状态"
    : "结果不完整，不能用于核对写入状态";
  const retryText = summary.retried_records > 0
    ? ` · 重试 ${summary.retried_records} 条，恢复 ${summary.recovered_after_retry} 条`
    : "";
  const topFailure = Object.entries(summary.failure_reasons || {})
    .sort((left, right) => Number(right[1]) - Number(left[1]))[0];
  const failureText = topFailure ? ` · 主要失败：${topFailure[0]} × ${topFailure[1]}` : "";
  $("query-quality-detail").textContent = `成功 ${summary.succeeded}/${summary.total} · 失败 ${summary.failed}${retryText}${failureText}`;
  $("query-quality-rate").textContent = `完整度 ${formatRate(summary.success_rate)}`;
}

function renderRows() {
  const needle = search.value.trim().toLowerCase();
  const selected = statusFilter.value;
  const rows = openedRows();
  state.filteredRows = rows.filter((row) => {
    const matchesSearch = !needle || JSON.stringify(row).toLowerCase().includes(needle);
    const matchesFilter = selected === "all"
      || (selected === "failed" && isQueryFailure(row))
      || (selected === "issue" && isProfileIssue(row))
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
  $("record-count").textContent = `${state.filteredRows.length} / ${rows.length} 张 USED 卡`;
  exportIccids.disabled = state.filteredRows.length === 0;
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
  const failed = isQueryFailure(row);
  const statusCell = failed ? chip("查询失败", "failed") : chip(profileLabel(raw), profileClass(raw));
  const writeState = writeConclusion(row);
  const values = [row.iccid || "", null, null,
    row.esimProfileStatusUpdatedAt || "", failed ? "失败" : (row.esimProfileStatusQueryStatus === "ok" ? "成功" : ""),
    row.esimProfileStatusQueryAttempts || 1];
  values.forEach((value, cellIndex) => {
    const td = document.createElement("td");
    if (cellIndex === 1) td.append(statusCell);
    else if (cellIndex === 2) td.append(chip(writeState.label, writeState.kind));
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

function chip(label, kind) {
  const span = document.createElement("span");
  span.className = `status-chip status-${kind}`;
  span.textContent = label;
  return span;
}

function profileLabel(raw) {
  return raw || "未返回状态";
}

function profileClass(raw) {
  if (raw === "INSTALLED") return "installed";
  if (raw === "RELEASED") return "released";
  if (["ERROR", "UNAVAILABLE"].includes(raw)) return "failed";
  return "raw";
}

function writeConclusion(row) {
  if (isQueryFailure(row)) return { label: "无法判断", kind: "failed" };
  if (row.esimProfileStatus === "INSTALLED") return { label: "已写入", kind: "installed" };
  if (row.esimProfileStatus === "RELEASED") return { label: "未写入", kind: "released" };
  if (row.esimProfileStatus === "ERROR") return { label: "Profile 异常", kind: "failed" };
  if (row.esimProfileStatus === "UNAVAILABLE") return { label: "状态不可用", kind: "failed" };
  return { label: "未知状态", kind: "raw" };
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
const recheckIccids = sessionStorage.getItem("profile-recheck-iccids");
if (recheckIccids) {
  sessionStorage.removeItem("profile-recheck-iccids");
  try {
    iccids.value = JSON.parse(recheckIccids).join("\n");
    const radio = document.querySelector('input[name="scope"][value="selected"]');
    radio.checked = true;
    radio.dispatchEvent(new Event("change"));
    setFormMessage("已带入安装批次 ICCID，请输入账号密码后查询；不会自动请求平台。");
  } catch { setFormMessage("ICCID 导入失败，请重新填写。"); }
}
