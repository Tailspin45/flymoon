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

/** Radar / log flight id: A–Z and 0–9 only, max 7 (matches backend). */
function normalizeAircraftDisplayId(s) {
    if (s == null || s === undefined) return '';
    return String(s).toUpperCase().replace(/[^A-Z0-9]/g, '').slice(0, 7);
}
if (typeof window !== 'undefined') {
    window.normalizeAircraftDisplayId = normalizeAircraftDisplayId;
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
// Quadrants: N=0-90° (NE), E=90-180° (SE), S=180-270° (SW), W=270-360° (NW)
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
    if (azimuth < 90) {
        return minAltN;   // NE: 0° to 90°
    } else if (azimuth < 180) {
        return minAltE;   // SE: 90° to 180°
    } else if (azimuth < 270) {
        return minAltS;   // SW: 180° to 270°
    } else {
        return minAltW;   // NW: 270° to 360°
    }
}

// Get minimum of all quadrant min altitudes (used only for reconnect logic)
function getMinAltitudeAllQuadrants() {
    const q = getQuadrantMinAltitudes();
    return Math.min(q.min_alt_n, q.min_alt_e, q.min_alt_s, q.min_alt_w);
}

// Return all four quadrant min-altitude values as an object
function getQuadrantMinAltitudes() {
    const d = 15;
    const v = id => { const el = document.getElementById(id); const n = parseFloat(el && el.value); return isNaN(n) ? d : n; };
    return { min_alt_n: v('minAltN'), min_alt_e: v('minAltE'), min_alt_s: v('minAltS'), min_alt_w: v('minAltW') };
}

// Track if we've already shown transit alerts (once per session)
let lastAlertedTransits = {
    sun: false,
    moon: false
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
        // Only alert once per session, regardless of flight ID
        if (!lastAlertedTransits.moon) {
            lastAlertedTransits.moon = true;
            
            const moonTransit = moonTransits[0]; // First one
            
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
    }
    // Note: Never reset lastAlertedTransits.moon — only one alert per session
    
    // Solar transit detected
    if (sunTransits.length > 0) {
        // Only alert once per session, regardless of flight ID
        if (!lastAlertedTransits.sun) {
            lastAlertedTransits.sun = true;
            // Solar transit alert removed per user request
        }
    }
    // Note: Never reset lastAlertedTransits.sun — only one alert per session
}

var alertsEnabled = localStorage.getItem('alertsEnabled') === 'true' || false;
// Sun/Moon target enable/disable state (persisted across sessions)
var sunEnabled  = localStorage.getItem('sunEnabled')  !== 'false'; // default true
var moonEnabled = localStorage.getItem('moonEnabled') !== 'false'; // default true
// Timeouts that clear azimuth arrows when a target sets
var _arrowCleanupTimeouts = {};
// Transit countdown tracking
var nextTransit = null;
var transitCountdownInterval = null;
var _transitBannerDismissed = false;   // user clicked ×
var _transitBannerAutohideTimer = null;
var _transitBannerLastThreshold = null; // last ETA bucket that triggered a re-show

window._dismissTransitBanner = function() {
    _transitBannerDismissed = true;
    if (_transitBannerAutohideTimer) { clearTimeout(_transitBannerAutohideTimer); _transitBannerAutohideTimer = null; }
    const el = document.getElementById('transitCountdown');
    if (el) el.style.display = 'none';
};
var target = "auto"; // Always auto-detect sun and moon
var autoGoInterval = null; // Auto-refresh interval
var refreshTimerLabelInterval = null; // Countdown timer interval
var softRefreshInterval = null; // For client-side position updates
var _radarFastInterval  = null; // 3s radar-only recalculate (active transit candidates only)
var _radarFastInFlight  = false;
window._radarLastUpdateTs = 0; // last successful radar recalculate payload ts (ms)
// Last recalculated positions — dead-reckon ONLY from these (not from original fetch)
// so a hard refresh never jumps the blip back to its starting point.
var _radarBaseFlights   = null;  // {id → flight object with lat/lon at _radarBaseTs}
var _radarBaseTs        = 0;     // Date.now() when _radarBaseFlights was last set

function _radarUpdateBase(flights) {
    // Called by hard refresh and soft refresh to reset the dead-reckoning origin.
    _radarBaseFlights = {};
    _radarBaseTs = Date.now();
    flights.forEach(f => {
        const id = normalizeAircraftDisplayId(f.id || f.name || '');
        if (id) _radarBaseFlights[id] = {...f};
    });
}

async function _radarFastUpdate() {
    if (_radarFastInFlight || !_radarBaseFlights) return;
    if (typeof window.pushInterceptPoint !== 'function') return;

    const dtSec = (Date.now() - _radarBaseTs) / 1000;
    if (dtSec > 300) return; // base too stale

    // Dead-reckon only the delta from the last known good positions
    const candidates = Object.values(_radarBaseFlights)
        .filter(f => parseInt(f.possibility_level ?? 0) >= 1 || f.is_possible_transit === 1)
        .map(f => {
            const updated = {...f};
            if (updated.latitude && updated.longitude && updated.speed && updated.direction) {
                const speedKmPerSec = updated.speed / 3600;
                const distKm = speedKmPerSec * dtSec;
                const hdgRad = updated.direction * Math.PI / 180;
                updated.latitude  += (distKm / 111.32) * Math.cos(hdgRad);
                updated.longitude += (distKm / (111.32 * Math.cos(updated.latitude * Math.PI / 180))) * Math.sin(hdgRad);
            }
            if (updated.is_possible_transit === 1 && updated.time != null)
                updated.time = Math.max(0, updated.time - dtSec / 60);
            return updated;
        });

    if (!candidates.length) return;

    _radarFastInFlight = true;
    try {
        const latitude  = parseFloat(document.getElementById('latitude')?.value);
        const longitude = parseFloat(document.getElementById('longitude')?.value);
        const elevation = parseFloat(document.getElementById('elevation')?.value);
        if (isNaN(latitude) || isNaN(longitude)) return;

        const disabledTargets = [];
        if (!sunEnabled)  disabledTargets.push('sun');
        if (!moonEnabled) disabledTargets.push('moon');

        const res = await fetch('/transits/recalculate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                flights: candidates, latitude, longitude, elevation,
                target, ...getQuadrantMinAltitudes(),
                disabled_targets: disabledTargets
            })
        });
        if (!res.ok) return;
        const data = await res.json();
        if (!data.flights) return;
        window._radarLastUpdateTs = Date.now();

        const activeIds = new Set();
        data.flights.forEach(f => {
            const lvl = parseInt(f.possibility_level ?? 0);
            if (lvl >= 1 || f.is_possible_transit === 1) {
                window.pushInterceptPoint(f);
                activeIds.add(normalizeAircraftDisplayId(f.id || f.name || ''));
            }
        });
        // Only prune when the recalculation actually found some candidates to
        // compare against — avoids wiping blips after a quiet/empty response
        // caused by stale dead-reckoning or a rate-limited data source.
        if (typeof window.pruneRadarTracks === 'function' && candidates.length > 0) {
            window.pruneRadarTracks(activeIds);
        }

        // Only update the scope's transit list when recalculation actively confirms
        // transits.  An empty result (dead-reckoning drift / quiet response) must NOT
        // clear a transit that was confirmed by the last full fetch or soft refresh.
        const transitHits = data.flights.filter(f => parseInt(f.possibility_level ?? 0) >= 1);
        if (typeof window.injectMapTransits === 'function' && transitHits.length > 0) {
            window.injectMapTransits(transitHits, { generatedAtMs: data.generated_at_ms });
        }
    } catch(e) { console.warn('[RadarFast] update failed:', e && e.message ? e.message : e); }
    finally { _radarFastInFlight = false; }
}

function _startRadarFastInterval() {
    if (_radarFastInterval) return;
    _radarFastInterval = setInterval(_radarFastUpdate, 3000);
}
function _stopRadarFastInterval() {
    if (_radarFastInterval) { clearInterval(_radarFastInterval); _radarFastInterval = null; }
}
var remainingSeconds = 120; // Track remaining seconds for countdown (default 2 min)
var lastFlightData = null; // Cache last flight response for soft refresh
window.lastFlightUpdateTime = parseInt(sessionStorage.getItem('lastFlightUpdateTime') || '0', 10);
// Restore cached flight data so the table is populated instantly on back-navigation
try {
    const _cached = sessionStorage.getItem('lastFlightData');
    if (_cached) lastFlightData = JSON.parse(_cached);
} catch(e) { lastFlightData = null; }
var currentCheckInterval = 120; // Current adaptive interval in seconds (default 2 min)
displayTarget();

// App configuration from server
var appConfig = {
    autoRefreshIntervalMinutes: 2   // Default 2 minutes
};

// Load configuration from server. Exposed as a promise so initialization
// code (initializeAutoRefresh) can await the form-field seed before reading
// the latitude input — otherwise on a fresh port origin (e.g. right after a
// Preferences save restarts Flask) the input is still empty when init runs.
window.configReady = fetch('/config')
    .then(response => response.json())
    .then(config => {
        appConfig = config;
        console.log('Loaded config:', appConfig);
        if (config.codeRevision) {
            console.log('Zipcatcher codeRevision:', config.codeRevision, '(should match git after pull + restart)');
        }

        // Sync localStorage with the server config. The server config is
        // authoritative: after a Preferences save Flask restarts with new
        // env vars, and any stale localStorage values from the previous
        // session must be overwritten or the UI renders the old location
        // with no bbox matching the new one.
        const serverLat = parseFloat(config.observerLatitude);
        const serverLon = parseFloat(config.observerLongitude);
        if (!isNaN(serverLat) && !isNaN(serverLon)) {
            const storedLat = parseFloat(localStorage.getItem("latitude"));
            const storedLon = parseFloat(localStorage.getItem("longitude"));
            const observerChanged = isNaN(storedLat) || isNaN(storedLon)
                || Math.abs(storedLat - serverLat) > 1e-6
                || Math.abs(storedLon - serverLon) > 1e-6;

            if (observerChanged) {
                localStorage.setItem("latitude", config.observerLatitude);
                localStorage.setItem("longitude", config.observerLongitude);
                localStorage.setItem("elevation", config.observerElevation || "0");
                console.log('Synced observer position from server config:', serverLat, serverLon);

                const latEl = document.getElementById("latitude");
                const lonEl = document.getElementById("longitude");
                const elevEl = document.getElementById("elevation");
                if (latEl) latEl.value = config.observerLatitude;
                if (lonEl) lonEl.value = config.observerLongitude;
                if (elevEl) elevEl.value = config.observerElevation || "0";
            }

            // Seed bbox when (a) observer changed, or (b) window.lastBoundingBox
            // is unset. Prefer server bbox when present, else default to ±1.5°
            // around the observer.
            if (observerChanged || !window.lastBoundingBox) {
                const bboxLatLL = parseFloat(config.bboxLatLL);
                const bboxLonLL = parseFloat(config.bboxLonLL);
                const bboxLatUR = parseFloat(config.bboxLatUR);
                const bboxLonUR = parseFloat(config.bboxLonUR);
                const serverBboxValid = [bboxLatLL, bboxLonLL, bboxLatUR, bboxLonUR].every(v => !isNaN(v));
                const bbox = serverBboxValid ? {
                    latLowerLeft:  bboxLatLL,
                    lonLowerLeft:  bboxLonLL,
                    latUpperRight: bboxLatUR,
                    lonUpperRight: bboxLonUR,
                } : {
                    latLowerLeft:  Math.round((serverLat - 1.5) * 10000) / 10000,
                    lonLowerLeft:  Math.round((serverLon - 1.5) * 10000) / 10000,
                    latUpperRight: Math.round((serverLat + 1.5) * 10000) / 10000,
                    lonUpperRight: Math.round((serverLon + 1.5) * 10000) / 10000,
                };
                window.lastBoundingBox = bbox;
                localStorage.setItem("boundingBox", JSON.stringify(bbox));
                localStorage.setItem("customBoundingBox", JSON.stringify(bbox));
                console.log('Seeded bounding box from server config:', bbox);
            }
        }
        // Do not POST observer here — stale localStorage would override .env and break telescope azimuth.
        return config;
    })
    .catch(error => {
        console.error('Error loading config:', error);
        return null;
    });

