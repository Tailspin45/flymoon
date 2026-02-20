/**
 * Telescope Control Interface - Frontend Logic
 * Manages connection, status polling, target selection, capture controls,
 * live preview, and file management for Seestar S50 telescope.
 */

// State Management
let isConnected = false;
let isRecording = false;
let statusPollInterval = null;
let visibilityPollInterval = null;
let lastUpdateInterval = null;
let transitPollInterval = null;
let transitCaptureActive = false;
let upcomingTransits = [];
let currentZoom = 1.0;
let zoomStep = 0.25;
let isSimulating = false;
let simulationVideo = null;
let simulationFiles = []; // Track temporary simulation files

// Initialize on page load
document.addEventListener('DOMContentLoaded', () => {
    console.log('[Telescope] Initializing interface');
    
    // Check initial status
    updateStatus();
    
    // Start polling for target visibility
    updateTargetVisibility();
    visibilityPollInterval = setInterval(updateTargetVisibility, 30000); // Every 30s
    
    // Update "last updated" timer
    lastUpdateInterval = setInterval(updateLastUpdateTime, 1000); // Every 1s
    
    // Load initial file list
    refreshFiles();
    
    // Start transit polling
    checkTransits();
    transitPollInterval = setInterval(checkTransits, 15000); // Every 15s
    
    // Load auto-capture preference
    const autoCapture = localStorage.getItem('autoCaptureTransits');
    if (autoCapture !== null) {
        document.getElementById('autoCaptureToggle').checked = autoCapture === 'true';
    }
});

// ============================================================================
// CONNECTION MANAGEMENT
// ============================================================================

