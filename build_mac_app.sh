#!/bin/bash
# Build Mac .app bundle for Transit Monitor
# This creates a double-clickable application that prompts for configuration
# and runs transit_capture.py in the background

APP_NAME="Transit Monitor"
SCRIPT_NAME="transit_capture.py"
BUNDLE_ID="com.flymoon.transit-monitor"

# AppleScript that prompts for configuration
APPLESCRIPT=$(cat << 'APPLESCRIPT_END'
on cancelSetup()
    display dialog "Setup cancelled. You can run the app again to restart setup." buttons {"OK"} default button "OK"
end cancelSetup

on run
    -- ── Find project path (bundle is 4 levels deep inside the .app) ────────────
    set projectPath to do shell script "dirname " & quoted form of POSIX path of (path to me) & " | xargs dirname | xargs dirname | xargs dirname"
    set envPath to projectPath & "/.env"
    set envMockPath to projectPath & "/.env.mock"
    set venvCmd to "cd " & quoted form of projectPath & " && source .venv/bin/activate"

    -- ── Silently bootstrap .env from .env.mock if missing ─────────────────────
    try
        do shell script "test -f " & quoted form of envPath
    on error
        try
            do shell script "cp " & quoted form of envMockPath & " " & quoted form of envPath
        on error
            display dialog "⚠️ Could not create .env file." & return & return & "Expected .env.mock at:" & return & projectPath & return & return & "Please check the installation and try again." buttons {"OK"} default button "OK"
            return
        end try
    end try

    -- ── Skip wizard if already fully configured ────────────────────────────────
    set needsSetup to true
    try
        do shell script venvCmd & " && python3 src/config_wizard.py --validate"
        set needsSetup to false
    on error
        set needsSetup to true
    end try

    -- ══════════════════════════════════════════════════════════════════════════
    -- SETUP WIZARD  (skipped when already configured)
    -- ══════════════════════════════════════════════════════════════════════════
    if needsSetup then

        -- ── Welcome ───────────────────────────────────────────────────────────
        try
            display dialog "🌙 Welcome to Flymoon!" & return & return & "Let's get you set up in 5 easy steps so you can start detecting aircraft transiting the Sun and Moon." & return & return & "You'll need:" & return & "  ✈️  A free FlightAware API key" & return & "  📍 Your location (latitude & longitude)" & return & "  📲 Telegram (optional, for notifications)" & return & "  🔭 Seestar telescope (optional)" & return & return & "Ready? Let's go!" buttons {"Cancel", "Let's Go! →"} default button "Let's Go! →"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of result is "Cancel" then
            cancelSetup()
            return
        end if

        -- ── Step 1/5 — FlightAware API key ───────────────────────────────────
        set apiKey to ""
        repeat
            try
                set d to display dialog "✈️  Step 1 of 5 — FlightAware API Key" & return & return & "Flymoon fetches live flight data from FlightAware AeroAPI. A free personal-tier key gives you 10 queries/minute — more than enough." & return & return & "Get a free key at:" & return & "https://www.flightaware.com/aeroapi/portal/" & return & return & "Paste your API key below:" buttons {"Cancel", "Open Website", "Next →"} default button "Next →" default answer ""
            on error number -128
                cancelSetup()
                return
            end try
            set clickedBtn to button returned of d
            if clickedBtn is "Cancel" then
                cancelSetup()
                return
            else if clickedBtn is "Open Website" then
                do shell script "open 'https://www.flightaware.com/aeroapi/portal/'"
                -- loop so user can paste after returning from browser
            else
                set apiKey to text returned of d
                if apiKey is "" then
                    display dialog "⚠️ An API key is required to fetch flight data. Please paste your key." buttons {"OK"} default button "OK"
                else
                    exit repeat
                end if
            end if
        end repeat

        -- ── Step 2/5 — Location ───────────────────────────────────────────────
        -- Latitude
        set lat to ""
        repeat
            try
                set d to display dialog "📍 Step 2 of 5 — Your Location (1 of 3)" & return & return & "Enter your latitude (decimal degrees)." & return & return & "Examples:" & return & "  34.052  →  Los Angeles" & return & "  51.507  →  London" & return & "  48.856  →  Paris" & return & "  35.689  →  Tokyo" & return & return & "💡 Tip: Right-click any spot in Google Maps to see its coordinates." buttons {"Cancel", "Next →"} default button "Next →" default answer ""
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set lat to text returned of d
            if lat is "" then
                display dialog "⚠️ Latitude is required." buttons {"OK"} default button "OK"
            else
                exit repeat
            end if
        end repeat

        -- Longitude
        set lon to ""
        repeat
            try
                set d to display dialog "📍 Step 2 of 5 — Your Location (2 of 3)" & return & return & "Enter your longitude (decimal degrees)." & return & return & "Examples:" & return & "  -118.243  →  Los Angeles" & return & "  -0.127   →  London" & return & "  2.352    →  Paris" & return & "  139.691  →  Tokyo" & return & return & "💡 Tip: maps.google.com → right-click any location → copy coordinates." buttons {"Cancel", "Next →"} default button "Next →" default answer ""
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set lon to text returned of d
            if lon is "" then
                display dialog "⚠️ Longitude is required." buttons {"OK"} default button "OK"
            else
                exit repeat
            end if
        end repeat

        -- Elevation
        try
            set d to display dialog "📍 Step 2 of 5 — Your Location (3 of 3)" & return & return & "Enter your elevation above sea level in metres." & return & return & "Examples: 71m (Los Angeles), 11m (London), 35m (Paris)" & return & return & "💡 Leave as 0 if you're unsure — it has minimal impact on calculations." buttons {"Cancel", "Next →"} default button "Next →" default answer "0"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of d is "Cancel" then
            cancelSetup()
            return
        end if
        set elev to text returned of d
        if elev is "" then set elev to "0"

        -- ── Step 3/5 — Bounding box ───────────────────────────────────────────
        try
            set d to display dialog "🗺️  Step 3 of 5 — Search Area (Bounding Box)" & return & return & "Flymoon only fetches flights inside a rectangular area around you. A bigger box means more flights to check but more API calls." & return & return & "Recommended: auto-compute ±2° around your location (≈ 220 km radius). Covers most aircraft visible from your position." & return & return & "Or enter the four corner coordinates manually." buttons {"Cancel", "Enter Manually", "Auto ±2° (Recommended)"} default button "Auto ±2° (Recommended)"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of d is "Cancel" then
            cancelSetup()
            return
        end if

        if button returned of d is "Auto ±2° (Recommended)" then
            set latLL to do shell script "python3 -c 'print(round(" & lat & " - 2, 3))'"
            set lonLL to do shell script "python3 -c 'print(round(" & lon & " - 2, 3))'"
            set latUR to do shell script "python3 -c 'print(round(" & lat & " + 2, 3))'"
            set lonUR to do shell script "python3 -c 'print(round(" & lon & " + 2, 3))'"
        else
            -- Manual — pre-fill with ±2° defaults for convenience
            set defLatLL to do shell script "python3 -c 'print(round(" & lat & " - 2, 3))'"
            set defLonLL to do shell script "python3 -c 'print(round(" & lon & " - 2, 3))'"
            set defLatUR to do shell script "python3 -c 'print(round(" & lat & " + 2, 3))'"
            set defLonUR to do shell script "python3 -c 'print(round(" & lon & " + 2, 3))'"

            try
                set d to display dialog "🗺️  Step 3 of 5 — SW Corner (Lower-Left)" & return & return & "Enter the latitude of the south-west corner of your search area:" buttons {"Cancel", "Next →"} default button "Next →" default answer defLatLL
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set latLL to text returned of d

            try
                set d to display dialog "🗺️  Step 3 of 5 — SW Corner (Lower-Left)" & return & return & "Enter the longitude of the south-west corner of your search area:" buttons {"Cancel", "Next →"} default button "Next →" default answer defLonLL
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set lonLL to text returned of d

            try
                set d to display dialog "🗺️  Step 3 of 5 — NE Corner (Upper-Right)" & return & return & "Enter the latitude of the north-east corner of your search area:" buttons {"Cancel", "Next →"} default button "Next →" default answer defLatUR
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set latUR to text returned of d

            try
                set d to display dialog "🗺️  Step 3 of 5 — NE Corner (Upper-Right)" & return & return & "Enter the longitude of the north-east corner of your search area:" buttons {"Cancel", "Next →"} default button "Next →" default answer defLonUR
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set lonUR to text returned of d
        end if

        -- ── Step 4/5 — Telegram ───────────────────────────────────────────────
        set telegramToken to ""
        set telegramChatId to ""

        try
            set d to display dialog "📲 Step 4 of 5 — Telegram Notifications" & return & return & "Flymoon can send you real-time alerts on your phone when a high-probability transit is detected. Notifications are sent via a free Telegram bot you create yourself (takes about 2 minutes)." & return & return & "You can skip this now and add it later by editing .env." buttons {"Cancel", "Skip for Now", "Set Up Telegram"} default button "Set Up Telegram"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of d is "Cancel" then
            cancelSetup()
            return
        end if

        if button returned of d is "Set Up Telegram" then
            -- Bot token
            try
                set d to display dialog "📲 Step 4 of 5 — Telegram Bot Token" & return & return & "How to get your Bot Token:" & return & "  1. Open Telegram and search for @BotFather" & return & "  2. Send /newbot and follow the prompts" & return & "  3. Copy the token BotFather sends you" & return & "     (looks like: 123456789:ABCdef…)" & return & return & "Paste your Bot Token here:" buttons {"Cancel", "Next →"} default button "Next →" default answer ""
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set telegramToken to text returned of d

            -- Chat ID (only if token was provided)
            if telegramToken is not "" then
                set chatIdUrl to "https://api.telegram.org/bot" & telegramToken & "/getUpdates"
                try
                    set d to display dialog "📲 Step 4 of 5 — Telegram Chat ID" & return & return & "How to get your Chat ID:" & return & "  1. Send any message to your new bot in Telegram" & return & "  2. Open this URL in a browser:" & return & "     " & chatIdUrl & return & "  3. Find the 'id' field inside the 'chat' object" & return & return & "Paste your Chat ID here:" buttons {"Cancel", "Open URL", "Next →"} default button "Next →" default answer ""
                on error number -128
                    cancelSetup()
                    return
                end try

                if button returned of d is "Cancel" then
                    cancelSetup()
                    return
                else if button returned of d is "Open URL" then
                    do shell script "open " & quoted form of chatIdUrl
                    try
                        set d2 to display dialog "📲 Step 4 of 5 — Telegram Chat ID" & return & return & "Once you've found the 'id' in the getUpdates response, paste it below:" buttons {"Cancel", "Next →"} default button "Next →" default answer ""
                    on error number -128
                        cancelSetup()
                        return
                    end try
                    if button returned of d2 is "Cancel" then
                        cancelSetup()
                        return
                    end if
                    set telegramChatId to text returned of d2
                else
                    set telegramChatId to text returned of d
                end if
            end if
        end if

        -- ── Step 5/5 — Seestar telescope ─────────────────────────────────────
        set enableSeestar to "false"
        set seestarHost to "192.168.1.100"
        set seestarPort to "4700"

        try
            set d to display dialog "🔭 Step 5 of 5 — Seestar S50 Telescope" & return & return & "Flymoon can automatically trigger your Seestar S50 to start recording video just before an aircraft transits the Sun or Moon, and stop after." & return & return & "Requirements:" & return & "  • Seestar on the same Wi-Fi network as this Mac" & return & "  • Already tracking the Sun or Moon before the transit" & return & return & "Skip this if you don't have a Seestar." buttons {"Cancel", "Skip", "Enable Seestar"} default button "Skip"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of d is "Cancel" then
            cancelSetup()
            return
        end if

        if button returned of d is "Enable Seestar" then
            set enableSeestar to "true"

            try
                set d to display dialog "🔭 Step 5 of 5 — Seestar IP Address" & return & return & "Enter your Seestar's IP address." & return & return & "Find it in:" & return & "  • The Seestar app → Device Info" & return & "  • Your router's connected-devices list" buttons {"Cancel", "Next →"} default button "Next →" default answer "192.168.1.100"
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set seestarHost to text returned of d
            if seestarHost is "" then set seestarHost to "192.168.1.100"

            try
                set d to display dialog "🔭 Step 5 of 5 — Seestar Port" & return & return & "Enter the Seestar's control port." & return & "The default is 4700 — only change this if you've customised it." buttons {"Cancel", "Next →"} default button "Next →" default answer "4700"
            on error number -128
                cancelSetup()
                return
            end try
            if button returned of d is "Cancel" then
                cancelSetup()
                return
            end if
            set seestarPort to text returned of d
            if seestarPort is "" then set seestarPort to "4700"
        end if

        -- ── Write all values to .env ──────────────────────────────────────────
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'AEROAPI_API_KEY', " & quoted form of apiKey & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'OBSERVER_LATITUDE', " & quoted form of lat & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'OBSERVER_LONGITUDE', " & quoted form of lon & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'OBSERVER_ELEVATION', " & quoted form of elev & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'LAT_LOWER_LEFT', " & quoted form of latLL & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'LONG_LOWER_LEFT', " & quoted form of lonLL & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'LAT_UPPER_RIGHT', " & quoted form of latUR & ")\""
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'LONG_UPPER_RIGHT', " & quoted form of lonUR & ")\""
        if telegramToken is not "" then
            do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'TELEGRAM_BOT_TOKEN', " & quoted form of telegramToken & ")\""
        end if
        if telegramChatId is not "" then
            do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'TELEGRAM_CHAT_ID', " & quoted form of telegramChatId & ")\""
        end if
        do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'ENABLE_SEESTAR', " & quoted form of enableSeestar & ")\""
        if enableSeestar is "true" then
            do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'SEESTAR_HOST', " & quoted form of seestarHost & ")\""
            do shell script venvCmd & " && python3 -c \"from dotenv import set_key; set_key('.env', 'SEESTAR_PORT', " & quoted form of seestarPort & ")\""
        end if

        -- ── Review summary ────────────────────────────────────────────────────
        set apiKeyDisplay to apiKey
        if length of apiKey > 8 then set apiKeyDisplay to (text 1 thru 8 of apiKey) & "…"

        set telegramStatus to "⏭️  Skipped"
        if telegramToken is not "" and telegramChatId is not "" then
            set telegramStatus to "✅ Configured"
        else if telegramToken is not "" then
            set telegramStatus to "⚠️  Token set, no Chat ID"
        end if

        set seestarStatus to "⏭️  Skipped"
        if enableSeestar is "true" then set seestarStatus to "✅ " & seestarHost & ":" & seestarPort

        set summaryMsg to "🎉 Setup complete! Here's what was saved:" & return & return & "✈️  API Key:    " & apiKeyDisplay & return & "📍 Location:   " & lat & ", " & lon & " (" & elev & "m)" & return & "🗺️  Box SW:     " & latLL & ", " & lonLL & return & "🗺️  Box NE:     " & latUR & ", " & lonUR & return & "📲 Telegram:   " & telegramStatus & return & "🔭 Seestar:    " & seestarStatus & return & return & "All values saved to .env — you can edit it anytime."

        try
            display dialog summaryMsg buttons {"Go Back", "Looks Good! →"} default button "Looks Good! →"
        on error number -128
            cancelSetup()
            return
        end try
        if button returned of result is "Go Back" then
            display dialog "To change any setting, edit .env directly at:" & return & return & projectPath & "/.env" & return & return & "Then reopen the app." buttons {"OK"} default button "OK"
            return
        end if

    end if
    -- ══════════════════════════════════════════════════════════════════════════
    -- END OF WIZARD
    -- ══════════════════════════════════════════════════════════════════════════

    -- ── Target selection ──────────────────────────────────────────────────────
    try
        set d to display dialog "🌟 Select Transit Target" & return & return & "Which celestial body should Flymoon watch for aircraft transits?" buttons {"Cancel", "🌙 Moon", "☀️ Sun"} default button "☀️ Sun"
    on error number -128
        cancelSetup()
        return
    end try
    if button returned of d is "Cancel" then
        cancelSetup()
        return
    else if button returned of d is "🌙 Moon" then
        set targetArg to "moon"
        set targetName to "Moon 🌙"
    else
        set targetArg to "sun"
        set targetName to "Sun ☀️"
    end if

    -- ── Read confirmed location from .env (covers "already configured" path) ──
    set confirmedLat to do shell script "grep '^OBSERVER_LATITUDE=' " & quoted form of envPath & " | cut -d= -f2 | tr -d '\"' || echo ''"
    set confirmedLon to do shell script "grep '^OBSERVER_LONGITUDE=' " & quoted form of envPath & " | cut -d= -f2 | tr -d '\"' || echo ''"
    set confirmedElev to do shell script "grep '^OBSERVER_ELEVATION=' " & quoted form of envPath & " | cut -d= -f2 | tr -d '\"' || echo '0'"

    -- ── Launch confirmation ───────────────────────────────────────────────────
    try
        display dialog "🚀 Ready to start monitoring!" & return & return & "Target:    " & targetName & return & "Location:  " & confirmedLat & ", " & confirmedLon & return & "Elevation: " & confirmedElev & "m" & return & return & "Flymoon will run in the background and alert you to any transits. Logs are written to /tmp/transit_monitor.log." buttons {"Cancel", "Start Monitoring 🚀"} default button "Start Monitoring 🚀"
    on error number -128
        cancelSetup()
        return
    end try
    if button returned of result is "Cancel" then
        cancelSetup()
        return
    end if

    -- ── Launch transit_capture.py ─────────────────────────────────────────────
    set cmd to "cd " & quoted form of projectPath & " && source .venv/bin/activate && python3 transit_capture.py --latitude " & confirmedLat & " --longitude " & confirmedLon & " --elevation " & confirmedElev & " --target " & targetArg & " > /tmp/transit_monitor.log 2>&1 &"
    do shell script cmd

    display dialog "✅ Transit Monitor is running!" & return & return & "Monitoring " & targetName & " transits in the background." & return & "You'll receive Telegram notifications for high-probability transits." & return & return & "📄 Logs: /tmp/transit_monitor.log" buttons {"OK"} default button "OK"