// ─── Data Source Mode ─────────────────────────────────────────────────────────
const DATA_SOURCES = {
    'fa-only': {
        icon: '☁️',
        label: 'FlightAware Only',
        cost: '$14–$87/month',
        color: '#3b82f6',
        note: 'Full metadata every refresh. Expensive — use min-angle masking and toggles to limit active hours.',
    },
    'opensky-only': {
        icon: '🌐',
        label: 'Multisource (no FA)',
        cost: 'Free',
        color: '#10b981',
        note: 'OpenSky + ADSB-One + adsb.lol + adsb.fi + ADS-B Exchange (if key set) in parallel. No FlightAware enrichment. Zero cost.',
    },
    'hybrid': {
        icon: '⚡',
        label: 'Hybrid (Multi-Source + FA)',
        cost: 'Free (within $5 credit)',
        color: '#a78bfa',
        note: 'OpenSky + ADSB-One + adsb.lol + adsb.fi + ADS-B Exchange for positions; FlightAware only on HIGH-probability transits. Typically <250 FA calls/month — covered by the free $5 credit.',
    },
    'adsb-local': {
        icon: '📡',
        label: 'Local ADS-B + FA',
        cost: '~$0/month',
        color: '#06b6d4',
        note: 'Local RTL-SDR / dump1090 receiver merged with OpenSky + ADSB-One. Near-zero ongoing cost. Requires hardware (set ADSB_LOCAL_URL in .env).',
    },
};

function getDataSourceMode() {
    return localStorage.getItem('flymoonDataSource') || 'opensky-only';
}

function updateDataSourceButton() {
    const mode = getDataSourceMode();
    const ds = DATA_SOURCES[mode] || DATA_SOURCES['fa-only'];
    const btn = document.getElementById('dataSourceBtn');
    if (btn) {
        btn.textContent = ds.icon + ' ' + ds.label;
        btn.style.color = ds.color;
        btn.style.borderColor = ds.color;
        btn.title = ds.label + ' — ' + ds.cost + '\n' + ds.note + '\nClick to view cost analysis and change mode.';
    }
}

// Show startup banner once per browser session (sessionStorage flag)
(function showDataSourceBanner() {
    if (sessionStorage.getItem('dsbDismissed')) return;
    const mode = getDataSourceMode();
    const ds = DATA_SOURCES[mode] || DATA_SOURCES['fa-only'];
    const banner = document.getElementById('dataSourceBanner');
    if (!banner) return;
    document.getElementById('dsb-icon').textContent = ds.icon;
    document.getElementById('dsb-mode').textContent = ds.label;
    document.getElementById('dsb-cost').textContent = ds.cost;
    document.getElementById('dsb-note').textContent = ds.note;
    banner.style.borderColor = ds.color;
    banner.style.display = 'flex';
    let autoDismissTimer = null;
    const dismiss = () => {
        if (autoDismissTimer !== null) {
            clearTimeout(autoDismissTimer);
            autoDismissTimer = null;
        }
        banner.style.display = 'none';
        sessionStorage.setItem('dsbDismissed', '1');
    };
    const btn = document.getElementById('dsbDismissBtn') || banner.querySelector('button');
    if (btn) btn.addEventListener('click', dismiss);
    autoDismissTimer = setTimeout(dismiss, 6000);
})();

document.addEventListener('DOMContentLoaded', updateDataSourceButton);

// ─── Rich Table ───────────────────────────────────────────────────────────────

const AIRCRAFT_CATEGORY = {
    0:  { icon: '❓', label: 'Unknown',       desc: 'No information at all' },
    1:  { icon: '✈️', label: 'No Cat Info',   desc: 'No ADS-B emitter category information' },
    2:  { icon: '🛩️', label: 'Light',         desc: 'Light (< 15,500 lbs)' },
    3:  { icon: '✈️', label: 'Small',         desc: 'Small (15,500 – 75,000 lbs)' },
    4:  { icon: '✈️', label: 'Large',         desc: 'Large (75,000 – 300,000 lbs)' },
    5:  { icon: '✈️', label: 'Hi-Vortex',     desc: 'High Vortex Large (e.g. B-757)' },
    6:  { icon: '✈️', label: 'Heavy',         desc: 'Heavy (> 300,000 lbs)' },
    7:  { icon: '⚡', label: 'Hi-Perf',       desc: 'High Performance (> 5g, > 400 kts)' },
    8:  { icon: '🚁', label: 'Rotorcraft',    desc: 'Rotorcraft' },
    9:  { icon: '⛵', label: 'Glider',        desc: 'Glider / sailplane' },
    10: { icon: '🎈', label: 'Lighter-Air',   desc: 'Lighter-than-air' },
    11: { icon: '🪂', label: 'Skydiver',      desc: 'Parachutist / skydiver' },
    12: { icon: '🛩️', label: 'Ultralight',    desc: 'Ultralight / hang-glider / paraglider' },
    13: { icon: '❓', label: 'Reserved',      desc: 'Reserved' },
    14: { icon: '🛸', label: 'UAV',           desc: 'Unmanned Aerial Vehicle' },
    15: { icon: '🚀', label: 'Space',         desc: 'Space / trans-atmospheric vehicle' },
    16: { icon: '🚨', label: 'Emergency Veh', desc: 'Surface Vehicle – Emergency' },
    17: { icon: '🚐', label: 'Service Veh',   desc: 'Surface Vehicle – Service' },
    18: { icon: '🎯', label: 'Obstacle',      desc: 'Point obstacle (incl. tethered balloons)' },
    19: { icon: '🎯', label: 'Cluster Obs',   desc: 'Cluster obstacle' },
    20: { icon: '⎯',  label: 'Line Obs',      desc: 'Line obstacle' },
};

// Category row-tint CSS colours (null = no tint)
const CATEGORY_TINT = {
    9: 'rgba(160,130,220,0.10)',   // Glider
    10: 'rgba(160,130,220,0.10)',  // LTA
    11: 'rgba(30,100,220,0.12)',   // Skydiver
    12: 'rgba(160,130,220,0.08)',  // Ultralight
    14: 'rgba(255,180,0,0.10)',    // UAV
    15: 'rgba(0,220,220,0.08)',    // Space
    16: 'rgba(220,30,30,0.15)',    // Emergency vehicle
};

function buildSquawkBadges(squawk, spi, onGround) {
    const parts = [];
    if (squawk === '7700')
        parts.push(`<span class="sq-badge sq-emrg sq-pulse" title="MAYDAY — aircraft in distress">🚨 MAYDAY</span>`);
    else if (squawk === '7600')
        parts.push(`<span class="sq-badge sq-warn" title="NORDO — lost radio contact">📻 NORDO</span>`);
    else if (squawk === '7500')
        parts.push(`<span class="sq-badge sq-emrg" title="HIJACK in progress">⚠️ HIJACK</span>`);
    else if (squawk >= '4000' && squawk <= '4777')
        parts.push(`<span class="sq-badge sq-mil" title="Military squawk ${squawk}">⚔️ MIL</span>`);
    else if (squawk === '1200')
        parts.push(`<span class="sq-badge sq-vfr" title="VFR flight — squawk 1200">VFR</span>`);
    if (spi)
        parts.push(`<span class="sq-badge sq-ident" title="Pilot has pressed IDENT button">💡 IDENT</span>`);
    if (onGround)
        parts.push(`<span class="sq-badge sq-gnd" title="Aircraft is on the ground">⬛ GND</span>`);
    return parts.join(' ');
}

const COMPASS = ['N','NNE','NE','ENE','E','ESE','SE','SSE','S','SSW','SW','WSW','W','WNW','NW','NNW'];
function headingWord(deg) {
    return COMPASS[Math.round(((deg % 360) + 360) % 360 / 22.5) % 16];
}

