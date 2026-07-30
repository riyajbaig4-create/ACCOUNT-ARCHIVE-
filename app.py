# -*- coding: utf-8 -*-
import threading
import subprocess
import os
import zipfile
import shutil
import json
import uuid
import time
import signal
import tempfile
import re
import requests
import select
import pty
import queue
import sqlite3
import sys
import resource
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, session, send_file, send_from_directory, redirect, Response, stream_with_context
from functools import wraps
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

app = Flask(__name__, static_folder='public', static_url_path='')
app.secret_key = 'yuvicodex_super_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# ---------- CONFIG ----------
PASSWORD = "your_secure_password"
MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', 'master123')
SECRET_KEY = os.environ.get('SECRET_KEY', 'secret123')
RENDER_API_KEY = os.environ.get('RENDER_API_KEY', 'rnd_27v7iMggh7mafESEqJq1Lf12wIkF')

UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BASE_DIR = os.path.abspath('.')

# ---------- STORAGE LIMITS ----------
MAX_STORAGE_GB = 10  # Max 10GB total storage
MAX_FILE_SIZE_MB = 50  # Max 50MB per file
MAX_BOT_STORAGE_MB = 200  # Max 200MB per bot project
MAX_WEBSITE_STORAGE_MB = 500  # Max 500MB per website

# ---------- SETTINGS ----------
SETTINGS_FILE = 'settings.json'
STATIC_LOGO_FOLDER = os.path.join('static', 'logos')
os.makedirs(STATIC_LOGO_FOLDER, exist_ok=True)

def load_settings():
    default = {
        "website_name": "YUVICODEX",
        "logo": None,
        "social_links": {
            "telegram": "#",
            "youtube": "#",
            "instagram": "#",
            "tiktok": "#"
        }
    }
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            data = json.load(f)
            for key, val in default.items():
                if key not in data:
                    data[key] = val
            if "social_links" not in data:
                data["social_links"] = default["social_links"]
            else:
                for sk, sv in default["social_links"].items():
                    if sk not in data["social_links"]:
                        data["social_links"][sk] = sv
            return data
    save_settings(default)
    return default

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

settings_db = load_settings()

# ---------- STORAGE CHECK FUNCTIONS ----------
def get_total_storage_used():
    """Calculate total storage used across all uploads"""
    total = 0
    if os.path.exists(UPLOAD_FOLDER):
        for root, dirs, files in os.walk(UPLOAD_FOLDER):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
    return total / (1024**3)  # GB

def get_folder_size_mb(folder):
    """Get folder size in MB"""
    total = 0
    if os.path.exists(folder):
        for root, dirs, files in os.walk(folder):
            for f in files:
                fp = os.path.join(root, f)
                if os.path.exists(fp):
                    total += os.path.getsize(fp)
    return total / (1024**2)

def check_storage_limit(additional_size_mb=0):
    """Check if adding more storage would exceed limit"""
    current_gb = get_total_storage_used()
    additional_gb = additional_size_mb / 1024
    return (current_gb + additional_gb) <= MAX_STORAGE_GB

def cleanup_old_logs():
    """Auto cleanup old logs to save storage"""
    log_folder = os.path.join(BASE_DIR, 'logs')
    if os.path.exists(log_folder):
        now = time.time()
        for root, dirs, files in os.walk(log_folder):
            for f in files:
                fp = os.path.join(root, f)
                try:
                    if os.path.getmtime(fp) < now - (7 * 86400):
                        os.remove(fp)
                except:
                    pass

def cleanup_temp_files():
    """Cleanup temporary files"""
    temp_dirs = ['/tmp', '/dev/shm']
    for temp_dir in temp_dirs:
        if os.path.exists(temp_dir):
            try:
                now = time.time()
                for f in os.listdir(temp_dir):
                    fp = os.path.join(temp_dir, f)
                    if os.path.isfile(fp) and f.startswith('tmp'):
                        if os.path.getmtime(fp) < now - (3600):
                            try: os.remove(fp)
                            except: pass
            except:
                pass

def auto_cleanup_task():
    """Background task to cleanup storage"""
    while True:
        try:
            cleanup_old_logs()
            cleanup_temp_files()
            
            # Check total storage
            total_gb = get_total_storage_used()
            if total_gb > MAX_STORAGE_GB * 0.9:  # 90% full
                # Delete oldest logs first
                log_folder = os.path.join(BASE_DIR, 'logs')
                if os.path.exists(log_folder):
                    files = []
                    for root, dirs, dir_files in os.walk(log_folder):
                        for f in dir_files:
                            fp = os.path.join(root, f)
                            files.append((fp, os.path.getmtime(fp)))
                    files.sort(key=lambda x: x[1])
                    # Delete oldest 20% logs
                    to_delete = int(len(files) * 0.2)
                    for fp, _ in files[:to_delete]:
                        try: os.remove(fp)
                        except: pass
        except:
            pass
        time.sleep(3600)  # Run every hour

# Start auto cleanup thread
cleanup_thread = threading.Thread(target=auto_cleanup_task, daemon=True)
cleanup_thread.start()

# ---------- BOT MANAGEMENT ----------
BOTS_FILE = os.path.join(UPLOAD_FOLDER, 'bots.json')
bots_db = {}

def load_bots():
    global bots_db
    if os.path.exists(BOTS_FILE):
        with open(BOTS_FILE, 'r') as f:
            bots_db = json.load(f)
    else:
        bots_db = {}

def save_bots():
    with open(BOTS_FILE, 'w') as f:
        json.dump(bots_db, f, indent=2)

load_bots()

# ---------- PROCESS TRACKING ----------
processes = {}

# ---------- SQLITE FOR WEBSITES ----------
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')

def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_username TEXT NOT NULL,
            website_name TEXT,
            website_slug TEXT UNIQUE NOT NULL,
            website_folder TEXT NOT NULL,
            startup_file TEXT,
            runtime TEXT DEFAULT 'python',
            status TEXT DEFAULT 'uploaded',
            allocated_port INTEGER UNIQUE,
            pid INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            last_started TIMESTAMP,
            last_stopped TIMESTAMP,
            storage_used INTEGER DEFAULT 0,
            website_size INTEGER DEFAULT 0,
            repo_url TEXT,
            branch TEXT DEFAULT 'main',
            deployment_type TEXT DEFAULT 'zip',
            total_runtime_seconds INTEGER DEFAULT 0,
            last_start_time TIMESTAMP,
            type TEXT DEFAULT 'website',
            bot_interpreter TEXT
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            repo_url TEXT,
            branch TEXT,
            status TEXT DEFAULT 'queued',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            duration INTEGER,
            commit_hash TEXT,
            log_file TEXT
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_website ON logs(website_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_deployments_website ON deployments(website_id)')
        
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                     ('total_hours_offset', '0'))
        conn.commit()
