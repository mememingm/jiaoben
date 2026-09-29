const $ = (id) => document.getElementById(id);

const state = {
  context: null,
  contextJobId: null,
  contextId: null,
  historyRevision: 0,
  inventory: [],
  inventoryExpanded: true,
  selectedInventoryIds: new Set(),
  inventoryJobId: null,
  batches: [],
  batch: null,
  qrJobId: null,
  platformJobBusy: false,
  creatingBatch: false,
  submissionAttempts: new Set(),
};

const form = $("activation-form");
const username = $("activation-username");
const password = $("activation-password");
const platformBaseUrl = "https://admin.nexsimus.com";
const orgDisplay = $("activation-org-id");
const productId = $("activation-product-id");
const productDetail = $("activation-product-detail");
const maxTotal = $("activation-max-total");
const loadContextButton = $("load-platform-context");
const accountMessage = $("platform-account-message");
const inventoryBody = $("inventory-body");
const inventoryTarget = $("inventory-target");
const inventorySummary = $("inventory-summary");
const inventorySelection = $("inventory-selection");
const inventoryMessage = $("inventory-message");
const inventoryTableWrap = $("inventory-table-wrap");
const toggleInventoryButton = $("toggle-inventory-list");
const loadInventoryButton = $("load-inventory");
const autoSelectInventoryButton = $("auto-select-inventory");
const clearInventoryButton = $("clear-inventory-selection");
const createBatchButton = $("create-platform-batch");
const batchSelect = $("platform-batch-select");
const batchSummary = $("platform-batch-summary");
const refreshBatchesButton = $("refresh-platform-batches");
const downloadBatch = $("download-platform-batch");
const downloadBatchQrZip = $("download-batch-qr-zip");
const downloadBatchLpaTxt = $("download-batch-lpa-txt");
const fetchQrButton = $("fetch-platform-qr");
const cancelQrButton = $("cancel-platform-qr");
const batchMessage = $("batch-message");
const qrResults = $("qr-results");
const verifyButton = $("verify-button");
const activateButton = $("activate-button");
const workflowState = $("workflow-state");
const verificationResult = $("verification-result");

form.addEventListener("submit", (event) => event.preventDefault());
loadContextButton.addEventListener("click", startContextJob);
loadInventoryButton.addEventListener("click", startInventoryJob);
autoSelectInventoryButton.addEventListener("click", autoSelectInventory);
clearInventoryButton.addEventListener("click", clearInventorySelection);
toggleInventoryButton.addEventListener("click", toggleInventoryList);
createBatchButton.addEventListener("click", createPlatformBatch);
refreshBatchesButton.addEventListener("click", () => loadPlatformBatches(state.batch?.batch_id || ""));
batchSelect.addEventListener("change", selectPlatformBatch);
fetchQrButton.addEventListener("click", confirmAndFetchQr);
cancelQrButton.addEventListener("click", cancelQrJob);
verifyButton.addEventListener("click", checkActivation);
activateButton.addEventListener("click", activateBatch);
[username, password, productId].forEach((field) => {
  field.addEventListener("input", updateControls);
});
[username, password].forEach((field) => {
  field.addEventListener("input", () => {
    state.context = null;
    state.contextId = null;
    clearBatchHistory();
    renderBusinessContext();
    setAccountError();
    $("context-progress").hidden = true;
    if (state.inventory.length) {
      state.inventory = [];
      state.selectedInventoryIds.clear();
      inventorySummary.textContent = "账号已改变，请重新识别账号并选择套餐。";
      inventoryMessage.textContent = "旧库存选择已清除。";
      renderInventory();
    } else {
      updateControls();
    }
  });
});
productId.addEventListener("change", () => {
  renderProductDetail();
  updateControls();
  if (!state.inventory.length) return;
  state.inventory = [];
  state.selectedInventoryIds.clear();
  inventorySummary.textContent = "套餐已改变，请重新读取平台库存。";
  inventoryMessage.textContent = "旧库存选择已清除。";
  renderInventory();
});

function setAccountError(message = "") {
  accountMessage.textContent = message;
  accountMessage.hidden = !message;
}

