# -*- mode: python ; coding: utf-8 -*-
from PyInstaller.utils.hooks import collect_data_files
import os

# 构建环境中的 Poppler 带有同名 ICU DLL，Qt 6.11 需要 Windows 系统 ICU。
# 不让构建工具从其他应用的 PATH 收集该同名文件。
os.environ['PATH'] = os.pathsep.join(p for p in os.environ.get('PATH', '').split(os.pathsep)
                                  if 'poppler' not in p.lower())

a = Analysis(
    ['main.py'], pathex=[], binaries=[],
    datas=[('docs', 'docs')] + collect_data_files('playwright'),
    hiddenimports=[], hookspath=[], hooksconfig={}, runtime_hooks=[],
    excludes=['PySide6.QtWebEngineWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtQml', 'PySide6.QtQuick'],
    noarchive=False,
)
# ICU 由 Windows 10/11 提供；不打包其他应用附带的不兼容副本。
a.binaries = [item for item in a.binaries if item[0].lower() not in ('icuuc.dll', 'icudt78.dll')]
pyz = PYZ(a.pure)
exe = EXE(pyz, a.scripts, [], exclude_binaries=True,
          name='学堂云播放助手', debug=False, bootloader_ignore_signals=False,
          strip=False, upx=False, console=False, disable_windowed_traceback=False)
coll = COLLECT(exe, a.binaries, a.datas, strip=False, upx=False, name='XuetangAssistant')