init_db()

# ---------- CONFIG HELPERS ----------
def get_config(key, default='0'):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

def set_config(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

# ---------- CONTAINER MEMORY ----------
def get_container_memory():
    try:
        if os.path.exists('/sys/fs/cgroup/memory.max'):
            with open('/sys/fs/cgroup/memory.max', 'r') as f:
                total_bytes = int(f.read().strip())
            with open('/sys/fs/cgroup/memory.current', 'r') as f:
                used_bytes = int(f.read().strip())
        elif os.path.exists('/sys/fs/cgroup/memory/memory.limit_in_bytes'):
            with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
                total_bytes = int(f.read().strip())
            with open('/sys/fs/cgroup/memory/memory.usage_in_bytes', 'r') as f:
                used_bytes = int(f.read().strip())
        else:
            mem = psutil.virtual_memory()
            return round(mem.used / 1024**2, 2), round(mem.total / 1024**2, 2), mem.percent
    except Exception:
        mem = psutil.virtual_memory()
        return round(mem.used / 1024**2, 2), round(mem.total / 1024**2, 2), mem.percent

    total_mb = total_bytes / (1024**2)
    used_mb = used_bytes / (1024**2)
    if total_mb > 10000:
        total_mb = 512.0
        if used_mb > total_mb:
            used_mb = total_mb
    percent = (used_mb / total_mb) * 100 if total_mb > 0 else 0
    return round(used_mb, 2), round(total_mb, 2), round(min(percent, 100), 2)

# ---------- RENDER API HELPERS ----------
RENDER_API_BASE = "https://api.render.com/v1"

def render_api_call(endpoint, params=None):
    if not RENDER_API_KEY:
        return None, "RENDER_API_KEY not set"
    headers = {"Authorization": f"Bearer {RENDER_API_KEY}", "Accept": "application/json"}
    url = f"{RENDER_API_BASE}/{endpoint.lstrip('/')}"
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=30)
        if resp.status_code == 200:
            return resp.json(), None
        else:
            return None, f"API error {resp.status_code}"
    except Exception as e:
        return None, str(e)

def get_all_services():
    services = []
    page = 1
    limit = 50
    while True:
        data, err = render_api_call("services", {"limit": limit, "page": page})
        if err or not data:
            break
        services.extend(data)
        if len(data) < limit:
            break
        page += 1
    return services

# ---------- RUNTIME DETECTION ----------
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py', 'wsgi.py', 'asgi.py']

def find_startup_file(folder):
    for filename in STARTUP_PRIORITY:
        if os.path.exists(os.path.join(folder, filename)):
            return filename
    return None

def detect_runtime_and_get_cmd(folder, port):
    # Node.js
    if os.path.exists(os.path.join(folder, 'package.json')):
        try:
            with open(os.path.join(folder, 'package.json'), 'r') as f:
                data = json.load(f)
                scripts = data.get('scripts', {})
                if 'start' in scripts:
                    return ['npm', 'start'], 'nodejs', {'NODE_ENV': 'production', 'PORT': str(port)}
        except:
            pass
    js_files = ['server.js', 'index.js', 'app.js', 'main.js']
    for fname in js_files:
        if os.path.exists(os.path.join(folder, fname)):
            return ['node', fname], 'nodejs', {'PORT': str(port)}
    try:
        for f in os.listdir(folder):
            if f.endswith('.js') and os.path.isfile(os.path.join(folder, f)):
                return ['node', f], 'nodejs', {'PORT': str(port)}
    except:
        pass
    
    # PHP
    if os.path.exists(os.path.join(folder, 'index.php')):
        return ['php', '-S', f'0.0.0.0:{port}'], 'php', {}
    
    # Go
    if os.path.exists(os.path.join(folder, 'go.mod')):
        return ['go', 'run', 'main.go'], 'go', {}
    
    # Java
    if os.path.exists(os.path.join(folder, 'pom.xml')):
        return ['mvn', 'spring-boot:run'], 'java', {}
    if os.path.exists(os.path.join(folder, 'build.gradle')):
        return ['./gradlew', 'bootRun'], 'java', {}
    jars = [f for f in os.listdir(folder) if f.endswith('.jar')]
    if jars:
        return ['java', '-jar', jars[0]], 'java', {}
    
    # Python
    flask_files = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
    for f in flask_files:
        path = os.path.join(folder, f)
        if os.path.exists(path):
            try:
                with open(path, 'r') as fh:
                    content = fh.read()
                    if 'Flask' in content or 'app.run' in content:
                        return [sys.executable, '-m', 'flask', 'run', '--host=0.0.0.0', '--port='+str(port)], 'flask', {}
            except:
                pass
    if os.path.exists(os.path.join(folder, 'manage.py')):
        return [sys.executable, 'manage.py', 'runserver', f'0.0.0.0:{port}'], 'django', {}
    if os.path.exists(os.path.join(folder, 'asgi.py')):
        return ['uvicorn', 'asgi:application', '--host', '0.0.0.0', '--port', str(port)], 'fastapi', {}
    startup = find_startup_file(folder)
    if startup:
        return [sys.executable, startup], 'python', {}
    
    if os.path.exists(os.path.join(folder, 'index.html')):
        return [sys.executable, '-m', 'http.server', str(port)], 'static', {}
    
    return None, None, {}

