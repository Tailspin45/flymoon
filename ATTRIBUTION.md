# Open-Source Attributions

Flymoon incorporates the following open-source libraries and services. Their licenses and acknowledgements are listed below.

---

## JavaScript / Frontend

### Leaflet 1.9.4
- **Project:** https://leafletjs.com
- **Copyright:** © 2010–2023 Vladimir Agafonkin, © 2010–2011 CloudMade
- **License:** BSD 2-Clause
- **Bundled as:** `static/leaflet.js`, `static/leaflet.css`

> Redistribution and use in source and binary forms, with or without modification, are permitted provided that the following conditions are met: (1) Redistributions of source code must retain the above copyright notice; (2) Redistributions in binary form must reproduce the above copyright notice in the documentation and/or other materials provided with the distribution.

### Leaflet.Editable
- **Project:** https://github.com/Leaflet/Leaflet.editable
- **Copyright:** © Yoann Aubineau / Makina Corpus
- **License:** MIT
- **Bundled as:** `static/leaflet-editable.js`

### Leaflet.heat
- **Project:** https://github.com/Leaflet/Leaflet.heat
- **Copyright:** © 2014 Vladimir Agafonkin
- **License:** MIT
- **Bundled as:** `static/leaflet-heat.js`

---

## Python Libraries

### Skyfield
- **Version:** 1.49
- **Project:** https://rhodesmill.org/skyfield/
- **Copyright:** © Brandon Rhodes
- **License:** MIT
- **Used for:** High-precision Sun and Moon altitude/azimuth calculations.

### Flask
- **Version:** 3.0.3
- **Project:** https://flask.palletsprojects.com
- **Copyright:** © Pallets
- **License:** BSD 3-Clause
- **Used for:** Web application framework.

### Requests
- **Version:** 2.32.3
- **Project:** https://requests.readthedocs.io
- **Copyright:** © Kenneth Reitz
- **License:** Apache 2.0
- **Used for:** HTTP calls to FlightAware AeroAPI and OpenAIP tile proxy.

### python-telegram-bot
- **Version:** 21.0
- **Project:** https://python-telegram-bot.org
- **Copyright:** © Leandro Toledo de Souza and contributors
- **License:** GNU Lesser General Public License v3 (LGPLv3)
- **Used for:** Telegram transit alert notifications.

### python-dotenv
- **Version:** 1.0.1
- **Project:** https://github.com/theskumar/python-dotenv
- **Copyright:** © Saurabh Kumar
- **License:** BSD 3-Clause
- **Used for:** Loading environment variables from `.env`.

### tzlocal
- **Version:** 5.2
- **Project:** https://github.com/regebro/tzlocal
- **Copyright:** © Lennart Regebro
- **License:** MIT
- **Used for:** Detecting the local timezone for celestial time calculations.

---

## Data Sources & External Services

### FlightAware AeroAPI
- **Provider:** FlightAware, LLC
- **Website:** https://www.flightaware.com/commercial/aeroapi/
- **Usage:** Real-time bounding-box flight searches (primary data source).
- **Terms:** https://flightaware.com/about/termsofuse
- **Note:** Proprietary commercial API. Requires an API key. Not open-source.

### OpenAIP
- **Provider:** OpenAIP contributors
- **Website:** https://www.openaip.net
- **Usage:** Aviation chart tile overlay (airspace boundaries, airports).
- **License:** [Creative Commons Attribution–NonCommercial–ShareAlike 4.0 (CC BY-NC-SA 4.0)](https://creativecommons.org/licenses/by-nc-sa/4.0/)
- **Attribution required:** Map tiles sourced from OpenAIP (www.openaip.net).
- **Note:** Tiles are fetched server-side via Flask proxy to avoid browser extension blocking. The API key is kept server-side and never exposed to the browser.

### OpenSky Network *(planned — Hybrid mode)*
- **Provider:** The OpenSky Network
- **Website:** https://opensky-network.org
- **Usage:** Free real-time ADS-B aircraft positions (planned integration for Hybrid and OpenSky-only cost modes).
- **Terms:** https://opensky-network.org/about/terms-of-use
- **Citation:** Matthias Schäfer, Martin Strohmeier, Vincent Lenders, Ivan Martinovic, Matthias Wilhelm. *Bringing Up OpenSky: A Large-scale ADS-B Sensor Network for Research*. IPSN 2014.

### NASA/JPL DE421 Ephemeris
- **Provider:** NASA Jet Propulsion Laboratory
- **Website:** https://naif.jpl.nasa.gov/pub/naif/generic_kernels/spk/planets/
- **Usage:** Planetary positions for precise Sun and Moon calculations (via Skyfield).
- **License:** Public Domain (US Government Work)
- **File:** `de421.bsp` — downloaded automatically by Skyfield on first run.

---

## MIT License Text (for MIT-licensed components above)

```
Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
```
