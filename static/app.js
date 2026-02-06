// Helper function to convert true heading to magnetic heading
function trueToMagnetic(trueHeading, latitude, longitude) {
    if (typeof geomag === 'undefined') {
        console.warn('geomag library not loaded, returning true heading');
        return trueHeading;
    }
    try {
        const geomagInfo = geomag.field(latitude, longitude);
        const declination = geomagInfo.declination; // Negative for west, positive for east
        let magneticHeading = trueHeading - declination;
        // Normalize to 0-360
        if (magneticHeading < 0) magneticHeading += 360;
        if (magneticHeading >= 360) magneticHeading -= 360;
        return magneticHeading;
    } catch (error) {
        console.error('Error calculating magnetic heading:', error);
        return trueHeading;
    }
}

const COLUMN_NAMES = [
    "id",
    "aircraft_type",
    "origin",
    "destination",
    "target_alt",
    "plane_alt",
    "alt_diff",
    "target_az",
    "plane_az",
    "az_diff",
    "elevation_change",
    "aircraft_elevation_feet",
    "direction",
    "distance_nm",
    "speed",
];
const MS_IN_A_MIN = 60000;
// Possibility levels
const LOW_LEVEL = 1, MEDIUM_LEVEL = 2, HIGH_LEVEL = 3;

// Get minimum altitude based on azimuth (in degrees)
// Azimuth: 0° = N, 90° = E, 180° = S, 270° = W
function getMinAltitudeForAzimuth(azimuth) {
    // Default values if not set
    const defaultMinAlt = 15;

    const minAltNEl = document.getElementById("minAltN");
    const minAltEEl = document.getElementById("minAltE");
    const minAltSEl = document.getElementById("minAltS");
    const minAltWEl = document.getElementById("minAltW");

    // If elements don't exist, return default
    if (!minAltNEl || !minAltEEl || !minAltSEl || !minAltWEl) {
        return defaultMinAlt;
    }

    const minAltN = parseFloat(minAltNEl.value) || defaultMinAlt;
    const minAltE = parseFloat(minAltEEl.value) || defaultMinAlt;
    const minAltS = parseFloat(minAltSEl.value) || defaultMinAlt;
    const minAltW = parseFloat(minAltWEl.value) || defaultMinAlt;

    if (azimuth === null || azimuth === undefined || isNaN(azimuth)) {
        // If no azimuth, use the minimum of all quadrants
        return Math.min(minAltN, minAltE, minAltS, minAltW);
    }

    // Normalize azimuth to 0-360
    azimuth = ((azimuth % 360) + 360) % 360;

    // Determine quadrant
    if (azimuth >= 315 || azimuth < 45) {
        return minAltN;  // North: 315° to 45°
    } else if (azimuth >= 45 && azimuth < 135) {
        return minAltE;  // East: 45° to 135°
    } else if (azimuth >= 135 && azimuth < 225) {
        return minAltS;  // South: 135° to 225°
    } else {
        return minAltW;  // West: 225° to 315°
    }
}

// Get minimum of all quadrant min altitudes
function getMinAltitudeAllQuadrants() {
    const defaultMinAlt = 15;

    const minAltNEl = document.getElementById("minAltN");
    const minAltEEl = document.getElementById("minAltE");
    const minAltSEl = document.getElementById("minAltS");
    const minAltWEl = document.getElementById("minAltW");

    // If elements don't exist, return default
    if (!minAltNEl || !minAltEEl || !minAltSEl || !minAltWEl) {
        return defaultMinAlt;
    }

    const minAltN = parseFloat(minAltNEl.value) || defaultMinAlt;
    const minAltE = parseFloat(minAltEEl.value) || defaultMinAlt;
    const minAltS = parseFloat(minAltSEl.value) || defaultMinAlt;
    const minAltW = parseFloat(minAltWEl.value) || defaultMinAlt;
    return Math.min(minAltN, minAltE, minAltS, minAltW);
}
var autoMode = false;
var alertsEnabled = localStorage.getItem('alertsEnabled') === 'true' || false;
// Transit countdown tracking
var nextTransit = null;
var transitCountdownInterval = null;
var target = getLocalStorageItem("target", "auto");
var autoGoInterval = setInterval(goFetch, 86400000);
var refreshTimerLabelInterval = setInterval(refreshTimer, 1000); // Update every second
var remainingSeconds = 0; // Track remaining seconds for countdown
// By default disable auto go and refresh timer label
clearInterval(autoGoInterval);
clearInterval(refreshTimerLabelInterval);
displayTarget();

// App configuration from server
var appConfig = {
    autoRefreshIntervalMinutes: 6  // Default, will be loaded from server
};

// Load configuration from server
fetch('/config')
    .then(response => response.json())
    .then(config => {
        appConfig = config;
        console.log('Loaded config:', appConfig);
    })
    .catch(error => {
        console.error('Error loading config:', error);
    });

// Page visibility detection - pause polling when page is hidden
document.addEventListener('visibilitychange', function() {
    if (document.hidden && autoMode) {
        console.log('Page hidden - pausing auto-refresh');
        clearInterval(autoGoInterval);
        clearInterval(refreshTimerLabelInterval);
    } else if (!document.hidden && autoMode) {
        console.log('Page visible - resuming auto-refresh');
        const freq = parseInt(localStorage.getItem("frequency")) || appConfig.autoRefreshIntervalMinutes;
        autoGoInterval = setInterval(goFetch, MS_IN_A_MIN * freq);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000); // Update every second
    }
});

// State tracking for toggles
var resultsVisible = false;
var mapVisible = false;

// Track mode state
var trackingFlightId = null;
var trackingInterval = null;
var trackingTimeout = null;
const TRACK_INTERVAL_MS = 6000;  // 6 seconds (max 10 queries/min on Personal tier)
const TRACK_TIMEOUT_MS = 180000; // 3 minutes

// Audio context for track mode sounds
let audioCtx = null;

