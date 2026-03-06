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
let currentZoom = 1.0;

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
const _PREVIEW_BACKOFF_MS = 30000; // 30s between retry attempts after stream failure
let isSimulating = false;
let simulationVideo = null;
let disconnectedPollCount = 0; // consecutive disconnected polls before stopping preview
let simulationFiles = []; // Track temporary simulation files

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

    // Status polling (always poll while panel is open)
    statusPollInterval = setInterval(updateStatus, 2000);

    // Auto-connect: check current state, then connect silently if not already connected
    updateStatus().then(() => {
        if (!isConnected) {
            connect();
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

    // 1-second local tick
    transitTickInterval = setInterval(() => {
        if (upcomingTransits.length > 0) {
            upcomingTransits.forEach(t => t.seconds_until--);
            updateTransitList();
            checkAutoCapture();
        }
        updateEclipseState();
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
        
        updateConnectionUI();
        updateStatus();
        startPreview();
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
        'refreshFilesBtn'
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
        if (justDisconnected) {
            console.warn('[Scope] Disconnected — prior connected state:', JSON.stringify(_lastConnectedStatus || {}));
            if (result.error) console.warn('[Scope] Server error:', result.error);
            // Immediately clear transit cards — nothing to capture with
            upcomingTransits = [];
            updateTransitList();
        }
        // Cache last connected response for diagnostics
        if (isConnected) {
            _lastConnectedStatus = { viewing_mode: result.viewing_mode, recording: result.recording, host: result.host };
        }
        _prevConnected = isConnected;

        if (isConnected && typeof startPreview === 'function') {
            disconnectedPollCount = 0;
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
        
        // Show lunar filter reminder
        showWarning(
            '✓ Remove solar filter if installed - Lunar viewing safe without filter',
            'info',
            10000
        );
    }
}

function checkTargetMismatch() {
    const banner = document.getElementById('mismatchBanner');
    const text   = document.getElementById('mismatchText');
    const btn    = document.getElementById('mismatchSwitchBtn');
    if (!banner || !text || !btn) return;

    // Only relevant when connected and in a known viewing mode
    if (!isConnected || !currentViewingMode) { banner.style.display = 'none'; return; }

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
// LIVE PREVIEW
// ============================================================================

function startPreview() {
    
    const previewImage = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot = document.getElementById('previewStatusDot');
    const previewStatusText = document.getElementById('previewStatusText');
    const previewTitleIcon = document.getElementById('previewTitleIcon');
    
    if (!previewImage) {
        console.error('[Telescope] Preview image element not found');
        return;
    }

    // Already streaming — don't restart
    if (previewImage.style.display === 'block' && previewImage.src) return;

    // Enforce backoff after a stream error
    if (_previewLastError && (Date.now() - _previewLastError) < _PREVIEW_BACKOFF_MS) {
        return;
    }
    
    // Set stream URL (adds timestamp to avoid caching)
    const streamUrl = `/telescope/preview/stream.mjpg?t=${Date.now()}`;
    
    previewImage.src = streamUrl;
    
    // Show image, hide placeholder
    previewImage.style.display = 'block';
    if (previewPlaceholder) {
        previewPlaceholder.style.display = 'none';
    }
    
    // Set status to connecting
    if (previewStatusDot) previewStatusDot.className = 'status-dot';
    if (previewStatusText) previewStatusText.textContent = 'Connecting...';
    if (previewTitleIcon) previewTitleIcon.textContent = '🟡';
    
    // After 2 seconds, assume stream is active (MJPEG streams don't trigger onload)
    setTimeout(() => {
        if (previewStatusDot) previewStatusDot.className = 'status-dot connected';
        if (previewStatusText) previewStatusText.textContent = 'Live Stream Active';
        if (previewTitleIcon) previewTitleIcon.textContent = '🟢';

        // Apply initial fit zoom once we know the image has loaded
        currentZoom = 1.0;
        applyZoom();
    }, 2000);
    
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
    currentZoom = 1.0;
    applyZoom();
}

function zoomFit() {
    currentZoom = 1.0;
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
    window.currentFiles = files.map(f => ({ path: f.url, name: f.name, thumbnail: f.thumbnail || null }));
    
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

async function deleteFile(url, filename) {
    console.log('[Telescope] deleteFile called:', url, filename);
    if (!confirm(`Delete ${filename}?`)) {
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
    // Also update inline status for non-modal context
    const statusBox = document.getElementById('statusMessage');
    if (statusBox) {
        statusBox.textContent = message;
        statusBox.className = `status-message ${type}`;
        statusBox.style.display = 'block';
    }
    if (autohide > 0) {
        setTimeout(() => {
            toast.style.display = 'none';
            if (statusBox) statusBox.style.display = 'none';
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

async function apiCall(endpoint, method = 'GET', body = null) {
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
            throw new Error(data.error || `HTTP ${response.status}`);
        }
        
        return data;
        
    } catch (error) {
        console.error(`[Telescope] API call failed: ${endpoint}`, error);
        showStatus(`Error: ${error.message}`, 'error');
        return null;
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
        return { stateClass: 'state-waiting', stateLabel: `Transit in ${timeStr}`, countdownText: timeStr, countdownClass: 'tc-big' };
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
                <div class="ta-state">${stateLabel}</div>
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
            if (stateEl) stateEl.textContent = stateLabel;
            // Update state class on card only when it changes (avoids reflow)
            if (card.dataset.transitState !== stateClass) {
                card.dataset.transitState = stateClass;
                card.className = `transit-alert ${probClass(transit)} ${stateClass}`;
            }
        }
    });

    // Remove cards for transits no longer in the list
    const activeIds = new Set(upcomingTransits.map(t => `ta-${t.flight.replace(/[^a-zA-Z0-9]/g, '_')}`));
    list.querySelectorAll('.transit-alert').forEach(card => {
        if (!activeIds.has(card.id)) card.remove();
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

    // Hide overlay after transit + post buffer
    setTimeout(() => {
        if (overlay) overlay.style.display = 'none';
        transitCaptureActive = false;
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
    
    if (files.length === 0) {
        filmstrip.innerHTML = '<p class="empty-state">No files captured yet</p>';
        return;
    }
    
    filmstrip.innerHTML = files.slice(0, 10).map(file => {
        const isTemp = file.isSimulation;
        const badge = isTemp ? '<span class="temp-badge">TEMP</span>' : '';
        const itemClass = isTemp ? 'filmstrip-item temp-file' : 'filmstrip-item';
        const isVideo = file.path.match(/\.(mp4|avi|mov)$/i);
        const thumbnail = file.thumbnail
            ? `<img src="${file.thumbnail}" alt="${file.name}" class="filmstrip-thumbnail">`
            : isVideo
                ? `<canvas class="filmstrip-thumbnail video-thumb-canvas" data-video-src="${file.path}"></canvas>`
                : `<img src="${file.path}" alt="${file.name}" class="filmstrip-thumbnail">`;
        
        return `
        <div class="${itemClass}" onclick="viewFile('${file.path}', '${file.name}')">
            ${badge}
            <div class="filmstrip-name" title="${file.name}">${file.name}</div>
            ${thumbnail}
            <div class="filmstrip-info">
                <div class="filmstrip-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download" ${isTemp ? 'disabled' : ''}>⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); deleteFile('${file.path}', '${file.name}')" title="${getFavorites().has(file.path) ? 'Remove favorite first' : 'Delete'}" ${isTemp || getFavorites().has(file.path) ? 'disabled' : ''}>🗑️</button>
                </div>
            </div>
        </div>
    `;
    }).join('');

    // Generate thumbnails from video first frame for any canvas placeholders
    filmstrip.querySelectorAll('canvas.video-thumb-canvas').forEach(generateVideoThumbnail);
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

async function gridDeleteSelected() {
    const paths = [...gridSelection.selected];
    const n = paths.length;
    if (n === 0) return;
    const favs = getFavorites();
    const protected_ = paths.filter(p => favs.has(p));
    const deletable = paths.filter(p => !favs.has(p));
    if (protected_.length > 0 && deletable.length === 0) {
        showStatus(`${protected_.length} file${protected_.length > 1 ? 's are' : ' is'} favorited — remove ❤️ first`, 'warning', 4000);
        return;
    }
    const msg = protected_.length > 0
        ? `Delete ${deletable.length} file${deletable.length > 1 ? 's' : ''}? (${protected_.length} favorited file${protected_.length > 1 ? 's' : ''} will be skipped)`
        : `Delete ${deletable.length} file${deletable.length > 1 ? 's' : ''}?`;
    if (!confirm(msg)) return;
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
        const thumbnail = file.thumbnail
            ? `<img src="${file.thumbnail}" alt="${file.name}" class="file-thumbnail">`
            : isVideo
                ? `<canvas class="file-thumbnail video-thumb-canvas" data-video-src="${file.path}"></canvas>`
                : `<img src="${file.path}" alt="${file.name}" class="file-thumbnail">`;
        return `
        <div class="file-item${sel}" data-file-path="${file.path}" data-file-idx="${idx}"
             onclick="gridSelectItem(${idx}, '${file.path}', event)">
            <div class="file-info">
                <span class="file-name" title="${file.name}">${file.name}</span>
                <div class="file-actions">
                    <button class="btn-icon btn-fav" data-fav-path="${file.path}" onclick="toggleFavorite('${file.path}', event)" title="${getFavorites().has(file.path) ? 'Unfavorite' : 'Favorite'}">${getFavorites().has(file.path) ? '❤️' : '🤍'}</button>
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download">⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); deleteFile('${file.path}', '${file.name}')" title="${getFavorites().has(file.path) ? 'Remove favorite first' : 'Delete'}" ${getFavorites().has(file.path) ? 'disabled' : ''}>🗑️</button>
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
    video.src = src;
    video.addEventListener('loadeddata', () => {
        video.currentTime = 0.5; // seek to 0.5s to avoid blank first frame
    });
    video.addEventListener('seeked', () => {
        canvas.width = video.videoWidth || 192;
        canvas.height = video.videoHeight || 108;
        canvas.getContext('2d').drawImage(video, 0, 0, canvas.width, canvas.height);
        video.src = ''; // free memory
    }, { once: true });
    video.addEventListener('error', () => {
        // Fallback: draw 🎬 emoji on canvas
        canvas.width = 192; canvas.height = 108;
        const ctx = canvas.getContext('2d');
        ctx.fillStyle = '#1a1a2e';
        ctx.fillRect(0, 0, 192, 108);
        ctx.font = '36px serif';
        ctx.textAlign = 'center';
        ctx.fillText('🎬', 96, 66);
    });
}

// Track viewer state for navigation
var _viewerIndex = -1;
var _loopSegment = null; // { start, end } for segment looping, or true for full loop

function viewFile(path, name, opts) {
    opts = opts || {};
    name = name || path.split('/').pop();
    const files = window.currentFiles || [];
    _viewerIndex = files.findIndex(f => f.path === path);

    const isVideo = /\.(mp4|avi|mov|mkv|webm)$/i.test(name);
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    const nameEl = document.getElementById('fileViewerName');
    const actionsEl = document.getElementById('fileViewerActions');

    nameEl.textContent = name;
    _setScanBanner(null); // clear any previous scan result
    _loopSegment = null;
    if (isVideo) {
        const loopAttr = opts.loop ? ' loop' : '';
        body.innerHTML =
            `<div style="position:relative; display:inline-block;">` +
            `<video src="${path}" controls autoplay playsinline${loopAttr} style="max-width:90vw; max-height:80vh;"></video>` +
            `<div id="videoPreciseTime" class="video-precise-time">0.00 / 0.00</div>` +
            `</div>`;
        const vid = body.querySelector('video');
        const timeEl = document.getElementById('videoPreciseTime');
        const updateTime = () => {
            const cur = (vid.currentTime || 0).toFixed(2);
            const dur = (vid.duration || 0).toFixed(2);
            timeEl.textContent = `${cur} / ${dur}`;
            // Segment loop: re-seek when past end
            if (_loopSegment && _loopSegment.start != null && vid.currentTime >= _loopSegment.end) {
                vid.currentTime = _loopSegment.start;
            }
        };
        vid.addEventListener('timeupdate', updateTime);
        vid.addEventListener('seeked', updateTime);
        vid.addEventListener('loadedmetadata', updateTime);
    } else {
        body.innerHTML = `<img src="${path}" alt="${name}" style="max-width:90vw; max-height:80vh; object-fit:contain;">`;
    }

    // Build action buttons (download, delete, prev/next, find transit)
    if (actionsEl) {
        const hasPrev = _viewerIndex > 0;
        const hasNext = _viewerIndex >= 0 && _viewerIndex < files.length - 1;
        const scanBtn = isVideo
            ? `<button class="btn-viewer" onmousedown="frameStepStart(-1)" onmouseup="frameStepStop()" onmouseleave="frameStepStop()" title="Back 1 frame (hold to repeat)">◁</button>` +
              `<button class="btn-viewer btn-viewer-scan" id="scanTransitBtn" onclick="scanTransit()" title="Analyze video for transits (OpenCV)">🎯 Find Transit</button>` +
              `<button class="btn-viewer" onmousedown="frameStepStart(1)" onmouseup="frameStepStop()" onmouseleave="frameStepStop()" title="Forward 1 frame (hold to repeat)">▷</button>`
            : '';
        // Show Replay button if an _analyzed.mp4 already exists for this file
        const hasAnalyzed = isVideo && !name.includes('_analyzed') && window.currentFiles &&
            window.currentFiles.some(f => f.path === path.replace('.mp4', '_analyzed.mp4'));
        const replayBtn = hasAnalyzed
            ? `<button class="btn-viewer" onclick="viewFile('${path.replace('.mp4', '_analyzed.mp4')}', '${name.replace('.mp4', '_analyzed.mp4')}', {})">🔄 Replay</button>`
            : '';
        const isFav = getFavorites().has(path);
        const favBtn = `<button class="btn-viewer" id="viewerFavBtn" onclick="toggleFavorite('${path}', event); _updateViewerFavState('${path}')" title="Favorite">${isFav ? '❤️' : '🤍'}</button>`;
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
    body.innerHTML = '';  // Stop video playback
    _setScanBanner(null);
    _viewerIndex = -1;
    _loopSegment = null;
}

var _frameStepTimer = null;

function frameStep(dir) {
    const vid = document.querySelector('#fileViewerBody video');
    if (!vid) return;
    vid.pause();
    vid.currentTime = Math.max(0, Math.min(vid.duration, vid.currentTime + dir / 30));
}

function frameStepStart(dir) {
    frameStepStop();
    frameStep(dir); // immediate first step
    let delay = 250; // initial repeat delay
    const repeat = () => {
        frameStep(dir);
        delay = Math.max(50, delay * 0.8); // accelerate
        _frameStepTimer = setTimeout(repeat, delay);
    };
    _frameStepTimer = setTimeout(repeat, delay);
}

function frameStepStop() {
    if (_frameStepTimer) { clearTimeout(_frameStepTimer); _frameStepTimer = null; }
}

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
    downloadFile(f.path, f.name);
}

async function viewerDelete(e) {
    const files = window.currentFiles || [];
    if (_viewerIndex < 0 || _viewerIndex >= files.length) return;
    const f = files[_viewerIndex];
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

async function scanTransit() {
    const files = window.currentFiles || [];
    if (_viewerIndex < 0 || _viewerIndex >= files.length) return;
    const f = files[_viewerIndex];
    const videoPath = f.path;
    if (!/\.(mp4|avi|mov|mkv|webm)$/i.test(f.name)) return;

    const btn = document.getElementById('scanTransitBtn');
    const playerVideo = document.querySelector('#fileViewerBody video');
    if (!playerVideo) return;

    if (btn) { btn.disabled = true; btn.textContent = '🔍 Analyzing…'; }
    // Show stop button
    _showStopAnalysisBtn(true);
    // Pulse the button text with frame count estimate
    let _analyzeTimer = setInterval(() => {
        if (btn && btn.disabled) {
            const dots = '.'.repeat(Math.floor(Date.now() / 500) % 4);
            btn.textContent = `🔍 Analyzing${dots}`;
        }
    }, 500);
    _setScanBanner(null);

    const apiPath = videoPath.replace(/^\/static\//, '');
    const controller = new AbortController();
    _analyzeController = controller;
    const timeoutId = setTimeout(() => controller.abort(), 5 * 60 * 1000);

    // Read tuning slider values (if panel exists)
    const sliderBody = {};
    const dtEl = document.getElementById('sliderDiffThreshold');
    const mbEl = document.getElementById('sliderMinBlob');
    const dmEl = document.getElementById('sliderDiskMargin');
    if (dtEl) sliderBody.diff_threshold = parseInt(dtEl.value);
    if (mbEl) sliderBody.min_blob_pixels = parseInt(mbEl.value);
    if (dmEl) sliderBody.disk_margin_pct = parseFloat(dmEl.value) / 100;

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
        if (btn) { btn.disabled = false; btn.textContent = '🎯 Find Transit'; }
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
    const annotatedFile = data.annotated_file;
    const annotatedPath = annotatedFile ? '/static/' + annotatedFile : null;
    const annotatedName = annotatedFile ? annotatedFile.split('/').pop() : '';

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
    if (staticCount > 0) summary += `<br>${staticCount} sunspot(s) filtered`;

    const panel = document.createElement('div');
    panel.id = 'analysisLegendPanel';
    panel.onclick = e => e.stopPropagation();
    panel.style.cssText = 'background:#1a1a1a; border-left:1px solid #333; padding:14px 16px; min-width:200px; max-width:240px; display:flex; flex-direction:column; gap:10px; font-size:0.85em; color:#ccc; overflow-y:auto; max-height:80vh;';

    // Result section
    const iconColor = events.length > 0 ? '#4dff88' : '#ffcc44';
    const icon = events.length > 0 ? '🎯' : '🔍';
    let html = `<div style="font-weight:bold; color:${iconColor}; font-size:1.05em;">${icon} ${summary}</div>`;

    // Legend items - vertical list
    html += `<div style="display:flex; flex-direction:column; gap:6px; padding:6px 0; border-top:1px solid #333;">`;
    html += `<div style="font-weight:bold; color:#aaa; font-size:0.9em; margin-bottom:2px;">Legend</div>`;
    html += `<div><span style="display:inline-block; width:12px; height:12px; border:3px solid #ff4444; border-radius:50%; margin-right:8px; vertical-align:middle;"></span>Transit detection</div>`;
    html += `<div><span style="display:inline-block; width:12px; height:12px; border:1px solid #888; border-radius:50%; margin-right:8px; vertical-align:middle;"></span>Sunspot (filtered)</div>`;
    html += `<div><span style="display:inline-block; width:12px; height:12px; border:2px solid #ffff00; border-radius:50%; margin-right:8px; vertical-align:middle;"></span>Disk boundary</div>`;
    html += `</div>`;

    // Buttons
    if (annotatedPath) {
        html += `<button class="btn-viewer" id="legendViewBtn" style="font-size:0.85em; padding:4px 10px; width:100%;">▶ View annotated</button>`;
    }

    // Tuning sliders
    html += `<div style="border-top:1px solid #333; padding-top:8px;">`;
    html += `<div style="font-weight:bold; color:#aaa; font-size:0.9em; margin-bottom:6px;">Detection Tuning</div>`;

    html += _sliderRow('sliderDiffThreshold', 'Sensitivity', 1, 30, 8,
        'Lower = more sensitive (detects fainter objects, more noise)');
    html += _sliderRow('sliderMinBlob', 'Min Blob Size', 1, 20, 3,
        'Minimum pixel area to count as a detection');
    html += _sliderRow('sliderDiskMargin', 'Edge Margin %', 0, 15, 5,
        'Percentage of disk edge to ignore (trims atmospheric distortion)');

    html += `<div style="display:flex; gap:6px; margin-top:6px;">`;
    html += `<button class="btn-viewer" id="legendReanalyzeBtn" style="font-size:0.85em; padding:4px 10px; flex:1;">🔄 Re-analyze</button>`;
    html += `<button class="btn-viewer" id="legendResetBtn" style="font-size:0.85em; padding:4px 10px;" title="Reset sliders to defaults">↩ Reset</button>`;
    html += `</div>`;
    html += `</div>`;

    panel.innerHTML = html;
    body.appendChild(panel);

    // Wire up button events (after innerHTML so elements exist)
    const viewBtn = document.getElementById('legendViewBtn');
    if (viewBtn) viewBtn.onclick = (e) => { e.stopPropagation(); viewFile(annotatedPath, annotatedName, {}); };
    const reBtn = document.getElementById('legendReanalyzeBtn');
    if (reBtn) reBtn.onclick = (e) => { e.stopPropagation(); scanTransit(); };
    const resetBtn = document.getElementById('legendResetBtn');
    if (resetBtn) resetBtn.onclick = (e) => {
        e.stopPropagation();
        _resetSliders();
    };

    // Hide the top banner (legend panel replaces it)
    _setScanBanner(null);
}

function _sliderRow(id, label, min, max, defaultVal, tooltip) {
    return `<div style="margin-bottom:6px;" title="${tooltip}">` +
        `<div style="display:flex; justify-content:space-between; font-size:0.85em;">` +
        `<span>${label}</span><span id="${id}Val">${defaultVal}</span></div>` +
        `<input type="range" id="${id}" min="${min}" max="${max}" value="${defaultVal}" ` +
        `data-default="${defaultVal}" ` +
        `style="width:100%; accent-color:#4dff88;" ` +
        `oninput="document.getElementById('${id}Val').textContent=this.value">` +
        `</div>`;
}

function _resetSliders() {
    ['sliderDiffThreshold', 'sliderMinBlob', 'sliderDiskMargin'].forEach(id => {
        const el = document.getElementById(id);
        if (el) {
            el.value = el.dataset.default;
            const valEl = document.getElementById(id + 'Val');
            if (valEl) valEl.textContent = el.dataset.default;
        }
    });
}

function _formatTimestamp(secs) {
    const m = Math.floor(secs / 60);
    const s = (secs % 60).toFixed(2);
    return m > 0 ? `${m}:${s.padStart(5, '0')}` : `${s}s`;
}

function _setScanBanner(type, text, onclick) {
    const el = document.getElementById('scanResultBanner');
    if (!el) return;
    if (!type) { el.style.display = 'none'; el.className = ''; el.onclick = null; return; }
    el.className = type === 'found' ? 'scan-found' : type === 'none' ? 'scan-none' : 'scan-error';
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
        const result = await apiCall('/telescope/detect/start', 'POST', {
            record_on_detect: true
        });
        if (result && !result.error) {
            isDetecting = true;
            showStatus('🎯 Transit detection started', 'success', 3000);
            // Start polling detection status
            if (!detectionPollInterval) {
                detectionPollInterval = setInterval(pollDetectionStatus, 2000);
            }
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

        updateDetectionUI();
    } catch (e) {
        // Silent — polling failure is transient; don't reset isDetecting
    }
}

function onTransitDetected(event) {
    const ts = new Date(event.timestamp).toLocaleTimeString();
    const flight = event.flight_info;
    let msg = `🎯 Transit detected at ${ts}`;
    if (flight) {
        msg += ` — ${flight.name} (${flight.aircraft_type}) ${flight.separation_deg}°`;
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

    const row = document.createElement('div');
    row.className = 'detect-event-row';
    row.innerHTML =
        `<span class="detect-event-time">${ts}</span>` +
        `<span class="detect-event-flight">${flightStr}</span>` +
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
            // Populate event log with recent events
            if (result.recent_events) {
                result.recent_events.forEach(e => appendDetectionEvent(e));
            }
        }
    } catch (e) {
        // Detection endpoint may not exist yet — ignore
    }
    updateDetectionUI();
}

// ============================================================================
// CLEANUP
// ============================================================================

window.destroyTelescope = function() {
    if (statusPollInterval)     { clearInterval(statusPollInterval);     statusPollInterval     = null; }
    if (visibilityPollInterval) { clearInterval(visibilityPollInterval); visibilityPollInterval = null; }
    if (lastUpdateInterval)     { clearInterval(lastUpdateInterval);     lastUpdateInterval     = null; }
    if (transitPollInterval)    { clearInterval(transitPollInterval);    transitPollInterval    = null; }
    if (transitTickInterval)    { clearInterval(transitTickInterval);    transitTickInterval    = null; }
    if (detectionPollInterval)  { clearInterval(detectionPollInterval);  detectionPollInterval  = null; }
};

console.log('[Telescope] Module loaded');
