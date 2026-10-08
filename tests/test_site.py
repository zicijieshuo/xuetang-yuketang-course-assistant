import pytest
from xuetang_assistant.models import AssistantError, Course, COURSE_LIST


@pytest.mark.parametrize('background', [False, True])
def test_overview_filters_and_follows_video_popup_as_only_tab(fixture_site, background):
    adapter, session, logs = fixture_site
    session.background_playback = background
    course = Course('overview', '课程甲', url='https://dcc.yuketang.cn/pro/lms/demo/101/studycontent')
    source = session.page
    tasks = adapter.scan(course)
    assert [(t.title, t.ordinal) for t in tasks] == [('第一节', 0), ('第二节', 4)]
    adapter.open_task(tasks[0])
    assert source.is_closed() is (not background)
    if background:
        assert session.page is source
        assert session.page.evaluate("!window.__xuetang_assistant_video_entry__")
    assert len(session.page.context.pages) == 1
    assert '/video/1' in tasks[0].video_url
    assert adapter.prepare_and_play() == 2
    adapter.pause()
    adapter.open_task(tasks[1])
    assert len(session.page.context.pages) == 1
    assert '/video/5' in tasks[1].video_url


def test_multiple_courses_iframe_mixed_nodes_and_order(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto(COURSE_LIST)
    courses = adapter.read_courses()
    assert [c.title for c in courses] == ["课程甲", "课程乙"]
    first = adapter.scan(courses[0])
    second = adapter.scan(courses[1])
    assert [task.title for task in first + second] == ["第一节", "第二节", "第一节", "第二节"]
    assert len({task.course_key for task in first + second}) == 2
    assert any("作业" in text for text in logs)
    assert any("已完成" in text for text in logs)
    assert all("exercise" not in task.video_url for task in first + second)


def test_mute_speed_actual_playback_and_platform_completion(fixture_site):
    adapter, session, logs = fixture_site
    course = Course("manual", "课程甲", url="https://dcc.yuketang.cn/ai-workspace/lms-graph/101")
    tasks = adapter.scan(course)
    adapter.open_task(tasks[0])
    assert adapter.read_progress() == (20, False)  # 目录中其他已完成视频不能影响当前视频。
    rate = adapter.prepare_and_play()
    assert rate == 2
    actual = adapter.snapshot()
    assert actual.muted and actual.rate == 2
    assert adapter.wait(lambda: adapter.snapshot().current_time > 0, 4)
    assert adapter.wait(lambda: adapter.read_progress()[1], 6)
    assert adapter.snapshot().completed
    adapter.pause()
    assert adapter.snapshot().paused
    adapter.open_task(tasks[1])
    assert "/video/5" in tasks[1].video_url
    assert not adapter.snapshot().completed


def test_speed_fallback(fixture_site):
    adapter, session, logs = fixture_site
    course = Course("manual", "课程甲", url="https://dcc.yuketang.cn/ai-workspace/lms-graph/101?rates=fallback")
    task = adapter.scan(course)[0]
    adapter.open_task(task)
    assert adapter.prepare_and_play() == 1.5
    assert adapter.snapshot().muted
    assert any("1.5" in text for text in logs)


@pytest.mark.parametrize('background', [False, True])
def test_new_video_desynced_mute_icon_recovers_and_sets_two_times(fixture_site, background):
    adapter, session, logs = fixture_site
    session.background_playback = background
    course = Course('mute', '课程甲', url='https://dcc.yuketang.cn/ai-workspace/lms-graph/101?mute=desync&resetonplay=1')
    tasks = adapter.scan(course)
    for task in tasks:
        adapter.open_task(task)
        assert not adapter.snapshot().muted
        assert adapter.prepare_and_play() == 2
        actual = adapter.snapshot()
        assert actual.muted and actual.rate == 2
        assert any('重新同步' in text for text in logs)
        adapter.pause()


def test_speed_requires_mouse_movement_after_option_entry(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto('https://dcc.yuketang.cn/ai-workspace/lms-graph/101/video/1?mousegate=1')
    assert adapter.prepare_and_play() == 2
    assert adapter.snapshot().rate == 2
    adapter.pause()


def test_chapter_directory_expands_without_duplicate_toggle(fixture_site):
    adapter, session, logs = fixture_site
    course = Course("manual", "章节课程", url="https://dcc.yuketang.cn/ai-workspace/lms-graph/101?directory=chapter")
    tasks = adapter.scan(course)
    assert [task.title for task in tasks] == ["第一节", "第二节"]
    adapter.open_task(tasks[0])
    assert adapter.read_progress() == (20, False)


def test_two_times_failure_uses_next_highest_available(fixture_site):
    adapter, session, logs = fixture_site
    course = Course("manual", "课程甲", url="https://dcc.yuketang.cn/ai-workspace/lms-graph/101?rate2=broken")
    task = adapter.scan(course)[0]
    adapter.open_task(task)
    assert adapter.prepare_and_play() == 1.5
    assert adapter.snapshot().muted



def test_mute_failure_stops_before_play(fixture_site):
    adapter, session, logs = fixture_site
    course = Course("manual", "课程甲", url="https://dcc.yuketang.cn/ai-workspace/lms-graph/101?mute=broken")
    task = adapter.scan(course)[0]
    adapter.open_task(task)
    with pytest.raises(AssistantError, match="静音"):
        adapter.prepare_and_play()
    assert adapter.snapshot().paused
    assert not adapter.snapshot().muted


def test_ambiguous_progress_stops(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto("https://dcc.yuketang.cn/ai-workspace/lms-graph/101/video/1")
    # 模拟平台显示两个当前视频状态，确保程序不猜。
    session.page.locator(".rate-detail").evaluate("n=>n.appendChild(n.firstElementChild.cloneNode(true))")
    with pytest.raises(AssistantError, match="唯一"):
        adapter.read_progress()


def test_minimized_window_is_waited_without_play(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto("https://dcc.yuketang.cn/ai-workspace/lms-graph/101/video/1")
    session.minimized = lambda: True
    assert not adapter.snapshot().visible
    with pytest.raises(AssistantError, match="恢复"):
        adapter.prepare_and_play()
    assert adapter.snapshot().paused


def test_background_option_allows_minimized_play_and_disable_restores_wait(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto('https://dcc.yuketang.cn/ai-workspace/lms-graph/101/video/1')
    session.minimized = lambda: True
    session.background_playback = True
    assert adapter.prepare_and_play() == 2
    assert adapter.snapshot().visible
    adapter.pause()
    session.background_playback = False
    assert not adapter.snapshot().visible


def test_login_or_dialog_stops(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto(COURSE_LIST)
    session.page.locator("#app").evaluate("n=>n.innerHTML='<input type=password>'")
    with pytest.raises(AssistantError, match="登录"):
        adapter.read_courses()
    session.page.locator("input").evaluate("n=>n.remove()")
    session.dialog = True
    with pytest.raises(AssistantError, match="弹窗"):
        adapter.assert_no_intervention()


def test_preloaded_transparent_offscreen_captcha_is_not_intervention(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto(COURSE_LIST)
    session.page.locator("#app").evaluate("""n => {
        const holder = document.createElement('div');
        holder.style.cssText = 'position:absolute;top:-1000000px;opacity:0';
        holder.innerHTML = '<iframe src="https://dcc.yuketang.cn/captcha"></iframe>';
        n.appendChild(holder);
    }""")
    assert len(adapter.read_courses()) == 2
    session.page.locator('iframe[src*="captcha"]').evaluate("n => n.parentElement.style.cssText='position:fixed;top:0;opacity:0'")
    adapter.assert_no_intervention()
    session.page.locator('iframe[src*="captcha"]').evaluate("n => n.parentElement.style.opacity='1'")
    with pytest.raises(AssistantError, match="验证码"):
        adapter.assert_no_intervention()


def test_navigation_context_loss_is_retried(fixture_site):
    from playwright.sync_api import Error
    adapter, session, logs = fixture_site
    attempts = []
    def reading():
        attempts.append(1)
        if len(attempts) == 1:
            raise Error('Execution context was destroyed, most likely because of a navigation')
        return True
    assert adapter.wait(reading, 1)
    assert len(attempts) == 2
    with pytest.raises(Error, match="Unknown error"):
        adapter.wait(lambda: (_ for _ in ()).throw(Error('Unknown error')), 1)


def test_diagnostics_do_not_include_body_or_credentials(fixture_site):
    adapter, session, logs = fixture_site
    adapter.goto(COURSE_LIST)
    value = adapter.diagnostics()
    assert value["frames"][0]["controls"]["course_card"] == 2
    assert "课程甲" not in str(value)
    assert set(value["frames"][0]) == {"host", "controls"}
