"""Independent platform inventory batches and one-shot QR preparation.

This module deliberately does not use eSIM Profile status. Platform batches are
created from the authenticated platform inventory and are stored separately
from historical Profile-query write batches.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import threading
import time
import uuid
import zipfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import qrcode

import nexsim_platform_activation as platform_activation


MAX_BATCH_SIZE = 200
BATCH_ID_PATTERN = re.compile(r"^pb-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$")
ICCID_PATTERN = re.compile(r"^[0-9]{18,22}$")
LPA_PATTERN = re.compile(r"^LPA:1\$[^\s$]+\$.+$")
ProgressCallback = Callable[[str, float, str], None]


class PlatformBatchStop(RuntimeError):
    """A user-facing platform inventory or batch error."""


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def exclusive_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise PlatformBatchStop("这张卡已经尝试过获取二维码，禁止重复请求。") from exc


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlatformBatchStop(f"{label}必须是正整数。")
    return value


def _qr_capacity(row: dict[str, Any]) -> tuple[int | None, int | None, int | None, str]:
    count = row.get("qrViewCount")
    limit = row.get("qrViewLimit")
    if type(count) is not int or count < 0:
        return None, None, None, "二维码已查看次数缺失或异常"
    if type(limit) is not int or limit <= 0:
        return count, None, None, "二维码查看上限缺失或异常"
    remaining = limit - count
    if remaining <= 0:
        return count, limit, remaining, "二维码查看次数已耗尽"
    return count, limit, remaining, ""


def _inventory_view(row: dict[str, Any], org_id: int, product_id: int) -> dict[str, Any]:
    count, limit, remaining, capacity_error = _qr_capacity(row)
    blockers: list[str] = []
    inventory_id = row.get("id")
    iccid = row.get("iccid")
    if type(inventory_id) is not int or inventory_id <= 0:
        blockers.append("库存 ID 无效")
    if not isinstance(iccid, str) or not ICCID_PATTERN.fullmatch(iccid):
        blockers.append("ICCID 无效")
    if row.get("ownerOrgId") != org_id:
        blockers.append("不属于当前组织")
    if row.get("productId") != product_id:
        blockers.append("产品不匹配")
    if row.get("inventoryType") != "P":
        blockers.append("库存类型不是 P")
    if row.get("simType") != "ESIM":
        blockers.append("不是 eSIM")
    if row.get("status") != "ALLOCATED":
        blockers.append("库存状态不是 ALLOCATED")
    if row.get("qrCodeSupported") is not True:
        blockers.append("不支持二维码")
    if capacity_error:
        blockers.append(capacity_error)
    return {
        "inventory_id": inventory_id,
        "iccid": iccid,
        "owner_org_id": row.get("ownerOrgId"),
        "product_id": row.get("productId"),
        "inventory_type": row.get("inventoryType"),
        "sim_type": row.get("simType"),
        "inventory_status": row.get("status"),
        "qr_code_supported": row.get("qrCodeSupported") is True,
        "qr_view_count": count,
        "qr_view_limit": limit,
        "qr_remaining": remaining,
        "qr_view_status": str(row.get("qrViewStatus") or ""),
        "eligible": not blockers,
        "blocked_reason": "；".join(blockers),
    }


def inspect_inventory(
    client: platform_activation.PlatformClient,
    org_id: int,
    product_id: int,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Read platform inventory only; this never calls the QR endpoint."""
    org_id = _positive_int(org_id, "组织 ID")
    product_id = _positive_int(product_id, "产品 ID")
    if progress:
        progress("inventory", 15, "正在读取平台 eSIM 库存……")
    rows = platform_activation.list_inventory(client)
    matching = [
        row for row in rows
        if row.get("ownerOrgId") == org_id and row.get("productId") == product_id
    ]
    views = [_inventory_view(row, org_id, product_id) for row in matching]
    seen_ids: set[int] = set()
    seen_iccids: set[str] = set()
    for row in views:
        inventory_id = row.get("inventory_id")
        iccid = row.get("iccid")
        if type(inventory_id) is int and inventory_id in seen_ids:
            row["eligible"] = False
            row["blocked_reason"] = "库存 ID 重复"
        if isinstance(iccid, str) and iccid in seen_iccids:
            row["eligible"] = False
            row["blocked_reason"] = "ICCID 重复"
        if type(inventory_id) is int:
            seen_ids.add(inventory_id)
        if isinstance(iccid, str):
            seen_iccids.add(iccid)
    views.sort(key=lambda row: (not row["eligible"], str(row.get("iccid") or "")))
    eligible_count = sum(row["eligible"] for row in views)
    if progress:
        progress("done", 100, f"平台库存读取完成：{eligible_count} 张可加入批次。")
    return {
        "org_id": org_id,
        "product_id": product_id,
        "count": len(views),
        "eligible_count": eligible_count,
        "blocked_count": len(views) - eligible_count,
        "checked_at": now(),
        "rows": views,
    }


