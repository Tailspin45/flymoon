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
    "target_az",
    "plane_az",
    "alt_diff",
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

    const minAltN = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltNEl.value)));
    const minAltE = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltEEl.value)));
    const minAltS = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltSEl.value)));
    const minAltW = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltWEl.value)));

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

    const minAltN = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltNEl.value)));
    const minAltE = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltEEl.value)));
    const minAltS = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltSEl.value)));
    const minAltW = ((v => isNaN(v) ? defaultMinAlt : v)(parseFloat(minAltWEl.value)));
    return Math.min(minAltN, minAltE, minAltS, minAltW);
}

// Track last alerted transit to avoid spamming alerts
let lastAlertedTransits = {
    sun: null,
    moon: null
};

// Check for transits and alert user about filter changes
function checkAndAlertFilterChange(flights, targetCoordinates) {
    // Find medium/high probability transits grouped by target
    const sunTransits = flights.filter(f => 
        f.target === 'sun' && 
        f.is_possible_transit === 1 && 
        (parseInt(f.possibility_level) === MEDIUM_LEVEL || parseInt(f.possibility_level) === HIGH_LEVEL)
    );
    
    const moonTransits = flights.filter(f => 
        f.target === 'moon' && 
        f.is_possible_transit === 1 && 
        (parseInt(f.possibility_level) === MEDIUM_LEVEL || parseInt(f.possibility_level) === HIGH_LEVEL)
    );
    
    const sunIsUp = targetCoordinates && targetCoordinates.sun && targetCoordinates.sun.altitude > 0;
    
    // Moon transit detected
    if (moonTransits.length > 0) {
        const moonTransit = moonTransits[0]; // First one
        const transitId = moonTransit.id;
        
        // Only alert if this is a new transit (not already alerted)
        if (lastAlertedTransits.moon !== transitId) {
            lastAlertedTransits.moon = transitId;
            
            if (sunIsUp) {
                // Sun is up - need to remove solar filter for moon
                alert('🌙 MOON TRANSIT DETECTED!\n\n' +
                      '⚠️ IMPORTANT:\n' +
                      '1. REMOVE SOLAR FILTER from telescope\n' +
                      '2. Reset telescope to track the MOON\n' +
                      '3. Point telescope at moon position\n\n' +
                      `Transit ETA: ${moonTransit.time ? moonTransit.time.toFixed(1) : 'N/A'} minutes`);
            } else {
                // Sun is down - just track moon
                alert('🌙 MOON TRANSIT DETECTED!\n\n' +
                      'Reset telescope to track the MOON\n\n' +
                      `Transit ETA: ${moonTransit.time ? moonTransit.time.toFixed(1) : 'N/A'} minutes`);
            }
        }
    } else {
        // No moon transits - clear the alert memory
        lastAlertedTransits.moon = null;
    }
    
    // Solar transit detected
    if (sunTransits.length > 0) {
        const sunTransit = sunTransits[0]; // First one
        const transitId = sunTransit.id;
        
        // Only alert if this is a new transit (not already alerted)
        if (lastAlertedTransits.sun !== transitId) {
            lastAlertedTransits.sun = transitId;
            
            alert('☀️ SOLAR TRANSIT DETECTED!\n\n' +
                  '⚠️ IMPORTANT:\n' +
                  '1. INSTALL SOLAR FILTER on telescope\n' +
                  '2. Reset telescope to track the SUN\n' +
                  '3. Point telescope at sun position\n\n' +
                  '⚠️ NEVER look at the sun without proper solar filter!\n\n' +
                  `Transit ETA: ${sunTransit.time ? sunTransit.time.toFixed(1) : 'N/A'} minutes`);
        }
    } else {
        // No sun transits - clear the alert memory
        lastAlertedTransits.sun = null;
    }
}

var alertsEnabled = localStorage.getItem('alertsEnabled') === 'true' || false;
// Transit countdown tracking
var nextTransit = null;
var transitCountdownInterval = null;
var target = "auto"; // Always auto-detect sun and moon
var autoGoInterval = null; // Auto-refresh interval
var refreshTimerLabelInterval = null; // Countdown timer interval
var softRefreshInterval = null; // For client-side position updates
var remainingSeconds = 600; // Track remaining seconds for countdown (default 10 min)
var lastFlightData = null; // Cache last flight response for soft refresh
window.lastFlightUpdateTime = parseInt(sessionStorage.getItem('lastFlightUpdateTime') || '0', 10);
// Restore cached flight data so the table is populated instantly on back-navigation
try {
    const _cached = sessionStorage.getItem('lastFlightData');
    if (_cached) lastFlightData = JSON.parse(_cached);
} catch(e) { lastFlightData = null; }
var currentCheckInterval = 600; // Current adaptive interval in seconds (default 10 min to match cache TTL)
displayTarget();

