import os
import threading
import time
import io
import wave
from pathlib import Path
import pytest
from xuetang_assistant.browser import BrowserSession, installed_edge
from xuetang_assistant.site import SiteAdapter
from xuetang_assistant.models import Course


@pytest.mark.skipif(os.name != 'nt' or not installed_edge(), reason='需要本机 Windows Edge')
def test_background_minimized_media_advances_and_new_page_inherits_mode(tmp_path, monkeypatch):
    monkeypatch.setenv('PLAYWRIGHT_BROWSERS_PATH', 'test-placeholder')
    session = BrowserSession(tmp_path / '后台模式测试', lambda text: None, threading.Event())
    session.set_background_playback(True)
    try:
        session.open('msedge')
        html = (Path(__file__).parent / 'fixtures/site.html').read_text(encoding='utf-8')
        buffer = io.BytesIO()
        with wave.open(buffer, 'wb') as wav:
            wav.setnchannels(1); wav.setsampwidth(2); wav.setframerate(8000)
            wav.writeframes(b'\0\0' * 8000 * 12)
        def route_local(route):
            if '/fixtures/tone.wav' in route.request.url:
                route.fulfill(content_type='audio/wav', body=buffer.getvalue())
            else:
                route.fulfill(content_type='text/html; charset=utf-8', body=html)
        session.context.route('**/*', route_local)
        logs = []
        adapter = SiteAdapter(session, logs.append, lambda: None)
        tasks = adapter.scan(Course('background', '课程甲', url='https://dcc.yuketang.cn/pro/lms/demo/101/studycontent'))
        original_page = session.page
        popups = []
        original_page.on('popup', lambda page: popups.append(page))
        info = session.cdp.send('Browser.getWindowForTarget')
        session.cdp.send('Browser.setWindowBounds', {'windowId': info['windowId'], 'bounds': {'windowState': 'minimized'}})
        assert adapter.wait(session.minimized, 3)
        adapter.open_task(tasks[0])
        assert session.page is original_page and session.minimized()
        assert not popups
        assert len(session.context.pages) == 1
        # 模拟站点在可见性变化时主动暂停，验证浏览器层模拟实际影响监听器。
        session.page.add_script_tag(content="document.addEventListener('visibilitychange',()=>{if(document.hidden)document.querySelector('video').pause()})")
        info = session.cdp.send('Browser.getWindowForTarget')
        session.cdp.send('Browser.setWindowBounds', {'windowId': info['windowId'], 'bounds': {'windowState': 'minimized'}})
        assert adapter.wait(session.minimized, 3)
        assert adapter.prepare_and_play() == 2, logs
        assert adapter.wait(lambda: adapter.snapshot().current_time > 0.5, 5)
        assert session.minimized()
        first = adapter.snapshot().current_time
        assert adapter.wait(lambda: adapter.snapshot().current_time > first + 0.5, 5)
        adapter.open_task(tasks[1])
        assert session.page is original_page and session.minimized()
        assert not popups and len(session.context.pages) == 1
        assert adapter.prepare_and_play() == 2
        assert adapter.wait(lambda: adapter.snapshot().current_time > 0.5, 5)
        adapter.pause()
        session.set_background_playback(False)
        assert adapter.wait(lambda: session.page.evaluate('document.hidden'), 3)
        assert not adapter.visible()
        session.set_background_playback(True)
        assert adapter.prepare_and_play() == 2
        other = session.context.new_page()
        other.goto('https://dcc.yuketang.cn/ai-workspace/lms-graph/101/video/5')
        other.bring_to_front()  # 仅测试切换标签页，产品不主动抢占窗口。
        session.page.wait_for_timeout(700)  # 等待原生窗口恢复动画，避免随后最小化被恢复覆盖。
        assert adapter.snapshot().visible
        first = adapter.snapshot().current_time
        assert adapter.wait(lambda: adapter.snapshot().current_time > first + 0.5, 5)
        adapter.pause()
        session.use_page(other)
        assert session.page.evaluate('document.visibilityState') == 'visible'
        session.set_background_playback(False)
        assert not session.background_playback
        observer = session.context.new_page()
        observer.set_content('<p>验证关闭后台模式后，切换标签页恢复 hidden</p>')
        observer.bring_to_front()
        assert adapter.wait(lambda: session.page.evaluate('document.hidden'), 3)
        assert not adapter.visible()
    finally:
        session.close()


@pytest.mark.skipif(os.name != "nt" or not installed_edge(), reason="需要本机已安装的 Windows Edge")
def test_dedicated_browser_real_visibility_and_profile(tmp_path, monkeypatch):
    # 测试仅打开独立临时配置的空白窗口，不连接学堂云或使用用户登录会话。
    monkeypatch.setenv("PLAYWRIGHT_BROWSERS_PATH", "test-placeholder")
    session = BrowserSession(tmp_path / "中文浏览器配置", lambda text: None, threading.Event())
    try:
        session.open("msedge")
        session.page.set_content('<meta charset="utf-8"><h1>本地浏览器可见性测试</h1>')
        assert session.page.locator("h1").inner_text() == "本地浏览器可见性测试"
        assert (session.root / "profile-msedge").exists()
        assert not session.minimized()
        info = session.cdp.send("Browser.getWindowForTarget")
        session.cdp.send("Browser.setWindowBounds", {"windowId": info["windowId"], "bounds": {"windowState": "minimized"}})
        session.page.wait_for_timeout(300)
        assert session.minimized()
        assert session.page.evaluate("document.visibilityState") == "hidden"
        session.page.bring_to_front()  # 仅测试模拟用户恢复窗口，不用于产品。
        deadline = time.monotonic() + 3
        while session.minimized() and time.monotonic() < deadline:
            session.page.wait_for_timeout(100)
        assert not session.minimized()
        other = session.context.new_page()
        other.set_content("<p>独立测试窗口中的另一个标签页</p>")
        other.bring_to_front()
        session.page.wait_for_timeout(300)
        assert session.page.evaluate("document.visibilityState") == "hidden"
        other.close()
    finally:
        session.close()
