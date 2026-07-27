import os
import sys
import sqlite3
import zipfile
import shutil
import subprocess
import signal
import time
import re
import json
import requests
import threading
import queue
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, abort, Response, stream_with_context
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key_change_me_in_production'

# ---------- कॉन्फ़िगरेशन ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py']
AUTO_RESTART_MAX = 3
AUTO_RESTART_INTERVAL = 10
GIT_CLONE_TIMEOUT = 120

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# ---------- डेटाबेस ----------
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
            owner_id INTEGER NOT NULL,
            website_name TEXT,
            website_slug TEXT UNIQUE NOT NULL,
            default_domain TEXT,
            custom_domain TEXT,
            website_folder TEXT NOT NULL,
            startup_file TEXT,
            python_version TEXT DEFAULT '3',
            status TEXT DEFAULT 'uploaded',
            allocated_port INTEGER UNIQUE,
            pid INTEGER,
            env_vars TEXT,
            repo_url TEXT,
            repo_branch TEXT DEFAULT 'main',
            build_log_file TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMP,
            last_started TIMESTAMP,
            last_stopped TIMESTAMP,
            storage_used INTEGER DEFAULT 0,
            website_size INTEGER DEFAULT 0,
            ssl_enabled INTEGER DEFAULT 0,
            restart_count INTEGER DEFAULT 0,
            crash_count INTEGER DEFAULT 0,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')
        
        conn.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')
        
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_website ON logs(website_id)')
        conn.commit()
        
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         ('admin', 'admin@hosting.com', generate_password_hash('admin123'), 'admin', 'pro'))
            conn.commit()
            print("✅ Default admin: admin / admin123 (Pro plan)")

init_db()

# ---------- हेल्पर ----------
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

def get_websites_by_user(user_id):
    with get_db() as conn:
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

def validate_zip(zip_path):
    if not os.path.exists(zip_path):
        return False, "ZIP file not found"
    if os.path.getsize(zip_path) == 0:
        return False, "ZIP file is empty"
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    return False, "ZIP is password protected"
            if len(zf.namelist()) == 0:
                return False, "ZIP is empty"
    except zipfile.BadZipFile:
        return False, "ZIP is corrupted"
    except Exception as e:
        return False, f"ZIP validation failed: {str(e)}"
    return True, "OK"

def rollback_upload(website_id, folder):
    shutil.rmtree(folder, ignore_errors=True)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.commit()

def extract_zip(zip_path, extract_to):
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(extract_to)
        return True, "Extracted"
    except Exception as e:
        return False, f"Extraction failed: {str(e)}"

def find_startup_file(folder):
    for filename in STARTUP_PRIORITY:
        if os.path.exists(os.path.join(folder, filename)):
            return filename, 'python'
    for f in os.listdir(folder):
        if f.endswith('.py') and f != '__init__.py':
            return f, 'python'
    if os.path.exists(os.path.join(folder, 'package.json')):
        return 'package.json', 'node'
    if os.path.exists(os.path.join(folder, 'index.html')):
        return 'index.html', 'static'
    for f in os.listdir(folder):
        if f.endswith('.html'):
            return f, 'static'
    return None, None

def load_env_file(folder):
    env_path = os.path.join(folder, '.env')
    env_dict = {}
    if os.path.exists(env_path):
        try:
            with open(env_path, 'r', encoding='utf-8') as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith('#'):
                        continue
                    if '=' in line:
                        key, value = line.split('=', 1)
                        env_dict[key.strip()] = value.strip()
        except:
            pass
    return env_dict

def install_requirements(folder, website_id, log_callback=None):
    req_file = os.path.join(folder, 'requirements.txt')
    if not os.path.exists(req_file):
        return True, "No requirements.txt", []
    
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}_install.log")
    logs = []
    def log(msg):
        logs.append(msg)
        if log_callback:
            log_callback(msg)
    
    log(f"📦 Installing requirements from {req_file} ...")
    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt']
        with open(log_file, 'w') as f:
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                logs.append(line)
                f.write(line)
                if log_callback:
                    log_callback(line)
            proc.wait()
        if proc.returncode != 0:
            error = ''.join(logs[-500:])
            log(f"❌ Installation failed: {error}")
            return False, f"Installation failed: {error}", logs
        log("✅ Installation successful")
        return True, "Installation successful", logs
    except Exception as e:
        error_msg = str(e)
        log(f"❌ Installation error: {error_msg}")
        return False, f"Installation error: {error_msg}", [error_msg]

def install_node_modules(folder, website_id, log_callback=None):
    if not os.path.exists(os.path.join(folder, 'package.json')):
        return True, "No package.json", []
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}_install.log")
    logs = []
    def log(msg):
        logs.append(msg)
        if log_callback:
            log_callback(msg)
    log(f"📦 Installing npm packages ...")
    try:
        cmd = ['npm', 'install']
        with open(log_file, 'a') as f:
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                logs.append(line)
                f.write(line)
                if log_callback:
                    log_callback(line)
            proc.wait()
        if proc.returncode != 0:
            error = ''.join(logs[-500:])
            log(f"❌ npm install failed: {error}")
            return False, f"npm install failed: {error}", logs
        log("✅ npm install successful")
        return True, "npm install successful", logs
    except Exception as e:
        error_msg = str(e)
        log(f"❌ npm install error: {error_msg}")
        return False, f"npm install error: {error_msg}", [error_msg]

def health_check(port, timeout=5):
    try:
        response = requests.get(f"http://localhost:{port}", timeout=timeout)
        if response.status_code < 500:
            return True, "OK"
        else:
            return False, f"HTTP {response.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "Connection refused"
    except requests.exceptions.Timeout:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)

