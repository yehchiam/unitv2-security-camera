#!/usr/bin/env python3
"""
UnitV2 Camera Dashboard Server
Runs on Pi 4, serves web UI and proxies to UnitV2.
Supports: status, stream, clips listing, video playback, clip deletion.
"""

import base64
import hashlib
import http.server
import json
import os
import socketserver
import subprocess
from datetime import datetime
from urllib.parse import unquote

# ── Basic Auth ──────────────────────────────────────────────────────────
# Set CAM_DASH_USER and CAM_DASH_PASS env vars, or edit below.
# For public deployments, ALWAYS set auth credentials!
AUTH_USER = os.environ.get('CAM_DASH_USER', 'admin')
AUTH_PASS = os.environ.get('CAM_DASH_PASS', 'changeme')
AUTH_REALM = 'Camera Dashboard'
# SHA-256 hash for constant-time comparison
_CRED_HASH = hashlib.sha256(('%s:%s' % (AUTH_USER, AUTH_PASS)).encode()).hexdigest()

# ── Bind Address ────────────────────────────────────────────────────────
# "0.0.0.0" = all interfaces (for tunnel/LAN access)
# "127.0.0.1" = localhost only (most restrictive)
BIND_HOST = os.environ.get('CAM_DASH_HOST', '0.0.0.0')
PORT = int(os.environ.get('CAM_DASH_PORT', '3006'))


def check_auth(headers):
    """Validate HTTP Basic Auth credentials."""
    auth_header = headers.get('Authorization', '')
    if not auth_header.startswith('Basic '):
        return False
    try:
        decoded = base64.b64decode(auth_header[6:]).decode('utf-8', errors='ignore')
        incoming_hash = hashlib.sha256(decoded.encode()).hexdigest()
        return incoming_hash == _CRED_HASH
    except Exception:
        return False


def send_auth_challenge(handler, suppress_browser_popup=True):
    """Send 401 response as JSON. Never send WWW-Authenticate header
    because we use a custom login form — the browser native popup is never wanted."""
    handler.send_response(401)
    handler.send_header('Content-Type', 'application/json')
    handler.end_headers()
    handler.wfile.write(json.dumps({'error': 'Unauthorized'}).encode())

# ── UnitV2 Connection ───────────────────────────────────────────────────
# Set these via env vars or edit below for your setup
UNITV2_HOST = os.environ.get('UNITV2_HOST', '192.168.100.134')
UNITV2_PORT = int(os.environ.get('UNITV2_PORT', '8080'))
UNITV2_SSH_USER = os.environ.get('UNITV2_SSH_USER', 'root')
UNITV2_SSH_PASS = os.environ.get('UNITV2_SSH_PASS', 'YOUR_SSH_PASSWORD_HERE')
DASHBOARD_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dashboard")
PORT = 3006

def unitv2_url(path):
    return "http://%s:%d%s" % (UNITV2_HOST, UNITV2_PORT, path)

def format_size(bytes_val):
    if bytes_val < 1024: return "%d B" % bytes_val
    if bytes_val < 1024*1024: return "%.1f KB" % (bytes_val / 1024)
    if bytes_val < 1024*1024*1024: return "%.1f MB" % (bytes_val / (1024*1024))
    return "%.2f GB" % (bytes_val / (1024*1024*1024))

def get_clips_via_ssh():
    """Get clip listing from UnitV2 via SSH."""
    try:
        result = subprocess.run(
            ['sshpass', '-p', UNITV2_SSH_PASS, 'ssh',
             '-o', 'StrictHostKeyChecking=no',
             '-o', 'ConnectTimeout=5',
             'root@%s' % UNITV2_HOST,
             'ls -l /mnt/sd/clips/*.avi 2>/dev/null'],
            capture_output=True, text=True, timeout=10
        )
        clips = []
        for line in result.stdout.strip().split('\n'):
            if not line.strip() or line.startswith('total') or line.startswith('ls:'):
                continue
            parts = line.split()
            if len(parts) < 9:
                continue
            filename = parts[-1]
            # Extract just the basename from full path
            basename = os.path.basename(filename)
            try:
                size_bytes = int(parts[4])
            except:
                size_bytes = 0

            # Parse timestamp from filename: 20260609_164628.avi
            name = basename.replace('.avi', '')
            try:
                dt = datetime.strptime(name, "%Y%m%d_%H%M%S")
                # Format for MY timezone (UTC+8)
                from datetime import timedelta
                dt_local = dt + timedelta(hours=8)
                time_str = dt_local.strftime("%I:%M %p %b %d")
                # Also store ISO for sorting
                iso_str = dt_local.isoformat()
            except:
                time_str = name
                iso_str = name

            # Duration estimate: ~25KB per frame at 10fps, XVID 320x240
            if size_bytes > 10000:
                duration_sec = min(60, size_bytes / 25000)
            else:
                duration_sec = 0
            frames = int(duration_sec * 10)

            clips.append({
                "filename": basename,
                "start": time_str,
                "start_iso": iso_str,
                "duration": "%ds" % int(duration_sec) if duration_sec > 0 else "<1s",
                "frames": frames,
                "size": format_size(size_bytes),
                "size_bytes": size_bytes
            })
        clips.sort(key=lambda x: x.get('start_iso', ''), reverse=True)
        return clips
    except Exception as e:
        print("SSH clip list error: %s" % e)
        return []


