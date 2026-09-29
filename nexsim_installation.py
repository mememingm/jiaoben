"""ICCID-based installation material retrieval, entirely separate from opening orders.

Prepare is read-only. Fetch requires explicit confirmation and fresh identity/state
checks. No method in this module submits orders or activates subscriptions.
"""
from __future__ import annotations

import io
import json
import re
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit

import qrcode

import nexsim_platform_activation as platform
import nexsim_platform_batch as batches
import nexsim_status_checker as checker
import nexsim_material_guard as guard


class InstallationStop(checker.Stop):
    pass


def owner_of(client):
    return {"base_url": f"https://{urlsplit(client.base).hostname.lower()}",
            "org_id": client.org_id, "username": client.account["username"]}


def resolve_cards(client, iccids):
    """Resolve the full requested set; unmatched targets must never disappear."""
    inventory = platform._all_pages(client, "/api/inventory/page",
                                    {"simType": "ESIM", "status": "USED"})
    results = []
    for iccid in iccids:
        matches = [r for r in inventory if r.get("iccid") == iccid
                   and r.get("ownerOrgId") == client.org_id]
        row = {"iccid": iccid, "eligible": False, "reason": "", "state": "blocked"}
        if len(matches) != 1:
            row["reason"] = "未在当前账号 USED 库存中唯一匹配（不存在、不在范围内或重复）"
        else:
            card = matches[0]
            inventory_id = card.get("id")
            row.update(inventory_id=inventory_id, inventory_status=card.get("status"))
            if type(inventory_id) is not int or inventory_id <= 0:
                row["reason"] = "库存 ID 无效"
            elif card.get("status") != "USED" or card.get("simType") != "ESIM" or card.get("inventoryType") != "P":
                row["reason"] = "不是 USED 的 P 类 eSIM"
            elif card.get("qrCodeSupported") is not True:
                row["reason"] = "平台未标记支持安装资料获取"
            else:
                count, limit, remaining, error = batches._qr_capacity(card)
                row.update(view_count=count, view_limit=limit, remaining=remaining)
                if error:
                    row["reason"] = error
                else:
                    try:
                        data = client.get(f"/api/inventory/{inventory_id}/esim-usage-status")
                        status = data.get("status") if isinstance(data, dict) else None
                        row["profile_status"] = status if isinstance(status, str) else "UNKNOWN"
                        if status != "RELEASED":
                            row["reason"] = "Profile 未确认是 RELEASED"
                        else:
                            row.update(eligible=True, state="ready")
                    except platform.PlatformActivationStop:
                        row["reason"] = "Profile 查询失败，请重新核对"
        results.append(row)
    return results


