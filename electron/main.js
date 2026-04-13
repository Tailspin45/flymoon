'use strict';

const { app, BrowserWindow, ipcMain, shell, dialog, Menu, screen, session } = require('electron');
const path    = require('path');
const fs      = require('fs');
const http    = require('http');
const https   = require('https');
const net     = require('net');
const { spawn, execFile } = require('child_process');

// ─── Paths ──────────────────────────────────────────────────────────────────
const IS_PACKAGED   = app.isPackaged;
const USER_DATA     = app.getPath('userData');
const CONFIG_FILE      = path.join(USER_DATA, 'config.json');
const WIN_STATE_FILE   = path.join(USER_DATA, 'window-state.json');
const RESOURCES        = IS_PACKAGED ? process.resourcesPath : path.join(__dirname, '..');

// ─── Globals ────────────────────────────────────────────────────────────────
let flaskProcess  = null;
let flaskPort     = null;
let mainWindow    = null;
let wizardWindow  = null;
let wizardMode    = 'preferences'; // 'first-launch' | 'preferences'
let splashWindow  = null;
let suppressWindowAllClosed = false;
let wizardBootInProgress = false;

// ─── Window state persistence ────────────────────────────────────────────────
const DEFAULT_WIDTH  = 1920;  // Default launch width

function loadWindowState() {
    try {
        if (fs.existsSync(WIN_STATE_FILE)) {
            return JSON.parse(fs.readFileSync(WIN_STATE_FILE, 'utf8'));
        }
    } catch (e) { /* ignore */ }
    return null;
}

function saveWindowState(win) {
    if (!win || win.isMinimized() || win.isMaximized()) return;
    try {
        const b = win.getBounds();
        fs.writeFileSync(WIN_STATE_FILE, JSON.stringify(b));
    } catch (e) { /* ignore */ }
}

function centerOnPrimaryDisplay(width, height) {
    const { workArea } = screen.getPrimaryDisplay();
    return {
        x: Math.round(workArea.x + (workArea.width  - width)  / 2),
        y: Math.round(workArea.y + (workArea.height - height) / 2),
    };
}

function defaultHeight() {
    // Fill available vertical space up to ~1200px (enough for 15 aircraft rows)
    const { workArea } = screen.getPrimaryDisplay();
    return Math.min(workArea.height, 1200);
}

// ─── Utilities ──────────────────────────────────────────────────────────────

function findFreePort() {
    return new Promise((resolve, reject) => {
        const srv = net.createServer();
        srv.listen(0, '127.0.0.1', () => {
            const port = srv.address().port;
            srv.close(() => resolve(port));
        });
        srv.on('error', reject);
    });
}

function waitForFlask(port, maxMs = 30000) {
    return new Promise((resolve, reject) => {
        const deadline = Date.now() + maxMs;
        const attempt = () => {
            http.get(`http://127.0.0.1:${port}/`, (res) => {
                resolve();
            }).on('error', () => {
                if (Date.now() > deadline) return reject(new Error('Flask did not start in time'));
                setTimeout(attempt, 400);
            });
        };
        attempt();
    });
}

// IP-based geolocation used by the wizard's Auto-detect button. Electron's
// built-in navigator.geolocation provider is unusable without a Google API
// key, so the wizard calls this directly.
//
// Tries multiple providers in order so one rate-limiting / outage doesn't
// brick detection. Each provider's response is normalised to a common shape.
const IP_GEO_PROVIDERS = [
    {
        name: 'ipapi.co',
        url:  'https://ipapi.co/json/',
        parse: (j) => (typeof j.latitude === 'number' && typeof j.longitude === 'number') ? {
            latitude:  j.latitude,
            longitude: j.longitude,
            city:      j.city || '',
            region:    j.region || '',
            country:   j.country_name || j.country || '',
        } : null,
    },
    {
        name: 'ipinfo.io',
        url:  'https://ipinfo.io/json',
        // ipinfo returns "loc":"34.0522,-118.2437"
        parse: (j) => {
            if (!j.loc || typeof j.loc !== 'string') return null;
            const [latS, lonS] = j.loc.split(',');
            const lat = parseFloat(latS), lon = parseFloat(lonS);
            if (!isFinite(lat) || !isFinite(lon)) return null;
            return {
                latitude:  lat,
                longitude: lon,
                city:      j.city || '',
                region:    j.region || '',
                country:   j.country || '',
            };
        },
    },
    {
        name: 'ip-api.com',
        url:  'http://ip-api.com/json/', // http-only on free tier
        parse: (j) => (j.status === 'success' && typeof j.lat === 'number' && typeof j.lon === 'number') ? {
            latitude:  j.lat,
            longitude: j.lon,
            city:      j.city || '',
            region:    j.regionName || '',
            country:   j.country || '',
        } : null,
    },
];