function setMessage(value = "") { $("activation-message").textContent = value; }

function setWorkflow(value, tone = "neutral") {
  workflowState.textContent = value;
  workflowState.className = `badge badge-${tone}`;
}

function credentialsReady() { return Boolean(username.value.trim() && password.value && platformBaseUrl); }

function positiveInteger(value) {
  const normalized = String(value || "").trim();
  return /^[1-9]\d*$/.test(normalized) && Number.isSafeInteger(Number(normalized));
}

function selectedProductId() {
  return positiveInteger(productId.value) ? Number.parseInt(productId.value, 10) : null;
}

function effectiveOrgId() {
  return state.batch?.org_id || state.context?.org_id || null;
}

function effectiveProductId() {
  return state.batch?.product_id || selectedProductId();
}

function businessConfigReady() {
  if (!positiveInteger(effectiveOrgId()) || !positiveInteger(effectiveProductId())) return false;
  if (!state.batch) return inventoryConfigReady();
  const products = Array.isArray(state.context?.products) ? state.context.products : [];
  return batchMatchesAccount(state.batch)
    && products.some((product) => product.id === state.batch.product_id && product.activation_supported === true);
}

function batchMatchesAccount(batch) {
  return Boolean(batch?.owner && state.context
    && batch.owner.username === state.context.account?.username
    && batch.owner.org_id === state.context.org_id
    && batch.owner.base_url === state.context.base_url);
}

function historyHeaders() { return { "X-Platform-Context": state.contextId || "" }; }

function historyDownloadUrl(url) {
  return `${url}${url.includes("?") ? "&" : "?"}context_id=${encodeURIComponent(state.contextId || "")}`;
}

function clearBatchDetails() {
  state.batch = null;
  verifyButton.textContent = "检查是否已激活";
  verificationResult.hidden = true;
  verificationResult.replaceChildren();
  downloadBatch.hidden = true;
  downloadBatch.removeAttribute("href");
  downloadBatchQrZip.hidden = true;
  downloadBatchQrZip.removeAttribute("href");
  downloadBatchLpaTxt.hidden = true;
  downloadBatchLpaTxt.removeAttribute("href");
  renderQrResults([]);
  $("qr-progress").hidden = true;
  $("platform-progress").hidden = true;
  batchMessage.textContent = "";
  setMessage();
  setWorkflow("等待选择批次");
}

function clearBatchHistory() {
  state.historyRevision += 1;
  state.batches = [];
  clearBatchDetails();
  batchSelect.replaceChildren(new Option("请先识别账号以加载记录", ""));
  batchSummary.textContent = "识别账号后显示该账号的批次记录。";
}

function inventoryConfigReady() {
  return Boolean(state.context && positiveInteger(state.context.org_id)
    && state.context.products?.some((product) => product.id === selectedProductId() && product.activation_supported === true));
}

function selectedRows() {
  const available = new Set(state.inventory.filter((row) => row.eligible).map((row) => row.inventory_id));
  return state.inventory.filter((row) => available.has(row.inventory_id) && state.selectedInventoryIds.has(row.inventory_id));
}

function selectedBatchReady() { return Boolean(state.batch && Array.isArray(state.batch.records)); }

function productLabel(product) {
  const parts = [product.display_code, product.product_name || `套餐 #${product.id}`, product.package_spec].filter(Boolean);
  if (product.display_price !== "" && product.display_price != null && Number.isFinite(Number(product.display_price))) {
    parts.push(`单价 ${Number(product.display_price).toFixed(2)}`);
  }
  return parts.join(" · ");
}

function productOption(product) {
  const supported = product.activation_supported === true;
  const option = new Option(`${productLabel(product)}${supported ? "" : "（暂不支持 A 卡）"}`, String(product.id));
  option.disabled = !supported;
  option.title = option.textContent;
  return option;
}

function renderProductDetail() {
  const product = state.context?.products?.find((item) => item.id === effectiveProductId());
  productDetail.textContent = product ? productLabel(product) : "";
  productDetail.hidden = !product;
  productId.title = productDetail.textContent;
}

