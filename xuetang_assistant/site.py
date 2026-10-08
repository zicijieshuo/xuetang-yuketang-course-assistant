"""只读 DOM 识别与正常控件点击；所有站点选择器集中于此。"""
import hashlib
import math
import re
import time
from urllib.parse import urlparse
from playwright.sync_api import Error as PlaywrightError, TimeoutError as PlaywrightTimeout
from .models import AssistantError, Cancelled, Course, Snapshot, VideoTask, allowed_url, is_video_url, progress_value

# 来源见 docs/架构说明.md；覆盖文件仅替换 CSS，不接受任意 JavaScript。
SELECTORS = {
    "course_card": ".lesson-cardS",
    "course_title": "h1",
    "course_subtitle": ".className",
    "overview_leaf": ".chapter-list .leaf-detail",
    "overview_title": ".leaf-title .title",
    "overview_video": ".leaf-title .icon--shipin",
    "overview_progress": ".progress-wrap .item",
    "overview_locked": ".icon--suo",
    "leaf": ".leaf-item",
    "leaf_type": ".leaf-item-tag",
    "leaf_title": ".leaf-item-title",
    "leaf_complete": ".leaf-item-status .icon-yuanquangou, .leaf-item-status .icon-yuanquangou-mianzhuang",
    "leaf_locked": ".icon-suoding",
    "expand": ".nav-item-node-inner > .expand-icon:not(.is-expanded), .learning-space-student-chapter-nav .nav-item-title:not(.is-expand):has(> .expand-icon)",
    "progress": ".learning-space-video .rate-detail .text",
    "video": ".learning-center-player video, .xt_video_player_container video",
    "player": ".xt_video_player_container",
    "play": ".xt_video_player_play_btn",
    "mute": ".xt_video_player_volume .xt_video_player_common_icon",
    "speed_button": ".xt_video_player_speed",
    "speed_option": ".xt_video_player_speed li[data-speed]",
}