# ---------- INSTALL DEPENDENCIES ----------
def install_dependencies(folder, runtime, log_callback=None):
    if runtime == 'nodejs':
        if os.path.exists(os.path.join(folder, 'package.json')):
            cmd = ['npm', 'install', '--no-audit', '--no-fund']
            if log_callback: log_callback("BUILD", "Running npm install")
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if line.strip() and log_callback: log_callback("BUILD", line.strip())
            proc.wait()
            if proc.returncode != 0: return False, "npm install failed"
            try:
                with open(os.path.join(folder, 'package.json'), 'r') as f:
                    data = json.load(f)
                    if 'build' in data.get('scripts', {}):
                        cmd = ['npm', 'run', 'build']
                        if log_callback: log_callback("BUILD", "Running npm run build")
                        proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
                        for line in iter(proc.stdout.readline, ''):
                            if line.strip() and log_callback: log_callback("BUILD", line.strip())
                        proc.wait()
                        if proc.returncode != 0: return False, "npm run build failed"
            except: pass
            return True, "Dependencies installed"
        return True, "No deps"
    elif runtime == 'php':
        if os.path.exists(os.path.join(folder, 'composer.json')):
            cmd = ['composer', 'install', '--no-dev', '--no-interaction']
            if log_callback: log_callback("BUILD", "Running composer install")
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if line.strip() and log_callback: log_callback("BUILD", line.strip())
            proc.wait()
            if proc.returncode != 0: return False, "composer install failed"
            return True, "Deps installed"
        return True, "No deps"
    else:
        req_file = os.path.join(folder, 'requirements.txt')
        if os.path.exists(req_file):
            cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt', '--no-cache-dir']
            if log_callback: log_callback("BUILD", f"Running: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if line.strip() and log_callback: log_callback("BUILD", line.strip())
            proc.wait()
            if proc.returncode != 0: return False, "pip install failed"
            return True, "Requirements installed"
        return True, "No deps"
    return True, "Unknown runtime"

# ---------- START WEBSITE PROCESS ----------
def start_website_process(website_id, log_callback=None):
    website = get_website_by_id(website_id)
    if not website: return False, "Website not found"
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        log_website(website_id, "Folder missing", 'error')
        update_website_status(website_id, 'failed')
        return False, "Folder not found"
    
    allocated_port = get_next_available_port()
    cmd, runtime, env_extra = detect_runtime_and_get_cmd(folder, allocated_port)
    if not cmd:
        log_website(website_id, "No startup file detected", 'error')
        update_website_status(website_id, 'failed')
        return False, "No startup file detected"
    
    with get_db() as conn:
        conn.execute('UPDATE websites SET runtime = ? WHERE id = ?', (runtime, website_id))
        conn.commit()
    
    if log_callback: log_callback("BUILD", f"Installing dependencies for {runtime}...")
    success, msg = install_dependencies(folder, runtime, log_callback)
    if not success:
        log_website(website_id, f"Dependency install failed: {msg}", 'error')
        update_website_status(website_id, 'failed')
        return False, msg
    
    env = os.environ.copy()
    env['PORT'] = str(allocated_port)
    env['PYTHONUNBUFFERED'] = '1'
    env.update(env_extra)
    if runtime == 'flask':
        if os.path.exists(os.path.join(folder, 'app.py')): env['FLASK_APP'] = 'app.py'
        elif os.path.exists(os.path.join(folder, 'main.py')): env['FLASK_APP'] = 'main.py'
    if runtime == 'php':
        cmd = ['php', '-S', f'0.0.0.0:{allocated_port}']
    
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    if log_callback: log_callback("STARTUP", f"Starting: {' '.join(cmd)} on port {allocated_port}")
    
    try:
        f_log = open(log_file, 'a')
        if os.name == 'nt':
            proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(cmd, cwd=folder, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, preexec_fn=os.setsid)
            try:
                os.nice(-10)
            except:
                pass
        
        def read_output():
            for line in iter(proc.stdout.readline, b''):
                if line:
                    decoded = line.decode('utf-8', errors='replace')
                    f_log.write(decoded); f_log.flush()
                    if log_callback: log_callback("PROCESS", decoded.strip())
            f_log.close()
        thread = threading.Thread(target=read_output); thread.daemon = True; thread.start()
        
        with get_db() as conn:
            conn.execute('UPDATE websites SET last_start_time = CURRENT_TIMESTAMP, status = ? WHERE id = ?', ('starting', website_id))
            conn.commit()
        
        time.sleep(3)
        if proc.poll() is not None:
            with open(log_file, 'r') as f:
                error_lines = f.read()[-500:]
            update_website_status(website_id, 'failed')
            log_website(website_id, f"Process crashed: {error_lines}", 'error')
            return False, f"Process crashed: {error_lines}"
        
        healthy = False
        actual_port = None
        for attempt in range(10):
            try:
                response = requests.get(f"http://localhost:{allocated_port}", timeout=3)
                if response.status_code < 500:
                    healthy = True
                    actual_port = allocated_port
                    break
            except:
                pass
            time.sleep(2)
        
        if healthy:
            update_website_status(website_id, 'running', proc.pid, actual_port)
            log_website(website_id, f"Started on port {actual_port} (PID {proc.pid})")
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ?, last_started = CURRENT_TIMESTAMP WHERE id = ?', (cmd[0] if not runtime.startswith('python') else 'app', website_id))
                conn.commit()
            if log_callback: log_callback("SUCCESS", f"Running on port {actual_port}")
            return True, f"Running on port {actual_port}"
        else:
            try:
                if os.name == 'nt': subprocess.run(['taskkill', '/PID', str(proc.pid), '/F'], capture_output=True)
                else: os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except: pass
            update_website_status(website_id, 'crashed')
            return False, "Health check failed"
    except Exception as e:
        log_website(website_id, f"Start error: {str(e)}", 'error')
        update_website_status(website_id, 'failed')
        return False, str(e)

def stop_website_process(website_id):
    website = get_website_by_id(website_id)
    if not website: return False, "Not found"
    pid = website['pid']
    if not pid: return False, "No running process"
    
    last_start = website['last_start_time']
    if last_start:
        try:
            start_dt = datetime.fromisoformat(last_start.replace(' ', 'T'))
            elapsed = int((datetime.now() - start_dt).total_seconds())
            with get_db() as conn:
                conn.execute('UPDATE websites SET total_runtime_seconds = total_runtime_seconds + ? WHERE id = ?', (elapsed, website_id))
                conn.commit()
        except: pass
    
    try:
        if os.name == 'nt': subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except: pass
    update_website_status(website_id, 'stopped', None, None)
    with get_db() as conn:
        conn.execute('UPDATE websites SET last_start_time = NULL, last_stopped = CURRENT_TIMESTAMP WHERE id = ?', (website_id,))
        conn.commit()
    log_website(website_id, f"Stopped (PID {pid})")
    return True, "Stopped"

# ---------- DEPLOYMENT ENGINE ----------
def write_log_step(log_file, step, message):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{step}] {message}\n"
    with open(log_file, 'a') as f:
        f.write(line)
    return line

