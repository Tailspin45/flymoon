/**
 * Flymoon Map Visualization
 *
 * Interactive Leaflet map displaying:
 * - Observer location and bounding box for flight searches
 * - Aircraft markers with color-coded transit probability
 * - Azimuth arrows showing celestial target directions (Sun/Moon)
 * - Flight routes and historical tracks
 * - Altitude overlay with clickable indicators
 *
 * @author Flymoon Team
 * @version 1.0
 */

let map = null;
let observerMarker = null;
let boundingBoxLayer = null;
let azimuthArrows = {};  // Store arrows by target name
let aircraftMarkers = {};
let aircraftLayer = null;   // LayerGroup for all aircraft icons — cleared atomically
let headingArrowLayer = null; // LayerGroup for heading arrows
let ghostLayer = null;      // LayerGroup for ghost dots (previous positions)
let mapInitialized = false;
let boundingBoxUserEdited = localStorage.getItem('boundingBoxUserEdited') === 'true';
let aircraftRouteCache = {};  // Cache fetched routes/tracks
let flightWaypointsMap = {};  // Waypoints from search data, keyed by normalised flight ID
let currentRouteLayer = null;  // Currently displayed route/track
let userInteractingWithMap = false;  // Prevent auto-zoom during user interaction
let headingArrows = {};  // Store heading arrows for medium/high probability transits
let ghostMarkers = {};  // Arrays of dots showing breadcrumb trail per flight (ghostMarkers[id] = [circleMarker, ...])
let hardRefreshCount = 0;  // Counts auto hard refreshes; ghosts cleared every 3rd
let layerControl = null;  // Leaflet layer control (base + overlays)

// ── Heatmap state ──────────────────────────────────────────────────────────────
let heatLayer = null;
let heatmapVisible = false;
const HEATMAP_STORAGE_KEY = 'flymoonHeatPoints';
const HEATMAP_MAX_POINTS  = 2000; // cap localStorage size
// Accumulated [lat, lon, intensity] points (loaded from localStorage on first use)
let _heatPoints = null;

function _loadHeatPoints() {
    if (_heatPoints !== null) return _heatPoints;
    try {
        const raw = localStorage.getItem(HEATMAP_STORAGE_KEY);
        _heatPoints = raw ? JSON.parse(raw) : [];
    } catch(e) {
        _heatPoints = [];
    }
    return _heatPoints;
}

function _saveHeatPoints() {
    try {
        localStorage.setItem(HEATMAP_STORAGE_KEY, JSON.stringify(_heatPoints));
    } catch(e) { /* storage full — ignore */ }
}

/** Add current flight positions to the accumulated heatmap dataset. */
function updateHeatmapData(flights) {
    const pts = _loadHeatPoints();
    flights.forEach(f => {
        if (f.latitude != null && f.longitude != null) {
            pts.push([f.latitude, f.longitude, 1]);
        }
    });
    // Keep only the most recent HEATMAP_MAX_POINTS entries
    if (pts.length > HEATMAP_MAX_POINTS) {
        pts.splice(0, pts.length - HEATMAP_MAX_POINTS);
    }
    _heatPoints = pts;
    _saveHeatPoints();
    if (heatLayer) heatLayer.setLatLngs(pts);
}

/** Toggle the heatmap layer on/off. */
function toggleHeatmap() {
    if (!map) return;
    heatmapVisible = !heatmapVisible;

    const btn = document.getElementById('heatmapToggle');
    if (heatmapVisible) {
        const pts = _loadHeatPoints();
        heatLayer = L.heatLayer(pts, {
            radius: 18,
            blur: 20,
            maxZoom: 12,
            gradient: { 0.2: 'blue', 0.5: 'lime', 0.8: 'orange', 1.0: 'red' }
        }).addTo(map);
        if (btn) { btn.style.opacity = '1'; btn.title = 'Hide traffic heatmap'; }
    } else {
        if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
        if (btn) { btn.style.opacity = '0.45'; btn.title = 'Show traffic density heatmap'; }
    }
}

/** Clear all accumulated heatmap data and remove the layer. */
function clearHeatmap() {
    _heatPoints = [];
    localStorage.removeItem(HEATMAP_STORAGE_KEY);
    if (heatLayer) { map.removeLayer(heatLayer); heatLayer = null; }
    heatmapVisible = false;
    const btn = document.getElementById('heatmapToggle');
    if (btn) { btn.style.opacity = '0.45'; btn.title = 'Show traffic density heatmap'; }
}

// Arrow colors for each target
const ARROW_COLORS = {
    sun: '#FF4500',   // Orange-red
    moon: '#4169E1'   // Royal blue
};

// Color scheme for possibility levels
const COLORS = {
    LOW: '#FFD700',      // Yellow/Gold
    MEDIUM: '#FF8C00',   // Dark Orange
    HIGH: '#32CD32',     // Lime Green
    DEFAULT: '#808080'   // Gray
};

// Track currently selected row
let selectedRowId = null;

// Flash a table row by flight ID and keep it highlighted
function flashTableRow(flightId) {
    const row = document.querySelector(`tr[data-flight-id="${flightId}"]`);
    if (row) {
        // Toggle off if clicking the already-selected row
        if (selectedRowId === flightId) {
            row.classList.remove('selected-row');
            selectedRowId = null;
            return;
        }

        // Remove highlight from previously selected row
        if (selectedRowId) {
            const prevRow = document.querySelector(`tr[data-flight-id="${selectedRowId}"]`);
            if (prevRow) {
                prevRow.classList.remove('selected-row');
            }
        }

        // Flash animation
        row.classList.remove('flash-row');
        void row.offsetWidth; // Trigger reflow
        row.classList.add('flash-row');

        // Add persistent highlight
        row.classList.add('selected-row');
        selectedRowId = flightId;

        // Scroll within table container, not the entire page
        row.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });

        // Also flash the altitude bar
        flashAltitudeBar(flightId);
    }
}

