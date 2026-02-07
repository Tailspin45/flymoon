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
    const statusDot = document.getElementById('connectionStatus');
    const statusText = document.getElementById('statusText');
    const connectBtn = document.getElementById('connectBtn');
    const disconnectBtn = document.getElementById('disconnectBtn');
    
    if (isConnected) {
        statusDot.className = 'status-dot connected';
        statusText.textContent = 'Connected';
        connectBtn.disabled = true;
        disconnectBtn.disabled = false;
    } else {
        statusDot.className = 'status-dot disconnected';
        statusText.textContent = 'Disconnected';
        connectBtn.disabled = false;
        disconnectBtn.disabled = true;
    }
    
    // Enable/disable all controls based on connection
    document.querySelectorAll('.requires-connection').forEach(btn => {
        btn.disabled = !isConnected;
    });
}

async function updateStatus() {
    const result = await apiCall('/telescope/status', 'GET');
    if (result) {
        isConnected = result.connected || false;
        isRecording = result.is_recording || false;
        
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
    const sunBadge = document.getElementById('sunVisibility');
    const sunAlt = document.getElementById('sunAlt');
    const sunAz = document.getElementById('sunAz');
    const sunBtn = document.getElementById('switchToSunBtn');
    
    if (result.sun) {
        sunAlt.textContent = `${result.sun.altitude.toFixed(1)}°`;
        sunAz.textContent = `${result.sun.azimuth.toFixed(1)}°`;
        
        if (result.sun.visible) {
            sunBadge.textContent = 'Visible';
            sunBadge.className = 'visibility-badge visible';
            if (isConnected) sunBtn.disabled = false;
        } else {
            sunBadge.textContent = 'Below Horizon';
            sunBadge.className = 'visibility-badge not-visible';
            sunBtn.disabled = true;
        }
    }
    
    // Update Moon
    const moonBadge = document.getElementById('moonVisibility');
    const moonAlt = document.getElementById('moonAlt');
    const moonAz = document.getElementById('moonAz');
    const moonBtn = document.getElementById('switchToMoonBtn');
    
    if (result.moon) {
        moonAlt.textContent = `${result.moon.altitude.toFixed(1)}°`;
        moonAz.textContent = `${result.moon.azimuth.toFixed(1)}°`;
        
        if (result.moon.visible) {
            moonBadge.textContent = 'Visible';
            moonBadge.className = 'visibility-badge visible';
            if (isConnected) moonBtn.disabled = false;
        } else {
            moonBadge.textContent = 'Below Horizon';
            moonBadge.className = 'visibility-badge not-visible';
            moonBtn.disabled = true;
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
    
    const exposureInput = document.getElementById('exposureTime');
    const exposureTime = parseFloat(exposureInput.value) || 1.0;
    
    showStatus(`Capturing photo (${exposureTime}s exposure)...`, 'info');
    
    const result = await apiCall('/telescope/capture/photo', 'POST', {
        exposure_time: exposureTime
    });
    
    if (result && result.success) {
        showStatus('Photo captured successfully!', 'success', 5000);
        
        // Refresh file list after a short delay
        setTimeout(refreshFiles, 2000);
    }
}

async function startRecording() {
    console.log('[Telescope] Starting recording');
    showStatus('Starting recording...', 'info');
    
    const result = await apiCall('/telescope/recording/start', 'POST');
    if (result && result.success) {
        isRecording = true;
        updateRecordingUI();
        showStatus('Recording started', 'success', 5000);
    }
}

async function stopRecording() {
    console.log('[Telescope] Stopping recording');
    showStatus('Stopping recording...', 'info');
    
    const result = await apiCall('/telescope/recording/stop', 'POST');
    if (result && result.success) {
        isRecording = false;
        updateRecordingUI();
        showStatus('Recording stopped', 'success', 5000);
        
        // Refresh file list after a short delay
        setTimeout(refreshFiles, 2000);
    }
}

function updateRecordingUI() {
    const startBtn = document.getElementById('startRecordingBtn');
    const stopBtn = document.getElementById('stopRecordingBtn');
    const recordingIndicator = document.getElementById('recordingIndicator');
    
    if (isRecording) {
        startBtn.disabled = true;
        stopBtn.disabled = false;
        if (recordingIndicator) {
            recordingIndicator.style.display = 'flex';
        }
    } else {
        startBtn.disabled = !isConnected;
        stopBtn.disabled = true;
        if (recordingIndicator) {
            recordingIndicator.style.display = 'none';
        }
    }
}

// ============================================================================
// LIVE PREVIEW
// ============================================================================

function startPreview() {
    console.log('[Telescope] Starting preview stream');
    
    const previewImage = document.getElementById('previewImage');
    const previewStatus = document.getElementById('previewStatus');
    
    if (!previewImage) return;
    
    // Set stream URL (adds timestamp to avoid caching)
    previewImage.src = `/telescope/preview/stream.mjpg?t=${Date.now()}`;
    
    previewImage.onload = () => {
        console.log('[Telescope] Preview stream loaded');
        if (previewStatus) {
            previewStatus.textContent = 'Live Stream Active';
            previewStatus.className = 'preview-status active';
        }
    };
    
    previewImage.onerror = () => {
        console.error('[Telescope] Preview stream failed');
        if (previewStatus) {
            previewStatus.textContent = 'Preview Unavailable (Check RTSP/FFmpeg)';
            previewStatus.className = 'preview-status error';
        }
    };
}

function stopPreview() {
    console.log('[Telescope] Stopping preview stream');
    
    const previewImage = document.getElementById('previewImage');
    const previewStatus = document.getElementById('previewStatus');
    
    if (previewImage) {
        previewImage.src = '';
    }
    
    if (previewStatus) {
        previewStatus.textContent = 'Not Connected';
        previewStatus.className = 'preview-status';
    }
}

// ============================================================================
// FILE MANAGEMENT
// ============================================================================

async function refreshFiles() {
    console.log('[Telescope] Refreshing file list');
    
    const result = await apiCall('/telescope/files', 'GET');
    if (!result) return;
    
    const filesGrid = document.getElementById('filesGrid');
    const fileCount = document.getElementById('fileCount');
    
    if (!filesGrid) return;
    
    // Clear existing files
    filesGrid.innerHTML = '';
    
    const files = result.files || [];
    
    // Update count badge
    if (fileCount) {
        fileCount.textContent = files.length;
    }
    
    if (files.length === 0) {
        filesGrid.innerHTML = '<div class="no-files">No files captured yet</div>';
        return;
    }
    
    // Display files
    files.forEach(file => {
        const fileItem = document.createElement('div');
        fileItem.className = 'file-item';
        fileItem.onclick = () => window.open(file.url, '_blank');
        
        // Determine icon based on file type
        let icon = '📄';
        if (file.name.match(/\.(mp4|avi|mov)$/i)) {
            icon = '📹';
        } else if (file.name.match(/\.(jpg|jpeg|png|gif|bmp|tiff)$/i)) {
            icon = '📷';
        }
        
        fileItem.innerHTML = `
            <div class="file-icon">${icon}</div>
            <div class="file-name">${file.name}</div>
        `;
        
        filesGrid.appendChild(fileItem);
    });
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
// CLEANUP
// ============================================================================

window.addEventListener('beforeunload', () => {
    // Clean up intervals
    if (statusPollInterval) clearInterval(statusPollInterval);
    if (visibilityPollInterval) clearInterval(visibilityPollInterval);
    if (lastUpdateInterval) clearInterval(lastUpdateInterval);
});

console.log('[Telescope] Module loaded');
