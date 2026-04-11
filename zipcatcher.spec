# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for Zipcatcher Flask backend
# Output: electron/zipcatcher-server/zipcatcher-server (onedir mode)
#
# Onedir mode is used instead of onefile so macOS doesn't have to unpack
# a ~1 GB archive into /var/folders on every launch. First-launch time
# drops from ~60 s to under 10 s because Gatekeeper and the PyInstaller
# bootloader can mmap the already-expanded Python runtime.

import os
from pathlib import Path

block_cipher = None
root = Path(SPECPATH)


def _collect_static(base: Path):
    """Walk static/ and emit (src, dest) tuples for every file except
    the user's capture history, which can be hundreds of MB / GB and
    doesn't belong in the packaged app."""
    out = []
    for path in base.rglob('*'):
        if not path.is_file():
            continue
        rel = path.relative_to(base)
        top = rel.parts[0]
        if top == 'captures':
            continue
        dest_dir = str(Path('static') / rel.parent)
        out.append((str(path), dest_dir))
    return out


a = Analysis(
    [str(root / 'app.py')],
    pathex=[str(root)],
    binaries=[],
    datas=[
        (str(root / 'de421.bsp'), '.'),
        (str(root / 'templates'), 'templates'),
        (str(root / 'src'),       'src'),
    ] + _collect_static(root / 'static'),
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
    # Torch and ONNX aren't imported by any Zipcatcher code — PyInstaller
    # pulls them in transitively from the venv. Excluding them saves
    # ~350 MB of bundled weight.
    excludes=[
        'torch',
        'torchvision',
        'torchaudio',
        'onnx',
        'onnxruntime',
        'ml_dtypes',
        'jax',
        'jaxlib',
        'tensorflow',
    ],
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
    name='zipcatcher-server',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=True,
    disable_windowed_traceback=False,
    target_arch=None,
    codesign_identity=os.environ.get('CODESIGN_IDENTITY', None),
    entitlements_file=os.environ.get('ENTITLEMENTS_FILE', None),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name='zipcatcher-server',
)