def start_website_process(website_id, log_callback=None):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found", []
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        if log_callback: log_callback("❌ Folder missing")
        return False, "Folder not found", []
    
    startup_file_from_db = website['startup_file']
    startup_file = None
    runtime = None
    if startup_file_from_db:
        full_path = os.path.join(folder, startup_file_from_db)
        if os.path.exists(full_path) and os.path.isfile(full_path):
            startup_file = startup_file_from_db
            if startup_file.endswith('.py'):
                runtime = 'python'
            elif startup_file.endswith('.html'):
                runtime = 'static'
            elif startup_file == 'package.json':
                runtime = 'node'
            else:
                startup_file, runtime = find_startup_file(folder)
        else:
            startup_file, runtime = find_startup_file(folder)
    else:
        startup_file, runtime = find_startup_file(folder)
    if not startup_file:
        if log_callback: log_callback("❌ No startup file found")
        return False, "No startup file detected.", []
    
    log_lines = []
    def log(msg):
        log_lines.append(msg)
        if log_callback:
            log_callback(msg)
    
    # --- Install dependencies if needed ---
    if runtime == 'python':
        # Check if requirements.txt exists and install
        req_path = os.path.join(folder, 'requirements.txt')
        if os.path.exists(req_path):
            success, msg, logs = install_requirements(folder, website_id, log_callback)
            log_lines.extend(logs)
            if not success:
                update_website_status(website_id, 'failed')
                log_website(website_id, f"Installation failed: {msg}", 'error')
                return False, msg, log_lines
            else:
                log_website(website_id, "Installation successful")
    elif runtime == 'node':
        success, msg, logs = install_node_modules(folder, website_id, log_callback)
        log_lines.extend(logs)
        if not success:
            update_website_status(website_id, 'failed')
            log_website(website_id, f"npm install failed: {msg}", 'error')
            return False, msg, log_lines
    
    # Save build log
    build_log_file = os.path.join(LOG_FOLDER, f"website_{website_id}_build.log")
    with open(build_log_file, 'w') as f:
        f.write('\n'.join(log_lines))
    with get_db() as conn:
        conn.execute('UPDATE websites SET build_log_file = ? WHERE id = ?', (build_log_file, website_id))
        conn.commit()
    
    port = get_next_available_port()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    
    env = os.environ.copy()
    env['PORT'] = str(port)
    env['PYTHONUNBUFFERED'] = '1'
    dotenv_vars = load_env_file(folder)
    env.update(dotenv_vars)
    if website['env_vars']:
        try:
            extra_env = json.loads(website['env_vars'])
            env.update(extra_env)
        except:
            pass
    
    if runtime == 'python':
        cmd = [sys.executable, startup_file]
    elif runtime == 'node':
        cmd = ['npm', 'start']
    else:
        cmd = [sys.executable, '-m', 'http.server', str(port)]
    
    log(f"🚀 Starting with command: {' '.join(cmd)}")
    try:
        if os.name == 'nt':
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=open(log_file, 'a'), stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=open(log_file, 'a'), stderr=subprocess.STDOUT,
                                    preexec_fn=os.setsid)
        time.sleep(3)
        healthy, health_msg = health_check(port, timeout=5)
        if healthy:
            update_website_status(website_id, 'running', proc.pid, port)
            log(f"✅ Server running on port {port} (PID {proc.pid})")
            return True, f"Running on port {port}", log_lines
        else:
            update_website_status(website_id, 'running', proc.pid, port)
            log(f"⚠️ Health check failed: {health_msg}. Check logs.")
            return True, f"Running (health check failed: {health_msg})", log_lines
    except Exception as e:
        log(f"❌ Start error: {str(e)}")
        return False, str(e), log_lines

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
    log_website(website_id, f"Stopped (PID {pid})")
    return True, "Stopped"

def clone_github_repo(repo_url, branch, target_folder, log_callback=None):
    logs = []
    def log(msg):
        logs.append(msg)
        if log_callback:
            log_callback(msg)
    safe_url = repo_url.replace('https://', '').replace('http://', '')
    log(f"Cloning {safe_url} (branch: {branch})")
    cmd = ['git', 'clone', '--branch', branch, '--depth', '1', repo_url, target_folder]
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            line = line.strip()
            if line:
                log(line)
        proc.wait(timeout=GIT_CLONE_TIMEOUT)
        if proc.returncode != 0:
            error = ''.join(logs[-100:])
            return False, f"Git clone failed: {error}", logs
        log("✅ Clone successful")
        return True, "Clone successful", logs
    except subprocess.TimeoutExpired:
        proc.kill()
        return False, "Clone timeout", logs
    except Exception as e:
        return False, f"Clone error: {str(e)}", logs

# ---------- Auto-Restart Monitor ----------
def monitor_websites():
    while True:
        try:
            with get_db() as conn:
                websites = conn.execute('SELECT * FROM websites WHERE status = "running"').fetchall()
                for w in websites:
                    pid = w['pid']
                    if pid:
                        try:
                            os.kill(pid, 0)
                        except OSError:
                            update_website_status(w['id'], 'crashed', None, None)
                            log_website(w['id'], "Auto-detected crash", 'error')
                            if w['crash_count'] < AUTO_RESTART_MAX:
                                with get_db() as conn2:
                                    conn2.execute('UPDATE websites SET crash_count = crash_count + 1 WHERE id = ?', (w['id'],))
                                    conn2.commit()
                                log_website(w['id'], f"Auto-restarting (attempt {w['crash_count']+1})", 'info')
                                start_website_process(w['id'])
                            else:
                                log_website(w['id'], f"Auto-restart limit reached ({AUTO_RESTART_MAX})", 'error')
        except:
            pass
        time.sleep(AUTO_RESTART_INTERVAL)

monitor_thread = threading.Thread(target=monitor_websites, daemon=True)
monitor_thread.start()

# ============================================================
# ⚠️ सारे स्पेसिफिक रूट्स पहले – प्रॉक्सी सबसे नीचे
# ============================================================

