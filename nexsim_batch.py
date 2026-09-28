"""NexSim 1.6: freeze inventory, render QR locally, activate once, verify twice."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

import qrcode
import requests
from PIL import Image

PRODUCT = "P_VOICE_SMS_30M_100"
CUSTOMER = "pddhuhu（老平台）"


class Stop(RuntimeError):
    """An actionable, credential-free error safe to show in the console."""


def now():
    return datetime.now(timezone(timedelta(hours=8))).isoformat(timespec="seconds")


def atomic_write(path, data):
    path = Path(path)
    tmp = path.with_name(path.name + ".tmp")
    with tmp.open("wb") as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(tmp, path)


@contextmanager
def exclusive_lock(path):
    # OS releases the lock after a crash; no manual stale-lock deletion needed.
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


class Ledger:
    def __init__(self, path):
        self.db = sqlite3.connect(path)
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
            CREATE TABLE IF NOT EXISTS cards (
                iccid TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL,
                phase TEXT NOT NULL DEFAULT 'prepared', order_no TEXT,
                qr_sha256 TEXT, verified_at TEXT, note TEXT);
            CREATE TABLE IF NOT EXISTS evidence (
                id INTEGER PRIMARY KEY, beijing_time TEXT, method TEXT,
                endpoint TEXT, http_status INTEGER, response TEXT);
        """)
        self.db.row_factory = sqlite3.Row

    def cards(self):
        return [dict(r) for r in self.db.execute("SELECT * FROM cards ORDER BY rowid")]

    def update(self, iccid, **values):
        allowed = {"phase", "order_no", "qr_sha256", "verified_at", "note"}
        if not set(values) <= allowed:
            raise ValueError("invalid ledger field")
        with self.db:
            self.db.execute("UPDATE cards SET " + ",".join(f"{k}=?" for k in values)
                            + " WHERE iccid=?", [*values.values(), iccid])

    def record(self, method, endpoint, status, response):
        # Tokens and QR activation codes never enter the evidence database.
        if endpoint == "/auth/token" or endpoint.endswith("/qr-code"):
            return
        with self.db:
            self.db.execute("INSERT INTO evidence VALUES(NULL,?,?,?,?,?)",
                            (now(), method, endpoint, status,
                             json.dumps(response, ensure_ascii=False)))


