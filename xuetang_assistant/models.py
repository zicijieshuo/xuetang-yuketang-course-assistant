from dataclasses import asdict, dataclass
from enum import Enum
import math
import re
from urllib.parse import urlparse

HOST = "dcc.yuketang.cn"
COURSE_LIST = f"https://{HOST}/pro/courselist"


class AssistantError(Exception):
    """可安全显示给用户的错误信息。"""


class Cancelled(Exception):
    pass


def allowed_url(url: str) -> bool:
    try:
        parsed = urlparse(url)
        return (parsed.scheme == "https" and parsed.hostname == HOST
                and parsed.port in (None, 443) and not parsed.username and not parsed.password)
    except (ValueError, TypeError):
        return False


def is_video_url(url: str) -> bool:
    return allowed_url(url) and bool(re.search(r"/lms-graph/\d+/video/\d+(?:/|$)", urlparse(url).path))


def progress_value(text: str) -> tuple[float | None, bool]:
    """只解析当前视频专属状态；不能将‘未完成’或其他单元状态识别为完成。"""
    text = " ".join(text.split())
    if text in ("已完成", "Completed"):
        return 100.0, True
    match = re.fullmatch(r"(?:(?:完成度|完成率|Progress)\s*[:：]?\s*)?(\d+(?:\.\d+)?)\s*%", text, re.I)
    if match:
        value = float(match.group(1))
        if 0 <= value <= 100:
            return value, value == 100
    return None, False


@dataclass
class Course:
    key: str
    title: str
    subtitle: str = ""
    url: str = ""
    ordinal: int = 0
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class VideoTask:
    course_key: str
    course_title: str
    course_url: str
    title: str
    ordinal: int
    video_url: str = ""
    status: str = "待播放"
    progress: float | None = None
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class Snapshot:
    visible: bool
    paused: bool
    ended: bool
    current_time: float
    duration: float
    muted: bool
    rate: float
    progress: float | None
    completed: bool


class Action(str, Enum):
    COMPLETE = "complete"
    WAIT_VISIBLE = "wait_visible"
    RECOVER = "recover"
    RELOAD = "reload"
    FAIL = "fail"
    WAIT = "wait"
    PLAYING = "playing"


class Watchdog:
    """纯状态机：真实页面由适配器读取，时间由调用方传入。"""
    def __init__(self, now: float, position: float = 0):
        self.last_position = position
        self.last_move = now
        self.ended_since: float | None = None
        self.reloaded = False
        self.recover_attempted = False
        self.hidden = False

    def decide(self, snapshot: Snapshot, now: float) -> tuple[Action, str]:
        if snapshot.completed:
            return Action.COMPLETE, "平台已确认当前视频完成"
        if not snapshot.visible:
            self.hidden = True
            self.last_move = now
            return Action.WAIT_VISIBLE, "请恢复浏览器窗口并切回播放标签页"
        if self.hidden:
            self.hidden = False
            self.last_move = now
            self.recover_attempted = False
        if not snapshot.muted:
            return Action.FAIL, "检测到当前视频未静音，请处理后点击继续"
        if snapshot.ended:
            if self.ended_since is None:
                self.ended_since = now
            elapsed = now - self.ended_since
            if not self.reloaded and elapsed >= 60:
                self.reloaded = True
                self.ended_since = now
                return Action.RELOAD, "播放结束但平台未确认完成，刷新状态一次"
            if self.reloaded and elapsed >= 15:
                return Action.FAIL, "播放已结束，但平台仍未确认完成，请检查完成度"
            return Action.WAIT, "等待平台确认完成"
        # 刷新后视频可能回到开头；只读取平台状态，不自动重播。
        if self.reloaded:
            if now - (self.ended_since or 0) >= 15:
                return Action.FAIL, "刷新后平台仍未确认完成，请检查完成度"
            return Action.WAIT, "等待刷新后的平台完成状态"
        if math.isfinite(snapshot.current_time) and snapshot.current_time > self.last_position + 0.05:
            self.last_position = snapshot.current_time
            self.last_move = now
            self.recover_attempted = False
        if now - self.last_move >= 60:
            return Action.FAIL, "60 秒内播放时间没有前进，请检查网络或播放器"
        if snapshot.paused and not self.recover_attempted:
            self.recover_attempted = True
            return Action.RECOVER, "尝试恢复意外暂停的播放"
        return Action.PLAYING, "正在播放" if not snapshot.paused else "等待播放恢复"