// Flash an aircraft marker by flight ID
function flashAircraftMarker(flightId) {
    const marker = aircraftMarkers[flightId];
    if (marker) {
        const element = marker.getElement();
        if (element) {
            // Apply animation to the inner div, not the positioned container
            const innerDiv = element.querySelector('div');
            if (innerDiv) {
                innerDiv.classList.remove('flash-marker');
                void innerDiv.offsetWidth; // Trigger reflow
                innerDiv.classList.add('flash-marker');
            }
        }
        // Pan to marker
        map.panTo(marker.getLatLng());
    }
}

// Flash an altitude bar by flight ID
function flashAltitudeBar(flightId) {
    console.log('flashAltitudeBar called with:', flightId);

    // Check if container exists
    const container = document.getElementById('altitudeBars');
    console.log('altitudeBars container:', container);
    console.log('Container children:', container ? container.children.length : 'N/A');

    const bars = document.querySelectorAll('.altitude-bar');
    console.log('Found', bars.length, 'altitude bars via querySelectorAll');

    // Also try querying within container
    if (container) {
        const barsInContainer = container.querySelectorAll('.altitude-bar');
        console.log('Found', barsInContainer.length, 'bars within container');

        // Check what children actually exist
        console.log('Container innerHTML length:', container.innerHTML.length);
        if (container.children.length > 0) {
            console.log('First child class:', container.children[0].className);
        }
    }

    let found = false;
    bars.forEach(bar => {
        const idLabel = bar.querySelector('.altitude-bar-id');
        if (idLabel) {
            const barId = idLabel.textContent.trim().toUpperCase();
            console.log('Checking bar:', barId, 'against', flightId.toUpperCase());
            if (barId === flightId.toUpperCase()) {
                console.log('Match found! Flashing bar for', flightId);
                bar.classList.remove('flash-altitude-bar');
                void bar.offsetWidth; // Trigger reflow
                bar.classList.add('flash-altitude-bar');
                found = true;
            }
        }
    });

    if (!found) {
        console.log('No matching altitude bar found for', flightId);
    }
}

function initializeMap(centerLat, centerLon) {
    if (mapInitialized) {
        return;
    }

    map = L.map('map', {
        editable: true
    }).setView([centerLat, centerLon], 9);

    // Tile layers — no API keys required
    const tileLayers = {
        'Street': L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
            maxZoom: 19
        }),
        'Satellite': L.tileLayer('https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}', {
            attribution: 'Tiles &copy; Esri &mdash; Source: Esri, Maxar, Earthstar Geographics',
            maxZoom: 19
        }),
        'Dark': L.tileLayer('https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png', {
            attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors &copy; <a href="https://carto.com/attributions">CARTO</a>',
            subdomains: 'abcd',
            maxZoom: 19
        }),
    };
    const savedLayer = localStorage.getItem('mapLayer') || 'Street';
    (tileLayers[savedLayer] || tileLayers['Street']).addTo(map);

    // SkyVector aviation charts deep-link control (added first = sits above layer control)
    const SkyVectorControl = L.Control.extend({
        options: { position: 'topright' },
        onAdd() {
            const wrap = L.DomUtil.create('div', 'sv-control leaflet-control');
            wrap.innerHTML = `
                <button class="sv-toggle" title="Open current view in SkyVector">
                    ✈ SkyVector <span class="sv-caret">▼</span>
                </button>
                <div class="sv-menu">
                    <div class="sv-menu-header">Open in SkyVector</div>
                    <a href="#" data-chart="301">
                        <span class="sv-icon">🗺</span>
                        <span class="sv-label">World Lo</span>
                        <span class="sv-sub">IFR Low Altitude</span>
                    </a>
                    <a href="#" data-chart="302">
                        <span class="sv-icon">🗺</span>
                        <span class="sv-label">World Hi</span>
                        <span class="sv-sub">IFR High Altitude</span>
                    </a>
                    <a href="#" data-chart="">
                        <span class="sv-icon">🗺</span>
                        <span class="sv-label">World VFR</span>
                        <span class="sv-sub">Visual flight</span>
                    </a>
                </div>`;
            L.DomEvent.disableClickPropagation(wrap);
            const menu = wrap.querySelector('.sv-menu');
            wrap.querySelector('.sv-toggle').addEventListener('click', e => {
                e.stopPropagation();
                menu.style.display = menu.style.display === 'block' ? 'none' : 'block';
            });
            wrap.querySelectorAll('a[data-chart]').forEach(a => {
                a.addEventListener('click', e => {
                    e.preventDefault();
                    const c = map.getCenter();
                    const z = Math.min(Math.max(Math.round(map.getZoom() / 2), 1), 5);
                    const chartParam = a.dataset.chart ? `&chart=${a.dataset.chart}` : '';
                    window.open(`https://skyvector.com/?ll=${c.lat.toFixed(4)},${c.lng.toFixed(4)}${chartParam}&zoom=${z}`, '_blank');
                    menu.style.display = 'none';
                });
            });
            document.addEventListener('click', () => { menu.style.display = 'none'; });
            return wrap;
        }
    });
    new SkyVectorControl().addTo(map);
    layerControl = L.control.layers(tileLayers, {}, { position: 'topright' }).addTo(map);
    map.on('baselayerchange', e => localStorage.setItem('mapLayer', e.name));

    // LayerGroups for atomic clear/add cycles
    aircraftLayer = L.layerGroup().addTo(map);
    headingArrowLayer = L.layerGroup().addTo(map);

    // Ghost/breadcrumb layer: use a custom pane with pointer-events:none so it
    // never intercepts clicks on aircraft markers beneath it
    map.createPane('ghostPane');
    map.getPane('ghostPane').style.pointerEvents = 'none';
    ghostLayer = L.layerGroup({ pane: 'ghostPane' }).addTo(map);

    mapInitialized = true;
    // Add OpenAIP overlay now that layerControl exists — key was injected server-side
    console.log('[OpenAIP] initializeMap complete, calling addOpenAIPOverlay…');
    addOpenAIPOverlay(window.OPENAIP_API_KEY || '');
}