function renderBusinessContext() {
  const preferredProductId = effectiveProductId();
  productId.replaceChildren();
  productDetail.textContent = "";
  productDetail.hidden = true;
  productId.title = "";
  if (state.batch) {
    const matches = state.context?.org_id === state.batch.org_id;
    orgDisplay.textContent = state.context
      ? `组织 #${state.batch.org_id}（${matches ? "账号已匹配" : `当前账号为 #${state.context.org_id}，不匹配`}）`
      : `组织 #${state.batch.org_id}（批次锁定，待验证账号）`;
    const known = state.context?.org_id === state.batch.org_id
      ? state.context.products?.find((item) => item.id === state.batch.product_id)
      : null;
    productId.append(known ? productOption(known) : new Option(`批次套餐 #${state.batch.product_id}（当前不可开户）`, String(state.batch.product_id)));
    productId.value = String(state.batch.product_id);
    renderProductDetail();
    return;
  }
  if (!state.context) {
    orgDisplay.textContent = "尚未识别";
    productId.append(new Option("请先识别账号", ""));
    return;
  }
  const account = state.context.account || {};
  const accountName = account.display_name || account.username || "当前账号";
  orgDisplay.textContent = `${accountName} · 组织 #${state.context.org_id}`;
  const products = Array.isArray(state.context.products) ? state.context.products : [];
  if (!products.length) {
    productId.append(new Option("当前组织没有可开户套餐", ""));
    return;
  }
  productId.append(new Option("请选择开户套餐……", ""));
  products.forEach((product) => productId.append(productOption(product)));
  const supported = products.filter((product) => product.activation_supported === true);
  if (supported.some((product) => product.id === preferredProductId)) productId.value = String(preferredProductId);
  else if (supported.length === 1) productId.value = String(supported[0].id);
  renderProductDetail();
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
  const busy = Boolean(state.contextJobId || state.inventoryJobId || state.qrJobId || state.platformJobBusy || state.creatingBatch);
  username.disabled = busy;
  password.disabled = busy;
  maxTotal.disabled = busy;
  loadContextButton.disabled = busy || !credentials;
  productId.disabled = busy || selectedBatchReady() || !state.context;
  loadInventoryButton.disabled = busy || !credentials || !inventoryConfigReady() || selectedBatchReady();
  autoSelectInventoryButton.disabled = busy || !state.inventory.some((row) => row.eligible);
  clearInventoryButton.disabled = busy || rows.length === 0;
  toggleInventoryButton.disabled = busy || state.inventory.length === 0;
  createBatchButton.disabled = busy || rows.length === 0 || rows.length > 200;
  fetchQrButton.disabled = busy || !selectedBatchReady() || !config || state.batch.qr_operations !== "none";
  cancelQrButton.disabled = !state.qrJobId;
  verifyButton.disabled = busy || !selectedBatchReady() || !credentials || !batchMatchesAccount(state.batch);
  activateButton.disabled = busy || !credentials || !config || !canActivateBatch();
  refreshBatchesButton.disabled = busy || !state.context;
  batchSelect.disabled = busy || !state.context;
}

function updateInventoryDisclosure() {
  inventoryTableWrap.hidden = !state.inventoryExpanded;
  toggleInventoryButton.setAttribute("aria-expanded", String(state.inventoryExpanded));
  toggleInventoryButton.textContent = state.inventoryExpanded ? "收起库存" : "展开库存";
  toggleInventoryButton.title = state.inventoryExpanded ? "收起库存列表" : "展开库存列表";
}

function toggleInventoryList() {
  if (!state.inventory.length) return;
  state.inventoryExpanded = !state.inventoryExpanded;
  updateInventoryDisclosure();
}

async function startContextJob() {
  if (state.contextJobId || !credentialsReady()) return;
  state.context = null;
  state.contextId = null;
  clearBatchHistory();
  state.inventory = [];
  state.selectedInventoryIds.clear();
  renderBusinessContext();
  renderInventory();
  state.contextJobId = "starting";
  setAccountError();
  setProgress("context", 0, "准备识别账号……");
  updateControls();
  try {
    const response = await fetch("/api/platform-context-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        username: username.value.trim(),
        password: password.value,
        base_url: platformBaseUrl,
      }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "账号识别任务创建失败。");
    state.contextJobId = data.job_id;
    await pollContextJob();
  } catch (error) {
    setAccountError(error.message);
  } finally {
    state.contextJobId = null;
    $("context-progress").hidden = true;
    updateControls();
  }
}

