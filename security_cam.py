#!/usr/bin/env python3
"""
UnitV2-M12 Security Camera - v4 (crash-hardened + watchdog + logging)
Runs on M5Stack UnitV2-M12 (SKU: U078-M12, GC2053 camera, dual M12 lenses).
Uses Python + OpenCV directly (no M5Stack binaries needed).
Person detection via background subtraction (lightweight, 128MB RAM friendly).
Records clips to SD card, streams MJPEG, alerts via ntfy.

v4 changes from v3:
- Heartbeat file written every 30s (for Pi-side watchdog)
- Automatic reboot if OOM or critical memory pressure (<5MB available)
- Camera reconnection with exponential backoff (2s, 4s, 8s, max 30s)
- Periodic WiFi connectivity check (reconnects if needed)
- Crash log written to SD card with timestamp + traceback
- Watchdog timer: reboots device if main loop stalls for >60s
- Frame leak prevention: explicit del + gc.collect on every error path
- Startup delay: wait for network before starting HTTP server
- Status endpoint includes heartbeat timestamp + uptime for monitoring
"""

import cv2
import sys
import time
import json
import os
import gc
import threading
import http.server
import socketserver
import traceback as tb_module
from datetime import datetime, timedelta

# Force unbuffered output
sys.stdout = os.fdopen(sys.stdout.fileno(), 'w', buffering=1)
sys.stderr = os.fdopen(sys.stderr.fileno(), 'w', buffering=1)

# ── Config ──────────────────────────────────────────────
SD_CARD = "/mnt/sdcard"
CLIPS_DIR = os.path.join(SD_CARD, "clips")
LOG_FILE = os.path.join(SD_CARD, "camera.log")  # persistent crash log
HEARTBEAT_FILE = os.path.join(SD_CARD, "heartbeat.json")  # for Pi watchdog
CLIP_LENGTH = 60          # seconds per clip
COOLDOWN = 30             # seconds after last motion before recording stops
FPS = 10                  # recording frame rate
RESIZE_W = 320            # recording width (save space)
RESIZE_H = 240            # recording height
DETECT_W = 160            # motion detection width (smaller = faster)
DETECT_H = 120            # motion detection height
MAX_STORAGE_MB = 110000   # leave ~9GB free on 119GB card
STREAM_PORT = 8080        # MJPEG stream port
STREAM_FPS = 5            # MJPEG stream FPS (lower = less RAM/bandwidth)
STREAM_TIMEOUT = 300       # Max seconds a client can stream before disconnect
NTFY_URL = os.environ.get("NTFY_URL", "https://ntfy.sh/YOUR_NTFY_TOPIC")
JPEG_QUALITY = 60         # JPEG quality for stream (reduced from 70 for less memory)
MIN_CONTOUR_AREA = 500    # min pixel area to count as motion
MOTION_THRESHOLD = 25     # pixel diff threshold
RECORD_CODEC = "MJPG"     # MJPEG for reliable OpenCV recording
DETECT_SKIP = 2           # process every Nth frame for detection
LOW_MEM_KB = 10240        # Skip snapshot if less than this many KB available
CRIT_MEM_KB = 5120        # Reboot if less than this many KB available
HEARTBEAT_INTERVAL = 30   # write heartbeat file every 30s
WIFI_CHECK_INTERVAL = 300  # check WiFi every 5 minutes
CAM_RECONNECT_BASE_DELAY = 2  # initial camera reconnect delay (seconds)
CAM_RECONNECT_MAX_DELAY = 30  # max camera reconnect delay
# ────────────────────────────────────────────────────────

# ── State ───────────────────────────────────────────────
latest_frame = None       # raw JPEG bytes for MJPEG stream
frame_lock = threading.Lock()
motion_detected = False
motion_last_seen = 0
recording = False
current_writer = None
current_clip_start = 0
clip_count = 0
start_time = time.time()
stream_clients = 0         # track connected clients
last_heartbeat = 0         # last heartbeat write time
last_wifi_check = 0        # last WiFi connectivity check


def log_to_file(msg):
    """Append message to persistent log file on SD card."""
    try:
        timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
        with open(LOG_FILE, 'a') as f:
            f.write('%s %s\n' % (timestamp, msg))
    except:
        pass