/**
 * Add OpenAIP aviation overlay (airspace, airports, navaids) to the layer control.
 * Called after config loads — only runs if an API key is configured.
 * Get a free key at https://www.openaip.net
 */
function showOpenAIPInstructions(e) {
    e.preventDefault();
    e.stopPropagation();
    const existing = document.getElementById('openaip-modal');
    if (existing) { existing.remove(); return; }
    const modal = document.createElement('div');
    modal.id = 'openaip-modal';
    modal.innerHTML = `
        <div id="openaip-modal-box">
            <h3>✈ Get a Free OpenAIP Key</h3>
            <ol>
                <li>Go to <a href="https://www.openaip.net" target="_blank">openaip.net</a> and create a free account</li>
                <li>Confirm your email and log in</li>
                <li>Click your <strong>username/avatar</strong> (top-right) → <strong>Profile</strong></li>
                <li>Scroll to the <strong>"API Clients"</strong> section <em>(not "API Keys")</em></li>
                <li>Click <strong>"Add client"</strong>, give it a name (e.g. <code>flymoon</code>), submit</li>
                <li>Copy the <strong>API key</strong> shown on the new client</li>
                <li>Add it to your <code>.env</code> file:<br><code>OPENAIP_API_KEY=your_key_here</code></li>
            </ol>
            <p>Reload the page after saving — no server restart needed.</p>
            <button onclick="document.getElementById('openaip-modal').remove()">Close</button>
        </div>`;
    modal.addEventListener('click', ev => { if (ev.target === modal) modal.remove(); });
    document.body.appendChild(modal);
}

let openAIPLayer = null;  // Keep reference for layer control toggle

function addOpenAIPOverlay(apiKey) {
    console.log('[OpenAIP] addOpenAIPOverlay called, key=' + (apiKey ? apiKey.slice(0,8)+'…' : 'EMPTY') +
                ', map=' + !!map + ', layerControl=' + !!layerControl);
    if (!layerControl || !map) {
        console.error('[OpenAIP] ABORT — map or layerControl not ready');
        return;
    }
    if (!apiKey) {
        layerControl.addOverlay(
            L.tileLayer('', { opacity: 0 }),
            'Aviation (OpenAIP) — <a href="#" onclick="showOpenAIPInstructions(event)" style="color:#7ab8d4;">get free key</a>'
        );
        return;
    }

    const url = 'https://api.tiles.openaip.net/api/data/openaip/{z}/{x}/{y}.png?apiKey=' + apiKey;
    console.log('[OpenAIP] Creating tile layer with URL:', url.replace(apiKey, apiKey.slice(0,8)+'…'));

    openAIPLayer = L.tileLayer(url, {
        attribution: '&copy; <a href="https://www.openaip.net">OpenAIP</a>',
        maxZoom: 17,
        opacity: 0.7,
        zIndex: 650,
    });

    // Debug: log tile load success/failure
    let _tileLoaded = 0, _tileError = 0;
    openAIPLayer.on('tileload', () => {
        _tileLoaded++;
        if (_tileLoaded <= 3) console.log('[OpenAIP] tile loaded (' + _tileLoaded + ' total)');
    });
    openAIPLayer.on('tileerror', (evt) => {
        _tileError++;
        console.error('[OpenAIP] tile ERROR (' + _tileError + ' total)', evt.coords, evt.tile?.src?.slice(0,80));
    });
    openAIPLayer.on('loading', () => console.log('[OpenAIP] tiles loading…'));
    openAIPLayer.on('load', () => console.log('[OpenAIP] ALL tiles loaded (' + _tileLoaded + ' ok, ' + _tileError + ' err)'));

    localStorage.removeItem('mapOverlayAvia');
    openAIPLayer.addTo(map);
    console.log('[OpenAIP] addTo(map) done, hasLayer=' + map.hasLayer(openAIPLayer));

    layerControl.addOverlay(openAIPLayer, 'Aviation (OpenAIP)');
    console.log('[OpenAIP] registered in layerControl');

    map.on('overlayadd',    e => { if (e.name === 'Aviation (OpenAIP)') { console.log('[OpenAIP] overlay ADD event'); localStorage.removeItem('mapOverlayAvia'); }});
    map.on('overlayremove', e => { if (e.name === 'Aviation (OpenAIP)') { console.log('[OpenAIP] overlay REMOVE event'); localStorage.setItem('mapOverlayAvia', 'off'); }});

    // Periodic health check (runs 3 times, then stops)
    let _checks = 0;
    const _healthCheck = setInterval(() => {
        _checks++;
        const onMap = map.hasLayer(openAIPLayer);
        const container = openAIPLayer.getContainer ? openAIPLayer.getContainer() : null;
        const visible = container ? getComputedStyle(container).display !== 'none' && getComputedStyle(container).visibility !== 'hidden' : 'N/A';
        const opacity = container ? getComputedStyle(container).opacity : 'N/A';
        const zIdx = container ? getComputedStyle(container).zIndex : 'N/A';
        const imgs = container ? container.querySelectorAll('img').length : 0;
        console.log('[OpenAIP] health #' + _checks + ': onMap=' + onMap + ', visible=' + visible +
                    ', opacity=' + opacity + ', zIndex=' + zIdx + ', imgs=' + imgs);
        if (_checks >= 5) clearInterval(_healthCheck);
    }, 3000);
}

function updateObserverMarker(lat, lon, elevation) {
    if (!map) return;

    // Remove existing marker
    if (observerMarker) {
        map.removeLayer(observerMarker);
    }

    // Create custom icon for observer - simple red dot
    const observerIcon = L.divIcon({
        html: '<div style="width: 8px; height: 8px; background-color: #FF0000; border: 1px solid white; border-radius: 50%; box-shadow: 0 0 3px rgba(0,0,0,0.6);"></div>',
        iconSize: [10, 10],
        iconAnchor: [5, 5],  // Center of the dot
        className: 'observer-icon'
    });

    observerMarker = L.marker([lat, lon], { icon: observerIcon })
        .addTo(map)
        .bindPopup(`<b>Observer</b><br>Lat: ${lat.toFixed(4)}°<br>Lon: ${lon.toFixed(4)}°<br>Elev: ${elevation}m`);

    // Center map on observer
    map.setView([lat, lon], map.getZoom());
}

