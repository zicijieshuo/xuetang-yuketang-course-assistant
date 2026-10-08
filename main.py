"""UTF-8 Windows 桌面入口。"""
import ctypes
import sys
from PySide6.QtCore import QLockFile
from PySide6.QtWidgets import QApplication, QMessageBox
from xuetang_assistant.storage import data_dir
from xuetang_assistant.gui import MainWindow


def main():
    if "--self-test" in sys.argv:
        from xuetang_assistant.smoke import run_smoke
        return run_smoke()
    if sys.platform == "win32":
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("XuetangAssistant.Desktop.1")
    app = QApplication(sys.argv)
    app.setApplicationName("学堂云播放助手")
    app.setOrganizationName("XuetangAssistant")
    root = data_dir()
    lock = QLockFile(str(root / "assistant.lock"))
    lock.setStaleLockTime(0)
    if not lock.tryLock(0):
        QMessageBox.information(None, "程序已运行", "学堂云播放助手已经运行，请使用已有窗口。")
        return 1
    window = MainWindow(root)
    window.show()
    result = app.exec()
    lock.unlock()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