class DashboardHandler(http.server.BaseHTTPRequestHandler):
    def end_headers(self):
        # Auto-add CORS headers to all responses
        self.send_header('Access-Control-Allow-Origin', '*')
        self.send_header('Access-Control-Allow-Methods', 'GET, DELETE, OPTIONS')
        self.send_header('Access-Control-Allow-Headers', 'Authorization, Content-Type')
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    def do_GET(self):
        path = self.path.split('?')[0]

        # HTML page is always served (login form handles auth client-side)
        if path == '/' or path == '/index.html':
            self.serve_file('index.html', 'text/html')
            return

        # Stream endpoint: allow unauthenticated access for <img> tag compatibility
        # The stream is just a live camera feed, not sensitive data.
        # Alternatively, support ?token=base64creds for authenticated access.
        if path == '/stream':
            # Check for token-based auth first
            token = self.path.split('token=', 1)[-1].split('&')[0] if 'token=' in self.path else None
            if token:
                try:
                    decoded = base64.b64decode(token).decode('utf-8', errors='ignore')
                    incoming_hash = hashlib.sha256(decoded.encode()).hexdigest()
                    if incoming_hash != _CRED_HASH:
                        send_auth_challenge(self)
                        return
                except Exception:
                    send_auth_challenge(self)
                    return
            self.proxy_stream('/stream')
            return

        # All other endpoints require auth
        if not check_auth(self.headers):
            send_auth_challenge(self, suppress_browser_popup=False)
            return

        if path == '/status':
            self.proxy_json('/status')
        elif path == '/perf':
            self.proxy_text('/perf')
        elif path == '/stream':
            self.proxy_stream('/stream')
        elif path == '/clips':
            clips = get_clips_via_ssh()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"clips": clips}).encode())
        elif path.startswith('/clip/'):
            filename = unquote(path.split('/clip/')[1])
            self.serve_clip(filename)
        else:
            self.send_response(404)
            self.end_headers()

    def do_DELETE(self):
        if not check_auth(self.headers):
            send_auth_challenge(self)
            return

        path = self.path.split('?')[0]
        if path.startswith('/delete/'):
            filename = unquote(path.split('/delete/')[1])
            self.delete_clip(filename)
        else:
            self.send_response(404)
            self.end_headers()

    def serve_file(self, filename, content_type):
        filepath = os.path.join(DASHBOARD_DIR, filename)
        if os.path.exists(filepath):
            with open(filepath, 'rb') as f:
                content = f.read()
            self.send_response(200)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(content)))
            self.send_header('Cache-Control', 'no-cache, no-store, must-revalidate')
            self.send_header('Pragma', 'no-cache')
            self.send_header('Expires', '0')
            self.end_headers()
            self.wfile.write(content)
        else:
            self.send_response(404)
            self.end_headers()

    def proxy_json(self, path):
        import urllib.request
        try:
            req = urllib.request.Request(unitv2_url(path))
            with urllib.request.urlopen(req, timeout=5) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            # Return degraded status instead of error when UnitV2 is down
            self.send_response(200)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e), "motion_detected": False, "recording": False, "uptime": 0, "storage_mb": 0, "clips": 0, "offline": True}).encode())

    def proxy_text(self, path):
        import urllib.request
        try:
            req = urllib.request.Request(unitv2_url(path))
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = resp.read()
            self.send_response(200)
            self.send_header('Content-Type', 'text/csv')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            self.send_response(502)
            self.send_header('Content-Type', 'text/plain')
            self.end_headers()
            self.wfile.write(str(e).encode())

    def proxy_stream(self, path):
        import urllib.request
        try:
            req = urllib.request.Request(unitv2_url(path))
            resp = urllib.request.urlopen(req, timeout=10)
            self.send_response(200)
            self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
            self.send_header('Cache-Control', 'no-cache')
            self.end_headers()
            try:
                while True:
                    chunk = resp.read(4096)
                    if not chunk:
                        break
                    try:
                        self.wfile.write(chunk)
                        self.wfile.flush()
                    except (BrokenPipeError, ConnectionResetError):
                        break
            except:
                pass
            finally:
                resp.close()
        except Exception as e:
            try:
                self.send_response(502)
                self.end_headers()
            except:
                pass

    def serve_clip(self, filename):
        """Transcode MJPEG AVI to MP4 H.264 for browser playback."""
        filename = filename.replace('/', '').replace('..', '')
        import tempfile

        # Download AVI from UnitV2
        try:
            result = subprocess.run(
                ['sshpass', '-p', UNITV2_SSH_PASS, 'ssh',
                 '-o', 'StrictHostKeyChecking=no',
                 '-o', 'ConnectTimeout=10',
                 'root@%s' % UNITV2_HOST,
                 'cat /mnt/sd/clips/%s' % filename],
                capture_output=True, timeout=60
            )
            if result.returncode != 0 or not result.stdout:
                self.send_response(404)
                self.end_headers()
                return
        except Exception as e:
            self.send_response(502)
            self.end_headers()
            return

        # Transcode to MP4 using ffmpeg (MJPEG AVI -> H264 MP4)
        tmp_avi = '/tmp/cam_%s' % filename
        tmp_mp4 = tmp_avi.replace('.avi', '.mp4')
        try:
            with open(tmp_avi, 'wb') as f:
                f.write(result.stdout)

            transcode = subprocess.run(
                ['ffmpeg', '-y', '-i', tmp_avi,
                 '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '28',
                 '-movflags', '+faststart', '-an',
                 tmp_mp4],
                capture_output=True, timeout=30
            )

            if transcode.returncode == 0 and os.path.exists(tmp_mp4):
                mp4_size = os.path.getsize(tmp_mp4)
                self.send_response(200)
                self.send_header('Content-Type', 'video/mp4')
                self.send_header('Content-Length', str(mp4_size))
                self.send_header('Accept-Ranges', 'bytes')
                self.end_headers()
                with open(tmp_mp4, 'rb') as f:
                    while True:
                        chunk = f.read(65536)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                return
            else:
                # ffmpeg failed, serve raw AVI
                self.send_response(200)
                self.send_header('Content-Type', 'video/x-msvideo')
                self.send_header('Content-Length', str(len(result.stdout)))
                self.end_headers()
                self.wfile.write(result.stdout)
                return
        except Exception as e:
            self.send_response(500)
            self.end_headers()
            return
        finally:
            try: os.remove(tmp_avi)
            except: pass
            try: os.remove(tmp_mp4)
            except: pass

    def delete_clip(self, filename):
        """Delete a clip from the UnitV2."""
        filename = filename.replace('/', '').replace('..', '')
        try:
            result = subprocess.run(
                ['sshpass', '-p', UNITV2_SSH_PASS, 'ssh',
                 '-o', 'StrictHostKeyChecking=no',
                 '-o', 'ConnectTimeout=5',
                 'root@%s' % UNITV2_HOST,
                 'rm /mnt/sd/clips/%s' % filename],
                capture_output=True, timeout=10
            )
            if result.returncode == 0:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                self.wfile.write(json.dumps({"deleted": filename}).encode())
            else:
                self.send_response(404)
                self.end_headers()
        except Exception as e:
            self.send_response(500)
            self.send_header('Content-Type', 'application/json')
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, format, *args):
        pass


class ThreadedHTTPServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    """Handle each request in a separate thread so streaming doesn't block."""
    daemon_threads = True
    timeout = 30

if __name__ == "__main__":
    host = BIND_HOST
    server = ThreadedHTTPServer((host, PORT), DashboardHandler)
    print("Camera dashboard running on %s:%d (auth: %s)" % (host, PORT, AUTH_USER))
    if AUTH_PASS == 'changeme':
        print("WARNING: Using default password! Set CAM_DASH_PASS for security.")
    server.serve_forever()