// Auto-save quadrant min altitude values to localStorage when they change
function setupStickyQuadrantInputs() {
    const quadrantIds = ['minAltN', 'minAltE', 'minAltS', 'minAltW'];

    quadrantIds.forEach(id => {
        const input = document.getElementById(id);
        if (input) {
            let lastSavedValue = input.value;

            input.addEventListener('change', function() {
                const value = parseFloat(this.value);
                if (!isNaN(value) && value !== lastSavedValue) {
                    localStorage.setItem(id, value);
                    lastSavedValue = value;
                    console.log(`Saved ${id}: ${value}`);

                    // Auto-refresh if results are visible (debounced to avoid duplicate calls)
                    if (resultsVisible) {
                        console.log('Auto-refreshing flight data due to min altitude change');
                        fetchFlights();
                    }
                }
            });

            // Save on blur without triggering refresh (change event already did that)
            input.addEventListener('blur', function() {
                const value = parseFloat(this.value);
                if (!isNaN(value)) {
                    localStorage.setItem(id, value);
                }
            });
        }
    });
}

// Initialize sticky inputs when DOM is ready
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', setupStickyQuadrantInputs);
} else {
    setupStickyQuadrantInputs();
}

// Reset all quadrant min altitude values to 0
function resetQuadrantValues() {
    const quadrantIds = ['minAltN', 'minAltE', 'minAltS', 'minAltW'];

    quadrantIds.forEach(id => {
        const input = document.getElementById(id);
        if (input) {
            input.value = '0';
            localStorage.setItem(id, '0');
        }
    });

    console.log('All quadrant values reset to 0');
}

function playTrackOnSound() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const now = audioCtx.currentTime;

    // First tone: 400 Hz, 100ms
    const osc1 = audioCtx.createOscillator();
    const gain1 = audioCtx.createGain();
    osc1.frequency.value = 400;
    osc1.connect(gain1);
    gain1.connect(audioCtx.destination);
    gain1.gain.setValueAtTime(0.3, now);
    gain1.gain.exponentialRampToValueAtTime(0.01, now + 0.1);
    osc1.start(now);
    osc1.stop(now + 0.1);

    // Second tone: 700 Hz, 100ms (after 140ms pause)
    const osc2 = audioCtx.createOscillator();
    const gain2 = audioCtx.createGain();
    osc2.frequency.value = 700;
    osc2.connect(gain2);
    gain2.connect(audioCtx.destination);
    gain2.gain.setValueAtTime(0.3, now + 0.14);
    gain2.gain.exponentialRampToValueAtTime(0.01, now + 0.24);
    osc2.start(now + 0.14);
    osc2.stop(now + 0.24);
}

function playTrackOffSound() {
    if (!audioCtx) audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const now = audioCtx.currentTime;

    // Single tone: 380 Hz, 120ms, quieter
    const osc = audioCtx.createOscillator();
    const gain = audioCtx.createGain();
    osc.frequency.value = 380;
    osc.connect(gain);
    gain.connect(audioCtx.destination);
    gain.gain.setValueAtTime(0.2, now);
    gain.gain.exponentialRampToValueAtTime(0.01, now + 0.12);
    osc.start(now);
    osc.stop(now + 0.12);
}

function updateTrackedFlight() {
    if (!trackingFlightId) return;

    let latitude = document.getElementById("latitude").value;
    let longitude = document.getElementById("longitude").value;
    let elevation = document.getElementById("elevation").value;
    const minAltitude = getMinAltitudeAllQuadrants();

    let endpoint_url = (
        `/flights?target=${encodeURIComponent(target)}`
        + `&latitude=${encodeURIComponent(latitude)}`
        + `&longitude=${encodeURIComponent(longitude)}`
        + `&elevation=${encodeURIComponent(elevation)}`
        + `&min_altitude=${encodeURIComponent(minAltitude)}`
        + `&send-notification=true`
    );

    if (window.lastBoundingBox) {
        endpoint_url += `&bbox_lat_ll=${encodeURIComponent(window.lastBoundingBox.latLowerLeft)}`;
        endpoint_url += `&bbox_lon_ll=${encodeURIComponent(window.lastBoundingBox.lonLowerLeft)}`;
        endpoint_url += `&bbox_lat_ur=${encodeURIComponent(window.lastBoundingBox.latUpperRight)}`;
        endpoint_url += `&bbox_lon_ur=${encodeURIComponent(window.lastBoundingBox.lonUpperRight)}`;
    }

    fetch(endpoint_url)
    .then(response => response.json())
    .then(data => {
        // Find the tracked flight in the response
        const trackedFlight = data.flights.find(f =>
            String(f.id).trim().toUpperCase() === trackingFlightId
        );

        if (!trackedFlight) {
            console.log(`Track mode: flight ${trackingFlightId} no longer in range`);
            stopTracking();
            return;
        }

        // Update only the tracked flight's row
        const row = document.querySelector(`tr[data-flight-id="${trackingFlightId}"]`);
        if (row) {
            updateFlightRow(row, trackedFlight);
        }

        // Update the marker on the map
        if (typeof updateSingleAircraftMarker === 'function') {
            updateSingleAircraftMarker(trackedFlight);
        }
    })
    .catch(error => {
        console.error('Track mode update error:', error);
    });
}