function _fetchIpProvider(provider, timeoutMs) {
    return new Promise((resolve) => {
        const client = provider.url.startsWith('https') ? https : http;
        const req = client.get(provider.url, { timeout: timeoutMs }, (res) => {
            if (res.statusCode !== 200) {
                res.resume();
                return resolve({ ok: false, reason: `HTTP ${res.statusCode}` });
            }
            let body = '';
            res.setEncoding('utf8');
            res.on('data', (c) => { body += c; });
            res.on('end', () => {
                try {
                    const j = JSON.parse(body);
                    const geo = provider.parse(j);
                    if (geo) return resolve({ ok: true, geo });
                    resolve({ ok: false, reason: 'no lat/lon in response' });
                } catch (e) {
                    resolve({ ok: false, reason: `parse error: ${e.message}` });
                }
            });
        });
        req.on('timeout', () => { req.destroy(); resolve({ ok: false, reason: 'timeout' }); });
        req.on('error',   (e) => resolve({ ok: false, reason: `network error: ${e.message}` }));
    });
}

async function ipGeolocate(timeoutMs = 5000) {
    for (const provider of IP_GEO_PROVIDERS) {
        const result = await _fetchIpProvider(provider, timeoutMs);
        if (result.ok) {
            console.log(`[ipGeolocate] ${provider.name} → ${result.geo.latitude}, ${result.geo.longitude} (${result.geo.city})`);
            return result.geo;
        }
        console.warn(`[ipGeolocate] ${provider.name} failed: ${result.reason}`);
    }
    return null;
}

// Greenwich Observatory — used as the fallback when the user opens the app
// without any saved config (including Skip All) so Flask always has valid
// coordinates to render a map and compute celestial positions.
const GREENWICH = {
    latitude:  51.4769,
    longitude: -0.0005,
    elevation: 46,
};

// Matches DEFAULT_BBOX_HALF_WIDTH in wizard.html — keep in sync.
const DEFAULT_BBOX_HALF_WIDTH = 0.35;

function buildGreenwichConfig() {
    const { latitude: lat, longitude: lon, elevation } = GREENWICH;
    const d = DEFAULT_BBOX_HALF_WIDTH;
    return {
        aeroapi_key: '',
        latitude:  lat,
        longitude: lon,
        elevation,
        bbox_lat_ll: lat - d,
        bbox_lon_ll: lon - d,
        bbox_lat_ur: lat + d,
        bbox_lon_ur: lon + d,
        seestar_enabled: false,
    };
}

// Backfill any missing required fields with Greenwich values so a
// partially-filled Skip'd wizard still yields a usable config.
function applyGreenwichDefaults(cfg) {
    const def = buildGreenwichConfig();
    const out = { ...def, ...cfg };
    const requiredNum = ['latitude','longitude','bbox_lat_ll','bbox_lon_ll','bbox_lat_ur','bbox_lon_ur'];
    for (const k of requiredNum) {
        const v = parseFloat(out[k]);
        if (!isFinite(v)) out[k] = def[k];
    }
    if (!isFinite(parseFloat(out.elevation))) out.elevation = def.elevation;
    return out;
}

function loadConfig() {
    try {
        return JSON.parse(fs.readFileSync(CONFIG_FILE, 'utf8'));
    } catch {
        return null;
    }
}

function saveConfig(cfg) {
    fs.mkdirSync(USER_DATA, { recursive: true });
    fs.writeFileSync(CONFIG_FILE, JSON.stringify(cfg, null, 2));
}

