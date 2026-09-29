"""NexSim platform-side batch activation and verification.

This module never performs network work at import time. Callers must provide
credentials and explicitly choose preview, submit, or verify. Submission is
write-once per batch: an intent file is persisted before the external request,
and an uncertain response can only be investigated with verify, never retried.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests


MAX_BATCH_SIZE = 200
BATCH_ID_PATTERN = re.compile(r"^wb-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$")
ICCID_PATTERN = re.compile(r"^[0-9]{18,22}$")
ProgressCallback = Callable[[str, float, str], None]


class PlatformActivationStop(RuntimeError):
    """A safe, user-facing platform activation error."""

    def __init__(self, message: str, *, unknown: bool = False):
        super().__init__(message)
        self.unknown = unknown


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
    """Create a durable write-once marker without a check-then-write race."""
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        with path.open("xb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
    except FileExistsError as exc:
        raise PlatformActivationStop(
            "这个批次已经尝试过平台开户激活；禁止重发，请执行结果核验。"
        ) from exc


def _base_url(value: Any) -> str:
    raw = str(value or "").rstrip("/")
    parsed = urlsplit(raw)
    allowed = {"admin.nexsimus.com"}
    allowed.update(
        item.strip().lower()
        for item in os.environ.get("NEXSIM_ALLOWED_HOSTS", "").split(",")
        if item.strip()
    )
    if parsed.scheme != "https" or not parsed.hostname or parsed.port not in (None, 443):
        raise PlatformActivationStop("后台地址必须是纯 HTTPS 域名。")
    if parsed.path or parsed.query or parsed.fragment or parsed.username or parsed.password:
        raise PlatformActivationStop("后台地址不能包含路径、参数或登录信息。")
    if parsed.hostname.lower() not in allowed:
        raise PlatformActivationStop("后台域名不在允许列表中。")
    return raw


def _positive_int(value: Any, label: str) -> int:
    if type(value) is not int or value <= 0:
        raise PlatformActivationStop(f"{label}必须是正整数。")
    return value


def _money(value: Any, label: str) -> Decimal:
    try:
        result = Decimal(str(value))
        if not result.is_finite() or result < 0:
            raise InvalidOperation
        result = result.quantize(Decimal("0.01"))
    except (InvalidOperation, ValueError) as exc:
        raise PlatformActivationStop(f"{label}格式不正确。") from exc
    return result


class PlatformClient:
    """Authenticated client for platform activation endpoints."""

    def __init__(self, base_url: str, username: str, password: str, expected_org_id: int):
        self.base = _base_url(base_url)
        username = str(username or "").strip()
        if not username or not isinstance(password, str) or not password:
            raise PlatformActivationStop("平台开户需要账号和密码。")
        self.session = requests.Session()
        try:
            response = self.session.post(
                self.base + "/api/auth/login",
                json={"username": username, "password": password},
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PlatformActivationStop("登录网络失败。") from exc
        finally:
            password = ""
        payload = self._payload(response, "登录")
        data = payload.get("data")
        if response.status_code != 200 or payload.get("code") != 0 or not isinstance(data, dict):
            raise PlatformActivationStop(f"登录失败（HTTP {response.status_code}）。")
        user = data.get("user")
        token = data.get("token")
        if not isinstance(user, dict) or not token:
            raise PlatformActivationStop("登录响应缺少用户或令牌。")
        org_id = user.get("orgId")
        if org_id != expected_org_id:
            raise PlatformActivationStop("登录账号所属组织与填写的组织 ID 不一致。")
        self.org_id = expected_org_id
        self.session.headers["Authorization"] = "Bearer " + str(token)

    @staticmethod
    def _payload(response: requests.Response, operation: str) -> dict[str, Any]:
        try:
            payload = response.json()
        except ValueError as exc:
            raise PlatformActivationStop(f"{operation}返回非 JSON（HTTP {response.status_code}）。") from exc
        if not isinstance(payload, dict):
            raise PlatformActivationStop(f"{operation}响应格式不正确。")
        return payload

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if not endpoint.startswith("/api/") or "//" in endpoint:
            raise PlatformActivationStop("平台接口路径不正确。")
        try:
            response = self.session.get(
                self.base + endpoint,
                params=params,
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PlatformActivationStop("平台查询网络失败。") from exc
        payload = self._payload(response, "平台查询")
        if response.status_code != 200 or payload.get("code") != 0:
            raise PlatformActivationStop(f"平台查询失败（HTTP {response.status_code}）。")
        return payload.get("data")

    def submit_activation_csv(self, filename: str, blob: bytes, org_id: int, product_id: int) -> dict[str, Any]:
        try:
            response = self.session.post(
                self.base + "/api/orders/batch-create-and-activate-csv",
                files={
                    "file": (filename, blob, "text/csv"),
                    "request": (
                        "blob",
                        json.dumps({"orgId": org_id, "productId": product_id}),
                        "application/json",
                    ),
                },
                timeout=(10, 120),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise PlatformActivationStop(
                "激活提交响应未知；已禁止重发，请使用结果核验。",
                unknown=True,
            ) from exc
        try:
            payload = self._payload(response, "激活提交")
        except PlatformActivationStop as exc:
            raise PlatformActivationStop(
                "激活提交返回格式未知；已禁止重发，请使用结果核验。",
                unknown=True,
            ) from exc
        payload["_http_status"] = response.status_code
        return payload


def _all_pages(client: PlatformClient, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    values = dict(params or {})
    values.update({"pageNo": 1, "pageSize": 100})
    first = client.get(endpoint, values)
    if not isinstance(first, dict) or not isinstance(first.get("records"), list):
        raise PlatformActivationStop("平台分页响应格式不正确。")
    pages = first.get("pages", 1)
    if type(pages) is not int or not 1 <= pages <= 10000:
        raise PlatformActivationStop("平台分页数量异常。")
    rows = list(first["records"])
    for page_no in range(2, pages + 1):
        values["pageNo"] = page_no
        page = client.get(endpoint, values)
        if not isinstance(page, dict) or not isinstance(page.get("records"), list):
            raise PlatformActivationStop("平台分页响应格式不正确。")
        rows.extend(page["records"])
    if not all(isinstance(row, dict) for row in rows):
        raise PlatformActivationStop("平台分页记录格式不正确。")
    return rows


def _batch_records(batch: dict[str, Any], output_dir: Path) -> tuple[str, list[dict[str, Any]]]:
    batch_id = str(batch.get("batch_id", ""))
    if not BATCH_ID_PATTERN.fullmatch(batch_id):
        raise PlatformActivationStop("写卡批次编号格式不正确。")
    records = batch.get("records")
    if not isinstance(records, list) or not 1 <= len(records) <= MAX_BATCH_SIZE:
        raise PlatformActivationStop("写卡批次必须包含 1—200 张卡。")
    expected_qr_dir = (output_dir.resolve() / "write-batches" / batch_id / "qr").resolve()
    inventory_ids: set[int] = set()
    iccids: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for sequence, record in enumerate(records, 1):
        if not isinstance(record, dict):
            raise PlatformActivationStop("写卡批次记录格式不正确。")
        inventory_id = _positive_int(record.get("inventory_id"), "库存 ID")
        iccid = record.get("iccid")
        if not isinstance(iccid, str) or not ICCID_PATTERN.fullmatch(iccid):
            raise PlatformActivationStop("写卡批次包含无效 ICCID。")
        if inventory_id in inventory_ids or iccid in iccids:
            raise PlatformActivationStop("写卡批次包含重复库存 ID 或 ICCID。")
        if record.get("profile_status") != "RELEASED":
            raise PlatformActivationStop(f"ICCID {iccid} 不是 RELEASED。")
        if record.get("precheck_status") != "RELEASED" or record.get("qr_status") != "saved":
            raise PlatformActivationStop(f"ICCID {iccid} 尚未完成激活数据准备。")
        qr_path = Path(str(record.get("qr_path", ""))).resolve()
        try:
            qr_path.relative_to(expected_qr_dir)
        except ValueError as exc:
            raise PlatformActivationStop(f"ICCID {iccid} 的二维码路径超出批次目录。") from exc
        if not qr_path.is_file():
            raise PlatformActivationStop(f"ICCID {iccid} 的二维码文件不存在。")
        expected_hash = str(record.get("qr_sha256", ""))
        if not re.fullmatch(r"[a-f0-9]{64}", expected_hash):
            raise PlatformActivationStop(f"ICCID {iccid} 的二维码摘要无效。")
        if hashlib.sha256(qr_path.read_bytes()).hexdigest() != expected_hash:
            raise PlatformActivationStop(f"ICCID {iccid} 的二维码文件校验失败。")
        inventory_ids.add(inventory_id)
        iccids.add(iccid)
        normalized.append({
            "sequence": sequence,
            "inventory_id": inventory_id,
            "iccid": iccid,
        })
    return batch_id, normalized


def _product(client: PlatformClient, org_id: int, product_id: int) -> dict[str, Any]:
    products = client.get("/api/products", {"orgId": org_id, "operationType": "ACTIVATION"})
    if not isinstance(products, list):
        raise PlatformActivationStop("产品列表响应格式不正确。")
    matches = [item for item in products if isinstance(item, dict) and item.get("id") == product_id]
    if len(matches) != 1:
        raise PlatformActivationStop("指定产品不在当前组织的激活产品列表中。")
    return matches[0]


def _activation_balance(client: PlatformClient, org_id: int) -> Decimal:
    balances = client.get(f"/api/finance/organizations/{org_id}/balances")
    if not isinstance(balances, list):
        raise PlatformActivationStop("余额响应格式不正确。")
    matches = [item for item in balances if isinstance(item, dict) and item.get("balanceType") == "ACTIVATION"]
    if len(matches) != 1:
        raise PlatformActivationStop("未找到唯一的激活余额。")
    return _money(matches[0].get("balance"), "激活余额")


def preview_activation(
    client: PlatformClient,
    batch: dict[str, Any],
    output_dir: Path,
    org_id: int,
    product_id: int,
    max_total: Any,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    """Perform read-only eligibility, duplicate-order, product, and balance checks."""
    org_id = _positive_int(org_id, "组织 ID")
    product_id = _positive_int(product_id, "产品 ID")
    maximum = _money(max_total, "授权金额上限")
    batch_id, records = _batch_records(batch, output_dir)
    if progress:
        progress("inventory", 10, "正在复核库存资格……")
    inventory = _all_pages(client, "/api/inventory/page", {
        "status": "ALLOCATED",
        "simType": "ESIM",
        "iccid": "",
        "dateType": "IMPORTED",
    })
    eligible = {
        row.get("id"): row
        for row in inventory
        if row.get("ownerOrgId") == org_id and row.get("productId") == product_id
    }
    for record in records:
        row = eligible.get(record["inventory_id"])
        if not row or row.get("iccid") != record["iccid"]:
            raise PlatformActivationStop(f"ICCID {record['iccid']} 已不在可激活库存中。")
        if row.get("inventoryType") != "P" or row.get("simType") != "ESIM" \
                or row.get("status") != "ALLOCATED" or not row.get("qrCodeSupported"):
            raise PlatformActivationStop(f"ICCID {record['iccid']} 的库存属性不符合激活要求。")
    if progress:
        progress("orders", 45, "正在检查重复订单……")
    target_iccids = {record["iccid"] for record in records}
    orders = _all_pages(client, "/api/orders/page", {"orgId": org_id})
    duplicates = [row.get("iccid") for row in orders if row.get("iccid") in target_iccids]
    if duplicates:
        raise PlatformActivationStop(f"批次中有 {len(set(duplicates))} 张卡已经存在订单，已停止。")
    if progress:
        progress("finance", 70, "正在核对产品、金额与余额……")
    product = _product(client, org_id, product_id)
    unit_price = _money(product.get("displayPrice"), "产品单价")
    total = (unit_price * len(records)).quantize(Decimal("0.01"))
    balance = _activation_balance(client, org_id)
    if total > maximum:
        raise PlatformActivationStop("批次总金额超过本次授权上限。")
    if total > balance:
        raise PlatformActivationStop("激活余额不足。")
    if progress:
        progress("done", 100, "预检完成，尚未提交激活。")
    return {
        "batch_id": batch_id,
        "count": len(records),
        "org_id": org_id,
        "product_id": product_id,
        "product_code": str(product.get("productCode", "")),
        "product_name": str(product.get("productName") or product.get("name") or ""),
        "unit_price": str(unit_price),
        "total": str(total),
        "max_total": str(maximum),
        "activation_balance": str(balance),
        "eligible_count": len(records),
        "checked_at": now(),
    }


def _activation_csv(records: list[dict[str, Any]]) -> bytes:
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ICCID"])
    writer.writerows([[record["iccid"]] for record in records])
    return output.getvalue().encode("utf-8-sig")


def activation_directory(output_dir: Path, batch_id: str) -> Path:
    if not BATCH_ID_PATTERN.fullmatch(batch_id):
        raise PlatformActivationStop("写卡批次编号格式不正确。")
    return output_dir.resolve() / "write-batches" / batch_id / "platform-activation"


def activation_artifact_summary(output_dir: Path, batch_id: str) -> dict[str, Any]:
    """Return filesystem-only state; never contacts NexSim or reads QR data."""
    directory = activation_directory(output_dir, batch_id)
    has_intent = (directory / "intent.json").is_file()
    has_response = (directory / "response.json").is_file()
    has_unknown = (directory / "unknown.json").is_file()
    has_verification = (directory / "verification.csv").is_file()
    if has_verification:
        state = "verified"
    elif has_unknown:
        state = "unknown"
    elif has_response:
        state = "submitted"
    elif has_intent:
        state = "submission_attempted"
    else:
        state = "not_started"
    return {
        "state": state,
        "has_submission_intent": has_intent,
        "has_verification": has_verification,
        "verification_csv_url": (
            f"/api/write-batches/{batch_id}/platform-activation/verification.csv"
            if has_verification else ""
        ),
    }


def read_activation_artifact(output_dir: Path, batch_id: str, filename: str) -> bytes | None:
    """Read an explicitly allowlisted, non-secret downloadable artifact."""
    if filename != "verification.csv":
        raise PlatformActivationStop("平台开户结果文件不允许下载。")
    path = activation_directory(output_dir, batch_id) / filename
    try:
        return path.read_bytes() if path.is_file() else None
    except OSError as exc:
        raise PlatformActivationStop("平台开户核验结果无法读取。") from exc


def submit_activation(
    client: PlatformClient,
    batch: dict[str, Any],
    output_dir: Path,
    org_id: int,
    product_id: int,
    max_total: Any,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    def precheck_progress(stage: str, percent: float, message: str) -> None:
        if progress:
            progress(stage, percent * 0.5, message)

    preview = preview_activation(
        client, batch, output_dir, org_id, product_id, max_total,
        precheck_progress if progress else None,
    )
    batch_id, records = _batch_records(batch, output_dir)
    directory = activation_directory(output_dir, batch_id)
    intent_path = directory / "intent.json"
    response_path = directory / "response.json"
    csv_blob = _activation_csv(records)
    csv_path = directory / "activation.csv"
    atomic_write(csv_path, csv_blob)
    intent = {
        "batch_id": batch_id,
        "created_at": now(),
        "state": "submission_attempted",
        "count": len(records),
        "org_id": org_id,
        "product_id": product_id,
        "unit_price": preview["unit_price"],
        "total": preview["total"],
        "max_total": preview["max_total"],
        "iccids_sha256": hashlib.sha256("\n".join(item["iccid"] for item in records).encode()).hexdigest(),
    }
    exclusive_write(intent_path, json.dumps(intent, ensure_ascii=False, indent=2).encode("utf-8"))
    if progress:
        progress("submit", 55, "正在提交一次性平台开户激活请求……")
    try:
        response = client.submit_activation_csv(csv_path.name, csv_blob, org_id, product_id)
    except PlatformActivationStop as exc:
        if exc.unknown:
            atomic_write(
                directory / "unknown.json",
                json.dumps({"time": now(), "error": str(exc)}, ensure_ascii=False, indent=2).encode("utf-8"),
            )
        raise
    atomic_write(response_path, json.dumps(response, ensure_ascii=False, indent=2).encode("utf-8"))
    if response.get("_http_status") != 200 or response.get("code") != 0 or not isinstance(response.get("data"), dict):
        raise PlatformActivationStop("平台拒绝激活请求；回包已保存，禁止自动重发。")
    job = response["data"].get("job")
    final_job: dict[str, Any] | None = None
    if isinstance(job, dict) and type(job.get("id")) is int:
        for attempt in range(360):
            final_job = client.get(f"/api/orders/batch-jobs/{job['id']}")
            if not isinstance(final_job, dict):
                raise PlatformActivationStop("平台批量任务响应格式不正确。")
            atomic_write(directory / "job-status.json", json.dumps(final_job, ensure_ascii=False, indent=2).encode("utf-8"))
            job_state = str(final_job.get("jobStatus", ""))
            if progress:
                completed = int(final_job.get("successCount", 0) or 0) + int(final_job.get("failedCount", 0) or 0)
                percent = 60 + min(completed / max(len(records), 1), 1) * 35
                progress("poll", percent, f"平台处理中：{completed}/{len(records)}，状态 {job_state or 'UNKNOWN'}")
            if job_state not in {"PENDING", "PROCESSING"}:
                break
            time.sleep(5)
        else:
            raise PlatformActivationStop("平台任务仍在处理中；请稍后执行结果核验。")
    if progress:
        progress("submitted", 100, "激活提交阶段结束，请执行结果核验。")
    safe_job = None
    if isinstance(final_job, dict):
        safe_job = {
            key: final_job.get(key)
            for key in ("id", "jobStatus", "totalCount", "successCount", "failedCount", "createdAt", "updatedAt")
            if key in final_job
        }
    return {"preview": preview, "job": safe_job, "submitted_at": now()}


def verify_activation(
    client: PlatformClient,
    batch: dict[str, Any],
    output_dir: Path,
    org_id: int,
    product_id: int,
    progress: ProgressCallback | None = None,
) -> dict[str, Any]:
    org_id = _positive_int(org_id, "组织 ID")
    product_id = _positive_int(product_id, "产品 ID")
    batch_id, records = _batch_records(batch, output_dir)
    directory = activation_directory(output_dir, batch_id)
    response_path = directory / "response.json"
    expected: dict[str, Any] = {}
    if response_path.is_file():
        try:
            stored_response = json.loads(response_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise PlatformActivationStop("已保存的激活回包无法读取。") from exc
        data = stored_response.get("data") if isinstance(stored_response, dict) else None
        results = data.get("results", []) if isinstance(data, dict) else []
        if isinstance(results, list):
            expected = {
                row.get("iccid"): row.get("orderId")
                for row in results
                if isinstance(row, dict) and isinstance(row.get("iccid"), str)
            }
    if progress:
        progress("orders", 20, "正在读取订单结果……")
    orders = _all_pages(client, "/api/orders/page", {"orgId": org_id})
    if progress:
        progress("subscribers", 55, "正在读取号码与信号状态……")
    subscribers = _all_pages(client, "/api/orders/subscribers/page", {"orgId": org_id})
    rows: list[dict[str, Any]] = []
    evidence: list[dict[str, Any]] = []
    for index, card in enumerate(records, 1):
        matched = [
            order for order in orders
            if order.get("iccid") == card["iccid"]
            and order.get("cardId") == card["inventory_id"]
            and order.get("orgId") == org_id
            and order.get("productId") == product_id
            and (not expected or order.get("id") == expected.get(card["iccid"]))
        ]
        order = max(matched, key=lambda item: item.get("id", 0)) if matched else {}
        related = [
            subscriber for subscriber in subscribers
            if subscriber.get("iccid") == card["iccid"]
            and subscriber.get("cardId") == card["inventory_id"]
            and subscriber.get("orgId") == org_id
            and subscriber.get("productId") == product_id
            and subscriber.get("orderId") == order.get("id")
        ]
        subscriber = related[0] if len(related) == 1 else {}
        phone = str(subscriber.get("phoneNumber") or "")
        success = bool(
            order.get("orderStatus") == "ACTIVE"
            and subscriber.get("subscriberStatus") == "ACTIVE"
            and subscriber.get("signalAddonStatus") == "SUCCESS"
            and order.get("signalAddonStatus") == "SUCCESS"
            and phone
            and phone == str(order.get("phoneNumber") or "")
        )
        row = {
            "sequence": index,
            "iccid": card["iccid"],
            "state": "verified" if success else "unconfirmed",
            "order_no": str(order.get("orderNo") or ""),
            "phone_number": phone,
            "order_status": str(order.get("orderStatus") or ""),
            "subscriber_status": str(subscriber.get("subscriberStatus") or ""),
            "signal_status": str(subscriber.get("signalAddonStatus") or ""),
            "checked_at": now(),
            "message": "订单、号码和信号均已确认" if success else str(
                order.get("failureReason") or "尚未满足全部成功条件"
            ),
        }
        rows.append(row)
        evidence.append({
            "iccid": card["iccid"],
            "inventory_id": card["inventory_id"],
            "order_id": order.get("id"),
            "subscriber_id": subscriber.get("id"),
            "order_status": row["order_status"],
            "subscriber_status": row["subscriber_status"],
            "signal_status": row["signal_status"],
            "checked_at": row["checked_at"],
        })
        if progress:
            progress("verify", 60 + index / len(records) * 40, f"正在核验：{index}/{len(records)}")
    atomic_write(directory / "verification.json", json.dumps(evidence, ensure_ascii=False, indent=2).encode("utf-8"))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(rows[0]))
    writer.writeheader()
    writer.writerows(rows)
    atomic_write(directory / "verification.csv", output.getvalue().encode("utf-8-sig"))
    confirmed = sum(row["state"] == "verified" for row in rows)
    return {
        "batch_id": batch_id,
        "count": len(rows),
        "confirmed": confirmed,
        "all_confirmed": confirmed == len(rows),
        "checked_at": now(),
        "verification_csv_url": f"/api/write-batches/{batch_id}/platform-activation/verification.csv",
        "rows": rows,
    }