class Api:
    def __init__(self, base_url, client_id, secret, ledger):
        self.base = base_url
        self.client_id, self.secret, self.ledger = client_id, secret, ledger
        self.session = requests.Session()
        self.token = None
        self.expires_at = 0

    def _request(self, method, endpoint, body=None, params=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        kwargs = dict(params=params, headers=headers, timeout=(10, 45),
                      allow_redirects=False)
        if body is not None:
            kwargs["json"] = body
        try:
            response = self.session.request(method, self.base + endpoint, **kwargs)
        except requests.RequestException:
            self.ledger.record(method, endpoint, None, {"error": "transport_error"})
            raise Stop("网络失败或超时；写操作结果待确认，禁止盲目重发。") from None
        try:
            envelope = response.json()
        except ValueError:
            self.ledger.record(method, endpoint, response.status_code,
                               {"error": "non_json_response"})
            raise Stop("接口返回非 JSON；已停止，不输出响应正文。") from None
        evidence_endpoint = endpoint + ("?" + urlencode(params) if params else "")
        self.ledger.record(method, evidence_endpoint, response.status_code, envelope)
        if not 200 <= response.status_code < 300 or not isinstance(envelope, dict) \
                or type(envelope.get("code")) is not int or envelope["code"] != 0:
            raise Stop(f"API 失败（HTTP {response.status_code}），查看本地证据；不重发写操作。")
        return envelope.get("data")

    def call(self, method, endpoint, body=None, params=None):
        if not self.token or time.monotonic() >= self.expires_at:
            data = self._request("POST", "/auth/token", {
                "clientId": self.client_id, "clientSecret": self.secret})
            if not isinstance(data, dict) or not isinstance(data.get("accessToken"), str) \
                    or not data["accessToken"]:
                raise Stop("令牌响应格式不完整。")
            self.token = data["accessToken"]
            self.expires_at = time.monotonic() + max(0, int(data.get("expiresIn", 0)) - 60)
        # No automatic POST retries, including 401/409/5xx and activation timeout.
        return self._request(method, endpoint, body, params, self.token)


def load_config(path, require_status=False):
    config = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    base = config.get("base_url", "").rstrip("/")
    url = urlsplit(base)
    if url.scheme != "https" or not url.hostname or url.username or url.password \
            or url.query or url.fragment or url.path != "/open-api/v1" \
            or "REPLACE" in base:
        raise Stop("base_url 必须是平台提供的 https://域名/open-api/v1。")
    if config.get("customer_label") != CUSTOMER \
            or config.get("credential_customer_confirmed") is not True:
        raise Stop("请先向平台确认凭证属于 pddhuhu（老平台），再设置确认字段。")
    if type(config.get("target")) is not int or not 1 <= config["target"] <= 500:
        raise Stop("target 必须是 1—500 的整数；本任务应为 496。")
    for key in ("poll_seconds", "poll_attempts"):
        if type(config.get(key)) is not int or not 1 <= config[key] <= 3600:
            raise Stop(f"{key} 必须是 1—3600 的整数。")
    if not Path(config.get("output_dir", "")).is_absolute():
        raise Stop("output_dir 必须是绝对路径。")
    if require_status:
        statuses = [config.get(k) for k in ("order_created_status", "order_success_status")]
        if any(not isinstance(s, str) or not s.strip() for s in statuses) \
                or statuses[0] == statuses[1]:
            raise Stop("文档未定义订单状态枚举；请填入平台确认的待激活和成功状态值。")
    config["base_url"] = base
    return config


def read_csv(path):
    with Path(path).open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != ["ICCID"]:
            raise Stop("CSV 必须只有一列，表头为 ICCID；用文本保存，不能转为科学计数法。")
        values = []
        for row in reader:
            if None in row or not isinstance(row.get("ICCID"), str):
                raise Stop("CSV 行格式错误。")
            values.append(row["ICCID"].strip())
    if not values or any(not re.fullmatch(r"[0-9]{18,22}", v) for v in values):
        raise Stop("ICCID 必须为 18—22 位纯数字文本，不能有空行或科学计数法。")
    if len(set(values)) != len(values):
        raise Stop("CSV 中有重复 ICCID；不会静默去重。")
    return values


def bind_batch(ledger, config, client_id):
    # Prevent accidentally resuming this ledger with another account or target.
    binding = {k: config[k] for k in ("base_url", "customer_label", "target")}
    binding.update(product=PRODUCT, client_id_sha256=hashlib.sha256(client_id.encode()).hexdigest())
    value = json.dumps(binding, sort_keys=True, ensure_ascii=False)
    row = ledger.db.execute("SELECT value FROM meta WHERE key='binding'").fetchone()
    if row and row[0] != value:
        raise Stop("当前配置/凭证与已固定批次不符；不得换账号继续或重新选卡。")
    with ledger.db:
        ledger.db.execute("INSERT OR IGNORE INTO meta VALUES('binding',?)", (value,))


def qr_path(config, iccid):
    if not re.fullmatch(r"[0-9]{18,22}", iccid):
        raise Stop("库存 ICCID 格式异常。")
    return Path(config["output_dir"]) / f"eSIM二维码_{iccid}.png"


def check_qr(config, card):
    path = qr_path(config, card["iccid"])
    if not card["qr_sha256"] or not path.is_file() \
            or hashlib.sha256(path.read_bytes()).hexdigest() != card["qr_sha256"]:
        raise Stop("二维码缺失或内容改变；禁止计为成功或继续激活。")
    with Image.open(path) as img:
        img.verify()


def prepare(api, ledger, config, csv_path=None):
    if config.get("csv_template"):
        with Path(config["csv_template"]).open(encoding="utf-8-sig", newline="") as source:
            if next(csv.reader(source), None) != ["ICCID"]:
                raise Stop("指定 CSV 模板表头不是 ICCID，停止以避免错误导入。")
    cards = ledger.cards()
    if not cards:
        inventory = api.call("GET", "/inventory", params={"productCode": PRODUCT})
        if not isinstance(inventory, list):
            raise Stop("库存响应不是文档规定的数组。")
        available = []
        for item in inventory:
            if not isinstance(item, dict) or item.get("productCode") != PRODUCT \
                    or item.get("inventoryType") != "P":
                raise Stop("库存接口返回了其他套餐/卡类型；请让平台核对映射。")
            iccid = item.get("iccid")
            if not isinstance(iccid, str):
                raise Stop("接口 ICCID 必须为字符串，避免精度损失。")
            qr_path(config, iccid)
            available.append(iccid)
        if len(available) != len(set(available)):
            raise Stop("库存接口有重复 ICCID。")
        selected = read_csv(csv_path) if csv_path else sorted(available)[:config["target"]]
        if len(selected) != config["target"] or not set(selected) <= set(available):
            raise Stop("没有足够的目标套餐库存，或 CSV 数量/所属库存不符；未创建任何订单。")
        with ledger.db:
            ledger.db.executemany("INSERT INTO cards(iccid,request_id) VALUES(?,?)",
                                  [(v, "nx-" + uuid.uuid4().hex) for v in selected])
        cards = ledger.cards()
    elif csv_path and read_csv(csv_path) != [c["iccid"] for c in cards]:
        raise Stop("CSV 与已固定批次不同；不会替换 ICCID。")
    if len(cards) != config["target"]:
        raise Stop("台账数量与目标不一致。")
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["ICCID"])
    writer.writerows([[c["iccid"]] for c in cards])
    atomic_write(Path(config["output_dir"]) / "iccid.csv", output.getvalue().encode("utf-8-sig"))
    for index, card in enumerate(cards, 1):
        if card["qr_sha256"]:
            check_qr(config, card)
            continue
        data = api.call("GET", f"/inventory/{card['iccid']}/qr-code")
        if not isinstance(data, dict) or data.get("iccid") != card["iccid"] \
                or data.get("status") not in {"AVAILABLE", "RESTORED"} \
                or not isinstance(data.get("activationCode"), str) \
                or not data["activationCode"].startswith("LPA:1$"):
            raise Stop("二维码响应的 ICCID、可用状态或激活码格式不符合文档。")
        image = qrcode.make(data["activationCode"], box_size=10, border=4)
        buffer = io.BytesIO()
        image.save(buffer, format="PNG")
        blob = buffer.getvalue()
        path = qr_path(config, card["iccid"])
        if path.exists() and path.read_bytes() != blob:
            raise Stop("同名二维码已经存在且内容不同；保留原文件并停止。")
        atomic_write(path, blob)
        digest = hashlib.sha256(blob).hexdigest()
        ledger.update(card["iccid"], qr_sha256=digest, note="二维码已保存；尚未确认激活")
        print(f"二维码已准备 {index}/{len(cards)}")


