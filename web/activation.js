const $ = (id) => document.getElementById(id);

const state = {
  inventory: [],
  selectedInventoryIds: new Set(),
  inventoryJobId: null,
  batches: [],
  batch: null,
  qrJobId: null,
  platformJobBusy: false,
  preview: null,
};

const form = $("activation-form");
const username = $("activation-username");
const password = $("activation-password");
const baseUrl = $("activation-base-url");
const orgId = $("activation-org-id");
const productId = $("activation-product-id");
const maxTotal = $("activation-max-total");
const inventoryBody = $("inventory-body");
const inventoryTarget = $("inventory-target");
const inventorySummary = $("inventory-summary");
const inventorySelection = $("inventory-selection");
const inventoryMessage = $("inventory-message");
const loadInventoryButton = $("load-inventory");
const autoSelectInventoryButton = $("auto-select-inventory");
const clearInventoryButton = $("clear-inventory-selection");
const createBatchButton = $("create-platform-batch");
const batchSelect = $("platform-batch-select");
const batchSummary = $("platform-batch-summary");
const refreshBatchesButton = $("refresh-platform-batches");
const downloadBatch = $("download-platform-batch");
const fetchQrButton = $("fetch-platform-qr");
const cancelQrButton = $("cancel-platform-qr");
const batchMessage = $("batch-message");
const qrResults = $("qr-results");
const previewButton = $("preview-button");
const submitButton = $("submit-button");
const verifyButton = $("verify-button");
const workflowState = $("workflow-state");
const previewResult = $("preview-result");
const verificationResult = $("verification-result");

form.addEventListener("submit", (event) => event.preventDefault());
loadInventoryButton.addEventListener("click", startInventoryJob);
autoSelectInventoryButton.addEventListener("click", autoSelectInventory);
clearInventoryButton.addEventListener("click", clearInventorySelection);
createBatchButton.addEventListener("click", createPlatformBatch);
refreshBatchesButton.addEventListener("click", loadPlatformBatches);
batchSelect.addEventListener("change", selectPlatformBatch);
fetchQrButton.addEventListener("click", confirmAndFetchQr);
cancelQrButton.addEventListener("click", cancelQrJob);
previewButton.addEventListener("click", () => startPlatformJob("preview"));
submitButton.addEventListener("click", confirmAndSubmit);
verifyButton.addEventListener("click", () => startPlatformJob("verify"));
[username, password, baseUrl, orgId, productId, maxTotal].forEach((field) => {
  field.addEventListener("input", () => {
    if (state.preview && !state.platformJobBusy) {
      state.preview = null;
      previewResult.hidden = true;
      setWorkflow(state.batch ? "参数已改变，请重新预检" : "等待二维码准备", "neutral");
      setMessage("业务参数已改变，原预检结果已失效。");
    }
    updateControls();
  });
});
[username, baseUrl, orgId, productId].forEach((field) => {
  field.addEventListener("input", () => {
    if (!state.inventory.length) return;
    state.inventory = [];
    state.selectedInventoryIds.clear();
    inventorySummary.textContent = "账号或业务参数已改变，请重新读取平台库存。";
    inventoryMessage.textContent = "旧库存选择已清除。";
    renderInventory();
  });
});

function setMessage(value = "") { $("activation-message").textContent = value; }

function setWorkflow(value, tone = "neutral") {
  workflowState.textContent = value;
  workflowState.className = `badge badge-${tone}`;
}

function credentialsReady() { return Boolean(username.value.trim() && password.value && baseUrl.value.trim()); }

function positiveInteger(value) {
  const normalized = String(value || "").trim();
  return /^[1-9]\d*$/.test(normalized) && Number.isSafeInteger(Number(normalized));
}

function businessConfigReady() { return positiveInteger(orgId.value) && positiveInteger(productId.value); }

function amountReady() { return /^\d+(?:\.\d{1,2})?$/.test(maxTotal.value.trim()); }

function selectedRows() {
  const available = new Set(state.inventory.filter((row) => row.eligible).map((row) => row.inventory_id));
  return state.inventory.filter((row) => available.has(row.inventory_id) && state.selectedInventoryIds.has(row.inventory_id));
}

function selectedBatchReady() { return Boolean(state.batch && Array.isArray(state.batch.records)); }

function batchQrReady() {
  return selectedBatchReady() && state.batch.records.every((record) => record.qr_status === "saved");
}