function updateBoundingBox(latLowerLeft, lonLowerLeft, latUpperRight, lonUpperRight, fitToBox = true) {
    if (!map) return;

    // Use saved custom bounding box if it exists AND contains the observer
    const savedBox = localStorage.getItem('customBoundingBox');
    if (savedBox) {
        try {
            const customBox = JSON.parse(savedBox);
            const obsLat = parseFloat(document.getElementById('latitude')?.value);
            const obsLon = parseFloat(document.getElementById('longitude')?.value);
            const insideBox = !isNaN(obsLat) && !isNaN(obsLon)
                && obsLat >= customBox.latLowerLeft  && obsLat <= customBox.latUpperRight
                && obsLon >= customBox.lonLowerLeft  && obsLon <= customBox.lonUpperRight;
            if (insideBox) {
                latLowerLeft  = customBox.latLowerLeft;
                lonLowerLeft  = customBox.lonLowerLeft;
                latUpperRight = customBox.latUpperRight;
                lonUpperRight = customBox.lonUpperRight;
                window.lastBoundingBox = customBox;
            } else {
                // Stale custom bbox — ignore it and use whatever was passed in (server default or new save)
                console.warn('customBoundingBox does not contain observer — ignoring stale custom bbox');
                localStorage.removeItem('customBoundingBox');
                localStorage.removeItem('boundingBoxUserEdited');
            }
        } catch (e) {
            console.error('Error parsing saved bounding box:', e);
        }
    }

    // Remove existing bounding box
    if (boundingBoxLayer) {
        map.removeLayer(boundingBoxLayer);
    }

    // Create rectangle for bounding box
    const bounds = L.latLngBounds(
        [latLowerLeft, lonLowerLeft],
        [latUpperRight, lonUpperRight]
    );

    boundingBoxLayer = L.rectangle(bounds, {
        color: '#FF0000',
        weight: 2,
        fillOpacity: 0.1,
        dashArray: '5, 10'
    }).addTo(map).bindPopup('<b>Search Bounding Box</b><br>Drag corners to resize');

    // Fit map to bounding box only if the observer is inside it (avoids flying to a
    // distant bbox when observer and search area are in different regions)
    if (fitToBox) {
        const obsLat = parseFloat(document.getElementById('latitude')?.value) || 0;
        const obsLon = parseFloat(document.getElementById('longitude')?.value) || 0;
        const observerInBox = (obsLat && obsLon) ? bounds.contains([obsLat, obsLon]) : false;
        if (observerInBox) {
            map.fitBounds(bounds, { padding: [20, 20] });
        }
        // If observer is outside the box just draw the rectangle without changing the view
    }

    // Enable editing (draggable corners)
    if (boundingBoxLayer.enableEdit) {
        boundingBoxLayer.enableEdit();

        // Track when user edits the bounding box
        boundingBoxLayer.on('editable:vertex:dragend', function() {
            boundingBoxUserEdited = true;
            localStorage.setItem('boundingBoxUserEdited', 'true');

            // Save the new bounding box coordinates
            const newBounds = boundingBoxLayer.getBounds();
            const newBoundingBox = {
                latLowerLeft: newBounds.getSouth(),
                lonLowerLeft: newBounds.getWest(),
                latUpperRight: newBounds.getNorth(),
                lonUpperRight: newBounds.getEast()
            };
            window.lastBoundingBox = newBoundingBox;
            localStorage.setItem('customBoundingBox', JSON.stringify(newBoundingBox));
            localStorage.setItem('boundingBox', JSON.stringify(newBoundingBox));
            console.log("Bounding box resized - triggering API refresh");

            // Fit map to new bounding box
            map.fitBounds(newBounds, { padding: [20, 20] });

            // Trigger API refresh with new bounding box
            if (typeof fetchFlights === 'function') {
                fetchFlights();
            }
        });
    }
}

function clearAzimuthArrows() {
    // Remove all existing arrows
    Object.values(azimuthArrows).forEach(arrow => {
        if (arrow) map.removeLayer(arrow);
    });
    azimuthArrows = {};
}

function clearAzimuthArrow(targetName) {
    if (azimuthArrows[targetName]) {
        map.removeLayer(azimuthArrows[targetName]);
        delete azimuthArrows[targetName];
    }
}

function updateAzimuthArrow(observerLat, observerLon, azimuth, altitude, targetName) {
    if (!map) return;

    // Remove arrow if target is at or below the horizon
    if (altitude <= 0) {
        clearAzimuthArrow(targetName);
        return;
    }

    // Remove existing arrow for this target
    if (azimuthArrows[targetName]) {
        map.removeLayer(azimuthArrows[targetName]);
    }

    // Calculate endpoint for arrow (15km in azimuth direction)
    const distance = 15; // km
    const endPoint = calculateDestination(observerLat, observerLon, azimuth, distance);

    // Create arrow polyline
    const arrowPoints = [
        [observerLat, observerLon],
        [endPoint.lat, endPoint.lon]
    ];

    const targetCapitalized = targetName.charAt(0).toUpperCase() + targetName.slice(1);
    const color = ARROW_COLORS[targetName] || '#FF4500';

    azimuthArrows[targetName] = L.polyline(arrowPoints, {
        color: color,
        weight: 6,
        opacity: 0.9,
        lineCap: 'round',  // Round caps center the line better on the observer
        lineJoin: 'round'
    }).addTo(map).bindPopup(`<b>${targetCapitalized}</b><br>Altitude: ${altitude.toFixed(1)}°<br>Azimuth: ${azimuth.toFixed(1)}°`);
}

