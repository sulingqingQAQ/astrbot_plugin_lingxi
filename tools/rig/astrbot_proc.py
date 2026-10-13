"""E-1c 隔离 AstrBot 进程启动器。

只用标准库。日志文件以二进制写、读时 UTF-8 errors=replace 解码：
ASCII 标志串在任何控制台编码下都能命中；行尾 40/80 行只做诊断展示。
"""

import os
import subprocess
import time
import urllib.request

PYTHON = r"F:\Gemini\AstrBot\AstrBot\backend\python\python.exe"
APP_DIR = r"F:\Gemini\AstrBot\AstrBot\backend\app"
WEBUI_DIR = r"F:\Gemini\AstrBot\AstrBot\webui"

READY_MARKERS = (
    "Loading IM platform adapter aiocqhttp",
    "Starting WebUI at",
)


class AstrBotProc:
    """隔离 AstrBot 实例进程。只管理自己 start() 起的那个 PID。"""

    def __init__(
        self,
        root: str,
        webui_port: int,
        python: str = PYTHON,
        app_dir: str = APP_DIR,
        webui_dir: str = WEBUI_DIR,
    ) -> None:
        self._root = root
        self._webui_port = webui_port
        self._python = python
        self._app_dir = app_dir
        self._webui_dir = webui_dir
        self._proc = None
        self._log_file = None
        self._log_path = os.path.join(root, "astrbot_rig.log")

    @property
    def pid(self):
        return self._proc.pid if self._proc is not None else None

    @property
    def log_path(self):
        return self._log_path

    def start(self):
        if self._proc is not None and self._proc.poll() is None:
            return self._proc.pid
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None
        self._proc = None
        env = dict(os.environ)
        env["ASTRBOT_ROOT"] = self._root
        log_file = open(self._log_path, "wb")
        try:
            self._proc = subprocess.Popen(
                [self._python, "main.py", "--webui-dir", self._webui_dir],
                cwd=self._app_dir,
                env=env,
                stdout=log_file,
                stderr=subprocess.STDOUT,
            )
        except Exception:
            log_file.close()
            raise
        self._log_file = log_file
        return self._proc.pid

    def _log_text(self) -> str:
        try:
            with open(self._log_path, "rb") as f:
                raw = f.read()
        except OSError:
            return ""
        return raw.decode("utf-8", errors="replace")

    def _dashboard_ok(self) -> bool:
        try:
            with urllib.request.urlopen(
                "http://127.0.0.1:%d/" % self._webui_port, timeout=5
            ) as resp:
                return 200 <= resp.status < 500
        except Exception:
            return False

    def wait_ready(self, timeout: int = 120) -> None:
        deadline = time.time() + timeout
        http_ok = False
        text = ""
        while time.time() < deadline:
            if self._proc is not None and self._proc.poll() is not None:
                raise RuntimeError(
                    "AstrBot 进程已退出(rc=%s)，日志末尾 40 行：\n%s"
                    % (self._proc.returncode, "\n".join(self.log_tail(40)))
                )
            http_ok = self._dashboard_ok()
            text = self._log_text()
            if http_ok and any(m in text for m in READY_MARKERS):
                return
            time.sleep(1)
        raise RuntimeError(
            "wait_ready 超时(%ss)：http_ok=%s，日志末尾 40 行：\n%s"
            % (timeout, http_ok, "\n".join(self.log_tail(40)))
        )

    def stop(self) -> None:
        proc, self._proc = self._proc, None
        log_file, self._log_file = self._log_file, None
        try:
            if proc is None:
                return
            if proc.poll() is not None:
                return
            subprocess.run(
                ["taskkill", "/PID", str(proc.pid), "/T", "/F"],
                capture_output=True,
            )
        finally:
            if log_file is not None:
                try:
                    log_file.close()
                except Exception:
                    pass

    def log_tail(self, n: int):
        return self._log_text().splitlines()[-n:]
