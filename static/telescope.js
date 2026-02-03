/**
 * Telescope Control Frontend
 * Handles UI interactions and API calls for Seestar telescope control
 */

// State
let statusPollingInterval = null;
let lastUpdateDisplayInterval = null;
let isConnected = false;
let lastUpdateTime = null;

// DOM Elements
const elements = {
    // Connection
    connectBtn: document.getElementById('connectBtn'),
    disconnectBtn: document.getElementById('disconnectBtn'),
    statusDot: document.getElementById('statusDot'),
    connectionStatus: document.getElementById('connectionStatus'),
    connectionInfo: document.getElementById('connectionInfo'),


    // Files
    refreshFilesBtn: document.getElementById('refreshFilesBtn'),
    fileCount: document.getElementById('fileCount'),
    filesList: document.getElementById('filesList'),

    // Status Message
    statusMessage: document.getElementById('statusMessage'),

    // Last Update Status
    lastUpdateStatus: document.getElementById('lastUpdateStatus')
};

// API Functions

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
        console.error(`API call failed: ${endpoint}`, error);
        throw error;
    }
}

async function connectTelescope() {
    try {
        showMessage('Connecting to telescope...', 'info');
        const result = await apiCall('/telescope/connect', 'POST');
        showMessage(result.message, 'success');
        await updateStatus();
    } catch (error) {
        showMessage(`Connection failed: ${error.message}`, 'error');
    }
}

async function disconnectTelescope() {
    try {
        const result = await apiCall('/telescope/disconnect', 'POST');
        showMessage(result.message, 'success');
        await updateStatus();
    } catch (error) {
        showMessage(`Disconnect failed: ${error.message}`, 'error');
    }
}



async function refreshFiles() {
    try {
        const result = await apiCall('/telescope/files', 'GET');
        displayFiles(result);
        showMessage(`Loaded ${result.total_files} files from ${result.albums.length} albums`, 'success');
    } catch (error) {
        showMessage(`Failed to load files: ${error.message}`, 'error');
        elements.filesList.innerHTML = '<p class="files-empty">Failed to load files</p>';
    }
}

async function updateStatus() {
    try {
        const status = await apiCall('/telescope/status', 'GET');

        // Record update time
        lastUpdateTime = Date.now();

        // Update connection status
        isConnected = status.connected;
        elements.statusDot.className = 'status-dot' + (isConnected ? ' connected' : '');

        // Show mock mode indicator
        const mockIndicator = status.mock_mode ? ' [MOCK MODE]' : '';
        elements.connectionStatus.textContent = (isConnected ? 'Connected' : 'Disconnected') + mockIndicator;

        if (isConnected) {
            elements.connectionInfo.textContent = `${status.host}:${status.port}`;
        } else {
            elements.connectionInfo.textContent = status.mock_mode ? 'Mock telescope ready' : '';
        }

        // Update UI state
        updateUIState();

        // Update last update display
        updateLastUpdateDisplay();

    } catch (error) {
        console.error('Status update failed:', error);
        // Don't show error message for status polling failures
    }
}

// UI State Management

async function showSetupInstructions() {
    try {
        const target = await apiCall('/telescope/target', 'GET');

        const targetEmoji = target.target === 'sun' ? '☀️' : '🌙';
        const targetName = target.target === 'sun' ? 'Sun' : 'Moon';

        const message = `${targetEmoji} Please set up your Seestar to track the ${targetName}.\n\nFlymoon will automatically start/stop recording during transit events.`;

        alert(message);
    } catch (error) {
        console.error('Failed to get target info:', error);
    }
}

function updateUIState() {
    // Connection buttons
    elements.connectBtn.disabled = isConnected;
    elements.disconnectBtn.disabled = !isConnected;

    // Files button
    elements.refreshFilesBtn.disabled = !isConnected;
}

// Status Polling

function startStatusPolling() {
    // Initial update
    updateStatus();

    // Poll every 2 seconds
    statusPollingInterval = setInterval(updateStatus, 2000);

    // Update last update display every 15 seconds
    updateLastUpdateDisplay();
    lastUpdateDisplayInterval = setInterval(updateLastUpdateDisplay, 15000);
}

function stopStatusPolling() {
    if (statusPollingInterval) {
        clearInterval(statusPollingInterval);
        statusPollingInterval = null;
    }
    if (lastUpdateDisplayInterval) {
        clearInterval(lastUpdateDisplayInterval);
        lastUpdateDisplayInterval = null;
    }
}

function updateLastUpdateDisplay() {
    console.log('updateLastUpdateDisplay called, lastUpdateTime:', lastUpdateTime);

    if (!lastUpdateTime) {
        console.log('No lastUpdateTime, returning early');
        return;
    }

    const now = Date.now();
    const elapsedSeconds = Math.floor((now - lastUpdateTime) / 1000);
    const minutes = Math.floor(elapsedSeconds / 60);
    const seconds = elapsedSeconds % 60;

    const timeStr = `${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`;

    const updateDate = new Date(lastUpdateTime);
    const hours = String(updateDate.getHours()).padStart(2, '0');
    const mins = String(updateDate.getMinutes()).padStart(2, '0');
    const secs = String(updateDate.getSeconds()).padStart(2, '0');
    const timestampStr = `${hours}:${mins}:${secs}`;

    const displayText = `Time since last update ${timeStr} at ${timestampStr}`;
    console.log('Setting text to:', displayText);
    console.log('Element:', elements.lastUpdateStatus);

    elements.lastUpdateStatus.textContent = displayText;
}

// File Display

function displayFiles(data) {
    if (!data.albums || data.albums.length === 0) {
        elements.filesList.innerHTML = '<p class="files-empty">No files found</p>';
        elements.fileCount.textContent = '';
        return;
    }

    elements.fileCount.textContent = `${data.total_files} file(s)`;

    let html = '';
    data.albums.forEach(album => {
        html += `<div class="album">`;
        html += `<div class="album-name">📁 ${album.name}</div>`;

        if (album.files && album.files.length > 0) {
            album.files.forEach(file => {
                html += `<div class="file-item">`;
                html += `<a href="${file.url}" class="file-link" target="_blank">📹 ${file.name}</a>`;
                html += `</div>`;
            });
        } else {
            html += `<p class="files-empty">No files in this album</p>`;
        }

        html += `</div>`;
    });

    elements.filesList.innerHTML = html;
}

// Status Message

function showMessage(message, type = 'info') {
    elements.statusMessage.textContent = message;
    elements.statusMessage.className = `status-message ${type}`;

    // Auto-hide after 5 seconds
    setTimeout(() => {
        elements.statusMessage.style.display = 'none';
    }, 5000);
}

// Event Listeners

elements.connectBtn.addEventListener('click', connectTelescope);
elements.disconnectBtn.addEventListener('click', disconnectTelescope);
elements.refreshFilesBtn.addEventListener('click', refreshFiles);

// Page Lifecycle

window.addEventListener('load', () => {
    console.log('Telescope control interface loaded');
    startStatusPolling();
    showSetupInstructions();
});

window.addEventListener('beforeunload', () => {
    stopStatusPolling();
    if (lastUpdateDisplayInterval) {
        clearInterval(lastUpdateDisplayInterval);
    }
});
