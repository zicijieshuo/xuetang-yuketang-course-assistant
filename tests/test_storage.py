from xuetang_assistant.storage import Store


def test_utf8_chinese_path_and_corrupt_record(tmp_path):
    store = Store(tmp_path / "中文目录")
    store.write("任务.json", {"课程": "工程伦理", "状态": "待播放"})
    assert store.read("任务.json", {}) == {"课程": "工程伦理", "状态": "待播放"}
    assert "工程伦理" in (store.root / "任务.json").read_text(encoding="utf-8")
    (store.root / "任务.json").write_text("损坏", encoding="utf-8")
    assert store.read("任务.json", {}) == {}
