"""所有 Playwright 操作限定在一个工作线程，GUI 只投递命令。"""
from dataclasses import asdict
import itertools
import queue
import threading
import time
from PySide6.QtCore import QThread, Signal
from .browser import BrowserSession
from .models import Action, AssistantError, Cancelled, Course, COURSE_LIST, VideoTask, Watchdog
from .site import SiteAdapter
from .storage import Store, configure_logging


ACTIVE = {"SCANNING", "PLAYING", "WAIT_VISIBLE", "WAIT_CONFIRM"}


class Worker(QThread):
    message = Signal(str)
    state_changed = Signal(dict)
    courses_changed = Signal(list)
    tasks_changed = Signal(list)
    notify = Signal(str, str)
    diagnostic = Signal(dict)
    background_changed = Signal(bool)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = root
        self.store = Store(root)
        self.logger = configure_logging(root)
        self.commands = queue.PriorityQueue()
        self.command_sequence = itertools.count()
        self.discard_before = -1
        self.active_sequence = 0
        self.cancelled = threading.Event()
        self.session = None
        self.adapter = None
        self.state = "IDLE"
        self.tasks: list[VideoTask] = []
        self.selected: list[Course] = []
        self.courses: list[Course] = []
        self.index = -1
        self.watchdog = None
        self.next_tick = 0.0
        self.quit_requested = False
        self.channel = "chromium"
        self.rate = None
        self.prepared = False
        self.notice_hidden = False
        self.scan_pending = False
        preferences = self.store.read("preferences.json", {})
        self.background_playback = isinstance(preferences, dict) and preferences.get("background_playback") is True
        saved = self.store.read("session.json", {})
        if isinstance(saved, dict):
            for item in saved.get("courses", []):
                try:
                    self.courses.append(Course(**item))
                except (TypeError, ValueError):
                    pass

    def submit(self, name, value=None):
        priority = {"quit": 0, "stop": 1, "pause": 2}.get(name, 3)
        sequence = next(self.command_sequence)
        if name in ("pause", "stop", "quit"):
            self.discard_before = max(self.discard_before, sequence)
            self.cancelled.set()
        self.commands.put((priority, sequence, name, value))

    def check_cancel(self):
        if self.cancelled.is_set() or self.active_sequence < self.discard_before:
            raise Cancelled()

    def log(self, text):
        self.logger.info(text)
        self.message.emit(text)

    def set_state(self, state, text):
        self.state = state
        task = self.tasks[self.index] if 0 <= self.index < len(self.tasks) else None
        self.state_changed.emit({"state": state, "text": text, "index": self.index,
                                 "total": len(self.tasks), "rate": self.rate,
                                 "task": task.to_dict() if task else None})

    def save(self):
        self.store.write("session.json", {"courses": [c.to_dict() for c in self.courses],
                                          "selected": [c.key for c in self.selected],
                                          "queue": [t.to_dict() for t in self.tasks],
                                          "state": self.state})

    def publish_tasks(self):
        self.tasks_changed.emit([task.to_dict() for task in self.tasks])
        self.save()

    def ensure_browser(self):
        if self.session.page is None or self.session.page.is_closed():
            raise AssistantError("请先点击打开浏览器，并手动完成学堂云登录。")

    def safe_pause(self):
        if self.session.page and not self.session.page.is_closed() and not self.session.dialog:
            try:
                if not self.adapter.visible_matches(self.adapter.s["video"]):
                    return
                self.adapter.pause()
            except Exception:
                self.log("无法确认浏览器中的视频已暂停，请检查播放窗口。")

    def error(self, exc):
        self.safe_pause()
        text = str(exc) if isinstance(exc, AssistantError) else f"浏览器操作失败（{type(exc).__name__}），请检查页面后点击继续。"
        self.log(text)
        self.set_state("NEEDS_ACTION", text)
        self.notify.emit("播放助手需要处理", text)
        self.save()

    def start_tasks(self, selected):
        self.ensure_browser()
        if not selected:
            raise AssistantError("请先勾选至少一门课程。")
        self.safe_pause()
        self.selected = selected
        self.tasks = []
        self.index = -1
        self.scan_pending = True
        self.prepared = False
        self.rate = None
        self.watchdog = None
        self.set_state("SCANNING", "正在读取所选课程的最新目录和平台状态")
        self.publish_tasks()
        for course in selected:
            self.check_cancel()
            self.log(f"扫描课程：{course.title}。")
            tasks = self.adapter.scan(course)
            self.tasks.extend(tasks)
            self.log(f"课程 {course.title}：识别到 {len(tasks)} 个未完成且开放的视频。")
            self.publish_tasks()
        self.scan_pending = False
        self.index = 0
        self.next_task()

    def next_task(self):
        self.prepared = False
        self.notice_hidden = False
        self.watchdog = None
        self.rate = None
        if self.index >= len(self.tasks):
            if self.tasks:
                # 最后一个视频可能在播放结束前就达到 100%；卸载页面停止媒体。
                self.adapter.finish_playback()
            self.set_state("COMPLETED", "全部所选课程的视频队列处理完成")
            self.log("全部队列处理完成；没有待播放视频。")
            self.notify.emit("视频队列已完成", "所选课程中的开放且未完成视频已处理完毕。")
            self.publish_tasks()
            return
        task = self.tasks[self.index]
        task.status = "加载中"
        self.set_state("PLAYING", "正在打开下一个视频")
        self.publish_tasks()
        self.adapter.open_task(task)
        self.begin_current()

    def begin_current(self):
        task = self.tasks[self.index]
        snapshot = self.adapter.snapshot()
        task.progress = snapshot.progress
        self.watchdog = Watchdog(time.monotonic(), snapshot.current_time)
        if not snapshot.visible:
            self.wait_visible()
            return
        if snapshot.completed:
            self.complete_current()
            return
        self.rate = self.adapter.prepare_and_play()
        self.prepared = True
        task.status = "播放中"
        self.set_state("PLAYING", "已静音，正在播放")
        self.next_tick = time.monotonic() + 3
        self.publish_tasks()

    def complete_current(self):
        # 平台已确认完成，直接导航下一条，由页面卸载结束当前媒体；不点击暂停开关。
        task = self.tasks[self.index]
        task.status = "已完成"
        task.progress = 100.0
        self.log(f"平台确认视频已完成：{task.title}。")
        self.publish_tasks()
        self.index += 1
        # 用下一次循环进入下一条，避免一串已完成任务递归导航。
        self.prepared = False
        self.watchdog = None
        self.set_state("PLAYING", "准备切换下一个视频")
        self.next_tick = time.monotonic()

    def wait_visible(self):
        self.tasks[self.index].status = "等待窗口恢复"
        self.set_state("WAIT_VISIBLE", "请恢复播放窗口并切回视频标签页，恢复可见后自动继续")
        if not self.notice_hidden:
            self.notice_hidden = True
            self.notify.emit("播放窗口已隐藏", "请恢复浏览器并切回视频标签页；恢复后自动继续。")
            self.log("等待播放窗口恢复，已发送通知。")
        self.next_tick = time.monotonic() + 3
        self.publish_tasks()

    def tick(self):
        self.check_cancel()
        if self.watchdog is None:
            self.next_task()
            return
        snapshot = self.adapter.snapshot()
        task = self.tasks[self.index]
        old_progress = task.progress
        task.progress = snapshot.progress
        if not snapshot.visible:
            self.watchdog.decide(snapshot, time.monotonic())
            self.wait_visible()
            return
        if self.state == "WAIT_VISIBLE":
            self.notice_hidden = False
            # 如果在等待平台确认，不重新播放已结束/已刷新的视频。
            if self.prepared and (snapshot.ended or self.watchdog.reloaded):
                pass
            elif not snapshot.completed:
                self.rate = self.adapter.prepare_and_play()
                self.prepared = True
                snapshot = self.adapter.snapshot()
        action, text = self.watchdog.decide(snapshot, time.monotonic())
        if action == Action.COMPLETE:
            self.complete_current()
            return
        if action == Action.FAIL:
            raise AssistantError(text)
        if action == Action.RELOAD:
            self.log(text)
            self.adapter.goto(task.video_url)
            if not self.adapter.wait(lambda: bool(self.adapter.visible_matches(self.adapter.s["progress"])) and bool(self.adapter.visible_matches(self.adapter.s["video"])), 15):
                raise AssistantError("刷新后未找到视频或完成度控件。")
            self.adapter.pause()
        if action == Action.RECOVER:
            self.log(text)
            self.rate = self.adapter.prepare_and_play()
        task.status = "等待平台确认" if action in (Action.WAIT, Action.RELOAD) else "播放中"
        self.set_state("WAIT_CONFIRM" if task.status == "等待平台确认" else "PLAYING", text)
        self.next_tick = time.monotonic() + 3
        if old_progress != task.progress:
            self.publish_tasks()
        else:
            self.tasks_changed.emit([t.to_dict() for t in self.tasks])

    def handle(self, name, value):
        if name == "open":
            if self.state in ACTIVE:
                raise AssistantError("请先停止任务，再打开或切换浏览器。")
            self.channel = value or "chromium"
            if self.session.page and not self.session.page.is_closed():
                if self.session.channel == self.channel:
                    self.log("专用浏览器已经打开，请在该窗口登录或恢复视频标签页。")
                    return
                self.safe_pause()
                self.session.close()
            self.set_state("OPENING", "正在准备并打开专用浏览器")
            self.session.open(self.channel)
            self.adapter.goto(COURSE_LIST)
            self.set_state("IDLE", "浏览器已打开，请手动登录，然后刷新课程")
            self.log("请在专用浏览器手动登录学堂云。")
        elif name == "refresh":
            if self.state in ACTIVE:
                raise AssistantError("请先停止任务，再刷新课程。")
            self.ensure_browser()
            self.set_state("SCANNING", "正在读取课程列表")
            self.adapter.goto(COURSE_LIST)
            manual = [course for course in self.courses if course.url]
            self.courses = self.adapter.read_courses() + manual
            self.courses_changed.emit([c.to_dict() for c in self.courses])
            self.set_state("IDLE", f"已读取 {len(self.courses)} 门课程，请勾选后开始")
            self.save()
        elif name == "start":
            if self.state in ACTIVE:
                return
            self.start_tasks([Course(**item) for item in value])
        elif name == "add_course":
            course = Course(**value)
            if not any(c.key == course.key for c in self.courses):
                self.courses.append(course)
            self.courses_changed.emit([c.to_dict() for c in self.courses])
            self.save()
        elif name == "pause":
            self.safe_pause()
            self.set_state("USER_PAUSED", "已手动暂停，点击继续后恢复")
            self.log("用户暂停任务。")
            self.save()
        elif name == "resume":
            if self.state not in ("USER_PAUSED", "NEEDS_ACTION"):
                return
            self.ensure_browser()
            self.session.dialog = False
            self.adapter.assert_no_intervention()
            if self.scan_pending:
                self.start_tasks(self.selected)
            elif 0 <= self.index < len(self.tasks):
                # 恢复时定位同一视频，禁止将用户切到的另一视频当成当前任务。
                self.adapter.open_task(self.tasks[self.index])
                self.begin_current()
            else:
                self.set_state("IDLE", "请刷新课程并勾选，然后开始")
        elif name == "stop":
            self.safe_pause()
            self.scan_pending = False
            self.set_state("STOPPED", "已停止，队列已保留；再次开始将重新扫描平台进度")
            self.log("任务已停止，保留队列记录。")
            self.save()
        elif name == "configure":
            if self.state in ACTIVE:
                raise AssistantError("请先暂停或停止，再修改控件设置。")
            self.store.write("selectors.json", value)
            self.adapter = SiteAdapter(self.session, self.log, self.check_cancel, value)
            self.log("控件设置已保存。")
        elif name == "background":
            try:
                self.session.set_background_playback(bool(value))
            except Exception:
                self.background_changed.emit(self.background_playback)
                raise
            self.background_playback = bool(value)
            self.store.write("preferences.json", {"background_playback": self.background_playback})
            self.background_changed.emit(self.background_playback)
            self.log("绕过后台暂停检测已开启，允许最小化或切换标签页后继续播放。" if value else "绕过后台暂停检测已关闭，需保持播放窗口可见。")
            # 模式切换只影响窗口等待，不恢复用户暂停或停止的任务。
            if self.state in ("PLAYING", "WAIT_VISIBLE", "WAIT_CONFIRM"):
                self.next_tick = time.monotonic()
        elif name == "diagnose":
            self.ensure_browser()
            self.diagnostic.emit(self.adapter.diagnostics())
        elif name == "quit":
            self.safe_pause()
            self.save()
            self.quit_requested = True

    def run(self):
        self.session = BrowserSession(self.root, self.log, self.cancelled)
        self.session.set_background_playback(self.background_playback)
        self.adapter = SiteAdapter(self.session, self.log, self.check_cancel, self.store.read("selectors.json", {}))
        self.set_state("IDLE", "打开浏览器并手动登录，然后刷新课程")
        try:
            while not self.quit_requested:
                try:
                    command = self.commands.get_nowait()
                except queue.Empty:
                    command = None
                try:
                    if command:
                        if command[1] < self.discard_before:
                            continue
                        self.active_sequence = command[1]
                        # 急停在工作边界处理；不要清掉仍排队的急停信号。
                        self.cancelled.clear()
                        self.handle(command[2], command[3])
                    elif self.state in ("PLAYING", "WAIT_VISIBLE", "WAIT_CONFIRM") and time.monotonic() >= self.next_tick:
                        self.tick()
                    elif self.session.page and not self.session.page.is_closed() and not self.session.dialog:
                        self.session.page.wait_for_timeout(80)
                    else:
                        time.sleep(0.08)
                except Cancelled:
                    self.log("当前操作已中断，正在处理暂停或停止命令。")
                except Exception as exc:
                    self.error(exc)
        finally:
            self.session.close()