def checked_order(data, card):
    if not isinstance(data, dict) or data.get("iccid") != card["iccid"] \
            or data.get("productCode") != PRODUCT \
            or data.get("orderNo") != card["order_no"]:
        raise Stop("订单号、ICCID 或套餐不匹配，停止处理。")
    return data


def verify_card(api, ledger, config, card):
    check_qr(config, card)
    if not card["order_no"]:
        # A POST may have succeeded before the connection/process was lost.
        # ICCID history does not expose requestId in v1.6; never guess an order.
        if card["phase"] == "create_attempted":
            api.call("GET", "/orders", params={"iccid": card["iccid"]})
        return False
    order = checked_order(api.call("GET", "/orders/" + quote(card["order_no"], safe="")), card)
    if order.get("status") != config["order_success_status"]:
        return False
    phone = order.get("phoneNumber")
    if not isinstance(phone, str) or not phone:
        return False
    subscriber = api.call("GET", "/subscribers", params={"phoneNumber": phone, "iccid": card["iccid"]})
    if not isinstance(subscriber, dict) or subscriber.get("iccid") != card["iccid"] \
            or subscriber.get("phoneNumber") != phone or subscriber.get("status") != "ACTIVE":
        return False
    ledger.update(card["iccid"], phase="verified", verified_at=now(), note="订单成功 + 号码 ACTIVE + ICCID 匹配 + 二维码完整")
    return True


def wait_verified(api, ledger, config, card):
    for attempt in range(config["poll_attempts"]):
        if verify_card(api, ledger, config, card):
            return
        if attempt + 1 < config["poll_attempts"]:
            time.sleep(config["poll_seconds"])
    raise Stop("轮询结束仍未满足成功判据；保留订单，先运行 verify，不重发激活。")


