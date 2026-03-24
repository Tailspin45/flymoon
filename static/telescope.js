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
function getFavorites() {
    try { return new Set(JSON.parse(localStorage.getItem('flymoon_favorites') || '[]')); }
    catch { return new Set(); }
}
function saveFavorites(favs) {
    localStorage.setItem('flymoon_favorites', JSON.stringify([...favs]));
}
function toggleFavorite(path, event) {
    if (event) event.stopPropagation();
    const favs = getFavorites();
    if (favs.has(path)) favs.delete(path); else favs.add(path);
    saveFavorites(favs);
    // Update all heart buttons for this path
    document.querySelectorAll(`[data-fav-path="${CSS.escape(path)}"]`).forEach(btn => {
        btn.textContent = favs.has(path) ? '❤️' : '🤍';
        btn.title = favs.has(path) ? 'Unfavorite' : 'Favorite';
    });
    // Sync delete button states everywhere
    _updateDeleteBtnState(path, favs.has(path));
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
    const viewerDelBtn = document.getElementById('viewerDeleteBtn');
    if (viewerDelBtn) {
        viewerDelBtn.disabled = isFav;
        viewerDelBtn.title = isFav ? 'Remove favorite first' : 'Delete (⌘/Ctrl+click to skip confirm)';
    }
    // Viewer fav button text
    const viewerFavBtn = document.getElementById('viewerFavBtn');
    if (viewerFavBtn) {
        viewerFavBtn.textContent = isFav ? '❤️' : '🤍';
    }
}
let zoomStep = 0.1;
let _previewLastError = 0; // timestamp of last preview onerror (ms)
const _PREVIEW_BACKOFF_MS = 5000; // 5s between retry attempts after stream failure
let _previewCheckTimer = null;
let _lastPreviewRefreshMs = 0;
const _PREVIEW_REFRESH_INTERVAL_MS = Infinity; // never restart a healthy stream — onerror handles failures
const _DEBUG_INGEST_URL = 'http://127.0.0.1:7352/ingest/42acc25b-9174-476d-8462-1b85f40db694';
const _DEBUG_SESSION_ID = '616e1a';

function _agentDebugLog(runId, hypothesisId, location, message, data) {
    fetch(_DEBUG_INGEST_URL,{method:'POST',headers:{'Content-Type':'application/json','X-Debug-Session-Id':'616e1a'},body:JSON.stringify({sessionId:_DEBUG_SESSION_ID,runId,hypothesisId,location,message,data,timestamp:Date.now()})}).catch(()=>{});
}
let isSimulating = false;
let simulationVideo = null;
let disconnectedPollCount = 0; // consecutive disconnected polls before stopping preview
let simulationFiles = []; // Track temporary simulation files
let filmstripFiles = []; // current files rendered in the horizontal filmstrip
let _lastDiscoveredSeestarIp = null; // last discovered scope IP

const filmstripSelection = {
    selected: new Set(),   // set of file paths selected in filmstrip
    lastClicked: null,     // index in filmstripFiles for shift-range selection
};

// Detection state
let isDetecting = false;
let detectionPollInterval = null;
let detectionStats = { fps: 0, detections: 0, elapsed_seconds: 0 };

// Eclipse state
let eclipseData = null;         // populated from /telescope/status
let eclipseAlertLevel = null;   // 'outlook'|'watch'|'warning'|'active'|'cleared'|null
let _eclipseRecordingScheduled = false; // prevents duplicate setTimeout during warning phase
let eclipseBannerDismissed = false; // per-session dismiss flag
let currentViewingMode = null;  // 'sun'|'moon'|null — last known scope viewing mode
let _mismatchDismissedFor = null; // which opposite target the user dismissed