// App configuration from server
var appConfig = {
    autoRefreshIntervalMinutes: 10  // Default 10 minutes to match cache TTL
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

// Page visibility detection - optionally pause polling when page is hidden
document.addEventListener('visibilitychange', function() {
    const pauseWhenHidden = localStorage.getItem("pauseWhenHidden") !== "false"; // Default true
    
    if (document.hidden && pauseWhenHidden) {
        console.log('Page hidden - pausing auto-refresh');
        if (autoGoInterval) clearInterval(autoGoInterval);
        if (refreshTimerLabelInterval) clearInterval(refreshTimerLabelInterval);
        if (softRefreshInterval) clearInterval(softRefreshInterval);
    } else if (!document.hidden && pauseWhenHidden) {
        console.log('Page visible - resuming auto-refresh');
        const intervalSecs = currentCheckInterval || appConfig.autoRefreshIntervalMinutes * 60;
        autoGoInterval = setInterval(goFetch, intervalSecs * 1000);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000);
        softRefreshInterval = setInterval(softRefresh, 15000); // Soft refresh every 15 seconds
    }
});

/**
 * Client-side position prediction and UI update without API call
 * Uses constant velocity model to extrapolate aircraft positions
 * Now also recalculates transit predictions with updated positions
 */
async function softRefresh() {
    if (!lastFlightData || !window.lastFlightUpdateTime) {
        return; // No data to update
    }

    const secondsElapsed = (Date.now() - window.lastFlightUpdateTime) / 1000;
    
    // Don't soft refresh if too much time has passed (data too stale)
    if (secondsElapsed > 300) {  // 5 minutes
        console.log('Data too stale for soft refresh, waiting for full refresh');
        return;
    }

    // Clone and update flight positions
    const updatedFlights = lastFlightData.flights.map(flight => {
        const updated = {...flight};
        
        // Update position for all flights, not just transits
        if (updated.latitude && updated.longitude && updated.speed && updated.direction) {
            const speedKmPerSec = updated.speed / 3600;  // km/h to km/s
            const distanceKm = speedKmPerSec * secondsElapsed;
            
            // Convert heading to radians
            const headingRad = updated.direction * Math.PI / 180;
            
            // Update position (simplified flat-earth model, good enough for short distances)
            const latChange = (distanceKm / 111.32) * Math.cos(headingRad);
            const lonChange = (distanceKm / (111.32 * Math.cos(updated.latitude * Math.PI / 180))) * Math.sin(headingRad);
            
            updated.latitude += latChange;
            updated.longitude += lonChange;
        }
        
        // Update transit ETA
        if (updated.is_possible_transit === 1 && updated.time !== null) {
            updated.time = Math.max(0, updated.time - (secondsElapsed / 60));
        }
        
        return updated;
    });

    // Recalculate transit predictions with updated positions
    try {
        const latitude = parseFloat(document.getElementById("latitude").value);
        const longitude = parseFloat(document.getElementById("longitude").value);
        const elevation = parseFloat(document.getElementById("elevation").value);
        
        // Skip recalculation if coordinates are invalid
        if (isNaN(latitude) || isNaN(longitude) || isNaN(elevation)) {
            console.log('Soft refresh: Skipping recalculation - invalid coordinates');
            // Fallback to position-only update
            updateFlightTable(updatedFlights);
            if (mapVisible && typeof updateAircraftMarkers === 'function') {
                updateAircraftMarkers(updatedFlights, latitude, longitude);
            }
            return;
        }
        
        const response = await fetch('/transits/recalculate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                flights: updatedFlights,
                latitude: latitude,
                longitude: longitude,
                elevation: elevation,
                target: target,
                min_altitude: getMinAltitudeAllQuadrants()
            })
        });
        
        if (response.ok) {
            const recalcData = await response.json();
            // Update table cells in-place — no scroll save/restore needed
            updateFlightTableFull(recalcData.flights);
            
            // Update map markers
            if (mapVisible && typeof updateAircraftMarkers === 'function') {
                updateAircraftMarkers(recalcData.flights, latitude, longitude);
            }
            
            console.log(`Soft refresh: Updated ${updatedFlights.length} flights with transit recalculation (+${Math.floor(secondsElapsed)}s)`);
        } else {
            // Fallback to position-only update if recalculation fails
            updateFlightTable(updatedFlights);
            if (mapVisible && typeof updateAircraftMarkers === 'function') {
                updateAircraftMarkers(updatedFlights, latitude, longitude);
            }
            console.log(`Soft refresh: Updated positions only (+${Math.floor(secondsElapsed)}s)`);
        }
    } catch (error) {
        console.error('Soft refresh recalculation failed:', error);
        // Fallback to position-only update
        updateFlightTable(updatedFlights);
        if (mapVisible && typeof updateAircraftMarkers === 'function') {
            updateAircraftMarkers(updatedFlights, latitude, longitude);
        }
    }
    
    // Update "Last updated" display
    updateLastUpdateDisplay();
    
    // Show soft refresh indicator briefly
    const statusDiv = document.getElementById('lastUpdateStatus');
    if (statusDiv) {
        const originalText = statusDiv.textContent;
        statusDiv.textContent = originalText + ' 🔄';
        setTimeout(() => {
            statusDiv.textContent = originalText;
        }, 500);
    }
}

