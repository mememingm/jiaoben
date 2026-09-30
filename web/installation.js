"use strict";
const el = (id) => document.getElementById(id);
let current = null;
let busy = false;
let jobId = sessionStorage.getItem("installation-job") || "";
let revision = 0;
const selected = new Set();
const message = (text) => { el("message").textContent = text; };
const credentials = () => ({ username: el("username").value.trim(), password: el("password").value, base_url: el("base-url").value.trim() });
async function api(url, body) {
  const response = await fetch(url, body ? { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) } : { cache: "no-store" });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || "请求未完成");
  return data;
}
function controls() {
  ["username", "password", "base-url", "iccids", "prepare"].forEach((id) => { el(id).disabled = busy; });
  const prepared = !busy && current?.state === "prepared" && !current.fetch_started;
  const readyIds = prepared ? current.rows.filter((row) => row.eligible).map((row) => row.inventory_id) : [];
  const allSelected = readyIds.length > 0 && readyIds.every((id) => selected.has(id));
  el("select-ready").disabled = !readyIds.length;
  el("select-ready").textContent = allSelected ? "取消全选可处理卡" : "选择全部可处理卡";
  el("selection-summary").textContent = prepared
    ? `已选 ${selected.size} / 可处理 ${readyIds.length} 张`
    : current?.fetch_started ? "本批次已开始获取；可下载已保存的资料。" : "核对完成后可选择卡片。";
  el("fetch").disabled = !prepared || !selected.size;
  el("cancel").disabled = current?.state !== "fetching";
  el("recheck").disabled = busy || !current?.rows?.some((row) => row.state === "saved");
}
function render() {
  el("rows").replaceChildren();
  for (const row of current?.rows || []) {
    const tr = document.createElement("tr");
    const check = document.createElement("input"); check.type = "checkbox";
    check.setAttribute("aria-label", `选择 ${row.iccid}`);
    check.checked = selected.has(row.inventory_id);
    check.disabled = busy || current.state !== "prepared" || !row.eligible;
    check.addEventListener("change", () => { check.checked ? selected.add(row.inventory_id) : selected.delete(row.inventory_id); controls(); });
    const status = ({ ready: "可处理", saved: "已保存", attempted: "已开始，勿重复请求", unknown: "结果待核查", skipped: "未请求", blocked: "不可处理" })[row.state] || row.state;
    for (const value of [check, row.iccid, row.profile_status || "—", row.remaining ?? "—", `${status}：${row.reason || "核对通过"}`]) {
      const td = document.createElement("td"); value instanceof Node ? td.append(value) : td.textContent = String(value); tr.append(td);
    }
    const files = document.createElement("td");
    if (row.state === "saved") for (const [ext, label] of [["png", "下载二维码"], ["txt", "下载 LPA"]]) {
      const a = document.createElement("a"); a.href = `/api/installation-jobs/${jobId}/files/${row.inventory_id}.${ext}`;
      a.textContent = label; a.download = `${row.iccid}.${ext}`; a.style.marginRight = "12px"; files.append(a);
    }
    tr.append(files); el("rows").append(tr);
  }
  controls();
}
async function poll() {
  const stamp = ++revision;
  try {
    do {
      const data = await api(`/api/installation-jobs/${encodeURIComponent(jobId)}`);
      if (stamp !== revision) return;
      current = data; busy = ["checking", "fetching"].includes(data.state);
      message(data.message); render();
      if (!busy) return;
      await new Promise((resolve) => setTimeout(resolve, 800));
    } while (stamp === revision);
  } catch (error) {
    busy = false; message(`${error.message}。若已经点击获取，请刷新恢复任务，勿重复获取。`); controls();
  }
}
el("installation-form").addEventListener("submit", async (event) => {
  event.preventDefault(); if (busy) return;
  const iccids = [...new Set(el("iccids").value.trim().split(/[\s,;，；]+/).filter(Boolean))];
  if (!iccids.length || iccids.length > 200 || iccids.some((v) => !/^\d{18,22}$/.test(v))) { message("请输入 1—200 个有效 ICCID（18—22 位数字）。"); return; }
  busy = true; current = null; selected.clear(); controls(); message("正在创建只读核对任务……");
  try { const data = await api("/api/installation/prepare", { ...credentials(), iccids }); jobId = data.job_id; sessionStorage.setItem("installation-job", jobId); await poll(); }
  catch (error) { busy = false; message(error.message); controls(); }
});
el("select-ready").addEventListener("click", () => {
  if (busy || current?.state !== "prepared" || current.fetch_started) return;
  const readyIds = current.rows.filter((row) => row.eligible).map((row) => row.inventory_id);
  if (!readyIds.length) return;
  if (readyIds.every((id) => selected.has(id))) readyIds.forEach((id) => selected.delete(id));
  else readyIds.forEach((id) => selected.add(id));
  render();
});
el("fetch").addEventListener("click", async () => {
  if (busy || !selected.size || current?.fetch_started) return;
  if (!el("username").value.trim() || !el("password").value) { message("请重新填写同一账号的用户名与密码。"); return; }
  if (!confirm(`将为 ${selected.size} 张已开卡 eSIM 获取安装资料，可能消耗二维码查看次数。不会提交新开户订单。确认继续？`)) return;
  busy = true; controls();
  try { await api("/api/installation/fetch", { ...credentials(), job_id: jobId, inventory_ids: [...selected], confirmed: true }); selected.clear(); await poll(); }
  catch (error) { message(`${error.message}；请刷新恢复任务状态，不要重复提交。`); current = null; busy = false; controls(); }
});
el("cancel").addEventListener("click", async () => { try { await api("/api/installation/cancel", { job_id: jobId }); message("已请求停止后续卡；当前请求可能仍在执行。"); } catch (error) { message(error.message); } });
el("recheck").addEventListener("click", () => { sessionStorage.setItem("profile-recheck-iccids", JSON.stringify(current.rows.map((r) => r.iccid))); location.href = "/"; });
for (const id of ["username", "base-url", "iccids"]) el(id).addEventListener("input", () => {
  if (busy) return;
  if (current?.fetch_started) { message("输入已改变；本批次已保存的资料仍可下载。提交新 ICCID 前需重新核对。"); return; }
  revision++; current = null; selected.clear(); jobId = ""; sessionStorage.removeItem("installation-job"); render(); message("输入已改变，请重新核对。");
});
const imported = sessionStorage.getItem("installation-iccids");
if (imported) { sessionStorage.removeItem("installation-iccids"); sessionStorage.removeItem("installation-job"); jobId = ""; try { el("iccids").value = JSON.parse(imported).join("\n"); } catch { message("ICCID 导入失败，请手动填写。"); } }
if (jobId) { busy = true; controls(); poll(); } else controls();