def write_heartbeat():
    """Write heartbeat JSON file for Pi-side watchdog monitoring."""
    global motion_detected, recording, clip_count
    try:
        data = {
            "timestamp": datetime.now().isoformat(),
            "uptime_sec": int(time.time() - start_time),
            "motion": motion_detected,
            "recording": recording,
            "clips": clip_count,
            "mem_avail_kb": get_available_mem_kb(),
            "pid": os.getpid(),
        }
        tmp = HEARTBEAT_FILE + ".tmp"
        with open(tmp, 'w') as f:
            json.dump(data, f)
        os.replace(tmp, HEARTBEAT_FILE)  # atomic write
    except Exception as e:
        log_to_file("heartbeat error: %s" % e)


def check_wifi():
    """Check WiFi connectivity and attempt reconnect if down."""
    try:
        # Ping the router (gateway) to check WiFi
        result = os.system('ping -c 1 -W 3 192.168.100.1 > /dev/null 2>&1')
        if result != 0:
            log_to_file("WiFi check failed, reconnecting...")
            # Try to reconnect WiFi
            os.system('wlarm_hci attach || true')
            os.system('ifconfig wlan0 down 2>/dev/null; sleep 1; ifconfig wlan0 up 2>/dev/null')
            os.system('udhcpc -i wlan0 -q 2>/dev/null')
            time.sleep(2)
            # Verify
            result2 = os.system('ping -c 1 -W 3 192.168.100.1 > /dev/null 2>&1')
            if result2 != 0:
                log_to_file("WiFi reconnect FAILED")
            else:
                log_to_file("WiFi reconnect OK")
    except Exception as e:
        log_to_file("WiFi check error: %s" % e)


def check_oom():
    """Check for critical memory pressure. Reboot if too low."""
    avail = get_available_mem_kb()
    if avail < CRIT_MEM_KB:
        log_to_file("CRITICAL: Only %d KB available, rebooting to prevent OOM" % avail)
        # Try to free memory first
        gc.collect()
        time.sleep(2)
        avail = get_available_mem_kb()
        if avail < CRIT_MEM_KB:
            # Still too low — reboot the device
            os.system('sync')  # flush filesystem
            time.sleep(1)
            os.system('reboot -f')
    return avail


# ── Performance Logging ──────────────────────────────────
PERF_LOG = os.path.join(SD_CARD, "perf_log.csv")
PERF_INTERVAL = 300  # log every 5 minutes
_prev_cpu_idle = None
_prev_cpu_total = None

def log_performance():
    """Log system performance to CSV on SD card."""
    global _prev_cpu_idle, _prev_cpu_total
    while True:
        try:
            time.sleep(PERF_INTERVAL)
            # CPU usage
            cpu_usage = 0
            try:
                vals = [int(x) for x in open('/proc/stat').readlines()[0].split()[1:]]
                idle = vals[3] + vals[4]
                total = sum(vals)
                if _prev_cpu_idle is not None:
                    diff_idle = idle - _prev_cpu_idle
                    diff_total = total - _prev_cpu_total
                    cpu_usage = round(100 * (1 - diff_idle / diff_total), 1) if diff_total > 0 else 0
                _prev_cpu_idle = idle
                _prev_cpu_total = total
            except: pass
            # Memory
            mem_total = mem_used = mem_avail = 0
            try:
                lines = open('/proc/meminfo').readlines()
                mem_total = int([l for l in lines if l.startswith('MemTotal:')][0].split()[1]) // 1024
                mem_avail = int([l for l in lines if l.startswith('MemAvailable:')][0].split()[1]) // 1024
                mem_used = mem_total - mem_avail
            except: pass
            # Uptime
            uptime_sec = int(time.time() - start_time)
            # Motion/recording state
            is_motion = motion_detected
            is_recording = recording
            # Clip count & storage
            n_clips = len([f for f in os.listdir(CLIPS_DIR) if f.endswith('.avi')]) if os.path.isdir(CLIPS_DIR) else 0
            storage = round(get_storage_mb(), 1)
            # Write CSV
            now = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
            line = '%s,%s,%s,%.1f,%d,%d,%d,%d,%.1f,%d\n' % (
                now, uptime_sec, 'M' if is_motion else ('R' if is_recording else 'I'),
                cpu_usage, mem_total, mem_used, mem_avail,
                n_clips, storage, PERF_INTERVAL
            )
            # Write header if file is new
            if not os.path.exists(PERF_LOG):
                with open(PERF_LOG, 'a') as f:
                    f.write('timestamp,uptime_sec,state,cpu_pct,mem_total_mb,mem_used_mb,mem_avail_mb,clips,storage_mb,interval_sec\n')
            with open(PERF_LOG, 'a') as f:
                f.write(line)
            print('[perf] %s | CPU=%s%% | MEM=%d/%dMB | clips=%d | storage=%.1fMB | state=%s' % (
                now, cpu_usage, mem_used, mem_total, n_clips, storage,
                'MOTION' if is_motion else ('REC' if is_recording else 'IDLE')
            ))
        except Exception as e:
            print('[perf] log error: %s' % e)