def _normalize_selected(rows: Any, org_id: int, product_id: int) -> list[dict[str, Any]]:
    if not isinstance(rows, list) or not 1 <= len(rows) <= MAX_BATCH_SIZE:
        raise PlatformBatchStop("平台开户批次必须选择 1—200 张卡。")
    normalized: list[dict[str, Any]] = []
    inventory_ids: set[int] = set()
    iccids: set[str] = set()
    for sequence, row in enumerate(rows, 1):
        if not isinstance(row, dict):
            raise PlatformBatchStop("选择的库存记录格式不正确。")
        inventory_id = _positive_int(row.get("inventory_id"), "库存 ID")
        iccid = row.get("iccid")
        if not isinstance(iccid, str) or not ICCID_PATTERN.fullmatch(iccid):
            raise PlatformBatchStop("选择的库存包含无效 ICCID。")
        if inventory_id in inventory_ids or iccid in iccids:
            raise PlatformBatchStop("选择的库存包含重复卡。")
        if row.get("owner_org_id") != org_id or row.get("product_id") != product_id:
            raise PlatformBatchStop(f"ICCID {iccid} 的组织或产品不匹配。")
        if row.get("inventory_type") != "P" or row.get("sim_type") != "ESIM" \
                or row.get("inventory_status") != "ALLOCATED" or row.get("qr_code_supported") is not True:
            raise PlatformBatchStop(f"ICCID {iccid} 不符合平台开户库存条件。")
        count = row.get("qr_view_count")
        limit = row.get("qr_view_limit")
        if type(count) is not int or count < 0 or type(limit) is not int or limit <= count:
            raise PlatformBatchStop(f"ICCID {iccid} 的二维码查看次数不可用。")
        inventory_ids.add(inventory_id)
        iccids.add(iccid)
        normalized.append({
            "sequence": sequence,
            "inventory_id": inventory_id,
            "iccid": iccid,
            "owner_org_id": org_id,
            "product_id": product_id,
            "inventory_type": "P",
            "sim_type": "ESIM",
            "inventory_status": "ALLOCATED",
            "qr_code_supported": True,
            "qr_view_count": count,
            "qr_view_limit": limit,
            "qr_remaining": limit - count,
            "qr_view_status": str(row.get("qr_view_status") or ""),
            "lpa_status": "not_requested",
            "qr_status": "not_requested",
        })
    return normalized


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ICCID"])
    writer.writerows([[record["iccid"]] for record in records])
    atomic_write(path, output.getvalue().encode("utf-8-sig"))


