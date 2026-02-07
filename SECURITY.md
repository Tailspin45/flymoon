# Flymoon Security Guide

## Overview

Flymoon is designed to run on a local network for personal use. This document outlines security considerations and best practices.

## Network Security

### Default Configuration
- **Binding**: Application binds to `0.0.0.0:8000` by default (all interfaces)
- **Debug Mode**: Disabled in production (`debug=False`)
- **Access**: Anyone on your local network can access the application

### Recommendations

#### 1. LAN-Only Deployment (Recommended)
- Keep Flymoon on your local network only
- **DO NOT** expose to the internet without proper security measures
- Use a firewall to block external access to port 8000

#### 2. Localhost-Only (Most Secure)
To restrict to localhost only, modify `app.py`:
```python
app.run(host="127.0.0.1", port=port, debug=False)
```

Access only via: `http://localhost:8000`

#### 3. Reverse Proxy with Authentication
If internet access is needed:
- Use nginx/Apache reverse proxy
- Add HTTPS with Let's Encrypt
- Implement HTTP Basic Auth or OAuth
- Consider VPN instead

## Gallery Security

### Authentication System

Gallery write operations (upload/delete/update) require a bearer token for authentication.

#### Setup Gallery Authentication

1. **Generate a secure token**:
```bash
python3 -c "import secrets; print(secrets.token_urlsafe(32))"
```

2. **Add to `.env`**:
```bash
GALLERY_AUTH_TOKEN=your_generated_token_here
```

3. **Test the setup**:
- Visit `http://localhost:8000/gallery`
- Try to upload an image
- Enter your token when prompted
- Token is stored in browser localStorage

#### How It Works

- **Read operations** (`/gallery`, `/gallery/list`): Public, no auth required
- **Write operations** (`/gallery/upload`, `/gallery/delete`, `/gallery/update`): Require `Authorization: Bearer <token>` header
- **Token validation**: Uses constant-time comparison to prevent timing attacks
- **No token configured**: All write operations return HTTP 403 Forbidden

#### Disabling Gallery Uploads

To make gallery read-only:
```bash
# In .env, leave GALLERY_AUTH_TOKEN empty or remove it
GALLERY_AUTH_TOKEN=
```

### File Upload Restrictions

- **Allowed types**: PNG, JPG, JPEG, GIF only
- **Max file size**: 16 MB per upload
- **Path validation**: All paths validated to prevent directory traversal
- **Filename sanitization**: Uses `werkzeug.secure_filename()` for safe filenames

## API Security

### FlightAware API Key
- Stored in `.env` file (not committed to git)
- Never expose in logs or error messages
- Regenerate if compromised

### Telescope Control
- Seestar telescope accessed via local network JSON-RPC
- No authentication built into Seestar protocol
- **Risk**: Anyone on LAN can control telescope if endpoints are accessible
- **Mitigation**: Keep Flymoon on trusted network only

### Rate Limiting
- No rate limiting implemented
- FlightAware API: Personal tier = 10 queries/minute
- Application uses caching to reduce API calls

## Data Security

### Local Storage
- Flight logs: `data/possible_transits.log`
- Gallery images: `static/gallery/YYYY/MM/`
- No encryption at rest
- Protect file system permissions

### Sensitive Data
- API keys in `.env` (not in git)
- Telegram bot tokens in `.env`
- Gallery auth token in `.env`
- Observer location (lat/lon) not sensitive but avoid over-sharing

## Vulnerability Disclosure

If you discover a security vulnerability:
1. **DO NOT** open a public GitHub issue
2. Email the maintainer privately (see README)
3. Include reproduction steps
4. Allow time for fix before public disclosure

## Security Checklist

Before running Flymoon:
- [ ] `.env` file has strong `GALLERY_AUTH_TOKEN` or left empty for read-only
- [ ] Application not exposed to internet
- [ ] Firewall blocks external access to port 8000
- [ ] File system permissions restrict `.env` access
- [ ] FlightAware API key kept private
- [ ] Telescope on trusted network segment only
- [ ] Debug mode disabled (`debug=False`)

## Security Updates

Stay informed about security updates:
- Watch the GitHub repository for security advisories
- Pull latest changes regularly: `git pull origin main`
- Review `CHANGELOG.md` for security fixes

## Threat Model

### In Scope (Protected)
- Unauthorized file uploads/deletions (via gallery auth)
- Directory traversal attacks (via path validation)
- Arbitrary code execution (debug disabled, no eval/exec)

### Out of Scope (User Responsibility)
- Network access control (use firewall)
- Physical access to server
- API key compromise from external breach
- Telescope firmware vulnerabilities

## Best Practices

1. **Run as non-root user**
2. **Keep Python dependencies updated**: `pip install -U -r requirements.txt`
3. **Regular backups** of gallery and flight logs
4. **Monitor access logs** for suspicious activity
5. **Use HTTPS** if exposing beyond LAN (reverse proxy)
6. **Rotate tokens** periodically (especially if shared)

---

**Remember**: Flymoon is designed for personal/small group use on trusted networks, not as a public web service. Additional security hardening required for internet-facing deployments.
