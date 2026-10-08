"""发行包自检：离屏绘制本程序，并检查打包驱动，不访问真实站点。"""
import json
import os
from pathlib import Path
import subprocess
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QApplication, QLabel, QTextBrowser
from playwright._impl._driver import compute_driver_executable
from playwright.sync_api import sync_playwright
from .gui import MainWindow
from .models import Course, VideoTask
from .resources import resource_path
from .storage import data_dir


def run_smoke() -> int:
    os.environ["QT_QPA_PLATFORM"] = "offscreen"
    app = QApplication.instance() or QApplication([])
    # 即使用户直接执行 --self-test，也不覆盖真实任务记录和登录目录。
    root = data_dir() / "self-test"
    root.mkdir(parents=True, exist_ok=True)
    window = MainWindow(root)
    window.show()
    result = {}

    def check():
        try:
            node, cli = compute_driver_executable()
            result["driver_files"] = Path(node).is_file() and Path(cli).is_file()
            flags = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
            command = subprocess.run([node, cli, "--version"], capture_output=True, timeout=15, creationflags=flags)
            result["driver_runs"] = command.returncode == 0
            result["driver_version"] = command.stdout.decode("utf-8").strip()
            browser_path = os.environ.get("XUETANG_SMOKE_BROWSER")
            browser_channel = os.environ.get("XUETANG_SMOKE_CHANNEL")
            if browser_path or browser_channel:
                with sync_playwright() as playwright:
                    browser = playwright.chromium.launch(headless=True, executable_path=browser_path, channel=browser_channel)
                    try:
                        page = browser.new_page()
                        page.set_content('<meta charset="utf-8"><h1>发行包浏览器自检</h1>')
                        result["browser_control"] = page.locator("h1").inner_text() == "发行包浏览器自检"
                    finally:
                        browser.close()
            else:
                result["browser_control"] = "未指定测试浏览器"
            result["help_utf8"] = "学堂云" in resource_path("docs/使用说明.md").read_text(encoding="utf-8")
            result["tutorial_utf8"] = "绕过后台暂停检测" in resource_path("docs/使用教程.md").read_text(encoding="utf-8")
            window.show_courses([Course("a", "工程伦理", "2026 秋季").to_dict(), Course("b", "大学英语", "班级 2").to_dict()])
            task = VideoTask("a", "工程伦理", "https://dcc.yuketang.cn/pro/courselist", "第一章 · 技术与责任", 0, progress=46, status="播放中")
            window.show_tasks([task.to_dict()])
            window.on_state({"state": "PLAYING", "text": "已静音，正在播放（离屏界面自检）", "index": 0, "total": 1, "rate": 2, "task": task.to_dict()})
            app.processEvents()
            result["ui_rendered"] = window.grab().save(str(root / "ui-preview.png"))
            result["window_width"] = window.width()
            result["window_height"] = window.height()
            window.help_button.click()
            app.processEvents()
            tutorial = window.tutorial_dialog
            content = tutorial.findChild(QTextBrowser)
            result["tutorial_rendered"] = tutorial.grab().save(str(root / "tutorial-preview.png"))
            result["tutorial_scrollable"] = content.verticalScrollBar().maximum() > 0
            result["tutorial_markdown_clean"] = "**" not in content.toPlainText()
            result["tutorial_keeps_playing"] = window.state == "PLAYING" and not tutorial.isModal()
            content.verticalScrollBar().setValue(content.verticalScrollBar().maximum())
            window.help_button.click()
            result["tutorial_single_instance"] = window.tutorial_dialog is tutorial
            tutorial.close()
            window.help_button.click()
            app.processEvents()
            result["tutorial_reopened"] = tutorial.isVisible() and content.verticalScrollBar().value() == 0
            tutorial.close()
            for state in ("IDLE", "USER_PAUSED", "NEEDS_ACTION", "COMPLETED"):
                window.on_state({"state": state, "text": "界面状态自检", "index": 0, "total": 1,
                                 "rate": 2, "task": None if state == "IDLE" else task.to_dict()})
                app.processEvents()
                window.grab().save(str(root / f"state-{state.lower()}.png"))
            long_task = task.to_dict()
            long_task["title"] = "长标题显示检查：" + "劳动教育与职业安全实践课程" * 8
            window.show_tasks([long_task])
            window.on_state({"state": "PLAYING", "text": "已静音，正在播放（长标题与小窗口自检）", "index": 0,
                             "total": 1, "rate": 2, "task": long_task})
            window.resize(900, 660)
            app.processEvents()
            result["minimum_ui_rendered"] = window.grab().save(str(root / "ui-minimum.png"))
            result["minimum_size"] = [window.width(), window.height()]
            bounds = window.centralWidget().rect()
            essentials = [window.help_button, window.state_badge, window.background_checkbox,
                          window.progress_value, *window.buttons.values(),
                          window.findChild(QLabel, "watermark")]
            result["minimum_controls_fit"] = all(bounds.contains(widget.geometry().translated(
                widget.parentWidget().mapTo(window.centralWidget(), widget.parentWidget().rect().topLeft())))
                for widget in essentials)
            result["tray_available"] = window.tray.isSystemTrayAvailable()
            result["notification_supported"] = window.tray.supportsMessages()
            result["success"] = all(result.get(key) for key in ("driver_files", "driver_runs", "help_utf8", "ui_rendered",
                "tutorial_utf8", "tutorial_rendered", "tutorial_scrollable", "tutorial_markdown_clean", "tutorial_keeps_playing",
                "tutorial_single_instance", "tutorial_reopened", "minimum_ui_rendered", "minimum_controls_fit"))
            result["success"] = result["success"] and result["minimum_size"] == [900, 660]
            if browser_path or browser_channel:
                result["success"] = result["success"] and result.get("browser_control") is True
        except Exception as exc:
            result["success"] = False
            result["error_type"] = type(exc).__name__
        finally:
            (root / "smoke-report.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            window.close()

    QTimer.singleShot(350, check)
    app.exec()
    return 0 if result.get("success") else 1