/**
 * Add a heading arrow to show aircraft's true heading direction
 * Only for medium/high probability transits
 */
function addHeadingArrow(flight, flightId, color) {
    if (!map || !flight.latitude || !flight.longitude || !flight.direction) return;
    
    const lat = flight.latitude;
    const lon = flight.longitude;
    // FlightAware heading is true heading (GPS-derived) - use directly on true-north map
    const heading = flight.direction;
    
    // Calculate arrow endpoint (12km in heading direction)
    const distance = 12; // km
    const endPoint = calculateDestination(lat, lon, heading, distance);
    
    // Create arrow line matching sun/moon azimuth arrow style (no arrowhead)
    const arrowLine = L.polyline(
        [[lat, lon], [endPoint.lat, endPoint.lon]],
        {
            color: color,
            weight: 6,
            opacity: 0.9,
            lineCap: 'round',
            lineJoin: 'round',
            className: 'heading-arrow'
        }
    ).addTo(headingArrowLayer);
    
    // Store for cleanup
    headingArrows[flightId] = arrowLine;
}

function updateSingleAircraftMarker(flight) {
    if (!map || !aircraftLayer) return;

    const normalizedId = String(flight.id).trim().toUpperCase();

    // Remove existing marker and heading arrow for this flight
    if (aircraftMarkers[normalizedId]) {
        aircraftLayer.removeLayer(aircraftMarkers[normalizedId]);
        delete aircraftMarkers[normalizedId];
    }
    if (headingArrows[normalizedId]) {
        headingArrowLayer.removeLayer(headingArrows[normalizedId]);
        delete headingArrows[normalizedId];
    }

    // Determine color based on possibility level
    let color = COLORS.DEFAULT;
    if (flight.is_possible_transit === 1) {
        const level = parseInt(flight.possibility_level);
        if (level === 1) color = COLORS.LOW;
        else if (level === 2) color = COLORS.MEDIUM;
        else if (level === 3) color = COLORS.HIGH;
    }

    // Use diamond for transit aircraft, SVG airplane for others
    const isTransit = flight.is_possible_transit === 1;
    const rotation = flight.direction; // SVG points north (up), rotate by true heading

    const aircraftIcon = L.divIcon({
        html: isTransit
            ? `<div style="font-size: 36px; color: ${color}; text-shadow: 0 0 3px black, 0 0 3px black, 0 0 8px ${color}, 1px 1px 0 black, -1px -1px 0 black, 1px -1px 0 black, -1px 1px 0 black; display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; line-height: 1;">◆</div>`
            : `<div style="transform: rotate(${rotation}deg); width: 24px; height: 24px;"><svg xmlns="http://www.w3.org/2000/svg" viewBox="10 13 30 24" width="24" height="24" fill="#4a90d9"><rect x="23.5" y="14" width="3" height="22" rx="1.5"/><polygon points="25,14 23.5,18 26.5,18"/><polygon points="10,27 23.5,23 23.5,27 12,29"/><polygon points="40,27 26.5,23 26.5,27 38,29"/><polygon points="20,35 23.5,33 23.5,35"/><polygon points="30,35 26.5,33 26.5,35"/></svg></div>`,
        iconSize: [24, 24],
        iconAnchor: [12, 12],
        className: 'aircraft-icon'
    });

    // Add marker if we have coordinates
    if (flight.latitude !== undefined && flight.latitude !== null &&
        flight.longitude !== undefined && flight.longitude !== null) {
        const marker = L.marker([flight.latitude, flight.longitude], { icon: aircraftIcon })
            .addTo(aircraftLayer);

        marker.getElement()?.style.setProperty('filter', `drop-shadow(0 0 8px ${color}) drop-shadow(0 0 4px rgba(0,0,0,0.8))`);
        marker.flightId = normalizedId;

        marker.on('click', function() {
            toggleFlightRouteTrack(flight.fa_flight_id, normalizedId);
            flashTableRow(normalizedId);
        });

        aircraftMarkers[normalizedId] = marker;
        
        // Add heading arrow for medium/high probability transits
        if (flight.is_possible_transit === 1) {
            const level = parseInt(flight.possibility_level);
            if (level === 2 || level === 3) {  // MEDIUM or HIGH
                addHeadingArrow(flight, normalizedId, color);
            }
        }
    }
}

