import json
import hashlib
from pathlib import Path
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QColor, QFont, QFontDatabase, QIcon, QPainter, QPixmap
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QDialog, QDialogButtonBox,
    QFormLayout, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMainWindow,
    QMessageBox, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QSplitter,
    QStyle, QSizePolicy, QTextBrowser, QSystemTrayIcon, QTableWidget, QTableWidgetItem, QVBoxLayout, QWidget)
from .models import Course, allowed_url
from .site import SELECTORS
from .storage import Store
from .worker import ACTIVE, Worker
from .browser import installed_edge


STATE_LABELS = {"IDLE": "就绪", "OPENING": "准备浏览器", "SCANNING": "扫描课程", "PLAYING": "播放中",
                "WAIT_VISIBLE": "等待窗口恢复", "WAIT_CONFIRM": "等待平台确认", "USER_PAUSED": "已暂停",
                "NEEDS_ACTION": "需要处理", "STOPPED": "已停止", "COMPLETED": "队列已完成"}

DEVELOPER_WATERMARK = "由‘霁月狐’进行开发"
BACKGROUND_LABEL = "绕过后台暂停检测"
BACKGROUND_DESCRIPTION = "开启后可以最小化后继续播放，绕过学堂云的必须在前台才能播放的检测"


def app_icon():
    pixmap = QPixmap(64, 64)
    pixmap.fill(QColor("#2396f3"))
    painter = QPainter(pixmap)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor("#ffffff"))
    from PySide6.QtGui import QPolygon
    from PySide6.QtCore import QPoint
    painter.drawPolygon(QPolygon([QPoint(25, 16), QPoint(25, 48), QPoint(47, 32)]))
    painter.end()
    return QIcon(pixmap)


def ensure_fonts():
    """Windows 离屏 Qt 插件不自动枚举系统字体，显式加载本机字体供自检。"""
    if "Microsoft YaHei UI" not in QFontDatabase.families():
        import os
        fonts = Path(os.environ.get("SystemRoot", "C:/Windows")) / "Fonts"
        for filename in ("msyh.ttc", "msyhbd.ttc", "segoeui.ttf"):
            path = fonts / filename
            if path.is_file():
                QFontDatabase.addApplicationFont(str(path))
    app = QApplication.instance()
    if app:
        app.setFont(QFont("Microsoft YaHei UI", 10))


class CurrentVideoLabel(QLabel):
    """长标题单行省略，悬停显示完整文本，不挤压视频队列。"""
    def setText(self, text):
        self.full_text = text
        self.setToolTip(text)
        QLabel.setText(self, self.fontMetrics().elidedText(text, Qt.TextElideMode.ElideRight, max(1, self.contentsRect().width())))

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self.setText(getattr(self, "full_text", ""))


class SelectorDialog(QDialog):
    def __init__(self, overrides, parent):
        super().__init__(parent)
        self.setWindowTitle("页面控件设置")
        self.resize(780, 650)
        layout = QVBoxLayout(self)
        note = QLabel("默认值来自公开前端源码。仅在页面识别失败时调整 CSS；保存后点击诊断检查数量。\n视频、播放器、完成度及各播放按钮应在当前页面唯一匹配。留空使用默认值。")
        note.setWordWrap(True)
        layout.addWidget(note)
        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QFormLayout(content)
        self.inputs = {}
        for key, default in SELECTORS.items():
            edit = QLineEdit(overrides.get(key, ""))
            edit.setPlaceholderText(default)
            form.addRow(key, edit)
            self.inputs[key] = edit
        scroll.setWidget(content)
        layout.addWidget(scroll)
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel)
        reset = buttons.addButton("恢复默认", QDialogButtonBox.ButtonRole.ResetRole)
        reset.clicked.connect(lambda: [item.clear() for item in self.inputs.values()])
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def values(self):
        return {key: edit.text().strip() for key, edit in self.inputs.items() if edit.text().strip()}


