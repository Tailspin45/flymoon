/**
 * Telescope Control Interface - Frontend Logic
 * Manages connection, status polling, target selection, capture controls,
 * live preview, and file management for Seestar S50 telescope.
 */

// State Management
let isConnected = false;
let _prevConnected = false; // tracks previous poll state to detect reconnect
let _lastConnectedStatus = null; // cache last connected response for disconnect diagnostics
let isRecording = false;
let statusPollInterval = null;
let visibilityPollInterval = null;
let lastUpdateInterval = null;
let transitPollInterval = null;
let transitTickInterval = null; // 1-second local countdown tick
let transitCaptureActive = false;
let upcomingTransits = [];
const capturedTransits = new Set(); // flight IDs triggered this session — persists across array replacements
let currentZoom = 2.0;

// Favorites stored in localStorage
const _FAVORITES_KEY = 'flymoon_favorites';
let _favoritesCache = null;
let _favoritesSyncInFlight = null;

function _normalizeFavoritePath(path) {
    if (path == null) return null;
    let s = String(path).trim();
    if (!s) return null;
    s = s.split('?')[0].split('#')[0];
    if (!s.startsWith('/static/captures/')) return null;
    if (s.includes('..')) return null;
    return s;
}

function _normalizeFavoriteCollection(values) {
    const out = [];
    const seen = new Set();
    const arr = Array.isArray(values) ? values : [];
    for (const v of arr) {
        const n = _normalizeFavoritePath(v);
        if (!n || seen.has(n)) continue;
        seen.add(n);
        out.push(n);
    }
    return out;
}

function _getFavoriteCache() {
    if (_favoritesCache !== null) return _favoritesCache;
    try {
        const raw = JSON.parse(localStorage.getItem(_FAVORITES_KEY) || '[]');
        _favoritesCache = new Set(_normalizeFavoriteCollection(raw));
    } catch {
        _favoritesCache = new Set();
    }
    return _favoritesCache;
}

async function _syncFavoritesFromServer() {
    if (_favoritesSyncInFlight) return _favoritesSyncInFlight;
    _favoritesSyncInFlight = (async () => {
        try {
            const resp = await fetch('/telescope/files/favorites', { cache: 'no-store' });
            if (!resp.ok) return;
            const data = await resp.json().catch(() => ({}));
            const normalized = _normalizeFavoriteCollection(data.favorites || []);
            _favoritesCache = new Set(normalized);
            localStorage.setItem(_FAVORITES_KEY, JSON.stringify(normalized));
        } catch (_) {
            // Keep local fallback when server sync is unavailable.
        } finally {
            _favoritesSyncInFlight = null;
        }
    })();
    return _favoritesSyncInFlight;
}

async function _saveFavoritesToServer(favs) {
    const payload = _normalizeFavoriteCollection([...favs]);
    try {
        await fetch('/telescope/files/favorites', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ favorites: payload }),
        });
    } catch (_) {
        // localStorage remains the fallback source of truth when offline.
    }
}

function getFavorites() {
    return new Set(_getFavoriteCache());
}
function saveFavorites(favs) {
    const normalized = _normalizeFavoriteCollection([...favs]);
    _favoritesCache = new Set(normalized);
    localStorage.setItem(_FAVORITES_KEY, JSON.stringify(normalized));
    _saveFavoritesToServer(_favoritesCache);
}
function _favoriteTargetsFromContext(path, event) {
    const el = event && event.currentTarget;
    if (el && el.closest('.file-item') && gridSelection.selected.size > 0 && gridSelection.selected.has(path)) {
        return [...gridSelection.selected];
    }
    if (el && el.closest('.filmstrip-item') && filmstripSelection.selected.size > 0 && filmstripSelection.selected.has(path)) {
        return [...filmstripSelection.selected];
    }
    return [path];
}
function _setFavoriteForPaths(paths, shouldFavorite) {
    if (!Array.isArray(paths) || paths.length === 0) return;
    const favs = getFavorites();
    for (const p of paths) {
        if (shouldFavorite) favs.add(p);
        else favs.delete(p);
    }
    saveFavorites(favs);
    for (const p of paths) {
        const isFav = favs.has(p);
        document.querySelectorAll(`[data-fav-path="${CSS.escape(p)}"]`).forEach(btn => {
            btn.textContent = isFav ? '❤️' : '🤍';
            btn.title = isFav ? 'Unfavorite' : 'Favorite';
        });
        _updateDeleteBtnState(p, isFav);
        _updateRenameBtnState(p, isFav);
    }
}
function toggleFavorite(path, event) {
    if (event) event.stopPropagation();
    const targets = _favoriteTargetsFromContext(path, event);
    const favs = getFavorites();
    const allFav = targets.every(p => favs.has(p));
    const shouldFavorite = !allFav;
    _setFavoriteForPaths(targets, shouldFavorite);
    if (targets.length > 1) {
        showStatus(`${shouldFavorite ? 'Favorited' : 'Unfavorited'} ${targets.length} files`, 'success', 2000);
    }
}

function _updateDeleteBtnState(path, isFav) {
    // Filmstrip + grid thumbnail delete buttons
    document.querySelectorAll(`[data-fav-path="${CSS.escape(path)}"]`).forEach(favBtn => {
        const actions = favBtn.closest('.filmstrip-actions, .file-actions');
        if (!actions) return;
        const delBtn = actions.querySelector('.btn-danger');
        if (!delBtn) return;
        delBtn.disabled = isFav;
        delBtn.title = isFav ? 'Remove favorite first' : 'Delete';
    });
    // Viewer delete button
    const viewerFavBtn = document.getElementById('viewerFavBtn');
    const viewerPath = viewerFavBtn ? viewerFavBtn.dataset.favPath : '';
    const viewerDelBtn = document.getElementById('viewerDeleteBtn');
    if (viewerDelBtn && viewerPath === path) {
        viewerDelBtn.disabled = isFav;
        viewerDelBtn.title = isFav ? 'Remove favorite first' : 'Delete (⌘/Ctrl+click to skip confirm)';
    }
    // Viewer fav button text
    if (viewerFavBtn && viewerPath === path) {
        viewerFavBtn.textContent = isFav ? '❤️' : '🤍';
        viewerFavBtn.title = isFav ? 'Unfavorite' : 'Favorite';
    }
}

function _updateRenameBtnState(path, isFav) {
    // Rename buttons are always enabled (no longer gated on favorites)
}
let zoomStep = 0.1;
let _previewLastError = 0; // timestamp of last preview onerror (ms)
const _PREVIEW_BACKOFF_MS = 5000; // 5s between retry attempts after stream failure
let _previewCheckTimer = null;
let _lastPreviewRefreshMs = 0;
const _PREVIEW_REFRESH_INTERVAL_MS = Infinity; // never restart a healthy stream — onerror handles failures
let isSimulating = false;
let simulationVideo = null;
let disconnectedPollCount = 0; // consecutive disconnected polls before stopping preview
let simulationFiles = []; // Track temporary simulation files
let filmstripFiles = []; // current files rendered in the horizontal filmstrip
let _galleryMode = 'video'; // 'video' | 'diff' | 'trigger'
let _lastDiscoveredSeestarIp = null; // last discovered scope IP
const _refreshFilesTimers = new Map();

const filmstripSelection = {
    selected: new Set(),   // set of file paths selected in filmstrip
    lastClicked: null,     // index in filmstripFiles for shift-range selection
};

// Detection state
let isDetecting = false;
let detectionPollInterval = null;
let detectionStats = { fps: 0, detections: 0, elapsed_seconds: 0 };
let _ctrlState = 'idle';

// Experimental Sun centering state
let sunCenterPollInterval = null;
let sunCenterStatus = null;
let _sunCenterApiUnavailable = false;
let _sunCenterApiWarningShown = false;
let _sunCenterSelectedSearchMode = (() => {
    try {
        const v = localStorage.getItem('sunCenterSearchMode');
        return ['adaptive', 'spiral', 'raster', 'random_walk'].includes(v) ? v : 'adaptive';
    } catch (_) {
        return 'adaptive';
    }
})();
let _sunCenterSearchModeDirty = false;

// Eclipse state
let eclipseData = null;         // populated from /telescope/status
let eclipseAlertLevel = null;   // 'outlook'|'watch'|'warning'|'active'|'cleared'|null
let _eclipseRecordingScheduled = false; // prevents duplicate setTimeout during warning phase
let eclipseBannerDismissed = false; // per-session dismiss flag
let currentViewingMode = null;  // 'sun'|'moon'|null — last known scope viewing mode
let _mismatchDismissedFor = null; // which opposite target the user dismissed

function _normalizeViewingMode(mode) {
    const m = String(mode || '').trim().toLowerCase();
    if (!m) return null;
    if (m === 'solar') return 'sun';
    if (m === 'lunar') return 'moon';
    if (m === 'scene' || m === 'landscape') return 'scenery';
    return m;
}

window.initTelescope = function() {
    console.log('[Telescope] Initializing interface');
    destroyTelescope(); // clear any existing intervals
    ensureHarnessUI();
    ensureTuningUI();
    syncAlpacaPollSliderFromServer();
    ensureTransitRadar();
    ensureDetectionEventHistoryPanel().catch(() => {});

    // Status polling (always poll while panel is open)
    statusPollInterval = setInterval(updateStatus, 2000);

    // Check current state — do NOT auto-connect (UDP floods when scope is off).
    // User must click Connect or Find explicitly.
    updateStatus().then(async () => {
        // Check if detection is already running (page reload) — if so just hook up polling
        const statusResult = await apiCall('/telescope/detect/status', 'GET');
        if (statusResult && statusResult.running) {
            isDetecting = true;
            if (!detectionPollInterval) {
                detectionPollInterval = setInterval(pollDetectionStatus, 2000);
            }
            updateDetectionUI();
        } else if (!isDetecting) {
            await startDetection();
        }
    });

    // Sync mute button state from server
    apiCall('/telescope/notifications/status', 'GET').then(r => {
        if (r) updateTelegramMuteBtn(r.muted);
    });

    // Initialise focus step keycap — mark the default size as active
    setFocusStepSize(_focusStepSize);

    // Start polling for target visibility
    updateTargetVisibility();
    visibilityPollInterval = setInterval(updateTargetVisibility, 30000);

    // Update "last updated" timer
    lastUpdateInterval = setInterval(updateLastUpdateTime, 1000);

    // Load initial file list
    refreshFiles();

    // Start transit polling
    checkTransits();
    transitPollInterval = setInterval(checkTransits, 15000);

    // Check if timelapse is already running (e.g. after page reload)
    _pollTimelapseStatus();

    // 1-second local tick
    transitTickInterval = setInterval(() => {
        if (upcomingTransits.length > 0) {
            upcomingTransits.forEach(t => t.seconds_until--);
            updateTransitList();
            checkAutoCapture();
        }
        updateEclipseState();
        // Tick timelapse countdown locally so it moves every second, not just on poll
        if (_timelapseRunning) {
            if (_timelapseNextIn > 0) _timelapseNextIn--;
            _renderTimelapseCountdown(document.getElementById('timelapseInfo'));
        }
    }, 1000);

    // Load auto-capture preference
    const autoCapture = localStorage.getItem('autoCaptureTransits');
    if (autoCapture !== null) {
        const isOn = autoCapture === 'true';
        document.getElementById('autoCaptureToggle').checked = isOn;
        const autoCaptureBtn = document.getElementById('autoCaptureBtn');
        if (autoCaptureBtn) {
            autoCaptureBtn.classList.toggle('is-active', isOn);
        }
    }

    // Mouse-wheel zoom on preview container
    const previewContainer = document.getElementById('previewContainer');
    if (previewContainer) {
        previewContainer.addEventListener('wheel', (e) => {
            e.preventDefault();
            const delta = e.deltaY < 0 ? zoomStep : -zoomStep;
            currentZoom = Math.min(4.0, Math.max(0.5, +(currentZoom + delta).toFixed(2)));
            applyZoom();
        }, { passive: false });
    }

    // Load saved detection preference and sync UI
    syncDetectionUI();

    // Sun centering status poll (experimental)
    _sunCenterApiUnavailable = false;
    _sunCenterApiWarningShown = false;
    if (sunCenterPollInterval) {
        clearInterval(sunCenterPollInterval);
        sunCenterPollInterval = null;
    }
    pollSunCenterStatus();
    sunCenterPollInterval = setInterval(pollSunCenterStatus, 1500);

    // Initialize Control Panel (GoTo, named locations)
    initControlPanel();
};

// ============================================================================
// CONNECTION MANAGEMENT
// ============================================================================

async function connect() {
    console.log('[Telescope] Connecting...');
    const btn = document.getElementById('connectBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Connecting…'; }
    showStatus('Connecting to telescope (3 attempts with backoff)…', 'info', 0);

    const result = await apiCall('/telescope/connect', 'POST');
    if (btn) { btn.disabled = false; btn.textContent = 'Connect'; }

    if (result && result.success) {
        isConnected = true;
        showStatus('Connected successfully!', 'success', 5000);

        // Eagerly resolve ALPACA state so nudge uses the correct path immediately
        await pollAlpacaTelemetry();
        syncAlpacaPollSliderFromServer();

        // Start status polling
        if (statusPollInterval) clearInterval(statusPollInterval);
        statusPollInterval = setInterval(updateStatus, 2000);

        updateConnectionUI();
        updateStatus();
        startPositionSync();
        _previewLastError = 0;
        stopPreview();
        setTimeout(startPreview, 2000);
    } else {
        showStatus('Scope not found — power it on and click Find', 'warning', 10000);
        updateConnectionUI();
    }
}

async function findSeestar() {
    const btn = document.getElementById('findSeestarBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Scanning…'; }
    showStatus('Scanning network for Seestar…', 'info', 0);
    try {
        const resp = await fetch('/telescope/discover');
        const data = await resp.json();
        if (btn) { btn.disabled = false; btn.textContent = 'Find'; }
        if (!data.found || data.found.length === 0) {
            showStatus('No Seestar found on ' + data.subnet + ' — is it powered on?', 'warning', 8000);
            return;
        }
        const ip = data.found[0];
        _lastDiscoveredSeestarIp = ip;
        const others = data.found.slice(1);
        let msg = `Found Seestar at ${ip}`;
        if (others.length) msg += ` (also: ${others.join(', ')})`;
        msg += ` — update SEESTAR_HOST in .env and restart`;
        showStatus(msg, 'success', 12000);
        console.log('[Discover]', msg, '— found:', data.found);

    } catch (err) {
        if (btn) { btn.disabled = false; btn.textContent = 'Find'; }
        showStatus('Scan error: ' + err.message, 'error', 6000);
    }
}

async function disconnect() {
    console.log('[Telescope] Disconnecting...');
    showStatus('Disconnecting...', 'info');
    
    const result = await apiCall('/telescope/disconnect', 'POST');
    if (result && result.success) {
        isConnected = false;
        isRecording = false;
        showStatus('Disconnected', 'info');

        // Stop polling
        if (statusPollInterval) {
            clearInterval(statusPollInterval);
            statusPollInterval = null;
        }

        stopPositionSync();
        updateConnectionUI();
        stopPreview();
    }
}

function updateConnectionUI() {
    // Update connection status display
    const statusDot = document.getElementById('statusDot');
    const statusText = document.getElementById('connectionStatus');
    const connectBtn = document.getElementById('connectBtn');
    const disconnectBtn = document.getElementById('disconnectBtn');
    
    if (!statusDot || !statusText) {
        console.warn('[Telescope] Connection UI elements not found');
        return;
    }
    
    if (isConnected) {
        statusDot.className = 'status-dot connected';
        statusText.textContent = 'Connected';
        if (connectBtn) connectBtn.disabled = true;
        if (disconnectBtn) disconnectBtn.disabled = false;
    } else {
        statusDot.className = 'status-dot disconnected';
        statusText.textContent = 'Disconnected';
        if (connectBtn) connectBtn.disabled = false;
        if (disconnectBtn) disconnectBtn.disabled = true;
    }
    
    // Enable/disable all controls based on connection
    updateButtonStates();
}

function updateButtonStates() {
    // Update all buttons that require connection
    const buttons = [
        'targetSunBtn',
        'targetMoonBtn', 
        'capturePhotoBtn',
        'startRecordingBtn',
        'stopRecordingBtn',
        'refreshFilesBtn',
        'startTimelapseBtn',
        'sunCenterStartBtn',
        'sunCenterStopBtn',
        'sunCenterRecenterBtn',
        'sunCenterApplyModeBtn'
    ];
    
    buttons.forEach(id => {
        const btn = document.getElementById(id);
        if (btn && !btn.classList.contains('force-enabled')) {
            btn.disabled = !isConnected;
        }
    });
}

/**
 * Sync Solar/Lunar/Scene mode button keycap state from currentViewingMode.
 * Applies .is-active to the active mode button and removes it from the others.
 * Called on every status poll and immediately after a mode switch.
 */
function updateModeButtons() {
    const map = {
        sun:  'modeSolarBtn',
        moon: 'modeLunarBtn',
        scenery: 'modeSceneBtn',
    };
    Object.entries(map).forEach(([mode, id]) => {
        const btn = document.getElementById(id);
        if (btn) btn.classList.toggle('is-active', currentViewingMode === mode);
    });
}

async function updateStatus() {
    if (isSimulating) return; // sim owns connection state — don't let real status poll overwrite it
    const result = await apiCall('/telescope/status', 'GET');
    if (result) {
        isConnected = result.connected || false;
        const serverMode = _normalizeViewingMode(result.viewing_mode);
        if (serverMode) {
            currentViewingMode = serverMode;
        } else if (!isConnected) {
            currentViewingMode = null;
        }
        _ctrlState = result.ctrl_state || 'idle';
        // Sync recording state from server — server is authoritative.
        // Only preserve local state if we're mid-recording AND server agrees it's active.
        const serverRecording = result.recording || false;
        if (isRecording && !serverRecording) {
            // Server says not recording but JS thinks it is — stale state, sync it
            isRecording = false;
            stopRecordingTimer();
        } else if (!isRecording) {
            isRecording = serverRecording;
        }

        // Update eclipse data from server (refreshes seconds_to_c1 baseline).
        // Don't clobber an active sim eclipse with a null server response —
        // real eclipse data always wins, but sim data is preserved when server has none.
        if (result.eclipse !== null && result.eclipse !== undefined) {
            eclipseData = result.eclipse;  // real eclipse — always take it
        } else if (!_simEclipseActive) {
            eclipseData = result.eclipse;  // no sim running — clear if server says null
        }
        // else: sim is active and server has no real eclipse — keep sim eclipseData

        updateConnectionUI();
        updateRecordingUI();
        updateModeButtons();    // sync Solar/Lunar/Scene mode keycap state
        checkTargetMismatch();
        _renderSunCenterStatus(sunCenterStatus || { running: false, state: 'idle' });
        
        // Auto-start preview if connected; stop stale stream if disconnected
        const justReconnected = isConnected && !_prevConnected;
        const justDisconnected = !isConnected && _prevConnected;
        if (justReconnected) {
            console.log('[Scope] Reconnected — mode:', result.viewing_mode);
            _previewLastError = 0;
            _lastConnectedStatus = null;
            startPositionSync();
        }
        if (justDisconnected) {
            console.warn('[Scope] Disconnected — prior connected state:', JSON.stringify(_lastConnectedStatus || {}));
            if (result.error) console.warn('[Scope] Server error:', result.error);
            upcomingTransits = [];
            updateTransitList();
            stopPositionSync();
        }
        // Cache last connected response for diagnostics
        if (isConnected) {
            _lastConnectedStatus = { viewing_mode: result.viewing_mode, recording: result.recording, host: result.host };
        }

        // Update focus odometer (server polls get_focuser_position periodically)
        const focEl = document.getElementById('focusPos');
        const fplEl = document.getElementById('focusPosLabel');
        if (!isConnected && focEl) {
            focEl.textContent = '—';
            if (fplEl) fplEl.textContent = 'Focus Position';
        } else if (focEl) {
            let _fp = result.focus_pos;
            if (typeof _fp === 'bigint') _fp = Number(_fp);
            const n = _fp == null || _fp === '' ? NaN : Number(_fp);
            if (fplEl) fplEl.textContent = 'Focus Position';
            if (Number.isFinite(n)) {
                focEl.textContent = String(Math.round(n));
                if (!result.mock_mode && result.focus_pos_source === 'relative') {
                    focEl.title =
                        'Estimated from relative focus moves (absolute readback unavailable)';
                } else {
                    focEl.removeAttribute('title');
                }
            } else {
                focEl.textContent = '—';
                focEl.removeAttribute('title');
            }
        }

        // Sync camera gain readout/slider with backend state.
        const gainSliderEl = document.getElementById('gainSlider');
        const gainValueEl = document.getElementById('gainVal');
        if (gainSliderEl && gainValueEl) {
            const toNum = (v) => {
                if (v === undefined || v === null) return null;
                if (typeof v === 'string' && v.trim() === '') return null;
                const n = Number(v);
                return Number.isFinite(n) ? n : null;
            };
            const g = toNum(result.camera_gain);
            const gMin = toNum(result.camera_gain_min);
            const gMax = toNum(result.camera_gain_max);
            const parsedMin = parseInt(gainSliderEl.min || '0', 10);
            const parsedMax = parseInt(gainSliderEl.max || '120', 10);
            const sliderMin = gMin != null ? Math.round(gMin) : (Number.isFinite(parsedMin) ? parsedMin : 0);
            const sliderMax = gMax != null ? Math.round(gMax) : (Number.isFinite(parsedMax) ? parsedMax : 120);
            if (Number.isFinite(sliderMin)) gainSliderEl.min = String(sliderMin);
            if (Number.isFinite(sliderMax)) gainSliderEl.max = String(sliderMax);
            if (g != null) {
                const clamped = Math.min(sliderMax, Math.max(sliderMin, Math.round(g)));
                gainSliderEl.value = String(clamped);
                gainValueEl.textContent = String(clamped);
            }
        }
        _prevConnected = isConnected;

        if (isConnected && typeof startPreview === 'function') {
            disconnectedPollCount = 0;
            _previewLastError = 0; // clear backoff so preview retries
            startPreview();
        } else if (!isConnected && typeof stopPreview === 'function') {
            disconnectedPollCount++;
            if (disconnectedPollCount >= 3) {
                stopPreview();
            }
        }
        
        // Update last update timestamp
        if (result.last_update) {
            const timestamp = document.getElementById('lastUpdate');
            if (timestamp) {
                const date = new Date(result.last_update);
                timestamp.dataset.lastUpdate = date.getTime();
            }
        }
    }
}

function updateLastUpdateTime() {
    const timestamp = document.getElementById('lastUpdate');
    if (!timestamp || !timestamp.dataset.lastUpdate) return;
    
    const lastUpdate = parseInt(timestamp.dataset.lastUpdate);
    const now = Date.now();
    const seconds = Math.floor((now - lastUpdate) / 1000);
    
    if (seconds < 60) {
        timestamp.textContent = `${seconds}s ago`;
    } else if (seconds < 3600) {
        timestamp.textContent = `${Math.floor(seconds / 60)}m ago`;
    } else {
        timestamp.textContent = `${Math.floor(seconds / 3600)}h ago`;
    }
}

// ============================================================================
// TARGET SELECTION & VISIBILITY
// ============================================================================

async function updateTargetVisibility() {
    console.log('[Telescope] Updating target visibility');
    
    const result = await apiCall('/telescope/target/visibility', 'GET');
    if (!result) return;
    
    // Update Sun
    const sunBadge = document.getElementById('sunBadge');
    const sunCoords = document.getElementById('sunCoords');
    const sunBtn = document.getElementById('targetSunBtn');
    
    if (result.sun && sunCoords && sunBadge) {
        sunCoords.textContent = `Alt: ${result.sun.altitude.toFixed(1)}° / Az: ${result.sun.azimuth.toFixed(1)}°`;
        
        if (result.sun.visible) {
            sunBadge.textContent = 'Visible';
            sunBadge.className = 'visibility-badge visible';
            if (isConnected && sunBtn) sunBtn.disabled = false;
        } else {
            sunBadge.textContent = 'Below';
            sunBadge.className = 'visibility-badge not-visible';
            if (sunBtn) sunBtn.disabled = true;
        }
    }
    
    // Update Moon
    const moonBadge = document.getElementById('moonBadge');
    const moonCoords = document.getElementById('moonCoords');
    const moonBtn = document.getElementById('targetMoonBtn');
    
    if (result.moon && moonCoords && moonBadge) {
        moonCoords.textContent = `Alt: ${result.moon.altitude.toFixed(1)}° / Az: ${result.moon.azimuth.toFixed(1)}°`;
        
        if (result.moon.visible) {
            moonBadge.textContent = 'Visible';
            moonBadge.className = 'visibility-badge visible';
            if (isConnected && moonBtn) moonBtn.disabled = false;
        } else {
            moonBadge.textContent = 'Below';
            moonBadge.className = 'visibility-badge not-visible';
            if (moonBtn) moonBtn.disabled = true;
        }
    }
}

async function switchToSun() {
    console.log('[Telescope] Switching to Sun');
    showStatus('Switching to Solar mode...', 'info');

    const result = await apiCall('/telescope/target/sun', 'POST');
    if (result && result.success) {
        showStatus('Switched to Solar mode', 'success', 5000);
        currentViewingMode = 'sun';
        updateModeButtons();    // immediately light Solar keycap
        stopPreview();
        _previewLastError = 0;
        setTimeout(startPreview, 3000);
        showWarning(
            '⚠️ SOLAR FILTER REQUIRED - Ensure solar filter is installed before viewing!',
            'warning',
            10000
        );
    }
}

async function switchToMoon() {
    console.log('[Telescope] Switching to Moon');
    showStatus('Switching to Lunar mode...', 'info');

    const result = await apiCall('/telescope/target/moon', 'POST');
    if (result && result.success) {
        showStatus('Switched to Lunar mode', 'success', 5000);
        currentViewingMode = 'moon';
        updateModeButtons();    // immediately light Lunar keycap
        stopPreview();
        _previewLastError = 0;
        setTimeout(startPreview, 3000);
        showWarning(
            '✓ Remove solar filter if installed - Lunar viewing safe without filter',
            'info',
            10000
        );
    }
}

async function switchToScenery() {
    console.log('[Telescope] Switching to Scenery');
    showStatus('Switching to Scenery mode...', 'info');

    const result = await apiCall('/telescope/mode/scenery', 'POST');
    if (result && result.success) {
        showStatus('Scenery mode active — no tracking, manual positioning enabled', 'success', 5000);
        currentViewingMode = 'scenery';
        updateModeButtons();    // immediately light Scene keycap
        stopPreview();
        _previewLastError = 0;
        setTimeout(startPreview, 3000);
    }
}

function checkTargetMismatch() {
    const banner = document.getElementById('mismatchBanner');
    const text   = document.getElementById('mismatchText');
    const btn    = document.getElementById('mismatchSwitchBtn');
    if (!banner || !text || !btn) return;

    // Only relevant when connected and in sun or moon viewing mode
    // Scenery mode is target-neutral — no mismatch possible
    if (!isConnected || !currentViewingMode || (currentViewingMode !== 'sun' && currentViewingMode !== 'moon')) { banner.style.display = 'none'; return; }

    const flights = (window.lastFlightData && window.lastFlightData.flights) || [];
    const oppositeTarget = currentViewingMode === 'sun' ? 'moon' : 'sun';

    // Find the soonest HIGH or MEDIUM transit on the opposite target
    const best = flights
        .filter(f => f.target === oppositeTarget
                  && f.is_possible_transit === 1
                  && parseInt(f.possibility_level) >= 2   // MEDIUM or HIGH
                  && f.time != null && f.time > 0)
        .sort((a, b) => a.time - b.time)[0];

    if (!best) {
        // Mismatch cleared — reset dismiss state so future mismatches show
        _mismatchDismissedFor = null;
        banner.style.display = 'none';
        return;
    }

    // User already dismissed this mismatch — don't re-show
    if (_mismatchDismissedFor === oppositeTarget) return;

    const targetLabel = oppositeTarget === 'sun' ? '☀️ Sun' : '🌙 Moon';
    const scopeLabel  = currentViewingMode  === 'sun' ? '☀️ Solar' : '🌙 Lunar';
    const eta = best.time < 1 ? '<1' : best.time.toFixed(1);
    const level = parseInt(best.possibility_level) === 3 ? 'HIGH' : 'MEDIUM';

    text.textContent = `⚠️ ${level} probability ${targetLabel} transit in ${eta} min — scope is in ${scopeLabel} mode`;
    btn.textContent  = `Switch to ${oppositeTarget === 'sun' ? 'Solar ☀️' : 'Lunar 🌙'}`;
    btn.onclick = oppositeTarget === 'sun' ? switchToSun : switchToMoon;
    banner.style.display = 'flex';
}

function dismissMismatchBanner() {
    _mismatchDismissedFor = currentViewingMode === 'sun' ? 'moon' : 'sun';
    document.getElementById('mismatchBanner').style.display = 'none';
}

// ============================================================================
// CAPTURE CONTROLS
// ============================================================================

async function capturePhoto() {
    console.log('[Telescope] Capturing photo');
    
    showStatus('Capturing photo...', 'info');
    
    // Handle simulation mode
    if (isSimulating) {
        simulateCapturePhoto();
        return;
    }
    
    const result = await apiCall('/telescope/capture/photo', 'POST', {});
    
    if (result && result.success) {
        showStatus('Photo captured successfully!', 'success', 5000);
        
        // Retry refresh so delayed disk writes still show up in the filmstrip.
        scheduleRefreshFiles([0, 2000, 5000, 9000]);
    }
}

// Recording state
let recordingStartTime = null;
let recordingEndTime = null;   // absolute ms timestamp — can be extended for overlapping transits
// True when current recording was triggered by a real transit/eclipse (not sim demo).
// Real transits always outrank sim recordings: if a real transit arrives while a
// sim recording is running, the sim is stopped and the real transit recorded instead.
let recordingIsReal = false;
let recordingTimerInterval = null;

async function startRecording() {
    console.log('[Telescope] Starting recording');
    showStatus('Starting recording...', 'info');
    
    const durationInput = document.getElementById('videoDuration');
    const intervalInput = document.getElementById('frameInterval');
    
    const duration = durationInput ? parseInt(durationInput.value) : 30;
    const interval = intervalInput ? parseFloat(intervalInput.value) : 0;
    
    // Handle simulation mode
    if (isSimulating) {
        simulateStartRecording(duration, interval);
        return;
    }
    
    const result = await apiCall('/telescope/recording/start', 'POST', {
        duration: duration,
        interval: interval
    });
    
    if (result && result.success) {
        isRecording = true;
        recordingIsReal = true;    // real hardware recording
        recordingStartTime = Date.now();
        updateRecordingUI();
        startRecordingTimer(duration);
        
        const mode = interval > 0 ? `timelapse (${interval}s interval)` : 'normal';
        showStatus(`Recording started (${duration}s ${mode})`, 'success', 5000);
    }
}

async function stopRecording() {
    console.log('[Telescope] Stopping recording');
    // Immediately mark as not recording so concurrent calls (e.g. timer + recordTransit)
    // don't both try to stop an already-stopped recording.
    if (!isRecording && !isSimulating) return;
    isRecording = false;
    stopRecordingTimer();
    updateRecordingUI();
    showStatus('Stopping recording...', 'info');
    
    // Handle simulation mode
    if (isSimulating) {
        simulateStopRecording();
        return;
    }
    
    const result = await apiCall('/telescope/recording/stop', 'POST');
    if (result && result.success) {
        showStatus('Recording stopped', 'success', 5000);
        // Retry refresh so delayed file finalization still shows up in the filmstrip.
        scheduleRefreshFiles([0, 2000, 5000, 9000]);
    } else {
        // Backend already stopped (400) — state already reset above, just warn quietly
        showStatus('⚠️ Recording already stopped', 'warning', 3000);
    }
}

function startRecordingTimer(totalDuration) {
    const timerSpan = document.getElementById('recordingTimer');
    if (!timerSpan) return;

    // Clear any existing timer to avoid ghost intervals calling stopRecording twice
    if (recordingTimerInterval) {
        clearInterval(recordingTimerInterval);
        recordingTimerInterval = null;
    }

    recordingEndTime = Date.now() + totalDuration * 1000;

    recordingTimerInterval = setInterval(async () => {
        if (!recordingStartTime || !recordingEndTime) return;

        const elapsed   = (Date.now() - recordingStartTime) / 1000;
        const remaining = Math.max(0, (recordingEndTime - Date.now()) / 1000);
        const total     = elapsed + remaining;

        timerSpan.textContent = `${elapsed.toFixed(1)}s / ${total.toFixed(0)}s`;

        if (remaining <= 0 && isRecording) {
            await stopRecording();
        }
    }, 100);
}

/** Extend an active recording so it ends no sooner than newEndMs */
function extendRecording(newEndMs) {
    if (recordingEndTime === null) return;
    if (newEndMs > recordingEndTime) {
        recordingEndTime = newEndMs;
        console.log(`[Recording] Extended end time by ${((newEndMs - Date.now()) / 1000).toFixed(0)}s`);
    }
}

function stopRecordingTimer() {
    if (recordingTimerInterval) {
        clearInterval(recordingTimerInterval);
        recordingTimerInterval = null;
    }
    recordingStartTime = null;
    recordingEndTime = null;
    recordingIsReal = false;

    const timerSpan = document.getElementById('recordingTimer');
    if (timerSpan) timerSpan.textContent = '';
}

function updateRecordingUI() {
    const startBtn = document.getElementById('startRecordingBtn');
    const stopBtn = document.getElementById('stopRecordingBtn');
    const recordingDot = document.getElementById('recordingDot');
    const recordingText = document.getElementById('recordingText');
    
    if (isRecording) {
        if (startBtn) {
            startBtn.disabled = true;
            startBtn.style.display = 'none';
        }
        if (stopBtn) {
            stopBtn.disabled = false;
            stopBtn.style.display = 'inline-block';
        }
        if (recordingDot) recordingDot.className = 'status-dot recording';
        if (recordingText) recordingText.textContent = 'Recording...';
    } else if (transitCaptureActive) {
        // Auto-capture is armed/waiting — show pending recording state on the button
        if (startBtn) {
            startBtn.disabled = true;
            startBtn.style.display = 'none';
        }
        if (stopBtn) {
            stopBtn.disabled = true;
            stopBtn.style.display = 'inline-block';
        }
        if (recordingDot) recordingDot.className = 'status-dot recording';
        if (recordingText) recordingText.textContent = 'Capture Armed...';
    } else {
        if (startBtn) {
            startBtn.disabled = !isConnected;
            startBtn.style.display = 'inline-block';  // Use inline-block instead of block
        }
        if (stopBtn) {
            stopBtn.disabled = true;
            stopBtn.style.display = 'none';
        }
        if (recordingDot) recordingDot.className = 'status-dot';
        if (recordingText) recordingText.textContent = 'Not Recording';
    }
}

// ============================================================================
// SOLAR TIMELAPSE
// ============================================================================

let _timelapseRunning = false;
let _timelapsePollInterval = null;
let _timelapseNextIn = 0;       // local countdown ticked down each second
let _timelapseLastError = null; // cached for local tick display
let _timelapseFailures = 0;     // cached for local tick display
let _timelapseInfoPrefix = '';  // frames · span prefix, refreshed on poll

async function startTimelapse() {
    const intervalInput = document.getElementById('timelapseInterval');
    const interval = intervalInput ? parseFloat(intervalInput.value) : 120;
    showStatus('Starting timelapse...', 'info');

    const result = await apiCall('/telescope/timelapse/start', 'POST', { interval });
    if (result && !result.error) {
        _timelapseRunning = true;
        updateTimelapseUI();
        _startTimelapsePoll();
        showStatus(`Timelapse started (${interval}s interval)`, 'success', 5000);
    } else {
        showStatus(result?.error || 'Failed to start timelapse', 'error', 5000);
    }
}

async function stopTimelapse() {
    showStatus('Stopping timelapse & assembling video...', 'info');
    const result = await apiCall('/telescope/timelapse/stop', 'POST');
    if (result && !result.error) {
        _timelapseRunning = false;
        updateTimelapseUI();
        _stopTimelapsePoll();
        showStatus('Timelapse stopped — assembling video', 'success', 5000);
        // Timelapse assembly may finish late; retry refresh until it appears.
        scheduleRefreshFiles([0, 5000, 10000, 15000]);
    }
}

async function previewTimelapse() {
    const btn = document.getElementById('previewTimelapseBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Building...'; }
    showStatus('Building timelapse preview...', 'info');

    const result = await apiCall('/telescope/timelapse/preview', 'POST');
    if (btn) { btn.disabled = false; btn.textContent = '👁️ Preview'; }

    if (result && result.url) {
        showStatus('Preview ready', 'success', 3000);
        // Open in the file viewer if available, otherwise new tab
        const viewerBody = document.getElementById('fileViewerBody');
        if (viewerBody) {
            const viewer = document.getElementById('fileViewer');
            const viewerName = document.getElementById('fileViewerName');
            viewerBody.innerHTML = `<video src="${result.url}" controls autoplay style="max-width:100%;max-height:80vh;"></video>`;
            if (viewerName) viewerName.textContent = 'Timelapse Preview (so far)';
            if (viewer) viewer.style.display = 'flex';
        } else {
            window.open(result.url);
        }
    } else {
        showStatus(result?.error || 'Preview failed', 'error', 5000);
    }
}

function openTimelapseFrame(src) {
    // Strip cache-buster before opening so the URL stays clean
    const cleanSrc = src.split('?')[0];
    const viewer = document.getElementById('fileViewer');
    const body   = document.getElementById('fileViewerBody');
    const name   = document.getElementById('fileViewerName');
    if (viewer && body) {
        body.innerHTML = `<img src="${cleanSrc}" style="max-width:100%; max-height:85vh; display:block; margin:auto;" alt="Timelapse frame">`;
        if (name) name.textContent = cleanSrc.split('/').pop();
        viewer.style.display = 'flex';
    } else {
        window.open(cleanSrc);
    }
}

function _startTimelapsePoll() {
    _stopTimelapsePoll();
    _timelapsePollInterval = setInterval(_pollTimelapseStatus, 5000);
}

function _stopTimelapsePoll() {
    if (_timelapsePollInterval) {
        clearInterval(_timelapsePollInterval);
        _timelapsePollInterval = null;
    }
}

async function _pollTimelapseStatus() {
    try {
        const resp = await fetch('/telescope/timelapse/status');
        if (!resp.ok) return;
        const data = await resp.json();
        _timelapseRunning = data.running;
        updateTimelapseUI(data);
        if (data.running && !_timelapsePollInterval) {
            // Ensure recurring poll is running (e.g. after page reload with timelapse active)
            _startTimelapsePoll();
        } else if (!data.running) {
            _stopTimelapsePoll();
        }
    } catch (e) { /* ignore */ }
}

function _renderTimelapseCountdown(infoEl) {
    if (!infoEl) return;
    const el = infoEl || document.getElementById('timelapseInfo');
    if (!el) return;
    const nextIn = Math.max(0, _timelapseNextIn);
    if (_timelapseLastError && _timelapseFailures > 0) {
        el.textContent = `⚠️ ${_timelapseLastError} · next in ${nextIn}s`;
    } else {
        el.textContent = `${_timelapseInfoPrefix} · next in ${nextIn}s`;
    }
}

function updateTimelapseUI(data) {
    const startBtn = document.getElementById('startTimelapseBtn');
    const stopBtn = document.getElementById('stopTimelapseBtn');
    const previewBtn = document.getElementById('previewTimelapseBtn');
    const dot = document.getElementById('timelapseDot');
    const text = document.getElementById('timelapseText');
    const info = document.getElementById('timelapseInfo');
    const thumbWrap = document.getElementById('timelapseThumb');
    const thumbImg = document.getElementById('timelapseLatestFrame');

    if (_timelapseRunning) {
        if (startBtn) { startBtn.disabled = true; startBtn.style.display = 'none'; }
        if (stopBtn) { stopBtn.disabled = false; stopBtn.style.display = 'inline-block'; }
        
        let statusClass = 'status-dot recording';
        
        if (data) {
            const paused = data.paused ? ' (paused)' : '';
            let label = `Capturing${paused}`;
            
            if (data.consecutive_failures > 0) {
                // Show retry status immediately if failing
                statusClass = 'status-dot warning';
                label = `Retrying (${data.consecutive_failures})...`;
            }
            
            if (dot) dot.className = statusClass;
            if (text) text.textContent = label;

            const frames = data.frame_count || 0;
            if (info) {
                const span = data.capture_span_seconds ?? 0;
                const hrs = Math.floor(span / 3600);
                const mins = Math.floor((span % 3600) / 60);
                _timelapseInfoPrefix = `${frames} frames · ${hrs}h${mins}m`;
                _timelapseNextIn = data.next_capture_in || 0;
                _timelapseLastError = data.last_error || null;
                _timelapseFailures = data.consecutive_failures || 0;
                _renderTimelapseCountdown(info);
            }
            // Show preview button once we have ≥2 frames
            if (previewBtn) {
                previewBtn.disabled = frames < 2;
                previewBtn.style.display = frames >= 2 ? 'inline-block' : 'none';
            }
            // Show latest frame thumbnail
            if (data.latest_frame && thumbWrap && thumbImg) {
                thumbImg.src = data.latest_frame + '?t=' + Date.now();
                thumbWrap.style.display = 'block';
            }
        } else {
            if (text) text.textContent = 'Running...';
        }
    } else {
        if (startBtn) { startBtn.disabled = !isConnected; startBtn.style.display = 'inline-block'; }
        if (stopBtn) { stopBtn.disabled = true; stopBtn.style.display = 'none'; }
        if (previewBtn) { previewBtn.disabled = true; previewBtn.style.display = 'none'; }
        if (dot) dot.className = 'status-dot';
        const frames = (data && data.frame_count) ? data.frame_count : 0;
        if (frames > 0) {
            if (text) text.textContent = data.resume_available ? 'Paused (resume available)' : 'Idle';
            if (info) {
                const span = data.capture_span_seconds ?? 0;
                const hrs = Math.floor(span / 3600);
                const mins = Math.floor((span % 3600) / 60);
                info.textContent = `${frames} frames accumulated · ${hrs}h${mins}m`;
            }
            if (data.latest_frame && thumbWrap && thumbImg) {
                thumbImg.src = data.latest_frame + '?t=' + Date.now();
                thumbWrap.style.display = 'block';
            } else if (thumbWrap) {
                thumbWrap.style.display = 'none';
            }
        } else {
            if (text) text.textContent = 'Idle';
            if (info) info.textContent = '';
            if (thumbWrap) thumbWrap.style.display = 'none';
        }
    }
}

// Apply interval and smoothing changes live
(function() {
    let _intervalDebounce = null;
    document.addEventListener('DOMContentLoaded', () => {
        const input = document.getElementById('timelapseInterval');
        if (input) {
            input.addEventListener('change', () => {
                if (!_timelapseRunning) return;
                clearTimeout(_intervalDebounce);
                _intervalDebounce = setTimeout(async () => {
                    const val = parseFloat(input.value);
                    if (val >= 10) {
                        await apiCall('/telescope/timelapse/settings', 'PATCH', { interval: val });
                        showStatus(`Timelapse interval → ${val}s`, 'success', 3000);
                    }
                }, 500);
            });
        }

        const smoothingSlider = document.getElementById('timelapseSmoothing');
        const smoothingVal = document.getElementById('timelapseSmoothingVal');
        if (smoothingSlider) {
            // Restore saved value
            const saved = localStorage.getItem('tl_smoothing');
            if (saved !== null) {
                smoothingSlider.value = saved;
                if (smoothingVal) smoothingVal.textContent = parseFloat(saved).toFixed(2);
            }
            smoothingSlider.addEventListener('input', () => {
                const val = parseFloat(smoothingSlider.value);
                if (smoothingVal) smoothingVal.textContent = val.toFixed(2);
                localStorage.setItem('tl_smoothing', val);
                if (_timelapseRunning) {
                    apiCall('/telescope/timelapse/settings', 'PATCH', { smoothing: val });
                }
            });
        }
    });
})();

// ============================================================================
// LIVE PREVIEW
// ============================================================================

function startPreview(forceRefresh = false) {
    const previewImage = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot = document.getElementById('previewStatusDot');
    const previewStatusText = document.getElementById('previewStatusText');
    const previewTitleIcon = document.getElementById('previewTitleIcon');
    
    if (!previewImage) {
        console.error('[Telescope] Preview image element not found');
        return;
    }

    // Keep stream alive without constantly restarting ffmpeg/RTSP.
    // Refresh immediately when forced (mode changes/reconnect), otherwise
    // only refresh periodically.
    if (!forceRefresh && previewImage.style.display === 'block' && previewImage.src) {
        if ((Date.now() - _lastPreviewRefreshMs) < _PREVIEW_REFRESH_INTERVAL_MS) {
            return;
        }
    }

    // Refresh stream source so preview can recover from stale sockets.
    if (_previewCheckTimer) {
        clearTimeout(_previewCheckTimer);
        _previewCheckTimer = null;
    }
    previewImage.removeAttribute('src');
    
    // Set stream URL (adds timestamp to avoid caching)
    const streamUrl = `/telescope/preview/stream.mjpg?t=${Date.now()}`;
    
    // Re-center once the first frame renders and natural dimensions are known
    previewImage.onload = () => { applyZoom(); previewImage.onload = null; };

    previewImage.src = streamUrl;
    _lastPreviewRefreshMs = Date.now();

    // Show image, hide placeholder
    previewImage.style.display = 'block';
    if (previewPlaceholder) {
        previewPlaceholder.style.display = 'none';
    }
    
    // Set status to connecting
    if (previewStatusDot) previewStatusDot.className = 'status-dot';
    if (previewStatusText) previewStatusText.textContent = 'Connecting...';
    if (previewTitleIcon) previewTitleIcon.textContent = '🟡';
    
    // Confirm stream is live by checking whether the <img> element has started
    // rendering frames (naturalWidth > 0).  We cannot use HEAD on the MJPEG URL —
    // MJPEG is a long-lived streaming response; HEAD either hangs or returns
    // non-OK even when the stream is fully working.
    const checkStream = (attempt = 1) => {
        const img = document.getElementById('previewImage');
        const live = img && img.naturalWidth > 0;
        if (live) {
            if (previewStatusDot) previewStatusDot.className = 'status-dot connected';
            if (previewStatusText) previewStatusText.textContent = 'Live Stream Active';
            if (previewTitleIcon) previewTitleIcon.textContent = '🟢';
            currentZoom = 2.0;
            applyZoom();
        } else if (attempt < 6) {
            // Frames may take a few seconds to arrive — keep polling
            _previewCheckTimer = setTimeout(() => checkStream(attempt + 1), 2000);
        } else {
            if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
            if (previewStatusText) previewStatusText.textContent = 'Stream unavailable';
            if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
        }
    };
    setTimeout(checkStream, 2000);

    // Error handler — reset state so the guard above doesn't block retries
    previewImage.onerror = () => {
        const backoffSecs = (_PREVIEW_BACKOFF_MS / 1000).toFixed(0);
        console.warn(`[Preview] Stream failed (scope connected=${isConnected}, backoff=${backoffSecs}s) — ${streamUrl}`);
        _previewLastError = Date.now();
        previewImage.removeAttribute('src'); // avoid empty-src triggering another error
        previewImage.style.display = 'none';
        if (previewPlaceholder) previewPlaceholder.style.display = 'flex';
        if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
        if (previewStatusText) previewStatusText.textContent = 'Stream unavailable';
        if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
    };
}

function zoomIn() {
    currentZoom = Math.min(+(currentZoom + zoomStep).toFixed(2), 4.0);
    applyZoom();
}

function zoomOut() {
    currentZoom = Math.max(+(currentZoom - zoomStep).toFixed(2), 0.5);
    applyZoom();
}

function zoomReset() {
    currentZoom = 2.0;
    applyZoom();
}

function zoomFit() {
    currentZoom = 2.0;
    applyZoom();
}

function setZoom(value) {
    currentZoom = Math.round(value) / 100;
    applyZoom();
}

function setZoomPreset(pct) {
    currentZoom = pct / 100;
    applyZoom();
    // Highlight active button
    const bar = document.getElementById('zoomBtnBar');
    if (bar) {
        bar.querySelectorAll('.zoom-preset').forEach(btn => {
            btn.classList.toggle('active', btn.textContent.trim() === pct + '%');
        });
    }
}

function updateSlider() {
    const slider = document.getElementById('zoomSlider');
    const percent = document.getElementById('zoomPercent');
    if (slider) slider.value = Math.round(currentZoom * 100);
    if (percent) percent.textContent = Math.round(currentZoom * 100) + '%';
    // Also update zoom preset button highlights
    const bar = document.getElementById('zoomBtnBar');
    if (bar) {
        const pct = Math.round(currentZoom * 100);
        bar.querySelectorAll('.zoom-preset').forEach(btn => {
            btn.classList.toggle('active', btn.textContent.trim() === pct + '%');
        });
    }
}

function _getActiveMediaElement() {
    const video = document.getElementById('simulationVideo');
    if (video && video.style.display !== 'none') return video;
    return document.getElementById('previewImage');
}

function applyZoom() {
    const container = document.getElementById('previewContainer');
    const el = _getActiveMediaElement();
    if (!container || !el || el.style.display === 'none') { updateSlider(); return; }

    const cw = container.clientWidth;
    const ch = container.clientHeight;
    if (!cw || !ch) { updateSlider(); return; }

    const nw = el.naturalWidth  || el.videoWidth  || 0;
    const nh = el.naturalHeight || el.videoHeight || 0;

    if (nw && nh) {
        // Letterbox fit: fill as much of the container as possible, no cropping.
        const fitScale = Math.min(cw / nw, ch / nh);
        el.style.width     = Math.round(nw * fitScale * currentZoom) + 'px';
        el.style.height    = Math.round(nh * fitScale * currentZoom) + 'px';
        el.style.maxWidth  = 'none';
        el.style.maxHeight = 'none';
    } else {
        // Natural dimensions not yet available — constrain without distorting.
        el.style.width     = 'auto';
        el.style.height    = 'auto';
        el.style.maxWidth  = cw + 'px';
        el.style.maxHeight = ch + 'px';
    }

    // CSS margin:auto centres the image when it's smaller than the container.
    // When it's larger, margins collapse to 0 and the container scrolls.
    // Scroll to show the centre of the image after a zoom change.
    requestAnimationFrame(() => {
        container.scrollLeft = Math.round((container.scrollWidth  - container.clientWidth)  / 2);
        container.scrollTop  = Math.round((container.scrollHeight - container.clientHeight) / 2);
    });

    updateSlider();
}

function centerPreview() {
    const container = document.getElementById('previewContainer');
    if (!container) return;
    setTimeout(() => {
        container.scrollTop  = (container.scrollHeight - container.clientHeight) / 2;
        container.scrollLeft = (container.scrollWidth  - container.clientWidth)  / 2;
    }, 100);
}

function stopPreview() {
    console.log('[Telescope] Stopping preview stream');
    
    const previewImage = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot = document.getElementById('previewStatusDot');
    const previewStatusText = document.getElementById('previewStatusText');
    const previewTitleIcon = document.getElementById('previewTitleIcon');
    
    if (previewImage) {
        previewImage.src = '';
        previewImage.style.display = 'none';
    }
    
    // Show placeholder again
    if (previewPlaceholder) previewPlaceholder.style.display = 'flex';
    
    if (previewStatusDot) previewStatusDot.className = 'status-dot';
    if (previewStatusText) previewStatusText.textContent = 'Preview Inactive';
    if (previewTitleIcon) previewTitleIcon.textContent = '⚫';
}

// ============================================================================
// FILMSTRIP SCROLL NAVIGATION
// ============================================================================

const FILMSTRIP_REPEAT_INITIAL_MS = 480;
const FILMSTRIP_REPEAT_INTERVAL_MS = 320;

let _filmstripRepeatTimer = null;
let _filmstripRepeatInterval = null;
let _filmstripPointerRelease = null;

function filmstripSlideStepPx(list) {
    const el = list || document.getElementById('filmstripList');
    if (!el) return 158;
    const item = el.querySelector('.filmstrip-item');
    if (!item) return 158;
    const g = parseFloat(getComputedStyle(el).gap) || 8;
    return item.getBoundingClientRect().width + g;
}

function filmstripClearRepeatTimers() {
    if (_filmstripRepeatTimer) {
        clearTimeout(_filmstripRepeatTimer);
        _filmstripRepeatTimer = null;
    }
    if (_filmstripRepeatInterval) {
        clearInterval(_filmstripRepeatInterval);
        _filmstripRepeatInterval = null;
    }
}

/** Call when pointer is released (anywhere) or navigation should stop. */
function filmstripNavUp() {
    filmstripClearRepeatTimers();
    if (_filmstripPointerRelease) {
        document.removeEventListener('pointerup', _filmstripPointerRelease);
        document.removeEventListener('pointercancel', _filmstripPointerRelease);
        _filmstripPointerRelease = null;
    }
}

/**
 * One slide per activation; hold to repeat after FILMSTRIP_REPEAT_INITIAL_MS, then every FILMSTRIP_REPEAT_INTERVAL_MS.
 * direction: -1 = newer (scroll left), +1 = older (scroll right).
 */
function filmstripNavDown(direction, ev) {
    if (ev && ev.pointerType === 'mouse' && ev.button !== 0) return;
    if (ev && typeof ev.preventDefault === 'function') ev.preventDefault();

    filmstripNavUp();

    const list = document.getElementById('filmstripList');
    if (!list) return;

    const stepOnce = () => {
        const step = filmstripSlideStepPx(list);
        list.scrollBy({ left: direction * step, behavior: 'auto' });
    };
    stepOnce();

    _filmstripPointerRelease = () => {
        filmstripNavUp();
    };
    document.addEventListener('pointerup', _filmstripPointerRelease);
    document.addEventListener('pointercancel', _filmstripPointerRelease);

    _filmstripRepeatTimer = setTimeout(() => {
        _filmstripRepeatTimer = null;
        _filmstripRepeatInterval = setInterval(stepOnce, FILMSTRIP_REPEAT_INTERVAL_MS);
    }, FILMSTRIP_REPEAT_INITIAL_MS);
}

/** Keyboard: one slide per key event (Enter / Space); OS key-repeat gives a steady scan when held. */
function filmstripNavKey(event, direction) {
    if (event.key !== 'Enter' && event.key !== ' ') return;
    event.preventDefault();
    const list = document.getElementById('filmstripList');
    if (!list) return;
    list.scrollBy({ left: direction * filmstripSlideStepPx(list), behavior: 'auto' });
}

function filmstripScrollTo(pos) {
    const list = document.getElementById('filmstripList');
    if (!list) return;
    if (pos === 'start') {
        list.scrollTo({ left: 0, behavior: 'smooth' });
    } else {
        const maxLeft = Math.max(0, list.scrollWidth - list.clientWidth);
        list.scrollTo({ left: maxLeft, behavior: 'smooth' });
    }
}

// FILE MANAGEMENT
// ============================================================================

async function refreshFiles() {
    console.log('[Telescope] Refreshing file list');
    await _syncFavoritesFromServer();
    
    const result = await apiCall(`/telescope/files?_=${Date.now()}`, 'GET');
    if (!result) return;
    
    const fileCount = document.getElementById('fileCount');
    const files = result.files || [];
    
    // Store globally for modal
    window.currentFiles = files.map(f => ({
        path: f.url,
        name: f.name,
        thumbnail: f.thumbnail || null,
        diff_heatmap: f.diff_heatmap || null,
        trigger_frame: f.trigger_frame || null,
        timelapse_frame_count: f.timelapse_frame_count ?? null,
        timelapse_interval_seconds: f.timelapse_interval_seconds ?? null
    }));
    
    // Update count badge
    if (fileCount) {
        fileCount.textContent = files.length;
    }
    
    // Update filmstrip
    updateFilmstrip(window.currentFiles);
    const filesModal = document.getElementById('filesModal');
    if (filesModal && filesModal.style.display !== 'none') {
        updateFilesGrid();
    }
    
    // Enable/disable controls
    const refreshBtn = document.getElementById('refreshFilesBtn');
    const expandBtn = document.getElementById('expandFilesBtn');
    if (refreshBtn) refreshBtn.disabled = false;
    if (expandBtn) expandBtn.disabled = files.length === 0;
}

function scheduleRefreshFiles(delaysMs = [0]) {
    delaysMs.forEach(delayMs => {
        const safeDelay = Math.max(0, Number(delayMs) || 0);
        if (_refreshFilesTimers.has(safeDelay)) return;
        const timerId = setTimeout(() => {
            _refreshFilesTimers.delete(safeDelay);
            refreshFiles();
        }, safeDelay);
        _refreshFilesTimers.set(safeDelay, timerId);
    });
}

function downloadFile(url, filename) {
    console.log('[Telescope] Downloading file:', filename);
    
    // Create temporary link and trigger download
    const link = document.createElement('a');
    link.href = url;
    link.download = filename;
    document.body.appendChild(link);
    link.click();
    document.body.removeChild(link);
    
    showStatus(`Downloading ${filename}...`, 'info', 3000);
}

function _findFileByPath(path) {
    const current = (window.currentFiles || []).find(f => f.path === path);
    if (current) return current;
    return (filmstripFiles || []).find(f => f.path === path) || null;
}

async function renameFavoriteFile(url, event) {
    if (event) event.stopPropagation();

    const favs = getFavorites();

    const file = _findFileByPath(url);
    const currentName = file?.name || (url.split('/').pop() || '');
    const dotIdx = currentName.lastIndexOf('.');
    const hasExt = dotIdx > 0;
    const currentBase = hasExt ? currentName.slice(0, dotIdx) : currentName;
    const currentExt = hasExt ? currentName.slice(dotIdx) : '';

    const raw = prompt(
        `Rename favorite file:\n${currentName}\n\nEnter new name:`,
        currentBase
    );
    if (raw === null) return;

    const typed = raw.trim();
    if (!typed) {
        showStatus('Rename failed: name cannot be empty', 'error', 4000);
        return;
    }
    if (typed.includes('/') || typed.includes('\\')) {
        showStatus('Rename failed: name cannot contain path separators', 'error', 4000);
        return;
    }

    const typedExtIdx = typed.lastIndexOf('.');
    const typedHasExt = typedExtIdx > 0;
    if (currentExt && typedHasExt && typed.slice(typedExtIdx).toLowerCase() !== currentExt.toLowerCase()) {
        showStatus(`Rename failed: keep original ${currentExt} extension`, 'error', 4000);
        return;
    }
    const newName = currentExt && !typedHasExt ? `${typed}${currentExt}` : typed;
    if (newName === currentName) return;

    try {
        const path = url.replace('/static/', '');
        const response = await fetch('/telescope/files/rename', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path, new_name: newName })
        });
        const data = await response.json().catch(() => ({}));

        if (!response.ok || !data.success) {
            showStatus(`Rename failed: ${data.error || response.status}`, 'error', 5000);
            return;
        }

        const newUrl = data.url;
        if (newUrl && favs.has(url)) {
            favs.delete(url);
            favs.add(newUrl);
            saveFavorites(favs);
        }

        if (filmstripSelection.selected.has(url)) {
            filmstripSelection.selected.delete(url);
            if (newUrl) filmstripSelection.selected.add(newUrl);
        }
        if (gridSelection.selected.has(url)) {
            gridSelection.selected.delete(url);
            if (newUrl) gridSelection.selected.add(newUrl);
        }

        showStatus(`Renamed to ${data.name || newName}`, 'success', 3000);
        await refreshFiles();
        updateFilesGrid();
    } catch (error) {
        showStatus(`Rename failed: ${error.message}`, 'error', 5000);
    }
}

async function deleteFile(url, filename, skipConfirm) {
    console.log('[Telescope] deleteFile called:', url, filename);
    if (!skipConfirm && !confirm(`Delete ${filename}?`)) {
        return;
    }

    try {
        const path = url.replace('/static/', '');
        const response = await fetch('/telescope/files/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showStatus(`Deleted ${filename}`, 'success', 3000);
        } else {
            showStatus(`Delete failed: ${data.error || response.status}`, 'error', 5000);
        }
    } catch (error) {
        showStatus(`Delete failed: ${error.message}`, 'error', 5000);
    }
    await refreshFiles();
    updateFilesGrid();
}

// ============================================================================
// UI HELPERS
// ============================================================================

function showStatus(message, type = 'info', autohide = 0) {
    // Use a fixed-position toast so it's visible above modals
    let toast = document.getElementById('statusToast');
    if (!toast) {
        toast = document.createElement('div');
        toast.id = 'statusToast';
        document.body.appendChild(toast);
    }
    toast.textContent = message;
    toast.className = `status-toast status-toast-${type}`;
    toast.style.display = 'block';
    if (autohide > 0) {
        setTimeout(() => {
            toast.style.display = 'none';
        }, autohide);
    }
}

function showWarning(message, type = 'warning', autohide = 0) {
    // Create or update warning box
    let warningBox = document.getElementById('dynamicWarning');
    
    if (!warningBox) {
        warningBox = document.createElement('div');
        warningBox.id = 'dynamicWarning';
        warningBox.className = `warning-box ${type}`;
        
        const targetSection = document.querySelector('.target-selection');
        if (targetSection) {
            targetSection.appendChild(warningBox);
        }
    }
    
    warningBox.textContent = message;
    warningBox.className = `warning-box ${type}`;
    warningBox.style.display = 'block';
    
    // Auto-hide after timeout
    if (autohide > 0) {
        setTimeout(() => {
            warningBox.style.display = 'none';
        }, autohide);
    }
}

// ============================================================================
// API HELPERS
// ============================================================================

function _formatApiError(data, status) {
    const fallback = `HTTP ${status}`;
    if (!data || data.error === undefined || data.error === null) return fallback;

    if (typeof data.error === 'string') return data.error;

    if (typeof data.error === 'object') {
        const msg = data.error.message || data.error.code || fallback;
        const code = data.error.code ? ` (${data.error.code})` : '';
        return `${msg}${code}`;
    }

    return fallback;
}

async function apiCall(endpoint, method = 'GET', body = null, options = {}) {
    const silent = options.silent === true;
    try {
        const fetchOpts = {
            method: method,
            headers: {}
        };
        if (body) {
            fetchOpts.headers['Content-Type'] = 'application/json';
            fetchOpts.body = JSON.stringify(body);
        }

        const response = await fetch(endpoint, fetchOpts);
        const contentType = (response.headers.get('content-type') || '').toLowerCase();
        const looksJson =
            contentType.includes('application/json') ||
            contentType.includes('text/json') ||
            contentType.includes('+json');
        let data;
        if (looksJson) {
            data = await response.json();
        } else {
            const text = await response.text();
            try {
                data = text ? JSON.parse(text) : {};
            } catch (_) {
                throw new Error(`HTTP ${response.status}: expected JSON, got ${contentType || 'unknown type'}`);
            }
        }

        if (!response.ok) {
            throw new Error(_formatApiError(data, response.status));
        }

        return data;

    } catch (error) {
        console.error(`[Telescope] API call failed: ${endpoint}`, error);
        if (!silent) {
            showStatus(`Error: ${error.message}`, 'error');
        }
        return null;
    }
}

// ============================================================================
// CONTROL PANEL — GoTo, Park, Autofocus, Camera Settings, Named Locations
// ============================================================================

function _renderSunCenterStatus(data) {
    const panel = document.getElementById('sunCenterPanel');
    if (!panel) return;

    const stateEl = document.getElementById('sunCenterState');
    const modeEl = document.getElementById('sunCenterTolMode');
    const errEl = document.getElementById('sunCenterErr');
    const recEl = document.getElementById('sunCenterRecoveries');
    const searchKindEl = document.getElementById('sunCenterSearchKind');
    const msgEl = document.getElementById('sunCenterMsg');
    const startBtn = document.getElementById('sunCenterStartBtn');
    const stopBtn = document.getElementById('sunCenterStopBtn');
    const recenterBtn = document.getElementById('sunCenterRecenterBtn');
    const modeSelect = document.getElementById('sunCenterSearchMode');
    const applyModeBtn = document.getElementById('sunCenterApplyModeBtn');

    const running = !!(data && data.running);
    const state = (data && data.state) ? String(data.state) : 'idle';
    const unavailable = state === 'unavailable';
    const tolMode = (data && data.tolerance_mode) ? String(data.tolerance_mode) : 'strict';
    const serverSearchMode = (data && data.search_pattern_mode) ? String(data.search_pattern_mode) : null;
    const activeSearchKind = (data && data.search_pattern_kind)
        ? String(data.search_pattern_kind)
        : null;
    const configuredSearchMode = serverSearchMode || _sunCenterSelectedSearchMode || '-';
    const err = (data && data.error_norm != null) ? Number(data.error_norm) : null;
    const recov = (data && data.recovery_attempts != null) ? Number(data.recovery_attempts) : 0;
    const msg = (data && data.message) ? String(data.message) : 'Solar mode only. Experimental feature.';

    if (stateEl) stateEl.textContent = state;
    if (modeEl) modeEl.textContent = tolMode;
    if (searchKindEl) {
        let searchDisplay = '-';
        if (configuredSearchMode === 'adaptive') {
            searchDisplay = activeSearchKind ? `adaptive (${activeSearchKind})` : 'adaptive';
        } else if (configuredSearchMode && configuredSearchMode !== '-') {
            if (activeSearchKind && activeSearchKind !== configuredSearchMode) {
                searchDisplay = `${configuredSearchMode} (${activeSearchKind})`;
            } else {
                searchDisplay = configuredSearchMode;
            }
        } else {
            searchDisplay = activeSearchKind || '-';
        }

        if (_sunCenterSearchModeDirty) {
            searchKindEl.textContent = `${searchDisplay} (pending ${_sunCenterSelectedSearchMode})`;
        } else {
            searchKindEl.textContent = searchDisplay;
        }
    }
    if (errEl) errEl.textContent = Number.isFinite(err) ? err.toFixed(3) : '-';
    if (recEl) recEl.textContent = String(Number.isFinite(recov) ? recov : 0);

    if (modeSelect) {
        const allowed = ['adaptive', 'spiral', 'raster', 'random_walk'];
        if (allowed.includes(serverSearchMode)) {
            if (_sunCenterSearchModeDirty) {
                if (serverSearchMode === _sunCenterSelectedSearchMode) {
                    _sunCenterSearchModeDirty = false;
                }
            } else {
                _sunCenterSelectedSearchMode = serverSearchMode;
                try { localStorage.setItem('sunCenterSearchMode', _sunCenterSelectedSearchMode); } catch (_) {}
            }
        }
        if (allowed.includes(_sunCenterSelectedSearchMode)) {
            modeSelect.value = _sunCenterSelectedSearchMode;
        }
    }

    let footer = msg;
    const mode = (currentViewingMode || '').toLowerCase();
    if (mode && mode !== 'sun') {
        footer = 'Experimental and Solar-only for now. Switch to Solar mode to start.';
    }
    if (msgEl) msgEl.textContent = footer;

    if (startBtn) {
        startBtn.disabled = unavailable || !isConnected || running;
    }
    if (stopBtn) stopBtn.disabled = unavailable || !running;
    if (recenterBtn) recenterBtn.disabled = unavailable || !running;
    if (modeSelect) modeSelect.disabled = unavailable || !isConnected;
    if (applyModeBtn) applyModeBtn.disabled = unavailable || !isConnected;
}

function sunCenterOnModeChanged() {
    const modeSelect = document.getElementById('sunCenterSearchMode');
    const mode = modeSelect ? String(modeSelect.value || '').trim() : '';
    if (!['adaptive', 'spiral', 'raster', 'random_walk'].includes(mode)) return;
    _sunCenterSelectedSearchMode = mode;
    _sunCenterSearchModeDirty = true;
    try { localStorage.setItem('sunCenterSearchMode', mode); } catch (_) {}
}

function _markSunCenterApiUnavailable(message) {
    _sunCenterApiUnavailable = true;
    if (sunCenterPollInterval) {
        clearInterval(sunCenterPollInterval);
        sunCenterPollInterval = null;
    }

    sunCenterStatus = {
        running: false,
        state: 'unavailable',
        tolerance_mode: 'strict',
        error_norm: null,
        recovery_attempts: 0,
        message: message,
    };
    _renderSunCenterStatus(sunCenterStatus);

    if (!_sunCenterApiWarningShown) {
        _sunCenterApiWarningShown = true;
        showStatus(message, 'warning', 9000);
    }
}

async function pollSunCenterStatus() {
    if (_sunCenterApiUnavailable) return;

    try {
        const response = await fetch('/telescope/sun-center/status', {
            method: 'GET',
            headers: {},
        });

        if (response.status === 404) {
            _markSunCenterApiUnavailable(
                'Sun centering API unavailable (404). Restart backend and reload to enable /telescope/sun-center/* routes.'
            );
            return;
        }

        const contentType = (response.headers.get('content-type') || '').toLowerCase();
        const looksJson =
            contentType.includes('application/json') ||
            contentType.includes('text/json') ||
            contentType.includes('+json');

        let data;
        if (looksJson) {
            data = await response.json();
        } else {
            const text = await response.text();
            try {
                data = text ? JSON.parse(text) : {};
            } catch (_) {
                throw new Error(`HTTP ${response.status}: expected JSON, got ${contentType || 'unknown type'}`);
            }
        }

        if (!response.ok) {
            throw new Error(_formatApiError(data, response.status));
        }

        sunCenterStatus = data;
        _renderSunCenterStatus(data);
    } catch (error) {
        console.error('[Telescope] Sun-center status poll failed', error);
    }
}

async function sunCenterStart() {
    if (_sunCenterApiUnavailable) {
        showStatus('Sun centering API unavailable. Restart backend and reload this page.', 'warning', 7000);
        return;
    }
    const modeSelect = document.getElementById('sunCenterSearchMode');
    const mode = modeSelect ? String(modeSelect.value || '').trim() : _sunCenterSelectedSearchMode;
    if (['adaptive', 'spiral', 'raster', 'random_walk'].includes(mode)) {
        _sunCenterSelectedSearchMode = mode;
        _sunCenterSearchModeDirty = false;
        try { localStorage.setItem('sunCenterSearchMode', mode); } catch (_) {}
    }

    const result = await apiCall('/telescope/sun-center/start', 'POST', {
        search_pattern_mode: _sunCenterSelectedSearchMode,
    });
    if (!result) return;
    sunCenterStatus = result;
    _renderSunCenterStatus(result);
    showStatus('Sun centering started', 'success', 2500);
}

async function sunCenterStop() {
    if (_sunCenterApiUnavailable) {
        showStatus('Sun centering API unavailable. Restart backend and reload this page.', 'warning', 7000);
        return;
    }
    const result = await apiCall('/telescope/sun-center/stop', 'POST', {});
    if (!result) return;
    sunCenterStatus = result;
    _renderSunCenterStatus(result);
    showStatus('Sun centering stopped', 'info', 2500);
}

async function sunCenterRecenter() {
    if (_sunCenterApiUnavailable) {
        showStatus('Sun centering API unavailable. Restart backend and reload this page.', 'warning', 7000);
        return;
    }
    const result = await apiCall('/telescope/sun-center/recenter', 'POST', {});
    if (!result) return;
    sunCenterStatus = result;
    _renderSunCenterStatus(result);
    showStatus('Recenter requested: acquisition restarted', 'info', 3000);
}

async function sunCenterApplySearchMode() {
    if (_sunCenterApiUnavailable) {
        showStatus('Sun centering API unavailable. Restart backend and reload this page.', 'warning', 7000);
        return;
    }

    const modeSelect = document.getElementById('sunCenterSearchMode');
    const mode = modeSelect ? String(modeSelect.value || '').trim() : '';
    if (!['adaptive', 'spiral', 'raster', 'random_walk'].includes(mode)) {
        showStatus('Invalid search mode selection', 'warning', 3500);
        return;
    }

    _sunCenterSelectedSearchMode = mode;
    try { localStorage.setItem('sunCenterSearchMode', mode); } catch (_) {}

    const running = !!(sunCenterStatus && sunCenterStatus.running);
    if (!running) {
        _sunCenterSearchModeDirty = false;
        _renderSunCenterStatus(sunCenterStatus || { running: false, state: 'idle' });
        showStatus(`Sun search mode saved (${mode}); it will apply on next start`, 'info', 3200);
        return;
    }

    const result = await apiCall('/telescope/sun-center/settings', 'PATCH', {
        search_pattern_mode: mode,
    });
    if (!result) return;

    _sunCenterSearchModeDirty = false;
    sunCenterStatus = result;
    _renderSunCenterStatus(result);
    showStatus(`Sun search mode set to ${mode}`, 'info', 2500);
}

function initControlPanel() {
    loadSavedLocations();
}

// -- Live position readout (scope_get_horiz_coord) --

let _posInterval = null;
let _posInFlight = false;

function startPositionSync() {
    if (_posInterval) return;
    _pollPosition();
    _posInterval = setInterval(_pollPosition, 3000);
}

function stopPositionSync() {
    clearInterval(_posInterval);
    _posInterval = null;
    document.getElementById('scopeAlt') && (document.getElementById('scopeAlt').textContent = '—');
    document.getElementById('scopeAz')  && (document.getElementById('scopeAz').textContent  = '—');
}

async function _pollPosition() {
    if (_posInFlight || !isConnected) return;
    _posInFlight = true;
    try {
        const ctrl = new AbortController();
        const t = setTimeout(() => ctrl.abort(), 4000);
        const res = await fetch(`/telescope/position?t=${Date.now()}`, {
            signal: ctrl.signal, cache: 'no-store',
        });
        clearTimeout(t);
        const d = await res.json();
        const altEl = document.getElementById('scopeAlt');
        const azEl  = document.getElementById('scopeAz');
        if (!res.ok || d.error) {
            // Scenery mode or other — no pointing source, show dash
            if (altEl) altEl.textContent = '—';
            if (azEl)  azEl.textContent  = '—';
            return;
        }

        // Update position readout
        if (altEl) altEl.textContent = (+d.alt).toFixed(1);
        if (azEl)  azEl.textContent  = (+d.az).toFixed(1);

        // Pre-fill GoTo inputs if user hasn't typed in them
        const altIn = document.getElementById('gotoAlt');
        const azIn  = document.getElementById('gotoAz');
        if (altIn && !altIn.dataset.userEdited && document.activeElement !== altIn)
            altIn.value = (+d.alt).toFixed(1);
        if (azIn && !azIn.dataset.userEdited && document.activeElement !== azIn)
            azIn.value = (+d.az).toFixed(1);
    } catch (_) { /* silent — scope may not support this command */ }
    finally { _posInFlight = false; }
}

// -- GoTo mode radio toggle --

function gotoModeChanged() {
    const isAltaz = document.getElementById('gotoModeAltaz').checked;
    document.getElementById('gotoAltazInputs').style.display = isAltaz ? '' : 'none';
    document.getElementById('gotoRadecInputs').style.display = isAltaz ? 'none' : '';
}

// -- GoTo execute --

// -- Manual Slew (Joystick) --
const _NUDGE_TAP_MIN_MS = 180; // ensure a visible micro-move on quick click/tap
let _nudgeActive = false;
let _nudgeStartedAt = 0;
let _nudgeStopTimer = null;

function nudgePressStart(angle, ev) {
    if (ev) {
        if (typeof ev.button === 'number' && ev.button !== 0) return;
        ev.preventDefault();
    }
    nudgeStart(angle);
}

function nudgePressEnd(ev) {
    if (ev) ev.preventDefault();
    nudgeStop();
}

function nudgeStart(angle) {
    if (!_alpacaConnected) {
        showStatus('Motor control requires ALPACA connection', 'error', 3000);
        return;
    }
    if (_ctrlState === 'slewing' || _ctrlState === 'goto_resuming') {
        showStatus(`Cannot nudge while ${_ctrlState.replace('_', ' ')}`, 'warning', 3000);
        return;
    }
    // ALPACA: send once — backend holds the rate until nudge/stop
    // angle: 0=left, 90=down, 180=right, 270=up (legacy convention)
    const fast = document.querySelector('input[name="nudgeSpeed"]:checked')?.value === 'fast';
    const rate = fast ? 3.0 : 1.0;
    let axis, dir;
    if (angle === 270)      { axis = 1; dir =  1; } // up = Dec/Alt +
    else if (angle === 90)  { axis = 1; dir = -1; } // down = Dec/Alt -
    else if (angle === 180) { axis = 0; dir =  1; } // right = RA/Az +
    else                    { axis = 0; dir = -1; } // left = RA/Az -
    if (_nudgeStopTimer) {
        clearTimeout(_nudgeStopTimer);
        _nudgeStopTimer = null;
    }
    _nudgeActive = true;
    _nudgeStartedAt = Date.now();
    apiCall('/telescope/nudge', 'POST', { axis, rate: rate * dir });
}

function nudgeStop() {
    if (!_nudgeActive) return;
    const elapsed = Date.now() - _nudgeStartedAt;
    const delay = Math.max(0, _NUDGE_TAP_MIN_MS - elapsed);
    if (_nudgeStopTimer) {
        clearTimeout(_nudgeStopTimer);
        _nudgeStopTimer = null;
    }
    _nudgeStopTimer = setTimeout(() => {
        apiCall('/telescope/nudge/stop', 'POST', {});
        _nudgeActive = false;
        _nudgeStopTimer = null;
    }, delay);
}

// Mark GoTo inputs as user-edited (prevents accidental resets).
// Use delegated handler in case control-panel DOM is recreated.
document.addEventListener('input', (ev) => {
    const el = ev.target;
    if (!el || !el.id) return;
    if (el.id === 'gotoAlt' || el.id === 'gotoAz' || el.id === 'gotoRa' || el.id === 'gotoDec') {
        el.dataset.userEdited = '1';
    }
});

async function gotoExecute(overrideAlt, overrideAz) {
    if (_ctrlState === 'nudging') {
        showStatus('Stopping active nudge before GoTo…', 'info', 3000);
        await apiCall('/telescope/nudge/stop', 'POST', {});
    }
    const mode = document.getElementById('gotoModeAltaz').checked ? 'altaz' : 'radec';
    let body;
    if (mode === 'altaz') {
        const altEl = document.getElementById('gotoAlt');
        const azEl  = document.getElementById('gotoAz');
        const alt = overrideAlt !== undefined ? overrideAlt : parseFloat(altEl.value || altEl.placeholder);
        const az  = overrideAz  !== undefined ? overrideAz  : parseFloat(azEl.value || azEl.placeholder);
        if (isNaN(alt) || isNaN(az)) { showStatus('Enter Alt and Az values', 'error', 3000); return; }
        body = { mode: 'altaz', alt, az };
        // Alt/Az slew uses scenery mode (tracking off). Re-enter sun/moon after
        // slew completes if we were tracking that body (see telescope_routes).
        if (currentViewingMode === 'sun') body.resume_tracking = 'sun';
        else if (currentViewingMode === 'moon') body.resume_tracking = 'moon';
    } else {
        const raEl  = document.getElementById('gotoRa');
        const decEl = document.getElementById('gotoDec');
        const ra  = parseFloat(raEl.value || raEl.placeholder);
        const dec = parseFloat(decEl.value || decEl.placeholder);
        if (isNaN(ra) || isNaN(dec)) { showStatus('Enter RA and Dec values', 'error', 3000); return; }
        body = { mode: 'radec', ra, dec };
    }
    showStatus('Sending GoTo…', 'info', 5000);
    const result = await apiCall('/telescope/goto', 'POST', body);
    if (result) {
        if (result.success) {
            const msg = result.message || 'GoTo command sent';
            if (result.resume_tracking) {
                showStatus(
                    `${msg} — will re-enable ${result.resume_tracking} tracking after slew`,
                    'info',
                    15000,
                );
            } else {
                showStatus(msg, 'success', 5000);
            }
        }
        // Clear user-edited flag so GoTo fields can be updated again
        ['gotoAlt','gotoAz','gotoRa','gotoDec'].forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                delete el.dataset.userEdited;
            }
        });
    }
}

// -- Named Locations --

async function loadSavedLocations() {
    try {
        const res = await fetch('/telescope/goto/locations');
        if (!res.ok) return;
        const locs = await res.json();
        const sel = document.getElementById('savedLocations');
        if (!sel) return;
        sel.innerHTML = locs.length
            ? locs.map(l => `<option value="${encodeURIComponent(l.name)}" data-alt="${l.alt}" data-az="${l.az}">${l.name}</option>`).join('')
            : '<option value="" disabled>No saved locations</option>';
        document.getElementById('locationsStatus').textContent =
            locs.length ? `${locs.length} location${locs.length !== 1 ? 's' : ''} saved` : '';
    } catch (_) { /* non-fatal */ }
}

async function gotoSavedLocation() {
    const sel = document.getElementById('savedLocations');
    const opt = sel && sel.selectedOptions[0];
    if (!opt || !opt.dataset.alt) { showStatus('Select a location first', 'error', 3000); return; }
    // Switch to alt/az mode
    document.getElementById('gotoModeAltaz').checked = true;
    gotoModeChanged();
    await gotoExecute(parseFloat(opt.dataset.alt), parseFloat(opt.dataset.az));
}

async function saveCurrentLocation() {
    const nameInput = document.getElementById('saveLocationName');
    const name = nameInput ? nameInput.value.trim() : '';
    if (!name) { showStatus('Enter a name for this location', 'error', 3000); return; }

    // Use the GoTo inputs for alt/az
    const altInput = parseFloat(document.getElementById('gotoAlt').value);
    const azInput  = parseFloat(document.getElementById('gotoAz').value);
    if (isNaN(altInput) || isNaN(azInput)) {
        showStatus('Enter Alt/Az in the GoTo fields first', 'error', 5000);
        return;
    }
    {
        await _doSaveLocation(name, altInput, azInput);
    }
    if (nameInput) nameInput.value = '';
}

async function _doSaveLocation(name, alt, az) {
    const result = await apiCall('/telescope/goto/locations', 'POST', { name, alt, az });
    if (result && result.success) {
        showStatus(`Saved "${name}"`, 'success', 3000);
        document.getElementById('locationsStatus').textContent = `Saved "${name}"`;
        await loadSavedLocations();
    }
}

async function deleteSelectedLocation() {
    const sel = document.getElementById('savedLocations');
    const opt = sel && sel.selectedOptions[0];
    if (!opt || !opt.value) { showStatus('Select a location to delete', 'error', 3000); return; }
    const name = decodeURIComponent(opt.value);
    if (!confirm(`Delete location "${name}"?`)) return;
    const res = await fetch(`/telescope/goto/locations/${encodeURIComponent(name)}`, { method: 'DELETE' });
    if (res.ok) {
        showStatus(`Deleted "${name}"`, 'success', 3000);
        await loadSavedLocations();
    } else {
        showStatus('Delete failed', 'error', 3000);
    }
}

// -- Stop / Park / Autofocus --

async function telescopeStopView() {
    showStatus('Stopping view mode…', 'info', 5000);
    const result = await apiCall('/telescope/stop', 'POST', {});
    if (result) {
        showStatus('View stopped', 'success', 3000);
        stopPreview();
    }
}

async function telescopeOpenArm() {
    showStatus('Opening arm…', 'info', 5000);
    const result = await apiCall('/telescope/open-arm', 'POST', {});
    if (result) showStatus('Open arm command sent', 'success', 3000);
}

async function telescopePark() {
    showStatus('Parking…', 'info', 10000);
    const result = await apiCall('/telescope/park', 'POST', {});
    if (result) showStatus('Park command sent', 'success', 3000);
}

let _autofocusInFlight = false;

async function telescopeAutofocus() {
    if (_autofocusInFlight) {
        showStatus('Autofocus already in progress', 'warning', 3000);
        return;
    }

    _autofocusInFlight = true;
    try {
        showStatus('Running autofocus routine…', 'info', 45000);
        const result = await apiCall('/telescope/autofocus', 'POST', {});
        if (!result) return;

        const provider = String(result.provider || '');
        const af = result.result || {};

        // Keep focus readout in sync when backend reports a final position.
        const finalPos = _focusPosFromRpcPayload(af.final_focus_pos ?? af.best_position ?? af.focus_pos);
        if (finalPos != null) {
            const el = document.getElementById('focusPos');
            if (el) el.textContent = String(finalPos);
        }

        if (provider === 'alpaca_autofocus' && af && af.success) {
            const best = _focusPosFromRpcPayload(af.best_position);
            const score = (typeof af.best_score === 'number' && Number.isFinite(af.best_score))
                ? `, score ${af.best_score.toFixed(3)}`
                : '';
            showStatus(
                `Autofocus complete at ${best != null ? best : '—'}${score}`,
                'success',
                8000
            );
        } else if (result.confirmed) {
            showStatus(result.message || 'Autofocus started', 'success', 5000);
        } else {
            showStatus(
                result.message || 'Autofocus sent but scope did not confirm start',
                'warning',
                7000
            );
        }
    } finally {
        _autofocusInFlight = false;
    }
}

async function telescopeShutdown() {
    if (!confirm('Shut down the Seestar? You will need to physically restart it.')) return;
    showStatus('Sending shutdown…', 'info', 5000);
    const result = await apiCall('/telescope/shutdown', 'POST', {});
    if (result) showStatus('Shutdown command sent — scope powering off', 'success', 5000);
}

// -- Manual Focus --

let _focusStepSize = 10;

// Wrappers for inline onclick — let variables are not on window, functions are.
function focusStepIn()  { focusStep(-_focusStepSize); }
function focusStepOut() { focusStep(_focusStepSize); }

function setFocusStepSize(size) {
    _focusStepSize = size;
    // Use .is-active (locked-down keycap) instead of inline styles
    document.querySelectorAll('.focus-step-btn').forEach(btn => {
        btn.classList.toggle('is-active', parseInt(btn.dataset.steps) === size);
    });
}

function _focusPosFromRpcPayload(payload) {
    if (payload == null) return null;
    if (typeof payload === 'number' && Number.isFinite(payload)) return Math.round(payload);
    if (typeof payload === 'string' && /^-?\d+$/.test(payload.trim())) return parseInt(payload.trim(), 10);
    if (typeof payload === 'object') {
        for (const k of ['focus_pos', 'target_param', 'step', 'FocusPos', 'Step', 'position', 'result']) {
            const v = payload[k];
            const n = _focusPosFromRpcPayload(v);
            if (n != null) return n;
        }
    }
    return null;
}

async function focusStep(steps) {
    const result = await apiCall('/telescope/focus/step', 'POST', { steps });
    if (result) {
        const pos = _focusPosFromRpcPayload(result.result);
        if (pos != null) {
            const el = document.getElementById('focusPos');
            if (el) el.textContent = String(pos);
        }
    }
}

// -- Camera Settings --

function toggleDewPower() {
    const on = document.getElementById('dewHeaterToggle').checked;
    document.getElementById('dewPowerRow').style.display = on ? '' : 'none';
}

async function applyGain() {
    const slider = document.getElementById('gainSlider');
    const gainEl = document.getElementById('gainVal');
    if (!slider) return;
    const gain = parseInt(slider.value, 10);
    if (Number.isFinite(gain) && gainEl) gainEl.textContent = String(gain);
    await apiCall('/telescope/settings/camera', 'PATCH', { gain });
}

async function applyLpFilter() {
    const lp_filter = document.getElementById('lpFilterToggle').checked;
    await apiCall('/telescope/settings/camera', 'PATCH', { lp_filter });
}

async function applyDewHeater() {
    const dew_heater = document.getElementById('dewHeaterToggle').checked;
    const dew_power  = parseInt(document.getElementById('dewPowerSlider').value);
    await apiCall('/telescope/settings/camera', 'PATCH', { dew_heater, dew_power });
}

async function toggleAutoExp() {
    const btn = document.getElementById('autoExpBtn');
    // Toggle state locally (server has no persistent on/off for auto-exp)
    const willEnable = !(btn && btn.classList.contains('is-active'));
    const result = await apiCall('/telescope/camera/auto-exp', 'POST', { enabled: willEnable });
    if (result && result.success) {
        // .is-active = locked-down keycap when auto-exp is on
        if (btn) btn.classList.toggle('is-active', willEnable);
        showStatus(willEnable ? 'Auto exposure on' : 'Auto exposure off', 'success', 3000);
    }
}

// ============================================================================
// TRANSIT AUTO-CAPTURE
// ============================================================================

async function checkTransits() {
    // Don't poll or populate transit list when scope is disconnected
    if (!isConnected) return;

    try {
        const response = await fetch('/telescope/transit/status');
        if (!response.ok) return;
        
        const data = await response.json();

        // Preserve any sim transits (SIM-*) already in the list — server
        // doesn't know about them and would wipe them on every 15s poll.
        const simTransits = upcomingTransits.filter(t =>
            t.flight && t.flight.startsWith('SIM-')
        );
        upcomingTransits = [...(data.transits || []), ...simTransits];
        
        updateTransitList();
        checkAutoCapture();
    } catch (error) {
        console.warn('[Telescope] Transit check failed:', error);
    }
}

function _transitStateFor(s) {
    const PRE = 10;
    if (s > PRE) {
        const mins = Math.floor(s / 60);
        const secs = s % 60;
        const timeStr = mins > 0 ? `${mins}m ${secs}s` : `${secs}s`;
        return { stateClass: 'state-waiting', stateLabel: '', countdownText: timeStr, countdownClass: 'tc-big' };
    } else if (s > 0) {
        return { stateClass: 'state-recording', stateLabel: `🔴 Recording — transit in ${s}s`, countdownText: `${s}s`, countdownClass: 'tc-big tc-red' };
    } else if (s === 0) {
        return { stateClass: 'state-transit', stateLabel: '🎯 TRANSIT NOW', countdownText: 'NOW', countdownClass: 'tc-big tc-red' };
    } else {
        return { stateClass: 'state-post', stateLabel: `🔴 Recording — transit passed ${Math.abs(s)}s ago`, countdownText: `+${Math.abs(s)}s`, countdownClass: 'tc-big tc-dim' };
    }
}

function updateTransitList() {
    const list = document.getElementById('transitList');
    if (!list) return;

    // Never show predicted transits when the scope isn't connected —
    // there's nothing to capture with, so the alert is misleading.
    if (!isConnected) {
        upcomingTransits = [];
        list.innerHTML = '<p class="empty-state">Connect telescope to monitor transits</p>';
        return;
    }

    // Auto-remove transits that are more than POST seconds past (recording done)
    const POST = 10;
    upcomingTransits = upcomingTransits.filter(t => t.seconds_until > -POST);

    if (upcomingTransits.length === 0) {
        list.innerHTML = '<p class="empty-state">No transits detected</p>';
        return;
    }

    // Remove stale empty-state if present
    const empty = list.querySelector('.empty-state');
    if (empty) list.innerHTML = '';

    const probClass = t => (t.probability || '').toLowerCase();

    upcomingTransits.forEach(transit => {
        const s = transit.seconds_until;
        const { stateClass, stateLabel, countdownText, countdownClass } = _transitStateFor(s);
        const cardId = `ta-${transit.flight.replace(/[^a-zA-Z0-9]/g, '_')}`;
        let card = document.getElementById(cardId);

        if (!card) {
            // First render — create the card
            card = document.createElement('div');
            card.id = cardId;
            card.className = `transit-alert ${probClass(transit)} ${stateClass}`;
            card.dataset.transitState = stateClass;
            card.innerHTML = `
                <div class="ta-header">
                    <span class="ta-flight">✈️ ${transit.flight}</span>
                    <span class="ta-prob">${transit.probability}</span>
                </div>
                <div class="ta-target">${transit.target || ''} &nbsp;·&nbsp; Alt ${transit.altitude}° Az ${transit.azimuth}°</div>
                <div class="${countdownClass} ta-countdown">${countdownText}</div>
                ${stateLabel ? `<div class="ta-state">${stateLabel}</div>` : ""}
            `;
            list.appendChild(card);
        } else {
            // Patch in-place — only update text nodes, never rebuild DOM
            const countdown = card.querySelector('.ta-countdown');
            const stateEl   = card.querySelector('.ta-state');
            if (countdown) {
                countdown.textContent = countdownText;
                countdown.className = `${countdownClass} ta-countdown`;
            }
            if (stateEl) {
                stateEl.textContent = stateLabel;
                stateEl.style.display = stateLabel ? '' : 'none';
            }
            // Update state class on card only when it changes (avoids reflow)
            if (card.dataset.transitState !== stateClass) {
                card.dataset.transitState = stateClass;
                card.className = `transit-alert ${probClass(transit)} ${stateClass}`;
            }
        }
    });

    // Remove cards for transits no longer in the list; show expiry msg for unconfirmed ones
    const activeIds = new Set(upcomingTransits.map(t => `ta-${t.flight.replace(/[^a-zA-Z0-9]/g, '_')}`));
    list.querySelectorAll('.transit-alert').forEach(card => {
        if (!activeIds.has(card.id) && !card.dataset.expiring) {
            const wasRecorded = card.dataset.transitState === 'state-transit' || card.dataset.transitState === 'state-recording' || card.dataset.transitState === 'state-post';
            if (!wasRecorded) {
                // Prediction expired before the transit could be confirmed
                card.dataset.expiring = '1';
                card.className = 'transit-alert low state-expired';
                const countdown = card.querySelector('.ta-countdown');
                const stateEl = card.querySelector('.ta-state');
                if (countdown) { countdown.textContent = '—'; countdown.className = 'tc-big tc-dim'; }
                if (stateEl) { stateEl.textContent = 'Prediction expired — transit no longer anticipated'; stateEl.style.display = ''; }
                setTimeout(() => card.remove(), 5000);
            } else {
                card.remove();
            }
        }
    });
}

function formatCountdown(seconds) {
    if (seconds < 0) return 'PASSED';
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

async function checkAutoCapture() {
    const autoCapture = document.getElementById('autoCaptureToggle').checked;
    if (!autoCapture || !isConnected) return;

    const PRE = 10, POST = 10;

    // Find next unhandled transit within PRE seconds that hasn't already been captured
    const imminent = upcomingTransits.find(t =>
        t.seconds_until <= PRE && t.seconds_until > 0 && !t.handled && !capturedTransits.has(t.flight)
    );
    if (!imminent) return;

    imminent.handled = true;           // prevent re-triggering on per-second tick
    capturedTransits.add(imminent.flight); // persist across array replacements from server poll

    // Force-sync recording state before deciding — guards against the backend
    // TransitRecorder having already started Seestar's internal recording, which
    // would otherwise be invisible to the 2 s background poll.
    await updateStatus();

    const isSimFlight = imminent.flight === SIM_TRANSIT.flight ||
                        imminent.flight === SIM_ECLIPSE_TRANSIT.flight;

    if (isRecording) {
        if (!recordingIsReal && !isSimFlight) {
            // Sim recording is running but a REAL transit is imminent.
            // Real always wins — stop sim and let recordTransit() take over.
            console.log(`[Telescope] Real transit ${imminent.flight} preempts sim recording`);
            showStatus(`✈️ Real transit ${imminent.flight} — stopping sim, switching to real capture`, 'warning', 5000);
            stopRecording();  // routes to simulateStopRecording() in sim mode
            // Small delay to let stop settle, then record the real transit
            setTimeout(() => recordTransit(imminent.flight, imminent.seconds_until), 300);
        } else {
            // Real-vs-real or sim-vs-sim: extend instead of interrupting
            const newEndMs = Date.now() + (imminent.seconds_until + POST) * 1000;
            extendRecording(newEndMs);
            showStatus(`📹 Recording extended for ${imminent.flight} (transit in ${imminent.seconds_until}s)`, 'info', 5000);
            console.log('[Telescope] Extended recording for overlapping transit:', imminent.flight);
            // If an eclipse is active, mark this transit as a timestamped event within the clip
            if (eclipseAlertLevel === 'active' && recordingStartTime) {
                addTransitMarkerToEclipseRecording(imminent.flight, Date.now() - recordingStartTime);
            }
        }
    } else {
        console.log('[Telescope] Auto-capturing transit:', imminent.flight, `(${imminent.seconds_until}s)`);
        recordTransit(imminent.flight, imminent.seconds_until);
    }
}

// ============================================================================
// ECLIPSE ALERT SYSTEM
// ============================================================================
//
// Alert levels (mirroring NWS Watch/Warning convention):
//   outlook  — eclipse within 48 h; banner shown, no card
//   watch    — eclipse within 60 min; countdown card added to transit panel
//   warning  — eclipse within 30 s of C1; card pulses red, recording arms
//   active   — C1 ≤ now ≤ C4; recording in progress
//   cleared  — ≤ 30 min past C4; summary card, then fades
//   null     — no eclipse in window
//
// Recording rule: recordingEndTime can only move LATER.  Once an eclipse
// goes Active, recordingEndTime is pinned to ≥ C4 + 10 s.  Aircraft transits
// that happen during the eclipse window extend recordingEndTime further and
// add a ✈️ marker in the filmstrip entry.
// ============================================================================

/**
 * Parse an ISO date string from the server into a JS Date.
 * Returns null if input is null/undefined.
 */
function _parseEclipseDate(iso) {
    return iso ? new Date(iso) : null;
}

/**
 * Format seconds as M:SS or H:MM:SS countdown string.
 */
function _fmtCountdown(sec) {
    sec = Math.abs(Math.round(sec));
    const h = Math.floor(sec / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = sec % 60;
    if (h > 0) return `${h}:${String(m).padStart(2,'0')}:${String(s).padStart(2,'0')}`;
    return `${m}:${String(s).padStart(2,'0')}`;
}

/**
 * Return the current phase label for a given time relative to eclipse contacts.
 *   c1, c2, c3, c4 are Date objects (c2/c3 may be null for partial eclipses).
 */
function renderEclipsePhase(c1, c2, c3, c4) {
    const now = new Date();
    if (now < c1) return 'Pre-eclipse';
    if (c2 && now < c2) return 'Partial (ingress)';
    if (c2 && c3 && now >= c2 && now <= c3) return 'Totality';
    if (c3 && now > c3 && now <= c4) return 'Partial (egress)';
    if (now <= c4) return 'Partial';
    return 'Post-eclipse';
}

/**
 * Main eclipse state machine — called every second from transitTickInterval.
 * Computes the current alert level, updates UI, and arms/extends recording.
 */
function updateEclipseState() {
    if (!eclipseData) {
        // Clear any stale eclipse UI
        if (eclipseAlertLevel !== null) {
            eclipseAlertLevel = null;
            _hideEclipseCard();
            _hideEclipseBanner();
        }
        return;
    }

    const c1  = _parseEclipseDate(eclipseData.c1);
    const c2  = _parseEclipseDate(eclipseData.c2);
    const c3  = _parseEclipseDate(eclipseData.c3);
    const c4  = _parseEclipseDate(eclipseData.c4);
    const now = new Date();

    const secsToC1 = (c1 - now) / 1000;
    const secsToC4 = (c4 - now) / 1000;

    // Determine alert level
    let level;
    const CLEARED_WINDOW = 30 * 60; // show Cleared card for 30 min after C4
    if (secsToC4 < -CLEARED_WINDOW) {
        // Eclipse is fully over and Cleared window has passed — remove data
        eclipseData = null;
        level = null;
    } else if (secsToC4 < 0) {
        level = 'cleared';
    } else if (secsToC1 <= 0) {
        level = 'active';
    } else if (secsToC1 <= 30) {
        level = 'warning';
    } else if (secsToC1 <= 3600) {
        level = 'watch';
    } else {
        level = 'outlook';
    }

    const levelChanged = level !== eclipseAlertLevel;
    eclipseAlertLevel = level;

    if (!level) {
        _hideEclipseCard();
        _hideEclipseBanner();
        return;
    }

    // ── Banner (outlook only — or watch/warning if still showing) ────────
    updateEclipseBanner(level, c1, eclipseData);

    // ── Card (watch, warning, active, cleared) ────────────────────────────
    if (level === 'outlook') {
        _hideEclipseCard();
    } else {
        updateEclipseCard(level, c1, c2, c3, c4, secsToC1, eclipseData);
    }

    // ── Recording logic ───────────────────────────────────────────────────
    if (level === 'warning' && !isRecording) {
        // Arm: start recording at C1 − 10 s. Use a flag rather than levelChanged
        // so that a page reload mid-warning correctly reschedules the recording.
        const startDelay = Math.max(0, secsToC1 - 10);
        if (!_eclipseRecordingScheduled) {
            _eclipseRecordingScheduled = true;
            console.log(`[Eclipse] Warning — recording starts in ${startDelay.toFixed(0)}s`);
            setTimeout(() => { _eclipseRecordingScheduled = false; startEclipseRecording(c1, c4, eclipseData); }, startDelay * 1000);
        }
    } else if (level !== 'warning') {
        // Reset flag whenever we leave warning phase
        _eclipseRecordingScheduled = false;
    }

    if (level === 'active') {
        if (isRecording) {
            // Ensure recording end is pinned to C4 + 10 s
            const c4PlusTen = c4.getTime() + 10000;
            extendRecording(c4PlusTen);
        } else if (levelChanged) {
            // Eclipse became Active but we somehow aren't recording — start now
            startEclipseRecording(c1, c4, eclipseData);
        }
    }

    // Update Fire Transit button visibility (sim eclipse only)
    _updateSimEclipseFireBtn();
}

/**
 * Start a recording that spans the eclipse: begins immediately (or from C1−10s
 * if called during the warning phase), ends at C4+10s.
 */
async function startEclipseRecording(c1, c4, eclipse) {
    if (isRecording) {
        // Already recording (e.g. from an aircraft transit) — just extend
        extendRecording(c4.getTime() + 10000);
        console.log('[Eclipse] Extended existing recording to cover eclipse C4+10s');
        return;
    }
    const totalSecs = Math.max(20, Math.ceil((c4.getTime() + 10000 - Date.now()) / 1000));
    const typeLabel = eclipse.eclipse_class.charAt(0).toUpperCase() + eclipse.eclipse_class.slice(1);
    const label = `${typeLabel} ${eclipse.type === 'solar' ? 'Solar' : 'Lunar'} Eclipse`;
    console.log(`[Eclipse] Starting eclipse timelapse: ${label} — ${totalSecs}s, 1s interval`);

    if (isSimulating) {
        // In sim mode, treat like a regular recording
        startSimRecording(totalSecs);
        return;
    }

    const eclipseInterval = 1; // timelapse: 1 frame per second
    const result = await apiCall('/telescope/recording/start', 'POST', {
        duration: totalSecs,
        interval: eclipseInterval
    });
    if (result && result.success) {
        isRecording = true;
        recordingIsReal = true;    // real eclipse recording
        recordingStartTime = Date.now();
        recordingEndTime = c4.getTime() + 10000;
        const intervalInput = document.getElementById('frameInterval');
        if (intervalInput) intervalInput.value = eclipseInterval;
        updateRecordingUI();
        startRecordingTimer(totalSecs);
        showStatus(`🌙 Eclipse timelapse started: ${label} (${totalSecs}s, ${eclipseInterval}s interval)`, 'success', 8000);
    }
}

/**
 * Called by checkAutoCapture when an aircraft transit occurs during an active
 * eclipse recording.  Extends the recording and adds a ✈️ marker.
 */
function addTransitMarkerToEclipseRecording(flight, offsetMs) {
    console.log(`[Eclipse] Aircraft transit during eclipse: ${flight} at +${(offsetMs/1000).toFixed(1)}s`);
    // Add a visual marker to the active filmstrip entry if possible
    const activeEntry = document.querySelector('.filmstrip-entry.recording-active');
    if (activeEntry) {
        const marker = document.createElement('span');
        marker.className = 'ec-transit-marker';
        marker.title = `${flight} transit at +${(offsetMs/1000).toFixed(0)}s`;
        marker.textContent = '✈️';
        const meta = activeEntry.querySelector('.entry-meta') || activeEntry;
        meta.appendChild(marker);
    }
}

// ── Banner rendering ──────────────────────────────────────────────────────────

function updateEclipseBanner(level, c1, eclipse) {
    const banner = document.getElementById('eclipseBanner');
    const icon   = document.getElementById('eclipseBannerIcon');
    const text   = document.getElementById('eclipseBannerText');
    if (!banner) return;

    // Never show banner once dismissed this session
    if (eclipseBannerDismissed) {
        banner.style.display = 'none';
        return;
    }

    // Only show banner for outlook; hide once we enter watch/warning (card takes over)
    if (level !== 'outlook') {
        banner.style.display = 'none';
        return;
    }

    const isSolar = eclipse.type === 'solar';
    const typeStr = `${eclipse.eclipse_class.charAt(0).toUpperCase()}${eclipse.eclipse_class.slice(1)} ${isSolar ? 'Solar' : 'Lunar'} Eclipse`;
    const dateStr = c1.toLocaleString([], { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit', timeZoneName: 'short' });
    const hoursAway = Math.round((c1 - new Date()) / 3600000);

    banner.className = `eclipse-banner ${isSolar ? 'eclipse-solar' : 'eclipse-lunar'}`;
    icon.textContent  = isSolar ? '☀️' : '🌙';
    text.textContent  = `${typeStr} — ${dateStr}  (${hoursAway}h away) · Recording will start automatically`;
    banner.style.display = 'flex';
}

function _hideEclipseBanner() {
    const banner = document.getElementById('eclipseBanner');
    if (banner) banner.style.display = 'none';
}

function dismissEclipseBanner() {
    eclipseBannerDismissed = true;
    _hideEclipseBanner();
}

// ── Card rendering ────────────────────────────────────────────────────────────

function updateEclipseCard(level, c1, c2, c3, c4, secsToC1, eclipse) {
    const card = document.getElementById('eclipseCard');
    if (!card) return;

    const isSolar   = eclipse.type === 'solar';
    const typeClass = isSolar ? 'eclipse-solar' : 'eclipse-lunar';
    const newClass  = `eclipse-card ${level} ${typeClass}`;

    // Only rebuild the full card when the level (and therefore structure) changes.
    // On subsequent ticks just patch the countdown in-place so CSS animations
    // (pulse, fade) are not reset every second — which made numbers look frozen.
    if (card.dataset.eclipseLevel !== level) {
        card.dataset.eclipseLevel = level;

        const typeEmoji = isSolar ? '☀️' : '🌙';
        const typeStr   = `${eclipse.eclipse_class.charAt(0).toUpperCase()}${eclipse.eclipse_class.slice(1)} ${isSolar ? 'Solar' : 'Lunar'} Eclipse`;

        let labelText, phaseHtml = '';
        let showCountdown = true;

        if (level === 'watch') {
            labelText    = '🔭 Eclipse Watch';
            phaseHtml    = '<div class="ec-phase">First contact approaching</div>';
        } else if (level === 'warning') {
            labelText    = '🔴 Eclipse Warning';
            phaseHtml    = '<div class="ec-phase">Recording starting soon</div>';
        } else if (level === 'active') {
            labelText    = '🔴 Eclipse Active';
            phaseHtml    = `<div class="ec-phase" id="eclipsePhase">${renderEclipsePhase(c1, c2, c3, c4)}</div>`;
        } else if (level === 'cleared') {
            labelText    = '✅ Eclipse Complete';
            showCountdown = false;
            phaseHtml    = '<div class="ec-phase">Recording saved to filmstrip</div>';
        }

        const fmtTime = d => d ? d.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'}) : '—';
        const contactsHtml = `
            <div class="ec-contacts">
                C1 <span>${fmtTime(c1)}</span>
                ${c2 ? `· C2 <span>${fmtTime(c2)}</span> · C3 <span>${fmtTime(c3)}</span>` : ''}
                · C4 <span>${fmtTime(c4)}</span>
            </div>
            <div class="ec-clock" id="eclipseClock">🕐 ${fmtTime(new Date())}</div>`;

        card.className   = newClass;
        card.style.display = 'block';
        card.innerHTML   = `
            <div class="ec-header">
                <span class="ec-icon">${typeEmoji}</span>
                <span class="ec-label">${labelText}</span>
            </div>
            <div class="ec-type">${typeStr}</div>
            ${showCountdown ? '<div class="ec-countdown" id="eclipseCountdown"></div>' : ''}
            ${phaseHtml}
            ${contactsHtml}`;
    } else {
        // Patch className in case type class changed (shouldn't, but be safe)
        card.className = newClass;
    }

    // Always update the live countdown number in-place
    const cdEl = document.getElementById('eclipseCountdown');
    if (cdEl) {
        if (level === 'active') {
            const secsToC4 = (c4 - new Date()) / 1000;
            cdEl.textContent = secsToC4 > 0 ? `${_fmtCountdown(secsToC4)} remaining` : 'Eclipse ending…';
        } else {
            cdEl.textContent = _fmtCountdown(secsToC1);
        }
    }

    // Always update the live clock
    const clockEl = document.getElementById('eclipseClock');
    if (clockEl) {
        const now = new Date();
        clockEl.textContent = `🕐 ${now.toLocaleTimeString([], {hour:'2-digit',minute:'2-digit',second:'2-digit'})}`;
    }

    // Always update phase label during active (changes as eclipse progresses)
    if (level === 'active') {
        const phaseEl = document.getElementById('eclipsePhase');
        if (phaseEl) phaseEl.textContent = renderEclipsePhase(c1, c2, c3, c4);
    }
}

function _hideEclipseCard() {
    const card = document.getElementById('eclipseCard');
    if (card) card.style.display = 'none';
}

async function recordTransit(flight, secondsUntil) {
    // Pause timelapse during transit capture
    if (_timelapseRunning) {
        try { await fetch('/telescope/timelapse/pause', { method: 'POST' }); } catch {}
    }

    // Stop any current recording (normally checkAutoCapture handles preemption,
    // but guard here too for manual triggers)
    if (isRecording) {
        console.log('[Telescope] Interrupting current recording for transit:', flight);
        await stopRecording();
        // Wait for hardware to stop (skip in sim mode — no hardware involved)
        if (!isSimulating) await new Promise(resolve => setTimeout(resolve, 1000));
    }

    const PRE  = 10; // seconds to record before transit
    const POST = 10; // seconds to record after transit
    const totalDuration = PRE + POST; // always 20s

    // Show overlay
    transitCaptureActive = true;
    updateRecordingUI();
    const overlay = document.getElementById('transitOverlay');
    const overlayInfo = document.getElementById('transitOverlayInfo');
    if (overlay) {
        overlayInfo.textContent = `${flight} — recording starts in ${Math.max(0, secondsUntil - PRE)}s`;
        overlay.style.display = 'flex';
    }

    const startDelayMs = Math.max(0, (secondsUntil - PRE)) * 1000;

    const doRecord = async () => {
        if (!isConnected && !isSimulating) return;
        if (overlayInfo) overlayInfo.textContent = `${flight} — transit in ${PRE}s`;
        document.getElementById('videoDuration').value = totalDuration;
        document.getElementById('frameInterval').value = 0;
        await startRecording();
    };

    if (startDelayMs > 0) {
        showStatus(`⏳ Recording starts in ${Math.round(startDelayMs / 1000)}s (${PRE}s before transit)`, 'info', startDelayMs);
        clearTimeout(recordDelayTimeout);
        recordDelayTimeout = setTimeout(doRecord, startDelayMs);
    } else {
        await doRecord();
    }

    // Hide overlay after transit + post buffer, resume timelapse
    setTimeout(() => {
        if (overlay) overlay.style.display = 'none';
        transitCaptureActive = false;
        updateRecordingUI();
        // Resume timelapse if it was running
        if (_timelapseRunning) {
            fetch('/telescope/timelapse/resume', { method: 'POST' }).catch(() => {});
        }
    }, (secondsUntil + POST) * 1000);
}

function dismissTransit(flight) {
    upcomingTransits = upcomingTransits.filter(t => t.flight !== flight);
    updateTransitList();
}

// One-time UI event listeners — set up once at module load
(function() {
    const toggle = document.getElementById('autoCaptureToggle');
    if (toggle) {
        toggle.addEventListener('change', (e) => {
            localStorage.setItem('autoCaptureTransits', e.target.checked);
        });
    }
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeFileViewer();
        const tag = document.activeElement ? document.activeElement.tagName : '';
        const inInput = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT'
            || document.activeElement?.isContentEditable;
        // CMD/Ctrl + Delete: delete without confirmation in any context
        if ((e.key === 'Delete' || e.key === 'Backspace') && (e.metaKey || e.ctrlKey)) {
            // 1. File viewer lightbox is open
            const viewer = document.getElementById('fileViewer');
            if (viewer && viewer.style.display !== 'none') {
                e.preventDefault();
                viewerDelete(e);
                return;
            }
            // 2. Files grid modal is open with selected items
            const modal = document.getElementById('filesModal');
            if (modal && modal.style.display !== 'none' && gridSelection.selected.size > 0) {
                e.preventDefault();
                gridDeleteSelectedSkipConfirm();
                return;
            }
            // 3. Filmstrip has selected items
            if (filmstripSelection.selected.size > 0) {
                e.preventDefault();
                filmstripDeleteSelectedSkipConfirm();
                return;
            }
        }
        // Plain Delete: delete filmstrip selection (non-favorited skipped silently)
        if (e.key === 'Delete' && !e.metaKey && !e.ctrlKey && !inInput) {
            if (filmstripSelection.selected.size > 0) {
                e.preventDefault();
                filmstripDeleteSelectedSkipConfirm();
            }
        }
    });
})();

// ============================================================================
// FILMSTRIP & MODAL
// ============================================================================

async function toggleTelegramMute() {
    const result = await apiCall('/telescope/notifications/mute', 'POST');
    if (result !== null) updateTelegramMuteBtn(result.muted);
}

function updateTelegramMuteBtn(muted) {
    // Update header toolbar button (top bar) — no emojis
    const btn = document.getElementById('telegramMuteBtn');
    if (btn) {
        btn.textContent = muted ? 'Muted' : 'Notif';
        btn.title = muted ? 'Telegram alerts muted — click to unmute' : 'Mute Telegram alerts';
    }
    // Update sidebar panel button: .is-active = muted (locked-down keycap)
    const btn2 = document.getElementById('telegramMuteBtn2');
    if (btn2) {
        btn2.classList.toggle('is-active', !!muted);
        btn2.title = muted ? 'Telegram alerts muted — click to unmute' : 'Mute/unmute Telegram alerts';
    }
}

function toggleFilesModal() {
    const modal = document.getElementById('filesModal');
    
    if (modal.style.display === 'none' || !modal.style.display) {
        modal.style.display = 'flex';
        gridSelectNone();
        updateFilesGrid();
    } else {
        modal.style.display = 'none';
        gridSelectNone();
    }
}

function setGalleryMode(mode) {
    _galleryMode = mode;
    document.querySelectorAll('.gallery-mode-btn').forEach(b => {
        b.classList.toggle('active', b.dataset.mode === mode);
    });
    updateFilmstrip(filmstripFiles);
}

function updateFilmstrip(files) {
    const filmstrip = document.getElementById('filmstripList');
    if (!filmstrip) return;

    /* Show all files in the strip; scroll horizontally (no arbitrary cap). */
    filmstripFiles = Array.isArray(files) ? [...files] : [];
    const stripPathSet = new Set(filmstripFiles.map(f => f.path));
    for (const p of filmstripSelection.selected) {
        if (!stripPathSet.has(p)) filmstripSelection.selected.delete(p);
    }
    
    if (files.length === 0) {
        filmstripSelection.selected.clear();
        filmstripSelection.lastClicked = null;
        filmstrip.innerHTML = '<p class="empty-state">No files captured yet</p>';
        return;
    }
    
    filmstrip.innerHTML = filmstripFiles.map((file, index) => {
        const isTemp = file.isSimulation;
        const badge = isTemp ? '<span class="temp-badge">TEMP</span>' : '';
        const selected = filmstripSelection.selected.has(file.path) ? ' selected' : '';
        const itemClass = isTemp ? `filmstrip-item temp-file${selected}` : `filmstrip-item${selected}`;
        const isVideo = file.path.match(/\.(mp4|avi|mov)$/i);
        const isDiff = file.name.includes('_diff');
        const isFrame = file.name.includes('_frame');
        const imgTitle = isDiff
            ? 'Diff heatmap — shows pixel changes between frames. Bright/warm = motion. Blue = no change.'
            : isFrame
            ? 'Trigger frame — low-res detection frame at the moment motion was detected.'
            : file.name;
        const displayName = file.timelapse_frame_count
            ? `${file.name} · ${file.timelapse_frame_count} frames`
            : file.name;
        const isDetClipThumb = /\/det_[^/]+\.mp4$/i.test(file.path);
        let thumbSrc = file.thumbnail;
        if (isDetClipThumb && _galleryMode === 'diff'    && file.diff_heatmap)   thumbSrc = file.diff_heatmap;
        if (isDetClipThumb && _galleryMode === 'trigger' && file.trigger_frame)  thumbSrc = file.trigger_frame;
        const thumbnail = thumbSrc
            ? `<img src="${thumbSrc}" alt="${file.name}" title="${imgTitle}" class="filmstrip-thumbnail">`
            : isVideo
                ? `<canvas class="filmstrip-thumbnail video-thumb-canvas" data-video-src="${file.path}"></canvas>`
                : `<img src="${file.path}" alt="${file.name}" title="${imgTitle}" class="filmstrip-thumbnail">`;
        const detBadge2 = file.diff_heatmap
            ? '<span style="position:absolute; top:1px; right:1px; font-size:0.9em; filter:drop-shadow(0 0 2px #000);" title="Has heatmap">🔥</span>'
            : '';
        
        const isDetClip = /\/det_[^/]+\.mp4$/i.test(file.path);
        // Label badge — shows current TP/FP/FN state; no per-frame buttons
        const labelBadge = isDetClip
            ? `<span class="filmstrip-lbl-badge" data-det-name="${file.name}"></span>`
            : '';

        return `
        <div class="${itemClass}" data-file-path="${file.path}" data-file-idx="${index}" onclick="filmstripSelectItem(${index}, '${file.path}', event)" style="position:relative;">
            ${badge}${detBadge2}
            <div class="filmstrip-name" title="Click to rename" onclick="event.stopPropagation(); renameFavoriteFile('${file.path}', event)" style="cursor:text;">${displayName}</div>
            ${thumbnail}
            ${labelBadge}
            <div class="filmstrip-info">
                <div class="filmstrip-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
                    <button class="btn-icon" data-rename-path="${file.path}" onclick="event.stopPropagation(); renameFavoriteFile('${file.path}', event)" title="Rename" ${isTemp ? 'disabled' : ''}>✏️</button>
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download" ${isTemp ? 'disabled' : ''}>⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); filmstripTrashClick('${file.path}', '${file.name}', event)" title="${getFavorites().has(file.path) ? 'Remove favorite first' : 'Delete selected or this file (⌘/Ctrl+click to skip confirm)'}" ${isTemp || getFavorites().has(file.path) ? 'disabled' : ''}>🗑️</button>
                </div>
            </div>
        </div>
    `;
    }).join('');

    // Generate thumbnails from video first frame for any canvas placeholders
    filmstrip.querySelectorAll('canvas.video-thumb-canvas').forEach(generateVideoThumbnail);

    // Paint existing labels onto det_* items (one fetch, async)
    _paintFilmstripLabels(filmstrip);
}

const _LC = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800' };
const _LC_TEXT = { tp: '✅ TP', fp: '❌ FP', fn: '⚠️ FN' };

// Cached: stem → {label, timestamp}  — refreshed on each paint pass
let _stemLabelCache = {};

/** Build stem→{label,timestamp} map from transit-events list. */
function _buildStemMap(evts) {
    const map = {};
    for (const ev of evts) {
        if (!ev.timestamp) continue;
        const d = new Date(ev.timestamp);
        if (isNaN(d)) continue;
        const p = n => String(n).padStart(2,'0');
        const stem = `${d.getFullYear()}${p(d.getMonth()+1)}${p(d.getDate())}_${p(d.getHours())}${p(d.getMinutes())}${p(d.getSeconds())}`;
        map[stem] = { label: ev.label || '', timestamp: ev.timestamp };
    }
    return map;
}

/** Paint label badges on filmstrip items from a stem→{label} map. */
function _applyBadges(root, stemMap) {
    root.querySelectorAll('.filmstrip-lbl-badge[data-det-name]').forEach(badge => {
        const name = badge.dataset.detName || '';
        const sm = name.match(/det_(\d{8}_\d{6})/i);
        if (!sm) return;
        const info = stemMap[sm[1]];
        const lbl = info && info.label;
        if (lbl && _LC[lbl]) {
            badge.textContent = _LC_TEXT[lbl];
            badge.style.background = _LC[lbl];
            badge.style.display = 'inline-block';
        } else {
            badge.textContent = '';
            badge.style.display = 'none';
        }
    });
}

/** Paint label badges on grid items from a stem→{label} map. */
function _applyGridBadges(root, stemMap) {
    root.querySelectorAll('.file-lbl-badge[data-det-name]').forEach(badge => {
        const name = badge.dataset.detName || '';
        const sm = name.match(/det_(\d{8}_\d{6})/i);
        if (!sm) return;
        const info = stemMap[sm[1]];
        const lbl = info && info.label;
        if (lbl && _LC[lbl]) {
            badge.textContent = _LC_TEXT[lbl];
            badge.style.background = _LC[lbl];
            badge.style.display = 'inline-block';
        } else {
            badge.textContent = '';
            badge.style.display = 'none';
        }
    });
}

/** Fetch transit-events once, cache stem map, paint filmstrip + grid badges. */
async function _paintFilmstripLabels(filmstrip) {
    try {
        const resp = await fetch('/api/transit-events');
        if (!resp.ok) return;
        const evts = await resp.json();
        _stemLabelCache = _buildStemMap(evts);
        _applyBadges(filmstrip || document, _stemLabelCache);
        const grid = document.getElementById('filesGrid');
        if (grid) _applyGridBadges(grid, _stemLabelCache);
    } catch (_) {}
}

/** Resolve filename stem → event timestamp using the cache (or fetch if cold). */
async function _resolveEventTs(filename) {
    const m = filename.match(/det_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/i);
    if (!m) return null;
    const stem = `${m[1]}${m[2]}${m[3]}_${m[4]}${m[5]}${m[6]}`;

    // Use cache if available
    if (_stemLabelCache[stem]) return _stemLabelCache[stem].timestamp;

    // Cold fetch
    try {
        const resp = await fetch('/api/transit-events');
        if (!resp.ok) return null;
        const evts = await resp.json();
        _stemLabelCache = _buildStemMap(evts);
        return _stemLabelCache[stem]?.timestamp || null;
    } catch (_) { return null; }
}

/** Post a label for one event timestamp. Returns true on success. */
async function _postLabel(timestamp, label) {
    const resp = await fetch('/api/transit-events/label', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ timestamp, label: label || 'tn' }),
    });
    if (!resp.ok) throw new Error(await resp.text());
    // Keep cache consistent
    for (const stem of Object.keys(_stemLabelCache)) {
        if (_stemLabelCache[stem].timestamp === timestamp) {
            _stemLabelCache[stem].label = label;
        }
    }
    return true;
}

/** Label all selected det_* filmstrip frames with the given label ('tp','fp','fn', or '' to clear). */
async function filmstripLabelSelected(label) {
    const detFiles = filmstripFiles.filter(f =>
        filmstripSelection.selected.has(f.path) && /\/det_[^/]+\.mp4$/i.test(f.path)
    );
    if (detFiles.length === 0) { showStatus('No detection clips selected', 'warning', 2000); return; }

    let ok = 0;
    for (const f of detFiles) {
        const ts = await _resolveEventTs(f.name);
        if (!ts) continue;
        try { await _postLabel(ts, label); ok++; } catch (e) { console.error('[Label]', f.name, e); }
    }

    // Repaint badges
    const filmstrip = document.getElementById('filmstripList');
    if (filmstrip) _applyBadges(filmstrip, _stemLabelCache);
    const grid = document.getElementById('filesGrid');
    if (grid) _applyGridBadges(grid, _stemLabelCache);

    // Sync Detection Event History
    if (typeof _refreshDetectionEventHistory === 'function') _refreshDetectionEventHistory();

    if (ok > 0) showStatus(`Labeled ${ok} clip${ok > 1 ? 's' : ''} as ${label || 'cleared'}`, 'success', 2000);
}

/** Label all selected det_* grid files with the given label. */
async function gridLabelSelected(label) {
    const files = window.currentFiles || [];
    const detFiles = files.filter(f =>
        gridSelection.selected.has(f.path) && /\/det_[^/]+\.mp4$/i.test(f.path)
    );
    if (detFiles.length === 0) { showStatus('No detection clips selected', 'warning', 2000); return; }

    let ok = 0;
    for (const f of detFiles) {
        const ts = await _resolveEventTs(f.name);
        if (!ts) continue;
        try { await _postLabel(ts, label); ok++; } catch (e) { console.error('[Label]', f.name, e); }
    }

    // Repaint badges
    const filmstrip = document.getElementById('filmstripList');
    if (filmstrip) _applyBadges(filmstrip, _stemLabelCache);
    const grid = document.getElementById('filesGrid');
    if (grid) _applyGridBadges(grid, _stemLabelCache);

    // Sync Detection Event History
    if (typeof _refreshDetectionEventHistory === 'function') _refreshDetectionEventHistory();

    if (ok > 0) showStatus(`Labeled ${ok} clip${ok > 1 ? 's' : ''} as ${label || 'cleared'}`, 'success', 2000);
}

function _syncFilmstripSelectionUI() {
    const filmstrip = document.getElementById('filmstripList');
    if (!filmstrip) return;
    filmstrip.querySelectorAll('.filmstrip-item').forEach(el => {
        const path = el.dataset.filePath;
        el.classList.toggle('selected', filmstripSelection.selected.has(path));
    });

    // Show label toolbar only when at least one det_*.mp4 is selected
    const toolbar = document.getElementById('filmstripLabelToolbar');
    if (toolbar) {
        const anyDet = [...filmstripSelection.selected].some(p => /\/det_[^/]+\.mp4$/i.test(p));
        toolbar.style.display = anyDet ? 'inline-flex' : 'none';

        // Highlight the button matching a shared label across all selected det clips
        const selectedStems = filmstripFiles
            .filter(f => filmstripSelection.selected.has(f.path) && /\/det_[^/]+\.mp4$/i.test(f.path))
            .map(f => { const sm = f.name.match(/det_(\d{8}_\d{6})/i); return sm && sm[1]; })
            .filter(Boolean);
        const labels = selectedStems.map(s => (_stemLabelCache[s] && _stemLabelCache[s].label) || '');
        const sharedLabel = labels.length > 0 && labels.every(l => l === labels[0]) ? labels[0] : '';
        toolbar.querySelectorAll('.strip-lbl-btn[data-lbl]').forEach(b => {
            const lbl = b.getAttribute('data-lbl');
            const active = sharedLabel && lbl === sharedLabel;
            b.style.background  = active ? (_LC[lbl] || '#555') : '#2a2a3a';
            b.style.borderColor = active ? (_LC[lbl] || '#666') : '#444';
        });
    }
}

function filmstripSelectItem(index, path, event) {
    event.stopPropagation();
    if (event.shiftKey && filmstripSelection.lastClicked !== null) {
        const lo = Math.min(filmstripSelection.lastClicked, index);
        const hi = Math.max(filmstripSelection.lastClicked, index);
        for (let i = lo; i <= hi; i++) {
            filmstripSelection.selected.add(filmstripFiles[i].path);
        }
    } else if (event.ctrlKey || event.metaKey) {
        if (filmstripSelection.selected.has(path)) filmstripSelection.selected.delete(path);
        else filmstripSelection.selected.add(path);
    } else {
        if (filmstripSelection.selected.size === 1 && filmstripSelection.selected.has(path)) {
            const f = filmstripFiles[index];
            viewFile(f.path, f.name);
            return;
        }
        filmstripSelection.selected.clear();
        filmstripSelection.selected.add(path);
    }
    filmstripSelection.lastClicked = index;
    _syncFilmstripSelectionUI();
}

function filmstripTrashClick(path, name, event) {
    if (filmstripSelection.selected.size > 0) {
        filmstripDeleteSelectedSkipConfirm();
    } else {
        deleteFile(path, name, event.metaKey || event.ctrlKey);
    }
}

async function filmstripDeleteSelectedSkipConfirm() {
    const paths = [...filmstripSelection.selected];
    const n = paths.length;
    if (n === 0) return;
    const favs = getFavorites();
    const deletable = paths.filter(p => !favs.has(p));
    if (deletable.length === 0) {
        showStatus('Selected file(s) are favorited — remove ❤️ first', 'warning', 3000);
        return;
    }
    let deleted = 0;
    for (const filePath of deletable) {
        try {
            const p = filePath.replace('/static/', '');
            const response = await fetch('/telescope/files/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: p })
            });
            const data = await response.json();
            if (response.ok && data.success) deleted++;
        } catch (err) {
            console.error('[Filmstrip] Delete error for', filePath, err);
        }
    }
    filmstripSelection.selected.clear();
    filmstripSelection.lastClicked = null;
    showStatus(`Deleted ${deleted} of ${n} file${n > 1 ? 's' : ''}`, deleted > 0 ? 'success' : 'error', 3000);
    await refreshFiles();
}

// ── Multi-select state for expanded files grid ──
const gridSelection = {
    selected: new Set(),   // set of file paths
    lastClicked: null,     // index of last clicked item (for shift-range)
    dragging: false,       // lasso drag active
    startX: 0, startY: 0, // lasso origin (page coords)
};

function gridSelectItem(index, path, event) {
    event.stopPropagation();
    const files = window.currentFiles || [];
    if (event.shiftKey && gridSelection.lastClicked !== null) {
        // Range select
        const lo = Math.min(gridSelection.lastClicked, index);
        const hi = Math.max(gridSelection.lastClicked, index);
        for (let i = lo; i <= hi; i++) gridSelection.selected.add(files[i].path);
    } else if (event.ctrlKey || event.metaKey) {
        // Toggle single
        if (gridSelection.selected.has(path)) gridSelection.selected.delete(path);
        else gridSelection.selected.add(path);
    } else {
        // Plain click — if already the only selection, open viewer; else select just this
        if (gridSelection.selected.size === 1 && gridSelection.selected.has(path)) {
            const f = files[index];
            viewFile(f.path, f.name);
            return;
        }
        gridSelection.selected.clear();
        gridSelection.selected.add(path);
    }
    gridSelection.lastClicked = index;
    _syncGridSelectionUI();
}

function gridSelectAll() {
    const files = window.currentFiles || [];
    files.forEach(f => gridSelection.selected.add(f.path));
    _syncGridSelectionUI();
}

function gridSelectNone() {
    gridSelection.selected.clear();
    gridSelection.lastClicked = null;
    _syncGridSelectionUI();
}

async function gridDeleteSelected(e) {
    const paths = [...gridSelection.selected];
    const n = paths.length;
    if (n === 0) return;
    const skipConfirm = e && (e.metaKey || e.ctrlKey);
    const favs = getFavorites();
    const protected_ = paths.filter(p => favs.has(p));
    const deletable = paths.filter(p => !favs.has(p));
    if (protected_.length > 0 && deletable.length === 0) {
        showStatus(`${protected_.length} file${protected_.length > 1 ? 's are' : ' is'} favorited — remove ❤️ first`, 'warning', 4000);
        return;
    }
    if (!skipConfirm) {
        const msg = protected_.length > 0
            ? `Delete ${deletable.length} file${deletable.length > 1 ? 's' : ''}? (${protected_.length} favorited file${protected_.length > 1 ? 's' : ''} will be skipped)`
            : `Delete ${deletable.length} file${deletable.length > 1 ? 's' : ''}?`;
        if (!confirm(msg)) return;
    }
    let deleted = 0;
    for (const filePath of deletable) {
        try {
            const p = filePath.replace('/static/', '');
            const response = await fetch('/telescope/files/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: p })
            });
            const data = await response.json();
            if (response.ok && data.success) deleted++;
            else console.warn('[Grid] Delete failed for', p, data.error);
        } catch (err) {
            console.error('[Grid] Delete error for', filePath, err);
        }
    }
    gridSelection.selected.clear();
    gridSelection.lastClicked = null;
    showStatus(`Deleted ${deleted} of ${n} file${n > 1 ? 's' : ''}`, deleted > 0 ? 'success' : 'error', 3000);
    await refreshFiles();
    updateFilesGrid();
}

async function gridDeleteSelectedSkipConfirm() {
    const paths = [...gridSelection.selected];
    const n = paths.length;
    if (n === 0) return;
    const favs = getFavorites();
    const deletable = paths.filter(p => !favs.has(p));
    if (deletable.length === 0) return;
    let deleted = 0;
    for (const filePath of deletable) {
        try {
            const p = filePath.replace('/static/', '');
            const response = await fetch('/telescope/files/delete', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ path: p })
            });
            const data = await response.json();
            if (response.ok && data.success) deleted++;
        } catch (err) {
            console.error('[Grid] Delete error for', filePath, err);
        }
    }
    gridSelection.selected.clear();
    gridSelection.lastClicked = null;
    showStatus(`Deleted ${deleted} of ${n} file${n > 1 ? 's' : ''}`, deleted > 0 ? 'success' : 'error', 3000);
    await refreshFiles();
    updateFilesGrid();
}

async function importVideoFile(input) {
    if (!input || !input.files.length) return;
    const files = Array.from(input.files);
    input.value = '';  // reset so same files can be re-selected

    const isAllowed = f => /\.(mp4|jpg|jpeg|png)$/i.test(f.name);
    const isImage   = f => /\.(jpg|jpeg|png)$/i.test(f.name);
    const sizeLimit = f => isImage(f) ? 50 * 1024 * 1024 : 500 * 1024 * 1024;
    const sizeLabel = f => isImage(f) ? '50 MB' : '500 MB';

    const invalid  = files.filter(f => !isAllowed(f));
    if (invalid.length) {
        showStatus(`Only .mp4, .jpg, .jpeg, .png files are accepted (skipping: ${invalid.map(f=>f.name).join(', ')})`, 'error', 5000);
    }
    const toUpload = files.filter(f => isAllowed(f) && f.size <= sizeLimit(f));
    const tooLarge = files.filter(f => isAllowed(f) && f.size > sizeLimit(f));
    if (tooLarge.length) showStatus(`Skipping ${tooLarge.length} file(s) over size limit (${tooLarge.map(f => `${f.name} — max ${sizeLabel(f)}`).join(', ')})`, 'warning', 5000);
    if (!toUpload.length) return;

    let uploaded = 0;
    for (let i = 0; i < toUpload.length; i++) {
        const file = toUpload[i];
        showStatus(`Uploading ${file.name} (${i + 1} of ${toUpload.length})…`, 'info', 0);
        try {
            const formData = new FormData();
            formData.append('file', file);
            const resp = await fetch('/telescope/files/upload', { method: 'POST', body: formData });
            const data = await resp.json();
            if (resp.ok && data.success) {
                uploaded++;
            } else {
                showStatus(`Import failed for ${file.name}: ${data.error || resp.statusText}`, 'error', 5000);
            }
        } catch (err) {
            showStatus(`Import error for ${file.name}: ${err.message}`, 'error', 5000);
        }
    }

    if (uploaded > 0) {
        showStatus(`Imported ${uploaded} of ${toUpload.length} file${toUpload.length > 1 ? 's' : ''}`, 'success', 4000);
        await refreshFiles();
        updateFilesGrid();
    }
}

function gridDownloadSelected() {
    const files = window.currentFiles || [];
    for (const f of files) {
        if (gridSelection.selected.has(f.path)) downloadFile(f.path, f.name);
    }
}

function gridFavoriteSelected() {
    const paths = [...gridSelection.selected];
    if (paths.length === 0) return;
    const favs = getFavorites();
    const allFav = paths.every(p => favs.has(p));
    const shouldFavorite = !allFav;
    _setFavoriteForPaths(paths, shouldFavorite);
    showStatus(`${shouldFavorite ? 'Favorited' : 'Unfavorited'} ${paths.length} file${paths.length > 1 ? 's' : ''}`, 'success', 2000);
}

function _syncGridSelectionUI() {
    const grid = document.getElementById('filesGrid');
    if (!grid) return;
    grid.querySelectorAll('.file-item').forEach(el => {
        const path = el.dataset.filePath;
        el.classList.toggle('selected', gridSelection.selected.has(path));
    });
    // Update toolbar
    const toolbar = document.getElementById('gridSelectionToolbar');
    if (toolbar) {
        const n = gridSelection.selected.size;
        toolbar.style.display = n > 0 ? 'flex' : 'none';
        const cnt = document.getElementById('gridSelCount');
        if (cnt) cnt.textContent = `${n} selected`;
    }
}

// Lasso drag-select on the files grid
function _initGridLasso() {
    const grid = document.getElementById('filesGrid');
    if (!grid || grid._lassoInit) return;
    grid._lassoInit = true;

    let lasso = null;

    grid.addEventListener('mousedown', e => {
        // Only start lasso from the grid background (not from items/buttons)
        if (e.target !== grid) return;
        e.preventDefault();
        gridSelection.dragging = true;
        gridSelection.startX = e.pageX;
        gridSelection.startY = e.pageY;
        if (!e.ctrlKey && !e.metaKey && !e.shiftKey) gridSelection.selected.clear();
        lasso = document.createElement('div');
        lasso.className = 'grid-lasso';
        document.body.appendChild(lasso);
    });

    document.addEventListener('mousemove', e => {
        if (!gridSelection.dragging || !lasso) return;
        const x1 = Math.min(gridSelection.startX, e.pageX);
        const y1 = Math.min(gridSelection.startY, e.pageY);
        const x2 = Math.max(gridSelection.startX, e.pageX);
        const y2 = Math.max(gridSelection.startY, e.pageY);
        Object.assign(lasso.style, {
            left: x1 + 'px', top: y1 + 'px',
            width: (x2 - x1) + 'px', height: (y2 - y1) + 'px',
        });
        // Hit-test items
        const rect = { left: x1, top: y1, right: x2, bottom: y2 };
        grid.querySelectorAll('.file-item').forEach(el => {
            const r = el.getBoundingClientRect();
            const ir = { left: r.left + window.scrollX, top: r.top + window.scrollY,
                         right: r.right + window.scrollX, bottom: r.bottom + window.scrollY };
            const hit = !(ir.right < rect.left || ir.left > rect.right ||
                          ir.bottom < rect.top || ir.top > rect.bottom);
            if (hit) gridSelection.selected.add(el.dataset.filePath);
            else if (!e.ctrlKey && !e.metaKey) gridSelection.selected.delete(el.dataset.filePath);
            el.classList.toggle('selected', gridSelection.selected.has(el.dataset.filePath));
        });
    });

    document.addEventListener('mouseup', () => {
        if (!gridSelection.dragging) return;
        gridSelection.dragging = false;
        if (lasso) { lasso.remove(); lasso = null; }
        _syncGridSelectionUI();
    });
}

function updateFilesGrid() {
    const grid = document.getElementById('filesGrid');
    if (!grid) return;
    
    // Get files from the global state (refreshed by refreshFiles)
    const files = window.currentFiles || [];
    
    if (files.length === 0) {
        grid.innerHTML = '<p class="empty-state">No files</p>';
        gridSelection.selected.clear();
        _syncGridSelectionUI();
        return;
    }
    
    // Prune selection of paths that no longer exist
    const pathSet = new Set(files.map(f => f.path));
    for (const p of gridSelection.selected) { if (!pathSet.has(p)) gridSelection.selected.delete(p); }

    grid.innerHTML = files.map((file, idx) => {
        const isVideo = file.path.match(/\.(mp4|avi|mov)$/i);
        const sel = gridSelection.selected.has(file.path) ? ' selected' : '';
        const isDiff = file.name.includes('_diff');
        const isFrame = file.name.includes('_frame');
        const imgTitle = isDiff
            ? 'Diff heatmap — shows pixel changes between frames. Bright/warm areas = motion detected. Blue = no change. Used to visualise what triggered a detection event.'
            : isFrame
            ? 'Trigger frame — the low-res detection frame captured at the moment motion was detected.'
            : file.name;
        const displayName = file.timelapse_frame_count
            ? `${file.name} · ${file.timelapse_frame_count} frames`
            : file.name;
        const thumbnail = file.thumbnail
            ? `<img src="${file.thumbnail}" alt="${file.name}" title="${imgTitle}" class="file-thumbnail">`
            : isVideo
                ? `<canvas class="file-thumbnail video-thumb-canvas" data-video-src="${file.path}"></canvas>`
                : `<img src="${file.path}" alt="${file.name}" title="${imgTitle}" class="file-thumbnail">`;
        const detBadge = file.diff_heatmap
            ? `<span style="position:absolute; top:2px; right:2px; font-size:1.1em; filter:drop-shadow(0 0 2px #000);" title="Detection has heatmap &amp; trigger frame">🔥</span>`
            : '';
        const isDetClipGrid = /\/det_[^/]+\.mp4$/i.test(file.path);
        const gridLabelBadge = isDetClipGrid
            ? `<span class="file-lbl-badge" data-det-name="${file.name}" style="display:none;"></span>`
            : '';
        return `
        <div class="file-item${sel}" data-file-path="${file.path}" data-file-idx="${idx}"
             onclick="gridSelectItem(${idx}, '${file.path}', event)" style="position:relative;">
            ${detBadge}${gridLabelBadge}
            <div class="file-info">
                <span class="file-name" title="Click to rename" onclick="event.stopPropagation(); renameFavoriteFile('${file.path}', event)" style="cursor:text;">${displayName}</span>
                <div class="file-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
                    <button class="btn-icon" data-rename-path="${file.path}" onclick="event.stopPropagation(); renameFavoriteFile('${file.path}', event)" title="Rename">✏️</button>
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download">⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); deleteFile('${file.path}', '${file.name}', event.metaKey || event.ctrlKey)" title="${getFavorites().has(file.path) ? 'Remove favorite first' : 'Delete (⌘/Ctrl+click to skip confirm)'}" ${getFavorites().has(file.path) ? 'disabled' : ''}>🗑️</button>
                </div>
            </div>
            ${thumbnail}
        </div>
    `}).join('');

    // Generate thumbnails from video first frame for any canvas placeholders
    grid.querySelectorAll('canvas.video-thumb-canvas').forEach(generateVideoThumbnail);
    _initGridLasso();
    _syncGridSelectionUI();
    // Paint labels — use cache if warm, else fetch
    if (Object.keys(_stemLabelCache).length > 0) {
        _applyGridBadges(grid, _stemLabelCache);
    } else {
        _paintFilmstripLabels(null); // fetches and paints both filmstrip + grid
    }
}

// Generate a thumbnail from a video's first frame onto a <canvas>
function generateVideoThumbnail(canvas) {
    const src = canvas.dataset.videoSrc;
    if (!src) return;
    const video = document.createElement('video');
    video.muted = true;
    video.playsInline = true;
    video.preload = 'metadata';
    let done = false;

    function _fallback() {
        if (done) return;
        done = true;
        video.src = '';
        canvas.width = 192; canvas.height = 108;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, 192, 108);
        ctx.font = '36px serif';
        ctx.textAlign = 'center';
        ctx.fillText('🎬', 96, 66);
    }

    // Safety timeout — if seeked never fires (e.g. 416 range error), fall back
    const _timeout = setTimeout(_fallback, 5000);

    video.addEventListener('loadeddata', () => {
        video.currentTime = 0.5; // seek to 0.5s to avoid blank first frame
    });
    video.addEventListener('seeked', () => {
        if (done) return;
        done = true;
        clearTimeout(_timeout);
        canvas.width = video.videoWidth || 192;
        canvas.height = video.videoHeight || 108;
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        video.src = ''; // free memory
    }, { once: true });
    video.addEventListener('error', _fallback);

    video.src = src;
}

// Track viewer state for navigation
var _viewerIndex = -1;
var _viewerFile = null;  // { path, name } of the currently open file
var _loopSegment = null; // { start, end } for segment looping, or true for full loop
var _markedFrames = new Set(); // frame indices marked for composite
var _trimIn = null;  // trim in-point (seconds), set by [ In button
var _trimOut = null; // trim out-point (seconds), set by Out ] button
var _videoFps = 30; // detected fps of current video
var _scrubSlider = null; // reference to the range input
// Isolation result for det_*.mp4 clips (transit spans / scores from backend)
var _isolateResult = null;

/** Build HTML strip showing diff heatmap and trigger frame beside the video. */
function _buildCompanionStrip(fileInfo) {
    const parts = [];
    const imgStyle = 'width:120px; height:auto; object-fit:contain; border:1px solid #555; border-radius:3px; cursor:pointer; background:#000;';
    if (fileInfo.diff_heatmap) {
        parts.push(
            `<div style="text-align:center;">` +
            `<div style="color:#f80; font-size:0.6em; margin-bottom:1px;">🔥 Heatmap</div>` +
            `<img src="${fileInfo.diff_heatmap}?t=${Date.now()}" alt="Diff heatmap" ` +
            `title="Diff heatmap — bright/warm pixels show motion that triggered detection." ` +
            `style="${imgStyle}" ` +
            `onclick="window.open('${fileInfo.diff_heatmap}','_blank')">` +
            `</div>`);
    }
    if (fileInfo.trigger_frame) {
        parts.push(
            `<div style="text-align:center;">` +
            `<div style="color:#0cf; font-size:0.6em; margin-bottom:1px;">📷 Trigger</div>` +
            `<img src="${fileInfo.trigger_frame}?t=${Date.now()}" alt="Trigger frame" ` +
            `title="Trigger frame — low-res detection frame at moment of detection." ` +
            `style="${imgStyle}" ` +
            `onclick="window.open('${fileInfo.trigger_frame}','_blank')">` +
            `</div>`);
    }
    if (parts.length === 0) return '';
    return parts.join('');
}

// ---------------------------------------------------------------------------
// Transit isolation for det_*.mp4 clips (lightweight, no disk-detect needed)
// ---------------------------------------------------------------------------

/**
 * Call the backend isolate-transit endpoint for a det_*.mp4 clip.
 * On success, draws red span marks on markedFrameBar and seeks the
 * hidden video to the peak transit frame without forcing loop playback.
 */
async function _runIsolateTransit(apiPath, peakHint) {
    _isolateResult = null;
    try {
        const body = { path: apiPath };
        if (peakHint != null) body.peak_time_s = peakHint;
        const resp = await fetch('/telescope/files/isolate-transit', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(body),
        });
        if (!resp.ok) return;
        const data = await resp.json();
        if (data.error) { console.warn('[isolate]', data.error); return; }
        _isolateResult = data;
        _drawTransitSpans();
        // Add Prev/Next buttons (idempotent)
        _ensureTransitNavButtons();
        // Seek near peak, but do not force looping playback.
        const vid = document.getElementById('hiddenVid');
        if (vid && data.peak_time_s != null) {
            const ps = parseFloat(data.peak_time_s);
            const duration = _safeVideoDuration(vid);
            _loopSegment = null;
            vid.pause();
            vid.currentTime = duration > 0
                ? Math.min(Math.max(0, ps), Math.max(0, duration - 0.001))
                : Math.max(0, ps);
        }
    } catch (err) {
        console.warn('[isolate] error:', err);
    }
}

/**
 * Draw red transit-span tick marks on markedFrameBar.
 * Yellow ticks (user-marked frames) are preserved alongside.
 * Also draws cyan In/Out trim markers.
 */
function _drawTransitSpans() {
    const bar = document.getElementById('markedFrameBar');
    if (!bar) return;
    const total = _frameTotalCount || 1;
    // Re-render yellow user marks first
    bar.innerHTML = '';
    for (const f of _markedFrames) {
        const pct = (f / Math.max(1, total - 1)) * 100;
        const tick = document.createElement('div');
        tick.style.cssText = `position:absolute; left:${pct}%; top:0; width:2px; height:100%; background:#fd0; border-radius:1px; cursor:pointer;`;
        tick.title = `Marked frame ${f}`;
        tick.onclick = () => { const v = document.querySelector('#fileViewerBody video'); if (v) { v.pause(); v.currentTime = f / _videoFps; } };
        bar.appendChild(tick);
    }
    // Trim In/Out markers (cyan)
    if (_trimIn !== null) {
        const inFrame = Math.round(_trimIn * _videoFps);
        const pct = (inFrame / Math.max(1, total - 1)) * 100;
        const inMark = document.createElement('div');
        inMark.style.cssText = `position:absolute; left:${pct}%; top:0; width:3px; height:100%; background:#0ff; border-radius:1px; cursor:pointer; z-index:10;`;
        inMark.title = `Trim In: ${_trimIn.toFixed(2)}s (frame ${inFrame})`;
        inMark.onclick = () => { const v = document.querySelector('#fileViewerBody video'); if (v) { v.pause(); v.currentTime = _trimIn; } };
        bar.appendChild(inMark);
    }
    if (_trimOut !== null) {
        const outFrame = Math.round(_trimOut * _videoFps);
        const pct = (outFrame / Math.max(1, total - 1)) * 100;
        const outMark = document.createElement('div');
        outMark.style.cssText = `position:absolute; left:${pct}%; top:0; width:3px; height:100%; background:#0ff; border-radius:1px; cursor:pointer; z-index:10;`;
        outMark.title = `Trim Out: ${_trimOut.toFixed(2)}s (frame ${outFrame})`;
        outMark.onclick = () => { const v = document.querySelector('#fileViewerBody video'); if (v) { v.pause(); v.currentTime = _trimOut; } };
        bar.appendChild(outMark);
    }
    // Optional: draw a highlighted region between In/Out
    if (_trimIn !== null && _trimOut !== null) {
        const inFrame = Math.round(_trimIn * _videoFps);
        const outFrame = Math.round(_trimOut * _videoFps);
        const pctL = (inFrame / Math.max(1, total - 1)) * 100;
        const pctW = Math.max(0.5, ((outFrame - inFrame) / Math.max(1, total - 1)) * 100);
        const region = document.createElement('div');
        region.style.cssText = `position:absolute; left:${pctL}%; top:0; width:${pctW}%; height:100%; background:rgba(0,255,255,0.15); pointer-events:none;`;
        region.title = 'Trim region';
        bar.appendChild(region);
    }
    // Red transit spans
    if (!_isolateResult || !_isolateResult.spans) return;
    for (const [s, e] of _isolateResult.spans) {
        const pctL = (s / Math.max(1, total - 1)) * 100;
        const pctW = Math.max(0.5, ((e - s + 1) / Math.max(1, total - 1)) * 100);
        const span = document.createElement('div');
        span.style.cssText = `position:absolute; left:${pctL}%; top:0; width:${pctW}%; height:100%; background:rgba(255,60,60,0.7); border-radius:2px; cursor:pointer;`;
        span.title = `Transit span: frames ${s}–${e}`;
        span.onclick = () => { const v = document.querySelector('#fileViewerBody video'); if (v) { v.pause(); v.currentTime = s / _videoFps; } };
        bar.appendChild(span);
    }
    // Peak frame indicator (bright green)
    if (_isolateResult.peak_frame != null) {
        const pct = (_isolateResult.peak_frame / Math.max(1, total - 1)) * 100;
        const pk = document.createElement('div');
        pk.style.cssText = `position:absolute; left:${pct}%; top:0; width:3px; height:100%; background:#0f0; border-radius:1px; cursor:pointer;`;
        pk.title = `Peak transit frame ${_isolateResult.peak_frame}`;
        pk.onclick = () => { const v = document.querySelector('#fileViewerBody video'); if (v) { v.pause(); v.currentTime = _isolateResult.peak_frame / _videoFps; } };
        bar.appendChild(pk);
    }
}

/** Inject Prev/Next transit-span buttons into the scrubber control row. */
function _ensureTransitNavButtons() {
    if (document.getElementById('transitPrevBtn')) return; // already there
    const counter = document.getElementById('frameCounter');
    if (!counter || !counter.parentNode) return;
    const prev = document.createElement('button');
    prev.id = 'transitPrevBtn';
    prev.className = 'btn-viewer';
    prev.style.cssText = 'font-size:0.8em; padding:2px 7px;';
    prev.title = 'Jump to previous transit span';
    prev.textContent = '⏮ Transit';
    prev.onclick = () => _jumpToTransitSpan(-1);
    const next = document.createElement('button');
    next.id = 'transitNextBtn';
    next.className = 'btn-viewer';
    next.style.cssText = 'font-size:0.8em; padding:2px 7px;';
    next.title = 'Jump to next transit span';
    next.textContent = 'Transit ⏭';
    next.onclick = () => _jumpToTransitSpan(1);
    counter.parentNode.insertBefore(prev, counter.nextSibling);
    counter.parentNode.insertBefore(next, prev.nextSibling);
}

/** Jump to the previous (-1) or next (+1) transit span relative to current frame. */
function _jumpToTransitSpan(dir) {
    if (!_isolateResult || !_isolateResult.spans || !_isolateResult.spans.length) return;
    const vid = document.getElementById('hiddenVid');
    if (!vid) return;
    const spans = _isolateResult.spans;
    const cur = _currentFrame;
    let target = null;
    if (dir > 0) {
        // next span whose start > cur
        target = spans.find(([s]) => s > cur);
        if (!target) target = spans[0]; // wrap
    } else {
        // previous span whose end < cur
        const prev = [...spans].reverse().find(([, e]) => e < cur);
        target = prev || spans[spans.length - 1]; // wrap
    }
    if (target) {
        vid.pause();
        const [s, e] = target;
        const mid = (s + e) / 2;
        vid.currentTime = Math.max(0, mid / _videoFps - 0.2);
        _loopSegment = { start: Math.max(0, s / _videoFps - 0.2), end: e / _videoFps + 0.4 };
        vid.play().catch(() => {});
    }
}


// ---------------------------------------------------------------------------
// Sidecar signal chart — uses live-detector data from det_*.json
// ---------------------------------------------------------------------------

/**
 * Load the enriched sidecar and populate transit marks + signal chart.
 * The sidecar.signal field contains scores_a/b, thresh_a/b, triggered,
 * and transit_hires_frame — all computed by the live detector at fire time.
 */
function _loadSidecarSignal(sidecar, videoPath) {
    const sig = sidecar.signal || {};
    const hiresFps = 30;                 // hires buffer is always 30fps
    const analysisFps = sig.analysis_fps || 15;
    const step = Math.round(hiresFps / analysisFps);  // detector frames → hires frames

    // Build _isolateResult from sidecar data so scrub bar marks work
    const triggered = Array.isArray(sig.triggered) ? sig.triggered : [];
    const anyTriggered = triggered.some(Boolean);
    const spans = [];
    let inSpan = false, spanStart = 0;
    for (let i = 0; i < triggered.length; i++) {
        if (triggered[i] && !inSpan) { inSpan = true; spanStart = i; }
        else if (!triggered[i] && inSpan) {
            inSpan = false;
            if (i - spanStart >= 1) spans.push([spanStart * step, (i - 1) * step]);
        }
    }
    if (inSpan) spans.push([spanStart * step, (triggered.length - 1) * step]);

    const transitHiresFrame = Number.isFinite(sig.transit_hires_frame)
        ? Math.round(sig.transit_hires_frame)
        : null;
    const peakTimeS = Number.isFinite(sidecar.peak_time_s)
        ? Number(sidecar.peak_time_s)
        : (transitHiresFrame != null ? transitHiresFrame / hiresFps : null);
    let resolvedSpans = spans.slice();
    if (resolvedSpans.length === 0 && anyTriggered && transitHiresFrame != null) {
        resolvedSpans = [[Math.max(0, transitHiresFrame - 8), transitHiresFrame + 8]];
    }
    const stemMatch = String(videoPath || '').match(/det_(\d{8}_\d{6})/i);
    const manualLabel = stemMatch ? ((_stemLabelCache[stemMatch[1]] && _stemLabelCache[stemMatch[1]].label) || '') : '';
    if (manualLabel === 'fp') {
        resolvedSpans = [];
    }

    _isolateResult = {
        spans: resolvedSpans,
        peak_frame: transitHiresFrame,
        peak_time_s: peakTimeS,
    };
    _drawTransitSpans();
    if (_isolateResult.spans && _isolateResult.spans.length > 0) {
        _ensureTransitNavButtons();
    }

    // Seek to transit, but do not force loop playback.
    const vid = document.getElementById('hiddenVid');
    if (vid && _isolateResult.peak_time_s != null && _isolateResult.spans.length > 0) {
        const ps = _isolateResult.peak_time_s;
        const duration = _safeVideoDuration(vid);
        _loopSegment = null;
        vid.pause();
        vid.currentTime = duration > 0
            ? Math.min(Math.max(0, ps), Math.max(0, duration - 0.001))
            : Math.max(0, ps);
    }

    // Confidence banner
    const conf = sig.confidence_score != null ? `${Math.round(sig.confidence_score * 100)}%` : '';
    const gate = sig.gate_detail || sig.gate_type || '';
    const cnn = sig.cnn_confidence != null ? ` · CNN ${Math.round(sig.cnn_confidence * 100)}%` : '';
    if (_isolateResult.spans.length > 0) {
        _setScanBanner('success', `✅ Live detection: ${gate} · confidence ${conf}${cnn}`);
    } else if (manualLabel === 'fp') {
        _setScanBanner('warning', 'Clip is labeled False Positive — transit spans hidden');
    } else {
        _setScanBanner('warning', 'No sustained transit span detected in this clip');
    }

    // Draw signal chart
    _drawSidecarSignalChart(sig, step);
}

/** Render the live-detector signal_a / signal_b history chart below the scrub bar. */
function _drawSidecarSignalChart(sig, step) {
    const root = document.getElementById('frameViewerRoot');
    if (!root) return;

    let card = document.getElementById('replaySignalCard');
    if (!card) {
        card = document.createElement('div');
        card.id = 'replaySignalCard';
        card.style.cssText = 'width:100%; padding:4px 12px 6px; background:#111; border-top:1px solid #333; flex-shrink:0;';
        card.innerHTML =
            '<div style="color:#888; font-size:0.7em; margin-bottom:2px;">📊 Live-detector signal — ' +
            '<span style="color:#0cf;">A</span> consec diff · ' +
            '<span style="color:#f80;">B</span> EMA/wavelet · ' +
            '<span style="color:#0cf; opacity:0.5;">─ ─</span> threshold A · ' +
            '<span style="color:#f80; opacity:0.5;">─ ─</span> threshold B</div>' +
            '<canvas id="replaySignalCanvas" style="width:100%; height:56px; display:block;"></canvas>';
        const scrubber = document.getElementById('frameScrubber');
        if (scrubber && scrubber.parentNode) {
            scrubber.parentNode.insertBefore(card, scrubber.nextSibling);
        } else {
            root.appendChild(card);
        }
    }

    const canvas = document.getElementById('replaySignalCanvas');
    if (!canvas) return;
    const W = canvas.offsetWidth || 600;
    const H = 56;
    canvas.width = W;
    canvas.height = H;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#111';
    ctx.fillRect(0, 0, W, H);

    const sa = sig.scores_a || [];
    const sb = sig.scores_b || [];
    const ta_val = sig.thresh_a || 0;
    const tb_val = sig.thresh_b || 0;
    const triggered = sig.triggered || [];
    const n = Math.max(sa.length, sb.length);
    if (n < 2) return;

    const ta = new Array(n).fill(ta_val);
    const tb = new Array(n).fill(tb_val);

    const allVals = [...sa, ...sb, ta_val, tb_val].filter(v => v > 0);
    const maxVal = Math.max(...allVals, 1e-6);
    const toY = v => H - 2 - Math.round((Math.min(v, maxVal) / maxVal) * (H - 4));
    const toX = i => Math.round((i / Math.max(n - 1, 1)) * (W - 1));

    // Shade triggered spans
    ctx.fillStyle = 'rgba(255,60,60,0.2)';
    let inSpan = false, spanX = 0;
    for (let i = 0; i < triggered.length; i++) {
        if (triggered[i] && !inSpan) { inSpan = true; spanX = toX(i); }
        else if (!triggered[i] && inSpan) { inSpan = false; ctx.fillRect(spanX, 0, toX(i) - spanX, H); }
    }
    if (inSpan) ctx.fillRect(spanX, 0, W - spanX, H);

    // Transit event marker (brighter)
    const peakDetFrame = sig.trigger_det_frame;
    if (peakDetFrame != null && peakDetFrame < n) {
        const px = toX(peakDetFrame);
        ctx.fillStyle = 'rgba(255,80,80,0.5)';
        ctx.fillRect(Math.max(0, px - 6), 0, 12, H);
        ctx.fillStyle = '#fff';
        ctx.font = '9px monospace';
        ctx.fillText('⚡', px - 4, 9);
    }

    const drawLine = (arr, color, dash) => {
        ctx.beginPath();
        ctx.strokeStyle = color;
        ctx.lineWidth = 1.2;
        ctx.setLineDash(dash ? [3, 3] : []);
        for (let i = 0; i < arr.length; i++) {
            const x = toX(i), y = toY(arr[i]);
            if (i === 0) ctx.moveTo(x, y); else ctx.lineTo(x, y);
        }
        ctx.stroke();
    };

    drawLine(ta, 'rgba(0,200,255,0.4)', true);
    drawLine(tb, 'rgba(255,136,0,0.4)', true);
    drawLine(sa, '#0cf', false);
    drawLine(sb, '#f80', false);
    ctx.setLineDash([]);

    // Playhead cursor — updated on scrub
    canvas._step = step;
    canvas._n = n;
}

/** Update the playhead cursor on the signal chart as the user scrubs. */
function _updateSidecarChartCursor() {
    const canvas = document.getElementById('replaySignalCanvas');
    if (!canvas || !canvas._n) return;
    // Lightweight redraw: the chart base is already painted; just draw/erase the cursor
    // by triggering a full redraw via _drawSidecarSignalChart with cached sig
    // (expensive for large arrays — skip cursor update if n > 1000)
    if (canvas._n > 500) return;
    const W = canvas.width, H = canvas.height;
    const ctx = canvas.getContext('2d');
    const detFrame = Math.round(_currentFrame / Math.max(1, canvas._step || 2));
    const x = Math.round((detFrame / Math.max(canvas._n - 1, 1)) * (W - 1));
    // We can't easily erase just the cursor without full redraw, so skip live cursor
    // The chart is static and the scrub bar already shows position.
}

function viewFile(path, name, opts) {
    opts = opts || {};
    // Strip any cache-bust query string for file list lookups; keep it for video src
    const videoSrc = path;
    path = path.split('?')[0];
    name = name || path.split('/').pop();
    const files = window.currentFiles || [];
    _viewerIndex = files.findIndex(f => f.path === path);
    _viewerFile = { path, name };

    const isVideo = /\.(mp4|avi|mov|mkv|webm)$/i.test(name);
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    const nameEl = document.getElementById('fileViewerName');

    // Hide the files grid modal while the viewer is open to prevent
    // z-index overlap issues where clicks fall through to the grid.
    const filesModal = document.getElementById('filesModal');
    if (filesModal && filesModal.style.display !== 'none') {
        viewer._filesModalWasOpen = true;
        filesModal.style.display = 'none';
    }
    const actionsEl = document.getElementById('fileViewerActions');

    nameEl.textContent = name;
    _setScanBanner(null); // clear any previous scan result
    _loopSegment = null;
    _markedFrames = new Set();
    _trimIn = null;
    _trimOut = null;
    _lastTrimOrigPath = null;
    _scrubPlayStop();
    _isolateResult = null;
    _scrubSlider = null;
    // Resolve companion images for this file
    const curFileInfo = files.find(f => f.path === path) || {};
    const companionHtml = _buildCompanionStrip(curFileInfo);

    if (isVideo) {
        const loopAttr = opts.loop ? ' loop' : '';
        body.innerHTML =
            `<div style="display:flex; flex-direction:column; width:100%;" id="frameViewerRoot">` +
              `<video src="${videoSrc}" playsinline${loopAttr} muted style="position:absolute;left:-9999px;width:1px;height:1px;" id="hiddenVid"></video>` +
              `<div style="position:relative; flex-shrink:0;">` +
                `<div id="fivePanel" style="display:flex; justify-content:center; align-items:center; gap:3px; padding:4px 4px 0; background:#000;">` +
                  `${[-2,-1,0,1,2].map(off => {
                    const border = off === 0 ? '3px solid #0ff' : '3px solid #333';
                    return `<div class="fp-slot" data-fp-offset="${off}" style="position:relative; flex:1; min-width:0; border:${border}; border-radius:4px; cursor:pointer; overflow:hidden;" title="">` +
                      `<div class="thumb-placeholder" style="width:100%; padding-top:75%; background:#111;"></div>` +
                      `<div class="fp-label" style="position:absolute;bottom:2px;left:0;right:0;text-align:center;color:#888;font-size:0.75em;font-family:monospace;text-shadow:0 0 4px #000;"></div>` +
                      `<div class="fp-mark-dot" style="display:none;position:absolute;top:4px;right:4px;width:12px;height:12px;background:#fd0;border-radius:50%;border:1px solid #000;"></div>` +
                    `</div>`;
                  }).join('')}` +
                `</div>` +
                (companionHtml ? `<div id="companionOverlay" style="position:absolute; top:8px; right:8px; display:flex; flex-direction:column; gap:4px; z-index:10; opacity:0.85;">${companionHtml}</div>` : '') +
              `</div>` +
              `<div id="frameScrubber" style="width:100%; padding:6px 12px; background:#1a1a1a; border-top:1px solid #333; flex-shrink:0;">` +
                `<div style="display:flex; align-items:center; justify-content:center; gap:6px; margin-bottom:4px; flex-wrap:wrap;">` +
                  `<button class="btn-viewer" onclick="scrubPlayToggle(-1)" id="scrubPlayRevBtn" title="Play reverse (click again to stop)">◀◀</button>` +
                  `<span id="frameCounter" style="color:#0ff; font-family:monospace; font-size:0.85em; min-width:120px; text-align:center;">Frame 0 / 0</span>` +
                  `<button class="btn-viewer" onclick="scrubPlayToggle(1)"  id="scrubPlayFwdBtn" title="Play forward (click again to stop)">▶▶</button>` +
                  `<button id="markFrameBtn" class="btn-viewer" onclick="toggleMarkFrame()" ` +
                    `title="Mark In/Out points for trimming (M key)" style="font-size:0.85em; padding:2px 8px;">📌 Mark</button>` +
                `</div>` +
                `<input type="range" id="frameScrubSlider" min="0" max="100" value="0" step="1" ` +
                  `style="width:100%; height:20px; accent-color:#0ff; cursor:pointer;" title="Drag to scrub frames">` +
                `<div style="display:flex; justify-content:space-between; align-items:center; margin-top:4px;">` +
                  `<div style="color:#666; font-size:0.6em;">` +
                    `←/→ step · Shift ±10 · Space play/pause · M mark` +
                  `</div>` +
                `</div>` +
                `<div id="markedFrameBar" style="position:relative; width:100%; height:8px; background:#222; margin-top:4px; border-radius:4px; overflow:hidden;" title="Cyan = trim In/Out · Red = transit spans · Green = peak frame"></div>` +
              `</div>` +
              `<div id="buildCompositeRow" style="display:none; padding:4px 8px; background:#1a1a1a; border-top:1px solid #222; text-align:center; flex-shrink:0;">` +
                `<button class="btn-viewer" id="buildCompositeBtn" onclick="buildCompositeFromMarked()">🖼 Build Composite (<span id="compositeCountBtn">0</span>)</button>` +
              `</div>` +
            `</div>`;
        const vid = document.getElementById('hiddenVid');
        vid.pause();
        _currentFrame = 0;
        _frameTotalCount = 0;
        _videoFps = 30;

        _fetchServerVideoInfo(path).then((info) => {
            if (!_viewerFile || _viewerFile.path !== path || !info) return;
            if (Number.isFinite(info.fps) && info.fps > 0) _videoFps = Number(info.fps);
            if (Number.isFinite(info.frame_count) && info.frame_count > 0) {
                _frameTotalCount = Math.max(1, Math.round(info.frame_count));
            } else if (Number.isFinite(info.duration) && info.duration > 0) {
                _frameTotalCount = Math.max(_frameTotalCount || 0, Math.round(info.duration * (_videoFps || 30)));
            }
            const sliderEl = document.getElementById('frameScrubSlider');
            if (sliderEl && _frameTotalCount > 0) sliderEl.max = _frameTotalCount - 1;
            _updateScrubPosition(vid);
            _updateFivePanel();
        });

        const slider = document.getElementById('frameScrubSlider');
        slider.addEventListener('input', () => {
            const f = parseInt(slider.value, 10);
            _seekToFrame(vid, f);
        });

        const _scheduleViewerRender = () => {
            if (_renderPending) return;
            _renderPending = true;
            requestAnimationFrame(() => {
                _renderPending = false;
                const v = document.getElementById('hiddenVid');
                if (!v) return;
                _updateScrubPosition(v);
                _updateFivePanel();
            });
        };
        const updateAfterSeek = () => {
            _currentFrame = Math.round(vid.currentTime * _videoFps);
            _scheduleViewerRender();
        };
        // timeupdate only handles loop wrap-around; render is driven by seeked.
        vid.addEventListener('timeupdate', () => {
            if (_loopSegment && _loopSegment.start != null && vid.currentTime >= _loopSegment.end) {
                vid.currentTime = _loopSegment.start;
            }
        });
        vid.addEventListener('seeked', updateAfterSeek);
        vid.addEventListener('durationchange', () => _initFrameScrubber(vid));
        const _isDetClip = /\/det_[^/]+\.mp4$/i.test(path);
        // For det_*.mp4 clips: load the sidecar JSON which contains the exact
        // live-detector signal data (no replay needed).  Fall back to lightweight
        // isolation for older clips that predate the enriched sidecar format.
        const _maybeAutoIsolate = () => {
            if (!_isDetClip) return;
            const apiPath = path.replace(/^\/static\//, '');
            const sidecarPath = path.replace(/\.mp4$/i, '.json');
            fetch(sidecarPath).then(r => r.ok ? r.json() : null).then(sidecar => {
                const peak = sidecar && sidecar.peak_time_s != null ? sidecar.peak_time_s : null;
                if (sidecar && sidecar.signal) {
                    // New format: use live-detector signal directly
                    _loadSidecarSignal(sidecar, path);
                } else {
                    // Legacy: fall back to lightweight frame-diff isolation
                    _runIsolateTransit(apiPath, peak);
                }
            }).catch(() => {
                _runIsolateTransit(apiPath, null);
            });
        };
        const _initAfterMeta = async () => {
            _videoFps = await _probeVideoFps(vid, path);
            _initFrameScrubber(vid);
            // Ensure the playhead is settled at frame 0 and a real frame is
            // decoded before we size the thumb canvas / draw anything.
            await _seekToFrame(vid, 0);
            _extractFrameThumbs(vid);
            // Prime the pump: guarantee the centre panel has content.
            _captureLandedThumb(vid, 0);
            _updateFivePanel();
            updateAfterSeek();
            _maybeAutoIsolate();
        };
        vid.addEventListener('loadedmetadata', _initAfterMeta, { once: true });
        vid.addEventListener('loadeddata', () => { updateAfterSeek(); });
        if (vid.readyState >= 1) { _initAfterMeta(); }
    } else {
        const isDiff = name.includes('_diff');
        const isFrame = name.includes('_frame');
        const imgTooltip = isDiff
            ? 'Diff heatmap — shows pixel-level changes between consecutive frames. Bright/warm colours indicate motion (potential transit). Blue/cool areas had no change. This is NOT a camera image — it visualises what triggered the detection.'
            : isFrame
            ? 'Trigger frame — the low-resolution (160×90, upscaled 4×) detection frame captured at the exact moment motion was detected. This is from the detection pipeline, not the full-resolution telescope feed.'
            : '';
        body.innerHTML = `<img src="${path}" alt="${name}" title="${imgTooltip}" style="max-width:100%; max-height:100%; height:auto; display:block; margin:auto;">`;
    }

    // Build action buttons (download, delete, prev/next, find transit)
    if (actionsEl) {
        const hasPrev = _viewerIndex > 0;
        const hasNext = _viewerIndex >= 0 && _viewerIndex < files.length - 1;
        // det_*.mp4 clips are confirmed transits — analysis buttons are redundant.
        // Only show Solar/Lunar Transit analyze buttons for vid_* and other recordings.
        const _isDetFile = /\/det_[^/]+\.mp4$/i.test(path);
        const scanBtn = isVideo
            ? `<button class="btn-viewer" onmousedown="frameStepStart(-1)" onmouseup="frameStepStop()" onmouseleave="frameStepStop()" title="Back 1 frame (hold to repeat)">◁</button>` +
              (!_isDetFile ? `<button class="btn-viewer btn-viewer-sun" id="scanTransitBtn" onclick="scanTransit('sun')" title="Analyze for solar transit">☀️ Solar Transit</button>` +
              `<button class="btn-viewer btn-viewer-moon" onclick="scanTransit('moon')" title="Analyze for lunar transit">🌙 Lunar Transit</button>` : '') +
              `<button class="btn-viewer" onmousedown="frameStepStart(1)" onmouseup="frameStepStop()" onmouseleave="frameStepStop()" title="Forward 1 frame (hold to repeat)">▷</button>`
            : '';
        // Show composite image button if an analyzed_xxx.jpg exists for this file
        const stem = path.replace(/^.*\//, '').replace('.mp4', '');
        const folder = path.substring(0, path.lastIndexOf('/'));
        const analyzedJpg = folder + '/analyzed_' + stem + '.jpg';
        const hasAnalyzed = isVideo && !name.startsWith('analyzed_') && window.currentFiles &&
            window.currentFiles.some(f => f.path === analyzedJpg);
        const viewerUrl = '/telescope/composite?path=' + encodeURIComponent(analyzedJpg.replace(/^\/static\//, ''));
        const replayBtn = hasAnalyzed
            ? `<button class="btn-viewer" onclick="openCompositeModal('${analyzedJpg}?t=${Date.now()}', null)">🖼 Composite</button>`
            : '';
        const isFav = getFavorites().has(path);
        const favBtn = `<button class="btn-viewer" id="viewerFavBtn" data-fav-path="${path}" onclick="toggleFavorite('${path}', event)" title="Favorite">${isFav ? '❤️' : '🤍'}</button>`;
        const delDisabled = isFav ? 'disabled title="Remove favorite first"' : 'title="Delete (⌘/Ctrl+click to skip confirm)"';
        // TP/FP/FN label buttons for det_* clips
        let labelBtnsHtml = '';
        if (_isDetFile) {
            labelBtnsHtml = `<span id="viewerLabelBtns" style="display:inline-flex;gap:3px;align-items:center;"></span>`;
        }

        actionsEl.innerHTML =
            `<button class="btn-viewer" onclick="viewerNav(-1)" title="Previous" ${hasPrev ? '' : 'disabled'}>◀</button>` +
            scanBtn +
            replayBtn +
            favBtn +
            labelBtnsHtml +
            `<button class="btn-viewer" onclick="viewerDownload()" title="Download">⬇️ Download</button>` +
            (isVideo ? `<button class="btn-viewer" onclick="viewerExportMp4()" title="Re-encode as a clean, editable MP4">Export MP4</button>` : '') +
            `<button class="btn-viewer btn-viewer-danger" id="viewerDeleteBtn" onclick="viewerDelete(event)" ${delDisabled}>🗑️ Delete</button>` +
            `<button class="btn-viewer" onclick="viewerNav(1)" title="Next" ${hasNext ? '' : 'disabled'}>▶</button>`;

        if (_isDetFile) {
            _initViewerLabelBtns(name, path);
        }
    }

    // Trim row — only for video files
    const trimRow = document.getElementById('viewerTrimRow');
    if (trimRow) {
        if (isVideo) {
            trimRow.style.display = 'flex';
            _renderTrimRow();
        } else {
            trimRow.style.display = 'none';
            trimRow.innerHTML = '';
        }
    }

    viewer.style.display = 'flex';
}

function closeFileViewer() {
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    viewer.style.display = 'none';
    body.innerHTML = '';
    _scrubPlayStop();
    _setScanBanner(null);
    _viewerIndex = -1;
    _viewerFile = null;
    _loopSegment = null;
    _markedFrames = new Set();
    _trimIn = null;
    _trimOut = null;
    _scrubSlider = null;
    _frameThumbs = {};
    _thumbExtractionQueue = [];
    _thumbExtractionPending = new Set();
    _thumbCanvas = null;
    _thumbCtx = null;
    _thumbGeneration++;
    _thumbChain = Promise.resolve();
    _currentFrame = 0;

    if (viewer._filesModalWasOpen) {
        const filesModal = document.getElementById('filesModal');
        if (filesModal) filesModal.style.display = 'flex';
        viewer._filesModalWasOpen = false;
    }
}

/** Resolve the transit_events CSV timestamp for a det_* filename, then
 *  render TP/FP/FN buttons into #viewerLabelBtns. */
async function _initViewerLabelBtns(filename, path) {
    const wrap = document.getElementById('viewerLabelBtns');
    if (!wrap) return;

    // Parse det_YYYYMMDD_HHMMSS[_*].mp4 → Date
    const m = filename.match(/det_(\d{4})(\d{2})(\d{2})_(\d{2})(\d{2})(\d{2})/i);
    if (!m) return;
    const fileDate = new Date(
        parseInt(m[1]), parseInt(m[2])-1, parseInt(m[3]),
        parseInt(m[4]), parseInt(m[5]), parseInt(m[6])
    );

    // Find matching event timestamp from cached events (within 2 s)
    let eventTs = null;
    const cached = (_detEventsSelection && _detEventsSelection.allEvents) || [];
    for (const ev of cached) {
        if (!ev.timestamp) continue;
        const diff = Math.abs(new Date(ev.timestamp) - fileDate);
        if (diff <= 2000) { eventTs = ev.timestamp; break; }
    }

    // If not in cache, fetch fresh from API
    if (!eventTs) {
        try {
            const resp = await fetch('/api/transit-events');
            if (resp.ok) {
                const evts = await resp.json();
                for (const ev of evts) {
                    if (!ev.timestamp) continue;
                    const diff = Math.abs(new Date(ev.timestamp) - fileDate);
                    if (diff <= 2000) { eventTs = ev.timestamp; break; }
                }
            }
        } catch (_) {}
    }

    if (!eventTs) return;  // no matching event — no buttons

    const _LC = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800' };
    const _LI = { tp: '✅TP', fp: '❌FP', fn: '⚠️FN' };
    const _LT = { tp: 'True positive', fp: 'False positive', fn: 'False negative' };

    ['tp','fp','fn'].forEach(lbl => {
        const b = document.createElement('button');
        b.className = 'btn-viewer';
        b.id = `viewerLabel_${lbl}`;
        b.textContent = _LI[lbl];
        b.title = `${_LT[lbl]} — label this clip`;
        b.style.cssText = `background:#333;border:1px solid #555;color:#fff;padding:2px 7px;border-radius:3px;cursor:pointer;font-size:0.85em;`;
        b.onclick = async () => {
            try {
                const resp = await fetch('/api/transit-events/label', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({ timestamp: eventTs, label: lbl }),
                });
                if (!resp.ok) throw new Error(await resp.text());
                // Highlight active button
                wrap.querySelectorAll('button').forEach(btn => {
                    const bl = btn.id.replace('viewerLabel_','');
                    btn.style.background   = bl === lbl ? _LC[bl] : '#333';
                    btn.style.borderColor  = bl === lbl ? _LC[bl] : '#555';
                });
            } catch (e) { console.error('[ViewerLabel]', e); }
        };
        wrap.appendChild(b);
    });

    // Pre-fill current label if already set
    try {
        const resp = await fetch('/api/transit-events');
        if (resp.ok) {
            const evts = await resp.json();
            const ev = evts.find(e => e.timestamp === eventTs);
            if (ev && ev.label) {
                const activeBtn = document.getElementById(`viewerLabel_${ev.label}`);
                if (activeBtn) {
                    activeBtn.style.background  = _LC[ev.label];
                    activeBtn.style.borderColor = _LC[ev.label];
                }
            }
        }
    } catch (_) {}
}

var _frameStepTimer = null;

function frameStep(dir) {
    _stepFrame(dir);
}

function frameStepStart(dir) {
    frameStepStop();
    _stepFrame(dir);
    let delay = 250;
    const repeat = () => {
        _stepFrame(dir);
        delay = Math.max(50, delay * 0.8); // accelerate
        _frameStepTimer = setTimeout(repeat, delay);
    };
    _frameStepTimer = setTimeout(repeat, delay);
}

function frameStepStop() {
    if (_frameStepTimer) { clearTimeout(_frameStepTimer); _frameStepTimer = null; }
}

// Scrubber playback — serialized on seek completion, not a fixed interval.
var _scrubPlayDir = 0;
var _scrubPlayRunning = false;

function scrubPlayToggle(dir) {
    if (_scrubPlayDir === dir) {
        _scrubPlayStop();
        return;
    }
    _scrubPlayDir = dir;
    _scrubPlayUpdateBtns();
    if (!_scrubPlayRunning) _scrubPlayLoop();
}

function _scrubPlayStop() {
    _scrubPlayDir = 0;
    _scrubPlayUpdateBtns();
}

function _scrubPlayUpdateBtns() {
    const rev = document.getElementById('scrubPlayRevBtn');
    const fwd = document.getElementById('scrubPlayFwdBtn');
    if (rev) rev.style.background = _scrubPlayDir === -1 ? '#0a4' : '';
    if (fwd) fwd.style.background = _scrubPlayDir ===  1 ? '#0a4' : '';
}

async function _scrubPlayLoop() {
    if (_scrubPlayRunning) return;
    _scrubPlayRunning = true;
    try {
        while (_scrubPlayDir !== 0) {
            const vid = document.getElementById('hiddenVid');
            if (!vid || !vid.duration) break;
            vid.pause();
            const next = _currentFrame + _scrubPlayDir;
            if (next < 0 || next >= _frameTotalCount) {
                _scrubPlayStop();
                break;
            }
            const tStart = performance.now();
            await _seekToFrame(vid, next);
            const target = 1000 / (_videoFps || 30);
            const elapsed = performance.now() - tStart;
            if (elapsed < target) {
                await new Promise(r => setTimeout(r, target - elapsed));
            }
        }
    } finally {
        _scrubPlayRunning = false;
    }
}

// ---------------------------------------------------------------------------
// Frame scrubber with mark-for-composite
// ---------------------------------------------------------------------------

var _currentFrame = 0;
var _renderPending = false;
var _fivePanelRetryScheduled = false;

// Viewer debug logging. Leave false in production; flip to true when
// diagnosing filmstrip / playback regressions.
const _VIDDBG = false;
function _vdbg(...args) { if (_VIDDBG) try { console.log('[vid]', ...args); } catch (e) {} }

/** Capture whatever the decoder is currently showing into the shared
 *  offscreen canvas, stored against `frameIdx`. No-op if already captured
 *  or if the canvas isn't ready. Safe to call from any seek/play code path. */
function _captureLandedThumb(vid, frameIdx) {
    if (!_thumbCtx || !_thumbCanvas || !vid) return;
    if (frameIdx < 0 || frameIdx >= _frameTotalCount) return;
    if (_frameThumbs[frameIdx]) return;
    try {
        _thumbCtx.drawImage(vid, 0, 0, _thumbCanvas.width, _thumbCanvas.height);
        const url = _thumbCanvas.toDataURL('image/jpeg', 0.85);
        _frameThumbs[frameIdx] = url;
        _thumbExtractionPending.delete(frameIdx);
        _vdbg('captured', frameIdx, 'len=', url.length);
    } catch (e) {
        _vdbg('capture failed', frameIdx, e && e.message);
    }
}

/** Seek `vid` to the given frame and resolve once the post-seek frame is
 *  actually decoded (requestVideoFrameCallback) or seeked has fired +
 *  a best-effort settle in the fallback path. Opportunistically captures
 *  the landed frame into the thumb cache so playback/stepping also
 *  populates the filmstrip. */
function _seekToFrame(vid, frame) {
    if (!vid) return Promise.resolve();
    const fps = _videoFps || 30;
    const clamped = Math.max(0, Math.min((_frameTotalCount || 1) - 1, Math.round(frame)));
    _currentFrame = clamped;
    const useRVFC = typeof vid.requestVideoFrameCallback === 'function';
    _vdbg('seek start', clamped);
    return new Promise(resolve => {
        let done = false;
        const finish = () => {
            if (done) return;
            done = true;
            _captureLandedThumb(vid, clamped);
            _vdbg('seek settle', clamped);
            resolve();
        };
        const onSeeked = () => {
            vid.removeEventListener('seeked', onSeeked);
            if (useRVFC) {
                try { vid.requestVideoFrameCallback(() => finish()); return; } catch (e) {}
            }
            // Fallback: allow one browser tick for the frame to present.
            setTimeout(finish, 16);
        };
        vid.addEventListener('seeked', onSeeked, { once: true });
        try {
            vid.currentTime = clamped / fps;
        } catch (e) {
            vid.removeEventListener('seeked', onSeeked);
            finish();
        }
        // Safety net: never hang forever.
        setTimeout(finish, 1500);
    });
}

function _safeVideoDuration(vid) {
    if (!vid) return 0;
    let duration = Number.isFinite(vid.duration) && vid.duration > 0 ? vid.duration : 0;
    if (vid.seekable && vid.seekable.length > 0) {
        try {
            const end = vid.seekable.end(vid.seekable.length - 1);
            if (Number.isFinite(end) && end > duration) duration = end;
        } catch (_) {}
    }
    return duration;
}

function _stepFrame(dir) {
    const vid = document.getElementById('hiddenVid');
    const duration = _safeVideoDuration(vid);
    if (!vid || duration <= 0) return;
    vid.pause();
    _seekToFrame(vid, _currentFrame + dir);
}

/** Probe the video's real fps. Prefer requestVideoFrameCallback sampling
 *  (fast, client-only); fall back to the ffprobe-backed server route; fall
 *  back to 30. Resolves to a positive number. */
async function _probeVideoFps(vid, path) {
    try {
        if (typeof vid.requestVideoFrameCallback === 'function') {
            const deltas = [];
            const wasMuted = vid.muted;
            vid.muted = true;
            await new Promise(res => {
                let last = null;
                let count = 0;
                const cb = (_now, meta) => {
                    if (last !== null) {
                        const d = meta.mediaTime - last;
                        if (d > 0 && d < 1) deltas.push(d);
                    }
                    last = meta.mediaTime;
                    count++;
                    if (count < 12 && deltas.length < 10) {
                        try { vid.requestVideoFrameCallback(cb); } catch (e) { res(); }
                    } else {
                        res();
                    }
                };
                try { vid.requestVideoFrameCallback(cb); } catch (e) { res(); }
                vid.play().catch(() => {});
                setTimeout(res, 1200);
            });
            try { vid.pause(); } catch (e) {}
            vid.muted = wasMuted;
            try { vid.currentTime = 0; } catch (e) {}
            if (deltas.length >= 3) {
                deltas.sort((a, b) => a - b);
                const median = deltas[Math.floor(deltas.length / 2)];
                if (median > 0) {
                    const fps = 1 / median;
                    if (fps > 5 && fps < 480) {
                        const rounded = Math.round(fps * 1000) / 1000;
                        _vdbg('probe rVFC fps=', rounded);
                        return rounded;
                    }
                }
            }
        }
    } catch (e) { /* fall through */ }
    // Server fallback
    try {
        const apiPath = (path || '').replace(/^\/static\//, '');
        if (apiPath) {
            const r = await fetch('/api/video/fps?path=' + encodeURIComponent(apiPath));
            if (r.ok) {
                const j = await r.json();
                if (j && typeof j.fps === 'number' && j.fps > 0) {
                    _vdbg('probe server fps=', j.fps);
                    return j.fps;
                }
            }
        }
    } catch (e) { /* fall through */ }
    _vdbg('probe fallback fps=30');
    return 30;
}

function _initFrameScrubber(vid) {
    if (!vid || !vid.duration) return;
    _frameTotalCount = Math.round(vid.duration * _videoFps);
    const slider = document.getElementById('frameScrubSlider');
    if (slider) {
        slider.max = _frameTotalCount - 1;
        _currentFrame = Math.max(0, Math.min(_currentFrame, _frameTotalCount - 1));
        slider.value = _currentFrame;
    }
    _updateScrubPosition(vid);
}

function _updateScrubPosition(vid) {
    const counter = document.getElementById('frameCounter');
    const slider = document.getElementById('frameScrubSlider');
    if (!vid) return;
    const frame = _currentFrame;
    if (counter) counter.textContent = `Frame ${frame} / ${_frameTotalCount || '?'}`;
    if (slider && !slider.matches(':active')) {
        slider.value = frame;
    }
    const btn = document.getElementById('markFrameBtn');
    if (btn) {
        btn.style.background = _markedFrames.has(frame) ? '#fd0' : '';
        btn.style.color = _markedFrames.has(frame) ? '#000' : '';
    }
}

// ---------------------------------------------------------------------------
// Five-panel viewer: 5 equal big images showing ±2 around current frame
// ---------------------------------------------------------------------------

var _frameThumbs = {};      // frame index -> data URL
var _frameTotalCount = 0;
var _thumbCanvas = null;
var _thumbCtx = null;
var _thumbExtractionQueue = [];
var _thumbExtractionPending = new Set();
var _thumbChain = Promise.resolve();
var _thumbGeneration = 0;
var _lastThumbQueueMs = 0;
const THUMB_QUEUE_THROTTLE_MS = 120;

/** Install a shared offscreen canvas sized from mainVid. Thumb extraction
 *  reuses mainVid's own decoder — no second <video>, no decoder race. */
async function _extractFrameThumbs(mainVid) {
    _frameThumbs = {};
    _thumbExtractionQueue = [];
    _thumbExtractionPending = new Set();
    _thumbGeneration++;
    _thumbChain = Promise.resolve();

    // On large (4K) sources videoWidth can still be 0 right after the probe's
    // play/pause dance. Poll briefly before sizing the canvas.
    let tries = 0;
    while (!(mainVid.videoWidth > 0) && tries < 20) {
        await new Promise(r => setTimeout(r, 50));
        tries++;
    }
    if (!(mainVid.videoWidth > 0)) {
        _vdbg('extract bail: videoWidth still 0');
        return;
    }

    const vw = mainVid.videoWidth;
    const vh = mainVid.videoHeight || Math.round(vw * 9 / 16);
    const thumbW = Math.min(vw, 400);
    const thumbH = Math.max(1, Math.round(thumbW * (vh / vw)));
    _thumbCanvas = document.createElement('canvas');
    _thumbCanvas.width = thumbW;
    _thumbCanvas.height = thumbH;
    _thumbCtx = _thumbCanvas.getContext('2d');
    _vdbg('canvas', thumbW, 'x', thumbH);

    _queueFivePanelThumbs(_currentFrame);
}

function _queueFrameThumb(frameIdx) {
    if (!_thumbCtx || !_thumbCanvas) return;
    if (frameIdx < 0 || frameIdx >= _frameTotalCount) return;
    if (_frameThumbs[frameIdx] || _thumbExtractionPending.has(frameIdx)) return;
    _thumbExtractionPending.add(frameIdx);
    if (prioritize) _thumbExtractionQueue.unshift(frameIdx);
    else _thumbExtractionQueue.push(frameIdx);
}

function _blobToDataUrl(blob) {
    return new Promise((resolve) => {
        if (!blob || blob.size === 0) return resolve(null);
        const fr = new FileReader();
        fr.onload = () => resolve(typeof fr.result === 'string' ? fr.result : null);
        fr.onerror = () => resolve(null);
        fr.readAsDataURL(blob);
    });
}

async function _fetchServerFrameThumb(frameIdx) {
    if (!_viewerFile || !_viewerFile.path) return null;
    const apiPath = _viewerFile.path.replace(/^\/static\//, '');
    const fps = _videoFps || 30;
    // Request display-resolution frames: each five-panel slot is ~1/5 viewport width
    const panelW = document.getElementById('fivePanel');
    const slotWidth = panelW ? Math.ceil(panelW.offsetWidth / 5) : 400;
    const maxWidth = Math.min(1080, Math.max(320, slotWidth * 2)); // 2x for retina
    const qs = new URLSearchParams({
        path: apiPath,
        frame: String(frameIdx),
        fps: String(fps),
        max_width: String(maxWidth),
        t: String(Date.now()),
    });
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 10000);
    try {
        const resp = await fetch(`/telescope/files/frame?${qs.toString()}`, {
            method: 'GET',
            cache: 'no-store',
            signal: ctrl.signal,
        });
        if (!resp.ok) return null;
        const blob = await resp.blob();
        return await _blobToDataUrl(blob);
    } catch (_) {
        return null;
    } finally {
        clearTimeout(timeout);
    }
}

async function _fetchServerVideoInfo(path) {
    const apiPath = path.replace(/^\/static\//, '');
    const qs = new URLSearchParams({ path: apiPath, t: String(Date.now()) });
    const ctrl = new AbortController();
    const timeout = setTimeout(() => ctrl.abort(), 8000);
    try {
        const resp = await fetch(`/telescope/files/video-info?${qs.toString()}`, {
            method: 'GET',
            cache: 'no-store',
            signal: ctrl.signal,
        });
        if (!resp.ok) return null;
        const data = await resp.json();
        if (!data || !data.success) return null;
        return data;
    } catch (_) {
        return null;
    } finally {
        clearTimeout(timeout);
    }
}

function _captureCurrentAsThumb(vid, frameIdx) {
    try {
        _thumbCtx.drawImage(vid, 0, 0, _thumbCanvas.width, _thumbCanvas.height);
        _frameThumbs[frameIdx] = _thumbCanvas.toDataURL('image/jpeg', 0.85);
    } catch (e) { /* ignore per-frame failures */ }
    _thumbExtractionPending.delete(frameIdx);
}

function _drainThumbQueue() {
    if (!_thumbCtx || !_thumbCanvas) return;
    if (_thumbExtractionQueue.length === 0) return;
    const gen = _thumbGeneration;
    _thumbChain = _thumbChain.then(async () => {
        if (gen !== _thumbGeneration) return;
        const vid = document.getElementById('hiddenVid');
        if (!vid || !vid.duration) { _thumbExtractionQueue = []; return; }
        const homeFrame = _currentFrame;
        let movedPlayhead = false;
        while (_thumbExtractionQueue.length > 0 && gen === _thumbGeneration) {
            const frameIdx = _thumbExtractionQueue.shift();
            if (frameIdx === undefined) break;
            if (_frameThumbs[frameIdx]) { _thumbExtractionPending.delete(frameIdx); continue; }
            const curFrame = Math.round(vid.currentTime * (_videoFps || 30));
            if (_scrubPlayDir !== 0) {
                // Playing: only capture frames we're already sitting on.
                // Anything else goes back on the queue for the play loop /
                // _seekToFrame's opportunistic capture to pick up later.
                if (curFrame === frameIdx) {
                    _captureCurrentAsThumb(vid, frameIdx);
                    _updateFivePanel();
                } else {
                    _thumbExtractionQueue.push(frameIdx);
                    break;
                }
                continue;
            }
            // Paused: may seek to reach the frame.
            if (curFrame === frameIdx) {
                _captureCurrentAsThumb(vid, frameIdx);
            } else {
                vid.pause();
                await _seekToFrame(vid, frameIdx);
                if (gen !== _thumbGeneration) return;
                movedPlayhead = true;
                _captureCurrentAsThumb(vid, frameIdx);
            }
            _updateFivePanel();
        }
        // Restore user's playhead if we moved it for thumb capture.
        if (movedPlayhead && gen === _thumbGeneration && _scrubPlayDir === 0) {
            await _seekToFrame(vid, homeFrame);
        }
    }).catch(() => {});
}

function _queueFivePanelThumbs(centerFrame) {
    const offsets = [0, -1, 1, -2, 2, -3, 3, -4, 4];
    const targets = offsets
        .map(off => centerFrame + off)
        .filter(f => f >= 0 && f < _frameTotalCount);
    const targetSet = new Set(targets);

    // Drop stale queued work so current scrub position is always prioritized.
    _thumbExtractionQueue = _thumbExtractionQueue.filter(f => targetSet.has(f) && !_frameThumbs[f]);

    // Add newest targets to the front in reverse so final order is [0,-1,1,-2,2...].
    for (let i = targets.length - 1; i >= 0; i--) {
        _queueFrameThumb(targets[i], true);
    }
    _drainThumbQueue();
}

function _patchFivePanelThumb(frameIdx) {
    const panel = document.getElementById('fivePanel');
    if (!panel) return;
    const slot = panel.querySelector(`[data-frame-slot="${frameIdx}"]`);
    if (!slot) return;
    const src = _frameThumbs[frameIdx];
    if (!src) return;
    const existing = slot.querySelector('img');
    if (existing) {
        if (existing.src !== src) existing.src = src;
        return;
    }
    // Replace canvas preview or placeholder with server-quality image
    const target = slot.querySelector('canvas') || slot.querySelector('.thumb-placeholder');
    const img = document.createElement('img');
    img.src = src;
    img.draggable = false;
    img.style.display = 'block';
    img.style.width = '100%';
    img.style.height = 'auto';
    if (target && target.parentNode === slot) slot.replaceChild(img, target);
    else slot.prepend(img);
}

/** Render 5 equal big panels: frames [cur-2, cur-1, cur, cur+1, cur+2].
 *  Uses persistent DOM slots created in viewFile() — only updates src/styles. */
function _updateFivePanel() {
    const panel = document.getElementById('fivePanel');
    if (!panel) return;
    const total = _frameTotalCount || 1;
    const cur = _currentFrame;
    // If the thumb canvas isn't ready yet, draw placeholders and schedule
    // a single retry once the canvas finishes setup.
    if (!_thumbCanvas || !_thumbCtx) {
        if (!_fivePanelRetryScheduled) {
            _fivePanelRetryScheduled = true;
            requestAnimationFrame(() => {
                _fivePanelRetryScheduled = false;
                _updateFivePanel();
            });
        }
    } else {
        _queueFivePanelThumbs(cur);
    }

    const slots = panel.querySelectorAll('.fp-slot');
    if (slots.length !== 5) return;  // slots not yet created

    const offsets = [-2, -1, 0, 1, 2];
    offsets.forEach((off, i) => {
        const slot = slots[i];
        const f = cur + off;
        const isCurrent = off === 0;
        const inRange = f >= 0 && f < total;

        // Update slot attributes
        slot.dataset.frameSlot = String(f);
        slot.onclick = inRange ? () => _jumpToFrame(f) : null;
        slot.title = inRange ? `Frame ${f}` : '';
        slot.style.border = isCurrent ? '3px solid #0ff' : '3px solid #333';
        slot.style.opacity = inRange ? '1' : '0.12';

        // Update image: prefer server thumb > canvas preview > placeholder
        const src = (inRange && _frameThumbs[f]) ? _frameThumbs[f] : '';
        let img = slot.querySelector('img');
        let cvs = slot.querySelector('canvas');
        let placeholder = slot.querySelector('.thumb-placeholder');
        const vid = document.getElementById('hiddenVid');

        if (src) {
            // Server-quality thumb available — use <img>
            if (cvs && cvs.parentNode === slot) slot.removeChild(cvs);
            if (!img) {
                img = document.createElement('img');
                img.style.cssText = 'display:block; width:100%; height:auto;';
                img.draggable = false;
                if (placeholder) slot.replaceChild(img, placeholder);
                else slot.insertBefore(img, slot.firstChild);
            }
            if (img.src !== src) img.src = src;
        } else if (isCurrent && inRange && vid && vid.videoWidth > 0 && vid.readyState >= 2) {
            // No server thumb yet for center frame — draw instant canvas preview
            if (img && img.parentNode === slot) slot.removeChild(img);
            if (!cvs) {
                cvs = document.createElement('canvas');
                cvs.style.cssText = 'display:block; width:100%; height:auto;';
                if (placeholder) slot.replaceChild(cvs, placeholder);
                else slot.insertBefore(cvs, slot.firstChild);
            }
            cvs.width = vid.videoWidth;
            cvs.height = vid.videoHeight;
            try { cvs.getContext('2d').drawImage(vid, 0, 0); } catch (_) {}
        } else {
            // No thumb, no canvas source — show placeholder
            if (img && img.parentNode === slot) slot.removeChild(img);
            if (cvs && cvs.parentNode === slot) slot.removeChild(cvs);
            if (!slot.querySelector('.thumb-placeholder')) {
                placeholder = document.createElement('div');
                placeholder.className = 'thumb-placeholder';
                placeholder.style.cssText = 'width:100%; padding-top:75%; background:#111;';
                slot.insertBefore(placeholder, slot.firstChild);
            }
        }

        // Update label
        const label = slot.querySelector('.fp-label');
        if (label) {
            label.textContent = inRange ? String(f) : '';
            label.style.color = isCurrent ? '#0ff' : '#888';
            label.style.fontWeight = isCurrent ? 'bold' : 'normal';
        }

        // Update mark dot
        const dot = slot.querySelector('.fp-mark-dot');
        if (dot) dot.style.display = _markedFrames.has(f) ? '' : 'none';
    });
}

function _jumpToFrame(f) {
    const vid = document.getElementById('hiddenVid');
    const frame = Math.round(f);
    if (!vid || frame < 0 || frame >= _frameTotalCount) return;
    vid.pause();
    _scrubPlayStop();
    _seekToFrame(vid, frame);
}

function toggleMarkFrame() {
    const vid = document.getElementById('hiddenVid');
    const t = _currentFrame / _videoFps;

    if (_trimIn === null) {
        // No marks yet — set In
        _trimIn = t;
    } else if (_trimOut === null) {
        // In set, Out not yet — set Out (ensure In < Out)
        if (t <= _trimIn) {
            _trimIn = t; // replace In if user went backwards
        } else {
            _trimOut = t;
        }
    } else {
        // Both set — replace whichever is closer
        if (Math.abs(t - _trimIn) <= Math.abs(t - _trimOut)) {
            _trimIn = t;
        } else {
            _trimOut = t;
        }
        // Keep In < Out
        if (_trimIn > _trimOut) { const tmp = _trimIn; _trimIn = _trimOut; _trimOut = tmp; }
    }

    _updateMarkedUI();
    if (vid) _updateScrubPosition(vid);
    _updateFivePanel();
    _renderTrimRow();
}

function _updateMarkedUI() {
    const count = _markedFrames.size;
    const countEl = document.getElementById('markedCount');
    if (countEl) countEl.textContent = `${count} marked`;

    // Update "Build Composite" row visibility
    const buildRow = document.getElementById('buildCompositeRow');
    const buildCount = document.getElementById('compositeCountBtn');
    if (buildRow) {
        buildRow.style.display = count > 0 ? '' : 'none';
        if (buildCount) buildCount.textContent = count;
    }

    // Draw tick marks + transit spans on the marked-frame bar
    _drawTransitSpans();
}

async function buildCompositeFromMarked() {
    if (_markedFrames.size === 0) return;
    const vid = document.querySelector('#fileViewerBody video');
    if (!_viewerFile) return;

    const apiPath = _viewerFile.path.replace(/^\/static\//, '');
    const frames = Array.from(_markedFrames).sort((a, b) => a - b);

    const btn = document.getElementById('buildCompositeBtn');
    if (btn) { btn.disabled = true; btn.textContent = 'Building…'; }
    _setScanBanner('Building composite from ' + frames.length + ' frames…', 'info');

    try {
        const resp = await fetch('/telescope/files/composite-from-frames', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: apiPath, frame_indices: frames, fps: _videoFps }),
            signal: AbortSignal.timeout(120000),
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            _setScanBanner('Composite failed: ' + (data.error || resp.statusText), 'error');
            return;
        }
        _setScanBanner(`Composite built from ${frames.length} frames`, 'success');
        if (data.composite_image) {
            refreshFiles();
            setTimeout(() => {
                openCompositeModal('/static/' + data.composite_image + '?t=' + Date.now(), null);
            }, 500);
        }
    } catch (e) {
        _setScanBanner('Composite failed: ' + e.message, 'error');
    } finally {
        if (btn) { btn.disabled = false; btn.textContent = '🖼 Build Composite (' + _markedFrames.size + ')'; }
    }
}

// Keyboard shortcuts for frame viewer (when viewer is open)
document.addEventListener('keydown', function(e) {
    const viewer = document.getElementById('fileViewer');
    if (!viewer || viewer.style.display === 'none') return;
    const vid = document.getElementById('hiddenVid');
    if (!vid) return;
    // Note: do NOT bail on INPUT here — the slider has focus after dragging and
    // would swallow all arrow/space keys. Only skip character shortcuts (m/M).
    const inTextInput = e.target.tagName === 'TEXTAREA' ||
        (e.target.tagName === 'INPUT' && e.target.type === 'text');

    switch (e.key) {
        case 'ArrowLeft':
            e.preventDefault();
            _stepFrame(e.shiftKey ? -10 : -1);
            break;
        case 'ArrowRight':
            e.preventDefault();
            _stepFrame(e.shiftKey ? 10 : 1);
            break;
        case 'm':
        case 'M':
            if (inTextInput) return;
            e.preventDefault();
            toggleMarkFrame();
            break;
        case ' ':
            e.preventDefault();
            if (vid.paused) {
                vid.play().catch(() => {});
            } else {
                vid.pause();
            }
            break;
    }
});

function viewerNav(delta) {
    const files = window.currentFiles || [];
    const newIdx = _viewerIndex + delta;
    if (newIdx < 0 || newIdx >= files.length) return;
    const f = files[newIdx];
    viewFile(f.path, f.name);
}

function viewerDownload() {
    const files = window.currentFiles || [];
    if (_viewerIndex < 0 || _viewerIndex >= files.length) return;
    const f = files[_viewerIndex];

    // If a composite JPG was produced, offer that for download instead
    const panel = document.getElementById('analysisLegendPanel');
    const preview = panel && document.getElementById('compositePreview');
    if (preview && preview.src) {
        // Strip query-string cache-buster and extract just the path
        const srcUrl = new URL(preview.src);
        const jpgPath = srcUrl.pathname; // e.g. /static/captures/.../analyzed_x.jpg
        const jpgName = jpgPath.replace(/^.*\//, '');
        downloadFile(jpgPath, jpgName);
        return;
    }

    downloadFile(f.path, f.name);
}

async function viewerExportMp4() {
    const files = window.currentFiles || [];
    let f = (_viewerIndex >= 0 && _viewerIndex < files.length) ? files[_viewerIndex] : _viewerFile;
    if (!f) return;

    const exportPath = f.path.replace('/static/', '');
    showStatus('Exporting MP4…', 'info', 0);
    try {
        const resp = await fetch('/telescope/files/export', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: exportPath }),
        });
        const data = await resp.json();
        if (!resp.ok) {
            showStatus(`Export failed: ${data.error || resp.status}`, 'error', 6000);
            return;
        }
        showStatus(`Exported → ${data.name}`, 'success', 4000);
        await refreshFiles();
    } catch (err) {
        showStatus(`Export error: ${err.message}`, 'error', 6000);
    }
}

async function viewerDelete(e) {
    const files = window.currentFiles || [];
    // Use _viewerIndex if valid, otherwise fall back to _viewerFile
    let f = (_viewerIndex >= 0 && _viewerIndex < files.length) ? files[_viewerIndex] : _viewerFile;
    if (!f) return;
    if (getFavorites().has(f.path)) {
        showStatus('Remove favorite ❤️ first before deleting', 'warning', 3000);
        return;
    }
    const skipConfirm = e && (e.metaKey || e.ctrlKey);
    if (!skipConfirm && !confirm(`Delete ${f.name}?`)) return;

    try {
        const delPath = f.path.replace('/static/', '');
        const response = await fetch('/telescope/files/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: delPath })
        });
        const data = await response.json();
        if (response.ok && data.success) {
            showStatus(`Deleted ${f.name}`, 'success', 3000);
        } else {
            showStatus(`Delete failed: ${data.error || response.status}`, 'error', 5000);
            return;
        }
    } catch (error) {
        showStatus(`Delete failed: ${error.message}`, 'error', 5000);
        return;
    }

    // Refresh file list, then navigate to next (or previous, or close)
    await refreshFiles();
    updateFilesGrid();
    const updatedFiles = window.currentFiles || [];
    if (updatedFiles.length === 0) {
        closeFileViewer();
    } else if (_viewerIndex < updatedFiles.length) {
        const next = updatedFiles[_viewerIndex];
        viewFile(next.path, next.name);
    } else {
        const prev = updatedFiles[updatedFiles.length - 1];
        viewFile(prev.path, prev.name);
    }
}

// ============================================================================
// VIDEO TRIM
// Trim a video to [start_s, end_s] using the server-side ffmpeg -c copy route.
// The original is backed up as _orig.mp4 for single-level undo.
// ============================================================================


var _lastTrimOrigPath = null; // path of the original file from the last trim

/** Render the trim row into #viewerTrimRow.
 *  showReplace=true after a trim to offer "Replace Original". */
function _renderTrimRow(showReplace) {
    const row = document.getElementById('viewerTrimRow');
    if (!row) return;
    const vid = document.getElementById('hiddenVid');
    const dur = (vid && vid.duration && isFinite(vid.duration)) ? vid.duration.toFixed(2) : '';
    const durHint = dur ? ` · dur: ${dur}s` : '';
    const inStr  = _trimIn  !== null ? 'f' + Math.round(_trimIn * _videoFps) : '—';
    const outStr = _trimOut !== null ? 'f' + Math.round(_trimOut * _videoFps) : '—';
    const selDur = (_trimIn !== null && _trimOut !== null) ? (_trimOut - _trimIn).toFixed(2) + 's' : '—';
    row.innerHTML =
        `<span style="color:#aaa; font-size:0.78em;">✂️ In: <b style="color:#0ff">${inStr}</b> &nbsp;Out: <b style="color:#0ff">${outStr}</b> &nbsp;Sel: <b style="color:#0ff">${selDur}</b>${durHint}</span>` +
        `<button class="btn-viewer btn-viewer-sun" onclick="viewerTrim()" title="Save trimmed region as trim_<name>.mp4 (original untouched)">Trim</button>` +
        (showReplace
            ? `<button class="btn-viewer btn-viewer-danger" onclick="viewerTrimReplaceOriginal()" title="Delete the original — keep only the trimmed version">Replace Original</button>`
            : '');
}

async function viewerTrim() {
    const files = window.currentFiles || [];
    const f = (_viewerIndex >= 0 && _viewerIndex < files.length) ? files[_viewerIndex] : _viewerFile;
    if (!f) return;

    if (_trimIn === null || _trimOut === null) {
        showStatus('Mark In and Out points first (📌 Mark button)', 'warning', 3000);
        return;
    }
    const start_s = _trimIn;
    const end_s   = _trimOut;
    const dur = (end_s - start_s).toFixed(2);

    const trimPath = f.path.replace('/static/', '');
    showStatus('Trimming…', 'info', 0);
    try {
        const resp = await fetch('/telescope/files/trim', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: trimPath, start_s, end_s }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.success) {
            showStatus(`Trim failed: ${data.error || resp.status}`, 'error', 6000);
            return;
        }
        _lastTrimOrigPath = f.path; // remember original for Replace Original
        showStatus(`Saved ${data.name} (${dur}s)`, 'success', 4000);
        await refreshFiles();
        updateFilesGrid();
        // Open the trimmed file; show Replace Original button
        const trimmed = (window.currentFiles || []).find(f2 => f2.name === data.name);
        if (trimmed) viewFile(trimmed.path, trimmed.name);
        _renderTrimRow(true);
    } catch (err) {
        showStatus(`Trim error: ${err.message}`, 'error', 6000);
    }
}

async function viewerTrimReplaceOriginal() {
    if (!_lastTrimOrigPath) { showStatus('Original path unknown', 'error', 3000); return; }
    if (!confirm('Delete the original file? This cannot be undone.')) return;
    const delPath = _lastTrimOrigPath.replace('/static/', '');
    try {
        const resp = await fetch('/telescope/files/delete', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: delPath }),
        });
        const data = await resp.json();
        if (!resp.ok || !data.success) { showStatus(`Delete failed: ${data.error}`, 'error', 5000); return; }
        showStatus('Original deleted', 'success', 3000);
        _lastTrimOrigPath = null;
        await refreshFiles();
        updateFilesGrid();
        _renderTrimRow(false);
    } catch (err) {
        showStatus(`Error: ${err.message}`, 'error', 5000);
    }
}

// ============================================================================
// TRANSIT FRAME SCANNER
// Scans a video to find the transit (aircraft crossing sun/moon disk).
// Compares sampled frames against a reference to detect the brief anomaly,
// then seeks the player to the centre of the transit.
// ============================================================================

var _analyzeController = null;  // AbortController for in-flight analysis

async function scanTransit(target) {
    const files = window.currentFiles || [];
    if (_viewerIndex < 0 || _viewerIndex >= files.length) return;
    const f = files[_viewerIndex];
    // If viewing an analyzed file, re-analyze the original
    let videoPath = f.path;
    videoPath = videoPath.replace(/_analyzed/g, '');
    if (!/\.(mp4|avi|mov|mkv|webm)$/i.test(f.name)) return;

    const btn = document.getElementById('scanTransitBtn');
    const playerVideo = document.querySelector('#fileViewerBody video');
    if (!playerVideo) return;

    // Read tuning slider values BEFORE removing the old panel
    const sliderBody = {};
    // Persist target so re-analyze keeps the same mode
    if (target === 'moon' || target === 'sun') {
        localStorage.setItem('transit_last_target', target);
    } else {
        target = localStorage.getItem('transit_last_target') || 'sun';
    }
    sliderBody.target = target;
    const dtEl = document.getElementById('sliderDiffThreshold');
    const mbEl = document.getElementById('sliderMinBlob');
    const dmEl = document.getElementById('sliderDiskMargin');
    if (dtEl) { sliderBody.diff_threshold = parseInt(dtEl.value); localStorage.setItem('transit_slider_sliderDiffThreshold', dtEl.value); }
    if (mbEl) { sliderBody.min_blob_pixels = parseInt(mbEl.value); localStorage.setItem('transit_slider_sliderMinBlob', mbEl.value); }
    if (dmEl) { sliderBody.disk_margin_pct = parseFloat(dmEl.value) / 100; localStorage.setItem('transit_slider_sliderDiskMargin', dmEl.value); }
    const mpEl = document.getElementById('sliderMaxPositions');
    if (mpEl) { sliderBody.max_positions = parseInt(mpEl.value); localStorage.setItem('transit_slider_sliderMaxPositions', mpEl.value); }

    // Remove previous legend panel so user sees the UI change
    const oldPanel = document.getElementById('analysisLegendPanel');
    if (oldPanel) oldPanel.remove();

    const moonScanBtn = document.querySelector('.btn-viewer-moon');
    if (btn) { btn.disabled = true; btn.textContent = (sliderBody.target === 'moon') ? '🌙 Analyzing…' : '☀️ Analyzing…'; }
    if (moonScanBtn) moonScanBtn.disabled = true;
    // Show stop button
    _showStopAnalysisBtn(true);
    // Pulse the button text with frame count estimate
    const _analyzeStart = Date.now();
    _setScanBanner('info', 'Analyzing… 0s');
    let _analyzeTimer = setInterval(() => {
        if (btn && btn.disabled) {
            const dots = '.'.repeat(Math.floor(Date.now() / 500) % 4);
            btn.textContent = `Analyzing${dots}`;
        }
        const elapsed = Math.floor((Date.now() - _analyzeStart) / 1000);
        _setScanBanner('info', `Analyzing… ${elapsed}s`);
    }, 500);
    _setScanBanner(null);

    const apiPath = videoPath.replace(/^\/static\//, '');
    const controller = new AbortController();
    _analyzeController = controller;
    const timeoutId = setTimeout(() => controller.abort(), 5 * 60 * 1000);

    try {
        const resp = await fetch('/telescope/files/analyze', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ path: apiPath, ...sliderBody }),
            signal: controller.signal,
        });
        const data = await resp.json();
        if (!resp.ok || data.error) {
            _setScanBanner('error', `Analysis failed: ${data.error || resp.statusText}`);
            return;
        }

        const events = data.transit_events || [];
        const staticCount = data.static_detections || 0;

        if (events.length > 0) {
            // Seek to first transit event and set up loop
            const evt = events[0];
            const loopStart = Math.max(0, evt.start_seconds - 0.5);
            const loopEnd = evt.end_seconds + 0.5;
            _loopSegment = { start: loopStart, end: loopEnd };
            playerVideo.currentTime = loopStart;
            playerVideo.play();
        }

        // Refresh files and show legend (handles both found/not-found)
        await refreshFiles();
        // Re-sync _viewerIndex to the file we analyzed — refreshFiles may
        // have changed the list (new composite JPG added), shifting indices.
        const syncedIdx = (window.currentFiles || []).findIndex(f => f.path === videoPath);
        if (syncedIdx >= 0) _viewerIndex = syncedIdx;
        updateFilesGrid();
        _showAnalysisLegend(data, videoPath);
    } catch (err) {
        if (controller.signal.aborted) {
            _setScanBanner('error', 'Analysis stopped');
        } else {
            _setScanBanner('error', `Analysis error: ${err.message}`);
        }
    } finally {
        clearTimeout(timeoutId);
        clearInterval(_analyzeTimer);
        _analyzeController = null;
        _showStopAnalysisBtn(false);
        if (btn) { btn.disabled = false; btn.textContent = '☀️ Solar Transit'; }
        const _moonScanBtn = document.querySelector('.btn-viewer-moon');
        if (_moonScanBtn) _moonScanBtn.disabled = false;
    }
}

function stopAnalysis() {
    if (_analyzeController) { _analyzeController.abort(); _analyzeController = null; }
}

function _showStopAnalysisBtn(show) {
    let stopBtn = document.getElementById('stopAnalysisBtn');
    if (show) {
        if (!stopBtn) {
            const scanBtn = document.getElementById('scanTransitBtn');
            if (!scanBtn) return;
            stopBtn = document.createElement('button');
            stopBtn.id = 'stopAnalysisBtn';
            stopBtn.className = 'btn-viewer btn-viewer-danger';
            stopBtn.style.cssText = 'font-size:0.8em; padding:2px 8px;';
            stopBtn.textContent = '⏹ Stop';
            stopBtn.onclick = (e) => { e.stopPropagation(); stopAnalysis(); };
            scanBtn.parentNode.insertBefore(stopBtn, scanBtn.nextSibling);
        }
        stopBtn.style.display = '';
    } else if (stopBtn) {
        stopBtn.remove();
    }
}

function _showAnalysisLegend(data, originalPath) {
    // Remove any previous legend panel
    const old = document.getElementById('analysisLegendPanel');
    if (old) old.remove();

    const body = document.getElementById('fileViewerBody');
    if (!body) return;

    const events = data.transit_events || [];
    const staticCount = data.static_detections || 0;
    const compositeFile = data.composite_image || data.annotated_file;
    const compositePath = compositeFile ? '/static/' + compositeFile + '?t=' + Date.now() : null;

    // Summary
    let summary = '';
    if (events.length > 0) {
        const evt = events[0];
        const ts = _formatTimestamp((evt.start_seconds + evt.end_seconds) / 2);
        summary = events.length > 1
            ? `${events.length} transits — first at ${ts} (~${evt.duration_ms}ms)`
            : `Transit at ${ts} (~${evt.duration_ms}ms)`;
    } else {
        summary = 'No transit detected';
    }


    const panel = document.createElement('div');
    panel.id = 'analysisLegendPanel';
    panel.onclick = e => e.stopPropagation();
    panel.style.cssText = 'background:#1a1a1a; border-left:1px solid #333; padding:14px 16px; min-width:200px; max-width:240px; display:flex; flex-direction:column; gap:8px; font-size:0.85em; color:#ccc; height:100%; overflow:hidden;';

    // Result section
    const iconColor = events.length > 0 ? '#4dff88' : '#ffcc44';
    const icon = events.length > 0 ? '🎯' : '🔍';
    let html = `<div style="font-weight:bold; color:${iconColor}; font-size:1.05em;">${icon} ${summary}</div>`;

    // Composite image preview — flex-grow fills available space
    if (compositePath) {
        const diskCx = data.disk_cx || 0;
        const diskCy = data.disk_cy || 0;
        const diskR  = data.disk_radius || 0;
        html += `<div style="border-top:1px solid #333; padding-top:6px; flex:1; min-height:60px; overflow:hidden; display:flex; flex-direction:column;">`;
        html += `<div style="font-weight:bold; color:#aaa; font-size:0.9em; margin-bottom:4px;">Transit Composite</div>`;
        html += `<div style="position:relative; flex:1; min-height:0; overflow:hidden;">`;
        html += `<img id="compositePreview" src="${compositePath}" ` +
                `data-disk-cx="${diskCx}" data-disk-cy="${diskCy}" data-disk-r="${diskR}" ` +
                `style="width:100%; height:100%; object-fit:contain; border-radius:4px; cursor:pointer; display:block;" title="Click to view full size" />`;
        html += `<canvas id="compositeOverlay" style="position:absolute; top:0; left:0; width:100%; height:100%; pointer-events:none; border-radius:4px;"></canvas>`;
        html += `</div>`;
        html += `</div>`;
    }

    // Legend items - circles and text in aligned columns
    html += `<div style="border-top:1px solid #333; padding-top:6px;">`;
    html += `<div style="font-weight:bold; color:#aaa; font-size:0.9em; margin-bottom:4px;">Legend</div>`;
    const legendRows = [
        ['#ff4444', 'Transit detection'],
        ['#888888', 'Sunspot (filtered)'],
        ['#ffff00', 'Disk boundary'],
    ];
    legendRows.forEach(([color, label]) => {
        html += `<div style="display:flex; align-items:center; gap:8px; margin-bottom:3px;">` +
            `<span style="flex-shrink:0; width:12px; height:12px; border:2px solid ${color}; border-radius:50%; display:inline-block;"></span>` +
            `<span>${label}</span></div>`;
    });
    html += `</div>`;

    // Buttons
    if (compositePath) {
        html += `<button class="btn-viewer" id="legendViewBtn" style="font-size:0.85em; padding:4px 10px; width:100%;" data-img-src="${compositePath}">🖼 View full size</button>`;
    }

    // Tuning sliders
    html += `<div style="border-top:1px solid #333; padding-top:8px;">`;
    html += `<div style="font-weight:bold; color:#aaa; font-size:0.9em; margin-bottom:6px;">Detection Tuning</div>`;

    html += _sliderRow('sliderDiffThreshold', 'Sensitivity', 1, 30, 15,
        'Lower = more sensitive (detects fainter objects, more noise)');
    html += _sliderRow('sliderMinBlob', 'Min Blob Size', 1, 50, 20,
        'Minimum pixel area to count as a detection');
    html += _sliderRow('sliderDiskMargin', 'Edge Margin %', 5, 20, 12,
        'Percentage of disk edge to ignore (trims atmospheric distortion)');

    // Overlay positions slider — max is driven by detection count from analysis
    const totalPositions = data.transit_positions || 0;
    if (totalPositions > 1) {
        html += _sliderRow('sliderMaxPositions', 'Overlay Positions', 1, totalPositions, totalPositions,
            'How many silhouette positions to show in the composite (1 = single, max = all)');
    }

    html += `<div style="display:flex; gap:6px; margin-top:6px;">`;
    html += `<button class="btn-viewer" id="legendReanalyzeBtn" style="font-size:0.85em; padding:4px 10px; flex:1;">🔄 Re-analyze</button>`;
    html += `<button class="btn-viewer" id="legendResetBtn" style="font-size:0.85em; padding:4px 10px;" title="Reset sliders to defaults">↩ Reset</button>`;
    html += `</div>`;
    html += `</div>`;

    panel.innerHTML = html;
    body.appendChild(panel);

    // Wire up button events (after innerHTML so elements exist)
    const viewBtn = document.getElementById('legendViewBtn');
    if (viewBtn) {
        const imgSrc = viewBtn.dataset.imgSrc;
        viewBtn.onclick = (e) => { e.stopPropagation(); openCompositeModal(imgSrc, data); };
    }
    const previewImg = document.getElementById('compositePreview');
    if (previewImg && compositePath) {
        previewImg.onclick = (e) => { e.stopPropagation(); openCompositeModal(compositePath, data); };
        // Draw margin overlay when image has loaded dimensions
        previewImg.addEventListener('load', _updateMarginOverlay);
        if (previewImg.complete) _updateMarginOverlay();
    }
    const reBtn = document.getElementById('legendReanalyzeBtn');
    if (reBtn) reBtn.onclick = (e) => { e.stopPropagation(); scanTransit(); };
    const resetBtn = document.getElementById('legendResetBtn');
    if (resetBtn) resetBtn.onclick = (e) => {
        e.stopPropagation();
        _resetSliders();
        _updateMarginOverlay();
    };

    // Hook disk margin slider to live-update the overlay
    const dmSlider = document.getElementById('sliderDiskMargin');
    if (dmSlider) {
        const origInput = dmSlider.oninput;
        dmSlider.addEventListener('input', _updateMarginOverlay);
    }

    // Hide the top banner (legend panel replaces it)
    _setScanBanner(null);
}

function _sliderRow(id, label, min, max, defaultVal, tooltip) {
    const saved = localStorage.getItem('transit_slider_' + id);
    // Migrate: if stored edge margin was from old default (< 10), reset it
    if (id === 'sliderDiskMargin' && saved !== null && parseFloat(saved) < 10) {
        localStorage.removeItem('transit_slider_' + id);
    }
    const current = localStorage.getItem('transit_slider_' + id);
    const val = current !== null ? current : defaultVal;
    const extraCall = id === 'sliderDiskMargin' ? ' _updateMarginOverlay();' : '';
    return `<div style="margin-bottom:6px;" title="${tooltip}">` +
        `<div style="display:flex; justify-content:space-between; font-size:0.85em;">` +
        `<span>${label}</span><span id="${id}Val">${val}</span></div>` +
        `<input type="range" id="${id}" min="${min}" max="${max}" value="${val}" ` +
        `data-default="${defaultVal}" ` +
        `style="width:100%; accent-color:#4dff88;" ` +
        `oninput="document.getElementById('${id}Val').textContent=this.value; localStorage.setItem('transit_slider_${id}', this.value);${extraCall}">` +
        `</div>`;
}

function _resetSliders() {
    ['sliderDiffThreshold', 'sliderMinBlob', 'sliderDiskMargin', 'sliderMaxPositions'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.value = el.dataset.default;
            const valEl = document.getElementById(id + 'Val');
            if (valEl) valEl.textContent = el.dataset.default;
        }
        localStorage.removeItem('transit_slider_' + id);
    });
}

function _formatTimestamp(secs) {
    const m = Math.floor(secs / 60);
    const s = (secs % 60).toFixed(2);
    return m > 0 ? `${m}:${s.padStart(5, '0')}` : `${s}s`;
}

/**
 * Draw a yellow ring on the compositeOverlay canvas to show the excluded edge margin.
 * Called on page load (after img renders) and whenever sliderDiskMargin changes.
 */
function _updateMarginOverlay() {
    const img    = document.getElementById('compositePreview');
    const canvas = document.getElementById('compositeOverlay');
    const slider = document.getElementById('sliderDiskMargin');
    if (!img || !canvas || !slider) return;

    const marginPct = parseFloat(slider.value) / 100;
    const diskCx    = parseFloat(img.dataset.diskCx || 0);
    const diskCy    = parseFloat(img.dataset.diskCy || 0);
    const diskR     = parseFloat(img.dataset.diskR  || 0);
    if (!diskR) return;

    const dw = img.naturalWidth  || img.width;
    const dh = img.naturalHeight || img.height;
    const cw = img.clientWidth;
    const ch = img.clientHeight;
    if (!cw || !ch || !dw || !dh) return;

    // object-fit:contain scale & letterbox offsets
    const scale   = Math.min(cw / dw, ch / dh);
    const offsetX = (cw - dw * scale) / 2;
    const offsetY = (ch - dh * scale) / 2;

    canvas.width  = cw;
    canvas.height = ch;
    const ctx = canvas.getContext('2d');
    ctx.clearRect(0, 0, cw, ch);

    if (marginPct <= 0) return;

    const cx     = diskCx * scale + offsetX;
    const cy     = diskCy * scale + offsetY;
    const outerR = diskR  * scale;
    const innerR = outerR * (1 - marginPct);
    const band   = outerR - innerR;

    // Draw excluded ring as semi-transparent yellow fill
    ctx.beginPath();
    ctx.arc(cx, cy, outerR, 0, Math.PI * 2, false);
    ctx.arc(cx, cy, innerR, 0, Math.PI * 2, true);
    ctx.fillStyle = 'rgba(255,255,0,0.25)';
    ctx.fill();

    // Bright yellow inner boundary line
    ctx.beginPath();
    ctx.arc(cx, cy, innerR, 0, Math.PI * 2);
    ctx.strokeStyle = '#ffff00';
    ctx.lineWidth = Math.max(1, band * 0.15);
    ctx.stroke();
}

/**
 * Open the composite image in a full-screen modal overlay (instead of a new tab).
 * @param {string} imgSrc   - Full URL path e.g. "/static/captures/.../analyzed_xxx.jpg"
 * @param {object|null} data - Analysis result JSON, or null to load from sidecar
 */
async function openCompositeModal(imgSrc, data) {
    // Remove any existing modal
    const existing = document.getElementById('compositeModal');
    if (existing) existing.remove();

    // Load sidecar if data not provided
    if (!data) {
        const sidecarUrl = imgSrc.replace(/\.jpg(\?.*)?$/, '_analysis.json');
        try {
            const ctrl = new AbortController();
            const tid = setTimeout(() => ctrl.abort(), 5000);
            const r = await fetch(sidecarUrl, { signal: ctrl.signal });
            clearTimeout(tid);
            data = r.ok ? await r.json() : {};
        } catch (e) { data = {}; }
    }

    const events = data.transit_events || [];
    const staticCount = data.static_detections || (data.detection_count || 0) - events.length;
    const source = (data.source_file || imgSrc).split('/').pop();
    const diskDetected = data.disk_detected || false;
    const duration = data.duration_seconds || 0;
    const detectionCount = data.detection_count || 0;

    // Build events HTML for right panel — keep it brief (sidebar has details)
    let eventsHtml = '';
    if (events.length > 0) {
        events.forEach((evt, i) => {
            const ms = evt.duration_ms || 0;
            const conf = evt.confidence || '';
            const confColor = conf === 'high' ? '#4dff88' : conf === 'medium' ? '#ffcc44' : '#aaa';
            eventsHtml += `<div style="margin-bottom:4px;">Transit ${i + 1}: ~${ms}ms <span style="font-size:0.8em; color:${confColor};">${conf}</span></div>`;
        });
    } else {
        eventsHtml = '<div style="color:#888;">No transits detected</div>';
    }

    const modal = document.createElement('div');
    modal.id = 'compositeModal';
    modal.style.cssText = 'position:fixed; inset:0; z-index:9999; display:flex; background:#111; overflow:hidden;';

    modal.innerHTML = `
      <div id="compositeImagePane" style="flex:1; overflow:auto; text-align:center; background:#000; padding:8px;">
        <img id="compositeFullImg" src="${imgSrc}" alt="Transit Composite" style="max-width:100%; max-height:calc(100vh - 40px); width:auto; height:auto; display:inline-block;" />
      </div>
      <div style="width:220px; min-width:220px; background:#1a1a1a; border-left:1px solid #333; padding:16px; display:flex; flex-direction:column; gap:12px; font-size:0.85em; color:#ccc;">
        <div style="display:flex; justify-content:space-between; align-items:center;">
          <strong style="color:#eee; font-size:1em;">Transit Composite</strong>
          <button onclick="document.getElementById('compositeModal').remove()"
            style="background:none; border:1px solid #555; color:#ccc; border-radius:4px; padding:2px 8px; cursor:pointer; font-size:1.1em;" title="Close (Esc)">✕</button>
        </div>
        <div style="color:#aaa; font-size:0.8em; word-break:break-all;">${source}</div>
        <div style="border-top:1px solid #333; padding-top:10px;">
          <div style="font-weight:bold; color:#aaa; margin-bottom:6px; font-size:0.9em;">Result</div>
          ${eventsHtml}
          <div style="margin-top:6px; color:#888; font-size:0.85em;">${detectionCount} detections · ${duration.toFixed ? duration.toFixed(1) : duration}s · disk ${diskDetected ? '✓' : '✗'}</div>
        </div>
        <div style="border-top:1px solid #333; padding-top:10px;">
          <div style="font-weight:bold; color:#aaa; margin-bottom:6px; font-size:0.9em;">Legend</div>
          <div style="display:flex; align-items:center; gap:8px; margin-bottom:5px;"><span style="flex-shrink:0; width:12px; height:12px; border:2px solid #ff4444; border-radius:50%; display:inline-block;"></span><span>Transit position</span></div>
          <div style="display:flex; align-items:center; gap:8px; margin-bottom:5px;"><span style="flex-shrink:0; width:12px; height:12px; border:2px solid #888888; border-radius:50%; display:inline-block;"></span><span>Sunspot (filtered)</span></div>
          <div style="display:flex; align-items:center; gap:8px; margin-bottom:5px;"><span style="flex-shrink:0; width:12px; height:12px; border:2px solid #ffff00; border-radius:50%; display:inline-block;"></span><span>Disk boundary</span></div>
        </div>
      </div>`;

    // Close on clicking the image pane background (not the image itself)
    const paneEl = modal.querySelector('#compositeImagePane');
    paneEl.addEventListener('click', (e) => { if (e.target === paneEl) modal.remove(); });
    // Close on Escape
    const escHandler = (e) => { if (e.key === 'Escape') { modal.remove(); document.removeEventListener('keydown', escHandler); } };
    document.addEventListener('keydown', escHandler);

    document.body.appendChild(modal);

    // Scroll the image pane so the vertical midpoint of the image is centered
    const fullImg = modal.querySelector('#compositeFullImg');
    const pane = modal.querySelector('#compositeImagePane');
    if (fullImg && pane) {
        const doCenter = () => {
            // Use rendered offsetHeight (respects max-height CSS), not naturalHeight
            const imgH = fullImg.offsetHeight;
            const paneH = pane.clientHeight;
            if (imgH > paneH) {
                pane.scrollTop = (imgH / 2) - (paneH / 2);
            }
        };
        if (fullImg.complete && fullImg.offsetHeight > 0) doCenter();
        else fullImg.addEventListener('load', doCenter, { once: true });
    }
}

function _setScanBanner(type, text, onclick) {
    const el = document.getElementById('scanResultBanner');
    if (!el) return;
    if (!type) { el.style.display = 'none'; el.className = ''; el.onclick = null; return; }
    el.className = type === 'found' ? 'scan-found' : type === 'none' ? 'scan-none' : type === 'info' ? 'scan-info' : 'scan-error';
    el.textContent = text;
    el.style.display = 'block';
    el.onclick = onclick || null;
    el.style.cursor = onclick ? 'pointer' : 'default';
}

/**
 * Analyse a video for a transit event.
 * Returns { center, start, end, duration } in seconds, or null if nothing found.
 *
 * Uses two complementary signals so it catches both fast aircraft and
 * slow/stationary targets (high-altitude balloons):
 *
 *  Signal A – Consecutive-frame diff:  detects rapid change.  A fast
 *    aircraft produces spikes when the silhouette enters and exits.
 *    Even a single-frame transit (~33ms) creates a change boundary.
 *
 *  Signal B – Reference-frame diff (centre-weighted):  detects any
 *    anomaly vs. clean background, however slow-moving or diffuse.
 *    Centre-weighting focuses on the sun/moon disk where the target
 *    appears, boosting signal-to-noise for small objects.
 *
 *  Both diff functions subtract the mean per-channel brightness shift
 *  before summing, making them immune to atmospheric scintillation
 *  (global brightness fluctuations).  Only localised pixel changes
 *  (actual silhouettes) contribute to the score.
 *
 *  The coarse pass records both signals per sample and flags a sample
 *  as a spike if EITHER exceeds its own adaptive threshold.
 *
 *  Fine pass uses reference-frame comparison at ~30fps to pinpoint
 *  exact transit boundaries.
 */
async function _scanVideoForTransit(src, onProgress) {
    const video = document.createElement('video');
    video.muted = true;
    video.preload = 'auto';
    video.src = src;

    await new Promise((resolve, reject) => {
        video.addEventListener('loadeddata', resolve, { once: true });
        video.addEventListener('error', () => reject(new Error('Video load failed')), { once: true });
    });

    const duration = video.duration;
    if (!duration || duration < 0.5) { video.src = ''; return null; }

    // Small canvas for fast pixel comparison
    const W = 160, H = 90;
    const canvas = document.createElement('canvas');
    canvas.width = W; canvas.height = H;
    const ctx = canvas.getContext('2d', { willReadFrequently: true });
    const pixels = W * H;

    const seekTo = t => new Promise(resolve => {
        video.currentTime = Math.min(t, duration - 0.01);
        video.addEventListener('seeked', resolve, { once: true });
    });

    const grabFrame = () => {
        ctx.drawImage(video, 0, 0, W, H);
        return ctx.getImageData(0, 0, W, H).data;
    };

    // Scintillation-immune diff: subtracts the mean per-channel brightness
    // shift before summing.  Global brightness changes (atmospheric
    // scintillation) cancel out; only localised anomalies (silhouettes)
    // register.  Two passes over the pixels.
    const frameDiff = (a, b) => {
        const n = a.length / 4;
        let mR = 0, mG = 0, mB = 0;
        for (let i = 0; i < a.length; i += 4) {
            mR += a[i] - b[i];
            mG += a[i+1] - b[i+1];
            mB += a[i+2] - b[i+2];
        }
        mR /= n; mG /= n; mB /= n;
        let sum = 0;
        for (let i = 0; i < a.length; i += 4) {
            sum += Math.abs((a[i] - b[i]) - mR)
                 + Math.abs((a[i+1] - b[i+1]) - mG)
                 + Math.abs((a[i+2] - b[i+2]) - mB);
        }
        return sum;
    };

    // Centre-weighted, scintillation-immune diff.  Same mean-subtraction
    // approach, but pixels near the frame centre count more heavily since
    // the sun/moon disk occupies the centre of the telescope FOV.
    const cxHalf = W / 2, cyHalf = H / 2;
    const centreWeightedDiff = (a, b) => {
        const n = a.length / 4;
        let mR = 0, mG = 0, mB = 0;
        for (let i = 0; i < a.length; i += 4) {
            mR += a[i] - b[i];
            mG += a[i+1] - b[i+1];
            mB += a[i+2] - b[i+2];
        }
        mR /= n; mG /= n; mB /= n;
        let sum = 0;
        for (let y = 0; y < H; y++) {
            const dy = (y - cyHalf) / cyHalf;
            for (let x = 0; x < W; x++) {
                const dx = (x - cxHalf) / cxHalf;
                const w = 1.0 - 0.7 * Math.sqrt(dx * dx + dy * dy);
                const i = (y * W + x) * 4;
                sum += w * (Math.abs((a[i] - b[i]) - mR)
                          + Math.abs((a[i+1] - b[i+1]) - mG)
                          + Math.abs((a[i+2] - b[i+2]) - mB));
            }
        }
        return sum;
    };

    // Helper: adaptive threshold = median + max(2×MAD, 0.3×median)
    // Less conservative than 3×MAD so subtle transits aren't missed.
    const adaptiveThreshold = (values) => {
        const s = [...values].sort((a, b) => a - b);
        const med = s[Math.floor(s.length / 2)];
        const mad = [...values].map(d => Math.abs(d - med)).sort((a, b) => a - b)[Math.floor(values.length / 2)];
        return med + Math.max(mad * 2, med * 0.3);
    };

    // --- Coarse pass (0.1s steps, dual signals) ---
    const coarseStep = 0.1;
    const numCoarse = Math.floor(duration / coarseStep);

    await seekTo(0);
    const refFrame = grabFrame();   // Signal B reference (clean background)
    let prevFrame = refFrame;
    const coarseSamples = [];       // { time, consecDiff, refDiff }

    for (let i = 1; i < numCoarse; i++) {
        await seekTo(i * coarseStep);
        const frame = grabFrame();
        coarseSamples.push({
            time: i * coarseStep,
            consecDiff: frameDiff(frame, prevFrame),
            refDiff:    centreWeightedDiff(frame, refFrame),
        });
        prevFrame = frame;
        if (onProgress && i % 10 === 0) onProgress(Math.round(50 * i / numCoarse));
    }

    if (coarseSamples.length < 3) { video.src = ''; return null; }

    // Independent thresholds for each signal
    const threshA = adaptiveThreshold(coarseSamples.map(s => s.consecDiff));
    const threshB = adaptiveThreshold(coarseSamples.map(s => s.refDiff));

    // A sample is a spike if EITHER signal exceeds its threshold
    const spikeIndices = [];
    for (let i = 0; i < coarseSamples.length; i++) {
        if (coarseSamples[i].consecDiff > threshA || coarseSamples[i].refDiff > threshB) {
            spikeIndices.push(i);
        }
    }

    // If nothing exceeds threshold, fall back to the single highest-scoring frame
    // (a real transit might score highest even if it doesn't cross the threshold)
    let effectiveSpikes = spikeIndices;
    if (spikeIndices.length === 0) {
        const best = coarseSamples.reduce((a, b, i) =>
            (b.consecDiff / threshA + b.refDiff / threshB) > (a.val) 
                ? { idx: i, val: b.consecDiff / threshA + b.refDiff / threshB }
                : a,
            { idx: 0, val: 0 }
        );
        effectiveSpikes = [best.idx];
    }

    // Merge spikes into clusters (gap ≤ 5 coarse steps = 0.5s)
    const clusters = [];
    let cStart = effectiveSpikes[0], cEnd = effectiveSpikes[0];
    let cPeak = Math.max(coarseSamples[cStart].consecDiff, coarseSamples[cStart].refDiff);
    for (let k = 1; k < effectiveSpikes.length; k++) {
        if (effectiveSpikes[k] - effectiveSpikes[k-1] <= 5) {
            cEnd = effectiveSpikes[k];
            const s = coarseSamples[cEnd];
            cPeak = Math.max(cPeak, s.consecDiff, s.refDiff);
        } else {
            clusters.push({ start: coarseSamples[cStart].time, end: coarseSamples[cEnd].time, peak: cPeak });
            cStart = cEnd = effectiveSpikes[k];
            cPeak = Math.max(coarseSamples[cStart].consecDiff, coarseSamples[cStart].refDiff);
        }
    }
    clusters.push({ start: coarseSamples[cStart].time, end: coarseSamples[cEnd].time, peak: cPeak });

    // Pick the cluster with the highest peak
    const bestCluster = clusters.reduce((a, b) => b.peak > a.peak ? b : a);

    // --- Fine pass (~30fps around the candidate region) ---
    // Reference-frame (centre-weighted) comparison flags every frame
    // that contains the silhouette, whether it moved or not.
    const margin = 0.5;
    const fineStart = Math.max(0, bestCluster.start - margin);
    const fineEnd = Math.min(duration, bestCluster.end + margin);
    const fineStep = 0.033;
    const numFine = Math.ceil((fineEnd - fineStart) / fineStep);

    // Reference = frame 0 (cleanest background — avoids using a frame that
    // might already contain the transit object as the reference)
    const fineRef = refFrame;
    const fineSamples = [];

    for (let i = 0; i < numFine; i++) {
        const t = fineStart + i * fineStep;
        await seekTo(t);
        const frame = grabFrame();
        fineSamples.push({ time: t, diff: centreWeightedDiff(frame, fineRef) });
        if (onProgress && i % 5 === 0) onProgress(50 + Math.round(50 * i / numFine));
    }

    // Re-threshold on fine samples
    const fineThreshold = adaptiveThreshold(fineSamples.map(s => s.diff));

    let transitStart = null, transitEnd = null;
    for (const s of fineSamples) {
        if (s.diff > fineThreshold) {
            if (transitStart === null) transitStart = s.time;
            transitEnd = s.time;
        }
    }

    video.src = ''; // free memory

    if (transitStart === null) {
        // Fall back to coarse cluster centre
        const center = (bestCluster.start + bestCluster.end) / 2;
        return { center, start: bestCluster.start, end: bestCluster.end,
                 duration: bestCluster.end - bestCluster.start + coarseStep };
    }

    const center = (transitStart + transitEnd) / 2;
    return { center, start: transitStart, end: transitEnd,
             duration: transitEnd - transitStart + fineStep };
}

// ============================================================================
// SIMULATION MODE
// ============================================================================

let simTransitInterval = null;   // drives countdown tick
let simRecBlinkInterval = null;  // drives REC blink
let simCycleTimeout = null;      // schedules next auto-cycle
let recordDelayTimeout = null;   // delays recording start until PRE seconds before transit

const SIM_TRANSIT = {
    flight: 'SIM-001',
    target: 'Moon',
    probability: 'HIGH',
    altitude: 42.3,
    azimuth: 188.7,
};
const SIM_COUNTDOWN_START = 30; // seconds until simulated transit
const SIM_PRE  = 10; // seconds to record before transit
const SIM_POST = 10; // seconds to record after transit

async function toggleSimulation() {
    if (!isSimulating) {
        startSimulation();
    } else {
        stopSimulation();
    }
}

function startSimulation() {
    console.log('[Sim] Starting simulation mode');
    isSimulating = true;

    const simulateBtn = document.getElementById('simulateBtn');
    const connectBtn  = document.getElementById('connectBtn');
    const disconnectBtn = document.getElementById('disconnectBtn');
    const statusDot   = document.getElementById('statusDot');
    const statusText  = document.getElementById('connectionStatus');

    // Toggle .is-active (locked-down keycap) to show simulation is running
    if (simulateBtn)   { simulateBtn.textContent = 'Stop Sim'; simulateBtn.classList.add('is-active'); }
    if (connectBtn)    connectBtn.disabled = true;
    if (disconnectBtn) disconnectBtn.disabled = true;
    if (statusDot)     statusDot.className = 'status-dot connected';
    if (statusText)    statusText.textContent = 'Simulating';

    isConnected = true;
    updateButtonStates();
    startSimulatedPreview();

    // Show SIM badge
    const badge = document.getElementById('simBadge');
    if (badge) badge.style.display = 'block';

    showStatus('Simulation mode active — Using recorded footage', 'info');
    scheduleSimTransit(SIM_COUNTDOWN_START);
}

function stopSimulation() {
    console.log('[Sim] Stopping simulation mode');
    isSimulating = false;
    isConnected = false;

    // Stop eclipse sim if running
    if (_simEclipseActive) stopSimEclipse();

    clearTimeout(simCycleTimeout);
    clearTimeout(recordDelayTimeout);
    clearInterval(simTransitInterval);
    clearInterval(simRecBlinkInterval);
    simTransitInterval = null;
    simRecBlinkInterval = null;
    simCycleTimeout = null;

    // Hide all sim overlays
    ['simBadge','simCountdownOverlay','simRecOverlay','simFlash','simPlane'].forEach(id => {
        const el = document.getElementById(id);
        if (el) el.style.display = 'none';
    });

    // Remove fake transit from list
    upcomingTransits = upcomingTransits.filter(t => t.flight !== SIM_TRANSIT.flight);
    updateTransitList();

    cleanupSimulationFiles();

    const simulateBtn   = document.getElementById('simulateBtn');
    const connectBtn    = document.getElementById('connectBtn');
    const disconnectBtn = document.getElementById('disconnectBtn');
    const statusDot     = document.getElementById('statusDot');
    const statusText    = document.getElementById('connectionStatus');

    // Remove .is-active to restore raised/unselected keycap
    if (simulateBtn)   { simulateBtn.textContent = 'Sim'; simulateBtn.classList.remove('is-active'); }
    if (connectBtn)    connectBtn.disabled = false;
    if (disconnectBtn) disconnectBtn.disabled = true;
    if (statusDot)     statusDot.className = 'status-dot disconnected';
    if (statusText)    statusText.textContent = 'Disconnected';

    updateButtonStates();
    stopSimulatedPreview();
    showStatus('Simulation stopped', 'info');
}

/** Inject a fake transit entry and start its countdown */
function scheduleSimTransit(secondsUntil) {
    if (!isSimulating) return;

    // Insert fake transit into the upcoming list
    const fake = { ...SIM_TRANSIT, seconds_until: secondsUntil };
    upcomingTransits = upcomingTransits.filter(t => t.flight !== SIM_TRANSIT.flight);
    upcomingTransits.unshift(fake);
    updateTransitList();

    let remaining = secondsUntil;

    clearInterval(simTransitInterval);
    simTransitInterval = setInterval(() => {
        if (!isSimulating) { clearInterval(simTransitInterval); return; }
        remaining--;

        // Update transit list countdown
        const entry = upcomingTransits.find(t => t.flight === SIM_TRANSIT.flight);
        if (entry) { entry.seconds_until = remaining; updateTransitList(); }

        // Start recording 10s before transit
        if (remaining === SIM_PRE) {
            showStatus(`🔴 Recording started — transit in ${SIM_PRE}s`, 'success', SIM_PRE * 1000);
            startSimRecording(SIM_PRE + SIM_POST);
        }

        // Show countdown overlay when ≤10s to transit
        const overlay = document.getElementById('simCountdownOverlay');
        if (overlay) {
            if (remaining > 0 && remaining <= SIM_PRE) {
                overlay.style.display = 'block';
                overlay.textContent = `🌙 Transit in ${remaining}s`;
            } else {
                overlay.style.display = 'none';
            }
        }

        if (remaining <= 0) {
            clearInterval(simTransitInterval);
            simTransitInterval = null;
            triggerSimTransit();
        }
    }, 1000);
}

/** Called at the moment of simulated transit — effects only, recording already running */
function triggerSimTransit() {
    if (!isSimulating) return;
    console.log('[Sim] Transit triggered!');

    // Hide countdown overlay
    const countdown = document.getElementById('simCountdownOverlay');
    if (countdown) countdown.style.display = 'none';

    // Audio beep
    playSimBeep();

    // Plane fly-through animation; snapshot captured at mid-flight (~1.6s in)
    animateSimPlane();
    setTimeout(captureSimTransitSnapshot, 1600);

    // Shutter flash
    simCaptureFlash();

    showStatus(`🎯 TRANSIT NOW — recording ${SIM_POST}s more`, 'success', SIM_POST * 1000);
    // Recording auto-stops via startRecordingTimer; auto-cycle is triggered from stopSimRecording()
}

let _simTransitSnapshot = null;  // canvas data URL captured at transit mid-point

/** Composite the live sim video frame + plane image into a canvas thumbnail. */
function captureSimTransitSnapshot() {
    try {
        const video = document.getElementById('simulationVideo');
        const plane = document.getElementById('simPlane');
        const container = document.getElementById('previewContainer');
        if (!video || !container) return;

        const cw = container.offsetWidth  || 640;
        const ch = container.offsetHeight || 360;
        const canvas = document.createElement('canvas');
        canvas.width  = cw;
        canvas.height = ch;
        const ctx = canvas.getContext('2d');

        // Draw video frame letterboxed to avoid distortion
        const vw = video.videoWidth  || cw;
        const vh = video.videoHeight || ch;
        const scale = Math.min(cw / vw, ch / vh);
        const dx = (cw - vw * scale) / 2;
        const dy = (ch - vh * scale) / 2;
        ctx.fillStyle = '#000';
        ctx.fillRect(0, 0, cw, ch);
        ctx.drawImage(video, dx, dy, vw * scale, vh * scale);

        // Draw plane at its *actual* animated position via getBoundingClientRect
        if (plane && plane.style.display !== 'none') {
            const img = plane.querySelector('img');
            const containerRect = container.getBoundingClientRect();
            const planeRect = plane.getBoundingClientRect();
            if (img && img.complete && planeRect.width > 0) {
                const px = planeRect.left - containerRect.left;
                const py = planeRect.top  - containerRect.top;
                ctx.drawImage(img, px, py, planeRect.width, planeRect.height);
            }
        }

        // Label it as a simulation snapshot
        ctx.fillStyle = 'rgba(0,0,0,0.5)';
        ctx.fillRect(0, ch - 22, cw, 22);
        ctx.fillStyle = '#ffd700';
        ctx.font = 'bold 12px monospace';
        ctx.fillText('⚠ SIMULATION — transit captured', 6, ch - 6);

        _simTransitSnapshot = canvas.toDataURL('image/png');
    } catch (e) {
        console.warn('[Sim] Snapshot failed:', e);
        _simTransitSnapshot = null;
    }
}

/** Blinking REC overlay + fake filmstrip entry */

// Canvas compositor state for MediaRecorder-based sim recording
let _simCompositorCanvas = null;
let _simCompositorRAF    = null;
let _simCanvasRecorder   = null;
let _simRecordedChunks   = [];

/** Draw one composited frame: sim video + plane overlay */
function _drawSimFrame(ctx, video, plane, container, cw, ch) {
    const vw = video.videoWidth  || cw;
    const vh = video.videoHeight || ch;
    const scale = Math.min(cw / vw, ch / vh);
    const dx = (cw - vw * scale) / 2;
    const dy = (ch - vh * scale) / 2;
    ctx.fillStyle = '#000';
    ctx.fillRect(0, 0, cw, ch);
    ctx.drawImage(video, dx, dy, vw * scale, vh * scale);

    if (plane && plane.style.display !== 'none') {
        const img = plane.querySelector('img');
        const containerRect = container.getBoundingClientRect();
        const planeRect = plane.getBoundingClientRect();
        if (img && img.complete && planeRect.width > 0) {
            ctx.drawImage(img,
                planeRect.left - containerRect.left,
                planeRect.top  - containerRect.top,
                planeRect.width, planeRect.height);
        }
    }
}

function startSimRecording(duration = SIM_PRE + SIM_POST) {
    isRecording = true;
    recordingIsReal = false;
    recordingStartTime = Date.now();
    updateRecordingUI();
    startRecordingTimer(duration);

    // Start canvas compositor + MediaRecorder so the aircraft is in the video
    const video = document.getElementById('simulationVideo');
    const plane = document.getElementById('simPlane');
    const container = document.getElementById('previewContainer');
    if (video && container && window.MediaRecorder) {
        const cw = container.offsetWidth  || 640;
        const ch = container.offsetHeight || 360;
        _simCompositorCanvas = document.createElement('canvas');
        _simCompositorCanvas.width  = cw;
        _simCompositorCanvas.height = ch;
        const ctx = _simCompositorCanvas.getContext('2d');

        const loop = () => {
            _drawSimFrame(ctx, video, plane, container, cw, ch);
            _simCompositorRAF = requestAnimationFrame(loop);
        };
        loop();

        const mimeType = ['video/webm;codecs=vp9', 'video/webm', '']
            .find(t => !t || MediaRecorder.isTypeSupported(t));
        try {
            const stream = _simCompositorCanvas.captureStream(30);
            _simRecordedChunks = [];
            _simCanvasRecorder = new MediaRecorder(stream, mimeType ? { mimeType } : {});
            _simCanvasRecorder.ondataavailable = e => {
                if (e.data.size > 0) _simRecordedChunks.push(e.data);
            };
            _simCanvasRecorder.start(100);
        } catch (e) {
            console.warn('[Sim] MediaRecorder unavailable, falling back to demo.mp4:', e);
            _simCanvasRecorder = null;
        }
    }

    const rec = document.getElementById('simRecOverlay');
    if (rec) {
        rec.style.display = 'block';
        let visible = true;
        simRecBlinkInterval = setInterval(() => {
            visible = !visible;
            rec.style.opacity = visible ? '1' : '0';
        }, 500);
    }
}

function stopSimRecording() {
    isRecording = false;
    stopRecordingTimer();
    updateRecordingUI();

    clearInterval(simRecBlinkInterval);
    simRecBlinkInterval = null;
    const rec = document.getElementById('simRecOverlay');
    if (rec) rec.style.display = 'none';

    // Stop compositor
    if (_simCompositorRAF) { cancelAnimationFrame(_simCompositorRAF); _simCompositorRAF = null; }

    const snapshot   = _simTransitSnapshot;
    _simTransitSnapshot = null;

    const timestamp  = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
    const fileName   = `sim_transit_${timestamp}.webm`;

    const _addToFilmstrip = (url, path) => {
        const tempFile = {
            name: fileName,
            path,
            url,
            isSimulation: true,
            thumbnail: snapshot || null,
            timestamp: Date.now()
        };
        simulationFiles.push(tempFile);
        if (!window.currentFiles) window.currentFiles = [];
        window.currentFiles.unshift(tempFile);
        updateFilmstrip(window.currentFiles);
    };

    if (_simCanvasRecorder && _simCanvasRecorder.state !== 'inactive') {
        _simCanvasRecorder.onstop = () => {
            const blob = new Blob(_simRecordedChunks, { type: 'video/webm' });
            const url  = URL.createObjectURL(blob);
            _addToFilmstrip(url, url);
            _simCanvasRecorder  = null;
            _simRecordedChunks  = [];
            _simCompositorCanvas = null;
        };
        _simCanvasRecorder.stop();
    } else {
        // Fallback: no MediaRecorder — use demo.mp4
        _addToFilmstrip('/static/simulations/demo.mp4', '/static/simulations/demo.mp4');
        _simCompositorCanvas = null;
    }

    // Auto-cycle: schedule next sim transit 60s after this recording ends
    if (isSimulating) {
        showStatus('✅ Sim transit complete. Next transit in 60s…', 'info', 8000);
        simCycleTimeout = setTimeout(() => {
            if (isSimulating) scheduleSimTransit(SIM_COUNTDOWN_START);
        }, 60000);
    }
}

/** White camera-shutter flash */
function simCaptureFlash() {
    const flash = document.getElementById('simFlash');
    if (!flash) return;
    flash.style.display = 'block';
    flash.style.opacity = '0.9';
    flash.style.transition = 'opacity 0.4s ease';
    setTimeout(() => {
        flash.style.opacity = '0';
        setTimeout(() => { flash.style.display = 'none'; flash.style.transition = ''; }, 420);
    }, 60);
}

/** Aircraft SVG glides across the preview */
function animateSimPlane() {
    const container = document.getElementById('previewContainer');
    const plane = document.getElementById('simPlane');
    if (!plane || !container) return;

    const w = container.offsetWidth;
    const h = container.offsetHeight;
    const planeH = 80; // matches sim_plane.png height
    const y = Math.round(h * 0.42) - Math.round(planeH / 2); // vertically centred slightly above middle

    plane.style.display = 'block';
    plane.style.top = y + 'px';
    plane.style.left = '-140px';
    plane.style.transition = `left 3.2s linear`;

    // Force reflow so transition fires
    plane.getBoundingClientRect();
    plane.style.left = (w + 150) + 'px';

    setTimeout(() => {
        plane.style.display = 'none';
        plane.style.transition = '';
        plane.style.left = '-140px';
    }, 3400);
}

/** Short sine-wave beep via Web Audio API */
function playSimBeep() {
    try {
        const ctx = new (window.AudioContext || window.webkitAudioContext)();
        const osc = ctx.createOscillator();
        const gain = ctx.createGain();
        osc.connect(gain);
        gain.connect(ctx.destination);
        osc.type = 'sine';
        osc.frequency.setValueAtTime(880, ctx.currentTime);
        gain.gain.setValueAtTime(0.3, ctx.currentTime);
        gain.gain.exponentialRampToValueAtTime(0.001, ctx.currentTime + 0.4);
        osc.start(ctx.currentTime);
        osc.stop(ctx.currentTime + 0.4);
    } catch (e) { /* audio not available */ }
}

function startSimulatedPreview() {
    const previewImage       = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot   = document.getElementById('previewStatusDot');
    const previewStatusText  = document.getElementById('previewStatusText');
    const previewTitleIcon   = document.getElementById('previewTitleIcon');
    const previewContainer   = document.getElementById('previewContainer');

    if (previewPlaceholder) previewPlaceholder.style.display = 'none';
    if (previewImage) previewImage.style.display = 'none';

    simulationVideo = document.getElementById('simulationVideo');
    if (!simulationVideo) {
        simulationVideo = document.createElement('video');
        simulationVideo.id = 'simulationVideo';
        simulationVideo.autoplay = true;
        simulationVideo.loop = true;
        simulationVideo.muted = true;
        simulationVideo.style.cssText = 'display:block; flex-shrink:0; margin:auto;';
        simulationVideo.src = '/static/simulations/demo.mp4';
        simulationVideo.onloadedmetadata = () => applyZoom();
        previewContainer.appendChild(simulationVideo);
    } else {
        simulationVideo.style.display = 'block';
        simulationVideo.play();
        applyZoom();
    }

    if (previewStatusDot)  previewStatusDot.className = 'status-dot connected';
    if (previewStatusText) previewStatusText.textContent = 'Simulation Active';
    if (previewTitleIcon)  previewTitleIcon.textContent = '🎬';
}

function stopSimulatedPreview() {
    const previewImage       = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot   = document.getElementById('previewStatusDot');
    const previewStatusText  = document.getElementById('previewStatusText');
    const previewTitleIcon   = document.getElementById('previewTitleIcon');

    if (simulationVideo) { simulationVideo.pause(); simulationVideo.style.display = 'none'; }
    if (previewPlaceholder) previewPlaceholder.style.display = 'flex';
    if (previewImage) previewImage.style.display = 'none';
    if (previewStatusDot)  previewStatusDot.className = 'status-dot';
    if (previewStatusText) previewStatusText.textContent = 'Preview Inactive';
    if (previewTitleIcon)  previewTitleIcon.textContent = '⚫';
}

function simulateCapturePhoto() {
    simCaptureFlash();
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
    const tempFile = {
        name: `sim_capture_${timestamp}.jpg`,
        path: '/static/simulations/demo.mp4',
        url:  '/static/simulations/demo.mp4',
        isSimulation: true,
        timestamp: Date.now()
    };
    simulationFiles.push(tempFile);
    if (!window.currentFiles) window.currentFiles = [];
    window.currentFiles.unshift(tempFile);
    updateFilmstrip(window.currentFiles);
    showStatus('📸 Photo captured (simulation — temporary)', 'success', 5000);
}

function simulateStartRecording(duration, interval) {
    isRecording = true;
    recordingIsReal = false;   // sim recording — real transit can preempt
    recordingStartTime = Date.now();
    updateRecordingUI();
    startRecordingTimer(duration);
    const mode = interval > 0 ? `timelapse (${interval}s interval)` : 'normal';
    showStatus(`🎬 Recording started (simulation — ${duration}s ${mode})`, 'success', 5000);
}

function simulateStopRecording() {
    stopSimRecording();
    showStatus('🎬 Recording stopped (simulation — temporary)', 'success', 5000);
}

function cleanupSimulationFiles() {
    // Revoke blob URLs to free memory before clearing the list
    simulationFiles.forEach(f => {
        if (f.url && f.url.startsWith('blob:')) {
            URL.revokeObjectURL(f.url);
        }
    });
    if (window.currentFiles) {
        window.currentFiles = window.currentFiles.filter(f => !f.isSimulation);
    }
    simulationFiles = [];
    updateFilmstrip(window.currentFiles || []);
}

// ============================================================================
// ECLIPSE SIMULATOR
// ============================================================================
//
// Injects fake eclipseData with compressed contact times so the full
// Outlook → Watch → Warning → Active → Cleared sequence plays in ~2 minutes.
//
// Timeline (seconds after "Sim Eclipse" pressed):
//   T+0   button pressed, type selected
//   T+0   eclipseData injected with C1 = now + 35s  → Watch card appears
//   T+5   C1 within 30s                              → Warning (pulsing)
//   T+35  C1 reached                                 → Active, recording starts
//   T+55  C2 (totality/annularity start, if applicable)
//   T+85  C3 (totality end, if applicable)
//   T+105 C4 reached                                 → Cleared card, rec stops
//   T+135 Cleared card auto-fades, eclipseData cleared
//
// "Show Outlook Banner" checkbox forces the banner visible independently
// of the 48h threshold (useful since the compressed demo skips Outlook).
//
// "Fire Transit" button (visible during Active phase only) injects a fake
// aircraft transit 8s away, triggering recording extension + ✈️ marker.
// ============================================================================

let _simEclipseActive = false;
let _simEclipseTimeout = null;   // used to cancel pending cleanup

// Eclipse type presets: [type, eclipse_class, label emoji]
const SIM_ECLIPSE_TYPES = {
    lunar_total:    { type: 'lunar', eclipse_class: 'total',    target: 'Moon', icon: '🌙' },
    lunar_partial:  { type: 'lunar', eclipse_class: 'partial',  target: 'Moon', icon: '🌙' },
    solar_partial:  { type: 'solar', eclipse_class: 'partial',  target: 'Sun',  icon: '☀️' },
    solar_total:    { type: 'solar', eclipse_class: 'total',    target: 'Sun',  icon: '☀️' },
    solar_annular:  { type: 'solar', eclipse_class: 'annular',  target: 'Sun',  icon: '☀️' },
};

function toggleSimEclipse() {
    if (_simEclipseActive) {
        stopSimEclipse();
    } else {
        startSimEclipse();
    }
}

function startSimEclipse() {
    // Auto-start simulation mode if not already running — Sim Eclipse needs
    // isConnected = true so recording can arm, but shouldn't require a separate click.
    if (!isSimulating) {
        startSimulation();
    }

    _simEclipseActive = true;

    const typeKey  = document.getElementById('simEclipseType')?.value || 'lunar_total';
    const preset   = SIM_ECLIPSE_TYPES[typeKey] || SIM_ECLIPSE_TYPES.lunar_total;

    // Build compressed contact times
    const now = Date.now();
    const hasInnerContacts = preset.eclipse_class !== 'partial';
    const c1 = new Date(now + 35_000);
    const c2 = hasInnerContacts ? new Date(now + 55_000) : null;
    const c3 = hasInnerContacts ? new Date(now + 85_000) : null;
    const c4 = new Date(now + 105_000);
    const max = new Date(now + 70_000);

    eclipseData = {
        type:          preset.type,
        eclipse_class: preset.eclipse_class,
        target:        preset.target,
        c1:            c1.toISOString(),
        c2:            c2 ? c2.toISOString() : null,
        c3:            c3 ? c3.toISOString() : null,
        c4:            c4.toISOString(),
        max:           max.toISOString(),
        seconds_to_c1: 35,   // will be recomputed by updateEclipseState each second
    };
    eclipseAlertLevel = null;  // let updateEclipseState compute it fresh
    eclipseBannerDismissed = false; // reset so banner can show if checkbox ticked

    // Lock-down keycap (.is-active) to show eclipse sim is running
    const btn = document.getElementById('simEclipseBtn');
    if (btn) { btn.textContent = 'Stop Ecl'; btn.classList.add('is-active'); }

    // Show the controls row
    const controls = document.getElementById('simEclipseControls');
    if (controls) controls.style.display = 'flex';

    // Schedule cleanup after Cleared window (C4 + 30 min compressed to C4 + 30s)
    clearTimeout(_simEclipseTimeout);
    _simEclipseTimeout = setTimeout(() => {
        if (_simEclipseActive) stopSimEclipse();
    }, 140_000);  // 105s (C4) + 35s grace

    console.log(`[SimEclipse] Started: ${preset.eclipse_class} ${preset.type}`);
    showStatus(`🌑 Eclipse simulation started (${preset.eclipse_class} ${preset.type})`, 'info', 5000);
}

function stopSimEclipse() {
    _simEclipseActive = false;
    clearTimeout(_simEclipseTimeout);

    // Clear eclipse state and UI
    eclipseData = null;
    eclipseAlertLevel = null;
    eclipseBannerDismissed = false;
    updateEclipseState();  // immediately clears card and banner

    // Restore raised keycap on stop
    const btn = document.getElementById('simEclipseBtn');
    if (btn) { btn.textContent = 'Eclipse'; btn.classList.remove('is-active'); }

    // Hide fire-transit button and controls
    const fireBtn = document.getElementById('simFireTransitBtn');
    if (fireBtn) fireBtn.style.display = 'none';

    // Keep controls row visible (type picker stays accessible)
    console.log('[SimEclipse] Stopped');
    showStatus('Eclipse simulation stopped', 'info', 3000);
}

/**
 * Called from the "Show Outlook Banner" checkbox.
 * Forces the banner visible (bypassing the 48h threshold) regardless of
 * eclipseBannerDismissed state — useful to demo the Outlook level which
 * would otherwise be skipped in the compressed timeline.
 */
function toggleSimEclipseOutlook(checked) {
    const banner = document.getElementById('eclipseBanner');
    const icon   = document.getElementById('eclipseBannerIcon');
    const text   = document.getElementById('eclipseBannerText');
    if (!banner) return;

    if (checked && eclipseData) {
        const isSolar  = eclipseData.type === 'solar';
        const c1       = new Date(eclipseData.c1);
        const typeStr  = `${eclipseData.eclipse_class.charAt(0).toUpperCase()}${eclipseData.eclipse_class.slice(1)} ${isSolar ? 'Solar' : 'Lunar'} Eclipse`;
        const dateStr  = c1.toLocaleString([], { month:'short', day:'numeric', hour:'2-digit', minute:'2-digit' });

        eclipseBannerDismissed = false;
        banner.className = `eclipse-banner ${isSolar ? 'eclipse-solar' : 'eclipse-lunar'}`;
        icon.textContent = isSolar ? '☀️' : '🌙';
        text.textContent = `[SIM] ${typeStr} — ${dateStr}  · Recording will start automatically`;
        banner.style.display = 'flex';
    } else {
        eclipseBannerDismissed = true;
        banner.style.display = 'none';
    }
}

/**
 * Inject a fake aircraft transit 8 seconds away while eclipse is Active.
 * Demonstrates recording extension and ✈️ filmstrip marker.
 */
const SIM_ECLIPSE_TRANSIT = {
    flight: 'SIM-002', target: 'Moon', probability: 'HIGH',
    altitude: 35000, azimuth: 180, seconds_until: 8, handled: false
};

function fireSimTransitDuringEclipse() {
    if (!_simEclipseActive || eclipseAlertLevel !== 'active') {
        showStatus('Fire Transit only works during Active eclipse phase', 'warning', 3000);
        return;
    }

    // Check auto-capture is on; warn if not
    const autoCapture = document.getElementById('autoCaptureToggle');
    if (autoCapture && !autoCapture.checked) {
        showStatus('ℹ️ Auto-capture is off — enabling it for this demo', 'info', 3000);
        autoCapture.checked = true;
    }

    // Inject transit 8s away (handled=false so checkAutoCapture picks it up)
    const fake = { ...SIM_ECLIPSE_TRANSIT, seconds_until: 8, handled: false };
    upcomingTransits = upcomingTransits.filter(t => t.flight !== SIM_ECLIPSE_TRANSIT.flight);
    upcomingTransits.push(fake);
    updateTransitList();

    // Hide the button so it can't be double-fired
    const btn = document.getElementById('simFireTransitBtn');
    if (btn) btn.style.display = 'none';

    showStatus('✈️ Transit fired — watch recording extend and ✈️ marker appear', 'success', 6000);
    console.log('[SimEclipse] Injected transit during active eclipse');
}

/**
 * Hook into updateEclipseState to show/hide the "Fire Transit" button
 * based on the current eclipse alert level.
 */
function _updateSimEclipseFireBtn() {
    const fireBtn = document.getElementById('simFireTransitBtn');
    if (!fireBtn) return;
    if (_simEclipseActive && eclipseAlertLevel === 'active') {
        fireBtn.style.display = '';
    } else {
        fireBtn.style.display = 'none';
    }
}

// Also stop sim eclipse when main simulation is stopped (clean slate)
// Note: we call stopSimEclipse directly from stopSimulation() rather than
// wrapping, to avoid hoisting / double-declaration issues.

// ============================================================================
// REAL-TIME TRANSIT DETECTION
// ============================================================================

/**
 * Toggle detection on/off.
 */
async function toggleDetection() {
    if (isDetecting) {
        await stopDetection();
    } else {
        await startDetection();
    }
}

async function startDetection() {
    const btn = document.getElementById('detectToggleBtn');
    if (btn) btn.disabled = true;

    try {
        const tuning = _loadTuning();
        const result = await apiCall('/telescope/detect/start', 'POST', {
            record_on_detect: true,
            ...tuning,
        });
        if (result && !result.error) {
            isDetecting = true;
            showStatus('🎯 Transit detection started', 'success', 3000);
            if (!detectionPollInterval) {
                detectionPollInterval = setInterval(pollDetectionStatus, 2000);
            }
            // Sync sliders to what the detector actually started with
            if (result.settings) _syncTuningSliders(result.settings);
        } else {
            showStatus(result?.error || 'Failed to start detection', 'error', 5000);
        }
    } catch (e) {
        showStatus('Detection start failed: ' + e.message, 'error', 5000);
    }
    updateDetectionUI();
    if (btn) btn.disabled = false;
}

async function stopDetection() {
    const btn = document.getElementById('detectToggleBtn');
    if (btn) btn.disabled = true;

    try {
        await apiCall('/telescope/detect/stop', 'POST');
        isDetecting = false;
        if (detectionPollInterval) {
            clearInterval(detectionPollInterval);
            detectionPollInterval = null;
        }
        showStatus('Detection stopped', 'info', 3000);
    } catch (e) {
        showStatus('Detection stop failed: ' + e.message, 'error', 5000);
    }
    updateDetectionUI();
    if (btn) btn.disabled = false;
}

async function pollDetectionStatus() {
    try {
        const result = await apiCall('/telescope/detect/status', 'GET');
        if (!result) return;

        // Only transition from running→idle if server explicitly says not running
        // (avoids flicker from transient poll failures)
        if (result.running) {
            isDetecting = true;
        } else if (isDetecting && result.running === false) {
            // Server confirms stopped — respect it
            isDetecting = false;
        }
        detectionStats = {
            fps: result.fps || 0,
            detections: result.detections || 0,
            elapsed_seconds: result.elapsed_seconds || 0,
        };

        // Check for new detection events
        if (result.recent_events && result.recent_events.length > 0) {
            const latest = result.recent_events[result.recent_events.length - 1];
            const latestTs = latest.timestamp;
            if (latestTs !== window._lastDetectionTs) {
                window._lastDetectionTs = latestTs;
                onTransitDetected(latest);
            }
        }

        if (!isDetecting && detectionPollInterval) {
            clearInterval(detectionPollInterval);
            detectionPollInterval = null;
        }

        // Disc-lost watchdog (T03)
        updateDiscLostWarning(result.disc_lost_warning || false, result.disk_detected || false);

        // B4: primed prediction window indicator
        updatePrimedEventBadge(result.primed_events || []);

        // D4/Phase D: live signal sparkline
        if (result.signal_trace && result.signal_trace.length > 0 && isDetecting) {
            const sparkCard = document.getElementById('signalSparkCard');
            if (sparkCard) sparkCard.style.display = '';
            _pushSignalTrace(result.signal_trace);
        } else {
            const sparkCard = document.getElementById('signalSparkCard');
            if (sparkCard && !isDetecting) sparkCard.style.display = 'none';
        }

        updateDetectionUI();
    } catch (e) {
        // Silent — polling failure is transient; don't reset isDetecting
    }
}

function updateDiscLostWarning(lostWarning, diskDetected) {
    const warnId = 'discLostWarning';
    let el = document.getElementById(warnId);

    if (lostWarning && !diskDetected) {
        if (!el) {
            el = document.createElement('div');
            el.id = warnId;
            el.style.cssText =
                'background:#7c2d12; color:#fed7aa; border:1px solid #ea580c; ' +
                'border-radius:6px; padding:6px 10px; margin:6px 0; font-size:0.82em; ' +
                'display:flex; align-items:center; gap:6px;';
            el.innerHTML =
                '<span style="font-size:1.1em;">⚠️</span>' +
                '<span>Disc lost — telescope may be mispointed or solar tracking is off.</span>';
            // Insert before Detection Event History when present.
            const detectPanel = document.getElementById('detectPanel');
            const anchor = document.getElementById('detEventsPanel');
            if (detectPanel && anchor && anchor.parentNode === detectPanel) {
                detectPanel.insertBefore(el, anchor);
            } else if (detectPanel) {
                detectPanel.appendChild(el);
            }
        }
    } else if (el) {
        el.remove();
    }
}

function updatePrimedEventBadge(primedEvents) {
    const badgeId = 'primedEventBadge';
    let el = document.getElementById(badgeId);

    if (primedEvents && primedEvents.length > 0) {
        const ev = primedEvents[0]; // show first/nearest
        const etaLabel = ev.eta_s > 0 ? `ETA ${ev.eta_s}s` : 'NOW';
        const text = `Primed: ${ev.flight_id}  ${etaLabel}  (${ev.sep_deg}°)`;
        if (!el) {
            el = document.createElement('div');
            el.id = badgeId;
            el.style.cssText =
                'background:#1c3a1c; color:#86efac; border:1px solid #22c55e; ' +
                'border-radius:6px; padding:6px 10px; margin:6px 0; font-size:0.82em; ' +
                'display:flex; align-items:center; gap:6px;';
            el.innerHTML =
                '<span style="font-size:1.1em;">🎯</span>' +
                `<span id="${badgeId}Text"></span>`;
            const detectPanel = document.getElementById('detectPanel');
            const anchor = document.getElementById('detEventsPanel');
            if (detectPanel && anchor && anchor.parentNode === detectPanel) {
                detectPanel.insertBefore(el, anchor);
            } else if (detectPanel) {
                detectPanel.appendChild(el);
            }
        }
        const textEl = document.getElementById(`${badgeId}Text`);
        if (textEl) textEl.textContent = text;
    } else if (el) {
        el.remove();
    }
}

function onTransitDetected(event) {
    const ts = new Date(event.timestamp).toLocaleTimeString();
    const flight = event.flight_info;
    const predicted = event.predicted_flight_id;
    let msg = `🎯 Transit detected at ${ts}`;
    if (flight) {
        msg += ` — ${flight.name} (${flight.aircraft_type}) ${flight.separation_deg}°`;
    }
    if (predicted) {
        const matchLabel = flight && flight.name === predicted ? '✅ prediction match' : `predicted: ${predicted}`;
        msg += `  [${matchLabel}]`;
    }
    showStatus(msg, 'success', 10000);

    // Flash the detection indicator
    const indicator = document.getElementById('detectIndicator');
    if (indicator) {
        indicator.classList.add('detect-flash');
        setTimeout(() => indicator.classList.remove('detect-flash'), 2000);
    }

    // Refresh file list to show new recording
    if (event.recording_file) {
        scheduleRefreshFiles([0, 3000, 7000, 11000]);
    }

}

function updateDetectionUI() {
    const btn = document.getElementById('detectToggleBtn');
    const indicator = document.getElementById('detectIndicator');
    const statsEl = document.getElementById('detectStats');

    if (btn) {
        btn.textContent = isDetecting ? 'Stop' : 'Start';
        btn.className = isDetecting
            ? 'btn btn-danger btn-compact'
            : 'btn btn-primary btn-compact';
    }

    if (indicator) {
        if (isDetecting) {
            indicator.className = 'detect-indicator detect-active';
            indicator.innerHTML =
                `<span class="detect-dot"></span>` +
                `<span>Monitoring</span>`;
        } else {
            indicator.className = 'detect-indicator detect-idle';
            indicator.innerHTML = '<span>Idle</span>';
        }
    }

    if (statsEl && isDetecting) {
        const elapsed = detectionStats.elapsed_seconds;
        const mins = Math.floor(elapsed / 60);
        const secs = Math.floor(elapsed % 60);
        statsEl.textContent =
            `${mins}m${secs.toString().padStart(2, '0')}s · ${detectionStats.fps} fps · ${detectionStats.detections} detections`;
        statsEl.style.display = '';
    } else if (statsEl) {
        statsEl.style.display = 'none';
    }
}

/**
 * Sync detection UI on page load — check if already running.
 */
async function syncDetectionUI() {
    try {
        const result = await apiCall('/telescope/detect/status', 'GET');
        if (result && result.running) {
            isDetecting = true;
            detectionStats = {
                fps: result.fps || 0,
                detections: result.detections || 0,
                elapsed_seconds: result.elapsed_seconds || 0,
            };
            if (!detectionPollInterval) {
                detectionPollInterval = setInterval(pollDetectionStatus, 2000);
            }
            // Detection Event History is sourced from /api/transit-events only.
        }
        // Sync tuning sliders from live detector (overrides localStorage when running)
        if (result && result.settings) {
            _syncTuningSliders(result.settings);
        }
    } catch (e) {
        // Detection endpoint may not exist yet — ignore
    }
    updateDetectionUI();
}

// ============================================================================
// LIVE DETECTION TUNING
// ============================================================================

// Default values must match src/transit_detector.py constants
const TUNING_DEFAULTS = {
    disk_margin_pct: 0.25,
    centre_ratio_min: 2.5,
    consec_frames: 7,
    sensitivity_scale: 1.0,
    track_min_mag: 2.0,
    track_min_agree_frac: 0.6,
    mf_threshold_frac: 0.70,
};

const TUNING_PRESETS = {
    lowFp: {
        disk_margin_pct: 0.35,
        centre_ratio_min: 3.5,
        consec_frames: 10,
        sensitivity_scale: 1.6,
        track_min_mag: 3.0,
        track_min_agree_frac: 0.8,
        mf_threshold_frac: 0.85,
    },
    balanced: { ...TUNING_DEFAULTS },
    highDect: {
        disk_margin_pct: 0.20,
        centre_ratio_min: 1.8,
        consec_frames: 5,
        sensitivity_scale: 0.8,
        track_min_mag: 1.0,
        track_min_agree_frac: 0.35,
        mf_threshold_frac: 0.60,
    },
};

/** Load saved tuning from localStorage (fallback to defaults). */
function _loadTuning() {
    return {
        disk_margin_pct:      parseFloat(localStorage.getItem('det_disk_margin')    ?? TUNING_DEFAULTS.disk_margin_pct),
        centre_ratio_min:     parseFloat(localStorage.getItem('det_centre_ratio')   ?? TUNING_DEFAULTS.centre_ratio_min),
        consec_frames:        parseInt(  localStorage.getItem('det_consec_frames')  ?? TUNING_DEFAULTS.consec_frames),
        sensitivity_scale:    parseFloat(localStorage.getItem('det_sensitivity')    ?? TUNING_DEFAULTS.sensitivity_scale),
        track_min_mag:        parseFloat(localStorage.getItem('det_track_mag')      ?? TUNING_DEFAULTS.track_min_mag),
        track_min_agree_frac: parseFloat(localStorage.getItem('det_track_agree')    ?? TUNING_DEFAULTS.track_min_agree_frac),
        mf_threshold_frac:    parseFloat(localStorage.getItem('det_mf_thresh')      ?? TUNING_DEFAULTS.mf_threshold_frac),
    };
}

/** Push current slider values to the live detector API. */
async function _applyDetectionSettings(settings) {
    try {
        await fetch('/telescope/detect/settings', {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(settings),
        });
    } catch (_) {}
}

/** Populate sliders from a settings object (from API or localStorage). */
function _syncTuningSliders(s) {
    const m  = document.getElementById('tunMargin');
    const r  = document.getElementById('tunRatio');
    const c  = document.getElementById('tunConsec');
    const ss = document.getElementById('tunSensitivity');
    const tm = document.getElementById('tunTrackMag');
    const ta = document.getElementById('tunTrackAgree');
    const mf = document.getElementById('tunMFThresh');
    if (m)  { m.value  = Math.round((s.disk_margin_pct ?? TUNING_DEFAULTS.disk_margin_pct) * 100); _updateTuningLabel('tunMargin'); }
    if (r)  { r.value  = s.centre_ratio_min ?? TUNING_DEFAULTS.centre_ratio_min; _updateTuningLabel('tunRatio'); }
    if (c)  { c.value  = s.consec_frames ?? TUNING_DEFAULTS.consec_frames; _updateTuningLabel('tunConsec'); }
    if (ss) { ss.value = s.sensitivity_scale ?? TUNING_DEFAULTS.sensitivity_scale; _updateTuningLabel('tunSensitivity'); }
    if (tm) { tm.value = s.track_min_mag ?? TUNING_DEFAULTS.track_min_mag; _updateTuningLabel('tunTrackMag'); }
    if (ta) { ta.value = Math.round((s.track_min_agree_frac ?? TUNING_DEFAULTS.track_min_agree_frac) * 100); _updateTuningLabel('tunTrackAgree'); }
    if (mf) { mf.value = Math.round((s.mf_threshold_frac ?? TUNING_DEFAULTS.mf_threshold_frac) * 100); _updateTuningLabel('tunMFThresh'); }
}

function _updateTuningLabel(id) {
    const el = document.getElementById(id);
    const lbl = document.getElementById(id + 'Val');
    if (el && lbl) lbl.textContent = el.value;
}

function ensureTuningUI() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;
    if (document.getElementById('tuningBody')) return;

    const saved = _loadTuning();
    const _radarTune = _loadRadarTuning();
    const _alpacaPollSec = _loadAlpacaPollInterval();

    // Add compact TUNE keycap to the detect controls row
    const controls = document.getElementById('detectControls');
    if (controls) {
        const tuneBtn = document.createElement('button');
        tuneBtn.id = 'tuningToggleBtn';
        tuneBtn.className = 'btn btn-compact btn-toggle';
        tuneBtn.title = 'Detection tuning parameters';
        tuneBtn.textContent = 'TUNE';
        tuneBtn.onclick = _toggleTuningBody;
        controls.appendChild(tuneBtn);
    }

    // Collapsible tuning sliders body — inserted directly into detectPanel
    const body = document.createElement('div');
    body.id = 'tuningBody';
    body.style.cssText = 'display:none; margin-bottom:4px;';
    body.innerHTML = `
        <div style="font-size:0.74em;color:#6A7A85;padding:3px 0 5px;border-bottom:1px solid rgba(255,255,255,0.06);margin-bottom:6px;">
            Tuning — changes apply to live detector immediately.
        </div>
        ${_tuningSliderRow('tunMargin',  'Edge Margin %', 5, 50, Math.round(saved.disk_margin_pct*100), 1,
            'Exclude the outermost N% of disk radius (limb zone). Higher = fewer false positives from limb jitter.')}
        ${_tuningSliderRow('tunRatio',   'Centre Ratio', 0.5, 6, saved.centre_ratio_min, 0.1,
            'Inner-disk signal must be N× the limb signal. Higher = stricter concentration requirement.')}
        ${_tuningSliderRow('tunConsec',  'Consec Frames', 2, 20, saved.consec_frames, 1,
            'Fast gate — fires when N consecutive frames all exceed the signal threshold.')}
        ${_tuningSliderRow('tunMFThresh', 'MF Threshold %', 50, 100, Math.round(saved.mf_threshold_frac * 100), 5,
            'Matched-filter gate — fires when at least N% of frames in a sliding window are triggered.')}
        ${_tuningSliderRow('tunSensitivity', 'Sensitivity', 0.2, 3.0, saved.sensitivity_scale, 0.1,
            'Multiplier on both adaptive thresholds. Below 1 = more detections; above 1 = fewer.')}
        ${_tuningSliderRow('tunTrackMag', 'Track Min Motion (px)', 0, 10, saved.track_min_mag, 0.1,
            'Minimum centroid displacement per frame to count as real directional motion. Set 0 to disable.')}
        ${_tuningSliderRow('tunTrackAgree', 'Track Agreement %', 0, 100, Math.round(saved.track_min_agree_frac * 100), 5,
            'Fraction of streak frames that must agree on direction before firing. Set 0 to disable.')}
        <div style="border-top:1px solid rgba(255,255,255,0.08);margin:8px 0 6px;padding-top:6px;">
            <span style="font-size:0.74em;color:#6A7A85;">Radar Tracking</span>
        </div>
        ${_tuningSliderRow('tunRadarAlpha', 'Filter Alpha', 0.05, 0.80, _radarTune.alpha, 0.05,
            'Position weight. Higher = trust measurements more (responsive but jittery). Lower = smoother but laggier.')}
        ${_tuningSliderRow('tunRadarBeta', 'Filter Beta', 0.01, 0.30, _radarTune.beta, 0.01,
            'Velocity weight. Higher = velocity adapts faster to course changes. Lower = more stable heading.')}
        ${_tuningSliderRow('tunRadarTailScale', 'Tail Scale (px/100kph)', 0.5, 20, _radarTune.tailScale, 0.5,
            'Pixels of tail per 100 km/h. Higher = longer tails at speed.')}
        ${_tuningSliderRow('tunRadarStaleGrace', 'Stale Grace (s)', 5, 60, _radarTune.staleGrace, 5,
            'Seconds a track survives without updates before fading out.')}
        <div style="border-top:1px solid rgba(255,255,255,0.08);margin:8px 0 6px;padding-top:6px;">
            <span style="font-size:0.74em;color:#6A7A85;">ALPACA telemetry</span>
        </div>
        ${_tuningSliderRow('tunAlpacaPoll', 'Poll interval (s)', 2, 60, _alpacaPollSec, 1,
            'Seconds between full ALPACA poll cycles (many GETs per cycle). Increase if the scope times out or refuses connections. Also set SEESTAR_ALPACA_POLL_INTERVAL in .env (2–120 s).')}
        <div style="display:flex;gap:6px;margin-top:6px;">
            <button id="tunPresetLowFp" class="btn btn-secondary btn-compact" style="flex:1;"
                title="Most conservative profile. Prioritizes fewer false positives, but may produce fewer detections."
                onclick="_applyTuningPreset('lowFp')">LOW FP</button>
            <button id="tunPresetBalanced" class="btn btn-secondary btn-compact" style="flex:1;"
                title="Balanced profile. Good middle ground between false positives and detections for typical seeing conditions."
                onclick="_applyTuningPreset('balanced')">BALANCED</button>
            <button id="tunPresetHighDect" class="btn btn-secondary btn-compact" style="flex:1;"
                title="High DETECT profile. Tuned for more detections, but with more false positives."
                onclick="_applyTuningPreset('highDect')">HIGH DECT</button>
        </div>
    `;

    // Insert before harness panel
    const harness = document.getElementById('harnessPanel');
    const ref = harness;
    if (ref && ref.parentNode === detectPanel) {
        detectPanel.insertBefore(body, ref);
    } else {
        detectPanel.appendChild(body);
    }

    // Wire up detection sliders
    ['tunMargin', 'tunRatio', 'tunConsec', 'tunMFThresh', 'tunSensitivity', 'tunTrackMag', 'tunTrackAgree'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => {
            _updateTuningLabel(id);
            _debouncedApplyTuning();
        });
    });
    // Wire up radar tracking sliders (persist to localStorage on change)
    const _radarSliderKeys = {
        tunRadarAlpha:       'radar_alpha',
        tunRadarBeta:        'radar_beta',
        tunRadarTailScale:   'radar_tail_scale',
        tunRadarStaleGrace:  'radar_stale_grace',
    };
    Object.entries(_radarSliderKeys).forEach(([sliderId, storageKey]) => {
        const el = document.getElementById(sliderId);
        if (!el) return;
        el.addEventListener('input', () => {
            _updateTuningLabel(sliderId);
            localStorage.setItem(storageKey, el.value);
        });
    });

    const tunAp = document.getElementById('tunAlpacaPoll');
    if (tunAp) {
        const apLbl = document.getElementById('tunAlpacaPollVal');
        if (apLbl) apLbl.textContent = tunAp.value + ' s';
        tunAp.addEventListener('input', () => {
            if (apLbl) apLbl.textContent = tunAp.value + ' s';
            _debouncedApplyAlpacaPoll();
        });
    }

    _markTuningPreset(localStorage.getItem('det_tuning_preset'));
}

function _toggleTuningBody() {
    const body = document.getElementById('tuningBody');
    const btn = document.getElementById('tuningToggleBtn');
    if (!body) return;
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : '';
    if (btn) btn.classList.toggle('is-active', !open);
}

let _tuningDebounceTimer = null;

function _saveDetectionTuningToLocal(settings) {
    localStorage.setItem('det_disk_margin', settings.disk_margin_pct);
    localStorage.setItem('det_centre_ratio', settings.centre_ratio_min);
    localStorage.setItem('det_consec_frames', settings.consec_frames);
    localStorage.setItem('det_sensitivity', settings.sensitivity_scale);
    localStorage.setItem('det_track_mag', settings.track_min_mag);
    localStorage.setItem('det_track_agree', settings.track_min_agree_frac);
    localStorage.setItem('det_mf_thresh', settings.mf_threshold_frac);
}

function _markTuningPreset(activePreset) {
    const buttonMap = {
        lowFp: 'tunPresetLowFp',
        balanced: 'tunPresetBalanced',
        highDect: 'tunPresetHighDect',
    };
    Object.entries(buttonMap).forEach(([key, id]) => {
        const btn = document.getElementById(id);
        if (!btn) return;
        btn.classList.toggle('is-active', key === activePreset);
    });
}

function _applyTuningPreset(presetKey) {
    const preset = TUNING_PRESETS[presetKey];
    if (!preset) return;
    _syncTuningSliders(preset);
    _saveDetectionTuningToLocal(preset);
    localStorage.setItem('det_tuning_preset', presetKey);
    _markTuningPreset(presetKey);
    _applyDetectionSettings(preset);
    if (typeof showStatus === 'function') {
        const names = {
            lowFp: 'LOW FP preset applied',
            balanced: 'BALANCED preset applied',
            highDect: 'HIGH DECT preset applied',
        };
        showStatus(names[presetKey] || 'Preset applied', 'success', 2500);
    }
}

function _debouncedApplyTuning() {
    clearTimeout(_tuningDebounceTimer);
    _tuningDebounceTimer = setTimeout(() => {
        const m  = document.getElementById('tunMargin');
        const r  = document.getElementById('tunRatio');
        const c  = document.getElementById('tunConsec');
        const ss = document.getElementById('tunSensitivity');
        const tm = document.getElementById('tunTrackMag');
        const ta = document.getElementById('tunTrackAgree');
        const mf = document.getElementById('tunMFThresh');
        const settings = {
            disk_margin_pct:      m  ? parseFloat(m.value)  / 100 : TUNING_DEFAULTS.disk_margin_pct,
            centre_ratio_min:     r  ? parseFloat(r.value)        : TUNING_DEFAULTS.centre_ratio_min,
            consec_frames:        c  ? parseInt(c.value)          : TUNING_DEFAULTS.consec_frames,
            sensitivity_scale:    ss ? parseFloat(ss.value)       : TUNING_DEFAULTS.sensitivity_scale,
            track_min_mag:        tm ? parseFloat(tm.value)       : TUNING_DEFAULTS.track_min_mag,
            track_min_agree_frac: ta ? parseInt(ta.value) / 100   : TUNING_DEFAULTS.track_min_agree_frac,
            mf_threshold_frac:    mf ? parseInt(mf.value)  / 100  : TUNING_DEFAULTS.mf_threshold_frac,
        };
        _saveDetectionTuningToLocal(settings);
        localStorage.removeItem('det_tuning_preset');
        _markTuningPreset(null);
        _applyDetectionSettings(settings);
    }, 300);
}

function _resetTuning() {
    localStorage.removeItem('det_disk_margin');
    localStorage.removeItem('det_centre_ratio');
    localStorage.removeItem('det_consec_frames');
    localStorage.removeItem('det_sensitivity');
    localStorage.removeItem('det_track_mag');
    localStorage.removeItem('det_track_agree');
    localStorage.removeItem('det_mf_thresh');
    _syncTuningSliders(TUNING_DEFAULTS);
    _applyDetectionSettings(TUNING_DEFAULTS);
    // Reset radar tracking sliders
    localStorage.removeItem('radar_alpha');
    localStorage.removeItem('radar_beta');
    localStorage.removeItem('radar_tail_scale');
    localStorage.removeItem('radar_stale_grace');
    localStorage.removeItem('radar_entry_margin');
    _syncRadarTuningSliders(RADAR_TUNING_DEFAULTS);
    localStorage.removeItem('alpaca_poll_interval_sec');
    const tunAp = document.getElementById('tunAlpacaPoll');
    const apLbl = document.getElementById('tunAlpacaPollVal');
    if (tunAp) {
        tunAp.value = String(ALPACA_POLL_DEFAULT_SEC);
        if (apLbl) apLbl.textContent = ALPACA_POLL_DEFAULT_SEC + ' s';
    }
    fetch('/telescope/alpaca/settings', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ poll_interval_sec: ALPACA_POLL_DEFAULT_SEC }),
    }).catch(() => {});
}

function _syncRadarTuningSliders(d) {
    const map = {
        tunRadarAlpha:       d.radar_alpha,
        tunRadarBeta:        d.radar_beta,
        tunRadarTailScale:   d.radar_tail_scale,
        tunRadarStaleGrace:  d.radar_stale_grace,
        tunRadarEntryMargin: d.radar_entry_margin,
    };
    Object.entries(map).forEach(([id, val]) => {
        const el = document.getElementById(id);
        if (el) { el.value = val; _updateTuningLabel(id); }
    });
}

function _tuningSliderRow(id, label, min, max, val, step, tooltip) {
    if (step === undefined) step = (max - min > 10) ? 1 : 0.1;
    const tipHtml = tooltip
        ? `<span class="tun-tip-anchor" tabindex="0">ⓘ<span class="tun-tip-box">${tooltip}</span></span>`
        : '';
    return `<div style="margin-bottom:8px;">` +
        `<div style="display:flex; justify-content:space-between; align-items:center; font-size:0.85em; color:#ccc;">` +
        `<span style="display:flex; align-items:center; gap:4px;">${label}${tipHtml}</span>` +
        `<span id="${id}Val">${val}</span></div>` +
        `<input type="range" id="${id}" min="${min}" max="${max}" step="${step}" value="${val}" ` +
        `style="width:100%; accent-color:#4dff88; margin-top:3px;">` +
        `</div>`;
}

// ============================================================================
// DETECTION TEST HARNESS
// ============================================================================

function ensureHarnessUI() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;

    let panel = document.getElementById('harnessPanel');
    if (!panel) {
        panel = document.createElement('div');
        panel.id = 'harnessPanel';
        panel.className = 'harness-card';
        panel.innerHTML = `
            <div class="harness-card-title">🧪 Detection Tester</div>
            <div class="harness-mode-row">
                <label for="harnessMode"
                       class="harness-mode-label"
                       title="Choose how strict the analyzer should be for tester runs">Mode</label>
                <select id="harnessMode"
                        class="harness-mode-select"
                        title="Default = production-like thresholds. Sensitive = lower speed/travel gates and no static filtering, better for slow birds/balloons."
                        aria-label="Detection tester mode: default or sensitive">
                    <option value="default" selected>Default</option>
                    <option value="sensitive">Sensitive</option>
                </select>
            </div>
            <div class="harness-buttons">
                <button class="btn btn-secondary btn-compact"
                        title="Inject a synthetic dark blob across a generated solar/lunar disc and check if the analyzer detects it"
                        aria-label="Inject a synthetic dark blob across a generated solar or lunar disc and check if the analyzer detects it"
                        onclick="runHarnessInject()">💉 Inject</button>
                <button class="btn btn-secondary btn-compact"
                        title="Sweep blob size × speed combinations to map detection sensitivity boundaries (takes ~2 min)"
                        aria-label="Sweep blob size by speed combinations to map detection sensitivity boundaries"
                        onclick="runHarnessSweep()">📊 Sweep</button>
                <button class="btn btn-secondary btn-compact"
                        title="Run the analyzer on all captured MP4s to check for missed transits"
                        aria-label="Run the analyzer on all captured MP4 files to check for missed transits"
                        onclick="runHarnessValidate()">✅ Validate</button>
            </div>
            <div id="harnessStatus" class="harness-status" style="display:none"></div>
        `;

        const detectBtn = document.getElementById('detectToggleBtn');
        if (detectBtn && detectBtn.parentNode === detectPanel) {
            detectBtn.insertAdjacentElement('afterend', panel);
        } else {
            detectPanel.appendChild(panel);
        }
    }

    // Force visible even if stale markup/CSS hid it.
    panel.style.display = 'block';
}

function _harnessStatus(html) {
    const el = document.getElementById('harnessStatus');
    if (!el) return;
    el.style.display = html ? 'block' : 'none';
    el.innerHTML = html || '';
}

function _harnessDisableButtons(disabled) {
    document.querySelectorAll('#harnessPanel .btn').forEach(b => b.disabled = disabled);
    const modeSelect = document.getElementById('harnessMode');
    if (modeSelect) modeSelect.disabled = disabled;
}

function _harnessPreset() {
    const modeSelect = document.getElementById('harnessMode');
    return (modeSelect && modeSelect.value === 'sensitive') ? 'sensitive' : 'default';
}

async function runHarnessInject() {
    _harnessDisableButtons(true);
    _harnessStatus('<span class="harness-info">💉 Injecting synthetic transit…</span>');
    try {
        const preset = _harnessPreset();
        const data = await apiCall('/telescope/harness/inject', 'POST', {
            size: 14, speed: 300, target: 'sun', preset
        });
        if (!data) { _harnessStatus('<span class="harness-miss">Request failed</span>'); return; }
        const icon = data.detected ? '✅' : '❌';
        const cls  = data.detected ? 'harness-hit' : 'harness-miss';
        const badge = data.detected
            ? '<span class="harness-badge harness-badge-hit">PASS</span>'
            : '<span class="harness-badge harness-badge-miss">MISS</span>';
        let html = `<div class="harness-title">Inject Result (${data.preset || preset})</div>`;
        html += `<div class="harness-row"><span>${data.params.size}px @ ${data.params.speed}px/s</span>`;
        html += `<span class="${cls}">${icon} ${data.detected ? 'Detected' : 'Missed'} ${badge}</span></div>`;
        if (data.matched_event) {
            const ev = data.matched_event;
            html += `<div class="harness-row harness-info"><span>${ev.start_seconds?.toFixed(2)}s–${ev.end_seconds?.toFixed(2)}s (${ev.duration_ms}ms, ${ev.confidence})</span></div>`;
        }
        html += `<div class="harness-row harness-info"><span>GT: ${data.gt_start}s–${data.gt_end}s · ${data.num_events} event(s)</span></div>`;
        if (data.preset_description) {
            html += `<div class="harness-note">${data.preset_description}</div>`;
        }
        _harnessStatus(html);
        showStatus(
            data.detected
                ? `Inject (${data.preset || preset}) passed (${data.num_events} event${data.num_events === 1 ? '' : 's'})`
                : `Inject (${data.preset || preset}) missed synthetic transit`,
            data.detected ? 'success' : 'warning',
            5000
        );
    } catch (e) {
        _harnessStatus(`<span class="harness-miss">Error: ${e.message}</span>`);
    } finally {
        _harnessDisableButtons(false);
    }
}

async function runHarnessSweep() {
    _harnessDisableButtons(true);
    _harnessStatus('<span class="harness-info">📊 Sweeping size × speed… (this takes a couple of minutes)</span>');
    try {
        const preset = _harnessPreset();
        const data = await apiCall('/telescope/harness/sweep', 'POST', {
            target: 'sun',
            sizes: [6, 10, 14, 20],
            speeds: [60, 100, 200, 300],
            preset,
        });
        if (!data) { _harnessStatus('<span class="harness-miss">Request failed</span>'); return; }
        const sizes = data.sizes;
        const speeds = data.speeds;
        const grid = data.grid;

        const hasHit = (sz, sp) => {
            const keys = [
                `${sz},${sp}`,
                `${Number(sz)},${Number(sp)}`,
                `${Number(sz).toFixed(1)},${Number(sp).toFixed(1)}`
            ];
            for (const k of keys) {
                if (Object.prototype.hasOwnProperty.call(grid, k)) return !!grid[k];
            }
            if (Array.isArray(data.results)) {
                const found = data.results.find(r => Number(r.size) === Number(sz) && Number(r.speed) === Number(sp));
                if (found) return !!found.detected;
            }
            return false;
        };

        // Build ASCII grid
        let hdr = '     px  ';
        speeds.forEach(sp => hdr += String(sp).padStart(5));
        let rows = hdr + '\n';
        sizes.forEach(sz => {
            let row = String(sz).padStart(6) + 'px ';
            speeds.forEach(sp => {
                const hit = hasHit(sz, sp);
                row += (hit ? '  ✅' : '  ❌') + ' ';
            });
            rows += row + '\n';
        });
        const badge = data.detected > 0
            ? '<span class="harness-badge harness-badge-hit">HAS HITS</span>'
            : '<span class="harness-badge harness-badge-miss">ALL MISSED</span>';
        let html = `<div class="harness-title">Sweep (${data.preset || preset}): ${data.detected}/${data.total} detected (${(data.detection_rate*100).toFixed(0)}%) ${badge}</div>`;
        html += `<div class="harness-grid">${rows}</div>`;
        if ((data.preset || preset) === 'default' && data.detection_rate < 0.6) {
            html += `<div class="harness-note">Next step: switch Mode to <b>Sensitive</b> and rerun Sweep to see slow-object coverage.</div>`;
        } else if ((data.preset || preset) === 'sensitive' && data.detection_rate < 0.6) {
            html += `<div class="harness-note">Still low in Sensitive mode: run Validate on known clips, then we should lower speed/travel gates further.</div>`;
        } else if ((data.preset || preset) === 'sensitive') {
            html += `<div class="harness-note">Sensitive mode improved coverage. Use Default for normal operation; use Sensitive when checking for slower objects.</div>`;
        }
        if (data.preset_description) {
            html += `<div class="harness-note">${data.preset_description}</div>`;
        }
        _harnessStatus(html);
        showStatus(
            `Sweep (${data.preset || preset}) complete: ${data.detected}/${data.total} detected (${(data.detection_rate * 100).toFixed(0)}%)`,
            data.detected > 0 ? 'success' : 'warning',
            6000
        );
    } catch (e) {
        _harnessStatus(`<span class="harness-miss">Error: ${e.message}</span>`);
    } finally {
        _harnessDisableButtons(false);
    }
}

async function runHarnessValidate() {
    _harnessDisableButtons(true);
    _harnessStatus('<span class="harness-info">✅ Validating captured videos…</span>');
    try {
        const preset = _harnessPreset();
        const data = await apiCall('/telescope/harness/validate', 'POST', {target: 'auto', preset});
        if (!data) { _harnessStatus('<span class="harness-miss">Request failed</span>'); return; }
        if (data.results.length === 0) {
            _harnessStatus('<span class="harness-info">No MP4 captures found</span>');
            return;
        }
        let html = `<div class="harness-title">Validate (${data.preset || preset}): ${data.with_events}/${data.total} files with events</div>`;
        data.results.forEach(r => {
            if (r.error) return;
            const name = r.name.length > 28 ? r.name.slice(0, 25) + '…' : r.name;
            const icon = r.num_events > 0 ? '✅' : '⬜';
            const cls = r.num_events > 0 ? 'harness-hit' : 'harness-info';
            html += `<div class="harness-row"><span>${icon} ${name}</span><span class="${cls}">${r.num_events} event(s)</span></div>`;
        });
        if (data.preset_description) {
            html += `<div class="harness-note">${data.preset_description}</div>`;
        }
        _harnessStatus(html);
    } catch (e) {
        _harnessStatus(`<span class="harness-miss">Error: ${e.message}</span>`);
    } finally {
        _harnessDisableButtons(false);
    }
}

// ============================================================================
// D4 — Detection event history log panel
// ============================================================================

let _detEventsPanel = null;
let _detEventsSelection = {
    selected: new Set(),  // Set of event timestamps
    lastClickedIndex: null,  // for shift-range selection
    allEvents: [],  // cache of current event list
};

/**
 * Build the detection-event-history panel inside detectPanel (always visible).
 * Fetches from /api/transit-events on first create; call _refreshDetectionEventHistory to reload.
 */
async function ensureDetectionEventHistoryPanel() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;
    if (_detEventsPanel) return;

    _detEventsPanel = document.createElement('div');
    _detEventsPanel.id = 'detEventsPanel';
    _detEventsPanel.style.cssText =
        'margin:8px;background:#1a1a2e;border:1px solid #2a2a4a;border-radius:6px;padding:8px;';

    const header = document.createElement('div');
    header.className = 'det-events-header';
    header.style.cssText = 'display:flex;align-items:center;justify-content:space-between;margin-bottom:8px;';
    
    const titleWrap = document.createElement('div');
    titleWrap.style.cssText = 'display:flex;align-items:center;gap:8px;';
    
    const title = document.createElement('span');
    title.className = 'det-events-title';
    title.textContent = 'Detection Event History (last 7 days)';
    
    const selCount = document.createElement('span');
    selCount.id = 'detEventsSelCount';
    selCount.style.cssText = 'color:#90caf9;font-size:0.8em;font-weight:normal;';
    selCount.textContent = '';
    
    titleWrap.appendChild(title);
    titleWrap.appendChild(selCount);
    
    const btnWrap = document.createElement('div');
    btnWrap.style.cssText = 'display:flex;gap:6px;align-items:center;';
    
    const clearSelBtn = document.createElement('button');
    clearSelBtn.type = 'button';
    clearSelBtn.id = 'detEventsClearSel';
    clearSelBtn.className = 'btn btn-secondary btn-compact';
    clearSelBtn.textContent = 'Clear Selection';
    clearSelBtn.title = 'Clear selected events (Esc key)';
    clearSelBtn.style.display = 'none';
    clearSelBtn.onclick = () => {
        _detEventsSelection.selected.clear();
        _detEventsSelection.lastClickedIndex = null;
        _updateEventSelectionUI();
    };

    // Bulk-label buttons — visible only when rows are selected
    const _BULK_LABEL_COLORS = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800' };
    const _BULK_LABEL_ICONS  = { tp: '✅TP', fp: '❌FP', fn: '⚠️FN' };
    const bulkLabelWrap = document.createElement('span');
    bulkLabelWrap.id = 'detEventsBulkLabel';
    bulkLabelWrap.style.cssText = 'display:none;gap:4px;align-items:center;';
    ['tp','fp','fn'].forEach(lbl => {
        const b = document.createElement('button');
        b.type = 'button';
        b.className = 'btn btn-compact';
        b.textContent = _BULK_LABEL_ICONS[lbl];
        b.title = `Label all selected as ${lbl.toUpperCase()}`;
        b.style.cssText = `background:${_BULK_LABEL_COLORS[lbl]};border:1px solid ${_BULK_LABEL_COLORS[lbl]};color:#fff;padding:1px 7px;border-radius:3px;cursor:pointer;font-size:0.82em;`;
        b.onclick = () => {
            // Synthesize a fake btnEl pointing at the first selected row's button,
            // but _labelEventBtn reads from _detEventsSelection so any ts works.
            const firstTs = Array.from(_detEventsSelection.selected)[0] || '';
            const fakeBtn = document.createElement('button');
            fakeBtn.setAttribute('data-ts', firstTs);
            fakeBtn.setAttribute('data-lbl', lbl);
            _labelEventBtn(fakeBtn);
        };
        bulkLabelWrap.appendChild(b);
    });

    const retrainBtn = document.createElement('button');
    retrainBtn.type = 'button';
    retrainBtn.className = 'btn btn-secondary btn-compact det-events-retrain-btn';
    retrainBtn.textContent = 'Retrain';
    retrainBtn.title = 'Promote TP/FP-labeled clips from unlabeled, then retrain CNN and reload model (runs in background; may take several minutes)';
    retrainBtn.onclick = () => _startCnnRetrain(retrainBtn);

    btnWrap.appendChild(clearSelBtn);
    btnWrap.appendChild(bulkLabelWrap);
    btnWrap.appendChild(retrainBtn);
    
    header.appendChild(titleWrap);
    header.appendChild(btnWrap);
    _detEventsPanel.appendChild(header);

    const retrainStatus = document.createElement('div');
    retrainStatus.id = 'detEventsRetrainStatus';
    retrainStatus.className = 'det-events-retrain-status';
    retrainStatus.style.display = 'none';
    _detEventsPanel.appendChild(retrainStatus);

    const tableWrap = document.createElement('div');
    tableWrap.id = 'detEventsTableWrap';
    tableWrap.style.cssText = 'overflow-x:auto;max-height:260px;overflow-y:auto;';
    tableWrap.innerHTML = '<div style="color:#555;font-size:0.8em;padding:6px">Loading…</div>';
    _detEventsPanel.appendChild(tableWrap);

    const harnessPanel = document.getElementById('harnessPanel');
    if (harnessPanel && harnessPanel.parentNode === detectPanel) {
        detectPanel.insertBefore(_detEventsPanel, harnessPanel);
    } else {
        detectPanel.appendChild(_detEventsPanel);
    }
    
    // Keyboard shortcut: Escape to clear selection
    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape' && _detEventsSelection.selected.size > 0) {
            _detEventsSelection.selected.clear();
            _detEventsSelection.lastClickedIndex = null;
            _updateEventSelectionUI();
        }
    });
    
    await _refreshDetectionEventHistory();
}

async function _refreshDetectionEventHistory() {
    const wrap = document.getElementById('detEventsTableWrap');
    if (!wrap) return;

    let events = [];
    try {
        const resp = await fetch('/api/transit-events');
        if (resp.ok) events = await resp.json();
    } catch (_) {
        wrap.innerHTML = '<div style="color:#e57373;font-size:0.8em;padding:4px">Failed to load events</div>';
        return;
    }

    if (!events.length) {
        wrap.innerHTML = '<div style="color:#555;font-size:0.8em;padding:4px">No detection events recorded yet</div>';
        return;
    }

    const _LABEL_COLORS = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800', tn: '#9e9e9e' };
    const _LABEL_ICONS  = { tp: '✅TP', fp: '❌FP', fn: '⚠️FN', tn: '⬜TN' };
    const _LABEL_TITLES = {
        tp: 'True positive — real aircraft transit; label as a correct detection for training.',
        fp: 'False positive — not a real transit; trains the CNN to reject similar false alarms.',
        fn: 'False negative — missed or mishandled transit; use when this row should count as a miss for training feedback.',
    };

    const _escAttr = (s) => String(s)
        .replace(/&/g, '&amp;')
        .replace(/"/g, '&quot;')
        .replace(/</g, '&lt;');

    const _normFlightId = (s) => {
        if (typeof window !== 'undefined' && typeof window.normalizeAircraftDisplayId === 'function') {
            return window.normalizeAircraftDisplayId(s);
        }
        return String(s || '').toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 7);
    };

    /** YYYYMMDD_HHMMSS in *local* time — matches vid_* / det_* capture filenames (server uses datetime.now()). */
    const _captureFilenameStem = (v) => {
        if (!v) return null;
        const d = new Date(v);
        if (Number.isNaN(d.getTime())) return null;
        const p = (n) => String(n).padStart(2, '0');
        const y = d.getFullYear();
        const mo = p(d.getMonth() + 1);
        const da = p(d.getDate());
        const h = p(d.getHours());
        const mi = p(d.getMinutes());
        const s = p(d.getSeconds());
        return `${y}${mo}${da}_${h}${mi}${s}`;
    };

    const cols = [
        { key: 'timestamp',          label: 'Time (as in filename)', fmt: v => {
            if (!v) return '<span style="color:#888">—</span>';
            const stem = _captureFilenameStem(v);
            if (!stem) return `<span style="color:#e57373" title="Bad timestamp">${_escAttr(String(v))}</span>`;
            const d = new Date(v);
            const tip = d.toLocaleString(undefined, {
                weekday: 'short', year: 'numeric', month: 'short', day: 'numeric',
                hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false,
            });
            return `<span style="font-family:'SF Mono','Menlo','Consolas',monospace;font-size:0.92em;font-variant-numeric:tabular-nums;color:#c8e6ff" title="${_escAttr(tip)}">${stem}</span>`;
        }},
        { key: 'detected_flight_id', label: 'Flight',       fmt: (v, ev) => {
            const detected = _normFlightId(v || '');
            const predicted = _normFlightId(ev.predicted_flight_id || '');
            const typRaw = ev.aircraft_type != null ? String(ev.aircraft_type).trim() : '';
            const typ = typRaw && typRaw !== 'N/A' ? typRaw : '';
            const ctry = ev.origin_country != null ? String(ev.origin_country).trim() : '';
            const primary = detected || predicted;
            const subParts = [];
            if (typ) subParts.push(typ);
            if (ctry) subParts.push(ctry);
            if (!primary && !subParts.length) return '<span style="color:#777">—</span>';
            const gray = (parts) => parts.map((t) => `<span style="font-size:0.78em;color:#888">${_escAttr(t)}</span>`).join('');
            if (!primary) {
                const [a, ...rest] = subParts;
                return `<span style="font-weight:600;color:#e0e0e0">${_escAttr(a)}</span>${rest.length ? `<br>${gray(rest)}` : ''}`;
            }
            const sub = subParts.length ? `<br>${gray(subParts)}` : '';
            return `<span style="font-weight:600;color:#e0e0e0">${_escAttr(primary)}</span>${sub}`;
        }},
        { key: 'label',              label: 'Label',        fmt: (v, ev) => {
            const cur = v || '';
            const ts = ev.timestamp || '';
            const tsA = _escAttr(ts);
            const btns = ['tp','fp','fn'].map(lbl => {
                const active = cur === lbl;
                const col = active ? _LABEL_COLORS[lbl] : '#333';
                const bord = active ? _LABEL_COLORS[lbl] : '#444';
                const tip = _escAttr(_LABEL_TITLES[lbl] || '');
                return `<button type="button" data-ts="${tsA}" data-lbl="${lbl}"
                    title="${tip}"
                    style="background:${col};border:1px solid ${bord};color:#fff;
                    padding:1px 5px;border-radius:3px;cursor:pointer;font-size:0.78em;
                    margin-right:2px;">${_LABEL_ICONS[lbl]}</button>`;
            }).join('');
            return `<span style="display:inline-flex;align-items:center;">${btns}</span>`;
        }},
        { key: 'confidence_score',   label: 'Score',        fmt: v => {
            const n = parseFloat(v);
            if (isNaN(n)) return '—';
            const col = n >= 0.65 ? '#4caf50' : n >= 0.4 ? '#ff9800' : '#9e9e9e';
            return `<span style="color:${col};font-weight:600">${(n*100).toFixed(0)}%</span>`;
        }},
        { key: 'confidence',         label: 'Grade',        fmt: v => v === 'strong'
            ? '<span style="color:#4caf50">strong</span>'
            : '<span style="color:#9e9e9e">weak</span>' },
        { key: 'notes',              label: 'Notes',        fmt: v => v
            ? `<span style="color:#90caf9;font-size:0.82em">${String(v).replace(/</g, '&lt;')}</span>`
            : '' },
    ];

    const tbl = document.createElement('table');
    tbl.style.cssText = 'width:100%;border-collapse:collapse;font-size:0.78em;';
    const thead = tbl.createTHead();
    const hrow = thead.insertRow();
    cols.forEach(c => {
        const th = document.createElement('th');
        th.textContent = c.label;
        th.style.cssText = 'padding:3px 6px;text-align:left;color:#90caf9;border-bottom:1px solid #2a2a4a;white-space:nowrap;position:sticky;top:0;background:#1a1a2e;';
        hrow.appendChild(th);
    });

    // Suppress browser text-selection on shift/ctrl clicks across the whole table
    tbl.addEventListener('mousedown', (e) => {
        if (e.shiftKey || e.ctrlKey || e.metaKey) e.preventDefault();
    });

    const tbody = tbl.createTBody();
    const visibleEvents = events.slice(0, 100);
    const visibleTimestamps = new Set(
        visibleEvents.map(ev => ev.timestamp).filter(ts => !!ts)
    );

    // Prune stale selection entries that no longer exist in the visible table.
    _detEventsSelection.selected.forEach(ts => {
        if (!visibleTimestamps.has(ts)) _detEventsSelection.selected.delete(ts);
    });
    if (
        _detEventsSelection.lastClickedIndex !== null &&
        (_detEventsSelection.lastClickedIndex < 0 ||
         _detEventsSelection.lastClickedIndex >= visibleEvents.length)
    ) {
        _detEventsSelection.lastClickedIndex = null;
    }

    _detEventsSelection.allEvents = visibleEvents;  // cache for shift-range logic
    visibleEvents.forEach((ev, idx) => {
        const tr = tbody.insertRow();
        const ts = ev.timestamp || '';
        tr.setAttribute('data-ts', ts);
        tr.setAttribute('data-idx', idx);
        tr.setAttribute('data-current-label', ev.label || '');
        tr.style.cssText = 'border-bottom:1px solid #1e1e3a;cursor:pointer;user-select:none;';

        // Selection state
        const isSelected = _detEventsSelection.selected.has(ts);
        if (isSelected) {
            tr.style.background = '#2a3a5a';
        }

        tr.onmouseenter = () => { if (!_detEventsSelection.selected.has(ts)) tr.style.background = '#1e1e3a'; };
        tr.onmouseleave = () => { if (!_detEventsSelection.selected.has(ts)) tr.style.background = ''; };

        cols.forEach(c => {
            const td = tr.insertCell();
            const nowrap = c.key === 'timestamp' ? '' : 'white-space:nowrap;';
            td.style.cssText = `padding:3px 6px;${nowrap}`;
            td.innerHTML = c.fmt(ev[c.key] ?? '', ev);
        });
    });

    const _selectRow = (tr, e) => {
        const timestamp = tr.getAttribute('data-ts');
        const rowIdx = parseInt(tr.getAttribute('data-idx'), 10);
        if (!timestamp || Number.isNaN(rowIdx)) return;

        if (e.shiftKey && _detEventsSelection.lastClickedIndex !== null) {
            const start = Math.min(_detEventsSelection.lastClickedIndex, rowIdx);
            const end = Math.max(_detEventsSelection.lastClickedIndex, rowIdx);
            for (let i = start; i <= end; i++) {
                const evt = _detEventsSelection.allEvents[i];
                if (evt && evt.timestamp) _detEventsSelection.selected.add(evt.timestamp);
            }
            _detEventsSelection.lastClickedIndex = rowIdx;
        } else if (e.ctrlKey || e.metaKey) {
            if (_detEventsSelection.selected.has(timestamp)) {
                _detEventsSelection.selected.delete(timestamp);
            } else {
                _detEventsSelection.selected.add(timestamp);
            }
            _detEventsSelection.lastClickedIndex = rowIdx;
        } else {
            _detEventsSelection.selected.clear();
            _detEventsSelection.selected.add(timestamp);
            _detEventsSelection.lastClickedIndex = rowIdx;
        }
        _updateEventSelectionUI();
    };

    // Delegate click handling for reliable plain + modifier clicks across rows and label buttons.
    tbody.addEventListener('click', (e) => {
        const t = e.target;
        const targetEl = (t && t.nodeType === 1) ? t : (t && t.parentElement ? t.parentElement : null);
        const btn = targetEl && targetEl.closest ? targetEl.closest('button[data-lbl][data-ts]') : null;
        if (btn) {
            const row = btn.closest('tr[data-ts]');
            if (e.shiftKey || e.ctrlKey || e.metaKey) {
                if (row) _selectRow(row, e);
            } else {
                _labelEventBtn(btn);
            }
            e.preventDefault();
            e.stopPropagation();
            return;
        }

        const row = targetEl && targetEl.closest ? targetEl.closest('tr[data-ts]') : null;
        if (row) _selectRow(row, e);
    });

    wrap.innerHTML = '';
    wrap.appendChild(tbl);
    _updateEventSelectionUI();
}

/** T23 — Send a TP/FP/FN label for a detection event to the backend.
 *  If rows are selected, applies the label to all selected rows;
 *  otherwise labels only the clicked row. */
async function _labelEventBtn(btnEl) {
    const clickedTs = btnEl.getAttribute('data-ts');
    const label = btnEl.getAttribute('data-lbl');
    if (!clickedTs || !label) return;

    const _LABEL_COLORS_JS = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800' };

    // Toggle off if clicking the already-active label
    const wrap = document.getElementById('detEventsTableWrap');
    const clickedRow = (btnEl && typeof btnEl.closest === 'function')
        ? btnEl.closest('tr[data-ts]')
        : (wrap && wrap.querySelector(`tr[data-ts="${CSS.escape(clickedTs)}"]`));
    const currentLabel = clickedRow ? (clickedRow.getAttribute('data-current-label') || '') : '';
    const currentlyActive = currentLabel === label;
    const effectiveLabel = currentlyActive ? '' : label;

    // Set lastClickedIndex so shift-click works after a button click
    if (clickedRow) {
        const idx = parseInt(clickedRow.getAttribute('data-idx'), 10);
        if (!isNaN(idx)) _detEventsSelection.lastClickedIndex = idx;
    }

    // Determine which timestamps to label.
    // Only bulk-apply from the row buttons when the clicked row is in the current selection.
    const sel = _detEventsSelection.selected;
    const targets = (sel.size > 0 && sel.has(clickedTs)) ? Array.from(sel) : [clickedTs];

    // Send requests sequentially
    for (const ts of targets) {
        try {
            const resp = await fetch('/api/transit-events/label', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ timestamp: ts, label: effectiveLabel || 'tn' }),
            });
            if (!resp.ok) { console.error('[Label]', ts, await resp.text()); continue; }

            // Update button styles for this row
            if (wrap) {
                const row = wrap.querySelector(`tr[data-ts="${CSS.escape(ts)}"]`);
                if (row) {
                    row.setAttribute('data-current-label', effectiveLabel || '');
                    row.querySelectorAll('button[data-lbl]').forEach(b => {
                        const lbl = b.getAttribute('data-lbl');
                        const active = !effectiveLabel ? false : lbl === effectiveLabel;
                        b.style.background  = active ? (_LABEL_COLORS_JS[lbl] || '#555') : '#333';
                        b.style.borderColor = active ? (_LABEL_COLORS_JS[lbl] || '#666') : '#444';
                    });
                }
            }
        } catch (err) {
            console.error('[Label]', ts, err);
        }
    }

    // Keep stem cache consistent and repaint filmstrip + grid badges
    for (const ts of targets) {
        for (const stem of Object.keys(_stemLabelCache)) {
            if (_stemLabelCache[stem].timestamp === ts) {
                _stemLabelCache[stem].label = effectiveLabel;
            }
        }
    }
    const filmstrip = document.getElementById('filmstripList');
    if (filmstrip) _applyBadges(filmstrip, _stemLabelCache);
    const grid = document.getElementById('filesGrid');
    if (grid) _applyGridBadges(grid, _stemLabelCache);
}

/** Update visual selection state for all event rows. */
function _updateEventSelectionUI() {
    const wrap = document.getElementById('detEventsTableWrap');
    if (!wrap) return;
    const tbody = wrap.querySelector('tbody');
    if (!tbody) return;
    
    tbody.querySelectorAll('tr').forEach(tr => {
        const ts = tr.getAttribute('data-ts');
        const isSelected = _detEventsSelection.selected.has(ts);
        if (isSelected) {
            tr.style.background = '#2a3a5a';
        } else {
            tr.style.background = '';
        }
    });
    
    // Update count display
    const count = _detEventsSelection.selected.size;
    const countEl = document.getElementById('detEventsSelCount');
    if (countEl) {
        if (count > 0) {
            countEl.textContent = `(${count} selected)`;
        } else {
            countEl.textContent = '';
        }
    }
    
    // Show/hide clear button and bulk-label buttons
    const clearBtn = document.getElementById('detEventsClearSel');
    if (clearBtn) clearBtn.style.display = count > 0 ? '' : 'none';

    const bulkWrap = document.getElementById('detEventsBulkLabel');
    if (bulkWrap) {
        bulkWrap.style.display = count > 0 ? 'inline-flex' : 'none';
    }
}

window._labelEventBtn = _labelEventBtn;

let _cnnRetrainPollTimer = null;

async function _startCnnRetrain(btnEl) {
    if (btnEl && btnEl.disabled) return;
    const statusEl = document.getElementById('detEventsRetrainStatus');
    if (btnEl) {
        btnEl.disabled = true;
        btnEl.textContent = '…';
    }
    if (statusEl) {
        statusEl.style.display = 'block';
        statusEl.textContent = 'Starting retrain…';
    }
    try {
        const resp = await fetch('/api/cnn/retrain', { method: 'POST' });
        const data = await resp.json().catch(() => ({}));
        if (!resp.ok) {
            if (statusEl) statusEl.textContent = data.error || resp.statusText || 'Failed to start';
            if (btnEl) {
                btnEl.disabled = false;
                btnEl.textContent = 'Retrain';
            }
            return;
        }
        if (_cnnRetrainPollTimer) clearInterval(_cnnRetrainPollTimer);
        _cnnRetrainPollTimer = setInterval(async () => {
            try {
                const st = await fetch('/api/cnn/retrain/status');
                if (!st.ok) return;
                const s = await st.json();
                if (statusEl) {
                    let t = s.message || '';
                    if (s.error) t += (t ? ' — ' : '') + s.error;
                    statusEl.textContent = t || (s.running ? 'Working…' : 'Idle');
                    statusEl.style.color = s.error ? '#e57373' : '#9cd9ff';
                }
                if (!s.running) {
                    clearInterval(_cnnRetrainPollTimer);
                    _cnnRetrainPollTimer = null;
                    if (btnEl) {
                        btnEl.disabled = false;
                        btnEl.textContent = 'Retrain';
                    }
                    if (document.getElementById('detEventsTableWrap')) {
                        _refreshDetectionEventHistory().catch(() => {});
                    }
                }
            } catch (_) { /* ignore */ }
        }, 1500);
    } catch (e) {
        console.error('[Retrain]', e);
        if (statusEl) {
            statusEl.textContent = String(e);
            statusEl.style.color = '#e57373';
        }
        if (btnEl) {
            btnEl.disabled = false;
            btnEl.textContent = 'Retrain';
        }
    }
}

// ============================================================================
// CLEANUP
// ============================================================================

let _radarAnimFrame = null;
let _radarSweepAfterDetectTimer = null;
let _radarLastTransitTs = 0;        // ms — updated every push; sweep runs while fresh
const RADAR_SWEEP_KEEPALIVE_MS = 660_000; // freeze ~11 min after last transit push (> 10 min poll cycle)
window._radarLastUpdateTs = 0;

window.destroyTelescope = function() {
    if (statusPollInterval)     { clearInterval(statusPollInterval);     statusPollInterval     = null; }
    if (visibilityPollInterval) { clearInterval(visibilityPollInterval); visibilityPollInterval = null; }
    if (lastUpdateInterval)     { clearInterval(lastUpdateInterval);     lastUpdateInterval     = null; }
    if (transitPollInterval)    { clearInterval(transitPollInterval);    transitPollInterval    = null; }
    if (transitTickInterval)    { clearInterval(transitTickInterval);    transitTickInterval    = null; }
    if (detectionPollInterval)  { clearInterval(detectionPollInterval);  detectionPollInterval  = null; }
    if (sunCenterPollInterval)  { clearInterval(sunCenterPollInterval);  sunCenterPollInterval  = null; }
    if (_radarAnimFrame) { cancelAnimationFrame(_radarAnimFrame); _radarAnimFrame = null; }
    _radarLastTransitTs = 0;
};

document.addEventListener('DOMContentLoaded', () => {
    ensureHarnessUI();
    ensureTransitRadar();
});

// ============================================================================
// TRANSIT RADAR SCOPE
// ============================================================================

// ── constants ───────────────────────────────────────────────────────────────
const RADAR_SWEEP_SEC_PER_REV   = 3;       // seconds per full rotation
const RADAR_PREDICT_HORIZON_S   = 12;      // enhanced-mode look-ahead (s)
const RADAR_DISC_DEG            = 0.53;    // solar/lunar disc radius (°)
const RADAR_MAX_SEP_DEG         = 12;      // outer ring = 12° (full LOW range)
const RADAR_HISTORY_MAX         = 50;      // trail length per blip
const RADAR_SWEEP_RUN_AFTER_DETECT_S = 30; // seconds sweep stays on after detection
// ── radar tracking tuning (configurable via TUNE panel, persisted to localStorage) ──
const RADAR_TUNING_DEFAULTS = {
    radar_alpha:        0.30,   // α-β filter: position smoothing weight
    radar_beta:         0.10,   // α-β filter: velocity smoothing weight
    radar_tail_scale:   5,      // pixels of tail per 100 km/h of ADS-B speed
    radar_stale_grace:  15,     // seconds before a stale track is removed
    radar_entry_margin: 0,      // (deprecated — edge squares now handle far-away aircraft)
};

function _loadRadarTuning() {
    const d = RADAR_TUNING_DEFAULTS;
    return {
        alpha:       parseFloat(localStorage.getItem('radar_alpha')        ?? d.radar_alpha),
        beta:        parseFloat(localStorage.getItem('radar_beta')         ?? d.radar_beta),
        tailScale:   parseFloat(localStorage.getItem('radar_tail_scale')   ?? d.radar_tail_scale),
        staleGrace:  parseFloat(localStorage.getItem('radar_stale_grace')  ?? d.radar_stale_grace),
        entryMargin: parseFloat(localStorage.getItem('radar_entry_margin') ?? d.radar_entry_margin),
    };
}

const ALPACA_POLL_DEFAULT_SEC = 5;

/** Seconds between ALPACA telemetry poll cycles (TUNE panel + localStorage). */
function _loadAlpacaPollInterval() {
    const raw = localStorage.getItem('alpaca_poll_interval_sec');
    const n = raw != null ? parseFloat(raw) : ALPACA_POLL_DEFAULT_SEC;
    const v = Math.round(Number.isFinite(n) ? n : ALPACA_POLL_DEFAULT_SEC);
    return Math.min(60, Math.max(2, v));
}

async function syncAlpacaPollSliderFromServer() {
    const el = document.getElementById('tunAlpacaPoll');
    if (!el) return;
    try {
        const r = await fetch('/telescope/alpaca/settings');
        if (!r.ok) return;
        const j = await r.json();
        if (typeof j.poll_interval_sec !== 'number' || !Number.isFinite(j.poll_interval_sec)) return;
        const s = Math.round(Math.min(60, Math.max(2, j.poll_interval_sec)));
        el.value = String(s);
        const lbl = document.getElementById('tunAlpacaPollVal');
        if (lbl) lbl.textContent = s + ' s';
        localStorage.setItem('alpaca_poll_interval_sec', String(s));
    } catch (_) { /* offline or ALPACA disabled */ }
}

let _alpacaPollDebounceTimer = null;
function _debouncedApplyAlpacaPoll() {
    clearTimeout(_alpacaPollDebounceTimer);
    _alpacaPollDebounceTimer = setTimeout(async () => {
        const el = document.getElementById('tunAlpacaPoll');
        if (!el) return;
        const sec = Math.round(parseFloat(el.value)) || ALPACA_POLL_DEFAULT_SEC;
        const clamped = Math.min(60, Math.max(2, sec));
        localStorage.setItem('alpaca_poll_interval_sec', String(clamped));
        try {
            await fetch('/telescope/alpaca/settings', {
                method: 'PATCH',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ poll_interval_sec: clamped }),
            });
        } catch (_) {}
    }, 400);
}

// ── state ───────────────────────────────────────────────────────────────────
const _radarTracks       = new Map();  // id → track object (see pushInterceptPoint)
let   _radarCanvas       = null;
let   _radarCtx          = null;
let   _radarSweepStart   = null;       // performance.now() reference (null = frozen)
let   _radarSweepAngle   = 0;          // frozen angle (rad) when sweep is paused
let   _radarMode         = 'default';  // 'default' | 'enhanced'
let   _radarHoveredId    = null;
let   _radarPinnedId     = null;
let   _radarHitTest      = [];         // [{id, x, y, r}] rebuilt each frame
let   _radarSweepActive  = false;

// ── helpers ─────────────────────────────────────────────────────────────────
function _hexToRgba(hex, alpha) {
    const r = parseInt(hex.slice(1,3),16);
    const g = parseInt(hex.slice(3,5),16);
    const b = parseInt(hex.slice(5,7),16);
    return `rgba(${r},${g},${b},${alpha})`;
}

function _radarAzDelta(azCraft, azTarget) {
    let d = azCraft - azTarget;
    while (d >  180) d -= 360;
    while (d < -180) d += 360;
    return d;
}
function _radarAltFtStr(ft) { return ft != null ? `${Math.round(ft).toLocaleString()} ft` : '—'; }
function _radarSpeedStr(kmh){ return kmh != null ? `${Math.round(kmh)} km/h` : '—'; }

function _radarVelocity(pts) {
    if (pts.length < 2) return null;
    const a = pts[pts.length-2], b = pts[pts.length-1];
    const dt = (b.t - a.t) / 1000;
    if (dt < 0.1) return null;
    return { dAlt: (b.altD - a.altD)/dt, dAz: (b.azD - a.azD)/dt };
}

function _radarPolar(altD, azD, R) {
    // azD → tangential, altD → radial; map to canvas polar coords
    const sepDeg = Math.hypot(altD, azD);
    const ang    = Math.atan2(azD, altD);  // 0 = up (alt-only offset)
    const frac   = Math.min(sepDeg / RADAR_MAX_SEP_DEG, 1);
    return { r: frac * R, ang };
}

function _radarBlipXY(pt, cx, cy, R) {
    const { r, ang } = _radarPolar(pt.altD, pt.azD, R);
    return { x: cx + r * Math.sin(ang), y: cy - r * Math.cos(ang) };
}

// ── resize ──────────────────────────────────────────────────────────────────
function _resizeRadarCanvas() {
    if (!_radarCanvas) return;
    const W = _radarCanvas.offsetWidth;
    _radarCanvas.width  = W;
    _radarCanvas.height = W;
}

// ── draw frame ──────────────────────────────────────────────────────────────
let _radarLastEtaRender = 0;
function _radarDrawFrame(ts) {
    _radarAnimFrame = requestAnimationFrame(_radarDrawFrame);
    // Tick the upcoming-transit ETA countdown once per second
    if (ts - _radarLastEtaRender > 1000) {
        _radarLastEtaRender = ts;
        _renderUpcomingTransits();
    }

    const canvas = _radarCanvas;
    if (!canvas || !canvas.isConnected) return;
    const ctx = _radarCtx;
    const W = canvas.width, H = canvas.height;
    if (W < 10 || H < 10) return;
    const cx = W/2, cy = H/2;
    const R  = Math.min(cx, cy) - 8;

    const nowMs = Date.now();
    const tune  = _loadRadarTuning();
    const staleGraceMs = tune.staleGrace * 1000;
    // Dead-reckon stale tracks; remove when off-screen or past stale grace
    for (const [id, tr] of _radarTracks.entries()) {
        if (!tr.lastUpdateT) continue;
        const ageSec   = (nowMs - tr.lastUpdateT) / 1000;
        const staleSec = (nowMs - (tr.lastSeenT || tr.lastUpdateT)) / 1000;
        // Past stale grace with no backend confirmation — remove
        if (staleSec * 1000 > staleGraceMs) {
            _radarTracks.delete(id);
            continue;
        }
        // Dead-reckon position if we have velocity and data is stale (> 0.5s)
        if (ageSec > 0.5 && (Math.abs(tr.vAltD) > 0.0001 || Math.abs(tr.vAzD) > 0.0001)) {
            const drAltD = tr.altD + tr.vAltD * ageSec;
            const drAzD  = tr.azD  + tr.vAzD  * ageSec;
            const isEdgeDr = (tr.curSep ?? 0) > RADAR_MAX_SEP_DEG;
            // Inside-field tracks that dead-reckon off-screen — remove
            if (!isEdgeDr && Math.hypot(drAltD, drAzD) > RADAR_MAX_SEP_DEG * 1.3) {
                _radarTracks.delete(id);
                continue;
            }
            if (isEdgeDr) {
                // Edge track: update bearing in-place (no trail)
                tr.altD = drAltD;
                tr.azD  = drAzD;
                tr.lastUpdateT = nowMs;
            } else {
                // Append dead-reckoned point to trail (min 1s gap)
                const lastPt = tr.points[tr.points.length - 1];
                if (!lastPt || nowMs - lastPt.t >= 1000) {
                    tr.points.push({ altD: drAltD, azD: drAzD, t: nowMs });
                    if (tr.points.length > RADAR_HISTORY_MAX) tr.points.shift();
                }
            }
        }
        // Compute stale opacity for rendering (1.0 → 0.2 over grace period)
        tr._staleAlpha = staleSec > 2 ? Math.max(0.2, 1.0 - (staleSec / tune.staleGrace) * 0.8) : 1.0;
    }

    // derive active state from last-seen timestamp (no fixed timer)
    const recentMeasurement = !!window._radarLastUpdateTs &&
        (Date.now() - window._radarLastUpdateTs) < 5000;
    _radarSweepActive = recentMeasurement &&
                        _radarLastTransitTs > 0 &&
                        (Date.now() - _radarLastTransitTs) < RADAR_SWEEP_KEEPALIVE_MS;

    // sweep angle
    let sweepAng;
    if (_radarSweepActive) {
        if (_radarSweepStart == null) _radarSweepStart = ts;
        const elapsed = (ts - _radarSweepStart) / 1000;
        sweepAng = ((elapsed / RADAR_SWEEP_SEC_PER_REV) % 1) * Math.PI * 2;
        _radarSweepAngle = sweepAng;
    } else {
        _radarSweepStart = null;   // reset so it restarts cleanly next time
        sweepAng = _radarSweepAngle;
    }

    // ── background ──────────────────────────────────────────────────────────
    ctx.clearRect(0, 0, W, H);
    ctx.fillStyle = '#050a10';
    ctx.fillRect(0, 0, W, H);

    // rings
    const ringFracs = [0.25, 0.5, 0.75, 1.0];
    const ringLabels = ['3°','6°','9°','12°'];
    ctx.strokeStyle = 'rgba(0,180,80,0.18)';
    ctx.lineWidth   = 1;
    for (let i = 0; i < ringFracs.length; i++) {
        ctx.beginPath();
        ctx.arc(cx, cy, ringFracs[i]*R, 0, Math.PI*2);
        ctx.stroke();
        if (ringFracs[i] < 1) {
            ctx.fillStyle = 'rgba(0,180,80,0.28)';
            ctx.font = '9px monospace';
            ctx.textAlign = 'left';
            ctx.fillText(ringLabels[i], cx + ringFracs[i]*R + 3, cy + 3);
        }
    }
    // disc circle
    const discR = (RADAR_DISC_DEG / RADAR_MAX_SEP_DEG) * R;
    ctx.strokeStyle = 'rgba(255,220,50,0.35)';
    ctx.lineWidth = 1;
    ctx.setLineDash([3,3]);
    ctx.beginPath(); ctx.arc(cx, cy, discR, 0, Math.PI*2); ctx.stroke();
    ctx.setLineDash([]);

    // crosshairs
    ctx.strokeStyle = 'rgba(0,180,80,0.15)';
    ctx.lineWidth = 1;
    [[0, -R, 0, R],[-R, 0, R, 0]].forEach(([x1,y1,x2,y2])=>{
        ctx.beginPath(); ctx.moveTo(cx+x1,cy+y1); ctx.lineTo(cx+x2,cy+y2); ctx.stroke();
    });

    // ── sweep wedge (always visible; only rotates when active) ──────────────
    {
        const wedgeAlpha = _radarSweepActive ? 0.10 : 0.05;
        const lineAlpha  = _radarSweepActive ? 0.85 : 0.35;
        ctx.save();
        ctx.translate(cx, cy);
        ctx.rotate(sweepAng);
        // Radial gradient: bright at centre, fades to transparent at rim
        const grad = ctx.createRadialGradient(0, 0, 0, 0, 0, R);
        grad.addColorStop(0,   `rgba(0,255,80,${wedgeAlpha * 2.5})`);
        grad.addColorStop(0.4, `rgba(0,255,80,${wedgeAlpha * 1.2})`);
        grad.addColorStop(1,   `rgba(0,255,80,0)`);
        ctx.beginPath();
        ctx.moveTo(0, 0);
        // arc goes counter-clockwise (anticlockwise=true) so glow trails behind the line
        ctx.arc(0, 0, R, -Math.PI/2, -Math.PI/2 - Math.PI*0.55, true);
        ctx.closePath();
        ctx.fillStyle = grad;
        ctx.fill();
        ctx.restore();

        ctx.save();
        ctx.strokeStyle = `rgba(0,255,80,${lineAlpha})`;
        ctx.lineWidth = _radarSweepActive ? 1.5 : 1;
        if (_radarSweepActive) { ctx.shadowBlur = 4; ctx.shadowColor = '#00ff50'; }
        ctx.beginPath();
        ctx.moveTo(cx, cy);
        ctx.lineTo(cx + R*Math.sin(sweepAng), cy - R*Math.cos(sweepAng));
        ctx.stroke();
        ctx.restore();
    }

    // ── blips ────────────────────────────────────────────────────────────────
    _radarHitTest = [];
    const beamHalf = Math.PI * 2 / (RADAR_SWEEP_SEC_PER_REV * 60); // ~1 frame

    // sort ascending level so HIGH draws on top
    const sorted = [..._radarTracks.values()].sort((a,b) => a.level - b.level);

    sorted.forEach(track => {
        const id    = track.id;
        const color = track.color;
        const staleA = track._staleAlpha ?? 1.0;
        const isEdge = (track.curSep ?? 0) > RADAR_MAX_SEP_DEG;

        if (isEdge) {
            // ── EDGE SQUARE: aircraft outside 12 deg — pinned on the rim ─
            const ang = _radarPolar(track.altD, track.azD, R).ang;
            const ex  = cx + R * Math.sin(ang);
            const ey  = cy - R * Math.cos(ang);
            const sqR = 5;
            const alpha = 0.75 * staleA;

            // Square marker
            ctx.fillStyle = _hexToRgba(color, alpha);
            ctx.fillRect(ex - sqR, ey - sqR, sqR * 2, sqR * 2);
            ctx.strokeStyle = _hexToRgba(color, alpha * 0.6);
            ctx.lineWidth = 1;
            ctx.strokeRect(ex - sqR, ey - sqR, sqR * 2, sqR * 2);

            // Label: registration + ETA (drawn inside the rim to avoid clipping)
            let lbl = track.label;
            if (track.etaSec != null) {
                const aged = track.etaSec - (nowMs - (track.etaBaseT || nowMs)) / 1000;
                if (aged > 0) {
                    const m = Math.floor(aged / 60);
                    const s = Math.floor(aged % 60);
                    lbl += ' ' + m + ':' + String(s).padStart(2, '0');
                }
            }
            ctx.fillStyle = _hexToRgba(color, 0.8 * staleA);
            ctx.font = '8px monospace';
            const lx = cx + (R - 14) * Math.sin(ang);
            const ly = cy - (R - 14) * Math.cos(ang);
            ctx.textAlign = 'center';
            ctx.fillText(lbl, lx, ly + 3);

            _radarHitTest.push({id, x: ex, y: ey, r: sqR + 4});
            return;
        }

        // ── DIAMOND BLIP: aircraft within 12 deg ─────────────────────────
        if (!track.points.length) return;

        const ageSec = (nowMs - track.lastUpdateT) / 1000;
        const curAltD = track.altD + track.vAltD * Math.min(ageSec, 60);
        const curAzD  = track.azD  + track.vAzD  * Math.min(ageSec, 60);

        const drawPts = track.points;

        // trail
        if (drawPts.length > 1) {
            ctx.beginPath();
            drawPts.forEach((p, i) => {
                const {x,y} = _radarBlipXY(p, cx, cy, R);
                i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
            });
            ctx.strokeStyle = _hexToRgba(color, 0.25 * staleA);
            ctx.lineWidth = 1;
            ctx.stroke();
        }

        const curPt = { altD: curAltD, azD: curAzD };
        const {x, y} = _radarBlipXY(curPt, cx, cy, R);
        const dAng = _radarSweepActive
            ? ((sweepAng - _radarPolar(curAltD, curAzD, R).ang) % (Math.PI*2) + Math.PI*2) % (Math.PI*2)
            : 0;
        const lit   = !_radarSweepActive || dAng < beamHalf + 0.15;
        const alpha = (lit ? 1.0 : 0.82) * staleA;
        const blipR = track.level >= 3 ? 6 : track.level === 2 ? 5 : 4;

        // ── velocity / heading line (tadpole tail) ───────────────────────
        const speed2d = Math.hypot(track.vAltD, track.vAzD);
        const speedKmh = track.speedKmh;
        if (speed2d > 0.0005 && speedKmh != null && speedKmh > 0) {
            const eps = 0.01;
            const stepAlt = track.vAltD / speed2d * eps;
            const stepAz  = track.vAzD  / speed2d * eps;
            const refXY = _radarBlipXY({ altD: curAltD + stepAlt, azD: curAzD + stepAz }, cx, cy, R);
            const ang = Math.atan2(refXY.y - y, refXY.x - x);
            const len = Math.min(Math.max((speedKmh / 100) * tune.tailScale, 4), R * 0.5);
            ctx.save();
            ctx.strokeStyle = _hexToRgba(color, alpha * 0.9);
            ctx.lineWidth   = 1.5;
            ctx.beginPath();
            ctx.moveTo(x, y);
            ctx.lineTo(x + len * Math.cos(ang), y + len * Math.sin(ang));
            ctx.stroke();
            ctx.restore();
        }

        // ── diamond blip ──────────────────────────────────────────────────
        if (lit && _radarSweepActive) {
            ctx.save();
            ctx.shadowBlur  = 14;
            ctx.shadowColor = color;
            ctx.fillStyle   = '#fff';
            ctx.beginPath();
            ctx.moveTo(x,          y - (blipR + 2));
            ctx.lineTo(x + blipR + 2, y);
            ctx.lineTo(x,          y + (blipR + 2));
            ctx.lineTo(x - blipR - 2, y);
            ctx.closePath();
            ctx.fill();
            ctx.restore();
        }
        ctx.fillStyle = _hexToRgba(color, alpha);
        ctx.beginPath();
        ctx.moveTo(x,       y - blipR);
        ctx.lineTo(x + blipR, y);
        ctx.lineTo(x,       y + blipR);
        ctx.lineTo(x - blipR, y);
        ctx.closePath();
        ctx.fill();

        // ring for hovered/pinned
        if (id === _radarHoveredId || id === _radarPinnedId) {
            ctx.strokeStyle = _hexToRgba(color, 0.9);
            ctx.lineWidth = 1.5;
            ctx.beginPath();
            ctx.moveTo(x,           y - (blipR + 5));
            ctx.lineTo(x + blipR + 5, y);
            ctx.lineTo(x,           y + (blipR + 5));
            ctx.lineTo(x - blipR - 5, y);
            ctx.closePath();
            ctx.stroke();
        }

        // label
        ctx.fillStyle = _hexToRgba(color, 0.85);
        ctx.font = '9px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(track.label, x + blipR + 3, y + 3);

        _radarHitTest.push({id, x, y, r: blipR + 6});
    });

    // centre dot (target body)
    ctx.fillStyle = 'rgba(255,220,50,0.85)';
    ctx.beginPath(); ctx.arc(cx, cy, 4, 0, Math.PI*2); ctx.fill();

    // empty state
    if (_radarTracks.size === 0) {
        ctx.fillStyle = 'rgba(0,180,80,0.35)';
        ctx.font = '11px monospace';
        ctx.textAlign = 'center';
        ctx.fillText('Waiting for transit candidates…', cx, cy + R*0.55);
    }
}

// ── hit testing ──────────────────────────────────────────────────────────────
function _radarPick(mx, my) {
    for (const h of _radarHitTest) {
        if (Math.hypot(mx-h.x, my-h.y) <= h.r) return h.id;
    }
    return null;
}

// ── tooltip ──────────────────────────────────────────────────────────────────
function _radarShowTooltip(id, x, y) {
    let tt = document.getElementById('radarTooltip');
    if (!tt) {
        tt = document.createElement('div');
        tt.id = 'radarTooltip';
        tt.style.cssText =
            'position:absolute;pointer-events:none;background:rgba(5,12,20,0.92);' +
            'border:1px solid rgba(0,255,80,0.4);border-radius:6px;padding:6px 9px;' +
            'font:11px/1.6 monospace;color:#c8ffd0;z-index:9999;white-space:nowrap;' +
            'box-shadow:0 0 8px rgba(0,255,80,0.25);';
        document.body.appendChild(tt);
    }
    const t = _radarTracks.get(id);
    if (!t) return;
    const lvlStr = t.level >= 3 ? 'HIGH' : t.level === 2 ? 'MEDIUM' : 'LOW';
    const lvlCol = t.level >= 3 ? '#00FF00' : t.level === 2 ? '#FFFF00' : '#808080';
    const sep    = Math.hypot(t.altD, t.azD).toFixed(2);
    tt.innerHTML = `
        <div style="font-weight:700;font-size:12px;letter-spacing:.05em">${t.label}</div>
        <div>${_radarAltFtStr(t.altFt)}</div>
        <div>${_radarSpeedStr(t.speedKmh)}</div>
        <div>Sep&nbsp;<b>${sep}°</b></div>
        <div style="color:${lvlCol}">${lvlStr}</div>
    `;
    const canvas = _radarCanvas;
    const rect   = canvas ? canvas.getBoundingClientRect() : {left:0,top:0};
    tt.style.left = (rect.left + x + 12) + 'px';
    tt.style.top  = (rect.top  + y -  8 + window.scrollY) + 'px';
    tt.style.display = 'block';
}

function _radarHideTooltip() {
    const tt = document.getElementById('radarTooltip');
    if (tt) tt.style.display = 'none';
}

// ── sweep control ────────────────────────────────────────────────────────────
// Sweep stays active as long as transit candidates keep arriving.
// It freezes only after RADAR_SWEEP_KEEPALIVE_MS with no new pushes.
function _radarMarkTransitSeen() {
    _radarLastTransitTs = Date.now();
}

// Call from outside when a transit is confirmed
window.onTransitDetected = function() { _radarMarkTransitSeen(); };

// Mark tracks not in the active set as stale (graceful fade-out instead of
// instant deletion).  Actual removal happens in _radarDrawFrame when the
// stale grace period expires.
window.pruneRadarTracks = function(activeIds) {
    const testIds = new Set(_TEST_AIRCRAFT.map(a => a.id));
    for (const [id, tr] of _radarTracks.entries()) {
        if (testIds.has(id)) continue; // never prune synthetic test tracks
        if (activeIds.has(id)) {
            tr.lastSeenT = Date.now();  // refresh
        }
        // Tracks not in activeIds simply don't get their lastSeenT refreshed,
        // so they will fade and be removed by the stale grace logic in _radarDrawFrame.
    }
};

// ── pushInterceptPoint (public API for app.js) ───────────────────────────────
// Uses CURRENT position (where the aircraft is NOW relative to the target) for
// radar blip placement, with an α-β filter for smooth motion.  Closest-approach
// data is stored separately for ETA and edge-marker rendering.
window.pushInterceptPoint = function(flight) {
    const id = String(flight.id || flight.name || '').trim().toUpperCase();
    if (!id) return;
    const level = parseInt(flight.possibility_level ?? 0);
    if (level < 1 || level > 3) return;

    const color = level >= 3 ? '#00FF00' : level === 2 ? '#FFFF00' : '#808080';

    // ── Current position: where the aircraft is NOW relative to the target ──
    // Prefer backend current_signed_* fields; fall back to computing from altaz
    let measAltD = parseFloat(flight.current_signed_alt_diff ?? 'NaN');
    let measAzD  = parseFloat(flight.current_signed_az_diff  ?? 'NaN');
    if (!isFinite(measAltD) || !isFinite(measAzD)) {
        // Fall back to closest-approach signed diffs (old backend)
        measAltD = parseFloat(flight.signed_alt_diff ?? 'NaN');
        measAzD  = parseFloat(flight.signed_az_diff  ?? 'NaN');
    }
    if (!isFinite(measAltD) || !isFinite(measAzD)) {
        const pAlt = parseFloat(flight.plane_alt  ?? 'NaN');
        const tAlt = parseFloat(flight.target_alt ?? 'NaN');
        const pAz  = parseFloat(flight.plane_az   ?? 'NaN');
        const tAz  = parseFloat(flight.target_az  ?? 'NaN');
        if (isFinite(pAlt) && isFinite(tAlt) && isFinite(pAz) && isFinite(tAz)) {
            measAltD = pAlt - tAlt;
            let dAz = pAz - tAz;
            measAzD = ((dAz + 180) % 360) - 180;
        } else {
            measAltD = parseFloat(flight.alt_diff ?? 0);
            measAzD  = parseFloat(flight.az_diff  ?? 0);
        }
    }
    if (!isFinite(measAltD)) measAltD = 0;
    if (!isFinite(measAzD))  measAzD  = 0;

    const curSep = parseFloat(flight.current_angular_separation ?? flight.angular_separation ?? 999);

    // ETA in seconds (for edge-marker label)
    const etaSec = flight.transit_eta_seconds != null
        ? parseFloat(flight.transit_eta_seconds)
        : (flight.time != null ? parseFloat(flight.time) * 60 : null);

    const now  = Date.now();
    const tune = _loadRadarTuning();

    if (!_radarTracks.has(id)) {
        _radarTracks.set(id, {
            id, points: [], color, level, label: id,
            altFt: null, speedKmh: null, heading: null,
            altD: measAltD, azD: measAzD,
            vAltD: 0, vAzD: 0,
            lastUpdateT: now,
            lastSeenT: now,
            // Current separation and ETA for edge-marker vs diamond decision
            curSep, etaSec, etaBaseT: now,
        });
    }

    const track = _radarTracks.get(id);
    track.color    = color;
    track.level    = level;
    track.label    = id;
    track.altFt    = flight.altitude != null ? parseFloat(flight.altitude) : null;
    track.speedKmh = flight.speed    != null ? parseFloat(flight.speed)    : null;
    track.heading  = flight.heading  != null ? parseFloat(flight.heading)  : null;
    track.lastSeenT = now;
    track.curSep   = curSep;
    track.etaSec   = etaSec;
    track.etaBaseT = now;

    // α-β filter update (only for tracks within the radar field)
    if (curSep <= RADAR_MAX_SEP_DEG) {
        const dt = (now - track.lastUpdateT) / 1000;
        if (dt > 0.1 && dt < 120) {
            const predAlt = track.altD + track.vAltD * dt;
            const predAz  = track.azD  + track.vAzD  * dt;
            const resAlt = measAltD - predAlt;
            const resAz  = measAzD  - predAz;
            track.altD  = predAlt + tune.alpha * resAlt;
            track.azD   = predAz  + tune.alpha * resAz;
            track.vAltD = track.vAltD + (tune.beta / dt) * resAlt;
            track.vAzD  = track.vAzD  + (tune.beta / dt) * resAz;
        } else {
            track.altD = measAltD;
            track.azD  = measAzD;
        }
        track.lastUpdateT = now;

        // Append filtered position to trail (min 1s gap)
        const lastPt = track.points[track.points.length - 1];
        if (!lastPt || now - lastPt.t >= 1000) {
            track.points.push({ altD: track.altD, azD: track.azD, t: now });
            if (track.points.length > RADAR_HISTORY_MAX) track.points.shift();
        } else {
            lastPt.altD = track.altD;
            lastPt.azD  = track.azD;
        }
    } else {
        // Outside radar field — update bearing for edge marker; estimate velocity
        // from consecutive measurements so the bearing moves between server updates.
        const dt = (now - track.lastUpdateT) / 1000;
        if (dt > 0.1 && dt < 120) {
            track.vAltD = (measAltD - track.altD) / dt;
            track.vAzD  = (measAzD  - track.azD)  / dt;
        } else {
            track.vAltD = 0;
            track.vAzD  = 0;
        }
        track.altD = measAltD;
        track.azD  = measAzD;
        track.lastUpdateT = now;
        track.points = [];
    }

    if (level >= 1) _radarMarkTransitSeen();
};

// ── injectMapTransits: populate upcoming transits list from app.js data ───────
const _upcomingTransits = [];
window.injectMapTransits = function(flights, opts = {}) {
    _upcomingTransits.length = 0;
    const generatedAtMs = Number(opts.generatedAtMs) || Date.now();
    if (!Array.isArray(flights)) return;
    flights.forEach(f => {
        const level = parseInt(f.possibility_level ?? 0);
        if (level < 1) return;
        // transit_eta_seconds is not always set; fall back to time (minutes) × 60
        const etaSec = f.transit_eta_seconds != null
            ? parseFloat(f.transit_eta_seconds)
            : (f.time != null ? parseFloat(f.time) * 60 : null);
        _upcomingTransits.push({
            id:    String(f.id || f.name || '').trim().toUpperCase(),
            eta:   etaSec,
            ts:    generatedAtMs, // backend compute time for consistent ETA aging
            level,
            target: f.target || 'sun',
            track: f.direction != null ? Math.round(f.direction) : null,
            speed: f.speed != null ? Math.round(f.speed * 0.539957) : null,
        });
    });
    _upcomingTransits.sort((a,b) => {
        if (a.eta == null && b.eta == null) return 0;
        if (a.eta == null) return 1;
        if (b.eta == null) return -1;
        return a.eta - b.eta;
    });
    _renderUpcomingTransits();
};

function _renderUpcomingTransits() {
    const el = document.getElementById('upcomingTransitsList');
    if (!el) return;
    const now = Date.now();
    // Age-out the stored ETA by elapsed wall-clock time since it was received
    const live = _upcomingTransits
        .map(tr => {
            const ageS = (now - (tr.ts || now)) / 1000;
            return {...tr, liveEta: tr.eta != null ? tr.eta - ageS : null};
        })
        .filter(tr => tr.liveEta == null || tr.liveEta > -30); // keep for 30s past T-0
    if (!live.length) {
        el.innerHTML = '';
        return;
    }
    live.sort((a, b) => {
        if (a.liveEta == null && b.liveEta == null) return 0;
        if (a.liveEta == null) return 1;
        if (b.liveEta == null) return -1;
        return a.liveEta - b.liveEta;
    });
    el.innerHTML = live.slice(0, 5).map(tr => {
        const lvlCol = tr.level >= 3 ? '#00FF00' : tr.level === 2 ? '#FFD700' : '#A0A0A0';
        const lvlStr = tr.level >= 3 ? 'HIGH' : tr.level === 2 ? 'MED' : 'LOW';
        let etaStr;
        if (tr.liveEta == null) {
            etaStr = '\u2014';
        } else if (tr.liveEta <= 0) {
            etaStr = '<span style="color:#ff4444;font-weight:700">NOW</span>';
        } else {
            const m = Math.floor(tr.liveEta / 60), s = Math.floor(tr.liveEta % 60);
            etaStr = m > 0 ? `T\u2212${m}m${String(s).padStart(2,'0')}s` : `T\u2212${s}s`;
        }
        const trkStr = tr.track != null ? `${tr.track}\u00B0` : '\u2014';
        const spdStr = tr.speed != null ? `${tr.speed}kt` : '\u2014';
        return `<div style="display:grid;grid-template-columns:1fr auto auto auto auto;gap:6px;align-items:center;
                    padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.07);font-size:0.88em;">
            <span style="font-weight:600;letter-spacing:.04em;color:#D8DCE2;overflow:hidden;text-overflow:ellipsis;white-space:nowrap">${tr.id}</span>
            <span style="color:${lvlCol};font-weight:700;font-size:0.85em;min-width:32px;text-align:center">${lvlStr}</span>
            <span style="color:#B0B4BC;font-size:0.85em;min-width:28px;text-align:right;font-variant-numeric:tabular-nums">${trkStr}</span>
            <span style="color:#B0B4BC;font-size:0.85em;min-width:38px;text-align:right;font-variant-numeric:tabular-nums">${spdStr}</span>
            <span style="color:#C8CCD2;font-size:0.85em;min-width:52px;text-align:right;font-family:'SF Mono','Menlo','Consolas',monospace;font-variant-numeric:tabular-nums">${etaStr}</span>
        </div>`;
    }).join('');
}

// ── synthetic test-mode aircraft ─────────────────────────────────────────────
let _radarTestInterval = null;
let _radarTestT        = 0;      // elapsed ticks
const _RADAR_TEST_DT   = 2000;   // ms between ticks

// Three synthetic aircraft: each has a starting position in (altD, azD) space
// and a velocity (deg per tick = deg per _RADAR_TEST_DT seconds).
// Closest-approach distances (d_min) are set by the cross-product formula:
//   d_min = |altD*vAz - azD*vAlt| / hypot(vAlt, vAz)
const _TEST_AIRCRAFT = [
    {
        id: 'TEST-H', level: 3,   // HIGH  — transits the disc centre  (d_min ≈ 0°)
        altD:  9.0, azD: -4.5,
        vAlt: -0.50, vAz:  0.25, // ratio matches start → passes through (0,0) at t≈18
        altitude: 35000, speed: 860,
    },
    {
        id: 'TEST-M', level: 2,   // MEDIUM — passes ~2° from centre
        altD: -8.0, azD:  3.0,
        vAlt:  0.45, vAz: -0.05, // d_min = |(-8)(-0.05)-(3)(0.45)| / hypot ≈ 2.1°
        altitude: 39000, speed: 920,
    },
    {
        id: 'TEST-L', level: 1,   // LOW    — passes ~6° from centre
        altD:  7.0, azD: -9.0,
        vAlt: -0.05, vAz:  0.45, // d_min = |(7)(0.45)-(-9)(-0.05)| / hypot ≈ 5.9°
        altitude: 28000, speed: 590,
    },
];

function _startRadarTest(btn) {
    // Reset per-aircraft mutable state
    _TEST_AIRCRAFT.forEach(a => { a._altD = a.altD; a._azD = a.azD; });
    _radarMarkTransitSeen();
    if (btn) { btn.classList.add('is-active'); btn.textContent = 'Stop'; }

    function _tick() {
        window._radarLastUpdateTs = Date.now();   // keep sweep active during test
        _TEST_AIRCRAFT.forEach(a => {
            a._altD += a.vAlt;
            a._azD  += a.vAz;

            // Wrap back to starting position when blip exits the scope
            if (Math.hypot(a._altD, a._azD) > RADAR_MAX_SEP_DEG * 1.1) {
                a._altD = a.altD;
                a._azD  = a.azD;
                // Delete the track so pushInterceptPoint creates a fresh one
                // (avoids filter artifacts from the position discontinuity)
                _radarTracks.delete(a.id);
            }

            window.pushInterceptPoint({
                id:                  a.id,
                name:                a.id,
                possibility_level:   String(a.level),
                angular_separation:           String(Math.hypot(a._altD, a._azD).toFixed(3)),
                current_angular_separation:   String(Math.hypot(a._altD, a._azD).toFixed(3)),
                alt_diff:                     String(Math.abs(a._altD).toFixed(3)),
                az_diff:                      String(Math.abs(a._azD).toFixed(3)),
                current_signed_alt_diff:      String(a._altD.toFixed(3)),
                current_signed_az_diff:       String(a._azD.toFixed(3)),
                signed_alt_diff:              String(a._altD.toFixed(3)),
                signed_az_diff:               String(a._azD.toFixed(3)),
                altitude:                     String(a.altitude),
                speed:                        String(a.speed),
                heading:                      String(Math.atan2(a.vAz, a.vAlt) * 180 / Math.PI),
                is_possible_transit:          1,
            });
        });
        _radarMarkTransitSeen();
    }

    _tick();  // immediate first frame
    _radarTestInterval = setInterval(_tick, _RADAR_TEST_DT);
}

function _stopRadarTest() {
    if (!_radarTestInterval) return;
    clearInterval(_radarTestInterval);
    _radarTestInterval = null;
    // Remove synthetic tracks
    _TEST_AIRCRAFT.forEach(a => _radarTracks.delete(a.id));
    const btn = document.getElementById('radarModeTest');
    if (btn) { btn.classList.remove('is-active'); btn.textContent = 'Test'; }
}

// ── ensureTransitRadar ────────────────────────────────────────────────────────
function ensureTransitRadar() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;

    let card = document.getElementById('transitRadarCard');
    if (!card) {
        card = document.createElement('div');
        card.id = 'transitRadarCard';
        // Styling applied via #transitRadarCard in telescope.css (edge-to-edge dark CRT inset)

        // NASA console header row: engraved label plate + keycap mode buttons
        card.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;
                        padding:0 6px;margin-bottom:4px;">
                <span style="font-size:0.63em;font-weight:700;color:#9AA0A8;
                             letter-spacing:2.2px;text-transform:uppercase;
                             font-family:'SF Mono','Menlo',monospace;">
                    Transit Radar
                </span>
                <div style="display:flex;gap:2px;">
                    <button id="radarModeDefault" title="Default mode: current position only"
                        class="btn btn-compact btn-toggle"
                        style="min-height:18px;padding:2px 7px;font-size:0.62em;">Default</button>
                    <button id="radarModeEnhanced" title="Enhanced mode: projects trajectory forward ${RADAR_PREDICT_HORIZON_S}s"
                        class="btn btn-compact btn-toggle"
                        style="min-height:18px;padding:2px 7px;font-size:0.62em;">Enhanced</button>
                    <button id="radarModeTest" title="Inject synthetic aircraft to preview blip rendering"
                        class="btn btn-compact btn-toggle"
                        style="min-height:18px;padding:2px 7px;font-size:0.62em;">Test</button>
                </div>
            </div>
            <canvas id="radarCanvas"
                    style="display:block;width:100%;aspect-ratio:1;
                           border-top:1px solid rgba(0,255,80,0.06);"></canvas>
        `;

        if (detectPanel.firstChild) {
            detectPanel.insertBefore(card, detectPanel.firstChild);
        } else {
            detectPanel.appendChild(card);
        }
    }

    // bind canvas
    _radarCanvas = card.querySelector('#radarCanvas');
    _radarCtx    = _radarCanvas.getContext('2d');
    _resizeRadarCanvas();
    if (window.ResizeObserver) {
        new ResizeObserver(() => _resizeRadarCanvas()).observe(card);
    }

    // mode buttons — use .is-active keycap (locked-down = selected mode)
    const btnDef  = card.querySelector('#radarModeDefault');
    const btnEnh  = card.querySelector('#radarModeEnhanced');
    const btnTest = card.querySelector('#radarModeTest');
    function _applyMode(m) {
        _radarMode = m;
        btnDef.classList.toggle('is-active', m === 'default');
        btnEnh.classList.toggle('is-active', m === 'enhanced');
    }
    btnDef.onclick = () => { _stopRadarTest(); _applyMode('default'); };
    btnEnh.onclick = () => { _stopRadarTest(); _applyMode('enhanced'); };
    btnTest.onclick = () => {
        if (_radarTestInterval) { _stopRadarTest(); }
        else { _startRadarTest(btnTest); }
    };
    _applyMode(_radarMode);

    // mouse events
    _radarCanvas.addEventListener('mousemove', e => {
        const rect = _radarCanvas.getBoundingClientRect();
        const scaleX = _radarCanvas.width  / rect.width;
        const scaleY = _radarCanvas.height / rect.height;
        const mx = (e.clientX - rect.left) * scaleX;
        const my = (e.clientY - rect.top ) * scaleY;
        const hit = _radarPick(mx, my);
        _radarHoveredId = hit;
        if (hit && hit !== _radarPinnedId) _radarShowTooltip(hit, e.clientX - rect.left, e.clientY - rect.top);
        else if (!hit && !_radarPinnedId) _radarHideTooltip();
        _radarCanvas.style.cursor = hit ? 'pointer' : 'default';
    });
    _radarCanvas.addEventListener('click', e => {
        const rect = _radarCanvas.getBoundingClientRect();
        const scaleX = _radarCanvas.width  / rect.width;
        const scaleY = _radarCanvas.height / rect.height;
        const mx = (e.clientX - rect.left) * scaleX;
        const my = (e.clientY - rect.top ) * scaleY;
        const hit = _radarPick(mx, my);
        if (hit) {
            _radarPinnedId = _radarPinnedId === hit ? null : hit;
            if (_radarPinnedId) _radarShowTooltip(hit, e.clientX - rect.left, e.clientY - rect.top);
            else _radarHideTooltip();
        } else {
            _radarPinnedId = null;
            _radarHideTooltip();
        }
    });
    _radarCanvas.addEventListener('mouseleave', () => {
        _radarHoveredId = null;
        if (!_radarPinnedId) _radarHideTooltip();
    });

    // start animation loop (cancel any previous)
    if (_radarAnimFrame) cancelAnimationFrame(_radarAnimFrame);
    _radarAnimFrame = requestAnimationFrame(_radarDrawFrame);

    _renderUpcomingTransits();
}


// ============================================================================
// ALPACA Telemetry Panel
// ============================================================================

let _alpacaConnected = false;
let _alpacaPollTimer = null;
let _alpacaTracking = false;

async function pollAlpacaTelemetry() {
    try {
        const data = await apiCall('/telescope/alpaca/telemetry', 'GET', null, { silent: true });
        if (!data || !data.connected) {
            _alpacaConnected = false;
            _updateAlpacaPanel(null);
            return;
        }
        _alpacaConnected = true;
        _updateAlpacaPanel(data);
    } catch (e) {
        _alpacaConnected = false;
        _updateAlpacaPanel(null);
    }
}

function _updateAlpacaPanel(data) {
    const panel = document.getElementById('alpacaTelemetryPanel');
    if (!panel) return;

    panel.style.display = '';

    const _setAlpacaEndpoint = (payload) => {
        const endpoint = document.getElementById('alpacaEndpoint');
        if (!endpoint) return;
        const hostRaw =
            (payload && payload.host !== undefined ? payload.host : null) ??
            (_lastConnectedStatus && _lastConnectedStatus.host);
        const host = typeof hostRaw === 'string' ? hostRaw.trim() : '';
        const n = payload && payload.port !== undefined && payload.port !== null
            ? Number(payload.port)
            : NaN;
        const port = Number.isFinite(n) ? Math.round(n) : null;
        if (host && port != null) endpoint.textContent = `${host}:${port}`;
        else if (host) endpoint.textContent = host;
        else endpoint.textContent = '—';
    };

    if (!data || !data.connected) {
        // Show panel in disconnected/placeholder state — coords reset to dashes
        ['alpacaRA','alpacaDec','alpacaAlt','alpacaAz','alpacaLST','alpacaGMT','alpacaFocusPos','alpacaCameraGain'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.textContent = '—';
        });
        const badge = document.getElementById('alpacaStatusBadge');
        if (badge) badge.className = 'alpaca-status-badge';
        const label = document.getElementById('alpacaStatusLabel');
        if (label) label.textContent = 'DISC';
        const dot = document.getElementById('alpacaStatusDot');
        if (dot) dot.style.background = '#555';
        _setAlpacaEndpoint(data);
        const name = document.getElementById('alpacaDeviceName');
        if (name) name.textContent = '';
        const drvr = document.getElementById('alpacaDriverInfo');
        if (drvr) drvr.textContent = '';
        ['alpacaChipTrk','alpacaChipSlw','alpacaChipPrk'].forEach(id => {
            const el = document.getElementById(id);
            if (el) el.className = 'alpaca-state-chip';
        });
        return;
    }

    const pos = data.position || {};
    const state = data.state || {};
    const info = data.device_info || {};
    const focuser = data.focuser || {};
    const camera = data.camera || {};

    const _toNum = (v) => {
        if (v === undefined || v === null) return null;
        if (typeof v === 'string' && v.trim() === '') return null;
        const n = Number(v);
        return Number.isFinite(n) ? n : null;
    };
    const fmt = (v, d) => {
        const n = _toNum(v);
        return n == null ? '—' : n.toFixed(d);
    };

    _setAlpacaEndpoint(data);

    // Coordinate values
    const ra = document.getElementById('alpacaRA');
    const dec = document.getElementById('alpacaDec');
    const alt = document.getElementById('alpacaAlt');
    const az = document.getElementById('alpacaAz');
    if (ra) ra.textContent = fmt(pos.ra, 4);
    if (dec) dec.textContent = fmt(pos.dec, 2);
    if (alt) alt.textContent = fmt(pos.alt, 2);
    if (az) az.textContent = fmt(pos.az, 2);

    // Local time — from system clock
    const lst = document.getElementById('alpacaLST');
    if (lst) {
        const now = new Date();
        const h = now.getHours();
        const m = now.getMinutes();
        lst.textContent = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
    }

    // GMT/UTC time — use UTC methods to avoid timezone/DST issues
    const gmt = document.getElementById('alpacaGMT');
    if (gmt) {
        const now = new Date();
        const h = now.getUTCHours();
        const m = now.getUTCMinutes();
        gmt.textContent = String(h).padStart(2,'0') + ':' + String(m).padStart(2,'0');
    }

    const focPos = document.getElementById('alpacaFocusPos');
    if (focPos) {
        const n = _toNum(focuser.position);
        focPos.textContent = n == null ? '—' : String(Math.round(n));
    }
    const camGain = document.getElementById('alpacaCameraGain');
    if (camGain) {
        const n = _toNum(camera.gain);
        camGain.textContent = n == null ? '—' : String(Math.round(n));
    }

    // Also update the GoTo pointing display with ALPACA data
    const scopeAlt = document.getElementById('scopeAlt');
    const scopeAz = document.getElementById('scopeAz');
    if (scopeAlt && pos.alt != null) scopeAlt.textContent = fmt(pos.alt, 1);
    if (scopeAz && pos.az != null) scopeAz.textContent = fmt(pos.az, 1);

    // Tracking state
    _alpacaTracking = state.tracking || false;

    // State chips — TRK / SLW / PRK
    const chipTrk = document.getElementById('alpacaChipTrk');
    const chipSlw = document.getElementById('alpacaChipSlw');
    const chipPrk = document.getElementById('alpacaChipPrk');
    if (chipTrk) chipTrk.className = 'alpaca-state-chip' + (state.tracking ? ' active' : '');
    if (chipSlw) chipSlw.className = 'alpaca-state-chip' + (state.slewing ? ' warn' : '');
    if (chipPrk) chipPrk.className = 'alpaca-state-chip' + (state.parked ? ' active' : '');

    // Device name
    const nameEl = document.getElementById('alpacaDeviceName');
    if (nameEl && info.name) nameEl.textContent = info.name;

    // Driver info line
    const driverEl = document.getElementById('alpacaDriverInfo');
    if (driverEl && info.driverinfo) {
        driverEl.textContent = info.driverinfo + (info.driverversion ? ' v' + info.driverversion : '');
    }


    // Tracking button — teal = tracking on, white = tracking off
    const btn = document.getElementById('alpacaTrackingBtn');
    if (btn) {
        btn.classList.toggle('is-active', !!_alpacaTracking);
    }
}

function startAlpacaPolling() {
    if (_alpacaPollTimer) return;
    pollAlpacaTelemetry();
    _alpacaPollTimer = setInterval(pollAlpacaTelemetry, 2500);
}

function stopAlpacaPolling() {
    if (_alpacaPollTimer) {
        clearInterval(_alpacaPollTimer);
        _alpacaPollTimer = null;
    }
    _alpacaConnected = false;
    _updateAlpacaPanel(null);
}

async function alpacaToggleTracking() {
    const newState = !_alpacaTracking;
    await apiCall('/telescope/alpaca/tracking', 'POST', { enabled: newState });
    // .is-active.btn-mode = lit teal indicator when tracking is on
    const btn = document.getElementById('alpacaTrackingBtn');
    if (btn) btn.classList.toggle('is-active', newState);
    showStatus(newState ? 'Tracking enabled' : 'Tracking disabled', 'info', 2000);
}

async function alpacaAbortSlew() {
    await apiCall('/telescope/alpaca/abort', 'POST', {});
    showStatus('Slew aborted', 'warning', 2000);
}

// Start/stop ALPACA polling with telescope connection
const _origUpdateStatus = updateStatus;
updateStatus = async function() {
    await _origUpdateStatus();
    // Start/stop ALPACA polling based on connection state
    if (isConnected && !_alpacaPollTimer) {
        startAlpacaPolling();
    } else if (!isConnected && _alpacaPollTimer) {
        stopAlpacaPolling();
    }
};

console.log('[Telescope] Module loaded (ALPACA support enabled)');

// ============================================================================
// ARMED STATUS BANNER + BACKGROUND TRANSIT CHECK
// Prevents silent misses when only the telescope page is open (no map tab).
//
// Two complementary mechanisms:
// 1. _pollArmedStatus() — every 10 s: checks /telescope/armed and shows a
//    loud red banner if scope is in solar/lunar mode but detector is OFF.
// 2. _bgTransitCheck()  — every 90 s: POSTs /telescope/transit/check so the
//    server-side TransitRecorder schedules recordings for any HIGH transits
//    even when the map tab is closed.
// ============================================================================

let _armedPollTimer   = null;
let _transitCheckTimer = null;
let _armedBannerShown = false;

function _ensureArmedBanner() {
    if (document.getElementById('armed-warning-banner')) return;
    const banner = document.createElement('div');
    banner.id = 'armed-warning-banner';
    banner.style.cssText = [
        'display:none',
        'position:fixed',
        'top:0',
        'left:0',
        'right:0',
        'z-index:9999',
        'background:#c0392b',
        'color:#fff',
        'font-weight:bold',
        'font-size:15px',
        'padding:10px 16px',
        'text-align:center',
        'cursor:pointer',
        'box-shadow:0 3px 8px rgba(0,0,0,0.5)',
        'animation:armed-pulse 1.5s ease-in-out infinite',
    ].join(';');
    banner.innerHTML = '⚠️ TRANSIT DETECTION IS OFF — aircraft crossing the Sun/Moon will NOT be captured. <u>Click to start detection.</u>';
    banner.addEventListener('click', async () => {
        try {
            const res = await fetch('/telescope/detect/start', {method:'POST', headers:{'Content-Type':'application/json'}, body:'{}'});
            if (res.ok) {
                showStatus('Transit detection started ✓', 'success', 3000);
                banner.style.display = 'none';
                _armedBannerShown = false;
            } else {
                showStatus('Could not start detection — check console', 'error', 4000);
            }
        } catch(e) {
            showStatus('Could not start detection: ' + e.message, 'error', 4000);
        }
    });
    // Inject keyframe animation if not already present
    if (!document.getElementById('armed-banner-style')) {
        const style = document.createElement('style');
        style.id = 'armed-banner-style';
        style.textContent = '@keyframes armed-pulse{0%,100%{opacity:1}50%{opacity:.7}}';
        document.head.appendChild(style);
    }
    document.body.prepend(banner);
}

async function _pollArmedStatus() {
    try {
        _ensureArmedBanner();
        const res = await fetch('/telescope/armed', {cache:'no-store'});
        if (!res.ok) return;
        const data = await res.json();
        const banner = document.getElementById('armed-warning-banner');
        if (!banner) return;
        if (data.warning) {
            banner.style.display = 'block';
            _armedBannerShown = true;
        } else {
            banner.style.display = 'none';
            _armedBannerShown = false;
        }
    } catch (_) { /* non-fatal */ }
}

async function _bgTransitCheck() {
    try {
        const res = await fetch('/telescope/transit/check', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: '{}',
            cache: 'no-store',
        });
        if (!res.ok) return;
        const data = await res.json();
        if (data.high_transits && data.high_transits.length > 0) {
            const first = data.high_transits[0];
            const eta   = first.eta_min < 1
                ? `${Math.round(first.eta_min * 60)}s`
                : `${first.eta_min.toFixed(1)} min`;
            showStatus(
                `🚨 HIGH transit: ${first.id || 'unknown'} — ETA ${eta} (sep ${first.sep_deg}°)`,
                'warning',
                8000
            );
            console.log('[TransitCheck] HIGH transits:', data.high_transits);
        }
    } catch (_) { /* non-fatal */ }
}

function _startArmedPolling() {
    if (_armedPollTimer) return;
    _pollArmedStatus();   // immediate first check
    _armedPollTimer = setInterval(_pollArmedStatus, 10_000);     // every 10 s
    _bgTransitCheck();    // immediate first check
    _transitCheckTimer = setInterval(_bgTransitCheck, 90_000);   // every 90 s
}

function _stopArmedPolling() {
    if (_armedPollTimer)   { clearInterval(_armedPollTimer);    _armedPollTimer   = null; }
    if (_transitCheckTimer){ clearInterval(_transitCheckTimer); _transitCheckTimer = null; }
    const banner = document.getElementById('armed-warning-banner');
    if (banner) banner.style.display = 'none';
}

// Hook into the existing updateStatus wrapper so polling tracks connection state
const _origUpdateStatus2 = updateStatus;
updateStatus = async function() {
    await _origUpdateStatus2();
    if (isConnected && !_armedPollTimer) {
        _startArmedPolling();
    } else if (!isConnected && _armedPollTimer) {
        _stopArmedPolling();
    }
};