function setProgress(prefix, percent, message) {
  const panel = $(`${prefix}-progress`);
  const bar = $(`${prefix}-progress-bar`);
  const label = $(`${prefix}-progress-message`);
  const valueLabel = $(`${prefix}-progress-percent`);
  if (!panel || !bar || !label || !valueLabel) return;
  const value = Math.max(0, Math.min(100, Number(percent) || 0));
  panel.hidden = false;
  bar.style.width = `${value}%`;
  const track = bar.parentElement;
  if (track) {
    track.setAttribute("aria-valuemin", "0");
    track.setAttribute("aria-valuemax", "100");
    track.setAttribute("aria-valuenow", String(Math.round(value)));
  }
  valueLabel.textContent = `${Math.round(value)}%`;
  label.textContent = message;
}

function updateControls() {
  const credentials = credentialsReady();
  const config = businessConfigReady();
  const rows = selectedRows();
  const busy = Boolean(state.inventoryJobId || state.qrJobId || state.platformJobBusy);
  username.disabled = busy;
  password.disabled = busy;
  baseUrl.disabled = busy;
  orgId.disabled = busy || selectedBatchReady();
  productId.disabled = busy || selectedBatchReady();
  maxTotal.disabled = busy;
  loadInventoryButton.disabled = busy || !credentials || !config;
  autoSelectInventoryButton.disabled = busy || !state.inventory.some((row) => row.eligible);
  clearInventoryButton.disabled = busy || rows.length === 0;
  createBatchButton.disabled = busy || rows.length === 0 || rows.length > 200;
  fetchQrButton.disabled = busy || !selectedBatchReady() || state.batch.qr_operations !== "none";
  cancelQrButton.disabled = !state.qrJobId;
  const activationReady = !busy && batchQrReady() && credentials && config;
  const hasSubmissionIntent = Boolean(state.batch?.platform_activation?.has_submission_intent);
  const previewReady = activationReady && amountReady();
  previewButton.disabled = !previewReady || Boolean(state.preview) || hasSubmissionIntent;
  submitButton.disabled = !previewReady || !state.preview || hasSubmissionIntent;
  verifyButton.disabled = !activationReady || !hasSubmissionIntent;
  refreshBatchesButton.disabled = busy;
  batchSelect.disabled = busy;
}

async function startInventoryJob() {
  if (state.inventoryJobId || !credentialsReady() || !businessConfigReady()) return;
  state.inventory = [];
  state.selectedInventoryIds.clear();
  renderInventory();
  inventoryMessage.textContent = "正在创建库存读取任务……";
  setProgress("inventory", 0, "准备读取平台库存……");
  updateControls();
  try {
    const response = await fetch("/api/platform-inventory-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(), password: password.value, base_url: baseUrl.value.trim(),
        org_id: Number.parseInt(orgId.value, 10), product_id: Number.parseInt(productId.value, 10),
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "库存任务创建失败。");
    state.inventoryJobId = data.job_id;
    await pollInventoryJob();
  } catch (error) {
    inventoryMessage.textContent = error.message;
  } finally {
    state.inventoryJobId = null;
    updateControls();
  }
}

