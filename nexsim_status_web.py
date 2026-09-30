"""Cross-platform web service for the eSIM Profile status tool.

The service uses only Python's standard library. Keep it bound to localhost
unless an authenticated reverse proxy is placed in front of it.
"""
from __future__ import annotations

import argparse
import io
import json
import os
import re
import secrets
import sys
import threading
import uuid
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import CookieError, SimpleCookie
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

import nexsim_status_checker as checker
import nexsim_profile_writer as writer
import nexsim_profile_activation as activation
import nexsim_platform_activation as platform_activation
import nexsim_platform_batch as platform_batch
from nexsim_installation import InstallationManager


ROOT = Path(__file__).resolve().parent
WEB_ROOT = ROOT / "web"
DEFAULT_OUTPUT_DIR = ROOT / "outputs"
MAX_BODY_BYTES = 1_000_000
WRITE_BATCH_ID = re.compile(r"^wb-[0-9]{8}-[0-9]{6}-[a-f0-9]{8}$")
HISTORY_FILE_PATTERN = re.compile(
    r"^esim-status-raw-[0-9]{8}-[0-9]{6}(?:-[0-9]{6})?\.json$"
)


def _configure_headless_output(output_dir: Path) -> None:
    """pythonw 没有标准输出时，把服务日志落到结果目录。"""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        stream = (output_dir / "service.log").open("a", encoding="utf-8", buffering=1)
    except OSError:
        stream = open(os.devnull, "a", encoding="utf-8", buffering=1)
    if sys.stdout is None:
        sys.stdout = stream
    if sys.stderr is None:
        sys.stderr = stream