class MainWindow(QMainWindow):
    def __init__(self, root: Path):
        super().__init__()
        ensure_fonts()
        self.root, self.store = root, Store(root)
        self.setWindowTitle("学堂云播放助手")
        self.setWindowIcon(app_icon())
        self.resize(1160, 820)
        self.setMinimumSize(900, 660)
        self.state = "IDLE"
        self.closing = False
        self.tutorial_dialog = None
        self.courses = []
        self.icon = app_icon()
        self.tray = QSystemTrayIcon(self.icon, self)
        self.tray.setToolTip("学堂云播放助手")
        self.tray.activated.connect(lambda reason: self.showNormal() if reason == QSystemTrayIcon.ActivationReason.Trigger else None)
        self.tray.show()
        self.build_ui()
        self.worker = Worker(root, self)
        self.worker.message.connect(self.add_log)
        self.worker.state_changed.connect(self.on_state)
        self.worker.courses_changed.connect(self.show_courses)
        self.worker.tasks_changed.connect(self.show_tasks)
        self.worker.notify.connect(self.notification)
        self.worker.diagnostic.connect(self.show_diagnostic)
        self.worker.background_changed.connect(self.on_background_changed)
        self.worker.finished.connect(self.on_worker_finished)
        saved = self.store.read("session.json", {})
        if not isinstance(saved, dict):
            saved = {}
        self.saved_selection = saved.get("selected", [])
        self.show_courses(saved.get("courses", []))
        self.show_tasks(saved.get("queue", []))
        self.worker.start()
        self.add_log("已载入本机记录。再次开始前会重新扫描平台当前进度。")

    def build_ui(self):
        container = QWidget()
        container.setObjectName("centralWidget")
        self.setCentralWidget(container)
        layout = QVBoxLayout(container)
        layout.setContentsMargins(26, 22, 26, 20)
        layout.setSpacing(14)
        header = QHBoxLayout()
        title = QLabel("学堂云 / 本地播放助手")
        title.setObjectName("title")
        header.addWidget(title)
        header.addStretch()
        self.state_badge = QLabel("就绪")
        self.state_badge.setObjectName("badge")
        self.help_button = QPushButton("使用说明")
        self.help_button.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_FileDialogInfoView))
        self.help_button.clicked.connect(self.help)
        header.addWidget(self.help_button)
        header.addSpacing(6)
        header.addWidget(self.state_badge)
        layout.addLayout(header)
        intro = QLabel("手动登录 → 勾选课程 → 开始播放。作业和考试会自动跳过。")
        intro.setObjectName("muted")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        toolbar = QHBoxLayout()
        self.browser_choice = QComboBox()
        self.browser_choice.setMinimumWidth(150)
        self.browser_choice.addItem("专用 Chromium", "chromium")
        self.browser_choice.addItem("已安装 Edge", "msedge")
        if installed_edge():
            self.browser_choice.setCurrentIndex(1)
        toolbar.addWidget(self.browser_choice)
        self.buttons = {}
        icons = {"open": QStyle.StandardPixmap.SP_DialogOpenButton,
                 "refresh": QStyle.StandardPixmap.SP_BrowserReload,
                 "start": QStyle.StandardPixmap.SP_MediaPlay,
                 "pause": QStyle.StandardPixmap.SP_MediaPause,
                 "resume": QStyle.StandardPixmap.SP_MediaPlay,
                 "stop": QStyle.StandardPixmap.SP_MediaStop}
        for label, name in [("打开浏览器", "open"), ("刷新课程", "refresh"), ("开始", "start"),
                            ("暂停", "pause"), ("继续", "resume"), ("停止", "stop")]:
            button = QPushButton(label)
            button.setObjectName(name)
            button.setIcon(self.style().standardIcon(icons[name]))
            button.clicked.connect(lambda checked=False, key=name: self.command(key))
            self.buttons[name] = button
            toolbar.addWidget(button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        options = QHBoxLayout()
        options.setSpacing(14)
        self.background_checkbox = QCheckBox(BACKGROUND_LABEL)
        preferences = self.store.read("preferences.json", {})
        self.background_checkbox.setChecked(isinstance(preferences, dict) and preferences.get("background_playback") is True)
        self.background_checkbox.setToolTip(BACKGROUND_DESCRIPTION + "；关闭后需保持播放窗口可见。手动暂停仍须点击继续。")
        self.background_checkbox.toggled.connect(lambda enabled: self.worker.submit("background", enabled))
        options.addWidget(self.background_checkbox)
        explanation = QLabel(BACKGROUND_DESCRIPTION)
        explanation.setObjectName("muted")
        explanation.setWordWrap(True)
        explanation.setMinimumWidth(0)
        options.addWidget(explanation, 1)
        layout.addLayout(options)
        playback_panel = QWidget()
        playback_panel.setObjectName("playbackPanel")
        playback_layout = QVBoxLayout(playback_panel)
        playback_layout.setContentsMargins(18, 12, 18, 14)
        playback_layout.setSpacing(7)
        caption = QLabel("当前播放")
        caption.setObjectName("sectionCaption")
        playback_layout.addWidget(caption)
        self.status_text = QLabel("打开浏览器并手动登录，然后刷新课程")
        self.status_text.setWordWrap(True)
        self.status_text.setObjectName("muted")
        self.current = CurrentVideoLabel()
        self.current.setText("当前视频：—")
        self.current.setObjectName("currentVideo")
        self.current.setMinimumWidth(0)
        self.current.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
        playback_layout.addWidget(self.current)
        playback_layout.addWidget(self.status_text)
        progress_row = QHBoxLayout()
        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setTextVisible(False)
        self.progress.setMinimumWidth(60)
        progress_row.addWidget(self.progress)
        self.progress_value = QLabel("平台完成度：未知")
        self.progress_value.setObjectName("progressValue")
        progress_row.addWidget(self.progress_value)
        self.queue_count = QLabel("队列 0 / 0")
        progress_row.addWidget(self.queue_count)
        playback_layout.addLayout(progress_row)
        layout.addWidget(playback_panel)
        splitter = QSplitter(Qt.Orientation.Horizontal)
        course_panel = QWidget()
        course_layout = QVBoxLayout(course_panel)
        course_layout.setContentsMargins(0, 8, 10, 0)
        course_head = QHBoxLayout()
        course_label = QLabel("选择课程")
        course_label.setObjectName("sectionTitle")
        course_head.addWidget(course_label)
        self.select_all = QCheckBox("全选")
        self.select_all.toggled.connect(self.check_all)
        course_head.addStretch()
        course_head.addWidget(self.select_all)
        self.add_link = QPushButton("添加链接")
        self.add_link.clicked.connect(self.add_course_link)
        course_head.addWidget(self.add_link)
        course_layout.addLayout(course_head)
        self.course_table = QTableWidget(0, 3)
        self.course_table.setHorizontalHeaderLabels(["选择", "课程", "班级 / 入口"])
        self.course_table.setColumnWidth(0, 48)
        self.course_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.course_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.ResizeMode.Stretch)
        course_layout.addWidget(self.course_table)
        queue_panel = QWidget()
        queue_layout = QVBoxLayout(queue_panel)
        queue_layout.setContentsMargins(10, 8, 0, 0)
        queue_label = QLabel("视频队列 · 按课程目录顺序")
        queue_label.setObjectName("sectionTitle")
        queue_layout.addWidget(queue_label)
        self.task_table = QTableWidget(0, 4)
        self.task_table.setHorizontalHeaderLabels(["课程", "视频", "平台完成度", "状态"])
        self.task_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeMode.Stretch)
        self.task_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.ResizeMode.Stretch)
        self.task_table.setColumnWidth(2, 110)
        self.task_table.setColumnWidth(3, 90)
        queue_layout.addWidget(self.task_table)
        for table in (self.course_table, self.task_table):
            table.setAlternatingRowColors(True)
            table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
            table.setSelectionBehavior(QTableWidget.SelectionBehavior.SelectRows)
            table.verticalHeader().hide()
            table.verticalHeader().setDefaultSectionSize(42)
            table.setWordWrap(False)
            table.setTextElideMode(Qt.TextElideMode.ElideRight)
            table.setShowGrid(False)
            table.setMinimumWidth(0)
        splitter.addWidget(course_panel)
        splitter.addWidget(queue_panel)
        splitter.setSizes([380, 680])
        splitter.setChildrenCollapsible(False)
        self.course_table.setMinimumHeight(110)
        self.task_table.setMinimumHeight(110)
        layout.addWidget(splitter, 3)
        log_panel = QWidget()
        log_layout = QVBoxLayout(log_panel)
        log_layout.setContentsMargins(0, 0, 0, 0)
        log_layout.setSpacing(6)
        log_title = QLabel("运行日志")
        log_title.setObjectName("sectionTitle")
        log_layout.addWidget(log_title)
        self.logs = QPlainTextEdit()
        self.logs.setReadOnly(True)
        self.logs.setPlaceholderText("操作日志")
        self.logs.setMaximumBlockCount(1000)
        self.logs.setMinimumHeight(55)
        log_layout.addWidget(self.logs)
        log_panel.setMaximumHeight(145)
        layout.addWidget(log_panel, 1)
        footer = QHBoxLayout()
        for text, callback in [("控件设置", self.configure), ("诊断", lambda: self.command("diagnose")),
                               ("数据目录", self.show_data_directory)]:
            button = QPushButton(text)
            button.clicked.connect(callback)
            footer.addWidget(button)
        footer.addStretch()
        footer_note = QLabel("本机运行 · 以平台状态为准")
        footer_note.setObjectName("muted")
        footer.addWidget(footer_note)
        bottom_watermark = QLabel(DEVELOPER_WATERMARK)
        bottom_watermark.setObjectName("watermark")
        bottom_watermark.setAlignment(Qt.AlignmentFlag.AlignRight)
        footer.addWidget(bottom_watermark)
        layout.addLayout(footer)
        self.setStyleSheet("""
            QWidget { font-family: 'Microsoft YaHei UI'; font-size: 14px; color: #183348; }
            QMainWindow, QDialog, QWidget#centralWidget { background: #ffffff; }
            QLabel#title { font-size: 28px; font-weight: 700; }
            QLabel#muted { color: #71869b; font-size: 13px; }
            QLabel#watermark { color: #168ce9; font-size: 22px; font-weight: 700; padding: 6px 12px; background: #eef7ff; border: 1px solid #c4e3fc; border-radius: 8px; }
            QLabel#badge { background: #eaf6ff; color: #168ce9; padding: 9px 18px; border: 1px solid #c4e3fc; border-radius: 8px; font-weight: 600; }
            QWidget#playbackPanel { background: #eff8ff; border-radius: 10px; }
            QLabel#currentVideo { font-size: 19px; font-weight: 600; }
            QLabel#sectionTitle { font-size: 15px; font-weight: 600; padding-bottom: 4px; }
            QLabel#sectionCaption { color: #577895; font-size: 13px; }
            QLabel#progressValue { color: #168ce9; font-size: 16px; font-weight: 600; }
            QPushButton { background: #ffffff; padding: 9px 12px; border: 1px solid #d4e3f0; border-radius: 8px; }
            QPushButton:hover { background: #eef7ff; border-color: #91c9f5; }
            QPushButton:pressed { background: #dcefff; }
            QPushButton#start { background: #2396f3; color: white; border: 1px solid #2396f3; font-weight: 600; }
            QPushButton#start:hover { background: #168ce9; }
            QPushButton#start:disabled { background: #dceafa; color: #93a9bf; border-color: #dceafa; }
            QPushButton:disabled { color: #93a9bf; background: #f4f8fc; }
            QTableWidget { background: #ffffff; alternate-background-color: #f8fbfe; border: 1px solid #dbe8f3; border-radius: 8px; selection-background-color: #e0f1ff; selection-color: #183348; }
            QTableWidget::item { padding: 4px; border-bottom: 1px solid #eef3f8; }
            QTableWidget::item:disabled { color: #48637a; }
            QHeaderView::section { color: #577895; background: #f4f9fe; border: none; padding: 10px 6px; }
            QPlainTextEdit, QTextBrowser, QLineEdit { background: #ffffff; border: 1px solid #dbe8f3; border-radius: 8px; padding: 8px; selection-background-color: #dcefff; }
            QComboBox { background: white; border: 1px solid #d4e3f0; border-radius: 8px; padding: 9px; }
            QCheckBox { spacing: 8px; }
            QCheckBox::indicator { width: 17px; height: 17px; }
            QProgressBar { background: #d7edfe; border: none; border-radius: 6px; min-height: 12px; max-height: 12px; }
            QProgressBar::chunk { background: #2396f3; border-radius: 6px; }
            QSplitter::handle { background: #ffffff; width: 8px; }
            QLabel#tutorialNotice { background: #eef7ff; color: #146aaf; padding: 12px; border-radius: 8px; }
        """)

    def command(self, name):
        if name == "start":
            chosen = [self.courses[i] for i in range(len(self.courses))
                      if self.course_table.item(i, 0).checkState() == Qt.CheckState.Checked]
            if not chosen:
                QMessageBox.information(self, "选择课程", "请先勾选至少一门课程。")
                return
            self.worker.submit(name, chosen)
        elif name == "open":
            self.worker.submit(name, self.browser_choice.currentData())
        else:
            self.worker.submit(name)

    def on_background_changed(self, enabled):
        self.background_checkbox.blockSignals(True)
        self.background_checkbox.setChecked(enabled)
        self.background_checkbox.blockSignals(False)

    def check_all(self, checked):
        for row in range(self.course_table.rowCount()):
            self.course_table.item(row, 0).setCheckState(Qt.CheckState.Checked if checked else Qt.CheckState.Unchecked)

    def show_courses(self, courses):
        selected = {self.courses[i]["key"] for i in range(len(self.courses))
                    if self.course_table.item(i, 0).checkState() == Qt.CheckState.Checked}
        selected.update(getattr(self, "saved_selection", []))
        self.courses = courses
        self.course_table.setRowCount(len(courses))
        for row, course in enumerate(courses):
            check = QTableWidgetItem()
            check.setFlags(Qt.ItemFlag.ItemIsEnabled | Qt.ItemFlag.ItemIsUserCheckable)
            check.setCheckState(Qt.CheckState.Checked if course["key"] in selected else Qt.CheckState.Unchecked)
            self.course_table.setItem(row, 0, check)
            self.course_table.setItem(row, 1, QTableWidgetItem(course["title"]))
            self.course_table.setItem(row, 2, QTableWidgetItem(course.get("subtitle", "")))
            for column in (1, 2):
                self.course_table.item(row, column).setToolTip(self.course_table.item(row, column).text())

    def show_tasks(self, tasks):
        self.task_table.setRowCount(len(tasks))
        for row, task in enumerate(tasks):
            values = [task["course_title"], task["title"],
                      "—" if task["progress"] is None else f'{task["progress"]:g}%', task["status"]]
            for col, value in enumerate(values):
                item = QTableWidgetItem(value)
                item.setToolTip(value)
                if task["status"] == "播放中":
                    item.setBackground(QColor("#e0f1ff"))
                self.task_table.setItem(row, col, item)
            if task["status"] == "播放中":
                self.task_table.selectRow(row)

    def on_state(self, value):
        self.state = value["state"]
        self.state_badge.setText(STATE_LABELS.get(self.state, self.state))
        self.status_text.setText(value["text"])
        task = value.get("task")
        if task:
            rate = f' · {value["rate"]:g}×' if value.get("rate") else ""
            self.current.setText(f'当前：{task["course_title"]} / {task["title"]}{rate}')
            progress = task.get("progress")
            self.progress.setValue(round(progress or 0))
            self.progress.setFormat("平台完成度：未知" if progress is None else f"平台完成度：{progress:g}%")
            self.progress_value.setText(self.progress.format())
        else:
            self.current.setText("当前视频：—")
            self.progress.setValue(0)
            self.progress.setFormat("平台完成度：未知")
            self.progress_value.setText("平台完成度：未知")
        self.queue_count.setText(f'队列 {min(max(value["index"] + 1, 0), value["total"])} / {value["total"]}')
        busy = self.state in ACTIVE or self.state == "OPENING"
        for key in ("open", "refresh", "start"):
            self.buttons[key].setEnabled(not busy and not self.closing)
        self.buttons["pause"].setEnabled(self.state in ACTIVE or self.state == "OPENING")
        self.buttons["resume"].setEnabled(self.state in ("USER_PAUSED", "NEEDS_ACTION"))
        self.buttons["stop"].setEnabled(self.state not in ("IDLE", "STOPPED", "COMPLETED"))
        self.course_table.setEnabled(not busy)
        self.select_all.setEnabled(not busy)
        self.add_link.setEnabled(not busy)
        self.browser_choice.setEnabled(not busy)

    def add_log(self, text):
        from datetime import datetime
        self.logs.appendPlainText(datetime.now().strftime("%H:%M:%S") + "  " + text)

    def notification(self, title, text):
        self.add_log("通知：" + text)
        if QSystemTrayIcon.isSystemTrayAvailable() and QSystemTrayIcon.supportsMessages():
            self.tray.showMessage(title, text, QSystemTrayIcon.MessageIcon.Information, 10000)
        else:
            self.add_log("Windows 通知不可用，请查看程序状态栏。")

    def add_course_link(self):
        url, ok = QInputDialog.getText(self, "添加课程入口", "粘贴学堂云课程学习页或视频 HTTPS 链接：")
        if not ok:
            return
        url = url.strip()
        try:
            valid = allowed_url(url)
        except ValueError:
            valid = False
        if not valid:
            QMessageBox.warning(self, "链接不支持", "仅支持 https://dcc.yuketang.cn 的链接。")
            return
        title, ok = QInputDialog.getText(self, "课程名称", "为该课程入口设置显示名称：")
        if not ok or not title.strip():
            return
        key = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
        if any(c["key"] == key for c in self.courses):
            return
        course = Course(key, title.strip(), "手动入口", url)
        self.worker.submit("add_course", course.to_dict())

    def configure(self):
        if self.state in ACTIVE or self.state == "OPENING":
            QMessageBox.information(self, "先暂停", "请先暂停或停止任务，再修改控件设置。")
            return
        dialog = SelectorDialog(self.store.read("selectors.json", {}), self)
        if dialog.exec() == QDialog.DialogCode.Accepted:
            self.worker.submit("configure", dialog.values())

    def show_diagnostic(self, result):
        dialog = QDialog(self)
        dialog.setWindowTitle("页面控件诊断 · 不包含登录信息")
        dialog.resize(720, 600)
        layout = QVBoxLayout(dialog)
        text = QPlainTextEdit(json.dumps(result, ensure_ascii=False, indent=2))
        text.setReadOnly(True)
        layout.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        close.rejected.connect(dialog.reject)
        layout.addWidget(close)
        dialog.exec()

    def help(self):
        from .resources import resource_path
        if self.tutorial_dialog is None:
            dialog = QDialog(self)
            dialog.setWindowTitle("使用教程")
            dialog.resize(860, 680)
            dialog.setMinimumSize(640, 480)
            layout = QVBoxLayout(dialog)
            layout.setContentsMargins(20, 18, 20, 18)
            notice = QLabel("重点：手动暂停后必须点击「继续」。关闭检测开关时，请保持播放窗口可见。")
            notice.setObjectName("tutorialNotice")
            notice.setWordWrap(True)
            layout.addWidget(notice)
            text = QTextBrowser()
            text.setObjectName("tutorialContent")
            text.setOpenExternalLinks(False)
            text.document().setDefaultStyleSheet("h1, h2, h3 { color: #146aaf; } p { margin-bottom: 10px; } blockquote { color: #146aaf; }")
            text.setMarkdown(resource_path("docs/使用教程.md").read_text(encoding="utf-8"))
            layout.addWidget(text, 1)
            close = QPushButton("关闭教程")
            close.clicked.connect(dialog.close)
            buttons = QHBoxLayout()
            buttons.addStretch()
            buttons.addWidget(close)
            layout.addLayout(buttons)
            self.tutorial_dialog = dialog
        # 非模态窗口，不向工作线程发送暂停或继续指令。
        dialog = self.tutorial_dialog
        if not dialog.isVisible():
            dialog.findChild(QTextBrowser).verticalScrollBar().setValue(0)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()

    def show_data_directory(self):
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtCore import QUrl
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.root)))

    def closeEvent(self, event):
        if self.worker.isRunning():
            event.ignore()
            if not self.closing:
                self.closing = True
                self.status_text.setText("正在停止任务并关闭专用浏览器，请稍候…")
                self.centralWidget().setEnabled(False)
                self.worker.submit("quit")
            return
        self.tray.hide()
        event.accept()

    def on_worker_finished(self):
        if self.closing:
            self.close()