/**
 * Update flight table with new data
 */
function updateFlightTable(flights) {
    flights.forEach(flight => {
        const row = document.querySelector(`tr[data-flight-id="${flight.id}"]`);
        if (row && flight.is_possible_transit === 1) {
            // Update time cell
            const timeCell = row.querySelector('td:nth-child(17)'); // Adjust column index if needed
            if (timeCell && flight.time !== null) {
                timeCell.textContent = flight.time.toFixed(1);
            }
        }
    });
}

/**
 * Update flight table with full recalculated data (all columns)
 * Used during soft refresh after transit recalculation
 */
function updateFlightTableFull(flights) {
    const bodyTable = document.getElementById('flightData');
    if (!bodyTable) return;
    
    flights.forEach(flight => {
        const row = document.querySelector(`tr[data-flight-id="${flight.id}"]`);
        if (!row) return; // Flight not in table
        
        // Update all relevant cells by column index
        const cells = row.querySelectorAll('td');
        
        // Column indexes (0: Target, 1: ID, 2: Type, 3: Origin, 4: Dest, 5: Target Alt, 6: Plane Alt,
        // 7: Target Az, 8: Plane Az, 9: Alt Diff, 10: Az Diff, 11: Elev Change, 12: Aircraft Alt (ft),
        // 13: Direction, 14: Distance, 15: Speed, 16: Time)
        
        if (flight.target_alt !== null && cells[5]) {
            cells[5].textContent = flight.target_alt.toFixed(1) + "º";
        }
        if (flight.plane_alt !== null && cells[6]) {
            cells[6].textContent = flight.plane_alt.toFixed(1) + "º";
        }
        if (flight.target_az !== null && cells[7]) {
            cells[7].textContent = flight.target_az.toFixed(1) + "º";
        }
        if (flight.plane_az !== null && cells[8]) {
            cells[8].textContent = flight.plane_az.toFixed(1) + "º";
        }
        if (flight.alt_diff !== null && cells[9]) {
            cells[9].textContent = Math.round(flight.alt_diff) + "º";
            cells[9].style.color = Math.abs(Math.round(flight.alt_diff)) >= 3 ? "#888" : "";
        }
        if (flight.az_diff !== null && cells[10]) {
            cells[10].textContent = Math.round(flight.az_diff) + "º";
            cells[10].style.color = Math.abs(Math.round(flight.az_diff)) >= 3 ? "#888" : "";
        }
        if (flight.distance_nm !== null && cells[14]) {
            const km = (flight.distance_nm * 1.852).toFixed(1);
            const miles = (flight.distance_nm * 1.15078).toFixed(1);
            const spans = cells[14].querySelectorAll('span');
            if (spans.length === 2) {
                spans[0].textContent = km;
                spans[1].textContent = miles;
            } else {
                cells[14].innerHTML = `<span style="display:inline-block;text-align:right;min-width:4ch">${km}</span>/<span style="display:inline-block;text-align:left;min-width:4ch">${miles}</span>`;
            }
        }
        if (flight.time !== null && cells[16]) {
            cells[16].textContent = flight.time.toFixed(1);
        }
        // Update source badge (cell 17) if position_source changed (e.g. OS→ADS-B)
        if (flight.position_source && cells[17]) {
            const srcMap = {
                "opensky":     { label: "OS",    color: "#4caf50", title: "OpenSky (~10s latency)" },
                "flightaware": { label: "FA",    color: "#888",    title: "FlightAware (60–300s latency)" },
                "track":       { label: "TRK",   color: "#2196f3", title: "Track-derived velocity" },
                "adsb":        { label: "ADS-B", color: "#00e5ff", title: "Direct ADS-B (<5s latency)" },
            };
            const si = srcMap[flight.position_source] || { label: flight.position_source.toUpperCase(), color: "#888", title: flight.position_source };
            const age = flight.position_age_s != null ? ` (${flight.position_age_s}s)` : "";
            const span = cells[17].querySelector("span");
            if (span) {
                span.textContent = si.label;
                span.style.background = si.color;
                span.title = si.title + age;
            }
        }
        
        // Update row highlight based on new transit status
        if (flight.is_possible_transit === 1) {
            const possibilityLevel = parseInt(flight.possibility_level);
            highlightPossibleTransit(possibilityLevel, row);
        } else {
            // Remove highlighting if no longer a transit
            row.style.backgroundColor = "";
        }
    });
}

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
            const km = (value * 1.852).toFixed(1);
            const miles = (value * 1.15078).toFixed(1);
            cell.innerHTML = `<span style="display:inline-block;text-align:right;min-width:4ch">${km}</span>/<span style="display:inline-block;text-align:left;min-width:4ch">${miles}</span>`;
        } else if (column === "alt_diff" || column === "az_diff") {
            const roundedValue = Math.round(value);
            cell.textContent = roundedValue + "º";
            cell.style.color = Math.abs(roundedValue) >= 3 ? "#888" : "";
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

    const targetName = nextTransit.flight && nextTransit.flight.target
        ? nextTransit.flight.target.charAt(0).toUpperCase() + nextTransit.flight.target.slice(1)
        : '';
    const targetEmoji = nextTransit.flight && nextTransit.flight.target === 'sun' ? '☀️' : '🌙';

    countdownDiv.style.backgroundColor = bgColor;
    countdownDiv.style.color = 'white';
    countdownDiv.style.display = 'block';
    countdownDiv.innerHTML = `${targetEmoji} ${targetName} — ${levelText} probability transit in ${timeStr}`;
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
    // Manual refresh button - always show warning unless cache is expired
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

    // Check if data is fresh (within cache TTL)
    const cacheValidSeconds = 600; // 10 minutes - matches backend cache
    const secondsSinceUpdate = (Date.now() - window.lastFlightUpdateTime) / 1000;
    
    // Always show warning if we have recent data
    if (window.lastFlightUpdateTime > 0 && secondsSinceUpdate < cacheValidSeconds) {
        const minutesRemaining = Math.ceil((cacheValidSeconds - secondsSinceUpdate) / 60);
        const secondsRemaining = Math.floor(cacheValidSeconds - secondsSinceUpdate);
        
        const confirmed = confirm(
            `⚠️ API Rate Limit Protection\n\n` +
            `Last update: ${secondsRemaining}s ago\n` +
            `Cache expires in: ${minutesRemaining} minute(s)\n\n` +
            `Making a new API call now wastes your FlightAware quota.\n` +
            `Aircraft positions are being updated automatically every 15 seconds.\n\n` +
            `Force a new API call anyway?`
        );
        
        if (!confirmed) {
            console.log('User cancelled refresh - data is still fresh');
            // Still show results if hidden
            if (!resultsVisible) {
                resultsVisible = true;
                mapVisible = true;
                resultsDiv.style.display = 'block';
                mapContainer.style.display = 'block';
            }
            return;
        }
        
        console.log('User forced refresh despite fresh cache');
    }

    // Show results and map if not already visible
    if (!resultsVisible) {
        resultsVisible = true;
        mapVisible = true;
        resultsDiv.style.display = 'block';
        mapContainer.style.display = 'block';
    }

    // Fetch fresh data — mark this as a user-forced refresh so map clears breadcrumbs
    window._pendingForceRefresh = true;
    fetchFlights();
}