class SiteAdapter:
    def __init__(self, session, log, check_cancel, overrides: dict | None = None):
        self.session, self.log, self.check_cancel = session, log, check_cancel
        self.s = dict(SELECTORS)
        self.s.update({key: value for key, value in (overrides or {}).items()
                       if key in SELECTORS and isinstance(value, str) and value.strip()})

    @property
    def page(self):
        page = self.session.page
        if page is None or page.is_closed():
            raise AssistantError("播放浏览器已关闭，请点击打开浏览器。")
        return page

    def wait(self, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            self.check_cancel()
            try:
                if predicate():
                    return True
            except PlaywrightError as exc:
                # 正常导航会销毁旧 document / iframe，重新读取新页面即可。
                if not any(marker in str(exc) for marker in (
                    "Execution context was destroyed", "Frame was detached", "frame was detached",
                    "Cannot find context with specified id", "Inspected target navigated",
                )):
                    raise
            self.page.wait_for_timeout(150)
        return False

    def goto(self, url: str):
        if not allowed_url(url):
            raise AssistantError("仅支持 https://dcc.yuketang.cn 的课程或视频链接。")
        for attempt in range(3):
            self.check_cancel()
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=15000)
                self.assert_no_intervention()
                return
            except PlaywrightTimeout:
                if attempt == 2:
                    raise AssistantError("页面加载失败，已重试两次，请检查网络后继续。") from None
                self.log(f"页面加载超时，重试 {attempt + 1}/2。")

    def frames(self):
        return [frame for frame in self.page.frames if allowed_url(frame.url)]

    def assert_no_intervention(self):
        if not self.wait(self._check_intervention, 5):
            raise AssistantError("页面仍在切换，请等待加载完成后刷新课程或继续。")

    def _check_intervention(self):
        if self.session.dialog:
            raise AssistantError("网页弹窗等待处理；请在浏览器中处理后点击继续。")
        if not allowed_url(self.page.url) or re.search(r"/(?:login|signin|passport)(?:/|$)", urlparse(self.page.url).path, re.I):
            raise AssistantError("请在专用浏览器中手动完成登录，再点击刷新课程或继续。")
        for frame in self.frames():
            # 检查实际可见输入框/验证组件，避免正文中出现“登录”误报。
            candidates = frame.locator('input[type="password"], iframe[src*="captcha"], .geetest_panel, .tcaptcha-transform')
            if any(self.intervention_present(candidates.nth(i)) for i in range(candidates.count())):
                raise AssistantError("检测到登录或验证码，请手动处理后继续。")
        return True

    @staticmethod
    def intervention_present(candidate):
        if not candidate.is_visible():
            return False
        # Playwright is_visible 不考虑透明度和视口；平台常驻验证码在 y=-1000000。
        return candidate.evaluate("""node => {
            const r = node.getBoundingClientRect();
            if (r.width <= 0 || r.height <= 0 || r.bottom <= 0 || r.right <= 0 ||
                r.top >= innerHeight || r.left >= innerWidth) return false;
            for (let e = node; e; e = e.parentElement) {
                const s = getComputedStyle(e);
                if (Number(s.opacity) === 0 || s.visibility !== 'visible' || s.display === 'none') return false;
            }
            return true;
        }""")

    def visible_matches(self, selector: str):
        matches = []
        for frame in self.frames():
            locator = frame.locator(selector)
            for i in range(locator.count()):
                candidate = locator.nth(i)
                if candidate.is_visible():
                    matches.append((frame, candidate))
        return matches

    def read_courses(self) -> list[Course]:
        self.assert_no_intervention()
        cards = []
        self.wait(lambda: bool(self.visible_matches(self.s["course_card"])), 12)
        cards = self.visible_matches(self.s["course_card"])
        if not cards:
            # 使用公开前端中已确认的“我听的课”导航文字，不点击其他按钮。
            tabs = self.visible_matches(".nav__item")
            student_tabs = [loc for _, loc in tabs if loc.inner_text().strip() == "我听的课"]
            if len(student_tabs) == 1:
                self.click_element(student_tabs[0])
                self.wait(lambda: bool(self.visible_matches(self.s["course_card"])), 12)
                cards = self.visible_matches(self.s["course_card"])
        self.assert_no_intervention()
        if not cards:
            raise AssistantError("没有识别到课程卡片。请确认已登录并打开‘我听的课’，或使用添加链接 / 控件设置。")
        courses = []
        occurrences = {}
        for ordinal, (_, card) in enumerate(cards):
            self.check_cancel()
            title = card.locator(self.s["course_title"])
            subtitle = card.locator(self.s["course_subtitle"])
            if title.count() != 1:
                raise AssistantError("课程标题结构不明确，请在控件设置中校准课程标题。")
            name = title.inner_text().strip()
            group = subtitle.first.inner_text().strip() if subtitle.count() else ""
            identity = name + "\n" + group
            occurrence = occurrences.get(identity, 0)
            occurrences[identity] = occurrence + 1
            key = hashlib.sha256(f"{identity}\n{occurrence}".encode("utf-8")).hexdigest()[:16]
            courses.append(Course(key, name, group, ordinal=ordinal))
        return courses

    def open_course(self, course: Course):
        from .models import COURSE_LIST
        if course.url:
            self.goto(course.url)
        else:
            self.goto(COURSE_LIST)
            current = self.read_courses()
            found = [i for i, c in enumerate(current) if c.key == course.key]
            if len(found) != 1:
                raise AssistantError("课程列表已变化，请刷新课程后重新勾选。")
            cards = self.visible_matches(self.s["course_card"])
            self.click_element(cards[found[0]][1])
        def has_directory():
            self.assert_no_intervention()
            return bool(self.visible_matches(self.s["overview_leaf"]) or self.visible_matches(self.s["leaf"]) or self.visible_matches(self.s["expand"]))
        if not self.wait(has_directory, 20):
            raise AssistantError("没有识别到视频目录。请在浏览器进入课程学习空间，或校准目录控件。")
        # 保存正常导航产生的 URL；不读取 Vue 内部对象或生成进度请求。
        return self.page.url

    def expand_directory(self):
        for _ in range(150):
            self.check_cancel()
            expanders = self.visible_matches(self.s["expand"])
            if not expanders:
                return
            self.click_element(expanders[0][1])
            self.page.wait_for_timeout(120)
        raise AssistantError("目录展开数量超出限制或控件状态不明确，请检查目录。")

    def scan(self, course: Course) -> list[VideoTask]:
        course_url = self.open_course(course)
        if self.visible_matches(self.s["overview_leaf"]):
            tasks = []
            for ordinal, (_, leaf) in enumerate(self.visible_matches(self.s["overview_leaf"])):
                self.check_cancel()
                title = leaf.locator(self.s["overview_title"])
                if not leaf.locator(self.s["overview_video"]).count() or title.count() != 1:
                    self.log("跳过总览中的非视频或类型不明确节点。")
                    continue
                if leaf.locator(self.s["overview_locked"]).count():
                    self.log("跳过总览中未开放的视频。")
                    continue
                status = leaf.locator(self.s["overview_progress"])
                text = status.inner_text().strip() if status.count() == 1 else ""
                value, completed = progress_value(text)
                if completed:
                    self.log("跳过总览中已完成的视频。")
                    continue
                if value is None and text not in ("未开始", "进行中", "学习中", "未完成"):
                    self.log("跳过总览中状态不明确的视频。")
                    continue
                tasks.append(VideoTask(course.key, course.title, course_url, title.inner_text().strip(), ordinal))
            return tasks
        self.expand_directory()
        tasks = []
        for ordinal, (_, leaf) in enumerate(self.visible_matches(self.s["leaf"])):
            self.check_cancel()
            tag = leaf.locator(self.s["leaf_type"])
            title = leaf.locator(self.s["leaf_title"])
            if tag.count() < 1 or title.count() != 1:
                self.log("跳过类型或标题无法识别的目录节点。")
                continue
            kind = tag.first.inner_text().strip()
            # 片段在公开前端定义为视频类型，其他节点不访问。
            if kind not in ("视频", "片段", "Video"):
                self.log(f"跳过非视频节点：{kind[:30] or '未知类型'}。")
                continue
            name = title.inner_text().strip()
            if leaf.locator(self.s["leaf_locked"]).count():
                self.log(f"跳过未开放的视频：{name}。")
                continue
            if leaf.locator(self.s["leaf_complete"]).count():
                self.log(f"跳过已完成的视频：{name}。")
                continue
            tasks.append(VideoTask(course.key, course.title, course_url, name, ordinal))
        return tasks

    def open_task(self, task: VideoTask):
        self.goto(task.video_url or task.course_url)
        if not task.video_url:
            self.wait(lambda: bool(self.visible_matches(self.s["overview_leaf"]) or self.visible_matches(self.s["leaf"]) or self.visible_matches(self.s["expand"])), 15)
            self.expand_directory()
            overview = bool(self.visible_matches(self.s["overview_leaf"]))
            leaves = self.visible_matches(self.s["overview_leaf"] if overview else self.s["leaf"])
            if task.ordinal >= len(leaves):
                raise AssistantError("课程目录已变化，请停止并重新开始以刷新队列。")
            leaf = leaves[task.ordinal][1]
            title = leaf.locator(self.s["overview_title"] if overview else self.s["leaf_title"])
            tag = leaf.locator(self.s["overview_video"] if overview else self.s["leaf_type"])
            is_video = bool(tag.count()) if overview else bool(tag.count() and tag.first.inner_text().strip() in ("视频", "片段", "Video"))
            if title.count() != 1 or title.inner_text().strip() != task.title or not is_video:
                raise AssistantError("视频节点与扫描结果不一致，请重新开始刷新队列。")
            self.click_video_entry(leaf)
        def ready():
            self.assert_no_intervention()
            return bool(self.visible_matches(self.s["progress"]) and self.visible_matches(self.s["video"]))
        if not self.wait(ready, 20):
            raise AssistantError("视频或专属完成度控件无法识别，请检查页面并校准控件。")
        video_routes = [f.url for f in self.frames() if is_video_url(f.url)]
        if not video_routes:
            raise AssistantError("当前页面未进入已确认的视频路由，程序已暂停。")
        # iframe 内视频路由可直接打开，沿用平台正常导航的查询参数。
        task.video_url = video_routes[0]

    def click_video_entry(self, leaf):
        source = self.page
        popups = []
        capture_key = "__xuetang_assistant_video_entry__"
        capture_frames = self.frames() if getattr(self.session, "background_playback", False) else []
        captured_urls = []
        def capture_popup(page):
            popups.append(page)
        source.on("popup", capture_popup)
        try:
            # 平台视频入口用 window.open，新标签页会使 Edge 从最小化恢复。
            # 仅本次点击期间截取本站视频路由，随后在原工作标签页正常导航。
            for frame in capture_frames:
                frame.evaluate(r"""key => {
                    const original = window.open;
                    const state = { original, urls: [] };
                    state.handler = function(url, ...args) {
                        let target;
                        try { target = new URL(String(url), document.baseURI); } catch (_) {}
                        if (target && target.origin === 'https://dcc.yuketang.cn' &&
                            !target.username && !target.password &&
                            /\/lms-graph\/\d+\/video\/\d+(?:\/|$)/.test(target.pathname)) {
                            state.urls.push(target.href);
                            return null;
                        }
                        return original.call(window, url, ...args);
                    };
                    window[key] = state;
                    window.open = state.handler;
                }""", capture_key)
            self.click_element(leaf)
            def entered():
                self.assert_no_intervention()
                for frame in capture_frames:
                    for url in frame.evaluate("key => window[key]?.urls || []", capture_key):
                        if url not in captured_urls:
                            captured_urls.append(url)
                return bool(captured_urls or popups or self.visible_matches(self.s["progress"]))
            self.wait(entered, 12)
        finally:
            source.remove_listener("popup", capture_popup)
            for frame in capture_frames:
                try:
                    frame.evaluate("""key => {
                        const state = window[key];
                        if (!state) return;
                        if (window.open === state.handler) window.open = state.original;
                        delete window[key];
                    }""", capture_key)
                except PlaywrightError:
                    pass  # 正常同页导航可能已经销毁旧 document。
        if captured_urls:
            if len(captured_urls) != 1 or not is_video_url(captured_urls[0]):
                raise AssistantError("视频入口路由不明确，程序已暂停，请检查目录。")
            self.goto(captured_urls[0])
            return
        if popups:
            target = popups[0]
            target.wait_for_load_state("domcontentloaded", timeout=15000)
            if not allowed_url(target.url):
                raise AssistantError("视频入口打开了非适配站点，请手动检查。")
            if hasattr(self.session, "use_page"):
                self.session.use_page(target)
            else:
                self.session.page = target
            # 仅关闭本次导航的旧页面，保持一个工作标签页；不调用 bring_to_front。
            source.close()

    def video(self):
        candidates = self.visible_matches(self.s["video"])
        # 同一个 video 可能匹配逗号分隔 CSS，浏览器原生去重；多个视频则不猜。
        if len(candidates) != 1:
            raise AssistantError("没有唯一的可见视频，请在控件设置中校准视频选择器。")
        return candidates[0]

    def read_progress(self):
        matches = self.visible_matches(self.s["progress"])
        if len(matches) != 1:
            raise AssistantError("没有唯一的当前视频完成度控件，请校准完成度选择器。")
        text = matches[0][1].inner_text().strip()
        value, completed = progress_value(text)
        if value is None:
            raise AssistantError("当前视频完成度无法解析，请确认页面已加载或校准控件。")
        return value, completed

    def visible(self) -> bool:
        if not getattr(self.session, "background_playback", False) and self.session.minimized():
            return False
        if self.page.evaluate("document.visibilityState !== 'visible'"):
            return False
        frame, _ = self.video()
        return frame.evaluate("document.visibilityState === 'visible'")

    def snapshot(self) -> Snapshot:
        self.assert_no_intervention()
        frame, video = self.video()
        data = video.evaluate("v => ({paused:v.paused,ended:v.ended,time:v.currentTime,duration:Number.isFinite(v.duration)?v.duration:0,muted:v.muted||v.volume===0,rate:v.playbackRate})")
        progress, completed = self.read_progress()
        return Snapshot(self.visible(), data["paused"], data["ended"], data["time"], data["duration"],
                        data["muted"], data["rate"], progress, completed)

    def hover_controls(self):
        if not self.visible():
            raise AssistantError("请恢复播放窗口后点击继续。")
        frame, video = self.video()
        player = frame.locator(self.s["player"])
        if player.count() != 1:
            raise AssistantError("播放器容器无法唯一识别，请在控件设置中校准。")
        self.hover_element(player)
        # XtPlayer 菜单利用鼠标移动打开列表，hover 会产生正常鼠标事件。
        return frame

    def hover_element(self, element):
        if getattr(self.session, "background_playback", False):
            element.dispatch_event("mouseover", {"clientX": 1, "clientY": 1})
            element.dispatch_event("mousemove", {"clientX": 2, "clientY": 1})
        else:
            element.hover()

    def click_element(self, element):
        if getattr(self.session, "background_playback", False):
            element.dispatch_event("click")
        else:
            element.click()

    def click_control(self, name: str):
        frame = self.hover_controls()
        controls = frame.locator(self.s[name])
        background = getattr(self.session, "background_playback", False)
        visible = [controls.nth(i) for i in range(controls.count()) if controls.nth(i).is_visible() or background]
        if len(visible) != 1:
            raise AssistantError(f"播放器 {name} 控件无法唯一识别，请在控件设置中校准。")
        if background:
            visible[0].dispatch_event("click")
        else:
            visible[0].click()

    def pause(self):
        _, video = self.video()
        if not video.evaluate("v => v.paused"):
            self.click_control("play")
            if not self.wait(lambda: video.evaluate("v => v.paused"), 3):
                raise AssistantError("暂停控件未生效，请手动暂停浏览器中的视频。")

    def finish_playback(self):
        """队列结束时卸载媒体页面，不依赖平台暂停按钮或任何完成请求。"""
        self.page.goto("about:blank", wait_until="commit", timeout=5000)

    def prepare_and_play(self) -> float:
        self.assert_no_intervention()
        # DOM 出现不代表 loadedmetadata / 播放器控件初始化已结束。
        if not self.wait(lambda: self.video()[1].evaluate("v => v.readyState >= 1"), 15):
            raise AssistantError("视频媒体信息尚未加载完成，请检查网络后继续。")
        self.page.wait_for_timeout(300)
        self.check_cancel()
        self.ensure_muted()
        target = self.select_speed()
        _, video = self.video()
        if video.evaluate("v => v.paused && !v.ended"):
            self.click_control("play")
        self.page.wait_for_timeout(300)
        self.check_cancel()
        # loadedmetadata / 自动播放可能在控件设置后重置状态，再次读取当前元素。
        self.ensure_muted()
        if abs(self.video()[1].evaluate("v => v.playbackRate") - target) > 0.01:
            self.log("播放启动后倍速被播放器重置，重新应用倍速。")
            target = self.select_speed()
        return target

    def media_muted(self):
        return self.video()[1].evaluate("v => v.muted || v.volume === 0")

    def ensure_muted(self):
        for attempt in range(2):
            self.check_cancel()
            if self.media_muted():
                return
            frame = self.hover_controls()
            button = frame.locator(self.s["mute"])
            if button.count() != 1:
                raise AssistantError("静音控件无法唯一识别，请校准控件。")
            # XtPlayer 图标先更新，volume 赋值可能被其 100ms 节流跳过。
            # 图标已静音而媒体未静音时，先复位开关，再等待节流间隔后静音。
            if "xt_video_player_common_icon_muted" in (button.get_attribute("class") or "").split():
                self.log("静音图标与媒体音量不同步，正在重新同步。")
                self.click_control("mute")
                self.page.wait_for_timeout(180)
                self.check_cancel()
            self.click_control("mute")
            if self.wait(self.media_muted, 2):
                self.page.wait_for_timeout(180)
                if self.media_muted():
                    return
            self.log("播放器静音状态尚未稳定，重新确认。")
        raise AssistantError("播放器实际音量仍未静音，程序已暂停，请手动设置静音。")

    def select_speed(self) -> float:
        _, video = self.video()
        frame = self.hover_controls()
        button = frame.locator(self.s["speed_button"])
        target = 1.0
        background = getattr(self.session, "background_playback", False)
        if button.count() == 1 and (button.is_visible() or background):
            self.hover_element(button)
            options = frame.locator(self.s["speed_option"])
            choices = []
            for i in range(options.count()):
                item = options.nth(i)
                raw = item.get_attribute("data-speed") or item.inner_text().strip().rstrip("xX倍")
                try:
                    rate = float(raw)
                except ValueError:
                    continue
                if math.isfinite(rate) and 0 < rate <= 16 and (item.is_visible() or getattr(self.session, "background_playback", False)):
                    choices.append((rate, item))
            if not choices:
                raise AssistantError("倍速菜单存在，但无法读取可用选项，请校准或手动处理。")
            ordered = sorted(choices, key=lambda choice: (choice[0] != 2, -choice[0]))
            success = False
            for candidate_rate, option in ordered:
                self.check_cancel()
                self.hover_element(button)
                # 正常点击，不赋值 playbackRate。某选项不生效时尝试其余可用选项。
                if abs(video.evaluate("v => v.playbackRate") - candidate_rate) > 0.01:
                    try:
                        # XtPlayer 在 mouseover 后要求一次有距离的 mousemove 才接受倍速点击。
                        # 在同一选项内正常移动鼠标，不修改播放器状态或派发合成 DOM 事件。
                        box = option.bounding_box()
                        if background:
                            self.hover_element(option)
                            option.dispatch_event("click")
                        elif box:
                            first = {"x": box["width"] * 0.4, "y": box["height"] * 0.5}
                            last = {"x": box["width"] * 0.6, "y": box["height"] * 0.5}
                            option.hover(position=first, timeout=1500)
                            option.hover(position=last, timeout=1500)
                            option.click(position=last, timeout=1500)
                        else:
                            option.click(timeout=1500)
                    except PlaywrightTimeout:
                        if getattr(self.session, "background_playback", False):
                            # 最小化时浏览器可能不支持鼠标命中菜单；只触发已识别控件的事件。
                            option.dispatch_event("mouseover", {"clientX": 1, "clientY": 1})
                            option.dispatch_event("mousemove", {"clientX": 2, "clientY": 1})
                            option.dispatch_event("click")
                        else:
                            self.log(f"倍速 {candidate_rate:g}× 控件不可操作，尝试其余选项。")
                            continue
                if self.wait(lambda: abs(video.evaluate("v => v.playbackRate") - candidate_rate) < 0.01, 2):
                    target, success = candidate_rate, True
                    break
                self.log(f"倍速 {candidate_rate:g}× 未生效，尝试其余选项。")
            if not success:
                raise AssistantError("播放器所有可用倍速设置均未生效，请检查控件后继续。")
        else:
            target = video.evaluate("v => v.playbackRate")
            if not math.isfinite(target) or target <= 0:
                raise AssistantError("无法读取播放器当前倍速。")
            self.log("未发现已适配的倍速菜单，沿用播放器当前倍速；如页面有倍速菜单，请校准。")
        if target != 2:
            self.log(f"二倍速不可用，使用播放器可用倍速 {target:g}×。")
        return target

    def diagnostics(self) -> dict:
        """仅返回控件数量和域名，不导出页面正文、HTML、输入值或登录信息。"""
        result = {"frames": []}
        for frame in self.frames():
            counts = {}
            for name, selector in self.s.items():
                try:
                    counts[name] = frame.locator(selector).count()
                except Exception:
                    counts[name] = "无效选择器"
            result["frames"].append({"host": urlparse(frame.url).hostname, "controls": counts})
        return result