def execute(api, ledger, config, limit):
    cards = ledger.cards()
    if len(cards) != config["target"]:
        raise Stop("请先 prepare，固定完整批次并保存全部二维码。")
    for card in cards:
        check_qr(config, card)
    submitted = 0
    for index, card in enumerate(cards, 1):
        try:
            if card["phase"] == "verified":
                ledger.update(card["iccid"], verified_at=None)
                if not verify_card(api, ledger, config, card):
                    ledger.update(card["iccid"], phase="activate_attempted")
                    raise Stop("上次成功的卡本次复查未通过；停止新增开卡。")
                continue
            if card["phase"] in {"create_attempted", "activate_attempted"}:
                if verify_card(api, ledger, config, card):
                    continue
                raise Stop("存在结果待确认的写操作；仅查询，禁止自动重发。")
            if submitted >= limit:
                break
            if card["phase"] == "prepared":
                history = api.call("GET", "/orders", params={"iccid": card["iccid"]})
                if not isinstance(history, list) or history:
                    raise Stop("该 ICCID 已有订单或查询格式异常；请平台核查，禁止另建订单。")
                # Commit intent before POST. A crash can never reset it to prepared.
                ledger.update(card["iccid"], phase="create_attempted", note="创建结果待确认")
                data = api.call("POST", "/orders", body={
                    "productCode": PRODUCT, "iccid": card["iccid"], "requestId": card["request_id"]})
                if not isinstance(data, dict) or not isinstance(data.get("orderNo"), str) \
                        or not data["orderNo"]:
                    raise Stop("创建回包没有有效 orderNo；查询原订单，不重建。")
                card["order_no"] = data["orderNo"]
                ledger.update(card["iccid"], phase="created", order_no=card["order_no"])
                card["phase"] = "created"
            order_path = "/orders/" + quote(card["order_no"], safe="")
            order = checked_order(api.call("GET", order_path), card)
            if order.get("status") == config["order_success_status"]:
                wait_verified(api, ledger, config, card)
                continue
            if order.get("status") != config["order_created_status"]:
                raise Stop("订单当前不是平台确认的待激活状态；停止并核查。")
            ledger.update(card["iccid"], phase="activate_attempted", note="激活已尝试，等待回查")
            try:
                api.call("POST", order_path + "/activate")
            except Stop:
                # Even an HTTP error/timeout can follow a successful activation.
                # Resolve exclusively with GET; never repeat this POST.
                pass
            submitted += 1
            wait_verified(api, ledger, config, card)
            print(f"已逐项确认 {index}/{len(cards)}")
        except Stop as exc:
            ledger.update(card["iccid"], verified_at=None, note=str(exc))
            raise


def verify_all(api, ledger, config):
    cards = ledger.cards()
    confirmed = 0
    for card in cards:
        ledger.update(card["iccid"], verified_at=None)
        try:
            passed = verify_card(api, ledger, config, card)
            if not passed:
                ledger.update(card["iccid"], note="本次查询未满足全部成功条件")
            confirmed += int(passed)
        except Stop as exc:
            passed = False
            ledger.update(card["iccid"], note=str(exc))
        if not passed and card["phase"] == "verified":
            ledger.update(card["iccid"], phase="activate_attempted")
    print(f"本次逐卡回查：确认成功 {confirmed}/{config['target']}，时间 {now()}")
    return len(cards) == config["target"] == confirmed


def report(ledger, config):
    cards = ledger.cards()
    output = io.StringIO(newline="")
    fields = ["iccid", "phase", "order_no", "request_id", "verified_at", "note"]
    writer = csv.DictWriter(output, fieldnames=fields, extrasaction="ignore")
    writer.writeheader()
    writer.writerows(cards)
    directory = Path(config["output_dir"])
    atomic_write(directory / "activation_report.csv", output.getvalue().encode("utf-8-sig"))
    atomic_write(directory / ".nexsim-batch" / "report.json",
                 json.dumps(cards, ensure_ascii=False).encode("utf-8"))
    confirmed = sum(c["phase"] == "verified" and bool(c["verified_at"]) for c in cards)
    print(f"本地台账：{len(cards)} 张，最近确认成功 {confirmed} 张；实时状态以 verify 为准。")