function updateFlightRow(row, flight) {
    // Update all cells except the first (target emoji)
    const cells = row.querySelectorAll('td');
    let cellIndex = 1; // Skip target emoji column

    COLUMN_NAMES.forEach(column => {
        const cell = cells[cellIndex++];
        if (!cell) return;

        const value = flight[column];

        if (value === null || value === undefined) {
            cell.textContent = "";
        } else if (column === "id") {
            cell.textContent = value;
        } else if (column === "aircraft_type") {
            cell.textContent = value === "N/A" ? "" : value;
        } else if (column === "aircraft_elevation_feet") {
            const altitude = Math.round(value);
            if (altitude > 18000) {
                const flightLevel = Math.round(altitude / 100);
                cell.textContent = `FL${flightLevel}`;
            } else {
                cell.textContent = altitude.toLocaleString('en-US');
            }
        } else if (column === "distance_nm") {
            cell.textContent = value.toFixed(1);
        } else if (column === "direction") {
            cell.textContent = Math.round(value) + "°";
        } else if (column === "alt_diff" || column === "az_diff") {
            const roundedValue = Math.round(value);
            cell.textContent = roundedValue + "º";
            cell.style.color = Math.abs(roundedValue) > 10 ? "#888" : "";
        } else if (column === "target_alt" || column === "target_az") {
            const numValue = value.toFixed(1);
            cell.textContent = numValue + "º";
            if (value < 0) {
                cell.style.color = "#888";
                cell.style.fontStyle = "italic";
            } else {
                cell.style.color = "";
                cell.style.fontStyle = "";
            }
        } else if (column === "plane_alt" || column === "plane_az") {
            const numValue = value.toFixed(1);
            cell.textContent = numValue + "º";
            if (value < 0) {
                cell.style.color = "#888";
                cell.style.fontStyle = "italic";
            } else {
                cell.style.color = "";
                cell.style.fontStyle = "";
            }
        } else if (column === "angular_separation") {
            cell.textContent = value.toFixed(2) + "º";
        } else {
            cell.textContent = value;
        }
    });
}

function startTracking(flightId) {
    // Stop any existing tracking
    stopTracking();

    trackingFlightId = flightId;
    console.log(`Track mode: started for ${flightId}`);
    playTrackOnSound();

    // Visual indicator
    updateTrackingIndicator();

    // Start polling - use updateTrackedFlight instead of fetchFlights
    trackingInterval = setInterval(updateTrackedFlight, TRACK_INTERVAL_MS);

    // Auto-stop after 3 minutes
    trackingTimeout = setTimeout(() => {
        console.log('Track mode: 3 minute timeout');
        stopTracking();
    }, TRACK_TIMEOUT_MS);

    // Immediate update
    updateTrackedFlight();
}

function stopTracking() {
    if (trackingInterval) {
        clearInterval(trackingInterval);
        trackingInterval = null;
    }
    if (trackingTimeout) {
        clearTimeout(trackingTimeout);
        trackingTimeout = null;
    }
    if (trackingFlightId) {
        console.log(`Track mode: stopped for ${trackingFlightId}`);
        trackingFlightId = null;
        playTrackOffSound();
    }
    updateTrackingIndicator();
}

function updateTrackingIndicator() {
    // Remove previous tracking highlight
    document.querySelectorAll('.tracking-row').forEach(row => {
        row.classList.remove('tracking-row');
    });

    // Add highlight to tracked row
    if (trackingFlightId) {
        const row = document.querySelector(`tr[data-flight-id="${trackingFlightId}"]`);
        if (row) {
            row.classList.add('tracking-row');
        }
        document.getElementById("trackingStatus").innerHTML += ` | 🎯 Tracking ${trackingFlightId}`;
    }
}


function savePosition() {
    let lat = document.getElementById("latitude");
    let latitude = parseFloat(lat.value);
    let long = document.getElementById("longitude");
    let longitude = parseFloat(long.value);
    let elev = document.getElementById("elevation");
    let elevation = parseFloat(elev.value);

    if(isNaN(latitude) || isNaN(longitude) || isNaN(elevation)) {
        alert("Please, type all your coordinates. Use MAPS.ie or Google Earth");
        return;
    }

    localStorage.setItem("latitude", latitude);
    localStorage.setItem("longitude", longitude);
    localStorage.setItem("elevation", elevation);

    // Save transit criteria thresholds
    const altThreshold = parseFloat(document.getElementById("altThreshold").value) || 5.0;
    const azThreshold = parseFloat(document.getElementById("azThreshold").value) || 10.0;
    localStorage.setItem("altThreshold", altThreshold);
    localStorage.setItem("azThreshold", azThreshold);

    // Save quadrant min altitudes
    const minAltN = parseFloat(document.getElementById("minAltN").value) || 15;
    const minAltE = parseFloat(document.getElementById("minAltE").value) || 15;
    const minAltS = parseFloat(document.getElementById("minAltS").value) || 15;
    const minAltW = parseFloat(document.getElementById("minAltW").value) || 15;
    localStorage.setItem("minAltN", minAltN);
    localStorage.setItem("minAltE", minAltE);
    localStorage.setItem("minAltS", minAltS);
    localStorage.setItem("minAltW", minAltW);

    // Save bounding box if user has edited it
    if (window.lastBoundingBox) {
        localStorage.setItem("boundingBox", JSON.stringify(window.lastBoundingBox));
    }

    alert("Position saved in local storage!");
}