function goFetch() {
    // Internal function for periodic auto-fetch
    let lat = document.getElementById("latitude");
    let latitude = parseFloat(lat.value);

    if(isNaN(latitude)) {
        return;
    }

    // Auto-show results on first auto-fetch
    if (!resultsVisible) {
        resultsVisible = true;
        mapVisible = true;
        document.getElementById("results").style.display = 'block';
        document.getElementById("mapContainer").style.display = 'block';
    }

    fetchFlights();
}

function refreshTimer() {
    // Decrement remaining seconds
    remainingSeconds--;

    // Reset and trigger fetch when countdown reaches 0
    if (remainingSeconds <= 0) {
        remainingSeconds = currentCheckInterval;
        goFetch(); // Trigger fetch when timer hits zero
    }

    // Update last update display
    updateLastUpdateDisplay();
}

var _fetchRequestSeq = 0; // Sequence counter to discard stale concurrent responses

function showErrorBanner(msg) {
    const banner = document.getElementById("errorBanner");
    if (!banner) return;
    banner.textContent = msg;
    banner.style.display = "block";
}

function clearErrorBanner() {
    const banner = document.getElementById("errorBanner");
    if (banner) banner.style.display = "none";
}

function fetchFlights() {
    const thisSeq = ++_fetchRequestSeq;
    let latitude = document.getElementById("latitude").value;
    let longitude = document.getElementById("longitude").value;
    let elevation = document.getElementById("elevation").value;

    let hasVeryPossibleTransits = false;
    let transitDetails = []; // Collect high-priority transits for notification

    const bodyTable = document.getElementById('flightData');
    let alertNoResults = document.getElementById("noResults");
    let alertTargetUnderHorizon = document.getElementById("targetUnderHorizon");
    alertNoResults.innerHTML = '';
    alertTargetUnderHorizon = '';

    const minAltitude = getMinAltitudeAllQuadrants();
    // Thresholds now configured via server .env (ALT_THRESHOLD, AZ_THRESHOLD)
    // Default values used here are overridden by server config
    const altThreshold = 1.0;
    const azThreshold = 1.0;
    
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
        // Discard stale responses from concurrent fetches; leave spinner running
        // so it stays visible until the winning request finishes rendering.
        if (thisSeq !== _fetchRequestSeq) {
            console.log(`[fetchFlights] Discarding stale response (seq ${thisSeq} < ${_fetchRequestSeq})`);
            return;
        }

        // Clear table here (inside async callback) to prevent duplicate rows from concurrent fetches
        bodyTable.innerHTML = '';

        // Record update time and cache data
        clearErrorBanner();
        window.lastFlightUpdateTime = Date.now();
        sessionStorage.setItem('lastFlightUpdateTime', String(window.lastFlightUpdateTime));
        lastFlightData = data;
        try { sessionStorage.setItem('lastFlightData', JSON.stringify(data)); } catch(e) {}
        updateLastUpdateDisplay();

        if(data.flights.length == 0) {
            alertNoResults.innerHTML = "No flights!"
        }

        // Update adaptive interval if provided by server
        if (data.nextCheckInterval) {
            currentCheckInterval = data.nextCheckInterval;
            console.log(`Adaptive interval: ${currentCheckInterval}s (${(currentCheckInterval/60).toFixed(1)} min)`);
            
            // Restart auto-refresh with new interval
            if (autoGoInterval) {
                clearInterval(autoGoInterval);
                autoGoInterval = setInterval(goFetch, currentCheckInterval * 1000);
                remainingSeconds = currentCheckInterval;
            }
        }
        
        // Auto-pause if no targets being tracked
        if (data.trackingTargets && data.trackingTargets.length === 0) {
            console.log("⏸️  No targets above horizon, pausing auto-refresh");
            if (autoGoInterval) clearInterval(autoGoInterval);
            if (softRefreshInterval) clearInterval(softRefreshInterval);
            // Will resume automatically on next fetch when targets rise
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

        // LINE 4: Rise/set times
        if(data.riseSetTimes) {
            const r = data.riseSetTimes;
            const riseSetParts = [];
            if(r.sun_rise || r.sun_set) {
                let s = "🌞";
                if(r.sun_rise) s += ` ↑${r.sun_rise}`;
                if(r.sun_set)  s += ` ↓${r.sun_set}`;
                riseSetParts.push(s);
            }
            if(r.moon_rise || r.moon_set) {
                let s = "🌙";
                if(r.moon_rise) s += ` ↑${r.moon_rise}`;
                if(r.moon_set)  s += ` ↓${r.moon_set}`;
                riseSetParts.push(s);
            }
            const riseSetEl = document.getElementById("riseSetTimes");
            if(riseSetEl && riseSetParts.length > 0) {
                riseSetEl.innerHTML = riseSetParts.join("&nbsp;&nbsp;&nbsp;&nbsp;");
            }
        }

        // Check if any targets are trackable
        if(data.trackingTargets && data.trackingTargets.length === 0) {
            alertNoResults.innerHTML = "Sun or moon is below the min angle you selected or weather is bad";
        }

        // Save bounding box BEFORE filtering (so filter can use it)
        if(data.boundingBox) {
            window.lastBoundingBox = data.boundingBox;
            console.log('Bounding box:', window.lastBoundingBox);
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
        
        // Filter flights to only show those within bounding box
        const filteredFlights = window.lastBoundingBox ? uniqueFlights.filter(flight => {
            if (!flight.latitude || !flight.longitude) return false;
            const bbox = window.lastBoundingBox;
            const inBounds = (
                flight.latitude >= bbox.latLowerLeft &&
                flight.latitude <= bbox.latUpperRight &&
                flight.longitude >= bbox.lonLowerLeft &&
                flight.longitude <= bbox.lonUpperRight
            );
            if (!inBounds) {
                console.log(`❌ Filtering out ${flight.id} at (${flight.latitude.toFixed(2)}, ${flight.longitude.toFixed(2)}) - outside bbox [${bbox.latLowerLeft.toFixed(2)},${bbox.lonLowerLeft.toFixed(2)} to ${bbox.latUpperRight.toFixed(2)},${bbox.lonUpperRight.toFixed(2)}]`);
            }
            return inBounds;
        }) : uniqueFlights;
        
        if (window.lastBoundingBox && filteredFlights.length < uniqueFlights.length) {
            console.log(`🔍 Bbox filter: ${uniqueFlights.length} flights -> ${filteredFlights.length} in bounds`);
        } else if (!window.lastBoundingBox) {
            console.warn('⚠️ No bounding box set - showing all flights');
        }
        
        // Debug: show final dedupe results
        filteredFlights.forEach(f => {
            if (f.is_possible_transit) {
                console.log(`  ${f.id} (${f.target}): level=${f.possibility_level}, is_transit=${f.is_possible_transit}`);
            }
        });

        // Find next HIGH or MEDIUM probability transit for countdown
        nextTransit = null;
        filteredFlights.forEach(flight => {
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

        // Check for medium/high transits and alert user about filter changes
        checkAndAlertFilterChange(filteredFlights, data.targetCoordinates);

        filteredFlights.forEach(item => {
            const row = document.createElement('tr');

            // Store normalized flight ID, possibility level, and transit time for cross-referencing
            const normalizedId = String(item.id).trim().toUpperCase();
            const possibilityLevel = item.is_possible_transit === 1 ? parseInt(item.possibility_level) : 0;
            row.setAttribute('data-flight-id', normalizedId);
            row.setAttribute('data-possibility', possibilityLevel);
            // Store transit time for route/track conditional fetching
            if (item.time !== null && item.time !== undefined) {
                row.setAttribute('data-transit-time', item.time);
            }

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
                    // Show speed in MPH (value is in km/h from backend)
                    val.textContent = Math.round(value / 1.60934);  // Convert km/h to MPH
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
                    // Show distance in km/miles (converted from nautical miles)
                    const km = (value * 1.852).toFixed(1);
                    const miles = (value * 1.15078).toFixed(1);
                    val.innerHTML = `<span style="display:inline-block;text-align:right;min-width:4ch">${km}</span>/<span style="display:inline-block;text-align:left;min-width:4ch">${miles}</span>`;
                } else if (column === "direction") {
                    // Convert true heading to magnetic heading
                    const trueHeading = value;
                    const magHeading = trueToMagnetic(trueHeading, item.latitude, item.longitude);
                    val.textContent = Math.round(magHeading) + "°";
                    val.title = `True: ${Math.round(trueHeading)}°, Magnetic: ${Math.round(magHeading)}°`;
                } else if (column === "alt_diff" || column === "az_diff") {
                    const roundedValue = Math.round(value);
                    val.textContent = roundedValue + "º";
                    // Black if within 3°, grey otherwise
                    if (Math.abs(roundedValue) >= 3) {
                        val.style.color = "#888";
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

            // Position source badge (appended after COLUMN_NAMES loop)
            const srcCell = document.createElement("td");
            const src = item["position_source"] || "fa";
            const srcAge = item["position_age_s"];
            const srcMap = {
                "opensky":      { label: "OS",    color: "#4caf50", title: "OpenSky (~10s latency)" },
                "flightaware":  { label: "FA",    color: "#888",    title: "FlightAware (60–300s latency)" },
                "track":        { label: "TRK",   color: "#2196f3", title: "Track-derived velocity" },
                "adsb":         { label: "ADS-B", color: "#00e5ff", title: "Direct ADS-B (<5s latency)" },
            };
            const srcInfo = srcMap[src] || { label: src.toUpperCase(), color: "#888", title: src };
            const ageStr = srcAge != null ? ` (${srcAge}s)` : "";
            srcCell.innerHTML = `<span style="font-size:0.7em;padding:1px 4px;border-radius:3px;background:${srcInfo.color};color:#fff;white-space:nowrap" title="${srcInfo.title}${ageStr}">${srcInfo.label}</span>`;
            row.appendChild(srcCell);

            if(item["is_possible_transit"] == 1) {
                const possibilityLevel = parseInt(item["possibility_level"]);
                highlightPossibleTransit(possibilityLevel, row);

                if(possibilityLevel == MEDIUM_LEVEL || possibilityLevel == HIGH_LEVEL) {
                    hasVeryPossibleTransits = true;
                    // Collect details for notification
                    transitDetails.push({
                        flight: item["id"],
                        level: possibilityLevel === HIGH_LEVEL ? "HIGH" : "MEDIUM",
                        time: item["time"],
                        altDiff: item["alt_diff"],
                        azDiff: item["az_diff"]
                    });
                }
            }

            bodyTable.appendChild(row);
        });

        // renderTargetCoordinates(data.targetCoordinates); // Disabled - now using inline display above
        if (hasVeryPossibleTransits == true) soundAlert(transitDetails);

        // Update cached flights to the filtered+deduped set so soft refresh uses the same list
        lastFlightData = {...data, flights: filteredFlights};

        // Always update map visualization when data is fetched (use filtered flights)
        if(mapVisible) {
            const mapData = {...data, flights: filteredFlights};
            const isForceRefresh = !!window._pendingForceRefresh;
            window._pendingForceRefresh = false;
            updateMapVisualization(mapData, parseFloat(latitude), parseFloat(longitude), parseFloat(elevation), isForceRefresh);
        }

        // Update altitude display - DISABLED: updateAltitudeOverlay in map.js handles this now
        // updateAltitudeDisplay(data.flights);

        // Hide spinner only after all rendering is complete
        document.getElementById("loadingSpinner").style.display = "none";
        document.getElementById("results").style.display = "block";
    })
    .catch(error => {
        // Hide loading spinner on error
        document.getElementById("loadingSpinner").style.display = "none";
        document.getElementById("results").style.display = "block";
        
        const errorMsg = error.message || error.toString() || "Unknown error";
        const stack = error.stack ? `\n\n${error.stack}` : "";
        let displayMsg;
        if (errorMsg.includes("AEROAPI") || errorMsg.includes("API key")) {
            displayMsg = "⚠️ FlightAware API key not configured.\n\nPlease set AEROAPI_API_KEY in your .env file.\nSee SETUP.md for instructions.";
        } else if (errorMsg.includes("Failed to fetch") || errorMsg.includes("ERR_EMPTY_RESPONSE") || errorMsg === "") {
            displayMsg = "⚠️ Server not responding (ERR_EMPTY_RESPONSE)\n\nThe Flask server may have crashed. Check the terminal running app.py for the Python traceback.";
        } else {
            displayMsg = `⚠️ Error getting flight data:\n${errorMsg}${stack}`;
        }
        showErrorBanner(displayMsg);
        console.error("Fetch error:", error);
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
        const normalizedId = String(flight.id).trim().toUpperCase();
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
    // Target is always "auto" — no toggle needed
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
    localStorage.setItem("target", "auto");
}

function resetResultsTable() {
    document.getElementById("flightData").innerHTML = "";
}

function soundAlert(transitDetails = []) {
    // Try to play audio (may be blocked if tab is hidden)
    const audio = document.getElementById('alertSound');
    if (audio && !document.hidden) {
        audio.play().catch(err => console.log('Audio play blocked:', err));
    }
    
    // Always show desktop notification (works even when tab is hidden)
    showDesktopNotification(transitDetails);
}

function showDesktopNotification(transitDetails = []) {
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
        createNotification(transitDetails);
    } else if (Notification.permission !== 'denied') {
        // Request permission
        Notification.requestPermission().then(permission => {
            if (permission === 'granted') {
                createNotification(transitDetails);
            }
        });
    }
}

function createNotification(transitDetails = []) {
    const targetName = target === 'auto' ? 'Sun/Moon' : (target === 'moon' ? 'Moon' : 'Sun');
    
    // Build notification body with transit details
    let body = '';
    if (transitDetails.length > 0) {
        const count = transitDetails.length;
        const plural = count > 1 ? 's' : '';
        body = `${count} possible transit${plural} detected:\n`;
        
        // Show up to 3 transits
        transitDetails.slice(0, 3).forEach(t => {
            body += `\n✈️ ${t.flight} in ${t.time} min (${t.level})`;
        });
        
        if (transitDetails.length > 3) {
            body += `\n... and ${transitDetails.length - 3} more`;
        }
    } else {
        body = 'Possible aircraft transit detected. Check the results table for details.';
    }
    
    const notification = new Notification(`🚨 Transit Alert! ${targetName}`, {
        body: body,
        icon: '/static/images/favicon.ico',
        badge: '/static/images/favicon.ico',
        tag: 'flymoon-transit',
        requireInteraction: true,  // Keep notification until user interacts
        silent: false  // Enable system sound (respects OS notification settings)
    });
    
    notification.onclick = function() {
        window.focus();
        this.close();
    };
    
    // Auto-close after 30 seconds (increased from 10)
    setTimeout(() => notification.close(), 30000);
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
    
    // Initialize pause when hidden checkbox
    initPauseWhenHidden();
    
    // Auto-start refresh cycle on page load
    initializeAutoRefresh();
});

/**
 * Initialize automatic refresh cycle on page load
 * - Checks if cache is still valid (< 10 minutes old)
 * - Only fetches if cache expired or missing
 * - Always starts auto-refresh and soft-refresh intervals
 */
function initializeAutoRefresh() {
    console.log('[Init] Starting automatic refresh system');
    
    // Clean up old localStorage values from previous manual/auto mode
    localStorage.removeItem('frequency');
    
    // Check if we have saved observer coordinates
    const lat = document.getElementById("latitude");
    const latitude = parseFloat(lat.value);
    
    if (isNaN(latitude)) {
        console.log('[Init] No observer coordinates - waiting for user input');
        return;
    }
    
    // Check cache age
    const cacheValidSeconds = 600; // 10 minutes
    const secondsSinceUpdate = (Date.now() - window.lastFlightUpdateTime) / 1000;
    
    // Start intervals regardless of cache state
    currentCheckInterval = appConfig.autoRefreshIntervalMinutes * 60;
    remainingSeconds = currentCheckInterval;
    
    // Start auto-refresh interval (10 minutes)
    autoGoInterval = setInterval(goFetch, currentCheckInterval * 1000);
    console.log(`[Init] Auto-refresh interval started (${appConfig.autoRefreshIntervalMinutes} min)`);
    
    // Start countdown timer
    refreshTimerLabelInterval = setInterval(refreshTimer, 1000);
    console.log('[Init] Countdown timer started');
    
    // Start soft refresh interval (15 seconds)
    softRefreshInterval = setInterval(softRefresh, 15000);
    console.log('[Init] Soft refresh started (15s interval)');
    
    // Decide whether to fetch immediately
    if (window.lastFlightUpdateTime === 0) {
        // No cache - fetch immediately
        console.log('[Init] No cache found - fetching initial data');
        goFetch();
    } else if (secondsSinceUpdate >= cacheValidSeconds) {
        // Cache expired - fetch immediately
        console.log(`[Init] Cache expired (${Math.floor(secondsSinceUpdate)}s old) - fetching fresh data`);
        goFetch();
    } else {
        // Cache still valid - use it
        const remainingCacheTime = Math.floor(cacheValidSeconds - secondsSinceUpdate);
        console.log(`[Init] Using cached data (${remainingCacheTime}s until refresh)`);
        
        // Adjust countdown timer to match cache expiry
        remainingSeconds = remainingCacheTime;

        // Restore table from cached flight data (e.g. after back-navigation from telescope)
        if (lastFlightData && lastFlightData.flights) {
            updateFlightTableFull(lastFlightData.flights);
        }
    }
}

function requestNotificationPermission() {
    // Deprecated - replaced by toggleAlerts()
    toggleAlerts();
}

// Last update display
function updateLastUpdateDisplay() {
    const elem = document.getElementById('lastUpdateStatus');
    if (!elem) {
        return;
    }

    // If no update has happened yet, show waiting message
    if (!window.lastFlightUpdateTime) {
        elem.textContent = 'Waiting for flight data...';
        return;
    }

    // Get refresh frequency from currentCheckInterval (always 10 minutes)
    const refreshIntervalSeconds = currentCheckInterval;
    const refreshIntervalMs = refreshIntervalSeconds * 1000;

    const now = Date.now();
    const elapsedMs = now - window.lastFlightUpdateTime;
    let remainingMs = refreshIntervalMs - elapsedMs;
    
    // If countdown expired, wrap around to show next cycle time
    if (remainingMs < 0) {
        // Calculate how far into the next cycle we are
        remainingMs = refreshIntervalMs - (Math.abs(remainingMs) % refreshIntervalMs);
    }
    
    const remainingSeconds = Math.floor(remainingMs / 1000);

    // Format last update time as HH:MM:SS
    const lastUpdateDate = new Date(window.lastFlightUpdateTime);
    const hours = String(lastUpdateDate.getHours()).padStart(2, '0');
    const mins = String(lastUpdateDate.getMinutes()).padStart(2, '0');
    const secs = String(lastUpdateDate.getSeconds()).padStart(2, '0');
    const lastUpdateStr = `${hours}:${mins}:${secs}`;

    // Format remaining time as MM:SS
    const minutes = Math.floor(remainingSeconds / 60);
    const seconds = remainingSeconds % 60;
    const nextUpdateStr = String(minutes).padStart(2, '0') + ':' + String(seconds).padStart(2, '0');

    elem.textContent = `Last update at ${lastUpdateStr}. Next update in ${nextUpdateStr}`;
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

// Pause when hidden preference
function togglePauseWhenHidden() {
    const checkbox = document.getElementById('pauseWhenHidden');
    localStorage.setItem('pauseWhenHidden', checkbox.checked);
    console.log('Pause when hidden:', checkbox.checked);
}

// Initialize pause when hidden checkbox
function initPauseWhenHidden() {
    const checkbox = document.getElementById('pauseWhenHidden');
    if (checkbox) {
        // Default to true if not set
        const pauseWhenHidden = localStorage.getItem('pauseWhenHidden') !== 'false';
        checkbox.checked = pauseWhenHidden;
    }
}

// Update telescope status every 2 seconds
var _telescopeStatusInterval = null;
function startTelescopeStatusPolling() {
    updateTelescopeStatus();
    _telescopeStatusInterval = setInterval(updateTelescopeStatus, 2000);
}
startTelescopeStatusPolling();

// bfcache support: pause all intervals on pagehide so there are no in-flight
// requests blocking restoration. Restart on pageshow if restored from cache.
window.addEventListener('pagehide', () => {
    clearInterval(autoGoInterval);
    clearInterval(softRefreshInterval);
    clearInterval(refreshTimerLabelInterval);
    clearInterval(_telescopeStatusInterval);
    _telescopeStatusInterval = null;
});

window.addEventListener('pageshow', (e) => {
    if (e.persisted) {
        // Page was restored from bfcache — restart polling/timers,
        // but do NOT re-render or re-fetch (data is already in memory).
        startTelescopeStatusPolling();
        autoGoInterval = setInterval(goFetch, currentCheckInterval * 1000);
        softRefreshInterval = setInterval(softRefresh, 15000);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000);
    }
});