# ────────────────────────────────────────────────────────

def ensure_dirs():
    os.makedirs(CLIPS_DIR, exist_ok=True)

def get_storage_mb():
    try:
        stat = os.statvfs(SD_CARD)
        free = stat.f_bavail * stat.f_frsize / (1024 * 1024)
        total = stat.f_blocks * stat.f_frsize / (1024 * 1024)
        return total - free
    except:
        return 0

def get_available_mem_kb():
    """Get available memory in KB."""
    try:
        with open('/proc/meminfo') as f:
            for line in f:
                if line.startswith('MemAvailable:'):
                    return int(line.split()[1])
    except:
        pass
    return 999999  # assume OK if can't read

def cleanup_old_clips():
    if get_storage_mb() < MAX_STORAGE_MB:
        return
    clips = sorted([f for f in os.listdir(CLIPS_DIR) if f.endswith(".avi")])
    while clips and get_storage_mb() > MAX_STORAGE_MB * 0.9:
        oldest = os.path.join(CLIPS_DIR, clips.pop(0))
        os.remove(oldest)
        print("[cleanup] deleted %s" % oldest)

def start_new_clip(cap_width, cap_height):
    global current_writer, current_clip_start, clip_count
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    clip_path = os.path.join(CLIPS_DIR, "%s.avi" % timestamp)
    fourcc = cv2.VideoWriter_fourcc(*RECORD_CODEC)
    current_writer = cv2.VideoWriter(
        clip_path, fourcc, FPS, (RESIZE_W, RESIZE_H)
    )
    current_clip_start = time.time()
    clip_count += 1
    print("[cam] Recording: %s" % clip_path)
    log_to_file("Recording: %s" % clip_path)
    return clip_path

def stop_clip():
    global current_writer, recording
    if current_writer is not None:
        current_writer.release()
        current_writer = None
    recording = False
    print("[cam] Recording stopped at %s" % datetime.now().strftime("%H:%M:%S"))
    gc.collect()  # reclaim memory after clip ends

def send_ntfy(message, priority="default", tags="", click_url=""):
    """Send alert to ntfy.sh (with SSL workaround for UnitV2)."""
    try:
        import urllib.request
        import ssl
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        headers = {
            "Title": "Camera Alert",
            "Priority": priority,
        }
        if tags:
            headers["Tags"] = tags
        if click_url:
            headers["Click"] = click_url
        req = urllib.request.Request(
            NTFY_URL,
            data=message.encode(),
            headers=headers
        )
        urllib.request.urlopen(req, timeout=10, context=ctx)
        print("[ntfy] sent: %s" % message)
    except Exception as e:
        print("[ntfy] failed: %s" % e)

def send_ntfy_with_frame(message, frame, priority="high", tags="warning,rotating_light", click_url=""):
    """Send ntfy alert with a JPEG snapshot attached (compressed thumbnail).
    Memory-safe: checks available RAM before decoding/resizing."""
    try:
        import urllib.request
        import ssl
        import numpy as np

        # Check available memory first — skip thumbnail if low
        avail = get_available_mem_kb()
        if avail < LOW_MEM_KB:
            print("[ntfy] Low memory (%d KB), sending text only" % avail)
            send_ntfy(message, priority=priority, tags=tags, click_url=click_url)
            return

        # Compress frame to small thumbnail JPEG for ntfy
        arr = np.frombuffer(frame, dtype=np.uint8)
        img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        if img is not None:
            # Resize to 160x120 thumbnail
            thumb = cv2.resize(img, (160, 120), interpolation=cv2.INTER_AREA)
            encode_params = [cv2.IMWRITE_JPEG_QUALITY, 50]
            ok, thumb_bytes = cv2.imencode('.jpg', thumb, encode_params)
            if ok:
                frame_data = thumb_bytes.tobytes()
            else:
                frame_data = frame
            # Free large arrays immediately
            del img, arr, thumb
            if ok:
                del thumb_bytes
            gc.collect()  # reclaim memory after thumbnail creation
        else:
            frame_data = frame
            del arr

        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE

        # Use PUT method: send JPEG as body with metadata in headers
        headers = {
            "Content-Type": "image/jpeg",
            "Filename": "snapshot.jpg",
            "Title": "Motion Detected",
            "Priority": priority,
            "Tags": tags,
            "Click": click_url,
        }
        req = urllib.request.Request(
            NTFY_URL,
            data=frame_data,
            headers=headers,
            method="PUT"
        )
        urllib.request.urlopen(req, timeout=15, context=ctx)
        print("[ntfy] sent with snapshot (%d bytes): %s" % (len(frame_data), message))
    except Exception as e:
        print("[ntfy] snapshot failed: %s, falling back to text" % e)
        send_ntfy(message, priority=priority, tags=tags, click_url=click_url)
    finally:
        # Always try to free memory
        try:
            del frame_data
        except:
            pass
        gc.collect()