class JobManager:
    def __init__(self, output_dir: Path, profile_workers: int = 2):
        self.output_dir = output_dir.resolve()
        self.profile_workers = profile_workers
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any]) -> str:
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("请输入账号和密码。")
        base_url = str(payload.get("base_url") or "https://admin.nexsimus.com").strip()
        product_value = payload.get("product_id")
        if product_value in (None, ""):
            product_id = None
        else:
            try:
                product_id = int(product_value)
            except (TypeError, ValueError) as exc:
                raise checker.Stop("商品 ID 必须是整数，或留空。") from exc

        targets: set[str] | None = None
        if payload.get("scope") == "selected":
            values = payload.get("iccids", [])
            if isinstance(values, str):
                iccid_text = values
            elif isinstance(values, list):
                iccid_text = "\n".join(str(value) for value in values)
            else:
                raise checker.Stop("ICCID 列表格式不正确。")
            targets = checker.parse_iccids(iccid_text)

        config = checker.validate_config({
            "base_url": base_url,
            "credential_file": None,
            "org_id": None,
            "product_id": product_id,
            "output_dir": str(self.output_dir),
            "page_size": 100,
            "profile_workers": self.profile_workers,
            "query_params": {"simType": "ESIM", "status": "USED"},
        }, require_credential_file=False)

        job_id = uuid.uuid4().hex
        job = {
            "id": job_id,
            "account": username,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "正在登录后台……",
            "rows": [],
            "profile_query_summary": None,
            "json_path": "",
            "csv_path": "",
            "error": "",
            "cancel": threading.Event(),
            "credentials": (username, password),
        }
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job_id, config, targets),
            daemon=True,
            name=f"esim-query-{job_id[:8]}",
        )
        thread.start()
        return job_id

    def _run(self, job_id: str, config: dict[str, Any], targets: set[str] | None) -> None:
        job = self.jobs[job_id]

        def progress(stage: str, current: int, total: int) -> None:
            ratio = min(current / max(total, 1), 1)
            if stage == "inventory":
                percent = 5 + ratio * 15
                message = f"读取库存分页：{current} / {total}"
            elif stage == "profile":
                percent = 20 + ratio * 55
                message = f"读取 Profile 状态：{current} / {total}"
            elif stage.startswith("profile_retry_wait_"):
                attempt = int(stage.rsplit("_", 1)[-1])
                retry_base = {2: 75, 3: 82, 4: 89}.get(attempt, 75)
                percent = retry_base
                cooldown = checker.PROFILE_RETRY_COOLDOWNS[attempt - 1]
                message = (
                    f"平台出现临时错误，{cooldown:g} 秒后开始第 {attempt}/"
                    f"{checker.PROFILE_MAX_ATTEMPTS} 轮重试（{total} 条）"
                )
            elif stage.startswith("profile_retry_"):
                attempt = int(stage.rsplit("_", 1)[-1])
                retry_base = {2: 75, 3: 82, 4: 89}.get(attempt, 75)
                retry_span = {2: 7, 3: 7, 4: 6}.get(attempt, 6)
                percent = retry_base + ratio * retry_span
                message = (
                    f"重试临时失败：第 {attempt}/{checker.PROFILE_MAX_ATTEMPTS} 轮 · "
                    f"{current} / {total}"
                )
            else:
                percent = 95 + ratio * 5
                message = "正在保存 JSON 和 CSV……"
            self._update(job_id, progress=round(percent, 1), stage=stage, message=message)

        try:
            result = checker.query_status(
                config,
                iccids=targets,
                credentials=job["credentials"],
                progress=progress,
                cancelled=job["cancel"].is_set,
            )
            rows, json_path, csv_path = result
            summary = checker.build_profile_query_summary(rows)
            if summary["complete"]:
                message = "查询完整，结果已保存。"
            else:
                message = (
                    f"查询结束但结果不完整：成功 {summary['succeeded']}/"
                    f"{summary['total']}，仍有 {summary['failed']} 条失败。"
                )
            self._update(job_id, state="done", progress=100, stage="done",
                         message=message, rows=rows, profile_query_summary=summary,
                         json_path=str(json_path), csv_path=str(csv_path))
        except checker.Stop as exc:
            if job["cancel"].is_set():
                self._update(job_id, state="cancelled", stage="cancelled",
                             message="查询已取消。")
            else:
                self._update(job_id, state="failed", stage="error",
                             message="查询失败。", error=str(exc))
        except Exception:
            self._update(job_id, state="failed", stage="error",
                         message="查询失败。", error="服务内部发生未预期错误。")
        finally:
            with self.lock:
                job["credentials"] = None

    def _update(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            self.jobs[job_id].update(changes)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["state"] != "running":
                return False
            job["cancel"].set()
            job["message"] = "正在停止查询……"
            return True

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {key: value for key, value in job.items() if key not in {"cancel", "credentials"}}

    def _history_files(self) -> list[Path]:
        return sorted(
            (path for path in self.output_dir.glob("esim-status-raw-*.json")
             if HISTORY_FILE_PATTERN.fullmatch(path.name)),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )

    def _history_path(self, filename: str) -> Path | None:
        if not HISTORY_FILE_PATTERN.fullmatch(filename):
            return None
        path = (self.output_dir / filename).resolve()
        if path.parent != self.output_dir or not path.is_file():
            return None
        return path

    def history_index(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in self._history_files():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
                rows = payload.get("records", [])
                if not isinstance(rows, list):
                    continue
                used_rows = [
                    row for row in rows
                    if isinstance(row, dict) and row.get("status") == "USED"
                ]
                summary = checker.build_profile_query_summary(used_rows)
                statuses: dict[str, int] = {}
                for row in used_rows:
                    status = str(row.get("esimProfileStatus", "failed"))
                    statuses[status] = statuses.get(status, 0) + 1
                items.append({
                    "filename": path.name,
                    "account": str(payload.get("account") or "未知账号（旧记录）"),
                    "queried_at": payload.get("queried_at", ""),
                    "count": len(used_rows),
                    "statuses": statuses,
                    "profile_query_summary": summary,
                })
            except (OSError, json.JSONDecodeError, TypeError):
                continue
        return items

    def load_history(self, filename: str | None = None) -> dict[str, Any] | None:
        if filename is None:
            files = self._history_files()
            path = files[0] if files else None
        else:
            path = self._history_path(filename)
        if path is None:
            return None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
        if not isinstance(payload, dict) or not isinstance(payload.get("records"), list):
            return None
        used_rows = [
            row for row in payload["records"]
            if isinstance(row, dict) and row.get("status") == "USED"
        ]
        payload["records"] = used_rows
        payload["account"] = str(payload.get("account") or "未知账号（旧记录）")
        payload["profile_query_summary"] = checker.build_profile_query_summary(used_rows)
        payload["history_filename"] = path.name
        payload["json_path"] = str(path)
        payload["csv_path"] = str(path.with_suffix(".csv"))
        return payload

    def delete_history(self, filename: str) -> bool:
        path = self._history_path(filename)
        if path is None:
            return False
        csv_path = path.with_suffix(".csv")
        try:
            path.unlink()
            try:
                csv_path.unlink()
            except FileNotFoundError:
                pass
        except OSError:
            return False
        return True


class WriteBatchManager:
    """保存写卡前的 RELEASED 批次；不调用任何二维码或写卡接口。"""

    def __init__(self, output_dir: Path):
        self.output_dir = output_dir.resolve()
        self.batch_dir = self.output_dir / "write-batches"
        self.lock = threading.Lock()
        self.activation_lock = threading.Lock()
        self.batches: dict[str, dict[str, Any]] = {}

    @staticmethod
    def _validate_id(batch_id: str) -> str:
        if not WRITE_BATCH_ID.fullmatch(batch_id):
            raise checker.Stop("写卡批次编号格式不正确。")
        return batch_id

    def create(self, payload: dict[str, Any]) -> dict[str, Any]:
        rows = payload.get("rows")
        if not isinstance(rows, list) or not rows:
            raise checker.Stop("请选择至少一张 RELEASED 卡。")
        count = payload.get("count", len(rows))
        if type(count) is not int:
            raise checker.Stop("写卡批次数量必须是整数。")
        if len(rows) != count:
            raise checker.Stop("写卡批次数量与选择的卡数不一致。")
        try:
            batch = writer.create_batch(rows, self.output_dir, count, source="web-status-query")
        except writer.WriterStop as exc:
            raise checker.Stop(str(exc)) from exc
        batch_id = batch["batch_id"]
        with self.lock:
            self.batches[batch_id] = batch
        return batch

    def snapshot(self, batch_id: str) -> dict[str, Any] | None:
        self._validate_id(batch_id)
        with self.lock:
            cached = self.batches.get(batch_id)
            if cached is not None:
                return self._decorate(dict(cached), batch_id)
        path = self.batch_dir / f"{batch_id}.json"
        if not path.is_file():
            return None
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise checker.Stop("写卡批次文件无法读取。") from exc
        if not isinstance(value, dict):
            raise checker.Stop("写卡批次文件格式异常。")
        return self._decorate(value, batch_id)

    def _decorate(self, value: dict[str, Any], batch_id: str) -> dict[str, Any]:
        value.update({
            "json_path": str(self.batch_dir / f"{batch_id}.json"),
            "csv_path": str(self.batch_dir / f"{batch_id}.csv"),
            "csv_download": f"/api/write-batches/{batch_id}/export.csv",
            "platform_activation": platform_activation.activation_artifact_summary(
                self.output_dir, batch_id
            ),
        })
        return value

    def index(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for path in sorted(self.batch_dir.glob("wb-*.json"), key=lambda item: item.stat().st_mtime, reverse=True):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            records = value.get("records")
            if not isinstance(records, list):
                records = []
            batch_id = path.stem
            items.append({
                "batch_id": batch_id,
                "created_at": value.get("created_at", ""),
                "state": value.get("state", "draft"),
                "stage": value.get("stage", ""),
                "count": len(records),
                "qr_operations": value.get("qr_operations", "none"),
                "lpa_operations": value.get("lpa_operations", "none"),
                "write_operations": value.get("write_operations", "none"),
                "platform_activation": platform_activation.activation_artifact_summary(
                    self.output_dir, batch_id
                ),
            })
        return items

    def export_csv(self, batch_id: str) -> bytes | None:
        self._validate_id(batch_id)
        path = self.batch_dir / f"{batch_id}.csv"
        try:
            return path.read_bytes() if path.is_file() else None
        except OSError as exc:
            raise checker.Stop("写卡批次 CSV 无法读取。") from exc

    def _persist(self, batch: dict[str, Any]) -> None:
        batch_id = self._validate_id(str(batch.get("batch_id", "")))
        json_path = self.batch_dir / f"{batch_id}.json"
        csv_path = self.batch_dir / f"{batch_id}.csv"
        transient = {"json_path", "csv_path", "csv_download", "platform_activation", "results"}
        stored = {key: value for key, value in batch.items() if key not in transient}
        writer.atomic_write(json_path, json.dumps(stored, ensure_ascii=False, indent=2).encode("utf-8"))
        records = stored.get("records")
        if isinstance(records, list):
            writer._write_csv(csv_path, records)

    def mark_activation_started(self, batch_id: str) -> dict[str, Any]:
        with self.activation_lock:
            batch = self.snapshot(batch_id)
            if batch is None:
                raise checker.Stop("写卡批次不存在。")
            if batch.get("qr_operations") != "none":
                raise checker.Stop("这个批次已经触发过二维码获取，不能重复请求；请新建批次。")
            batch["state"] = "preparing_profile_data"
            batch["stage"] = "qr_once"
            batch["qr_operations"] = "started"
            batch["lpa_operations"] = "memory_only"
            self._persist(batch)
            with self.lock:
                self.batches[batch_id] = batch
            return batch

    def finish_activation(self, batch_id: str, results: list[dict[str, Any]]) -> None:
        batch = self.snapshot(batch_id)
        if batch is None:
            raise checker.Stop("写卡批次不存在。")
        by_id = {result.get("inventory_id"): result for result in results}
        safe_fields = {
            "fetched_at", "precheck_status", "lpa_status", "qr_status", "qr_path", "qr_sha256", "error",
        }
        for record in batch.get("records", []):
            result = by_id.get(record.get("inventory_id"))
            if not isinstance(result, dict):
                continue
            for key in safe_fields:
                if key in result:
                    record[key] = result[key]
            if result.get("state") == "ready":
                record["write_status"] = "not_started"
            elif result.get("state") in {"unknown", "failed", "skipped"}:
                record["write_status"] = "blocked"
                record["error"] = result.get("error", "")
        counts: dict[str, int] = {}
        for result in results:
            state = str(result.get("state", "unknown"))
            counts[state] = counts.get(state, 0) + 1
        ready = counts.get("ready", 0)
        total = len(batch.get("records", []))
        batch["state"] = "profile_data_ready" if ready == total else "profile_data_partial"
        batch["stage"] = "ready_for_writer" if ready else "blocked"
        batch["qr_operations"] = "complete" if ready == total else "partial"
        batch["activation_result_summary"] = counts
        batch["finished_at"] = activation.now()
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch

    def abort_activation(self, batch_id: str, message: str) -> None:
        batch = self.snapshot(batch_id)
        if batch is None:
            return
        batch["state"] = "profile_data_failed"
        batch["stage"] = "blocked"
        batch["qr_operations"] = "partial"
        batch["activation_error"] = message
        batch["finished_at"] = activation.now()
        self._persist(batch)
        with self.lock:
            self.batches[batch_id] = batch

    def qr_file(self, batch_id: str, sequence: int) -> bytes | None:
        if type(sequence) is not int or not 1 <= sequence <= 200:
            raise checker.Stop("二维码序号不正确。")
        batch = self.snapshot(batch_id)
        if batch is None:
            return None
        for record in batch.get("records", []):
            if record.get("sequence") != sequence or record.get("qr_status") != "saved":
                continue
            path_value = record.get("qr_path")
            if not isinstance(path_value, str) or not path_value:
                return None
            path = Path(path_value).resolve()
            root = (self.batch_dir / batch_id).resolve()
            if root not in path.parents or path.suffix.lower() != ".png":
                raise checker.Stop("二维码文件路径校验失败。")
            try:
                return path.read_bytes() if path.is_file() else None
            except OSError as exc:
                raise checker.Stop("二维码文件无法读取。") from exc
        return None

    def qr_batch_zip(self, batch_id: str) -> bytes | None:
        """将批次中所有已保存的二维码打包成ZIP文件。"""
        batch = self.snapshot(batch_id)
        if batch is None:
            return None

        root = (self.batch_dir / batch_id).resolve()
        saved_records = [
            record for record in batch.get("records", [])
            if record.get("qr_status") == "saved"
        ]

        if not saved_records:
            raise checker.Stop("该批次没有已保存的二维码。")

        # 创建内存中的ZIP文件
        zip_buffer = io.BytesIO()
        with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zip_file:
            for record in saved_records:
                path_value = record.get("qr_path")
                if not isinstance(path_value, str) or not path_value:
                    continue

                path = Path(path_value).resolve()
                if root not in path.parents or path.suffix.lower() != ".png":
                    continue

                try:
                    if path.is_file():
                        # 使用 sequence-iccid.png 作为ZIP内的文件名
                        iccid = record.get("iccid", "unknown")
                        sequence = record.get("sequence", 0)
                        zip_filename = f"{sequence:03d}-{iccid}.png"
                        zip_file.writestr(zip_filename, path.read_bytes())
                except OSError:
                    continue

        zip_buffer.seek(0)
        return zip_buffer.read()


class ActivationJobManager:
    """异步执行一次性 LPA/二维码获取，不重试单张请求。"""

    def __init__(self, batches: WriteBatchManager, output_dir: Path):
        self.batches = batches
        self.output_dir = output_dir.resolve()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any]) -> str:
        batch_id = str(payload.get("batch_id", "")).strip()
        self.batches._validate_id(batch_id)
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("获取 LPA 需要重新确认后台账号和密码。")
        base_url = str(payload.get("base_url") or "https://admin.nexsimus.com").strip()
        if not re.fullmatch(r"https://[^/]+", base_url.rstrip("/")):
            raise checker.Stop("后台地址必须是纯 HTTPS 域名。")
        batch = self.batches.mark_activation_started(batch_id)
        job_id = "ad-" + uuid.uuid4().hex
        job = {
            "id": job_id,
            "batch_id": batch_id,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "准备一次性获取激活数据……",
            "results": [],
            "error": "",
            "cancel": threading.Event(),
            "credentials": (username, password),
            "base_url": base_url,
        }
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job_id, batch),
            daemon=True,
            name=f"activation-data-{job_id[:8]}",
        )
        thread.start()
        return job_id

    def _update(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(changes)

    def _run(self, job_id: str, batch: dict[str, Any]) -> None:
        job = self.jobs[job_id]

        def progress(stage: str, current: int, total: int, message: str) -> None:
            self._update(job_id, progress=round(current / max(total, 1) * 100, 1),
                         stage=stage, message=message)

        try:
            results = activation.fetch_batch_profile_data(
                batch["batch_id"], batch["records"], self.output_dir,
                job["base_url"], job["credentials"], progress,
                cancelled=job["cancel"].is_set,
            )
            public_results = []
            for result in results:
                view = {key: value for key, value in result.items() if key != "qr_path"}
                if result.get("state") == "ready":
                    view["qr_url"] = f"/api/write-batches/{batch['batch_id']}/qr/{int(result['sequence'])}"
                public_results.append(view)
            self.batches.finish_activation(batch["batch_id"], results)
            self._update(job_id, state="done", progress=100, stage="done",
                         message="激活数据获取完成；LPA 只保存在当前任务内存。",
                         results=public_results)
        except activation.ActivationStop as exc:
            self.batches.abort_activation(batch["batch_id"], str(exc))
            state = "cancelled" if job["cancel"].is_set() else "failed"
            self._update(job_id, state=state, stage="blocked", message=str(exc), error=str(exc))
        except Exception:
            message = "激活数据任务发生未预期错误；不会自动重试。"
            self.batches.abort_activation(batch["batch_id"], message)
            self._update(job_id, state="failed", stage="blocked", message=message, error=message)
        finally:
            with self.lock:
                job["credentials"] = None

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["state"] != "running":
                return False
            job["cancel"].set()
            job["message"] = "正在停止；不会再请求后续卡……"
            return True

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {key: value for key, value in job.items() if key not in {"cancel", "credentials"}}


class PlatformContextJobManager:
    """Identify the authenticated organization and list activation products."""

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any]) -> str:
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("识别平台账号需要账号和密码。")
        job_id = "pc-" + uuid.uuid4().hex
        job = {
            "id": job_id,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "正在登录并识别账号组织……",
            "result": {},
            "error": "",
            "credentials": (username, password),
            "base_url": str(payload.get("base_url") or "https://admin.nexsimus.com").strip(),
        }
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(job_id,),
            daemon=True,
            name=f"platform-context-{job_id[:8]}",
        ).start()
        return job_id

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]
        try:
            with self.lock:
                job.update(progress=20, message="正在登录平台……")
            client = platform_activation.PlatformClient(
                job["base_url"], job["credentials"][0], job["credentials"][1]
            )
            with self.lock:
                job.update(progress=65, stage="products", message="正在读取可开户套餐……")
            products = platform_activation.list_activation_products(client)
            result = {
                "org_id": client.org_id,
                "account": client.account,
                "base_url": f"https://{urlsplit(client.base).hostname.lower()}",
                "products": products,
                "product_catalog_version": 1,
            }
            with self.lock:
                job.update(
                    state="done",
                    progress=100,
                    stage="done",
                    result=result,
                    message=f"账号识别完成：组织 {client.org_id}，{len(products)} 个可开户套餐。",
                )
        except platform_activation.PlatformActivationStop as exc:
            with self.lock:
                job.update(state="failed", stage="blocked", message=str(exc), error=str(exc))
        except Exception:
            message = "账号识别任务发生未预期错误。"
            with self.lock:
                job.update(state="failed", stage="blocked", message=message, error=message)
        finally:
            with self.lock:
                job["credentials"] = None

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {key: value for key, value in job.items() if key != "credentials"}