function configToEnv(cfg) {
    // Build environment variable object from saved config
    const env = {
        ...process.env,
        FLASK_PORT:          String(cfg.flask_port || flaskPort),
        AEROAPI_API_KEY:     cfg.aeroapi_key      || '',
        OBSERVER_LATITUDE:   String(cfg.latitude  || ''),
        OBSERVER_LONGITUDE:  String(cfg.longitude || ''),
        OBSERVER_ELEVATION:  String(cfg.elevation || '0'),
        LAT_LOWER_LEFT:      String(cfg.bbox_lat_ll || ''),
        LONG_LOWER_LEFT:     String(cfg.bbox_lon_ll || ''),
        LAT_UPPER_RIGHT:     String(cfg.bbox_lat_ur || ''),
        LONG_UPPER_RIGHT:    String(cfg.bbox_lon_ur || ''),
    };
    if (cfg.telegram_token)  env.TELEGRAM_BOT_TOKEN = cfg.telegram_token;
    if (cfg.telegram_chat)   env.TELEGRAM_CHAT_ID   = cfg.telegram_chat;
    if (cfg.seestar_enabled) {
        env.ENABLE_SEESTAR = 'true';
        env.SEESTAR_HOST   = cfg.seestar_host || '';
        env.SEESTAR_PORT   = String(cfg.seestar_port || 4700);
    } else {
        env.ENABLE_SEESTAR = 'false';
    }
    return env;
}

// ─── Flask Server ────────────────────────────────────────────────────────────

function detectFfmpeg() {
    // Prefer the bundled ffmpeg (ffmpeg-static) so users don't have to
    // install anything. In packaged mode it lives at
    //   Contents/Resources/ffmpeg (+ .exe on Windows)
    // and in dev mode we resolve it via require.resolve from node_modules.
    const exeName = process.platform === 'win32' ? 'ffmpeg.exe' : 'ffmpeg';
    const bundled = path.join(RESOURCES, exeName);
    if (fs.existsSync(bundled)) return bundled;

    if (!IS_PACKAGED) {
        try {
            const devPath = require('ffmpeg-static');
            if (devPath && fs.existsSync(devPath)) return devPath;
        } catch (_) { /* package not installed in dev env */ }
    }

    // Fallback: look for a system ffmpeg on PATH or in well-known locations.
    const { execFileSync } = require('child_process');
    const candidates = ['ffmpeg'];
    if (process.platform === 'darwin') {
        candidates.push('/opt/homebrew/bin/ffmpeg', '/usr/local/bin/ffmpeg');
    } else if (process.platform === 'win32') {
        candidates.push('C:\\ffmpeg\\bin\\ffmpeg.exe');
    }
    for (const cmd of candidates) {
        try {
            execFileSync(cmd, ['-version'], { stdio: 'ignore', timeout: 5000 });
            return cmd;
        } catch { /* not found, try next */ }
    }
    return null;
}

function resolveBundledServerPath() {
    // PyInstaller onedir layout: Contents/Resources/zipcatcher-server/zipcatcher-server
    const binName = process.platform === 'win32' ? 'zipcatcher-server.exe' : 'zipcatcher-server';
    const candidates = [
        path.join(RESOURCES, 'zipcatcher-server', binName),
        path.join(RESOURCES, binName),
        path.join(RESOURCES, 'bin', binName),
        path.join(RESOURCES, 'app.asar.unpacked', binName),
    ];

    for (const candidate of candidates) {
        if (fs.existsSync(candidate)) return candidate;
    }

    const topLevel = fs.existsSync(RESOURCES) ? fs.readdirSync(RESOURCES).slice(0, 20) : [];
    throw new Error(
        `Bundled server binary not found (${binName}) in resources path: ${RESOURCES}. ` +
        `Top-level entries: ${topLevel.join(', ') || '(none)'}`
    );
}