function loadPosition() {
    const savedLat = localStorage.getItem("latitude");
    const savedLon = localStorage.getItem("longitude");
    const savedElev = localStorage.getItem("elevation");
    const savedBoundingBox = localStorage.getItem("boundingBox");

    // Load quadrant min altitudes (try new format first, fall back to old single minAltitude)
    const savedMinAltN = localStorage.getItem("minAltN");
    const savedMinAltE = localStorage.getItem("minAltE");
    const savedMinAltS = localStorage.getItem("minAltS");
    const savedMinAltW = localStorage.getItem("minAltW");
    const oldMinAlt = localStorage.getItem("minAltitude"); // Legacy support

    // Get quadrant input elements with null checks
    const minAltNEl = document.getElementById("minAltN");
    const minAltEEl = document.getElementById("minAltE");
    const minAltSEl = document.getElementById("minAltS");
    const minAltWEl = document.getElementById("minAltW");

    if (savedLat === null || savedLat === "" || savedLat === "null") {
        console.log("No position saved in local storage");
        // Set default values only if elements exist
        if (minAltNEl) minAltNEl.value = 30;
        if (minAltEEl) minAltEEl.value = 30;
        if (minAltSEl) minAltSEl.value = 30;
        if (minAltWEl) minAltWEl.value = 30;
        return;
    }

    document.getElementById("latitude").value = savedLat;
    document.getElementById("longitude").value = savedLon;
    document.getElementById("elevation").value = savedElev;

    // Load transit criteria thresholds
    const savedAltThreshold = localStorage.getItem("altThreshold");
    const savedAzThreshold = localStorage.getItem("azThreshold");
    const altThresholdEl = document.getElementById("altThreshold");
    const azThresholdEl = document.getElementById("azThreshold");
    if (altThresholdEl) {
        altThresholdEl.value = savedAltThreshold !== null ? savedAltThreshold : 5.0;
    }
    if (azThresholdEl) {
        azThresholdEl.value = savedAzThreshold !== null ? savedAzThreshold : 10.0;
    }

    // Load quadrant values or use legacy single value (only if elements exist)
    if (minAltNEl) {
        minAltNEl.value = (savedMinAltN !== null) ? savedMinAltN : (oldMinAlt || 30);
    }
    if (minAltEEl) {
        minAltEEl.value = (savedMinAltE !== null) ? savedMinAltE : (oldMinAlt || 30);
    }
    if (minAltSEl) {
        minAltSEl.value = (savedMinAltS !== null) ? savedMinAltS : (oldMinAlt || 30);
    }
    if (minAltWEl) {
        minAltWEl.value = (savedMinAltW !== null) ? savedMinAltW : (oldMinAlt || 30);
    }

    // Load saved bounding box
    if (savedBoundingBox) {
        try {
            window.lastBoundingBox = JSON.parse(savedBoundingBox);
            console.log("Bounding box loaded from local storage:", window.lastBoundingBox);
        } catch (e) {
            console.error("Error parsing saved bounding box:", e);
        }
    }

    console.log("Position loaded from local storage:", savedLat, savedLon, savedElev);
}

function getLocalStorageItem(key, defaultValue) {
    const value = localStorage.getItem(key);
    return value !== null ? value : defaultValue;
}

function updateTransitCountdown() {
    const countdownDiv = document.getElementById('transitCountdown');

    if (!nextTransit || !countdownDiv) {
        if (countdownDiv) countdownDiv.style.display = 'none';
        return;
    }

    const now = Date.now();
    const remainingMs = nextTransit.targetTime - now;

    if (remainingMs <= 0) {
        countdownDiv.style.display = 'none';
        nextTransit = null;
        return;
    }

    const remainingSeconds = Math.floor(remainingMs / 1000);
    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    const timeStr = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');

    // Style based on priority level
    const isHigh = nextTransit.level === HIGH_LEVEL;
    const bgColor = isHigh ? '#dc3545' : '#fd7e14';  // red for HIGH, orange for MEDIUM
    const levelText = isHigh ? 'High' : 'Medium';

    countdownDiv.style.backgroundColor = bgColor;
    countdownDiv.style.color = 'white';
    countdownDiv.style.display = 'block';
    countdownDiv.innerHTML = `${levelText} probability transit in ${timeStr}`;
}

function clearPosition() {
    localStorage.clear();

    document.getElementById("latitude").value = "";
    document.getElementById("longitude").value = "";
    document.getElementById("elevation").value = "";

    const minAltNEl = document.getElementById("minAltN");
    const minAltEEl = document.getElementById("minAltE");
    const minAltSEl = document.getElementById("minAltS");
    const minAltWEl = document.getElementById("minAltW");

    if (minAltNEl) minAltNEl.value = "30";
    if (minAltEEl) minAltEEl.value = "30";
    if (minAltSEl) minAltSEl.value = "30";
    if (minAltWEl) minAltWEl.value = "30";
}

function go() {
    // Refresh flight data
    const resultsDiv = document.getElementById("results");
    const mapContainer = document.getElementById("mapContainer");

    // Reset map interaction flag on manual refresh
    if (typeof userInteractingWithMap !== 'undefined') {
        userInteractingWithMap = false;
    }

    // Validate coordinates first
    let lat = document.getElementById("latitude");
    let latitude = parseFloat(lat.value);

    if(isNaN(latitude)) {
        alert("Please, type your coordinates and save them");
        return;
    }

    // Show results and map if not already visible
    if (!resultsVisible) {
        resultsVisible = true;
        mapVisible = true;
        resultsDiv.style.display = 'block';
        mapContainer.style.display = 'block';
    }

    // Always fetch fresh data
    fetchFlights();
}

function goFetch() {
    // Internal function for auto mode - just fetches without toggling
    let lat = document.getElementById("latitude");
    let latitude = parseFloat(lat.value);

    if(isNaN(latitude)) {
        return;
    }

    // Auto-show results if in auto mode
    if (autoMode && !resultsVisible) {
        resultsVisible = true;
        mapVisible = true;
        document.getElementById("results").style.display = 'block';
        document.getElementById("mapContainer").style.display = 'block';
    }

    fetchFlights();
}

function auto() {
    if(autoMode == true) {
        document.getElementById("autoBtn").innerHTML = 'Auto';
        document.getElementById("autoGoNote").innerHTML = "";

        autoMode = false;
        clearInterval(autoGoInterval);
        clearInterval(refreshTimerLabelInterval);
    }
    else {
        // Get configured default from server or use saved frequency
        const savedFreq = localStorage.getItem("frequency");
        const defaultFreq = appConfig.autoRefreshIntervalMinutes || 6;
        const suggestedFreq = savedFreq || defaultFreq;

        let freq = prompt(
            `Enter refresh interval in minutes\n` +
            `Default: ${defaultFreq} min (configured)\n` +
            `Recommended: 5-10 min for continuous monitoring`,
            suggestedFreq
        );

        // User cancelled
        if (freq === null) {
            return;
        }

        try {
            freq = parseInt(freq);

            if(isNaN(freq) || freq <= 0) {
                throw new Error("");
            }
        }
        catch (error) {
            alert("Invalid frequency. Please try again!");
            return auto();
        }

        localStorage.setItem("frequency", freq);
        const initialTimeStr = String(freq).padStart(2, '0') + ':00';
        document.getElementById("autoBtn").innerHTML = "Auto " + initialTimeStr + " ⴵ";
        document.getElementById("autoGoNote").innerHTML = `Auto-refresh every ${freq} minute(s). Pauses when page is hidden.`;

        autoMode = true;
        autoGoInterval = setInterval(goFetch, MS_IN_A_MIN * freq);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000); // Update every second
        remainingSeconds = freq * 60; // Initialize countdown in seconds

        // Trigger initial fetch
        goFetch();
    }
}