class PlatformInventoryJobManager:
    """Read and classify platform inventory without touching QR data."""

    def __init__(self):
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any]) -> str:
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("读取平台库存需要账号和密码。")
        product_id = payload.get("product_id")
        if type(product_id) is not int or product_id <= 0:
            raise checker.Stop("请选择有效的开户套餐。")
        job_id = "pi-" + uuid.uuid4().hex
        job = {
            "id": job_id,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "正在登录并读取平台库存……",
            "result": {},
            "error": "",
            "credentials": (username, password),
            "base_url": str(payload.get("base_url") or "https://admin.nexsimus.com").strip(),
            "product_id": product_id,
        }
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(job_id,),
            daemon=True,
            name=f"platform-inventory-{job_id[:8]}",
        ).start()
        return job_id

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]

        def progress(stage: str, percent: float, message: str) -> None:
            with self.lock:
                job.update(stage=stage, progress=round(percent, 1), message=message)

        try:
            client = platform_activation.PlatformClient(
                job["base_url"], job["credentials"][0], job["credentials"][1]
            )
            platform_activation.get_activation_product(client, job["product_id"])
            result = platform_batch.inspect_inventory(
                client, client.org_id, job["product_id"], progress
            )
            with self.lock:
                job.update(state="done", progress=100, stage="done", result=result,
                           message=f"平台库存读取完成：{result['eligible_count']} 张可加入批次。")
        except (platform_activation.PlatformActivationStop, platform_batch.PlatformBatchStop) as exc:
            with self.lock:
                job.update(state="failed", stage="blocked", message=str(exc), error=str(exc))
        except Exception:
            message = "平台库存任务发生未预期错误。"
            with self.lock:
                job.update(state="failed", stage="blocked", message=message, error=message)
        finally:
            with self.lock:
                job["credentials"] = None

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {key: value for key, value in job.items() if key != "credentials"}


