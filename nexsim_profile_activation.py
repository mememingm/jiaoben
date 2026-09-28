"""一次性获取 eSIM 激活数据并生成二维码。

本模块不会在导入时调用网络。只有 Web 页面明确触发一次性任务时，才会：

1. 登录后台；
2. 对批次中的每张卡调用一次二维码接口；
3. 从响应中读取 activationCode（LPA）；
4. 在本地生成 PNG。

不自动重试二维码请求。请求超时或连接结果未知时，任务会停止，避免重复消耗查看次数。
LPA 只保存在当前任务内存中，不写入批次 JSON/CSV 或日志。
"""
from __future__ import annotations

import hashlib
import io
import os
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable

import qrcode
import requests


QR_ENDPOINT_PATTERN = re.compile(r"^/api/inventory/([1-9][0-9]*)/qr-code$")
STATUS_ENDPOINT_PATTERN = re.compile(r"^/api/inventory/([1-9][0-9]*)/esim-usage-status$")
LPA_PATTERN = re.compile(r"^LPA:1\$[^\s$]+\$[^\s$]+(?:\$[^\s$]+)*$")
ProgressCallback = Callable[[str, int, int, str], None]
CancelCallback = Callable[[], bool]


class ActivationStop(RuntimeError):
    """不会自动重试的激活数据错误。"""

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


def _validate_base_url(base_url: str) -> str:
    value = str(base_url or "").rstrip("/")
    if not re.fullmatch(r"https://[^/]+", value):
        raise ActivationStop("后台地址必须是纯 HTTPS 域名。")
    return value


class OneShotClient:
    """登录一次，并保证每个 inventory id 最多发出一次二维码 GET。"""

    def __init__(self, base_url: str, username: str, password: str):
        self.base = _validate_base_url(base_url)
        username = str(username or "").strip()
        if not username or not isinstance(password, str) or not password:
            raise ActivationStop("获取激活数据需要账号和密码。")
        self.session = requests.Session()
        try:
            response = self.session.post(
                self.base + "/api/auth/login",
                json={"username": username, "password": password},
                timeout=(10, 30),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ActivationStop("登录网络失败。") from exc
        finally:
            password = ""
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActivationStop(f"登录返回非 JSON（HTTP {response.status_code}）。") from exc
        if response.status_code != 200 or payload.get("code") != 0:
            raise ActivationStop(f"登录失败（HTTP {response.status_code}）。")
        data = payload.get("data") or {}
        user = data.get("user") or {}
        token = data.get("token")
        org_id = user.get("orgId")
        if not token or type(org_id) is not int or org_id <= 0:
            raise ActivationStop("登录响应缺少令牌或组织 ID。")
        self.org_id = org_id
        self.session.headers.update({
            "Authorization": "Bearer " + str(token),
            "Content-Type": "application/json",
        })
        self.requested_ids: set[int] = set()
        self.status_checked_ids: set[int] = set()

    def get_profile_status_once(self, inventory_id: int, expected_iccid: str) -> str:
        if type(inventory_id) is not int or inventory_id <= 0:
            raise ActivationStop("批次包含无效的 inventory id。")
        endpoint = f"/api/inventory/{inventory_id}/esim-usage-status"
        if not STATUS_ENDPOINT_PATTERN.fullmatch(endpoint):
            raise ActivationStop("Profile 状态接口路径校验失败。")
        if inventory_id in self.status_checked_ids:
            raise ActivationStop(f"inventory id {inventory_id} 已复核过，禁止重复复核。")
        self.status_checked_ids.add(inventory_id)
        try:
            response = self.session.get(
                self.base + endpoint,
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的 Profile 状态复核结果未知；未请求二维码，已停止。",
                unknown=True,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的 Profile 状态响应不是 JSON；未请求二维码，已停止。",
                unknown=True,
            ) from exc
        if response.status_code != 200 or payload.get("code") != 0:
            raise ActivationStop(f"ICCID {expected_iccid} 的 Profile 状态复核失败；未请求二维码。")
        data = payload.get("data")
        if not isinstance(data, dict) or "status" not in data:
            raise ActivationStop(f"ICCID {expected_iccid} 的 Profile 状态响应格式异常；未请求二维码。")
        return str(data["status"]).upper()

    def get_qr_code_once(self, inventory_id: int, expected_iccid: str) -> tuple[str, dict[str, Any]]:
        if type(inventory_id) is not int or inventory_id <= 0:
            raise ActivationStop("批次包含无效的 inventory id。")
        endpoint = f"/api/inventory/{inventory_id}/qr-code"
        if not QR_ENDPOINT_PATTERN.fullmatch(endpoint):
            raise ActivationStop("二维码接口路径校验失败。")
        if inventory_id in self.requested_ids:
            raise ActivationStop(f"inventory id {inventory_id} 已请求过，禁止重复请求。")
        self.requested_ids.add(inventory_id)
        try:
            response = self.session.get(
                self.base + endpoint,
                timeout=(10, 60),
                allow_redirects=False,
            )
        except requests.Timeout as exc:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的二维码请求超时；结果未知，已停止且不会重试。",
                unknown=True,
            ) from exc
        except requests.RequestException as exc:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的二维码请求网络中断；结果未知，已停止且不会重试。",
                unknown=True,
            ) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的二维码响应不是 JSON；结果未知，已停止且不会重试。",
                unknown=True,
            ) from exc
        if response.status_code != 200 or payload.get("code") != 0:
            raise ActivationStop(
                f"ICCID {expected_iccid} 的二维码接口返回失败（HTTP {response.status_code}）。"
            )
        data = payload.get("data")
        if not isinstance(data, dict):
            raise ActivationStop(f"ICCID {expected_iccid} 的二维码响应格式异常。")
        if data.get("cardId") != inventory_id or data.get("iccid") != expected_iccid:
            raise ActivationStop(f"ICCID {expected_iccid} 的二维码身份不匹配。")
        if data.get("status") != "READY":
            raise ActivationStop(f"ICCID {expected_iccid} 的二维码状态不是 READY。")
        activation_code = data.get("activationCode")
        if not isinstance(activation_code, str) or not LPA_PATTERN.fullmatch(activation_code):
            raise ActivationStop(f"ICCID {expected_iccid} 未返回有效 LPA。")
        return activation_code, {"status": data.get("status"), "cardId": data.get("cardId"), "iccid": data.get("iccid")}