async function pollContextJob() {
  while (state.contextJobId && state.contextJobId !== "starting") {
    const response = await fetch(`/api/platform-context-jobs/${encodeURIComponent(state.contextJobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "账号识别任务不存在。");
    setProgress("context", data.progress || 0, data.message || "正在识别账号……");
    if (data.state === "done") {
      const result = data.result || {};
      if (!positiveInteger(result.org_id) || !Array.isArray(result.products)) throw new Error("账号识别结果格式不正确。");
      if (!result.base_url || !result.account?.username) throw new Error("后台服务仍是旧版本，请重启本地服务后重新识别账号。");
      if (result.product_catalog_version !== 1) throw new Error("套餐列表已更新，请重启本地服务后重新识别账号。");
      state.context = result;
      state.contextId = state.contextJobId;
      renderBusinessContext();
      await loadPlatformBatches();
      const count = result.products.length;
      if (state.batch && !businessConfigReady()) {
        setAccountError("账号或套餐与当前批次不一致。");
      } else {
        setAccountError();
      }
      if (!state.batch) inventorySummary.textContent = count ? "请选择套餐后读取平台库存。" : "当前组织没有可开户套餐。";
      return;
    }
    if (data.state === "failed") throw new Error(data.error || data.message || "账号识别失败。");
    await delay(350);
  }
}

async function startInventoryJob() {
  if (state.inventoryJobId || !credentialsReady() || !inventoryConfigReady()) return;
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
        username: username.value.trim(), password: password.value, base_url: platformBaseUrl,
        product_id: selectedProductId(),
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
      if (result.org_id !== state.context?.org_id || result.product_id !== selectedProductId()) {
        throw new Error("库存结果与当前账号或套餐不一致，请重新识别账号。");
      }
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
  updateInventoryDisclosure();
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
  if (!rows.length || rows.length > 200 || !state.context || state.creatingBatch) return;
  state.creatingBatch = true;
  inventoryMessage.textContent = "正在保存平台库存批次……";
  updateControls();
  try {
    const response = await fetch("/api/platform-batches", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...historyHeaders() },
      body: JSON.stringify({ org_id: state.context.org_id, product_id: selectedProductId(), rows }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "平台开户批次创建失败。");
    state.selectedInventoryIds.clear();
    inventoryMessage.textContent = `平台开户批次已建立：${data.batch_id}。未获取二维码，未提交开户。`;
    await loadPlatformBatches(data.batch_id);
  } catch (error) {
    inventoryMessage.textContent = error.message;
  } finally {
    state.creatingBatch = false;
    updateControls();
  }
}

async function loadPlatformBatches(selectId = "") {
  if (!state.contextId) { clearBatchHistory(); updateControls(); return; }
  const revision = ++state.historyRevision;
  state.batches = [];
  clearBatchDetails();
  renderBusinessContext();
  batchSelect.replaceChildren(new Option("正在读取当前账号记录……", ""));
  updateControls();
  try {
    const response = await fetch("/api/platform-batches", { cache: "no-store", headers: historyHeaders() });
    const data = await response.json();
    if (revision !== state.historyRevision) return;
    if (!response.ok) throw new Error(data.error || "平台开户批次读取失败。");
    state.batches = (Array.isArray(data.items) ? data.items : []).filter((item) =>
      batchMatchesAccount(item) || (!item.owner && item.org_id === state.context.org_id));
    batchSelect.replaceChildren();
    if (!state.batches.length) batchSelect.append(new Option("还没有平台批次", ""));
    else {
      batchSelect.append(new Option("选择一个平台开户批次……", ""));
      const current = document.createElement("optgroup");
      current.label = `当前账号：${state.context.account.username}`;
      const legacy = document.createElement("optgroup");
      legacy.label = "同组织旧记录（未记录账号，仅供查看）";
      state.batches.forEach((item) => {
        const qr = `${item.qr_ready_count || 0}/${item.count || 0}`;
        (item.owner ? current : legacy).append(new Option(`${item.created_at || item.batch_id} · 套餐 #${item.product_id} · ${item.count} 张 · 已保存 ${qr} · ${platformBatchStateLabel(item.state)}`, item.batch_id));
      });
      if (current.children.length) batchSelect.append(current);
      if (legacy.children.length) batchSelect.append(legacy);
      if (selectId) batchSelect.value = selectId;
    }
    if (batchSelect.value) await selectPlatformBatch();
    else {
      state.batch = null;
      renderBusinessContext();
      batchSummary.textContent = state.batches.length ? "请选择一个平台批次。" : "还没有平台库存批次。";
      renderQrResults([]);
      updateControls();
    }
  } catch (error) {
    if (revision !== state.historyRevision) return;
    batchSummary.textContent = error.message;
  }
}

