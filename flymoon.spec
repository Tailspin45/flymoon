# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Zipcatcher Flask backend
# Output: flymoon-server (or flymoon-server.exe on Windows)

import sys, os
from pathlib import Path

block_cipher = None
root = Path(SPECPATH)

a = Analysis(
    [str(root / 'app.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        # Ephemeris file (required by Skyfield)
        (str(root / 'de421.bsp'), '.'),
        # Flask templates and static assets
        (str(root / 'templates'), 'templates'),
        (str(root / 'static'),    'static'),
        # Python source modules
        (str(root / 'src'),       'src'),
    ],
    hiddenimports=[
        'skyfield',
        'skyfield.api',
        'skyfield.almanac',
        'skyfield.data',
        'skyfield.data.planets',
        'skyfield.sgp4lib',
        'sgp4',
        'sgp4.api',
        'numpy',
        'numpy.core',
        'numpy.core._methods',
        'numpy.lib.format',
        'tzlocal',
        'zoneinfo',
        'flask',
        'flask.json',
        'flask.json.provider',
        'jinja2',
        'jinja2.ext',
        'werkzeug',
        'werkzeug.serving',
        'werkzeug.debug',
        'dotenv',
        'requests',
        'requests.adapters',
        'urllib3',
        'certifi',
        'charset_normalizer',
        'idna',
        'telegram',
        'aiofiles',
        'cv2',
        'csv',
        'email.mime.text',
        'email.mime.multipart',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
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
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='flymoon-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,      # Keep console for logging; Electron hides it
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=os.environ.get('CODESIGN_IDENTITY', None),
    entitlements_file=os.environ.get('ENTITLEMENTS_FILE', None),
)