async function pollInventoryJob() {
  while (state.inventoryJobId) {
    const response = await fetch(`/api/platform-inventory-jobs/${encodeURIComponent(state.inventoryJobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "库存任务不存在。");
    setProgress("inventory", data.progress || 0, data.message || "正在读取库存……");
    if (data.state === "done") {
      const result = data.result || {};
      state.inventory = Array.isArray(result.rows) ? result.rows : [];
      inventorySummary.textContent = `组织 ${result.org_id} · 产品 ${result.product_id} · 总库存 ${result.count} 张 · 可开户 ${result.eligible_count} 张 · 已阻止 ${result.blocked_count} 张。`;
      inventoryMessage.textContent = "库存读取只使用平台库存字段，没有查询 Profile 状态。";
      renderInventory();
      return;
    }
    if (data.state === "failed") throw new Error(data.error || data.message || "库存读取失败。");
    await delay(450);
  }
}

function renderInventory() {
  inventoryBody.replaceChildren();
  if (!state.inventory.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 5;
    cell.className = "platform-empty";
    cell.textContent = "没有可展示的平台库存";
    row.append(cell);
    inventoryBody.append(row);
  } else {
    state.inventory.forEach((item) => {
      const row = document.createElement("tr");
      const selector = document.createElement("input");
      selector.type = "checkbox";
      selector.checked = item.eligible && state.selectedInventoryIds.has(item.inventory_id);
      selector.disabled = !item.eligible || Boolean(state.inventoryJobId || state.qrJobId || state.platformJobBusy);
      selector.setAttribute("aria-label", `选择 ${item.iccid || "此卡"}`);
      selector.addEventListener("change", () => {
        if (selector.checked) state.selectedInventoryIds.add(item.inventory_id);
        else state.selectedInventoryIds.delete(item.inventory_id);
        renderInventory();
      });
      const cells = [selector, item.iccid || "", `${item.inventory_status || "-"} / ${item.sim_type || "-"}`, `${item.qr_view_count ?? "-"} / ${item.qr_view_limit ?? "-"}（剩余 ${item.qr_remaining ?? "-"}）`, item.eligible ? "可加入" : item.blocked_reason || "已阻止"];
      cells.forEach((value, index) => {
        const cell = document.createElement("td");
        if (index === 0) cell.append(value);
        else cell.textContent = value;
        if (index === 4) cell.className = item.eligible ? "inventory-eligible" : "inventory-blocked";
        row.append(cell);
      });
      inventoryBody.append(row);
    });
  }
  const rows = selectedRows();
  inventorySelection.textContent = `已选择 ${rows.length} 张；可开户 ${state.inventory.filter((row) => row.eligible).length} 张；单批最多 200 张。`;
  updateControls();
}

function autoSelectInventory() {
  const target = Math.max(1, Math.min(200, Number.parseInt(inventoryTarget.value, 10) || 1));
  inventoryTarget.value = String(target);
  state.selectedInventoryIds = new Set(state.inventory.filter((row) => row.eligible).slice(0, target).map((row) => row.inventory_id));
  renderInventory();
}

function clearInventorySelection() {
  state.selectedInventoryIds.clear();
  renderInventory();
}

async function createPlatformBatch() {
  const rows = selectedRows();
  if (!rows.length || rows.length > 200) return;
  inventoryMessage.textContent = "正在保存平台库存批次……";
  createBatchButton.disabled = true;
  try {
    const response = await fetch("/api/platform-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ org_id: Number.parseInt(orgId.value, 10), product_id: Number.parseInt(productId.value, 10), rows }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户批次创建失败。");
    state.selectedInventoryIds.clear();
    inventoryMessage.textContent = `平台开户批次已建立：${data.batch_id}。未获取二维码，未提交开户。`;
    await loadPlatformBatches(data.batch_id);
  } catch (error) {
    inventoryMessage.textContent = error.message;
  } finally {
    updateControls();
  }
}

async function loadPlatformBatches(selectId = "") {
  try {
    const response = await fetch("/api/platform-batches", { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户批次读取失败。");
    state.batches = Array.isArray(data.items) ? data.items : [];
    batchSelect.replaceChildren();
    if (!state.batches.length) batchSelect.append(new Option("还没有平台批次", ""));
    else {
      batchSelect.append(new Option("选择一个平台开户批次……", ""));
      state.batches.forEach((item) => {
        const qr = `${item.qr_ready_count || 0}/${item.count || 0}`;
        batchSelect.append(new Option(`${item.batch_id} · ${item.count} 张 · 二维码 ${qr} · ${platformBatchStateLabel(item.state)}`, item.batch_id));
      });
      if (selectId) batchSelect.value = selectId;
    }
    if (batchSelect.value) await selectPlatformBatch();
    else {
      state.batch = null;
      batchSummary.textContent = state.batches.length ? "请选择一个平台批次。" : "还没有平台库存批次。";
      renderQrResults([]);
      updateControls();
    }
  } catch (error) {
    batchSummary.textContent = error.message;
  }
}

async function selectPlatformBatch() {
  const batchId = batchSelect.value;
  state.preview = null;
  previewResult.hidden = true;
  verificationResult.hidden = true;
  if (!batchId) {
    state.batch = null;
    batchSummary.textContent = "请选择一个平台批次。";
    setWorkflow("等待二维码准备");
    updateControls();
    return;
  }
  batchSummary.textContent = "正在读取平台批次详情……";
  try {
    const response = await fetch(`/api/platform-batches/${encodeURIComponent(batchId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户批次读取失败。");
    state.batch = data;
    orgId.value = String(data.org_id || "");
    productId.value = String(data.product_id || "");
    const records = Array.isArray(data.records) ? data.records : [];
    const ready = records.filter((record) => record.qr_status === "saved").length;
    batchSummary.textContent = `${data.batch_id} · ${records.length} 张 · 二维码 ${ready}/${records.length} 张 · ${platformBatchStateLabel(data.state)}。`;
    downloadBatch.href = data.csv_download || "#";
    downloadBatch.hidden = !data.csv_download;
    renderQrResults([]);
    if (data.platform_activation?.has_verification) renderSavedVerification(data.platform_activation);
    setWorkflow(ready === records.length ? "可进行只读预检" : "等待二维码准备", ready === records.length ? "blue" : "neutral");
  } catch (error) {
    state.batch = null;
    batchSummary.textContent = error.message;
  }
  updateControls();
}

function confirmAndFetchQr() {
  if (!state.batch || state.qrJobId) return;
  const count = state.batch.records?.length || 0;
  if (!window.confirm(`将对 ${count} 张卡各请求一次二维码接口。查看次数可能被消耗；超时或未知结果不会重试。确认继续吗？`)) return;
  startQrJob();
}

async function startQrJob() {
  state.qrJobId = "starting";
  batchMessage.textContent = "正在创建一次性二维码任务……";
  updateControls();
  try {
    const response = await fetch("/api/platform-qr-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ batch_id: state.batch.batch_id, username: username.value.trim(), password: password.value, base_url: baseUrl.value.trim() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "二维码任务创建失败。");
    state.qrJobId = data.job_id;
    await pollQrJob();
  } catch (error) {
    batchMessage.textContent = error.message;
  } finally {
    state.qrJobId = null;
    await refreshSelectedBatch();
    updateControls();
  }
}

async function pollQrJob() {
  while (state.qrJobId) {
    const response = await fetch(`/api/platform-qr-jobs/${encodeURIComponent(state.qrJobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "二维码任务不存在。");
    setProgress("qr", data.progress || 0, data.message || "正在获取二维码……");
    if (data.state === "done") {
      renderQrResults(Array.isArray(data.results) ? data.results : []);
      batchMessage.textContent = data.message || "二维码任务完成。";
      return;
    }
    if (data.state === "failed" || data.state === "cancelled") throw new Error(data.error || data.message || "二维码任务未完成。");
    await delay(600);
  }
}

async function cancelQrJob() {
  if (!state.qrJobId || state.qrJobId === "starting") return;
  await fetch(`/api/platform-qr-jobs/${encodeURIComponent(state.qrJobId)}/cancel`, { method: "POST" }).catch(() => {});
}

async function refreshSelectedBatch() {
  if (!state.batch?.batch_id) return;
  const response = await fetch(`/api/platform-batches/${encodeURIComponent(state.batch.batch_id)}`, { cache: "no-store" });
  if (response.ok) {
    state.batch = await response.json();
    const ready = state.batch.records.filter((record) => record.qr_status === "saved").length;
    batchSummary.textContent = `${state.batch.batch_id} · ${state.batch.records.length} 张 · 二维码 ${ready}/${state.batch.records.length} 张 · ${platformBatchStateLabel(state.batch.state)}。`;
  }
}

function renderQrResults(results) {
  qrResults.replaceChildren();
  const records = Array.isArray(results) ? results : [];
  if (!records.length) { qrResults.hidden = true; return; }
  qrResults.hidden = false;
  const heading = document.createElement("strong");
  heading.textContent = `二维码任务结果：${records.filter((row) => row.state === "ready").length}/${records.length} 张已保存`;
  qrResults.append(heading);
  records.forEach((result) => {
    const item = document.createElement("details");
    item.className = `activation-item activation-${result.state || "unknown"}`;
    const summary = document.createElement("summary");
    summary.textContent = `${result.sequence || "-"}. ${result.iccid || "未知 ICCID"} · ${qrStateLabel(result.state)}`;
    item.append(summary);
    if (result.state === "ready") {
      const code = document.createElement("code");
      code.className = "lpa-value";
      code.textContent = result.activation_code || "LPA 仅在当前任务中返回";
      const link = document.createElement("a");
      link.href = result.qr_url || "#";
      link.target = "_blank";
      link.rel = "noreferrer";
      link.textContent = "打开二维码 PNG";
      item.append(code, link);
    } else {
      const error = document.createElement("span");
      error.textContent = result.error || "未获取成功，未重试。";
      item.append(error);
    }
    qrResults.append(item);
  });
}

async function confirmAndSubmit() {
  if (!state.preview) return;
  if (!window.confirm(`即将提交 ${state.preview.count} 张卡的平台开户激活请求，总金额 ${state.preview.total}。这是一次性提交，未知结果禁止重发。确认继续吗？`)) return;
  await startPlatformJob("submit");
}

function activationPayload() {
  return { batch_id: state.batch?.batch_id || "", username: username.value.trim(), password: password.value, base_url: baseUrl.value.trim(), org_id: Number.parseInt(orgId.value, 10), product_id: Number.parseInt(productId.value, 10), max_total: maxTotal.value.trim() };
}

async function startPlatformJob(action) {
  if (state.platformJobBusy || !batchQrReady()) return;
  if (!credentialsReady() || !businessConfigReady() || (action !== "verify" && !amountReady())) { setMessage(action === "verify" ? "请先填写账号、密码、组织 ID 和产品 ID。" : "请先填写账号、密码、组织 ID、产品 ID 和金额上限。"); return; }
  state.platformJobBusy = true;
  if (action === "preview") { state.preview = null; previewResult.hidden = true; setWorkflow("预检进行中", "blue"); }
  else if (action === "submit") setWorkflow("提交进行中", "blue");
  else setWorkflow("核验进行中", "blue");
  setProgress("platform", 0, action === "preview" ? "准备只读预检……" : action === "submit" ? "准备提交……" : "准备核验……");
  updateControls();
  try {
    const response = await fetch(`/api/platform-activation/${action}`, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(activationPayload()) });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户任务创建失败。");
    await pollPlatformJob(data.job_id, action);
  } catch (error) {
    setMessage(error.message);
    setWorkflow("任务未完成");
  } finally {
    state.platformJobBusy = false;
    await refreshSelectedBatch();
    updateControls();
  }
}

async function pollPlatformJob(jobId, action) {
  while (true) {
    const response = await fetch(`/api/platform-activation-jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户任务不存在。");
    setProgress("platform", data.progress || 0, data.message || "任务处理中……");
    if (data.state === "done") { renderPlatformJobResult(data, action); return; }
    if (data.state === "failed") throw new Error(data.error || data.message || "平台开户任务未完成。");
    await delay(700);
  }
}

function renderPlatformJobResult(data, action) {
  if (action === "preview") {
    state.preview = data.result || data.preview || {};
    previewResult.hidden = false;
    previewResult.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = "只读预检通过";
    const body = document.createElement("span");
    body.textContent = `批次 ${state.preview.batch_id} · ${state.preview.count} 张 · 单价 ${state.preview.unit_price} · 总金额 ${state.preview.total} · 激活余额 ${state.preview.activation_balance}`;
    previewResult.append(title, body);
    setWorkflow("预检通过，等待人工提交", "blue");
    setMessage("预检只读取平台库存、订单和余额，尚未提交开户激活。");
  } else if (action === "submit") {
    setWorkflow("已提交，等待核验", "blue");
    setMessage("平台已返回提交结果；请执行结果核验。提交结果未知时不要重发。");
  } else {
    const result = data.result || {};
    verificationResult.hidden = false;
    verificationResult.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = `核验结果：${result.confirmed || 0}/${result.count || 0} 张确认成功`;
    const body = document.createElement("span");
    body.textContent = result.all_confirmed ? "订单、号码和信号全部满足成功条件。" : "仍有未满足全部条件的卡，请查看核验 CSV。";
    verificationResult.append(title, body);
    if (result.verification_csv_url) {
      const link = document.createElement("a");
      link.className = "button button-secondary verification-download";
      link.href = result.verification_csv_url;
      link.download = "";
      link.textContent = "下载核验 CSV";
      verificationResult.append(link);
    }
    setWorkflow(result.all_confirmed ? "核验完成" : "核验部分完成", result.all_confirmed ? "blue" : "neutral");
    setMessage("核验只读取订单和号码状态，没有重新提交激活。");
  }
}

function renderSavedVerification(summary) {
  if (!summary?.has_verification || !summary.verification_csv_url) return;
  verificationResult.hidden = false;
  verificationResult.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "已有本地核验结果";
  const link = document.createElement("a");
  link.className = "button button-secondary verification-download";
  link.href = summary.verification_csv_url;
  link.download = "";
  link.textContent = "下载核验 CSV";
  verificationResult.append(title, link);
}

function qrStateLabel(value) { return ({ ready: "已保存", unknown: "结果未知，已停止", skipped: "已跳过", failed: "获取失败" })[value] || "处理中"; }

function platformBatchStateLabel(value) { return ({ inventory_selected: "已选库存", preparing_qr: "二维码准备中", qr_ready: "二维码已准备", qr_partial: "二维码部分完成", qr_failed: "二维码任务失败" })[value] || value || "未开始"; }

function delay(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }

loadPlatformBatches();
updateControls();