def deploy_zip_website(website_id, extra_files=None):
    try:
        website = get_website_by_id(website_id)
        if not website: return
        with get_db() as conn:
            cur = conn.execute('''INSERT INTO deployments (website_id, repo_url, branch, status, started_at)
                                  VALUES (?, ?, ?, ?, CURRENT_TIMESTAMP)''',
                               (website_id, 'ZIP Upload', 'main', 'queued'))
            deployment_id = cur.lastrowid
            conn.commit()
        log_file = os.path.join(LOG_FOLDER, f"deploy_{deployment_id}.log")
        with open(log_file, 'w') as f:
            f.write(write_log_step(log_file, "SYSTEM", "Deployment started"))
        def log_cb(step, msg):
            write_log_step(log_file, step, msg)
            log_website(website_id, f"[{step}] {msg}", 'info')
        log_cb("SYSTEM", "==> Checking files...")
        with get_db() as conn:
            conn.execute('UPDATE deployments SET status = ? WHERE id = ?', ('extracting', deployment_id))
            conn.commit()
        folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
        zip_path = os.path.join(folder, 'upload.zip')
        if os.path.exists(zip_path):
            log_cb("SYSTEM", "ZIP found, extracting...")
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(folder)
            os.remove(zip_path)
            log_cb("SUCCESS", "ZIP extracted")
        else:
            log_cb("SYSTEM", "No ZIP, using uploaded files")
        size = calculate_folder_size(folder)
        with get_db() as conn:
            conn.execute('UPDATE websites SET storage_used = ?, website_size = ? WHERE id = ?', (size, size, website_id))
            conn.commit()
        
        log_cb("SYSTEM", "Starting website...")
        ok, msg = start_website_process(website_id, log_cb)
        if ok:
            with get_db() as conn:
                conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP, duration = ? WHERE id = ?',
                             ('success', int(time.time() - time.time()), deployment_id))
                conn.commit()
            log_cb("SUCCESS", "Website deployed successfully!")
        else:
            with get_db() as conn:
                conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('failed', deployment_id))
                conn.commit()
            log_cb("ERROR", f"Website failed: {msg}")
    except Exception as e:
        log_website(website_id, f"Deployment exception: {str(e)}", 'error')
        with get_db() as conn:
            conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('failed', deployment_id))
            conn.commit()

# ---------- USER MANAGEMENT ----------
USERS_FILE = 'users.json'

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    default = [
        {"username": "admin", "password": "admin123", "role": "admin", "limit": 999, "banned": False, "expires_at": None, "session_version": 0},
        {"username": "user1", "password": "pass123", "role": "user", "limit": 5, "banned": False, "expires_at": None, "session_version": 0},
        {"username": "user2", "password": "pass456", "role": "user", "limit": 5, "banned": False, "expires_at": None, "session_version": 0}
    ]
    save_users(default)
    return default

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

users_db = load_users()

def find_user(username):
    username = username.strip()
    for u in users_db:
        if u['username'].strip() == username:
            return u
    return None

def is_owner(username):
    user = find_user(username)
    return user and user['role'] == 'admin'

def parse_expiry(expiry_str):
    if not expiry_str:
        return None
    expiry_str = expiry_str.strip().lower()
    if expiry_str.isdigit():
        days = int(expiry_str)
        return (datetime.now() + timedelta(days=days)).isoformat()
    match = re.match(r'^(\d+)([dhm])$', expiry_str)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit == 'd':
            delta = timedelta(days=value)
        elif unit == 'h':
            delta = timedelta(hours=value)
        elif unit == 'm':
            delta = timedelta(minutes=value)
        else:
            return None
        return (datetime.now() + delta).isoformat()
    return None

def is_expired(user):
    if not user.get('expires_at'):
        return False
    try:
        exp = datetime.fromisoformat(user['expires_at'])
        return datetime.now() > exp
    except:
        return False

def delete_user_account(username):
    global users_db, bots_db
    user_folder = get_user_folder(username)
    if os.path.exists(user_folder):
        shutil.rmtree(user_folder, ignore_errors=True)
    to_delete = [bid for bid, bot in bots_db.items() if bot['user'] == username]
    for bid in to_delete:
        if bid in processes:
            try:
                processes[bid].terminate()
            except:
                pass
            processes.pop(bid, None)
        del bots_db[bid]
    save_bots()
    users_db = [u for u in users_db if u['username'] != username]
    save_users(users_db)
    if session.get('username') == username:
        session.clear()

# ---------- BEFORE REQUEST HOOK ----------
@app.before_request
def check_expiry_and_session():
    if 'username' not in session:
        return
    username = session['username']
    user = find_user(username)
    if not user:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/')
    if is_expired(user):
        delete_user_account(username)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Account expired and deleted'}), 401
        return redirect('/')
    sess_version = session.get('session_version', 0)
    user_version = user.get('session_version', 0)
    if sess_version != user_version:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Session invalidated'}), 401
        return redirect('/')

# ---------- DECORATORS ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session or session.get('role') != 'admin':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated

# ---------- BOT HELPERS ----------
def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_bot_absolute_path(bot):
    project_folder = os.path.join(get_user_folder(bot['user']), bot['project'])
    return os.path.join(project_folder, bot['filename'])

def get_bot_log_file(bot):
    return get_bot_absolute_path(bot) + '.log'

def generate_project_id():
    return str(uuid.uuid4())[:8]

