const $ = (id) => document.getElementById(id);

const state = {
  batches: [],
  batch: null,
  preview: null,
  busy: false,
};

const form = $("activation-form");
const username = $("activation-username");
const password = $("activation-password");
const baseUrl = $("activation-base-url");
const orgId = $("activation-org-id");
const productId = $("activation-product-id");
const maxTotal = $("activation-max-total");
const batchSelect = $("batch-select");
const batchSummary = $("batch-summary");
const previewButton = $("preview-button");
const submitButton = $("submit-button");
const verifyButton = $("verify-button");
const refreshButton = $("refresh-batches");
const workflowState = $("workflow-state");
const message = $("activation-message");
const previewResult = $("preview-result");
const verificationResult = $("verification-result");
const progressPanel = $("platform-progress");
const progressBar = $("platform-progress-bar");
const progressMessage = $("platform-progress-message");
const progressPercent = $("platform-progress-percent");

form.addEventListener("submit", (event) => event.preventDefault());
batchSelect.addEventListener("change", selectBatch);
refreshButton.addEventListener("click", loadBatches);
previewButton.addEventListener("click", () => startJob("preview"));
submitButton.addEventListener("click", confirmAndSubmit);
verifyButton.addEventListener("click", () => startJob("verify"));

function setMessage(value = "") {
  message.textContent = value;
}

function setWorkflow(value, tone = "neutral") {
  workflowState.textContent = value;
  workflowState.className = `badge badge-${tone}`;
}

function credentialsReady() {
  return Boolean(username.value.trim() && password.value && baseUrl.value.trim());
}

function numericConfigReady() {
  return positiveSafeInteger(orgId.value) && positiveSafeInteger(productId.value);
}

function positiveSafeInteger(value) {
  const normalized = value.trim();
  const parsed = Number(normalized);
  return /^[1-9]\d*$/.test(normalized) && Number.isSafeInteger(parsed) && parsed > 0;
}

function amountReady() {
  const value = maxTotal.value.trim();
  return /^\d+(?:\.\d{1,2})?$/.test(value) && Number.isFinite(Number(value));
}

function selectedBatchReady() {
  return Boolean(state.batch);
}

function updateButtons() {
  const locked = state.busy;
  const baseReady = credentialsReady() && numericConfigReady() && selectedBatchReady();
  const submitReady = baseReady && amountReady();
  const hasSubmissionIntent = Boolean(state.batch?.platform_activation?.has_submission_intent);
  previewButton.disabled = locked || !submitReady || Boolean(state.preview) || hasSubmissionIntent;
  submitButton.disabled = locked || !submitReady || !state.preview || hasSubmissionIntent;
  verifyButton.disabled = locked || !baseReady || !hasSubmissionIntent;
  refreshButton.disabled = locked;
  batchSelect.disabled = locked;
}

function renderBatchOptions() {
  batchSelect.replaceChildren();
  if (!state.batches.length) {
    const empty = new Option("没有可用批次", "");
    batchSelect.append(empty);
    batchSelect.disabled = true;
    return;
  }
  const placeholder = new Option("选择一个批次……", "");
  batchSelect.append(placeholder);
  state.batches.forEach((item) => {
    const platformState = item.platform_activation?.state || "not_started";
    const label = `${item.batch_id} · ${item.count} 张 · ${item.stage || item.state} · ${platformStateLabel(platformState)}`;
    const option = new Option(label, item.batch_id);
    option.disabled = item.qr_operations !== "complete";
    batchSelect.append(option);
  });
  batchSelect.disabled = false;
}

async function loadBatches() {
  setMessage("");
  state.batch = null;
  state.preview = null;
  previewResult.hidden = true;
  verificationResult.hidden = true;
  setWorkflow("等待选择批次");
  try {
    const response = await fetch("/api/write-batches", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "批次读取失败。");
    state.batches = Array.isArray(data.items) ? data.items : [];
    renderBatchOptions();
    batchSummary.textContent = state.batches.length ? "请选择一个批次。" : "还没有写卡准备批次。";
  } catch (error) {
    state.batches = [];
    renderBatchOptions();
    batchSummary.textContent = error.message;
  }
  updateButtons();
}

