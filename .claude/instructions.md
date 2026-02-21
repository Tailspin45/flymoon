# Flymoon Project Instructions

## Project Structure

This project has one active directory:
- `/Users/Tom/flymoon/` - Main source code and running application

The `/Users/Tom/flymoon/archive/` folder contains outdated distribution copies — do not edit files there.

**IMPORTANT:** Always edit files in `/Users/Tom/flymoon/`, NOT in any archive or dist subfolder.

## File Locations

- **Python backend:** `/Users/Tom/flymoon/src/`
- **Web frontend:** `/Users/Tom/flymoon/static/` and `/Users/Tom/flymoon/templates/`
- **Application entry:** `/Users/Tom/flymoon/app.py`
- **Configuration:** `/Users/Tom/flymoon/.env`

## Running the Application

The Flask server runs from `/Users/Tom/flymoon/` and imports from the appropriate subdirectories (e.g., `src`, `static`, `templates`).

When making changes:
1. Edit files in `/Users/Tom/flymoon/`
2. Restart the server from `/Users/Tom/flymoon/`
3. Clear Python cache if needed: `find /Users/Tom/flymoon -type d -name "__pycache__" -exec rm -rf {} +`

## Automatic Permissions

All file operations within `/Users/Tom/flymoon/**` are pre-approved.