def export_excel(config):
    node = Path(os.getenv("NEXSIM_NODE", str(Path.home() / ".cache" / "codex-runtimes" /
                "codex-primary-runtime" / "dependencies" / "node" / "bin" / "node.exe")))
    if not node.is_file():
        raise Stop("Excel 导出需要 Codex 自带 Node 运行库；CSV/台账已保存。")
    result = subprocess.run([str(node), str(Path(__file__).with_name("export_excel.mjs")),
                             str(Path(config["output_dir"]) / ".nexsim-batch" / "report.json"),
                             str(Path(config["output_dir"]) / config.get("report_filename", "激活核对表.xlsx"))],
                            capture_output=True, timeout=120)
    if result.returncode:
        raise Stop("Excel 导出失败；CSV/台账已保存，不在终端打印可能含卡信息的错误。")


def recover_order(api, ledger, config, iccid, order_no):
    card = next((c for c in ledger.cards() if c["iccid"] == iccid), None)
    if not card or card["phase"] != "create_attempted" or card["order_no"]:
        raise Stop("只能补录创建结果未知且没有 orderNo 的原记录。")
    card["order_no"] = order_no
    order = checked_order(api.call("GET", "/orders/" + quote(order_no, safe="")), card)
    if order.get("status") not in {config["order_created_status"], config["order_success_status"]}:
        raise Stop("原订单状态不明确；保留待确认，不修改本地状态。")
    ledger.update(iccid, order_no=order_no, phase="created", note="人工确认原订单号，已只读核对归属及套餐")


def main():
    parser = argparse.ArgumentParser(description="NexSim 批量开卡：默认不执行写操作")
    parser.add_argument("--config", default="config.json")
    sub = parser.add_subparsers(dest="command", required=True)
    prep = sub.add_parser("prepare", help="只读获取库存与激活码，保存二维码和 CSV")
    prep.add_argument("--csv", help="可选，使用填好的 ICCID 单列 CSV；留空则从库存选择")
    run = sub.add_parser("execute", help="创建并激活，可能扣费；默认最多激活 1 张")
    run.add_argument("--limit", type=int, default=1)
    run.add_argument("--confirm-activation", action="store_true")
    sub.add_parser("verify", help="仅查询，逐张核对订单与号码状态")
    sub.add_parser("report", help="仅导出本地台账，不查询实时状态")
    recovery = sub.add_parser("recover-order", help="创建回包丢失后，补录平台确认的原订单号；仅查询")
    recovery.add_argument("--iccid", required=True)
    recovery.add_argument("--order-no", required=True)
    args = parser.parse_args()
    try:
        config = load_config(args.config, require_status=args.command in {"execute", "verify", "recover-order"})
        if args.command == "execute" and (not args.confirm_activation or not 1 <= args.limit <= config["target"]):
            raise Stop("执行需 --confirm-activation，且 --limit 在 1 到目标数量之间。")
        directory = Path(config["output_dir"])
        private = directory / ".nexsim-batch"
        private.mkdir(parents=True, exist_ok=True)
        with exclusive_lock(private / "run.lock"):
            ledger = Ledger(private / "ledger.sqlite3")
            try:
                if args.command == "report":
                    report(ledger, config)
                    export_excel(config)
                    return 0
                client_id, secret = os.getenv("NEXSIM_CLIENT_ID"), os.getenv("NEXSIM_CLIENT_SECRET")
                if not client_id or not secret:
                    raise Stop("缺少 NEXSIM_CLIENT_ID / NEXSIM_CLIENT_SECRET；网页登录密码不能替代。")
                bind_batch(ledger, config, client_id)
                api = Api(config["base_url"], client_id, secret, ledger)
                try:
                    if args.command == "prepare":
                        prepare(api, ledger, config, args.csv)
                        return 0
                    if args.command == "recover-order":
                        recover_order(api, ledger, config, args.iccid, args.order_no)
                        return 0
                    if args.command == "execute":
                        execute(api, ledger, config, args.limit)
                    return 0 if verify_all(api, ledger, config) else 2
                finally:
                    report(ledger, config)
                    export_excel(config)
            finally:
                ledger.db.close()
    except (Stop, OSError, ValueError, sqlite3.Error, subprocess.TimeoutExpired) as exc:
        # Do not echo third-party exception strings (could contain data/secrets).
        print("停止：" + (str(exc) if isinstance(exc, Stop) else f"本地文件/数据错误（{type(exc).__name__}）"), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