function updateAircraftMarkers(flights, observerLat, observerLon, isFullRefresh = false, isForceRefresh = false) {
    if (!map || !aircraftLayer) return;

    if (isFullRefresh) {
        if (isForceRefresh) {
            // Force refresh by user: clear all ghost dots and reset counter
            ghostLayer.clearLayers();
            ghostMarkers = {};
            hardRefreshCount = 0;
            // Clear track on explicit user force refresh
            if (currentRouteLayer) {
                map.removeLayer(currentRouteLayer);
                currentRouteLayer = null;
                userInteractingWithMap = false;
            }
        } else {
            // Auto hard refresh: clear ghost dots every 3rd refresh
            hardRefreshCount++;
            if (hardRefreshCount % 3 === 0) {
                ghostLayer.clearLayers();
                ghostMarkers = {};
            }
            // Keep track if flight still in data
            if (currentRouteLayer) {
                const activeId = currentRouteLayer.flightId;
                const stillPresent = flights.some(f => String(f.id).trim().toUpperCase() === activeId);
                if (!stillPresent) {
                    map.removeLayer(currentRouteLayer);
                    currentRouteLayer = null;
                    userInteractingWithMap = false;
                }
            }
        }
    } else {
        // Soft refresh: add a new breadcrumb dot at current position for each aircraft
        Object.entries(aircraftMarkers).forEach(([id, marker]) => {
            const latlng = marker.getLatLng();
            const dot = L.circleMarker(latlng, {
                radius: 2,
                color: '#888',
                fillColor: '#888',
                fillOpacity: 1,
                weight: 0,
                interactive: false,
                pane: 'ghostPane'
            }).addTo(ghostLayer);
            if (!ghostMarkers[id]) ghostMarkers[id] = [];
            ghostMarkers[id].push(dot);
        });

        // Remove ghost dots for aircraft no longer in the flight data
        const activeIds = new Set(flights.map(f => String(f.id).trim().toUpperCase()));
        Object.keys(ghostMarkers).forEach(id => {
            if (!activeIds.has(id)) {
                ghostMarkers[id].forEach(dot => ghostLayer.removeLayer(dot));
                delete ghostMarkers[id];
            }
        });
    }

    // Atomically clear all aircraft markers and heading arrows
    aircraftLayer.clearLayers();
    headingArrowLayer.clearLayers();
    aircraftMarkers = {};
    headingArrows = {};

    // Add new aircraft markers
    flights.forEach(flight => {
        const flightId = flight.id;
        
        // Determine color based on possibility level
        let color = COLORS.DEFAULT;
        if (flight.is_possible_transit === 1) {
            const level = parseInt(flight.possibility_level);
            if (level === 1) color = COLORS.LOW;
            else if (level === 2) color = COLORS.MEDIUM;
            else if (level === 3) color = COLORS.HIGH;
        }

                // SVG airplane points north (up), rotate by true heading — works on all platforms
        const isTransit = flight.is_possible_transit === 1;
        const rotation = flight.direction;

        const aircraftIcon = L.divIcon({
            html: isTransit
                ? `<div style="font-size: 36px; color: ${color}; text-shadow: 0 0 3px black, 0 0 3px black, 0 0 8px ${color}, 1px 1px 0 black, -1px -1px 0 black, 1px -1px 0 black, -1px 1px 0 black; display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; line-height: 1;">◆</div>`
                : `<div style="transform: rotate(${rotation}deg); width: 24px; height: 24px;"><svg xmlns="http://www.w3.org/2000/svg" viewBox="10 13 30 24" width="24" height="24" fill="#4a90d9"><rect x="23.5" y="14" width="3" height="22" rx="1.5"/><polygon points="25,14 23.5,18 26.5,18"/><polygon points="10,27 23.5,23 23.5,27 12,29"/><polygon points="40,27 26.5,23 26.5,27 38,29"/><polygon points="20,35 23.5,33 23.5,35"/><polygon points="30,35 26.5,33 26.5,35"/></svg></div>`,
            iconSize: [24, 24],
            iconAnchor: [12, 12],  // Center the icon on coordinates
            className: 'aircraft-icon'
        });

        // Add marker if we have coordinates (check for undefined/null, not falsy)
        if (flight.latitude !== undefined && flight.latitude !== null &&
            flight.longitude !== undefined && flight.longitude !== null) {
            const marker = L.marker([flight.latitude, flight.longitude], { icon: aircraftIcon })
                .addTo(aircraftLayer);

            // Add strong shadow for visibility
            marker.getElement()?.style.setProperty('filter', `drop-shadow(0 0 8px ${color}) drop-shadow(0 0 4px rgba(0,0,0,0.8))`);

            // Store normalized ID for cross-referencing
            const normalizedId = String(flightId).trim().toUpperCase();
            marker.flightId = normalizedId;

            // Click handler to show route/track and flash table row
            marker.on('click', function() {
                console.log('Marker clicked!', { fa_flight_id: flight.fa_flight_id, normalizedId });
                toggleFlightRouteTrack(flight.fa_flight_id, normalizedId);
                flashTableRow(normalizedId);
                flashAircraftMarker(normalizedId);
            });

            aircraftMarkers[normalizedId] = marker;
            // Store waypoints from search data for route display
            if (flight.waypoints && flight.waypoints.length >= 2) {
                flightWaypointsMap[normalizedId] = flight.waypoints;
            }

            // Add heading arrow for medium/high probability transits
            if (flight.is_possible_transit === 1) {
                const level = parseInt(flight.possibility_level);
                if (level === 2 || level === 3) {  // MEDIUM or HIGH
                    addHeadingArrow(flight, normalizedId, color);
                }
            }
        }
    });

    // Fit map to show aircraft — only on full refresh, not soft refresh.
    // Only auto-fit if aircraft are near the observer (i.e., observer is inside or close to the
    // aircraft bounds). Avoids zooming out to show a distant search area on misconfigured setups.
    if (!isFullRefresh) return;

    if (Object.keys(aircraftMarkers).length > 0 && !userInteractingWithMap) {
        const aircraftBounds = L.latLngBounds(
            Object.values(aircraftMarkers).map(marker => marker.getLatLng())
        );

        // Only auto-fit if observer is already inside (or very close to) the aircraft bounds.
        // If observer is far away, the bbox is probably misconfigured — stay centered on observer.
        const padded = aircraftBounds.pad(0.5); // 50% padding to give "close to" leniency
        if (padded.contains([observerLat, observerLon])) {
            aircraftBounds.extend([observerLat, observerLon]);
            map.fitBounds(aircraftBounds, { padding: [10, 10] });
        } else {
            // Aircraft are in a different region — just keep observer view
            map.setView([observerLat, observerLon], map.getZoom() || 9);
        }
    } else if (Object.keys(aircraftMarkers).length === 0) {
        // No aircraft - center on observer at reasonable zoom
        map.setView([observerLat, observerLon], 9);
    }
}

/**
 * Update the altitude overlay with clickable bars for each flight
 *
 * Creates thin horizontal bars positioned by altitude, color-coded by
 * transit probability. Bars are clickable to show route/track on map
 * and highlight the corresponding table row and aircraft marker.
 *
 * @param {Array} flights - Array of flight objects with altitude and transit data
 */