class InstallationManager:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir).resolve()
        self.root = self.output_dir / "installation-batches"
        self.jobs = {}
        self.lock = threading.Lock()

    def _client(self, payload):
        return platform.PlatformClient(payload.get("base_url") or "https://admin.nexsimus.com",
                                       payload.get("username", ""), payload.get("password", ""))

    def start_prepare(self, payload):
        raw = payload.get("iccids")
        if not isinstance(raw, list) or not 1 <= len(raw) <= 200 or not all(isinstance(v, str) for v in raw):
            raise InstallationStop("请输入 1—200 个 ICCID。")
        iccids = list(dict.fromkeys(v.strip() for v in raw))
        if any(not re.fullmatch(r"[0-9]{18,22}", v) for v in iccids):
            raise InstallationStop("ICCID 必须是 18—22 位数字。")
        if not payload.get("username") or not payload.get("password"):
            raise InstallationStop("请填写账号和密码。")
        job_id = "im-" + uuid.uuid4().hex
        with self.lock:
            self.jobs[job_id] = {"id": job_id, "state": "checking", "rows": [],
                                 "message": "正在匹配库存和 Profile 状态", "owner": None,
                                 "fetch_started": False, "cancel": False}
        threading.Thread(target=self._prepare, args=(job_id, dict(payload), iccids), daemon=True).start()
        return job_id

    def _update(self, job_id, **values):
        with self.lock:
            self.jobs[job_id].update(values)

    def _prepare(self, job_id, payload, iccids):
        try:
            client = self._client(payload)
            owner = owner_of(client)
            rows = resolve_cards(client, iccids)
            for row in rows:
                if row["eligible"] and self._previous_attempt(owner, row):
                    row.update(eligible=False, reason="已有获取记录，请核查本地资料；禁止重复获取", state="blocked")
            self._update(job_id, owner=owner, rows=rows, state="prepared",
                         message=f"核对 {len(rows)} 张，可处理 {sum(r['eligible'] for r in rows)} 张；尚未获取安装资料")
        except (platform.PlatformActivationStop, InstallationStop) as exc:
            self._update(job_id, state="failed", message=str(exc))
        except Exception:
            self._update(job_id, state="failed", message="核对失败，未获取安装资料")
        finally:
            payload.clear()

    def _marker(self, owner, row):
        # Shared identity, independent of batch ID. Exclusive create prevents two
        # installation jobs from consuming the same card concurrently.
        return guard.marker_path(self.output_dir, owner['base_url'], owner['org_id'], row['inventory_id'])

    def _previous_attempt(self, owner, row):
        if self._marker(owner, row).exists():
            return True
        # Only read metadata; never open saved QR images or activation strings.
        for folder, pattern in (("platform-activation-batches", "pb-*.json"), ("write-batches", "wb-*.json")):
            for path in (self.output_dir / folder).glob(pattern):
                try:
                    batch = json.loads(path.read_text(encoding="utf-8-sig"))
                    saved_owner = batch.get("owner")
                    if saved_owner and any(saved_owner.get(k) != owner[k] for k in ("base_url", "org_id")):
                        continue
                    for record in batch.get("records", []):
                        if record.get("iccid") != row["iccid"]:
                            continue
                        if record.get("qr_status", "not_requested") != "not_requested" or batch.get("qr_operations", "none") != "none":
                            return True
                except (OSError, ValueError, AttributeError):
                    raise InstallationStop("历史处理记录无法核对，已停止；请先修复记录。")
        return False

    def start_fetch(self, payload):
        if payload.get("confirmed") is not True:
            raise InstallationStop("必须明确确认一次性获取。")
        ids = payload.get("inventory_ids")
        if not isinstance(ids, list) or not 1 <= len(ids) <= 200 or any(type(i) is not int for i in ids) or len(set(ids)) != len(ids):
            raise InstallationStop("请选择有效且不重复的库存记录。")
        job_id = payload.get("job_id")
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["state"] != "prepared" or job["fetch_started"]:
                raise InstallationStop("请先核对 ICCID；已经开始的任务不能重复获取。")
            selected = [dict(r) for r in job["rows"] if r.get("inventory_id") in ids and r["eligible"]]
            if len(selected) != len(ids):
                raise InstallationStop("选择包含未通过核对的卡。")
            job.update(state="fetching", fetch_started=True, message="正在重新验证账号和状态")
        threading.Thread(target=self._fetch, args=(job_id, dict(payload), selected), daemon=True).start()
        return job_id

    def _persist(self, job_id):
        value = self.snapshot(job_id)
        checker.atomic_write(self.root / job_id / "record.json", json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))

    def _record(self, job_id, result):
        with self.lock:
            for row in self.jobs[job_id]["rows"]:
                if row.get("inventory_id") == result.get("inventory_id"):
                    row.update(result)
                    break
        self._persist(job_id)

    def _fetch(self, job_id, payload, selected):
        try:
            client = self._client(payload)
            owner = self.jobs[job_id]["owner"]
            if owner_of(client) != owner:
                raise InstallationStop("账号或平台与核对记录不一致。")
            # Fresh inventory snapshot and profile check before any consumable call.
            live = resolve_cards(client, [r["iccid"] for r in selected])
            if any(not r["eligible"] or r.get("inventory_id") != original["inventory_id"]
                   for r, original in zip(live, selected)):
                raise InstallationStop("卡片状态或库存身份已变化，请重新核对。")
            stopped = False
            for index, row in enumerate(live, 1):
                if stopped or self.jobs[job_id]["cancel"]:
                    self._record(job_id, {**row, "state": "skipped", "eligible": False,
                                          "reason": "任务停止，未请求此卡"})
                    continue
                self._update(job_id, message=f"处理安装资料 {index}/{len(live)}")
                try:
                    latest = client.get(f"/api/inventory/{row['inventory_id']}/esim-usage-status")
                    if not isinstance(latest, dict) or latest.get("status") != "RELEASED":
                        self._record(job_id, {**row, "state": "blocked", "eligible": False,
                                              "reason": "获取前复核未确认 RELEASED，未请求安装资料"})
                        continue
                    if self._previous_attempt(owner, row):
                        raise InstallationStop("已有获取记录，禁止重复请求")
                    marker = guard.claim(self.output_dir, client, row["inventory_id"], job_id)
                    self._record(job_id, {**row, "state": "attempted", "eligible": False})
                    code, metadata = client.get_qr_code_once(row["inventory_id"], row["iccid"])
                    if metadata.get("status") != "READY" or not batches.LPA_PATTERN.fullmatch(code):
                        raise InstallationStop("平台没有返回 READY 的有效安装资料")
                    folder = self.root / job_id / "materials"
                    # Persist before rendering to avoid losing the response on image failure.
                    checker.atomic_write(folder / f"{row['inventory_id']}.txt", code.encode("utf-8"))
                    buffer = io.BytesIO()
                    qrcode.make(code, box_size=10, border=4).save(buffer, format="PNG")
                    checker.atomic_write(folder / f"{row['inventory_id']}.png", buffer.getvalue())
                    code = ""
                    self._record(job_id, {**row, "state": "saved", "eligible": False,
                                          "reason": "已保存，可下载后手动安装", "saved_at": checker.now()})
                    checker.atomic_write(marker, json.dumps({"job_id": job_id, "state": "saved"}).encode())
                except (platform.PlatformActivationStop, InstallationStop):
                    # No automatic retry, including malformed responses and refusals.
                    self._record(job_id, {**row, "state": "unknown", "eligible": False,
                                          "reason": "未取得完整资料或已有处理记录；已停止，勿重复获取"})
                    stopped = True
                except Exception:
                    self._record(job_id, {**row, "state": "unknown", "eligible": False,
                                          "reason": "保存或请求异常，请核查本地文件，禁止重复获取"})
                    stopped = True
            self._update(job_id, state="finished", message="处理结束，请逐卡查看结果；未提交任何开户订单")
            self._persist(job_id)
        except (platform.PlatformActivationStop, InstallationStop) as exc:
            self._update(job_id, state="failed", message=str(exc))
        except Exception:
            self._update(job_id, state="failed", message="任务异常，禁止重复获取，请核查本地记录")
        finally:
            payload.clear()

    def snapshot(self, job_id):
        if not isinstance(job_id, str) or not re.fullmatch(r"im-[a-f0-9]{32}", job_id):
            return None
        with self.lock:
            job = self.jobs.get(job_id)
            if job:
                return json.loads(json.dumps({k: v for k, v in job.items() if k != "cancel"}))
        try:
            saved = json.loads((self.root / job_id / "record.json").read_text(encoding="utf-8"))
            if saved.get("id") != job_id or not isinstance(saved.get("rows"), list):
                return None
            if saved.get("state") in {"checking", "fetching"}:
                saved.update(state="interrupted", fetch_started=True,
                             message="服务曾中断。已保存资料可下载，其他记录需核查；不会自动重试。")
            return saved
        except (OSError, ValueError, AttributeError):
            return None

    def cancel(self, job_id):
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["state"] != "fetching":
                return False
            job["cancel"] = True
            return True

    def download(self, job_id, inventory_id, extension):
        # The unguessable job ID is a bearer capability held by this browser tab.
        job = self.snapshot(job_id)
        if not job or extension not in {"txt", "png"}:
            return None
        if not any(r.get("inventory_id") == inventory_id and r.get("state") == "saved" for r in job["rows"]):
            return None
        path = self.root / job_id / "materials" / f"{inventory_id}.{extension}"
        return path.read_bytes() if path.is_file() else None