class PlatformQrJobManager:
    """Run explicitly confirmed, one-shot QR retrieval for a platform batch."""

    def __init__(self, batches: platform_batch.PlatformBatchStore, output_dir: Path):
        self.batches = batches
        self.output_dir = output_dir.resolve()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any]) -> str:
        batch_id = str(payload.get("batch_id", "")).strip()
        self.batches.validate_id(batch_id)
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("获取二维码需要重新确认平台账号和密码。")
        batch = self.batches.mark_qr_started(batch_id)
        job_id = "pq-" + uuid.uuid4().hex
        job = {
            "id": job_id,
            "batch_id": batch_id,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "准备一次性获取二维码……",
            "results": [],
            "summary": {},
            "error": "",
            "cancel": threading.Event(),
            "credentials": (username, password),
            "base_url": str(payload.get("base_url") or "https://admin.nexsimus.com").strip(),
            "batch": batch,
        }
        with self.lock:
            self.jobs[job_id] = job
        threading.Thread(
            target=self._run,
            args=(job_id,),
            daemon=True,
            name=f"platform-qr-{job_id[:8]}",
        ).start()
        return job_id

    def _run(self, job_id: str) -> None:
        job = self.jobs[job_id]

        def progress(stage: str, percent: float, message: str) -> None:
            with self.lock:
                job.update(stage=stage, progress=round(percent, 1), message=message)

        try:
            batch = job["batch"]
            client = platform_activation.PlatformClient(
                job["base_url"], job["credentials"][0], job["credentials"][1], batch["org_id"]
            )
            results = platform_batch.fetch_batch_qr(
                client, batch, self.output_dir, progress, job["cancel"].is_set,
                checkpoint=lambda rows: self.batches.checkpoint_qr(batch["batch_id"], rows),
            )
            public_results: list[dict[str, Any]] = []
            for result in results:
                view = {key: value for key, value in result.items() if key != "qr_path"}
                if result.get("state") == "ready":
                    view["qr_url"] = f"/api/platform-batches/{batch['batch_id']}/qr/{int(result['sequence'])}"
                public_results.append(view)
            summary = self.batches.finish_qr(batch["batch_id"], results)
            ready = summary.get("ready", 0)
            with self.lock:
                job.update(state="done", progress=100, stage="done", results=public_results,
                           summary=summary,
                           message=f"二维码任务结束：{ready}/{len(batch['records'])} 张已保存。")
        except (platform_activation.PlatformActivationStop, platform_batch.PlatformBatchStop) as exc:
            self.batches.abort_qr(job["batch_id"], str(exc))
            state = "cancelled" if job["cancel"].is_set() else "failed"
            with self.lock:
                job.update(state=state, stage="blocked", message=str(exc), error=str(exc))
        except Exception:
            message = "二维码任务发生未预期错误；不会自动重试。"
            self.batches.abort_qr(job["batch_id"], message)
            with self.lock:
                job.update(state="failed", stage="blocked", message=message, error=message)
        finally:
            with self.lock:
                job["credentials"] = None
                job.pop("batch", None)

    def cancel(self, job_id: str) -> bool:
        with self.lock:
            job = self.jobs.get(job_id)
            if not job or job["state"] != "running":
                return False
            job["cancel"].set()
            job["message"] = "正在停止；不会请求后续卡。"
            return True

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {
                key: value for key, value in job.items()
                if key not in {"credentials", "cancel", "batch"}
            }