function updateAltitudeOverlay(flights) {
    const container = document.getElementById('altitudeBars');
    if (!container) return;

    container.innerHTML = '';

    console.log('updateAltitudeOverlay: received', flights.length, 'flights');

    // Sort by aircraft elevation descending
    const sortedFlights = [...flights].sort((a, b) =>
        (b.aircraft_elevation || 0) - (a.aircraft_elevation || 0)
    );

    const MAX_ALT = 45000; // feet
    let barsCreated = 0;

    sortedFlights.forEach(flight => {
        // Get GPS altitude - can be negative for below sea level
        const altMeters = flight.aircraft_elevation;

        // Skip only if altitude data is missing (null/undefined), not if it's 0 or negative
        if (altMeters === null || altMeters === undefined) {
            console.log('Skipping flight', flight.id, '- no altitude data');
            return;
        }

        const altFeet = Math.round(altMeters * 3.28084); // meters to feet
        console.log('Creating bar for flight', flight.id, '- altitude:', altFeet, 'ft');

        // For bar width, use absolute value to ensure positive bar, but cap at MAX_ALT
        const barWidthPercent = (Math.abs(altFeet) / MAX_ALT) * 100;

        // Determine color
        let color = '#808080'; // Gray default
        if (flight.is_possible_transit === 1) {
            const level = parseInt(flight.possibility_level);
            if (level === 3) color = '#32CD32'; // GREEN
            else if (level === 2) color = '#FF8C00'; // ORANGE
            else if (level === 1) color = '#FFD700'; // YELLOW
        }

        // Use red color for negative altitudes (below sea level)
        if (altFeet < 0) {
            color = '#FF4444'; // Red for below sea level
        }

        const bar = document.createElement('div');
        bar.className = 'altitude-bar';
        bar.style.background = color;

        // Position bar vertically based on altitude (0 at bottom, MAX_ALT at top)
        const positionPercent = (altFeet / MAX_ALT) * 100;
        bar.style.bottom = `${Math.max(0, Math.min(100, positionPercent))}%`;

        // Labels removed - just show colored bars

        // Click to flash on map and table, and show route/track
        const clickHandler = () => {
            const normalizedId = String(flight.id).trim().toUpperCase();

            // Show route/track
            if (typeof toggleFlightRouteTrack === 'function' && flight.fa_flight_id) {
                toggleFlightRouteTrack(flight.fa_flight_id, normalizedId);
            }

            // Flash marker and highlight row
            if (typeof flashAircraftMarker === 'function') {
                flashAircraftMarker(normalizedId);
            }
            if (typeof flashTableRow === 'function') {
                flashTableRow(normalizedId);
            }
        };

        bar.addEventListener('click', clickHandler);

        container.appendChild(bar);
        barsCreated++;
    });

    console.log('updateAltitudeOverlay: created', barsCreated, 'bars');
}

/**
 * Toggle display of flight route and historical track on the map
 *
 * Fetches and displays:
 * - Planned route (blue dashed line with waypoints)
 * - Historical track (green solid line with actual positions)
 *
 * Clicking again hides the route/track. Uses caching to avoid redundant API calls.
 *
 * @param {string} faFlightId - FlightAware flight ID for API queries
 * @param {string} flightId - Normalized flight identifier for UI cross-reference
 */
async function toggleFlightRouteTrack(faFlightId, flightId) {
    console.log('toggleFlightRouteTrack called:', { faFlightId, flightId });
    
    if (!map) return;

    // If already showing this flight's route, hide it and restore breadcrumbs
    if (currentRouteLayer && currentRouteLayer.flightId === flightId) {
        console.log('Hiding current route layer for', flightId);
        map.removeLayer(currentRouteLayer);
        currentRouteLayer = null;
        userInteractingWithMap = false;  // Allow auto-zoom again
        // Restore breadcrumb dots for this flight
        if (ghostMarkers[flightId]) {
            ghostMarkers[flightId].forEach(dot => ghostLayer.addLayer(dot));
        }
        return;
    }

    // Remove any previous route from a different flight and restore its breadcrumbs
    if (currentRouteLayer) {
        const prevId = currentRouteLayer.flightId;
        map.removeLayer(currentRouteLayer);
        currentRouteLayer = null;
        if (prevId && ghostMarkers[prevId]) {
            ghostMarkers[prevId].forEach(dot => ghostLayer.addLayer(dot));
        }
    }

    // Need fa_flight_id to fetch route/track data
    if (!faFlightId) {
        console.log('No faFlightId — cannot show track for', flightId);
        return;
    }

    // User is now interacting with the map - prevent auto-zoom
    userInteractingWithMap = true;
    console.log('User interacting with map, fetching route/track...');

    // Hide breadcrumb dots for this flight while track is shown
    if (ghostMarkers[flightId]) {
        ghostMarkers[flightId].forEach(dot => ghostLayer.removeLayer(dot));
    }

    // Check cache first
    if (aircraftRouteCache[flightId]) {
        displayRouteTrack(aircraftRouteCache[flightId], flightId);
        return;
    }

    // Fetch only the historical track; route comes from stored waypoints
    try {
        console.log(`Fetching track for ${faFlightId}`);
        const trackResponse = await fetch(`/flights/${faFlightId}/track`)
            .then(r => r.json())
            .catch(e => ({ error: e.message }));

        const cached = {
            waypoints: flightWaypointsMap[flightId] || [],
            track: trackResponse
        };
        aircraftRouteCache[flightId] = cached;
        displayRouteTrack(cached, flightId);
    } catch (error) {
        console.error('Error fetching track:', error);
        alert('Could not fetch track data. This may be because the aircraft is not currently transmitting data or API rate limits have been reached.');
    }
}

