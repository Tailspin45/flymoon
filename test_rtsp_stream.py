#!/usr/bin/env python3
"""
Test script to diagnose Seestar RTSP stream availability.
"""

import os
import socket
import subprocess
from dotenv import load_dotenv

load_dotenv()

# Get telescope settings
host = os.getenv("SEESTAR_HOST", "192.168.1.100")
rtsp_port = int(os.getenv("SEESTAR_RTSP_PORT", "4554"))

print("🔭 Seestar RTSP Stream Diagnostic")
print("=" * 50)
print(f"Host: {host}")
print(f"RTSP Port: {rtsp_port}")
print()

# Test 1: Check if host is reachable
print("Test 1: Checking if telescope is reachable...")
try:
    socket.create_connection((host, rtsp_port), timeout=5)
    print(f"✅ Port {rtsp_port} is open on {host}")
except Exception as e:
    print(f"❌ Cannot reach {host}:{rtsp_port}")
    print(f"   Error: {e}")
    print("\nTroubleshooting:")
    print("  - Is telescope powered on?")
    print("  - Is SEESTAR_HOST correct in .env?")
    print("  - Are you on the same network as the telescope?")
    exit(1)

# Test 2: Check FFmpeg
print("\nTest 2: Checking FFmpeg installation...")
try:
    result = subprocess.run(['ffmpeg', '-version'], capture_output=True, text=True)
    version_line = result.stdout.split('\n')[0]
    print(f"✅ {version_line}")
except FileNotFoundError:
    print("❌ FFmpeg not found")
    print("\nInstall FFmpeg:")
    print("  macOS: brew install ffmpeg")
    print("  Ubuntu: sudo apt install ffmpeg")
    exit(1)

# Test 3: Try to probe RTSP stream
print("\nTest 3: Probing RTSP stream...")
rtsp_url = f"rtsp://{host}:{rtsp_port}/stream"
print(f"URL: {rtsp_url}")

try:
    cmd = [
        'ffprobe',
        '-v', 'error',
        '-show_entries', 'stream=codec_type,codec_name,width,height',
        '-of', 'default=noprint_wrappers=1',
        rtsp_url
    ]
    
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=10)
    
    if result.returncode == 0:
        print("✅ RTSP stream is accessible!")
        print("\nStream info:")
        print(result.stdout)
    else:
        print("❌ RTSP stream not accessible")
        print(f"\nFFprobe output:\n{result.stderr}")
        print("\nPossible reasons:")
        print("  - Telescope not in Solar/Lunar viewing mode")
        print("  - RTSP streaming not started on telescope")
        print("  - Wrong RTSP port (try 554 or 4554)")
        
except subprocess.TimeoutExpired:
    print("❌ Timeout waiting for RTSP stream")
    print("\nTroubleshooting:")
    print("  - Telescope must be in viewing mode (not deep-sky)")
    print("  - Try clicking 'View Sun' or 'View Moon' in UI first")
    print("  - Stream may take 5-10 seconds to start")
    
except Exception as e:
    print(f"❌ Error: {e}")

print("\n" + "=" * 50)
print("Diagnostic complete!")
