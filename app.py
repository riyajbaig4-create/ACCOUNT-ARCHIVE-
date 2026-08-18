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
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, send_file, send_from_directory, redirect, Response, stream_with_context
from functools import wraps
from io import BytesIO
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash

try:
    import psutil
    PSUTIL_AVAILABLE = True
except ImportError:
    PSUTIL_AVAILABLE = False

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# ---------- CONFIG ----------
PASSWORD = "your_secure_password"
MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', 'master123')
SECRET_KEY = os.environ.get('SECRET_KEY', 'secret123')
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BASE_DIR = os.path.abspath('.')

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

# ---------- KILL SWITCH ----------
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

# ---------- SQLITE DATABASE ----------
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')

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
            email TEXT,
            password_hash TEXT,
            role TEXT DEFAULT 'user',
            plan TEXT DEFAULT 'free',
            limit INTEGER DEFAULT 5,
            status TEXT DEFAULT 'active',
            expires_at TEXT,
            session_version INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            last_login TIMESTAMP
        )''')
        
        # Bots Table
        conn.execute('''CREATE TABLE IF NOT EXISTS bots (
            id TEXT PRIMARY KEY,
            user TEXT NOT NULL,
            project TEXT NOT NULL,
            filename TEXT NOT NULL,
            status TEXT DEFAULT 'stopped',
            pid INTEGER,
            start_time REAL,
            interpreter TEXT,
            is_cli INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP
        )''')
        
        # Websites Table
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
            type TEXT DEFAULT 'website'
        )''')
        
        # CLI Tools Table
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
        
        # Logs Table
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            bot_id TEXT,
            cli_tool_id INTEGER,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        # Deployments Table
        conn.execute('''CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            bot_id TEXT,
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
        
        # Config Table
        conn.execute('''CREATE TABLE IF NOT EXISTS config (
            key TEXT PRIMARY KEY,
            value TEXT
        )''')
        
        # Indexes
        conn.execute('CREATE INDEX IF NOT EXISTS idx_bots_user ON bots(user)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_cli_tools_owner ON cli_tools(owner_username)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_timestamp ON logs(timestamp)')
        
        # Default admin user
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute('INSERT INTO users (username, password_hash, role, plan, limit) VALUES (?, ?, ?, ?, ?)',
                         ('admin', generate_password_hash('admin123'), 'admin', 'pro', 999))
            conn.commit()
            print("✅ Default admin created: admin / admin123")
        
        # Config
        conn.execute('INSERT OR IGNORE INTO config (key, value) VALUES (?, ?)',
                     ('total_hours_offset', '0'))
        conn.commit()
init_db()

# ---------- HELPERS ----------
def get_user_by_username(username):
    with get_db() as conn:
        return conn.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()

def get_bot_by_id(bot_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM bots WHERE id = ?', (bot_id,)).fetchone()

def get_bots_by_user(username):
    with get_db() as conn:
        return conn.execute('SELECT * FROM bots WHERE user = ? ORDER BY created_at DESC', (username,)).fetchall()

def get_all_bots():
    with get_db() as conn:
        return conn.execute('SELECT * FROM bots ORDER BY created_at DESC').fetchall()

def create_bot(username, project_id, filename, interpreter, is_cli=0):
    bot_id = str(uuid.uuid4())[:8]
    with get_db() as conn:
        conn.execute('''INSERT INTO bots (id, user, project, filename, status, interpreter, is_cli)
                        VALUES (?, ?, ?, ?, ?, ?, ?)''',
                     (bot_id, username, project_id, filename, 'stopped', interpreter, is_cli))
        conn.commit()
    return bot_id

def update_bot_status(bot_id, status, pid=None, start_time=None):
    with get_db() as conn:
        if pid is not None and start_time is not None:
            conn.execute('UPDATE bots SET status = ?, pid = ?, start_time = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, pid, start_time, bot_id))
        elif pid is not None:
            conn.execute('UPDATE bots SET status = ?, pid = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, pid, bot_id))
        else:
            conn.execute('UPDATE bots SET status = ?, pid = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (status, bot_id))
        conn.commit()

def delete_bot_from_db(bot_id):
    with get_db() as conn:
        conn.execute('DELETE FROM bots WHERE id = ?', (bot_id,))
        conn.execute('DELETE FROM logs WHERE bot_id = ?', (bot_id,))
        conn.execute('DELETE FROM deployments WHERE bot_id = ?', (bot_id,))
        conn.commit()

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

def log_to_db(website_id=None, bot_id=None, cli_tool_id=None, log_type='info', message=''):
    with get_db() as conn:
        conn.execute('INSERT INTO logs (website_id, bot_id, cli_tool_id, log_type, log_text) VALUES (?, ?, ?, ?, ?)',
                     (website_id, bot_id, cli_tool_id, log_type, message))
        conn.commit()

def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

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

def detect_startup_file(folder):
    """Detect the main startup file from a folder"""
    STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py', 'wsgi.py', 'asgi.py']
    
    for filename in STARTUP_PRIORITY:
        if os.path.exists(os.path.join(folder, filename)):
            return filename
    
    # Scan for any executable file
    for f in os.listdir(folder):
        if get_interpreter(f):
            return f
    
    return None

def get_runtime_command(folder, filename):
    """Get the command to run a file"""
    ext = os.path.splitext(filename)[1].lower()
    
    if ext == '.py':
        # Check if Flask app
        filepath = os.path.join(folder, filename)
        try:
            with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
                content = f.read()
                if 'Flask' in content or 'app.run' in content:
                    return ['python', '-m', 'flask', 'run', '--host=0.0.0.0', '--port=5000']
        except:
            pass
        return ['python', filename]
    elif ext == '.js':
        return ['node', filename]
    elif ext == '.php':
        return ['php', '-S', '0.0.0.0:5000']
    elif ext == '.go':
        return ['go', 'run', filename]
    elif ext == '.rb':
        return ['ruby', filename]
    elif ext == '.sh':
        return ['bash', filename]
    else:
        return None

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

# ---------- PROJECT DATA (SINGLE JSON FILE) ----------
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

# ---------- LOGS AUTO-CLEAR (1 HOUR) ----------
def clear_all_logs():
    """Clear ALL logs - bot, website, cli, everything"""
    try:
        # Delete log files
        if os.path.exists(LOG_FOLDER):
            for f in os.listdir(LOG_FOLDER):
                file_path = os.path.join(LOG_FOLDER, f)
                if os.path.isfile(file_path):
                    os.remove(file_path)
        
        # Clear database logs
        with get_db() as conn:
            conn.execute('DELETE FROM logs')
            conn.commit()
        
        # Clear bot logs from project folders
        with get_db() as conn:
            bots = conn.execute('SELECT id, user, project FROM bots').fetchall()
            for bot in bots:
                project_folder = os.path.join(UPLOAD_FOLDER, bot['user'], bot['project'])
                log_file = os.path.join(project_folder, bot['id'] + '.log')
                if os.path.exists(log_file):
                    os.remove(log_file)
        
        # Clear CLI logs from project folders
        with get_db() as conn:
            cli_tools = conn.execute('SELECT id, owner_username, tool_folder FROM cli_tools').fetchall()
            for tool in cli_tools:
                log_file = os.path.join(tool['tool_folder'], 'cli.log')
                if os.path.exists(log_file):
                    os.remove(log_file)
        
        print(f"✅ All logs cleared at {datetime.now()}")
        return True
    except Exception as e:
        print(f"Error clearing logs: {e}")
        return False

def schedule_log_clear():
    clear_all_logs()
    threading.Timer(3600, schedule_log_clear).start()  # 1 hour

# Start log auto-clear
schedule_log_clear()

# ---------- BOT FUNCTIONS ----------
def get_bot_log_file(bot):
    project_folder = os.path.join(get_user_folder(bot['user']), bot['project'])
    return os.path.join(project_folder, bot['id'] + '.log')

def start_bot_by_id(bot_id):
    bot = get_bot_by_id(bot_id)
    if not bot:
        return False, "Bot not found"
    if bot['status'] == 'running':
        return False, "Already running"
    
    username = bot['user']
    user = get_user_by_username(username)
    if user:
        with get_db() as conn:
            running = conn.execute('SELECT COUNT(*) FROM bots WHERE user = ? AND status = ?', 
                                  (username, 'running')).fetchone()[0]
            if running >= user['limit']:
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
    
    # Project data
    project_data = ProjectData(project_folder)
    project_data.add_log(f"Starting {bot['filename']}...", "info")
    project_data.update_stat("last_start", datetime.now().isoformat())
    project_data.update_stat("start_count", project_data.get_stat("start_count", 0) + 1)
    
    try:
        proc = subprocess.Popen(
            [interpreter, bot['filename']],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            cwd=project_folder,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        update_bot_status(bot_id, 'running', proc.pid, time.time())
        processes[bot_id] = proc
        project_data.add_log(f"Started with PID {proc.pid}", "success")
        return True, None
    except Exception as e:
        project_data.add_log(f"Error: {str(e)}", "error")
        return False, str(e)

def stop_bot_by_id(bot_id):
    bot = get_bot_by_id(bot_id)
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
    
    update_bot_status(bot_id, 'stopped')
    
    project_folder = os.path.join(get_user_folder(bot['user']), bot['project'])
    project_data = ProjectData(project_folder)
    project_data.add_log("Bot stopped", "info")
    
    return True, None

# ---------- WEBSITE FUNCTIONS ----------
def start_website_process(website_id, log_callback=None):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        return False, "Folder not found"
    
    # Find startup file
    startup_file = detect_startup_file(folder)
    if not startup_file:
        return False, "No startup file detected"
    
    # Get runtime command
    cmd = get_runtime_command(folder, startup_file)
    if not cmd:
        return False, "Unsupported file type"
    
    allocated_port = get_next_available_port()
    
    env = os.environ.copy()
    env['PORT'] = str(allocated_port)
    env['PYTHONUNBUFFERED'] = '1'
    
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    
    try:
        proc = subprocess.Popen(
            cmd,
            cwd=folder,
            env=env,
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        
        update_website_status(website_id, 'running', proc.pid, allocated_port)
        
        # Project data
        project_data = ProjectData(folder)
        project_data.add_log(f"Started on port {allocated_port}", "success")
        project_data.update_stat("last_start", datetime.now().isoformat())
        
        return True, f"Running on port {allocated_port}"
    except Exception as e:
        return False, str(e)

def stop_website_process(website_id):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
    
    pid = website['pid']
    if not pid:
        return False, "No running process"
    
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(1)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except:
        pass
    
    update_website_status(website_id, 'stopped', None, None)
    return True, "Stopped"

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

# ---------- CLI TOOL FUNCTIONS ----------
cli_processes = {}

def get_cli_tool_by_id(tool_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM cli_tools WHERE id = ?', (tool_id,)).fetchone()

def start_cli_tool(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ?', (tool_id,)).fetchone()
    
    if not tool:
        return False, "Tool not found"
    if tool['status'] == 'running':
        return False, "Already running"
    
    tool_folder = tool['tool_folder']
    startup_file = tool['startup_file']
    interpreter = tool['interpreter']
    
    if not interpreter:
        interpreter = get_interpreter(startup_file)
    
    if not interpreter:
        return False, "No interpreter found"
    
    log_file = os.path.join(tool_folder, 'cli.log')
    
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
        return True, f"Started with PID {proc.pid}"
    except Exception as e:
        return False, str(e)

def stop_cli_tool(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ?', (tool_id,)).fetchone()
    
    if not tool:
        return False, "Tool not found"
    
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
    
    return True, "Stopped"

def send_cli_input(tool_id, input_data):
    proc = cli_processes.get(tool_id)
    if not proc:
        return False, "Process not running"
    
    try:
        proc.stdin.write((input_data + '\n').encode())
        proc.stdin.flush()
        return True, "Input sent"
    except Exception as e:
        return False, str(e)

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
    global users_db
    user_folder = get_user_folder(username)
    if os.path.exists(user_folder):
        shutil.rmtree(user_folder, ignore_errors=True)
    
    with get_db() as conn:
        conn.execute('DELETE FROM bots WHERE user = ?', (username,))
        conn.execute('DELETE FROM websites WHERE owner_username = ?', (username,))
        conn.execute('DELETE FROM cli_tools WHERE owner_username = ?', (username,))
        conn.execute('DELETE FROM logs WHERE bot_id IN (SELECT id FROM bots WHERE user = ?)', (username,))
        conn.execute('DELETE FROM logs WHERE website_id IN (SELECT id FROM websites WHERE owner_username = ?)', (username,))
        conn.execute('DELETE FROM logs WHERE cli_tool_id IN (SELECT id FROM cli_tools WHERE owner_username = ?)', (username,))
        conn.commit()
    
    users_db = [u for u in users_db if u['username'] != username]
    save_users(users_db)
    
    if session.get('username') == username:
        session.clear()

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

    global KILL_SWITCH_ACTIVE
    if KILL_SWITCH_ACTIVE:
        return "404 Not Found<br>The requested URL was not found on this server.", 404

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

# ---------- ROUTES ----------
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
                                   user_password=user_password)

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

# --- User Management ---
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

# --- Profile ---
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
        save_users(users_db)
    
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
    if session.get('role') == 'admin':
        bots = get_all_bots()
    else:
        bots = get_bots_by_user(username)
    
    result = []
    for bot in bots:
        bot = dict(bot)
        filepath = os.path.join(get_user_folder(bot['user']), bot['project'], bot['filename'])
        token, bot_username = detect_bot_token(filepath) if os.path.exists(filepath) else (None, None)
        bot['has_token'] = bool(token)
        bot['bot_username'] = bot_username
        result.append(bot)
    return jsonify(result)

@app.route('/api/bots/<bot_id>/logs', methods=['GET'])
@login_required
def get_bot_logs(bot_id):
    bot = get_bot_by_id(bot_id)
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
    bot = get_bot_by_id(bot_id)
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
    bot = get_bot_by_id(bot_id)
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
    time.sleep(1)
    return start_bot(bot_id)

@app.route('/api/bots/<bot_id>', methods=['DELETE'])
@login_required
def delete_bot(bot_id):
    bot = get_bot_by_id(bot_id)
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

    project_folder = os.path.join(get_user_folder(username), bot['project'])
    remaining_bots = get_bots_by_user(username)
    remaining = [b for b in remaining_bots if b['project'] == bot['project']]
    if not remaining:
        shutil.rmtree(project_folder, ignore_errors=True)
    
    delete_bot_from_db(bot_id)
    return jsonify({'success': True})

@app.route('/api/bots/<bot_id>/download', methods=['GET'])
@login_required
def download_bot(bot_id):
    bot = get_bot_by_id(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403

    project_folder = os.path.join(get_user_folder(username), bot['project'])
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(project_folder):
            for root, _, files_in_folder in os.walk(project_folder):
                for fname in files_in_folder:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, project_folder)
                    zipf.write(full_path, arcname)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"{bot['project']}.zip")

@app.route('/api/bots/<bot_id>/content', methods=['GET'])
@login_required
def get_bot_content(bot_id):
    bot = get_bot_by_id(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    filepath = os.path.join(get_user_folder(username), bot['project'], bot['filename'])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content})

@app.route('/api/bots/<bot_id>/content', methods=['PUT'])
@login_required
def update_bot_content(bot_id):
    bot = get_bot_by_id(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    new_content = data.get('content', '')
    filepath = os.path.join(get_user_folder(username), bot['project'], bot['filename'])
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    if bot['status'] == 'running':
        stop_bot_by_id(bot_id)
        start_bot_by_id(bot_id)
    return jsonify({'success': True})

# ---------- BOT UPLOAD ----------
@app.route('/upload', methods=['POST'])
@login_required
def upload_bot():
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
                    bot_id = create_bot(username, project_id, fname, interpreter, 0)
                    created_bots.append(bot_id)

    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    # Start bots
    for bid in created_bots:
        start_bot_by_id(bid)

    return jsonify({
        'success': True,
        'project_id': project_id,
        'bots_created': len(created_bots)
    })

# ---------- WEBSITE ROUTES ----------
@app.route('/api/websites')
@login_required
def api_list_websites():
    username = session['username']
    with get_db() as conn:
        websites = conn.execute('SELECT * FROM websites WHERE owner_username = ? AND type = ? ORDER BY created_at DESC', 
                               (username, 'website')).fetchall()
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
    
    with get_db() as conn:
        bot_count = conn.execute('SELECT COUNT(*) FROM bots WHERE user = ?', (username,)).fetchone()[0]
        website_count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_username = ?', (username,)).fetchone()[0]
        cli_count = conn.execute('SELECT COUNT(*) FROM cli_tools WHERE owner_username = ?', (username,)).fetchone()[0]
    total_current = bot_count + website_count + cli_count

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
        with zipfile.ZipFile(os.path.join(folder, 'upload.zip'), 'r') as zf:
            zf.extractall(folder)
        os.remove(os.path.join(folder, 'upload.zip'))
    
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
    return send_file(zip_buffer, as_attachment=True, download_name=f"{w['website_slug']}.zip")

# ---------- CLI TOOL ROUTES ----------
@app.route('/api/cli_tools')
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

        # Find startup file
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

        # Save to database
        slug = f"cli_{tool_id}"
        with get_db() as conn:
            cur = conn.execute('''INSERT INTO cli_tools 
                (owner_username, tool_name, tool_slug, tool_folder, startup_file, interpreter, status)
                VALUES (?, ?, ?, ?, ?, ?, ?)''',
                (username, startup_file, slug, tool_folder, startup_file, interpreter, 'stopped'))
            tool_db_id = cur.lastrowid
            conn.commit()

        return jsonify({'success': True, 'tool_id': tool_db_id, 'tool_slug': slug})

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

    ok, msg = start_cli_tool(tool_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'error': msg}), 500

@app.route('/api/cli_tool/<int:tool_id>/stop', methods=['POST'])
@login_required
def cli_tool_stop(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?', 
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    ok, msg = stop_cli_tool(tool_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'error': msg}), 500

@app.route('/api/cli_tool/<int:tool_id>/delete', methods=['POST'])
@login_required
def cli_tool_delete(tool_id):
    with get_db() as conn:
        tool = conn.execute('SELECT * FROM cli_tools WHERE id = ? AND owner_username = ?', 
                           (tool_id, session['username'])).fetchone()
    if not tool:
        return jsonify({'error': 'Tool not found'}), 404

    if tool['status'] == 'running':
        stop_cli_tool(tool_id)

    tool_folder = tool['tool_folder']
    shutil.rmtree(tool_folder, ignore_errors=True)

    with get_db() as conn:
        conn.execute('DELETE FROM cli_tools WHERE id = ?', (tool_id,))
        conn.execute('DELETE FROM logs WHERE cli_tool_id = ?', (tool_id,))
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
    
    ok, msg = send_cli_input(tool_id, input_data)
    if ok:
        return jsonify({'success': True})
    return jsonify({'error': msg}), 400

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
    
    # Send file path to CLI process if running
    if tool_id in cli_processes:
        proc = cli_processes[tool_id]
        try:
            proc.stdin.write((file_path + '\n').encode())
            proc.stdin.flush()
        except:
            pass
    
    return jsonify({
        'success': True,
        'filename': filename,
        'filepath': file_path
    })

# ---------- WEBSITE PROXY ----------
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

# ---------- SYSTEM STATS ----------
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

    offset_hours = float(get_config('total_hours_offset', '0'))
    internal_hours = total_internal_seconds / 3600.0
    main_hours = main_uptime_seconds / 3600.0
    total_hours = offset_hours + main_hours + internal_hours

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
        'offset_hours': round(offset_hours, 2),
        'total_hours': round(total_hours, 2),
        'websites_count': len(rows),
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

# ---------- TERMINAL ----------
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

@app.route('/api/terminal/read', methods(['GET'])
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

# ---------- TEMPLATES ----------
ERROR_TEMPLATE = """<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;display:flex;justify-content:center;align-items:center;height:100vh}.card{background:rgba(255,255,255,0.05);padding:40px;border-radius:20px;text-align:center}h1{color:#ff4757}a{color:#00e5ff;text-decoration:none}</style>
</head><body><div class="card"><h1>{{ message }}</h1><p>Slug: {{ slug }}</p><a href="/dashboard">← Dashboard</a></div></body></html>"""

# ---------- HTML TEMPLATE ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>{{ website_name }} · Admin Panel</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <style>
        *{margin:0;padding:0;box-sizing:border-box;font-family:'Arial',sans-serif}
        body{background:#05070d;color:#fff;min-height:100vh;display:flex;justify-content:center;align-items:center;padding:20px}
        .view{display:none;width:100%;max-width:420px;margin:0 auto}
        .view.active{display:block}
        .login-card{position:relative;width:100%;padding:30px 20px;background:#0c1018;border-radius:25px;overflow:hidden;box-shadow:0 0 20px rgba(0,0,0,.5)}
        .login-card::before{content:"";position:absolute;inset:-3px;background:conic-gradient(#00e5ff,transparent,transparent,transparent,#00e5ff);animation:spin 4s linear infinite}
        .login-card::after{content:"";position:absolute;inset:3px;background:#0c1018;border-radius:22px}
        .login-content{position:relative;z-index:2}
        .login-icon{width:110px;height:110px;margin:auto;border:3px solid #00e5ff;border-radius:50%;display:flex;justify-content:center;align-items:center;font-size:45px;color:#00e5ff;box-shadow:0 0 20px #00e5ff;overflow:hidden;background:#0c1018;cursor:pointer;transition:transform .1s;user-select:none}
        .login-icon:active{transform:scale(.95)}
        .login-icon img{width:100%;height:100%;object-fit:cover;border-radius:50%}
        .login-title{margin:25px 0;text-align:center;color:#cfffff;letter-spacing:4px;font-size:1.3rem}
        .login-card select,.login-card input{width:100%;margin:12px 0;padding:16px;background:#161b25;border:1px solid #2b3240;border-radius:15px;color:#fff;font-size:16px;outline:none}
        .login-card select option{background:#161b25}
        .login-btn{width:100%;margin-top:20px;padding:16px;border:none;border-radius:15px;font-size:18px;font-weight:700;color:#fff;cursor:pointer;background:linear-gradient(90deg,#7a00ff,#00d9ff);transition:opacity .2s}
        .login-btn:hover{opacity:.9}
        .login-error{color:#ff4d4d;text-align:center;font-size:14px;margin-top:10px;min-height:22px}
        @keyframes spin{100%{transform:rotate(360deg)}}
        @keyframes shake{0%,100%{transform:translateX(0)}10%,30%,50%,70%,90%{transform:translateX(-10px)}20%,40%,60%,80%{transform:translateX(10px)}}
        .shake{animation:shake .5s ease-in-out}
        .user-container{max-width:400px;width:100%;margin:0 auto}
        .user-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px}
        .user-title{letter-spacing:3px;font-weight:800;font-size:1.2rem}
        .hamburger{font-size:28px;cursor:pointer;color:#fff;padding:4px 8px;border-radius:8px;transition:background .2s;user-select:none}
        .hamburger:hover{background:rgba(255,255,255,.08)}
        .power-btn{color:#ff4d4d;font-size:20px;cursor:pointer}
        .user-header-left{display:flex;align-items:center;gap:12px}
        .tabs{display:flex;gap:10px;margin:15px 0 10px;border-bottom:1px solid rgba(255,255,255,.1);padding-bottom:10px}
        .tab-btn{background:transparent;border:none;color:#888;font-size:1rem;font-weight:700;padding:8px 16px;cursor:pointer;transition:.3s;border-radius:10px}
        .tab-btn:hover{color:#fff;background:rgba(255,255,255,.05)}
        .tab-btn.active{color:#00e5ff;background:rgba(0,229,255,.1)}
        .tab-content{display:none}
        .tab-content.active{display:block}
        .upload-card{border:1px dashed #00e5ff;border-radius:15px;padding:20px;text-align:center;background:rgba(0,229,255,.05);position:relative;cursor:pointer;margin-bottom:15px}
        .upload-card .settings-icon{position:absolute;top:15px;right:15px;border:1px solid #00e5ff;padding:5px 8px;border-radius:6px;font-size:14px;color:#00e5ff;cursor:pointer}
        .cloud-icon{font-size:40px;margin-bottom:10px;color:#00e5ff}
        .upload-card>div:nth-child(3){color:#aaa;font-size:14px}
        .deploy-btn{background:#fff;color:#000;padding:15px;border-radius:10px;font-weight:900;margin-top:15px;text-transform:uppercase;cursor:pointer;border:none;width:100%;font-size:14px}
        #fileCountDisplay{font-size:12px;color:#888;margin-top:8px}
        .bot-card,.website-card,.cli-card{background:#111;border:1px solid #333;border-radius:15px;padding:15px;transition:border-color .2s;cursor:pointer;margin-bottom:15px}
        .bot-card:hover,.website-card:hover,.cli-card:hover{border-color:#555}
        .bot-card.selected,.website-card.selected,.cli-card.selected{border-color:#00e5ff}
        .bot-header,.website-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:10px}
        .bot-name,.website-name{font-weight:700;font-size:16px}
        .bot-status{font-size:12px;padding:2px 12px;border-radius:12px;font-weight:700}
        .bot-status.running{background:#00ff6a33;color:#00ff6a;border:1px solid #00ff6a}
        .bot-status.stopped{background:#555;color:#aaa;border:1px solid #666}
        .bot-uptime{font-size:12px;color:#888;margin-bottom:10px;font-family:monospace}
        .bot-owner{font-size:11px;color:#888;margin-bottom:8px}
        .bot-controls,.website-actions,.cli-tool-controls{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin-top:10px}
        .bot-controls button,.website-actions button,.cli-tool-controls button{padding:6px 14px;border:none;border-radius:12px;font-size:.8rem;font-weight:600;cursor:pointer;transition:.2s}
        .bot-controls button:hover,.website-actions button:hover,.cli-tool-controls button:hover{transform:scale(1.05)}
        .btn-start,.btn-start-w{background:rgba(0,229,255,.2);color:#00e5ff}
        .btn-start:hover,.btn-start-w:hover{background:#00e5ff;color:#000}
        .btn-stop,.btn-stop-w{background:rgba(255,71,87,.2);color:#ff4757}
        .btn-stop:hover,.btn-stop-w:hover{background:#ff4757;color:#fff}
        .btn-restart,.btn-restart-w{background:rgba(255,170,0,.2);color:#ffaa00}
        .btn-restart:hover,.btn-restart-w:hover{background:#ffaa00;color:#000}
        .btn-delete,.btn-delete-w{background:rgba(255,0,0,.15);color:#ff4444}
        .btn-delete:hover,.btn-delete-w:hover{background:#ff0000;color:#fff}
        .btn-edit{background:rgba(77,136,255,.2);color:#4d88ff}
        .btn-edit:hover{background:#4d88ff;color:#fff}
        .btn-download{background:rgba(46,204,113,.2);color:#2ecc71}
        .btn-download:hover{background:#2ecc71;color:#000}
        .btn-openbot{background:#1da1f2;color:#fff;grid-column:span 2;padding:10px;border-radius:8px;border:none;font-weight:700;cursor:pointer;width:100%;transition:background .2s}
        .btn-openbot:hover{background:#1a8cd8}
        .btn-visit-w{background:rgba(46,204,113,.2);color:#2ecc71}
        .btn-visit-w:hover{background:#2ecc71;color:#000}
        .btn-files-w{background:rgba(255,255,255,.1);color:#aaa}
        .btn-files-w:hover{background:rgba(255,255,255,.2);color:#fff}
        .btn-miniweb{background:rgba(255,165,0,.15);color:#ffaa00;border:1px solid rgba(255,165,0,.2)}
        .btn-miniweb:hover{background:#ffaa00;color:#000}
        .btn-full{grid-column:span 2;background:#222;color:#fff;margin-top:5px}
        .btn-full.danger{background:#400}
        .btn-buildlogs{background:#ffa50033;color:#ffa500;border:1px solid #ffa500;grid-column:span 2}
        .btn-buildlogs:hover{background:#ffa500;color:#000}
        .name-edit{display:flex;gap:8px;margin-top:12px}
        .name-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,.05);border:1px solid rgba(255,255,255,.1);border-radius:12px;color:#fff;outline:none;font-size:.85rem}
        .name-edit input:focus{border-color:#00e5ff}
        .name-edit button{padding:8px 16px;background:#00e5ff;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer}
        .console-wrapper{display:flex;align-items:stretch;gap:8px;margin-top:15px}
        .console{background:#000;color:#00ff6a;padding:10px;font-family:monospace;font-size:10px;border-radius:8px;height:100px;overflow-y:auto;border:1px solid #333;line-height:1.6;white-space:pre-wrap;flex:1}
        .cli-terminal-container{background:#0d1117;border:1px solid #30363d;border-radius:15px;margin-top:20px;overflow:hidden}
        .cli-terminal-header{display:flex;justify-content:space-between;align-items:center;padding:12px 20px;background:rgba(255,255,255,.03);border-bottom:1px solid #30363d;font-size:14px}
        .cli-terminal-header span:first-child{color:#58a6ff}
        .cli-terminal{background:#010409;color:#50fa7b;padding:15px 20px;font-family:'Courier New',monospace;font-size:14px;min-height:300px;max-height:400px;overflow-y:auto;white-space:pre-wrap;line-height:1.6}
        .cli-terminal-input-row{display:flex;gap:10px;padding:12px 20px;background:rgba(255,255,255,.02);border-top:1px solid #30363d;flex-wrap:wrap}
        .cli-terminal-input-row input{flex:1;background:#0d1117;border:1px solid #30363d;border-radius:8px;color:#fff;padding:12px 16px;font-family:'Courier New',monospace;font-size:14px;outline:none;min-width:100px}
        .cli-terminal-input-row input:focus{border-color:#00e5ff}
        .cli-terminal-input-row input:disabled{opacity:.5}
        .cli-terminal-input-row button{padding:12px 20px;border:none;border-radius:8px;font-weight:600;cursor:pointer;transition:.2s}
        .cli-terminal-input-row button:disabled{opacity:.5;cursor:not-allowed}
        #cliSendBtn{background:#238636;color:#fff}
        #cliSendBtn:hover:not(:disabled){background:#2ea043}
        #cliClearBtn{background:#555;color:#fff}
        #cliClearBtn:hover{background:#666}
        .btn-upload-file{background:#7a00ff;color:#fff}
        .btn-upload-file:hover:not(:disabled){background:#9a2aff}
        .user-footer{text-align:center;margin-top:30px}
        .f-title{font-size:22px;font-weight:900;letter-spacing:5px}
        .f-sub{font-size:11px;opacity:.6;margin-bottom:15px}
        .social-box{display:flex;justify-content:center;gap:20px}
        .social-box a{color:#fff;font-size:20px;width:40px;height:40px;border:1px solid #333;border-radius:50%;display:flex;align-items:center;justify-content:center;text-decoration:none;transition:border-color .2s}
        .social-box a:hover{border-color:#00e5ff}
        .admin-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.7);z-index:999;justify-content:flex-end;animation:fadeIn .25s ease}
        .admin-overlay.open{display:flex}
        .admin-drawer{width:100%;max-width:480px;height:100%;background:#0c1018;padding:24px 20px;overflow-y:auto;box-shadow:-10px 0 30px rgba(0,0,0,.8);animation:slideIn .3s ease;display:flex;flex-direction:column}
        .admin-drawer-header{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #222}
        .admin-drawer-header h2{color:#00e5ff;font-size:1.2rem;letter-spacing:2px}
        .admin-close-btn{background:0 0;border:none;color:#ff4d4d;font-size:28px;cursor:pointer;padding:0 6px}
        .admin-tabs{display:flex;gap:10px;margin-bottom:20px;flex-wrap:wrap}
        .admin-tabs button{flex:1;padding:12px;border:1px solid #333;border-radius:10px;background:transparent;color:#aaa;font-weight:700;font-size:13px;cursor:pointer;transition:all .2s;min-width:80px}
        .admin-tabs button.active{background:#00e5ff22;border-color:#00e5ff;color:#00e5ff}
        .admin-panel-content{flex:1}
        .admin-tab-content{display:none}
        .admin-tab-content.active{display:block}
        .list-item{background:#111;border:1px solid #2a2a2a;border-radius:12px;padding:14px 16px;margin-bottom:14px;display:flex;flex-direction:column;gap:10px}
        .list-item .row{display:flex;flex-wrap:wrap;align-items:center;gap:8px}
        .list-item .row .info{flex:1;min-width:120px}
        .list-item .info .uname{font-weight:700;font-size:15px;color:#fff}
        .list-item .info .upass{font-size:13px;color:#888;font-family:monospace}
        .badge-role{font-size:10px;padding:2px 10px;border-radius:20px;font-weight:700;text-transform:uppercase;white-space:nowrap}
        .badge-role.admin{background:#00e5ff33;color:#00e5ff;border:1px solid #00e5ff55}
        .badge-role.user{background:#444;color:#ccc;border:1px solid #555}
        .badge-role.banned{background:#ff333333;color:#ff4d4d;border:1px solid #ff4d4d55}
        .limit-group{display:flex;align-items:center;gap:6px}
        .limit-group label{color:#aaa;font-size:13px;font-weight:700}
        .list-item .limit-input{width:70px;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 6px;border-radius:5px;font-size:13px;outline:none;text-align:center}
        .list-item .limit-input:focus{border-color:#00e5ff}
        .btn-action{border:none;cursor:pointer;font-weight:700;border-radius:5px;padding:8px 14px;font-size:12px;white-space:nowrap}
        .btn-set{background:#00e5ff33;color:#00e5ff;border:1px solid #00e5ff55}
        .btn-set:hover{background:#00e5ff55}
        .btn-ban{background:#ff333333;color:#ff4d4d;border:1px solid #ff4d4d55}
        .btn-ban:hover{background:#ff4d4d33}
        .btn-reset{background:#333;color:#fff;border:1px solid #555}
        .btn-reset:hover{background:#444}
        .btn-del{background:#ff333333;color:#ff4d4d;border:1px solid #ff4d4d55;width:100%;padding:10px;text-align:center}
        .btn-del:hover{background:#ff4d4d33}
        .btn-create{background:#00e5ff;color:#000;border:none;padding:10px 18px;border-radius:8px;font-weight:700;cursor:pointer;font-size:13px}
        .btn-create:hover{opacity:.9}
        .btn-remove{background:transparent;color:#ff4d4d;border:1px solid #ff4d4d55;padding:6px 14px;border-radius:6px;cursor:pointer;font-weight:700;font-size:12px}
        .btn-remove:hover{background:#ff4d4d22}
        #createUserForm{display:none;background:#1a1a1a;padding:16px;border-radius:12px;margin-bottom:20px;border:1px solid #2a2a2a}
        #createUserForm input,#createUserForm select{background:#0c1018;border:1px solid #333;color:#fff;padding:12px;border-radius:8px;width:100%;margin-bottom:10px;outline:none;font-size:14px}
        #createUserForm input:focus,#createUserForm select:focus{border-color:#00e5ff}
        .simple-list-item{background:#111;border:1px solid #2a2a2a;border-radius:10px;padding:12px 16px;margin-bottom:10px;display:flex;justify-content:space-between;align-items:center}
        .simple-list-item .info{display:flex;flex-direction:column}
        .simple-list-item .info .uname{font-weight:700;font-size:14px;color:#fff}
        .simple-list-item .info .upass{font-size:12px;color:#888;font-family:monospace}
        .simple-list-item .actions button{background:transparent;color:#ff4d4d;border:1px solid #ff4d4d55;padding:6px 12px;border-radius:6px;cursor:pointer;font-weight:700;font-size:12px}
        .simple-list-item .actions button:hover{background:#ff4d4d22}
        .section-title{color:#00e5ff;font-size:14px;font-weight:700;margin:18px 0 10px;border-bottom:1px solid #222;padding-bottom:6px}
        .empty-msg{text-align:center;color:#555;padding:20px 0;font-size:14px}
        .terminal-box{background:#010409;color:#50fa7b;height:350px;overflow-y:scroll;padding:12px;border:1px solid #30363d;font-family:'Courier New',monospace;font-size:14px;white-space:pre-wrap;border-radius:6px;margin-bottom:10px;line-height:1.6}
        .terminal-controls{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
        .terminal-controls input{flex:1;background:#0d1117;border:1px solid #30363d;color:#fff;padding:14px;border-radius:6px;font-size:16px;outline:none;min-width:150px}
        .terminal-controls input:focus{border-color:#00e5ff}
        .terminal-controls button{padding:12px 20px;border:none;border-radius:6px;font-weight:700;cursor:pointer;font-size:14px}
        .btn-term-run{background:#238636;color:#fff}
        .btn-term-run:hover{background:#2ea043}
        .btn-term-stop{background:#da3633;color:#fff}
        .btn-term-stop:hover{background:#f85149}
        .btn-term-clear{background:#555;color:#fff}
        .btn-term-clear:hover{background:#666}
        .custom-modal-overlay{display:none;position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:10001;justify-content:center;align-items:center;animation:fadeIn .2s ease}
        .custom-modal-overlay.open{display:flex}
        .custom-modal{background:#0c1018;border:1px solid #2a2a2a;border-radius:20px;padding:30px 28px;max-width:500px;width:90%;max-height:90vh;overflow-y:auto;box-shadow:0 10px 40px rgba(0,0,0,.8);text-align:left}
        .custom-modal .modal-icon{font-size:40px;margin-bottom:12px;text-align:center;color:#00e5ff}
        .custom-modal .modal-body{color:#eee;font-size:15px;line-height:1.6;margin-bottom:24px}
        .custom-modal .modal-body textarea{width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:.7rem;resize:vertical;tab-size:4;min-height:200px}
        .custom-modal .modal-actions{display:flex;gap:12px;justify-content:flex-end;flex-wrap:wrap}
        .custom-modal .modal-actions button{padding:12px 28px;border:none;border-radius:10px;font-weight:700;font-size:15px;cursor:pointer;min-width:100px;transition:background .2s}
        .custom-modal .modal-actions .btn-confirm{background:#00e5ff;color:#000}
        .custom-modal .modal-actions .btn-confirm:hover{background:#00d4f0}
        .custom-modal .modal-actions .btn-cancel{background:#333;color:#fff;border:1px solid #555}
        .custom-modal .modal-actions .btn-cancel:hover{background:#444}
        .custom-modal .modal-actions .btn-ok{background:#00e5ff;color:#000;width:100%}
        .custom-modal .modal-actions .btn-ok:hover{background:#00d4f0}
        .settings-form label{display:block;color:#aaa;font-size:13px;margin-top:15px;margin-bottom:4px}
        .settings-form input[type=text],.settings-form input[type=file]{width:100%;background:#161b25;border:1px solid #2b3240;color:#fff;padding:12px;border-radius:8px;outline:none;font-size:14px}
        .settings-form input:focus{border-color:#00e5ff}
        .settings-form .logo-preview{margin-top:10px;max-width:100px;max-height:100px;border-radius:50%;border:2px solid #00e5ff}
        .settings-form .btn-remove-logo{background:#ff3333;color:#fff;border:none;padding:8px 16px;border-radius:5px;cursor:pointer;margin-top:8px}
        .settings-form .btn-remove-logo:hover{background:#cc0000}
        .file-manager{max-height:400px;overflow-y:auto;background:#0d1117;border-radius:8px;padding:10px}
        .file-item{display:flex;justify-content:space-between;align-items:center;padding:8px 12px;border-bottom:1px solid #1e1e1e;cursor:pointer;transition:background .2s;user-select:none}
        .file-item:hover{background:#1a1f2b}
        .file-item.selected{background:#2a3a5a;border-left:3px solid #00e5ff}
        .file-item .name{display:flex;align-items:center;gap:8px;color:#ccc}
        .file-item .name i{width:20px;color:#00e5ff}
        .file-item .name .dir-icon{color:#f0c674}
        .file-item .size{font-size:12px;color:#888}
        .file-breadcrumb{display:flex;flex-wrap:wrap;gap:5px;margin-bottom:10px;padding:8px;background:#1a1f2b;border-radius:6px}
        .file-breadcrumb span{color:#00e5ff;cursor:pointer;padding:2px 6px;border-radius:4px}
        .file-breadcrumb span:hover{background:#2a3a5a}
        .file-breadcrumb .sep{color:#555;cursor:default}
        .file-context-menu{display:none;position:fixed;background:#1a1f2b;border:1px solid #333;border-radius:8px;padding:6px 0;z-index:10002;min-width:150px}
        .file-context-menu .menu-item{padding:8px 16px;color:#ccc;cursor:pointer;display:flex;align-items:center;gap:10px}
        .file-context-menu .menu-item:hover{background:#2a3a5a}
        .file-context-menu .menu-item.danger{color:#ff4d4d}
        .stats-grid{display:grid;grid-template-columns:1fr 1fr;gap:15px;margin-bottom:15px}
        .stat-card{background:rgba(255,255,255,.03);border:1px solid rgba(255,255,255,.06);border-radius:15px;padding:15px;text-align:center}
        .stat-card .label{color:#888;font-size:.8rem;text-transform:uppercase;letter-spacing:1px}
        .stat-card .value{font-size:1.6rem;font-weight:700;color:#00e5ff;margin:5px 0}
        .stat-card .sub{color:#666;font-size:.8rem}
        .stat-card .progress-bar{width:100%;height:6px;background:#1a1a1a;border-radius:4px;margin-top:8px;overflow:hidden}
        .stat-card .progress-bar .fill{height:100%;background:linear-gradient(90deg,#7a00ff,#00e5ff);border-radius:4px;transition:width .5s}
        .offset-section{border-top:1px solid rgba(255,255,255,.1);padding-top:15px;margin-top:15px}
        .offset-section label{color:#aaa}
        .offset-section .offset-input{display:flex;gap:10px;margin-top:5px}
        .offset-section .offset-input input{flex:1;background:#1a1a1a;border:1px solid #333;border-radius:8px;padding:10px;color:#fff}
        .offset-section .offset-input button{background:#00e5ff;border:none;border-radius:8px;padding:10px 20px;color:#000;font-weight:700;cursor:pointer}
        @keyframes fadeIn{0%{opacity:0}100%{opacity:1}}
        @keyframes slideIn{0%{transform:translateX(60px);opacity:0}100%{transform:translateX(0);opacity:1}}
        ::-webkit-scrollbar{width:4px}
        ::-webkit-scrollbar-track{background:#0c1018}
        ::-webkit-scrollbar-thumb{background:#333;border-radius:4px}
        @media(max-width:480px){.admin-drawer{max-width:100%;padding:18px 14px}.list-item .row{flex-direction:column;align-items:stretch}.list-item .limit-input{width:100%}.create-row{flex-direction:column}.limit-group{flex-wrap:wrap}.admin-tabs button{font-size:11px;padding:8px}.terminal-controls{flex-wrap:wrap}.terminal-controls input{width:100%}.bot-controls,.website-actions,.cli-tool-controls{grid-template-columns:1fr 1fr}.file-item{flex-wrap:wrap}.stats-grid{grid-template-columns:1fr}}
    </style>
</head>
<body>
    <div id="loginView" class="view {% if not logged_in %}active{% endif %}">
        <div class="login-card" id="loginCard">
            <div class="login-content">
                <div class="login-icon" id="loginIcon">
                    {% if logo_url %}<img src="{{ logo_url }}" alt="Logo" />{% else %}<i class="fa-solid fa-user"></i>{% endif %}
                </div>
                <h1 class="login-title">{{ website_name }}</h1>
                <select id="loginRoleSelect"><option value="user" selected>USER ACCESS</option><option value="admin">Admin</option></select>
                <input type="text" id="loginUsername" placeholder="Enter Username" />
                <input type="password" id="loginPassword" placeholder="Password" />
                <button class="login-btn" id="loginBtn">ACCESS SYSTEM</button>
                <div class="login-error" id="loginError"></div>
            </div>
        </div>
    </div>
    <div id="userView" class="view {% if logged_in %}active{% endif %}">
        <div class="user-container">
            <div class="user-header">
                <div class="user-header-left">
                    <span class="hamburger" id="hamburgerBtn">☰</span>
                    <span class="user-title">{{ website_name }}</span>
                </div>
                <div class="power-btn" id="logoutBtn"><i class="fa-solid fa-power-off"></i></div>
            </div>
            <div class="tabs">
                <button class="tab-btn active" data-tab="websites">🌐 Websites</button>
                <button class="tab-btn" data-tab="bots">🤖 Bots</button>
                <button class="tab-btn" data-tab="cli">💻 CLI Tools</button>
            </div>
            <div id="tab-websites" class="tab-content active">
                <div class="upload-card" id="uploadCardWebsite">
                    <div class="cloud-icon"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                    <div id="uploadLabelWebsite">UPLOAD WEBSITE (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnWebsite">DEPLOY WEBSITE</div>
                    <input type="file" id="fileInputWebsite" style="display:none;" multiple accept=".zip,.py,.js,.html,.css,.json,.txt,.php,.go,.rb,.sh,.pl,.jar,.war,.xml,.gradle" />
                    <div id="fileCountDisplayWebsite"></div>
                </div>
                <div id="websiteGrid" class="website-grid"></div>
                <div class="console-wrapper"><div class="console" id="websiteConsole">Select a website to see logs.</div></div>
            </div>
            <div id="tab-bots" class="tab-content">
                <div class="upload-card" id="uploadCardBot">
                    <div class="cloud-icon"><i class="fa-solid fa-robot"></i></div>
                    <div id="uploadLabelBot">UPLOAD BOT (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnBot">DEPLOY BOT</div>
                    <input type="file" id="fileInputBot" style="display:none;" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl,.json,.txt" />
                    <div id="fileCountDisplayBot"></div>
                </div>
                <div id="botListContainer"></div>
                <div class="console-wrapper"><div class="console" id="botConsole">Select a bot to see logs.</div></div>
            </div>
            <div id="tab-cli" class="tab-content">
                <div class="upload-card" id="uploadCardCli">
                    <div class="cloud-icon"><i class="fa-solid fa-terminal"></i></div>
                    <div id="uploadLabelCli">UPLOAD CLI TOOL (ZIP or files)</div>
                    <div class="deploy-btn" id="deployBtnCli">DEPLOY CLI TOOL</div>
                    <input type="file" id="fileInputCli" style="display:none;" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl,.json,.txt,.csv" />
                    <div id="fileCountDisplayCli"></div>
                </div>
                <div id="cliToolListContainer"></div>
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
    <div class="admin-overlay" id="adminOverlay">
        <div class="admin-drawer">
            <div class="admin-drawer-header">
                <h2><i class="fa-solid fa-shield-halved"></i> ADMIN PANEL</h2>
                <button class="admin-close-btn" id="adminCloseBtn">✕</button>
            </div>
            <div class="admin-tabs">
                <button class="active" data-tab="tabAdminMenu">🛠️ ADMIN MENU</button>
                <button data-tab="tabUserMenu">👥 USER MENU</button>
                <button data-tab="tabTerminal">💻 TERMINAL</button>
                <button data-tab="tabFileManager">📁 FILES</button>
                {% if is_admin %}<button data-tab="tabStats">📊 STATS</button>{% endif %}
            </div>
            <div class="admin-panel-content">
                <div id="tabAdminMenu" class="admin-tab-content active">
                    <button class="btn-create" id="toggleCreateUserBtn" style="width:100%;margin-bottom:12px;"><i class="fa-solid fa-plus"></i> NEW USER</button>
                    <button class="btn-create" id="editProfileBtn" style="width:100%;margin-bottom:12px;background:#4d88ff;"><i class="fa-solid fa-user-edit"></i> EDIT PROFILE</button>
                    <div id="createUserForm">
                        <input type="text" id="newUsername" placeholder="Username" />
                        <input type="password" id="newPassword" placeholder="Password" />
                        <input type="text" id="newExpiry" placeholder="Expiry (Days, e.g. 1, 5, 30)" />
                        <select id="newRole"><option value="user">User</option><option value="admin">Admin</option></select>
                        <button class="btn-create" id="createUserBtn" style="width:100%;">CREATE</button>
                    </div>
                    <div id="fullUserListContainer"></div>
                </div>
                <div id="tabUserMenu" class="admin-tab-content">
                    <div class="section-title">👑 Admin List</div>
                    <div id="simpleAdminListContainer"></div>
                    <div class="section-title" style="margin-top:24px;">👤 User List</div>
                    <div id="simpleUserListContainer"></div>
                </div>
                <div id="tabTerminal" class="admin-tab-content">
                    <div class="terminal-box" id="terminalOutput"><span class="prompt">$ </span>Connected...</div>
                    <div class="terminal-controls">
                        <input type="text" id="terminalCommand" placeholder="Type command or input..." />
                        <button class="btn-term-run" id="termRunBtn"><i class="fa-solid fa-play"></i> Run</button>
                        <button class="btn-term-stop" id="termStopBtn"><i class="fa-solid fa-stop"></i> Stop</button>
                        <button class="btn-term-clear" id="termClearBtn"><i class="fa-solid fa-eraser"></i> Clear</button>
                    </div>
                </div>
                <div id="tabFileManager" class="admin-tab-content">
                    <div class="file-breadcrumb" id="fileBreadcrumb"></div>
                    <div class="file-manager" id="fileManagerList"></div>
                    <div style="margin-top:10px;font-size:12px;color:#555;">Long press on item (or right-click) for actions</div>
                </div>
                {% if is_admin %}
                <div id="tabStats" class="admin-tab-content">
                    <div class="stats-grid" id="statsGrid">
                        <div class="stat-card"><div class="label">Total Hours Used</div><div class="value" id="statTotalHours">--</div><div class="sub">Offset + Running</div></div>
                        <div class="stat-card"><div class="label">Main Container Uptime</div><div class="value" id="statMainHours">--</div><div class="sub">Flask App</div></div>
                        <div class="stat-card"><div class="label">Internal Sites/Bots</div><div class="value" id="statInternalHours">--</div><div class="sub">Subprocesses</div></div>
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
                    </div>
                </div>
                {% endif %}
            </div>
        </div>
    </div>
    <div class="custom-modal-overlay" id="customModalOverlay">
        <div class="custom-modal">
            <div class="modal-icon" id="modalIcon">⚠️</div>
            <div class="modal-body" id="modalBody"></div>
            <div class="modal-actions" id="modalActions"></div>
        </div>
    </div>
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
    <div class="file-context-menu" id="fileContextMenu">
        <div class="menu-item" id="ctxDelete"><i class="fa-solid fa-trash"></i> Delete</div>
        <div class="menu-item" id="ctxRename"><i class="fa-solid fa-pen"></i> Rename</div>
        <div class="menu-item" id="ctxDownload"><i class="fa-solid fa-download"></i> Download</div>
    </div>
    <script>
    (function(){
        'use strict';
        const originalFetch=window.fetch;
        window.fetch=function(url,options){
            return originalFetch(url,options).then(response=>{
                if(response.status===401){window.location.href='/';return Promise.reject('Unauthorized');}
                return response;
            });
        };
        const modalOverlay=document.getElementById('customModalOverlay');
        const modalIcon=document.getElementById('modalIcon');
        const modalBody=document.getElementById('modalBody');
        const modalActions=document.getElementById('modalActions');
        function showCustomModal(icon,bodyHTML,buttons){
            return new Promise((resolve)=>{
                modalIcon.textContent=icon||'⚠️';
                modalBody.innerHTML=bodyHTML||'';
                modalActions.innerHTML='';
                buttons.forEach((btn)=>{
                    const buttonEl=document.createElement('button');
                    buttonEl.textContent=btn.label;
                    buttonEl.className=btn.className||'btn-confirm';
                    buttonEl.addEventListener('click',()=>{closeModal();resolve(btn.value);});
                    modalActions.appendChild(buttonEl);
                });
                modalOverlay.classList.add('open');
            });
        }
        window.customAlert=function(message,icon='ℹ️'){
            return showCustomModal(icon,`<div style="font-size:16px;color:#eee;">${message}</div>`, [{label:'OK',value:true,className:'btn-ok'}]);
        };
        window.customConfirm=function(message,icon='⚠️'){
            return showCustomModal(icon,`<div style="font-size:16px;color:#eee;">${message}</div>`, [{label:'Cancel',value:false,className:'btn-cancel'},{label:'OK',value:true,className:'btn-confirm'}]);
        };
        function closeModal(){modalOverlay.classList.remove('open');}
        let loginIconClickCount=0,loginIconTimer=null;
        document.getElementById('loginIcon').addEventListener('click',function(e){
            loginIconClickCount++;
            clearTimeout(loginIconTimer);
            loginIconTimer=setTimeout(()=>{loginIconClickCount=0;},2000);
            if(loginIconClickCount>=5){loginIconClickCount=0;showSecretKeyModal();}
        });
        async function showSecretKeyModal(){
            const bodyHTML=`<div style="text-align:center;"><p style="margin-bottom:12px;">Enter Secret Key to login as Admin:</p><input type="password" id="secretKeyInput" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" /></div>`;
            const result=await showCustomModal('🔑',bodyHTML,[{label:'Cancel',value:false,className:'btn-cancel'},{label:'Login',value:true,className:'btn-confirm'}]);
            if(result){
                const secret=document.getElementById('secretKeyInput')?.value;
                if(!secret){await customAlert('Enter secret key.','⚠️');return;}
                try{
                    const res=await fetch('/api/secret_login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({secret})});
                    const data=await res.json();
                    if(data.success){location.reload();}else{await customAlert(data.error||'Invalid secret','❌');}
                }catch(e){await customAlert('Error: '+e.message,'❌');}
            }
        }
        const editProfileBtn=document.getElementById('editProfileBtn');
        if(editProfileBtn){
            editProfileBtn.addEventListener('click',async function(){
                const currentUsername='{{ username }}';
                const bodyHTML=`<div style="text-align:center;"><div style="font-size:20px;margin-bottom:20px;">✎ Edit Profile</div><div style="margin-bottom:12px;"><label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Username</label><input type="text" id="editUsername" value="${currentUsername}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" /></div><div><label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Password (leave blank to keep current)</label><input type="password" id="editPassword" placeholder="New password..." style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" /></div></div>`;
                const result=await showCustomModal('✎',bodyHTML,[{label:'Cancel',value:false,className:'btn-cancel'},{label:'Save',value:true,className:'btn-confirm'}]);
                if(result){
                    const newUsername=document.getElementById('editUsername').value.trim();
                    const newPassword=document.getElementById('editPassword').value.trim();
                    try{
                        const res=await fetch('/api/profile',{method:'PUT',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:newUsername,password:newPassword})});
                        const data=await res.json();
                        if(data.success){if(data.logout){await customAlert('Profile updated! You will be logged out.','✅');window.location.href='/';}else{await customAlert('Profile updated!','✅');location.reload();}}else{await customAlert(data.error||'Update failed','❌');}
                    }catch(e){}
                }
            });
        }
        const settingsModalOverlay=document.getElementById('settingsModalOverlay');
        const settingsCancelBtn=document.getElementById('settingsCancelBtn');
        const settingsSaveBtn=document.getElementById('settingsSaveBtn');
        const settingsWebsiteName=document.getElementById('settingsWebsiteName');
        const settingsTelegram=document.getElementById('settingsTelegram');
        const settingsYoutube=document.getElementById('settingsYoutube');
        const settingsInstagram=document.getElementById('settingsInstagram');
        const settingsTiktok=document.getElementById('settingsTiktok');
        const settingsLogoInput=document.getElementById('settingsLogoInput');
        const settingsLogoPreview=document.getElementById('settingsLogoPreview');
        const settingsRemoveLogoBtn=document.getElementById('settingsRemoveLogoBtn');
        let currentSettings={};
        async function loadSettings(){
            try{
                const res=await fetch('/api/settings');
                const data=await res.json();
                currentSettings=data;
                settingsWebsiteName.value=data.website_name||'YUVICODEX';
                settingsTelegram.value=data.social_links?.telegram||'#';
                settingsYoutube.value=data.social_links?.youtube||'#';
                settingsInstagram.value=data.social_links?.instagram||'#';
                settingsTiktok.value=data.social_links?.tiktok||'#';
                if(data.logo){settingsLogoPreview.innerHTML=`<img src="${data.logo}" class="logo-preview" />`;}else{settingsLogoPreview.innerHTML='';}
            }catch(e){console.error('Failed to load settings',e);}
        }
        async function saveSettings(){
            const payload={website_name:settingsWebsiteName.value.trim()||'YUVICODEX',social_links:{telegram:settingsTelegram.value.trim()||'#',youtube:settingsYoutube.value.trim()||'#',instagram:settingsInstagram.value.trim()||'#',tiktok:settingsTiktok.value.trim()||'#'}};
            try{
                const res=await fetch('/api/settings',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload)});
                const data=await res.json();
                if(data.success){await customAlert('Settings saved successfully!','✅');closeSettingsModal();location.reload();}else{await customAlert('Failed to save settings.','❌');}
            }catch(e){}
        }
        async function uploadLogo(file){
            const formData=new FormData();
            formData.append('logo',file);
            try{
                const res=await fetch('/api/settings/logo',{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){await customAlert('Logo uploaded!','✅');await loadSettings();location.reload();}else{await customAlert(data.error||'Upload failed','❌');}
            }catch(e){}
        }
        async function removeLogo(){
            const confirmed=await customConfirm('Remove logo?','🗑️');
            if(!confirmed)return;
            try{
                const res=await fetch('/api/settings/logo',{method:'DELETE'});
                const data=await res.json();
                if(data.success){await customAlert('Logo removed.','✅');await loadSettings();closeSettingsModal();setTimeout(()=>{location.reload();},500);}else{await customAlert('Failed to remove logo.','❌');}
            }catch(e){}
        }
        function openSettingsModal(){loadSettings();settingsModalOverlay.classList.add('open');}
        function closeSettingsModal(){settingsModalOverlay.classList.remove('open');}
        settingsCancelBtn.addEventListener('click',closeSettingsModal);
        settingsSaveBtn.addEventListener('click',saveSettings);
        settingsRemoveLogoBtn.addEventListener('click',removeLogo);
        settingsLogoInput.addEventListener('change',function(){if(this.files.length>0){uploadLogo(this.files[0]);this.value='';}});
        settingsModalOverlay.addEventListener('click',function(e){if(e.target===this)closeSettingsModal();});
        const loginView=document.getElementById('loginView');
        const userView=document.getElementById('userView');
        const adminOverlay=document.getElementById('adminOverlay');
        const loginUsername=document.getElementById('loginUsername');
        const loginPassword=document.getElementById('loginPassword');
        const loginRoleSelect=document.getElementById('loginRoleSelect');
        const loginBtn=document.getElementById('loginBtn');
        const loginError=document.getElementById('loginError');
        const loginCard=document.getElementById('loginCard');
        const hamburgerBtn=document.getElementById('hamburgerBtn');
        const adminCloseBtn=document.getElementById('adminCloseBtn');
        const logoutBtn=document.getElementById('logoutBtn');
        const botListContainer=document.getElementById('botListContainer');
        const botConsole=document.getElementById('botConsole');
        const websiteGrid=document.getElementById('websiteGrid');
        const websiteConsole=document.getElementById('websiteConsole');
        const cliToolListContainer=document.getElementById('cliToolListContainer');
        const cliTerminal=document.getElementById('cliTerminal');
        const cliTerminalInput=document.getElementById('cliTerminalInput');
        const cliSendBtn=document.getElementById('cliSendBtn');
        const cliClearBtn=document.getElementById('cliClearBtn');
        const cliUploadFileBtn=document.getElementById('cliUploadFileBtn');
        const cliFileUploadInput=document.getElementById('cliFileUploadInput');
        const cliUploadStatus=document.getElementById('cliUploadStatus');
        const cliStatusBadge=document.getElementById('cliStatusBadge');
        const uploadCardWebsite=document.getElementById('uploadCardWebsite');
        const deployBtnWebsite=document.getElementById('deployBtnWebsite');
        const fileInputWebsite=document.getElementById('fileInputWebsite');
        const fileCountDisplayWebsite=document.getElementById('fileCountDisplayWebsite');
        const uploadCardBot=document.getElementById('uploadCardBot');
        const deployBtnBot=document.getElementById('deployBtnBot');
        const fileInputBot=document.getElementById('fileInputBot');
        const fileCountDisplayBot=document.getElementById('fileCountDisplayBot');
        const uploadCardCli=document.getElementById('uploadCardCli');
        const deployBtnCli=document.getElementById('deployBtnCli');
        const fileInputCli=document.getElementById('fileInputCli');
        const fileCountDisplayCli=document.getElementById('fileCountDisplayCli');
        const fullUserListContainer=document.getElementById('fullUserListContainer');
        const simpleAdminListContainer=document.getElementById('simpleAdminListContainer');
        const simpleUserListContainer=document.getElementById('simpleUserListContainer');
        const toggleCreateUserBtn=document.getElementById('toggleCreateUserBtn');
        const createUserForm=document.getElementById('createUserForm');
        const newUsername=document.getElementById('newUsername');
        const newPassword=document.getElementById('newPassword');
        const newExpiry=document.getElementById('newExpiry');
        const newRole=document.getElementById('newRole');
        const createUserBtn=document.getElementById('createUserBtn');
        const terminalOutput=document.getElementById('terminalOutput');
        const terminalCommand=document.getElementById('terminalCommand');
        const termRunBtn=document.getElementById('termRunBtn');
        const termStopBtn=document.getElementById('termStopBtn');
        const termClearBtn=document.getElementById('termClearBtn');
        let currentUser=null;
        let selectedBotId=null;
        let selectedWebsiteId=null;
        let selectedCliToolId=null;
        let websiteLogInterval=null;
        let botLogInterval=null;
        let cliLogInterval=null;
        let cliProcessRunning=false;
        let uptimeIntervals={};
        let terminalPollInterval=null;
        let isTerminalRunning=false;
        async function apiCall(url,options={}){
            const res=await fetch(url,{...options,headers:{'Content-Type':'application/json',...options.headers}});
            if(!res.ok){const err=await res.json().catch(()=>({}));throw new Error(err.error||'API error');}
            return res.json();
        }
        async function handleLogin(){
            const username=loginUsername.value.trim();
            const password=loginPassword.value.trim();
            const role=loginRoleSelect.value;
            loginError.textContent='';
            loginCard.classList.remove('shake');
            if(!username||!password){loginError.textContent='Please enter username and password.';loginCard.classList.add('shake');return;}
            try{
                const data=await apiCall('/login',{method:'POST',body:JSON.stringify({username,password,role})});
                if(data.success){loginError.style.color='#00ff6a';loginError.textContent='✅ Login successful! Redirecting...';currentUser={username:data.username,role:data.role};setTimeout(()=>{location.reload();},800);}else{loginError.style.color='#ff4d4d';loginError.textContent=data.error||'Invalid credentials';loginCard.classList.add('shake');}
            }catch(e){loginError.style.color='#ff4d4d';loginError.textContent=e.message||'Login failed';loginCard.classList.add('shake');}
        }
        async function handleLogout(){
            const confirmed=await customConfirm('Logout?','👋');
            if(!confirmed)return;
            try{await apiCall('/logout',{method:'POST'});}catch(_){}
            location.reload();
        }
        const tabBtns=document.querySelectorAll('.tabs .tab-btn');
        tabBtns.forEach(btn=>{
            btn.addEventListener('click',function(){
                tabBtns.forEach(b=>b.classList.remove('active'));
                this.classList.add('active');
                const tabId=this.dataset.tab;
                document.querySelectorAll('.tab-content').forEach(tc=>tc.classList.remove('active'));
                document.getElementById('tab-'+tabId).classList.add('active');
                if(tabId==='websites'){loadWebsites();}
                else if(tabId==='bots'){loadBots();}
                else if(tabId==='cli'){loadCliTools();}
            });
        });
        // WEBSITES
        async function loadWebsites(){
            try{
                const res=await fetch('/api/websites');
                const data=await res.json();
                renderWebsites(data);
            }catch(e){console.error('Failed to load websites:',e);websiteGrid.innerHTML='<div class="empty-msg">Error loading websites</div>';}
        }
        function renderWebsites(websites){
            if(!websites||websites.length===0){websiteGrid.innerHTML='<div class="empty-msg">No websites deployed. Upload a project!</div>';return;}
            let html='';
            websites.forEach(w=>{
                const statusClass=w.status==='running'?'running':'stopped';
                const uptimeDisplay=w.status==='running'&&w.last_start_time?formatUptime((Date.now()/1000)-new Date(w.last_start_time).getTime()/1000):'--';
                html+=`<div class="website-card" data-id="${w.id}"><div class="website-header"><span class="website-name">${escapeHtml(w.website_name||w.website_slug)}</span><span class="bot-status ${statusClass}">● ${w.status.toUpperCase()}</span></div><div class="website-slug">🔗 ${escapeHtml(w.website_slug)}</div><div class="website-port">Port: ${w.allocated_port||'N/A'}</div><div class="bot-uptime" id="w-uptime-${w.id}">UPTIME: ${uptimeDisplay}</div><div class="website-actions"><button class="btn-visit-w" onclick="window.open('/${w.website_slug}/','_blank')">🌐 Visit</button><button class="btn-miniweb" onclick="window.open('/mini/website/${w.id}','_blank')">📱 Mini Web</button><button class="btn-start-w" data-action="start-w" data-id="${w.id}">▶ START</button><button class="btn-stop-w" data-action="stop-w" data-id="${w.id}">⏹ STOP</button><button class="btn-restart-w" data-action="restart-w" data-id="${w.id}">⟳ RESTART</button><button class="btn-delete-w" data-action="delete-w" data-id="${w.id}">🗑 DELETE</button><button class="btn-edit-w" data-action="edit-w" data-id="${w.id}">✎ EDIT</button><button class="btn-download-w" data-action="download-w" data-id="${w.id}">⬇ DOWNLOAD</button><button class="btn-files-w" data-action="files-w" data-id="${w.id}">📁 FILES</button></div><div class="name-edit"><input type="text" placeholder="Rename" id="w-name-input-${w.id}" value="${escapeHtml(w.website_name||'')}" /><button onclick="renameWebsite(${w.id})">Rename</button></div></div>`;
            });
            websiteGrid.innerHTML=html;
            document.querySelectorAll('.website-card [data-action]').forEach(btn=>{
                btn.addEventListener('click',async function(e){
                    e.stopPropagation();
                    const action=this.dataset.action;
                    const id=parseInt(this.dataset.id);
                    if(action==='start-w'){await websiteAction(id,'start');}
                    else if(action==='stop-w'){await websiteAction(id,'stop');}
                    else if(action==='restart-w'){await websiteAction(id,'restart');}
                    else if(action==='delete-w'){const confirmed=await customConfirm('Delete this website?','🗑️');if(confirmed){await websiteAction(id,'delete');}}
                    else if(action==='edit-w'){await editWebsite(id);}
                    else if(action==='download-w'){window.open(`/api/website/${id}/download`,'_blank');}
                    else if(action==='files-w'){window.open(`/website/${id}/files`,'_blank');}
                });
            });
            document.querySelectorAll('.website-card').forEach(card=>{
                card.addEventListener('click',function(e){if(e.target.closest('button')||e.target.closest('.name-edit'))return;const id=parseInt(this.dataset.id);selectWebsite(id);});
            });
            websites.forEach(w=>{if(w.status==='running'&&w.last_start_time){const start=new Date(w.last_start_time).getTime()/1000;startUptimeUpdate('w-uptime-'+w.id,start);}});
            if(!selectedWebsiteId&&websites.length>0){selectWebsite(websites[0].id);}
        }
        async function websiteAction(id,action){
            try{
                const res=await apiCall(`/api/website/${id}/${action}`,{method:'POST'});
                if(res.success){await loadWebsites();await customAlert(res.message||'Action successful','✅');}else{await customAlert(res.error||'Action failed','❌');}
            }catch(e){await customAlert(e.message,'❌');}
        }
        async function editWebsite(id){
            try{
                const data=await apiCall(`/api/website/${id}/content`);
                const content=data.content||'';
                const bodyHTML=`<div style="margin-bottom:8px;"><button class="btn-sm" id="copyAllBtnW" style="padding:6px 14px;font-size:.55rem;border:1px solid #33ddff;color:#33ddff;background:transparent;border-radius:6px;cursor:pointer;">📋 Copy All</button></div><textarea id="editFileContentW" rows="15" style="width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:.7rem;resize:vertical;tab-size:4;">${escapeHtml(content)}</textarea>`;
                const result=await showCustomModal('✎ Edit File',bodyHTML,[{label:'Cancel',value:false,className:'btn-cancel'},{label:'💾 SAVE',value:true,className:'btn-confirm'}]);
                if(result){
                    const newContent=document.getElementById('editFileContentW').value;
                    try{await apiCall(`/api/website/${id}/content`,{method:'PUT',body:JSON.stringify({content:newContent})});await customAlert('File saved and website restarted (if running).','✅');await loadWebsites();}catch(e){await customAlert(e.message,'❌');}
                }
                setTimeout(()=>{const copyBtn=document.getElementById('copyAllBtnW');if(copyBtn){copyBtn.onclick=function(){const textarea=document.getElementById('editFileContentW');textarea.select();try{navigator.clipboard.writeText(textarea.value).then(()=>{customAlert('📋 Copied all code!','✅');}).catch(()=>{document.execCommand('copy');customAlert('📋 Copied!','✅');});}catch(e){document.execCommand('copy');customAlert('📋 Copied!','✅');}};}},100);
            }catch(e){await customAlert(e.message,'❌');}
        }
        function selectWebsite(id){
            selectedWebsiteId=id;
            document.querySelectorAll('.website-card').forEach(c=>c.classList.remove('selected'));
            const card=document.querySelector(`.website-card[data-id="${id}"]`);
            if(card)card.classList.add('selected');
            loadWebsiteLogs(id);
            if(websiteLogInterval)clearInterval(websiteLogInterval);
            websiteLogInterval=setInterval(()=>loadWebsiteLogs(id,true),3000);
        }
        async function loadWebsiteLogs(id,silent=false){
            try{const data=await apiCall(`/api/website/${id}/logs`);websiteConsole.textContent=data.logs||'No logs yet.';}catch(e){if(!silent)websiteConsole.textContent='Error loading logs.';}
        }
        async function renameWebsite(id){
            const input=document.getElementById('w-name-input-'+id);
            const newName=input.value.trim();
            if(!newName)return;
            try{
                const formData=new FormData();
                formData.append('name',newName);
                const res=await fetch(`/api/website/${id}/rename`,{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){await loadWebsites();}else{await customAlert(data.error||'Rename failed','❌');}
            }catch(e){await customAlert(e.message,'❌');}
        }
        uploadCardWebsite.addEventListener('click',function(e){if(e.target.closest('.deploy-btn'))return;fileInputWebsite.click();});
        deployBtnWebsite.addEventListener('click',async function(){
            if(fileInputWebsite.files.length===0){await customAlert('Please select at least one file first.','⚠️');return;}
            const formData=new FormData();
            for(let i=0;i<fileInputWebsite.files.length;i++){formData.append('files[]',fileInputWebsite.files[i]);}
            try{
                this.textContent='UPLOADING...';
                this.disabled=true;
                const res=await fetch('/upload_website',{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){await customAlert(`Website deployed! ID: ${data.website_id}`,'✅');await loadWebsites();fileInputWebsite.value='';fileCountDisplayWebsite.textContent='';}else{await customAlert(data.error||'Upload failed','❌');}
            }catch(e){}finally{this.textContent='DEPLOY WEBSITE';this.disabled=false;}
        });
        fileInputWebsite.addEventListener('change',function(){const count=this.files.length;if(count===0){fileCountDisplayWebsite.textContent='';}else{const names=Array.from(this.files).map(f=>f.name).join(', ');fileCountDisplayWebsite.textContent=`${count} file(s) selected: ${names}`;}});
        // BOTS
        async function loadBots(){
            try{
                const bots=await apiCall('/api/bots');
                renderBots(bots);
            }catch(e){console.error('Failed to load bots:',e);botListContainer.innerHTML='<div class="empty-msg">Error loading bots</div>';}
        }
        function renderBots(bots){
            if(!bots||bots.length===0){botListContainer.innerHTML='<div class="empty-msg">No bots deployed. Upload a project!</div>';return;}
            let html='';
            bots.forEach(bot=>{
                const statusClass=bot.status==='running'?'running':'stopped';
                const uptimeDisplay=bot.status==='running'&&bot.start_time?formatUptime(Date.now()/1000-bot.start_time):'--';
                const selected=(bot.id===selectedBotId)?'selected':'';
                const hasToken=bot.has_token||false;
                const botUsername=bot.bot_username||null;
                html+=`<div class="bot-card ${selected}" data-id="${bot.id}"><div class="bot-header"><span class="bot-name">${escapeHtml(bot.filename)}</span><span class="bot-status ${statusClass}">● ${bot.status.toUpperCase()}</span></div><div class="bot-owner">👤 ${escapeHtml(bot.user)}</div><div class="bot-uptime" id="uptime-${bot.id}">UPTIME: ${uptimeDisplay}</div><div class="bot-controls"><button class="btn-start" data-action="start">${bot.status==='running'?'▶ RUNNING':'▶ START'}</button><button class="btn-stop" data-action="stop">⏹ STOP</button><button class="btn-edit" data-action="edit">✎ EDIT</button><button class="btn-restart" data-action="restart">⟳ RESTART</button><button class="btn-download" data-action="download">⬇ DOWNLOAD</button><button class="btn-delete" data-action="delete">🗑 DELETE</button>${(hasToken&&botUsername)?`<button class="btn-openbot" data-action="openbot" data-bot="${botUsername}">🤖 Open Bot</button>`:''}<button class="btn-miniweb" onclick="window.open('/mini/bot/${bot.id}','_blank')">📱 Mini Web</button></div></div>`;
            });
            botListContainer.innerHTML=html;
            document.querySelectorAll('.bot-card').forEach(card=>{
                card.addEventListener('click',function(e){if(e.target.closest('button'))return;const id=this.dataset.id;selectBot(id);});
            });
            document.querySelectorAll('.bot-card [data-action]').forEach(btn=>{
                btn.addEventListener('click',async function(e){
                    e.stopPropagation();
                    const action=this.dataset.action;
                    const card=this.closest('.bot-card');
                    const botId=card.dataset.id;
                    if(action==='openbot'){const botUsername=this.dataset.bot;if(botUsername){window.open(`https://t.me/${botUsername}`,'_blank');await customAlert(`🤖 Opening @${botUsername}`,'✅');}return;}
                    if(action==='edit'){await openEditModal(botId);return;}
                    if(action==='download'){window.open(`/api/bots/${botId}/download`,'_blank');return;}
                    await handleBotAction(botId,action);
                });
            });
            bots.forEach(bot=>{if(bot.status==='running'&&bot.start_time){startUptimeUpdate('uptime-'+bot.id,bot.start_time);}});
            if(!selectedBotId&&bots.length>0){selectBot(bots[0].id);}
        }
        function startUptimeUpdate(elId,startTime){
            if(uptimeIntervals[elId])clearInterval(uptimeIntervals[elId]);
            const el=document.getElementById(elId);
            if(!el)return;
            uptimeIntervals[elId]=setInterval(()=>{const now=Date.now()/1000;const diff=now-startTime;el.textContent='UPTIME: '+formatUptime(diff);},1000);
        }
        function selectBot(botId){
            selectedBotId=botId;
            document.querySelectorAll('.bot-card').forEach(c=>c.classList.remove('selected'));
            const card=document.querySelector(`.bot-card[data-id="${botId}"]`);
            if(card)card.classList.add('selected');
            loadBotLogs(botId);
            if(botLogInterval)clearInterval(botLogInterval);
            botLogInterval=setInterval(()=>loadBotLogs(botId,true),3000);
        }
        async function loadBotLogs(botId,silent=false){
            try{const data=await apiCall(`/api/bots/${botId}/logs`);botConsole.textContent=data.logs||'No logs yet.';}catch(e){if(!silent)botConsole.textContent='Error loading logs.';}
        }
        async function handleBotAction(botId,action){
            try{
                if(action==='start'){await apiCall(`/api/bots/${botId}/start`,{method:'POST'});}
                else if(action==='stop'){await apiCall(`/api/bots/${botId}/stop`,{method:'POST'});}
                else if(action==='restart'){await apiCall(`/api/bots/${botId}/restart`,{method:'POST'});}
                else if(action==='delete'){const confirmed=await customConfirm('Delete this bot?','🗑️');if(!confirmed)return;await apiCall(`/api/bots/${botId}`,{method:'DELETE'});if(selectedBotId===botId){selectedBotId=null;if(botLogInterval)clearInterval(botLogInterval);botConsole.textContent='Bot deleted.';}}
                await loadBots();
            }catch(e){await customAlert(e.message||'Action failed','❌');}
        }
        async function openEditModal(botId){
            try{
                const data=await apiCall(`/api/bots/${botId}/content`);
                const content=data.content||'';
                const bodyHTML=`<div style="margin-bottom:8px;"><button class="btn-sm" id="copyAllBtn" style="padding:6px 14px;font-size:.55rem;border:1px solid #33ddff;color:#33ddff;background:transparent;border-radius:6px;cursor:pointer;">📋 Copy All</button></div><textarea id="editFileContent" rows="15" style="width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:.7rem;resize:vertical;tab-size:4;">${escapeHtml(content)}</textarea>`;
                const result=await showCustomModal('✎ Edit File',bodyHTML,[{label:'Cancel',value:false,className:'btn-cancel'},{label:'💾 SAVE',value:true,className:'btn-confirm'}]);
                if(result){
                    const newContent=document.getElementById('editFileContent').value;
                    try{await apiCall(`/api/bots/${botId}/content`,{method:'PUT',body:JSON.stringify({content:newContent})});await customAlert('File saved and bot restarted (if running).','✅');await loadBots();}catch(e){await customAlert(e.message,'❌');}
                }
                setTimeout(()=>{const copyBtn=document.getElementById('copyAllBtn');if(copyBtn){copyBtn.onclick=function(){const textarea=document.getElementById('editFileContent');textarea.select();try{navigator.clipboard.writeText(textarea.value).then(()=>{customAlert('📋 Copied all code!','✅');}).catch(()=>{document.execCommand('copy');customAlert('📋 Copied!','✅');});}catch(e){document.execCommand('copy');customAlert('📋 Copied!','✅');}};}},100);
            }catch(e){await customAlert(e.message,'❌');}
        }
        uploadCardBot.addEventListener('click',function(e){if(e.target.closest('.deploy-btn'))return;fileInputBot.click();});
        deployBtnBot.addEventListener('click',async function(){
            if(fileInputBot.files.length===0){await customAlert('Please select at least one file first.','⚠️');return;}
            const formData=new FormData();
            for(let i=0;i<fileInputBot.files.length;i++){formData.append('files[]',fileInputBot.files[i]);}
            try{
                this.textContent='UPLOADING...';
                this.disabled=true;
                const res=await fetch('/upload',{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){await customAlert(`Uploaded! ${data.bots_created} bot(s) created.`,'✅');await loadBots();fileInputBot.value='';fileCountDisplayBot.textContent='';}else{await customAlert(data.error||'Upload failed','❌');}
            }catch(e){}finally{this.textContent='DEPLOY BOT';this.disabled=false;}
        });
        fileInputBot.addEventListener('change',function(){const count=this.files.length;if(count===0){fileCountDisplayBot.textContent='';}else{const names=Array.from(this.files).map(f=>f.name).join(', ');fileCountDisplayBot.textContent=`${count} file(s) selected: ${names}`;}});
        // CLI TOOLS
        let cliUploadResolve=null;
        async function loadCliTools(){
            try{
                const res=await fetch('/api/cli_tools');
                const data=await res.json();
                renderCliTools(data);
            }catch(e){console.error('Failed to load CLI tools:',e);cliToolListContainer.innerHTML='<div class="empty-msg">Error loading CLI tools</div>';}
        }
        function renderCliTools(tools){
            if(!tools||tools.length===0){cliToolListContainer.innerHTML='<div class="empty-msg">No CLI tools deployed. Upload a project!</div>';return;}
            let html='';
            tools.forEach(tool=>{
                const statusClass=tool.status==='running'?'running':'stopped';
                html+=`<div class="cli-card" data-id="${tool.id}"><div class="bot-header"><span class="bot-name">${escapeHtml(tool.startup_file)}</span><span class="bot-status ${statusClass}">● ${tool.status.toUpperCase()}</span></div><div style="font-size:12px;color:#888;">Interpreter: ${tool.interpreter||'N/A'}</div><div class="cli-tool-controls"><button class="btn-start" data-action="start-cli" data-id="${tool.id}">▶ START</button><button class="btn-stop" data-action="stop-cli" data-id="${tool.id}">⏹ STOP</button><button class="btn-delete" data-action="delete-cli" data-id="${tool.id}">🗑 DELETE</button><button class="btn-download" data-action="download-cli" data-id="${tool.id}">⬇ DOWNLOAD</button></div></div>`;
            });
            cliToolListContainer.innerHTML=html;
            document.querySelectorAll('.cli-card [data-action]').forEach(btn=>{
                btn.addEventListener('click',async function(e){
                    e.stopPropagation();
                    const action=this.dataset.action;
                    const id=parseInt(this.dataset.id);
                    if(action==='start-cli'){await cliAction(id,'start');}
                    else if(action==='stop-cli'){await cliAction(id,'stop');}
                    else if(action==='delete-cli'){const confirmed=await customConfirm('Delete this CLI tool?','🗑️');if(confirmed){await cliAction(id,'delete');}}
                    else if(action==='download-cli'){window.open(`/api/cli_tool/${id}/download`,'_blank');}
                });
            });
            document.querySelectorAll('.cli-card').forEach(card=>{
                card.addEventListener('click',function(e){if(e.target.closest('button'))return;const id=parseInt(this.dataset.id);selectCliTool(id);});
            });
            if(!selectedCliToolId&&tools.length>0){selectCliTool(tools[0].id);}
        }
        async function cliAction(id,action){
            try{
                const res=await apiCall(`/api/cli_tool/${id}/${action}`,{method:'POST'});
                if(res.success){await loadCliTools();await customAlert(res.message||'Action successful','✅');if(action==='start'){cliProcessRunning=true;cliTerminalInput.disabled=false;cliSendBtn.disabled=false;cliUploadFileBtn.disabled=false;cliStatusBadge.textContent='● RUNNING';cliStatusBadge.className='status-badge status-running';updateCliLogs(id);}else if(action==='stop'){cliProcessRunning=false;cliTerminalInput.disabled=true;cliSendBtn.disabled=true;cliUploadFileBtn.disabled=true;cliStatusBadge.textContent='● STOPPED';cliStatusBadge.className='status-badge status-stopped';}}else{await customAlert(res.error||'Action failed','❌');}
            }catch(e){await customAlert(e.message,'❌');}
        }
        function selectCliTool(id){
            selectedCliToolId=id;
            document.querySelectorAll('.cli-card').forEach(c=>c.classList.remove('selected'));
            const card=document.querySelector(`.cli-card[data-id="${id}"]`);
            if(card)card.classList.add('selected');
            updateCliLogs(id);
            if(cliLogInterval)clearInterval(cliLogInterval);
            cliLogInterval=setInterval(()=>updateCliLogs(id,true),3000);
        }
        async function updateCliLogs(id,silent=false){
            try{const data=await apiCall(`/api/cli_tool/${id}/logs`);cliTerminal.textContent=data.logs||'No logs yet.';cliTerminal.scrollTop=cliTerminal.scrollHeight;}catch(e){if(!silent)console.error('Error loading CLI logs:',e);}
        }
        cliSendBtn.addEventListener('click',async function(){
            const data=cliTerminalInput.value;
            if(!data||!selectedCliToolId)return;
            try{
                await apiCall(`/api/cli_tool/${selectedCliToolId}/send_input`,{method:'POST',body:JSON.stringify({input:data})});
                cliTerminal.textContent+=`\n> ${data}`;
                cliTerminal.scrollTop=cliTerminal.scrollHeight;
                cliTerminalInput.value='';
            }catch(e){await customAlert(e.message,'❌');}
        });
        cliTerminalInput.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();cliSendBtn.click();}});
        cliClearBtn.addEventListener('click',function(){cliTerminal.textContent='';});
        cliUploadFileBtn.addEventListener('click',function(){cliFileUploadInput.click();});
        cliFileUploadInput.addEventListener('change',async function(){
            const file=this.files[0];
            if(!file)return;
            cliUploadStatus.style.display='block';
            cliUploadStatus.innerHTML=`<span style="color:#ffaa00;">⏳ Uploading: ${file.name}...</span>`;
            try{
                const formData=new FormData();
                formData.append('file',file);
                const res=await fetch(`/api/cli_tool/${selectedCliToolId}/upload_file`,{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){
                    cliUploadStatus.innerHTML=`<span style="color:#00ff6a;">✅ Uploaded: ${data.filename}</span>`;
                    if(cliUploadResolve){cliUploadResolve(data.filepath);cliUploadResolve=null;}
                    this.value='';
                }else{cliUploadStatus.innerHTML=`<span style="color:#ff4d4d;">❌ Failed: ${data.error}</span>`;}
            }catch(e){cliUploadStatus.innerHTML=`<span style="color:#ff4d4d;">❌ Error: ${e.message}</span>`;}
        });
        uploadCardCli.addEventListener('click',function(e){if(e.target.closest('.deploy-btn'))return;fileInputCli.click();});
        deployBtnCli.addEventListener('click',async function(){
            if(fileInputCli.files.length===0){await customAlert('Please select at least one file first.','⚠️');return;}
            const formData=new FormData();
            for(let i=0;i<fileInputCli.files.length;i++){formData.append('files[]',fileInputCli.files[i]);}
            try{
                this.textContent='UPLOADING...';
                this.disabled=true;
                const res=await fetch('/upload_cli',{method:'POST',body:formData});
                const data=await res.json();
                if(data.success){await customAlert('CLI tool deployed!','✅');await loadCliTools();fileInputCli.value='';fileCountDisplayCli.textContent='';}else{await customAlert(data.error||'Upload failed','❌');}
            }catch(e){}finally{this.textContent='DEPLOY CLI TOOL';this.disabled=false;}
        });
        fileInputCli.addEventListener('change',function(){const count=this.files.length;if(count===0){fileCountDisplayCli.textContent='';}else{const names=Array.from(this.files).map(f=>f.name).join(', ');fileCountDisplayCli.textContent=`${count} file(s) selected: ${names}`;}});
        // ADMIN PANEL
        function openAdminPanel(){
            if(!currentUser){customAlert('Please login first.','⚠️');return;}
            if(currentUser.role!=='admin'){
                const username=currentUser.username;
                const password='{{ user_password }}';
                const bodyHTML=`<div style="text-align:center;"><div style="font-size:20px;margin-bottom:20px;">👤 Your Profile</div><div style="background:#161b25;padding:15px;border-radius:10px;margin-bottom:10px;"><strong style="color:#00e5ff;">Username</strong><br /><span style="font-size:18px;color:#fff;">${username}</span></div><div style="background:#161b25;padding:15px;border-radius:10px;"><strong style="color:#00e5ff;">Password</strong><br /><span style="font-size:18px;color:#fff;">${password}</span></div></div>`;
                showCustomModal('ℹ️',bodyHTML,[{label:'OK',value:true,className:'btn-ok'}]);
                return;
            }
            loadAdminUsers();
            adminOverlay.classList.add('open');
        }
        function closeAdminPanel(){adminOverlay.classList.remove('open');}
        async function loadAdminUsers(){
            try{const users=await apiCall('/api/users');renderFullUserList(users);renderSimpleLists(users);}catch(e){console.error('Failed to load users:',e);}
        }
        function renderFullUserList(users){
            if(!users||users.length===0){fullUserListContainer.innerHTML='<div class="empty-msg">No users found.</div>';return;}
            let html='';
            users.forEach(u=>{
                const bannedClass=u.banned?'banned':(u.role==='admin'?'admin':'user');
                const bannedText=u.banned?'UNBAN':'BAN';
                const roleLabel=u.role.toUpperCase();
                let expiryDisplay='Never';
                if(u.expires_at){try{const exp=new Date(u.expires_at);expiryDisplay=exp.toLocaleString();}catch(e){expiryDisplay='Invalid';}}
                html+=`<div class="list-item" data-username="${u.username}"><div class="row"><div class="info"><span class="uname">${escapeHtml(u.username)}</span><span class="upass">🔑 ${escapeHtml(u.password)}</span><span style="font-size:12px;color:#888;">Expires: ${expiryDisplay}</span></div><span class="badge-role ${bannedClass}">${u.banned?'BANNED':roleLabel}</span></div><div class="row"><div class="limit-group"><label>Limit:</label><input type="number" class="limit-input" value="${u.limit||0}" min="0" step="1" /></div><button class="btn-action btn-set" data-action="setLimit" data-username="${u.username}">SET</button><button class="btn-action btn-ban" data-action="toggleBan" data-username="${u.username}">${bannedText}</button></div><div class="row"><input type="text" placeholder="New password..." style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newPass" /><button class="btn-action btn-reset" data-action="resetPass" data-username="${u.username}">RESET PW</button></div><div class="row"><input type="text" placeholder="New expiry (e.g. 5, 1m, 2h)" style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newExpiry" /><button class="btn-action btn-set" data-action="setExpiry" data-username="${u.username}">SET EXPIRY</button></div><button class="btn-action btn-del" data-action="deleteUser" data-username="${u.username}">DELETE USER + ALL BOTS</button></div>`;
            });
            fullUserListContainer.innerHTML=html;
            attachFullListEvents();
        }
        function attachFullListEvents(){
            document.querySelectorAll('#fullUserListContainer [data-action]').forEach(btn=>{
                btn.addEventListener('click',async function(e){
                    e.stopPropagation();
                    const action=this.dataset.action;
                    const username=this.dataset.username;
                    const card=this.closest('.list-item');
                    if(action==='setLimit'){
                        const input=card.querySelector('.limit-input');
                        const val=parseInt(input.value,10);
                        if(isNaN(val)||val<0){await customAlert('Enter a valid number.','⚠️');return;}
                        try{await apiCall(`/api/users/${username}`,{method:'PUT',body:JSON.stringify({limit:val})});await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                    }else if(action==='toggleBan'){
                        const user=(await apiCall('/api/users')).find(u=>u.username===username);
                        if(!user)return;
                        try{await apiCall(`/api/users/${username}`,{method:'PUT',body:JSON.stringify({banned:!user.banned})});await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                    }else if(action==='resetPass'){
                        const passInput=card.querySelector('[data-field="newPass"]');
                        const newPass=passInput.value.trim();
                        if(!newPass){await customAlert('Enter a new password.','⚠️');return;}
                        try{await apiCall(`/api/users/${username}`,{method:'PUT',body:JSON.stringify({password:newPass})});passInput.value='';await customAlert('Password updated.','✅');await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                    }else if(action==='setExpiry'){
                        const expiryInput=card.querySelector('[data-field="newExpiry"]');
                        const expiry=expiryInput.value.trim();
                        try{await apiCall(`/api/users/${username}`,{method:'PUT',body:JSON.stringify({expiry:expiry})});expiryInput.value='';await customAlert('Expiry updated.','✅');await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                    }else if(action==='deleteUser'){
                        const confirmed=await customConfirm(`Delete user ${username} and all their bots?`,'🗑️');
                        if(!confirmed)return;
                        if(username===currentUser.username){await customAlert('Cannot delete yourself.','🚫');return;}
                        try{await apiCall(`/api/users/${username}`,{method:'DELETE'});await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                    }
                });
            });
        }
        function renderSimpleLists(users){
            const admins=users.filter(u=>u.role==='admin'&&!u.banned);
            const regulars=users.filter(u=>u.role==='user'&&!u.banned);
            if(!admins.length){simpleAdminListContainer.innerHTML='<div class="empty-msg">No admins.</div>';}else{let html='';admins.forEach(u=>{html+=`<div class="simple-list-item" data-username="${u.username}"><div class="info"><span class="uname">${escapeHtml(u.username)}</span><span class="upass">🔑 ${escapeHtml(u.password)}</span></div><div class="actions"><button class="btn-remove-simple" data-username="${u.username}">REMOVE</button></div></div>`;});simpleAdminListContainer.innerHTML=html;}
            if(!regulars.length){simpleUserListContainer.innerHTML='<div class="empty-msg">No users.</div>';}else{let html='';regulars.forEach(u=>{html+=`<div class="simple-list-item" data-username="${u.username}"><div class="info"><span class="uname">${escapeHtml(u.username)}</span><span class="upass">🔑 ${escapeHtml(u.password)}</span></div><div class="actions"><button class="btn-remove-simple" data-username="${u.username}">REMOVE</button></div></div>`;});simpleUserListContainer.innerHTML=html;}
            document.querySelectorAll('.btn-remove-simple').forEach(btn=>{
                btn.addEventListener('click',async function(e){
                    e.stopPropagation();
                    const username=this.dataset.username;
                    const confirmed=await customConfirm(`Remove user ${username}?`,'🗑️');
                    if(!confirmed)return;
                    if(username===currentUser.username){await customAlert('Cannot remove yourself.','🚫');return;}
                    try{await apiCall(`/api/users/${username}`,{method:'DELETE'});await loadAdminUsers();}catch(e){await customAlert(e.message,'❌');}
                });
            });
        }
        async function handleCreateUser(){
            const username=newUsername.value.trim();
            const password=newPassword.value.trim();
            const expiry=newExpiry.value.trim();
            const role=newRole.value;
            if(!username||!password){await customAlert('Username and Password required.','⚠️');return;}
            try{await apiCall('/api/users',{method:'POST',body:JSON.stringify({username,password,role,expiry})});await loadAdminUsers();newUsername.value='';newPassword.value='';newExpiry.value='';createUserForm.style.display='none';await customAlert(`User ${username} created.`,'✅');}catch(e){await customAlert(e.message||'Creation failed','❌');}
        }
        // FILE MANAGER
        const fileManagerList=document.getElementById('fileManagerList');
        const fileBreadcrumb=document.getElementById('fileBreadcrumb');
        const contextMenu=document.getElementById('fileContextMenu');
        const ctxDelete=document.getElementById('ctxDelete');
        const ctxRename=document.getElementById('ctxRename');
        const ctxDownload=document.getElementById('ctxDownload');
        let currentPath='';
        let selectedFilePath=null;
        async function loadDirectory(path=''){
            currentPath=path;
            try{
                const res=await fetch(`/api/files?path=${encodeURIComponent(path)}`);
                if(!res.ok){const err=await res.json();await customAlert(err.error||'Failed to load','❌');return;}
                const data=await res.json();
                renderFileList(data);
            }catch(e){await customAlert('Error: '+e.message,'❌');}
        }
        function renderFileList(data){
            const items=data.items||[];
            let breadHtml='';
            const parts=currentPath.split('/').filter(p=>p);
            let cum='';
            breadHtml+=`<span onclick="window._loadDirectory('')">📁 root</span>`;
            parts.forEach((p,idx)=>{cum+=(cum?'/':'')+p;breadHtml+=`<span class="sep">/</span><span onclick="window._loadDirectory('${cum}')">${escapeHtml(p)}</span>`;});
            fileBreadcrumb.innerHTML=breadHtml;
            let html='';
            if(currentPath){html+=`<div class="file-item" onclick="window._loadDirectory('${currentPath.split('/').slice(0,-1).join('/')}')"><span class="name"><i class="fa-solid fa-arrow-up"></i> ..</span></div>`;}
            items.forEach(item=>{const icon=item.type==='directory'?'<i class="fa-solid fa-folder dir-icon"></i>':'<i class="fa-solid fa-file"></i>';const sizeText=item.type==='file'?(item.size/1024).toFixed(1)+' KB':'';html+=`<div class="file-item" data-path="${item.path}" data-type="${item.type}"><span class="name">${icon} ${escapeHtml(item.name)}</span><span class="size">${sizeText}</span></div>`;});
            fileManagerList.innerHTML=html;
            document.querySelectorAll('.file-item').forEach(el=>{
                el.addEventListener('click',function(e){const path=this.dataset.path;const type=this.dataset.type;if(type==='directory'){window._loadDirectory(path);}else{document.querySelectorAll('.file-item').forEach(f=>f.classList.remove('selected'));this.classList.add('selected');selectedFilePath=path;}});
                let timer;el.addEventListener('touchstart',function(e){timer=setTimeout(()=>{e.preventDefault();const path=this.dataset.path;showContextMenu(e.touches[0].clientX,e.touches[0].clientY,path);document.querySelectorAll('.file-item').forEach(f=>f.classList.remove('selected'));this.classList.add('selected');selectedFilePath=path;},3000);});el.addEventListener('touchend',function(){clearTimeout(timer);});el.addEventListener('touchmove',function(){clearTimeout(timer);});
                el.addEventListener('contextmenu',function(e){e.preventDefault();const path=this.dataset.path;showContextMenu(e.clientX,e.clientY,path);document.querySelectorAll('.file-item').forEach(f=>f.classList.remove('selected'));this.classList.add('selected');selectedFilePath=path;});
            });
        }
        function showContextMenu(x,y,path){contextMenu.style.display='block';contextMenu.style.left=x+'px';contextMenu.style.top=y+'px';contextMenu.dataset.path=path;}
        function hideContextMenu(){contextMenu.style.display='none';}
        ctxDelete.addEventListener('click',async function(){const path=contextMenu.dataset.path||selectedFilePath;if(!path)return;hideContextMenu();const confirmed=await customConfirm(`Delete ${path}?`,'🗑️');if(!confirmed)return;try{const res=await fetch('/api/files/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})});const data=await res.json();if(data.success){await customAlert('Deleted.','✅');loadDirectory(currentPath);}else{await customAlert(data.error||'Delete failed','❌');}}catch(e){}});
        ctxRename.addEventListener('click',async function(){const path=contextMenu.dataset.path||selectedFilePath;if(!path)return;hideContextMenu();const newName=await customPrompt('Enter new name:',path.split('/').pop());if(newName===null)return;if(!newName.trim()){await customAlert('Name cannot be empty.','⚠️');return;}try{const res=await fetch('/api/files/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_path:path,new_name:newName.trim()})});const data=await res.json();if(data.success){await customAlert('Renamed.','✅');loadDirectory(currentPath);}else{await customAlert(data.error||'Rename failed','❌');}}catch(e){}});
        ctxDownload.addEventListener('click',function(){const path=contextMenu.dataset.path||selectedFilePath;if(!path)return;hideContextMenu();window.open(`/api/files/download?path=${encodeURIComponent(path)}`,'_blank');});
        function customPrompt(message,defaultValue){return new Promise((resolve)=>{const bodyHTML=`<div style="text-align:center;"><p style="margin-bottom:12px;">${message}</p><input type="text" id="promptInput" value="${escapeHtml(defaultValue||'')}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" /></div>`;showCustomModal('✏️',bodyHTML,[{label:'Cancel',value:null,className:'btn-cancel'},{label:'OK',value:true,className:'btn-confirm'}]).then((result)=>{if(result===null)resolve(null);else{const val=document.getElementById('promptInput')?.value;resolve(val);}});});}
        window._loadDirectory=function(path){hideContextMenu();loadDirectory(path);};
        document.addEventListener('click',function(e){if(!contextMenu.contains(e.target)){hideContextMenu();}});
        // ADMIN TABS
        const adminTabBtns=document.querySelectorAll('.admin-tabs button');
        const adminTabContents={tabAdminMenu:document.getElementById('tabAdminMenu'),tabUserMenu:document.getElementById('tabUserMenu'),tabTerminal:document.getElementById('tabTerminal'),tabFileManager:document.getElementById('tabFileManager'),tabStats:document.getElementById('tabStats')};
        adminTabBtns.forEach(btn=>{
            btn.addEventListener('click',function(){
                const tabId=this.dataset.tab;
                adminTabBtns.forEach(b=>b.classList.remove('active'));
                this.classList.add('active');
                Object.keys(adminTabContents).forEach(key=>{if(adminTabContents[key]){adminTabContents[key].classList.toggle('active',key===tabId);}});
                if(tabId==='tabTerminal'){setTimeout(()=>terminalCommand.focus(),100);if(!terminalPollInterval){startTerminalPolling();}}
                if(tabId==='tabFileManager'){loadDirectory('');}
                if(tabId==='tabStats'){fetchStats();if(window.statsInterval)clearInterval(window.statsInterval);window.statsInterval=setInterval(fetchStats,5000);}else{if(window.statsInterval){clearInterval(window.statsInterval);window.statsInterval=null;}}
            });
        });
        // TERMINAL
        async function startTerminalPolling(){
            if(terminalPollInterval)clearInterval(terminalPollInterval);
            try{await fetch('/api/terminal/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'{{ password }}'})});}catch(e){console.error('Failed to start terminal:',e);}
            terminalPollInterval=setInterval(async ()=>{
                try{const res=await fetch('/api/terminal/read');const data=await res.json();if(data.output){terminalOutput.innerHTML+=`<span class="output">${escapeHtml(data.output)}</span>`;terminalOutput.scrollTop=terminalOutput.scrollHeight;}isTerminalRunning=data.running;termStopBtn.disabled=!isTerminalRunning;}catch(e){}
            },500);
        }
        async function sendTerminalData(data){try{await fetch('/api/terminal/send',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({data:data,password:'{{ password }}'})});}catch(e){}}
        async function stopTerminal(){try{await fetch('/api/terminal/stop',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({password:'{{ password }}'})});terminalOutput.innerHTML+=`<span class="prompt">[Terminal stopped]</span><br />`;isTerminalRunning=false;termStopBtn.disabled=true;setTimeout(()=>{startTerminalPolling();},500);}catch(e){}}
        function clearTerminal(){terminalOutput.innerHTML='<span class="prompt">$ </span>Terminal cleared.<br />';}
        termRunBtn.addEventListener('click',function(){const data=terminalCommand.value;if(!data)return;sendTerminalData(data);terminalCommand.value='';});
        termStopBtn.addEventListener('click',stopTerminal);termClearBtn.addEventListener('click',clearTerminal);
        terminalCommand.addEventListener('keydown',function(e){if(e.key==='Enter'){e.preventDefault();termRunBtn.click();}});
        // STATS
        async function fetchStats(){
            try{const res=await fetch('/api/stats');if(!res.ok)return;const data=await res.json();document.getElementById('statTotalHours').textContent=data.total_hours+' hrs';document.getElementById('statMainHours').textContent=data.main_hours+' hrs';document.getElementById('statInternalHours').textContent=data.internal_hours+' hrs';document.getElementById('statStorage').textContent=data.storage_used_gb+' GB';document.getElementById('statDiskFree').textContent=data.disk_free_gb;document.getElementById('statRam').textContent=data.ram.percent+'%';document.getElementById('statRamUsed').textContent=data.ram.used_mb;document.getElementById('statRamTotal').textContent=data.ram.total_mb;document.getElementById('ramFill').style.width=Math.min(data.ram.percent,100)+'%';document.getElementById('statCpu').textContent=data.cpu_percent+'%';}catch(e){console.error('Stats error:',e);}
        }
        document.getElementById('setOffsetBtn')?.addEventListener('click',async function(){const val=parseFloat(document.getElementById('offsetInput').value);if(isNaN(val)){await customAlert('Enter valid number','⚠️');return;}try{const res=await fetch('/api/set_offset',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({offset:val})});const data=await res.json();if(data.success){await customAlert('Offset set to '+val+' hrs','✅');fetchStats();}else{await customAlert(data.error||'Failed','❌');}}catch(e){await customAlert(e.message,'❌');}});
        // EVENT BINDINGS
        loginBtn.addEventListener('click',handleLogin);
        document.addEventListener('keydown',function(e){if(e.key==='Enter'&&loginView.classList.contains('active')){handleLogin();}});
        hamburgerBtn.addEventListener('click',openAdminPanel);
        adminCloseBtn.addEventListener('click',closeAdminPanel);
        adminOverlay.addEventListener('click',function(e){if(e.target===this)closeAdminPanel();});
        logoutBtn.addEventListener('click',handleLogout);
        toggleCreateUserBtn.addEventListener('click',function(){const form=document.getElementById('createUserForm');form.style.display=form.style.display==='none'?'block':'none';});
        createUserBtn.addEventListener('click',handleCreateUser);
        // INIT
        const loggedIn={{ logged_in|tojson }};
        if(loggedIn){currentUser={username:'{{ username }}',role:'{{ session.get("role", "") }}'};loadWebsites();loadBots();loadCliTools();if(currentUser.role==='admin'){loadAdminUsers();}}
        function escapeHtml(str){const div=document.createElement('div');div.textContent=str;return div.innerHTML;}
        function formatUptime(seconds){if(seconds<0)return '--';const d=Math.floor(seconds/86400);const h=Math.floor((seconds%86400)/3600);const m=Math.floor((seconds%3600)/60);const s=Math.floor(seconds%60);return `${d}d ${h}h ${m}m ${s}s`;}
        console.log('🔐 YUVICODEX System ready (Websites + Bots + CLI).');
        console.log('📋 Default accounts: admin/admin123 (admin), user1/pass123, user2/pass456');
        console.log('💻 Use upload to deploy websites, bots or CLI tools.');
        console.log('🔑 Master Password: {{ MASTER_PASSWORD if MASTER_PASSWORD else "not set" }}');
        console.log('🔐 Secret Key: {{ SECRET_KEY if SECRET_KEY else "not set" }}');
        console.log('👉 Click logo 5 times for secret key login.');
        console.log('📡 Interactive terminal started.');
        console.log('🗑️ Logs auto-clear every 1 hour.');
    })();
    </script>
</body>
</html>
"""

# ---------- LOG FOLDER ----------
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
os.makedirs(LOG_FOLDER, exist_ok=True)

# ---------- PROCESSES ----------
processes = {}

# ---------- MAIN START ----------
MAIN_START_TIME = int(time.time())

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🚀 YUVICODEX ULTIMATE (Websites + Bots + CLI)")
    print(f"🌐 Port: {port}")
    print("👤 Admin: admin / admin123")
    print("📊 Stats: Owner only. Set Offset from Render Dashboard.")
    print("🌍 Websites at /<slug>/")
    print("💻 CLI Tools with Terminal and File Upload")
    print("🗑️ Logs auto-clear every 1 hour")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)