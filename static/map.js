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
let mapInitialized = false;
let boundingBoxUserEdited = localStorage.getItem('boundingBoxUserEdited') === 'true';
let aircraftRouteCache = {};  // Cache fetched routes/tracks
let currentRouteLayer = null;  // Currently displayed route/track
let userInteractingWithMap = false;  // Prevent auto-zoom during user interaction
let headingArrows = {};  // Store heading arrows for medium/high probability transits

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

    // Add OpenStreetMap tiles
    L.tileLayer('https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png', {
        attribution: '&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors',
        maxZoom: 19
    }).addTo(map);

    mapInitialized = true;
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

    // Use saved custom bounding box if it exists (overrides server values)
    const savedBox = localStorage.getItem('customBoundingBox');
    if (savedBox) {
        try {
            const customBox = JSON.parse(savedBox);
            latLowerLeft = customBox.latLowerLeft;
            lonLowerLeft = customBox.lonLowerLeft;
            latUpperRight = customBox.latUpperRight;
            lonUpperRight = customBox.lonUpperRight;
            window.lastBoundingBox = customBox;
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

    // Fit map to bounding box on initial load
    if (fitToBox) {
        map.fitBounds(bounds, { padding: [20, 20] });
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

function updateAzimuthArrow(observerLat, observerLon, azimuth, altitude, targetName) {
    if (!map) return;

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
 * Add a heading arrow to show aircraft's magnetic heading direction
 * Only for medium/high probability transits
 */
function addHeadingArrow(flight, flightId, color) {
    if (!map || !flight.latitude || !flight.longitude || !flight.direction) return;
    
    const lat = flight.latitude;
    const lon = flight.longitude;
    let heading = flight.direction;
    
    // Convert true heading to magnetic heading
    if (typeof geomag !== 'undefined') {
        try {
            const geomagInfo = geomag.field(lat, lon);
            const declination = geomagInfo.declination;
            heading = heading - declination;
            if (heading < 0) heading += 360;
            if (heading >= 360) heading -= 360;
        } catch (error) {
            console.warn('Could not calculate magnetic declination for arrow:', error);
        }
    }
    
    // Calculate arrow endpoint (12km in heading direction - slightly shorter than sun/moon arrows at 15km)
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
    ).addTo(map);
    
    // Store for cleanup
    headingArrows[flightId] = arrowLine;
}

function updateSingleAircraftMarker(flight) {
    if (!map) return;

    const normalizedId = String(flight.id).trim().toUpperCase();

    // Remove existing marker and heading arrow for this flight
    if (aircraftMarkers[normalizedId]) {
        map.removeLayer(aircraftMarkers[normalizedId]);
        delete aircraftMarkers[normalizedId];
    }
    if (headingArrows[normalizedId]) {
        map.removeLayer(headingArrows[normalizedId]);
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

    // Use diamond for transit aircraft, airplane emoji for others
    const isTransit = flight.is_possible_transit === 1;
    const rotation = (flight.direction - 90);

    const aircraftIcon = L.divIcon({
        html: isTransit
            ? `<div style="font-size: 36px; color: ${color}; text-shadow: 0 0 3px black, 0 0 3px black, 0 0 8px ${color}, 1px 1px 0 black, -1px -1px 0 black, 1px -1px 0 black, -1px 1px 0 black; display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; line-height: 1;">◆</div>`
            : `<div style="transform: rotate(${rotation}deg); font-size: 20px;">✈️</div>`,
        iconSize: [36, 36],
        iconAnchor: [18, 18],
        className: 'aircraft-icon'
    });

    // Add marker if we have coordinates
    if (flight.latitude !== undefined && flight.latitude !== null &&
        flight.longitude !== undefined && flight.longitude !== null) {
        const marker = L.marker([flight.latitude, flight.longitude], { icon: aircraftIcon })
            .addTo(map);

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

function updateAircraftMarkers(flights, observerLat, observerLon) {
    if (!map) return;

    // Clear existing aircraft markers
    Object.values(aircraftMarkers).forEach(marker => {
        map.removeLayer(marker);
    });
    aircraftMarkers = {};

    // Add new aircraft markers
    flights.forEach(flight => {
        // Calculate current position (use flight data directly)
        // For future position visualization, we'd need to add predicted coordinates
        const flightId = flight.id;
        
        // Determine color based on possibility level
        let color = COLORS.DEFAULT;
        if (flight.is_possible_transit === 1) {
            const level = parseInt(flight.possibility_level);
            if (level === 1) color = COLORS.LOW;
            else if (level === 2) color = COLORS.MEDIUM;
            else if (level === 3) color = COLORS.HIGH;
        }

        // Use diamond for transit aircraft (NTDS style), airplane emoji for others
        // Convert true heading to magnetic heading for proper visual alignment
        const isTransit = flight.is_possible_transit === 1;
        let headingForDisplay = flight.direction;
        if (typeof geomag !== 'undefined') {
            try {
                const geomagInfo = geomag.field(flight.latitude, flight.longitude);
                const declination = geomagInfo.declination;
                headingForDisplay = flight.direction - declination;
                if (headingForDisplay < 0) headingForDisplay += 360;
                if (headingForDisplay >= 360) headingForDisplay -= 360;
            } catch (error) {
                console.warn('Could not calculate magnetic declination:', error);
            }
        }
        // Airplane emoji points right (90°), so subtract 90 to align with compass heading
        const rotation = (headingForDisplay - 90);

        // Debug: Log heading and rotation for verification
        if (!isTransit && flight.direction) {
            console.log(`Aircraft ${flightId}: true=${flight.direction}°, magnetic=${Math.round(headingForDisplay)}°`);
        }

        const aircraftIcon = L.divIcon({
            html: isTransit
                ? `<div style="font-size: 36px; color: ${color}; text-shadow: 0 0 3px black, 0 0 3px black, 0 0 8px ${color}, 1px 1px 0 black, -1px -1px 0 black, 1px -1px 0 black, -1px 1px 0 black; display: flex; align-items: center; justify-content: center; width: 36px; height: 36px; line-height: 1;">◆</div>`
                : `<div style="transform: rotate(${rotation}deg); font-size: 20px;">✈️</div>`,
            iconSize: [36, 36],
            iconAnchor: [18, 18],  // Center the icon on coordinates
            className: 'aircraft-icon'
        });

        // Add marker if we have coordinates (check for undefined/null, not falsy)
        if (flight.latitude !== undefined && flight.latitude !== null &&
            flight.longitude !== undefined && flight.longitude !== null) {
            const marker = L.marker([flight.latitude, flight.longitude], { icon: aircraftIcon })
                .addTo(map);

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
            
            // Add heading arrow for medium/high probability transits
            if (flight.is_possible_transit === 1) {
                const level = parseInt(flight.possibility_level);
                if (level === 2 || level === 3) {  // MEDIUM or HIGH
                    addHeadingArrow(flight, normalizedId, color);
                }
            }
        }
    });

    // Fit map to show aircraft and observer
    // Skip auto-zoom if user is interacting with map (viewing route/track)
    if (Object.keys(aircraftMarkers).length > 0 && !userInteractingWithMap) {
        const aircraftBounds = L.latLngBounds(
            Object.values(aircraftMarkers).map(marker => marker.getLatLng())
        );
        
        // Check if there are any transits (medium or high probability)
        const hasTransits = flights.some(f => 
            f.is_possible_transit === 1 && 
            (parseInt(f.possibility_level) === 2 || parseInt(f.possibility_level) === 3)
        );
        
        if (hasTransits) {
            // Include observer position in bounds to show the full transit context
            aircraftBounds.extend([observerLat, observerLon]);
            
            // Zoom in more for transits to fill about half the window
            // Reduce padding and allow higher max zoom
            map.fitBounds(aircraftBounds, { padding: [30, 30], maxZoom: 15 });
        } else {
            // Normal view for non-transit aircraft
            map.fitBounds(aircraftBounds, { padding: [50, 50], maxZoom: 13 });
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
    
    if (!map || !faFlightId) {
        console.log('Aborted: map or faFlightId missing', { map: !!map, faFlightId });
        return;
    }

    // If already showing this flight's route, hide it
    if (currentRouteLayer && currentRouteLayer.flightId === flightId) {
        console.log('Hiding current route layer for', flightId);
        map.removeLayer(currentRouteLayer);
        currentRouteLayer = null;
        userInteractingWithMap = false;  // Allow auto-zoom again
        return;
    }

    // User is now interacting with the map - prevent auto-zoom
    userInteractingWithMap = true;
    console.log('User interacting with map, fetching route/track...');

    // Remove previous route if showing different flight
    if (currentRouteLayer) {
        map.removeLayer(currentRouteLayer);
    }

    // Check cache first
    if (aircraftRouteCache[flightId]) {
        displayRouteTrack(aircraftRouteCache[flightId], flightId);
        return;
    }

    // Determine which data to fetch based on transit timing
    const row = document.querySelector(`tr[data-flight-id="${flightId}"]`);
    let transitTime = null;
    if (row) {
        const timeCell = row.querySelector('td:nth-child(17)'); // Time column
        if (timeCell && timeCell.textContent) {
            transitTime = parseFloat(timeCell.textContent);
        }
    }

    // Conditional fetching: only get what's useful
    try {
        let routeResponse = { error: 'Not requested' };
        let trackResponse = { error: 'Not requested' };
        
        if (transitTime !== null && !isNaN(transitTime) && transitTime > 0) {
            // Future transit - show planned route only
            console.log(`Future transit (${transitTime.toFixed(1)} min), fetching route only`);
            routeResponse = await fetch(`/flights/${faFlightId}/route`)
                .then(r => r.json())
                .catch(e => ({ error: e.message }));
        } else {
            // Past/current transit or unknown time - show actual track only
            console.log('Past/current transit, fetching track only');
            trackResponse = await fetch(`/flights/${faFlightId}/track`)
                .then(r => r.json())
                .catch(e => ({ error: e.message }));
        }

        // Cache the data
        aircraftRouteCache[flightId] = { route: routeResponse, track: trackResponse };
        displayRouteTrack(aircraftRouteCache[flightId], flightId);
    } catch (error) {
        console.error('Error fetching route/track:', error);
        alert('Could not fetch route/track data. This may be because the aircraft is not currently transmitting data or API rate limits have been reached.');
    }
}

function displayRouteTrack(data, flightId) {
    if (!map) return;

    const layerGroup = L.layerGroup();

    console.log('Route/Track data for', flightId, ':', data);

    // Display route (blue dashed)
    if (data.route && !data.route.error) {
        console.log('Route data:', data.route);

        // Check different possible response structures
        const waypoints = data.route.waypoints || data.route.route_waypoints || [];

        if (waypoints.length > 0) {
            const routePoints = waypoints
                .filter(pt => pt.latitude != null && pt.longitude != null)
                .map(pt => [pt.latitude, pt.longitude]);

            if (routePoints.length > 0) {
                console.log('Drawing route with', routePoints.length, 'points');
                const routeLine = L.polyline(routePoints, {
                    color: '#4169E1',
                    weight: 3,
                    dashArray: '10, 10',
                    opacity: 0.7
                });
                layerGroup.addLayer(routeLine);
                routeLine.bindPopup('📍 Planned Route (' + routePoints.length + ' waypoints)');
            } else {
                console.log('Route has waypoints but no valid lat/lon coordinates');
            }
        } else {
            console.log('No waypoints in route data. Route may not be available for this flight.');
        }
    } else if (data.route && data.route.error) {
        console.log('Route error:', data.route.error);
    } else {
        console.log('No route data available');
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
function updateMapVisualization(data, observerLat, observerLon, observerElev) {
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

    // Update aircraft markers
    if (data.flights && data.flights.length > 0) {
        updateAircraftMarkers(data.flights, observerLat, observerLon);
        updateAltitudeOverlay(data.flights);
    }
}