function refreshTimer() {
    let autoBtn = document.getElementById("autoBtn");
    const currentFreq = parseInt(localStorage.getItem("frequency")) || appConfig.autoRefreshIntervalMinutes;

    // Decrement remaining seconds
    remainingSeconds--;

    // Reset and trigger fetch when countdown reaches 0
    if (remainingSeconds <= 0) {
        remainingSeconds = currentFreq * 60;
        goFetch(); // Trigger fetch when timer hits zero
    }

    // Format as MM:SS
    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    const timeStr = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');

    autoBtn.innerHTML = "Auto " + timeStr + " ⴵ";
}

function fetchFlights() {
    let latitude = document.getElementById("latitude").value;
    let longitude = document.getElementById("longitude").value;
    let elevation = document.getElementById("elevation").value;

    let hasVeryPossibleTransits = false;

    const bodyTable = document.getElementById('flightData');
    let alertNoResults = document.getElementById("noResults");
    let alertTargetUnderHorizon = document.getElementById("targetUnderHorizon");
    bodyTable.innerHTML = '';
    alertNoResults.innerHTML = '';
    alertTargetUnderHorizon = '';

    const minAltitude = getMinAltitudeAllQuadrants();
    const altThreshold = parseFloat(document.getElementById("altThreshold").value) || 5.0;
    const azThreshold = parseFloat(document.getElementById("azThreshold").value) || 10.0;
    
    let endpoint_url = (
        `/flights?target=${encodeURIComponent(target)}`
        + `&latitude=${encodeURIComponent(latitude)}`
        + `&longitude=${encodeURIComponent(longitude)}`
        + `&elevation=${encodeURIComponent(elevation)}`
        + `&min_altitude=${encodeURIComponent(minAltitude)}`
        + `&alt_threshold=${encodeURIComponent(altThreshold)}`
        + `&az_threshold=${encodeURIComponent(azThreshold)}`
        + `&send-notification=true`
    );

    // Add custom bounding box if user has edited it
    if (window.lastBoundingBox) {
        endpoint_url += `&bbox_lat_ll=${encodeURIComponent(window.lastBoundingBox.latLowerLeft)}`;
        endpoint_url += `&bbox_lon_ll=${encodeURIComponent(window.lastBoundingBox.lonLowerLeft)}`;
        endpoint_url += `&bbox_lat_ur=${encodeURIComponent(window.lastBoundingBox.latUpperRight)}`;
        endpoint_url += `&bbox_lon_ur=${encodeURIComponent(window.lastBoundingBox.lonUpperRight)}`;
    }

    // Show loading spinner
    document.getElementById("loadingSpinner").style.display = "block";
    document.getElementById("results").style.display = "none";

    fetch(endpoint_url)
    .then(response => {
        if (!response.ok) {
            return response.json().then(err => {
                throw new Error(err.error || `Server error: ${response.status}`);
            }).catch(() => {
                throw new Error(`Server error: ${response.status}`);
            });
        }
        return response.json();
    })
    .then(data => {
        // Record update time
        window.lastFlightUpdateTime = Date.now();
        updateLastUpdateDisplay();

        // Hide loading spinner
        document.getElementById("loadingSpinner").style.display = "none";
        document.getElementById("results").style.display = "block";

        if(data.flights.length == 0) {
            alertNoResults.innerHTML = "No flights!"
        }

        // LINE 1: Tracking status - Sun and Moon with weather
        let trackingParts = [];

        // Always show Sun status
        if(data.targetCoordinates && data.targetCoordinates.sun) {
            let isTracking = data.trackingTargets && data.trackingTargets.includes('sun');
            let status = isTracking ? "Tracking" : "Not tracking";
            let text = isTracking ? `<span style="color: #FFD700">Sun: ${status}</span>` : `Sun: ${status}`;
            trackingParts.push(text);
        }

        // Always show Moon status
        if(data.targetCoordinates && data.targetCoordinates.moon) {
            let isTracking = data.trackingTargets && data.trackingTargets.includes('moon');
            let status = isTracking ? "Tracking" : "Not tracking";
            let text = isTracking ? `<span style="color: #FFD700">Moon: ${status}</span>` : `Moon: ${status}`;
            trackingParts.push(text);
        }

        // Weather (no color styling)
        if(data.weather && data.weather.cloud_cover !== null) {
            trackingParts.push(`☁️ ${data.weather.cloud_cover}% clouds`);
        }

        document.getElementById("trackingStatus").innerHTML = trackingParts.join("&nbsp;&nbsp;&nbsp;&nbsp;");

        // LINE 2: Celestial positions with emoji (always show, even below horizon)
        let positionParts = [];
        
        // Always show Sun position if coordinates available
        if(data.targetCoordinates && data.targetCoordinates.sun) {
            const sun = data.targetCoordinates.sun;
            const altStr = sun.altitude.toFixed(1);
            const azStr = sun.azimuthal.toFixed(1);
            positionParts.push(`🌞 Alt: ${altStr}° Az: ${azStr}°`);
        }
        
        // Always show Moon position if coordinates available
        if(data.targetCoordinates && data.targetCoordinates.moon) {
            const moon = data.targetCoordinates.moon;
            const altStr = moon.altitude.toFixed(1);
            const azStr = moon.azimuthal.toFixed(1);
            positionParts.push(`🌙 Alt: ${altStr}° Az: ${azStr}°`);
        }
        
        if(positionParts.length > 0) {
            const celestialEl = document.getElementById("celestialPositions");
            if(celestialEl) {
                celestialEl.innerHTML = positionParts.join("&nbsp;&nbsp;&nbsp;&nbsp;");
            }
        }

        // Check if any targets are trackable
        if(data.trackingTargets && data.trackingTargets.length === 0) {
            alertNoResults.innerHTML = "Sun or moon is below the min angle you selected or weather is bad";
        }

        // Deduplicate flights by ID for display (keep highest possibility level)
        const seenFlights = {};
        data.flights.forEach(flight => {
            // Normalize ID (trim whitespace, consistent case)
            const id = String(flight.id).trim().toUpperCase();
            if (!seenFlights[id]) {
                seenFlights[id] = flight;
            } else {
                // Keep the one with higher possibility (transit > non-transit, higher level wins)
                const existing = seenFlights[id];
                if (flight.is_possible_transit > existing.is_possible_transit) {
                    seenFlights[id] = flight;
                } else if (flight.is_possible_transit === existing.is_possible_transit) {
                    if (parseInt(flight.possibility_level || 0) > parseInt(existing.possibility_level || 0)) {
                        seenFlights[id] = flight;
                    }
                }
            }
        });
        const uniqueFlights = Object.values(seenFlights);
        console.log(`Dedupe: ${data.flights.length} flights -> ${uniqueFlights.length} unique`);
        // Debug: show final dedupe results
        uniqueFlights.forEach(f => {
            if (f.is_possible_transit) {
                console.log(`  ${f.id} (${f.target}): level=${f.possibility_level}, is_transit=${f.is_possible_transit}`);
            }
        });

        // Find next HIGH or MEDIUM probability transit for countdown
        nextTransit = null;
        uniqueFlights.forEach(flight => {
            const level = flight.is_possible_transit === 1 ? parseInt(flight.possibility_level) : 0;
            if (level === HIGH_LEVEL || level === MEDIUM_LEVEL) {
                const etaSeconds = flight.transit_eta_seconds || (flight.time * 60);
                const targetTime = Date.now() + (etaSeconds * 1000);

                // Keep the soonest transit, or highest priority if same time
                if (!nextTransit || targetTime < nextTransit.targetTime ||
                    (targetTime === nextTransit.targetTime && level > nextTransit.level)) {
                    nextTransit = { targetTime, level, flight };
                }
            }
        });

        // Start or clear countdown interval
        if (transitCountdownInterval) {
            clearInterval(transitCountdownInterval);
        }
        if (nextTransit) {
            updateTransitCountdown();
            transitCountdownInterval = setInterval(updateTransitCountdown, 1000);
        } else {
            const countdownEl = document.getElementById('transitCountdown');
            if (countdownEl) {
                countdownEl.style.display = 'none';
            }
        }

        uniqueFlights.forEach(item => {
            const row = document.createElement('tr');

            // Store normalized flight ID and possibility level for cross-referencing
            const normalizedId = String(item.id).trim().toUpperCase();
            const possibilityLevel = item.is_possible_transit === 1 ? parseInt(item.possibility_level) : 0;
            row.setAttribute('data-flight-id', normalizedId);
            row.setAttribute('data-possibility', possibilityLevel);

            // Click handler: normal click flashes, Cmd/Ctrl+click toggles tracking
            row.addEventListener('click', function(e) {
                if ((e.metaKey || e.ctrlKey) && e.altKey) {
                    // Cmd/Ctrl+Option: test sounds only
                    playTrackOnSound();
                    setTimeout(playTrackOffSound, 500);
                } else if (e.metaKey || e.ctrlKey) {
                    // Cmd/Ctrl+click: toggle track mode
                    if (trackingFlightId === normalizedId) {
                        stopTracking();
                    } else {
                        // Safety check: only track medium or high probability
                        if (possibilityLevel < MEDIUM_LEVEL) {
                            alert('Track Mode requires medium or high probability transit.\n\nThis flight has ' +
                                (possibilityLevel === LOW_LEVEL ? 'low' : 'no') +
                                ' probability of transit.');
                            return;
                        }
                        startTracking(normalizedId);
                    }
                } else {
                    // Normal click: flash aircraft on map, highlight row, and show route/track
                    if (typeof flashAircraftMarker === 'function') {
                        flashAircraftMarker(normalizedId);
                    }
                    if (typeof flashTableRow === 'function') {
                        flashTableRow(normalizedId);
                    }
                    if (typeof toggleFlightRouteTrack === 'function') {
                        toggleFlightRouteTrack(item.fa_flight_id, normalizedId);
                    }
                }
            });

            // Add target name as first column
            const targetCell = document.createElement("td");
            if (item.target === "moon") targetCell.textContent = "Moon";
            else if (item.target === "sun") targetCell.textContent = "Sun";
            else targetCell.textContent = "";
            row.appendChild(targetCell);

            COLUMN_NAMES.forEach(column => {
                const val = document.createElement("td");
                const value = item[column];

                if (value === null || value === undefined) {
                    val.textContent = "";
                } else if (column === "id") {
                    // Show just the ID
                    val.textContent = value;
                } else if (column === "aircraft_type") {
                    // Show aircraft type, hide "N/A"
                    val.textContent = value === "N/A" ? "" : value;
                } else if (column === "origin" || column === "destination") {
                    // Scrunch origin/destination with max-width and ellipsis
                    val.textContent = value;
                    val.style.maxWidth = "60px";
                    val.style.overflow = "hidden";
                    val.style.textOverflow = "ellipsis";
                    val.style.whiteSpace = "nowrap";
                    val.title = value;  // Show full name on hover
                } else if (column === "speed") {
                    // Show speed in knots (already converted from km/h in backend)
                    val.textContent = Math.round(value / 1.852);  // Convert km/h back to knots
                } else if (column === "aircraft_elevation_feet") {
                    // Show GPS altitude in feet with comma formatting, or as flight level if > 18000
                    const altitude = Math.round(value);
                    if (altitude > 18000) {
                        const flightLevel = Math.round(altitude / 100);
                        val.textContent = `FL${flightLevel}`;
                    } else {
                        val.textContent = altitude.toLocaleString('en-US');
                    }
                } else if (column === "distance_nm") {
                    // Show distance in nautical miles with one decimal place
                    val.textContent = value.toFixed(1);
                } else if (column === "direction") {
                    // Convert true heading to magnetic heading
                    const trueHeading = value;
                    const magHeading = trueToMagnetic(trueHeading, item.latitude, item.longitude);
                    val.textContent = Math.round(magHeading) + "°";
                    val.title = `True: ${Math.round(trueHeading)}°, Magnetic: ${Math.round(magHeading)}°`;
                } else if (column === "alt_diff" || column === "az_diff") {
                    const roundedValue = Math.round(value);
                    val.textContent = roundedValue + "º";
                    // Color code large angle differences
                    if (Math.abs(roundedValue) > 10) {
                        val.style.color = "#888"; // Gray for large differences
                    }
                } else if (column === "target_alt" || column === "target_az") {
                    // Always show target values, color code negative/invalid
                    const numValue = value.toFixed(1);
                    val.textContent = numValue + "º";
                    if (value < 0) {
                        val.style.color = "#888"; // Gray for below horizon
                        val.style.fontStyle = "italic";
                    }
                } else if (column === "plane_alt" || column === "plane_az") {
                    // Always show plane values, color code negative/invalid
                    const numValue = value.toFixed(1);
                    val.textContent = numValue + "º";
                    if (value < 0) {
                        val.style.color = "#888"; // Gray for negative angles
                        val.style.fontStyle = "italic";
                    }
                } else if (value === "N/D") {
                    val.textContent = value + " ⚠️";
                } else {
                    val.textContent = value;
                }

                row.appendChild(val);
            });

            if(item["is_possible_transit"] == 1) {
                const possibilityLevel = parseInt(item["possibility_level"]);
                highlightPossibleTransit(possibilityLevel, row);

                if(possibilityLevel == MEDIUM_LEVEL || possibilityLevel == HIGH_LEVEL) {
                    hasVeryPossibleTransits = true;
                }
            }

            bodyTable.appendChild(row);
        });

        // renderTargetCoordinates(data.targetCoordinates); // Disabled - now using inline display above
        if(autoMode == true && hasVeryPossibleTransits == true) soundAlert();

        // Always update map visualization when data is fetched (use deduplicated flights)
        if(mapVisible) {
            const mapData = {...data, flights: uniqueFlights};
            updateMapVisualization(mapData, parseFloat(latitude), parseFloat(longitude), parseFloat(elevation));
        }

        // Update altitude display - DISABLED: updateAltitudeOverlay in map.js handles this now
        // updateAltitudeDisplay(data.flights);

        // Save bounding box for next time
        if(data.boundingBox) {
            window.lastBoundingBox = data.boundingBox;
        }
    })
    .catch(error => {
        // Hide loading spinner on error
        document.getElementById("loadingSpinner").style.display = "none";
        document.getElementById("results").style.display = "block";
        
        let errorMsg = error.message || "Unknown error";
        if (errorMsg.includes("AEROAPI") || errorMsg.includes("API key")) {
            alert("⚠️ FlightAware API key not configured.\n\nPlease set AEROAPI_API_KEY in your .env file.\nSee SETUP.md for instructions.");
        } else {
            alert(`Error getting flight data:\n${errorMsg}\n\nCheck console for details.`);
        }
        console.error("Error:", error);
    });
}