@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/api/login', methods=['POST'])
def api_login():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    user = get_user_by_username(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return jsonify({'error': 'Invalid credentials'}), 401
    if user['status'] != 'active':
        return jsonify({'error': 'Account disabled'}), 403
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['plan'] = user['plan']
    return jsonify({
        'success': True,
        'username': user['username'],
        'role': user['role']
    })

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
        return render_template_string(REGISTER_TEMPLATE, error='Username already taken')
    with get_db() as conn:
        try:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         (username, email, generate_password_hash(password), 'owner', 'free'))
            conn.commit()
        except sqlite3.IntegrityError:
            return render_template_string(REGISTER_TEMPLATE, error='Email or username already exists')
    log_activity(None, 'register', f'User {username} registered')
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
        return render_template_string(LOGIN_TEMPLATE, error='Account is disabled')
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['plan'] = user['plan']
    with get_db() as conn:
        conn.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (user['id'],))
        conn.commit()
    log_activity(user['id'], 'login', 'User logged in')
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_activity(session['user_id'], 'logout', 'User logged out')
    session.clear()
    return redirect(url_for('index'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('index'))
    rows = get_websites_by_user(session['user_id'])
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    websites = []
    for w in rows:
        site = dict(w)
        site['url'] = f"{base_url}/{site['website_slug']}/"
        websites.append(site)
    user = get_user_by_id(session['user_id'])
    return render_template_string(DASHBOARD_TEMPLATE, 
                                  user=session['username'], 
                                  websites=websites, 
                                  role=session.get('role', 'owner'),
                                  plan=session.get('plan', 'free'),
                                  base_url=base_url,
                                  user_obj=user)

# ---------- अपलोड – Multiple Files ----------
@app.route('/upload', methods=['POST'])
def upload_website():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    if user['status'] != 'active':
        return jsonify({'success': False, 'error': 'Account disabled'}), 403
    
    # फाइलें लें (चाहे एक हो या कई)
    files = request.files.getlist('files[]')
    if not files or len(files) == 0:
        # fallback for single file (old method)
        if 'file' in request.files:
            files = [request.files['file']]
        else:
            return jsonify({'success': False, 'error': 'No files uploaded'}), 400
    
    # पहली फ़ाइल का नाम चेक करें (ZIP या not)
    # हम एक फ़ाइल को ZIP मानेंगे अगर वह .zip हो, बाकी को single files
    # हम पहली ZIP को प्राथमिकता देंगे (अगर कई हैं तो पहली ZIP extract करें)
    zip_files = [f for f in files if f.filename.lower().endswith('.zip')]
    non_zip_files = [f for f in files if not f.filename.lower().endswith('.zip')]
    
    # Generate slug
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (user_id,)).fetchone()[0]
    slug = generate_website_slug(session['username'], count)
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ?', (slug,)).fetchone():
            count += 1
            slug = generate_website_slug(session['username'], count)
    
    # Create website record
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO websites (owner_id, website_slug, website_folder, status)
                              VALUES (?, ?, ?, ?)''',
                           (user_id, slug, f"website_{0}", 'uploaded'))
        website_id = cur.lastrowid
        conn.commit()
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    try:
        os.makedirs(folder, exist_ok=True)
    except PermissionError:
        rollback_upload(website_id, folder)
        return jsonify({'success': False, 'error': 'Permission denied'}), 500
    
    # Process ZIP files (extract first ZIP, ignore others? but we can extract all ZIPs into same folder)
    # We'll extract each ZIP into the folder, merging contents.
    for zf in zip_files:
        zip_path = os.path.join(folder, zf.filename)
        try:
            zf.save(zip_path)
        except Exception as e:
            rollback_upload(website_id, folder)
            return jsonify({'success': False, 'error': f'Failed to save ZIP: {str(e)}'}), 500
        
        valid, msg = validate_zip(zip_path)
        if not valid:
            rollback_upload(website_id, folder)
            return jsonify({'success': False, 'error': msg}), 400
        
        ok, msg = extract_zip(zip_path, folder)
        if not ok:
            rollback_upload(website_id, folder)
            return jsonify({'success': False, 'error': msg}), 400
        
        os.remove(zip_path)  # delete zip after extraction
    
    # Process non-ZIP files (single files)
    for f in non_zip_files:
        file_path = os.path.join(folder, f.filename)
        try:
            f.save(file_path)
        except Exception as e:
            rollback_upload(website_id, folder)
            return jsonify({'success': False, 'error': f'Failed to save file: {str(e)}'}), 500
    
    # Determine startup file: if any non-zip file is a Python file, we may set startup_file
    # Otherwise, detection will happen on start.
    startup_file = None
    website_name = None
    # Try to find a .py file among the uploaded files (non-zip)
    for f in non_zip_files:
        if f.filename.endswith('.py') and not f.filename.startswith('.'):
            startup_file = f.filename
            website_name = f.filename
            break
    # If no .py found, maybe there's an index.html
    if not startup_file:
        for f in non_zip_files:
            if f.filename == 'index.html':
                startup_file = f.filename
                website_name = f.filename
                break
    
    size_used = calculate_folder_size(folder)
    
    with get_db() as conn:
        conn.execute('''UPDATE websites SET 
                        website_name = ?, 
                        website_folder = ?,
                        storage_used = ?,
                        website_size = ?,
                        startup_file = ?,
                        status = 'uploaded',
                        updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?''',
                     (website_name or 'Website',
                      f"website_{website_id}", size_used, size_used, startup_file, website_id))
        conn.commit()
    
    log_website(website_id, f"Uploaded {len(files)} file(s)")
    log_activity(user_id, 'upload', f'Uploaded {len(files)} files', request.remote_addr)
    
    # Auto-start
    update_website_status(website_id, 'starting')
    ok, msg, logs = start_website_process(website_id)
    if ok:
        log_website(website_id, f"Auto-started successfully: {msg}")
    else:
        log_website(website_id, f"Auto-start failed: {msg}", 'error')
    
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug, 'auto_started': ok})

# ---------- GitHub Deploy ----------
@app.route('/deploy_github/stream', methods=['POST'])
def deploy_github_stream():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid JSON'}), 400
    repo_url = data.get('repo_url', '').strip()
    branch = data.get('branch', 'main').strip()
    if not repo_url:
        return jsonify({'error': 'Repo URL required'}), 400
    if not repo_url.endswith('.git'):
        repo_url += '.git'
    user_id = session['user_id']
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (user_id,)).fetchone()[0]
    slug = generate_website_slug(session['username'], count)
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ?', (slug,)).fetchone():
            count += 1
            slug = generate_website_slug(session['username'], count)
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO websites (owner_id, website_slug, website_folder, status, repo_url, repo_branch)
                              VALUES (?, ?, ?, ?, ?, ?)''',
                           (user_id, slug, f"website_{0}", 'cloning', repo_url, branch))
        website_id = cur.lastrowid
        conn.commit()
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    os.makedirs(folder, exist_ok=True)
    log_queue = queue.Queue()
    def log_callback(msg):
        log_queue.put(('log', msg))
    def do_work():
        success, msg, logs = clone_github_repo(repo_url, branch, folder, log_callback)
        if not success:
            log_queue.put(('error', msg))
            shutil.rmtree(folder, ignore_errors=True)
            with get_db() as conn:
                conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
                conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
                conn.commit()
            return
        def start_callback(msg):
            log_queue.put(('build', msg))
        update_website_status(website_id, 'starting')
        ok, start_msg, start_logs = start_website_process(website_id, start_callback)
        if ok:
            log_queue.put(('done', {'website_id': website_id, 'slug': slug}))
        else:
            log_queue.put(('error', start_msg))
    thread = threading.Thread(target=do_work, daemon=True)
    thread.start()
    def generate():
        while True:
            try:
                item = log_queue.get(timeout=1)
                typ, data = item
                if typ == 'log':
                    yield f"data: {json.dumps({'type': 'log', 'message': data})}\n\n"
                elif typ == 'build':
                    yield f"data: {json.dumps({'type': 'build', 'message': data})}\n\n"
                elif typ == 'error':
                    yield f"data: {json.dumps({'type': 'error', 'message': data})}\n\n"
                elif typ == 'done':
                    yield f"data: {json.dumps({'type': 'done', **data})}\n\n"
                    break
            except queue.Empty:
                if not thread.is_alive():
                    break
                continue
            except GeneratorExit:
                break
    return Response(generate(), mimetype='text/event-stream')

