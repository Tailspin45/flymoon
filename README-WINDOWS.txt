Zipcatcher for Windows — Quick Start
=====================================

No extra software required — everything is included.


1) INSTALL ZIPCATCHER
---------------------
Double-click:
  START-HERE-Install-Zipcatcher.bat

This launches the Zipcatcher setup wizard.
Follow the prompts (click Next, then Install, then Finish).
Zipcatcher will start automatically when installation is complete.


2) RUNNING ZIPCATCHER AGAIN
-----------------------------
Use the Zipcatcher shortcut on your Desktop or in the Start Menu.


3) FIRST-TIME CONFIGURATION
-----------------------------
On first launch Zipcatcher will ask for your FlightAware API key and
observer location. Follow the on-screen setup wizard to complete
configuration.


4) SYSTEM TRAY
--------------
Zipcatcher runs in the Windows system tray.

  Gray   = idle / waiting
  Green  = actively monitoring for transits
  Orange = transit detected
  Red    = error (check the log)

Right-click the tray icon to change target (Sun/Moon), mute
Telegram alerts, or exit.


5) TELESCOPE (SEESTAR S50) — OPTIONAL
---------------------------------------
If you have a Seestar S50:
- Make sure the scope and your PC are on the same Wi-Fi network.
- Open Zipcatcher in your browser (http://localhost:8000).
- Click "Find Scope" in the Telescope panel — Zipcatcher will
  discover it automatically.
- Select Sun or Moon as the target and Zipcatcher will slew and
  record automatically when a high-probability transit is imminent.


TROUBLESHOOTING
---------------
- If Windows SmartScreen appears, click "More info" -> "Run anyway".
- If a firewall prompt appears, allow private network access.
- If Zipcatcher does not start after install, use the Desktop shortcut.
- Logs are written to %TEMP%\zipcatcher.log
