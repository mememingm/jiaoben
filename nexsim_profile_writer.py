"""eSIM 写卡批次准备器。

当前版本只负责把状态查询结果整理成可复核的写卡批次清单。
二维码、activationCode、LPA 和实际写卡器调用暂未接入，且不会被读取或保存。
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


MAX_BATCH_SIZE = 200
RELEASED_STATUS = "RELEASED"
FORBIDDEN_KEY_PARTS = ("qr", "activationcode", "activation_code", "lpa")
FORBIDDEN_VALUE_PREFIXES = ("lpa:",)
ICCID_PATTERN = re.compile(r"^[0-9]{18,22}$")


class WriterStop(RuntimeError):
    """可直接显示给用户的写卡批次错误。"""


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


def _forbidden_key(key: str) -> bool:
    normalized = re.sub(r"[^a-z0-9_]", "", key.lower())
    return any(part in normalized for part in FORBIDDEN_KEY_PARTS)


def _contains_forbidden(value: Any) -> bool:
    if isinstance(value, dict):
        return any(_forbidden_key(str(key)) or _contains_forbidden(item)
                   for key, item in value.items())
    if isinstance(value, list):
        return any(_contains_forbidden(item) for item in value)
    if isinstance(value, str):
        return value.strip().lower().startswith(FORBIDDEN_VALUE_PREFIXES)
    return False


def _as_records(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, dict) and isinstance(value.get("records"), list):
        value = value["records"]
    if not isinstance(value, list) or not all(isinstance(row, dict) for row in value):
        raise WriterStop("状态文件必须包含 records 数组。")
    return value


def _safe_record(row: dict[str, Any], sequence: int) -> dict[str, Any]:
    """只把批次复核需要的非敏感字段写入清单。"""
    if _contains_forbidden(row):
        raise WriterStop("输入结果包含二维码、activationCode 或 LPA 字段；已停止，不会保存。")
    inventory_id = row.get("id")
    if type(inventory_id) is not int or inventory_id <= 0:
        raise WriterStop("候选卡缺少有效的库存记录 id。")
    iccid = row.get("iccid")
    if not isinstance(iccid, str) or not ICCID_PATTERN.fullmatch(iccid):
        raise WriterStop("候选卡的 ICCID 必须是 18—22 位纯数字文本。")
    if row.get("esimProfileStatus") != RELEASED_STATUS:
        raise WriterStop(f"ICCID {iccid} 不是 RELEASED，不能加入写卡批次。")
    if row.get("esimProfileStatusQueryStatus") != "ok":
        raise WriterStop(f"ICCID {iccid} 的 Profile 状态未成功查询，不能加入写卡批次。")
    if row.get("simType") not in (None, "ESIM"):
        raise WriterStop(f"ICCID {iccid} 不是 ESIM，不能加入写卡批次。")
    if row.get("inventoryType") not in (None, "P"):
        raise WriterStop(f"ICCID {iccid} 不是可写 Profile 库存类型。")
    return {
        "sequence": sequence,
        "inventory_id": inventory_id,
        "iccid": iccid,
        "inventory_status": row.get("status", ""),
        "product_id": row.get("productId"),
        "owner_org_id": row.get("ownerOrgId"),
        "profile_status": RELEASED_STATUS,
        "profile_status_updated_at": row.get("esimProfileStatusUpdatedAt", ""),
        "precheck_status": "not_checked",
        "lpa_status": "not_requested",
        "qr_status": "not_requested",
        "qr_path": "",
        "qr_sha256": "",
        "write_status": "not_started",
        "error": "",
    }


def select_released(rows: Iterable[dict[str, Any]], count: int = MAX_BATCH_SIZE) -> list[dict[str, Any]]:
    if type(count) is not int or not 1 <= count <= MAX_BATCH_SIZE:
        raise WriterStop(f"批次数量必须是 1—{MAX_BATCH_SIZE}。")
    selected: list[dict[str, Any]] = []
    seen_ids: set[int] = set()
    seen_iccids: set[str] = set()
    for row in rows:
        if row.get("esimProfileStatus") != RELEASED_STATUS:
            continue
        safe = _safe_record(row, len(selected) + 1)
        if safe["inventory_id"] in seen_ids or safe["iccid"] in seen_iccids:
            raise WriterStop("候选结果包含重复库存 id 或 ICCID，已停止。")
        seen_ids.add(safe["inventory_id"])
        seen_iccids.add(safe["iccid"])
        selected.append(safe)
        if len(selected) == count:
            break
    if len(selected) < count:
        raise WriterStop(f"可加入批次的 RELEASED 卡只有 {len(selected)} 张，少于目标 {count} 张。")
    return selected


def _csv_value(key: str, value: Any) -> str:
    if value is None:
        return ""
    text = str(value)
    if key == "iccid" and ICCID_PATTERN.fullmatch(text):
        return f'="{text}"'
    return text


def _write_csv(path: Path, records: list[dict[str, Any]]) -> None:
    fields = [
        "sequence", "inventory_id", "iccid", "inventory_status", "product_id",
        "owner_org_id", "profile_status", "profile_status_updated_at", "precheck_status",
        "lpa_status", "qr_status", "qr_path", "qr_sha256", "write_status", "error",
    ]
    from io import StringIO
    stream = StringIO(newline="")
    writer = csv.DictWriter(stream, fieldnames=fields)
    writer.writeheader()
    for record in records:
        writer.writerow({field: _csv_value(field, record.get(field)) for field in fields})
    atomic_write(path, stream.getvalue().encode("utf-8-sig"))


def create_batch(rows: Iterable[dict[str, Any]], output_dir: Path, count: int = MAX_BATCH_SIZE,
                 source: str = "status-query") -> dict[str, Any]:
    records = select_released(rows, count)
    batch_id = "wb-" + datetime.now(timezone(timedelta(hours=8))).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:8]
    batch_dir = output_dir.resolve() / "write-batches"
    batch = {
        "batch_id": batch_id,
        "state": "draft",
        "stage": "waiting_for_lpa",
        "created_at": now(),
        "source": source,
        "count": len(records),
        "max_batch_size": MAX_BATCH_SIZE,
        "qr_operations": "none",
        "lpa_operations": "none",
        "write_operations": "none",
        "requires_status_recheck_before_lpa": True,
        "next_step": "用户明确点击一次性按钮后，先复核 RELEASED，再逐张获取激活数据。",
        "records": records,
    }
    json_path = batch_dir / f"{batch_id}.json"
    csv_path = batch_dir / f"{batch_id}.csv"
    atomic_write(json_path, json.dumps(batch, ensure_ascii=False, indent=2).encode("utf-8"))
    _write_csv(csv_path, records)
    batch["json_path"] = str(json_path)
    batch["csv_path"] = str(csv_path)
    batch["csv_download"] = f"/api/write-batches/{batch_id}/export.csv"
    return batch


def load_status_file(path: Path) -> list[dict[str, Any]]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WriterStop(f"状态 JSON 无法读取：{path}") from exc
    return _as_records(value)


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="创建 RELEASED eSIM 写卡批次清单（二维码/LPA 暂不接入）")
    parser.add_argument("--input", required=True, type=Path, help="状态查询导出的 JSON 文件")
    parser.add_argument("--count", type=int, default=MAX_BATCH_SIZE, help="批次数量，默认 200，最大 200")
    parser.add_argument("--output-dir", type=Path, help="批次输出目录，默认使用输入文件所在目录")
    args = parser.parse_args(argv)
    rows = load_status_file(args.input)
    output_dir = args.output_dir or args.input.parent
    batch = create_batch(rows, output_dir, args.count, source=str(args.input))
    print(f"已创建写卡准备批次：{batch['batch_id']}")
    print(f"数量：{batch['count']} 张，筛选条件：esimProfileStatus = RELEASED")
    print(f"JSON：{batch['json_path']}")
    print(f"CSV：{batch['csv_path']}")
    print("当前未获取二维码、未读取 LPA、未执行写卡。")
    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