# ---------- वेबसाइट मैनेजमेंट API ----------
@app.route('/website/<int:website_id>/start', methods=['POST'])
def start_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] in ['running', 'starting']:
        return jsonify({'success': False, 'error': 'Already running'}), 400
    update_website_status(website_id, 'starting')
    ok, msg, logs = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/website/<int:website_id>/stop', methods=['POST'])
def stop_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] not in ['running', 'starting']:
        return jsonify({'success': False, 'error': 'Not running'}), 400
    ok, msg = stop_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/website/<int:website_id>/restart', methods=['POST'])
def restart_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    with get_db() as conn:
        conn.execute('UPDATE websites SET restart_count = restart_count + 1 WHERE id = ?', (website_id,))
        conn.commit()
    if website['status'] == 'running':
        stop_website_process(website_id)
    update_website_status(website_id, 'starting')
    ok, msg, logs = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/website/<int:website_id>/delete', methods=['POST'])
def delete_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] in ['running', 'starting']:
        stop_website_process(website_id)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    shutil.rmtree(folder, ignore_errors=True)
    for f in [f"website_{website_id}.log", f"website_{website_id}_install.log", f"website_{website_id}_build.log"]:
        fp = os.path.join(LOG_FOLDER, f)
        if os.path.exists(fp):
            os.remove(fp)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.commit()
    log_activity(session['user_id'], 'delete', f'Deleted website {website_id}', request.remote_addr)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/files')
