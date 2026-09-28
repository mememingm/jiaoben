"""NexSim 管理后台批量 eSIM 工具。

流程分为四个明确阶段：inspect -> collect -> activate -> verify。
写操作只有 activate，且提交前必须传 --confirm-activation；提交结果未知时永不自动重发。
本工具使用的是当前 NexSim 管理后台实际接口，不是公开 Open API。
"""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import sys
import time
import zipfile
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape, quoteattr

import qrcode
import requests

class Stop(RuntimeError):
    """可直接显示给用户的安全错误，不包含密码、令牌或完整二维码。"""


def now() -> str:
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def atomic_write(path: Path, data: bytes) -> None:
    path = Path(path)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


@contextmanager
def exclusive_lock(path: Path):
    """同一输出目录只允许一个进程运行；进程崩溃后由操作系统释放锁。"""
    with Path(path).open("a+b") as stream:
        if os.fstat(stream.fileno()).st_size == 0:
            stream.write(b"0")
            stream.flush()
        stream.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise Stop("同一输出目录已有脚本运行。") from None
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == "nt":
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream.fileno(), fcntl.LOCK_UN)


def read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, json.JSONDecodeError) as exc:
        raise Stop(f"配置文件无法读取：{path}") from exc
    if not isinstance(value, dict):
        raise Stop("配置文件必须是 JSON 对象。")
    return value


def load_config(path: Path) -> dict[str, Any]:
    config = read_json(path)
    base = str(config.get("base_url", "")).rstrip("/")
    if not base.startswith("https://") or "/" in base.removeprefix("https://"):
        raise Stop("base_url 必须是类似 https://admin.nexsimus.com 的 HTTPS 域名。")
    required = ("credential_file", "org_id", "product_code", "target", "output_dir")
    missing = [key for key in required if key not in config]
    if missing:
        raise Stop("配置缺少字段：" + ", ".join(missing))
    if type(config["org_id"]) is not int or config["org_id"] <= 0:
        raise Stop("org_id 必须是正整数。")
    if not isinstance(config["product_code"], str) or not config["product_code"].strip():
        raise Stop("product_code 必须是非空字符串。")
    if type(config["target"]) is not int or not 1 <= config["target"] <= 500:
        raise Stop("target 必须是 1—500 的整数。")
    for key, default in (("poll_seconds", 5), ("poll_attempts", 360)):
        if key not in config:
            config[key] = default
        if type(config[key]) is not int or not 1 <= config[key] <= 3600:
            raise Stop(f"{key} 必须是 1—3600 的整数。")
    output = Path(str(config["output_dir"]))
    if not output.is_absolute():
        raise Stop("output_dir 必须使用绝对路径。")
    qr_dir = Path(str(config.get("qr_dir", output)))
    if not qr_dir.is_absolute():
        raise Stop("qr_dir 必须使用绝对路径。")
    template = config.get("csv_template")
    if template and not Path(str(template)).is_absolute():
        raise Stop("csv_template 必须使用绝对路径。")
    config["base_url"] = base
    config["output_dir"] = str(output)
    config["qr_dir"] = str(qr_dir)
    config["product_id"] = int(config["product_id"]) if config.get("product_id") is not None else None
    return config


def credentials(config: dict[str, Any]) -> tuple[str, str]:
    """读取两行凭据；密码永远不进入日志或配置快照。"""
    path = Path(str(config["credential_file"]))
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except OSError as exc:
        raise Stop(f"凭据文件无法读取：{path}") from exc
    lines = [line.strip() for line in lines if line.strip()]
    if len(lines) != 2 or not lines[0] or not lines[1]:
        raise Stop("凭据文件必须只有两行：第一行账号，第二行密码。")
    return lines[0], lines[1]