class PlatformActivationJobManager:
    """Run platform activation preview, submit, or verification as explicit jobs."""

    def __init__(self, batches: platform_batch.PlatformBatchStore, output_dir: Path):
        self.batches = batches
        self.output_dir = output_dir.resolve()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any], action: str) -> str:
        if action not in {"preview", "submit", "verify"}:
            raise checker.Stop("平台开户动作不正确。")
        batch_id = str(payload.get("batch_id", "")).strip()
        self.batches.validate_id(batch_id)
        batch = self.batches.snapshot(batch_id)
        if batch is None:
            raise checker.Stop("平台开户批次不存在。")
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("平台开户需要重新确认后台账号和密码。")
        org_id = batch.get("org_id")
        product_id = batch.get("product_id")
        if type(org_id) is not int or org_id <= 0 or type(product_id) is not int or product_id <= 0:
            raise checker.Stop("平台开户批次缺少有效的组织 ID 或产品 ID。")
        if payload.get("org_id") != org_id or payload.get("product_id") != product_id:
            raise checker.Stop("页面中的组织或产品与所选批次不一致；请重新选择批次。")
        confirmed_total = payload.get("confirmed_total")
        max_total = str(payload.get("max_total") if payload.get("max_total") is not None else "").strip()
        if action != "verify" and not max_total:
            raise checker.Stop("提交前必须填写本次授权金额上限。")
        if action == "submit" and confirmed_total is None:
            raise checker.Stop("请先预检并确认实际总价。")
        artifact_state = platform_activation.activation_artifact_summary(
            self.output_dir, batch_id
        )
        if action == "submit" and artifact_state["has_submission_intent"]:
            raise checker.Stop("这个批次已经尝试过平台开户激活；禁止重发，请执行结果核验。")
        base_url = str(payload.get("base_url") or "https://admin.nexsimus.com").strip()
        job_id = "pa-" + uuid.uuid4().hex
        job = {
            "id": job_id,
            "action": action,
            "batch_id": batch_id,
            "state": "running",
            "progress": 0,
            "stage": "login",
            "message": "准备平台开户任务……",
            "preview": {},
            "result": {},
            "error": "",
            "credentials": (username, password),
            "base_url": base_url,
            "org_id": org_id,
            "product_id": product_id,
            "confirmed_total": confirmed_total,
            "max_total": max_total,
            "batch": batch,
        }
        with self.lock:
            self.jobs[job_id] = job
        thread = threading.Thread(
            target=self._run,
            args=(job_id,),
            daemon=True,
            name=f"platform-activation-{job_id[:8]}",
        )
        thread.start()
        return job_id

    def _update(self, job_id: str, **changes: Any) -> None:
        with self.lock:
            job = self.jobs[job_id]
            job.update(changes)

    def _run(self, job_id: str) -> None:
        with self.lock:
            job = self.jobs[job_id]

        def progress(stage: str, percent: float, message: str) -> None:
            self._update(job_id, stage=stage, progress=round(percent, 1), message=message)

        try:
            client = platform_activation.PlatformClient(
                job["base_url"], job["credentials"][0], job["credentials"][1], job["org_id"]
            )
            action = job["action"]
            if action == "preview":
                result = platform_activation.preview_activation(
                    client, job["batch"], self.output_dir, job["org_id"], job["product_id"],
                    progress,
                    max_total=job["max_total"],
                )
                self._update(job_id, state="done", progress=100, stage="done",
                             message="预检完成，尚未提交激活。", preview=result, result=result)
            elif action == "submit":
                result = platform_activation.submit_activation(
                    client, job["batch"], self.output_dir, job["org_id"], job["product_id"],
                    job["confirmed_total"], progress,
                    max_total=job["max_total"],
                )
                try:
                    result["verification"] = platform_activation.verify_activation(
                        client, job["batch"], self.output_dir, job["org_id"], job["product_id"], progress,
                    )
                except Exception:
                    result["verification_error"] = "已提交，但激活状态查询未完成；请点击查询激活状态，勿再次提交。"
                self._update(job_id, state="done", progress=100, stage="done",
                             message="激活提交阶段结束，请查看逐卡激活状态。", result=result)
            else:
                result = platform_activation.verify_activation(
                    client, job["batch"], self.output_dir, job["org_id"], job["product_id"], progress,
                )
                self._update(job_id, state="done", progress=100, stage="done",
                             message=f"结果核验完成：{result['confirmed']}/{result['count']} 张确认成功。",
                             result=result)
        except platform_activation.PlatformActivationStop as exc:
            self._update(job_id, state="failed", stage="blocked", message=str(exc), error=str(exc))
        except Exception:
            message = "平台开户任务发生未预期错误；请检查服务日志后再决定是否核验。"
            self._update(job_id, state="failed", stage="blocked", message=message, error=message)
        finally:
            with self.lock:
                job["credentials"] = None
                job.pop("batch", None)

    def snapshot(self, job_id: str) -> dict[str, Any] | None:
        with self.lock:
            job = self.jobs.get(job_id)
            if job is None:
                return None
            return {
                key: value for key, value in job.items()
                if key not in {"credentials", "batch"}
            }