async function selectBatch() {
  const batchId = batchSelect.value;
  state.batch = null;
  state.preview = null;
  previewResult.hidden = true;
  verificationResult.hidden = true;
  if (!batchId) {
    batchSummary.textContent = "请选择一个批次。";
    setWorkflow("等待选择批次");
    updateButtons();
    return;
  }
  batchSummary.textContent = "正在读取批次详情……";
  try {
    const response = await fetch(`/api/write-batches/${encodeURIComponent(batchId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "批次读取失败。");
    state.batch = data;
    const records = Array.isArray(data.records) ? data.records : [];
    const ready = records.filter((record) => record.qr_status === "saved").length;
    const platformState = data.platform_activation?.state || "not_started";
    batchSummary.textContent = `${data.batch_id} · ${records.length} 张 · 激活数据 ${ready}/${records.length} 张 · ${platformStateLabel(platformState)}。`;
    setWorkflow(
      data.platform_activation?.has_submission_intent ? "只允许结果核验" : "可进行只读预检",
      "blue",
    );
    renderSavedVerification(data.platform_activation);
  } catch (error) {
    batchSummary.textContent = error.message;
    setWorkflow("批次不可用", "neutral");
  }
  updateButtons();
}

function payload() {
  return {
    batch_id: state.batch?.batch_id || "",
    username: username.value.trim(),
    password: password.value,
    base_url: baseUrl.value.trim(),
    org_id: Number.parseInt(orgId.value, 10),
    product_id: Number.parseInt(productId.value, 10),
    max_total: maxTotal.value.trim(),
  };
}

async function confirmAndSubmit() {
  if (!state.preview) return;
  const confirmed = window.confirm(
    `即将提交 ${state.preview.count} 张卡的平台开户激活请求。\n` +
    `产品：${state.preview.product_name || state.preview.product_code || "未知"}\n` +
    `总金额：${state.preview.total}\n\n` +
    "这是一次性提交；如果响应未知，系统不会重发。确认继续吗？",
  );
  if (!confirmed) return;
  await startJob("submit");
}

async function startJob(action) {
  if (state.busy || !selectedBatchReady()) return;
  if (!credentialsReady() || !numericConfigReady() || (action !== "verify" && !amountReady())) {
    setMessage(action === "verify"
      ? "请先填写账号、密码、组织 ID 和产品 ID。"
      : "请先填写账号、密码、组织 ID、产品 ID 和金额上限。");
    return;
  }
  state.busy = true;
  if (action === "preview") {
    state.preview = null;
    previewResult.hidden = true;
    setWorkflow("预检进行中", "blue");
  } else if (action === "submit") {
    setWorkflow("提交进行中", "blue");
  } else {
    setWorkflow("核验进行中", "blue");
  }
  setMessage("");
  showProgress(0, action === "preview" ? "准备只读预检……" : action === "submit" ? "准备提交……" : "准备核验……");
  updateButtons();
  try {
    const response = await fetch(`/api/platform-activation/${action}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload()),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户任务创建失败。");
    const jobId = data.job_id;
    await pollJob(jobId, action);
  } catch (error) {
    setMessage(error.message);
    setWorkflow("任务未完成", "neutral");
  } finally {
    if (action !== "preview") await refreshSelectedBatchMetadata();
    state.busy = false;
    updateButtons();
  }
}

async function refreshSelectedBatchMetadata() {
  const batchId = state.batch?.batch_id;
  if (!batchId) return;
  try {
    const response = await fetch(`/api/write-batches/${encodeURIComponent(batchId)}`, { cache: "no-store" });
    const data = await response.json();
    if (response.ok) state.batch = data;
  } catch {
    // The task result remains visible; the next manual refresh can reload metadata.
  }
}

function invalidatePreview() {
  if (state.preview && !state.busy) {
    state.preview = null;
    previewResult.hidden = true;
    setWorkflow(state.batch ? "参数已改变，请重新预检" : "等待选择批次", "neutral");
    setMessage("业务参数已改变，原预检结果已失效。");
  }
  updateButtons();
}

[username, password, baseUrl, orgId, productId, maxTotal].forEach((field) => {
  field.addEventListener("input", invalidatePreview);
});