async function connect() {
    console.log('[Telescope] Connecting...');
    showStatus('Connecting to telescope...', 'info');
    
    const result = await apiCall('/telescope/connect', 'POST');
    if (result && result.success) {
        isConnected = true;
        showStatus('Connected successfully!', 'success');
        
        // Start status polling
        if (statusPollInterval) clearInterval(statusPollInterval);
        statusPollInterval = setInterval(updateStatus, 2000); // Every 2s
        
        updateConnectionUI();
        updateStatus();
        startPreview();
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
    const result = await apiCall('/telescope/status', 'GET');
    if (result) {
        isConnected = result.connected || false;
        // Don't overwrite isRecording if we're actively recording locally
        // The status endpoint doesn't know about our RTSP recordings
        if (!isRecording) {
            isRecording = result.is_recording || false;
        }
        
        updateConnectionUI();
        updateRecordingUI();
        
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
        recordingStartTime = Date.now();
        updateRecordingUI();
        startRecordingTimer(duration);
        
        const mode = interval > 0 ? `timelapse (${interval}s interval)` : 'normal';
        showStatus(`Recording started (${duration}s ${mode})`, 'success', 5000);
    }
}

async function stopRecording() {
    console.log('[Telescope] Stopping recording');
    showStatus('Stopping recording...', 'info');
    
    // Handle simulation mode
    if (isSimulating) {
        simulateStopRecording();
        return;
    }
    
    const result = await apiCall('/telescope/recording/stop', 'POST');
    if (result && result.success) {
        isRecording = false;
        stopRecordingTimer();
        updateRecordingUI();
        showStatus('Recording stopped', 'success', 5000);
        
        // Refresh file list after a short delay
        setTimeout(refreshFiles, 2000);
    }
}

function startRecordingTimer(totalDuration) {
    const timerSpan = document.getElementById('recordingTimer');
    if (!timerSpan) return;
    
    // Update timer every 100ms
    recordingTimerInterval = setInterval(() => {
        if (!recordingStartTime) return;
        
        const elapsed = (Date.now() - recordingStartTime) / 1000;
        const remaining = Math.max(0, totalDuration - elapsed);
        
        timerSpan.textContent = `${elapsed.toFixed(1)}s / ${totalDuration}s`;
        
        // Auto-stop when duration reached
        if (remaining <= 0 && isRecording) {
            stopRecording();
        }
    }, 100);
}

function stopRecordingTimer() {
    if (recordingTimerInterval) {
        clearInterval(recordingTimerInterval);
        recordingTimerInterval = null;
    }
    recordingStartTime = null;
    
    const timerSpan = document.getElementById('recordingTimer');
    if (timerSpan) timerSpan.textContent = '';
}

function updateRecordingUI() {
    const startBtn = document.getElementById('startRecordingBtn');
    const stopBtn = document.getElementById('stopRecordingBtn');
    const recordingDot = document.getElementById('recordingDot');
    const recordingText = document.getElementById('recordingText');
    
    console.log('[Telescope] updateRecordingUI called, isRecording:', isRecording);
    console.log('[Telescope] Start button:', startBtn, 'Stop button:', stopBtn);
    
    if (isRecording) {
        if (startBtn) {
            startBtn.disabled = true;
            startBtn.style.display = 'none';
        }
        if (stopBtn) {
            stopBtn.disabled = false;
            stopBtn.style.display = 'inline-block';  // Use inline-block instead of block
            console.log('[Telescope] Stop button should now be visible');
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
    console.log('[Telescope] Starting preview stream');
    
    const previewImage = document.getElementById('previewImage');
    const previewPlaceholder = document.getElementById('previewPlaceholder');
    const previewStatusDot = document.getElementById('previewStatusDot');
    const previewStatusText = document.getElementById('previewStatusText');
    const previewTitleIcon = document.getElementById('previewTitleIcon');
    
    if (!previewImage) {
        console.error('[Telescope] Preview image element not found');
        return;
    }
    
    // Set stream URL (adds timestamp to avoid caching)
    const streamUrl = `/telescope/preview/stream.mjpg?t=${Date.now()}`;
    console.log('[Telescope] Loading stream from:', streamUrl);
    
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
        console.log('[Telescope] Stream assumed active');
        if (previewStatusDot) previewStatusDot.className = 'status-dot connected';
        if (previewStatusText) previewStatusText.textContent = 'Live Stream Active';
        if (previewTitleIcon) previewTitleIcon.textContent = '🟢';
        
        // Fit to window
        zoomFit();
    }, 2000);
    
    // Error handler
    previewImage.onerror = () => {
        console.error('[Telescope] Preview stream failed');
        if (previewStatusDot) previewStatusDot.className = 'status-dot disconnected';
        if (previewStatusText) previewStatusText.textContent = 'Stream Error';
        if (previewTitleIcon) previewTitleIcon.textContent = '🔴';
    };
}

function zoomIn() {
    currentZoom += zoomStep;
    if (currentZoom > 3.0) currentZoom = 3.0;
    applyZoom();
}

function zoomOut() {
    currentZoom -= zoomStep;
    if (currentZoom < 0.5) currentZoom = 0.5;
    applyZoom();
}

function zoomReset() {
    currentZoom = 1.0;
    applyZoom();
}

function zoomFit() {
    // Fit the image/video to container
    currentZoom = 1.0;
    const image = document.getElementById('previewImage');
    const video = document.getElementById('simulationVideo');
    const container = document.getElementById('previewContainer');
    
    if (!container) return;
    
    const element = (video && video.style.display !== 'none') ? video : image;
    if (!element) return;
    
    // Remove transform to reset
    element.classList.remove('zoomed');
    element.style.transform = '';
    element.style.width = '100%';
    element.style.height = 'auto';
    
    // Reset scroll
    container.scrollTop = 0;
    container.scrollLeft = 0;
    
    updateSlider();
}

function setZoom(value) {
    currentZoom = value / 100;
    applyZoom();
}

function updateSlider() {
    const slider = document.getElementById('zoomSlider');
    const percent = document.getElementById('zoomPercent');
    if (slider) slider.value = currentZoom * 100;
    if (percent) percent.textContent = Math.round(currentZoom * 100) + '%';
}

function applyZoom() {
    const image = document.getElementById('previewImage');
    const video = document.getElementById('simulationVideo');
    const container = document.getElementById('previewContainer');
    
    if (!container) return;
    
    // Determine which element is active (image or video)
    const element = (video && video.style.display !== 'none') ? video : image;
    if (!element) return;
    
    if (currentZoom === 1.0) {
        element.classList.remove('zoomed');
        element.style.transform = '';
        element.style.width = '100%';
        element.style.height = 'auto';
        container.scrollTop = 0;
        container.scrollLeft = 0;
        updateSlider();
        return;
    }
    
    // Store current scroll position as percentage
    const scrollXPercent = container.scrollLeft / (container.scrollWidth - container.clientWidth || 1);
    const scrollYPercent = container.scrollTop / (container.scrollHeight - container.clientHeight || 1);
    
    // Apply zoom using CSS transform
    element.classList.add('zoomed');
    element.style.transform = `scale(${currentZoom})`;
    element.style.transformOrigin = 'center center';
    element.style.width = '100%';
    element.style.height = 'auto';
    
    updateSlider();
    
    // Restore scroll position after zoom
    setTimeout(() => {
        container.scrollLeft = scrollXPercent * (container.scrollWidth - container.clientWidth);
        container.scrollTop = scrollYPercent * (container.scrollHeight - container.clientHeight);
    }, 10);
}

function centerPreview() {
    const container = document.getElementById('previewContainer');
    const image = document.getElementById('previewImage');
    
    if (!container || !image) return;
    
    // Wait a bit for image to render, then center
    setTimeout(() => {
        const scrollHeight = container.scrollHeight;
        const clientHeight = container.clientHeight;
        const centerPosition = (scrollHeight - clientHeight) / 2;
        
        container.scrollTop = centerPosition;
        console.log('[Telescope] Preview centered at', centerPosition);
    }, 500);
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
    
    const result = await apiCall('/telescope/files', 'GET');
    if (!result) return;
    
    const fileCount = document.getElementById('fileCount');
    const files = result.files || [];
    
    // Store globally for modal
    window.currentFiles = files.map(f => ({ path: f.url, name: f.name }));
    
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
    if (!confirm(`Delete ${filename}?`)) {
        return;
    }
    
    console.log('[Telescope] Deleting file:', filename);
    
    try {
        const result = await apiCall('/telescope/files/delete', 'POST', {
            path: url.replace('/static/', '')
        });
        
        if (result && result.success) {
            showStatus(`Deleted ${filename}`, 'success', 3000);
            refreshFiles();
        }
    } catch (error) {
        console.error('[Telescope] Delete failed:', error);
        showStatus(`Failed to delete ${filename}`, 'error', 5000);
    }
}

// ============================================================================
// UI HELPERS
// ============================================================================

function showStatus(message, type = 'info', autohide = 0) {
    const statusBox = document.getElementById('statusMessage');
    if (!statusBox) return;
    
    statusBox.textContent = message;
    statusBox.className = `status-message ${type}`;
    statusBox.style.display = 'block';
    
    // Auto-hide after timeout
    if (autohide > 0) {
        setTimeout(() => {
            statusBox.style.display = 'none';
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
    try {
        const response = await fetch('/telescope/transit/status');
        if (!response.ok) return;
        
        const data = await response.json();
        upcomingTransits = data.transits || [];
        
        updateTransitList();
        checkAutoCapture();
    } catch (error) {
        console.warn('[Telescope] Transit check failed:', error);
    }
}

function updateTransitList() {
    const list = document.getElementById('transitList');
    if (!list) return;
    
    if (upcomingTransits.length === 0) {
        list.innerHTML = '<p class="empty-state">No transits detected</p>';
        return;
    }
    
    list.innerHTML = upcomingTransits.map(transit => {
        const countdown = formatCountdown(transit.seconds_until);
        const probabilityClass = transit.probability.toLowerCase();
        
        return `
            <div class="transit-item ${probabilityClass}">
                <div class="transit-header">
                    <span class="transit-flight">✈️ ${transit.flight}</span>
                    <span class="transit-countdown">${countdown}</span>
                </div>
                <div class="transit-info">
                    ${transit.target} • ${transit.probability} probability<br>
                    Alt: ${transit.altitude}° • Az: ${transit.azimuth}°
                </div>
                <div class="transit-actions">
                    <button class="btn-transit primary" onclick="recordTransit('${transit.flight}', ${transit.seconds_until})">
                        📹 Record Now
                    </button>
                    <button class="btn-transit dismiss" onclick="dismissTransit('${transit.flight}')">✕</button>
                </div>
            </div>
        `;
    }).join('');
}

function formatCountdown(seconds) {
    if (seconds < 0) return 'PASSED';
    const mins = Math.floor(seconds / 60);
    const secs = seconds % 60;
    return `${mins}:${secs.toString().padStart(2, '0')}`;
}

function checkAutoCapture() {
    const autoCapture = document.getElementById('autoCaptureToggle').checked;
    if (!autoCapture || !isConnected || transitCaptureActive) return;
    
    // Check for imminent transits (within 15 seconds)
    const imminent = upcomingTransits.find(t => t.seconds_until <= 15 && t.seconds_until > 0);
    if (imminent) {
        console.log('[Telescope] Auto-capturing imminent transit:', imminent.flight);
        recordTransit(imminent.flight, imminent.seconds_until);
    }
}

async function recordTransit(flight, secondsUntil) {
    // Stop any current recording
    if (isRecording) {
        console.log('[Telescope] Interrupting current recording for transit');
        await stopRecording();
        await new Promise(resolve => setTimeout(resolve, 1000)); // Wait for stop
    }
    
    // Show overlay
    transitCaptureActive = true;
    const overlay = document.getElementById('transitOverlay');
    const overlayInfo = document.getElementById('transitOverlayInfo');
    if (overlay) {
        overlayInfo.textContent = `${flight} in ${secondsUntil}s`;
        overlay.style.display = 'flex';
    }
    
    // Calculate recording duration (pre + post buffers)
    const preBuffer = 10; // seconds before transit
    const postBuffer = 10; // seconds after transit
    const totalDuration = Math.max(secondsUntil - preBuffer, 0) + postBuffer;
    
    // Start recording
    document.getElementById('videoDuration').value = totalDuration;
    document.getElementById('frameInterval').value = 0; // Normal video
    await startRecording();
    
    // Hide overlay after transit passes
    setTimeout(() => {
        if (overlay) overlay.style.display = 'none';
        transitCaptureActive = false;
    }, (secondsUntil + postBuffer) * 1000);
}

function dismissTransit(flight) {
    upcomingTransits = upcomingTransits.filter(t => t.flight !== flight);
    updateTransitList();
}

// Save auto-capture preference
document.addEventListener('DOMContentLoaded', () => {
    const toggle = document.getElementById('autoCaptureToggle');
    if (toggle) {
        toggle.addEventListener('change', (e) => {
            localStorage.setItem('autoCaptureTransits', e.target.checked);
        });

    document.addEventListener('keydown', (e) => {
        if (e.key === 'Escape') closeFileViewer();
    });
    }
});

// ============================================================================
// FILMSTRIP & MODAL
// ============================================================================

function toggleFilesModal() {
    const modal = document.getElementById('filesModal');
    if (!modal) return;
    
    if (modal.style.display === 'none' || !modal.style.display) {
        modal.style.display = 'flex';
        updateFilesGrid();
    } else {
        modal.style.display = 'none';
    }
}

function updateFilmstrip(files) {
    const filmstrip = document.getElementById('filmstripList');
    if (!filmstrip) return;
    
    if (files.length === 0) {
        filmstrip.innerHTML = '<p class="empty-state">No files captured yet</p>';
        return;
    }
    
    filmstrip.innerHTML = files.slice(-10).reverse().map(file => {
        const isTemp = file.isSimulation;
        const badge = isTemp ? '<span class="temp-badge">TEMP</span>' : '';
        const itemClass = isTemp ? 'filmstrip-item temp-file' : 'filmstrip-item';
        const isVideo = file.path.match(/\.(mp4|avi|mov)$/i);
        const thumbnail = isVideo
            ? `<div class="filmstrip-thumbnail video-thumb">🎬</div>`
            : `<img src="${file.path}" alt="${file.name}" class="filmstrip-thumbnail">`;
        
        return `
        <div class="${itemClass}" onclick="viewFile('${file.path}')">
            ${badge}
            ${thumbnail}
            <div class="filmstrip-info">
                <span>${file.name.split('_')[0]}</span>
                <div class="filmstrip-actions">
                    <button class="btn-icon" onclick="event.stopPropagation(); downloadFile('${file.path}', '${file.name}')" title="Download" ${isTemp ? 'disabled' : ''}>⬇️</button>
                    <button class="btn-icon btn-danger" onclick="event.stopPropagation(); deleteFile('${file.path}', '${file.name}')" title="Delete" ${isTemp ? 'disabled' : ''}>🗑️</button>
                </div>
            </div>
        </div>
    `;
    }).join('');
}

function updateFilesGrid() {
    const grid = document.getElementById('filesGrid');
    if (!grid) return;
    
    // Get files from the global state (refreshed by refreshFiles)
    const files = window.currentFiles || [];
    
    if (files.length === 0) {
        grid.innerHTML = '<p class="empty-state">No files</p>';
        return;
    }
    
    grid.innerHTML = files.map(file => {
        const isVideo = file.path.match(/\.(mp4|avi|mov)$/i);
        const thumbnail = isVideo
            ? `<div class="file-thumbnail video-thumb" onclick="viewFile('${file.path}')">🎬</div>`
            : `<img src="${file.path}" alt="${file.name}" class="file-thumbnail" onclick="viewFile('${file.path}')">`;
        return `
        <div class="file-item">
            ${thumbnail}
            <div class="file-info">
                <span class="file-name" title="${file.name}">${file.name}</span>
                <div class="file-actions">
                    <button class="btn-icon" onclick="downloadFile('${file.path}', '${file.name}')" title="Download">⬇️</button>
                    <button class="btn-icon btn-danger" onclick="deleteFile('${file.path}', '${file.name}')" title="Delete">🗑️</button>
                </div>
            </div>
        </div>
    `}).join('');
}

function viewFile(path) {
    const name = path.split('/').pop();
    const isVideo = /\.(mp4|avi|mov|mkv)$/i.test(name);
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    const nameEl = document.getElementById('fileViewerName');

    nameEl.textContent = name;
    body.innerHTML = isVideo
        ? `<video src="${path}" controls autoplay style="max-width:90vw; max-height:80vh;"></video>`
        : `<img src="${path}" alt="${name}" style="max-width:90vw; max-height:80vh; object-fit:contain;">`;

    viewer.style.display = 'flex';
}

function closeFileViewer() {
    const viewer = document.getElementById('fileViewer');
    const body = document.getElementById('fileViewerBody');
    viewer.style.display = 'none';
    body.innerHTML = '';  // Stop video playback
}

// ============================================================================
// SIMULATION MODE
// ============================================================================

let simTransitInterval = null;   // drives countdown tick
let simRecBlinkInterval = null;  // drives REC blink
let simCycleTimeout = null;      // schedules next auto-cycle

const SIM_TRANSIT = {
    flight: 'SIM-001',
    target: 'Moon',
    probability: 'HIGH',
    altitude: 42.3,
    azimuth: 188.7,
};
const SIM_COUNTDOWN_START = 30; // seconds until simulated transit
const SIM_RECORD_DURATION = 12; // seconds of simulated recording

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

    clearTimeout(simCycleTimeout);
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

        // Show countdown overlay on video when ≤15s
        const overlay = document.getElementById('simCountdownOverlay');
        if (overlay) {
            if (remaining > 0 && remaining <= 15) {
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

/** Called at the moment of simulated transit */
function triggerSimTransit() {
    if (!isSimulating) return;
    console.log('[Sim] Transit triggered!');

    // Hide countdown overlay
    const countdown = document.getElementById('simCountdownOverlay');
    if (countdown) countdown.style.display = 'none';

    // Audio beep
    playSimBeep();

    // Plane fly-through animation
    animateSimPlane();

    // Shutter flash
    simCaptureFlash();

    // Start recording overlay + fake filmstrip entry
    startSimRecording();

    showStatus('🎯 TRANSIT IN PROGRESS — Recording!', 'success', SIM_RECORD_DURATION * 1000);

    // Stop recording after SIM_RECORD_DURATION seconds, then schedule next cycle
    simCycleTimeout = setTimeout(() => {
        if (!isSimulating) return;
        stopSimRecording();
        showStatus('✅ Sim transit complete. Next transit in 60s…', 'info', 8000);
        // Auto-cycle: schedule next transit in ~60s
        simCycleTimeout = setTimeout(() => {
            if (isSimulating) scheduleSimTransit(SIM_COUNTDOWN_START);
        }, 60000);
    }, SIM_RECORD_DURATION * 1000);
}

/** Blinking REC overlay + fake filmstrip entry */
function startSimRecording() {
    isRecording = true;
    recordingStartTime = Date.now();
    updateRecordingUI();
    startRecordingTimer(SIM_RECORD_DURATION);

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

    // Add fake recording to filmstrip
    const timestamp = new Date().toISOString().replace(/[:.]/g, '-').slice(0, -5);
    const tempFile = {
        name: `sim_transit_${timestamp}.mp4`,
        path: '/static/simulations/demo.mp4',
        url:  '/static/simulations/demo.mp4',
        isSimulation: true,
        timestamp: Date.now()
    };
    simulationFiles.push(tempFile);
    if (!window.currentFiles) window.currentFiles = [];
    window.currentFiles.unshift(tempFile);
    updateFilmstrip(window.currentFiles);
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
    const y = Math.round(h * 0.42); // slightly above centre like a real transit

    plane.style.display = 'block';
    plane.style.top = y + 'px';
    plane.style.left = '-40px';
    plane.style.transition = `left 2.4s linear`;

    // Force reflow so transition fires
    plane.getBoundingClientRect();
    plane.style.left = (w + 50) + 'px';

    setTimeout(() => {
        plane.style.display = 'none';
        plane.style.transition = '';
        plane.style.left = '-40px';
    }, 2500);
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
        simulationVideo.style.cssText = 'width:100%; height:auto; display:block; object-fit:contain;';
        simulationVideo.src = '/static/simulations/demo.mp4';
        if (previewContainer) previewContainer.appendChild(simulationVideo);
    } else {
        simulationVideo.style.display = 'block';
        simulationVideo.play();
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
    if (window.currentFiles) {
        window.currentFiles = window.currentFiles.filter(f => !f.isSimulation);
    }
    simulationFiles = [];
    updateFilmstrip(window.currentFiles || []);
}

// ============================================================================
// CLEANUP
// ============================================================================

window.addEventListener('beforeunload', () => {
    // Clean up intervals
    if (statusPollInterval) clearInterval(statusPollInterval);
    if (visibilityPollInterval) clearInterval(visibilityPollInterval);
    if (lastUpdateInterval) clearInterval(lastUpdateInterval);
});

console.log('[Telescope] Module loaded');