async function startFlask(cfg) {
    flaskPort = await findFreePort();
    cfg.flask_port = flaskPort;

    const env  = configToEnv(cfg);
    let cwd  = RESOURCES;

    let cmd, args;
    if (IS_PACKAGED) {
        // Use the bundled PyInstaller binary, with robust lookup for installer layouts.
        cmd = resolveBundledServerPath();
        cwd = path.dirname(cmd);
        args = [];
    } else {
        // Development: use system python
        cmd  = process.platform === 'win32' ? 'python' : 'python3';
        args = [path.join(RESOURCES, 'app.py')];
    }

    env.PORT = String(flaskPort);

    // Suppress Flask's built-in auto-open-browser behavior. Without this,
    // app.py calls webbrowser.open() on startup and the system default
    // browser launches an extra window pointing at the Flask port — the
    // mysterious "standalone app" users saw alongside the Electron shell.
    env.FLYMOON_NO_BROWSER = '1';

    // Detect ffmpeg and pass its path to Flask
    const ffmpegPath = detectFfmpeg();
    if (ffmpegPath) {
        env.FFMPEG_PATH = ffmpegPath;
        console.log(`[ffmpeg] Found at: ${ffmpegPath}`);
    } else {
        console.warn('[ffmpeg] Not found — telescope recording/detection disabled');
    }

    flaskProcess = spawn(cmd, args, {
        cwd,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    const proc = flaskProcess;
    proc.stdout.on('data', d => console.log('[Flask]', d.toString().trim()));
    proc.stderr.on('data', d => console.error('[Flask]', d.toString().trim()));

    proc.on('exit', (code) => {
        console.log(`[Flask] exited with code ${code}`);
        flaskProcess = null;
    });

    const spawnErrorPromise = new Promise((_, reject) => {
        proc.once('error', (err) => {
            const context = IS_PACKAGED ? `Bundled server path: ${cmd}` : `Command: ${cmd}`;
            reject(new Error(`Failed to start Zipcatcher server (${err.message}). ${context}`));
        });
    });

    const earlyExitPromise = new Promise((_, reject) => {
        proc.once('exit', (code) => {
            reject(new Error(`Zipcatcher server exited before becoming ready (exit code ${code}).`));
        });
    });

    await Promise.race([
        waitForFlask(flaskPort),
        spawnErrorPromise,
        earlyExitPromise,
    ]);
    console.log(`[Flask] Ready on port ${flaskPort}`);
}

// ─── Windows ─────────────────────────────────────────────────────────────────

function createMainWindow() {
    const saved  = loadWindowState();
    const height = saved ? saved.height : defaultHeight();
    const pos    = saved || centerOnPrimaryDisplay(DEFAULT_WIDTH, height);

    mainWindow = new BrowserWindow({
        width:     saved ? saved.width : DEFAULT_WIDTH,
        height,
        x:         pos.x,
        y:         pos.y,
        minWidth:  900,
        minHeight: 600,
        title: 'Zipcatcher',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });

    // Save bounds whenever the user moves or resizes
    const persist = () => saveWindowState(mainWindow);
    mainWindow.on('resize', persist);
    mainWindow.on('move',   persist);

    mainWindow.loadURL(`http://127.0.0.1:${flaskPort}/`);

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        shell.openExternal(url);
        return { action: 'deny' };
    });

    buildMenu();
    mainWindow.on('closed', () => { mainWindow = null; });
}

function createSplashWindow() {
    splashWindow = new BrowserWindow({
        width:  720,
        height: 480,
        resizable: false,
        frame: false,
        transparent: false,
        center: true,
        alwaysOnTop: true,
        skipTaskbar: true,
        webPreferences: {
            contextIsolation: true,
            nodeIntegration: false,
        },
    });
    splashWindow.loadFile(path.join(__dirname, 'splash.html'));
    splashWindow.on('closed', () => { splashWindow = null; });
}