function renderRichFlightRow(item, bodyTable) {
    const row = document.createElement('tr');
    const normalizedId = normalizeAircraftDisplayId(item.id);
    const possibilityLevel = item.is_possible_transit === 1 ? parseInt(item.possibility_level) : 0;
    row.setAttribute('data-flight-id', normalizedId);
    row.setAttribute('data-possibility', possibilityLevel);
    if (item.time != null) row.setAttribute('data-transit-time', item.time);

    // Category tint
    const catTint = CATEGORY_TINT[item.category];
    if (catTint) row.style.backgroundColor = catTint;

    // Dim transits where target is below quadrant min altitude (visible but not trackable)
    const belowMinAlt = item.target_below_min_alt === true;
    if (belowMinAlt && possibilityLevel > 0) {
        row.style.opacity = '0.45';
        row.title = `${(item.target || '').toUpperCase()} is below your minimum altitude for this quadrant`;
    }

    // Emergency row styling
    const sq = item.squawk || '';
    if (sq === '7700' || sq === '7500') {
        row.style.outline = '1px solid #ff2020';
        row.classList.add('sq-emergency-row');
    } else if (sq === '7600') {
        row.style.outline = '1px solid #ffa500';
    }

    // Click handler — same behaviour as classic table
    row.addEventListener('click', function(e) {
        if (e.metaKey || e.ctrlKey) {
            if (trackingFlightId === normalizedId) {
                stopTracking();
            } else {
                if (possibilityLevel < MEDIUM_LEVEL) {
                    alert('Track Mode requires medium or high probability transit.');
                    return;
                }
                startTracking(normalizedId);
            }
        } else {
            if (typeof flashAircraftMarker === 'function') flashAircraftMarker(normalizedId);
            if (typeof flashTableRow === 'function') flashTableRow(normalizedId);
            if (typeof toggleFlightRouteTrack === 'function') toggleFlightRouteTrack(item.fa_flight_id, normalizedId);
        }
    });

    // Col 1 — Status badges
    const statusCell = document.createElement('td');
    statusCell.style.whiteSpace = 'nowrap';
    statusCell.innerHTML = buildSquawkBadges(sq, item.spi, item.on_ground) || '<span style="color:#444">—</span>';
    row.appendChild(statusCell);

    // Col 2 — Transit
    const transitCell = document.createElement('td');
    transitCell.style.whiteSpace = 'nowrap';
    if (possibilityLevel >= HIGH_LEVEL) {
        const eta = item.transit_eta_seconds || (item.time * 60);
        const min = Math.floor(eta / 60), sec = Math.floor(eta % 60);
        transitCell.innerHTML = `<span style="color:#4caf50;font-weight:bold">🟢 T-${min}:${String(sec).padStart(2,'0')}</span>`;
    } else if (possibilityLevel === MEDIUM_LEVEL) {
        const eta = item.transit_eta_seconds || (item.time * 60);
        const min = Math.floor(eta / 60), sec = Math.floor(eta % 60);
        transitCell.innerHTML = `<span style="color:#ff9800;font-weight:bold">🟠 T-${min}:${String(sec).padStart(2,'0')}</span>`;
    } else if (possibilityLevel === LOW_LEVEL) {
        transitCell.innerHTML = `<span style="color:#888">⚪ Low</span>`;
    } else {
        transitCell.innerHTML = `<span style="color:#444">—</span>`;
    }
    row.appendChild(transitCell);

    // Col 3 — Target + target altitude
    const tgtCell = document.createElement('td');
    const targetEmoji = item.target === 'sun' ? '☀️' : item.target === 'moon' ? '🌙' : '—';
    let targetAltMarkup = '<span class="target-alt-value">—</span>';
    if (item.target_alt != null) {
        const alt = item.target_alt;
        const targetAltClass = alt < 0 ? 'target-alt-value below-horizon' : (alt < 10 ? 'target-alt-value low-horizon' : 'target-alt-value');
        targetAltMarkup = `<span class="${targetAltClass}">${alt.toFixed(1)}°</span>`;
    }
    tgtCell.innerHTML = `<span class="target-cell-wrap"><span class="target-emoji">${targetEmoji}</span>${targetAltMarkup}</span>`;
    row.appendChild(tgtCell);

    // Col 4 — Aircraft (callsign + type)
    const acCell = document.createElement('td');
    const type = (item.aircraft_type && item.aircraft_type !== 'N/A') ? item.aircraft_type : '';
    const country = item.origin_country || '';
    const acSub = [type, country].filter(Boolean).map(t => `<span style="font-size:0.78em;color:#888">${t}</span>`).join(' ');
    acCell.innerHTML = `<strong style="color:#e0e0e0">${normalizeAircraftDisplayId(item.id)}</strong>${acSub ? `<br>${acSub}` : ''}`;
    row.appendChild(acCell);

    // Col 6 — Altitude
    const altCell = document.createElement('td');
    altCell.style.whiteSpace = 'nowrap';
    if (item.aircraft_elevation_feet != null) {
        const ft = Math.round(item.aircraft_elevation_feet);
        altCell.textContent = ft > 18000 ? `FL${Math.round(ft/100)}` : ft.toLocaleString('en-US');
    } else {
        altCell.innerHTML = '<span style="color:#444">—</span>';
    }
    row.appendChild(altCell);

    // Col 7 — Vertical speed
    const vsCell = document.createElement('td');
    vsCell.style.whiteSpace = 'nowrap';
    if (item.vertical_rate != null) {
        const fpm = Math.round(item.vertical_rate * 196.85);
        if (fpm > 64) vsCell.innerHTML = `<span style="color:#4caf50">▲ +${fpm.toLocaleString()}</span>`;
        else if (fpm < -64) vsCell.innerHTML = `<span style="color:#f44336">▼ ${fpm.toLocaleString()}</span>`;
        else vsCell.innerHTML = `<span style="color:#888">▶ level</span>`;
    } else if (item.elevation_change === 'climbing') {
        vsCell.innerHTML = `<span style="color:#4caf50">▲</span>`;
    } else if (item.elevation_change === 'descending') {
        vsCell.innerHTML = `<span style="color:#f44336">▼</span>`;
    } else {
        vsCell.innerHTML = '<span style="color:#444">—</span>';
    }
    row.appendChild(vsCell);

    // Col 8 — Sky Δ  (separation + optional IMM ±1σ uncertainty band)
    const skyCell = document.createElement('td');
    skyCell.style.whiteSpace = 'nowrap';
    if (item.alt_diff != null && item.az_diff != null) {
        const ad = item.alt_diff.toFixed(1), azd = item.az_diff.toFixed(1);
        const sep = item.angular_separation;
        // Color by angular separation: HIGH ≤0.20°, MEDIUM ≤0.27°
        const c = (sep != null && sep <= 0.20) ? '#4caf50' : (sep != null && sep <= 0.27) ? '#ff9800' : '#888';
        const sigma = item.sep_1sigma;
        const sigmaStr = (sigma != null && sigma > 0)
            ? ` <span style="color:#9e9e9e;font-size:0.82em" title="IMM 1σ position uncertainty">±${sigma.toFixed(2)}°</span>`
            : '';
        skyCell.innerHTML =
            `<span style="color:${c}" title="Δalt ${ad}°, Δaz ${azd}°${sep != null ? ', sep ' + sep.toFixed(2) + '°' : ''}">↕${ad}° ↔${azd}°</span>` +
            sigmaStr;
    } else {
        skyCell.innerHTML = '<span style="color:#444">—</span>';
    }
    row.appendChild(skyCell);

    // Col 9 — Track (heading degrees only)
    const trackCell = document.createElement('td');
    trackCell.style.whiteSpace = 'nowrap';
    if (item.direction != null) {
        trackCell.textContent = `${Math.round(item.direction)}°`;
    } else {
        trackCell.innerHTML = '<span style="color:#444">—</span>';
    }
    row.appendChild(trackCell);

    // Col 10 — Ground speed (kph / mph / kts)
    const spdCell = document.createElement('td');
    spdCell.style.whiteSpace = 'nowrap';
    spdCell.style.textAlign = 'center';
    if (item.speed != null && item.speed > 0) {
        const kph = Math.round(item.speed);
        const mph = Math.round(item.speed * 0.621371);
        const kts = Math.round(item.speed * 0.539957);
        spdCell.textContent = `${kph}/${mph}/${kts}`;
    } else {
        spdCell.innerHTML = '<span style="color:#444">—</span>';
    }
    row.appendChild(spdCell);

    // Col 12 — Src / Age
    const srcCell = document.createElement('td');
    srcCell.style.whiteSpace = 'nowrap';
    const srcMap = {
        'opensky':     { label: 'OS',      color: '#4caf50', title: 'OpenSky' },
        'flightaware': { label: 'FA',      color: '#5b9bd5', title: 'FlightAware' },
        'adsb':        { label: 'ADS-B',   color: '#00e5ff', title: 'ADS-B (ADSB-One / ADS-B Exchange)' },
        'mlat':        { label: 'MLAT',    color: '#ffeb3b', title: 'Multilateration (ADSB-One / ADS-B Exchange)' },
        'flarm':       { label: 'FLARM',   color: '#66bb6a', title: 'FLARM' },
        'asterix':     { label: 'ASTERIX', color: '#ce93d8', title: 'ASTERIX surveillance' },
        'track':       { label: 'TRK',     color: '#2196f3', title: 'Track-derived' },
        'other':       { label: 'OTHER',   color: '#9e9e9e', title: 'Other surveillance source' },
    };
    const src = (item.position_source || 'flightaware').toLowerCase();
    const si = srcMap[src] || { label: src.toUpperCase(), color: '#888', title: src };
    const age = item.position_age_s;
    let ageColor = '#4caf50';
    if (age > 60) ageColor = '#f44336';
    else if (age > 30) ageColor = '#ff9800';
    else if (age > 5) ageColor = '#ffeb3b';
    const ageStr = age != null ? ` <span style="color:${ageColor};font-size:0.8em">${age}s</span>` : '';
    srcCell.innerHTML = `<span style="font-size:0.7em;padding:1px 4px;border-radius:3px;background:${si.color};color:#000" title="${si.title}">${si.label}</span>${ageStr}`;
    row.appendChild(srcCell);

    // Highlight transit rows
    if (item.is_possible_transit === 1) {
        highlightPossibleTransit(possibilityLevel, row);
    }

    bodyTable.appendChild(row);
}

// Table view toggle (classic ↔ rich)
function getTableView() {
    return localStorage.getItem('flymoonTableView') || 'rich';
}

function initTableView() {
    const view = getTableView();
    const classic = document.getElementById('resultsTable');
    const rich = document.getElementById('resultsTableRich');
    const btn = document.getElementById('tableViewToggle');
    if (!classic || !rich) return;
    if (view === 'rich') {
        classic.style.display = 'none';
        rich.style.display = '';
        if (btn) { btn.textContent = '⊟ Classic View'; btn.style.color = '#7ab8d4'; }
    } else {
        classic.style.display = '';
        rich.style.display = 'none';
        if (btn) { btn.textContent = '⊞ Rich View'; btn.style.color = '#a78bfa'; }
    }
}

function toggleTableView() {
    const current = getTableView();
    localStorage.setItem('flymoonTableView', current === 'rich' ? 'classic' : 'rich');
    initTableView();
}

document.addEventListener('DOMContentLoaded', initTableView);

// Page visibility detection - optionally pause polling when page is hidden
document.addEventListener('visibilitychange', function() {
    const pauseWhenHidden = localStorage.getItem("pauseWhenHidden") !== "false"; // Default true
    
    if (document.hidden && pauseWhenHidden) {
        console.log('Page hidden - pausing auto-refresh');
        if (autoGoInterval) clearInterval(autoGoInterval);
        if (refreshTimerLabelInterval) clearInterval(refreshTimerLabelInterval);
        if (softRefreshInterval) clearInterval(softRefreshInterval);
        _stopRadarFastInterval();
    } else if (!document.hidden && pauseWhenHidden) {
        console.log('Page visible - resuming auto-refresh');
        const intervalSecs = currentCheckInterval || appConfig.autoRefreshIntervalMinutes * 60;
        autoGoInterval = setInterval(goFetch, intervalSecs * 1000);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000);
        softRefreshInterval = setInterval(softRefresh, 15000); // Soft refresh every 15 seconds
        _startRadarFastInterval();
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
    if (secondsElapsed > 600) {  // 10 minutes — gives resilience during source outages
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

    // Recalculate transit predictions with updated positions.
    // Only send flights that already have some transit potential — UNLIKELY
    // flights just get position extrapolation on the client, no server round-trip.
    const recalcFlights = updatedFlights.filter(f => f.is_possible_transit === 1);
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

        if (recalcFlights.length === 0) {
            // Nothing to recalculate — just update map positions
            if (mapVisible && typeof updateAircraftMarkers === 'function') {
                updateAircraftMarkers(updatedFlights, latitude, longitude);
            }
            return;
        }
        
        const disabledForRecalc = [];
        if (!sunEnabled)  disabledForRecalc.push('sun');
        if (!moonEnabled) disabledForRecalc.push('moon');

        const response = await fetch('/transits/recalculate', {
            method: 'POST',
            headers: {'Content-Type': 'application/json'},
            body: JSON.stringify({
                flights: recalcFlights,
                latitude: latitude,
                longitude: longitude,
                elevation: elevation,
                target: target,
                ...getQuadrantMinAltitudes(),  // min_alt_n/e/s/w
                disabled_targets: disabledForRecalc
            })
        });
        
        if (response.ok) {
            const recalcData = await response.json();
            // Update table cells in-place — no scroll save/restore needed
            updateFlightTableFull(recalcData.flights);
            
            // Merge recalculated transit candidates back into the full flight list
            // so non-transit aircraft remain visible on the map with updated positions
            const recalcById = {};
            recalcData.flights.forEach(f => { recalcById[normalizeAircraftDisplayId(f.id)] = f; });
            const mergedFlights = updatedFlights.map(f => recalcById[normalizeAircraftDisplayId(f.id)] || f);

            // Update radar dead-reckoning base so fast updates continue from here.
            _radarUpdateBase(mergedFlights);

            // Also push any confirmed transit candidates directly to the radar.
            // This keeps blips alive during full-fetch outages (rate-limited sources)
            // when _radarFastUpdate may not have run recently.
            if (typeof window.pushInterceptPoint === 'function') {
                recalcData.flights.forEach(f => {
                    const lvl = parseInt(f.possibility_level ?? 0);
                    if (lvl >= 1 || f.is_possible_transit === 1) {
                        window.pushInterceptPoint(f);
                    }
                });
            }

            // Update map markers (full set including non-transit for display)
            if (mapVisible && typeof updateAircraftMarkers === 'function') {
                updateAircraftMarkers(mergedFlights, latitude, longitude);
            }

            console.log(`Soft refresh: Recalculated ${recalcFlights.length}/${updatedFlights.length} transit-candidate flights (+${Math.floor(secondsElapsed)}s)`);
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
        const row = document.querySelector(`tr[data-flight-id="${normalizeAircraftDisplayId(flight.id)}"]`);
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
        const row = document.querySelector(`tr[data-flight-id="${normalizeAircraftDisplayId(flight.id)}"]`);
        if (!row) return; // Flight not in table
        
        // Update all relevant cells by column index
        const cells = row.querySelectorAll('td');
        
        // Column indexes (0: Target, 1: ID, 2: Type, 3: Origin, 4: Dest, 5: Target Alt, 6: Plane Alt,
        // 7: Target Az, 8: Plane Az, 9: Alt Diff, 10: Az Diff, 11: Elev Change, 12: Aircraft Alt (ft),
        // 13: Direction, 14: Distance, 15: Speed, 16: Src)
        
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
            cells[9].textContent = flight.alt_diff.toFixed(1) + "º";
            cells[9].style.color = Math.abs(flight.alt_diff) >= 5 ? "#888" : "";
        }
        if (flight.az_diff !== null && cells[10]) {
            cells[10].textContent = flight.az_diff.toFixed(1) + "º";
            cells[10].style.color = Math.abs(flight.az_diff) >= 5 ? "#888" : "";
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
        // Update source badge (cell 16) if position_source changed (e.g. OS→ADS-B)
        if (flight.position_source && cells[16]) {
            const srcMap = {
                "opensky":     { label: "OS",      color: "#4caf50", title: "OpenSky (~10s latency)" },
                "flightaware": { label: "FA",       color: "#888",    title: "FlightAware (60–300s latency)" },
                "track":       { label: "TRK",      color: "#2196f3", title: "Track-derived velocity" },
                "adsb":        { label: "ADS-B",    color: "#00e5ff", title: "ADS-B (<5s latency) — ADSB-One or ADS-B Exchange" },
                "mlat":        { label: "MLAT",     color: "#ffeb3b", title: "Multilateration (ADSB-One/ADS-B Exchange)" },
                "flarm":       { label: "FLARM",    color: "#66bb6a", title: "FLARM" },
                "asterix":     { label: "ASTERIX",  color: "#ce93d8", title: "ASTERIX surveillance" },
                "other":       { label: "OTHER",    color: "#9e9e9e", title: "Other surveillance source" },
            };
            const si = srcMap[flight.position_source] || { label: flight.position_source.toUpperCase(), color: "#888", title: flight.position_source };
            const age = flight.position_age_s != null ? ` (${flight.position_age_s}s)` : "";
            const span = cells[16].querySelector("span");
            if (span) {
                span.textContent = si.label;
                span.style.background = si.color;
                span.title = si.title + age;
            }
        }
        
        // Update row highlight based on new transit status
        const possibilityLevel = flight.is_possible_transit === 1 ? parseInt(flight.possibility_level) : 0;
        highlightPossibleTransit(possibilityLevel, row);
    });
}

// State tracking for toggles
var resultsVisible = false;
var mapVisible = false;
var _freshFetchThisSession = false; // true only after a successful API response this page load

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
                    // Warn if value is below 5° (cost impact)
                    if (value < 5) {
                        showCostModal(value, this);
                        // Don't save/refresh yet — wait for modal decision
                        return;
                    }
                    localStorage.setItem(id, value);
                    lastSavedValue = value;
                    console.log(`Saved ${id}: ${value}`);
                    syncMinAltitudeToServer();

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

/**
 * Push the lowest quadrant min-altitude to the server so the telescope
 * reconnect logic uses the same threshold the user set in the UI.
 */
function syncMinAltitudeToServer() {
    const minAlt = getMinAltitudeAllQuadrants();
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ min_reconnect_altitude: minAlt }),
    }).catch(() => {/* non-critical — server will fall back to .env default */});
}