async function pollJob(jobId, action) {
  while (true) {
    const response = await fetch(`/api/platform-activation-jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户任务不存在。");
    showProgress(data.progress || 0, data.message || "任务处理中……");
    if (data.state === "done") {
      renderJobResult(data, action);
      return;
    }
    if (data.state === "failed") {
      throw new Error(data.error || data.message || "平台开户任务未完成。");
    }
    await new Promise((resolve) => setTimeout(resolve, 700));
  }
}

function showProgress(percent, text) {
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  progressPanel.hidden = false;
  progressBar.style.width = `${value}%`;
  progressPercent.textContent = `${Math.round(value)}%`;
  progressMessage.textContent = text;
}

function renderJobResult(data, action) {
  if (action === "preview") {
    state.preview = data.result || data.preview || {};
    const result = state.preview;
    previewResult.hidden = false;
    previewResult.innerHTML = "";
    const title = document.createElement("strong");
    title.textContent = "只读预检通过";
    const body = document.createElement("span");
    body.textContent = `批次 ${result.batch_id} · ${result.count} 张 · 单价 ${result.unit_price} · 总金额 ${result.total} · 激活余额 ${result.activation_balance}`;
    previewResult.append(title, body);
    setWorkflow("预检通过，等待人工提交", "blue");
    setMessage("预检只读取平台信息，尚未提交开户激活。");
  } else if (action === "submit") {
    const result = data.result || {};
    setWorkflow("已提交，等待核验", "blue");
    setMessage(result.job ? "平台任务已返回；请稍后执行结果核验。" : "平台已返回提交结果；请执行结果核验。");
  } else {
    const result = data.result || {};
    verificationResult.hidden = false;
    verificationResult.innerHTML = "";
    const title = document.createElement("strong");
    title.textContent = `核验结果：${result.confirmed || 0}/${result.count || 0} 张确认成功`;
    const body = document.createElement("span");
    body.textContent = result.all_confirmed ? "全部满足订单、号码和信号成功条件。" : "仍有未满足全部条件的卡，请查看核验 CSV。";
    const actions = document.createElement("div");
    actions.className = "verification-actions";
    if (result.verification_csv_url) {
      const download = document.createElement("a");
      download.className = "button button-secondary verification-download";
      download.href = result.verification_csv_url;
      download.textContent = "下载核验 CSV";
      download.setAttribute("download", "");
      actions.append(download);
    }
    verificationResult.append(title, body, actions);
    renderVerificationTable(result.rows);
    setWorkflow(result.all_confirmed ? "核验完成" : "核验部分完成", result.all_confirmed ? "blue" : "neutral");
    setMessage("核验只读取订单和号码状态，没有重新提交激活。");
  }
}

function renderVerificationTable(rows) {
  if (!Array.isArray(rows) || !rows.length) return;
  const wrapper = document.createElement("div");
  wrapper.className = "verification-table-wrap";
  const table = document.createElement("table");
  table.className = "verification-table";
  const head = document.createElement("thead");
  const headerRow = document.createElement("tr");
  ["序号", "ICCID", "结果", "订单号", "手机号", "订单", "号码", "信号", "说明"].forEach((label) => {
    const cell = document.createElement("th");
    cell.scope = "col";
    cell.textContent = label;
    headerRow.append(cell);
  });
  head.append(headerRow);
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const line = document.createElement("tr");
    line.className = row.state === "verified" ? "verification-success" : "verification-pending";
    [
      row.sequence,
      row.iccid,
      row.state === "verified" ? "已确认" : "待确认",
      row.order_no,
      row.phone_number,
      row.order_status,
      row.subscriber_status,
      row.signal_status,
      row.message,
    ].forEach((value) => {
      const cell = document.createElement("td");
      cell.textContent = value ?? "";
      line.append(cell);
    });
    body.append(line);
  });
  table.append(head, body);
  wrapper.append(table);
  verificationResult.append(wrapper);
}

function renderSavedVerification(platformState) {
  if (!platformState?.has_verification || !platformState.verification_csv_url) return;
  verificationResult.hidden = false;
  verificationResult.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "已有本地核验结果";
  const body = document.createElement("span");
  body.textContent = "可直接下载上一次保存的逐卡核验 CSV。";
  const actions = document.createElement("div");
  actions.className = "verification-actions";
  const download = document.createElement("a");
  download.className = "button button-secondary verification-download";
  download.href = platformState.verification_csv_url;
  download.textContent = "下载核验 CSV";
  download.setAttribute("download", "");
  actions.append(download);
  verificationResult.append(title, body, actions);
}

function platformStateLabel(value) {
  return ({
    not_started: "未提交平台开户",
    submission_attempted: "提交结果待核验",
    unknown: "提交结果未知",
    submitted: "已提交待核验",
    verified: "已有核验结果",
  })[value] || `平台状态 ${value}`;
}

loadBatches();
updateButtons();