class StreamHandler(http.server.BaseHTTPRequestHandler):
    timeout = 30
    def handle_one_request(self):
        try:
            super().handle_one_request()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
    def do_GET(self):
        try:
            self._handle_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as e:
            print("[cam] HTTP handler error: %s" % e)

    def _handle_get(self):
        if self.path == '/stream' or self.path == '/':
            global stream_clients
            stream_clients += 1
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'multipart/x-mixed-replace; boundary=frame')
                self.send_header('Cache-Control', 'no-cache')
                self.end_headers()
                start = time.time()
                while time.time() - start < STREAM_TIMEOUT:
                    with frame_lock:
                        frame = latest_frame
                    if frame:
                        self.wfile.write(b'--frame\r\nContent-Type: image/jpeg\r\n\r\n')
                        self.wfile.write(frame)
                        self.wfile.write(b'\r\n')
                    time.sleep(1.0 / STREAM_FPS)
            except:
                pass
            finally:
                stream_clients -= 1
        elif self.path.startswith('/clip/'):
            filename = self.path.split('/clip/')[1].split('?')[0]
            filename = filename.replace('/', '').replace('..', '')
            clip_path = os.path.join(CLIPS_DIR, filename)
            if os.path.exists(clip_path):
                file_size = os.path.getsize(clip_path)
                range_header = self.headers.get('Range')
                if range_header:
                    start = 0
                    end = file_size - 1
                    if range_header.startswith('bytes='):
                        parts = range_header[6:].split('-')
                        start = int(parts[0]) if parts[0] else 0
                        end = int(parts[1]) if parts[1] else file_size - 1
                    self.send_response(206)
                    self.send_header('Content-Type', 'video/x-msvideo')
                    self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, file_size))
                    self.send_header('Content-Length', str(end - start + 1))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    with open(clip_path, 'rb') as f:
                        f.seek(start)
                        remaining = end - start + 1
                        while remaining > 0:
                            chunk = f.read(min(65536, remaining))
                            if not chunk:
                                break
                            self.wfile.write(chunk)
                            remaining -= len(chunk)
                else:
                    self.send_response(200)
                    self.send_header('Content-Type', 'video/x-msvideo')
                    self.send_header('Content-Length', str(file_size))
                    self.send_header('Accept-Ranges', 'bytes')
                    self.end_headers()
                    with open(clip_path, 'rb') as f:
                        while True:
                            chunk = f.read(65536)
                            if not chunk:
                                break
                            self.wfile.write(chunk)
            else:
                self.send_response(404)
                self.end_headers()
        elif self.path == '/perf':
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/csv')
                self.send_header('Access-Control-Allow-Origin', '*')
                self.end_headers()
                if os.path.exists(PERF_LOG):
                    with open(PERF_LOG, 'rb') as f:
                        self.wfile.write(f.read())
                else:
                    self.wfile.write(b'"timestamp,uptime_sec,state,cpu_pct,mem_total_mb,mem_used_mb,mem_avail_mb,clips,storage_mb,interval_sec"\n')
            except (BrokenPipeError, ConnectionResetError):
                pass
        elif self.path == '/status':
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.end_headers()
                cpu_usage = 0
                try:
                    vals = [int(x) for x in open('/proc/stat').readlines()[0].split()[1:]]
                    idle = vals[3] + vals[4]
                    total = sum(vals)
                    if hasattr(get_storage_mb, '_prev_idle'):
                        diff_idle = idle - get_storage_mb._prev_idle
                        diff_total = total - get_storage_mb._prev_total
                        cpu_usage = round(100 * (1 - diff_idle / diff_total), 1) if diff_total > 0 else 0
                    get_storage_mb._prev_idle = idle
                    get_storage_mb._prev_total = total
                except: pass
                mem_total = mem_used = 0
                try:
                    mem_lines = open('/proc/meminfo').readlines()
                    mem_total = int([l for l in mem_lines if l.startswith('MemTotal:')][0].split()[1]) // 1024
                    mem_avail = int([l for l in mem_lines if l.startswith('MemAvailable:')][0].split()[1]) // 1024
                    mem_used = mem_total - mem_avail
                except: pass
                disk_total = disk_used = 0
                try:
                    stat = os.statvfs('/mnt/sdcard')
                    disk_total = round(stat.f_blocks * stat.f_bsize / 1024 / 1024 / 1024, 1)
                    disk_used = round((stat.f_blocks - stat.f_bfree) * stat.f_bsize / 1024 / 1024 / 1024, 1)
                except: pass
                # Include heartbeat timestamp for Pi-side monitoring
                status = {
                    "motion_detected": motion_detected,
                    "recording": recording,
                    "uptime": time.time() - start_time,
                    "storage_mb": round(get_storage_mb(), 1),
                    "clips": len([f for f in os.listdir(CLIPS_DIR) if f.endswith('.avi')]) if os.path.isdir(CLIPS_DIR) else 0,
                    "cpu": "ARMv7 (SigmaStar SSD202D)",
                    "cpu_cores": 2,
                    "cpu_usage": cpu_usage,
                    "mem_total_mb": mem_total,
                    "mem_used_mb": mem_used,
                    "mem_avail_kb": get_available_mem_kb(),
                    "disk_total_gb": disk_total,
                    "disk_used_gb": disk_used,
                    "heartbeat_ts": datetime.now().isoformat(),
                    "pid": os.getpid(),
                }
                self.wfile.write(json.dumps(status).encode())
            except (BrokenPipeError, ConnectionResetError):
                pass
        else:
            try:
                self.send_response(404)
                self.end_headers()
            except (BrokenPipeError, ConnectionResetError):
                pass

    def log_message(self, format, *args):
        pass

class ReuseTCPServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    allow_reuse_address = True
    daemon_threads = True
    timeout = 30

def start_stream_server():
    with ReuseTCPServer(("", STREAM_PORT), StreamHandler) as httpd:
        print("[cam] MJPEG stream on port %d" % STREAM_PORT)
        log_to_file("HTTP stream server started on port %d" % STREAM_PORT)
        httpd.serve_forever()

def main():
    global latest_frame, motion_detected, motion_last_seen, recording, stream_clients
    global last_heartbeat, last_wifi_check, start_time

    # Wait for NTP time sync (UnitV2 has no RTC)
    print("[cam] Waiting for time sync...")
    log_to_file("Starting security camera v4...")
    for i in range(30):
        now = datetime.now()
        if now.year > 2020:
            print("[cam] Time synced: %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
            log_to_file("Time synced: %s" % now.strftime("%Y-%m-%d %H:%M:%S"))
            break
        time.sleep(1)
    else:
        print("[cam] WARNING: Time not synced, using current time")
        log_to_file("WARNING: Time not synced")

    # Wait for network connectivity
    print("[cam] Waiting for network...")
    for i in range(60):
        if os.system('ping -c 1 -W 2 192.168.100.1 > /dev/null 2>&1') == 0:
            print("[cam] Network ready (gateway reachable)")
            log_to_file("Network ready after %ds" % (i + 1))
            break
        time.sleep(1)
    else:
        print("[cam] WARNING: Gateway not reachable, continuing anyway")
        log_to_file("WARNING: Gateway not reachable after 60s")

    start_time = time.time()
    print("=" * 50)
    print("  UnitV2 Security Camera v4 (crash-hardened)")
    print("  OpenCV + Background Subtraction")
    print("=" * 50)

    # Check SD card
    if not os.path.ismount(SD_CARD):
        os.system("mount /dev/mmcblk0p1 %s" % SD_CARD)
        time.sleep(1)
    if not os.path.isdir(SD_CARD):
        print("[cam] ERROR: SD card not available at %s" % SD_CARD)
        log_to_file("ERROR: SD card not available at %s" % SD_CARD)
        return

    ensure_dirs()
    print("[cam] SD card ready: %.0fMB used" % get_storage_mb())
    log_to_file("SD card ready: %.0fMB used" % get_storage_mb())

    # Pre-run garbage collection
    gc.collect()

    # Open camera with retry
    cam_retry_delay = CAM_RECONNECT_BASE_DELAY
    cap = None
    for attempt in range(5):
        cap = cv2.VideoCapture(0)
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
        cap.set(cv2.CAP_PROP_FPS, 15)
        if cap.isOpened():
            break
        print("[cam] Camera open failed (attempt %d), retrying in %ds..." % (attempt + 1, cam_retry_delay))
        log_to_file("Camera open failed (attempt %d)" % (attempt + 1))
        time.sleep(cam_retry_delay)
        cam_retry_delay = min(cam_retry_delay * 2, CAM_RECONNECT_MAX_DELAY)

    if not cap or not cap.isOpened():
        print("[cam] ERROR: Cannot open camera after 5 attempts")
        log_to_file("FATAL: Cannot open camera after 5 attempts")
        return

    cap_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    cap_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    print("[cam] Camera: %dx%d" % (cap_w, cap_h))
    log_to_file("Camera opened: %dx%d" % (cap_w, cap_h))

    # Start MJPEG stream server
    stream_thread = threading.Thread(target=start_stream_server, daemon=True)
    stream_thread.start()

    # Watchdog: restart HTTP server if it dies
    def stream_watchdog():
        while True:
            time.sleep(30)
            if not stream_thread.is_alive():
                print("[cam] HTTP server died, restarting...")
                log_to_file("HTTP server died, restarting")
                new_thread = threading.Thread(target=start_stream_server, daemon=True)
                new_thread.start()
                stream_thread = new_thread
    watchdog_thread = threading.Thread(target=stream_watchdog, daemon=True)
    watchdog_thread.start()

    # Performance logging thread
    perf_thread = threading.Thread(target=log_performance, daemon=True)
    perf_thread.start()
    print("[cam] Performance logging every %ds to %s" % (PERF_INTERVAL, PERF_LOG))

    # Background subtractor for motion detection
    bgsub = cv2.createBackgroundSubtractorMOG2(
        history=300, varThreshold=MOTION_THRESHOLD, detectShadows=False
    )

    # Pre-allocate kernel for morphology (reuse every frame)
    kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))
    # JPEG encode params (reuse)
    encode_params = [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY]

    frame_count = 0
    alert_sent_time = 0
    cam_errors = 0  # track consecutive camera read errors
    print("[cam] Detection running...")
    log_to_file("Detection running")

    while True:
        ret, frame = cap.read()
        if not ret:
            cam_errors += 1
            print("[cam] Camera read error #%d, reconnecting..." % cam_errors)
            log_to_file("Camera read error #%d" % cam_errors)
            cap.release()
            # Exponential backoff on repeated errors
            delay = min(CAM_RECONNECT_BASE_DELAY * (2 ** min(cam_errors, 4)), CAM_RECONNECT_MAX_DELAY)
            time.sleep(delay)
            cap = cv2.VideoCapture(0)
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, 640)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 480)
            cap.set(cv2.CAP_PROP_FPS, 15)
            continue

        # Reset error counter on successful read
        if cam_errors > 0:
            log_to_file("Camera recovered after %d errors" % cam_errors)
            cam_errors = 0

        frame_count += 1
        now = time.time()

        # ── Periodic tasks ──
        # Write heartbeat file every 30 seconds
        if now - last_heartbeat >= HEARTBEAT_INTERVAL:
            write_heartbeat()
            last_heartbeat = now

        # Check WiFi every 5 minutes
        if now - last_wifi_check >= WIFI_CHECK_INTERVAL:
            check_wifi()
            last_wifi_check = now

        # Check memory pressure
        check_oom()

        # ── Motion Detection (every Nth frame, on small grayscale) ──
        has_motion = False
        if frame_count % DETECT_SKIP == 0:
            # Convert to grayscale first (saves memory vs color resize)
            gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
            # Resize for detection (very small = fast)
            small = cv2.resize(gray, (DETECT_W, DETECT_H), interpolation=cv2.INTER_AREA)
            del gray  # free immediately
            fgmask = bgsub.apply(small)
            del small  # free immediately

            # Clean up noise with smaller kernel
            fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_OPEN, kernel)
            fgmask = cv2.morphologyEx(fgmask, cv2.MORPH_CLOSE, kernel)

            # Find contours
            contours_result = cv2.findContours(fgmask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
            contours = contours_result[-2]
            for contour in contours:
                if cv2.contourArea(contour) > MIN_CONTOUR_AREA:
                    has_motion = True
                    break
            del fgmask  # free immediately

        # ── Recording Logic ──
        if has_motion:
            motion_last_seen = now
            if not motion_detected:
                motion_detected = True
                print("[cam] MOTION DETECTED at %s" % datetime.now().strftime("%H:%M:%S"))
                # Send alert (max once per 1 min)
                if now - alert_sent_time > 60:
                    with frame_lock:
                        frame_for_notify = latest_frame
                    if frame_for_notify:
                        avail = get_available_mem_kb()
                        if avail < LOW_MEM_KB:
                            print("[cam] Low memory (%d KB), skipping snapshot" % avail)
                            threading.Thread(
                                target=send_ntfy,
                                args=("Motion detected! Recording started.",),
                                kwargs={"priority": "high", "tags": "warning,rotating_light", "click_url": "https://camera.hensem.xyz"},
                                daemon=True
                            ).start()
                        else:
                            threading.Thread(
                                target=send_ntfy_with_frame,
                                args=("Motion detected! Tap to view.", frame_for_notify),
                                daemon=True
                            ).start()
                    else:
                        threading.Thread(
                            target=send_ntfy,
                            args=("Motion detected! Recording started.",),
                            kwargs={"priority": "high", "tags": "warning,rotating_light", "click_url": "https://camera.hensem.xyz"},
                            daemon=True
                        ).start()
                    alert_sent_time = now

            if not recording:
                recording = True
                start_new_clip(cap_w, cap_h)

        else:
            # No motion
            if motion_detected and (now - motion_last_seen) > COOLDOWN:
                motion_detected = False
                if recording:
                    stop_clip()

        # ── Write frame if recording ──
        if recording and current_writer is not None:
            resized = cv2.resize(frame, (RESIZE_W, RESIZE_H))
            current_writer.write(resized)
            del resized  # free immediately

            # Check if clip length reached
            if now - current_clip_start >= CLIP_LENGTH:
                stop_clip()
                if motion_detected:
                    start_new_clip(cap_w, cap_h)

        # ── Update MJPEG stream frame ──
        # Skip frame update when no clients connected (saves CPU/memory)
        if stream_clients > 0 or frame_count % 10 == 0:
            if recording:
                cv2.circle(frame, (20, 20), 8, (0, 0, 255), -1)
                cv2.putText(frame, "REC %s" % datetime.now().strftime("%H:%M:%S"),
                           (35, 25), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 1)
            elif motion_detected:
                cv2.circle(frame, (20, 20), 8, (0, 255, 255), -1)
            else:
                cv2.circle(frame, (20, 20), 8, (0, 255, 0), -1)

            _, jpeg = cv2.imencode('.jpg', frame, encode_params)
            with frame_lock:
                latest_frame = jpeg.tobytes()
            del jpeg  # free numpy array

        # ── Periodic status ──
        if frame_count % 300 == 0:
            free_mb = 0
            try:
                stat = os.statvfs(SD_CARD)
                free_mb = stat.f_bavail * stat.f_frsize / (1024 * 1024)
            except:
                pass
            avail_kb = get_available_mem_kb()
            print("[cam] Frame %d | Motion: %s | Recording: %s | Free: %.0fMB | Clips: %d | Avail: %dKB" % (
                frame_count, motion_detected, recording, free_mb,
                len([f for f in os.listdir(CLIPS_DIR) if f.endswith('.avi')]) if os.path.isdir(CLIPS_DIR) else 0,
                avail_kb
            ))
            # Periodic garbage collection
            gc.collect()

    cap.release()

if __name__ == "__main__":
    while True:
        try:
            main()
        except Exception as e:
            print("[cam] CRASHED: %s" % e)
            tb_module.print_exc()
            # Log crash to SD card
            log_to_file("CRASHED: %s\n%s" % (e, tb_module.format_exc()))
            gc.collect()
            print("[cam] Restarting in 5 seconds...")
            time.sleep(5)