async function selectPlatformBatch() {
  const batchId = batchSelect.value;
  const revision = ++state.historyRevision;
  clearBatchDetails();
  renderBusinessContext();
  updateControls();
  if (!batchId) {
    state.batch = null;
    renderBusinessContext();
    batchSummary.textContent = "请选择一个平台批次。";
    setWorkflow("等待选择批次");
    updateControls();
    return;
  }
  batchSummary.textContent = "正在读取平台批次详情……";
  try {
    const response = await fetch(`/api/platform-batches/${encodeURIComponent(batchId)}`, { cache: "no-store", headers: historyHeaders() });
    const data = await response.json();
    if (revision !== state.historyRevision) return;
    if (!response.ok) throw new Error(data.error || "平台开户批次读取失败。");
    if (data.owner && !batchMatchesAccount(data)) throw new Error("批次账号不匹配，请重新加载记录。");
    state.batch = data;
    renderBusinessContext();
    const records = Array.isArray(data.records) ? data.records : [];
    const ready = records.filter((record) => record.qr_status === "saved").length;
    const lpaReady = records.filter((record) => record.lpa_status === "saved" || record.lpa_status === "memory_only").length;
    batchSummary.textContent = `${data.owner?.username || "账号未记录（同组织旧批次）"} · 组织 #${data.org_id} · ${data.batch_id} · ${records.length} 张 · 已保存 ${ready}/${records.length} 张 · ${platformBatchStateLabel(data.state)}。`;
    downloadBatch.href = data.csv_download ? historyDownloadUrl(data.csv_download) : "#";
    downloadBatch.hidden = !data.csv_download;
    downloadBatchQrZip.href = ready > 0 ? `/api/platform-batches/${encodeURIComponent(batchId)}/qr/download-all.zip` : "#";
    downloadBatchQrZip.hidden = !(ready > 0);
    downloadBatchLpaTxt.href = lpaReady > 0 ? `/api/platform-batches/${encodeURIComponent(batchId)}/lpa/download-all.txt` : "#";
    downloadBatchLpaTxt.hidden = !(lpaReady > 0);
    renderSavedQrRecords(data);
    setWorkflow(!data.owner ? "旧记录仅供查看" : "待检查");
    if (data.platform_activation?.has_verification || data.platform_activation?.verification) renderSavedVerification(data.platform_activation);
  } catch (error) {
    if (revision !== state.historyRevision) return;
    state.batch = null;
    renderBusinessContext();
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
  let shouldCheckActivation = false;
  state.qrJobId = "starting";
  batchMessage.textContent = "正在创建一次性二维码任务……";
  updateControls();
  try {
    const response = await fetch("/api/platform-qr-jobs", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...historyHeaders() },
      body: JSON.stringify({ batch_id: state.batch.batch_id, username: username.value.trim(), password: password.value, base_url: platformBaseUrl }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "二维码任务创建失败。");
    state.qrJobId = data.job_id;
    shouldCheckActivation = await pollQrJob();
  } catch (error) {
    batchMessage.textContent = error.message;
  } finally {
    state.qrJobId = null;
    try { await refreshSelectedBatch({ keepLiveQrResults: true }); }
    catch { batchMessage.textContent = "批次刷新失败，请重新加载记录；不要重复获取二维码。"; }
    updateControls();
  }
  if (shouldCheckActivation) {
    if (canActivateBatch()) await activateBatch();
    else await checkActivation();
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
      // 刷新批次状态并更新工作流
      await refreshSelectedBatch({ keepLiveQrResults: true });
      renderBusinessContext();
      updateControls();
      return Array.isArray(data.results) && data.results.some((row) => row.state === "ready");
    }
    if (data.state === "failed" || data.state === "cancelled") throw new Error(data.error || data.message || "二维码任务未完成。");
    await delay(600);
  }
}

