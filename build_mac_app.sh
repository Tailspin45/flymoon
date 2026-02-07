#!/bin/bash
# Build Mac .app bundle for Transit Monitor
# This creates a double-clickable application that prompts for configuration
# and runs transit_capture.py in the background

APP_NAME="Transit Monitor"
SCRIPT_NAME="transit_capture.py"
BUNDLE_ID="com.flymoon.transit-monitor"

# AppleScript that prompts for configuration
APPLESCRIPT=$(cat << 'APPLESCRIPT_END'
on run
    -- Check if .env exists
    set projectPath to do shell script "dirname " & quoted form of POSIX path of (path to me) & " | xargs dirname | xargs dirname | xargs dirname"
    set envPath to projectPath & "/.env"
    
    try
        do shell script "test -f " & quoted form of envPath
    on error
        display dialog "ERROR: .env file not found!" & return & return & "Please create .env from .env.mock and configure:" & return & "• AEROAPI_API_KEY" & return & "• TELEGRAM_BOT_TOKEN" & return & "• TELEGRAM_CHAT_ID" & return & "• Observer location (lat/lon/elevation)" & return & return & "See SETUP.md for instructions." buttons {"Open Documentation", "Cancel"} default button "Cancel"
        if button returned of result is "Open Documentation" then
            do shell script "open " & quoted form of (projectPath & "/SETUP.md")
        end if
        return
    end try
    
    -- Check if TELEGRAM_BOT_TOKEN is configured
    try
        do shell script "grep -q '^TELEGRAM_BOT_TOKEN=.\\+' " & quoted form of envPath
    on error
        display dialog "ERROR: TELEGRAM_BOT_TOKEN not configured in .env" & return & return & "Transit Monitor requires Telegram for notifications." & return & return & "See SETUP.md for setup instructions." buttons {"Open Documentation", "Cancel"} default button "Cancel"
        if button returned of result is "Open Documentation" then
            do shell script "open " & quoted form of (projectPath & "/SETUP.md")
        end if
        return
    end try
    
    -- Prompt for observer location (pre-filled from .env if available)
    set defaultLat to do shell script "grep '^OBSERVER_LATITUDE=' " & quoted form of envPath & " | cut -d= -f2 || echo ''"
    set defaultLon to do shell script "grep '^OBSERVER_LONGITUDE=' " & quoted form of envPath & " | cut -d= -f2 || echo ''"
    set defaultElev to do shell script "grep '^OBSERVER_ELEVATION=' " & quoted form of envPath & " | cut -d= -f2 || echo '0'"
    
    -- Prompt for target
    set targetChoice to button returned of (display dialog "Select transit target:" buttons {"Sun", "Moon", "Auto"} default button "Sun")
    set targetName to targetChoice
    if targetName is "Sun" then
        set targetArg to "sun"
    else if targetName is "Moon" then
        set targetArg to "moon"
    else
        set targetArg to "auto"
    end if
    
    -- Confirm settings
    set confirmMsg to "Transit Monitor will start with:" & return & return & "Target: " & targetName & return & "Location: " & defaultLat & ", " & defaultLon & return & "Elevation: " & defaultElev & "m" & return & return & "Monitoring will run in the background." & return & "Close this application to stop monitoring."
    
    display dialog confirmMsg buttons {"Cancel", "Start Monitoring"} default button "Start Monitoring"
    if button returned of result is "Cancel" then
        return
    end if
    
    -- Build command with proper escaping
    set cmd to "cd " & quoted form of projectPath & " && source .venv/bin/activate && python3 transit_capture.py --latitude " & defaultLat & " --longitude " & defaultLon & " --elevation " & defaultElev & " --target " & targetArg & " > /tmp/transit_monitor.log 2>&1 &"
    
    -- Start monitoring
    do shell script cmd
    
    -- Show success message
    display dialog "✅ Transit Monitor Started!" & return & return & "Monitoring " & targetName & " transits." & return & "You will receive Telegram notifications for high-probability transits." & return & return & "Logs: /tmp/transit_monitor.log" buttons {"OK"} default button "OK"
    
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
