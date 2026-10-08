"""只下载公开前端资源用于确认页面结构；不使用登录信息或执行脚本。"""
from pathlib import Path
import re
import urllib.request

ROOT = Path(__file__).resolve().parents[1] / "research"
ROOT.mkdir(exist_ok=True)
URLS = {
    "courses.html": "https://dcc.yuketang.cn/pro/courselist",
    "video.html": "https://dcc.yuketang.cn/ai-workspace/lms-graph/33573658/video/90603191?fromProIframe=1&isyth=1&is_chapter=1&node_id=17178117",
}

def download(url: str, name: str) -> str:
    with urllib.request.urlopen(url, timeout=30) as response:
        text = response.read().decode("utf-8")
    (ROOT / name).write_text(text, encoding="utf-8")
    print(name, len(text), url)
    return text

if __name__ == "__main__":
    for name, url in URLS.items():
        html = download(url, name)
        for src in re.findall(r'<script[^>]+src="([^"]+)"', html):
            if "fe-static-yuketang" in src and any(x in src for x in ("app_", "manifest_", "manifest.", "aiworkspace.", "index-", "index.")):
                download(src, name.split(".")[0] + "-" + src.rsplit("/", 1)[-1])
