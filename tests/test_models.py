from dataclasses import replace
import pytest
from xuetang_assistant.models import Action, Snapshot, Watchdog, allowed_url, is_video_url, progress_value


@pytest.mark.parametrize("text,value,done", [
    ("已完成", 100, True), ("完成度：100%", 100, True), ("完成度：99.9%", 99.9, False),
    ("未完成", None, False), ("已完成 3/10", None, False), ("作业已完成", None, False),
    ("完成度：101%", None, False), ("完成度：0%", 0, False),
])
def test_progress_is_current_video_only(text, value, done):
    assert progress_value(text) == (value, done)


@pytest.mark.parametrize("url,expected", [
    ("https://dcc.yuketang.cn/pro/courselist", True),
    ("https://dcc.yuketang.cn.evil.com/pro/courselist", False),
    ("http://dcc.yuketang.cn/pro/courselist", False),
    ("https://user:secret@dcc.yuketang.cn/pro/courselist", False),
    ("https://dcc.yuketang.cn:8443/pro/courselist", False),
])
def test_host_restriction(url, expected):
    assert allowed_url(url) == expected


def snapshot(**kwargs):
    value = Snapshot(True, False, False, 10, 100, True, 2, 25, False)
    return replace(value, **kwargs)


def test_ended_waits_for_platform_refreshes_once_and_stops():
    watch = Watchdog(0, 10)
    ended = snapshot(ended=True, paused=True)
    assert watch.decide(ended, 0)[0] == Action.WAIT
    assert watch.decide(ended, 59)[0] == Action.WAIT
    assert watch.decide(ended, 60)[0] == Action.RELOAD
    assert watch.decide(snapshot(paused=True, current_time=0), 64)[0] == Action.WAIT
    assert watch.decide(snapshot(paused=True, current_time=0), 75)[0] == Action.FAIL


def test_platform_confirmation_can_precede_end():
    assert Watchdog(0).decide(snapshot(completed=True), 1)[0] == Action.COMPLETE


def test_hidden_time_does_not_trigger_stall():
    watch = Watchdog(0, 10)
    assert watch.decide(snapshot(visible=False, paused=True), 100)[0] == Action.WAIT_VISIBLE
    assert watch.decide(snapshot(visible=False, paused=True), 500)[0] == Action.WAIT_VISIBLE
    assert watch.decide(snapshot(paused=True), 503)[0] == Action.RECOVER
    assert watch.decide(snapshot(paused=True), 506)[0] == Action.PLAYING
    assert watch.decide(snapshot(paused=True), 564)[0] == Action.FAIL


def test_stall_and_lost_mute():
    assert Watchdog(0, 10).decide(snapshot(), 60)[0] == Action.FAIL
    assert Watchdog(0, 10).decide(snapshot(muted=False), 1)[0] == Action.FAIL


def test_motion_resets_stall_timer():
    watch = Watchdog(0, 10)
    assert watch.decide(snapshot(current_time=15), 59)[0] == Action.PLAYING
    assert watch.decide(snapshot(current_time=15), 60)[0] == Action.PLAYING
