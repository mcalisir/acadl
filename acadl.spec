# -*- mode: python ; coding: utf-8 -*-
import os
import sys

block_cipher = None

# Patchright driver + browser paths
site_packages = os.path.join(
    os.environ["LOCALAPPDATA"],
    r"Python\pythoncore-3.14-64\Lib\site-packages"
)
patchright_driver = os.path.join(site_packages, "patchright", "driver")
ms_playwright = os.path.join(os.environ["LOCALAPPDATA"], "ms-playwright")

apify_data = os.path.join(site_packages, "apify_fingerprint_datapoints")
browserforge_pkg = os.path.join(site_packages, "browserforge")

datas = [
    (patchright_driver, os.path.join("patchright", "driver")),
    (apify_data, "apify_fingerprint_datapoints"),
    (browserforge_pkg, "browserforge"),
]

# Bundle chromium browser for stealth mode
chromium_dir = os.path.join(ms_playwright, "chromium-1248")
if os.path.isdir(chromium_dir):
    datas.append((chromium_dir, os.path.join("ms-playwright", "chromium-1248")))

# ffmpeg for video support
ffmpeg_dir = os.path.join(ms_playwright, "ffmpeg-1013")
if os.path.isdir(ffmpeg_dir):
    datas.append((ffmpeg_dir, os.path.join("ms-playwright", "ffmpeg-1013")))

a = Analysis(
    ['acadl.py'],
    pathex=[],
    binaries=[],
    datas=datas,
    hiddenimports=[
        'scrapling',
        'scrapling.fetchers',
        'scrapling.engines',
        'scrapling.engines.toolbelt',
        'patchright',
        'patchright.sync_api',
        'lxml',
        'lxml.html',
        'lxml.etree',
        'browserforge',
        'browserforge.headers',
        'browserforge.bayesian_network',
        'apify_fingerprint_datapoints',
        'curl_cffi',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=['runtime_hook.py'],
    excludes=[],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='acadl',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    icon='acadl.ico',
    console=False,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='acadl',
)