function setSplashProgress(msg, pct) {
    if (!splashWindow || splashWindow.isDestroyed()) return;
    const safe = msg.replace(/\\/g, '\\\\').replace(/'/g, "\\'");
    splashWindow.webContents.executeJavaScript(
        `window.setSplashStatus && window.setSplashStatus('${safe}', ${pct});`
    ).catch(() => {});
}

function sendWizardProgress(msg, pct) {
    if (wizardWindow && !wizardWindow.isDestroyed()) {
        wizardWindow.webContents.send('launch-progress', { msg, pct });
    }
}

function createWizardWindow(mode = 'preferences') {
    wizardMode = mode;
    wizardWindow = new BrowserWindow({
        width:  720,
        height: 680,
        resizable: false,
        title: 'Zipcatcher Preferences',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });

    wizardWindow.loadFile(path.join(__dirname, 'wizard.html'), {
        query: { mode },
    });
    wizardWindow.on('close', () => {
        // In first-launch mode, the wizard may close before the main window
        // exists. Prevent the app-level "window-all-closed" handler from
        // killing Flask during this intentional handoff.
        if (wizardBootInProgress || (wizardMode === 'first-launch' && !mainWindow)) {
            suppressWindowAllClosed = true;
        }
    });
    wizardWindow.on('closed', () => {
        wizardWindow = null;
        // If the user closed the wizard on first launch without completing
        // it (window close button), fall back to Greenwich defaults so the
        // app still has a usable config.
        if (wizardMode === 'first-launch' && !loadConfig()) {
            wizardBootInProgress = true;
            const cfg = buildGreenwichConfig();
            saveConfig(cfg);
            wizardMode = 'preferences';
            bootFromConfig(cfg).catch((err) => {
                dialog.showErrorBox('Startup Error', `Failed to start Zipcatcher server:\n\n${err.message}`);
                app.quit();
            }).finally(() => {
                wizardBootInProgress = false;
                suppressWindowAllClosed = false;
            });
            return;
        }
        if (!wizardBootInProgress) suppressWindowAllClosed = false;
        wizardMode = 'preferences';
    });
}

function buildMenu() {
    const docsDir = path.join(RESOURCES, 'docs');
    const openHelpDoc = (docId, fallbackFilename) => {
        const fallbackPath = path.join(docsDir, fallbackFilename);
        const fallbackOpen = () => shell.openPath(fallbackPath);

        if (!mainWindow || mainWindow.isDestroyed()) {
            fallbackOpen();
            return;
        }

        if (mainWindow.isMinimized()) mainWindow.restore();
        mainWindow.show();
        mainWindow.focus();

        const script = `(typeof window.openHelpDocumentFromMenu === 'function')
            ? window.openHelpDocumentFromMenu(${JSON.stringify(docId)})
            : false;`;

        mainWindow.webContents
            .executeJavaScript(script, true)
            .then((handled) => {
                if (!handled) fallbackOpen();
            })
            .catch(() => fallbackOpen());
    };

    const template = [
        {
            label: 'File',
            submenu: [
                {
                    label: 'Preferences…',
                    accelerator: 'CmdOrCtrl+,',
                    click: () => openWizardForEdit(),
                },
                { type: 'separator' },
                { role: 'quit' },
            ],
        },
        { role: 'editMenu' },
        { role: 'viewMenu' },
        {
            label: 'Help',
            submenu: [
                { label: 'Quick Start',      click: () => openHelpDoc('quick-start', 'QUICKSTART.md') },
                { label: 'Setup Guide',      click: () => openHelpDoc('setup-guide', 'SETUP.md') },
                { label: 'Telescope Guide',  click: () => openHelpDoc('telescope-guide', 'TELESCOPE_GUIDE.md') },
                { type: 'separator' },
                { label: 'Zipcatcher Article (PDF)',       click: () => openHelpDoc('zipcatcher-article', 'Zipcatcher-article.pdf') },
                { label: 'Transit Position Paper (PDF)',click: () => openHelpDoc('transit-position-paper', 'transit_capture_position_paper.pdf') },
                { type: 'separator' },
                { label: 'Get FlightAware API Key', click: () => shell.openExternal('https://www.flightaware.com/aeroapi/portal/') },
                { label: 'Create Telegram Bot',     click: () => shell.openExternal('https://t.me/botfather') },
            ],
        },
    ];
    Menu.setApplicationMenu(Menu.buildFromTemplate(template));
}

function openWizardForEdit() {
    if (wizardWindow) { wizardWindow.focus(); return; }
    createWizardWindow('preferences');
}

// Start Flask with the given config and show the main window. Shared by
// the first-launch wizard flow and the normal startup path.
async function bootFromConfig(cfg) {
    setSplashProgress('Starting Zipcatcher server…', 30);
    await startFlask(cfg);
    setSplashProgress('Connecting to flight data…', 75);
    await new Promise(r => setTimeout(r, 200));
    setSplashProgress('Ready.', 100);
    if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
    if (!mainWindow) createMainWindow();
    else mainWindow.show();
}

// ─── IPC Handlers ─────────────────────────────────────────────────────────────

ipcMain.handle('get-config', () => loadConfig() || {});

ipcMain.handle('save-config', async (_e, cfg) => {
    saveConfig(cfg);
    return { ok: true };
});

async function restartFlask(cfg) {
    if (flaskProcess) {
        const proc = flaskProcess;
        flaskProcess = null;
        proc.removeAllListeners('exit');
        proc.kill();
        await new Promise(r => setTimeout(r, 300));
    }
    await startFlask(cfg);
}

ipcMain.handle('wizard-complete', async (_e, cfg) => {
    const wasFirstLaunch = wizardMode === 'first-launch';
    const finalCfg = applyGreenwichDefaults(cfg || {});
    saveConfig(finalCfg);

    try {
        if (wasFirstLaunch) {
            wizardBootInProgress = true;
            suppressWindowAllClosed = true;
            // Reset mode before we close the window so the `closed` handler
            // doesn't treat this as an abandoned first-launch.
            wizardMode = 'preferences';
            if (wizardWindow && !wizardWindow.isDestroyed()) {
                wizardWindow.close();
                wizardWindow = null;
            }
            await bootFromConfig(finalCfg);
        } else {
            wizardMode = 'preferences';
            if (wizardWindow && !wizardWindow.isDestroyed()) {
                wizardWindow.close();
                wizardWindow = null;
            }
            // Preferences edit: Flask is already running; restart so the
            // new env vars take effect, then reload the main window.
            await restartFlask(finalCfg);
            if (!mainWindow) createMainWindow();
            else mainWindow.loadURL(`http://127.0.0.1:${flaskPort}/`);
        }
    } catch (err) {
        dialog.showErrorBox('Startup Error', `Failed to start Zipcatcher server:\n\n${err.message}`);
    } finally {
        if (wasFirstLaunch) {
            wizardBootInProgress = false;
            suppressWindowAllClosed = false;
        }
    }
    return { ok: true };
});

// Invoked by the wizard's "Skip All" button on Card 1. Writes a Greenwich
// default config, boots Flask, and shows the main window.
ipcMain.handle('wizard-skip-all', async () => {
    const wasFirstLaunch = wizardMode === 'first-launch';
    const existing = loadConfig();
    const finalCfg = applyGreenwichDefaults(existing || {});
    saveConfig(finalCfg);

    try {
        if (wasFirstLaunch) {
            wizardBootInProgress = true;
            suppressWindowAllClosed = true;
            wizardMode = 'preferences';
            if (wizardWindow && !wizardWindow.isDestroyed()) {
                wizardWindow.close();
                wizardWindow = null;
            }
            await bootFromConfig(finalCfg);
        } else {
            wizardMode = 'preferences';
            if (wizardWindow && !wizardWindow.isDestroyed()) {
                wizardWindow.close();
                wizardWindow = null;
            }
        }
        // In preferences mode, Flask is already running with the existing
        // config; nothing to do.
    } catch (err) {
        dialog.showErrorBox('Startup Error', `Failed to start Zipcatcher server:\n\n${err.message}`);
    } finally {
        if (wasFirstLaunch) {
            wizardBootInProgress = false;
            suppressWindowAllClosed = false;
        }
    }
    return { ok: true };
});

ipcMain.handle('ip-geolocate', async () => {
    return await ipGeolocate();
});

ipcMain.handle('open-preferences', () => {
    openWizardForEdit();
    return { ok: true };
});

ipcMain.handle('open-external', (_e, url) => shell.openExternal(url));

ipcMain.handle('get-resources-path', () => RESOURCES);

// ─── App Lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
    // Grant the wizard renderer permission to use navigator.geolocation.
    // Without this, Electron silently denies geolocation in non-https
    // contexts and the wizard's "Auto-detect my location" button fails.
    session.defaultSession.setPermissionRequestHandler((_wc, permission, callback) => {
        if (permission === 'geolocation') return callback(true);
        callback(false);
    });

    const cfg = loadConfig();
    if (!cfg) {
        // First launch: show the wizard, do not start Flask yet, do not
        // create the main window. Flask boots only after the user completes
        // or skips the wizard.
        createWizardWindow('first-launch');
        return;
    }

    // Normal startup path: splash → Flask → main window.
    createSplashWindow();
    await new Promise(resolve => {
        if (splashWindow && !splashWindow.isDestroyed()) {
            splashWindow.webContents.once('did-finish-load', resolve);
        } else {
            resolve();
        }
    });

    try {
        await bootFromConfig(cfg);
    } catch (err) {
        if (splashWindow && !splashWindow.isDestroyed()) splashWindow.close();
        dialog.showErrorBox('Startup Error', `Failed to start Zipcatcher server:\n\n${err.message}`);
        app.quit();
    }
});

app.on('window-all-closed', () => {
    if (suppressWindowAllClosed) return;
    if (flaskProcess) flaskProcess.kill();
    if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
    if (flaskProcess) flaskProcess.kill();
});

app.on('activate', async () => {
    if (!mainWindow && !wizardWindow) {
        const cfg = loadConfig();
        if (cfg) {
            if (!flaskProcess) await startFlask(cfg);
            createMainWindow();
        }
    }
});