class Client:
    def __init__(self, config: dict[str, Any], ledger_dir: Path | None = None):
        self.config = config
        self.base = config["base_url"]
        self.username, password = credentials(config)
        self.session = requests.Session()
        try:
            response = self.session.post(
                self.base + "/api/auth/login",
                json={"username": self.username, "password": password},
                timeout=(10, 30),
                allow_redirects=False,
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
        if data.get("token") is None or user.get("orgId") != config["org_id"]:
            raise Stop("登录账号所属组织与配置的 org_id 不一致。")
        self.session.headers.update({
            "Authorization": "Bearer " + str(data["token"]),
            "Content-Type": "application/json",
        })
        self.ledger_dir = ledger_dir

    def _record(self, method: str, endpoint: str, status: int | None, payload: Any) -> None:
        if not self.ledger_dir or endpoint.endswith("/qr-code"):
            return
        self.ledger_dir.mkdir(parents=True, exist_ok=True)
        entry = {"checked_at": now(), "method": method, "endpoint": endpoint,
                 "http_status": status, "response": payload}
        with (self.ledger_dir / "http-evidence.ndjson").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def get(self, endpoint: str, params: dict[str, Any] | None = None) -> Any:
        try:
            response = self.session.get(self.base + endpoint, params=params,
                                        timeout=(10, 60), allow_redirects=False)
        except requests.RequestException as exc:
            raise Stop("查询网络失败。") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            self._record("GET", endpoint, response.status_code, {"error": "non_json"})
            raise Stop(f"查询返回非 JSON（HTTP {response.status_code}）。") from exc
        self._record("GET", endpoint, response.status_code, payload)
        if response.status_code != 200 or payload.get("code") != 0:
            raise Stop(f"查询失败（HTTP {response.status_code}）。")
        return payload.get("data")

    def post_json(self, endpoint: str, body: dict[str, Any]) -> Any:
        try:
            response = self.session.post(self.base + endpoint, json=body,
                                         timeout=(10, 120), allow_redirects=False)
        except requests.RequestException as exc:
            self._record("POST", endpoint, None, {"error": "transport_error"})
            raise Stop("写操作网络中断；结果待确认，禁止重发。") from exc
        try:
            payload = response.json()
        except ValueError as exc:
            self._record("POST", endpoint, response.status_code, {"error": "non_json"})
            raise Stop("写操作返回非 JSON；结果待确认，禁止重发。") from exc
        self._record("POST", endpoint, response.status_code, payload)
        if response.status_code != 200 or payload.get("code") != 0:
            raise Stop(f"写操作失败（HTTP {response.status_code}）；不要重发。")
        return payload.get("data")

    def post_csv(self, endpoint: str, csv_path: Path, body: dict[str, Any]) -> dict[str, Any]:
        # requests 必须自己生成 multipart boundary；不能沿用 JSON Content-Type。
        content_type = self.session.headers.pop("Content-Type", None)
        try:
            response = self.session.post(
                self.base + endpoint,
                files={"file": (csv_path.name, csv_path.read_bytes(), "text/csv"),
                       "request": ("blob", json.dumps(body).encode("utf-8"), "application/json")},
                timeout=(10, 120), allow_redirects=False,
            )
        except requests.RequestException as exc:
            self._record("POST", endpoint, None, {"error": "transport_error"})
            raise Stop("批量提交网络中断；结果待确认，禁止重发。") from exc
        finally:
            if content_type is not None:
                self.session.headers["Content-Type"] = content_type
        try:
            payload = response.json()
        except ValueError as exc:
            self._record("POST", endpoint, response.status_code, {"error": "non_json"})
            raise Stop("批量提交返回非 JSON；结果待确认，禁止重发。") from exc
        self._record("POST", endpoint, response.status_code, payload)
        if response.status_code != 200 or payload.get("code") != 0:
            raise Stop("批量提交返回失败；已保留回包，禁止重发。")
        return payload


def pages(client: Client, endpoint: str, params: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    base = dict(params or {})
    base.setdefault("pageSize", 100)
    page_no = 1
    rows: list[dict[str, Any]] = []
    while True:
        base["pageNo"] = page_no
        data = client.get(endpoint, base)
        if not isinstance(data, dict) or not isinstance(data.get("records"), list):
            raise Stop(f"{endpoint} 返回格式异常。")
        rows.extend(r for r in data["records"] if isinstance(r, dict))
        total_pages = int(data.get("pages", page_no))
        if page_no >= total_pages:
            return rows
        page_no += 1


def product(client: Client, config: dict[str, Any]) -> dict[str, Any]:
    products = client.get("/api/products", {"orgId": config["org_id"], "operationType": "ACTIVATION"})
    if not isinstance(products, list):
        raise Stop("套餐接口返回格式异常。")
    matches = [p for p in products if isinstance(p, dict)
               and p.get("productCode") == config["product_code"]]
    if config.get("product_id") is not None:
        matches = [p for p in matches if p.get("id") == config["product_id"]]
    if len(matches) != 1:
        raise Stop("找不到唯一匹配的套餐；请检查 product_code 和 product_id。")
    return matches[0]


def eligible(client: Client, config: dict[str, Any], product_id: int) -> list[dict[str, Any]]:
    rows = pages(client, "/api/inventory/page", {
        "status": "ALLOCATED", "simType": "ESIM", "iccid": "",
        "dateType": "IMPORTED",
    })
    selected = [r for r in rows if r.get("ownerOrgId") == config["org_id"]
                and r.get("productId") == product_id]
    for row in selected:
        if (row.get("inventoryType") != "P" or row.get("simType") != "ESIM"
                or row.get("status") != "ALLOCATED" or not row.get("qrCodeSupported")
                or not isinstance(row.get("iccid"), str) or not row["iccid"].isdigit()):
            raise Stop("库存包含不符合条件的卡；已停止，不创建订单。")
    if len({row["iccid"] for row in selected}) != len(selected):
        raise Stop("库存返回重复 ICCID。")
    return selected


def qr_path(config: dict[str, Any], iccid: str) -> Path:
    if not iccid.isdigit() or not 18 <= len(iccid) <= 22:
        raise Stop("ICCID 必须是 18—22 位数字文本。")
    return Path(config["qr_dir"]) / f"eSIM二维码_{iccid}.png"


def private_dir(config: dict[str, Any]) -> Path:
    return Path(config["output_dir"]) / ".nexsim-batch"


def bind(config: dict[str, Any], client: Client) -> None:
    path = private_dir(config) / "binding.json"
    value = {
        "base_url": config["base_url"], "org_id": config["org_id"],
        "customer_label": config.get("customer_label", ""),
        "product_code": config["product_code"], "product_id": config.get("product_id"),
        "target": config["target"],
        "username_sha256": hashlib.sha256(client.username.encode()).hexdigest(),
    }
    if path.exists() and read_json(path) != value:
        raise Stop("当前配置或账号与输出目录已有批次不一致；请使用新的 output_dir。")
    atomic_write(path, json.dumps(value, ensure_ascii=False, indent=2).encode("utf-8"))


def read_template(config: dict[str, Any]) -> None:
    path = config.get("csv_template")
    if not path:
        return
    try:
        with Path(path).open(encoding="utf-8-sig", newline="") as stream:
            if next(csv.reader(stream), None) != ["ICCID"]:
                raise Stop("csv_template 的表头必须是 ICCID。")
    except OSError as exc:
        raise Stop("csv_template 无法读取。") from exc


def write_csv(config: dict[str, Any], cards: list[dict[str, Any]]) -> Path:
    read_template(config)
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ICCID"])
    writer.writerows([[card["iccid"]] for card in cards])
    path = Path(config["output_dir"]) / f"开户激活_{len(cards)}.csv"
    atomic_write(path, output.getvalue().encode("utf-8-sig"))
    return path


def load_cards(config: dict[str, Any]) -> list[dict[str, Any]]:
    path = private_dir(config) / "cards.json"
    if not path.exists():
        raise Stop("还没有固定批次，请先运行 collect。")
    cards = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(cards, list) or not cards or len(cards) != config["target"]:
        raise Stop("固定批次数量与 target 不一致。")
    if len({c.get("iccid") for c in cards}) != len(cards):
        raise Stop("固定批次存在重复 ICCID。")
    for card in cards:
        path = qr_path(config, card["iccid"])
        if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != card.get("qr_sha256"):
            raise Stop(f"二维码缺失或已被修改：{card['iccid']}。")
    return cards


def export_excel_portable(config: dict[str, Any]) -> None:
    """用 Python 标准库生成最小 XLSX；ICCID 全部写成 inlineStr 文本。"""
    report_path = private_dir(config) / "report.json"
    if not report_path.exists():
        raise Stop("还没有核对数据，无法生成 Excel。")
    rows = json.loads(report_path.read_text(encoding="utf-8"))
    if not isinstance(rows, list):
        raise Stop("本地核对数据格式异常。")
    for row in rows:
        if not isinstance(row.get("iccid"), str) or not row["iccid"].isdigit():
            raise Stop("核对数据中的 ICCID 不是文本数字。")
    list_values = [["ICCID"]] + [[r["iccid"]] for r in rows]
    report_headers = ["ICCID", "激活情况", "订单号", "手机号", "订单状态", "号码状态",
                      "信号开通状态", "核对时间（北京时间）", "备注"]
    def label(row: dict[str, Any]) -> str:
        if row.get("phase") == "verified" and row.get("verified_at"):
            return "激活与信号均成功"
        if row.get("order_status") == "ACTIVE" and row.get("subscriber_status") == "ACTIVE":
            return "已激活，信号待开通"
        if row.get("order_status") == "ACTIVATION_UNKNOWN":
            return "激活结果待确认"
        return "尚未确认激活"
    report_values = [report_headers] + [[r["iccid"], label(r), r.get("order_no", ""),
        r.get("phone_number", ""), r.get("order_status", ""), r.get("subscriber_status", ""),
        r.get("signal_status", ""), r.get("checked_at") or r.get("verified_at", ""),
        r.get("note", "")] for r in rows]
    def col_name(index: int) -> str:
        name = ""
        while index:
            index, remainder = divmod(index - 1, 26)
            name = chr(65 + remainder) + name
        return name
    def sheet_xml(values: list[list[Any]], widths: list[int]) -> str:
        rows_xml = []
        for r_index, value_row in enumerate(values, 1):
            cells = []
            for c_index, value in enumerate(value_row, 1):
                cell_ref = f"{col_name(c_index)}{r_index}"
                text = escape(str(value or ""))
                style = 1 if r_index == 1 else 0
                cells.append(f'<c r={quoteattr(cell_ref)} t="inlineStr" s="{style}"><is><t>{text}</t></is></c>')
            rows_xml.append(f'<row r="{r_index}">' + "".join(cells) + "</row>")
        cols = "".join(f'<col min="{i}" max="{i}" width="{w}" customWidth="1"/>'
                        for i, w in enumerate(widths, 1))
        last = f"{col_name(len(widths))}{len(values)}"
        return (f'<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                f'<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
                f'<sheetViews><sheetView showGridLines="0"><pane ySplit="1" topLeftCell="A2" activePane="bottomLeft" state="frozen"/></sheetView></sheetViews>'
                f'<cols>{cols}</cols><sheetData>{"".join(rows_xml)}</sheetData>'
                f'<autoFilter ref="A1:{last}"/></worksheet>')
    content_types = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/xl/workbook.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet.main+xml"/>
<Override PartName="/xl/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.styles+xml"/>
<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
<Override PartName="/xl/worksheets/sheet2.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>
</Types>'''
    root_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="xl/workbook.xml"/></Relationships>'''
    workbook = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="ICCID清单" sheetId="1" r:id="rId1"/><sheet name="激活核对" sheetId="2" r:id="rId2"/></sheets></workbook>'''
    workbook_rels = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/><Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet2.xml"/><Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/></Relationships>'''
    styles = '''<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><numFmts count="1"><numFmt numFmtId="49" formatCode="@"/></numFmts><fonts count="2"><font><sz val="11"/><name val="Arial"/></font><font><b/><color rgb="FFFFFFFF"/><sz val="11"/><name val="Arial"/></font></fonts><fills count="2"><fill><patternFill patternType="none"/></fill><fill><patternFill patternType="solid"><fgColor rgb="FF23445A"/><bgColor indexed="64"/></patternFill></fill></fills><borders count="1"><border/></borders><cellStyleXfs count="1"><xf numFmtId="0"/></cellStyleXfs><cellXfs count="2"><xf numFmtId="49" fontId="0" fillId="0" borderId="0" applyNumberFormat="1"/><xf numFmtId="49" fontId="1" fillId="1" borderId="0" applyNumberFormat="1"/></cellXfs></styleSheet>'''
    output = Path(config["output_dir"]) / config.get("report_filename", "激活核对表.xlsx")
    temporary = output.with_name(output.name + ".tmp.xlsx")
    with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        archive.writestr("[Content_Types].xml", content_types)
        archive.writestr("_rels/.rels", root_rels)
        archive.writestr("xl/workbook.xml", workbook)
        archive.writestr("xl/_rels/workbook.xml.rels", workbook_rels)
        archive.writestr("xl/styles.xml", styles)
        archive.writestr("xl/worksheets/sheet1.xml", sheet_xml(list_values, [30]))
        archive.writestr("xl/worksheets/sheet2.xml", sheet_xml(report_values, [30, 24, 30, 20, 16, 16, 18, 30, 65]))
    os.replace(temporary, output)


def collect(client: Client, config: dict[str, Any], product_id: int) -> None:
    Path(config["output_dir"]).mkdir(parents=True, exist_ok=True)
    Path(config["qr_dir"]).mkdir(parents=True, exist_ok=True)
    pdir = private_dir(config)
    pdir.mkdir(parents=True, exist_ok=True)
    manifest = pdir / "cards.json"
    if manifest.exists():
        cards = json.loads(manifest.read_text(encoding="utf-8"))
    else:
        available = eligible(client, config, product_id)
        if len(available) < config["target"]:
            raise Stop(f"可用库存只有 {len(available)} 张，少于目标 {config['target']} 张。")
        cards = available[: config["target"]]
        atomic_write(manifest, json.dumps(cards, ensure_ascii=False, indent=2).encode("utf-8"))
    if len(cards) != config["target"]:
        raise Stop("固定清单数量与 target 不一致。")
    write_csv(config, cards)
    for index, card in enumerate(cards, 1):
        path = qr_path(config, card["iccid"])
        if card.get("qr_sha256"):
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != card["qr_sha256"]:
                raise Stop("已保存二维码缺失或内容改变。")
            continue
        data = client.get(f"/api/inventory/{card['id']}/qr-code")
        if not isinstance(data, dict) or data.get("cardId") != card["id"] \
                or data.get("iccid") != card["iccid"] or data.get("status") != "READY" \
                or not str(data.get("activationCode", "")).startswith("LPA:1$"):
            raise Stop(f"第 {index} 张二维码响应不符合预期。")
        stream = io.BytesIO()
        qrcode.make(data["activationCode"], box_size=10, border=4).save(stream, format="PNG")
        blob = stream.getvalue()
        if path.exists() and path.read_bytes() != blob:
            raise Stop("同名二维码已存在且内容不同。")
        atomic_write(path, blob)
        card["qr_sha256"] = hashlib.sha256(blob).hexdigest()
        card["qr_saved_at"] = now()
        atomic_write(manifest, json.dumps(cards, ensure_ascii=False, indent=2).encode("utf-8"))
        if index % 10 == 0 or index == len(cards):
            print(f"已保存二维码 {index}/{len(cards)}")
        time.sleep(0.2)
    report_rows = [{"iccid": c["iccid"], "phase": "prepared", "order_no": "",
                    "request_id": "", "verified_at": "", "checked_at": "",
                    "phone_number": "", "order_status": "", "subscriber_status": "",
                    "signal_status": "", "note": "已采集二维码，等待激活"} for c in cards]
    atomic_write(pdir / "report.json", json.dumps(report_rows, ensure_ascii=False).encode("utf-8"))
    export_excel_portable(config)
    print(f"采集完成：{len(cards)} 张二维码和 CSV；尚未执行激活。")


def order_and_subscriber(config: dict[str, Any], card: dict[str, Any], expected: dict[str, Any],
                         orders: list[dict[str, Any]], subscribers: list[dict[str, Any]]):
    matched = [o for o in orders if o.get("iccid") == card["iccid"]
               and o.get("cardId") == card["id"] and o.get("orgId") == config["org_id"]
               and o.get("productId") == card.get("productId")
               and (not expected or o.get("id") == expected.get(card["iccid"]))]
    order = max(matched, key=lambda o: str(o.get("id", ""))) if matched else {}
    subs = [s for s in subscribers if s.get("iccid") == card["iccid"]
            and s.get("cardId") == card["id"] and s.get("orgId") == config["org_id"]
            and s.get("productId") == card.get("productId")
            and s.get("orderId") == order.get("id")]
    return order, subs[0] if len(subs) == 1 else {}


def verify(client: Client, config: dict[str, Any]) -> bool:
    cards = load_cards(config)
    orders = pages(client, "/api/orders/page")
    subscribers = pages(client, "/api/orders/subscribers/page")
    response_path = private_dir(config) / "activation-response.json"
    expected: dict[str, Any] = {}
    if response_path.exists():
        try:
            data = read_json(response_path).get("data") or {}
            results = data.get("results", []) if isinstance(data, dict) else []
            expected = {str(r.get("iccid")): r.get("orderId") for r in results
                        if isinstance(r, dict) and r.get("iccid")}
        except (KeyError, TypeError):
            expected = {}
    result: list[dict[str, Any]] = []
    proofs: list[dict[str, Any]] = []
    for card in cards:
        order, sub = order_and_subscriber(config, card, expected, orders, subscribers)
        success = bool(order.get("orderStatus") == "ACTIVE"
                       and sub.get("subscriberStatus") == "ACTIVE"
                       and order.get("signalAddonStatus") == "SUCCESS"
                       and sub.get("signalAddonStatus") == "SUCCESS"
                       and sub.get("phoneNumber")
                       and sub.get("phoneNumber") == order.get("phoneNumber"))
        stamp = now()
        note = ("订单、号码、信号和二维码已逐项核对" if success else
                order.get("failureReason") or
                ("号码已激活，信号仍在平台处理中" if order.get("orderStatus") == "ACTIVE"
                 and sub.get("subscriberStatus") == "ACTIVE" else
                 "激活结果待确认或未满足全部成功条件"))
        result.append({"iccid": card["iccid"], "phase": "verified" if success else "unconfirmed",
                       "order_no": order.get("orderNo", ""), "request_id": "",
                       "verified_at": stamp if success else "", "checked_at": stamp,
                       "phone_number": sub.get("phoneNumber", ""),
                       "order_status": order.get("orderStatus", ""),
                       "subscriber_status": sub.get("subscriberStatus", ""),
                       "signal_status": sub.get("signalAddonStatus", ""), "note": note})
        proofs.append({"iccid": card["iccid"], "checked_at": stamp, "order": order, "subscriber": sub})
    pdir = private_dir(config)
    atomic_write(pdir / "verification-evidence.json", json.dumps(proofs, ensure_ascii=False, indent=2).encode("utf-8"))
    atomic_write(pdir / "report.json", json.dumps(result, ensure_ascii=False).encode("utf-8"))
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=list(result[0]))
    writer.writeheader(); writer.writerows(result)
    atomic_write(Path(config["output_dir"]) / "激活结果.csv", output.getvalue().encode("utf-8-sig"))
    export_excel_portable(config)
    confirmed = sum(row["phase"] == "verified" for row in result)
    print(f"逐卡确认成功 {confirmed}/{len(cards)}；核对时间 {now()}")
    return confirmed == len(cards)


def balance(client: Client, config: dict[str, Any]) -> Decimal:
    data = client.get(f"/api/finance/organizations/{config['org_id']}/balances")
    if not isinstance(data, list):
        raise Stop("余额接口返回格式异常。")
    for row in data:
        if row.get("balanceType") == "ACTIVATION":
            try:
                return Decimal(str(row["balance"]))
            except (KeyError, InvalidOperation) as exc:
                raise Stop("开卡余额格式异常。") from exc
    raise Stop("未找到 ACTIVATION 开卡余额。")


def activate(client: Client, config: dict[str, Any], product_data: dict[str, Any], max_total: Decimal) -> bool:
    cards = load_cards(config)
    pdir = private_dir(config); marker = pdir / "activation-intent.json"; response_path = pdir / "activation-response.json"
    csv_path = Path(config["output_dir"]) / f"开户激活_{len(cards)}.csv"
    if not marker.exists():
        current = {r["iccid"]: r["id"] for r in eligible(client, config, product_data["id"])}
        if any(current.get(c["iccid"]) != c["id"] for c in cards):
            raise Stop("部分卡已不再是可开卡库存；停止提交。")
        existing_orders = pages(client, "/api/orders/page")
        iccids = {c["iccid"] for c in cards}
        if any(o.get("iccid") in iccids for o in existing_orders):
            raise Stop("本批 ICCID 已有订单；停止以避免重复开卡。")
        try:
            unit = Decimal(str(product_data["displayPrice"]))
        except (KeyError, InvalidOperation) as exc:
            raise Stop("套餐单价格式异常。") from exc
        total = unit * len(cards); available_balance = balance(client, config)
        if product_data.get("productCode") != config["product_code"] or total > max_total or total > available_balance:
            raise Stop(f"套餐、金额上限或余额不满足：预计 {total}，上限 {max_total}，余额 {available_balance}。")
        if not csv_path.is_file():
            write_csv(config, cards)
        atomic_write(marker, json.dumps({"time": now(), "count": len(cards), "unit_price": str(unit),
                                         "total": str(total), "status": "submission_attempted"}, ensure_ascii=False).encode("utf-8"))
        payload = client.post_csv("/api/orders/batch-create-and-activate-csv", csv_path,
                                  {"orgId": config["org_id"], "productId": product_data["id"]})
        atomic_write(response_path, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    if not response_path.exists():
        raise Stop("提交结果文件不存在；结果未知，禁止重发。")
    response = read_json(response_path)
    job = (response.get("data") or {}).get("job")
    if job:
        for _ in range(config["poll_attempts"]):
            status = client.get(f"/api/orders/batch-jobs/{job['id']}")
            atomic_write(pdir / "job-status.json", json.dumps(status, ensure_ascii=False, indent=2).encode("utf-8"))
            print(f"平台处理中：成功 {status.get('successCount', 0)}，失败 {status.get('failedCount', 0)}，状态 {status.get('jobStatus')}")
            if status.get("jobStatus") not in {"PENDING", "PROCESSING"}:
                break
            time.sleep(config["poll_seconds"])
    return verify(client, config)


def emit(value: Any, as_json: bool) -> None:
    if as_json:
        print(json.dumps(value, ensure_ascii=False))
    elif isinstance(value, dict):
        for key, item in value.items():
            print(f"{key}: {item}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="NexSim 管理后台通用批量 eSIM 工具")
    parser.add_argument("--config", default="nexsim-admin.json", help="JSON 配置文件")
    parser.add_argument("--json", action="store_true", help="输出机器可读 JSON 摘要")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("doctor", help="只读检查配置、登录、套餐和库存")
    sub.add_parser("inspect", help="只读查看库存、套餐价格和余额")
    sub.add_parser("collect", help="只读采集二维码并生成 CSV/XLSX")
    run = sub.add_parser("activate", help="批量创建并激活，可能扣费")
    run.add_argument("--max-total", required=True, help="本批次金额上限，例如 1050.00")
    run.add_argument("--confirm-activation", action="store_true", help="确认执行扣费激活")
    sub.add_parser("verify", help="只读逐卡核对订单、号码和信号状态")
    sub.add_parser("report", help="只导出本地已有核对表")
    args = parser.parse_args(argv)
    try:
        config = load_config(Path(args.config))
        out = Path(config["output_dir"]); pdir = private_dir(config)
        out.mkdir(parents=True, exist_ok=True); pdir.mkdir(parents=True, exist_ok=True)
        if args.command == "report":
            export_excel_portable(config)
            return 0
        with exclusive_lock(pdir / "run.lock"):
            client = Client(config, pdir)
            bind(config, client)
            product_data = product(client, config)
            if args.command == "doctor":
                rows = eligible(client, config, product_data["id"])
                emit({"login": "ok", "customer": config.get("customer_label", ""),
                      "org_id": config["org_id"], "product_id": product_data["id"],
                      "product_code": product_data.get("productCode"), "eligible_count": len(rows)}, args.json)
                return 0
            if args.command == "inspect":
                rows = eligible(client, config, product_data["id"])
                emit({"eligible_count": len(rows), "target": config["target"],
                      "product_id": product_data["id"], "product_code": product_data.get("productCode"),
                      "unit_price": product_data.get("displayPrice"),
                      "activation_balance": str(balance(client, config))}, args.json)
                return 0
            if args.command == "collect":
                collect(client, config, product_data["id"])
                return 0
            if args.command == "verify":
                return 0 if verify(client, config) else 2
            if args.command == "activate":
                if not args.confirm_activation:
                    raise Stop("激活会扣费，必须同时传 --confirm-activation。")
                try:
                    limit = Decimal(args.max_total)
                except InvalidOperation as exc:
                    raise Stop("--max-total 必须是数字。") from exc
                if limit < 0:
                    raise Stop("--max-total 不能为负数。")
                return 0 if activate(client, config, product_data, limit) else 2
            raise Stop("未知命令。")
    except (Stop, OSError, ValueError, requests.RequestException) as exc:
        if args.json:
            print(json.dumps({"ok": False, "error": str(exc)}, ensure_ascii=False))
        else:
            print("停止：" + str(exc), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