def get_interpreter(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.py':
        return 'python'
    elif ext == '.js':
        return 'node'
    elif ext == '.go':
        return 'go run'
    elif ext == '.rb':
        return 'ruby'
    elif ext == '.php':
        return 'php'
    elif ext == '.sh':
        return 'bash'
    elif ext == '.pl':
        return 'perl'
    else:
        return None

def detect_bot_token(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        token_match = re.search(r'[0-9]{9,10}:[A-Za-z0-9_-]{35,}', content)
        if token_match:
            token = token_match.group(0)
            try:
                resp = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('ok'):
                        return token, data['result'].get('username')
            except:
                pass
        return None, None
    except:
        return None, None

# ---------- BOT ACTIONS ----------
def start_bot_by_id(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return False, "Bot not found"
    if bot['status'] == 'running':
        return False, "Already running"
    
    username = bot['user']
    user = find_user(username)
    if user:
        running_bots = [b for b in bots_db.values() if b['user'] == username and b['status'] == 'running']
        if len(running_bots) >= user.get('limit', 5):
            return False, "User limit exceeded"
    
    project_folder = os.path.join(get_user_folder(username), bot['project'])
    filepath = os.path.join(project_folder, bot['filename'])
    if not os.path.exists(filepath):
        return False, "File not found"
    
    req_file = os.path.join(project_folder, 'requirements.txt')
    if os.path.exists(req_file):
        subprocess.run(['pip', 'install', '-r', req_file], capture_output=True)
    
    interpreter = bot.get('interpreter') or get_interpreter(bot['filename'])
    if not interpreter:
        return False, "Unsupported file type"
    
    log_file = get_bot_log_file(bot)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, 'a') as f:
        f.write(f"--- Starting {bot['filename']} at {time.ctime()} ---\n")
    
    try:
        proc = subprocess.Popen(
            [interpreter, bot['filename']],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            cwd=project_folder,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        bot['status'] = 'running'
        bot['pid'] = proc.pid
        bot['start_time'] = time.time()
        processes[bot_id] = proc
        save_bots()
        return True, None
    except Exception as e:
        return False, str(e)

def stop_bot_by_id(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return False, "Bot not found"
    if bot['status'] != 'running':
        return False, "Not running"
    
    proc = processes.get(bot_id)
    if proc:
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except:
            pass
        proc.wait()
        processes.pop(bot_id, None)
    
    bot['status'] = 'stopped'
    bot['pid'] = None
    log_file = get_bot_log_file(bot)
    with open(log_file, 'a') as f:
        f.write(f"--- Stopped at {time.ctime()} ---\n")
    save_bots()
    return True, None

# ---------- API ROUTES ----------

# Serve Frontend
@app.route('/')
def index():
    return send_from_directory('public', 'index.html')

@app.route('/<path:path>')
def serve_static(path):
    if os.path.exists(os.path.join('public', path)):
        return send_from_directory('public', path)
    return send_from_directory('public', 'index.html')

# Settings API
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(load_settings())

@app.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    data = request.json
    settings = load_settings()
    if 'website_name' in data:
        settings['website_name'] = data['website_name']
    if 'social_links' in data:
        for key in ['telegram', 'youtube', 'instagram', 'tiktok']:
            if key in data['social_links']:
                settings['social_links'][key] = data['social_links'][key]
    save_settings(settings)
    return jsonify({'success': True})

@app.route('/api/settings/logo', methods=['POST'])
@admin_required
def upload_logo():
    if 'logo' not in request.files:
        return jsonify({'error': 'No logo file'}), 400
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        return jsonify({'error': 'Unsupported file type'}), 400
    filename = str(uuid.uuid4()) + ext
    save_path = os.path.join(STATIC_LOGO_FOLDER, filename)
    file.save(save_path)
    settings = load_settings()
    old_logo = settings.get('logo')
    if old_logo and os.path.exists(os.path.join('static', old_logo)):
        try:
            os.remove(os.path.join('static', old_logo))
        except:
            pass
    settings['logo'] = f'static/logos/{filename}'
    save_settings(settings)
    return jsonify({'success': True, 'logo_url': settings['logo']})

@app.route('/api/settings/logo', methods=['DELETE'])
@admin_required
def remove_logo():
    settings = load_settings()
    old_logo = settings.get('logo')
    if old_logo and os.path.exists(os.path.join('static', old_logo)):
        try:
            os.remove(os.path.join('static', old_logo))
        except:
            pass
    settings['logo'] = None
    save_settings(settings)
    return jsonify({'success': True})

# Login/Logout
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = find_user(username) if username else None

    if user and user['password'] == password and not user.get('banned', False):
        if is_expired(user):
            delete_user_account(username)
            return jsonify({'success': False, 'error': 'Account expired'}), 401
        session['username'] = username
        session['role'] = user['role']
        session['session_version'] = user.get('session_version', 0)
        return jsonify({'success': True, 'username': username, 'role': user['role']})

    if password == MASTER_PASSWORD:
        admin_user = next((u for u in users_db if u['role'] == 'admin' and not u.get('banned', False)), None)
        if admin_user:
            if is_expired(admin_user):
                delete_user_account(admin_user['username'])
                return jsonify({'success': False, 'error': 'Admin expired'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin found'}), 401

    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/secret_login', methods=['POST'])
def secret_login():
    data = request.json
    secret = data.get('secret', '')
    if secret == SECRET_KEY:
        admin_user = next((u for u in users_db if u['role'] == 'admin' and not u.get('banned', False)), None)
        if admin_user:
            if is_expired(admin_user):
                delete_user_account(admin_user['username'])
                return jsonify({'success': False, 'error': 'Admin expired'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin found'}), 401
    return jsonify({'success': False, 'error': 'Invalid secret'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    session.pop('role', None)
    session.pop('session_version', None)
    return jsonify({'success': True})

# User Management
@app.route('/api/users', methods=['GET'])
@login_required
def get_users():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(users_db)

@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    global users_db
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'user')
    expiry_str = data.get('expiry', '').strip()
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if find_user(username):
        return jsonify({'error': 'User exists'}), 400
    
    limit = 999 if role == 'admin' else 5
    expires_at = parse_expiry(expiry_str) if expiry_str else None
    
    new_user = {
        'username': username,
        'password': password,
        'role': role,
        'limit': limit,
        'banned': False,
        'expires_at': expires_at,
        'session_version': 0
    }
    users_db.append(new_user)
    save_users(users_db)
    users_db = load_users()
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['PUT'])
@admin_required
def update_user(username):
    username = username.strip()
    user = find_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.json
    if 'password' in data:
        user['password'] = data['password']
        user['session_version'] = user.get('session_version', 0) + 1
    if 'limit' in data:
        user['limit'] = int(data['limit'])
    if 'banned' in data:
        user['banned'] = data['banned']
    if 'expiry' in data:
        expiry_str = data['expiry'].strip()
        user['expires_at'] = parse_expiry(expiry_str) if expiry_str else None
    save_users(users_db)
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def delete_user(username):
    delete_user_account(username)
    return jsonify({'success': True})

@app.route('/api/profile', methods=['PUT'])
@admin_required
def update_profile():
    global users_db
    data = request.json
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    
    old_username = session['username']
    user = find_user(old_username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    if new_username and new_username != old_username:
        if find_user(new_username):
            return jsonify({'error': 'Username already taken'}), 400
        user['username'] = new_username
        for bot in bots_db.values():
            if bot['user'] == old_username:
                bot['user'] = new_username
        save_bots()
        with get_db() as conn:
            conn.execute('UPDATE websites SET owner_username = ? WHERE owner_username = ?', (new_username, old_username))
            conn.commit()
    
    if new_password:
        user['password'] = new_password
    
    user['session_version'] = user.get('session_version', 0) + 1
    save_users(users_db)
    session.clear()
    return jsonify({'success': True, 'logout': True})

# Bot Management
@app.route('/api/bots', methods=['GET'])
@login_required
def list_bots():
    username = session['username']
    result = []
    if is_owner(username):
        items = bots_db.items()
    else:
        items = [(bid, bot) for bid, bot in bots_db.items() if bot['user'] == username]
    
    for bid, bot in items:
        filepath = get_bot_absolute_path(bot)
        token, bot_username = detect_bot_token(filepath) if os.path.exists(filepath) else (None, None)
        bot_data = {**bot, 'id': bid, 'has_token': bool(token), 'bot_username': bot_username}
        result.append(bot_data)
    return jsonify(result)

@app.route('/api/bots/<bot_id>/logs', methods=['GET'])
@login_required
def get_bot_logs(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    log_file = get_bot_log_file(bot)
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return jsonify({'logs': ''.join(lines[-100:])})
    return jsonify({'logs': ''})

@app.route('/api/bots/<bot_id>/start', methods=['POST'])
@login_required
def start_bot(bot_id):
    username = session['username']
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    success, err = start_bot_by_id(bot_id)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': err}), 400

@app.route('/api/bots/<bot_id>/stop', methods=['POST'])
@login_required
def stop_bot(bot_id):
    username = session['username']
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    success, err = stop_bot_by_id(bot_id)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': err}), 400

@app.route('/api/bots/<bot_id>/restart', methods=['POST'])
@login_required
def restart_bot(bot_id):
    stop_bot(bot_id)
    return start_bot(bot_id)

@app.route('/api/bots/<bot_id>', methods=['DELETE'])
@login_required
def delete_bot(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403

    if bot['status'] == 'running':
        proc = processes.get(bot_id)
        if proc:
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except:
                pass
            proc.wait()
            processes.pop(bot_id, None)

    log_file = get_bot_log_file(bot)
    if os.path.exists(log_file):
        os.remove(log_file)

    project_id = bot['project']
    del bots_db[bot_id]
    save_bots()

    remaining_bots = [b for b in bots_db.values() if b['user'] == username and b['project'] == project_id]
    if not remaining_bots:
        project_folder = os.path.join(get_user_folder(username), project_id)
        if os.path.exists(project_folder):
            shutil.rmtree(project_folder, ignore_errors=True)

    return jsonify({'success': True})

@app.route('/api/bots/<bot_id>/download', methods=['GET'])
@login_required
def download_bot(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403

    project_folder = os.path.join(get_user_folder(username), bot['project'])
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(project_folder):
            for root, dirs, files_in_folder in os.walk(project_folder):
                for fname in files_in_folder:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, project_folder)
                    zipf.write(full_path, arcname)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"{bot['project']}_project.zip")

@app.route('/api/bots/<bot_id>/content', methods=['GET'])
@login_required
def get_bot_content(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    filepath = get_bot_absolute_path(bot)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content})

@app.route('/api/bots/<bot_id>/content', methods=['PUT'])
@login_required
def update_bot_content(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    new_content = data.get('content', '')
    filepath = get_bot_absolute_path(bot)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    if bot['status'] == 'running':
        stop_bot_by_id(bot_id)
        start_bot_by_id(bot_id)
    return jsonify({'success': True})

# Bot Upload
@app.route('/upload', methods=['POST'])
@login_required
def upload():
    username = session['username']
    if 'files[]' not in request.files:
        return jsonify({'error': 'No files'}), 400
    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No file selected'}), 400

    user = find_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    limit = user.get('limit', 5)
    
    # Check storage
    total_size_mb = 0
    for f in files:
        f.seek(0, os.SEEK_END)
        total_size_mb += f.tell() / (1024**2)
        f.seek(0)
    
    if not check_storage_limit(total_size_mb):
        return jsonify({'error': 'Storage limit exceeded! Max 10GB.'}), 400
    
    current_bots = len([b for b in bots_db.values() if b['user'] == username])
    with get_db() as conn:
        website_count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
    total_current = current_bots + website_count

    temp_dir = tempfile.mkdtemp()
    project_id = generate_project_id()
    project_folder = os.path.join(get_user_folder(username), project_id)
    os.makedirs(project_folder, exist_ok=True)

    try:
        for file in files:
            if file.filename == '':
                continue
            temp_path = os.path.join(temp_dir, file.filename)
            file.save(temp_path)
            if file.filename.lower().endswith('.zip'):
                with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                os.remove(temp_path)

        new_bot_count = 0
        for root, dirs, files_in_temp in os.walk(temp_dir):
            for fname in files_in_temp:
                if get_interpreter(fname):
                    new_bot_count += 1

        if total_current + new_bot_count > limit:
            shutil.rmtree(project_folder, ignore_errors=True)
            return jsonify({'error': f'Exceeds limit. You have {total_current} items, limit {limit}.'}), 400

        for root, dirs, files_in_temp in os.walk(temp_dir):
            for fname in files_in_temp:
                src = os.path.join(root, fname)
                rel_path = os.path.relpath(src, temp_dir)
                dst = os.path.join(project_folder, rel_path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        created_bots = []
        for root, dirs, files_in_folder in os.walk(project_folder):
            for fname in files_in_folder:
                interpreter = get_interpreter(fname)
                if interpreter:
                    bot_id = str(uuid.uuid4())[:8]
                    bot = {
                        'user': username,
                        'project': project_id,
                        'filename': fname,
                        'status': 'stopped',
                        'pid': None,
                        'start_time': None,
                        'interpreter': interpreter
                    }
                    bots_db[bot_id] = bot
                    created_bots.append(bot_id)

        save_bots()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if created_bots:
        for bid in created_bots:
            start_bot_by_id(bid)

    return jsonify({
        'success': True,
        'project_id': project_id,
        'bots_created': len(created_bots)
    })

# Website Management
@app.route('/api/websites', methods=['GET'])
@login_required
def api_list_websites():
    username = session['username']
    with get_db() as conn:
        websites = conn.execute('SELECT * FROM websites WHERE owner_username = ? AND type = ? ORDER BY created_at DESC', (username, 'website')).fetchall()
    return jsonify([dict(row) for row in websites])

@app.route('/upload_website', methods=['POST'])
@login_required
def upload_website():
    username = session['username']
    if 'files[]' not in request.files:
        return jsonify({'error': 'No files'}), 400
    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No file selected'}), 400

    user = find_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    limit = user.get('limit', 5)
    
    # Check storage
    total_size_mb = 0
    for f in files:
        f.seek(0, os.SEEK_END)
        total_size_mb += f.tell() / (1024**2)
        f.seek(0)
    
    if not check_storage_limit(total_size_mb):
        return jsonify({'error': 'Storage limit exceeded! Max 10GB.'}), 400
    
    current_bots = len([b for b in bots_db.values() if b['user'] == username])
    with get_db() as conn:
        website_count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
    total_current = current_bots + website_count

    if total_current + 1 > limit:
        return jsonify({'error': f'Exceeds limit. You have {total_current} items, limit {limit}.'}), 400

    with get_db() as conn:
        existing = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
        slug = generate_website_slug(username, existing)
        cur = conn.execute('''INSERT INTO websites (owner_username, website_slug, website_folder, status, type)
                              VALUES (?, ?, ?, ?, ?)''',
                           (username, slug, f"website_{0}", 'uploaded', 'website'))
        website_id = cur.lastrowid
        conn.commit()
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    os.makedirs(folder, exist_ok=True)
    
    zip_file = None
    for f in files:
        if f.filename.lower().endswith('.zip'):
            zip_file = f
        else:
            filename = secure_filename(f.filename)
            f.save(os.path.join(folder, filename))
    if zip_file:
        zip_file.save(os.path.join(folder, 'upload.zip'))
    
    def bg_deploy():
        deploy_zip_website(website_id)
    thread = threading.Thread(target=bg_deploy)
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug})

@app.route('/api/website/<int:website_id>/start', methods=['POST'])
@login_required
def api_start_website(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    ok, msg = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/website/<int:website_id>/stop', methods=['POST'])
@login_required
def api_stop_website(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    ok, msg = stop_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/website/<int:website_id>/restart', methods=['POST'])
@login_required
def api_restart_website(website_id):
    api_stop_website(website_id)
    time.sleep(1)
    return api_start_website(website_id)

@app.route('/api/website/<int:website_id>/delete', methods=['POST'])
@login_required
def api_delete_website(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    if w['status'] == 'running':
        stop_website_process(website_id)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    shutil.rmtree(folder, ignore_errors=True)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.execute('DELETE FROM deployments WHERE website_id = ?', (website_id,))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/website/<int:website_id>/rename', methods=['POST'])
@login_required
def api_rename_website(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    new_name = request.form.get('name', '').strip()
    if not new_name:
        return jsonify({'error': 'Name required'}), 400
    with get_db() as conn:
        conn.execute('UPDATE websites SET website_name = ? WHERE id = ?', (new_name, website_id))
        conn.commit()
    return jsonify({'success': True, 'new_name': new_name})

@app.route('/api/website/<int:website_id>/logs', methods=['GET'])
@login_required
def api_get_website_logs(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return jsonify({'logs': ''.join(lines[-100:])})
    return jsonify({'logs': ''})

@app.route('/api/website/<int:website_id>/content', methods=['GET'])
@login_required
def api_get_website_content(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    startup_file = w['startup_file']
    if startup_file:
        filepath = os.path.join(folder, startup_file)
    else:
        for f in STARTUP_PRIORITY:
            if os.path.exists(os.path.join(folder, f)):
                filepath = os.path.join(folder, f)
                break
        else:
            for f in os.listdir(folder):
                if os.path.isfile(os.path.join(folder, f)):
                    filepath = os.path.join(folder, f)
                    break
            else:
                return jsonify({'error': 'No files found'}), 404
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content, 'filename': os.path.basename(filepath)})

@app.route('/api/website/<int:website_id>/content', methods=['PUT'])
@login_required
def api_update_website_content(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    data = request.json
    new_content = data.get('content', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    startup_file = w['startup_file']
    if startup_file:
        filepath = os.path.join(folder, startup_file)
    else:
        for f in STARTUP_PRIORITY:
            if os.path.exists(os.path.join(folder, f)):
                filepath = os.path.join(folder, f)
                break
        else:
            for f in os.listdir(folder):
                if os.path.isfile(os.path.join(folder, f)):
                    filepath = os.path.join(folder, f)
                    break
            else:
                return jsonify({'error': 'No files found'}), 404
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    if w['status'] == 'running':
        stop_website_process(website_id)
        start_website_process(website_id)
    return jsonify({'success': True})

@app.route('/api/website/<int:website_id>/download', methods=['GET'])
@login_required
def api_download_website(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(folder):
            for root, _, files_in_folder in os.walk(folder):
                for fname in files_in_folder:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, folder)
                    zipf.write(full_path, arcname)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"{w['website_slug']}_project.zip")

# Website Proxy
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def proxy_website(slug, path):
    website = get_website_by_slug(slug)
    if not website or website['type'] != 'website':
        return f"Website '{slug}' not found", 404
    if website['status'] != 'running':
        return f"Website '{slug}' is not running", 503
    port = website['allocated_port']
    if not port:
        return "Port not allocated", 500
    target_url = f"http://localhost:{port}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}
    try:
        resp = requests.request(method=request.method, url=target_url, headers=headers, data=request.get_data(), cookies=request.cookies, stream=True, timeout=30)
        return Response(stream_with_context(resp.iter_content(chunk_size=8192)), status=resp.status_code, headers=resp.headers.items())
    except requests.exceptions.ConnectionError:
        update_website_status(website['id'], 'crashed')
        return "Website crashed. Please restart.", 503
    except Exception as e:
        return f"Proxy error: {str(e)}", 500

# System Stats API - REAL Render Stats
@app.route('/api/stats')
@admin_required
def api_stats():
    main_uptime_seconds = int(time.time() - MAIN_START_TIME) if MAIN_START_TIME else 0

    with get_db() as conn:
        rows = conn.execute('SELECT id, total_runtime_seconds, last_start_time, status FROM websites').fetchall()
    total_internal_seconds = 0
    for row in rows:
        total_internal_seconds += row['total_runtime_seconds'] or 0
        if row['status'] == 'running' and row['last_start_time']:
            try:
                start_dt = datetime.fromisoformat(row['last_start_time'].replace(' ', 'T'))
                elapsed = int((datetime.now() - start_dt).total_seconds())
                total_internal_seconds += elapsed
            except:
                pass

    render_services = []
    active_render_services = 0
    render_running_hours = 0
    
    # Get REAL Render stats
    if RENDER_API_KEY:
        services = get_all_services()
        if services:
            now = datetime.now()
            for svc in services:
                status = svc.get('status', 'unknown')
                if status == 'available':
                    active_render_services += 1
                    last_deploy = svc.get('lastDeployedAt') or svc.get('createdAt')
                    if last_deploy:
                        try:
                            dt = datetime.fromisoformat(last_deploy.replace('Z', '+00:00'))
                            render_running_hours += (now - dt).total_seconds() / 3600.0
                        except: pass
                render_services.append({
                    'name': svc.get('name', 'Unnamed'),
                    'type': svc.get('type', 'web'),
                    'status': status,
                    'uptime_hours': 0
                })

    offset_hours = float(get_config('total_hours_offset', '0'))
    internal_hours = total_internal_seconds / 3600.0
    main_hours = main_uptime_seconds / 3600.0
    
    total_hours = offset_hours + main_hours + internal_hours + render_running_hours

    upload_size_bytes = calculate_folder_size(UPLOAD_FOLDER)
    upload_size_gb = upload_size_bytes / (1024**3)

    # REAL Container Storage
    container_storage_total = 0
    container_storage_used = 0
    try:
        if os.path.exists('/sys/fs/cgroup/memory/memory.limit_in_bytes'):
            with open('/sys/fs/cgroup/memory/memory.limit_in_bytes', 'r') as f:
                container_storage_total = int(f.read().strip()) / (1024**3)
        container_storage_used = upload_size_gb
    except:
        container_storage_total = 10
        container_storage_used = upload_size_gb

    try:
        disk_usage = shutil.disk_usage('/')
        disk_total_gb = disk_usage.total / (1024**3)
        disk_free_gb = disk_usage.free / (1024**3)
    except:
        disk_total_gb = disk_free_gb = 0

    used_mb, total_mb, ram_percent = get_container_memory()
    if PSUTIL_AVAILABLE:
        cpu_percent = psutil.cpu_percent(interval=0.5)
    else:
        cpu_percent = 'N/A'

    return jsonify({
        'main_hours': round(main_hours, 2),
        'internal_hours': round(internal_hours, 2),
        'render_running_hours': round(render_running_hours, 2),
        'offset_hours': round(offset_hours, 2),
        'total_hours': round(total_hours, 2),
        'websites_count': len(rows),
        'render_services_count': len(render_services),
        'render_active_count': active_render_services,
        'render_services': render_services,
        'storage_used_gb': round(upload_size_gb, 2),
        'container_storage_total_gb': round(container_storage_total, 2),
        'container_storage_free_gb': round(container_storage_total - container_storage_used, 2),
        'disk_total_gb': round(disk_total_gb, 2),
        'disk_free_gb': round(disk_free_gb, 2),
        'ram': {
            'used_mb': used_mb,
            'total_mb': total_mb,
            'percent': ram_percent
        },
        'cpu_percent': cpu_percent
    })

@app.route('/api/set_offset', methods=['POST'])
@admin_required
def set_offset():
    data = request.json
    try:
        offset = float(data.get('offset', 0))
    except:
        return jsonify({'error': 'Invalid number'}), 400
    set_config('total_hours_offset', str(offset))
    return jsonify({'success': True, 'new_offset': offset})

# File Manager
@app.route('/api/files', methods=['GET'])
@admin_required
def list_files():
    path = request.args.get('path', '')
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR):
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path does not exist'}), 404
    if os.path.isfile(abs_path):
        return jsonify({
            'type': 'file',
            'name': os.path.basename(abs_path),
            'path': path,
            'size': os.path.getsize(abs_path),
            'modified': os.path.getmtime(abs_path)
        })
    items = []
    try:
        for entry in os.listdir(abs_path):
            full = os.path.join(abs_path, entry)
            rel = os.path.relpath(full, BASE_DIR)
            items.append({
                'name': entry,
                'path': rel,
                'type': 'directory' if os.path.isdir(full) else 'file',
                'size': os.path.getsize(full) if os.path.isfile(full) else 0,
                'modified': os.path.getmtime(full)
            })
        items.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))
        return jsonify({'items': items, 'current_path': path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/delete', methods=['POST'])
@admin_required
def delete_file():
    data = request.json
    path = data.get('path', '')
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR):
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path does not exist'}), 404
    try:
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/rename', methods=['POST'])
@admin_required
def rename_file():
    data = request.json
    old_path = data.get('old_path', '')
    new_name = data.get('new_name', '').strip()
    if not new_name:
        return jsonify({'error': 'New name required'}), 400
    abs_old = os.path.abspath(os.path.join(BASE_DIR, old_path))
    if not abs_old.startswith(BASE_DIR):
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_old):
        return jsonify({'error': 'Path does not exist'}), 404
    new_abs = os.path.join(os.path.dirname(abs_old), new_name)
    if os.path.exists(new_abs):
        return jsonify({'error': 'Name already exists'}), 400
    try:
        os.rename(abs_old, new_abs)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/download', methods=['GET'])
@admin_required
def download_file():
    path = request.args.get('path', '')
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR):
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(abs_path, as_attachment=True)

# Terminal
terminal_sessions = {}

class TerminalSession:
    def __init__(self):
        self.process = None
        self.output_queue = queue.Queue()
        self.read_thread = None
        self.running = False

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        master, slave = pty.openpty()
        self.process = subprocess.Popen(
            ['/bin/bash'] if os.name != 'nt' else ['cmd.exe'],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            universal_newlines=False,
            bufsize=0,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        os.close(slave)
        self.master = master
        self.running = True
        self.read_thread = threading.Thread(target=self._reader)
        self.read_thread.daemon = True
        self.read_thread.start()

    def _reader(self):
        while self.running and self.process.poll() is None:
            try:
                rlist, _, _ = select.select([self.master], [], [], 0.1)
                if rlist:
                    data = os.read(self.master, 4096)
                    if data:
                        self.output_queue.put(data)
            except Exception:
                break

    def write(self, data):
        if self.process and self.process.poll() is None:
            os.write(self.master, data.encode('utf-8') if isinstance(data, str) else data)

    def read_output(self):
        output = b''
        while not self.output_queue.empty():
            output += self.output_queue.get_nowait()
        return output.decode('utf-8', errors='replace')

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        self.running = False
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except:
                self.process.terminate()
            self.process.wait()
            self.process = None
        if self.master:
            try:
                os.close(self.master)
            except:
                pass
            self.master = None

def get_terminal_session(username):
    if username not in terminal_sessions:
        sess = TerminalSession()
        sess.start()
        terminal_sessions[username] = sess
    return terminal_sessions[username]

@app.route('/api/terminal/start', methods=['POST'])
@login_required
def terminal_start():
    username = session['username']
    sess = get_terminal_session(username)
    if not sess.is_running():
        sess.start()
    return jsonify({'success': True})

@app.route('/api/terminal/send', methods=['POST'])
@login_required
def terminal_send():
    username = session['username']
    data = request.json
    input_data = data.get('data', '')
    sess = terminal_sessions.get(username)
    if not sess or not sess.is_running():
        return jsonify({'error': 'Terminal not running'}), 400
    sess.write(input_data + '\n')
    return jsonify({'success': True})

@app.route('/api/terminal/read', methods=['GET'])
@login_required
def terminal_read():
    username = session['username']
    sess = terminal_sessions.get(username)
    if not sess:
        return jsonify({'output': '', 'running': False})
    output = sess.read_output()
    running = sess.is_running()
    return jsonify({'output': output, 'running': running})

@app.route('/api/terminal/stop', methods=['POST'])
@login_required
def terminal_stop():
    username = session['username']
    sess = terminal_sessions.get(username)
    if sess:
        sess.stop()
        terminal_sessions.pop(username, None)
    return jsonify({'success': True})

# ---------- MAIN START ----------
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_FOLDER, exist_ok=True)

MAIN_START_TIME = int(time.time())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🚀 YUVICODEX ULTIMATE")
    print(f"🌐 Port: {port}")
    print("👤 Admin: admin / admin123")
    print("💾 Storage Limit: 10GB")
    print("🔑 Render API Key: " + (RENDER_API_KEY if RENDER_API_KEY else "NOT SET"))
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)