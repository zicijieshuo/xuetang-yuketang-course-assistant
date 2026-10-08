"""专用浏览器和首次运行准备；不使用用户日常浏览器配置。"""
import os
from pathlib import Path
import subprocess
import threading
import time
from playwright.sync_api import sync_playwright
from .models import AssistantError, Cancelled


def edge_executable() -> Path | None:
    bases = [os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles"), os.environ.get("LOCALAPPDATA")]
    return next((Path(base) / "Microsoft/Edge/Application/msedge.exe" for base in bases
                 if base and (Path(base) / "Microsoft/Edge/Application/msedge.exe").is_file()), None)


def installed_edge() -> bool:
    return edge_executable() is not None


class BrowserSession:
    def __init__(self, root: Path, log, cancelled: threading.Event):
        self.root, self.log, self.cancelled = root, log, cancelled
        self.playwright = None
        self.context = None
        self.page = None
        self.dialog = False
        self.cdp = None
        self.channel = None
        self.browser = None
        self.process = None
        self.background_playback = False
        # 不继承其他工具设置的浏览器路径。
        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(root / "browsers")

    def install(self) -> None:
        from playwright._impl._driver import compute_driver_executable, get_driver_env
        node, cli = compute_driver_executable()
        self.log("首次使用：正在准备 Chromium 浏览器，下载大小约 200 MB，请保持网络连接。")
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        # 将完整安装输出只留在内存；UI 只显示过滤后的下载进度，避免代理凭据进入日志。
        process = subprocess.Popen([node, cli, "install", "--no-shell", "chromium"], env=get_driver_env(),
                                   stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=flags)
        lines = []
        def drain():
            for line in process.stdout:
                text = line.decode("utf-8", errors="replace")
                if "%" in text:
                    import re
                    progress = re.search(r"\b\d+%", text)
                    if progress:
                        self.log("浏览器准备：" + progress.group())
                lines.append(text)
                if len(lines) > 20:
                    lines.pop(0)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        try:
            while process.poll() is None:
                if self.cancelled.wait(0.2):
                    process.terminate()
                    raise Cancelled()
            if process.returncode != 0:
                raise AssistantError("浏览器下载失败，请检查网络，或在浏览器选项中选择已安装的 Edge。")
        finally:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=10)
            reader.join(timeout=2)
        self.log("Chromium 浏览器准备完成。")

    def open(self, channel: str = "chromium") -> None:
        if self.page is not None and not self.page.is_closed():
            return
        self.close()
        self.playwright = sync_playwright().start()
        if channel == "chromium" and not Path(self.playwright.chromium.executable_path).exists():
            self.install()
        requested = channel
        candidates = [channel]
        if channel == "chromium" and installed_edge():
            candidates.append("msedge")
        try:
            for candidate in candidates:
                try:
                    self._start_runtime(candidate)
                    break
                except Cancelled:
                    raise
                except Exception:
                    self._stop_runtime()
                    if candidate == candidates[-1]:
                        raise
                    self.log("Chromium 启动失败，正在尝试本机 Edge 的独立登录窗口。")
            self.context.set_default_timeout(3000)
            self.use_page(self.context.pages[0] if self.context.pages else self.context.new_page())
            self.channel = requested
        except Cancelled:
            self.close()
            raise
        except Exception:
            self.close()
            raise AssistantError("无法启动专用浏览器。请关闭本程序的其他实例，或切换 Chromium / Edge 后重试。") from None

    def _start_runtime(self, channel):
        executable = edge_executable() if channel == "msedge" else Path(self.playwright.chromium.executable_path)
        if executable is None or not executable.is_file():
            raise AssistantError("未找到该浏览器，请选择已安装 Edge 或准备 Chromium。")
        profile = self.root / ("profile-" + channel)
        profile.mkdir(parents=True, exist_ok=True)
        active_port = profile / "DevToolsActivePort"
        active_port.unlink(missing_ok=True)  # 仅清除本程序专用目录中的过期临时端口文件。
        flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
        self.process = subprocess.Popen([
            str(executable), f"--user-data-dir={profile}", "--remote-debugging-port=0",
            "--remote-debugging-address=127.0.0.1", "--no-first-run", "--no-default-browser-check",
            "--start-maximized", "--mute-audio", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, creationflags=flags)
        deadline = time.monotonic() + 15
        port = None
        while time.monotonic() < deadline:
            if self.cancelled.wait(0.1):
                raise Cancelled()
            if active_port.is_file():
                try:
                    port = int(active_port.read_text(encoding="utf-8").splitlines()[0])
                    if 1 <= port <= 65535:
                        break
                except (OSError, ValueError, IndexError):
                    pass
            if self.process.poll() not in (None, 0):
                raise AssistantError("专用浏览器启动后退出，请检查浏览器安装。")
        if port is None:
            raise AssistantError("等待专用浏览器启动超时。")
        # 默认不模拟焦点；后台模式通过独立 CDP 会话显式设置，便于随时关闭。
        self.browser = self.playwright.chromium.connect_over_cdp(f"http://127.0.0.1:{port}", no_defaults=True, timeout=5000)
        self.context = self.browser.contexts[0]

    def _stop_runtime(self, graceful=False):
        if graceful and self.cdp:
            try:
                self.cdp.send("Browser.close")
            except Exception:
                pass
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
        if self.process and self.process.poll() is None:
            if graceful:
                try:
                    self.process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    pass
        if self.process and self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        self.browser = self.process = None

    def _on_dialog(self, dialog):
        self.dialog = True
        self.log("网页出现需要人工处理的弹窗，程序将暂停。")
        # 不自动接受或关闭站点弹窗。

    def use_page(self, page):
        if self.cdp:
            try:
                # Chromium 同一窗口中旧页面的模拟可能仍影响可见性，先显式清理。
                if self.background_playback:
                    self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": False})
                self.cdp.detach()
            except Exception:
                pass
        self.page = page
        self.page.on("dialog", self._on_dialog)
        self.cdp = self.context.new_cdp_session(page)
        if self.background_playback:
            self.set_background_playback(True)

    def set_background_playback(self, enabled):
        enabled = bool(enabled)
        previous = self.background_playback
        if self.cdp:
            try:
                self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": enabled})
                if enabled and not self.page.evaluate("document.visibilityState === 'visible' && !document.hidden && document.hasFocus()"):
                    raise AssistantError("浏览器未确认后台模式生效，请恢复窗口后重试。")
            except Exception:
                try:
                    self.cdp.send("Emulation.setFocusEmulationEnabled", {"enabled": previous})
                except Exception:
                    pass
                raise AssistantError("当前浏览器无法启用或关闭后台模式，请恢复窗口并重新打开浏览器。") from None
        self.background_playback = enabled

    def minimized(self) -> bool:
        if self.cdp is None:
            return False
        try:
            info = self.cdp.send("Browser.getWindowForTarget")
            bounds = self.cdp.send("Browser.getWindowBounds", {"windowId": info["windowId"]})
            return bounds["bounds"].get("windowState") == "minimized"
        except Exception:
            raise AssistantError("无法读取播放窗口状态，请重新打开专用浏览器。") from None

    def close(self):
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
        self._stop_runtime(graceful=True)
        if self.playwright:
            try:
                self.playwright.stop()
            except Exception:
                pass
        self.context = self.page = self.playwright = self.cdp = None
        self.channel = None
        self.dialog = False