def render_qr_png(activation_code: str) -> bytes:
    if not isinstance(activation_code, str) or not LPA_PATTERN.fullmatch(activation_code):
        raise ActivationStop("无法为无效 LPA 生成二维码。")
    buffer = io.BytesIO()
    qrcode.make(activation_code, box_size=10, border=4).save(buffer, format="PNG")
    return buffer.getvalue()


def fetch_batch_profile_data(
    batch_id: str,
    records: list[dict[str, Any]],
    output_dir: Path,
    base_url: str,
    credentials: tuple[str, str],
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> list[dict[str, Any]]:
    """逐张获取 LPA 并生成二维码；LPA 只返回内存结果，不落盘。"""
    if not records or len(records) > 200:
        raise ActivationStop("一次性任务的批次数量必须是 1—200 张。")
    client = OneShotClient(base_url, credentials[0], credentials[1])
    qr_dir = output_dir.resolve() / "write-batches" / batch_id / "qr"
    results: list[dict[str, Any]] = []
    total = len(records)
    for index, record in enumerate(records, 1):
        if cancelled and cancelled():
            raise ActivationStop("获取激活数据已取消；未对后续卡发起请求。")
        inventory_id = record.get("inventory_id")
        iccid = record.get("iccid")
        base_result = {
            "sequence": record.get("sequence", index),
            "inventory_id": inventory_id,
            "iccid": iccid,
            "fetched_at": now(),
        }
        if progress:
            progress("qr", index - 1, total, f"准备请求第 {index}/{total} 张：{iccid}")
        try:
            profile_status = client.get_profile_status_once(inventory_id, iccid)
            base_result["precheck_status"] = profile_status
            if profile_status != "RELEASED":
                base_result.update({
                    "state": "skipped",
                    "lpa_status": "not_requested",
                    "qr_status": "not_requested",
                    "error": f"状态复核为 {profile_status}，未请求二维码。",
                })
                results.append(base_result)
                if progress:
                    progress("qr", index, total, f"第 {index}/{total} 张状态为 {profile_status}，已跳过")
                continue
            activation_code, response_meta = client.get_qr_code_once(inventory_id, iccid)
            qr_blob = render_qr_png(activation_code)
            qr_path = qr_dir / f"{int(record.get('sequence', index)):04d}-{iccid}.png"
            atomic_write(qr_path, qr_blob)
            base_result.update({
                "state": "ready",
                "lpa_status": "fetched_in_memory",
                "qr_status": "saved",
                "qr_path": str(qr_path),
                "qr_sha256": hashlib.sha256(qr_blob).hexdigest(),
                "response_meta": response_meta,
                # The LPA is intentionally not written to disk; the Web job keeps it in memory.
                "activation_code": activation_code,
            })
            results.append(base_result)
            if progress:
                progress("qr", index, total, f"已获取第 {index}/{total} 张 LPA，并生成二维码")
        except ActivationStop as exc:
            base_result.update({
                "state": "unknown" if exc.unknown else "failed",
                "lpa_status": "unknown" if exc.unknown else "not_available",
                "qr_status": "not_saved",
                "error": str(exc),
            })
            results.append(base_result)
            if exc.unknown:
                # A timeout may have consumed the one-time view. Do not touch any later card.
                for skipped in records[index:]:
                    results.append({
                        "sequence": skipped.get("sequence"),
                        "inventory_id": skipped.get("inventory_id"),
                        "iccid": skipped.get("iccid"),
                        "state": "skipped",
                        "lpa_status": "not_requested",
                        "qr_status": "not_requested",
                        "error": "前一张卡的二维码请求结果未知，已停止后续请求。",
                    })
                break
            if progress:
                progress("qr", index, total, f"第 {index}/{total} 张未获取成功，未重试")
    return results
