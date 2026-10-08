from types import SimpleNamespace
from dataclasses import replace
from xuetang_assistant.models import Course, Snapshot, VideoTask, Watchdog
from xuetang_assistant.worker import Worker


class FakeAdapter:
    s = {"video": "video"}
    def __init__(self):
        self.snapshot_value = Snapshot(True, False, False, 5, 100, True, 2, 20, False)
        self.prepares = 0
        self.opens = []
        self.pauses = 0
        self.finishes = 0
    def snapshot(self):
        return self.snapshot_value
    def visible_matches(self, selector):
        return [1]
    def pause(self):
        self.pauses += 1
    def finish_playback(self):
        self.finishes += 1
    def prepare_and_play(self):
        self.prepares += 1
        return 2
    def open_task(self, task):
        self.opens.append(task.title)
    def assert_no_intervention(self):
        pass


def worker(tmp_path):
    value = Worker(tmp_path)
    value.session = SimpleNamespace(page=SimpleNamespace(is_closed=lambda: False), dialog=False)
    value.adapter = FakeAdapter()
    value.tasks = [VideoTask("k", "课程", "https://dcc.yuketang.cn/ai-workspace/lms-graph/101", "视频", 0)]
    value.index = 0
    value.watchdog = Watchdog(0, 5)
    return value


def test_manual_pause_never_auto_resumes(tmp_path):
    value = worker(tmp_path)
    value.handle("pause", None)
    assert value.state == "USER_PAUSED"
    assert value.adapter.prepares == 0
    value.handle("resume", None)
    assert value.state == "PLAYING"
    assert value.adapter.prepares == 1
    assert value.adapter.opens == ["视频"]


def test_background_change_persists_without_resuming_user_pause(tmp_path):
    value = worker(tmp_path)
    applied = []
    value.session.set_background_playback = applied.append
    value.handle('pause', None)
    value.handle('background', True)
    assert value.state == 'USER_PAUSED'
    assert value.adapter.prepares == 0
    assert applied == [True]
    assert Worker(tmp_path).background_playback
    value.handle('stop', None)
    value.handle('background', False)
    assert value.state == 'STOPPED'
    assert not Worker(tmp_path).background_playback


def test_background_failure_retains_previous_preference(tmp_path):
    import pytest
    from xuetang_assistant.models import AssistantError
    value = worker(tmp_path)
    def fail(enabled):
        raise AssistantError('不支持')
    value.session.set_background_playback = fail
    changed = []
    value.background_changed.connect(changed.append)
    with pytest.raises(AssistantError):
        value.handle('background', True)
    assert changed == [False]
    assert not value.background_playback
    assert not value.store.read('preferences.json', {}).get('background_playback', False)


def test_hidden_notification_is_sent_once_and_restore_continues(tmp_path):
    value = worker(tmp_path)
    notices = []
    value.notify.connect(lambda *args: notices.append(args))
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, visible=False, paused=True)
    value.tick()
    value.tick()
    assert value.state == "WAIT_VISIBLE"
    assert len(notices) == 1
    assert value.adapter.prepares == 0
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, visible=True, paused=False)
    value.tick()
    assert value.state == "PLAYING"
    assert value.adapter.prepares == 1


def test_hidden_restore_keeps_one_refresh_state(tmp_path):
    value = worker(tmp_path)
    value.watchdog.reloaded = True
    import time
    value.watchdog.ended_since = time.monotonic()
    value.prepared = True
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, visible=False, paused=True)
    value.tick()
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, visible=True, paused=True)
    value.tick()
    assert value.watchdog.reloaded
    assert value.adapter.prepares == 0
    assert value.state == "WAIT_CONFIRM"


def test_completed_video_advances_without_pause_and_final_queue_unloads(tmp_path):
    value = worker(tmp_path)
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, completed=True, progress=100)
    value.tick()
    assert value.tasks[0].status == "已完成"
    assert value.adapter.pauses == 0
    assert value.index == 1
    value.tick()
    assert value.state == "COMPLETED"
    assert value.adapter.finishes == 1


def test_completion_opens_next_video_even_when_pause_control_would_fail(tmp_path):
    from xuetang_assistant.models import AssistantError
    value = worker(tmp_path)
    value.tasks.append(VideoTask('k', '课程', value.tasks[0].course_url, '下一个视频', 1))
    def broken_pause():
        raise AssistantError('暂停控件未生效')
    value.adapter.pause = broken_pause
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, completed=True, progress=100)
    value.tick()
    assert value.tasks[0].status == '已完成'
    value.adapter.snapshot_value = replace(value.adapter.snapshot_value, completed=False, progress=20)
    value.tick()
    assert value.state == 'PLAYING'
    assert value.index == 1
    assert value.adapter.opens == ['下一个视频']
    assert value.adapter.prepares == 1


def test_stop_retains_queue(tmp_path):
    value = worker(tmp_path)
    value.handle("stop", None)
    assert value.state == "STOPPED"
    assert len(value.store.read("session.json", {})["queue"]) == 1


def test_urgent_stop_is_processed_before_pending_start(tmp_path):
    value = worker(tmp_path)
    value.submit("start", [])
    value.submit("pause")
    value.submit("stop")
    assert value.commands.get_nowait()[2] == "stop"
    assert value.commands.get_nowait()[2] == "pause"
    assert value.commands.get_nowait()[2] == "start"
    assert value.discard_before == 2
    from xuetang_assistant.models import Cancelled
    import pytest
    value.cancelled.clear()
    value.active_sequence = 0
    with pytest.raises(Cancelled):
        value.check_cancel()


def test_actual_fixture_queue_completes_across_courses(tmp_path, fixture_site):
    from xuetang_assistant.models import COURSE_LIST
    import time
    adapter, session, logs = fixture_site
    adapter.goto(COURSE_LIST)
    courses = adapter.read_courses()
    value = Worker(tmp_path)
    value.session = session
    value.adapter = adapter
    value.courses = courses
    value.start_tasks(courses)
    deadline = time.monotonic() + 25
    while value.state != "COMPLETED" and time.monotonic() < deadline:
        value.tick()
        if value.state != "COMPLETED":
            session.page.wait_for_timeout(100)
    assert value.state == "COMPLETED"
    assert [task.course_title for task in value.tasks] == ["课程甲", "课程甲", "课程乙", "课程乙"]
    assert all(task.status == "已完成" for task in value.tasks)
    assert all(task.progress == 100 for task in value.tasks)
    assert value.store.read("session.json", {})["state"] == "COMPLETED"
    assert session.page.url == 'about:blank'
