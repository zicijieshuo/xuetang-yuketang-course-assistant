import json
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import tempfile


def data_dir() -> Path:
    override = os.environ.get("XUETANG_ASSISTANT_DATA")
    root = Path(override) if override else Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "XuetangAssistant"
    root.mkdir(parents=True, exist_ok=True)
    return root


class Store:
    def __init__(self, root: Path):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)

    def read(self, name: str, default):
        try:
            return json.loads((self.root / name).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return default

    def write(self, name: str, value) -> None:
        # 同目录原子替换；中途退出不会截断原有任务记录。
        fd, temporary = tempfile.mkstemp(dir=self.root, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(value, stream, ensure_ascii=False, indent=2)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, self.root / name)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)


def configure_logging(root: Path) -> logging.Logger:
    logger = logging.getLogger("xuetang")
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(root / "assistant.log", maxBytes=1_000_000, backupCount=3, encoding="utf-8")
        handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
        logger.addHandler(handler)
    return logger
