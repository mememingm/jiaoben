"""Cross-platform web service for the eSIM Profile status tool.

The service uses only Python's standard library. Keep it bound to localhost
unless an authenticated reverse proxy is placed in front of it.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import nexsim_status_checker as checker
import nexsim_profile_writer as writer
import nexsim_profile_activation as activation
import nexsim_platform_activation as platform_activation


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
    def __init__(self, output_dir: Path, profile_workers: int = 8):
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
            "query_params": {"simType": "ESIM"},
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
                percent = 20 + ratio * 75
                message = f"读取 Profile 状态：{current} / {total}"
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
            self._update(job_id, state="done", progress=100, stage="done",
                         message="查询完成，结果已保存。", rows=rows,
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
                statuses: dict[str, int] = {}
                for row in rows:
                    if isinstance(row, dict):
                        status = str(row.get("esimProfileStatus", "failed"))
                        statuses[status] = statuses.get(status, 0) + 1
                items.append({
                    "filename": path.name,
                    "account": str(payload.get("account") or "未知账号（旧记录）"),
                    "queried_at": payload.get("queried_at", ""),
                    "count": len(rows),
                    "statuses": statuses,
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
        payload["account"] = str(payload.get("account") or "未知账号（旧记录）")
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


class PlatformActivationJobManager:
    """Run platform activation preview, submit, or verification as explicit jobs."""

    def __init__(self, batches: WriteBatchManager, output_dir: Path):
        self.batches = batches
        self.output_dir = output_dir.resolve()
        self.jobs: dict[str, dict[str, Any]] = {}
        self.lock = threading.Lock()

    def start(self, payload: dict[str, Any], action: str) -> str:
        if action not in {"preview", "submit", "verify"}:
            raise checker.Stop("平台开户动作不正确。")
        batch_id = str(payload.get("batch_id", "")).strip()
        self.batches._validate_id(batch_id)
        batch = self.batches.snapshot(batch_id)
        if batch is None:
            raise checker.Stop("写卡批次不存在。")
        username = str(payload.get("username", "")).strip()
        password = payload.get("password")
        if not username or not isinstance(password, str) or not password:
            raise checker.Stop("平台开户需要重新确认后台账号和密码。")
        org_id = payload.get("org_id")
        product_id = payload.get("product_id")
        if type(org_id) is not int or org_id <= 0 or type(product_id) is not int or product_id <= 0:
            raise checker.Stop("组织 ID 和产品 ID 必须是正整数。")
        max_total = str(payload.get("max_total", "")).strip()
        if action != "verify" and not max_total:
            raise checker.Stop("提交前必须填写本次授权金额上限。")
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
                    job["max_total"], progress,
                )
                self._update(job_id, state="done", progress=100, stage="done",
                             message="预检完成，尚未提交激活。", preview=result, result=result)
            elif action == "submit":
                result = platform_activation.submit_activation(
                    client, job["batch"], self.output_dir, job["org_id"], job["product_id"],
                    job["max_total"], progress,
                )
                self._update(job_id, state="done", progress=100, stage="done",
                             message="激活提交结束，请执行结果核验。", result=result)
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
    manager: JobManager
    write_batches: WriteBatchManager
    activation_jobs: ActivationJobManager
    platform_activation_jobs: PlatformActivationJobManager

    def log_message(self, format: str, *args: Any) -> None:
        # Do not log request bodies, credentials, or query results.
        print(f"{self.address_string()} - {format % args}")

    def _json(self, value: Any, status: int = 200) -> None:
        data = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

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

    def do_GET(self) -> None:
        path = urlsplit(self.path).path
        if path == "/api/health":
            self._json({"ok": True, "service": "esim-profile-status"})
            return
        if path == "/api/history":
            self._json({"items": self.manager.history_index()})
            return
        if path == "/api/write-batches":
            self._json({"items": self.write_batches.index()})
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
            if path in {
                "/api/platform-activation/preview",
                "/api/platform-activation/submit",
                "/api/platform-activation/verify",
            }:
                action = path.rsplit("/", 1)[-1]
                job_id = self.platform_activation_jobs.start(self._read_json(), action)
                self._json({"job_id": job_id}, 202)
                return
            if path.startswith("/api/activation-jobs/") and path.endswith("/cancel"):
                job_id = path.removeprefix("/api/activation-jobs/").removesuffix("/cancel").strip("/")
                if not self.activation_jobs.cancel(job_id):
                    self._json({"error": "激活数据任务不存在或已经结束。"}, 409)
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
        except checker.Stop as exc:
            self._json({"error": str(exc)}, 400)
        except Exception:
            self._json({"error": "服务内部发生未预期错误。"}, 500)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="eSIM Profile 状态 Web 服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    parser.add_argument("--port", type=int, default=8765, help="监听端口，默认 8765")
    parser.add_argument("--output-dir", type=Path, default=DEFAULT_OUTPUT_DIR,
                        help="服务端结果目录")
    parser.add_argument("--profile-workers", type=int, default=8,
                        help="并发 Profile 查询数，范围 1—16")
    args = parser.parse_args(argv)
    if not 1 <= args.profile_workers <= 16:
        parser.error("--profile-workers 必须是 1—16")
    output_dir = args.output_dir.resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    _configure_headless_output(output_dir)
    manager = JobManager(output_dir, args.profile_workers)
    write_batches = WriteBatchManager(output_dir)
    activation_jobs = ActivationJobManager(write_batches, output_dir)
    platform_activation_jobs = PlatformActivationJobManager(write_batches, output_dir)
    Handler.manager = manager
    Handler.write_batches = write_batches
    Handler.activation_jobs = activation_jobs
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
