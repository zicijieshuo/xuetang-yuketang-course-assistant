from pathlib import Path
from types import SimpleNamespace
import io
import wave
import pytest
from playwright.sync_api import sync_playwright
from xuetang_assistant.site import SiteAdapter


@pytest.fixture(scope="session")
def browser():
    # 测试使用开发环境浏览器；不创建真实账户或读取用户配置。
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        yield browser
        browser.close()


@pytest.fixture
def fixture_site(browser):
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    page = context.new_page()
    html = (Path(__file__).parent / "fixtures/site.html").read_text(encoding="utf-8")
    buffer = io.BytesIO()
    with wave.open(buffer, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(8000)
        wav.writeframes(b"\0\0" * 8000 * 4)
    def fulfill(route):
        if "/fixtures/tone.wav" in route.request.url:
            route.fulfill(status=200, content_type="audio/wav", body=buffer.getvalue())
        else:
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
    context.route("**/*", fulfill)
    session = SimpleNamespace(page=page, dialog=False, minimized=lambda: False)
    logs = []
    adapter = SiteAdapter(session, logs.append, lambda: None)
    yield adapter, session, logs
    context.close()