function highlightPossibleTransit(possibilityLevel, row) {
    if(possibilityLevel == LOW_LEVEL) row.classList.add("possibleTransitHighlightLow");
    else if(possibilityLevel == MEDIUM_LEVEL) row.classList.add("possibleTransitHighlightMedium");
    else if(possibilityLevel == HIGH_LEVEL) row.classList.add("possibleTransitHighlightHigh");
}

function updateAltitudeDisplay(flights) {
    const barsContainer = document.getElementById("altitudeBars");

    if (!flights || flights.length === 0) {
        barsContainer.innerHTML = "";
        return;
    }

    // Clear existing lines
    barsContainer.innerHTML = "";

    // Maximum altitude for scale (FL450 = 45,000 ft)
    const MAX_ALTITUDE = 45000;

    // Create a thin line for each aircraft
    flights.forEach(flight => {
        const altitude = flight.aircraft_elevation_feet || 0;

        // Skip if altitude is invalid or above max
        if (altitude > MAX_ALTITUDE) return;

        // Calculate position from bottom (0 = ground, 100% = FL450)
        // Clamp negative altitudes to 0% (bottom)
        const clampedAltitude = Math.max(0, altitude);
        const percentFromBottom = (clampedAltitude / MAX_ALTITUDE) * 100;

        // Create line element
        const line = document.createElement("div");
        line.style.position = "absolute";
        line.style.bottom = percentFromBottom + "%";
        line.style.left = "0";
        line.style.right = "0";
        line.style.height = "2px";
        line.style.cursor = "pointer";
        line.style.transition = "height 0.2s, opacity 0.2s";

        // Color based on possibility level
        let color = "#666"; // Default gray for unlikely
        const possibilityLevel = parseInt(flight.possibility_level || 0);
        if (possibilityLevel === HIGH_LEVEL) {
            color = "#32CD32"; // Green
        } else if (possibilityLevel === MEDIUM_LEVEL) {
            color = "#FF8C00"; // Orange
        } else if (possibilityLevel === LOW_LEVEL) {
            color = "#FFD700"; // Yellow
        }
        line.style.background = color;

        // Hover effect
        line.addEventListener('mouseenter', () => {
            line.style.height = "4px";
            line.style.opacity = "1";
        });
        line.addEventListener('mouseleave', () => {
            line.style.height = "2px";
            line.style.opacity = "0.9";
        });

        // Add click handler to flash aircraft on map
        const normalizedId = flight.id.replace(/[^a-zA-Z0-9]/g, '_');
        line.addEventListener('click', () => {
            if (typeof flashAircraftMarker === 'function') {
                flashAircraftMarker(normalizedId);
            }
        });

        line.style.opacity = "0.9";
        barsContainer.appendChild(line);
    });
}