def files(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            abort(404)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        abort(404)
    items = []
    for root, dirs, files in os.walk(folder):
        rel = os.path.relpath(root, folder)
        if rel == '.':
            rel = ''
        for f in files:
            items.append({'name': f, 'path': os.path.join(rel, f).replace('\\', '/'), 'is_dir': False})
        for d in dirs:
            items.append({'name': d, 'path': os.path.join(rel, d).replace('\\', '/'), 'is_dir': True})
    return render_template_string(FILES_TEMPLATE, website=website, items=items)

@app.route('/website/<int:website_id>/edit', methods=['GET', 'POST'])
def edit_file(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            abort(404)
    file_path = request.args.get('path', '').strip()
    if not file_path:
        return "No file path", 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", file_path)
    if not os.path.exists(full) or not os.path.isfile(full):
        abort(404)
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in {'.py', '.html', '.css', '.js', '.txt', '.json', '.md', '.yml', '.yaml', '.sh', '.bat', '.xml', '.conf'}:
        return "Cannot edit binary files", 403
    if request.method == 'GET':
        with open(full, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return render_template_string(EDIT_TEMPLATE, website=website, file_path=file_path, content=content)
    else:
        new_content = request.form.get('content', '')
        with open(full, 'w', encoding='utf-8') as f:
            f.write(new_content)
        log_website(website_id, f"Edited: {file_path}")
        return redirect(url_for('files', website_id=website_id))

@app.route('/website/<int:website_id>/logs')
def view_logs(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            abort(404)
    with get_db() as conn:
        logs = conn.execute('SELECT * FROM logs WHERE website_id = ? ORDER BY timestamp DESC LIMIT 200', (website_id,)).fetchall()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    file_log = ''
    if os.path.exists(log_file):
        with open(log_file, 'r', errors='ignore') as f:
            file_log = f.read()
    install_log = ''
    install_log_file = os.path.join(LOG_FOLDER, f"website_{website_id}_install.log")
    if os.path.exists(install_log_file):
        with open(install_log_file, 'r', errors='ignore') as f:
            install_log = f.read()
    build_log = ''
    build_log_file = website['build_log_file']
    if build_log_file and os.path.exists(build_log_file):
        with open(build_log_file, 'r', errors='ignore') as f:
            build_log = f.read()
    return render_template_string(LOGS_TEMPLATE, website=website, logs=logs, file_log=file_log, install_log=install_log, build_log=build_log)

@app.route('/website/<int:website_id>/change_url', methods=['POST'])
def change_subdomain(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    new_slug = request.form.get('slug', '').strip()
    if not new_slug or not re.match(r'^[a-zA-Z0-9\-]+$', new_slug):
        return jsonify({'success': False, 'error': 'Invalid slug'}), 400
    with get_db() as conn:
        existing = conn.execute('SELECT id FROM websites WHERE website_slug = ? AND id != ?', (new_slug, website_id)).fetchone()
        if existing:
            base = new_slug
            suggestions = []
            for i in range(1, 4):
                sugg = f"{base}{i}"
                if not conn.execute('SELECT id FROM websites WHERE website_slug = ?', (sugg,)).fetchone():
                    suggestions.append(sugg)
            return jsonify({
                'success': False,
                'error': f'Slug "{new_slug}" is already taken.',
                'suggestions': suggestions
            }), 400
        conn.execute('UPDATE websites SET website_slug = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (new_slug, website_id))
        conn.commit()
    log_website(website_id, f"Changed slug to {new_slug}")
    return jsonify({'success': True, 'new_slug': new_slug})

@app.route('/website/<int:website_id>/env', methods=['GET', 'POST'])
def env_vars(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            abort(404)
    if request.method == 'GET':
        env_dict = {}
        if website['env_vars']:
            try:
                env_dict = json.loads(website['env_vars'])
            except:
                pass
        env_text = '\n'.join([f"{k}={v}" for k, v in env_dict.items()])
        return render_template_string(ENV_TEMPLATE, website=website, env_text=env_text)
    else:
        env_raw = request.form.get('env', '').strip()
        if not env_raw:
            with get_db() as conn:
                conn.execute('UPDATE websites SET env_vars = NULL, updated_at = CURRENT_TIMESTAMP WHERE id = ?', (website_id,))
                conn.commit()
            log_website(website_id, "Cleared environment variables")
            return jsonify({'success': True})
        env_dict = {}
        lines = env_raw.split('\n')
        for line in lines:
            line = line.strip()
            if not line or line.startswith('#'):
                continue
            if '=' not in line:
                return jsonify({'success': False, 'error': f'Invalid line: {line} (must contain =)'}), 400
            key, value = line.split('=', 1)
            key = key.strip()
            if not key:
                return jsonify({'success': False, 'error': 'Empty key not allowed'}), 400
            env_dict[key] = value.strip()
        with get_db() as conn:
            conn.execute('UPDATE websites SET env_vars = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (json.dumps(env_dict), website_id))
            conn.commit()
        log_website(website_id, f"Updated environment variables: {list(env_dict.keys())}")
        if website['status'] == 'running':
            stop_website_process(website_id)
            time.sleep(1)
            ok, msg, logs = start_website_process(website_id)
            if ok:
                return jsonify({'success': True, 'restarted': True})
            else:
                return jsonify({'success': False, 'error': f'Env saved but restart failed: {msg}'}), 500
        else:
            return jsonify({'success': True, 'restarted': False})

# ============================================================
# ⚠️ PROXY – सबसे नीचे
# ============================================================
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def proxy_website(slug, path):
    website = get_website_by_slug(slug)
    if not website:
        return render_template_string(ERROR_TEMPLATE, message="Website not found", slug=slug), 404
    if website['status'] != 'running':
        return render_template_string(ERROR_TEMPLATE, 
                                      message="This website is not running. Please start it from the dashboard.",
                                      slug=slug), 503
    port = website['allocated_port']
    if not port:
        return "Port not allocated", 500
    target_url = f"http://localhost:{port}/{path}"
    headers = {key: value for key, value in request.headers if key.lower() != 'host'}
    try:
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            stream=True,
            timeout=30
        )
        return Response(
            stream_with_context(resp.iter_content(chunk_size=8192)),
            status=resp.status_code,
            headers=resp.headers.items()
        )
    except requests.exceptions.ConnectionError:
        update_website_status(website['id'], 'crashed')
        log_website(website['id'], "Proxy connection failed - website crashed", 'error')
        return render_template_string(ERROR_TEMPLATE, 
                                      message="Website crashed. Please restart from dashboard.",
                                      slug=slug), 503
    except Exception as e:
        log_website(website['id'], f"Proxy error: {str(e)}", 'error')
        return f"Proxy error: {str(e)}", 500

# ---------- TEMPLATES ----------
# (All templates same as before - I'm including them for completeness)
ERROR_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Website Unavailable</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;display:flex;justify-content:center;align-items:center;height:100vh;overflow:hidden}
.glass{background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:30px;padding:50px;text-align:center;max-width:500px;box-shadow:0 0 80px rgba(0,229,255,0.05)}
h1{font-size:2.5rem;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:15px}
p{color:#aab;font-size:1.1rem;margin:15px 0}
a{color:#00e5ff;text-decoration:none;padding:12px 30px;border:2px solid #00e5ff;border-radius:50px;display:inline-block;margin-top:20px;transition:.3s}
a:hover{background:#00e5ff;color:#000;transform:scale(1.05)}
</style>
</head>
<body>
<div class="glass">
<h1>⚠️ {{ message }}</h1>
<p>Slug: <strong>{{ slug }}</strong></p>
<a href="/dashboard">← Go to Dashboard</a>
</div>
</body>
</html>
"""

LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Yuvicodex Host</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
@keyframes zoomIn{0%{opacity:0;transform:scale(0.9)}100%{opacity:1;transform:scale(1)}}
@keyframes float{0%{transform:translateY(0px)}50%{transform:translateY(-10px)}100%{transform:translateY(0px)}}
body{background:linear-gradient(135deg,#0a0e1a 0%,#1a1040 50%,#0a1a2a 100%);color:#fff;font-family:'Segoe UI',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.glass{background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:30px;padding:40px;width:100%;max-width:400px;animation:zoomIn 0.6s ease;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.logo{text-align:center;font-size:2.5rem;font-weight:900;background:linear-gradient(135deg,#00e5ff,#7a00ff,#00e5ff);background-size:300% 300%;-webkit-background-clip:text;-webkit-text-fill-color:transparent;animation:float 3s ease-in-out infinite;margin-bottom:10px}
.sub{text-align:center;color:#889;font-size:0.9rem;margin-bottom:30px}
input{width:100%;padding:14px 18px;margin:10px 0;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;font-size:1rem;outline:none;transition:.3s}
input:focus{border-color:#00e5ff;box-shadow:0 0 30px rgba(0,229,255,0.1)}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;border-radius:15px;color:#fff;font-size:1.1rem;font-weight:700;cursor:pointer;transition:.3s;margin-top:15px}
.btn:hover{transform:scale(1.02);box-shadow:0 0 40px rgba(0,229,255,0.2)}
.error{color:#ff4757;text-align:center;margin-top:10px;font-size:0.9rem}
.link{text-align:center;margin-top:20px;color:#889}
.link a{color:#00e5ff;text-decoration:none;font-weight:600}
.link a:hover{text-decoration:underline}
</style>
</head>
<body>
<div class="glass">
<div class="logo">🚀 Yuvicodex</div>
<div class="sub">Premium Cloud Hosting</div>
<form method="POST" action="/login">
<input type="text" name="username" placeholder="Username" required>
<input type="password" name="password" placeholder="Password" required>
<button class="btn" type="submit">Access Dashboard</button>
</form>
<div class="error">{{ error if error else '' }}</div>
<div class="link">New here? <a href="/register">Create Account</a></div>
</div>
</body>
</html>
"""

REGISTER_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Register - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
@keyframes zoomIn{0%{opacity:0;transform:scale(0.9)}100%{opacity:1;transform:scale(1)}}
body{background:linear-gradient(135deg,#0a0e1a 0%,#1a1040 50%,#0a1a2a 100%);color:#fff;font-family:'Segoe UI',sans-serif;display:flex;justify-content:center;align-items:center;min-height:100vh;padding:20px}
.glass{background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border:1px solid rgba(255,255,255,0.1);border-radius:30px;padding:40px;width:100%;max-width:400px;animation:zoomIn 0.6s ease;box-shadow:0 20px 60px rgba(0,0,0,0.5)}
.logo{text-align:center;font-size:2rem;font-weight:900;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent;margin-bottom:5px}
.sub{text-align:center;color:#889;font-size:0.9rem;margin-bottom:30px}
input{width:100%;padding:14px 18px;margin:10px 0;background:rgba(255,255,255,0.06);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;font-size:1rem;outline:none;transition:.3s}
input:focus{border-color:#00e5ff;box-shadow:0 0 30px rgba(0,229,255,0.1)}
.btn{width:100%;padding:14px;background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;border-radius:15px;color:#fff;font-size:1.1rem;font-weight:700;cursor:pointer;transition:.3s;margin-top:15px}
.btn:hover{transform:scale(1.02);box-shadow:0 0 40px rgba(0,229,255,0.2)}
.error{color:#ff4757;text-align:center;margin-top:10px;font-size:0.9rem}
.link{text-align:center;margin-top:20px;color:#889}
.link a{color:#00e5ff;text-decoration:none;font-weight:600}
</style>
</head>
<body>
<div class="glass">
<div class="logo">✨ Create Account</div>
<div class="sub">Start hosting in minutes</div>
<form method="POST" action="/register">
<input type="text" name="username" placeholder="Username" required>
<input type="email" name="email" placeholder="Email" required>
<input type="password" name="password" placeholder="Password" required>
<button class="btn" type="submit">Register</button>
</form>
<div class="error">{{ error if error else '' }}</div>
<div class="link">Already have account? <a href="/">Login</a></div>
</div>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Dashboard - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
@keyframes zoomIn{0%{opacity:0;transform:scale(0.95)}100%{opacity:1;transform:scale(1)}}
@keyframes glow{0%{box-shadow:0 0 20px rgba(0,229,255,0.1)}50%{box-shadow:0 0 40px rgba(0,229,255,0.2)}100%{box-shadow:0 0 20px rgba(0,229,255,0.1)}}
body{background:linear-gradient(135deg,#0a0e1a 0%,#0d1a2a 100%);color:#fff;font-family:'Segoe UI',sans-serif;padding:20px;min-height:100vh}
.container{max-width:1300px;margin:auto;animation:zoomIn 0.5s ease}
.header{display:flex;justify-content:space-between;align-items:center;padding:15px 25px;background:rgba(255,255,255,0.05);backdrop-filter:blur(20px);border-radius:20px;border:1px solid rgba(255,255,255,0.08);margin-bottom:30px}
.header h1{font-size:1.8rem;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.user-badge{display:flex;align-items:center;gap:15px}
.badge{background:rgba(0,229,255,0.15);padding:4px 14px;border-radius:50px;font-size:0.8rem;border:1px solid rgba(0,229,255,0.2)}
.btn-logout{color:#ff4757;text-decoration:none;font-weight:600;padding:8px 20px;border:1px solid #ff4757;border-radius:50px;transition:.3s}
.btn-logout:hover{background:#ff4757;color:#fff}
.upload-box{background:rgba(255,255,255,0.04);backdrop-filter:blur(10px);border:2px dashed rgba(255,255,255,0.1);border-radius:25px;padding:40px;text-align:center;margin-bottom:30px;transition:.3s}
.upload-box:hover{border-color:#00e5ff;animation:glow 2s ease-in-out infinite}
.upload-box h3{font-size:1.3rem;margin-bottom:15px;color:#ddd}
.upload-box input[type="file"]{margin:15px auto;display:block;color:#aaa;width:100%;padding:8px;background:rgba(255,255,255,0.03);border:1px solid rgba(255,255,255,0.1);border-radius:8px;cursor:pointer}
.upload-btn{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 40px;border-radius:50px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:.3s}
.upload-btn:hover{transform:scale(1.05);box-shadow:0 0 40px rgba(0,229,255,0.2)}
#uploadStatus{margin-top:15px;font-weight:500}
.github-box{background:rgba(255,255,255,0.04);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.08);border-radius:20px;padding:25px;margin-bottom:30px}
.github-box h3{color:#ddd;margin-bottom:15px}
.github-box input{width:100%;padding:10px 15px;margin:8px 0;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff;font-size:0.9rem;outline:none}
.github-box input:focus{border-color:#00e5ff}
.github-btn{background:linear-gradient(135deg,#f0f0f0,#ccc);border:none;padding:10px 30px;border-radius:50px;color:#000;font-weight:700;cursor:pointer;transition:.3s;margin-top:10px}
.github-btn:hover{transform:scale(1.05);box-shadow:0 0 30px rgba(255,255,255,0.1)}
.github-logs{background:#05070d;padding:10px;border-radius:10px;max-height:300px;overflow-y:auto;font-family:monospace;font-size:12px;color:#aab;display:none;white-space:pre-wrap;margin-top:10px;border:1px solid rgba(255,255,255,0.05)}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:25px;margin-top:20px}
.card{background:rgba(255,255,255,0.04);backdrop-filter:blur(10px);border:1px solid rgba(255,255,255,0.07);border-radius:20px;padding:25px;transition:.3s}
.card:hover{transform:translateY(-5px);border-color:rgba(0,229,255,0.2);box-shadow:0 20px 60px rgba(0,0,0,0.3)}
.card-title{font-size:1.2rem;font-weight:700;color:#fff}
.card-slug{color:#889;font-size:0.9rem;margin:5px 0}
.card-port{color:#889;font-size:0.8rem}
.status-badge{display:inline-block;padding:4px 14px;border-radius:50px;font-size:0.75rem;font-weight:600;margin:10px 0}
.status-running{background:rgba(0,229,255,0.15);color:#00e5ff;border:1px solid rgba(0,229,255,0.2)}
.status-stopped{background:rgba(255,71,87,0.15);color:#ff4757;border:1px solid rgba(255,71,87,0.2)}
.status-uploaded{background:rgba(255,170,0,0.15);color:#ffaa00;border:1px solid rgba(255,170,0,0.2)}
.status-cloning{background:rgba(255,170,0,0.15);color:#ffaa00;border:1px solid rgba(255,170,0,0.2)}
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
.url-edit{display:flex;gap:8px;margin-top:12px}
.url-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff;outline:none;font-size:0.85rem}
.url-edit input:focus{border-color:#00e5ff}
.url-edit button{padding:8px 16px;background:#00e5ff;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer;transition:.2s}
.url-edit button:hover{transform:scale(1.05)}
.plan-badge{background:linear-gradient(135deg,#7a00ff,#00e5ff);padding:2px 12px;border-radius:50px;font-size:0.7rem;font-weight:700}
@media(max-width:600px){.header{flex-direction:column;gap:10px;text-align:center}.grid{grid-template-columns:1fr}}
</style>
</head>
<body>
<div class="container">
<div class="header">
<h1>🚀 Yuvicodex Host</h1>
<div class="user-badge">
<span class="badge">{{ user }}</span>
<span class="plan-badge">{{ plan.upper() }}</span>
<a href="/logout" class="btn-logout">Logout</a>
</div>
</div>

<div class="upload-box">
<h3>📤 Upload Website (ZIP or Multiple Files)</h3>
<input type="file" id="zipFile" multiple>
<button class="upload-btn" id="uploadBtn">Upload & Deploy</button>
<div id="uploadStatus"></div>
</div>

<div class="github-box">
<h3>🐙 Deploy from GitHub</h3>
<input type="text" id="repoUrl" placeholder="https://github.com/username/repo.git">
<input type="text" id="repoBranch" placeholder="branch (default: main)" value="main">
<button class="github-btn" id="deployGitHubBtn">Deploy from GitHub</button>
<div id="githubStatus" style="margin-top:10px;"></div>
<div id="githubLogs" class="github-logs"></div>
</div>

<h2 style="margin-bottom:15px;">Your Websites</h2>
<div class="grid">
{% for w in websites %}
<div class="card">
<div class="card-title">{{ w.website_name or w.website_slug }}</div>
<div class="card-slug">🔗 {{ base_url }}/<strong>{{ w.website_slug }}</strong>/</div>
<div class="card-port">Port: {{ w.allocated_port or 'Not allocated' }}</div>
<div class="status-badge status-{{ w.status }}">{{ w.status.upper() }}</div>
<div class="card-meta">Created: {{ w.created_at[:10] }} | Size: {{ (w.website_size or 0)//1024 }} KB</div>
{% if w.status == 'running' %}
<a href="{{ w.url }}" target="_blank" class="visit-link">🌐 Visit Site</a>
{% else %}
<div style="color:#666;font-size:0.85rem;margin:10px 0;">⚪ Website not running</div>
{% endif %}
<div class="actions">
<button class="btn-start" onclick="action({{ w.id }},'start')">▶ Start</button>
<button class="btn-stop" onclick="action({{ w.id }},'stop')">■ Stop</button>
<button class="btn-restart" onclick="action({{ w.id }},'restart')">⟳ Restart</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/files'">📁 Files</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/logs'">📜 Logs</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/env'">🔑 Env</button>
<button class="btn-delete" onclick="if(confirm('Delete this website?')) action({{ w.id }},'delete')">🗑 Delete</button>
</div>
<div class="url-edit">
<input type="text" id="slug_input_{{ w.id }}" value="{{ w.website_slug }}" placeholder="new-slug">
<button onclick="changeSlug({{ w.id }})">Change</button>
</div>
</div>
{% endfor %}
</div>
</div>

<script>
function action(id,type){
fetch('/website/'+id+'/'+type,{method:'POST'})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}

function changeSlug(id){
const val=document.getElementById('slug_input_'+id).value.trim();
if(!val)return alert('Enter slug');
fetch('/website/'+id+'/change_url',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'slug='+encodeURIComponent(val)
})
.then(r=>r.json())
.then(d=>{
if(d.success){
location.reload();
}else{
if(d.suggestions && d.suggestions.length){
alert(d.error+'\\n\\nSuggestions:\\n'+d.suggestions.join('\\n'));
}else{
alert('Error: '+d.error);
}
}
})
.catch(()=>alert('Network error'));
}

document.getElementById('uploadBtn').onclick=function(){
const input=document.getElementById('zipFile');
const files=input.files;
if(!files || files.length===0)return alert('Select at least one file');
const fd=new FormData();
for(let i=0;i<files.length;i++){
fd.append('files[]', files[i]);
}
const st=document.getElementById('uploadStatus');
st.innerHTML='⏳ Uploading...';
fetch('/upload',{method:'POST',body:fd})
.then(r=>r.json())
.then(d=>{if(d.success){st.innerHTML='✅ Uploaded!';location.reload()}else st.innerHTML='❌ '+d.error})
.catch(()=>st.innerHTML='❌ Network error');
};

document.getElementById('deployGitHubBtn').onclick=function(){
const repo=document.getElementById('repoUrl').value.trim();
const branch=document.getElementById('repoBranch').value.trim() || 'main';
if(!repo)return alert('Enter GitHub repo URL');
const st=document.getElementById('githubStatus');
const logBox=document.getElementById('githubLogs');
logBox.style.display='block';
logBox.innerHTML='';
st.innerHTML='⏳ Deploying...';

fetch('/deploy_github/stream', {
method:'POST',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({repo_url:repo, branch:branch})
}).then(response => {
const reader = response.body.getReader();
const decoder = new TextDecoder();
function read() {
reader.read().then(({done, value}) => {
if (done) return;
const chunk = decoder.decode(value);
const lines = chunk.split('\\n');
for (let line of lines) {
if (line.startsWith('data: ')) {
try {
const data = JSON.parse(line.slice(6));
if (data.type === 'log' || data.type === 'build') {
logBox.innerHTML += data.message + '\\n';
logBox.scrollTop = logBox.scrollHeight;
} else if (data.type === 'done') {
st.innerHTML = '✅ Deployed! Website ID: ' + data.website_id;
setTimeout(() => location.reload(), 2000);
} else if (data.type === 'error') {
st.innerHTML = '❌ ' + data.message;
}
} catch(e) {}
}
}
read();
});
}
read();
}).catch(() => {
st.innerHTML = '❌ Network error';
});
};
</script>
</body>
</html>
"""

FILES_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Files - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:1000px;margin:auto}
.back{color:#00e5ff;text-decoration:none;font-weight:600}
h2{margin:20px 0;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
ul{list-style:none;padding:0}
li{display:flex;justify-content:space-between;padding:12px 15px;border-bottom:1px solid rgba(255,255,255,0.05);border-radius:10px;transition:.2s}
li:hover{background:rgba(255,255,255,0.03)}
a{color:#00e5ff;text-decoration:none}
.edit-link{color:#ffaa00;font-size:0.8rem}
</style>
</head>
<body>
<div class="container">
<a href="/dashboard" class="back">← Dashboard</a>
<h2>📁 {{ website.website_name or website.website_slug }}</h2>
<ul>
{% for item in items %}
<li>
<span>{% if item.is_dir %}📁 {% else %}📄 {% endif %}<a href="?path={{ item.path }}">{{ item.name }}</a></span>
<span>{% if not item.is_dir %}<a href="/website/{{ website.id }}/edit?path={{ item.path }}" class="edit-link">✏️ Edit</a>{% endif %}</span>
</li>
{% endfor %}
</ul>
</div>
</body>
</html>
"""

EDIT_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Edit - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:900px;margin:auto}
.back{color:#00e5ff;text-decoration:none;font-weight:600}
h2{margin:20px 0;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
textarea{width:100%;height:400px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;padding:15px;font-family:'Courier New',monospace;font-size:14px;outline:none}
textarea:focus{border-color:#00e5ff}
.btns{display:flex;gap:12px;margin-top:15px}
.save{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 30px;border-radius:50px;color:#fff;font-weight:700;cursor:pointer;transition:.3s}
.save:hover{transform:scale(1.05)}
.cancel{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);padding:12px 30px;border-radius:50px;color:#aaa;text-decoration:none;transition:.3s}
.cancel:hover{background:rgba(255,255,255,0.15);color:#fff}
</style>
</head>
<body>
<div class="container">
<a href="/website/{{ website.id }}/files" class="back">← Back</a>
<h2>✏️ {{ file_path }}</h2>
<form method="POST">
<textarea name="content">{{ content }}</textarea>
<div class="btns">
<button class="save" type="submit">💾 Save</button>
<a href="/website/{{ website.id }}/files" class="cancel">Cancel</a>
</div>
</form>
</div>
</body>
</html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Logs - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:1000px;margin:auto}
.back{color:#00e5ff;text-decoration:none;font-weight:600}
h2{margin:20px 0;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
h3{color:#889;margin:20px 0 10px;font-weight:400}
pre{background:rgba(0,0,0,0.4);padding:15px;border-radius:15px;max-height:300px;overflow-y:auto;border:1px solid rgba(255,255,255,0.05);font-family:'Courier New',monospace;font-size:12px;white-space:pre-wrap;color:#aab}
.log-entry{padding:3px 0;border-bottom:1px solid rgba(255,255,255,0.03)}
.timestamp{color:#666;margin-right:10px}
</style>
</head>
<body>
<div class="container">
<a href="/dashboard" class="back">← Dashboard</a>
<h2>📜 Logs for {{ website.website_name or website.website_slug }}</h2>

<h3>📋 Database Logs</h3>
<pre>
{% for log in logs %}
<span class="timestamp">{{ log.timestamp }}</span>[{{ log.log_type.upper() }}] {{ log.log_text }}
{% else %}
No logs yet.
{% endfor %}
</pre>

<h3>🖥️ Process Output</h3>
<pre>{{ file_log if file_log else 'No output file.' }}</pre>

<h3>📦 Installation Log</h3>
<pre>{{ install_log if install_log else 'No installation log.' }}</pre>

<h3>📋 Build Log (GitHub Deploy)</h3>
<pre>{{ build_log if build_log else 'No build log available.' }}</pre>
</div>
</body>
</html>
"""

ENV_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Environment Variables</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:800px;margin:auto}
.back{color:#00e5ff;text-decoration:none;font-weight:600}
h2{margin:20px 0;background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
textarea{width:100%;height:200px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;padding:15px;font-family:monospace;outline:none;font-size:14px}
textarea:focus{border-color:#00e5ff}
.btns{display:flex;gap:12px;margin-top:15px;flex-wrap:wrap}
.save{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 30px;border-radius:50px;color:#fff;font-weight:700;cursor:pointer}
.save:hover{transform:scale(1.05)}
.cancel{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);padding:12px 30px;border-radius:50px;color:#aaa;text-decoration:none}
.note{color:#888;font-size:0.8rem;margin-top:10px;border-top:1px solid rgba(255,255,255,0.05);padding-top:10px}
</style>
</head>
<body>
<div class="container">
<a href="/dashboard" class="back">← Dashboard</a>
<h2>🔑 Environment Variables – {{ website.website_name or website.website_slug }}</h2>
<p style="color:#889;margin-bottom:10px;">Enter each variable on a new line: <code>KEY=VALUE</code></p>
<form id="envForm">
<textarea id="envText" name="env" rows="8">{{ env_text }}</textarea>
<div class="btns">
<button type="submit" class="save">💾 Save &amp; Restart (if running)</button>
<a href="/dashboard" class="cancel">Cancel</a>
</div>
</form>
<div id="msg" style="margin-top:10px;"></div>
<div class="note">💡 Changes will take effect immediately by restarting the website.</div>
</div>
<script>
document.getElementById('envForm').onsubmit = function(e){
e.preventDefault();
const val = document.getElementById('envText').value.trim();
fetch(window.location.href, {
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'env='+encodeURIComponent(val)
})
.then(r=>r.json())
.then(d=>{
if(d.success){
document.getElementById('msg').innerHTML='✅ Saved and restarted!';
setTimeout(()=>location.reload(), 1500);
} else {
document.getElementById('msg').innerHTML='❌ '+d.error;
}
})
.catch(()=>document.getElementById('msg').innerHTML='❌ Network error');
};
</script>
</body>
</html>
"""

# ---------- सर्वर स्टार्ट ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)