# Vendored Third-Party Libraries

This directory contains self-hosted copies of third-party JavaScript libraries for security and reliability.

## Why Self-Host?

Self-hosting eliminates supply-chain security risks from CDN compromise and ensures the application works offline or when CDNs are unavailable.

## Libraries

### Leaflet 1.9.4
- **Website**: https://leafletjs.com/
- **License**: BSD-2-Clause
- **Files**:
  - `leaflet.js` (144KB) - Core mapping library
  - `leaflet.css` (14KB) - Stylesheet
  - `images/marker-icon.png`, `images/marker-icon-2x.png` - Default markers
  - `images/marker-shadow.png` - Marker shadows
  - `images/layers.png`, `images/layers-2x.png` - Layer control icons

### Leaflet.Editable 1.2.0
- **Repository**: https://github.com/Leaflet/Leaflet.Editable
- **License**: WTFPL
- **Files**:
  - `leaflet-editable.js` (73KB) - Editable shapes plugin

## Updating

To update to newer versions:

```bash
# Leaflet
VERSION=1.9.4
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/leaflet.css" -o static/leaflet.css
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/leaflet.js" -o static/leaflet.js
cd static/images
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/images/marker-icon.png" -o marker-icon.png
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/images/marker-icon-2x.png" -o marker-icon-2x.png
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/images/marker-shadow.png" -o marker-shadow.png
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/images/layers.png" -o layers.png
curl -sL "https://unpkg.com/leaflet@${VERSION}/dist/images/layers-2x.png" -o layers-2x.png

# Leaflet.Editable
VERSION=1.2.0
curl -sL "https://unpkg.com/leaflet-editable@${VERSION}/src/Leaflet.Editable.js" -o static/leaflet-editable.js
```

## Verification

After updating, test the map interface at http://localhost:8000 to ensure:
- Map tiles load correctly
- Markers display properly
- Bounding box editing works (drag corners)
- No console errors

## Security Note

These files are checked into git to ensure reproducible builds and eliminate runtime dependency on third-party CDNs. This follows security best practices for web applications.