function toggleTarget() {
    if(target == "moon") target = "sun";
    else if(target == "sun") target = "auto";
    else target = "moon";

    document.getElementById("targetCoordinates").innerHTML = "";
    document.getElementById("trackingStatus").innerHTML = "";
    displayTarget();

    resetResultsTable();
}

function renderTargetCoordinates(coordinates) {
    let time_ = (new Date()).toLocaleTimeString();
    let coordinates_str;

    // Check if coordinates is nested (auto mode) or direct (single target mode)
    if (coordinates.altitude !== undefined && coordinates.azimuthal !== undefined) {
        // Single target mode
        coordinates_str = "altitude: " + coordinates.altitude + "° azimuthal: " + coordinates.azimuthal + "° (" + time_ + ")";
    } else {
        // Auto mode - coordinates is an object with target names as keys
        let parts = [];
        for (let [targetName, coords] of Object.entries(coordinates)) {
            let name = targetName === "moon" ? "Moon" : "Sun";
            parts.push(`${name} alt: ${coords.altitude}° az: ${coords.azimuthal}°`);
        }
        coordinates_str = parts.join(" | ") + " (" + time_ + ")";
    }

    document.getElementById("targetCoordinates").innerHTML = coordinates_str;
}

function displayTarget() {
    // Target icon removed from UI - automatic tracking now
    localStorage.setItem("target", target);
}