end run
APPLESCRIPT_END
)

# Create app bundle structure
echo "Creating app bundle structure..."
mkdir -p "${APP_NAME}.app/Contents/MacOS"
mkdir -p "${APP_NAME}.app/Contents/Resources"

# Compile AppleScript
echo "Compiling AppleScript..."
echo "$APPLESCRIPT" | osacompile -o "${APP_NAME}.app/Contents/Resources/applet.scpt"

# Create launcher script
cat > "${APP_NAME}.app/Contents/MacOS/${APP_NAME}" << 'LAUNCHER_END'
#!/bin/bash
DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
osascript "${DIR}/../Resources/applet.scpt"
LAUNCHER_END

chmod +x "${APP_NAME}.app/Contents/MacOS/${APP_NAME}"

# Create Info.plist
cat > "${APP_NAME}.app/Contents/Info.plist" << PLIST_END
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleDevelopmentRegion</key>
    <string>en</string>
    <key>CFBundleExecutable</key>
    <string>${APP_NAME}</string>
    <key>CFBundleIconFile</key>
    <string>AppIcon</string>
    <key>CFBundleIdentifier</key>
    <string>${BUNDLE_ID}</string>
    <key>CFBundleInfoDictionaryVersion</key>
    <string>6.0</string>
    <key>CFBundleName</key>
    <string>${APP_NAME}</string>
    <key>CFBundlePackageType</key>
    <string>APPL</string>
    <key>CFBundleShortVersionString</key>
    <string>1.0</string>
    <key>CFBundleVersion</key>
    <string>1</string>
    <key>LSMinimumSystemVersion</key>
    <string>10.13</string>
    <key>NSHighResolutionCapable</key>
    <true/>
</dict>
</plist>
PLIST_END

echo "✅ ${APP_NAME}.app created successfully!"
echo ""
echo "To use:"
echo "  1. Ensure .env is configured with TELEGRAM_BOT_TOKEN and observer location"
echo "  2. Double-click '${APP_NAME}.app'"
echo "  3. Select target (Sun/Moon/Auto)"
echo "  4. Click 'Start Monitoring'"
echo ""
echo "The app will run transit_capture.py in the background."
echo "Logs are written to /tmp/transit_monitor.log"