async function cancelQrJob() {
  if (!state.qrJobId || state.qrJobId === "starting") return;
  await fetch(`/api/platform-qr-jobs/${encodeURIComponent(state.qrJobId)}/cancel`, { method: "POST" }).catch(() => {});
}

async function refreshSelectedBatch({ keepLiveQrResults = false } = {}) {
  if (!state.batch?.batch_id) return;
  const batchId = state.batch.batch_id;
  const revision = state.historyRevision;
  const response = await fetch(`/api/platform-batches/${encodeURIComponent(batchId)}`, { cache: "no-store", headers: historyHeaders() });
  if (response.ok) {
    const data = await response.json();
    if (revision !== state.historyRevision || state.batch?.batch_id !== batchId) return;
    state.batch = data;
    const ready = state.batch.records.filter((record) => record.qr_status === "saved").length;
    batchSummary.textContent = `${state.batch.batch_id} · ${state.batch.records.length} 张 · 二维码 ${ready}/${state.batch.records.length} 张 · ${platformBatchStateLabel(state.batch.state)}。`;
    if (!keepLiveQrResults) renderSavedQrRecords(state.batch);
  }
}

function renderSavedQrRecords(batch) {
  qrResults.replaceChildren();
  qrResults.hidden = false;
  if (batch.qr_error) {
    const error = document.createElement("p");
    error.textContent = batch.qr_error;
    qrResults.append(error);
  }
  for (const record of batch.records || []) {
    const item = document.createElement("details");
    item.className = "activation-item";
    const summary = document.createElement("summary");
    const status = batch.qr_operations !== "none" && record.qr_status === "not_requested" && !record.state
      ? "未记录结果，需核查"
      : ({ saved: "已保存", failed: "失败", unknown: "结果未知", not_requested: "未请求" })[record.qr_status] || record.qr_status || "未记录";
    const lpaStatus = (record.lpa_status === "saved" || record.lpa_status === "memory_only") ? " · LPA已保存" : "";
    summary.textContent = `${record.sequence}. ${record.iccid} · ${status}${lpaStatus}`;
    const detail = document.createElement("p");
    const lpaInfo = record.lpa_status === "saved" ? "已保存到文件" : record.lpa_status === "memory_only" ? "已保存（旧格式，可下载）" : record.lpa_status || "未记录";
    detail.textContent = `处理时间：${record.fetched_at || "未记录"}；LPA：${lpaInfo}；${record.error || "无错误记录"}`;
    item.append(summary, detail);
    qrResults.append(item);
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

function canActivateBatch() {
  return selectedBatchReady() && state.batch.records.length > 0
    && state.batch.records.every((record) => record.qr_status === "saved")
    && !state.batch.platform_activation?.has_submission_intent
    && !state.submissionAttempts.has(state.batch.batch_id);
}

function platformActionPayload() {
  return { batch_id: state.batch.batch_id, username: username.value.trim(),
    password: password.value, base_url: platformBaseUrl,
    org_id: effectiveOrgId(), product_id: effectiveProductId(), max_total: maxTotal.value.trim() };
}

async function runPlatformAction(action, payload, message) {
  setProgress("platform", 0, message);
  const response = await fetch(`/api/platform-activation/${action}`, {
    method: "POST", headers: { "Content-Type": "application/json", ...historyHeaders() },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "操作未完成。");
  while (true) {
    const snapshot = await fetch(`/api/platform-activation-jobs/${encodeURIComponent(data.job_id)}`, { cache: "no-store" });
    const job = await snapshot.json();
    if (!snapshot.ok) throw new Error(job.error || "任务不存在。");
    setProgress("platform", job.progress || 0, message);
    if (job.state === "done") return job.result || {};
    if (job.state === "failed") throw new Error(job.error || job.message || "操作未完成。");
    await delay(700);
  }
}

async function activateBatch() {
  if (state.platformJobBusy || state.qrJobId || !canActivateBatch()) return;
  if (!credentialsReady() || !batchMatchesAccount(state.batch) || !businessConfigReady()) {
    setMessage("请先识别账号并选择批次。");
    return;
  }
  if (!/^\d+(?:\.\d{1,2})?$/.test(maxTotal.value.trim()) || !Number.isFinite(Number(maxTotal.value))) {
    setMessage("请填写本次授权金额上限，最多保留两位小数。");
    maxTotal.focus();
    return;
  }
  state.platformJobBusy = true;
  verificationResult.hidden = true;
  setMessage();
  setWorkflow("准备激活", "blue");
  updateControls();
  const payload = platformActionPayload();
  let submissionAttempted = false;
  try {
    const preview = await runPlatformAction("preview", payload, "正在准备激活……");
    if (preview.batch_id !== payload.batch_id || preview.count !== state.batch.records.length || !/^\d+\.\d{2}$/.test(String(preview.total))) {
      throw new Error("激活确认信息不完整，请重新加载批次。");
    }
    if (Number(preview.total) > Number(payload.max_total)) {
      throw new Error(`总价 ${preview.total} 超过授权金额上限 ${payload.max_total}。`);
    }
    const selectedProduct = state.context.products.find((product) => product.id === payload.product_id);
    if (!window.confirm(`${productLabel(selectedProduct)}\n激活当前批次 ${preview.count} 张卡，总价 ${preview.total}，授权上限 ${payload.max_total}。确认后将提交平台并扣费。是否继续？`)) {
      setWorkflow("待激活");
      setMessage("尚未提交激活。");
      return;
    }
    // A lost submission response must never enable a second write.
    state.submissionAttempts.add(payload.batch_id);
    submissionAttempted = true;
    setWorkflow("激活中", "blue");
    const result = await runPlatformAction("submit", { ...payload, confirmed_total: preview.total }, "正在激活并检查结果……");
    if (result.verification) renderVerification(result.verification);
    else if (!result.verification_error) {
      // Older running servers submit without automatically checking the result.
      const verification = await runPlatformAction("verify", payload, "正在检查激活结果……");
      renderVerification(verification);
    } else {
      setWorkflow("已提交，待检查");
      setMessage(result.verification_error || "已提交激活，请点击重新检查。");
    }
  } catch (error) {
    setWorkflow(submissionAttempted ? "激活结果待确认" : "未提交激活");
    setMessage(submissionAttempted ? `${error.message} 请重新检查状态，不要重复提交激活。` : error.message);
  } finally {
    try { await refreshSelectedBatch({ keepLiveQrResults: true }); }
    catch { setMessage("批次刷新失败，请重新加载记录；如已提交激活，只检查状态，不要重复提交。"); }
    state.platformJobBusy = false;
    verifyButton.textContent = "重新检查";
    updateControls();
  }
}

async function checkActivation() {
  if (state.platformJobBusy || state.qrJobId || !selectedBatchReady()) return;
  if (!credentialsReady() || !batchMatchesAccount(state.batch)) {
    setMessage("请先识别账号并选择批次。");
    return;
  }
  state.platformJobBusy = true;
  verificationResult.hidden = true;
  setWorkflow("检查中", "blue");
  setMessage();
  setProgress("platform", 0, "正在检查是否已激活……");
  updateControls();
  try {
    const response = await fetch("/api/platform-activation/verify", {
      method: "POST",
      headers: { "Content-Type": "application/json", ...historyHeaders() },
      body: JSON.stringify({ batch_id: state.batch.batch_id, username: username.value.trim(),
        password: password.value, base_url: platformBaseUrl,
        org_id: effectiveOrgId(), product_id: effectiveProductId() }),
    });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "检查未完成，请重试。");
    await pollActivationCheck(data.job_id);
  } catch (error) {
    setMessage(error.message);
    setWorkflow("检查未完成");
  } finally {
    state.platformJobBusy = false;
    verifyButton.textContent = "重新检查";
    updateControls();
  }
}

async function pollActivationCheck(jobId) {
  while (true) {
    const response = await fetch(`/api/platform-activation-jobs/${encodeURIComponent(jobId)}`, { cache: "no-store" });
    const data = await response.json();
    if (!response.ok) throw new Error(data.error || "检查任务不存在。");
    setProgress("platform", data.progress || 0, "正在检查是否已激活……");
    if (data.state === "done") { renderVerification(data.result || {}); return; }
    if (data.state === "failed") throw new Error(data.error || data.message || "检查未完成。");
    await delay(700);
  }
}

function renderVerification(result, saved = false) {
    verificationResult.hidden = false;
    verificationResult.replaceChildren();
    const title = document.createElement("strong");
    title.textContent = `${saved ? "上次检查" : "检查结果"}：${result.confirmed || 0}/${result.count || 0} 张已激活`;
    const body = document.createElement("span");
    body.textContent = `${saved ? "上次检查" : "检查"}时间：${result.checked_at || "未记录"}${result.all_confirmed ? "" : "。未确认的卡可稍后重新检查。"}`;
    verifyButton.textContent = "重新检查";
    verificationResult.append(title, body);
    const wrap = document.createElement("div");
    wrap.className = "platform-table-wrap";
    const table = document.createElement("table");
    table.className = "platform-table";
    table.setAttribute("aria-label", "逐卡激活状态");
    const header = table.createTHead().insertRow();
    ["ICCID", "状态", "说明"].forEach((label) => {
      const th = document.createElement("th"); th.scope = "col"; th.textContent = label; header.append(th);
    });
    const tbody = table.createTBody();
    (result.rows || []).forEach((row) => {
      const tr = tbody.insertRow();
      [row.iccid, row.state === "verified" ? "已激活" : "未确认", row.state === "verified" ? "—" : row.message].forEach((value) => { tr.insertCell().textContent = value || "—"; });
    });
    wrap.append(table);
    verificationResult.append(wrap);
    if (result.verification_csv_url) {
      const link = document.createElement("a");
      link.className = "button button-secondary verification-download";
      link.href = historyDownloadUrl(result.verification_csv_url);
      link.download = "";
      link.textContent = "导出检查结果";
      verificationResult.append(link);
    }
    setWorkflow(`${saved ? "上次检查：" : ""}${result.all_confirmed ? "全部已激活" : "部分未确认"}`, result.all_confirmed ? "blue" : "neutral");
}

function renderSavedVerification(summary) {
  if (summary?.verification) { renderVerification(summary.verification, true); return; }
  if (!summary?.has_verification || !summary.verification_csv_url) return;
  verificationResult.hidden = false;
  verificationResult.replaceChildren();
  const title = document.createElement("strong");
  title.textContent = "已有历史记录，请重新检查";
  const link = document.createElement("a");
  link.className = "button button-secondary verification-download";
  link.href = historyDownloadUrl(summary.verification_csv_url);
  link.download = "";
  link.textContent = "导出检查结果";
  verificationResult.append(title, link);
}

function qrStateLabel(value) { return ({ ready: "已保存", unknown: "结果未知，已停止", skipped: "已跳过", failed: "获取失败" })[value] || "处理中"; }

function platformBatchStateLabel(value) { return ({ inventory_selected: "已选库存", preparing_qr: "未记录完成（处理中或中断，勿重试）", qr_ready: "二维码已准备", qr_partial: "二维码部分完成", qr_failed: "二维码任务失败", profile_data_ready: "历史准备任务已完成", profile_data_partial: "历史准备任务部分完成", profile_data_failed: "历史准备任务失败" })[value] || value || "未开始"; }

function delay(milliseconds) { return new Promise((resolve) => window.setTimeout(resolve, milliseconds)); }

renderBusinessContext();
loadPlatformBatches();
updateControls();
