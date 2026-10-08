"""Start only this project's services, and stop only their process groups."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]


def available(port: int) -> bool:
    with socket.socket() as sock:
        # Recently closed connections can remain in TIME_WAIT without any running service.
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            sock.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False


def main() -> int:
    if not (ROOT / ".venv/bin/python").is_file() or not (ROOT / "frontend/.next/BUILD_ID").is_file():
        print("尚未准备运行环境，请按 README 的开发安装步骤准备后再启动。")
        return 1
    if not all(available(port) for port in (8765, 3000)):
        print("本机端口 8765 或 3000 已占用。请先关闭之前的产品工厂窗口；不会结束其他程序。")
        return 1
    children = []
    environment = os.environ.copy()
    environment["FACTORY_DATA_DIR"] = str(
        Path(environment.get("FACTORY_DATA_DIR", str(ROOT / "data"))).resolve()
    )
    try:
        children.append(
            subprocess.Popen(
                [
                    str(ROOT / ".venv/bin/python"),
                    "-m",
                    "uvicorn",
                    "backend.app.main:create_app",
                    "--factory",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    "8765",
                    "--no-access-log",
                ],
                cwd=ROOT,
                env=environment,
                start_new_session=True,
            )
        )
        children.append(
            subprocess.Popen(
                ["npm", "run", "start"], cwd=ROOT / "frontend", env=environment, start_new_session=True
            )
        )
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if any(child.poll() is not None for child in children):
                print("服务启动失败，请保留终端中的错误提示以便排查。")
                return 1
            try:
                with urlopen("http://127.0.0.1:3000/api/factory/health", timeout=1) as response:
                    if response.status == 200 and json.load(response).get("status") == "ok":
                        break
            except OSError:
                time.sleep(0.2)
        else:
            print("启动超时，请检查本机服务。")
            return 1
        print("产品工厂已启动：http://127.0.0.1:3000；在此终端按 Control+C 停止。")
        if "--no-browser" not in sys.argv:
            subprocess.run(["open", "http://127.0.0.1:3000"], check=False)
        while all(child.poll() is None for child in children):
            time.sleep(0.5)
        return 1
    except KeyboardInterrupt:
        print("正在关闭本项目服务，登记信息已保存在本机。")
        return 0
    finally:
        for child in children:
            if child.poll() is None:
                os.killpg(child.pid, signal.SIGTERM)
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(child.pid, signal.SIGKILL)
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