window.initTelescope = function() {
    console.log('[Telescope] Initializing interface');
    destroyTelescope(); // clear any existing intervals
    ensureHarnessUI();
    ensureTuningUI();
    ensureTransitRadar();

    // Status polling (always poll while panel is open)
    statusPollInterval = setInterval(updateStatus, 2000);

    // Auto-connect: check current state, then connect silently if not already connected.
    // After connection is confirmed, auto-start detection (default on).
    updateStatus().then(async () => {
        if (!isConnected) {
            await connect();
        }
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
        document.getElementById('autoCaptureToggle').checked = autoCapture === 'true';
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

    // Initialize Control Panel (GoTo, named locations)
    initControlPanel();
};

// ============================================================================
// CONNECTION MANAGEMENT
// ============================================================================

async function connect() {
    console.log('[Telescope] Connecting...');
    showStatus('Connecting to telescope...', 'info');
    
    const result = await apiCall('/telescope/connect', 'POST');
    if (result && result.success) {
        isConnected = true;
        showStatus('Connected successfully!', 'success', 5000);

        // Start status polling
        if (statusPollInterval) clearInterval(statusPollInterval);
        statusPollInterval = setInterval(updateStatus, 2000); // Every 2s

        // Start telemetry polling (RA/Dec/Alt/Az strip)
        startTelemetryPolling();

        updateConnectionUI();
        updateStatus();
        // Clear any preview backoff and start stream
        _previewLastError = 0;
        stopPreview();
        setTimeout(startPreview, 2000); // give RTSP a moment to spin up
    }
}

async function findSeestar() {
    const btn = document.getElementById('findSeestarBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Scanning…'; }
    showStatus('Scanning network for Seestar…', 'info', 0);
    try {
        const resp = await fetch('/telescope/discover');
        const data = await resp.json();
        if (btn) { btn.disabled = false; btn.textContent = '🔍 Find'; }
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
        if (btn) { btn.disabled = false; btn.textContent = '🔍 Find'; }
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

        // Stop telemetry strip
        stopTelemetryPolling();

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
        'startTimelapseBtn'
    ];
    
    buttons.forEach(id => {
        const btn = document.getElementById(id);
        if (btn && !btn.classList.contains('force-enabled')) {
            btn.disabled = !isConnected;
        }
    });
}

async function updateStatus() {
    if (isSimulating) return; // sim owns connection state — don't let real status poll overwrite it
    const result = await apiCall('/telescope/status', 'GET');
    if (result) {
        isConnected = result.connected || false;
        currentViewingMode = result.viewing_mode || null;
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
        checkTargetMismatch();
        
        // Auto-start preview if connected; stop stale stream if disconnected
        const justReconnected = isConnected && !_prevConnected;
        const justDisconnected = !isConnected && _prevConnected;
        if (justReconnected) {
            console.log('[Scope] Reconnected — mode:', result.viewing_mode);
            _previewLastError = 0;
            _lastConnectedStatus = null;
        }
        // Always ensure telemetry polling runs while connected (covers auto-connect,
        // missed justReconnected edge cases, and page load order).
        if (isConnected) startTelemetryPolling();
        if (justDisconnected) {
            console.warn('[Scope] Disconnected — prior connected state:', JSON.stringify(_lastConnectedStatus || {}));
            if (result.error) console.warn('[Scope] Server error:', result.error);
            // Immediately clear transit cards — nothing to capture with
            upcomingTransits = [];
            updateTransitList();
            stopTelemetryPolling();
        }
        // Cache last connected response for diagnostics
        if (isConnected) {
            _lastConnectedStatus = { viewing_mode: result.viewing_mode, recording: result.recording, host: result.host };
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
            sunBadge.textContent = 'Below Horizon';
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
            moonBadge.textContent = 'Below Horizon';
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
        // Force-restart the preview stream (mode change kills old RTSP)
        stopPreview();
        _previewLastError = 0;
        setTimeout(startPreview, 3000);

        // Show solar filter warning
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
        // Force-restart the preview stream (mode change kills old RTSP)
        stopPreview();
        _previewLastError = 0;
        setTimeout(startPreview, 3000);

        // Show lunar filter reminder
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
        
        // Refresh file list after a short delay
        setTimeout(refreshFiles, 2000);
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
        // Refresh file list after a short delay
        setTimeout(refreshFiles, 2000);
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
        // Refresh file list after assembly has time to complete
        setTimeout(refreshFiles, 5000);
    }
}

async function previewTimelapse() {
    const btn = document.getElementById('previewTimelapseBtn');
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Building...'; }
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
    const runId = `preview_${Date.now()}`;
    
    const previewImage = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot = document.getElementById('previewStatusDot');
    const previewStatusText = document.getElementById('previewStatusText');
    const previewTitleIcon = document.getElementById('previewTitleIcon');
    
    if (!previewImage) {
        console.error('[Telescope] Preview image element not found');
        // #region agent log
        _agentDebugLog(runId,'H6','static/telescope.js:startPreview:noElement','previewImage missing',{});
        // #endregion
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
    
    // Confirm stream is live by polling for a successful HEAD request to the MJPEG endpoint
    // (MJPEG <img> streams don't fire onload, so we verify separately)
    const checkStream = async () => {
        try {
            const r = await fetch(streamUrl, { method: 'HEAD', signal: AbortSignal.timeout(4000) });
            if (r.ok) {
                if (previewStatusDot) previewStatusDot.className = 'status-dot connected';
                if (previewStatusText) previewStatusText.textContent = 'Live Stream Active';
                if (previewTitleIcon) previewTitleIcon.textContent = '🟢';
                currentZoom = 2.0;
                applyZoom();
            } else {
                // Server responded but not OK — stream not ready
                if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
                if (previewStatusText) previewStatusText.textContent = 'Stream unavailable';
                if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
            }
        } catch (_) {
            // Fetch failed — retry once more after 3s before giving up
            _previewCheckTimer = setTimeout(async () => {
                try {
                    const r2 = await fetch(streamUrl, { method: 'HEAD', signal: AbortSignal.timeout(4000) });
                    if (r2.ok) {
                        if (previewStatusDot) previewStatusDot.className = 'status-dot connected';
                        if (previewStatusText) previewStatusText.textContent = 'Live Stream Active';
                        if (previewTitleIcon) previewTitleIcon.textContent = '🟢';
                        currentZoom = 2.0;
                        applyZoom();
                    } else {
                        if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
                        if (previewStatusText) previewStatusText.textContent = 'Stream unavailable';
                        if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
                    }
                } catch (_2) {
                    if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
                    if (previewStatusText) previewStatusText.textContent = 'Stream unavailable';
                    if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
                }
            }, 3000);
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
        // #region agent log
        _agentDebugLog(runId,'H6','static/telescope.js:startPreview:onerror','preview image error',{streamUrl,isConnected});
        // #endregion
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

function updateSlider() {
    const slider = document.getElementById('zoomSlider');
    const percent = document.getElementById('zoomPercent');
    if (slider) slider.value = Math.round(currentZoom * 100);
    if (percent) percent.textContent = Math.round(currentZoom * 100) + '%';
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
// FILE MANAGEMENT
// ============================================================================

async function refreshFiles() {
    console.log('[Telescope] Refreshing file list');
    
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
    
    // Enable/disable controls
    const refreshBtn = document.getElementById('refreshFilesBtn');
    const expandBtn = document.getElementById('expandFilesBtn');
    if (refreshBtn) refreshBtn.disabled = false;
    if (expandBtn) expandBtn.disabled = files.length === 0;
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

async function apiCall(endpoint, method = 'GET', body = null) {
    const runId = `api_${Date.now()}`;
    if (endpoint === '/telescope/goto') {
        // #region agent log
        _agentDebugLog(runId,'H9','static/telescope.js:apiCall:send','apiCall send /telescope/goto',{method,hasBody:!!body,body});
        // #endregion
    }
    try {
        const options = {
            method: method,
            headers: {
                'Content-Type': 'application/json'
            }
        };

        if (body) {
            options.body = JSON.stringify(body);
        }

        const response = await fetch(endpoint, options);
        const contentType = response.headers.get('content-type') || '';
        if (!contentType.includes('application/json')) {
            throw new Error(`HTTP ${response.status}: unexpected response`);
        }
        const data = await response.json();

        if (!response.ok) {
            if (endpoint === '/telescope/goto') {
                // #region agent log
                _agentDebugLog(runId,'H9_H10','static/telescope.js:apiCall:response_error','apiCall /telescope/goto non-ok response',{status:response.status,data});
                // #endregion
            }
            throw new Error(_formatApiError(data, response.status));
        }
        if (endpoint === '/telescope/goto') {
            // #region agent log
            _agentDebugLog(runId,'H9_H10','static/telescope.js:apiCall:response_ok','apiCall /telescope/goto ok response',{status:response.status,data});
            // #endregion
        }

        return data;

    } catch (error) {
        if (endpoint === '/telescope/goto') {
            // #region agent log
            _agentDebugLog(runId,'H9_H10','static/telescope.js:apiCall:catch','apiCall /telescope/goto catch',{message:error?.message || String(error)});
            // #endregion
        }
        console.error(`[Telescope] API call failed: ${endpoint}`, error);
        showStatus(`Error: ${error.message}`, 'error');
        return null;
    }
}

// ============================================================================
// CONTROL PANEL — GoTo, Park, Autofocus, Camera Settings, Named Locations
// ============================================================================

let _telemetryInterval = null;
const _TELEMETRY_POLL_MS = 2000;
let _telemetryPollInFlight = false;

function initControlPanel() {
    loadSavedLocations();
}

// -- Telemetry polling --

function startTelemetryPolling() {
    const strip = document.getElementById('telemetryStrip');
    if (!strip) return;
    if (_telemetryInterval) return;
    _telemetryInterval = setInterval(_pollTelemetry, _TELEMETRY_POLL_MS);
    _pollTelemetry(); // immediate first fetch
    strip.style.display = '';
}

function stopTelemetryPolling() {
    if (_telemetryInterval) {
        clearInterval(_telemetryInterval);
        _telemetryInterval = null;
    }
    const strip = document.getElementById('telemetryStrip');
    if (strip) strip.style.display = 'none';
}

function _setText(id, val) {
    const el = document.getElementById(id);
    if (el) el.textContent = val != null ? String(val) : '—';
}

async function _pollTelemetry() {
    if (_telemetryPollInFlight) return;
    _telemetryPollInFlight = true;
    try {
        const controller = new AbortController();
        const timer = setTimeout(() => controller.abort(), 6000);
        const res = await fetch(`/telescope/telemetry?t=${Date.now()}`, {
            signal: controller.signal,
            cache: 'no-store',
        });
        clearTimeout(timer);
        if (!res.ok) {
            if (res.status === 503) {
                console.warn('[Telemetry] Scope not connected (503) — strip may stay empty until connect');
            } else {
                console.warn('[Telemetry] HTTP', res.status, res.statusText);
            }
            return;
        }
        const d = await res.json();
        if (d.error) {
            console.warn('[Telemetry]', d.error);
            return;
        }
        const fmt = (v, dp=2) => v != null ? (+v).toFixed(dp) : '—';
        const runId = `telemetry_ui_${Date.now()}`;

        // Pointing
        _setText('telmRA',  fmt(d.ra,  4) + (d.ra  != null ? 'h' : ''));
        _setText('telmDec', fmt(d.dec, 3) + (d.dec != null ? '°' : ''));
        _setText('telmAlt', fmt(d.alt, 1) + (d.alt != null ? '°' : ''));
        _setText('telmAz',  fmt(d.az,  1) + (d.az  != null ? '°' : ''));

        // Bidirectional Alt/Az — update GoTo inputs unless user has edited them
        const altIn = document.getElementById('gotoAlt');
        const azIn  = document.getElementById('gotoAz');
        // #region agent log
        _agentDebugLog(runId,'H7_H8','static/telescope.js:_pollTelemetry:inputs','telemetry before goto overwrite check',{alt:d.alt,az:d.az,altUserEdited:!!(altIn&&altIn.dataset.userEdited),azUserEdited:!!(azIn&&azIn.dataset.userEdited),altActive:document.activeElement===altIn,azActive:document.activeElement===azIn,shownAlt:document.getElementById('telmAlt')?.textContent,shownAz:document.getElementById('telmAz')?.textContent});
        // #endregion
        if (altIn && !altIn.dataset.userEdited && document.activeElement !== altIn && d.alt != null)
            altIn.value = (+d.alt).toFixed(1);
        if (azIn && !azIn.dataset.userEdited && document.activeElement !== azIn && d.az != null)
            azIn.value = (+d.az).toFixed(1);

        // View
        _setText('telmViewMode',   d.view_mode);
        _setText('telmViewTarget', d.view_target);
        _setText('telmViewStage',  d.view_stage);
        _setText('telmRtsp',       d.rtsp_state);
        _setText('telmLpFilter',   d.lp_filter != null ? (d.lp_filter ? 'On' : 'Off') : null);
        _setText('telmAutofocus',  d.autofocus_state);
        _setText('telmManualExp',  d.manual_exp != null ? (d.manual_exp ? 'Manual' : 'Auto') : null);

        // System
        const batt = d.battery_capacity;
        const battEl = document.getElementById('telmBatt');
        if (battEl) {
            battEl.textContent = batt != null ? batt + '%' : '—';
            battEl.className = 'telm-val' + (batt != null ? (batt > 50 ? ' telm-batt-green' : batt > 20 ? ' telm-batt-yellow' : ' telm-batt-red') : '');
        }
        _setText('telmCharger',  d.charger_status);
        _setText('telmCpuTemp',  d.cpu_temp != null ? (+d.cpu_temp).toFixed(1) + '°C' : null);
        _setText('telmBattTemp', d.battery_temp != null ? d.battery_temp + '°C' : null);
        // Overtemp warning
        const otRow = document.getElementById('telmOvertempRow');
        if (otRow) otRow.style.display = (d.is_overtemp || d.battery_overtemp) ? '' : 'none';

        // Focuser
        _setText('telmFocuserStep',  d.focuser_step);
        _setText('telmFocuserState', d.focuser_state);
        _setText('telmFocuserMax',   d.focuser_max_step);
        // Also update Manual Focus panel position
        const focusPosEl = document.getElementById('focusPos');
        if (focusPosEl && d.focus_pos != null) focusPosEl.textContent = d.focus_pos;

        // Mount
        _setText('telmTracking', d.mount_tracking != null ? (d.mount_tracking ? 'Yes' : 'No') : null);
        _setText('telmArm',      d.mount_closed != null ? (d.mount_closed ? 'Closed' : 'Open') : null);
        _setText('telmMoveType', d.mount_move_type);
        _setText('telmCompass',  d.compass_direction != null ? (+d.compass_direction).toFixed(0) + '°' : null);
        _setText('telmTilt',     d.tilt_angle != null ? (+d.tilt_angle).toFixed(1) + '°' : null);

        // Storage / WiFi
        _setText('telmStorageFree', d.storage_free_mb != null ? (d.storage_free_mb > 1024 ? (d.storage_free_mb / 1024).toFixed(1) + ' GB' : d.storage_free_mb + ' MB') : null);
        _setText('telmStorageUsed', d.storage_used_pct != null ? d.storage_used_pct + '%' : null);
        _setText('telmWifiSsid',    d.wifi_ssid);
        _setText('telmWifiSignal',  d.wifi_signal != null ? d.wifi_signal + ' dBm' : null);

        // Device
        _setText('telmFirmware', d.firmware_ver != null ? 'v' + d.firmware_ver : null);
        _setText('telmHeater',   d.heater_enable != null ? (d.heater_enable ? 'On' : 'Off') : null);

        // Update Auto Exp button style based on telemetry
        try {
            const aeBtn = document.getElementById('autoExpBtn');
            if (aeBtn && d.manual_exp != null) {
                aeBtn.className = d.manual_exp ? 'btn btn-warning btn-compact' : 'btn btn-secondary btn-compact';
            }
        } catch (_ae) { /* non-fatal */ }

    } catch (_) { /* non-fatal */ }
    finally { _telemetryPollInFlight = false; }
}

// -- GoTo mode radio toggle --

function gotoModeChanged() {
    const isAltaz = document.getElementById('gotoModeAltaz').checked;
    document.getElementById('gotoAltazInputs').style.display = isAltaz ? '' : 'none';
    document.getElementById('gotoRadecInputs').style.display = isAltaz ? 'none' : '';
}

// -- GoTo execute --

// -- Manual Slew (Joystick) --

let _nudgeInterval = null;

function nudgeStart(angle) {
    nudgeStop(); // clear any existing
    const speed = document.querySelector('input[name="nudgeSpeed"]:checked')?.value === 'fast' ? 80 : 20;
    // Send immediately, then repeat every 2s for held button
    const send = () => apiCall('/telescope/nudge', 'POST', { speed, angle, dur_sec: 2 });
    send();
    _nudgeInterval = setInterval(send, 2000);
}

function nudgeStop() {
    if (_nudgeInterval) {
        clearInterval(_nudgeInterval);
        _nudgeInterval = null;
    }
    apiCall('/telescope/nudge/stop', 'POST', {});
}

// Mark GoTo inputs as user-edited so telemetry doesn't overwrite them.
// Use delegated handler in case control-panel DOM is recreated.
document.addEventListener('input', (ev) => {
    const el = ev.target;
    if (!el || !el.id) return;
    if (el.id === 'gotoAlt' || el.id === 'gotoAz' || el.id === 'gotoRa' || el.id === 'gotoDec') {
        el.dataset.userEdited = '1';
        // #region agent log
        _agentDebugLog(`input_${Date.now()}`,'H7','static/telescope.js:input:userEdited','marked goto input userEdited',{id:el.id,value:el.value});
        // #endregion
    }
});

async function gotoExecute(overrideAlt, overrideAz) {
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
    showStatus('Slewing…', 'info', 10000);
    const result = await apiCall('/telescope/goto', 'POST', body);
    if (result) {
        if (result.manual_slew) {
            const msg = result.message || 'Manual slewing — watch telemetry for progress';
            if (result.resume_tracking) {
                showStatus(
                    `${msg} — will re-enable ${result.resume_tracking} tracking when aligned`,
                    'info',
                    15000,
                );
            } else {
                showStatus(msg, 'info', 15000);
            }
        } else {
            showStatus('GoTo command sent', 'success', 3000);
        }
        // Resume telemetry updates in GoTo fields
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

    // Use current telemetry alt/az if available, else prompt for manual entry
    const altEl = document.getElementById('telmAlt');
    const azEl  = document.getElementById('telmAz');
    const altText = altEl && altEl.textContent.replace('°','').trim();
    const azText  = azEl  && azEl.textContent.replace('°','').trim();
    const alt = parseFloat(altText);
    const az  = parseFloat(azText);

    if (isNaN(alt) || isNaN(az)) {
        // Fall back to the GoTo inputs
        const altInput = parseFloat(document.getElementById('gotoAlt').value);
        const azInput  = parseFloat(document.getElementById('gotoAz').value);
        if (isNaN(altInput) || isNaN(azInput)) {
            showStatus('No telemetry available — enter Alt/Az in the GoTo fields first', 'error', 5000);
            return;
        }
        await _doSaveLocation(name, altInput, azInput);
    } else {
        await _doSaveLocation(name, alt, az);
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

async function telescopeAutofocus() {
    showStatus('Autofocusing…', 'info', 20000);
    const result = await apiCall('/telescope/autofocus', 'POST', {});
    if (result) showStatus('Autofocus triggered', 'success', 3000);
}

async function telescopeShutdown() {
    if (!confirm('Shut down the Seestar? You will need to physically restart it.')) return;
    showStatus('Sending shutdown…', 'info', 5000);
    const result = await apiCall('/telescope/shutdown', 'POST', {});
    if (result) showStatus('Shutdown command sent — scope powering off', 'success', 5000);
}

// -- Manual Focus --

let _focusStepSize = 10;

function setFocusStepSize(size) {
    _focusStepSize = size;
    document.querySelectorAll('.focus-step-btn').forEach(btn => {
        const active = parseInt(btn.dataset.steps) === size;
        btn.style.borderColor = active ? '#2dd4bf' : '';
        btn.style.background  = active ? 'rgba(45,212,191,0.15)' : '';
    });
}

async function focusStep(steps) {
    const result = await apiCall('/telescope/focus/step', 'POST', { steps });
    if (result) {
        const pos = result.result && result.result.focus_pos != null
            ? result.result.focus_pos : null;
        if (pos != null) {
            const el = document.getElementById('focusPos');
            if (el) el.textContent = pos;
        }
    }
}

// -- Camera Settings --

function toggleDewPower() {
    const on = document.getElementById('dewHeaterToggle').checked;
    document.getElementById('dewPowerRow').style.display = on ? '' : 'none';
}

async function applyCameraSettings() {
    const body = {
        gain:       parseInt(document.getElementById('gainSlider').value),
        lp_filter:  document.getElementById('lpFilterToggle').checked,
        dew_heater: document.getElementById('dewHeaterToggle').checked,
        dew_power:  parseInt(document.getElementById('dewPowerSlider').value),
    };
    const result = await apiCall('/telescope/settings/camera', 'PATCH', body);
    if (result && result.success) showStatus('Camera settings applied', 'success', 3000);
}

async function toggleAutoExp() {
    const result = await apiCall('/telescope/camera/auto-exp', 'POST', { enabled: true });
    if (result && result.success) showStatus('Auto exposure enabled', 'success', 3000);
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
    const btn = document.getElementById('telegramMuteBtn');
    if (!btn) return;
    btn.textContent = muted ? '🔕' : '🔔';
    btn.title = muted ? 'Telegram alerts muted — click to unmute' : 'Mute Telegram alerts';
    btn.style.opacity = muted ? '0.5' : '1';
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

function updateFilmstrip(files) {
    const filmstrip = document.getElementById('filmstripList');
    if (!filmstrip) return;

    filmstripFiles = files.slice(0, 10);
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
        const thumbnail = file.thumbnail
            ? `<img src="${file.thumbnail}" alt="${file.name}" title="${imgTitle}" class="filmstrip-thumbnail">`
            : isVideo
                ? `<canvas class="filmstrip-thumbnail video-thumb-canvas" data-video-src="${file.path}"></canvas>`
                : `<img src="${file.path}" alt="${file.name}" title="${imgTitle}" class="filmstrip-thumbnail">`;
        const detBadge2 = file.diff_heatmap
            ? '<span style="position:absolute; top:1px; right:1px; font-size:0.9em; filter:drop-shadow(0 0 2px #000);" title="Has heatmap">🔥</span>'
            : '';
        
        return `
        <div class="${itemClass}" data-file-path="${file.path}" data-file-idx="${index}" onclick="filmstripSelectItem(${index}, '${file.path}', event)" style="position:relative;">
            ${badge}${detBadge2}
            <div class="filmstrip-name" title="${displayName}">${displayName}</div>
            ${thumbnail}
            <div class="filmstrip-info">
                <div class="filmstrip-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download" ${isTemp ? 'disabled' : ''}>⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); deleteFile('${file.path}', '${file.name}', event.metaKey || event.ctrlKey)" title="${getFavorites().has(file.path) ? 'Remove favorite first' : 'Delete (⌘/Ctrl+click to skip confirm)'}" ${isTemp || getFavorites().has(file.path) ? 'disabled' : ''}>🗑️</button>
                </div>
            </div>
        </div>
    `;
    }).join('');

    // Generate thumbnails from video first frame for any canvas placeholders
    filmstrip.querySelectorAll('canvas.video-thumb-canvas').forEach(generateVideoThumbnail);
}

function _syncFilmstripSelectionUI() {
    const filmstrip = document.getElementById('filmstripList');
    if (!filmstrip) return;
    filmstrip.querySelectorAll('.filmstrip-item').forEach(el => {
        const path = el.dataset.filePath;
        el.classList.toggle('selected', filmstripSelection.selected.has(path));
    });
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
        return `
        <div class="file-item${sel}" data-file-path="${file.path}" data-file-idx="${idx}"
             onclick="gridSelectItem(${idx}, '${file.path}', event)" style="position:relative;">
            ${detBadge}
            <div class="file-info">
                <span class="file-name" title="${displayName}">${displayName}</span>
                <div class="file-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
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
 * On success, draws red span marks on markedFrameBar and auto-seeks the
 * hidden video to the peak transit frame, setting a 3s loop around it.
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
        // Auto-seek to peak and set 3s loop
        const vid = document.getElementById('hiddenVid');
        if (vid && data.peak_time_s != null) {
            const ps = parseFloat(data.peak_time_s);
            const half = 1.5;
            _loopSegment = { start: Math.max(0, ps - half), end: ps + half };
            vid.currentTime = _loopSegment.start;
            vid.play().catch(() => {});
        }
    } catch (err) {
        console.warn('[isolate] error:', err);
    }
}

/**
 * Draw red transit-span tick marks on markedFrameBar.
 * Yellow ticks (user-marked frames) are preserved alongside.
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
    const triggered = sig.triggered || [];
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

    const transitHiresFrame = sig.transit_hires_frame != null
        ? sig.transit_hires_frame
        : Math.round((sidecar.peak_time_s || 1.0) * hiresFps);

    _isolateResult = {
        spans: spans.length > 0 ? spans : [[Math.max(0, transitHiresFrame - 8), transitHiresFrame + 8]],
        peak_frame: transitHiresFrame,
        peak_time_s: sidecar.peak_time_s || (transitHiresFrame / hiresFps),
    };
    _drawTransitSpans();
    _ensureTransitNavButtons();

    // Auto-seek to transit
    const vid = document.getElementById('hiddenVid');
    if (vid && _isolateResult.peak_time_s != null) {
        const ps = _isolateResult.peak_time_s;
        _loopSegment = { start: Math.max(0, ps - 1.0), end: ps + 1.5 };
        vid.currentTime = _loopSegment.start;
        vid.play().catch(() => {});
    }

    // Confidence banner
    const conf = sig.confidence_score != null ? `${Math.round(sig.confidence_score * 100)}%` : '';
    const gate = sig.gate_detail || sig.gate_type || '';
    const cnn = sig.cnn_confidence != null ? ` · CNN ${Math.round(sig.cnn_confidence * 100)}%` : '';
    _setScanBanner('success', `✅ Live detection: ${gate} · confidence ${conf}${cnn}`);

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
    _isolateResult = null;
    _scrubSlider = null;
    // Resolve companion images for this file
    const curFileInfo = files.find(f => f.path === path) || {};
    const companionHtml = _buildCompanionStrip(curFileInfo);

    if (isVideo) {
        const loopAttr = opts.loop ? ' loop' : '';
        body.innerHTML =
            `<div style="display:flex; flex-direction:column; width:100%; max-height:85vh; overflow-y:auto;" id="frameViewerRoot">` +
              `<video src="${path}" playsinline${loopAttr} muted style="position:absolute;left:-9999px;width:1px;height:1px;" id="hiddenVid"></video>` +
              `<div style="position:relative; flex-shrink:0;">` +
                `<div id="fivePanel" style="display:flex; justify-content:center; align-items:center; gap:3px; padding:4px 4px 0; background:#000;">` +
                  `<span style="color:#555; font-size:0.85em;">Loading…</span>` +
                `</div>` +
                (companionHtml ? `<div id="companionOverlay" style="position:absolute; top:8px; right:8px; display:flex; flex-direction:column; gap:4px; z-index:10; opacity:0.85;">${companionHtml}</div>` : '') +
              `</div>` +
              `<div id="frameScrubber" style="width:100%; padding:6px 12px; background:#1a1a1a; border-top:1px solid #333; flex-shrink:0;">` +
                `<div style="display:flex; align-items:center; justify-content:center; gap:8px; margin-bottom:4px;">` +
                  `<span id="frameCounter" style="color:#0ff; font-family:monospace; font-size:0.85em; min-width:120px;">Frame 0 / 0</span>` +
                  `<button id="markFrameBtn" class="btn-viewer" onclick="toggleMarkFrame()" ` +
                    `title="Mark/unmark this frame for composite (M key)" style="font-size:0.85em; padding:2px 8px;">📌 Mark</button>` +
                  `<span id="markedCount" style="color:#fd0; font-family:monospace; font-size:0.8em; min-width:70px;">0 marked</span>` +
                `</div>` +
                `<input type="range" id="frameScrubSlider" min="0" max="100" value="0" step="1" ` +
                  `style="width:100%; height:20px; accent-color:#0ff; cursor:pointer;" title="Drag to scrub frames">` +
                `<div style="display:flex; justify-content:space-between; align-items:center; margin-top:4px;">` +
                  `<div style="color:#666; font-size:0.6em;">` +
                    `←/→ step · Shift ±10 · Space play/pause · M mark` +
                  `</div>` +
                `</div>` +
                `<div id="markedFrameBar" style="position:relative; width:100%; height:8px; background:#222; margin-top:4px; border-radius:4px; overflow:hidden;" title="Yellow = marked frames · Red = transit spans · Green = peak frame"></div>` +
              `</div>` +
              `<div id="buildCompositeRow" style="display:none; padding:4px 8px; background:#1a1a1a; border-top:1px solid #222; text-align:center; flex-shrink:0;">` +
                `<button class="btn-viewer" id="buildCompositeBtn" onclick="buildCompositeFromMarked()">🖼 Build Composite (<span id="compositeCountBtn">0</span>)</button>` +
              `</div>` +
            `</div>`;
        const vid = document.getElementById('hiddenVid');
        vid.pause();
        _currentFrame = 0;

        const slider = document.getElementById('frameScrubSlider');
        slider.addEventListener('input', () => {
            const f = parseInt(slider.value, 10);
            _currentFrame = f;
            vid.currentTime = f / _videoFps;
        });

        const updateAfterSeek = () => {
            _currentFrame = Math.round(vid.currentTime * _videoFps);
            _updateScrubPosition(vid);
            _updateFivePanel();
        };
        vid.addEventListener('timeupdate', () => {
            if (_loopSegment && _loopSegment.start != null && vid.currentTime >= _loopSegment.end) {
                vid.currentTime = _loopSegment.start;
            }
            _currentFrame = Math.round(vid.currentTime * _videoFps);
            _updateScrubPosition(vid);
            _updateFivePanel();
        });
        vid.addEventListener('seeked', updateAfterSeek);
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
        vid.addEventListener('loadedmetadata', () => { _initFrameScrubber(vid); updateAfterSeek(); _maybeAutoIsolate(); });
        vid.addEventListener('loadeddata', () => { _initFrameScrubber(vid); updateAfterSeek(); });
        if (vid.readyState >= 1) { _initFrameScrubber(vid); updateAfterSeek(); }
        _extractFrameThumbs(vid);
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
        actionsEl.innerHTML =
            `<button class="btn-viewer" onclick="viewerNav(-1)" title="Previous" ${hasPrev ? '' : 'disabled'}>◀</button>` +
            scanBtn +
            replayBtn +
            favBtn +
            `<button class="btn-viewer" onclick="viewerDownload()" title="Download">⬇️ Download</button>` +
            `<button class="btn-viewer btn-viewer-danger" id="viewerDeleteBtn" onclick="viewerDelete(event)" ${delDisabled}>🗑️ Delete</button>` +
            `<button class="btn-viewer" onclick="viewerNav(1)" title="Next" ${hasNext ? '' : 'disabled'}>▶</button>`;
    }

    viewer.style.display = 'flex';
}

function closeFileViewer() {
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    viewer.style.display = 'none';
    body.innerHTML = '';
    _setScanBanner(null);
    _viewerIndex = -1;
    _viewerFile = null;
    _loopSegment = null;
    _markedFrames = new Set();
    _scrubSlider = null;
    _frameThumbs = [];
    _currentFrame = 0;
    if (_thumbExtractorVid) {
        try { document.body.removeChild(_thumbExtractorVid); } catch (e) {}
        _thumbExtractorVid = null;
    }

    if (viewer._filesModalWasOpen) {
        const filesModal = document.getElementById('filesModal');
        if (filesModal) filesModal.style.display = 'flex';
        viewer._filesModalWasOpen = false;
    }
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

// ---------------------------------------------------------------------------
// Frame scrubber with mark-for-composite
// ---------------------------------------------------------------------------

var _currentFrame = 0;

function _stepFrame(dir) {
    const vid = document.getElementById('hiddenVid');
    if (!vid || !vid.duration) return;
    vid.pause();
    const newFrame = Math.max(0, Math.min(_frameTotalCount - 1, _currentFrame + dir));
    _currentFrame = newFrame;
    vid.currentTime = newFrame / _videoFps;
}

function _initFrameScrubber(vid) {
    if (!vid || !vid.duration) return;
    _videoFps = 30;
    _frameTotalCount = Math.round(vid.duration * _videoFps);
    const slider = document.getElementById('frameScrubSlider');
    if (slider) {
        slider.max = _frameTotalCount - 1;
        slider.value = _currentFrame;
    }
    _updateScrubPosition(vid);
}

function _updateScrubPosition(vid) {
    const counter = document.getElementById('frameCounter');
    const slider = document.getElementById('frameScrubSlider');
    if (!vid) return;
    const frame = _currentFrame;
    if (counter) {
        counter.textContent = `Frame ${frame} / ${_frameTotalCount || '?'}`;
    }
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

var _frameThumbs = [];      // full-quality data-URL images per frame
var _frameTotalCount = 0;
var _thumbExtractorVid = null;

/** Extract all frames at display size using a hidden video element. */
function _extractFrameThumbs(mainVid) {
    if (_thumbExtractorVid) {
        try { document.body.removeChild(_thumbExtractorVid); } catch (e) {}
        _thumbExtractorVid = null;
    }
    _frameThumbs = [];

    const extVid = document.createElement('video');
    extVid.src = mainVid.src;
    extVid.muted = true;
    extVid.preload = 'auto';
    extVid.style.cssText = 'position:absolute;left:-9999px;width:1px;height:1px;';
    document.body.appendChild(extVid);
    _thumbExtractorVid = extVid;

    extVid.addEventListener('loadeddata', () => {
        const fps = _videoFps || 30;
        const total = Math.round(extVid.duration * fps);
        _frameTotalCount = total;
        _frameThumbs = new Array(total).fill(null);

        const slider = document.getElementById('frameScrubSlider');
        if (slider) slider.max = total - 1;

        const vw = extVid.videoWidth || 640;
        const vh = extVid.videoHeight || 480;
        // Extract at a reasonable size (panel will be ~20% of viewport width)
        const thumbW = Math.min(vw, 400);
        const thumbH = Math.round(thumbW * (vh / vw));
        const canvas = document.createElement('canvas');
        canvas.width = thumbW;
        canvas.height = thumbH;
        const ctx = canvas.getContext('2d');

        let idx = 0;
        const panel = document.getElementById('fivePanel');
        function next() {
            if (idx >= total || !document.getElementById('fivePanel')) {
                try { document.body.removeChild(extVid); } catch (e) {}
                _thumbExtractorVid = null;
                _updateFivePanel();
                return;
            }
            extVid.currentTime = idx / fps;
            extVid.onseeked = () => {
                ctx.drawImage(extVid, 0, 0, thumbW, thumbH);
                _frameThumbs[idx] = canvas.toDataURL('image/jpeg', 0.85);
                idx++;
                if (panel && idx % 30 === 0) {
                    panel.innerHTML = `<span style="color:#555; font-size:0.85em;">Extracting frames ${idx}/${total}…</span>`;
                }
                // Show the panel as soon as we have the first few frames
                if (idx === 5) _updateFivePanel();
                requestAnimationFrame(next);
            };
        }
        next();
    }, { once: true });
}

/** Render 5 equal big panels: frames [cur-2, cur-1, cur, cur+1, cur+2] */
function _updateFivePanel() {
    const panel = document.getElementById('fivePanel');
    if (!panel) return;
    const total = _frameTotalCount || 1;
    const cur = _currentFrame;

    if (!_frameThumbs.length || !_frameThumbs[0]) return; // still extracting

    const offsets = [-2, -1, 0, 1, 2];
    let html = '';
    offsets.forEach(off => {
        const f = cur + off;
        const isCurrent = off === 0;
        const border = isCurrent ? '3px solid #0ff' : '3px solid #333';
        const opacity = (f < 0 || f >= total) ? '0.12' : '1';
        const src = (f >= 0 && f < total && _frameThumbs[f]) ? _frameThumbs[f] : '';
        const marked = _markedFrames.has(f);
        const markDot = marked ? `<div style="position:absolute;top:4px;right:4px;width:12px;height:12px;background:#fd0;border-radius:50%;border:1px solid #000;"></div>` : '';
        const labelColor = isCurrent ? '#0ff' : '#888';
        html += `<div style="position:relative; flex:1; min-width:0; border:${border}; border-radius:4px; opacity:${opacity}; cursor:pointer; overflow:hidden;" ` +
                `onclick="_jumpToFrame(${f})" title="Frame ${f}">` +
                (src ? `<img src="${src}" style="display:block; width:100%; height:auto;" draggable="false">` :
                       `<div style="width:100%; padding-top:75%; background:#111;"></div>`) +
                `<div style="position:absolute;bottom:2px;left:0;right:0;text-align:center;color:${labelColor};font-size:0.75em;font-weight:${isCurrent?'bold':'normal'};font-family:monospace;text-shadow:0 0 4px #000;">${f >= 0 && f < total ? f : ''}</div>` +
                markDot +
                `</div>`;
    });
    panel.innerHTML = html;
}

function _jumpToFrame(f) {
    const vid = document.getElementById('hiddenVid');
    if (!vid || f < 0 || f >= _frameTotalCount) return;
    vid.pause();
    _currentFrame = f;
    vid.currentTime = f / _videoFps;
}

function toggleMarkFrame() {
    const frame = _currentFrame;
    if (_markedFrames.has(frame)) {
        _markedFrames.delete(frame);
    } else {
        _markedFrames.add(frame);
    }
    _updateMarkedUI();
    const vid = document.getElementById('hiddenVid');
    if (vid) _updateScrubPosition(vid);
    _updateFivePanel();
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
    if (btn) { btn.disabled = true; btn.textContent = '⏳ Building…'; }
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
    _setScanBanner('info', '🔍 Analyzing… 0s');
    let _analyzeTimer = setInterval(() => {
        if (btn && btn.disabled) {
            const dots = '.'.repeat(Math.floor(Date.now() / 500) % 4);
            btn.textContent = `🔍 Analyzing${dots}`;
        }
        const elapsed = Math.floor((Date.now() - _analyzeStart) / 1000);
        _setScanBanner('info', `🔍 Analyzing… ${elapsed}s`);
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

    if (simulateBtn)   { simulateBtn.textContent = 'Stop Sim'; simulateBtn.className = 'btn btn-warning'; }
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

    if (simulateBtn)   { simulateBtn.textContent = 'Simulate'; simulateBtn.className = 'btn btn-info'; }
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

    // Style the button as active
    const btn = document.getElementById('simEclipseBtn');
    if (btn) { btn.textContent = `${preset.icon} Stop Eclipse`; btn.classList.add('active'); }

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

    const btn = document.getElementById('simEclipseBtn');
    if (btn) { btn.textContent = '🌑 Sim Eclipse'; btn.classList.remove('active'); }

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
            // Insert before the event log inside the detect panel
            const log = document.getElementById('detectEventLog');
            if (log && log.parentNode) {
                log.parentNode.insertBefore(el, log);
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
            const log = document.getElementById('detectEventLog');
            if (log && log.parentNode) log.parentNode.insertBefore(el, log);
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
        setTimeout(refreshFiles, 3000);
    }

    // Add to event log
    appendDetectionEvent(event);
}

function appendDetectionEvent(event) {
    const log = document.getElementById('detectEventLog');
    if (!log) return;

    const ts = new Date(event.timestamp).toLocaleTimeString('en-US', {
        hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false
    });
    const flight = event.flight_info;
    const flightStr = flight ? `${flight.name} (${flight.aircraft_type})` : 'Unknown';
    const sepStr = flight ? `${flight.separation_deg}°` : '';

    // D3: numeric confidence score badge
    const cscore = event.confidence_score;
    const cCol = (cscore >= 0.65) ? '#4caf50' : (cscore >= 0.4) ? '#ff9800' : '#9e9e9e';
    const cBadge = (cscore != null)
        ? ` <span style="color:${cCol};font-size:0.82em" title="Confidence score">${Math.round(cscore * 100)}%</span>`
        : '';
    // D3: prediction match badge
    const predFid = event.predicted_flight_id;
    const evFlight = event.flight_info;
    const predMatched = predFid && evFlight && evFlight.name && predFid === evFlight.name;
    const predBadge = predFid
        ? (predMatched
            ? ' <span style="color:#4caf50;font-size:0.75em" title="Prediction matched">✅match</span>'
            : ` <span style="color:#ff9800;font-size:0.75em" title="Primed for ${predFid}">🎯primed</span>`)
        : '';

    const row = document.createElement('div');
    row.className = 'detect-event-row';
    row.innerHTML =
        `<span class="detect-event-time">${ts}</span>` +
        `<span class="detect-event-flight">${flightStr}${cBadge}${predBadge}</span>` +
        `<span class="detect-event-sep">${sepStr}</span>`;

    // If there's a recording file, make it clickable
    if (event.recording_file) {
        row.style.cursor = 'pointer';
        row.onclick = () => {
            const url = '/' + event.recording_file;
            const name = event.recording_file.split('/').pop();
            viewFile(url, name, { loop: true });
        };
    }

    // Prepend (newest first)
    const empty = log.querySelector('.empty-state');
    if (empty) empty.remove();
    log.insertBefore(row, log.firstChild);

    // Cap at 20 entries
    while (log.children.length > 20) {
        log.removeChild(log.lastChild);
    }
}

function updateDetectionUI() {
    const btn = document.getElementById('detectToggleBtn');
    const indicator = document.getElementById('detectIndicator');
    const statsEl = document.getElementById('detectStats');

    if (btn) {
        btn.textContent = isDetecting ? '⏹ Stop Detection' : '▶ Start Detection';
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

    // Update event log empty state when running with no detections
    const log = document.getElementById('detectEventLog');
    if (log) {
        const empty = log.querySelector('.empty-state');
        if (empty) {
            empty.textContent = isDetecting
                ? '🔭 Watching for transits…'
                : 'No detections yet';
        }
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
            if (result.recent_events) {
                result.recent_events.forEach(e => appendDetectionEvent(e));
            }
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
    if (document.getElementById('tuningCard')) return;

    const saved = _loadTuning();

    const card = document.createElement('div');
    card.id = 'tuningCard';
    card.style.cssText =
        'background:#111122; border:1px solid rgba(255,255,255,0.08); border-radius:8px; ' +
        'margin-bottom:8px; overflow:hidden;';

    card.innerHTML = `
        <button id="tuningToggleBtn" onclick="_toggleTuningBody()"
            style="width:100%; background:transparent; border:none; padding:8px 12px;
                   display:flex; justify-content:space-between; align-items:center;
                   cursor:pointer; color:#888; font-size:0.82em; font-weight:600;
                   letter-spacing:0.05em;">
            <span>⚙️ DETECTION TUNING</span>
            <span id="tuningChevron" style="opacity:0.5; transition:transform 0.2s;">▾</span>
        </button>
        <div id="tuningBody" style="display:none; padding:0 12px 10px 12px;">
            <div style="font-size:0.79em; color:#666; margin-bottom:8px;">
                Changes apply immediately to the live detector.
            </div>
            ${_tuningSliderRow('tunMargin',  'Edge Margin %', 5, 50, Math.round(saved.disk_margin_pct*100),
                'Exclude the outermost N% of disk radius (limb zone). Higher = fewer false positives from limb jitter.')}
            ${_tuningSliderRow('tunRatio',   'Centre Ratio', 0.5, 6, saved.centre_ratio_min, 0.1,
                'Inner-disk signal must be N× the limb signal. Higher = stricter concentration requirement.')}
            ${_tuningSliderRow('tunConsec',  'Consec Frames (fast gate)', 2, 20, saved.consec_frames, 1,
                '<strong>Fast gate</strong> — fires when N consecutive frames all exceed the signal threshold. Runs <em>in parallel</em> with the matched-filter gate (D2); a transit only needs to pass <em>one</em> of the two gates. Lower this for very fast aircraft; raise it to suppress noise. The MF gate covers slow transits that would miss here.')}
            ${_tuningSliderRow('tunMFThresh', 'MF Gate Threshold %', 50, 100, Math.round(saved.mf_threshold_frac * 100), 5,
                '<strong>Matched-filter gate</strong> — fires when at least N% of frames inside a sliding window (4 / 7 / 12 / 18 / 30 frames) are triggered. Lower = more sensitive to intermittent or slow transits; higher = stricter pattern required. Default 70%. Runs alongside the fast Consec gate.')}
            ${_tuningSliderRow('tunSensitivity', 'Sensitivity', 0.2, 3.0, saved.sensitivity_scale, 0.1,
                'Multiplier applied to both adaptive thresholds — affects <em>both</em> the fast Consec gate and the MF gate. Below 1 = lower bar (more detections). Above 1 = higher bar (fewer detections). Adjust if you are getting too many or too few alerts.')}
            ${_tuningSliderRow('tunTrackMag', 'Track Min Motion (px)', 0, 10, saved.track_min_mag, 0.1,
                'A <em>spatial</em> gate — complements Consec Frames. Sets the minimum pixel displacement of the detected blob\'s centroid between frames to count as real directional motion. Atmospheric shimmer moves the centroid randomly by ≤2 px with no consistent direction; a real aircraft moves 3–8 px per frame in a straight line. Frames below this threshold abstain from the direction vote. Set to 0 to disable.')}
            ${_tuningSliderRow('tunTrackAgree', 'Track Agreement %', 0, 100, Math.round(saved.track_min_agree_frac * 100), 5,
                'What fraction of the streak frames (that cleared Track Min Motion) must agree on direction before the detection fires. 60% = default. Set to 0 to disable the direction gate entirely.')}
            <button class="btn btn-secondary btn-compact" style="margin-top:6px; width:100%;" onclick="_resetTuning()">↩ Reset to defaults</button>
        </div>
    `;

    // Insert before the harness panel (or event log)
    const harness = document.getElementById('harnessPanel');
    const eventLog = document.getElementById('detectEventLog');
    const ref = harness || eventLog;
    if (ref && ref.parentNode === detectPanel) {
        detectPanel.insertBefore(card, ref);
    } else {
        detectPanel.appendChild(card);
    }

    // Wire up sliders
    ['tunMargin', 'tunRatio', 'tunConsec', 'tunMFThresh', 'tunSensitivity', 'tunTrackMag', 'tunTrackAgree'].forEach(id => {
        const el = document.getElementById(id);
        if (!el) return;
        el.addEventListener('input', () => {
            _updateTuningLabel(id);
            _debouncedApplyTuning();
        });
    });
}

function _toggleTuningBody() {
    const body = document.getElementById('tuningBody');
    const chevron = document.getElementById('tuningChevron');
    if (!body) return;
    const open = body.style.display !== 'none';
    body.style.display = open ? 'none' : '';
    if (chevron) chevron.style.transform = open ? '' : 'rotate(180deg)';
}

let _tuningDebounceTimer = null;
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
        localStorage.setItem('det_disk_margin',   settings.disk_margin_pct);
        localStorage.setItem('det_centre_ratio',  settings.centre_ratio_min);
        localStorage.setItem('det_consec_frames', settings.consec_frames);
        localStorage.setItem('det_sensitivity',   settings.sensitivity_scale);
        localStorage.setItem('det_track_mag',     settings.track_min_mag);
        localStorage.setItem('det_track_agree',   settings.track_min_agree_frac);
        localStorage.setItem('det_mf_thresh',     settings.mf_threshold_frac);
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

        const eventLog = document.getElementById('detectEventLog');
        if (eventLog && eventLog.parentNode === detectPanel) {
            detectPanel.insertBefore(panel, eventLog);
        } else {
            const detectBtn = document.getElementById('detectToggleBtn');
            if (detectBtn && detectBtn.parentNode === detectPanel) {
                detectBtn.insertAdjacentElement('afterend', panel);
            } else {
                detectPanel.appendChild(panel);
            }
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
let _detEventsFetched = false;

/**
 * Create or toggle the detection-event-history panel inside detectPanel.
 * Fetches from /api/transit-events the first time it's opened.
 */
window.toggleDetectionEventHistory = async function() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;

    if (_detEventsPanel) {
        _detEventsPanel.style.display =
            _detEventsPanel.style.display === 'none' ? '' : 'none';
        return;
    }

    _detEventsPanel = document.createElement('div');
    _detEventsPanel.id = 'detEventsPanel';
    _detEventsPanel.style.cssText =
        'margin:8px 0;background:#1a1a2e;border:1px solid #2a2a4a;border-radius:6px;padding:8px;';

    const title = document.createElement('div');
    title.style.cssText = 'font-weight:600;font-size:0.85em;color:#90caf9;margin-bottom:6px;';
    title.textContent = '📋 Detection Event History (last 7 days)';
    _detEventsPanel.appendChild(title);

    const tableWrap = document.createElement('div');
    tableWrap.id = 'detEventsTableWrap';
    tableWrap.style.cssText = 'overflow-x:auto;max-height:260px;overflow-y:auto;';
    tableWrap.innerHTML = '<div style="color:#555;font-size:0.8em;padding:6px">Loading…</div>';
    _detEventsPanel.appendChild(tableWrap);

    detectPanel.appendChild(_detEventsPanel);
    await _refreshDetectionEventHistory();
};

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

    const cols = [
        { key: 'timestamp',          label: 'Time',         fmt: v => v ? new Date(v).toLocaleString('en-US', {month:'short',day:'numeric',hour:'2-digit',minute:'2-digit',second:'2-digit',hour12:false}) : '—' },
        { key: 'detected_flight_id', label: 'Flight',       fmt: v => v || '<span style="color:#555">unconfirmed</span>' },
        { key: 'predicted_flight_id',label: 'Predicted',    fmt: v => v || '—' },
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
            ? `<span style="color:#90caf9;font-size:0.82em">${v}</span>`
            : '' },
        { key: 'label',              label: 'Label',        fmt: (v, ev) => {
            const cur = v || '';
            const ts = (ev.timestamp || '').replace(/"/g, '&quot;');
            const btns = ['tp','fp','fn'].map(lbl => {
                const active = cur === lbl;
                const col = active ? _LABEL_COLORS[lbl] : '#333';
                const bord = active ? _LABEL_COLORS[lbl] : '#444';
                return `<button onclick="_labelEvent('${ts}','${lbl}',this)"
                    style="background:${col};border:1px solid ${bord};color:#fff;
                    padding:1px 5px;border-radius:3px;cursor:pointer;font-size:0.78em;
                    margin-right:2px;">${_LABEL_ICONS[lbl]}</button>`;
            }).join('');
            return `<span style="display:inline-flex;align-items:center;">${btns}</span>`;
        }},
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

    const tbody = tbl.createTBody();
    events.slice(0, 100).forEach(ev => {
        const tr = tbody.insertRow();
        tr.style.cssText = 'border-bottom:1px solid #1e1e3a;';
        tr.onmouseenter = () => tr.style.background = '#1e1e3a';
        tr.onmouseleave = () => tr.style.background = '';
        cols.forEach(c => {
            const td = tr.insertCell();
            td.style.cssText = 'padding:3px 6px;white-space:nowrap;';
            td.innerHTML = c.fmt(ev[c.key] ?? '', ev);
        });
    });

    wrap.innerHTML = '';
    wrap.appendChild(tbl);
}

/** T23 — Send a TP/FP/FN label for a detection event to the backend. */
async function _labelEvent(timestamp, label, btnEl) {
    try {
        const resp = await fetch('/api/transit-events/label', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ timestamp, label }),
        });
        if (!resp.ok) throw new Error(await resp.text());

        // Visual feedback: highlight the active button in its row
        const row = btnEl.closest('tr');
        if (row) {
            row.querySelectorAll('button').forEach(b => {
                const lbl = b.textContent.toLowerCase().replace(/[^a-z]/g, '').slice(0, 2);
                const _LABEL_COLORS_JS = { tp: '#4caf50', fp: '#f44336', fn: '#ff9800' };
                const active = lbl === label;
                b.style.background = active ? (_LABEL_COLORS_JS[lbl] || '#555') : '#333';
                b.style.borderColor = active ? (_LABEL_COLORS_JS[lbl] || '#666') : '#444';
            });
        }
    } catch (e) {
        console.error('[Label]', e);
    }
}

// ============================================================================
// CLEANUP
// ============================================================================

let _radarAnimFrame = null;
let _radarSweepAfterDetectTimer = null;
let _radarLastTransitTs = 0;        // ms — updated every push; sweep runs while fresh
const RADAR_SWEEP_KEEPALIVE_MS = 660_000; // freeze ~11 min after last transit push (> 10 min poll cycle)

window.destroyTelescope = function() {
    if (statusPollInterval)     { clearInterval(statusPollInterval);     statusPollInterval     = null; }
    if (visibilityPollInterval) { clearInterval(visibilityPollInterval); visibilityPollInterval = null; }
    if (lastUpdateInterval)     { clearInterval(lastUpdateInterval);     lastUpdateInterval     = null; }
    if (transitPollInterval)    { clearInterval(transitPollInterval);    transitPollInterval    = null; }
    if (transitTickInterval)    { clearInterval(transitTickInterval);    transitTickInterval    = null; }
    if (detectionPollInterval)  { clearInterval(detectionPollInterval);  detectionPollInterval  = null; }
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
const RADAR_HISTORY_MAX         = 24;      // trail length per blip
const RADAR_SWEEP_RUN_AFTER_DETECT_S = 30; // seconds sweep stays on after detection

// ── state ───────────────────────────────────────────────────────────────────
const _radarTracks       = new Map();  // id → {points[], color, level, label, altFt, speedKmh, heading}
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
function _radarDrawFrame(ts) {
    _radarAnimFrame = requestAnimationFrame(_radarDrawFrame);

    const canvas = _radarCanvas;
    if (!canvas || !canvas.isConnected) return;
    const ctx = _radarCtx;
    const W = canvas.width, H = canvas.height;
    if (W < 10 || H < 10) return;
    const cx = W/2, cy = H/2;
    const R  = Math.min(cx, cy) - 8;

    // derive active state from last-seen timestamp (no fixed timer)
    _radarSweepActive = _radarLastTransitTs > 0 &&
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
        ctx.beginPath();
        ctx.moveTo(0, 0);
        ctx.arc(0, 0, R, -Math.PI/2, -Math.PI/2 + Math.PI*0.45);
        ctx.closePath();
        ctx.fillStyle = `rgba(0,255,80,${wedgeAlpha})`;
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
        if (!track.points.length) return;
        const last = track.points[track.points.length-1];
        const id   = track.id;
        const color= track.color;

        let drawPts = track.points;
        if (_radarMode === 'enhanced') {
            const vel = _radarVelocity(track.points);
            if (vel) {
                const pred = [];
                for (let dt = 1; dt <= RADAR_PREDICT_HORIZON_S; dt++) {
                    pred.push({
                        altD: last.altD + vel.dAlt * dt,
                        azD:  last.azD  + vel.dAz  * dt,
                        t:    last.t + dt*1000
                    });
                }
                // draw cone
                if (pred.length >= 2) {
                    const pA = _radarBlipXY(pred[0], cx, cy, R);
                    const pB = _radarBlipXY(pred[pred.length-1], cx, cy, R);
                    ctx.beginPath();
                    ctx.moveTo(pA.x, pA.y);
                    ctx.lineTo(pB.x, pB.y);
                    ctx.strokeStyle = _hexToRgba(color, 0.35);
                    ctx.lineWidth = 2;
                    ctx.setLineDash([3,3]);
                    ctx.stroke();
                    ctx.setLineDash([]);
                    // arrowhead
                    const ang2 = Math.atan2(pB.y-pA.y, pB.x-pA.x);
                    const al = 7;
                    ctx.fillStyle = _hexToRgba(color, 0.5);
                    ctx.beginPath();
                    ctx.moveTo(pB.x, pB.y);
                    ctx.lineTo(pB.x - al*Math.cos(ang2-0.4), pB.y - al*Math.sin(ang2-0.4));
                    ctx.lineTo(pB.x - al*Math.cos(ang2+0.4), pB.y - al*Math.sin(ang2+0.4));
                    ctx.closePath(); ctx.fill();
                }
            }
        }

        // trail
        if (drawPts.length > 1) {
            ctx.beginPath();
            drawPts.forEach((p, i) => {
                const {x,y} = _radarBlipXY(p, cx, cy, R);
                i === 0 ? ctx.moveTo(x,y) : ctx.lineTo(x,y);
            });
            ctx.strokeStyle = _hexToRgba(color, 0.25);
            ctx.lineWidth = 1;
            ctx.stroke();
        }

        // blip glow / brightness
        const {x, y} = _radarBlipXY(last, cx, cy, R);
        const dAng = _radarSweepActive
            ? ((sweepAng - _radarPolar(last.altD, last.azD, R).ang) % (Math.PI*2) + Math.PI*2) % (Math.PI*2)
            : 0;
        const lit = !_radarSweepActive || dAng < beamHalf + 0.15;
        const alpha = lit ? 1.0 : 0.55;
        const blipR = track.level >= 2 ? 5 : 4;

        if (lit && _radarSweepActive) {
            ctx.save();
            ctx.shadowBlur  = 12; ctx.shadowColor = color;
            ctx.fillStyle   = '#fff';
            ctx.beginPath(); ctx.arc(x, y, blipR+1, 0, Math.PI*2); ctx.fill();
            ctx.restore();
        }
        ctx.fillStyle = _hexToRgba(color, alpha);
        ctx.beginPath(); ctx.arc(x, y, blipR, 0, Math.PI*2); ctx.fill();

        // ring for hovered/pinned
        if (id === _radarHoveredId || id === _radarPinnedId) {
            ctx.strokeStyle = _hexToRgba(color, 0.9);
            ctx.lineWidth = 1.5;
            ctx.beginPath(); ctx.arc(x, y, blipR+4, 0, Math.PI*2); ctx.stroke();
        }

        // label
        ctx.fillStyle = _hexToRgba(color, 0.85);
        ctx.font = '9px monospace';
        ctx.textAlign = 'left';
        ctx.fillText(track.label, x+7, y+3);

        _radarHitTest.push({id, x, y, r: blipR+5});
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
    const lvlCol = t.level >= 3 ? '#4caf50' : t.level === 2 ? '#ff9800' : '#FFD700';
    const last   = t.points[t.points.length-1];
    const sep    = last ? Math.hypot(last.altD, last.azD).toFixed(2) : '—';
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

// ── pushInterceptPoint (public API for app.js) ───────────────────────────────
window.pushInterceptPoint = function(flight) {
    const id = String(flight.id || flight.name || '').trim().toUpperCase();
    if (!id || flight.angular_separation == null) return;
    const level = parseInt(flight.possibility_level ?? 0);
    if (level < 1 || level > 3) return;

    const color = level >= 3 ? '#4caf50' : level === 2 ? '#ff9800' : '#FFD700';
    if (!_radarTracks.has(id)) {
        _radarTracks.set(id, {id, points:[], color, level, label:id, altFt:null, speedKmh:null, heading:null});
    }
    const track = _radarTracks.get(id);
    track.color  = color;
    track.level  = level;
    track.label  = id;
    track.altFt  = flight.altitude != null ? parseFloat(flight.altitude) : null;
    track.speedKmh = flight.speed != null ? parseFloat(flight.speed) : null;
    track.heading  = flight.heading != null ? parseFloat(flight.heading) : null;

    let altD = parseFloat(flight.alt_diff  ?? 'NaN');
    let azD  = parseFloat(flight.az_diff   ?? 'NaN');
    if (!isFinite(altD) || !isFinite(azD)) {
        // fallback: place blip along altitude axis using angular_separation
        const sep = parseFloat(flight.angular_separation);
        altD = isFinite(sep) ? sep : 0;
        azD  = 0;
    }
    track.points.push({ altD, azD, t: Date.now() });
    if (track.points.length > RADAR_HISTORY_MAX) track.points.shift();

    if (level >= 1) _radarMarkTransitSeen();
};

// ── injectMapTransits: populate upcoming transits list from app.js data ───────
const _upcomingTransits = [];
window.injectMapTransits = function(flights) {
    _upcomingTransits.length = 0;
    if (!Array.isArray(flights)) return;
    flights.forEach(f => {
        const level = parseInt(f.possibility_level ?? 0);
        if (level < 1) return;
        _upcomingTransits.push({
            id:   String(f.id || f.name || '').trim().toUpperCase(),
            eta:  f.transit_eta_seconds != null ? parseFloat(f.transit_eta_seconds) : null,
            level,
            target: f.target || 'sun',
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
    if (!_upcomingTransits.length) {
        el.innerHTML = '<div style="color:#334;font-size:0.8em;padding:4px 0">No upcoming transits</div>';
        return;
    }
    el.innerHTML = _upcomingTransits.slice(0, 5).map(tr => {
        const lvlCol = tr.level >= 3 ? '#4caf50' : tr.level === 2 ? '#ff9800' : '#FFD700';
        const lvlStr = tr.level >= 3 ? 'HIGH' : tr.level === 2 ? 'MED' : 'LOW';
        const etaStr = tr.eta != null ? `T−${Math.round(tr.eta)}s` : '—';
        return `<div style="display:flex;justify-content:space-between;align-items:center;
                    padding:2px 0;border-bottom:1px solid rgba(255,255,255,0.05)">
            <span style="font-weight:600;letter-spacing:.04em">${tr.id}</span>
            <span style="color:${lvlCol};font-size:0.75em">${lvlStr}</span>
            <span style="color:#778;font-size:0.75em">${etaStr}</span>
        </div>`;
    }).join('');
}

// ── ensureTransitRadar ────────────────────────────────────────────────────────
function ensureTransitRadar() {
    const detectPanel = document.getElementById('detectPanel');
    if (!detectPanel) return;

    let card = document.getElementById('transitRadarCard');
    if (!card) {
        card = document.createElement('div');
        card.id = 'transitRadarCard';
        card.style.cssText =
            'background:#060d15;border:1px solid rgba(0,255,80,0.15);border-radius:8px;' +
            'padding:8px 10px 6px;margin-bottom:8px;';

        card.innerHTML = `
            <div style="display:flex;justify-content:space-between;align-items:center;
                        margin-bottom:6px;">
                <span style="font-size:0.72em;font-weight:600;color:#2a4a3a;
                             letter-spacing:.07em;text-transform:uppercase;">
                    ◎ Transit Radar
                </span>
                <div style="display:flex;gap:4px;">
                    <button id="radarModeDefault" title="Default mode: current position only"
                        style="font-size:0.68em;padding:2px 8px;border-radius:4px;
                               background:rgba(0,255,80,0.18);border:1px solid rgba(0,255,80,0.4);
                               color:#7fffb0;cursor:pointer;">Default</button>
                    <button id="radarModeEnhanced" title="Enhanced mode: projects trajectory forward ${RADAR_PREDICT_HORIZON_S}s"
                        style="font-size:0.68em;padding:2px 8px;border-radius:4px;
                               background:transparent;border:1px solid rgba(0,255,80,0.2);
                               color:#3a5a4a;cursor:pointer;">Enhanced</button>
                </div>
            </div>
            <div id="upcomingTransitsList"
                 style="font-size:0.76em;color:#c8ffd0;margin-bottom:6px;min-height:18px;"></div>
            <canvas id="radarCanvas"
                    style="display:block;width:100%;aspect-ratio:1;border-radius:4px;
                           border:1px solid rgba(0,255,80,0.08);"></canvas>
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

    // mode buttons
    const btnDef = card.querySelector('#radarModeDefault');
    const btnEnh = card.querySelector('#radarModeEnhanced');
    function _applyMode(m) {
        _radarMode = m;
        if (m === 'default') {
            btnDef.style.background = 'rgba(0,255,80,0.18)';
            btnDef.style.color = '#7fffb0';
            btnEnh.style.background = 'transparent';
            btnEnh.style.color = '#3a5a4a';
        } else {
            btnEnh.style.background = 'rgba(0,255,80,0.18)';
            btnEnh.style.color = '#7fffb0';
            btnDef.style.background = 'transparent';
            btnDef.style.color = '#3a5a4a';
        }
    }
    btnDef.onclick = () => _applyMode('default');
    btnEnh.onclick = () => _applyMode('enhanced');
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


console.log('[Telescope] Module loaded');