/**
 * Push observer lat/lon/elev to the server (explicit Save only).
 * Auto-sync on every page load was overriding a correct .env with stale localStorage,
 * which made telescope AZ wrong until restart — and wrong again on next page load.
 */
function syncObserverToServer() {
    const latEl = document.getElementById('latitude');
    const lonEl = document.getElementById('longitude');
    const elevEl = document.getElementById('elevation');
    if (!latEl || !lonEl) return;
    const latitude = parseFloat(latEl.value);
    const longitude = parseFloat(lonEl.value);
    if (isNaN(latitude) || isNaN(longitude)) return;
    const elevation = elevEl ? parseFloat(elevEl.value) : 0;
    fetch('/api/settings', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
            observer_latitude: latitude,
            observer_longitude: longitude,
            observer_elevation: isNaN(elevation) ? 0 : elevation,
        }),
    }).catch(() => {/* non-critical */});
}

// Initialize sticky inputs and toggle buttons when DOM is ready
function initUIControls() {
    setupStickyQuadrantInputs();
    updateToggleButtons();
    syncMinAltitudeToServer();  // tell server the current UI threshold on every page load
}
if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', initUIControls);
} else {
    initUIControls();
}

// ─── Custom tooltip system ────────────────────────────────────────────────────
// Intercepts all native `title` attributes and replaces them with a styled,
// immediately-visible dark tooltip that matches the app theme.
(function initCustomTooltips() {
    const tip = document.createElement('div');
    tip.id = 'flymoonTooltip';
    tip.style.cssText = [
        'position:fixed',
        'background:#1e2a3a',
        'color:#e8edf2',
        'border:1px solid #4a6280',
        'border-radius:5px',
        'padding:7px 11px',
        'font-size:0.8em',
        'max-width:320px',
        'pointer-events:none',
        'z-index:99999',
        'display:none',
        'line-height:1.45',
        'box-shadow:0 3px 10px rgba(0,0,0,0.65)',
        'white-space:pre-wrap',
        'word-wrap:break-word',
    ].join(';');
    document.body.appendChild(tip);

    let activeEl = null;

    document.addEventListener('mouseover', function(e) {
        let el = e.target;
        while (el && el !== document.body) {
            if (el.hasAttribute && el.hasAttribute('title') && el.getAttribute('title')) {
                const text = el.getAttribute('title');
                activeEl = el;
                el.dataset.tipBak = text;
                el.removeAttribute('title');   // suppress native tooltip
                tip.textContent = text;
                tip.style.display = 'block';
                return;
            }
            el = el.parentNode;
        }
    });

    document.addEventListener('mousemove', function(e) {
        if (tip.style.display === 'none') return;
        const pad = 14;
        let x = e.clientX + pad;
        let y = e.clientY + pad + 4;
        const w = tip.offsetWidth  || 280;
        const h = tip.offsetHeight || 40;
        if (x + w + 10 > window.innerWidth)  x = e.clientX - w - pad;
        if (y + h + 10 > window.innerHeight) y = e.clientY - h - pad;
        tip.style.left = Math.max(4, x) + 'px';
        tip.style.top  = Math.max(4, y) + 'px';
    });

    document.addEventListener('mouseout', function(e) {
        if (activeEl && activeEl.dataset.tipBak !== undefined) {
            activeEl.setAttribute('title', activeEl.dataset.tipBak);
            delete activeEl.dataset.tipBak;
            activeEl = null;
        }
        tip.style.display = 'none';
    });
})();

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
            normalizeAircraftDisplayId(f.id) === trackingFlightId
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

        // Update radar base so fast update has fresh position for this flight
        if (_radarBaseFlights) _radarBaseFlights[normalizeAircraftDisplayId(trackedFlight.id||'')] = {...trackedFlight};
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
            cell.textContent = normalizeAircraftDisplayId(value);
        } else if (column === "aircraft_type") {
            cell.textContent = value === "N/A" ? "" : value;
        } else if (column === "aircraft_elevation_feet") {
            const altitude = Math.round(value);
            const altStr = altitude > 18000
                ? `FL${Math.round(altitude / 100)}`
                : altitude.toLocaleString('en-US');
            let vsStr = '';
            if (flight.vertical_rate != null) {
                const fpm = Math.round(flight.vertical_rate * 196.85);
                if (fpm > 64) vsStr = ` <span style="color:#4caf50">▲ +${fpm.toLocaleString()}fpm</span>`;
                else if (fpm < -64) vsStr = ` <span style="color:#f44336">▼ ${fpm.toLocaleString()}fpm</span>`;
                else vsStr = ` <span style="color:#888">▶</span>`;
            } else if (flight.elevation_change === 'C' || flight.elevation_change === 'climbing') {
                vsStr = ` <span style="color:#4caf50">▲</span>`;
            } else if (flight.elevation_change === 'D' || flight.elevation_change === 'descending') {
                vsStr = ` <span style="color:#f44336">▼</span>`;
            }
            cell.innerHTML = altStr + vsStr;
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
            const sigma1 = flight.sep_1sigma;
            const sigTxt = (sigma1 != null && sigma1 > 0)
                ? ` <span style="color:#9e9e9e;font-size:0.82em" title="IMM 1σ uncertainty">±${sigma1.toFixed(2)}°</span>`
                : '';
            cell.innerHTML = value.toFixed(2) + "º" + sigTxt;
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
    syncObserverToServer();

    // Save quadrant min altitudes
    const minAltN = parseFloat(document.getElementById("minAltN").value) || 15;
    const minAltE = parseFloat(document.getElementById("minAltE").value) || 15;
    const minAltS = parseFloat(document.getElementById("minAltS").value) || 15;
    const minAltW = parseFloat(document.getElementById("minAltW").value) || 15;
    localStorage.setItem("minAltN", minAltN);
    localStorage.setItem("minAltE", minAltE);
    localStorage.setItem("minAltS", minAltS);
    localStorage.setItem("minAltW", minAltW);

    // Build a default ±1.5° bbox centred on the new observer position.
    // Clear any previous user-dragged custom bbox so the fresh one takes effect.
    const newBbox = {
        latLowerLeft:  Math.round((latitude  - 1.5) * 10000) / 10000,
        lonLowerLeft:  Math.round((longitude - 1.5) * 10000) / 10000,
        latUpperRight: Math.round((latitude  + 1.5) * 10000) / 10000,
        lonUpperRight: Math.round((longitude + 1.5) * 10000) / 10000,
    };
    window.lastBoundingBox = newBbox;
    // Write to both keys so map.js (customBoundingBox) and fetchFlights (boundingBox) agree
    localStorage.setItem("boundingBox", JSON.stringify(newBbox));
    localStorage.setItem("customBoundingBox", JSON.stringify(newBbox));
    localStorage.removeItem("boundingBoxUserEdited");

    // Immediately centre the map and redraw the bbox if the map is open
    if (typeof map !== 'undefined' && map && mapInitialized) {
        updateObserverMarker(latitude, longitude, elevation);
        map.setView([latitude, longitude], 9);
        updateBoundingBox(newBbox.latLowerLeft, newBbox.lonLowerLeft,
                          newBbox.latUpperRight, newBbox.lonUpperRight, true);
    } else if (typeof initializeMap === 'function' && !mapInitialized) {
        // Map not yet created — it will be centred correctly when it first opens
        const mapContainer = document.getElementById("mapContainer");
        if (mapContainer) mapContainer.style.display = 'block';
        mapVisible = true;
        initializeMap(latitude, longitude);
    }

    alert("Position saved! Refreshing flights...");
    fetchFlights();
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

    // Load saved bounding box — but discard it if the observer is no longer inside it
    // (handles stale bbox left over from a previous observer location)
    if (savedBoundingBox) {
        try {
            const parsed = JSON.parse(savedBoundingBox);
            const obsLat = parseFloat(savedLat);
            const obsLon = parseFloat(savedLon);
            const insideBox = !isNaN(obsLat) && !isNaN(obsLon)
                && obsLat >= parsed.latLowerLeft  && obsLat <= parsed.latUpperRight
                && obsLon >= parsed.lonLowerLeft  && obsLon <= parsed.lonUpperRight;
            if (insideBox) {
                window.lastBoundingBox = parsed;
                console.log("Bounding box loaded from local storage:", window.lastBoundingBox);
            } else {
                // Stale bbox — regenerate a default ±1.5° box around the current observer
                console.warn("Saved bbox does not contain observer — regenerating default bbox");
                const newBbox = {
                    latLowerLeft:  Math.round((obsLat - 1.5) * 10000) / 10000,
                    lonLowerLeft:  Math.round((obsLon - 1.5) * 10000) / 10000,
                    latUpperRight: Math.round((obsLat + 1.5) * 10000) / 10000,
                    lonUpperRight: Math.round((obsLon + 1.5) * 10000) / 10000,
                };
                window.lastBoundingBox = newBbox;
                localStorage.setItem("boundingBox", JSON.stringify(newBbox));
                localStorage.setItem("customBoundingBox", JSON.stringify(newBbox));
                localStorage.removeItem("boundingBoxUserEdited");
            }
        } catch (e) {
            console.error("Error parsing saved bounding box:", e);
        }
    } else if (savedLat && savedLon) {
        // No bbox saved at all — generate a default one around the observer
        const obsLat = parseFloat(savedLat);
        const obsLon = parseFloat(savedLon);
        if (!isNaN(obsLat) && !isNaN(obsLon)) {
            const newBbox = {
                latLowerLeft:  Math.round((obsLat - 1.5) * 10000) / 10000,
                lonLowerLeft:  Math.round((obsLon - 1.5) * 10000) / 10000,
                latUpperRight: Math.round((obsLat + 1.5) * 10000) / 10000,
                lonUpperRight: Math.round((obsLon + 1.5) * 10000) / 10000,
            };
            window.lastBoundingBox = newBbox;
            localStorage.setItem("boundingBox", JSON.stringify(newBbox));
            localStorage.setItem("customBoundingBox", JSON.stringify(newBbox));
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

    // Determine which ETA bucket we're in; re-show banner at each new threshold
    const etaSec = Math.floor(remainingMs / 1000);
    const threshold = etaSec <= 60 ? '1min' : etaSec <= 300 ? '5min' : 'initial';
    const isNewThreshold = threshold !== _transitBannerLastThreshold;
    if (isNewThreshold) {
        _transitBannerLastThreshold = threshold;
        _transitBannerDismissed = false; // re-show at each new threshold
        if (_transitBannerAutohideTimer) clearTimeout(_transitBannerAutohideTimer);
    }

    const msgText = `${targetEmoji} ${targetName} — ${levelText} probability transit in ${timeStr}`;
    // Update text content (don't touch the × button child)
    const btn = countdownDiv.querySelector('button');
    countdownDiv.childNodes.forEach(n => { if (n.nodeType === Node.TEXT_NODE) n.remove(); });
    countdownDiv.insertBefore(document.createTextNode(msgText), btn || null);

    countdownDiv.style.backgroundColor = bgColor;
    countdownDiv.style.color = 'white';

    if (!_transitBannerDismissed) {
        countdownDiv.style.display = 'block';
        // Auto-dismiss after 8 s unless within 60 s of transit (keep it up then)
        if (!_transitBannerAutohideTimer && etaSec > 60) {
            _transitBannerAutohideTimer = setTimeout(() => {
                _transitBannerAutohideTimer = null;
                _transitBannerDismissed = true;
                const el = document.getElementById('transitCountdown');
                if (el) el.style.display = 'none';
            }, 8000);
        }
    }
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

// ─── Sun / Moon target toggles ───────────────────────────────────────────────

function toggleTarget(targetName) {
    if (targetName === 'sun') {
        sunEnabled = !sunEnabled;
        localStorage.setItem('sunEnabled', sunEnabled);
    } else if (targetName === 'moon') {
        moonEnabled = !moonEnabled;
        localStorage.setItem('moonEnabled', moonEnabled);
    }
    updateToggleButtons();

    // Warn if both are off but at least one would be trackable
    if (!sunEnabled && !moonEnabled) {
        const minAlt = getMinAltitudeAllQuadrants();
        const coords = lastFlightData && lastFlightData.targetCoordinates;
        const sunAbove  = coords && coords.sun  && coords.sun.altitude  >= minAlt;
        const moonAbove = coords && coords.moon && coords.moon.altitude >= minAlt;
        if (sunAbove || moonAbove) {
            const targets = [sunAbove ? 'Sun' : null, moonAbove ? 'Moon' : null].filter(Boolean).join(' and ');
            setTimeout(() => alert(
                `⚠️ Both targets are disabled!\n\n${targets} ${sunAbove && moonAbove ? 'are' : 'is'} currently above your minimum angle.\n\nYou may miss transits while both are off.`
            ), 50);
        }
    }

    if (resultsVisible) fetchFlights();
}

function updateToggleButtons() {
    const sunBtn  = document.getElementById('sunToggle');
    const moonBtn = document.getElementById('moonToggle');
    if (sunBtn) {
        sunBtn.style.opacity = sunEnabled ? '1' : '0.45';
        sunBtn.style.textDecoration = sunEnabled ? '' : 'line-through';
        sunBtn.title = sunEnabled ? 'Sun tracking ON (click to disable)' : 'Sun tracking OFF (click to enable)';
    }
    if (moonBtn) {
        moonBtn.style.opacity = moonEnabled ? '1' : '0.45';
        moonBtn.style.textDecoration = moonEnabled ? '' : 'line-through';
        moonBtn.title = moonEnabled ? 'Moon tracking ON (click to disable)' : 'Moon tracking OFF (click to enable)';
    }
}

// ─── Cost impact modal for low min-altitude settings ─────────────────────────
// FA pricing: $0.02 per result set (1 result set = up to 15 flight records).
// max_pages=1 in flight_data.py → 1 result set per bounding-box call = $0.02/call.
// $5/month credit = 250 calls max (24/7 running).
// With min-angle masking limiting active hours to ~8h/day, a 60-min interval
// stays within budget (~$4.80/month). 10-min interval = ~$29/month.

function showCostModal(newValue, inputElement) {
    _pendingCostInput = inputElement;
    // FA charges $0.02 per result set; our bounding-box call returns 1 result set
    const COST_PER_CALL = 0.02;
    const MONTHLY_CREDIT = 5.0;
    // Extra active minutes per day: each degree below 15° adds ~4 min when Sun/Moon
    // would otherwise be below the threshold
    const extraMinPerDay = Math.max(0, (15 - newValue) * 4);
    const extraCallsPerMonth = Math.round((extraMinPerDay / 10) * 30);
    const extraCost = (extraCallsPerMonth * COST_PER_CALL).toFixed(2);
    const content = document.getElementById('costModalContent');
    content.innerHTML =
        `<p>Setting min altitude to <strong>${newValue}°</strong> means the app will ` +
        `check for aircraft for an extra ~${extraMinPerDay} minutes per day while the Sun or Moon ` +
        `is at a very low angle (${newValue}°–15°) — near or below typical obstructions.</p>` +
        `<p>That adds roughly <strong>~${extraCallsPerMonth} extra API calls/month</strong> ` +
        `at <strong>$${COST_PER_CALL.toFixed(2)}/call</strong> = <strong>~$${extraCost}/month extra</strong>.</p>` +
        `<p style="color:#ffcc88; font-size:0.9em;">⚠️ Context: FlightAware charges $${COST_PER_CALL.toFixed(2)} per result set ` +
        `(up to 15 flights). The $${MONTHLY_CREDIT}/month credit covers only ~${Math.floor(MONTHLY_CREDIT/COST_PER_CALL)} calls total. ` +
        `Running 24/7 at 10-min intervals costs ~$29/month — well over budget. ` +
        `Use the min-angle settings and ☀️/🌙 toggles to limit active hours.</p>` +
        `<p style="color:#aaa; font-size:0.9em;">Tip: Use the ☀️/🌙 toggle buttons to disable a target entirely ` +
        `during hours you don't care about it — zero API calls while disabled.</p>`;
    document.getElementById('costModal').style.display = 'flex';
}

// ─── Help / Info modal ────────────────────────────────────────────────────────

const HELP_DOCUMENTS = {
    'quick-start': {
        title: 'Quick Start',
        url: '/help/documents/quick-start',
    },
    'setup-guide': {
        title: 'Setup Guide',
        url: '/help/documents/setup-guide',
    },
    'telescope-guide': {
        title: 'Telescope Guide',
        url: '/help/documents/telescope-guide',
    },
    'zipcatcher-article': {
        title: 'Zipcatcher Article (PDF)',
        url: '/help/documents/zipcatcher-article',
    },
    'transit-position-paper': {
        title: 'Transit Position Paper (PDF)',
        url: '/help/documents/transit-position-paper',
    },
};

const HELP_CONTENT = {
    'min-angle': {
        title: 'Quadrant Min Angle Dial',
        body: `<p>Sets the <strong>minimum altitude</strong> (degrees above your horizon) per compass quadrant at which the Sun or Moon is worth tracking.</p>
<p><strong>Primary purpose:</strong> Exclude directions where your telescope's view is blocked by obstructions — houses, trees, hills, etc. Aircraft in those directions are still tracked, but transit alerts are suppressed when the Sun or Moon is below your obstruction line because the telescope can't see it anyway.</p>
<p>The dial has four sectors — <strong>N / E / S / W</strong> — matching the compass direction the Sun/Moon occupies.</p>
<p><strong>Example:</strong> Set South to 25° if a roofline blocks your view below that angle. Aircraft still appear on the map, but a transit won't be flagged while the Sun is below 25° in the south — you couldn't observe it regardless.</p>
<p><strong>Fringe benefit:</strong> Skipping transit calculations when the target is below the obstruction line also reduces FlightAware API calls slightly. At ≤5° you will see a cost warning because the Sun/Moon barely clears the horizon and transits become geometrically improbable.</p>
<p><strong>Click the centre label</strong> to reset all quadrants to 0°.</p>`
    },
    'force-refresh': {
        title: '⟳ Force Refresh',
        body: `<p>Triggers an immediate FlightAware API call, bypassing the 10-minute server-side cache.</p>
<p><strong>Cost model:</strong> FlightAware charges <strong>$0.02 per result set</strong> (up to 15 flight records each). Our bounding-box search uses <code>max_pages=1</code>, so each call costs exactly <strong>$0.02</strong> regardless of how many aircraft are returned (up to 15).</p>
<p>The Personal tier includes a <strong>$5/month credit = 250 calls</strong>. Running 24/7 at 10-min intervals uses 144 calls/day (~$2.88/day, ~$86/month) — far over budget. Realistically, with min-angle masking limiting active hours to ~8h/day, a 60-min interval costs ~$4.80/month and stays within budget.</p>
<p>Between API refreshes, aircraft positions are <em>interpolated</em> every 15 seconds using last known speed and heading — no extra cost. You rarely need to force a refresh.</p>
<p>The button warns you if the cache is still fresh so you can decide whether to proceed.</p>`
    },
    'sun-moon-toggle': {
        title: '☀️ Sun / 🌙 Moon Toggles',
        body: `<p>Enable or disable transit tracking for each target independently.</p>
<p>When a target is <strong>disabled</strong>:</p>
<ul>
<li>The server skips all transit calculations for it (no CPU cost)</li>
<li>Its azimuth arrow is removed from the map</li>
<li>No transit alerts are generated for it</li>
<li>The FlightAware bounding-box query still runs once (shared between targets)</li>
</ul>
<p>If <strong>both targets are disabled</strong> and one of them is actually above your min-angle threshold, a periodic reminder warns you so you don't accidentally miss a transit.</p>
<p>Toggle state persists across browser restarts.</p>`
    },
    'heatmap': {
        title: '🔥 Traffic Density Heatmap',
        body: `<p>An overlay that reveals which areas within your search bounding box see the most aircraft traffic, built up from every auto-refresh since you first enabled Zipcatcher.</p>
<p><strong>Colour scale:</strong> blue (sparse) → lime → orange → <strong>red</strong> (dense corridors).</p>
<p>The heatmap accumulates silently in the background — every refresh adds the current aircraft positions to a dataset saved in browser storage (capped at 2,000 points, oldest discarded first).</p>
<p>After a few hours you will see the main flight corridors. Corridors passing close to the Sun/Moon azimuth line are your best targets to watch.</p>
<p>Press 🗑 to wipe the dataset and start fresh (e.g. after moving to a new location).</p>`
    },
    'transit-levels': {
        title: 'Transit Probability Levels',
        body: `<p>Each aircraft is assigned a probability level from <strong>combined angular separation</strong> (on-sky degrees between aircraft and Sun/Moon at closest approach):</p>
<table style="width:100%;border-collapse:collapse;margin:10px 0;">
<tr style="border-bottom:1px solid #444"><td style="padding:5px 8px">🟢 <strong>High</strong></td><td style="padding:5px 8px">≤0.20° — very likely near or on disk. <em>Prioritise recording.</em></td></tr>
<tr style="border-bottom:1px solid #444"><td style="padding:5px 8px">🟠 <strong>Medium</strong></td><td style="padding:5px 8px">≤0.27° — close pass; may graze the limb or suit a wide field.</td></tr>
<tr style="border-bottom:1px solid #444"><td style="padding:5px 8px">⚪ <strong>Low</strong></td><td style="padding:5px 8px">≤0.40° — distant geometry; useful for monitoring, unlikely silhouette.</td></tr>
<tr><td style="padding:5px 8px">— <strong>Unlikely</strong></td><td style="padding:5px 8px">&gt;12° — shown but not treated as a transit candidate.</td></tr>
</table>
<p>The Sun and Moon each subtend about <strong>0.5°</strong> of arc. An aircraft crossing within that cone produces a true silhouette transit lasting 0.5–2 seconds.</p>
<p>These bands are defined in <code>get_possibility_level()</code> in <code>src/transit.py</code>. <code>ALT_THRESHOLD</code> / <code>AZ_THRESHOLD</code> in <code>.env</code> affect notifications and search prefiltering, not these labels.</p>`
    },
    'table-columns-rich': {
        title: 'Rich Table — Column Guide',
        body: `<table style="width:100%;border-collapse:collapse;font-size:0.88em;">
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>⚠ Status</strong></td><td style="padding:4px 8px">Emergency squawk badges: 🚨 MAYDAY (7700), 📻 NORDO (7600), ⚠️ HIJACK (7500), ⚔️ MIL (4000-4777), VFR (1200). Also: 💡 IDENT (pilot pressed IDENT), ⬛ GND (on ground). Silent when normal.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>transit</strong></td><td style="padding:4px 8px">🟢 HIGH / 🟠 MEDIUM / ⚪ LOW probability, with countdown to closest approach (T-mm:ss).</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>target / @CPA°</strong></td><td style="padding:4px 8px">☀️/🌙 target plus target altitude at closest approach time (CPA), so values can differ by aircraft.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>aircraft</strong></td><td style="padding:4px 8px">Callsign and ICAO type code (e.g. B738 = Boeing 737-800). Click to flash on map and show route.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>alt / v/s</strong></td><td style="padding:4px 8px">Altitude (FL350 above 18,000ft, otherwise feet). Vertical speed: ▲ climbing (green), ▼ descending (red), ▶ level (grey). V/S in ft/min from ADS-B vertical rate; ▲/▼ only from FlightAware elevation_change flag.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>sky Δ</strong></td><td style="padding:4px 8px">Angular separation at closest approach: ↕ altitude diff, ↔ azimuth diff. Colours follow level: green ≤0.20° HIGH, orange ≤0.27° MEDIUM, gold ≤0.40° LOW.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>track</strong></td><td style="padding:4px 8px">Compass direction (NNW etc.), true heading in degrees, and ground speed in knots.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>src / age</strong></td><td style="padding:4px 8px">Position source badge (ADS-B / MLAT / FLARM / OS / FA) and data age in seconds. Age colour: green ≤5s, yellow ≤30s, orange ≤60s, red >60s.</td></tr>
</table>
<p style="margin-top:10px;font-size:0.82em;color:#aaa">Category, vertical rate, squawk, SPI, and on-ground fields require OpenSky Network or ADS-B Receiver mode. In FlightAware-only mode these columns show — gracefully.</p>
<p style="font-size:0.82em;color:#aaa">Switch between Classic (17-column FA view) and Rich view with the <strong>⊞ Rich View</strong> / <strong>⊟ Classic View</strong> button in the toolbar. Preference is saved between sessions.</p>`
    },
    'table-columns': {
        title: 'Results Table — Column Guide',
        body: `<table style="width:100%;border-collapse:collapse;font-size:0.88em;">
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>target</strong></td><td style="padding:4px 8px">☀️ Sun or 🌙 Moon — which body this aircraft may transit</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>id</strong></td><td style="padding:4px 8px">ICAO flight callsign (e.g. UAL1234). Click to show route on map.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>type</strong></td><td style="padding:4px 8px">Aircraft ICAO type code (B738 = Boeing 737-800, A320 = Airbus A320)</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>target @CPA°</strong></td><td style="padding:4px 8px">Sun/Moon altitude above your horizon at this flight's closest approach time (CPA), not at the current moment. This is why values can differ between aircraft.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>plane angle</strong></td><td style="padding:4px 8px">Predicted altitude angle of the aircraft at closest approach to the target</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>target az</strong></td><td style="padding:4px 8px">Current azimuth of the Sun/Moon — degrees clockwise from true North</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>plane az</strong></td><td style="padding:4px 8px">Predicted azimuth of the aircraft at closest approach</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>△angle</strong></td><td style="padding:4px 8px">Altitude difference at closest approach (one component of separation). Row highlight uses combined separation: 🟢 HIGH ≤0.20°, 🟠 MEDIUM ≤0.27°, ⚪ LOW ≤0.40°.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>△az</strong></td><td style="padding:4px 8px">Azimuth difference at closest approach. Smaller = better.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>elev</strong></td><td style="padding:4px 8px">Flight level in hundreds of feet (350 = FL350 = 35,000 ft)</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>GPS alt (ft)</strong></td><td style="padding:4px 8px">GPS altitude in feet above sea level from ADS-B transponder</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>Hdg (T)</strong></td><td style="padding:4px 8px">True heading — degrees clockwise from true North (not magnetic). Used to project the flight path forward.</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>dist</strong></td><td style="padding:4px 8px">Straight-line distance from your observer position to the aircraft</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>Grnd Spd</strong></td><td style="padding:4px 8px">Aircraft ground speed in kph / mph / knots</td></tr>
<tr><td style="padding:4px 8px;color:#7eb8f7;white-space:nowrap"><strong>src</strong></td><td style="padding:4px 8px">Data source: FA=FlightAware, OS=OpenSky, ADS-B=direct receiver</td></tr>
</table>`
    },
    'alerts': {
        title: '🔔 Alerts',
        body: `<p>Controls audio alerts and browser notifications when a potential transit is detected.</p>
<p>When <strong>enabled</strong> and a High or Medium probability transit is found:</p>
<ul>
<li>An audio chime plays</li>
<li>A browser push notification appears (requires notification permission)</li>
<li>A countdown banner shows at the top of the screen</li>
</ul>
<p>Alerts fire even when the tab is in the background, unless <em>Pause Hidden</em> is also checked.</p>
<p>You can toggle alerts off temporarily (e.g. at night) without stopping the tracker.</p>`
    },
    'near-misses': {
        title: '📋 Near-Miss Log',
        body: `<p>A persistent log of every aircraft that came within the transit detection threshold of the Sun or Moon, sorted newest first.</p>
<p>Each row records the <strong>closest angular approach</strong> for that flight:</p>
<ul>
<li><strong>Alt° / Az°</strong> — altitude and azimuth separation at closest approach</li>
<li><strong>Sep°</strong> — total angular separation (red &lt;0.25° = inside the disk; orange &lt;0.5° = grazing limb). <span style="color:#9e9e9e">±σ</span> after the value is the 1-sigma position uncertainty from the IMM Kalman filter — narrows over successive ADS-B updates.</li>
<li><strong>ETA min</strong> — minutes to predicted closest approach at time of detection</li>
<li><strong>Scope</strong> — whether the Seestar was connected and in which mode</li>
</ul>
<p>The log is stored on the server and survives restarts. Use it to review events after the fact and identify which flight corridors are transit-prone.</p>`
    },
    'help-docs': {
        title: '📚 Help Documents',
        body: `<p>Open the main documentation set directly in a large in-app modal:</p>
<ul>
<li><a href="#" onclick="return openHelpDocumentModal('quick-start')">Quick Start</a></li>
<li><a href="#" onclick="return openHelpDocumentModal('setup-guide')">Setup Guide</a></li>
<li><a href="#" onclick="return openHelpDocumentModal('telescope-guide')">Telescope Guide</a></li>
<li><a href="#" onclick="return openHelpDocumentModal('zipcatcher-article')">Zipcatcher Article (PDF)</a></li>
<li><a href="#" onclick="return openHelpDocumentModal('transit-position-paper')">Transit Position Paper (PDF)</a></li>
</ul>`
    }
};

function showInfo(topic) {
    const c = HELP_CONTENT[topic];
    if (!c) return;
    document.getElementById('infoModalTitle').textContent = c.title;
    document.getElementById('infoModalBody').innerHTML = c.body;
    const modal = document.getElementById('infoModal');
    modal.style.display = 'flex';
}

function closeInfoModal() {
    document.getElementById('infoModal').style.display = 'none';
}

function openHelpDocumentModal(docId) {
    const doc = HELP_DOCUMENTS[docId];
    if (!doc) return false;

    const modal = document.getElementById('helpDocModal');
    const frame = document.getElementById('helpDocFrame');
    const title = document.getElementById('helpDocModalTitle');
    const openNewTabLink = document.getElementById('helpDocOpenNewTab');
    if (!modal || !frame || !title || !openNewTabLink) return false;

    const url = `${doc.url}?t=${Date.now()}`;
    title.textContent = doc.title;
    frame.src = url;
    openNewTabLink.href = url;
    modal.style.display = 'flex';
    return false;
}

function closeHelpDocumentModal() {
    const modal = document.getElementById('helpDocModal');
    const frame = document.getElementById('helpDocFrame');
    if (frame) frame.src = 'about:blank';
    if (modal) modal.style.display = 'none';
}

// Electron Help menu hooks into this to open docs inside the large modal.
window.openHelpDocumentFromMenu = function (docId) {
    const opened = openHelpDocumentModal(docId);
    return opened === false && !!HELP_DOCUMENTS[docId];
};

function dismissCostModal(keep) {
    document.getElementById('costModal').style.display = 'none';
    if (!keep && _pendingCostInput) {
        // User cancelled — revert to 15° and save
        _pendingCostInput.value = '15';
        localStorage.setItem(_pendingCostInput.id, '15');
    } else if (keep && _pendingCostInput) {
        // User accepted the low value — persist it and refresh
        const val = parseFloat(_pendingCostInput.value);
        if (!isNaN(val)) {
            localStorage.setItem(_pendingCostInput.id, val);
        }
        syncMinAltitudeToServer();
        if (resultsVisible) fetchFlights();
    }
    _pendingCostInput = null;
}

// ─── Schedule azimuth arrow cleanup when a target sets ────────────────────────

function scheduleAzimuthArrowCleanup(riseSetTimes) {
    // Cancel any previously scheduled cleanups
    Object.values(_arrowCleanupTimeouts).forEach(t => clearTimeout(t));
    _arrowCleanupTimeouts = {};

    const now = new Date();
    ['sun', 'moon'].forEach(targetName => {
        const setStr = riseSetTimes[targetName + '_set'];
        if (!setStr) return;
        const [hh, mm] = setStr.split(':').map(Number);
        const setTime = new Date(now);
        setTime.setHours(hh, mm, 30, 0); // 30s buffer after published set time
        if (setTime <= now) setTime.setDate(setTime.getDate() + 1); // crossed midnight
        const msUntil = setTime - now;
        if (msUntil > 0 && msUntil < 86400000) {
            _arrowCleanupTimeouts[targetName] = setTimeout(() => {
                if (typeof clearAzimuthArrow === 'function') clearAzimuthArrow(targetName);
            }, msUntil);
            console.log(`[AzimuthCleanup] ${targetName} arrow will be removed in ${Math.round(msUntil/60000)} min (at ${setStr})`);
        }
    });
}

// Periodic "both targets off" reminder while within min-altitude parameters
let _bothOffReminderInterval = null;
function _bothOffReminderTick() {
    if (!sunEnabled && !moonEnabled && lastFlightData && lastFlightData.targetCoordinates) {
        const minAlt = getMinAltitudeAllQuadrants();
        const coords = lastFlightData.targetCoordinates;
        const sunAbove  = coords.sun  && coords.sun.altitude  >= minAlt;
        const moonAbove = coords.moon && coords.moon.altitude >= minAlt;
        if ((sunAbove || moonAbove) && alertsEnabled) {
            const targets = [sunAbove ? 'Sun' : null, moonAbove ? 'Moon' : null].filter(Boolean).join(' and ');
            console.warn(`[Zipcatcher] Both targets disabled but ${targets} is in range!`);
            if (typeof showNotification === 'function') {
                showNotification(`Both targets off — ${targets} is in range`, 'warning');
            }
        }
    }
}
_bothOffReminderInterval = setInterval(_bothOffReminderTick, 60000);

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
    
    // Only warn about cost if a fresh fetch has already succeeded this session (i.e. this is a re-refresh, not the initial load)
    if (_freshFetchThisSession && window.lastFlightUpdateTime > 0 && secondsSinceUpdate < cacheValidSeconds) {
        const minutesRemaining = Math.ceil((cacheValidSeconds - secondsSinceUpdate) / 60);
        const secondsRemaining = Math.floor(cacheValidSeconds - secondsSinceUpdate);
        
        const confirmed = confirm(
            `⚠️ Cache Still Fresh\n\n` +
            `Last update: ${secondsRemaining}s ago\n` +
            `Cache expires in: ${minutesRemaining} minute(s)\n\n` +
            `Each FlightAware API call costs $0.02 (1 result set).\n` +
            `The $5/month credit covers only ~250 calls total.\n` +
            `Aircraft positions are being interpolated automatically every 15 seconds.\n\n` +
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

    // Guard against empty/NaN coordinates reaching the server — Flask returns
    // 400 on float(""), and tooltip-less "Server error: 400" is worthless to
    // the user. Callers that bypass the isNaN checks (sun/moon toggle,
    // updateTrackedFlight, direct console calls) land here.
    const _latN = parseFloat(latitude);
    const _lonN = parseFloat(longitude);
    if (!isFinite(_latN) || !isFinite(_lonN)) {
        console.warn('[fetchFlights] Skipping — observer coordinates not set yet',
                     { latitude, longitude });
        document.getElementById("loadingSpinner").style.display = "none";
        return;
    }

    let hasVeryPossibleTransits = false;
    let transitDetails = []; // Collect high-priority transits for notification

    const bodyTable = document.getElementById('flightData');
    const richBodyTable = document.getElementById('richFlightData');
    let alertNoResults = document.getElementById("noResults");
    let alertTargetUnderHorizon = document.getElementById("targetUnderHorizon");
    alertNoResults.innerHTML = '';
    alertTargetUnderHorizon = '';

    const minAltQ = getQuadrantMinAltitudes();
    // Use wide outer-search thresholds so all classifiable transits are returned.
    // Wide prefilter (5°) so server returns candidates; classification bands are
    // HIGH ≤0.20°, MEDIUM ≤0.27°, LOW ≤0.40° (see get_possibility_level in src/transit.py).
    const altThreshold = 5.0;
    const azThreshold = 5.0;
    
    let endpoint_url = (
        `/flights?target=${encodeURIComponent(target)}`
        + `&latitude=${encodeURIComponent(latitude)}`
        + `&longitude=${encodeURIComponent(longitude)}`
        + `&elevation=${encodeURIComponent(elevation)}`
        + `&min_alt_n=${encodeURIComponent(minAltQ.min_alt_n)}`
        + `&min_alt_e=${encodeURIComponent(minAltQ.min_alt_e)}`
        + `&min_alt_s=${encodeURIComponent(minAltQ.min_alt_s)}`
        + `&min_alt_w=${encodeURIComponent(minAltQ.min_alt_w)}`
        + `&alt_threshold=${encodeURIComponent(altThreshold)}`
        + `&az_threshold=${encodeURIComponent(azThreshold)}`
        + `&send-notification=true`
        + `&data_source=${encodeURIComponent(localStorage.getItem('flymoonDataSource') || 'opensky-only')}`
    );

    // Pass any user-disabled targets so the server skips them (saves API calls)
    const disabledTargets = [];
    if (!sunEnabled)  disabledTargets.push('sun');
    if (!moonEnabled) disabledTargets.push('moon');
    if (disabledTargets.length > 0) {
        endpoint_url += `&disabled_targets=${encodeURIComponent(disabledTargets.join(','))}`;
    }

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

    const fetchController = new AbortController();
    const fetchTimeout = setTimeout(() => fetchController.abort(), 55000); // 55s hard timeout

    fetch(endpoint_url, { signal: fetchController.signal })
    .then(response => {
        clearTimeout(fetchTimeout);
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
        if (richBodyTable) richBodyTable.innerHTML = '';

        // Record update time and cache data
        clearErrorBanner();
        _freshFetchThisSession = true;
        window.lastFlightUpdateTime = Date.now();
        sessionStorage.setItem('lastFlightUpdateTime', String(window.lastFlightUpdateTime));
        lastFlightData = data;
        _radarUpdateBase(data.flights || []);
        try { sessionStorage.setItem('lastFlightData', JSON.stringify(data)); } catch(e) {}
        updateLastUpdateDisplay();
        if (data.sources_used) updateApiSourceLights(data.sources_used);

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
        
        // When all targets are below the horizon, keep fetching flights (for map
        // display) but skip the fast soft-refresh cycle that recalculates transits.
        const anyTargetUp = data.targetCoordinates && Object.entries(data.targetCoordinates).some(
            ([name, c]) => c.altitude > 0 && !(data.disabledTargets || []).includes(name)
        );
        if (!anyTargetUp) {
            if (softRefreshInterval) clearInterval(softRefreshInterval);
            softRefreshInterval = null;
        }

        // LINE 1: Tracking status - Sun and Moon with weather
        let trackingParts = [];

        // Always show Sun status
        if(data.targetCoordinates && data.targetCoordinates.sun) {
            const isDisabled = data.disabledTargets && data.disabledTargets.includes('sun');
            const isTracking = !isDisabled && data.trackingTargets && data.trackingTargets.includes('sun');
            const alt = data.targetCoordinates.sun.altitude;
            const belowHorizon = alt <= 0;
            const belowMinAlt = !belowHorizon && !isTracking && !isDisabled;
            const status = isDisabled ? '<span style="opacity:0.5">Sun: Off</span>'
                         : isTracking ? `<span style="color: #FFD700">Sun: Tracking</span>`
                         : belowHorizon ? 'Sun: Below horizon'
                         : belowMinAlt ? `<span style="color: #ff9800">Sun: Below min alt</span>`
                         : 'Sun: Not tracking';
            trackingParts.push(status);
        }

        // Always show Moon status
        if(data.targetCoordinates && data.targetCoordinates.moon) {
            const isDisabled = data.disabledTargets && data.disabledTargets.includes('moon');
            const isTracking = !isDisabled && data.trackingTargets && data.trackingTargets.includes('moon');
            const alt = data.targetCoordinates.moon.altitude;
            const belowHorizon = alt <= 0;
            const belowMinAlt = !belowHorizon && !isTracking && !isDisabled;
            const status = isDisabled ? '<span style="opacity:0.5">Moon: Off</span>'
                         : isTracking ? `<span style="color: #FFD700">Moon: Tracking</span>`
                         : belowHorizon ? 'Moon: Below horizon'
                         : belowMinAlt ? `<span style="color: #ff9800">Moon: Below min alt</span>`
                         : 'Moon: Not tracking';
            trackingParts.push(status);
        }

        // Weather (no color styling)
        if(data.weather && data.weather.cloud_cover !== null) {
            trackingParts.push(`☁️ ${data.weather.cloud_cover}% clouds`);
        }

        document.getElementById("trackingStatus").innerHTML = trackingParts.join("&nbsp;&nbsp;&nbsp;&nbsp;");

        // LINES 3+4: Celestial positions and rise/set times, per-target columns
        const sunInfoEl  = document.getElementById("sunInfo");
        const moonInfoEl = document.getElementById("moonInfo");
        const coords = data.targetCoordinates || {};
        const rst    = data.riseSetTimes || {};

        if(sunInfoEl) {
            let html = '';
            if(coords.sun) {
                html += `🌞 Alt: ${coords.sun.altitude.toFixed(1)}° Az: ${coords.sun.azimuthal.toFixed(1)}°`;
            }
            const riseSet = [rst.sun_rise ? `↑${rst.sun_rise}` : '', rst.sun_set ? `↓${rst.sun_set}` : ''].filter(Boolean).join(' ');
            if(riseSet) html += `<br><span style="color:#bbb;">${riseSet}</span>`;
            sunInfoEl.innerHTML = html;
        }

        if(moonInfoEl) {
            let html = '';
            if(coords.moon) {
                html += `🌙 Alt: ${coords.moon.altitude.toFixed(1)}° Az: ${coords.moon.azimuthal.toFixed(1)}°`;
            }
            const riseSet = [rst.moon_rise ? `↑${rst.moon_rise}` : '', rst.moon_set ? `↓${rst.moon_set}` : ''].filter(Boolean).join(' ');
            if(riseSet) html += `<br><span style="color:#bbb;">${riseSet}</span>`;
            moonInfoEl.innerHTML = html;
        }

        // Schedule arrow removal when each target sets (fixes stale arrow after moon/sun sets)
        if (data.riseSetTimes) scheduleAzimuthArrowCleanup(data.riseSetTimes);

        // Check if any targets are trackable; clear flight table when nothing is up
        if(data.trackingTargets && data.trackingTargets.length === 0) {
            // Distinguish: is the target genuinely below the horizon, or just below the user's min angle?
            const coords = data.targetCoordinates || {};
            const sunAlt  = coords.sun  ? coords.sun.altitude  : null;
            const moonAlt = coords.moon ? coords.moon.altitude : null;
            const anyAboveHorizon = (sunAlt != null && sunAlt > 0) || (moonAlt != null && moonAlt > 0);
            if (anyAboveHorizon) {
                alertNoResults.innerHTML = "Sun/Moon is above the horizon but below your Min Angle setting — lower the Min Angle to see transits";
            } else {
                alertNoResults.innerHTML = "Sun and Moon are below the horizon — flights shown, no transit prediction";
            }
            // Don't clear flights — backend still sends them for map display
        }

        // Use server bbox only if user hasn't set a custom one via savePosition()
        if(data.boundingBox && !localStorage.getItem("boundingBox")) {
            window.lastBoundingBox = data.boundingBox;
            console.log('Bounding box (from server):', window.lastBoundingBox);
        }

        // Deduplicate flights by ID for display (keep highest possibility level)
        const seenFlights = {};
        data.flights.forEach(flight => {
            const id = normalizeAircraftDisplayId(flight.id);
            if (!id) return;
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
        const filteredFlights = Object.entries(seenFlights).map(([id, flight]) => ({
            ...flight,
            id,
        }));
        console.log(`Dedupe: ${data.flights.length} flights -> ${filteredFlights.length} unique`);

        // Radar base update — _radarFastUpdate handles ongoing pushInterceptPoint calls.
        // But populate transit list immediately so it doesn't wait 3s for the first cycle.
        if (typeof window.pushInterceptPoint === 'function') {
            filteredFlights.forEach(f => {
                const lvl = parseInt(f.possibility_level ?? 0);
                if (lvl >= 1 || f.is_possible_transit === 1) window.pushInterceptPoint(f);
            });
        }
        if (typeof window.injectMapTransits === 'function') {
            window.injectMapTransits(
                filteredFlights.filter(f => parseInt(f.possibility_level ?? 0) >= 1),
                { generatedAtMs: data.generated_at_ms }
            );
        }

        // Debug: show transit candidates
        filteredFlights.forEach(f => {
            if (f.is_possible_transit) {
                console.log(`  ${f.id} (${f.target}): level=${f.possibility_level}, is_transit=${f.is_possible_transit}`);
            }
        });

        // Find next HIGH or MEDIUM probability transit for countdown (skip below-min-alt)
        const _prevTransitFlight = nextTransit ? nextTransit.flight : null;
        nextTransit = null;
        filteredFlights.forEach(flight => {
            if (flight.target_below_min_alt === true) return;
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

        // Reset dismissed state when a different transit becomes imminent
        if (nextTransit && nextTransit.flight !== _prevTransitFlight) {
            _transitBannerDismissed = false;
            _transitBannerLastThreshold = null;
            if (_transitBannerAutohideTimer) { clearTimeout(_transitBannerAutohideTimer); _transitBannerAutohideTimer = null; }
        }

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

        // Show all flights in the table on hard refresh.
        // Transit candidates (LOW/MEDIUM/HIGH) get full detail; non-transit rows are static
        // between refreshes (positions update on the map via soft refresh, not in the table).
        const tableFlights = filteredFlights;
        const unlikelyCount = filteredFlights.filter(f => f.is_possible_transit !== 1).length;
        if (tableFlights.length === 0) {
            alertNoResults.innerHTML = "No flights visible";
        } else if (unlikelyCount > 0 && tableFlights.filter(f => f.is_possible_transit === 1).length === 0) {
            alertNoResults.innerHTML = `${unlikelyCount} flight${unlikelyCount !== 1 ? 's' : ''} in area — none within transit range`;
        } else if (unlikelyCount > 0) {
            alertNoResults.innerHTML = `+${unlikelyCount} flight${unlikelyCount !== 1 ? 's' : ''} in area outside transit range`;
        }

        // Build both tables off-DOM using DocumentFragment to avoid per-row reflow
        const classicFrag = document.createDocumentFragment();
        const richFrag = document.createDocumentFragment();

        tableFlights.forEach(item => {
            const row = document.createElement('tr');

            // Store normalized flight ID, possibility level, and transit time for cross-referencing
            const normalizedId = normalizeAircraftDisplayId(item.id);
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
                    val.textContent = normalizeAircraftDisplayId(value);
                } else if (column === "aircraft_type") {
                    // Show aircraft type, hide "N/A"
                    val.textContent = value === "N/A" ? "" : value;
                } else if (column === "origin" || column === "destination") {
                    // Scrunch origin/destination with max-width and ellipsis
                    const display = (value === "N/A" || value === "N/D") ? "" : value;
                    val.textContent = display;
                    val.style.maxWidth = "60px";
                    val.style.overflow = "hidden";
                    val.style.textOverflow = "ellipsis";
                    val.style.whiteSpace = "nowrap";
                    val.title = display;
                } else if (column === "speed") {
                    // Show speed in kph / mph / kts
                    const kph = Math.round(value);
                    const mph = Math.round(value * 0.621371);
                    const kts = Math.round(value * 0.539957);
                    val.textContent = `${kph}/${mph}/${kts}`;
                    val.style.textAlign = 'center';
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
                    const displayValue = value.toFixed(1);
                    val.textContent = displayValue + "º";
                    // Grey if >= 5°
                    if (Math.abs(value) >= 5) {
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
                } else if (value === "N/A") {
                    val.textContent = "";
                } else if (value === "N/D") {
                    val.textContent = "";
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
                "opensky":      { label: "OS",      color: "#4caf50", title: "OpenSky (~10s latency)" },
                "flightaware":  { label: "FA",       color: "#888",    title: "FlightAware (60–300s latency)" },
                "track":        { label: "TRK",      color: "#2196f3", title: "Track-derived velocity" },
                "adsb":         { label: "ADS-B",    color: "#00e5ff", title: "ADS-B (<5s latency) — ADSB-One or ADS-B Exchange" },
                "mlat":         { label: "MLAT",     color: "#ffeb3b", title: "Multilateration (ADSB-One / ADS-B Exchange)" },
                "flarm":        { label: "FLARM",    color: "#66bb6a", title: "FLARM" },
                "asterix":      { label: "ASTERIX",  color: "#ce93d8", title: "ASTERIX surveillance" },
                "other":        { label: "OTHER",    color: "#9e9e9e", title: "Other surveillance source" },
            };
            const srcInfo = srcMap[src] || { label: src.toUpperCase(), color: "#888", title: src };
            const ageStr = srcAge != null ? ` (${srcAge}s)` : "";
            srcCell.innerHTML = `<span style="font-size:0.7em;padding:1px 4px;border-radius:3px;background:${srcInfo.color};color:#fff;white-space:nowrap" title="${srcInfo.title}${ageStr}">${srcInfo.label}</span>`;
            row.appendChild(srcCell);

            if(item["is_possible_transit"] == 1) {
                const possibilityLevel = parseInt(item["possibility_level"]);
                highlightPossibleTransit(possibilityLevel, row);

                // Dim classic row when target is below quadrant min altitude
                if (item.target_below_min_alt === true) {
                    row.style.opacity = '0.45';
                    row.title = `${(item.target || '').toUpperCase()} is below your minimum altitude for this quadrant`;
                }

                if(possibilityLevel == MEDIUM_LEVEL || possibilityLevel == HIGH_LEVEL) {
                    // Don't count below-min-alt transits for notifications
                    if (item.target_below_min_alt !== true) {
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
            }

            classicFrag.appendChild(row);

            // Also render into rich table fragment
            renderRichFlightRow(item, richFrag);
        });

        // Single DOM write for both tables — no per-row reflow
        bodyTable.appendChild(classicFrag);
        if (richBodyTable) richBodyTable.appendChild(richFrag);

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
        // Slide up hero image after first data load
        const hero = document.getElementById("heroImageWrap");
        if (hero) {
            hero.style.opacity = "0";
            hero.style.maxHeight = "0";
        }
    })
    .catch(error => {
        clearTimeout(fetchTimeout);
        // Hide loading spinner on error
        document.getElementById("loadingSpinner").style.display = "none";
        document.getElementById("results").style.display = "block";
        
        const errorMsg = error.message || error.toString() || "Unknown error";
        const stack = error.stack ? `\n\n${error.stack}` : "";
        let displayMsg;
        if (error.name === 'AbortError') {
            displayMsg = "⚠️ Request timed out after 55 seconds.\n\nThe server may be overloaded or the FlightAware API is slow. Try again in a moment.";
        } else if (errorMsg.includes("AEROAPI") || errorMsg.includes("API key")) {
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
    row.classList.remove('possibleTransitHighlightLow', 'possibleTransitHighlightMedium', 'possibleTransitHighlightHigh');
    if      (possibilityLevel == LOW_LEVEL)    row.classList.add('possibleTransitHighlightLow');
    else if (possibilityLevel == MEDIUM_LEVEL) row.classList.add('possibleTransitHighlightMedium');
    else if (possibilityLevel == HIGH_LEVEL)   row.classList.add('possibleTransitHighlightHigh');
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
        const normalizedId = normalizeAircraftDisplayId(flight.id);
        line.addEventListener('click', () => {
            if (typeof flashAircraftMarker === 'function') {
                flashAircraftMarker(normalizedId);
            }
        });

        line.style.opacity = "0.9";
        barsContainer.appendChild(line);
    });
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
    const rich = document.getElementById("richFlightData");
    if (rich) rich.innerHTML = "";
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
        requireInteraction: false, // Auto-dismiss after 30 seconds
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
async function initializeAutoRefresh() {
    console.log('[Init] Starting automatic refresh system');

    // Clean up old localStorage values from previous manual/auto mode
    localStorage.removeItem('frequency');

    // Wait for the /config fetch to finish seeding localStorage and form
    // fields before checking the latitude input. Without this, on a fresh
    // port origin (right after a Preferences save restarts Flask) the field
    // would still be empty and init would bail out, leaving the countdown
    // and auto-refresh intervals unstarted.
    if (window.configReady && typeof window.configReady.then === 'function') {
        try { await window.configReady; } catch (_) {}
    }

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
    _startRadarFastInterval();
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

// Telescope status indicator + disconnect banner
let _scopeWasConnected = null;   // null = unknown (first poll)
let _scopeBannerDismissed = false;
let _disconnectPollCount = 0;    // consecutive polls where scope is disconnected

function dismissTelescopeBanner() {
    _scopeBannerDismissed = true;
    const banner = document.getElementById('telescopeDisconnectBanner');
    if (banner) banner.style.display = 'none';
}

function updateTelescopeStatus() {
    fetch('/telescope/status')
        .then(response => response.json())
        .then(data => {
            const statusLight = document.getElementById('telescopeStatusLight');
            const banner      = document.getElementById('telescopeDisconnectBanner');
            const detail      = document.getElementById('telescopeBannerDetail');
            const isEnabled   = data.enabled;
            const isConnected = data.connected;

            // Status light
            if (statusLight) {
                if (isConnected) {
                    statusLight.style.backgroundColor = '#00ff00';
                    statusLight.title = 'Telescope connected';
                } else if (!isEnabled) {
                    statusLight.style.backgroundColor = '#555';
                    statusLight.title = 'Telescope disabled';
                } else {
                    statusLight.style.backgroundColor = '#ff0000';
                    statusLight.title = 'Telescope disconnected';
                }
            }

            // Disconnect banner — only show if telescope is enabled and was previously
            // connected (i.e. we lost a live connection, not just startup with no scope)
            if (banner && isEnabled) {
                if (!isConnected) {
                    _disconnectPollCount++;
                    // Phase 2: after ~1 minute of failed reconnects, suggest scope is offline
                    if (detail) {
                        if (_disconnectPollCount > 30) {
                            detail.textContent = 'Scope may be offline — will keep checking.';
                        } else {
                            detail.textContent = 'Reconnecting automatically — transit recording suspended.';
                        }
                    }
                    // Was connected before → show banner (unless user dismissed this drop)
                    if (_scopeWasConnected === true && !_scopeBannerDismissed) {
                        banner.style.display = 'flex';
                    }
                } else {
                    // Reconnected — hide banner and reset counters
                    _disconnectPollCount = 0;
                    banner.style.display = 'none';
                    _scopeBannerDismissed = false;
                }
            }

            _scopeWasConnected = isConnected;
        })
        .catch(() => {
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

// ── Data Sources activity lights ─────────────────────────────────────────────
// sources_used: { source_name: { ts: float, count: int }, ... }
// Each response: blink the light if ts advanced since last render, then go dark.
// Odometer shows server-side call count.

var _apiPaused = false;
var _lastSourceTs = null; // null = first render this page load

function updateApiSourceLights(sourcesUsed) {
    const panel = document.getElementById('dataSourcesPanel');
    if (!panel || panel.classList.contains('paused')) return;

    // On first render after a page load, seed previous timestamps to 0 so any
    // persisted ts from the server triggers a blink immediately.
    const firstRender = (_lastSourceTs === null);
    if (firstRender) _lastSourceTs = {};

    // Sources that are optional/disabled-by-default don't count toward "all dead"
    const optionalSources = new Set(['adsbx', 'local', 'flightaware']);
    let anyActive = false;
    let allRequiredDead = true;

    document.querySelectorAll('#apiSourceLights .api-light-row').forEach(row => {
        const key = row.dataset.source;
        const led = row.querySelector('.api-led');
        const cnt = row.querySelector('.api-led-count');
        if (!led) return;

        const info  = sourcesUsed[key] || {};
        const ts    = info.ts    || 0;
        const count = info.count || 0;
        const inBackoff = !!info.in_backoff;

        if (cnt) cnt.textContent = count;

        // Red LED if the source is in backoff (timing out or rate-limited)
        if (inBackoff) {
            led.classList.add('error');
            led.classList.remove('blink');
        } else {
            led.classList.remove('error');
            const prev = _lastSourceTs[key] || 0;
            if (ts && ts > prev) {
                led.classList.remove('blink');
                void led.offsetWidth;
                led.classList.add('blink');
                setTimeout(() => led.classList.remove('blink'), 2600);
            }
        }
        _lastSourceTs[key] = ts;

        if (!optionalSources.has(key)) {
            if (!inBackoff && ts > 0) anyActive = true;
            if (!inBackoff) allRequiredDead = false;
        }
    });

    // Show toast when every required ADS-B source is in backoff simultaneously
    const allDead = allRequiredDead && !anyActive;
    _showSourceDeadToast(allDead);
}

var _sourceDeadToastVisible = false;
function _showSourceDeadToast(dead) {
    if (dead === _sourceDeadToastVisible) return;
    _sourceDeadToastVisible = dead;
    let toast = document.getElementById('source-toast');
    if (dead) {
        if (!toast) {
            toast = document.createElement('div');
            toast.id = 'source-toast';
            toast.textContent = '⚠ All ADS-B sources unavailable — flight data may be stale';
            document.body.appendChild(toast);
        }
    } else {
        if (toast) toast.remove();
    }
}

function toggleApiPause() {
    _apiPaused = !_apiPaused;
    const btn = document.getElementById('apiPauseBtn');
    const panel = document.getElementById('dataSourcesPanel');

    if (_apiPaused) {
        clearInterval(autoGoInterval);
        autoGoInterval = null;
        if (panel) panel.classList.add('paused');
        if (btn) btn.textContent = 'Resume (lights only)';
        if (btn) btn.classList.add('btn-warning');
        if (btn) btn.classList.remove('btn-secondary');
    } else {
        autoGoInterval = setInterval(goFetch, currentCheckInterval * 1000);
        if (panel) panel.classList.remove('paused');
        if (btn) btn.textContent = 'Pause All (lights only)';
        if (btn) btn.classList.remove('btn-warning');
        if (btn) btn.classList.add('btn-secondary');
        goFetch(); // immediate fetch on resume
    }
}

// bfcache support: pause all intervals on pagehide so there are no in-flight
// requests blocking restoration. Restart on pageshow if restored from cache.
window.addEventListener('pagehide', () => {
    clearInterval(autoGoInterval);
    clearInterval(softRefreshInterval);
    clearInterval(refreshTimerLabelInterval);
    clearInterval(_telescopeStatusInterval);
    _telescopeStatusInterval = null;
    clearInterval(_bothOffReminderInterval);
    _bothOffReminderInterval = null;
});

window.addEventListener('pageshow', (e) => {
    if (e.persisted) {
        // Page was restored from bfcache — restart polling/timers,
        // but do NOT re-render or re-fetch (data is already in memory).
        startTelescopeStatusPolling();
        autoGoInterval = setInterval(goFetch, currentCheckInterval * 1000);
        softRefreshInterval = setInterval(softRefresh, 15000);
        refreshTimerLabelInterval = setInterval(refreshTimer, 1000);
        if (!_bothOffReminderInterval) {
            _bothOffReminderInterval = setInterval(_bothOffReminderTick, 60000);
        }
    }
});