function resetResultsTable() {
    document.getElementById("flightData").innerHTML = "";
}

function soundAlert() {
    const audio = document.getElementById('alertSound');
    audio.play();
    
    // Also show desktop notification if permitted
    showDesktopNotification();
}

function showDesktopNotification() {
    // Check if alerts are enabled
    if (!alertsEnabled) {
        console.log('Alerts disabled - skipping notification');
        return;
    }
    
    // Check if browser supports notifications
    if (!('Notification' in window)) {
        console.log('Browser does not support desktop notifications');
        return;
    }
    
    // Check permission
    if (Notification.permission === 'granted') {
        createNotification();
    } else if (Notification.permission !== 'denied') {
        // Request permission
        Notification.requestPermission().then(permission => {
            if (permission === 'granted') {
                createNotification();
            }
        });
    }
}

function createNotification() {
    const targetName = target === 'auto' ? 'Sun/Moon' : (target === 'moon' ? 'Moon' : 'Sun');
    const title = `Transit Alert! ${targetName}`;
    const body = 'Possible aircraft transit detected. Check the results table for details.';
    
    const notification = new Notification(title, {
        body: body,
        icon: '/static/images/favicon.ico',
        badge: '/static/images/favicon.ico',
        tag: 'flymoon-transit',
        requireInteraction: false
    });
    
    notification.onclick = function() {
        window.focus();
        this.close();
    };
    
    // Auto-close after 10 seconds
    setTimeout(() => notification.close(), 10000);
}

function toggleAlerts() {
    // Find the alerts button - try multiple selectors
    let button = document.querySelector('[onclick*="Alerts"]');
    if (!button) {
        button = document.querySelector('button[title*="alerts" i]');
    }
    
    if (!('Notification' in window)) {
        alert('Your browser does not support desktop notifications');
        return;
    }

    // Toggle state
    alertsEnabled = !alertsEnabled;
    localStorage.setItem('alertsEnabled', alertsEnabled);
    
    // Update button appearance if found
    if (button) {
        if (alertsEnabled) {
            button.style.backgroundColor = '#32CD32';  // Green when on
            button.style.color = 'white';
            button.title = 'Alerts enabled (click to disable)';
            
            // Request permission if not already granted
            if (Notification.permission !== 'granted' && Notification.permission !== 'denied') {
                Notification.requestPermission();
            }
        } else {
            button.style.backgroundColor = '';  // Default when off
            button.style.color = '';
            button.title = 'Alerts disabled (click to enable)';
        }
    }
}

// Initialize alerts button state on page load
window.addEventListener('DOMContentLoaded', () => {
    let button = document.querySelector('[onclick*="Alerts"]');
    if (!button) {
        button = document.querySelector('button[title*="alerts" i]');
    }
    if (button && alertsEnabled) {
        button.style.backgroundColor = '#32CD32';
        button.style.color = 'white';
        button.title = 'Alerts enabled (click to disable)';
    }
});

function requestNotificationPermission() {
    // Deprecated - replaced by toggleAlerts()
    toggleAlerts();
}

// Last update display
function updateLastUpdateDisplay() {
    const elem = document.getElementById('lastUpdateStatus');
    if (!elem || !window.lastFlightUpdateTime) {
        return;
    }

    // Get refresh frequency from localStorage (default 6 minutes)
    const freq = parseInt(localStorage.getItem("frequency")) || 6;
    const refreshIntervalMs = freq * 60 * 1000;

    const now = Date.now();
    const elapsedMs = now - window.lastFlightUpdateTime;
    const remainingMs = Math.max(0, refreshIntervalMs - elapsedMs);
    const remainingSeconds = Math.floor(remainingMs / 1000);

    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;

    const timeStr = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');

    elem.textContent = 'Time until next update is ' + timeStr;
}

// Update display every second
setInterval(updateLastUpdateDisplay, 1000);

// Telescope status indicator
function updateTelescopeStatus() {
    fetch('/telescope/status')
        .then(response => response.json())
        .then(data => {
            const statusLight = document.getElementById('telescopeStatusLight');
            if (statusLight) {
                if (data.connected) {
                    statusLight.style.backgroundColor = '#00ff00';
                    statusLight.title = 'Telescope connected';
                } else {
                    statusLight.style.backgroundColor = '#ff0000';
                    statusLight.title = 'Telescope disconnected';
                }
            }
        })
        .catch(error => {
            const statusLight = document.getElementById('telescopeStatusLight');
            if (statusLight) {
                statusLight.style.backgroundColor = '#999';
                statusLight.title = 'Telescope status unknown';
            }
        });
}

// Update telescope status every 2 seconds
updateTelescopeStatus();
setInterval(updateTelescopeStatus, 2000);