function displayRouteTrack(data, flightId) {
    if (!map) return;

    const layerGroup = L.layerGroup();

    console.log('Route/Track data for', flightId, ':', data);

    // Display route (blue dashed) from flat [lat,lon,lat,lon,...] waypoints array
    if (data.waypoints && data.waypoints.length >= 2) {
        const flat = data.waypoints;
        const routePoints = [];
        for (let i = 0; i + 1 < flat.length; i += 2) {
            routePoints.push([flat[i], flat[i + 1]]);
        }
        if (routePoints.length > 0) {
            console.log('Drawing route with', routePoints.length, 'waypoints');
            const routeLine = L.polyline(routePoints, {
                color: '#4169E1',
                weight: 3,
                dashArray: '10, 10',
                opacity: 0.7
            });
            layerGroup.addLayer(routeLine);
            routeLine.bindPopup('📍 Planned Route (' + routePoints.length + ' points)');
        }
    }

    // Display track (green solid with dots)
    if (data.track && !data.track.error) {
        console.log('Track data:', data.track);

        const positions = data.track.positions || [];

        if (positions.length > 0) {
            const trackPoints = positions
                .filter(pt => pt.latitude != null && pt.longitude != null)
                .map(pt => [pt.latitude, pt.longitude]);

            if (trackPoints.length > 0) {
                console.log('Drawing track with', trackPoints.length, 'positions');
                const trackLine = L.polyline(trackPoints, {
                    color: '#000000',  // Black
                    weight: 3,
                    opacity: 0.8,
                    dashArray: '10, 5'  // Dashed pattern
                });
                layerGroup.addLayer(trackLine);
                trackLine.bindPopup('✈️ Historical Track (' + trackPoints.length + ' positions)');
            } else {
                console.log('Track has positions but no valid lat/lon coordinates');
            }
        } else {
            console.log('No positions in track data');
        }
    } else if (data.track && data.track.error) {
        console.log('Track error:', data.track.error);
    } else {
        console.log('No track data available');
    }

    layerGroup.addTo(map);
    layerGroup.flightId = flightId;
    currentRouteLayer = layerGroup;
    
    console.log('Layer group added to map with', layerGroup.getLayers().length, 'layers');
    
    // If no layers were added, show a message
    if (layerGroup.getLayers().length === 0) {
        console.warn('No route or track data available to display for flight', flightId);
    }
}

function getPossibilityText(isPossible, level) {
    if (isPossible !== 1) return 'No transit';
    const levelInt = parseInt(level);
    if (levelInt === 1) return 'Low probability';
    if (levelInt === 2) return 'Medium probability';
    if (levelInt === 3) return 'High probability';
    return 'Unknown';
}

// Haversine formula to calculate destination point given start, bearing, and distance
function calculateDestination(lat, lon, bearing, distance) {
    const R = 6371; // Earth's radius in km
    const d = distance / R; // Angular distance
    const brng = bearing * Math.PI / 180; // Convert to radians
    const lat1 = lat * Math.PI / 180;
    const lon1 = lon * Math.PI / 180;

    const lat2 = Math.asin(
        Math.sin(lat1) * Math.cos(d) +
        Math.cos(lat1) * Math.sin(d) * Math.cos(brng)
    );

    const lon2 = lon1 + Math.atan2(
        Math.sin(brng) * Math.sin(d) * Math.cos(lat1),
        Math.cos(d) - Math.sin(lat1) * Math.sin(lat2)
    );

    return {
        lat: lat2 * 180 / Math.PI,
        lon: lon2 * 180 / Math.PI
    };
}

function toggleMap() {
    const mapContainer = document.getElementById('mapContainer');
    const altOverlay = document.getElementById('altitudeOverlay');
    const isHidden = mapContainer.style.display === 'none';

    if (isHidden) {
        mapVisible = true;
        mapContainer.style.display = 'block';
        if (altOverlay) altOverlay.style.display = 'block';

        // Initialize map if not already done
        const lat = parseFloat(document.getElementById('latitude').value);
        const lon = parseFloat(document.getElementById('longitude').value);

        if (!isNaN(lat) && !isNaN(lon)) {
            if (!mapInitialized) {
                initializeMap(lat, lon);
            }
            // Refresh map display
            setTimeout(() => {
                if (map) map.invalidateSize();
            }, 100);
        } else {
            alert('Please enter your coordinates first');
            mapVisible = false;
            mapContainer.style.display = 'none';
            if (altOverlay) altOverlay.style.display = 'none';
        }
    } else {
        mapVisible = false;
        mapContainer.style.display = 'none';
        if (altOverlay) altOverlay.style.display = 'none';
    }
}

// Update map with all data from API response
function updateMapVisualization(data, observerLat, observerLon, observerElev, isForceRefresh = false) {
    if (!map || !mapInitialized) {
        initializeMap(observerLat, observerLon);
    }

    updateObserverMarker(observerLat, observerLon, observerElev);

    // Update bounding box if provided
    if (data.boundingBox) {
        updateBoundingBox(
            data.boundingBox.latLowerLeft,
            data.boundingBox.lonLowerLeft,
            data.boundingBox.latUpperRight,
            data.boundingBox.lonUpperRight
        );
    }

    // Update azimuth arrows - one for each trackable target
    clearAzimuthArrows();
    if (data.targetCoordinates && data.trackingTargets) {
        console.log('Observer position for arrows:', observerLat, observerLon);
        console.log('Observer marker position:', observerMarker ? observerMarker.getLatLng() : 'no marker');
        // Show arrow for each target that is currently being tracked (above horizon)
        data.trackingTargets.forEach(targetName => {
            const coords = data.targetCoordinates[targetName];
            if (coords && coords.azimuthal !== undefined && coords.altitude !== undefined) {
                console.log(`Creating arrow for ${targetName} with azimuth ${coords.azimuthal}`);
                updateAzimuthArrow(observerLat, observerLon, coords.azimuthal, coords.altitude, targetName);
            }
        });
    } else if (data.targetCoordinates && data.targetCoordinates.azimuthal !== undefined) {
        // Single target mode (legacy)
        updateAzimuthArrow(observerLat, observerLon, data.targetCoordinates.azimuthal, data.targetCoordinates.altitude || 0, target);
    }

    // Update aircraft markers (always call to clear stale markers even if empty)
    updateAircraftMarkers(data.flights || [], observerLat, observerLon, true, isForceRefresh);
    if (data.flights && data.flights.length > 0) {
        updateAltitudeOverlay(data.flights);
    }

    // Accumulate flight positions into the traffic heatmap dataset on every refresh
    if (data.flights && data.flights.length > 0) {
        updateHeatmapData(data.flights);
    }
}
