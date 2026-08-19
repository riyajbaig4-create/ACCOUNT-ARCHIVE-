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
import select
import pty
import queue
from datetime import datetime, timedelta
from functools import wraps  # ✅ Added for decorators
from flask import Flask, render_template_string, request, jsonify, session, send_file, redirect, Response, stream_with_context, abort
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key_change_me_in_production'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# ---------- DECORATORS (defined early) ----------
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

# ---------- CONFIG ----------
PASSWORD = "your_secure_password"
MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', 'master123')
SECRET_KEY = os.environ.get('SECRET_KEY', 'secret123')

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
SETTINGS_FILE = os.path.join(BASE_DIR, 'settings.json')
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
BOTS_FILE = os.path.join(UPLOAD_FOLDER, 'bots.json')
STATIC_LOGO_FOLDER = os.path.join(BASE_DIR, 'static', 'logos')
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py', 'wsgi.py', 'asgi.py']

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)
os.makedirs(STATIC_LOGO_FOLDER, exist_ok=True)

# ---------- SETTINGS ----------
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
            for k, v in default.items():
                if k not in data:
                    data[k] = v
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

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
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
            last_login TIMESTAMP
        )''')
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
            bot_interpreter TEXT,
            env_vars TEXT DEFAULT '{}'
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            cli_tool_id INTEGER,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        conn.execute('''CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            cli_tool_id INTEGER,
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
        conn.execute('''CREATE TABLE IF NOT EXISTS cli_tools (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_username TEXT NOT NULL,
            tool_name TEXT,
            tool_slug TEXT UNIQUE NOT NULL,
            tool_folder TEXT NOT NULL,
            startup_file TEXT,
            interpreter TEXT,
            status TEXT DEFAULT 'stopped',
            pid INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_started TIMESTAMP,
            last_stopped TIMESTAMP,
            storage_used INTEGER DEFAULT 0
        )''')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_website ON logs(website_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_deployments_website ON deployments(website_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cli_tools_owner ON cli_tools(owner_username)')
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                     ('total_hours_offset', '0'))
        conn.commit()
init_db()

# ---------- USER MANAGEMENT ----------
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
    user_folder = os.path.join(UPLOAD_FOLDER, username)
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

# ---------- BOT MANAGEMENT ----------
BOTS_FILE = os.path.join(UPLOAD_FOLDER, 'bots.json')
bots_db = {}
processes = {}

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

# ---------- HELPERS ----------
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

def get_website_by_id(website_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM websites WHERE id = ?', (website_id,)).fetchone()

def get_website_by_slug(slug):
    with get_db() as conn:
        return conn.execute('SELECT * FROM websites WHERE website_slug = ?', (slug,)).fetchone()

def get_websites_by_user(username, type_filter=None):
    with get_db() as conn:
        if type_filter:
            return conn.execute('SELECT * FROM websites WHERE owner_username = ? AND type = ? ORDER BY created_at DESC', (username, type_filter)).fetchall()
        return conn.execute('SELECT * FROM websites WHERE owner_username = ? ORDER BY created_at DESC', (username,)).fetchall()

def get_next_available_port(start=5001):
    with get_db() as conn:
        used = [r[0] for r in conn.execute('SELECT allocated_port FROM websites WHERE allocated_port IS NOT NULL').fetchall()]
    port = start
    while port in used:
        port += 1
    return port

def generate_website_slug(username, count):
    base = username.lower().replace('_', '-')
    return base if count == 0 else f"{base}{count}"

def log_website(website_id, message, log_type='info'):
    with get_db() as conn:
        conn.execute('INSERT INTO logs (website_id, log_type, log_text) VALUES (?, ?, ?)',
                     (website_id, log_type, message))
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

def get_config(key, default='0'):
    with get_db() as conn:
        row = conn.execute('SELECT value FROM config WHERE key = ?', (key,)).fetchone()
        return row['value'] if row else default

def set_config(key, value):
    with get_db() as conn:
        conn.execute('INSERT OR REPLACE INTO config (key, value) VALUES (?, ?)', (key, value))
        conn.commit()

# ---------- PROJECT DATA ----------
class ProjectData:
    def __init__(self, project_folder):
        self.folder = project_folder
        self.data_file = os.path.join(project_folder, 'project_data.json')
        self.data = self._load()

    def _load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r') as f:
                    return json.load(f)
            except:
                pass
        return {"logs": [], "stats": {}, "config": {}}

    def _save(self):
        try:
            with open(self.data_file, 'w') as f:
                json.dump(self.data, f, indent=2)
        except:
            pass

    def add_log(self, message, log_type='info'):
        self.data["logs"].append({
            "ts": datetime.now().isoformat(),
            "type": log_type,
            "msg": message
        })
        if len(self.data["logs"]) > 500:
            self.data["logs"] = self.data["logs"][-500:]
        self._save()

    def update_stat(self, key, value):
        self.data["stats"][key] = value
        self._save()

    def get_stat(self, key, default=None):
        return self.data["stats"].get(key, default)

    def set_config(self, key, value):
        self.data["config"][key] = value
        self._save()

    def get_config(self, key, default=None):
        return self.data["config"].get(key, default)

# ---------- LOGS AUTO-CLEAR ----------
def clear_all_logs():
    try:
        if os.path.exists(LOG_FOLDER):
            for f in os.listdir(LOG_FOLDER):
                file_path = os.path.join(LOG_FOLDER, f)
                if os.path.isfile(file_path):
                    try:
                        os.remove(file_path)
                    except:
                        pass
        with get_db() as conn:
            conn.execute('DELETE FROM logs')
            conn.commit()
        for bot_id, bot in list(bots_db.items()):
            project_folder = os.path.join(get_user_folder(bot['user']), bot['project'])
            log_file = os.path.join(project_folder, bot_id + '.log')
            if os.path.exists(log_file):
                try:
                    os.remove(log_file)
                except:
                    pass
        with get_db() as conn:
            cli_tools = conn.execute('SELECT id, tool_folder FROM cli_tools').fetchall()
            for tool in cli_tools:
                log_file = os.path.join(tool['tool_folder'], 'cli.log')
                if os.path.exists(log_file):
                    try:
                        os.remove(log_file)
                    except:
                        pass
        print(f"✅ All logs cleared at {datetime.now()}")
        return True
    except Exception as e:
        print(f"Error clearing logs: {e}")
        return False

def schedule_log_clear():
    clear_all_logs()
    threading.Timer(3600, schedule_log_clear).start()

schedule_log_clear()

# ---------- RUNTIME DETECTION (IMPROVED) ----------
def find_startup_file(folder):
    for filename in STARTUP_PRIORITY:
        if os.path.exists(os.path.join(folder, filename)):
            return filename
    return None

def detect_runtime_and_get_cmd(folder, port):
    # ----- Node.js (with package.json) -----
    if os.path.exists(os.path.join(folder, 'package.json')):
        try:
            with open(os.path.join(folder, 'package.json'), 'r') as f:
                data = json.load(f)
                scripts = data.get('scripts', {})
                if 'start' in scripts:
                    return ['npm', 'start'], 'nodejs', {'NODE_ENV': 'production', 'PORT': str(port)}
                else:
                    main_file = data.get('main')
                    if main_file and os.path.exists(os.path.join(folder, main_file)):
                        return ['node', main_file], 'nodejs', {'PORT': str(port)}
        except:
            pass

    # ----- Common JS files -----
    for fname in ['server.js', 'index.js', 'app.js', 'main.js']:
        if os.path.exists(os.path.join(folder, fname)):
            return ['node', fname], 'nodejs', {'PORT': str(port)}

    # ----- Fallback: any .js file -----
    try:
        for f in os.listdir(folder):
            if f.endswith('.js') and os.path.isfile(os.path.join(folder, f)):
                return ['node', f], 'nodejs', {'PORT': str(port)}
    except:
        pass

    # ----- PHP -----
    if os.path.exists(os.path.join(folder, 'index.php')):
        return ['php', '-S', f'0.0.0.0:{port}'], 'php', {}

    # ----- Go -----
    if os.path.exists(os.path.join(folder, 'go.mod')):
        return ['go', 'run', 'main.go'], 'go', {}

    # ----- Java -----
    if os.path.exists(os.path.join(folder, 'pom.xml')):
        return ['mvn', 'spring-boot:run'], 'java', {}
    if os.path.exists(os.path.join(folder, 'build.gradle')):
        return ['./gradlew', 'bootRun'], 'java', {}
    jars = [f for f in os.listdir(folder) if f.endswith('.jar')]
    if jars:
        return ['java', '-jar', jars[0]], 'java', {}

    # ----- Python Flask/Django -----
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

    # ----- Fallback: any .py file -----
    try:
        for f in os.listdir(folder):
            if f.endswith('.py') and os.path.isfile(os.path.join(folder, f)):
                return [sys.executable, f], 'python', {}
    except:
        pass

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

# ---------- HEALTH CHECK (IMPROVED) ----------
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

def health_check_on_ports(port_list, max_retries=20, delay=3):
    for port in port_list:
        for attempt in range(max_retries):
            try:
                response = requests.get(f"http://localhost:{port}", timeout=3)
                if response.status_code < 500:
                    return True, port, f"OK (port {port})"
            except:
                pass
            time.sleep(delay)
    return False, None, "Health check failed"

# ---------- START / STOP WEBSITE ----------
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

    # ---- Load env vars from database ----
    env_vars = {}
    try:
        env_vars = json.loads(website['env_vars'] or '{}')
    except:
        pass

    env = os.environ.copy()
    env['PORT'] = str(allocated_port)
    env['PYTHONUNBUFFERED'] = '1'
    env.update(env_extra)
    env.update(env_vars)  # user-defined env vars override

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

        healthy, actual_port, health_msg = health_check_on_ports(ports_to_try, max_retries=20, delay=3)
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
            update_website_status(website_id, 'failed')
            log_website(website_id, f"Health check failed: {health_msg}", 'error')
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

# ---------- ENV VARS API ----------
@app.route('/api/website/<int:website_id>/env', methods=['GET', 'PUT'])
@login_required
def website_env(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Forbidden'}), 403
    if request.method == 'GET':
        try:
            env = json.loads(w['env_vars'] or '{}')
        except:
            env = {}
        return jsonify({'env': env})
    else:
        data = request.json
        new_env = data.get('env', {})
        if not isinstance(new_env, dict):
            return jsonify({'error': 'Invalid format'}), 400
        with get_db() as conn:
            conn.execute('UPDATE websites SET env_vars = ? WHERE id = ?', (json.dumps(new_env), website_id))
            conn.commit()
        if w['status'] == 'running':
            stop_website_process(website_id)
            start_website_process(website_id)
        return jsonify({'success': True})

# ---------- DEPLOYMENT ----------
def write_log_step(log_file, step, message):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{step}] {message}\n"
    with open(log_file, 'a') as f:
        f.write(line)
    return line

def deploy_zip_website(website_id):
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

        # ---- Check for .env file in the extracted folder ----
        env_file = os.path.join(folder, '.env')
        if os.path.exists(env_file):
            log_cb("SYSTEM", "Found .env file, parsing...")
            try:
                with open(env_file, 'r') as f:
                    env_vars = {}
                    for line in f:
                        line = line.strip()
                        if line and not line.startswith('#'):
                            key, val = line.split('=', 1)
                            env_vars[key.strip()] = val.strip()
                with get_db() as conn:
                    conn.execute('UPDATE websites SET env_vars = ? WHERE id = ?', (json.dumps(env_vars), website_id))
                    conn.commit()
                log_cb("SUCCESS", f"Loaded {len(env_vars)} environment variables from .env")
            except Exception as e:
                log_cb("ERROR", f"Failed to parse .env: {str(e)}")

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
            try:
                conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?', ('failed', deployment_id))
            except:
                pass
            conn.commit()

# ---------- BOT ACTIONS ----------
def start_bot_by_id(bot_id):
    bot = bots_db.get(bot_id)
    if not bot: return False, "Bot not found"
    if bot['status'] == 'running': return False, "Already running"
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
    project_data = ProjectData(project_folder)
    project_data.add_log(f"Starting {bot['filename']}...", "info")
    project_data.update_stat("last_start", datetime.now().isoformat())
    project_data.update_stat("start_count", project_data.get_stat("start_count", 0) + 1)
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
        project_data.add_log(f"Started with PID {proc.pid}", "success")
        return True, None
    except Exception as e:
        project_data.add_log(f"Start failed: {str(e)}", "error")
        return False, str(e)

def stop_bot_by_id(bot_id):
    bot = bots_db.get(bot_id)
    if not bot: return False, "Bot not found"
    if bot['status'] != 'running': return False, "Not running"
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

# ---------- KILL SYSTEM ----------
KILL_API_KEY = "your_secret_kill_key_2024"
KILL_STATUS = False
KILL_LOG = []

@app.route('/api/kill', methods=['POST'])
def kill_system():
    global KILL_STATUS, KILL_LOG
    data = request.json or {}
    key = data.get('key', '')
    action = data.get('action', '')
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    if key != KILL_API_KEY:
        return jsonify({"error": "Invalid API key", "success": False}), 403
    if action == 'kill':
        if KILL_STATUS:
            return jsonify({"success": True, "message": "System already killed", "status": "killed"})
        KILL_STATUS = True
        killed_bots = []
        killed_websites = []
        for bot_id, bot in list(bots_db.items()):
            if bot['status'] == 'running':
                try:
                    stop_bot_by_id(bot_id)
                    killed_bots.append(bot_id)
                except:
                    pass
        with get_db() as conn:
            running_websites = conn.execute('SELECT id FROM websites WHERE status="running"').fetchall()
            for w in running_websites:
                try:
                    stop_website_process(w['id'])
                    killed_websites.append(w['id'])
                except:
                    pass
        KILL_LOG.append({"timestamp": timestamp, "action": "kill", "bots": len(killed_bots), "websites": len(killed_websites)})
        return jsonify({"success": True, "message": "All services killed", "killed_bots": len(killed_bots), "killed_websites": len(killed_websites), "status": "killed", "timestamp": timestamp})
    elif action == 'status':
        running_bots = len([b for b in bots_db.values() if b['status'] == 'running'])
        with get_db() as conn:
            running_websites = conn.execute('SELECT COUNT(*) FROM websites WHERE status="running"').fetchone()[0]
            total_websites = conn.execute('SELECT COUNT(*) FROM websites').fetchone()[0]
        return jsonify({"success": True, "status": "killed" if KILL_STATUS else "running", "killed": KILL_STATUS, "running_bots": running_bots, "running_websites": running_websites, "total_bots": len(bots_db), "total_websites": total_websites, "logs": KILL_LOG[-10:]})
    elif action == 'restore':
        KILL_STATUS = False
        KILL_LOG.append({"timestamp": timestamp, "action": "restore", "message": "System restored"})
        return jsonify({"success": True, "message": "System restored", "status": "running", "timestamp": timestamp})
    return jsonify({"error": "Invalid action. Use: kill, status, restore", "success": False}), 400

# ---------- RENDER API ----------
RENDER_API_KEY = os.environ.get('RENDER_API_KEY', 'rnd_27v7iMggh7mafESEqJq1Lf12wIkF')
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

# ---------- DECORATORS (already defined earlier, but kept for clarity) ----------
# login_required and admin_required are defined at the top.

# ---------- BEFORE REQUEST ----------
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

# ========== FLASK ROUTES ==========
# ---------- MAIN ----------
@app.route('/')
def index():
    settings = load_settings()
    logged_in = 'username' in session
    is_admin = session.get('role') == 'admin' if logged_in else False
    username = session.get('username') if logged_in else ''
    user_password = ''
    if logged_in:
        user_obj = find_user(username)
        if user_obj:
            user_password = user_obj.get('password', '')
    logo_url = settings.get('logo', None)
    if logo_url:
        logo_url = logo_url + '?v=' + str(int(time.time()))
    return render_template_string(HTML_TEMPLATE,
                                   password=PASSWORD,
                                   website_name=settings.get('website_name', 'YUVICODEX'),
                                   logo_url=logo_url,
                                   social_links=settings.get('social_links', {}),
                                   logged_in=logged_in,
                                   is_admin=is_admin,
                                   username=username,
                                   user_password=user_password,
                                   MASTER_PASSWORD=MASTER_PASSWORD,
                                   SECRET_KEY=SECRET_KEY)

# ---------- SETTINGS ----------
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

# ---------- LOGIN / LOGOUT ----------
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = find_user(username) if username else None

    if user and user['password'] == password and not user.get('banned', False):
        if is_expired(user):
            delete_user_account(username)
            return jsonify({'success': False, 'error': 'Account expired and deleted'}), 401
        session['username'] = username
        session['role'] = user['role']
        session['session_version'] = user.get('session_version', 0)
        return jsonify({'success': True, 'username': username, 'role': user['role']})

    if password == MASTER_PASSWORD:
        admin_user = next((u for u in users_db if u['role'] == 'admin' and not u.get('banned', False)), None)
        if admin_user:
            if is_expired(admin_user):
                delete_user_account(admin_user['username'])
                return jsonify({'success': False, 'error': 'Admin account expired and deleted'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin user found'}), 401

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
                return jsonify({'success': False, 'error': 'Admin account expired and deleted'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin user found'}), 401
    return jsonify({'success': False, 'error': 'Invalid secret'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    session.pop('role', None)
    session.pop('session_version', None)
    return jsonify({'success': True})

# ---------- USER MANAGEMENT API ----------
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
    if new_password:
        user['password'] = new_password
    user['session_version'] = user.get('session_version', 0) + 1
    save_users(users_db)
    session.clear()
    return jsonify({'success': True, 'logout': True})

# ---------- BOT ROUTES ----------
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
    project_folder = os.path.join(get_user_folder(username), project_id)
    project_data_file = os.path.join(project_folder, 'project_data.json')
    if os.path.exists(project_data_file):
        try:
            os.remove(project_data_file)
        except:
            pass
    del bots_db[bot_id]
    save_bots()
    remaining_bots = [b for b in bots_db.values() if b['user'] == username and b['project'] == project_id]
    if not remaining_bots:
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
            return jsonify({'error': f'Exceeds total limit. You have {total_current} items, limit {limit}.'}), 400

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

# ---------- WEBSITE ROUTES ----------
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
    current_bots = len([b for b in bots_db.values() if b['user'] == username])
    with get_db() as conn:
        website_count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
    total_current = current_bots + website_count

    if total_current + 1 > limit:
        return jsonify({'error': f'Exceeds total limit. You have {total_current} items, limit {limit}.'}), 400

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

@app.route('/api/websites')
@login_required
def api_list_websites():
    username = session['username']
    with get_db() as conn:
        websites = conn.execute('SELECT * FROM websites WHERE owner_username = ? AND type = ? ORDER BY created_at DESC', (username, 'website')).fetchall()
    return jsonify([dict(row) for row in websites])

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
    filepath = os.path.join(folder, w['startup_file'] or 'app.py')
    if not os.path.exists(filepath):
        for f in os.listdir(folder):
            if os.path.isfile(os.path.join(folder, f)):
                filepath = os.path.join(folder, f)
                break
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content})

@app.route('/api/website/<int:website_id>/content', methods=['PUT'])
@login_required
def api_update_website_content(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Not found'}), 404
    data = request.json
    new_content = data.get('content', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    filepath = os.path.join(folder, w['startup_file'] or 'app.py')
    if not os.path.exists(filepath):
        for f in os.listdir(folder):
            if os.path.isfile(os.path.join(folder, f)):
                filepath = os.path.join(folder, f)
                break
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

@app.route('/deploy/<int:website_id>/logs')
@login_required
def deploy_logs_sse(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        abort(404)
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    if not dep:
        return "No deployment", 404
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
                yield f"data: [SYSTEM] Completed with status: {status['status']}\n\n"
                break
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/website/<int:website_id>/files')
@login_required
def website_files(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        abort(404)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        abort(404)
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
@login_required
def website_edit_file(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        abort(404)
    file_path = request.args.get('path', '').strip()
    if not file_path:
        return "No path", 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", file_path)
    if not os.path.exists(full) or not os.path.isfile(full):
        abort(404)
    if request.method == 'GET':
        with open(full, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return render_template_string(EDIT_TEMPLATE, website=w, file_path=file_path, content=content)
    else:
        with open(full, 'w', encoding='utf-8') as f:
            f.write(request.form.get('content', ''))
        return redirect(url_for('website_files', website_id=website_id))

@app.route('/website/<int:website_id>/file/upload', methods=['POST'])
@login_required
def website_upload_file(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Unauthorized'}), 401
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty'}), 400
    rel_path = request.form.get('path', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", rel_path)
    os.makedirs(folder, exist_ok=True)
    filename = secure_filename(file.filename)
    file.save(os.path.join(folder, filename))
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/delete', methods=['POST'])
@login_required
def website_delete_file(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Unauthorized'}), 401
    path = request.json.get('path', '').strip()
    if not path:
        return jsonify({'error': 'Path required'}), 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full):
        return jsonify({'error': 'Not found'}), 404
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/rename', methods=['POST'])
@login_required
def website_rename_file(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.json
    old_path = data.get('old_path', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_path or not new_name:
        return jsonify({'error': 'Required'}), 400
    base = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    old_full = os.path.join(base, old_path)
    if not os.path.exists(old_full):
        return jsonify({'error': 'Not found'}), 404
    new_full = os.path.join(os.path.dirname(old_full), new_name)
    if os.path.exists(new_full):
        return jsonify({'error': 'Already exists'}), 400
    os.rename(old_full, new_full)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/download', methods=['GET'])
@login_required
def website_download_file(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        abort(404)
    path = request.args.get('path', '').strip()
    if not path:
        abort(400)
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full) or os.path.isdir(full):
        abort(404)
    return send_file(full, as_attachment=True)

@app.route('/website/<int:website_id>/build')
@login_required
def build_logs_page(website_id):
    w = get_website_by_id(website_id)
    if not w or w['owner_username'] != session['username']:
        abort(404)
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    return render_template_string(BUILD_LOGS_TEMPLATE, website=w, no_logs=not dep)

# ---------- PROXY ----------
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def proxy_website(slug, path):
    website = get_website_by_slug(slug)
    if not website or website['type'] != 'website':
        return render_template_string(ERROR_TEMPLATE, message="Website not found", slug=slug), 404
    if website['status'] != 'running':
        return render_template_string(ERROR_TEMPLATE, message="Website is not running", slug=slug), 503
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
        return render_template_string(ERROR_TEMPLATE, message="Website crashed. Please restart.", slug=slug), 503
    except Exception as e:
        return f"Proxy error: {str(e)}", 500

# ---------- STATS ----------
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

# ---------- FILE MANAGER (ADMIN SYSTEM) ----------
def safe_path(path):
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR):
        return None
    return abs_path

@app.route('/api/files', methods=['GET'])
@admin_required
def list_files():
    path = request.args.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None:
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
    abs_path = safe_path(path)
    if abs_path is None:
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
    abs_old = safe_path(old_path)
    if abs_old is None:
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
    abs_path = safe_path(path)
    if abs_path is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(abs_path, as_attachment=True)

# ---------- INTERACTIVE TERMINAL ----------
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

# ---------- OLD TERMINAL (compatibility) ----------
@app.route('/execute', methods=['POST'])
def execute():
    data = request.json
    if data.get('password') != PASSWORD:
        return jsonify({"output": "Access Denied"})
    try:
        result = subprocess.check_output(data['command'], shell=True, stderr=subprocess.STDOUT, timeout=600)
        return jsonify({"output": result.decode('utf-8')})
    except subprocess.TimeoutExpired:
        return jsonify({"output": "Command timed out"})
    except Exception as e:
        return jsonify({"output": str(e)})

# ---------- CLI TOOLS ROUTES ----------
cli_processes = {}

@app.route('/api/cli_tools', methods=['GET'])
@login_required
def api_list_cli_tools():
    username = session['username']
    with get_db() as conn:
        if session.get('role') == 'admin':
            tools = conn.execute('SELECT * FROM cli_tools ORDER BY created_at DESC').fetchall()
        else:
            tools = conn.execute('SELECT * FROM cli_tools WHERE owner_username = ? ORDER BY created_at DESC',
                                (username,)).fetchall()
    return jsonify([dict(row) for row in tools])

@app.route('/upload_cli', methods=['POST'])
@login_required
def upload_cli():
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
    with get_db() as conn:
        bot_count = conn.execute('SELECT COUNT(*) FROM bots WHERE user = ?', (username,)).fetchone()[0]
        website_count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
        cli_count = conn.execute('SELECT COUNT(*) FROM cli_tools WHERE owner_username = ?', (username,)).fetchone()[0]
    total_current = bot_count + website_count + cli_count

    temp_dir = tempfile.mkdtemp()
    tool_id = str(uuid.uuid4())[:8]
    tool_folder = os.path.join(UPLOAD_FOLDER, f"cli_tool_{tool_id}")
    os.makedirs(tool_folder, exist_ok=True)

    try:
        zip_file = None
        for file in files:
            if file.filename == '':
                continue
            temp_path = os.path.join(temp_dir, file.filename)
            file.save(temp_path)
            if file.filename.lower().endswith('.zip'):
                zip_file = temp_path
            else:
                shutil.move(temp_path, os.path.join(tool_folder, file.filename))

        if zip_file:
            with zipfile.ZipFile(zip_file, 'r') as zf:
                zf.extractall(tool_folder)
            os.remove(zip_file)

        startup_file = None
        interpreter = None
        for root, dirs, files_in_folder in os.walk(tool_folder):
            for fname in files_in_folder:
                interp = get_interpreter(fname)
                if interp:
                    startup_file = fname
                    interpreter = interp
                    break
            if startup_file:
                break

        if not startup_file:
            shutil.rmtree(tool_folder, ignore_errors=True)
            return jsonify({'error': 'No executable file found'}), 400

        slug = f"cli_{tool_id}"
        with get_db() as conn:
            cur = conn.execute('''INSERT INTO cli_tools
                (owner_username, tool_name, tool_slug, tool_folder, startup_file, interpreter, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (username, startup_file, slug, tool_folder, startup_file, interpreter, 'stopped'))
            tool_db_id = cur.lastrowid
            conn.commit()

        return jsonify({'success': True, 'tool_id': tool_db_id, 'tool_slug': slug})

    except Exception as e:
        return jsonify({'error': str(e)}), 500
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

@app.route('/api/cli_tool/<int:tool_id>/start', methods=['POST'])
@login_required
def cli_tool_start(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404
    if tool['status'] == 'running':
        return jsonify({'error': 'Already running'}), 400

    tool_folder = tool['tool_folder']
    startup_file = tool['startup_file']
    interpreter = tool['interpreter']
    if not interpreter:
        interpreter = get_interpreter(startup_file)
    if not interpreter:
        return jsonify({'error': 'Unsupported interpreter'}), 400

    log_file = os.path.join(tool_folder, 'cli.log')
    os.makedirs(os.path.dirname(log_file), exist_ok=True)

    try:
        proc = subprocess.Popen(
            [interpreter, startup_file],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            stdin=subprocess.PIPE,
            cwd=tool_folder,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )

        with get_db() as conn:
            conn.execute('UPDATE cli_tools SET status = ?, pid = ?, last_started = CURRENT_TIMESTAMP WHERE id = ?',
                         ('running', proc.pid, tool_id))
            conn.commit()

        cli_processes[tool_id] = proc
        return jsonify({'success': True, 'pid': proc.pid})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cli_tool/<int:tool_id>/stop', methods=['POST'])
@login_required
def cli_tool_stop(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    pid = tool['pid']
    if pid:
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(pid), signal.SIGTERM)
            else:
                subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        except:
            pass

        if tool_id in cli_processes:
            del cli_processes[tool_id]

    with get_db() as conn:
        conn.execute('UPDATE cli_tools SET status = ?, pid = NULL, last_stopped = CURRENT_TIMESTAMP WHERE id = ?',
                     ('stopped', tool_id))
        conn.commit()

    return jsonify({'success': True})

@app.route('/api/cli_tool/<int:tool_id>/delete', methods=['POST'])
@login_required
def cli_tool_delete(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    if tool['status'] == 'running':
        cli_tool_stop(tool_id)

    tool_folder = tool['tool_folder']
    shutil.rmtree(tool_folder, ignore_errors=True)

    with get_db() as conn:
        conn.execute('DELETE FROM cli_tools WHERE id = ?', (tool_id,))
        conn.commit()

    return jsonify({'success': True})

@app.route('/api/cli_tool/<int:tool_id>/download', methods=['GET'])
@login_required
def cli_tool_download(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    tool_folder = tool['tool_folder']
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(tool_folder):
            for root, _, files_in_folder in os.walk(tool_folder):
                for fname in files_in_folder:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, tool_folder)
                    zipf.write(full_path, arcname)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"cli_tool_{tool_id}.zip")

@app.route('/api/cli_tool/<int:tool_id>/send_input', methods=['POST'])
@login_required
def cli_tool_send_input(tool_id):
    data = request.json
    input_data = data.get('input', '')
    proc = cli_processes.get(tool_id)
    if not proc:
        return jsonify({'error': 'Process not running'}), 400
    try:
        proc.stdin.write((input_data + '\n').encode())
        proc.stdin.flush()
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/cli_tool/<int:tool_id>/logs', methods=['GET'])
@login_required
def cli_tool_logs(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    log_file = os.path.join(tool['tool_folder'], 'cli.log')
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return jsonify({'logs': ''.join(lines[-200:])})
    return jsonify({'logs': ''})

@app.route('/api/cli_tool/<int:tool_id>/upload_file', methods=['POST'])
@login_required
def cli_tool_upload_file(tool_id):
    if 'file' not in request.files:
        return jsonify({'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400

    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?',
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    tool_folder = tool['tool_folder']
    filename = secure_filename(file.filename)
    file_path = os.path.join(tool_folder, filename)
    file.save(file_path)

    if tool_id in cli_processes:
        proc = cli_processes[tool_id]
        try:
            proc.stdin.write((file_path + '\n').encode())
            proc.stdin.flush()
        except:
            pass

    return jsonify({'success': True, 'filename': filename, 'filepath': file_path})

# ---------- MINI WEB ----------
MINI_WEB_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Mini Web - {{ name }}</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <style>
        * { margin:0; padding:0; box-sizing:border-box; font-family:'Segoe UI', system-ui, sans-serif; }
        body { background:#05070d; color:#fff; min-height:100vh; display:flex; justify-content:center; align-items:center; padding:20px; }
        .mini-container { max-width:500px; width:100%; background:rgba(255,255,255,0.03); backdrop-filter:blur(24px); border-radius:30px; padding:30px 25px; border:1px solid rgba(255,255,255,0.08); box-shadow:0 0 80px rgba(0,229,255,0.05); position:relative; overflow:hidden; }
        .mini-container::before { content:""; position:absolute; inset:-3px; background:conic-gradient(#00e5ff, transparent, transparent, transparent, #00e5ff); animation:spin 4s linear infinite; z-index:-1; }
        .mini-container::after { content:""; position:absolute; inset:3px; background:rgba(10,14,26,0.95); border-radius:27px; z-index:-1; }
        @keyframes spin { 100% { transform:rotate(360deg); } }
        .mini-header { display:flex; justify-content:space-between; align-items:center; margin-bottom:20px; padding-bottom:15px; border-bottom:1px solid rgba(255,255,255,0.06); }
        .mini-title { font-size:1.2rem; font-weight:800; background:linear-gradient(135deg, #00e5ff, #7a00ff); -webkit-background-clip:text; -webkit-text-fill-color:transparent; }
        .mini-badge { padding:4px 16px; border-radius:50px; font-size:0.7rem; font-weight:700; text-transform:uppercase; background:rgba(255,255,255,0.05); border:1px solid rgba(255,255,255,0.1); }
        .mini-badge.running { background:rgba(0,229,255,0.15); color:#00e5ff; border-color:rgba(0,229,255,0.2); box-shadow:0 0 12px rgba(0,229,255,0.15); }
        .mini-badge.stopped { background:rgba(255,71,87,0.15); color:#ff4757; border-color:rgba(255,71,87,0.2); }
        .mini-badge.failed { background:rgba(255,0,0,0.15); color:#ff0000; border-color:rgba(255,0,0,0.2); }
        .mini-info { background:rgba(255,255,255,0.03); border-radius:15px; padding:15px; margin-bottom:20px; }
        .mini-info .row { display:flex; justify-content:space-between; padding:6px 0; color:#aaa; font-size:0.9rem; }
        .mini-info .row .label { color:#666; }
        .mini-info .row .value { color:#ddd; font-weight:600; }
        .mini-controls { display:flex; gap:10px; margin-bottom:20px; flex-wrap:wrap; }
        .mini-controls button { flex:1; padding:12px 20px; border:none; border-radius:12px; font-weight:700; font-size:0.9rem; cursor:pointer; transition:all .3s cubic-bezier(.4,0,.2,1); min-width:80px; background:rgba(255,255,255,0.05); color:#aaa; border:1px solid rgba(255,255,255,0.08); }
        .mini-controls button:hover:not(:disabled) { transform:scale(1.05); box-shadow:0 8px 30px rgba(0,0,0,0.3); }
        .mini-controls button:active:not(:disabled) { transform:scale(0.94); box-shadow:0 4px 20px rgba(0,0,0,0.5); }
        .mini-controls button:disabled { opacity:0.4; cursor:not-allowed; }
        .mini-btn-start { background:rgba(0,229,255,0.12); color:#00e5ff; border-color:rgba(0,229,255,0.2); }
        .mini-btn-start:hover:not(:disabled) { background:#00e5ff; color:#000; border-color:#00e5ff; }
        .mini-btn-stop { background:rgba(255,71,87,0.12); color:#ff4757; border-color:rgba(255,71,87,0.2); }
        .mini-btn-stop:hover:not(:disabled) { background:#ff4757; color:#fff; border-color:#ff4757; }
        .mini-btn-restart { background:rgba(255,170,0,0.12); color:#ffaa00; border-color:rgba(255,170,0,0.2); }
        .mini-btn-restart:hover:not(:disabled) { background:#ffaa00; color:#000; border-color:#ffaa00; }
        .mini-logs { background:rgba(0,0,0,0.5); border-radius:12px; padding:12px 15px; min-height:100px; max-height:200px; overflow-y:auto; font-family:'Courier New', monospace; font-size:12px; color:#50fa7b; border:1px solid rgba(255,255,255,0.05); line-height:1.6; white-space:pre-wrap; }
        .mini-logs::-webkit-scrollbar { width:4px; }
        .mini-logs::-webkit-scrollbar-track { background:transparent; }
        .mini-logs::-webkit-scrollbar-thumb { background:#00e5ff; border-radius:4px; }
        .mini-logs .empty { color:#555; font-style:italic; }
        .mini-visit { margin-top:15px; text-align:center; }
        .mini-visit a { color:#00e5ff; text-decoration:none; font-weight:600; padding:10px 25px; border:1px solid rgba(0,229,255,0.2); border-radius:50px; transition:.3s; display:inline-block; background:rgba(0,229,255,0.05); }
        .mini-visit a:hover { background:#00e5ff; color:#000; border-color:#00e5ff; transform:scale(1.02); }
        .mini-footer { margin-top:20px; text-align:center; font-size:0.7rem; color:#444; border-top:1px solid rgba(255,255,255,0.04); padding-top:15px; }
        @media(max-width:480px){ .mini-controls { flex-direction:column; } .mini-controls button { flex:none; width:100%; } }
    </style>
</head>
<body>
    <div class="mini-container">
        <div class="mini-header">
            <span class="mini-title"><i class="fa-solid fa-{% if type == 'website' %}globe{% else %}robot{% endif %}"></i> {{ name }}</span>
            <span class="mini-badge {{ status }}">● {{ status.upper() }}</span>
        </div>
        <div class="mini-info">
            <div class="row"><span class="label">Type</span><span class="value">{{ type.upper() }}</span></div>
            <div class="row"><span class="label">Status</span><span class="value">{{ status.upper() }}</span></div>
            {% if port %}<div class="row"><span class="label">Port</span><span class="value">{{ port }}</span></div>{% endif %}
            {% if type == 'bot' and item.get('bot_username') %}<div class="row"><span class="label">Bot</span><span class="value">@{{ item.bot_username }}</span></div>{% endif %}
        </div>
        <div class="mini-controls">
            <button class="mini-btn-start" id="miniStart" {% if status == 'running' %}disabled{% endif %}>▶ START</button>
            <button class="mini-btn-stop" id="miniStop" {% if status != 'running' %}disabled{% endif %}>⏹ STOP</button>
            <button class="mini-btn-restart" id="miniRestart">⟳ RESTART</button>
        </div>
        <div class="mini-logs" id="miniLogs"><span class="empty">Loading logs...</span></div>
        {% if type == 'website' and slug %}<div class="mini-visit"><a href="/{{ slug }}/" target="_blank"><i class="fa-solid fa-up-right-from-square"></i> Visit Website</a></div>{% endif %}
        <div class="mini-footer">YUVICODEX Mini Web • {{ type }} • {{ name }}</div>
    </div>
    <script>
        const type = '{{ type }}';
        const id = '{{ item_id }}';
        let logInterval = null;
        function loadLogs() {
            fetch(`/mini/api/${type}/${id}/logs`).then(r=>r.json()).then(data=>{
                const logs=document.getElementById('miniLogs');
                if(data.logs){logs.textContent=data.logs;}else{logs.innerHTML='<span class="empty">No logs yet.</span>';}
                logs.scrollTop=logs.scrollHeight;
            }).catch(()=>{});
        }
        function action(action) {
            const btns=document.querySelectorAll('.mini-controls button');
            btns.forEach(b=>b.disabled=true);
            fetch(`/mini/api/${type}/${id}/${action}`,{method:'POST',headers:{'Content-Type':'application/json'}})
            .then(r=>r.json()).then(data=>{
                if(data.success){location.reload();}else{alert('Error: '+data.error);}
            }).catch(()=>alert('Network error')).finally(()=>{btns.forEach(b=>b.disabled=false);});
        }
        document.getElementById('miniStart').addEventListener('click',()=>action('start'));
        document.getElementById('miniStop').addEventListener('click',()=>action('stop'));
        document.getElementById('miniRestart').addEventListener('click',()=>action('restart'));
        loadLogs(); logInterval=setInterval(loadLogs,2000);
        window.addEventListener('beforeunload',()=>{if(logInterval)clearInterval(logInterval);});
    </script>
</body></html>
"""

MINI_WEB_ERROR = """
<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.05);padding:40px;border-radius:20px;text-align:center}h1{color:#ff4757}</style>
</head><body><div class="card"><h1>{{ message }}</h1></div></body></html>
"""

@app.route('/mini/<string:type>/<string:id>')
def mini_web_view(type, id):
    if type == 'website':
        website = get_website_by_id(int(id))
        if not website:
            return render_template_string(MINI_WEB_ERROR, message="Website not found"), 404
        return render_template_string(MINI_WEB_TEMPLATE,
                                   item=website,
                                   type='website',
                                   name=website['website_name'] or website['website_slug'],
                                   status=website['status'],
                                   port=website['allocated_port'],
                                   slug=website['website_slug'],
                                   item_id=website['id'])
    elif type == 'bot':
        bot = bots_db.get(id)
        if not bot:
            return render_template_string(MINI_WEB_ERROR, message="Bot not found"), 404
        filepath = get_bot_absolute_path(bot)
        token, bot_username = detect_bot_token(filepath) if os.path.exists(filepath) else (None, None)
        bot['bot_username'] = bot_username
        return render_template_string(MINI_WEB_TEMPLATE,
                                   item=bot,
                                   type='bot',
                                   name=bot['filename'],
                                   status=bot['status'],
                                   port=None,
                                   slug=None,
                                   item_id=id)
    return render_template_string(MINI_WEB_ERROR, message="Invalid type"), 404

@app.route('/mini/api/<string:type>/<string:id>/<string:action>', methods=['POST'])
def mini_web_action(type, id, action):
    if type == 'website':
        website = get_website_by_id(int(id))
        if not website:
            return jsonify({'error': 'Not found'}), 404
        if action == 'start':
            ok, msg = start_website_process(int(id))
        elif action == 'stop':
            ok, msg = stop_website_process(int(id))
        elif action == 'restart':
            stop_website_process(int(id))
            time.sleep(1)
            ok, msg = start_website_process(int(id))
        else:
            return jsonify({'error': 'Invalid action'}), 400
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 500
    elif type == 'bot':
        if action == 'start':
            ok, msg = start_bot_by_id(id)
        elif action == 'stop':
            ok, msg = stop_bot_by_id(id)
        elif action == 'restart':
            stop_bot_by_id(id)
            time.sleep(1)
            ok, msg = start_bot_by_id(id)
        else:
            return jsonify({'error': 'Invalid action'}), 400
        if ok:
            return jsonify({'success': True, 'message': msg})
        return jsonify({'success': False, 'error': msg}), 500
    return jsonify({'error': 'Invalid type'}), 400

@app.route('/mini/api/<string:type>/<string:id>/logs')
def mini_web_logs(type, id):
    if type == 'website':
        website = get_website_by_id(int(id))
        if not website:
            return jsonify({'error': 'Not found'}), 404
        log_file = os.path.join(LOG_FOLDER, f"website_{id}.log")
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                lines = f.readlines()
            return jsonify({'logs': ''.join(lines[-50:])})
        return jsonify({'logs': ''})
    elif type == 'bot':
        bot = bots_db.get(id)
        if not bot:
            return jsonify({'error': 'Not found'}), 404
        log_file = get_bot_log_file(bot)
        if os.path.exists(log_file):
            with open(log_file, 'r') as f:
                lines = f.readlines()
            return jsonify({'logs': ''.join(lines[-50:])})
        return jsonify({'logs': ''})
    return jsonify({'error': 'Invalid type'}), 400

# ---------- BOT BUILD LOGS (SSE) ----------
@app.route('/bot/<bot_id>/build')
@login_required
def bot_build_logs_page(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        abort(404)
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        abort(403)
    return render_template_string(BOT_BUILD_LOGS_TEMPLATE, bot=bot)

BOT_BUILD_LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Bot Build Logs</title>
<style>body{background:#0a0e1a;color:#fff;height:100vh;display:flex;flex-direction:column;padding:20px;overflow:hidden}.top-bar{display:flex;justify-content:space-between;padding:10px 20px;background:rgba(255,255,255,0.05);border-radius:15px;margin-bottom:15px}.terminal{flex:1;background:#0d0d0d;border-radius:15px;padding:20px;overflow-y:auto;font-family:monospace;color:#0f0}</style>
</head><body><div class="top-bar"><h2>Bot Logs: {{ bot.filename }}</h2><a href="/dashboard">← Dashboard</a></div><div class="terminal" id="terminal"><div id="logContainer">Loading logs...</div></div>
<script>
const evt = new EventSource('/deploy/bot/{{ bot.id }}/logs');
evt.onmessage = function(e) {
    const div = document.createElement('div');
    div.textContent = e.data;
    document.getElementById('logContainer').appendChild(div);
    document.getElementById('terminal').scrollTop = document.getElementById('terminal').scrollHeight;
};
</script></body></html>
"""

@app.route('/deploy/bot/<bot_id>/logs')
@login_required
def deploy_bot_logs_sse(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        abort(404)
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        abort(403)
    log_file = get_bot_log_file(bot)
    if not os.path.exists(log_file):
        return "No log file", 404

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
            bot_refresh = bots_db.get(bot_id)
            if bot_refresh and bot_refresh['status'] != 'running':
                yield f"data: [SYSTEM] Bot stopped.\n\n"
                break
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# ---------- TEMPLATES ----------
ERROR_TEMPLATE = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.05);padding:40px;border-radius:20px;text-align:center}h1{color:#ff4757}a{color:#00e5ff;text-decoration:none}</style>
</head><body><div class="card"><h1>{{ message }}</h1><p>Slug: {{ slug }}</p><a href="/dashboard">← Dashboard</a></div></body></html>"""

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

# ---------- MAIN HTML TEMPLATE (with CLI Tab, Mini Web, Build Logs buttons) ----------
HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>{{ website_name }} · Admin Panel</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <style>
        /* ---------- RESET & BASE ---------- */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Arial', sans-serif;
        }

        body {
            background: #05070d;
            color: #fff;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }

        .view {
            display: none;
            width: 100%;
            max-width: 420px;
            margin: 0 auto;
        }
        .view.active {
            display: block;
        }

        /* ---------- LOGIN CARD ---------- */
        .login-card {
            position: relative;
            width: 100%;
            padding: 30px 20px;
            background: #0c1018;
            border-radius: 25px;
            overflow: hidden;
            box-shadow: 0 0 20px rgba(0, 0, 0, .5);
        }
        .login-card::before {
            content: "";
            position: absolute;
            inset: -3px;
            background: conic-gradient(#00e5ff, transparent, transparent, transparent, #00e5ff);
            animation: spin 4s linear infinite;
        }
        .login-card::after {
            content: "";
            position: absolute;
            inset: 3px;
            background: #0c1018;
            border-radius: 22px;
        }
        .login-content {
            position: relative;
            z-index: 2;
        }
        .login-icon {
            width: 110px;
            height: 110px;
            margin: auto;
            border: 3px solid #00e5ff;
            border-radius: 50%;
            display: flex;
            justify-content: center;
            align-items: center;
            font-size: 45px;
            color: #00e5ff;
            box-shadow: 0 0 20px #00e5ff;
            overflow: hidden;
            background: #0c1018;
            cursor: pointer;
            transition: transform 0.1s;
            user-select: none;
        }
        .login-icon:active {
            transform: scale(0.95);
        }
        .login-icon img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            border-radius: 50%;
        }
        .login-title {
            margin: 25px 0;
            text-align: center;
            color: #cfffff;
            letter-spacing: 4px;
            font-size: 1.3rem;
        }
        .login-card select,
        .login-card input {
            width: 100%;
            margin: 12px 0;
            padding: 16px;
            background: #161b25;
            border: 1px solid #2b3240;
            border-radius: 15px;
            color: white;
            font-size: 16px;
            outline: none;
        }
        .login-card select option {
            background: #161b25;
        }
        .login-btn {
            width: 100%;
            margin-top: 20px;
            padding: 16px;
            border: none;
            border-radius: 15px;
            font-size: 18px;
            font-weight: bold;
            color: white;
            cursor: pointer;
            background: linear-gradient(90deg, #7a00ff, #00d9ff);
            transition: opacity 0.2s;
        }
        .login-btn:hover {
            opacity: .9;
        }
        .login-error {
            color: #ff4d4d;
            text-align: center;
            font-size: 14px;
            margin-top: 10px;
            min-height: 22px;
        }
        @keyframes spin {
            100% {
                transform: rotate(360deg);
            }
        }

        /* ---------- USER DASHBOARD ---------- */
        .user-container {
            max-width: 400px;
            width: 100%;
            margin: 0 auto;
        }

        .user-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .user-title {
            letter-spacing: 3px;
            font-weight: 800;
            font-size: 1.2rem;
        }
        .hamburger {
            font-size: 28px;
            cursor: pointer;
            color: #fff;
            padding: 4px 8px;
            border-radius: 8px;
            transition: background 0.2s;
            user-select: none;
        }
        .hamburger:hover {
            background: rgba(255, 255, 255, 0.08);
        }
        .power-btn {
            color: #ff4d4d;
            font-size: 20px;
            cursor: pointer;
        }
        .user-header-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        /* Tabs */
        .tabs {
            display: flex;
            gap: 10px;
            margin: 15px 0 10px 0;
            border-bottom: 1px solid rgba(255,255,255,0.1);
            padding-bottom: 10px;
            flex-wrap: wrap;
        }
        .tab-btn {
            background: transparent;
            border: none;
            color: #888;
            font-size: 1rem;
            font-weight: 700;
            padding: 8px 16px;
            cursor: pointer;
            transition: .3s;
            border-radius: 10px;
        }
        .tab-btn:hover {
            color: #fff;
            background: rgba(255,255,255,0.05);
        }
        .tab-btn.active {
            color: #00e5ff;
            background: rgba(0,229,255,0.1);
        }
        .tab-content {
            display: none;
        }
        .tab-content.active {
            display: block;
        }

        /* Upload Cards */
        .upload-card {
            border: 1px dashed #00e5ff;
            border-radius: 15px;
            padding: 20px;
            text-align: center;
            background: rgba(0, 229, 255, 0.05);
            position: relative;
            cursor: pointer;
            margin-bottom: 15px;
        }
        .upload-card .settings-icon {
            position: absolute;
            top: 15px;
            right: 15px;
            border: 1px solid #00e5ff;
            padding: 5px 8px;
            border-radius: 6px;
            font-size: 14px;
            color: #00e5ff;
            cursor: pointer;
        }
        .cloud-icon {
            font-size: 40px;
            margin-bottom: 10px;
            color: #00e5ff;
        }
        .upload-card>div:nth-child(3) {
            color: #aaa;
            font-size: 14px;
        }
        .deploy-btn {
            background: #fff;
            color: #000;
            padding: 15px;
            border-radius: 10px;
            font-weight: 900;
            margin-top: 15px;
            text-transform: uppercase;
            cursor: pointer;
            border: none;
            width: 100%;
            font-size: 14px;
        }
        #fileCountDisplay {
            font-size: 12px;
            color: #888;
            margin-top: 8px;
        }

        /* Bot Cards - Matching Style */
        #botListContainer,
        #cliToolListContainer {
            margin-top: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .bot-card {
            background: #111;
            border: 1px solid #333;
            border-radius: 15px;
            padding: 15px;
            transition: border-color 0.2s;
            cursor: pointer;
        }
        .bot-card:hover {
            border-color: #555;
        }
        .bot-card.selected {
            border-color: #00e5ff;
        }
        .bot-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .bot-name {
            font-weight: bold;
            font-size: 16px;
        }
        .bot-status {
            font-size: 12px;
            padding: 2px 12px;
            border-radius: 12px;
            font-weight: bold;
        }
        .bot-status.running {
            background: #00ff6a33;
            color: #00ff6a;
            border: 1px solid #00ff6a;
            box-shadow: 0 0 10px rgba(0,255,100,0.2);
        }
        .bot-status.stopped {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }
        .bot-uptime {
            font-size: 12px;
            color: #888;
            margin-bottom: 10px;
            font-family: monospace;
        }
        .bot-owner {
            font-size: 11px;
            color: #888;
            margin-bottom: 8px;
        }
        .bot-controls,
        .cli-tool-controls {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        .bot-controls button,
        .cli-tool-controls button {
            border: none;
            padding: 10px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            font-size: 12px;
            transition: background 0.2s, opacity 0.2s, transform 0.1s;
        }
        .bot-controls button:active:not(:disabled),
        .cli-tool-controls button:active:not(:disabled) {
            transform: scale(0.94);
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        }
        .bot-controls button:disabled,
        .cli-tool-controls button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .btn-start {
            background: #00d4ff;
            color: #000;
        }
        .btn-start:hover:not(:disabled) {
            background: #00c0e6;
        }
        .btn-stop {
            background: #ff4d4d;
            color: #fff;
        }
        .btn-stop:hover:not(:disabled) {
            background: #e64444;
        }
        .btn-edit {
            background: #4d88ff;
            color: #fff;
        }
        .btn-restart {
            background: #ffaa00;
            color: #000;
        }
        .btn-download {
            background: #2ecc71;
            color: #000;
        }
        .btn-delete {
            background: #400;
            color: #fff;
        }
        .btn-openbot {
            background: #1da1f2;
            color: #fff;
            grid-column: span 2;
            padding: 10px;
            border-radius: 8px;
            border: none;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            transition: background 0.2s;
        }
        .btn-openbot:hover {
            background: #1a8cd8;
        }
        .btn-full {
            grid-column: span 2;
            background: #222;
            color: #fff;
            margin-top: 5px;
        }
        .btn-full.danger {
            background: #400;
        }
        /* Mini Web & Build Logs buttons */
        .btn-miniweb {
            background: #8e44ad;
            color: #fff;
            grid-column: span 2;
        }
        .btn-miniweb:hover {
            background: #7d3c98;
        }
        .btn-buildlogs {
            background: #8e44ad;
            color: #fff;
        }
        .btn-buildlogs:hover {
            background: #7d3c98;
        }
        .btn-buildlogs-w {
            background: #8e44ad;
            color: #fff;
        }
        .btn-buildlogs-w:hover {
            background: #7d3c98;
        }

        /* Website Cards - Matching Bot Card Style */
        .website-grid {
            display: flex;
            flex-direction: column;
            gap: 16px;
            margin-top: 20px;
        }
        .website-card {
            background: #111;
            border: 1px solid #333;
            border-radius: 15px;
            padding: 15px;
            transition: border-color 0.2s;
            cursor: pointer;
        }
        .website-card:hover {
            border-color: #555;
        }
        .website-card.selected {
            border-color: #00e5ff;
        }
        .website-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 8px;
        }
        .website-name {
            font-weight: bold;
            font-size: 16px;
        }
        .website-status {
            font-size: 12px;
            padding: 2px 12px;
            border-radius: 12px;
            font-weight: bold;
        }
        .website-status.running {
            background: #00ff6a33;
            color: #00ff6a;
            border: 1px solid #00ff6a;
            box-shadow: 0 0 10px rgba(0,255,100,0.2);
        }
        .website-status.stopped {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }
        .website-status.failed {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }
        .website-slug {
            color: #888;
            font-size: 0.9rem;
            margin: 3px 0;
        }
        .website-port {
            color: #888;
            font-size: 0.8rem;
        }
        .website-uptime {
            font-size: 12px;
            color: #888;
            margin-bottom: 10px;
            font-family: monospace;
        }
        .website-actions {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
            margin-top: 10px;
        }
        .website-actions button {
            border: none;
            padding: 10px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            font-size: 12px;
            transition: background 0.2s, opacity 0.2s, transform 0.1s;
        }
        .website-actions button:active:not(:disabled) {
            transform: scale(0.94);
            box-shadow: 0 2px 10px rgba(0,0,0,0.3);
        }
        .website-actions button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .btn-start-w {
            background: #00d4ff;
            color: #000;
        }
        .btn-start-w:hover:not(:disabled) {
            background: #00c0e6;
        }
        .btn-stop-w {
            background: #ff4d4d;
            color: #fff;
        }
        .btn-stop-w:hover:not(:disabled) {
            background: #e64444;
        }
        .btn-restart-w {
            background: #ffaa00;
            color: #000;
        }
        .btn-restart-w:hover:not(:disabled) {
            background: #e69900;
        }
        .btn-delete-w {
            background: #400;
            color: #fff;
        }
        .btn-delete-w:hover:not(:disabled) {
            background: #600;
        }
        .btn-edit-w {
            background: #4d88ff;
            color: #fff;
        }
        .btn-edit-w:hover:not(:disabled) {
            background: #3d78ef;
        }
        .btn-download-w {
            background: #2ecc71;
            color: #000;
        }
        .btn-download-w:hover:not(:disabled) {
            background: #27ae60;
        }
        .btn-files-w {
            background: #555;
            color: #fff;
        }
        .btn-files-w:hover:not(:disabled) {
            background: #666;
        }
        .btn-buildlogs-w {
            background: #8e44ad;
            color: #fff;
        }
        .btn-buildlogs-w:hover:not(:disabled) {
            background: #7d3c98;
        }
        .btn-visit-w {
            background: #1da1f2;
            color: #fff;
        }
        .btn-visit-w:hover:not(:disabled) {
            background: #1a8cd8;
        }
        .btn-miniweb-w {
            background: #8e44ad;
            color: #fff;
        }
        .btn-miniweb-w:hover:not(:disabled) {
            background: #7d3c98;
        }
        .name-edit {
            display: flex;
            gap: 8px;
            margin-top: 12px;
        }
        .name-edit input {
            flex: 1;
            padding: 8px 12px;
            background: rgba(255,255,255,0.05);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 12px;
            color: #fff;
            outline: none;
            font-size: 0.85rem;
        }
        .name-edit input:focus {
            border-color: #00e5ff;
        }
        .name-edit button {
            padding: 8px 16px;
            background: #00e5ff;
            border: none;
            border-radius: 12px;
            color: #000;
            font-weight: 600;
            cursor: pointer;
        }

        /* Console */
        .console-wrapper {
            display: flex;
            align-items: stretch;
            gap: 8px;
            margin-top: 15px;
        }
        .console {
            background: #000;
            color: #00ff6a;
            padding: 10px;
            font-family: monospace;
            font-size: 10px;
            border-radius: 8px;
            height: 100px;
            overflow-y: auto;
            border: 1px solid #333;
            line-height: 1.6;
            white-space: pre-wrap;
            flex: 1;
        }
        .copy-console-btn {
            background: transparent;
            border: none;
            color: #00e5ff;
            font-size: 24px;
            cursor: pointer;
            padding: 0 8px;
            display: flex;
            align-items: center;
            transition: transform 0.1s;
        }
        .copy-console-btn:hover {
            transform: scale(1.1);
        }

        /* Footer */
        .user-footer {
            text-align: center;
            margin-top: 30px;
        }
        .f-title {
            font-size: 22px;
            font-weight: 900;
            letter-spacing: 5px;
        }
        .f-sub {
            font-size: 11px;
            opacity: 0.6;
            margin-bottom: 15px;
        }
        .social-box {
            display: flex;
            justify-content: center;
            gap: 20px;
        }
        .social-box a {
            color: #fff;
            font-size: 20px;
            width: 40px;
            height: 40px;
            border: 1px solid #333;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
            transition: border-color 0.2s;
        }
        .social-box a:hover {
            border-color: #00e5ff;
        }

        /* ---------- ADMIN OVERLAY (DRAWER) ---------- */
        .admin-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 999;
            justify-content: flex-end;
            animation: fadeIn 0.25s ease;
        }
        .admin-overlay.open {
            display: flex;
        }

        .admin-drawer {
            width: 100%;
            max-width: 480px;
            height: 100%;
            background: #0c1018;
            padding: 24px 20px;
            overflow-y: auto;
            box-shadow: -10px 0 30px rgba(0, 0, 0, 0.8);
            animation: slideIn 0.3s ease;
            display: flex;
            flex-direction: column;
        }

        .admin-drawer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 1px solid #222;
        }
        .admin-drawer-header h2 {
            color: #00e5ff;
            font-size: 1.2rem;
            letter-spacing: 2px;
        }
        .admin-close-btn {
            background: none;
            border: none;
            color: #ff4d4d;
            font-size: 28px;
            cursor: pointer;
            padding: 0 6px;
        }

        .admin-tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .admin-tabs button {
            flex: 1;
            padding: 12px;
            border: 1px solid #333;
            border-radius: 10px;
            background: transparent;
            color: #aaa;
            font-weight: bold;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
            min-width: 80px;
        }
        .admin-tabs button.active {
            background: #00e5ff22;
            border-color: #00e5ff;
            color: #00e5ff;
        }
        .admin-tabs button:hover {
            border-color: #555;
        }

        .admin-panel-content {
            flex: 1;
        }
        .admin-tab-content {
            display: none;
        }
        .admin-tab-content.active {
            display: block;
        }

        /* ---------- ADMIN USER CARDS ---------- */
        .list-item {
            background: #111;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .list-item .row {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
        }
        .list-item .row .info {
            flex: 1;
            min-width: 120px;
        }
        .list-item .info .uname {
            font-weight: 700;
            font-size: 15px;
            color: #fff;
        }
        .list-item .info .upass {
            font-size: 13px;
            color: #888;
            font-family: monospace;
        }
        .badge-role {
            font-size: 10px;
            padding: 2px 10px;
            border-radius: 20px;
            font-weight: bold;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .badge-role.admin {
            background: #00e5ff33;
            color: #00e5ff;
            border: 1px solid #00e5ff55;
        }
        .badge-role.user {
            background: #444;
            color: #ccc;
            border: 1px solid #555;
        }
        .badge-role.banned {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }

        .limit-group {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .limit-group label {
            color: #aaa;
            font-size: 13px;
            font-weight: bold;
        }
        .list-item .limit-input {
            width: 70px;
            background: #1a1a1a;
            border: 1px solid #333;
            color: #fff;
            padding: 8px 6px;
            border-radius: 5px;
            font-size: 13px;
            outline: none;
            text-align: center;
        }
        .list-item .limit-input:focus {
            border-color: #00e5ff;
        }

        .btn-action {
            border: none;
            cursor: pointer;
            font-weight: bold;
            border-radius: 5px;
            padding: 8px 14px;
            font-size: 12px;
            white-space: nowrap;
        }
        .btn-set {
            background: #00e5ff33;
            color: #00e5ff;
            border: 1px solid #00e5ff55;
        }
        .btn-set:hover {
            background: #00e5ff55;
        }
        .btn-ban {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }
        .btn-ban:hover {
            background: #ff4d4d33;
        }
        .btn-reset {
            background: #333;
            color: #fff;
            border: 1px solid #555;
        }
        .btn-reset:hover {
            background: #444;
        }
        .btn-del {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            width: 100%;
            padding: 10px;
            text-align: center;
        }
        .btn-del:hover {
            background: #ff4d4d33;
        }
        .btn-create {
            background: #00e5ff;
            color: #000;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            font-size: 13px;
        }
        .btn-create:hover {
            opacity: 0.9;
        }
        .btn-remove {
            background: transparent;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 12px;
        }
        .btn-remove:hover {
            background: #ff4d4d22;
        }

        /* Create user form */
        #createUserForm {
            display: none;
            background: #1a1a1a;
            padding: 16px;
            border-radius: 12px;
            margin-bottom: 20px;
            border: 1px solid #2a2a2a;
        }
        #createUserForm input,
        #createUserForm select {
            background: #0c1018;
            border: 1px solid #333;
            color: #fff;
            padding: 12px;
            border-radius: 8px;
            width: 100%;
            margin-bottom: 10px;
            outline: none;
            font-size: 14px;
        }
        #createUserForm input:focus,
        #createUserForm select:focus {
            border-color: #00e5ff;
        }
        .create-row {
            display: flex;
            gap: 10px;
        }
        .create-row input {
            flex: 1;
        }

        /* Simple list for User Menu tab */
        .simple-list-item {
            background: #111;
            border: 1px solid #2a2a2a;
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .simple-list-item .info {
            display: flex;
            flex-direction: column;
        }
        .simple-list-item .info .uname {
            font-weight: 700;
            font-size: 14px;
            color: #fff;
        }
        .simple-list-item .info .upass {
            font-size: 12px;
            color: #888;
            font-family: monospace;
        }
        .simple-list-item .actions button {
            background: transparent;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 12px;
        }
        .simple-list-item .actions button:hover {
            background: #ff4d4d22;
        }

        .section-title {
            color: #00e5ff;
            font-size: 14px;
            font-weight: bold;
            margin: 18px 0 10px 0;
            border-bottom: 1px solid #222;
            padding-bottom: 6px;
        }

        .empty-msg {
            text-align: center;
            color: #555;
            padding: 20px 0;
            font-size: 14px;
        }

        /* ---------- TERMINAL ---------- */
        .terminal-box {
            background: #010409;
            color: #50fa7b;
            height: 350px;
            overflow-y: scroll;
            padding: 12px;
            border: 1px solid #30363d;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            white-space: pre-wrap;
            border-radius: 6px;
            margin-bottom: 10px;
            line-height: 1.6;
        }
        .terminal-box .prompt {
            color: #58a6ff;
        }
        .terminal-box .output {
            color: #50fa7b;
        }
        .terminal-box .error {
            color: #ff6b6b;
        }
        .terminal-controls {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .terminal-controls input {
            flex: 1;
            background: #0d1117;
            border: 1px solid #30363d;
            color: white;
            padding: 14px;
            border-radius: 6px;
            font-size: 16px;
            outline: none;
            min-width: 150px;
        }
        .terminal-controls input:focus {
            border-color: #00e5ff;
        }
        .terminal-controls button {
            padding: 12px 20px;
            border: none;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            font-size: 14px;
        }
        .btn-term-run {
            background: #238636;
            color: white;
        }
        .btn-term-run:hover {
            background: #2ea043;
        }
        .btn-term-stop {
            background: #da3633;
            color: white;
        }
        .btn-term-stop:hover {
            background: #f85149;
        }
        .btn-term-clear {
            background: #555;
            color: white;
        }
        .btn-term-clear:hover {
            background: #666;
        }

        /* ---------- CLI TERMINAL ---------- */
        .cli-terminal-container {
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 15px;
            margin-top: 20px;
            overflow: hidden;
        }
        .cli-terminal-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px 20px;
            background: rgba(255,255,255,.03);
            border-bottom: 1px solid #30363d;
            font-size: 14px;
        }
        .cli-terminal {
            background: #010409;
            color: #50fa7b;
            padding: 15px 20px;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            min-height: 300px;
            max-height: 400px;
            overflow-y: auto;
            white-space: pre-wrap;
            line-height: 1.6;
        }
        .cli-terminal-input-row {
            display: flex;
            gap: 10px;
            padding: 12px 20px;
            background: rgba(255,255,255,.02);
            border-top: 1px solid #30363d;
            flex-wrap: wrap;
        }
        .cli-terminal-input-row input {
            flex: 1;
            background: #0d1117;
            border: 1px solid #30363d;
            border-radius: 8px;
            color: #fff;
            padding: 12px 16px;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            outline: none;
            min-width: 100px;
        }
        .cli-terminal-input-row input:focus { border-color: #00e5ff; }
        .cli-terminal-input-row input:disabled { opacity: .5; }
        .cli-terminal-input-row button {
            padding: 12px 20px;
            border: none;
            border-radius: 8px;
            font-weight: 600;
            cursor: pointer;
            transition: .2s;
        }
        .cli-terminal-input-row button:disabled { opacity: .5; cursor: not-allowed; }
        #cliSendBtn { background: #238636; color: #fff; }
        #cliSendBtn:hover:not(:disabled) { background: #2ea043; }
        #cliClearBtn { background: #555; color: #fff; }
        #cliClearBtn:hover { background: #666; }
        .btn-upload-file { background: #7a00ff; color: #fff; }
        .btn-upload-file:hover:not(:disabled) { background: #9a2aff; }

        /* ---------- CUSTOM MODAL ---------- */
        .custom-modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 10001;
            justify-content: center;
            align-items: center;
            animation: fadeIn 0.2s ease;
        }
        .custom-modal-overlay.open {
            display: flex;
        }

        .custom-modal {
            background: #0c1018;
            border: 1px solid #2a2a2a;
            border-radius: 20px;
            padding: 30px 28px;
            max-width: 500px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
            text-align: left;
        }
        .custom-modal .modal-icon {
            font-size: 40px;
            margin-bottom: 12px;
            text-align: center;
            color: #00e5ff;
        }
        .custom-modal .modal-body {
            color: #eee;
            font-size: 15px;
            line-height: 1.6;
            margin-bottom: 24px;
        }
        .custom-modal .modal-body textarea {
            width: 100%;
            background: #050807;
            color: #00ff88;
            border: 1px solid #333;
            border-radius: 6px;
            padding: 10px;
            font-family: 'Courier New', monospace;
            font-size: 0.7rem;
            resize: vertical;
            tab-size: 4;
            min-height: 200px;
        }
        .custom-modal .modal-actions {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
            flex-wrap: wrap;
        }
        .custom-modal .modal-actions button {
            padding: 12px 28px;
            border: none;
            border-radius: 10px;
            font-weight: bold;
            font-size: 15px;
            cursor: pointer;
            min-width: 100px;
            transition: background 0.2s;
        }
        .custom-modal .modal-actions .btn-confirm {
            background: #00e5ff;
            color: #000;
        }
        .custom-modal .modal-actions .btn-confirm:hover {
            background: #00d4f0;
        }
        .custom-modal .modal-actions .btn-cancel {
            background: #333;
            color: #fff;
            border: 1px solid #555;
        }
        .custom-modal .modal-actions .btn-cancel:hover {
            background: #444;
        }
        .custom-modal .modal-actions .btn-ok {
            background: #00e5ff;
            color: #000;
            width: 100%;
        }
        .custom-modal .modal-actions .btn-ok:hover {
            background: #00d4f0;
        }
        .custom-modal .modal-body .btn-sm {
            padding: 6px 14px;
            font-size: 0.55rem;
            border: 1px solid #33ddff;
            color: #33ddff;
            background: transparent;
            border-radius: 6px;
            cursor: pointer;
        }

        /* ---------- SETTINGS MODAL (from gear icon) ---------- */
        #settingsModalOverlay {
            z-index: 9999;
        }

        .settings-form label {
            display: block;
            color: #aaa;
            font-size: 13px;
            margin-top: 15px;
            margin-bottom: 4px;
        }
        .settings-form input[type="text"],
        .settings-form input[type="file"] {
            width: 100%;
            background: #161b25;
            border: 1px solid #2b3240;
            color: white;
            padding: 12px;
            border-radius: 8px;
            outline: none;
            font-size: 14px;
        }
        .settings-form input:focus {
            border-color: #00e5ff;
        }
        .settings-form .logo-preview {
            margin-top: 10px;
            max-width: 100px;
            max-height: 100px;
            border-radius: 50%;
            border: 2px solid #00e5ff;
        }
        .settings-form .btn-remove-logo {
            background: #ff3333;
            color: #fff;
            border: none;
            padding: 8px 16px;
            border-radius: 5px;
            cursor: pointer;
            margin-top: 8px;
        }
        .settings-form .btn-remove-logo:hover {
            background: #cc0000;
        }

        /* ---------- FILE MANAGER ---------- */
        .file-manager {
            max-height: 400px;
            overflow-y: auto;
            background: #0d1117;
            border-radius: 8px;
            padding: 10px;
        }
        .file-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            border-bottom: 1px solid #1e1e1e;
            cursor: pointer;
            transition: background 0.2s;
            user-select: none;
        }
        .file-item:hover {
            background: #1a1f2b;
        }
        .file-item.selected {
            background: #2a3a5a;
            border-left: 3px solid #00e5ff;
        }
        .file-item .name {
            display: flex;
            align-items: center;
            gap: 8px;
            color: #ccc;
        }
        .file-item .name i {
            width: 20px;
            color: #00e5ff;
        }
        .file-item .name .dir-icon {
            color: #f0c674;
        }
        .file-item .size {
            font-size: 12px;
            color: #888;
        }
        .file-breadcrumb {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-bottom: 10px;
            padding: 8px;
            background: #1a1f2b;
            border-radius: 6px;
        }
        .file-breadcrumb span {
            color: #00e5ff;
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .file-breadcrumb span:hover {
            background: #2a3a5a;
        }
        .file-breadcrumb .sep {
            color: #555;
            cursor: default;
        }
        .file-context-menu {
            display: none;
            position: fixed;
            background: #1a1f2b;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 6px 0;
            z-index: 10002;
            min-width: 150px;
        }
        .file-context-menu .menu-item {
            padding: 8px 16px;
            color: #ccc;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .file-context-menu .menu-item:hover {
            background: #2a3a5a;
        }
        .file-context-menu .menu-item.danger {
            color: #ff4d4d;
        }

        /* Stats Modal */
        .stats-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
            margin-bottom: 15px;
        }
        .stat-card {
            background: rgba(255,255,255,0.03);
            border: 1px solid rgba(255,255,255,0.06);
            border-radius: 15px;
            padding: 15px;
            text-align: center;
        }
        .stat-card .label {
            color: #888;
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 1px;
        }
        .stat-card .value {
            font-size: 1.6rem;
            font-weight: bold;
            color: #00e5ff;
            margin: 5px 0;
        }
        .stat-card .sub {
            color: #666;
            font-size: 0.8rem;
        }
        .stat-card .progress-bar {
            width: 100%;
            height: 6px;
            background: #1a1a1a;
            border-radius: 4px;
            margin-top: 8px;
            overflow: hidden;
        }
        .stat-card .progress-bar .fill {
            height: 100%;
            background: linear-gradient(90deg,#7a00ff,#00e5ff);
            border-radius: 4px;
            transition: width 0.5s;
        }
        .offset-section {
            border-top: 1px solid rgba(255,255,255,0.1);
            padding-top: 15px;
            margin-top: 15px;
        }
        .offset-section label {
            color: #aaa;
        }
        .offset-section .offset-input {
            display: flex;
            gap: 10px;
            margin-top: 5px;
        }
        .offset-section .offset-input input {
            flex: 1;
            background: #1a1a1a;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 10px;
            color: #fff;
        }
        .offset-section .offset-input button {
            background: #00e5ff;
            border: none;
            border-radius: 8px;
            padding: 10px 20px;
            color: #000;
            font-weight: 700;
            cursor: pointer;
        }

        /* ---------- ANIMATIONS ---------- */
        @keyframes fadeIn {
            0% { opacity: 0; }
            100% { opacity: 1; }
        }
        @keyframes slideIn {
            0% { transform: translateX(60px); opacity: 0; }
            100% { transform: translateX(0); opacity: 1; }
        }

        ::-webkit-scrollbar {
            width: 4px;
        }
        ::-webkit-scrollbar-track {
            background: #0c1018;
        }
        ::-webkit-scrollbar-thumb {
            background: #333;
            border-radius: 4px;
        }

        @media (max-width: 480px) {
            .admin-drawer { max-width: 100%; padding: 18px 14px; }
            .list-item .row { flex-direction: column; align-items: stretch; }
            .list-item .limit-input { width: 100%; }
            .create-row { flex-direction: column; }
            .limit-group { flex-wrap: wrap; }
            .admin-tabs button { font-size: 11px; padding: 8px; }
            .terminal-controls { flex-wrap: wrap; }
            .terminal-controls input { width: 100%; }
            .bot-controls { grid-template-columns: 1fr 1fr; }
            .website-actions { grid-template-columns: 1fr 1fr; }
            .file-item { flex-wrap: wrap; }
            .stats-grid { grid-template-columns: 1fr; }
        }
    </style>
</head>
<body>

    <!-- ============================================================ -->
    <!--  VIEW: LOGIN                                                   -->
    <!-- ============================================================ -->
    <div id="loginView" class="view {% if not logged_in %}active{% endif %}">
        <div class="login-card">
            <div class="login-content">
                <div class="login-icon" id="loginIcon">
                    {% if logo_url %}
                        <img src="{{ logo_url }}" alt="Logo" />
                    {% else %}
                        <i class="fa-solid fa-user"></i>
                    {% endif %}
                </div>
                <h1 class="login-title">{{ website_name }}</h1>
                <select id="loginRoleSelect">
                    <option value="user" selected>USER ACCESS</option>
                    <option value="admin">Admin</option>
                </select>
                <input type="text" id="loginUsername" placeholder="Enter Username" />
                <input type="password" id="loginPassword" placeholder="Password" />
                <button class="login-btn" id="loginBtn">ACCESS SYSTEM</button>
                <div class="login-error" id="loginError"></div>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  VIEW: USER DASHBOARD                                          -->
    <!-- ============================================================ -->
    <div id="userView" class="view {% if logged_in %}active{% endif %}">
        <div class="user-container">

            <!-- Header -->
            <div class="user-header">
                <div class="user-header-left">
                    <span class="hamburger" id="hamburgerBtn">☰</span>
                    <span class="user-title">{{ website_name }}</span>
                </div>
                <div class="power-btn" id="logoutBtn"><i class="fa-solid fa-power-off"></i></div>
            </div>

            <!-- Tabs -->
            <div class="tabs">
                <button class="tab-btn active" data-tab="websites">🌐 Websites</button>
                <button class="tab-btn" data-tab="bots">🤖 Bots</button>
                <button class="tab-btn" data-tab="cli">💻 CLI Tools</button>
            </div>

            <!-- Websites Tab -->
            <div id="tab-websites" class="tab-content active">
                <div class="upload-card" id="uploadCardWebsite">
                    <div class="cloud-icon"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                    <div id="uploadLabelWebsite">UPLOAD WEBSITE (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnWebsite">DEPLOY WEBSITE</div>
                    <input type="file" id="fileInputWebsite" style="display:none;" multiple accept=".zip,.py,.js,.html,.css,.json,.txt,.php,.go,.rb,.sh,.pl,.jar,.war,.xml,.gradle" />
                    <div id="fileCountDisplayWebsite"></div>
                </div>
                <div id="websiteGrid" class="website-grid"></div>
                <div class="console-wrapper" style="margin-top:10px;">
                    <div class="console" id="websiteConsole">Select a website to see logs.</div>
                </div>
            </div>

            <!-- Bots Tab -->
            <div id="tab-bots" class="tab-content">
                <div class="upload-card" id="uploadCardBot">
                    <div class="cloud-icon"><i class="fa-solid fa-robot"></i></div>
                    <div id="uploadLabelBot">UPLOAD BOT (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnBot">DEPLOY BOT</div>
                    <input type="file" id="fileInputBot" style="display:none;" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl,.json,.txt" />
                    <div id="fileCountDisplayBot"></div>
                </div>
                <div id="botListContainer"></div>
                <div class="console-wrapper">
                    <div class="console" id="botConsole">Select a bot to see logs.</div>
                </div>
            </div>

            <!-- CLI Tools Tab -->
            <div id="tab-cli" class="tab-content">
                <div class="upload-card" id="uploadCardCli">
                    <div class="cloud-icon"><i class="fa-solid fa-terminal"></i></div>
                    <div id="uploadLabelCli">UPLOAD CLI TOOL (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnCli">DEPLOY CLI TOOL</div>
                    <input type="file" id="fileInputCli" style="display:none;" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl,.json,.txt,.csv" />
                    <div id="fileCountDisplayCli"></div>
                </div>
                <div id="cliToolListContainer"></div>
                <!-- CLI Terminal -->
                <div class="cli-terminal-container">
                    <div class="cli-terminal-header">
                        <span><i class="fa-solid fa-terminal"></i> CLI Terminal</span>
                        <span id="cliStatusBadge" class="status-badge status-stopped">● STOPPED</span>
                    </div>
                    <div class="cli-terminal" id="cliTerminal">Select a CLI tool to start...</div>
                    <div class="cli-terminal-input-row">
                        <input type="text" id="cliTerminalInput" placeholder="Type input for CLI tool..." disabled />
                        <button id="cliSendBtn" disabled><i class="fa-solid fa-paper-plane"></i> Send</button>
                        <button id="cliClearBtn"><i class="fa-solid fa-eraser"></i> Clear</button>
                        <button id="cliUploadFileBtn" class="btn-upload-file" disabled><i class="fa-solid fa-upload"></i> Upload File</button>
                        <input type="file" id="cliFileUploadInput" style="display:none;" />
                    </div>
                    <div id="cliUploadStatus" style="display:none;padding:10px 20px;background:rgba(255,255,255,.03);border-top:1px solid #30363d;"></div>
                </div>
            </div>

            <!-- Footer -->
            <div class="user-footer">
                <div class="f-title">{{ website_name }}</div>
                <div class="f-sub">LOVE YOU ALL. SUPPORT KARO</div>
                <div class="social-box">
                    <a href="{{ social_links.telegram }}" target="_blank"><i class="fa-brands fa-telegram"></i></a>
                    <a href="{{ social_links.youtube }}" target="_blank"><i class="fa-brands fa-youtube"></i></a>
                    <a href="{{ social_links.instagram }}" target="_blank"><i class="fa-brands fa-instagram"></i></a>
                    <a href="{{ social_links.tiktok }}" target="_blank"><i class="fa-brands fa-tiktok"></i></a>
                </div>
            </div>

        </div>
    </div>

    <!-- ============================================================ -->
    <!--  ADMIN OVERLAY (DRAWER)                                        -->
    <!-- ============================================================ -->
    <div class="admin-overlay" id="adminOverlay">
        <div class="admin-drawer">

            <div class="admin-drawer-header">
                <h2><i class="fa-solid fa-shield-halved" style="margin-right:8px;"></i>ADMIN PANEL</h2>
                <button class="admin-close-btn" id="adminCloseBtn">✕</button>
            </div>

            <div class="admin-tabs">
                <button class="active" data-tab="tabAdminMenu">🛠️ ADMIN MENU</button>
                <button data-tab="tabUserMenu">👥 USER MENU</button>
                <button data-tab="tabTerminal">💻 TERMINAL</button>
                <button data-tab="tabFileManager">📁 FILES</button>
                {% if is_admin %}
                <button data-tab="tabStats">📊 STATS</button>
                {% endif %}
            </div>

            <div class="admin-panel-content">
                <!-- ADMIN MENU -->
                <div id="tabAdminMenu" class="admin-tab-content active">
                    <button class="btn-create" id="toggleCreateUserBtn" style="width:100%;margin-bottom:12px;">
                        <i class="fa-solid fa-plus"></i> NEW USER
                    </button>
                    <button class="btn-create" id="editProfileBtn" style="width:100%;margin-bottom:12px;background:#4d88ff;">
                        <i class="fa-solid fa-user-edit"></i> EDIT PROFILE
                    </button>
                    <div id="createUserForm">
                        <input type="text" id="newUsername" placeholder="Username" />
                        <input type="password" id="newPassword" placeholder="Password" />
                        <input type="text" id="newExpiry" placeholder="Expiry (Days, e.g. 1, 5, 30)" />
                        <select id="newRole">
                            <option value="user">User</option>
                            <option value="admin">Admin</option>
                        </select>
                        <button class="btn-create" id="createUserBtn" style="width:100%;">CREATE</button>
                    </div>
                    <div id="fullUserListContainer"></div>
                </div>

                <!-- USER MENU -->
                <div id="tabUserMenu" class="admin-tab-content">
                    <div class="section-title">👑 Admin List</div>
                    <div id="simpleAdminListContainer"></div>
                    <div class="section-title" style="margin-top:24px;">👤 User List</div>
                    <div id="simpleUserListContainer"></div>
                </div>

                <!-- TERMINAL -->
                <div id="tabTerminal" class="admin-tab-content">
                    <div class="terminal-box" id="terminalOutput">
                        <span class="prompt">$ </span>Connected...<br />
                    </div>
                    <div class="terminal-controls">
                        <input type="text" id="terminalCommand" placeholder="Type command or input..." />
                        <button class="btn-term-run" id="termRunBtn"><i class="fa-solid fa-play"></i> Run</button>
                        <button class="btn-term-stop" id="termStopBtn"><i class="fa-solid fa-stop"></i> Stop</button>
                        <button class="btn-term-clear" id="termClearBtn"><i class="fa-solid fa-eraser"></i> Clear</button>
                    </div>
                </div>

                <!-- FILE MANAGER -->
                <div id="tabFileManager" class="admin-tab-content">
                    <div class="file-breadcrumb" id="fileBreadcrumb"></div>
                    <div class="file-manager" id="fileManagerList"></div>
                    <div style="margin-top:10px;font-size:12px;color:#555;">Long press on item (or right-click) for actions</div>
                </div>

                <!-- STATS -->
                {% if is_admin %}
                <div id="tabStats" class="admin-tab-content">
                    <div class="stats-grid" id="statsGrid">
                        <div class="stat-card"><div class="label">Total Hours Used</div><div class="value" id="statTotalHours">--</div><div class="sub">Offset + Running</div></div>
                        <div class="stat-card"><div class="label">Main Container Uptime</div><div class="value" id="statMainHours">--</div><div class="sub">Flask App</div></div>
                        <div class="stat-card"><div class="label">Internal Sites/Bots</div><div class="value" id="statInternalHours">--</div><div class="sub">Subprocesses</div></div>
                        <div class="stat-card"><div class="label">Render External Services</div><div class="value" id="statRenderHours">--</div><div class="sub">Active: <span id="statRenderActive">--</span></div></div>
                        <div class="stat-card"><div class="label">Storage (Uploads)</div><div class="value" id="statStorage">--</div><div class="sub">Free: <span id="statDiskFree">--</span> GB</div></div>
                        <div class="stat-card"><div class="label">Container RAM</div><div class="value" id="statRam">--</div><div class="sub"><span id="statRamUsed">--</span> MB / <span id="statRamTotal">--</span> MB</div><div class="progress-bar"><div class="fill" id="ramFill" style="width:0%;"></div></div></div>
                        <div class="stat-card"><div class="label">CPU Usage</div><div class="value" id="statCpu">--</div><div class="sub">Percent</div></div>
                    </div>
                    <div class="offset-section">
                        <label>🔧 Set Offset (Total Hours from Render Dashboard)</label>
                        <div class="offset-input">
                            <input type="number" id="offsetInput" step="0.01" placeholder="e.g. 52.30" />
                            <button id="setOffsetBtn">SET OFFSET</button>
                        </div>
                        <div style="font-size:0.75rem;color:#666;margin-top:5px;">Render Dashboard → Usage → Total Hours Used so far.</div>
                    </div>
                </div>
                {% endif %}
            </div>

        </div>
    </div>

    <!-- ============================================================ -->
    <!--  CUSTOM MODAL                                                 -->
    <!-- ============================================================ -->
    <div class="custom-modal-overlay" id="customModalOverlay">
        <div class="custom-modal">
            <div class="modal-icon" id="modalIcon">⚠️</div>
            <div class="modal-body" id="modalBody"></div>
            <div class="modal-actions" id="modalActions"></div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  SETTINGS MODAL (gear icon)                                   -->
    <!-- ============================================================ -->
    <div class="custom-modal-overlay" id="settingsModalOverlay">
        <div class="custom-modal">
            <div class="modal-icon" style="text-align:center;color:#00e5ff;"><i class="fa-solid fa-gear"></i></div>
            <div class="modal-body" id="settingsModalBody">
                <div class="settings-form">
                    <label>Website Name</label>
                    <input type="text" id="settingsWebsiteName" placeholder="Website name" />
                    
                    <label>Telegram Link</label>
                    <input type="text" id="settingsTelegram" placeholder="https://t.me/..." />
                    
                    <label>YouTube Link</label>
                    <input type="text" id="settingsYoutube" placeholder="https://youtube.com/..." />
                    
                    <label>Instagram Link</label>
                    <input type="text" id="settingsInstagram" placeholder="https://instagram.com/..." />
                    
                    <label>TikTok Link</label>
                    <input type="text" id="settingsTiktok" placeholder="https://tiktok.com/..." />
                    
                    <label>Upload Logo (PNG, JPG, GIF, WEBP)</label>
                    <input type="file" id="settingsLogoInput" accept="image/*" />
                    <div id="settingsLogoPreview"></div>
                    <button class="btn-remove-logo" id="settingsRemoveLogoBtn">Remove Logo</button>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn-cancel" id="settingsCancelBtn">Cancel</button>
                <button class="btn-confirm" id="settingsSaveBtn">Save Settings</button>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  CONTEXT MENU (file manager)                                  -->
    <!-- ============================================================ -->
    <div class="file-context-menu" id="fileContextMenu">
        <div class="menu-item" id="ctxDelete"><i class="fa-solid fa-trash"></i> Delete</div>
        <div class="menu-item" id="ctxRename"><i class="fa-solid fa-pen"></i> Rename</div>
        <div class="menu-item" id="ctxDownload"><i class="fa-solid fa-download"></i> Download</div>
    </div>

    <!-- ============================================================ -->
    <!--  JAVASCRIPT                                                   -->
    <!-- ============================================================ -->
    <script>
        (function() {
            'use strict';

            // ---------- GLOBAL FETCH INTERCEPTOR ----------
            const originalFetch = window.fetch;
            window.fetch = function(url, options) {
                return originalFetch(url, options).then(response => {
                    if (response.status === 401) {
                        window.location.href = '/';
                        return Promise.reject('Unauthorized');
                    }
                    return response;
                });
            };

            // ---------- CUSTOM MODAL ----------
            const modalOverlay = document.getElementById('customModalOverlay');
            const modalIcon = document.getElementById('modalIcon');
            const modalBody = document.getElementById('modalBody');
            const modalActions = document.getElementById('modalActions');

            function showCustomModal(icon, bodyHTML, buttons) {
                return new Promise((resolve) => {
                    modalIcon.textContent = icon || '⚠️';
                    modalBody.innerHTML = bodyHTML || '';
                    modalActions.innerHTML = '';
                    buttons.forEach((btn) => {
                        const buttonEl = document.createElement('button');
                        buttonEl.textContent = btn.label;
                        buttonEl.className = btn.className || 'btn-confirm';
                        buttonEl.addEventListener('click', () => {
                            closeModal();
                            resolve(btn.value);
                        });
                        modalActions.appendChild(buttonEl);
                    });
                    modalOverlay.classList.add('open');
                });
            }

            window.customAlert = function(message, icon = 'ℹ️') {
                return showCustomModal(icon, `<div style="font-size:16px;color:#eee;">${message}</div>`, [
                    { label: 'OK', value: true, className: 'btn-ok' }
                ]);
            };

            window.customConfirm = function(message, icon = '⚠️') {
                return showCustomModal(icon, `<div style="font-size:16px;color:#eee;">${message}</div>`, [
                    { label: 'Cancel', value: false, className: 'btn-cancel' },
                    { label: 'OK', value: true, className: 'btn-confirm' }
                ]);
            };

            function closeModal() {
                modalOverlay.classList.remove('open');
            }

            // ---------- SECRET KEY LOGIN (Logo clicks) ----------
            let loginIconClickCount = 0;
            let loginIconTimer = null;

            document.getElementById('loginIcon').addEventListener('click', function(e) {
                loginIconClickCount++;
                clearTimeout(loginIconTimer);
                loginIconTimer = setTimeout(() => { loginIconClickCount = 0; }, 2000);

                if (loginIconClickCount >= 5) {
                    loginIconClickCount = 0;
                    showSecretKeyModal();
                }
            });

            async function showSecretKeyModal() {
                const bodyHTML = `
                    <div style="text-align:center;">
                        <p style="margin-bottom:12px;">Enter Secret Key to login as Admin:</p>
                        <input type="password" id="secretKeyInput" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                    </div>
                `;
                const result = await showCustomModal('🔑', bodyHTML, [
                    { label: 'Cancel', value: false, className: 'btn-cancel' },
                    { label: 'Login', value: true, className: 'btn-confirm' }
                ]);
                if (result) {
                    const secret = document.getElementById('secretKeyInput')?.value;
                    if (!secret) {
                        await customAlert('Enter secret key.', '⚠️');
                        return;
                    }
                    try {
                        const res = await fetch('/api/secret_login', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ secret })
                        });
                        const data = await res.json();
                        if (data.success) {
                            location.reload();
                        } else {
                            await customAlert(data.error || 'Invalid secret', '❌');
                        }
                    } catch(e) {
                        await customAlert('Error: ' + e.message, '❌');
                    }
                }
            }

            // ---------- PROFILE EDIT ----------
            const editProfileBtn = document.getElementById('editProfileBtn');
            if (editProfileBtn) {
                editProfileBtn.addEventListener('click', async function() {
                    const currentUsername = '{{ username }}';
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <div style="font-size:20px; margin-bottom:20px;">✎ Edit Profile</div>
                            <div style="margin-bottom:12px;">
                                <label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Username</label>
                                <input type="text" id="editUsername" value="${currentUsername}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                            </div>
                            <div>
                                <label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Password (leave blank to keep current)</label>
                                <input type="password" id="editPassword" placeholder="New password..." style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                            </div>
                        </div>
                    `;
                    const result = await showCustomModal('✎', bodyHTML, [
                        { label: 'Cancel', value: false, className: 'btn-cancel' },
                        { label: 'Save', value: true, className: 'btn-confirm' }
                    ]);
                    if (result) {
                        const newUsername = document.getElementById('editUsername').value.trim();
                        const newPassword = document.getElementById('editPassword').value.trim();
                        try {
                            const res = await fetch('/api/profile', {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ username: newUsername, password: newPassword })
                            });
                            const data = await res.json();
                            if (data.success) {
                                if (data.logout) {
                                    await customAlert('Profile updated! You will be logged out.', '✅');
                                    window.location.href = '/';
                                } else {
                                    await customAlert('Profile updated!', '✅');
                                    location.reload();
                                }
                            } else {
                                await customAlert(data.error || 'Update failed', '❌');
                            }
                        } catch (e) {
                            // handled by interceptor
                        }
                    }
                });
            }

            // ---------- SETTINGS MODAL ----------
            const settingsModalOverlay = document.getElementById('settingsModalOverlay');
            const settingsCancelBtn = document.getElementById('settingsCancelBtn');
            const settingsSaveBtn = document.getElementById('settingsSaveBtn');
            const settingsWebsiteName = document.getElementById('settingsWebsiteName');
            const settingsTelegram = document.getElementById('settingsTelegram');
            const settingsYoutube = document.getElementById('settingsYoutube');
            const settingsInstagram = document.getElementById('settingsInstagram');
            const settingsTiktok = document.getElementById('settingsTiktok');
            const settingsLogoInput = document.getElementById('settingsLogoInput');
            const settingsLogoPreview = document.getElementById('settingsLogoPreview');
            const settingsRemoveLogoBtn = document.getElementById('settingsRemoveLogoBtn');

            let currentSettings = {};

            async function loadSettings() {
                try {
                    const res = await fetch('/api/settings');
                    const data = await res.json();
                    currentSettings = data;
                    settingsWebsiteName.value = data.website_name || 'YUVICODEX';
                    settingsTelegram.value = data.social_links?.telegram || '#';
                    settingsYoutube.value = data.social_links?.youtube || '#';
                    settingsInstagram.value = data.social_links?.instagram || '#';
                    settingsTiktok.value = data.social_links?.tiktok || '#';
                    if (data.logo) {
                        settingsLogoPreview.innerHTML = `<img src="${data.logo}" class="logo-preview" />`;
                    } else {
                        settingsLogoPreview.innerHTML = '';
                    }
                } catch (e) {
                    console.error('Failed to load settings', e);
                }
            }

            async function saveSettings() {
                const payload = {
                    website_name: settingsWebsiteName.value.trim() || 'YUVICODEX',
                    social_links: {
                        telegram: settingsTelegram.value.trim() || '#',
                        youtube: settingsYoutube.value.trim() || '#',
                        instagram: settingsInstagram.value.trim() || '#',
                        tiktok: settingsTiktok.value.trim() || '#'
                    }
                };
                try {
                    const res = await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Settings saved successfully!', '✅');
                        closeSettingsModal();
                        location.reload();
                    } else {
                        await customAlert('Failed to save settings.', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            }

            async function uploadLogo(file) {
                const formData = new FormData();
                formData.append('logo', file);
                try {
                    const res = await fetch('/api/settings/logo', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Logo uploaded!', '✅');
                        await loadSettings();
                        location.reload();
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            }

            async function removeLogo() {
                const confirmed = await customConfirm('Remove logo?', '🗑️');
                if (!confirmed) return;
                try {
                    const res = await fetch('/api/settings/logo', { method: 'DELETE' });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Logo removed.', '✅');
                        await loadSettings();
                        closeSettingsModal();
                        setTimeout(() => { location.reload(); }, 500);
                    } else {
                        await customAlert('Failed to remove logo.', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            }

            function openSettingsModal() {
                loadSettings();
                settingsModalOverlay.classList.add('open');
            }

            function closeSettingsModal() {
                settingsModalOverlay.classList.remove('open');
            }

            settingsCancelBtn.addEventListener('click', closeSettingsModal);
            settingsSaveBtn.addEventListener('click', saveSettings);
            settingsRemoveLogoBtn.addEventListener('click', removeLogo);
            settingsLogoInput.addEventListener('change', function() {
                if (this.files.length > 0) {
                    uploadLogo(this.files[0]);
                    this.value = '';
                }
            });
            settingsModalOverlay.addEventListener('click', function(e) {
                if (e.target === this) closeSettingsModal();
            });

            // ---------- DOM REFS ----------
            const loginView = document.getElementById('loginView');
            const userView = document.getElementById('userView');
            const adminOverlay = document.getElementById('adminOverlay');

            const loginUsername = document.getElementById('loginUsername');
            const loginPassword = document.getElementById('loginPassword');
            const loginRoleSelect = document.getElementById('loginRoleSelect');
            const loginBtn = document.getElementById('loginBtn');
            const loginError = document.getElementById('loginError');

            const hamburgerBtn = document.getElementById('hamburgerBtn');
            const adminCloseBtn = document.getElementById('adminCloseBtn');
            const logoutBtn = document.getElementById('logoutBtn');

            const botListContainer = document.getElementById('botListContainer');
            const botConsole = document.getElementById('botConsole');
            const websiteGrid = document.getElementById('websiteGrid');
            const websiteConsole = document.getElementById('websiteConsole');
            const cliToolListContainer = document.getElementById('cliToolListContainer');
            const cliTerminal = document.getElementById('cliTerminal');
            const cliTerminalInput = document.getElementById('cliTerminalInput');
            const cliSendBtn = document.getElementById('cliSendBtn');
            const cliClearBtn = document.getElementById('cliClearBtn');
            const cliUploadFileBtn = document.getElementById('cliUploadFileBtn');
            const cliFileUploadInput = document.getElementById('cliFileUploadInput');
            const cliStatusBadge = document.getElementById('cliStatusBadge');
            const cliUploadStatus = document.getElementById('cliUploadStatus');

            const uploadCardWebsite = document.getElementById('uploadCardWebsite');
            const deployBtnWebsite = document.getElementById('deployBtnWebsite');
            const fileInputWebsite = document.getElementById('fileInputWebsite');
            const fileCountDisplayWebsite = document.getElementById('fileCountDisplayWebsite');

            const uploadCardBot = document.getElementById('uploadCardBot');
            const deployBtnBot = document.getElementById('deployBtnBot');
            const fileInputBot = document.getElementById('fileInputBot');
            const fileCountDisplayBot = document.getElementById('fileCountDisplayBot');

            const uploadCardCli = document.getElementById('uploadCardCli');
            const deployBtnCli = document.getElementById('deployBtnCli');
            const fileInputCli = document.getElementById('fileInputCli');
            const fileCountDisplayCli = document.getElementById('fileCountDisplayCli');

            const fullUserListContainer = document.getElementById('fullUserListContainer');
            const simpleAdminListContainer = document.getElementById('simpleAdminListContainer');
            const simpleUserListContainer = document.getElementById('simpleUserListContainer');

            const toggleCreateUserBtn = document.getElementById('toggleCreateUserBtn');
            const createUserForm = document.getElementById('createUserForm');
            const newUsername = document.getElementById('newUsername');
            const newPassword = document.getElementById('newPassword');
            const newExpiry = document.getElementById('newExpiry');
            const newRole = document.getElementById('newRole');
            const createUserBtn = document.getElementById('createUserBtn');

            const terminalOutput = document.getElementById('terminalOutput');
            const terminalCommand = document.getElementById('terminalCommand');
            const termRunBtn = document.getElementById('termRunBtn');
            const termStopBtn = document.getElementById('termStopBtn');
            const termClearBtn = document.getElementById('termClearBtn');

            // ---------- STATE ----------
            let currentUser = null;
            let selectedBotId = null;
            let selectedWebsiteId = null;
            let selectedCliToolId = null;
            let cliProcessRunning = false;
            let cliUploadResolve = null;
            let logPollInterval = null;
            let websiteLogInterval = null;
            let botLogInterval = null;
            let cliLogInterval = null;
            let uptimeIntervals = {};
            let terminalPollInterval = null;
            let isTerminalRunning = false;

            // ---------- API HELPERS ----------
            async function apiCall(url, options = {}) {
                const res = await fetch(url, {
                    ...options,
                    headers: { 'Content-Type': 'application/json', ...options.headers }
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.error || 'API error');
                }
                return res.json();
            }

            // ---------- LOGIN / LOGOUT ----------
            async function handleLogin() {
                const username = loginUsername.value.trim();
                const password = loginPassword.value.trim();
                const role = loginRoleSelect.value;
                loginError.textContent = '';
                if (!username || !password) {
                    loginError.textContent = 'Please enter username and password.';
                    return;
                }
                try {
                    const data = await apiCall('/login', {
                        method: 'POST',
                        body: JSON.stringify({ username, password, role })
                    });
                    if (data.success) {
                        currentUser = { username: data.username, role: data.role };
                        location.reload();
                    }
                } catch (e) {
                    loginError.textContent = e.message || 'Login failed';
                }
            }

            async function handleLogout() {
                const confirmed = await customConfirm('Logout?', '👋');
                if (!confirmed) return;
                try {
                    await apiCall('/logout', { method: 'POST' });
                } catch (_) {}
                location.reload();
            }

            // ---------- TAB SWITCHING ----------
            const tabBtns = document.querySelectorAll('.tabs .tab-btn');
            tabBtns.forEach(btn => {
                btn.addEventListener('click', function() {
                    tabBtns.forEach(b => b.classList.remove('active'));
                    this.classList.add('active');
                    const tabId = this.dataset.tab;
                    document.querySelectorAll('.tab-content').forEach(tc => tc.classList.remove('active'));
                    document.getElementById('tab-' + tabId).classList.add('active');
                    if (tabId === 'websites') {
                        loadWebsites();
                    } else if (tabId === 'bots') {
                        loadBots();
                    } else if (tabId === 'cli') {
                        loadCliTools();
                    }
                });
            });

            // ---------- WEBSITES ----------
            async function loadWebsites() {
                try {
                    const res = await fetch('/api/websites');
                    const data = await res.json();
                    renderWebsites(data);
                } catch (e) {
                    console.error('Failed to load websites:', e);
                    websiteGrid.innerHTML = `<div class="empty-msg">Error loading websites</div>`;
                }
            }

            function renderWebsites(websites) {
                if (!websites || websites.length === 0) {
                    websiteGrid.innerHTML = `<div class="empty-msg">No websites deployed. Upload a project!</div>`;
                    return;
                }
                let html = '';
                websites.forEach(w => {
                    const statusClass = w.status === 'running' ? 'running' : (w.status === 'failed' ? 'failed' : 'stopped');
                    const uptimeDisplay = w.status === 'running' && w.last_start_time ?
                        formatUptime((Date.now() / 1000) - new Date(w.last_start_time).getTime()/1000) :
                        '--';
                    const selected = (w.id === selectedWebsiteId) ? 'selected' : '';
                    const visitUrl = window.location.origin + '/' + w.website_slug + '/';
                    html += `
                        <div class="website-card ${selected}" data-id="${w.id}">
                            <div class="website-header">
                                <span class="website-name">${escapeHtml(w.website_name || w.website_slug)}</span>
                                <span class="website-status ${statusClass}">● ${w.status.toUpperCase()}</span>
                            </div>
                            <div class="website-slug">🔗 ${escapeHtml(w.website_slug)}</div>
                            <div class="website-port">Port: ${w.allocated_port || 'N/A'}</div>
                            <div class="website-uptime" id="w-uptime-${w.id}">UPTIME: ${uptimeDisplay}</div>
                            <div class="website-actions">
                                <button class="btn-start-w" data-action="start-w" data-id="${w.id}">▶ START</button>
                                <button class="btn-stop-w" data-action="stop-w" data-id="${w.id}">⏹ STOP</button>
                                <button class="btn-restart-w" data-action="restart-w" data-id="${w.id}">⟳ RESTART</button>
                                <button class="btn-delete-w" data-action="delete-w" data-id="${w.id}">🗑 DELETE</button>
                                <button class="btn-edit-w" data-action="edit-w" data-id="${w.id}">✎ EDIT</button>
                                <button class="btn-download-w" data-action="download-w" data-id="${w.id}">⬇ DOWNLOAD</button>
                                <button class="btn-files-w" data-action="files-w" data-id="${w.id}">📁 FILES</button>
                                <button class="btn-buildlogs-w" data-action="buildlogs-w" data-id="${w.id}">🖥 BUILD LOGS</button>
                                <button class="btn-miniweb-w" data-action="miniweb-w" data-id="${w.id}">📱 Mini Web</button>
                                <button class="btn-visit-w" data-action="visit-w" data-id="${w.id}" data-url="${visitUrl}" style="grid-column: span 2;">🌐 VISIT WEBSITE</button>
                            </div>
                            <div class="name-edit">
                                <input type="text" placeholder="Rename" id="w-name-input-${w.id}" value="${escapeHtml(w.website_name || '')}" />
                                <button onclick="renameWebsite(${w.id})">Rename</button>
                            </div>
                        </div>
                    `;
                });
                websiteGrid.innerHTML = html;

                // Attach events
                document.querySelectorAll('.website-card [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const id = parseInt(this.dataset.id);
                        if (action === 'start-w') {
                            await websiteAction(id, 'start');
                        } else if (action === 'stop-w') {
                            await websiteAction(id, 'stop');
                        } else if (action === 'restart-w') {
                            await websiteAction(id, 'restart');
                        } else if (action === 'delete-w') {
                            const confirmed = await customConfirm('Delete this website?', '🗑️');
                            if (confirmed) {
                                await websiteAction(id, 'delete');
                            }
                        } else if (action === 'edit-w') {
                            await editWebsite(id);
                        } else if (action === 'download-w') {
                            window.open(`/api/website/${id}/download`, '_blank');
                        } else if (action === 'files-w') {
                            window.open(`/website/${id}/files`, '_blank');
                        } else if (action === 'buildlogs-w') {
                            window.open(`/website/${id}/build`, '_blank');
                        } else if (action === 'miniweb-w') {
                            window.open(`/mini/website/${id}`, '_blank');
                        } else if (action === 'visit-w') {
                            window.open(this.dataset.url, '_blank');
                        }
                    });
                });

                // Click card to select and show logs
                document.querySelectorAll('.website-card').forEach(card => {
                    card.addEventListener('click', function(e) {
                        if (e.target.closest('button') || e.target.closest('.name-edit')) return;
                        const id = parseInt(this.dataset.id);
                        selectWebsite(id);
                    });
                });

                // Uptime updates
                websites.forEach(w => {
                    if (w.status === 'running' && w.last_start_time) {
                        const start = new Date(w.last_start_time).getTime() / 1000;
                        startUptimeUpdate('w-uptime-' + w.id, start);
                    }
                });

                if (!selectedWebsiteId && websites.length > 0) {
                    selectWebsite(websites[0].id);
                }
            }

            async function websiteAction(id, action) {
                try {
                    const res = await apiCall(`/api/website/${id}/${action}`, { method: 'POST' });
                    if (res.success) {
                        await loadWebsites();
                    } else {
                        await customAlert(res.error || 'Action failed', '❌');
                    }
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            async function editWebsite(id) {
                try {
                    const data = await apiCall(`/api/website/${id}/content`);
                    const content = data.content || '';
                    const bodyHTML = `
                        <div style="margin-bottom:8px;">
                            <button class="btn-sm" id="copyAllBtnW" style="padding:6px 14px;font-size:0.55rem;border:1px solid #33ddff;color:#33ddff;background:transparent;border-radius:6px;cursor:pointer;">
                                📋 Copy All
                            </button>
                        </div>
                        <textarea id="editFileContentW" rows="15" style="width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:0.7rem;resize:vertical;tab-size:4;">${escapeHtml(content)}</textarea>
                    `;
                    const result = await showCustomModal('✎ Edit File', bodyHTML, [
                        { label: 'Cancel', value: false, className: 'btn-cancel' },
                        { label: '💾 SAVE', value: true, className: 'btn-confirm' }
                    ]);
                    if (result) {
                        const newContent = document.getElementById('editFileContentW').value;
                        try {
                            await apiCall(`/api/website/${id}/content`, {
                                method: 'PUT',
                                body: JSON.stringify({ content: newContent })
                            });
                            await customAlert('File saved and website restarted (if running).', '✅');
                            await loadWebsites();
                        } catch (e) {
                            await customAlert(e.message, '❌');
                        }
                    }
                    setTimeout(() => {
                        const copyBtn = document.getElementById('copyAllBtnW');
                        if (copyBtn) {
                            copyBtn.onclick = function() {
                                const textarea = document.getElementById('editFileContentW');
                                textarea.select();
                                try {
                                    navigator.clipboard.writeText(textarea.value).then(() => {
                                        customAlert('📋 Copied all code!', '✅');
                                    }).catch(() => {
                                        document.execCommand('copy');
                                        customAlert('📋 Copied!', '✅');
                                    });
                                } catch(e) {
                                    document.execCommand('copy');
                                    customAlert('📋 Copied!', '✅');
                                }
                            };
                        }
                    }, 100);
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            function selectWebsite(id) {
                selectedWebsiteId = id;
                document.querySelectorAll('.website-card').forEach(c => c.classList.remove('selected'));
                const card = document.querySelector(`.website-card[data-id="${id}"]`);
                if (card) card.classList.add('selected');
                loadWebsiteLogs(id);
                if (websiteLogInterval) clearInterval(websiteLogInterval);
                websiteLogInterval = setInterval(() => loadWebsiteLogs(id, true), 3000);
            }

            async function loadWebsiteLogs(id, silent = false) {
                try {
                    const data = await apiCall(`/api/website/${id}/logs`);
                    websiteConsole.textContent = data.logs || 'No logs yet.';
                } catch (e) {
                    if (!silent) websiteConsole.textContent = 'Error loading logs.';
                }
            }

            async function renameWebsite(id) {
                const input = document.getElementById('w-name-input-' + id);
                const newName = input.value.trim();
                if (!newName) return;
                try {
                    const formData = new FormData();
                    formData.append('name', newName);
                    const res = await fetch(`/api/website/${id}/rename`, {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await loadWebsites();
                    } else {
                        await customAlert(data.error || 'Rename failed', '❌');
                    }
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            // ---------- WEBSITE UPLOAD ----------
            uploadCardWebsite.addEventListener('click', function(e) {
                if (e.target.closest('.deploy-btn')) return;
                fileInputWebsite.click();
            });

            deployBtnWebsite.addEventListener('click', async function() {
                if (fileInputWebsite.files.length === 0) {
                    await customAlert('Please select at least one file first.', '⚠️');
                    return;
                }
                const formData = new FormData();
                for (let i = 0; i < fileInputWebsite.files.length; i++) {
                    formData.append('files[]', fileInputWebsite.files[i]);
                }
                try {
                    deployBtnWebsite.textContent = 'UPLOADING...';
                    deployBtnWebsite.disabled = true;
                    const res = await fetch('/upload_website', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert(`Website deployed! ID: ${data.website_id}`, '✅');
                        await loadWebsites();
                        fileInputWebsite.value = '';
                        fileCountDisplayWebsite.textContent = '';
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                } finally {
                    deployBtnWebsite.textContent = 'DEPLOY WEBSITE';
                    deployBtnWebsite.disabled = false;
                }
            });

            fileInputWebsite.addEventListener('change', function() {
                const count = this.files.length;
                if (count === 0) {
                    fileCountDisplayWebsite.textContent = '';
                } else {
                    const names = Array.from(this.files).map(f => f.name).join(', ');
                    fileCountDisplayWebsite.textContent = `${count} file(s) selected: ${names}`;
                }
            });

            // ---------- BOTS ----------
            async function loadBots() {
                try {
                    const bots = await apiCall('/api/bots');
                    renderBots(bots);
                } catch (e) {
                    console.error('Failed to load bots:', e);
                    botListContainer.innerHTML = `<div class="empty-msg">Error loading bots</div>`;
                }
            }

            function renderBots(bots) {
                if (!bots || bots.length === 0) {
                    botListContainer.innerHTML = `<div class="empty-msg">No bots deployed. Upload a project!</div>`;
                    return;
                }
                let html = '';
                bots.forEach(bot => {
                    const statusClass = bot.status === 'running' ? 'running' : 'stopped';
                    const uptimeDisplay = bot.status === 'running' && bot.start_time ?
                        formatUptime(Date.now() / 1000 - bot.start_time) :
                        '--';
                    const selected = (bot.id === selectedBotId) ? 'selected' : '';
                    const ownerDisplay = bot.user || 'unknown';
                    const hasToken = bot.has_token || false;
                    const botUsername = bot.bot_username || null;
                    html += `
                        <div class="bot-card ${selected}" data-id="${bot.id}" data-start-time="${bot.start_time || ''}">
                            <div class="bot-header">
                                <span class="bot-name">${escapeHtml(bot.filename)}</span>
                                <span class="bot-status ${statusClass}">● ${bot.status.toUpperCase()}</span>
                            </div>
                            <div class="bot-owner">👤 ${escapeHtml(ownerDisplay)}</div>
                            <div class="bot-uptime" id="uptime-${bot.id}">UPTIME: ${uptimeDisplay}</div>
                            <div class="bot-controls">
                                <button class="btn-start" data-action="start">${bot.status === 'running' ? '▶ RUNNING' : '▶ START'}</button>
                                <button class="btn-stop" data-action="stop">⏹ STOP</button>
                                <button class="btn-edit" data-action="edit">✎ EDIT</button>
                                <button class="btn-restart" data-action="restart">⟳ RESTART</button>
                                <button class="btn-download" data-action="download">⬇ DOWNLOAD</button>
                                <button class="btn-delete" data-action="delete">🗑 DELETE</button>
                                <button class="btn-miniweb" data-action="miniweb">📱 Mini Web</button>
                                <button class="btn-buildlogs" data-action="buildlogs">🖥 Build Logs</button>
                                ${(hasToken && botUsername) ? `<button class="btn-openbot" data-action="openbot" data-bot="${botUsername}">🤖 Open Bot</button>` : ''}
                            </div>
                        </div>
                    `;
                });
                botListContainer.innerHTML = html;

                document.querySelectorAll('.bot-card').forEach(card => {
                    card.addEventListener('click', function(e) {
                        if (e.target.closest('button')) return;
                        const id = this.dataset.id;
                        selectBot(id);
                    });
                });

                document.querySelectorAll('.bot-card [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const card = this.closest('.bot-card');
                        const botId = card.dataset.id;
                        const originalText = this.textContent;
                        this.disabled = true;
                        if (action === 'start') this.textContent = '⏳ Starting...';
                        else if (action === 'stop') this.textContent = '⏳ Stopping...';
                        else if (action === 'restart') this.textContent = '⏳ Restarting...';
                        else if (action === 'delete') this.textContent = '⏳ Deleting...';
                        else if (action === 'edit') { /* don't disable */ }
                        else if (action === 'download') { /* don't disable */ }
                        else if (action === 'openbot') { /* don't disable */ }
                        else if (action === 'miniweb') { /* don't disable */ }
                        else if (action === 'buildlogs') { /* don't disable */ }
                        try {
                            if (action === 'openbot') {
                                const botUsername = this.dataset.bot;
                                if (botUsername) {
                                    window.open(`https://t.me/${botUsername}`, '_blank');
                                    await customAlert(`🤖 Opening @${botUsername}`, '✅');
                                }
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'miniweb') {
                                window.open(`/mini/bot/${botId}`, '_blank');
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'buildlogs') {
                                window.open(`/bot/${botId}/build`, '_blank');
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'edit') {
                                await openEditModal(botId);
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'download') {
                                window.open(`/api/bots/${botId}/download`, '_blank');
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            await handleBotAction(botId, action);
                        } catch (e) {
                            // error already handled
                        } finally {
                            if (action !== 'edit' && action !== 'download' && action !== 'openbot' && action !== 'miniweb' && action !== 'buildlogs') {
                                this.disabled = false;
                                this.textContent = originalText;
                            }
                        }
                    });
                });

                bots.forEach(bot => {
                    if (bot.status === 'running' && bot.start_time) {
                        startUptimeUpdate('uptime-' + bot.id, bot.start_time);
                    }
                });

                if (!selectedBotId && bots.length > 0) {
                    selectBot(bots[0].id);
                }
            }

            function startUptimeUpdate(elId, startTime) {
                if (uptimeIntervals[elId]) clearInterval(uptimeIntervals[elId]);
                const el = document.getElementById(elId);
                if (!el) return;
                uptimeIntervals[elId] = setInterval(() => {
                    const now = Date.now() / 1000;
                    const diff = now - startTime;
                    el.textContent = 'UPTIME: ' + formatUptime(diff);
                }, 1000);
            }

            function stopUptimeUpdate(elId) {
                if (uptimeIntervals[elId]) {
                    clearInterval(uptimeIntervals[elId]);
                    delete uptimeIntervals[elId];
                }
            }

            function selectBot(botId) {
                selectedBotId = botId;
                document.querySelectorAll('.bot-card').forEach(c => c.classList.remove('selected'));
                const card = document.querySelector(`.bot-card[data-id="${botId}"]`);
                if (card) card.classList.add('selected');
                loadBotLogs(botId);
                if (botLogInterval) clearInterval(botLogInterval);
                botLogInterval = setInterval(() => loadBotLogs(botId, true), 3000);
            }

            async function loadBotLogs(botId, silent = false) {
                try {
                    const data = await apiCall(`/api/bots/${botId}/logs`);
                    botConsole.textContent = data.logs || 'No logs yet.';
                } catch (e) {
                    if (!silent) botConsole.textContent = 'Error loading logs.';
                }
            }

            async function handleBotAction(botId, action) {
                try {
                    if (action === 'start') {
                        await apiCall(`/api/bots/${botId}/start`, { method: 'POST' });
                    } else if (action === 'stop') {
                        await apiCall(`/api/bots/${botId}/stop`, { method: 'POST' });
                    } else if (action === 'restart') {
                        await apiCall(`/api/bots/${botId}/restart`, { method: 'POST' });
                    } else if (action === 'delete') {
                        const confirmed = await customConfirm('Delete this bot?', '🗑️');
                        if (!confirmed) return;
                        await apiCall(`/api/bots/${botId}`, { method: 'DELETE' });
                        if (selectedBotId === botId) {
                            selectedBotId = null;
                            if (botLogInterval) clearInterval(botLogInterval);
                            botConsole.textContent = 'Bot deleted.';
                        }
                    } else {
                        return;
                    }
                    await loadBots();
                } catch (e) {
                    await customAlert(e.message || 'Action failed', '❌');
                }
            }

            async function openEditModal(botId) {
                try {
                    const data = await apiCall(`/api/bots/${botId}/content`);
                    const content = data.content || '';
                    const bodyHTML = `
                        <div style="margin-bottom:8px;">
                            <button class="btn-sm" id="copyAllBtn" style="padding:6px 14px;font-size:0.55rem;border:1px solid #33ddff;color:#33ddff;background:transparent;border-radius:6px;cursor:pointer;">
                                📋 Copy All
                            </button>
                        </div>
                        <textarea id="editFileContent" rows="15" style="width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:0.7rem;resize:vertical;tab-size:4;">${escapeHtml(content)}</textarea>
                    `;
                    const result = await showCustomModal('✎ Edit File', bodyHTML, [
                        { label: 'Cancel', value: false, className: 'btn-cancel' },
                        { label: '💾 SAVE', value: true, className: 'btn-confirm' }
                    ]);
                    if (result) {
                        const newContent = document.getElementById('editFileContent').value;
                        try {
                            await apiCall(`/api/bots/${botId}/content`, {
                                method: 'PUT',
                                body: JSON.stringify({ content: newContent })
                            });
                            await customAlert('File saved and bot restarted (if running).', '✅');
                            await loadBots();
                        } catch (e) {
                            await customAlert(e.message, '❌');
                        }
                    }
                    setTimeout(() => {
                        const copyBtn = document.getElementById('copyAllBtn');
                        if (copyBtn) {
                            copyBtn.onclick = function() {
                                const textarea = document.getElementById('editFileContent');
                                textarea.select();
                                try {
                                    navigator.clipboard.writeText(textarea.value).then(() => {
                                        customAlert('📋 Copied all code!', '✅');
                                    }).catch(() => {
                                        document.execCommand('copy');
                                        customAlert('📋 Copied!', '✅');
                                    });
                                } catch(e) {
                                    document.execCommand('copy');
                                    customAlert('📋 Copied!', '✅');
                                }
                            };
                        }
                    }, 100);
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            // ---------- BOT UPLOAD ----------
            uploadCardBot.addEventListener('click', function(e) {
                if (e.target.closest('.deploy-btn')) return;
                fileInputBot.click();
            });

            deployBtnBot.addEventListener('click', async function() {
                if (fileInputBot.files.length === 0) {
                    await customAlert('Please select at least one file first.', '⚠️');
                    return;
                }
                const formData = new FormData();
                for (let i = 0; i < fileInputBot.files.length; i++) {
                    formData.append('files[]', fileInputBot.files[i]);
                }
                try {
                    deployBtnBot.textContent = 'UPLOADING...';
                    deployBtnBot.disabled = true;
                    const res = await fetch('/upload', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert(`Uploaded! ${data.bots_created} bot(s) created.`, '✅');
                        await loadBots();
                        fileInputBot.value = '';
                        fileCountDisplayBot.textContent = '';
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                } finally {
                    deployBtnBot.textContent = 'DEPLOY BOT';
                    deployBtnBot.disabled = false;
                }
            });

            fileInputBot.addEventListener('change', function() {
                const count = this.files.length;
                if (count === 0) {
                    fileCountDisplayBot.textContent = '';
                } else {
                    const names = Array.from(this.files).map(f => f.name).join(', ');
                    fileCountDisplayBot.textContent = `${count} file(s) selected: ${names}`;
                }
            });

            // ---------- CLI TOOLS ----------
            async function loadCliTools() {
                try {
                    const tools = await apiCall('/api/cli_tools');
                    renderCliTools(tools);
                } catch (e) {
                    console.error('Failed to load CLI tools:', e);
                    cliToolListContainer.innerHTML = `<div class="empty-msg">Error loading CLI tools</div>`;
                }
            }

            function renderCliTools(tools) {
                if (!tools || tools.length === 0) {
                    cliToolListContainer.innerHTML = `<div class="empty-msg">No CLI tools. Upload a project!</div>`;
                    return;
                }
                let html = '';
                tools.forEach(tool => {
                    const statusClass = tool.status === 'running' ? 'running' : 'stopped';
                    const selected = (tool.id === selectedCliToolId) ? 'selected' : '';
                    html += `
                        <div class="bot-card ${selected}" data-id="${tool.id}">
                            <div class="bot-header">
                                <span class="bot-name">${escapeHtml(tool.startup_file)}</span>
                                <span class="bot-status ${statusClass}">● ${tool.status.toUpperCase()}</span>
                            </div>
                            <div class="bot-owner">👤 ${escapeHtml(tool.owner_username)}</div>
                            <div class="cli-tool-controls">
                                <button class="btn-start" data-action="start-cli">▶ START</button>
                                <button class="btn-stop" data-action="stop-cli">⏹ STOP</button>
                                <button class="btn-delete" data-action="delete-cli">🗑 DELETE</button>
                                <button class="btn-download" data-action="download-cli">⬇ DOWNLOAD</button>
                            </div>
                        </div>
                    `;
                });
                cliToolListContainer.innerHTML = html;

                document.querySelectorAll('#cliToolListContainer .bot-card').forEach(card => {
                    card.addEventListener('click', function(e) {
                        if (e.target.closest('button')) return;
                        const id = parseInt(this.dataset.id);
                        selectCliTool(id);
                    });
                });

                document.querySelectorAll('#cliToolListContainer [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const card = this.closest('.bot-card');
                        const toolId = parseInt(card.dataset.id);
                        if (action === 'start-cli') {
                            await cliAction(toolId, 'start');
                        } else if (action === 'stop-cli') {
                            await cliAction(toolId, 'stop');
                        } else if (action === 'delete-cli') {
                            const confirmed = await customConfirm('Delete this CLI tool?', '🗑️');
                            if (confirmed) {
                                await cliAction(toolId, 'delete');
                            }
                        } else if (action === 'download-cli') {
                            window.open(`/api/cli_tool/${toolId}/download`, '_blank');
                        }
                    });
                });

                if (!selectedCliToolId && tools.length > 0) {
                    selectCliTool(tools[0].id);
                }
            }

            async function cliAction(toolId, action) {
                try {
                    const res = await apiCall(`/api/cli_tool/${toolId}/${action}`, { method: 'POST' });
                    if (res.success) {
                        if (action === 'start') {
                            cliProcessRunning = true;
                            cliTerminalInput.disabled = false;
                            cliSendBtn.disabled = false;
                            cliUploadFileBtn.disabled = false;
                            cliStatusBadge.textContent = '● RUNNING';
                            cliStatusBadge.className = 'status-badge status-running';
                            // Load logs for this tool
                            loadCliLogs(toolId);
                            if (cliLogInterval) clearInterval(cliLogInterval);
                            cliLogInterval = setInterval(() => loadCliLogs(toolId), 2000);
                        } else if (action === 'stop' || action === 'delete') {
                            cliProcessRunning = false;
                            cliTerminalInput.disabled = true;
                            cliSendBtn.disabled = true;
                            cliUploadFileBtn.disabled = true;
                            cliStatusBadge.textContent = '● STOPPED';
                            cliStatusBadge.className = 'status-badge status-stopped';
                            if (cliLogInterval) clearInterval(cliLogInterval);
                            if (action === 'delete') {
                                cliTerminal.textContent = 'CLI tool deleted.';
                                cliUploadStatus.style.display = 'none';
                                if (selectedCliToolId === toolId) {
                                    selectedCliToolId = null;
                                }
                            }
                        }
                        await loadCliTools();
                    } else {
                        await customAlert(res.error || 'Action failed', '❌');
                    }
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            function selectCliTool(id) {
                selectedCliToolId = id;
                document.querySelectorAll('#cliToolListContainer .bot-card').forEach(c => c.classList.remove('selected'));
                const card = document.querySelector(`#cliToolListContainer .bot-card[data-id="${id}"]`);
                if (card) card.classList.add('selected');
                // Check status
                fetch('/api/cli_tools').then(r => r.json()).then(tools => {
                    const tool = tools.find(t => t.id === id);
                    if (tool) {
                        if (tool.status === 'running') {
                            cliProcessRunning = true;
                            cliTerminalInput.disabled = false;
                            cliSendBtn.disabled = false;
                            cliUploadFileBtn.disabled = false;
                            cliStatusBadge.textContent = '● RUNNING';
                            cliStatusBadge.className = 'status-badge status-running';
                            loadCliLogs(id);
                            if (cliLogInterval) clearInterval(cliLogInterval);
                            cliLogInterval = setInterval(() => loadCliLogs(id), 2000);
                        } else {
                            cliProcessRunning = false;
                            cliTerminalInput.disabled = true;
                            cliSendBtn.disabled = true;
                            cliUploadFileBtn.disabled = true;
                            cliStatusBadge.textContent = '● STOPPED';
                            cliStatusBadge.className = 'status-badge status-stopped';
                            if (cliLogInterval) clearInterval(cliLogInterval);
                            cliTerminal.textContent = 'Select a CLI tool to start...';
                        }
                    }
                });
            }

            async function loadCliLogs(toolId) {
                try {
                    const data = await apiCall(`/api/cli_tool/${toolId}/logs`);
                    if (data.logs) {
                        cliTerminal.textContent = data.logs;
                    } else {
                        cliTerminal.textContent = 'No logs yet.';
                    }
                    cliTerminal.scrollTop = cliTerminal.scrollHeight;
                } catch (e) {
                    // ignore
                }
            }

            // CLI Upload
            uploadCardCli.addEventListener('click', function(e) {
                if (e.target.closest('.deploy-btn')) return;
                fileInputCli.click();
            });

            deployBtnCli.addEventListener('click', async function() {
                if (fileInputCli.files.length === 0) {
                    await customAlert('Please select at least one file first.', '⚠️');
                    return;
                }
                const formData = new FormData();
                for (let i = 0; i < fileInputCli.files.length; i++) {
                    formData.append('files[]', fileInputCli.files[i]);
                }
                try {
                    deployBtnCli.textContent = 'UPLOADING...';
                    deployBtnCli.disabled = true;
                    const res = await fetch('/upload_cli', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert(`CLI tool uploaded! ID: ${data.tool_id}`, '✅');
                        await loadCliTools();
                        fileInputCli.value = '';
                        fileCountDisplayCli.textContent = '';
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                } finally {
                    deployBtnCli.textContent = 'DEPLOY CLI TOOL';
                    deployBtnCli.disabled = false;
                }
            });

            fileInputCli.addEventListener('change', function() {
                const count = this.files.length;
                if (count === 0) {
                    fileCountDisplayCli.textContent = '';
                } else {
                    const names = Array.from(this.files).map(f => f.name).join(', ');
                    fileCountDisplayCli.textContent = `${count} file(s) selected: ${names}`;
                }
            });

            // CLI Terminal Input
            cliSendBtn.addEventListener('click', async function() {
                const data = cliTerminalInput.value;
                if (!data || !selectedCliToolId) return;
                try {
                    await apiCall(`/api/cli_tool/${selectedCliToolId}/send_input`, {
                        method: 'POST',
                        body: JSON.stringify({ input: data })
                    });
                    cliTerminal.textContent += `\n> ${data}`;
                    cliTerminalInput.value = '';
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            });

            cliTerminalInput.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    cliSendBtn.click();
                }
            });

            cliClearBtn.addEventListener('click', function() {
                if (selectedCliToolId) {
                    loadCliLogs(selectedCliToolId);
                } else {
                    cliTerminal.textContent = 'Select a CLI tool to start...';
                }
            });

            // CLI File Upload
            cliUploadFileBtn.addEventListener('click', function() {
                cliFileUploadInput.click();
            });

            cliFileUploadInput.addEventListener('change', async function() {
                const file = this.files[0];
                if (!file || !selectedCliToolId) return;
                const formData = new FormData();
                formData.append('file', file);
                try {
                    const res = await fetch(`/api/cli_tool/${selectedCliToolId}/upload_file`, {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        cliUploadStatus.style.display = 'block';
                        cliUploadStatus.innerHTML = `<span style="color:#00ff6a;">✅ Uploaded: ${data.filename}</span>`;
                        if (cliUploadResolve) {
                            cliUploadResolve(data.filepath);
                            cliUploadResolve = null;
                        }
                        setTimeout(() => {
                            cliUploadStatus.style.display = 'none';
                        }, 5000);
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
                this.value = '';
            });

            // ---------- ADMIN PANEL ----------
            function openAdminPanel() {
                if (!currentUser) {
                    customAlert('Please login first.', '⚠️');
                    return;
                }
                if (currentUser.role !== 'admin') {
                    const username = currentUser.username;
                    const password = '{{ user_password }}';
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <div style="font-size:20px; margin-bottom:20px;">👤 Your Profile</div>
                            <div style="background:#161b25; padding:15px; border-radius:10px; margin-bottom:10px;">
                                <strong style="color:#00e5ff;">Username</strong><br />
                                <span style="font-size:18px; color:#fff;">${username}</span>
                            </div>
                            <div style="background:#161b25; padding:15px; border-radius:10px;">
                                <strong style="color:#00e5ff;">Password</strong><br />
                                <span style="font-size:18px; color:#fff;">${password}</span>
                            </div>
                        </div>
                    `;
                    showCustomModal('ℹ️', bodyHTML, [
                        { label: 'OK', value: true, className: 'btn-ok' }
                    ]);
                    return;
                }
                loadAdminUsers();
                adminOverlay.classList.add('open');
            }

            function closeAdminPanel() {
                adminOverlay.classList.remove('open');
            }

            // ---------- USER MANAGEMENT ----------
            async function loadAdminUsers() {
                try {
                    const users = await apiCall('/api/users');
                    renderFullUserList(users);
                    renderSimpleLists(users);
                } catch (e) {
                    console.error('Failed to load users:', e);
                }
            }

            function renderFullUserList(users) {
                if (!users || users.length === 0) {
                    fullUserListContainer.innerHTML = `<div class="empty-msg">No users found.</div>`;
                    return;
                }
                let html = '';
                users.forEach((u, idx) => {
                    const bannedClass = u.banned ? 'banned' : (u.role === 'admin' ? 'admin' : 'user');
                    const bannedText = u.banned ? 'UNBAN' : 'BAN';
                    const roleLabel = u.role.toUpperCase();
                    let expiryDisplay = 'Never';
                    if (u.expires_at) {
                        try {
                            const exp = new Date(u.expires_at);
                            expiryDisplay = exp.toLocaleString();
                        } catch(e) { expiryDisplay = 'Invalid'; }
                    }
                    html += `
                        <div class="list-item" data-username="${u.username}">
                            <div class="row">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                    <span style="font-size:12px;color:#888;">Expires: ${expiryDisplay}</span>
                                </div>
                                <span class="badge-role ${bannedClass}">${u.banned ? 'BANNED' : roleLabel}</span>
                            </div>
                            <div class="row">
                                <div class="limit-group">
                                    <label>Limit:</label>
                                    <input type="number" class="limit-input" value="${u.limit || 0}" min="0" step="1" />
                                </div>
                                <button class="btn-action btn-set" data-action="setLimit" data-username="${u.username}">SET</button>
                                <button class="btn-action btn-ban" data-action="toggleBan" data-username="${u.username}">${bannedText}</button>
                            </div>
                            <div class="row">
                                <input type="text" placeholder="New password..." style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newPass" />
                                <button class="btn-action btn-reset" data-action="resetPass" data-username="${u.username}">RESET PW</button>
                            </div>
                            <div class="row">
                                <input type="text" placeholder="New expiry (e.g. 5, 1m, 2h)" style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newExpiry" />
                                <button class="btn-action btn-set" data-action="setExpiry" data-username="${u.username}">SET EXPIRY</button>
                            </div>
                            <button class="btn-action btn-del" data-action="deleteUser" data-username="${u.username}">DELETE USER + ALL BOTS</button>
                        </div>
                    `;
                });
                fullUserListContainer.innerHTML = html;
                attachFullListEvents();
            }

            function attachFullListEvents() {
                document.querySelectorAll('#fullUserListContainer [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const username = this.dataset.username;
                        const card = this.closest('.list-item');
                        if (action === 'setLimit') {
                            const input = card.querySelector('.limit-input');
                            const val = parseInt(input.value, 10);
                            if (isNaN(val) || val < 0) {
                                await customAlert('Enter a valid number.', '⚠️');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ limit: val })
                                });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'toggleBan') {
                            const user = (await apiCall('/api/users')).find(u => u.username === username);
                            if (!user) return;
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ banned: !user.banned })
                                });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'resetPass') {
                            const passInput = card.querySelector('[data-field="newPass"]');
                            const newPass = passInput.value.trim();
                            if (!newPass) {
                                await customAlert('Enter a new password.', '⚠️');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ password: newPass })
                                });
                                passInput.value = '';
                                await customAlert('Password updated.', '✅');
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'setExpiry') {
                            const expiryInput = card.querySelector('[data-field="newExpiry"]');
                            const expiry = expiryInput.value.trim();
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ expiry: expiry })
                                });
                                expiryInput.value = '';
                                await customAlert('Expiry updated.', '✅');
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'deleteUser') {
                            const confirmed = await customConfirm(`Delete user ${username} and all their bots?`, '🗑️');
                            if (!confirmed) return;
                            if (username === currentUser.username) {
                                await customAlert('Cannot delete yourself.', '🚫');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, { method: 'DELETE' });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        }
                    });
                });
            }

            function renderSimpleLists(users) {
                const admins = users.filter(u => u.role === 'admin' && !u.banned);
                const regulars = users.filter(u => u.role === 'user' && !u.banned);

                if (!admins.length) {
                    simpleAdminListContainer.innerHTML = `<div class="empty-msg">No admins.</div>`;
                } else {
                    let html = '';
                    admins.forEach(u => {
                        html += `
                            <div class="simple-list-item" data-username="${u.username}">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                </div>
                                <div class="actions">
                                    <button class="btn-remove-simple" data-username="${u.username}">REMOVE</button>
                                </div>
                            </div>
                        `;
                    });
                    simpleAdminListContainer.innerHTML = html;
                }

                if (!regulars.length) {
                    simpleUserListContainer.innerHTML = `<div class="empty-msg">No users.</div>`;
                } else {
                    let html = '';
                    regulars.forEach(u => {
                        html += `
                            <div class="simple-list-item" data-username="${u.username}">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                </div>
                                <div class="actions">
                                    <button class="btn-remove-simple" data-username="${u.username}">REMOVE</button>
                                </div>
                            </div>
                        `;
                    });
                    simpleUserListContainer.innerHTML = html;
                }

                document.querySelectorAll('.btn-remove-simple').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const username = this.dataset.username;
                        const confirmed = await customConfirm(`Remove user ${username}?`, '🗑️');
                        if (!confirmed) return;
                        if (username === currentUser.username) {
                            await customAlert('Cannot remove yourself.', '🚫');
                            return;
                        }
                        try {
                            await apiCall(`/api/users/${username}`, { method: 'DELETE' });
                            await loadAdminUsers();
                        } catch (e) {
                            await customAlert(e.message, '❌');
                        }
                    });
                });
            }

            // ---------- CREATE USER ----------
            async function handleCreateUser() {
                const username = newUsername.value.trim();
                const password = newPassword.value.trim();
                const expiry = newExpiry.value.trim();
                const role = newRole.value;
                if (!username || !password) {
                    await customAlert('Username and Password required.', '⚠️');
                    return;
                }
                try {
                    await apiCall('/api/users', {
                        method: 'POST',
                        body: JSON.stringify({ username, password, role, expiry })
                    });
                    await loadAdminUsers();
                    newUsername.value = '';
                    newPassword.value = '';
                    newExpiry.value = '';
                    createUserForm.style.display = 'none';
                    await customAlert(`User ${username} created.`, '✅');
                } catch (e) {
                    await customAlert(e.message || 'Creation failed', '❌');
                }
            }

            // ---------- FILE MANAGER ----------
            const fileManagerList = document.getElementById('fileManagerList');
            const fileBreadcrumb = document.getElementById('fileBreadcrumb');
            const contextMenu = document.getElementById('fileContextMenu');
            const ctxDelete = document.getElementById('ctxDelete');
            const ctxRename = document.getElementById('ctxRename');
            const ctxDownload = document.getElementById('ctxDownload');

            let currentPath = '';
            let selectedFilePath = null;

            async function loadDirectory(path = '') {
                currentPath = path;
                try {
                    const res = await fetch(`/api/files?path=${encodeURIComponent(path)}`);
                    if (!res.ok) {
                        const err = await res.json();
                        await customAlert(err.error || 'Failed to load', '❌');
                        return;
                    }
                    const data = await res.json();
                    renderFileList(data);
                } catch (e) {
                    await customAlert('Error: ' + e.message, '❌');
                }
            }

            function renderFileList(data) {
                const items = data.items || [];
                let breadHtml = '';
                const parts = currentPath.split('/').filter(p => p);
                let cum = '';
                breadHtml += `<span onclick="window._loadDirectory('')">📁 root</span>`;
                parts.forEach((p, idx) => {
                    cum += (cum ? '/' : '') + p;
                    breadHtml += `<span class="sep">/</span><span onclick="window._loadDirectory('${cum}')">${escapeHtml(p)}</span>`;
                });
                fileBreadcrumb.innerHTML = breadHtml;

                let html = '';
                if (currentPath) {
                    html += `<div class="file-item" onclick="window._loadDirectory('${currentPath.split('/').slice(0, -1).join('/')}')">
                        <span class="name"><i class="fa-solid fa-arrow-up"></i> ..</span>
                    </div>`;
                }
                items.forEach(item => {
                    const icon = item.type === 'directory' ? '<i class="fa-solid fa-folder dir-icon"></i>' : '<i class="fa-solid fa-file"></i>';
                    const sizeText = item.type === 'file' ? (item.size / 1024).toFixed(1) + ' KB' : '';
                    html += `
                        <div class="file-item" data-path="${item.path}" data-type="${item.type}">
                            <span class="name">${icon} ${escapeHtml(item.name)}</span>
                            <span class="size">${sizeText}</span>
                        </div>
                    `;
                });
                fileManagerList.innerHTML = html;

                document.querySelectorAll('.file-item').forEach(el => {
                    el.addEventListener('click', function(e) {
                        const path = this.dataset.path;
                        const type = this.dataset.type;
                        if (type === 'directory') {
                            window._loadDirectory(path);
                        } else {
                            document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                            this.classList.add('selected');
                            selectedFilePath = path;
                        }
                    });

                    let timer;
                    el.addEventListener('touchstart', function(e) {
                        timer = setTimeout(() => {
                            e.preventDefault();
                            const path = this.dataset.path;
                            showContextMenu(e.touches[0].clientX, e.touches[0].clientY, path);
                            document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                            this.classList.add('selected');
                            selectedFilePath = path;
                        }, 3000);
                    });
                    el.addEventListener('touchend', function() { clearTimeout(timer); });
                    el.addEventListener('touchmove', function() { clearTimeout(timer); });

                    el.addEventListener('contextmenu', function(e) {
                        e.preventDefault();
                        const path = this.dataset.path;
                        showContextMenu(e.clientX, e.clientY, path);
                        document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                        this.classList.add('selected');
                        selectedFilePath = path;
                    });
                });
            }

            function showContextMenu(x, y, path) {
                contextMenu.style.display = 'block';
                contextMenu.style.left = x + 'px';
                contextMenu.style.top = y + 'px';
                contextMenu.dataset.path = path;
            }

            function hideContextMenu() {
                contextMenu.style.display = 'none';
            }

            ctxDelete.addEventListener('click', async function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                const confirmed = await customConfirm(`Delete ${path}?`, '🗑️');
                if (!confirmed) return;
                try {
                    const res = await fetch('/api/files/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path })
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Deleted.', '✅');
                        loadDirectory(currentPath);
                    } else {
                        await customAlert(data.error || 'Delete failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            });

            ctxRename.addEventListener('click', async function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                const newName = await customPrompt('Enter new name:', path.split('/').pop());
                if (newName === null) return;
                if (!newName.trim()) {
                    await customAlert('Name cannot be empty.', '⚠️');
                    return;
                }
                try {
                    const res = await fetch('/api/files/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ old_path: path, new_name: newName.trim() })
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Renamed.', '✅');
                        loadDirectory(currentPath);
                    } else {
                        await customAlert(data.error || 'Rename failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            });

            ctxDownload.addEventListener('click', function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                window.open(`/api/files/download?path=${encodeURIComponent(path)}`, '_blank');
            });

            function customPrompt(message, defaultValue) {
                return new Promise((resolve) => {
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <p style="margin-bottom:12px;">${message}</p>
                            <input type="text" id="promptInput" value="${escapeHtml(defaultValue || '')}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                        </div>
                    `;
                    showCustomModal('✏️', bodyHTML, [
                        { label: 'Cancel', value: null, className: 'btn-cancel' },
                        { label: 'OK', value: true, className: 'btn-confirm' }
                    ]).then((result) => {
                        if (result === null) resolve(null);
                        else {
                            const val = document.getElementById('promptInput')?.value;
                            resolve(val);
                        }
                    });
                });
            }

            window._loadDirectory = function(path) {
                hideContextMenu();
                loadDirectory(path);
            };

            document.addEventListener('click', function(e) {
                if (!contextMenu.contains(e.target)) {
                    hideContextMenu();
                }
            });

            // ---------- ADMIN TAB SWITCHING ----------
            const adminTabBtns = document.querySelectorAll('.admin-tabs button');
            const adminTabContents = {
                tabAdminMenu: document.getElementById('tabAdminMenu'),
                tabUserMenu: document.getElementById('tabUserMenu'),
                tabTerminal: document.getElementById('tabTerminal'),
                tabFileManager: document.getElementById('tabFileManager'),
                tabStats: document.getElementById('tabStats')
            };

            adminTabBtns.forEach(btn => {
                btn.addEventListener('click', function() {
                    const tabId = this.dataset.tab;
                    adminTabBtns.forEach(b => b.classList.remove('active'));
                    this.classList.add('active');
                    Object.keys(adminTabContents).forEach(key => {
                        if (adminTabContents[key]) {
                            adminTabContents[key].classList.toggle('active', key === tabId);
                        }
                    });
                    if (tabId === 'tabTerminal') {
                        setTimeout(() => terminalCommand.focus(), 100);
                        if (!terminalPollInterval) {
                            startTerminalPolling();
                        }
                    }
                    if (tabId === 'tabFileManager') {
                        loadDirectory('');
                    }
                    if (tabId === 'tabStats') {
                        fetchStats();
                        if (window.statsInterval) clearInterval(window.statsInterval);
                        window.statsInterval = setInterval(fetchStats, 5000);
                    } else {
                        if (window.statsInterval) {
                            clearInterval(window.statsInterval);
                            window.statsInterval = null;
                        }
                    }
                });
            });

            // ---------- TERMINAL ----------
            async function startTerminalPolling() {
                if (terminalPollInterval) clearInterval(terminalPollInterval);
                try {
                    await fetch('/api/terminal/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password: '{{ password }}' })
                    });
                } catch (e) {
                    console.error('Failed to start terminal:', e);
                }
                terminalPollInterval = setInterval(async () => {
                    try {
                        const res = await fetch('/api/terminal/read');
                        const data = await res.json();
                        if (data.output) {
                            terminalOutput.innerHTML += `<span class="output">${escapeHtml(data.output)}</span>`;
                            terminalOutput.scrollTop = terminalOutput.scrollHeight;
                        }
                        isTerminalRunning = data.running;
                        termStopBtn.disabled = !isTerminalRunning;
                    } catch (e) {
                        // ignore
                    }
                }, 500);
            }

            async function sendTerminalData(data) {
                try {
                    await fetch('/api/terminal/send', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: data, password: '{{ password }}' })
                    });
                } catch (e) {
                    console.error('Failed to send terminal input:', e);
                }
            }

            async function stopTerminal() {
                try {
                    await fetch('/api/terminal/stop', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password: '{{ password }}' })
                    });
                    terminalOutput.innerHTML += `<span class="prompt">[Terminal stopped]</span><br />`;
                    isTerminalRunning = false;
                    termStopBtn.disabled = true;
                    setTimeout(() => {
                        startTerminalPolling();
                    }, 500);
                } catch (e) {
                    console.error('Failed to stop terminal:', e);
                }
            }

            function clearTerminal() {
                terminalOutput.innerHTML = '<span class="prompt">$ </span>Terminal cleared.<br />';
            }

            termRunBtn.addEventListener('click', function() {
                const data = terminalCommand.value;
                if (!data) return;
                if (!isTerminalRunning) {
                    // try to start
                }
                sendTerminalData(data);
                terminalCommand.value = '';
            });

            termStopBtn.addEventListener('click', stopTerminal);
            termClearBtn.addEventListener('click', clearTerminal);

            terminalCommand.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    termRunBtn.click();
                }
            });

            // ---------- STATS ----------
            async function fetchStats() {
                try {
                    const res = await fetch('/api/stats');
                    if (!res.ok) return;
                    const data = await res.json();
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
                } catch (e) {
                    console.error('Stats error:', e);
                }
            }

            document.getElementById('setOffsetBtn')?.addEventListener('click', async function() {
                const val = parseFloat(document.getElementById('offsetInput').value);
                if (isNaN(val)) {
                    await customAlert('Enter valid number', '⚠️');
                    return;
                }
                try {
                    const res = await fetch('/api/set_offset', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ offset: val })
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Offset set to ' + val + ' hrs', '✅');
                        fetchStats();
                    } else {
                        await customAlert(data.error || 'Failed', '❌');
                    }
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            });

            // ---------- EVENT BINDINGS ----------
            loginBtn.addEventListener('click', handleLogin);
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' && loginView.classList.contains('active')) {
                    handleLogin();
                }
            });

            hamburgerBtn.addEventListener('click', openAdminPanel);
            adminCloseBtn.addEventListener('click', closeAdminPanel);
            adminOverlay.addEventListener('click', function(e) {
                if (e.target === this) closeAdminPanel();
            });

            logoutBtn.addEventListener('click', handleLogout);

            toggleCreateUserBtn.addEventListener('click', function() {
                const form = document.getElementById('createUserForm');
                form.style.display = form.style.display === 'none' ? 'block' : 'none';
            });

            createUserBtn.addEventListener('click', handleCreateUser);

            // ---------- INIT ----------
            const loggedIn = {{ logged_in|tojson }};
            if (loggedIn) {
                currentUser = {
                    username: '{{ username }}',
                    role: '{{ session.get("role", "") }}'
                };
                loadWebsites();
                loadBots();
                loadCliTools();
                if (currentUser.role === 'admin') {
                    loadAdminUsers();
                }
            }

            function escapeHtml(str) {
                const div = document.createElement('div');
                div.textContent = str;
                return div.innerHTML;
            }

            function formatUptime(seconds) {
                if (seconds < 0) return '--';
                const d = Math.floor(seconds / 86400);
                const h = Math.floor((seconds % 86400) / 3600);
                const m = Math.floor((seconds % 3600) / 60);
                const s = Math.floor(seconds % 60);
                return `${d}d ${h}h ${m}m ${s}s`;
            }

            console.log('🔐 YUVICODEX System ready (Websites + Bots + CLI).');
            console.log('📋 Default accounts: admin/admin123 (admin), user1/pass123, user2/pass456');
            console.log('💻 Use upload to deploy websites, bots, or CLI tools.');
            console.log('🔑 Master Password: {{ MASTER_PASSWORD if MASTER_PASSWORD else "not set" }}');
            console.log('🔐 Secret Key: {{ SECRET_KEY if SECRET_KEY else "not set" }}');
            console.log('👉 Click logo 5 times for secret key login.');
            console.log('📡 Interactive terminal started.');
            console.log('🧹 Logs auto-clear every 1 hour.');
        })();
    </script>
</body>
</html>
"""

# ---------- MAIN START ----------
MAIN_START_TIME = int(time.time())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🚀 YUVICODEX ULTIMATE (Websites + Bots + CLI + Mini Web + Auto Logs + Project Data)")
    print(f"🌐 Port: {port}")
    print("👤 Admin: admin / admin123")
    print("📊 Stats: Owner only. Set Offset from Render Dashboard.")
    print("🌍 Websites at /<slug>/")
    print("📱 Mini Web at /mini/website/<id> or /mini/bot/<id>")
    print("🔑 Kill API Key: your_secret_kill_key_2024")
    print("   Use POST /api/kill with {key, action: kill|status|restore}")
    print("🧹 Logs auto-cleared every 1 hour.")
    print("📦 Project data stored in project_data.json inside each project folder.")
    print("🔐 Per‑website env vars supported – upload .env file or use API.")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)