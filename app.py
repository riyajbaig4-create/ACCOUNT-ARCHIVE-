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
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, abort, Response, stream_with_context, send_file
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-in-production'

# ---------- Configuration ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py']

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOG_FOLDER, exist_ok=True)

# ---------- Database ----------
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
        conn.execute('''CREATE TABLE IF NOT EXISTS deployments (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            repo_url TEXT,
            branch TEXT,
            status TEXT DEFAULT 'queued',
            started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            completed_at TIMESTAMP,
            duration INTEGER,
            log_file TEXT,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')
        conn.commit()
        if conn.execute('SELECT COUNT(*) FROM users').fetchone()[0] == 0:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         ('admin', 'admin@hosting.com', generate_password_hash('admin123'), 'admin', 'pro'))
            conn.commit()
            print("✅ Default admin: admin / admin123")
init_db()

# ---------- Helpers ----------
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

# ---------- Validation & Extraction ----------
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

# ---------- Runtime Detection ----------
def find_startup_file(folder):
    for name in STARTUP_PRIORITY:
        if os.path.exists(os.path.join(folder, name)):
            return name
    return None

def detect_runtime_and_get_cmd(folder, port):
    # Node.js
    if os.path.exists(os.path.join(folder, 'package.json')):
        try:
            with open(os.path.join(folder, 'package.json'), 'r') as f:
                data = json.load(f)
                scripts = data.get('scripts', {})
                if 'start' in scripts:
                    return ['npm', 'start'], 'nodejs', {'PORT': str(port)}
        except:
            pass
    js_files = ['server.js', 'index.js', 'app.js', 'main.js']
    for fname in js_files:
        if os.path.exists(os.path.join(folder, fname)):
            return ['node', fname], 'nodejs', {'PORT': str(port)}
    # PHP
    if os.path.exists(os.path.join(folder, 'index.php')):
        return ['php', '-S', f'0.0.0.0:{port}'], 'php', {}
    # Python Flask/Django
    flask_files = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
    for f in flask_files:
        path = os.path.join(folder, f)
        if os.path.exists(path):
            try:
                with open(path, 'r') as fh:
                    if 'Flask' in fh.read():
                        return [sys.executable, '-m', 'flask', 'run', '--host=0.0.0.0', '--port='+str(port)], 'flask', {}
            except:
                pass
    if os.path.exists(os.path.join(folder, 'manage.py')):
        return [sys.executable, 'manage.py', 'runserver', f'0.0.0.0:{port}'], 'django', {}
    startup = find_startup_file(folder)
    if startup:
        return [sys.executable, startup], 'python', {}
    if os.path.exists(os.path.join(folder, 'index.html')):
        return [sys.executable, '-m', 'http.server', str(port)], 'static', {}
    return None, None, {}

# ---------- Install Dependencies ----------
def install_dependencies(folder, runtime, log_callback=None):
    if runtime == 'nodejs' and os.path.exists(os.path.join(folder, 'package.json')):
        if log_callback:
            log_callback('BUILD', 'Running npm install')
        proc = subprocess.Popen(['npm', 'install'], cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            if log_callback:
                log_callback('PIP', line.strip())
        proc.wait()
        if proc.returncode != 0:
            return False, "npm install failed"
        return True, "npm install done"
    elif runtime == 'python' and os.path.exists(os.path.join(folder, 'requirements.txt')):
        if log_callback:
            log_callback('BUILD', 'Installing requirements')
        proc = subprocess.Popen([sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt'],
                                cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        for line in proc.stdout:
            if log_callback:
                log_callback('PIP', line.strip())
        proc.wait()
        if proc.returncode != 0:
            return False, "pip install failed"
        return True, "requirements installed"
    return True, "No dependencies"

# ---------- Start Process ----------
def start_website_process(website_id, log_callback=None):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
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
    if log_callback:
        log_callback("BUILD", f"Installing dependencies for {runtime}...")
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
        if os.path.exists(os.path.join(folder, 'app.py')):
            env['FLASK_APP'] = 'app.py'
        elif os.path.exists(os.path.join(folder, 'main.py')):
            env['FLASK_APP'] = 'main.py'
    if runtime == 'php':
        cmd = ['php', '-S', f'0.0.0.0:{allocated_port}']
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    if log_callback:
        log_callback("STARTUP", f"Starting: {' '.join(cmd)} on port {allocated_port}")
    try:
        f_log = open(log_file, 'a')
        if os.name == 'nt':
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    preexec_fn=os.setsid)
        def read_output():
            for line in iter(proc.stdout.readline, b''):
                if line:
                    decoded = line.decode('utf-8', errors='replace')
                    f_log.write(decoded)
                    f_log.flush()
                    if log_callback:
                        log_callback("PROCESS", decoded.strip())
            f_log.close()
        thread = threading.Thread(target=read_output)
        thread.daemon = True
        thread.start()
        time.sleep(3)
        if proc.poll() is not None:
            with open(log_file, 'r') as f:
                error_lines = f.read()[-500:]
            update_website_status(website_id, 'failed')
            log_website(website_id, f"Process crashed: {error_lines}", 'error')
            if log_callback:
                log_callback("ERROR", f"Process crashed: {error_lines}")
            return False, f"Process crashed: {error_lines}"
        # Health check (simplified – just check if process is alive)
        time.sleep(2)  # give it more time
        if proc.poll() is None:
            update_website_status(website_id, 'running', proc.pid, allocated_port)
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ?, last_started = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (cmd[0] if not runtime.startswith('python') else 'app', website_id))
                conn.commit()
            if log_callback:
                log_callback("SUCCESS", f"Application running on port {allocated_port}")
            return True, f"Running on port {allocated_port}"
        else:
            update_website_status(website_id, 'failed')
            return False, "Process died after start"
    except Exception as e:
        log_website(website_id, f"Start error: {str(e)}", 'error')
        update_website_status(website_id, 'failed')
        if log_callback:
            log_callback("ERROR", f"Start error: {str(e)}")
        return False, str(e)

# ---------- Stop Process ----------
def stop_website_process(website_id):
    website = get_website_by_id(website_id)
    if not website or not website['pid']:
        return False, "Not running"
    pid = website['pid']
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

# ---------- Deployment ----------
def write_log_step(log_file, step, message):
    ts = datetime.now().strftime("%H:%M:%S")
    line = f"[{ts}] [{step}] {message}\n"
    with open(log_file, 'a') as f:
        f.write(line)
    return line

def deploy_zip(website_id):
    try:
        website = get_website_by_id(website_id)
        if not website:
            return
        with get_db() as conn:
            cur = conn.execute('''INSERT INTO deployments (website_id, status, started_at)
                                  VALUES (?, ?, CURRENT_TIMESTAMP)''', (website_id, 'queued'))
            deployment_id = cur.lastrowid
            conn.commit()
        log_file = os.path.join(LOG_FOLDER, f"deploy_{deployment_id}.log")
        with open(log_file, 'w') as f:
            f.write(write_log_step(log_file, "SYSTEM", "Deployment started"))
        def log_cb(step, msg):
            write_log_step(log_file, step, msg)
            log_website(website_id, f"[{step}] {msg}", 'info')
        with get_db() as conn:
            conn.execute('UPDATE deployments SET status = ?, log_file = ? WHERE id = ?', ('extracting', log_file, deployment_id))
            conn.commit()
        folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
        zip_path = os.path.join(folder, 'upload.zip')
        if os.path.exists(zip_path):
            log_cb("SYSTEM", "Extracting ZIP...")
            ok, msg = extract_zip(zip_path, folder)
            if not ok:
                log_cb("ERROR", f"Extraction failed: {msg}")
                with get_db() as conn:
                    conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?',
                                 ('failed', deployment_id))
                    conn.commit()
                return
            os.remove(zip_path)
            log_cb("SUCCESS", "ZIP extracted")
        else:
            log_cb("SYSTEM", "No ZIP, using uploaded files")
        size = calculate_folder_size(folder)
        with get_db() as conn:
            conn.execute('UPDATE websites SET storage_used = ?, website_size = ? WHERE id = ?',
                         (size, size, website_id))
            conn.commit()
        with get_db() as conn:
            conn.execute('UPDATE deployments SET status = ? WHERE id = ?', ('starting', deployment_id))
            conn.commit()
        log_cb("SYSTEM", "Starting application...")
        ok, msg = start_website_process(website_id, log_cb)
        if ok:
            with get_db() as conn:
                conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?',
                             ('success', deployment_id))
                conn.commit()
            log_cb("SUCCESS", "Deployment successful!")
        else:
            with get_db() as conn:
                conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?',
                             ('failed', deployment_id))
                conn.commit()
            log_cb("ERROR", f"Deployment failed: {msg}")
    except Exception as e:
        log_website(website_id, f"Deployment exception: {str(e)}", 'error')
        with get_db() as conn:
            conn.execute('UPDATE deployments SET status = ?, completed_at = CURRENT_TIMESTAMP WHERE id = ?',
                         ('failed', deployment_id))
            conn.commit()

# ---------- HTML Rewrite for Proxy ----------
def rewrite_absolute_links(html, slug):
    # Replace href, src, action that start with '/' (absolute) to '/slug/...'
    # Avoid rewriting external URLs (http, https, //) and anchors (#)
    def replacer(match):
        attr = match.group(1)   # href, src, action
        url = match.group(2).strip()
        if url.startswith(('http://', 'https://', '//', '#')):
            return match.group(0)
        if url.startswith(f'/{slug}/'):
            return match.group(0)  # already rewritten
        new_url = f'/{slug}{url}' if url != '/' else f'/{slug}/'
        return f'{attr}="{new_url}"'
    pattern = r'(href|src|action)\s*=\s*"([^"]*)"'
    html = re.sub(pattern, replacer, html)
    pattern2 = r"(href|src|action)\s*=\s*'([^']*)'"
    html = re.sub(pattern2, replacer, html)
    return html

# ---------- Proxy Route (with rewrite) ----------
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def proxy(slug, path):
    website = get_website_by_slug(slug)
    if not website:
        return render_template_string(ERROR_TEMPLATE, message="Website not found", slug=slug), 404
    if website['status'] != 'running':
        return render_template_string(ERROR_TEMPLATE, message="Website is not running", slug=slug), 503
    port = website['allocated_port']
    if not port:
        return "Port not allocated", 500
    target = f"http://localhost:{port}/{path}"
    headers = {k: v for k, v in request.headers if k.lower() != 'host'}
    try:
        resp = requests.request(
            method=request.method,
            url=target,
            headers=headers,
            data=request.get_data(),
            cookies=request.cookies,
            stream=True,
            timeout=30
        )
        # If HTML, rewrite
        if 'text/html' in resp.headers.get('content-type', ''):
            content = resp.content
            try:
                html = content.decode('utf-8')
                rewritten = rewrite_absolute_links(html, slug)
                return Response(rewritten, status=resp.status_code, headers=dict(resp.headers))
            except:
                return Response(content, status=resp.status_code, headers=dict(resp.headers))
        else:
            return Response(stream_with_context(resp.iter_content(chunk_size=8192)),
                            status=resp.status_code, headers=resp.headers.items())
    except requests.exceptions.ConnectionError:
        update_website_status(website['id'], 'crashed')
        log_website(website['id'], "Proxy connection failed - website crashed", 'error')
        return render_template_string(ERROR_TEMPLATE, message="Website crashed", slug=slug), 503
    except Exception as e:
        log_website(website['id'], f"Proxy error: {str(e)}", 'error')
        return f"Proxy error: {str(e)}", 500

# ---------- Flask Routes ----------
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
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
        return redirect(url_for('index'))
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/login', methods=['POST'])
def login():
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    user = get_user_by_username(username)
    if not user or not check_password_hash(user['password_hash'], password):
        return render_template_string(LOGIN_TEMPLATE, error='Invalid credentials')
    if user['status'] != 'active':
        return render_template_string(LOGIN_TEMPLATE, error='Account disabled')
    session['user_id'] = user['id']
    session['username'] = user['username']
    session['role'] = user['role']
    session['plan'] = user['plan']
    with get_db() as conn:
        conn.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP WHERE id = ?', (user['id'],))
        conn.commit()
    return redirect(url_for('dashboard'))

@app.route('/logout')
def logout():
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
    return render_template_string(DASHBOARD_TEMPLATE, user=session['username'], websites=websites, base_url=base_url)

@app.route('/upload', methods=['POST'])
def upload():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    if 'files[]' not in request.files:
        return jsonify({'success': False, 'error': 'No files'}), 400
    files = request.files.getlist('files[]')
    if not files:
        return jsonify({'success': False, 'error': 'No files selected'}), 400
    user_id = session['user_id']
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (user_id,)).fetchone()[0]
    slug = generate_website_slug(session['username'], count)
    if get_website_by_slug(slug):
        count += 1
        slug = generate_website_slug(session['username'], count)
    with get_db() as conn:
        cur = conn.execute('INSERT INTO websites (owner_id, website_slug, website_folder, status) VALUES (?, ?, ?, ?)',
                           (user_id, slug, f"website_{0}", 'uploaded'))
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
    threading.Thread(target=deploy_zip, args=(website_id,), daemon=True).start()
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug})

@app.route('/website/<int:website_id>/start', methods=['POST'])
def start_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] in ('running', 'starting'):
        return jsonify({'success': False, 'error': 'Already running'}), 400
    update_website_status(website_id, 'starting')
    ok, msg = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/website/<int:website_id>/stop', methods=['POST'])
def stop_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] not in ('running', 'starting'):
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
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] == 'running':
        stop_website_process(website_id)
    update_website_status(website_id, 'starting')
    ok, msg = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/website/<int:website_id>/delete', methods=['POST'])
def delete_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if website['status'] in ('running', 'starting'):
        stop_website_process(website_id)
    shutil.rmtree(os.path.join(UPLOAD_FOLDER, f"website_{website_id}"), ignore_errors=True)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.execute('DELETE FROM deployments WHERE website_id = ?', (website_id,))
        conn.commit()
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/rename', methods=['POST'])
def rename_website(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    new_name = request.form.get('name', '').strip()
    if not new_name:
        return jsonify({'success': False, 'error': 'Name cannot be empty'}), 400
    with get_db() as conn:
        conn.execute('UPDATE websites SET website_name = ? WHERE id = ?', (new_name, website_id))
        conn.commit()
    return jsonify({'success': True, 'new_name': new_name})

# ---------- Logs and Deployments ----------
@app.route('/website/<int:website_id>/build')
def build_logs_page(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    with get_db() as conn:
        dep = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY id DESC LIMIT 1', (website_id,)).fetchone()
    if not dep:
        return render_template_string(BUILD_LOGS_TEMPLATE, website=website, no_logs=True)
    return render_template_string(BUILD_LOGS_TEMPLATE, website=website, no_logs=False)

@app.route('/deploy/<int:website_id>/logs')
def deploy_logs_sse(website_id):
    if 'user_id' not in session:
        return "Unauthorized", 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
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
                yield f"data: [SYSTEM] Deployment completed with status: {status['status']}\n\n"
                break
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

@app.route('/website/<int:website_id>/logs')
def view_logs(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    with get_db() as conn:
        logs = conn.execute('SELECT * FROM logs WHERE website_id = ? ORDER BY timestamp DESC LIMIT 200', (website_id,)).fetchall()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
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
    return render_template_string(LOGS_TEMPLATE, website=website, logs=logs, file_log=file_log, deploy_log=deploy_log, error_log_text=error_log_text)

@app.route('/website/<int:website_id>/deployments')
def deployment_history(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    with get_db() as conn:
        deployments = conn.execute('SELECT * FROM deployments WHERE website_id = ? ORDER BY started_at DESC', (website_id,)).fetchall()
    return render_template_string(DEPLOYMENTS_TEMPLATE, website=website, deployments=deployments)

# ---------- File Manager (simplified) ----------
@app.route('/website/<int:website_id>/files')
def files(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        abort(404)
    items = []
    for root, dirs, files_list in os.walk(folder):
        rel = os.path.relpath(root, folder)
        if rel == '.':
            rel = ''
        for f in files_list:
            full = os.path.join(root, f)
            size = os.path.getsize(full)
            items.append({'name': f, 'path': os.path.join(rel, f).replace('\\', '/'), 'is_dir': False, 'size': size})
        for d in dirs:
            items.append({'name': d, 'path': os.path.join(rel, d).replace('\\', '/'), 'is_dir': True})
    return render_template_string(FILES_TEMPLATE, website=website, items=items)

@app.route('/website/<int:website_id>/edit', methods=['GET', 'POST'])
def edit_file(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    file_path = request.args.get('path', '').strip()
    if not file_path:
        return "No file path", 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", file_path)
    if not os.path.exists(full) or not os.path.isfile(full):
        abort(404)
    if request.method == 'GET':
        with open(full, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        return render_template_string(EDIT_TEMPLATE, website=website, file_path=file_path, content=content)
    else:
        new_content = request.form.get('content', '')
        with open(full, 'w', encoding='utf-8') as f:
            f.write(new_content)
        return redirect(url_for('files', website_id=website_id))

@app.route('/website/<int:website_id>/file/upload', methods=['POST'])
def upload_file(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400
    rel_path = request.form.get('path', '')
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", rel_path)
    os.makedirs(folder, exist_ok=True)
    filename = secure_filename(file.filename)
    save_path = os.path.join(folder, filename)
    file.save(save_path)
    size = os.path.getsize(save_path)
    with get_db() as conn:
        conn.execute('UPDATE websites SET storage_used = storage_used + ? WHERE id = ?', (size, website_id))
        conn.commit()
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/delete', methods=['POST'])
def delete_file(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    path = request.json.get('path', '').strip()
    if not path:
        return jsonify({'success': False, 'error': 'Path required'}), 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    if os.path.isdir(full):
        shutil.rmtree(full)
    else:
        os.remove(full)
    new_size = calculate_folder_size(os.path.join(UPLOAD_FOLDER, f"website_{website_id}"))
    with get_db() as conn:
        conn.execute('UPDATE websites SET storage_used = ? WHERE id = ?', (new_size, website_id))
        conn.commit()
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/rename', methods=['POST'])
def rename_file(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        return jsonify({'success': False, 'error': 'Not found'}), 404
    data = request.json
    old_path = data.get('old_path', '').strip()
    new_name = data.get('new_name', '').strip()
    if not old_path or not new_name:
        return jsonify({'success': False, 'error': 'Old path and new name required'}), 400
    base = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    old_full = os.path.join(base, old_path)
    if not os.path.exists(old_full):
        return jsonify({'success': False, 'error': 'Not found'}), 404
    new_full = os.path.join(os.path.dirname(old_full), new_name)
    if os.path.exists(new_full):
        return jsonify({'success': False, 'error': 'Already exists'}), 400
    os.rename(old_full, new_full)
    return jsonify({'success': True})

@app.route('/website/<int:website_id>/file/download', methods=['GET'])
def download_file(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    path = request.args.get('path', '').strip()
    if not path:
        abort(400)
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", path)
    if not os.path.exists(full) or os.path.isdir(full):
        abort(404)
    return send_file(full, as_attachment=True)

# ---------- Manage Page ----------
@app.route('/website/<int:website_id>/manage')
def manage_website(website_id):
    if 'user_id' not in session:
        return redirect(url_for('index'))
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
    return render_template_string(MANAGE_TEMPLATE, website=website, base_url=base_url)

# ========== TEMPLATES ==========
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

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Dashboard</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1200px;margin:auto}.header{display:flex;justify-content:space-between;align-items:center;padding:15px 0;border-bottom:1px solid rgba(255,255,255,0.1)}.header a{color:#00e5ff;text-decoration:none}.upload-box{background:rgba(255,255,255,0.03);border:2px dashed rgba(255,255,255,0.15);border-radius:16px;padding:20px;margin:20px 0;text-align:center}.upload-box input{margin:10px 0}.btn{background:#00e5ff;border:none;padding:10px 30px;border-radius:30px;color:#000;font-weight:bold;cursor:pointer}.btn:hover{background:#00cce0}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(280px,1fr));gap:20px;margin-top:20px}.card{background:rgba(255,255,255,0.04);border-radius:16px;padding:20px;border:1px solid rgba(255,255,255,0.06)}.card .title{font-size:1.2rem;font-weight:bold}.card .slug{color:#889;font-size:0.9rem;margin:5px 0}.status{display:inline-block;padding:2px 12px;border-radius:20px;font-size:0.75rem;margin:8px 0}.status-running{background:rgba(0,229,255,0.2);color:#00e5ff}.status-stopped{background:rgba(255,71,87,0.2);color:#ff4757}.status-uploaded{background:rgba(255,170,0,0.2);color:#ffaa00}.status-failed{background:rgba(255,0,0,0.2);color:#ff0000}.actions{margin:10px 0}.actions button{padding:4px 12px;border:none;border-radius:8px;cursor:pointer;font-size:0.8rem;margin-right:4px}.start{background:rgba(0,229,255,0.2);color:#00e5ff}.start:hover{background:#00e5ff;color:#000}.stop{background:rgba(255,71,87,0.2);color:#ff4757}.stop:hover{background:#ff4757;color:#fff}.del{background:rgba(255,0,0,0.15);color:#ff4444}.del:hover{background:#ff0000;color:#fff}.visit{background:#00e5ff;color:#000;padding:4px 12px;border-radius:8px;text-decoration:none;display:inline-block;font-size:0.8rem}.manage{background:rgba(255,255,255,0.08);color:#aaa;padding:4px 12px;border-radius:8px;text-decoration:none;font-size:0.8rem}.manage:hover{background:rgba(255,255,255,0.15);color:#fff}.log-container{background:#0d0d0d;border-radius:12px;padding:15px;max-height:400px;overflow-y:auto;font-family:monospace;font-size:13px;display:none;margin:20px 0}.log-container .line{margin:0;white-space:pre-wrap}.line.SYSTEM{color:#00e5ff}.line.SUCCESS{color:#00ff88}.line.ERROR{color:#ff4757}.line.PIP{color:#ffaa00}.line.STARTUP{color:#fbbf24}.line.PROCESS{color:#9ca3af}</style>
</head><body>
<div class="container">
<div class="header"><h2>🚀 Host</h2><div><span>{{ user }}</span> <a href="/logout">Logout</a></div></div>
<div class="upload-box"><h3>Upload (ZIP or files)</h3><input type="file" id="fileInput" multiple><button class="btn" onclick="upload()">Upload & Deploy</button><div id="uploadStatus"></div></div>
<div id="logContainer" class="log-container"><div id="logContent"></div></div>
<h2>Your Websites</h2>
<div class="grid">
{% for w in websites %}
<div class="card">
<div class="title">{{ w.website_name or w.website_slug }}</div>
<div class="slug">🔗 {{ base_url }}/{{ w.website_slug }}/</div>
<div>Port: {{ w.allocated_port or '—' }}</div>
<div class="status status-{{ w.status }}">{{ w.status.upper() }}</div>
<div style="font-size:0.8rem;color:#666">Size: {{ (w.storage_used or 0)//1024 }} KB</div>
<div class="actions">
{% if w.status == 'running' %}
<button class="stop" onclick="action({{ w.id }},'stop')">Stop</button>
<a href="{{ base_url }}/{{ w.website_slug }}/" target="_blank" class="visit">Visit</a>
{% else %}
<button class="start" onclick="action({{ w.id }},'start')">Start</button>
{% endif %}
<button class="del" onclick="if(confirm('Delete?')) action({{ w.id }},'delete')">Delete</button>
<a class="manage" href="/website/{{ w.id }}/manage">Manage</a>
</div>
</div>
{% endfor %}
</div>
</div>
<script>
function action(id,type){fetch('/website/'+id+'/'+type,{method:'POST'}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})}
function upload(){const input=document.getElementById('fileInput');if(!input.files.length)return alert('Select files');const fd=new FormData();for(let f of input.files) fd.append('files[]', f);const st=document.getElementById('uploadStatus');st.innerHTML='⏳ Uploading...';fetch('/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{if(d.success){st.innerHTML='✅ Uploaded! Logs loading...';showLogs(d.website_id);}else st.innerHTML='❌ '+d.error;})}
let currentEventSource=null;function showLogs(websiteId){const container=document.getElementById('logContainer');const content=document.getElementById('logContent');if(currentEventSource){currentEventSource.close();currentEventSource=null;}content.innerHTML='';container.style.display='block';const evt=new EventSource('/deploy/'+websiteId+'/logs');currentEventSource=evt;let autoScroll=true;evt.onmessage=function(e){const data=e.data;if(!data)return;const div=document.createElement('div');div.className='line';const match=data.match(/^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] (.*)$/);if(match){const [,ts,step,msg]=match;div.innerHTML=`<span style="color:#666">[${ts}]</span> <span style="color:#888">[${step}]</span> ${msg}`;div.classList.add(step);}else div.textContent=data;content.appendChild(div);if(autoScroll)container.scrollTop=container.scrollHeight;if(data.includes('Deployment completed')){setTimeout(()=>{container.style.display='none';if(currentEventSource)currentEventSource.close();location.reload();},3000);}};evt.onerror=function(){};container.addEventListener('scroll',function(){autoScroll=container.scrollTop >= container.scrollHeight - container.clientHeight - 10;});}
</script>
</body></html>
"""

MANAGE_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Manage</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:800px;margin:auto}.back{color:#00e5ff;text-decoration:none}.actions{display:flex;flex-wrap:wrap;gap:10px;margin:15px 0}.btn{background:#00e5ff;border:none;padding:8px 20px;border-radius:30px;color:#000;font-weight:bold;cursor:pointer;text-decoration:none}.btn:hover{background:#00cce0}.btn-danger{background:#ff4757;color:#fff}.btn-outline{background:transparent;border:1px solid #00e5ff;color:#00e5ff}.card{background:rgba(255,255,255,0.04);border-radius:16px;padding:20px;margin:15px 0;border:1px solid rgba(255,255,255,0.06)}input[type="text"]{width:100%;padding:8px;background:#1a1a2e;border:1px solid #333;border-radius:8px;color:#fff}</style>
</head><body>
<div class="container"><a href="/dashboard" class="back">← Dashboard</a><h2>Manage: {{ website.website_name or website.website_slug }}</h2>
<div class="card"><h3>Actions</h3><div class="actions">
<a class="btn" href="{{ base_url }}/{{ website.website_slug }}/" target="_blank">Visit</a>
<form method="POST" action="/website/{{ website.id }}/start" style="display:inline"><button class="btn">Start</button></form>
<form method="POST" action="/website/{{ website.id }}/stop" style="display:inline"><button class="btn btn-danger">Stop</button></form>
<form method="POST" action="/website/{{ website.id }}/restart" style="display:inline"><button class="btn btn-outline">Restart</button></form>
<form method="POST" action="/website/{{ website.id }}/delete" style="display:inline" onsubmit="return confirm('Delete?')"><button class="btn btn-danger">Delete</button></form>
</div></div>
<div class="card"><h3>Rename</h3><form method="POST" action="/website/{{ website.id }}/rename"><input type="text" name="name" value="{{ website.website_name or '' }}" placeholder="New name"><button class="btn" style="margin-top:10px">Update Name</button></form></div>
<div class="card"><h3>Manage Files</h3><a class="btn" href="/website/{{ website.id }}/files">📁 File Manager</a></div>
<div class="card"><h3>Logs & Deployments</h3><div class="actions"><a class="btn btn-outline" href="/website/{{ website.id }}/logs">📜 Logs</a><a class="btn btn-outline" href="/website/{{ website.id }}/deployments">📋 Deployments</a><a class="btn btn-outline" href="/website/{{ website.id }}/build">🖥 Build Logs</a></div></div>
<div class="card"><h3>Details</h3><p><strong>Status:</strong> {{ website.status }}</p><p><strong>Port:</strong> {{ website.allocated_port or 'Not allocated' }}</p><p><strong>Runtime:</strong> {{ website.runtime }}</p><p><strong>Created:</strong> {{ website.created_at }}</p><p><strong>Size:</strong> {{ (website.storage_used or 0)//1024 }} KB</p></div>
</div></body></html>
"""

FILES_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Files</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1000px;margin:auto}.back{color:#00e5ff;text-decoration:none}h2{margin:20px 0}.upload-area{margin:15px 0;padding:20px;border:2px dashed rgba(255,255,255,0.2);border-radius:15px;text-align:center}.upload-area input{display:block;margin:10px auto}ul{list-style:none;padding:0}li{display:flex;justify-content:space-between;align-items:center;padding:10px 15px;border-bottom:1px solid rgba(255,255,255,0.05);border-radius:10px}li:hover{background:rgba(255,255,255,0.03)}a{color:#00e5ff;text-decoration:none}.actions a,.actions button{background:rgba(255,255,255,0.05);border:none;color:#aaa;padding:4px 10px;border-radius:8px;cursor:pointer;font-size:0.75rem}.actions a:hover,.actions button:hover{background:rgba(255,255,255,0.1);color:#fff}.search{width:100%;padding:12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff;margin-bottom:15px}</style>
</head><body><div class="container"><a href="/dashboard" class="back">← Dashboard</a><h2>📁 {{ website.website_name or website.website_slug }}</h2>
<div class="upload-area"><h4>Upload File</h4><input type="file" id="fileUpload" multiple><button onclick="uploadFiles({{ website.id }})">Upload</button></div>
<input type="text" class="search" id="searchFile" placeholder="Search..." onkeyup="filterFiles()"><ul id="fileList">
{% for item in items %}<li data-name="{{ item.name.lower() }}" data-path="{{ item.path }}"><span>{% if item.is_dir %}📁 {% else %}📄 {% endif %}<a href="?path={{ item.path }}">{{ item.name }}</a></span><span class="actions">{% if not item.is_dir %}<a href="/website/{{ website.id }}/edit?path={{ item.path }}">✏️</a><a href="/website/{{ website.id }}/file/download?path={{ item.path }}">⬇️</a>{% endif %}<button onclick="deleteFile({{ website.id }},'{{ item.path }}')">🗑</button><button onclick="renamePrompt({{ website.id }},'{{ item.path }}')">✏️ Rename</button></span></li>{% endfor %}</ul>
</div><script>
function uploadFiles(websiteId){const files=document.getElementById('fileUpload').files;if(!files.length)return alert('Select files');const fd=new FormData();for(let f of files) fd.append('file', f);const params=new URLSearchParams(window.location.search);const path=params.get('path')||'';fd.append('path', path);fetch('/website/'+websiteId+'/file/upload',{method:'POST',body:fd}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})}
function deleteFile(websiteId,path){if(!confirm('Delete?'))return;fetch('/website/'+websiteId+'/file/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path:path})}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})}
function renamePrompt(websiteId,oldPath){const newName=prompt('Enter new name:', oldPath.split('/').pop());if(!newName)return;fetch('/website/'+websiteId+'/file/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({old_path:oldPath, new_name:newName})}).then(r=>r.json()).then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})}
function filterFiles(){const q=document.getElementById('searchFile').value.toLowerCase();document.querySelectorAll('#fileList li').forEach(li=>{li.style.display=li.dataset.name.includes(q)?'flex':'none'})}
</script></body></html>
"""

EDIT_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Edit</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:900px;margin:auto}.back{color:#00e5ff;text-decoration:none}h2{margin:20px 0}textarea{width:100%;height:400px;background:rgba(255,255,255,0.04);border:1px solid rgba(255,255,255,0.1);border-radius:15px;color:#fff;padding:15px;font-family:monospace;font-size:14px;outline:none}textarea:focus{border-color:#00e5ff}.btns{display:flex;gap:12px;margin-top:15px}.save{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 30px;border-radius:50px;color:#fff;font-weight:700;cursor:pointer}.cancel{background:rgba(255,255,255,0.08);border:1px solid rgba(255,255,255,0.1);padding:12px 30px;border-radius:50px;color:#aaa;text-decoration:none}</style>
</head><body><div class="container"><a href="/website/{{ website.id }}/files" class="back">← Back</a><h2>✏️ {{ file_path }}</h2><form method="POST"><textarea name="content">{{ content }}</textarea><div class="btns"><button class="save" type="submit">💾 Save</button><a href="/website/{{ website.id }}/files" class="cancel">Cancel</a></div></form></div></body></html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Logs</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1000px;margin:auto}.back{color:#00e5ff;text-decoration:none}h2{margin:20px 0}.tabs{display:flex;gap:10px;margin:15px 0;flex-wrap:wrap}.tab{background:rgba(255,255,255,0.05);padding:8px 18px;border-radius:50px;cursor:pointer}.tab.active{background:rgba(0,229,255,0.2);color:#00e5ff}.tab:hover{background:rgba(255,255,255,0.1)}.tab-content{display:none}.tab-content.active{display:block}pre{background:rgba(0,0,0,0.4);padding:15px;border-radius:15px;max-height:400px;overflow-y:auto;font-family:monospace;font-size:12px;white-space:pre-wrap;color:#aab}</style>
</head><body><div class="container"><a href="/dashboard" class="back">← Dashboard</a><h2>📜 Logs for {{ website.website_name or website.website_slug }}</h2>
<div class="tabs"><div class="tab active" data-target="deploy">Deployment</div><div class="tab" data-target="runtime">Runtime (Live)</div><div class="tab" data-target="error">Errors</div></div>
<div id="deploy" class="tab-content active"><h3>📋 Deployment Log</h3><pre>{{ deploy_log if deploy_log else 'No deployment logs yet.' }}</pre></div>
<div id="runtime" class="tab-content"><h3>🖥️ Runtime Log (Live)</h3><div id="runtimeLogContainer" style="background:rgba(0,0,0,0.4);padding:15px;border-radius:15px;max-height:400px;overflow-y:auto;font-family:monospace;font-size:12px;white-space:pre-wrap;color:#aab;"></div></div>
<div id="error" class="tab-content"><h3>❌ Error Log</h3><pre>{{ error_log_text if error_log_text else 'No errors logged.' }}</pre></div>
</div><script>
document.querySelectorAll('.tab').forEach(tab=>{tab.addEventListener('click',function(){document.querySelectorAll('.tab').forEach(t=>t.classList.remove('active'));this.classList.add('active');document.querySelectorAll('.tab-content').forEach(tc=>tc.classList.remove('active'));document.getElementById(this.dataset.target).classList.add('active');if(this.dataset.target==='runtime')startRuntimeLogs();});});
let runtimeEventSource=null;function startRuntimeLogs(){if(runtimeEventSource){runtimeEventSource.close();runtimeEventSource=null;}const container=document.getElementById('runtimeLogContainer');container.innerHTML='Connecting...';runtimeEventSource=new EventSource('/runtime/{{ website.id }}/logs');let autoScroll=true;runtimeEventSource.onmessage=function(e){const data=e.data;if(!data)return;const div=document.createElement('div');div.textContent=data;container.appendChild(div);if(autoScroll)container.scrollTop=container.scrollHeight;};runtimeEventSource.onerror=function(){};container.addEventListener('scroll',function(){autoScroll=container.scrollTop>=container.scrollHeight-container.clientHeight-10;});}
</script></body></html>
"""

DEPLOYMENTS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Deployments</title>
<style>body{background:#0a0e1a;color:#fff;font-family:system-ui;padding:20px}.container{max-width:1000px;margin:auto}.back{color:#00e5ff;text-decoration:none}h2{margin:20px 0}table{width:100%;border-collapse:collapse;background:rgba(255,255,255,0.03);border-radius:15px;overflow:hidden}th,td{padding:12px 15px;text-align:left;border-bottom:1px solid rgba(255,255,255,0.05)}th{background:rgba(255,255,255,0.05)}.status-badge{padding:3px 10px;border-radius:50px;font-size:0.75rem}.status-success{background:rgba(0,229,255,0.2);color:#00e5ff}.status-failed{background:rgba(255,0,0,0.2);color:#ff0000}.status-queued{background:rgba(100,100,255,0.2);color:#6666ff}</style>
</head><body><div class="container"><a href="/dashboard" class="back">← Dashboard</a><h2>📋 Deployment History</h2><table><tr><th>#</th><th>Repo</th><th>Branch</th><th>Status</th><th>Started</th><th>Duration</th></tr>{% for dep in deployments %}<tr><td>{{ dep.id }}</td><td>{{ dep.repo_url or 'ZIP' }}</td><td>{{ dep.branch or 'main' }}</td><td><span class="status-badge status-{{ dep.status }}">{{ dep.status.upper() }}</span></td><td>{{ dep.started_at }}</td><td>{{ dep.duration or 'N/A' }}s</td></tr>{% else %}<tr><td colspan="6">No deployments yet.</td></tr>{% endfor %}</table></div></body></html>
"""

BUILD_LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Build Logs</title>
<style>*{margin:0;padding:0;box-sizing:border-box}body{background:#0a0e1a;color:#fff;font-family:system-ui;height:100vh;display:flex;flex-direction:column;padding:20px;overflow:hidden}.top-bar{display:flex;justify-content:space-between;align-items:center;padding:10px 20px;background:rgba(255,255,255,0.05);border-radius:15px;margin-bottom:15px;flex-shrink:0}.top-bar h2{color:#00e5ff}.top-bar a{color:#00e5ff;text-decoration:none}.terminal{flex:1;background:#0d0d0d;border-radius:15px;padding:20px;overflow-y:auto;font-family:monospace;font-size:14px;line-height:1.6;border:1px solid rgba(255,255,255,0.05)}.terminal .line{margin:0;white-space:pre-wrap}.terminal .line .timestamp{color:#666;margin-right:10px}.terminal .line .step{color:#888;margin-right:10px}.terminal .line.SYSTEM{color:#00e5ff}.terminal .line.SUCCESS{color:#00ff88}.terminal .line.ERROR{color:#ff4757}.terminal .line.PIP{color:#ffaa00}.terminal .line.STARTUP{color:#fbbf24}.terminal .line.PROCESS{color:#9ca3af}.status-indicator{padding:8px 16px;border-radius:50px;font-size:0.9rem;font-weight:600}.status-indicator.running{background:rgba(0,229,255,0.2);color:#00e5ff}.status-indicator.success{background:rgba(0,255,136,0.2);color:#00ff88}.status-indicator.failed{background:rgba(255,71,87,0.2);color:#ff4757}</style>
</head><body><div class="top-bar"><h2>🖥 Build Logs</h2><div><span class="status-indicator running" id="statusBadge">Running</span><a href="/dashboard" style="margin-left:20px;">← Dashboard</a></div></div><div class="terminal" id="terminal"><div id="logContainer">{% if no_logs %}<div class="line SYSTEM"><span class="timestamp">[--:--:--]</span><span class="step">[SYSTEM]</span>No deployment logs yet.</div>{% endif %}</div></div><script>
const terminal=document.getElementById('terminal');const logContainer=document.getElementById('logContainer');const statusBadge=document.getElementById('statusBadge');
{% if not no_logs %}
const evtSource=new EventSource('/deploy/{{ website.id }}/logs');let autoScroll=true;
evtSource.onmessage=function(e){const data=e.data;if(!data)return;if(data==='[REFRESH]'){location.reload();return;}const div=document.createElement('div');div.className='line';const match=data.match(/^\[(\d{2}:\d{2}:\d{2})\] \[([A-Z]+)\] (.*)$/);if(match){const [,ts,step,msg]=match;div.innerHTML=`<span class="timestamp">[${ts}]</span><span class="step">[${step}]</span>${msg}`;div.classList.add(step);}else div.textContent=data;logContainer.appendChild(div);if(autoScroll)terminal.scrollTop=terminal.scrollHeight;if(data.includes('Deployment Successful')){statusBadge.textContent='✅ Success';statusBadge.className='status-indicator success';evtSource.close();}else if(data.includes('Deployment failed')||data.includes('ERROR')){statusBadge.textContent='❌ Failed';statusBadge.className='status-indicator failed';}if(data.includes('Deployment completed with status:')){if(data.includes('success')){statusBadge.textContent='✅ Success';statusBadge.className='status-indicator success';}else{statusBadge.textContent='❌ Failed';statusBadge.className='status-indicator failed';}evtSource.close();}};
evtSource.onerror=function(){};terminal.addEventListener('scroll',function(){autoScroll=terminal.scrollTop>=terminal.scrollHeight-terminal.clientHeight-10;});
{% endif %}
</script></body></html>
"""

# ---------- Runtime Logs SSE ----------
@app.route('/runtime/<int:website_id>/logs')
def runtime_logs_sse(website_id):
    if 'user_id' not in session:
        return "Unauthorized", 401
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        abort(404)
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
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
    return Response(stream_with_context(generate()), mimetype='text/event-stream')

# ---------- Server ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)