class Handler(BaseHTTPRequestHandler):
    installation_jobs: InstallationManager
    manager: JobManager
    write_batches: WriteBatchManager
    activation_jobs: ActivationJobManager
    platform_context_jobs: PlatformContextJobManager
    platform_inventory_jobs: PlatformInventoryJobManager
    platform_batches: platform_batch.PlatformBatchStore
    platform_qr_jobs: PlatformQrJobManager
    platform_activation_jobs: PlatformActivationJobManager

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request bodies, credentials, or query results.
        message = re.sub(r"\?[^\s\"]+", "", format % args)
        message = re.sub(r"im-[a-f0-9]{32}", "installation-job", message)
        print(f"{self.address_string()} - {message}")

    def _json(self, value: Any, status: int = 200, headers: dict[str, str] | None = None) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        for name, header_value in (headers or {}).items():
            self.send_header(name, header_value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _installation_history_token(self) -> tuple[str, str | None]:
        try:
            cookies = SimpleCookie()
            cookies.load(self.headers.get("Cookie", ""))
            saved = cookies.get("esim_installation_history")
            token = saved.value if saved else ""
        except CookieError:
            token = ""
        if re.fullmatch(r"[a-f0-9]{64}", token):
            return token, None
        token = secrets.token_hex(32)
        cookie = (f"esim_installation_history={token}; Path=/api/installation; "
                  "Max-Age=31536000; HttpOnly; SameSite=Strict")
        return token, cookie

    def _read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise checker.Stop("请求体格式不正确。") from exc
        if length <= 0 or length > MAX_BODY_BYTES:
            raise checker.Stop("请求体大小不正确。")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise checker.Stop("请求体必须是 JSON。") from exc
        if not isinstance(value, dict):
            raise checker.Stop("请求体必须是 JSON 对象。")
        return value

    def _batch_owner(self) -> dict[str, Any]:
        context_id = self.headers.get("X-Platform-Context") or parse_qs(
            urlsplit(self.path).query
        ).get("context_id", [""])[0]
        context = self.platform_context_jobs.snapshot(context_id)
        if not context or context.get("state") != "done":
            raise platform_batch.PlatformBatchStop("请先识别账号；服务重启后需要重新识别。")
        result = context["result"]
        return {
            "base_url": result["base_url"],
            "org_id": result["org_id"],
            "username": result["account"]["username"],
        }

    @staticmethod
    def _batch_history_view(batch: dict[str, Any]) -> dict[str, Any]:
        # History never returns LPA, image contents, or an image URL.
        fields = {"sequence", "inventory_id", "iccid", "state", "fetched_at", "lpa_status",
                  "qr_status", "qr_sha256", "qr_view_count_before", "qr_view_limit", "error"}
        view = {key: batch[key] for key in (
            "batch_id", "owner", "created_at", "updated_at", "finished_at", "org_id", "product_id",
            "count", "state", "stage", "qr_operations", "qr_result_summary", "qr_error",
            "csv_download", "platform_activation"
        ) if key in batch}
        view["records"] = [{key: value for key, value in record.items() if key in fields}
                           for record in batch.get("records", [])]
        return view

    def _batch_action_payload(self) -> dict[str, Any]:
        payload = self._read_json()
        owner = self._batch_owner()
        batch = self.platform_batches.snapshot(str(payload.get("batch_id") or ""))
        if not batch or batch.get("owner") != owner:
            raise platform_batch.PlatformBatchStop("当前账号不能操作此批次；旧记录仅供查看。")
        return payload

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/installation/history":
            token, cookie = self._installation_history_token()
            try:
                items = self.installation_jobs.history_index(token)
                self._json({"items": items}, headers={"Set-Cookie": cookie} if cookie else None)
            except checker.Stop as exc:
                self._json({"error": str(exc)}, 500)
            return
        if path.startswith("/api/installation-jobs/"):
            suffix = path.removeprefix("/api/installation-jobs/")
            match = re.fullmatch(r"(im-[a-f0-9]{32})/files/([1-9][0-9]*)\.(txt|png)", suffix)
            if match:
                job_id, inventory_id, extension = match.groups()
                data = self.installation_jobs.download(job_id, int(inventory_id), extension)
                if data is None:
                    self._json({"error": "资料不存在或服务已重启，请核查本地 installation-batches 目录"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png" if extension == "png" else "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{inventory_id}.{extension}"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            job = self.installation_jobs.snapshot(suffix)
            self._json(job or {"error": "任务不存在或服务已重启；不要重复获取，请核查本地记录"}, 200 if job else 404)
            return
        if path == "/api/health":
            self._json({"ok": True, "service": "esim-profile-status"})
            return
        if path == "/api/history":
            self._json({"items": self.manager.history_index()})
            return
        if path == "/api/write-batches":
            self._json({"items": self.write_batches.index()})
            return
        if path == "/api/platform-batches":
            try:
                self._json({"items": self.platform_batches.index(self._batch_owner())})
            except platform_batch.PlatformBatchStop as exc:
                self._json({"error": str(exc)}, 403)
            return
        if path == "/api/history/latest":
            payload = self.manager.load_history()
            if payload is None:
                self._json({"error": "还没有保存的历史记录。"}, 404)
            else:
                self._json(payload)
            return
        if path.startswith("/api/history/"):
            filename = path.removeprefix("/api/history/").strip("/")
            payload = self.manager.load_history(filename)
            if payload is None:
                self._json({"error": "历史记录不存在。"}, 404)
            else:
                self._json(payload)
            return
        if path.startswith("/api/jobs/"):
            job_id = path.removeprefix("/api/jobs/").strip("/")
            snapshot = self.manager.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/activation-jobs/"):
            job_id = path.removeprefix("/api/activation-jobs/").removesuffix("/cancel").strip("/")
            snapshot = self.activation_jobs.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "激活数据任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/platform-context-jobs/"):
            job_id = path.removeprefix("/api/platform-context-jobs/").strip("/")
            snapshot = self.platform_context_jobs.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "平台账号识别任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/platform-inventory-jobs/"):
            job_id = path.removeprefix("/api/platform-inventory-jobs/").strip("/")
            snapshot = self.platform_inventory_jobs.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "平台库存任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/platform-qr-jobs/"):
            job_id = path.removeprefix("/api/platform-qr-jobs/").removesuffix("/cancel").strip("/")
            snapshot = self.platform_qr_jobs.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "二维码任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/platform-activation-jobs/"):
            job_id = path.removeprefix("/api/platform-activation-jobs/").strip("/")
            snapshot = self.platform_activation_jobs.snapshot(job_id)
            if snapshot is None:
                self._json({"error": "平台开户任务不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/write-batches/"):
            suffix = path.removeprefix("/api/write-batches/").strip("/")
            activation_artifact_match = re.fullmatch(
                r"([^/]+)/platform-activation/(verification\.csv)", suffix
            )
            if activation_artifact_match:
                batch_id, filename = activation_artifact_match.groups()
                try:
                    data = platform_activation.read_activation_artifact(
                        self.write_batches.output_dir, batch_id, filename
                    )
                except platform_activation.PlatformActivationStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "平台开户核验结果不存在。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{batch_id}-platform-verification.csv"',
                )
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            # 批量下载所有二维码为ZIP
            if suffix.endswith("/qr/download-all.zip"):
                batch_id = suffix.removesuffix("/qr/download-all.zip").strip("/")
                try:
                    data = self.write_batches.qr_batch_zip(batch_id)
                except checker.Stop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "批次不存在或没有二维码。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{batch_id}-qrcodes.zip"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            qr_match = re.fullmatch(r"([^/]+)/qr/([1-9][0-9]{0,2})", suffix)
            if qr_match:
                batch_id, sequence_text = qr_match.groups()
                try:
                    data = self.write_batches.qr_file(batch_id, int(sequence_text))
                except checker.Stop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "二维码不存在或尚未生成。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if suffix.endswith("/export.csv"):
                batch_id = suffix.removesuffix("/export.csv").strip("/")
                try:
                    data = self.write_batches.export_csv(batch_id)
                except checker.Stop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "写卡批次不存在。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{batch_id}.csv"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            batch_id = suffix
            try:
                snapshot = self.write_batches.snapshot(batch_id)
            except checker.Stop as exc:
                self._json({"error": str(exc)}, 400)
                return
            if snapshot is None:
                self._json({"error": "写卡批次不存在。"}, 404)
            else:
                self._json(snapshot)
            return
        if path.startswith("/api/platform-batches/"):
            suffix = path.removeprefix("/api/platform-batches/").strip("/")
            # Keep the existing explicit QR route unchanged; metadata/CSV require account context.
            if "/qr/" not in suffix:
                try:
                    owner = self._batch_owner()
                    batch = self.platform_batches.history_snapshot(suffix.split("/")[0])
                    if batch is None or not self.platform_batches.belongs_to(batch, owner):
                        self._json({"error": "当前账号没有这个批次记录。"}, 404)
                        return
                except platform_batch.PlatformBatchStop as exc:
                    self._json({"error": str(exc)}, 403)
                    return
            activation_artifact_match = re.fullmatch(
                r"([^/]+)/platform-activation/(verification\.csv)", suffix
            )
            if activation_artifact_match:
                batch_id, filename = activation_artifact_match.groups()
                try:
                    data = platform_activation.read_activation_artifact(
                        self.platform_batches.output_dir, batch_id, filename
                    )
                except platform_activation.PlatformActivationStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "平台开户核验结果不存在。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header(
                    "Content-Disposition",
                    f'attachment; filename="{batch_id}-platform-verification.csv"',
                )
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            # 批量下载所有LPA为文本文件
            if suffix.endswith("/lpa/download-all.txt"):
                batch_id = suffix.removesuffix("/lpa/download-all.txt").strip("/")
                try:
                    data = self.platform_batches.lpa_batch_file(batch_id)
                except platform_batch.PlatformBatchStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "批次不存在或没有LPA记录。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/plain; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{batch_id}-lpa-codes.txt"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            # 批量下载所有二维码为ZIP
            if suffix.endswith("/qr/download-all.zip"):
                batch_id = suffix.removesuffix("/qr/download-all.zip").strip("/")
                try:
                    data = self.platform_batches.qr_batch_zip(batch_id)
                except platform_batch.PlatformBatchStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "批次不存在或没有二维码。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/zip")
                self.send_header("Content-Disposition", f'attachment; filename="{batch_id}-qrcodes.zip"')
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            qr_match = re.fullmatch(r"([^/]+)/qr/([1-9][0-9]{0,2})", suffix)
            if qr_match:
                batch_id, sequence_text = qr_match.groups()
                try:
                    data = self.platform_batches.qr_file(batch_id, int(sequence_text))
                except platform_batch.PlatformBatchStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "二维码不存在或尚未生成。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "image/png")
                self.send_header("Cache-Control", "private, no-store")
                self.send_header("X-Content-Type-Options", "nosniff")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            if suffix.endswith("/export.csv"):
                batch_id = suffix.removesuffix("/export.csv").strip("/")
                try:
                    data = self.platform_batches.export_csv(batch_id)
                except platform_batch.PlatformBatchStop as exc:
                    self._json({"error": str(exc)}, 400)
                    return
                if data is None:
                    self._json({"error": "平台开户批次不存在。"}, 404)
                    return
                self.send_response(200)
                self.send_header("Content-Type", "text/csv; charset=utf-8")
                self.send_header("Content-Disposition", f'attachment; filename="{batch_id}.csv"')
                self.send_header("Cache-Control", "no-store")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                return
            batch_id = suffix
            try:
                snapshot = self.platform_batches.history_snapshot(batch_id)
            except platform_batch.PlatformBatchStop as exc:
                self._json({"error": str(exc)}, 400)
                return
            if snapshot is None:
                self._json({"error": "平台开户批次不存在。"}, 404)
            else:
                self._json(self._batch_history_view(snapshot))
            return
        self._serve_static(path)

    def do_DELETE(self) -> None:
        path = urlsplit(self.path).path
        if path.startswith("/api/history/"):
            filename = path.removeprefix("/api/history/").strip("/")
            if self.manager.delete_history(filename):
                self._json({"ok": True, "filename": filename})
            else:
                self._json({"error": "历史记录不存在或删除失败。"}, 404)
            return
        self._json({"error": "接口不存在。"}, 404)

    def _serve_static(self, path: str) -> None:
        files = {
            "/": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/index.html": (WEB_ROOT / "index.html", "text/html; charset=utf-8"),
            "/app.js": (WEB_ROOT / "app.js", "text/javascript; charset=utf-8"),
            "/styles.css": (WEB_ROOT / "styles.css", "text/css; charset=utf-8"),
            "/activation.html": (WEB_ROOT / "activation.html", "text/html; charset=utf-8"),
            "/activation.js": (WEB_ROOT / "activation.js", "text/javascript; charset=utf-8"),
            "/installation.html": (WEB_ROOT / "installation.html", "text/html; charset=utf-8"),
            "/installation.js": (WEB_ROOT / "installation.js", "text/javascript; charset=utf-8"),
            "/motion.js": (WEB_ROOT / "motion.js", "text/javascript; charset=utf-8"),
            "/vendor/gsap.min.js": (WEB_ROOT / "vendor" / "gsap.min.js", "text/javascript; charset=utf-8"),
        }
        entry = files.get(path)
        if entry is None or not entry[0].is_file():
            self._json({"error": "资源不存在。"}, 404)
            return
        data = entry[0].read_bytes()
        self.send_response(200)
        self.send_header("Content-Type", entry[1])
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self) -> None:
        path = urlsplit(self.path).path
        try:
            if path == "/api/installation/prepare":
                job_id = self.installation_jobs.start_prepare(self._read_json())
                token, cookie = self._installation_history_token()
                try:
                    self.installation_jobs.remember_history(token, job_id)
                    history_saved = True
                except (checker.Stop, OSError):
                    history_saved = False
                self._json({"job_id": job_id, "history_saved": history_saved}, 202,
                           {"Set-Cookie": cookie} if cookie else None)
                return
            if path == "/api/installation/history/attach":
                job_id = self._read_json().get("job_id")
                token, cookie = self._installation_history_token()
                self.installation_jobs.remember_history(token, job_id)
                self._json({"ok": True}, headers={"Set-Cookie": cookie} if cookie else None)
                return
            if path == "/api/installation/fetch":
                self._json({"job_id": self.installation_jobs.start_fetch(self._read_json())}, 202)
                return
            if path == "/api/installation/cancel":
                ok = self.installation_jobs.cancel(self._read_json().get("job_id"))
                self._json({"ok": ok}, 200 if ok else 409)
                return
            if path == "/api/jobs":
                job_id = self.manager.start(self._read_json())
                self._json({"job_id": job_id}, 202)
                return
            if path == "/api/write-batches":
                batch = self.write_batches.create(self._read_json())
                self._json({key: value for key, value in batch.items()
                            if key not in {"records"}}, 201)
                return
            if path == "/api/activation-jobs":
                job_id = self.activation_jobs.start(self._read_json())
                self._json({"job_id": job_id}, 202)
                return
            if path == "/api/platform-context-jobs":
                job_id = self.platform_context_jobs.start(self._read_json())
                self._json({"job_id": job_id}, 202)
                return
            if path == "/api/platform-inventory-jobs":
                job_id = self.platform_inventory_jobs.start(self._read_json())
                self._json({"job_id": job_id}, 202)
                return
            if path == "/api/platform-batches":
                payload = self._read_json()
                owner = self._batch_owner()
                if payload.get("org_id") != owner["org_id"]:
                    raise platform_batch.PlatformBatchStop("批次组织与已识别账号不一致。")
                payload["owner"] = owner
                batch = self.platform_batches.create(payload)
                self._json({key: value for key, value in batch.items() if key != "records"}, 201)
                return
            if path == "/api/platform-qr-jobs":
                job_id = self.platform_qr_jobs.start(self._batch_action_payload())
                self._json({"job_id": job_id}, 202)
                return
            if path in {
                "/api/platform-activation/preview",
                "/api/platform-activation/submit",
                "/api/platform-activation/verify",
            }:
                action = path.rsplit("/", 1)[-1]
                job_id = self.platform_activation_jobs.start(self._batch_action_payload(), action)
                self._json({"job_id": job_id}, 202)
                return
            if path.startswith("/api/activation-jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/api/activation-jobs/").removesuffix("/cancel").strip("/")
                if not self.activation_jobs.cancel(job_id):
                    self._json({"error": "激活数据任务不存在或已经结束。"}, 409)
                else:
                    self._json({"ok": True})
                return
            if path.startswith("/api/platform-qr-jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/api/platform-qr-jobs/").removesuffix("/cancel").strip("/")
                if not self.platform_qr_jobs.cancel(job_id):
                    self._json({"error": "二维码任务不存在或已经结束。"}, 409)
                else:
                    self._json({"ok": True})
                return
            if path.startswith("/api/jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/api/jobs/").removesuffix("/cancel").strip("/")
                if not self.manager.cancel(job_id):
                    self._json({"error": "任务不存在或已经结束。"}, 409)
                else:
                    self._json({"ok": True})
                return
            self._json({"error": "接口不存在。"}, 404)
        except (checker.Stop, platform_batch.PlatformBatchStop) as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            self._json({"error": "服务内部发生未预期错误。"}, 500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="eSIM Profile 状态 Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="服务端结果目录")
    parser.add_argument("--profile-workers", type=int, default=2,
                        help="并发 Profile 查询数，默认 2，范围 1—16")
    args = parser.parse_args(argv)
    if not 1 <= args.profile_workers <= 16:
        parser.error("--profile-workers 必须是 1—16")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_headless_output(output_dir)
    manager = JobManager(output_dir, args.profile_workers)
    write_batches = WriteBatchManager(output_dir)
    activation_jobs = ActivationJobManager(write_batches, output_dir)
    platform_context_jobs = PlatformContextJobManager()
    platform_inventory_jobs = PlatformInventoryJobManager()
    platform_batches = platform_batch.PlatformBatchStore(output_dir)
    platform_qr_jobs = PlatformQrJobManager(platform_batches, output_dir)
    platform_activation_jobs = PlatformActivationJobManager(platform_batches, output_dir)
    Handler.manager = manager
    Handler.installation_jobs = InstallationManager(output_dir)
    Handler.write_batches = write_batches
    Handler.activation_jobs = activation_jobs
    Handler.platform_context_jobs = platform_context_jobs
    Handler.platform_inventory_jobs = platform_inventory_jobs
    Handler.platform_batches = platform_batches
    Handler.platform_qr_jobs = platform_qr_jobs
    Handler.platform_activation_jobs = platform_activation_jobs
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    print(f"eSIM Profile 状态服务已启动：http://{args.host}:{args.port}")
    print(f"结果目录：{output_dir}")
    print("按 Ctrl+C 停止服务。")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n服务已停止。")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
