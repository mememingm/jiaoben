"""统一启动入口：默认启动 Web 服务；status 继续使用命令行模式。"""
from __future__ import annotations

import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def _is_web_mode(args: list[str]) -> bool:
    return not args or args[0] == "web"


def _detach_windows_web(args: list[str]) -> bool:
    """Windows 下让默认 Web 服务转到无控制台的 pythonw 进程。"""
    if sys.platform != "win32" or not _is_web_mode(args):
        return False
    if Path(sys.executable).name.lower() == "pythonw.exe":
        return False
    if "--console" in args or os.environ.get("ESIM_STATUS_NO_DETACH") == "1":
        return False
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        return False
    child_env = os.environ.copy()
    child_env["ESIM_STATUS_NO_DETACH"] = "1"
    child_args = [arg for arg in args if arg != "--console"]
    subprocess.Popen(
        [str(pythonw), str(Path(__file__).resolve()), *child_args],
        cwd=str(ROOT),
        env=child_env,
        close_fds=True,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    return True


def main() -> int:
    args = sys.argv[1:]
    if _detach_windows_web(args):
        return 0
    args = [arg for arg in args if arg != "--console"]
    if _is_web_mode(args):
        from nexsim_status_web import main as web_main
        return web_main(args[1:] if args else None)
    if args[0] == "prepare-write-batch":
        from nexsim_profile_writer import cli as writer_cli
        return writer_cli(args[1:])
    from nexsim_status_checker import main as checker_main
    return checker_main(args)


if __name__ == "__main__":
    raise SystemExit(main())
