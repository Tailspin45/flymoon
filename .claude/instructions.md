# Flymoon Project Instructions

## Project Structure

This project has two parallel directory structures:
- `/Users/Tom/flymoon/` - Main source code and running application
- `/Users/Tom/flymoon/dist/Flymoon-Web/` - Distribution/build directory (may be outdated)

**IMPORTANT:** Always edit files in `/Users/Tom/flymoon/` (the parent directory), NOT in the dist folder.

## File Locations

- **Python backend:** `/Users/Tom/flymoon/src/`
- **Web frontend:** `/Users/Tom/flymoon/static/` and `/Users/Tom/flymoon/templates/`
- **Application entry:** `/Users/Tom/flymoon/dist/Flymoon-Web/app.py`
- **Configuration:** `/Users/Tom/flymoon/.env`

## Running the Application

The Flask server runs from `/Users/Tom/flymoon/dist/Flymoon-Web/` but imports from the parent `/Users/Tom/flymoon/` directory.

When making changes:
1. Edit files in `/Users/Tom/flymoon/`
2. Restart the server from `/Users/Tom/flymoon/dist/Flymoon-Web/`
3. Clear Python cache if needed: `find /Users/Tom/flymoon -type d -name "__pycache__" -exec rm -rf {} +`

## Automatic Permissions

All file operations within `/Users/Tom/flymoon/**` are pre-approved.
