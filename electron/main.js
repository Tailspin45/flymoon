'use strict';

const { app, BrowserWindow, ipcMain, shell, dialog, Menu } = require('electron');
const path   = require('path');
const fs     = require('fs');
const http   = require('http');
const net    = require('net');
const { spawn, execFile } = require('child_process');

// ─── Paths ──────────────────────────────────────────────────────────────────
const IS_PACKAGED   = app.isPackaged;
const USER_DATA     = app.getPath('userData');
const CONFIG_FILE   = path.join(USER_DATA, 'config.json');
const RESOURCES     = IS_PACKAGED ? process.resourcesPath : path.join(__dirname, '..');

// ─── Globals ────────────────────────────────────────────────────────────────
let flaskProcess  = null;
let flaskPort     = null;
let mainWindow    = null;
let wizardWindow  = null;

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

function waitForFlask(port, maxMs = 20000) {
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

async function startFlask(cfg) {
    flaskPort = await findFreePort();
    cfg.flask_port = flaskPort;

    const env  = configToEnv(cfg);
    const cwd  = RESOURCES;

    let cmd, args;
    if (IS_PACKAGED) {
        // Use the bundled PyInstaller binary
        const binName = process.platform === 'win32' ? 'flymoon-server.exe' : 'flymoon-server';
        cmd  = path.join(RESOURCES, binName);
        args = [];
    } else {
        // Development: use system python
        cmd  = process.platform === 'win32' ? 'python' : 'python3';
        args = [path.join(RESOURCES, 'app.py')];
    }

    env.PORT = String(flaskPort);

    flaskProcess = spawn(cmd, args, {
        cwd,
        env,
        stdio: ['ignore', 'pipe', 'pipe'],
    });

    flaskProcess.stdout.on('data', d => console.log('[Flask]', d.toString().trim()));
    flaskProcess.stderr.on('data', d => console.error('[Flask]', d.toString().trim()));

    flaskProcess.on('exit', (code) => {
        console.log(`[Flask] exited with code ${code}`);
        flaskProcess = null;
    });

    await waitForFlask(flaskPort);
    console.log(`[Flask] Ready on port ${flaskPort}`);
}

// ─── Windows ─────────────────────────────────────────────────────────────────

function createMainWindow() {
    mainWindow = new BrowserWindow({
        width:  1400,
        height: 900,
        minWidth:  900,
        minHeight: 600,
        title: 'Flymoon',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });

    mainWindow.loadURL(`http://127.0.0.1:${flaskPort}/`);

    mainWindow.webContents.setWindowOpenHandler(({ url }) => {
        shell.openExternal(url);
        return { action: 'deny' };
    });

    buildMenu();
    mainWindow.on('closed', () => { mainWindow = null; });
}

function createWizardWindow() {
    wizardWindow = new BrowserWindow({
        width:  720,
        height: 680,
        resizable: false,
        title: 'Flymoon Setup',
        webPreferences: {
            preload: path.join(__dirname, 'preload.js'),
            contextIsolation: true,
            nodeIntegration: false,
        },
    });

    wizardWindow.loadFile(path.join(__dirname, 'wizard.html'));
    wizardWindow.on('closed', () => { wizardWindow = null; });
}

function buildMenu() {
    const docsDir = path.join(RESOURCES, 'docs');
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
                { label: 'Quick Start',      click: () => shell.openPath(path.join(docsDir, 'QUICKSTART.md')) },
                { label: 'Quick Reference',  click: () => shell.openPath(path.join(docsDir, 'QUICK_REFERENCE.md')) },
                { label: 'Setup Guide',      click: () => shell.openPath(path.join(docsDir, 'SETUP.md')) },
                { label: 'Telescope Guide',  click: () => shell.openPath(path.join(docsDir, 'TELESCOPE_GUIDE.md')) },
                { type: 'separator' },
                { label: 'Flymoon Article (PDF)',       click: () => shell.openPath(path.join(docsDir, 'Flymoon-article.pdf')) },
                { label: 'Transit Position Paper (PDF)',click: () => shell.openPath(path.join(docsDir, 'transit_capture_position_paper.pdf')) },
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
    createWizardWindow();
}

// ─── IPC Handlers ─────────────────────────────────────────────────────────────

ipcMain.handle('get-config', () => loadConfig() || {});

ipcMain.handle('save-config', async (_e, cfg) => {
    saveConfig(cfg);
    return { ok: true };
});

ipcMain.handle('wizard-complete', async (_e, cfg) => {
    saveConfig(cfg);
    if (wizardWindow) { wizardWindow.close(); wizardWindow = null; }
    if (!mainWindow) {
        await startFlask(cfg);
        createMainWindow();
    }
    return { ok: true };
});

ipcMain.handle('open-external', (_e, url) => shell.openExternal(url));

ipcMain.handle('get-resources-path', () => RESOURCES);

// ─── App Lifecycle ─────────────────────────────────────────────────────────────

app.whenReady().then(async () => {
    const cfg = loadConfig();
    if (!cfg || !cfg.aeroapi_key) {
        // First run: show setup wizard
        createWizardWindow();
    } else {
        try {
            await startFlask(cfg);
            createMainWindow();
        } catch (err) {
            dialog.showErrorBox('Startup Error', `Failed to start Flymoon server:\n\n${err.message}`);
            app.quit();
        }
    }
});

app.on('window-all-closed', () => {
    if (flaskProcess) flaskProcess.kill();
    if (process.platform !== 'darwin') app.quit();
});

app.on('before-quit', () => {
    if (flaskProcess) flaskProcess.kill();
});

app.on('activate', async () => {
    if (!mainWindow && !wizardWindow) {
        const cfg = loadConfig();
        if (cfg && cfg.aeroapi_key) {
            if (!flaskProcess) await startFlask(cfg);
            createMainWindow();
        }
    }
});