class PlatformBatchStore:
    """Persist platform-selected cards independently from Profile query output."""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir.resolve()
        self.batch_dir = self.output_dir / "platform-activation-batches"
        self.lock = threading.Lock()
        self.qr_lock = threading.Lock()
        self.batches: dict[str, dict[str, Any]] = {}

    @staticmethod
    def validate_id(batch_id: str) -> str:
        if not BATCH_ID_PATTERN.fullmatch(batch_id):
            raise PlatformBatchStop("平台开户批次编号格式不正确。")
        return batch_id

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        org_id = _positive_int(payload.get("org_id"), "组织 ID")
        owner = payload.get("owner")
        if not isinstance(owner, dict) or owner.get("org_id") != org_id or not owner.get("username") or not owner.get("base_url"):
            raise PlatformBatchStop("建立批次前请先识别账号。")
        product_id = _positive_int(payload.get("product_id"), "产品 ID")
        records = _normalize_selected(payload.get("rows"), org_id, product_id)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        batch_id = f"pb-{stamp}-{uuid.uuid4().hex[:8]}"
        batch = {
            "batch_id": batch_id,
            "owner": {key: owner[key] for key in ("base_url", "org_id", "username")},
            "created_at": now(),
            "source": "platform-inventory",
            "state": "inventory_selected",
            "stage": "awaiting_qr",
            "org_id": org_id,
            "product_id": product_id,
            "count": len(records),
            "qr_operations": "none",
            "records": records,
        }
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch
        return self._decorate(dict(batch))

    def _path(self, batch_id: str) -> Path:
        return self.batch_dir / f"{self.validate_id(batch_id)}.json"

    def _persist(self, batch: dict[str, Any]) -> None:
        batch_id = self.validate_id(str(batch.get("batch_id", "")))
        stored = {
            key: value for key, value in batch.items()
            if key not in {"json_path", "csv_path", "csv_download", "platform_activation", "results"}
        }
        atomic_write(self.batch_dir / f"{batch_id}.json", json.dumps(stored, ensure_ascii=False, indent=2).encode("utf-8"))
        records = stored.get("records")
        if isinstance(records, list):
            _write_csv(self.batch_dir / f"{batch_id}.csv", records)

    def _decorate(self, batch: dict[str, Any]) -> dict[str, Any]:
        batch_id = self.validate_id(str(batch.get("batch_id", "")))
        batch.update({
            "json_path": str(self.batch_dir / f"{batch_id}.json"),
            "csv_path": str(self.batch_dir / f"{batch_id}.csv"),
            "csv_download": f"/api/platform-batches/{batch_id}/export.csv",
            "platform_activation": platform_activation.activation_artifact_summary(self.output_dir, batch_id),
        })
        return batch

    def snapshot(self, batch_id: str) -> dict[str, Any] | None:
        self.validate_id(batch_id)
        with self.lock:
            cached = self.batches.get(batch_id)
            if cached is not None:
                return self._decorate(json.loads(json.dumps(cached)))
        path = self._path(batch_id)
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformBatchStop("平台开户批次文件无法读取。") from exc
        if not isinstance(value, dict):
            raise PlatformBatchStop("平台开户批次文件格式异常。")
        return self._decorate(value)

    @staticmethod
    def belongs_to(batch: dict[str, Any], owner: dict[str, Any]) -> bool:
        saved = batch.get("owner")
        if isinstance(saved, dict):
            return all(saved.get(key) == owner.get(key) for key in ("base_url", "org_id", "username"))
        # Old records have no reliable account identity. Show separately, read-only.
        return (batch.get("org_id") == owner.get("org_id")
                and owner.get("base_url") == "https://admin.nexsimus.com")

    def history_snapshot(self, batch_id: str) -> dict[str, Any] | None:
        if batch_id.startswith("pb-"):
            return self.snapshot(batch_id)
        if not re.fullmatch(r"wb-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}", batch_id):
            raise PlatformBatchStop("历史批次编号格式不正确。")
        path = self.output_dir / "write-batches" / f"{batch_id}.json"
        if not path.is_file():
            return None
        try:
            batch = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformBatchStop("旧批次记录无法读取。") from exc
        if not isinstance(batch, dict) or not isinstance(batch.get("records"), list):
            raise PlatformBatchStop("旧批次记录格式不正确。")
        records = batch["records"]
        if not records or not all(isinstance(row, dict) for row in records):
            return None
        orgs = {row.get("owner_org_id") for row in records}
        products = {row.get("product_id") for row in records}
        if len(orgs) != 1 or not all(type(org) is int and org > 0 for org in orgs):
            return None  # Cannot attribute this legacy batch safely.
        batch.update({
            "batch_id": batch_id, "owner": None, "org_id": orgs.pop(),
            "product_id": products.pop() if len(products) == 1 else None,
            "csv_download": f"/api/write-batches/{batch_id}/export.csv",
            "platform_activation": platform_activation.activation_artifact_summary(self.output_dir, batch_id),
        })
        return batch

    def index(self, owner: dict[str, Any]) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        paths = list(self.batch_dir.glob("pb-*.json"))
        paths.extend((self.output_dir / "write-batches").glob("wb-*.json"))
        for path in sorted(paths, key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                value = self.history_snapshot(path.stem)
            except (OSError, PlatformBatchStop):
                continue
            if not isinstance(value, dict):
                continue
            if not self.belongs_to(value, owner):
                continue
            records = value.get("records") if isinstance(value.get("records"), list) else []
            batch_id = path.stem
            items.append({
                "batch_id": batch_id,
                "owner": value.get("owner"),
                "created_at": value.get("created_at", ""),
                "state": value.get("state", "inventory_selected"),
                "stage": value.get("stage", "awaiting_qr"),
                "count": len(records),
                "org_id": value.get("org_id"),
                "product_id": value.get("product_id"),
                "qr_operations": value.get("qr_operations", "none"),
                "qr_ready_count": sum(record.get("qr_status") == "saved" for record in records),
                "platform_activation": platform_activation.activation_artifact_summary(self.output_dir, batch_id),
            })
        return items

    def export_csv(self, batch_id: str) -> bytes | None:
        path = self.batch_dir / f"{self.validate_id(batch_id)}.csv"
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError as exc:
            raise PlatformBatchStop("平台开户批次 CSV 无法读取。") from exc

    def mark_qr_started(self, batch_id: str) -> dict[str, Any]:
        with self.qr_lock:
            batch = self.snapshot(batch_id)
            if batch is None:
                raise PlatformBatchStop("平台开户批次不存在。")
            if batch.get("qr_operations") != "none":
                raise PlatformBatchStop("这个批次已经触发过二维码获取，禁止重复请求。")
            batch["state"] = "preparing_qr"
            batch["stage"] = "qr_once"
            batch["qr_operations"] = "started"
            self._persist(batch)
            with self.lock:
                self.batches[batch_id] = batch
            return batch

    def checkpoint_qr(self, batch_id: str, results: list[dict[str, Any]]) -> None:
        """Persist only operation metadata, including progress before task completion."""
        batch = self.snapshot(batch_id)
        if batch is None:
            raise PlatformBatchStop("平台开户批次不存在。")
        by_id = {result.get("inventory_id"): result for result in results}
        fields = {"state", "fetched_at", "lpa_status", "qr_status", "qr_path", "qr_sha256",
                  "qr_view_count_before", "qr_view_limit", "error"}
        for record in batch.get("records", []):
            result = by_id.get(record.get("inventory_id"), {})
            record.update({key: value for key, value in result.items() if key in fields})
        batch["updated_at"] = now()
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch

    def finish_qr(self, batch_id: str, results: list[dict[str, Any]]) -> dict[str, int]:
        batch = self.snapshot(batch_id)
        if batch is None:
            raise PlatformBatchStop("平台开户批次不存在。")
        by_id = {result.get("inventory_id"): result for result in results}
        safe_fields = {
            "state", "fetched_at", "lpa_status", "qr_status", "qr_path", "qr_sha256",
            "qr_view_count_before", "qr_view_limit", "error",
        }
        for record in batch.get("records", []):
            result = by_id.get(record.get("inventory_id"))
            if not isinstance(result, dict):
                continue
            for key in safe_fields:
                if key in result:
                    record[key] = result[key]
        counts: dict[str, int] = {}
        for result in results:
            state = str(result.get("state", "unknown"))
            counts[state] = counts.get(state, 0) + 1
        ready = counts.get("ready", 0)
        total = len(batch.get("records", []))
        batch["state"] = "qr_ready" if ready == total else "qr_partial"
        batch["stage"] = "ready_for_activation" if ready == total else "blocked"
        batch["qr_operations"] = "complete" if ready == total else "partial"
        batch["qr_result_summary"] = counts
        batch["finished_at"] = now()
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch
        return counts

    def abort_qr(self, batch_id: str, message: str) -> None:
        batch = self.snapshot(batch_id)
        if batch is None:
            return
        batch["state"] = "qr_failed"
        batch["stage"] = "blocked"
        batch["qr_operations"] = "partial"
        batch["qr_error"] = message
        batch["finished_at"] = now()
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch

    def qr_file(self, batch_id: str, sequence: int) -> bytes | None:
        if type(sequence) is not int or not 1 <= sequence <= MAX_BATCH_SIZE:
            raise PlatformBatchStop("二维码序号不正确。")
        batch = self.snapshot(batch_id)
        if batch is None:
            return None
        for record in batch.get("records", []):
            if record.get("sequence") != sequence or record.get("qr_status") != "saved":
                continue
            path = Path(str(record.get("qr_path") or "")).resolve()
            root = (self.batch_dir / batch_id / "qr").resolve()
            try:
                path.relative_to(root)
            except ValueError as exc:
                raise PlatformBatchStop("二维码文件路径校验失败。") from exc
            try:
                return path.read_bytes() if path.is_file() and path.suffix.lower() == ".png" else None
            except OSError as exc:
                raise PlatformBatchStop("二维码文件无法读取。") from exc
        return None

    def qr_batch_zip(self, batch_id: str) -> bytes | None:
        """将批次中所有已保存的二维码打包成ZIP文件。"""
        batch = self.snapshot(batch_id)
        if batch is None:
            return None

        root = (self.batch_dir / batch_id / "qr").resolve()
        saved_records = [
            record for record in batch.get("records", [])
            if record.get("qr_status") == "saved"
        ]

        if not saved_records:
            raise PlatformBatchStop("该批次没有已保存的二维码。")

        # 创建内存中的ZIP文件
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for record in saved_records:
                path = Path(str(record.get("qr_path") or "")).resolve()
                try:
                    path.relative_to(root)
                except ValueError:
                    continue

                try:
                    if path.is_file() and path.suffix.lower() == ".png":
                        # 使用 sequence-iccid.png 作为ZIP内的文件名
                        iccid = record.get("iccid", "unknown")
                        sequence = record.get("sequence", 0)
                        zip_filename = f"{sequence:03d}-{iccid}.png"
                        zip_file.writestr(zip_filename, path.read_bytes())
                except OSError:
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()

    def lpa_batch_file(self, batch_id: str) -> bytes | None:
        """生成包含所有已保存LPA的文本文件。"""
        batch = self.snapshot(batch_id)
        if batch is None:
            return None

        # 兼容旧数据：同时支持 lpa_status="saved" 和 "memory_only"
        saved_records = [
            record for record in batch.get("records", [])
            if record.get("lpa_status") in ("saved", "memory_only") or record.get("activation_code")
        ]

        if not saved_records:
            raise PlatformBatchStop("该批次没有已保存的LPA记录。")

        # 生成LPA列表文本文件
        lines = [f"# 批次 {batch_id} LPA 记录"]
        lines.append(f"# 生成时间: {now()}")
        lines.append(f"# 总计: {len(saved_records)} 条记录")
        lines.append("")

        for record in saved_records:
            sequence = record.get("sequence", 0)
            iccid = record.get("iccid", "unknown")
            lpa_path = record.get("lpa_path")
            activation_code = record.get("activation_code")

            lpa_code = None

            # 优先从文件读取
            if lpa_path and Path(lpa_path).is_file():
                try:
                    lpa_code = Path(lpa_path).read_text(encoding="utf-8").strip()
                except OSError:
                    pass

            # 如果文件不存在，尝试从内存记录读取（兼容旧数据）
            if not lpa_code and activation_code:
                lpa_code = activation_code

            if lpa_code:
                lines.append(f"# {sequence:03d} - {iccid}")
                lines.append(lpa_code)
                lines.append("")

        content = "\n".join(lines)
        return content.encode("utf-8")


def fetch_batch_qr(
    client: platform_activation.PlatformClient,
    batch: dict[str, Any],
    output_dir: Path,
    progress: ProgressCallback | None = None,
    cancelled: Callable[[], bool] | None = None,
    checkpoint: Callable[[list[dict[str, Any]]], None] | None = None,
) -> list[dict[str, Any]]:
    """Fetch each QR activation code exactly once and never retry unknown calls."""
    batch_id = str(batch.get("batch_id", ""))
    if not BATCH_ID_PATTERN.fullmatch(batch_id):
        raise PlatformBatchStop("平台开户批次编号格式不正确。")
    org_id = _positive_int(batch.get("org_id"), "组织 ID")
    product_id = _positive_int(batch.get("product_id"), "产品 ID")
    records = batch.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_SIZE:
        raise PlatformBatchStop("平台开户批次必须包含 1—200 张卡。")
    if progress:
        progress("inventory", 5, "正在只读复核平台库存和二维码次数……")
    current_rows = platform_activation.list_inventory(client)
    current = {row.get("id"): row for row in current_rows}
    verified: dict[int, dict[str, Any]] = {}
    for record in records:
        if not isinstance(record, dict):
            raise PlatformBatchStop("平台开户批次记录格式不正确。")
        inventory_id = _positive_int(record.get("inventory_id"), "库存 ID")
        row = current.get(inventory_id)
        view = _inventory_view(row, org_id, product_id) if isinstance(row, dict) else None
        if not view or not view["eligible"] or view.get("iccid") != record.get("iccid"):
            raise PlatformBatchStop(f"ICCID {record.get('iccid') or ''} 已不满足二维码获取条件。")
        verified[inventory_id] = view
    qr_dir = output_dir.resolve() / "platform-activation-batches" / batch_id / "qr"
    marker_dir = output_dir.resolve() / "platform-activation-batches" / batch_id / "qr-attempts"
    results: list[dict[str, Any]] = []
    stop_after_current = False
    total = len(records)
    for index, record in enumerate(records, 1):
        inventory_id = int(record["inventory_id"])
        iccid = str(record["iccid"])
        if stop_after_current or (cancelled and cancelled()):
            results.append({
                "sequence": index, "inventory_id": inventory_id, "iccid": iccid,
                "state": "skipped", "lpa_status": "not_requested", "qr_status": "not_requested",
                "error": "前一张结果未知或任务已停止，未请求二维码。",
            })
            continue
        live = verified[inventory_id]
        marker = marker_dir / f"{index:04d}-{inventory_id}.json"
        attempt = {
            "batch_id": batch_id,
            "sequence": index,
            "inventory_id": inventory_id,
            "iccid": iccid,
            "attempted_at": now(),
            "qr_view_count_before": live["qr_view_count"],
            "qr_view_limit": live["qr_view_limit"],
            "state": "request_started",
        }
        try:
            exclusive_write(marker, json.dumps(attempt, ensure_ascii=False, indent=2).encode("utf-8"))
            if progress:
                progress("qr", 5 + (index - 1) / total * 90, f"正在获取二维码：{index}/{total}")
            activation_code, response_meta = client.get_qr_code_once(inventory_id, iccid)
            if not LPA_PATTERN.fullmatch(activation_code):
                raise PlatformBatchStop("二维码接口返回的 LPA 格式不正确。")
            buffer = io.BytesIO()
            qrcode.make(activation_code, box_size=10, border=4).save(buffer, format="PNG")
            blob = buffer.getvalue()
            qr_path = qr_dir / f"{index:04d}-{iccid}.png"
            if qr_path.exists() and qr_path.read_bytes() != blob:
                raise PlatformBatchStop("已有同名二维码且内容不同，已停止。")
            atomic_write(qr_path, blob)
            # 同时保存LPA到文本文件
            lpa_path = qr_dir / f"{index:04d}-{iccid}.txt"
            atomic_write(lpa_path, activation_code.encode("utf-8"))
            fetched_at = now()
            attempt.update({"state": "saved", "saved_at": fetched_at, "qr_sha256": hashlib.sha256(blob).hexdigest()})
            atomic_write(marker, json.dumps(attempt, ensure_ascii=False, indent=2).encode("utf-8"))
            results.append({
                "sequence": index,
                "inventory_id": inventory_id,
                "iccid": iccid,
                "state": "ready",
                "fetched_at": fetched_at,
                "lpa_status": "saved",
                "qr_status": "saved",
                "qr_path": str(qr_path.resolve()),
                "lpa_path": str(lpa_path.resolve()),
                "qr_sha256": hashlib.sha256(blob).hexdigest(),
                "qr_view_count_before": live["qr_view_count"],
                "qr_view_limit": live["qr_view_limit"],
                "activation_code": activation_code,
                "response_status": response_meta.get("status"),
            })
        except platform_activation.PlatformActivationStop as exc:
            state = "unknown" if exc.unknown else "failed"
            attempt.update({"state": state, "finished_at": now(), "error": str(exc)})
            atomic_write(marker, json.dumps(attempt, ensure_ascii=False, indent=2).encode("utf-8"))
            results.append({
                "sequence": index, "inventory_id": inventory_id, "iccid": iccid,
                "state": state, "lpa_status": state, "qr_status": state,
                "qr_view_count_before": live["qr_view_count"], "qr_view_limit": live["qr_view_limit"],
                "error": str(exc),
            })
            stop_after_current = True
        except (OSError, PlatformBatchStop) as exc:
            attempt.update({"state": "failed", "finished_at": now(), "error": str(exc)})
            atomic_write(marker, json.dumps(attempt, ensure_ascii=False, indent=2).encode("utf-8"))
            results.append({
                "sequence": index, "inventory_id": inventory_id, "iccid": iccid,
                "state": "failed", "lpa_status": "failed", "qr_status": "failed", "error": str(exc),
            })
            stop_after_current = True
        if checkpoint:
            checkpoint(results)
        if progress:
            progress("qr", 5 + index / total * 90, f"二维码处理进度：{index}/{total}")
        time.sleep(0.2)
    if progress:
        ready = sum(result["state"] == "ready" for result in results)
        progress("done", 100, f"二维码任务结束：{ready}/{total} 张已保存。")
    return results
