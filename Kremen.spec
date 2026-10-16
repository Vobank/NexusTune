# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['C:/Users/VovaPC/NexusTune/main.py'],
    pathex=[],
    binaries=[],
    datas=[],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['PySide6.QtQml', 'PySide6.QtQuick', 'PySide6.QtQuick3D', 'PySide6.QtQuickWidgets', 'PySide6.QtWebEngineCore', 'PySide6.QtWebEngineWidgets', 'PySide6.QtWebChannel', 'PySide6.QtMultimedia', 'PySide6.QtMultimediaWidgets', 'PySide6.QtCharts', 'PySide6.QtDataVisualization', 'PySide6.Qt3DCore', 'PySide6.Qt3DRender', 'PySide6.QtBluetooth', 'PySide6.QtNfc', 'PySide6.QtPositioning', 'PySide6.QtSql', 'PySide6.QtTest', 'PySide6.QtDesigner', 'PySide6.QtHelp', 'PySide6.QtPdf', 'PySide6.QtPdfWidgets', 'PySide6.QtSerialPort', 'PySide6.QtOpenGL', 'PySide6.QtOpenGLWidgets', 'PySide6.QtSvgWidgets', 'tkinter', 'unittest', 'pydoc', 'doctest', 'test', 'lib2to3', 'matplotlib', 'numpy', 'PIL', 'setuptools', 'pip'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='Kremen',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    uac_admin=True,
    icon=['C:/Users/VovaPC/NexusTune/kremen.ico'],
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='Kremen',
)
