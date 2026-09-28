"""NexSim eSIM 原始状态查询工具。

只做后台登录，以及库存和 Profile 状态 GET 查询。
二维码、激活码、二维码查看次数等内容永久禁止触碰。
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

import requests


FORBIDDEN_TERMS = (
    "qr-code", "qrcode", "qr_code", "activationcode", "activation_code", "lpa:",
    "qrviewstatus", "qrviewcount", "qr_view_status", "qr_view_count",
)
FORBIDDEN_KEYS = {
    "activationcode", "activation_code", "qrcode", "qrcodeurl", "qrcode_url",
    "qrviewstatus", "qrviewcount", "qr_view_status", "qr_view_count",
}
INVENTORY_PAGE_ENDPOINT = "/api/inventory/page"
ESIM_USAGE_STATUS_PATTERN = re.compile(
    r"^/api/inventory/([1-9][0-9]*)/esim-usage-status$"
)
ProgressCallback = Callable[[str, int, int], None]
CancelCallback = Callable[[], bool]


class Stop(RuntimeError):
    """Safe, actionable error shown without credentials or response secrets."""


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


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"配置或数据文件无法读取：{path}") from exc
    if not isinstance(value, dict):
        raise Stop("JSON 文件必须是对象。")
    return value


def is_forbidden_endpoint(endpoint: str) -> bool:
    value = endpoint.lower()
    return any(term in value for term in FORBIDDEN_TERMS)


def is_forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]", "", key.lower())
    forbidden = {re.sub(r"[^a-z0-9_]", "", item.lower()) for item in FORBIDDEN_KEYS}
    return normalized.startswith("qr") or normalized in forbidden or "activationcode" in normalized


def strip_forbidden(value: Any) -> Any:
    """保留平台原始字段和值，但删除二维码/激活码字段。"""
    if isinstance(value, dict):
        return {key: strip_forbidden(item) for key, item in value.items()
                if not is_forbidden_key(str(key))}
    if isinstance(value, list):
        return [strip_forbidden(item) for item in value]
    if isinstance(value, str) and value.lower().startswith("lpa:"):
        return "[REDACTED_FORBIDDEN_ACTIVATION_CODE]"
    return value


def validate_config(config: dict[str, Any], *, require_credential_file: bool) -> dict[str, Any]:
    config = dict(config)
    base = str(config.get("base_url", "")).rstrip("/")
    parsed = urlsplit(base)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password \
            or parsed.query or parsed.fragment or parsed.path not in ("", "/"):
        raise Stop("base_url 必须是纯 HTTPS 域名，例如 https://admin.nexsimus.com。")
    credential_file = config.get("credential_file")
    if require_credential_file and (not isinstance(credential_file, str) or not credential_file):
        raise Stop("缺少 credential_file。")
    if credential_file is not None and not isinstance(credential_file, str):
        raise Stop("credential_file 必须是文件路径或 null。")
    if config.get("org_id") is not None \
            and (type(config["org_id"]) is not int or config["org_id"] <= 0):
        raise Stop("org_id 必须是正整数或 null。")
    if config.get("product_id") is not None and type(config["product_id"]) is not int:
        raise Stop("product_id 必须是整数或 null。")
    if not isinstance(config.get("output_dir"), str) or not Path(config["output_dir"]).is_absolute():
        raise Stop("output_dir 必须是绝对路径。")
    page_size = config.get("page_size", 100)
    if type(page_size) is not int or not 1 <= page_size <= 100:
        raise Stop("page_size 必须是 1—100。")
    profile_workers = config.get("profile_workers", 8)
    if type(profile_workers) is not int or not 1 <= profile_workers <= 16:
        raise Stop("profile_workers 必须是 1—16。")
    query_params = config.get("query_params", {"simType": "ESIM"})
    if not isinstance(query_params, dict):
        raise Stop("query_params 必须是 JSON 对象。")
    config["base_url"] = base
    config["page_size"] = page_size
    config["profile_workers"] = profile_workers
    config["query_params"] = query_params
    return config


def load_config(path: Path) -> dict[str, Any]:
    return validate_config(read_json(path), require_credential_file=True)


def read_credentials(path: Path) -> tuple[str, str]:
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    except OSError as exc:
        raise Stop(f"凭据文件无法读取：{path}") from exc
    if len(lines) != 2:
        raise Stop("凭据文件必须只有两行：账号、密码。")
    return lines[0], lines[1]


class Client:
    def __init__(self, config: dict[str, Any], credentials: tuple[str, str] | None = None):
        self.base = config["base_url"]
        if credentials is None:
            credential_file = config.get("credential_file")
            if not credential_file:
                raise Stop("缺少账号密码或 credential_file。")
            username, password = read_credentials(Path(credential_file))
        else:
            username, password = credentials
            if not username.strip() or not password:
                raise Stop("账号和密码不能为空。")
            username = username.strip()
        self.username = username
        self.session = requests.Session()
        try:
            response = self.session.post(
                self.base + "/api/auth/login",
                json={"username": username, "password": password},
                timeout=(10, 30), allow_redirects=False,
            )
        except requests.RequestException as exc:
            raise Stop("登录网络失败。") from exc
        finally:
            password = ""
        try:
            payload = response.json()
        except ValueError as exc:
            raise Stop(f"登录返回非 JSON（HTTP {response.status_code}）。") from exc
        if response.status_code != 200 or payload.get("code") != 0:
            raise Stop(f"登录失败（HTTP {response.status_code}）。")
        data = payload.get("data") or {}
        user = data.get("user") or {}
        self.org_id = user.get("orgId")
        if not data.get("token") or type(self.org_id) is not int or self.org_id <= 0:
            raise Stop("登录响应缺少令牌或有效的组织 ID。")
        if config.get("org_id") is not None and self.org_id != config["org_id"]:
            raise Stop("账号所属组织与配置中的 org_id 不一致。")
        self.session.headers.update({
            "Authorization": "Bearer " + str(data["token"]),
            "Content-Type": "application/json",
        })

    def clone_for_worker(self) -> "Client":
        """为并发只读请求创建独立连接，共享登录后的令牌和 Cookie。"""
        worker = object.__new__(Client)
        worker.base = self.base
        worker.username = self.username
        worker.org_id = self.org_id
        worker.session = requests.Session()
        worker.session.headers.update(self.session.headers)
        worker.session.cookies.update(self.session.cookies)
        return worker

    def _get_json(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        if is_forbidden_endpoint(endpoint):
            raise Stop("安全拦截：二维码或激活码接口永久禁止调用。")
        if endpoint != INVENTORY_PAGE_ENDPOINT and not ESIM_USAGE_STATUS_PATTERN.fullmatch(endpoint):
            raise Stop("安全拦截：状态工具只允许库存分页和 Profile 状态 GET 接口。")
        try:
            response = self.session.get(self.base + endpoint, params=params or {},
                                        timeout=(10, 60), allow_redirects=False)
        except requests.RequestException as exc:
            raise Stop("状态查询网络失败；未执行其他操作。") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            raise Stop(f"状态查询返回非 JSON（HTTP {response.status_code}）。") from exc
        if response.status_code != 200 or payload.get("code") != 0:
            raise Stop(f"状态查询失败（HTTP {response.status_code}）。")
        return payload.get("data")

    def get(self, endpoint: str, params: dict[str, Any]) -> Any:
        """查询库存分页；其他路径必须通过专用方法访问。"""
        if endpoint != INVENTORY_PAGE_ENDPOINT:
            raise Stop("安全拦截：库存分页必须使用固定接口路径。")
        return self._get_json(endpoint, params)

    def get_esim_usage_status(self, inventory_id: int) -> dict[str, Any]:
        """按库存记录数字 id 查询 Profile 原始状态。"""
        if type(inventory_id) is not int or inventory_id <= 0:
            raise Stop("库存记录缺少有效的数字 id。")
        endpoint = f"/api/inventory/{inventory_id}/esim-usage-status"
        data = self._get_json(endpoint)
        if not isinstance(data, dict):
            raise Stop("Profile 状态接口返回格式异常。")
        return data


def _record_profile_failure(row: dict[str, Any], error: str) -> None:
    """记录逐条 Profile 查询失败，不伪造原始状态值。"""
    row.pop("esimProfileStatus", None)
    row.pop("esimProfileUpdatedAt", None)
    row.pop("esimProfileStatusUpdatedAt", None)
    row["esimProfileStatusQueryStatus"] = "failed"
    row["esimProfileStatusQueryError"] = error


def enrich_profile_status(
    client: Client,
    rows: list[dict[str, Any]],
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
    workers: int = 8,
) -> None:
    """用专用只读接口补充每条记录的 Profile 状态。"""
    total = len(rows)
    if not rows:
        return

    def query_one(index: int, row: dict[str, Any]) -> tuple[int, dict[str, Any] | None, str | None]:
        if cancelled and cancelled():
            return index, None, "查询已取消。"
        inventory_id = row.get("id")
        if type(inventory_id) is not int or inventory_id <= 0:
            return index, None, "库存记录缺少有效的数字 id。"
        worker_client = getattr(thread_local, "client", None)
        if worker_client is None:
            worker_client = client.clone_for_worker()
            thread_local.client = worker_client
        try:
            profile = worker_client.get_esim_usage_status(inventory_id)
            if "status" not in profile:
                return index, None, "Profile 状态接口未返回 data.status。"
            return index, profile, None
        except Stop as exc:
            return index, None, str(exc)
        except Exception:
            return index, None, "Profile 状态查询异常。"

    # 先处理库存接口偶尔附带的同名字段，再并发请求专用 Profile 接口。
    for row in rows:
        # 库存接口偶尔会带同名字段，保留它但明确标注来源，避免覆盖专用接口结果。
        inventory_status = row.pop("esimProfileStatus", None)
        inventory_updated_at = row.pop("esimProfileUpdatedAt", None)
        if inventory_status is not None:
            row["inventoryEsimProfileStatus"] = inventory_status
        if inventory_updated_at is not None:
            row["inventoryEsimProfileUpdatedAt"] = inventory_updated_at

    thread_local = threading.local()
    completed = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="profile-status") as executor:
        futures = [executor.submit(query_one, index, row)
                   for index, row in enumerate(rows)]
        for future in as_completed(futures):
            if cancelled and cancelled():
                raise Stop("查询已取消。")
            index, profile, error = future.result()
            row = rows[index]
            if error:
                _record_profile_failure(row, error)
            else:
                row["esimProfileStatus"] = strip_forbidden(profile["status"])
                if "lastUpdatedAt" in profile:
                    row["esimProfileStatusUpdatedAt"] = strip_forbidden(profile["lastUpdatedAt"])
                row["esimProfileStatusQueryStatus"] = "ok"
            completed += 1
            if progress:
                progress("profile", completed, total)


def fetch_rows(
    client: Client,
    config: dict[str, Any],
    iccids: set[str] | None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    page_no = 1
    while True:
        if cancelled and cancelled():
            raise Stop("查询已取消。")
        params = dict(config["query_params"])
        params.update(pageNo=page_no, pageSize=config["page_size"])
        data = client.get(INVENTORY_PAGE_ENDPOINT, params)
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise Stop("库存状态接口返回格式异常。")
        rows.extend(strip_forbidden(row) for row in data["records"] if isinstance(row, dict))
        total_pages = data.get("pages", page_no)
        try:
            total_pages = int(total_pages)
        except (TypeError, ValueError) as exc:
            raise Stop("库存分页字段 pages 格式异常。") from exc
        if progress:
            progress("inventory", page_no, max(total_pages, page_no))
        if page_no >= total_pages:
            break
        page_no += 1
    filtered = []
    for row in rows:
        if row.get("ownerOrgId") not in (None, config["org_id"]):
            continue
        if config.get("product_id") is not None and row.get("productId") not in (None, config["product_id"]):
            continue
        if row.get("simType") not in (None, "ESIM"):
            continue
        if iccids and row.get("iccid") not in iccids:
            continue
        filtered.append(row)
    seen = [row.get("iccid") for row in filtered if row.get("iccid")]
    if len(seen) != len(set(seen)):
        raise Stop("状态查询结果包含重复 ICCID。")
    enrich_profile_status(client, filtered, progress, cancelled,
                          workers=config.get("profile_workers", 8))
    return filtered


def read_iccids(path: Path) -> set[str]:
    try:
        with path.open(encoding="utf-8-sig", newline="") as stream:
            reader = csv.DictReader(stream)
            if reader.fieldnames != ["ICCID"]:
                raise Stop("ICCID 文件必须只有 ICCID 一列。")
            values = {row["ICCID"].strip() for row in reader if row.get("ICCID")}
    except OSError as exc:
        raise Stop(f"ICCID 文件无法读取：{path}") from exc
    if not values or any(not re.fullmatch(r"[0-9]{18,22}", value) for value in values):
        raise Stop("ICCID 必须是 18—22 位纯数字文本。")
    return values


def parse_iccids(text: str) -> set[str]:
    """解析界面中用换行、逗号或分号分隔的 ICCID。"""
    values = {value for value in re.split(r"[\s,;，；]+", text.strip()) if value}
    if not values or any(not re.fullmatch(r"[0-9]{18,22}", value) for value in values):
        raise Stop("ICCID 必须是 18—22 位纯数字；多个 ICCID 可换行填写。")
    return values


def flatten_value(value: Any) -> str:
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if value is None:
        return ""
    return str(value)


def csv_value(key: str, value: Any) -> str:
    """让 Excel 打开报告 CSV 时把 ICCID 当文本，不显示科学计数法。"""
    text = flatten_value(value)
    if key.lower() == "iccid" and re.fullmatch(r"[0-9]{18,22}", text):
        return f'="{text}"'
    return text


def write_outputs(
    rows: list[dict[str, Any]],
    config: dict[str, Any],
    source: str,
    *,
    account: str | None = None,
) -> tuple[Path, Path]:
    out = Path(config["output_dir"])
    out.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S")
    json_path = out / f"esim-status-raw-{stamp}.json"
    csv_path = out / f"esim-status-raw-{stamp}.csv"
    metadata = {"queried_at": now(), "source": source,
                "account": account or "未知账号",
                "sources": [INVENTORY_PAGE_ENDPOINT,
                            "/api/inventory/{inventoryId}/esim-usage-status"],
                "count": len(rows),
                "qr_operations": "none", "records": rows}
    atomic_write(json_path, json.dumps(metadata, ensure_ascii=False, indent=2).encode("utf-8"))
    keys = sorted({str(key) for row in rows for key in row})
    io_string = io.StringIO(newline="")
    with io_string:
        writer = csv.DictWriter(io_string, fieldnames=keys, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            writer.writerow({key: csv_value(key, row.get(key)) for key in keys})
        atomic_write(csv_path, io_string.getvalue().encode("utf-8-sig"))
    return json_path, csv_path


def query_status(
    config: dict[str, Any],
    iccid_file: Path | None = None,
    *,
    iccids: set[str] | None = None,
    credentials: tuple[str, str] | None = None,
    progress: ProgressCallback | None = None,
    cancelled: CancelCallback | None = None,
) -> tuple[list[dict[str, Any]], Path, Path]:
    if iccid_file is not None and iccids is not None:
        raise Stop("ICCID 文件和 ICCID 列表不能同时使用。")
    targets = read_iccids(iccid_file) if iccid_file else iccids
    client = Client(config, credentials)
    effective_config = dict(config)
    effective_config["org_id"] = client.org_id
    rows = fetch_rows(client, effective_config, targets, progress, cancelled)
    if cancelled and cancelled():
        raise Stop("查询已取消。")
    source = (f"GET {INVENTORY_PAGE_ENDPOINT}; "
              "GET /api/inventory/{inventoryId}/esim-usage-status")
    if progress:
        progress("export", 0, 1)
    json_path, csv_path = write_outputs(rows, config, source, account=client.username)
    if progress:
        progress("export", 1, 1)
    return rows, json_path, csv_path


def status_command(config: dict[str, Any], iccid_file: Path | None) -> int:
    rows, json_path, csv_path = query_status(config, iccid_file)
    raw_statuses = sorted({str(row["status"]) for row in rows if "status" in row})
    profile_statuses = sorted({str(row["esimProfileStatus"])
                               for row in rows if "esimProfileStatus" in row})
    profile_failures = sum(row.get("esimProfileStatusQueryStatus") == "failed" for row in rows)
    print(f"已保存原始状态：{len(rows)} 条")
    print(f"原始 status 值：{', '.join(raw_statuses) if raw_statuses else '接口未返回 status 字段'}")
    print(f"原始 esimProfileStatus 值：{', '.join(profile_statuses) if profile_statuses else '无成功结果'}")
    print(f"Profile 状态查询失败：{profile_failures} 条")
    print(f"JSON：{json_path}")
    print(f"CSV：{csv_path}")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NexSim eSIM 原始状态查询工具（二维码永久禁用）")
    parser.add_argument("--config", default="nexsim-status.json")
    sub = parser.add_subparsers(dest="command")
    sub.add_parser("gui", help="打开可视化界面")
    status = sub.add_parser("status", help="只读查询 eSIM 原始库存状态")
    status.add_argument("--iccid-file", type=Path, help="可选，ICCID 单列 CSV")
    args = parser.parse_args(argv)
    try:
        if args.command in (None, "gui"):
            from nexsim_status_gui import launch
            launch()
            return 0
        if args.command == "status":
            config = load_config(Path(args.config))
            return status_command(config, args.iccid_file)
        raise Stop("未知命令。")
    except (Stop, OSError, ValueError, requests.RequestException) as exc:
        print("停止：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
