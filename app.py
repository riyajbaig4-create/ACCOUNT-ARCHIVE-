#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import json
import uuid
import shutil
import zipfile
import subprocess
import time
import threading
import socket
import signal
import requests
from datetime import datetime
from flask import Flask, request, jsonify, session, render_template_string, send_from_directory, Response

app = Flask(__name__)
app.secret_key = 'secret-key-change-me'

# ---------- CONFIG ----------
ADMIN_USER = "admin"
ADMIN_PASS = "admin"

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_FILE = os.path.join(BASE_DIR, 'deployments.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# ---------- DATABASE ----------
def load_db():
    if os.path.exists(DB_FILE):
        with open(DB_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_db(db):
    with open(DB_FILE, 'w') as f:
        json.dump(db, f, indent=2)

db = load_db()

# ---------- HELPERS ----------
def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_log_file(username):
    return os.path.join(LOGS_FOLDER, f"{username}.log")

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def extract_zip(zip_path, dest):
    with zipfile.ZipFile(zip_path, 'r') as z:
        z.extractall(dest)
    os.remove(zip_path)

def detect_main_file(folder):
    candidates = ['app.py', 'main.py', 'server.py', 'index.py', 'bot.py']
    for f in candidates:
        if os.path.exists(os.path.join(folder, f)):
            return f
    return None

def wait_for_port(port, timeout=30):
    start = time.time()
    while time.time() - start < timeout:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
                s.connect(('localhost', port))
                return True
        except:
            time.sleep(0.5)
    return False

# ---------- PROCESS TRACKING ----------
processes = {}  # username -> subprocess.Popen

def log_append(username, line):
    log_file = get_log_file(username)
    with open(log_file, 'a') as f:
        f.write(line + '\n')

def get_log_content(username):
    log_file = get_log_file(username)
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            return f.read()
    return ""

# ---------- DEPLOYMENT ENGINE ----------
def deploy_app(username, folder):
    """Background thread function"""
    try:
        log_append(username, f"🚀 Deployment started for {username} at {datetime.now()}")
        log_append(username, f"📁 Folder: {folder}")

        # 1. Detect main file
        main_file = detect_main_file(folder)
        if not main_file:
            # Static site - no process needed
            log_append(username, "📄 Static site detected (no Python file found)")
            db[username]['status'] = 'static'
            db[username]['link'] = f"/{username}"
            save_db(db)
            return

        log_append(username, f"🐍 Python app detected: {main_file}")

        # 2. Install requirements
        req_file = os.path.join(folder, 'requirements.txt')
        if os.path.exists(req_file):
            log_append(username, "📦 Installing dependencies...")
            try:
                proc = subprocess.Popen(
                    ['pip', 'install', '-r', 'requirements.txt'],
                    cwd=folder,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                for line in proc.stdout:
                    log_append(username, f"  {line.strip()}")
                proc.wait()
                if proc.returncode != 0:
                    log_append(username, "❌ pip install failed")
                    db[username]['status'] = 'failed'
                    db[username]['error'] = 'pip install failed'
                    save_db(db)
                    return
                log_append(username, "✅ Dependencies installed")
            except Exception as e:
                log_append(username, f"❌ pip install error: {e}")
                db[username]['status'] = 'failed'
                db[username]['error'] = str(e)
                save_db(db)
                return

        # 3. Find free port
        port = find_free_port()
        log_append(username, f"🔌 Using port: {port}")

        # 4. Start the app
        env = os.environ.copy()
        env['PORT'] = str(port)
        env['HOST'] = '0.0.0.0'

        log_file = get_log_file(username)

        try:
            proc = subprocess.Popen(
                ['python3', main_file],
                cwd=folder,
                stdout=open(log_file, 'a'),
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )

            processes[username] = proc
            db[username]['pid'] = proc.pid
            db[username]['port'] = port
            db[username]['status'] = 'starting'
            db[username]['started_at'] = datetime.now().isoformat()
            save_db(db)

            log_append(username, f"✅ Process started with PID {proc.pid}")
            log_append(username, "⏳ Waiting for server to be ready...")

            # 5. Wait for port
            if wait_for_port(port, timeout=30):
                log_append(username, "✅ Server is ready!")
                db[username]['status'] = 'running'
                db[username]['link'] = f"/{username}"
                save_db(db)
                log_append(username, f"🔗 Link: /{username}")
            else:
                # Check if process died
                if proc.poll() is not None:
                    log_append(username, f"❌ Process died with code {proc.returncode}")
                else:
                    log_append(username, "❌ Timeout waiting for server")
                    # Kill it
                    try:
                        if os.name != 'nt':
                            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                        else:
                            proc.terminate()
                    except:
                        pass
                db[username]['status'] = 'failed'
                db[username]['error'] = 'Server failed to start (timeout)'
                save_db(db)

        except Exception as e:
            log_append(username, f"❌ Error starting process: {e}")
            db[username]['status'] = 'failed'
            db[username]['error'] = str(e)
            save_db(db)

    except Exception as e:
        log_append(username, f"❌ Deployment error: {e}")
        db[username]['status'] = 'failed'
        db[username]['error'] = str(e)
        save_db(db)

def stop_app(username):
    if username in processes:
        try:
            proc = processes[username]
            if proc.poll() is None:
                if os.name != 'nt':
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
                proc.wait(timeout=5)
            del processes[username]
        except:
            pass
    if username in db:
        db[username]['status'] = 'stopped'
        db[username]['pid'] = None
        save_db(db)
    log_append(username, "🛑 App stopped")

# ---------- CLEANUP ON EXIT ----------
def cleanup():
    for username in list(processes.keys()):
        try:
            proc = processes[username]
            if proc.poll() is None:
                if os.name != 'nt':
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
        except:
            pass

import atexit
atexit.register(cleanup)

# ---------- ROUTES ----------
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        u = request.form.get('username')
        p = request.form.get('password')
        if u == ADMIN_USER and p == ADMIN_PASS:
            session['logged_in'] = True
            return redirect('/dashboard')
        return render_template_string(LOGIN_HTML, error="Invalid credentials")
    if session.get('logged_in'):
        return redirect('/dashboard')
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect('/')
    return render_template_string(DASHBOARD_HTML, deployments=db)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ---------- DEPLOY ----------
@app.route('/deploy', methods=['POST'])
def deploy():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = request.form.get('username', '').strip()
    if not username:
        username = str(uuid.uuid4())[:8]

    if username in db:
        return jsonify({'error': 'Username already taken'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files uploaded'}), 400

    # Stop any existing
    if username in processes:
        stop_app(username)

    # Create folder
    folder = get_user_folder(username)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)

    # Clear log
    log_file = get_log_file(username)
    if os.path.exists(log_file):
        os.remove(log_file)

    # Save files
    for file in files:
        if file.filename == '':
            continue
        if file.filename.lower().endswith('.zip'):
            temp = os.path.join(folder, file.filename)
            file.save(temp)
            extract_zip(temp, folder)
        else:
            file.save(os.path.join(folder, file.filename))

    # Create DB entry
    db[username] = {
        'username': username,
        'created_at': datetime.now().isoformat(),
        'status': 'uploaded',
        'port': None,
        'pid': None,
        'link': None,
        'error': None,
        'folder': folder
    }
    save_db(db)

    log_append(username, f"📁 Files saved to {folder}")

    # Start deployment in background
    threading.Thread(target=deploy_app, args=(username, folder), daemon=True).start()

    return jsonify({
        'success': True,
        'username': username,
        'message': 'Deployment started'
    })

# ---------- STATUS (for polling) ----------
@app.route('/status/<username>')
def status(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in db:
        return jsonify({'error': 'Not found'}), 404

    data = db[username].copy()
    data['logs'] = get_log_content(username)
    data['link'] = data.get('link', '')
    data['is_running'] = username in processes and processes[username].poll() is None

    # Check if process died
    if data['status'] == 'running' and username in processes and processes[username].poll() is not None:
        data['status'] = 'failed'
        data['error'] = f"Process died with code {processes[username].returncode}"
        db[username]['status'] = 'failed'
        save_db(db)

    return jsonify(data)

# ---------- STOP / RESTART / DELETE ----------
@app.route('/stop/<username>', methods=['POST'])
def stop_route(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in db:
        return jsonify({'error': 'Not found'}), 404
    stop_app(username)
    return jsonify({'success': True})

@app.route('/restart/<username>', methods=['POST'])
def restart_route(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in db:
        return jsonify({'error': 'Not found'}), 404
    stop_app(username)
    folder = get_user_folder(username)
    threading.Thread(target=deploy_app, args=(username, folder), daemon=True).start()
    return jsonify({'success': True})

@app.route('/delete/<username>', methods=['POST'])
def delete_route(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in db:
        return jsonify({'error': 'Not found'}), 404
    stop_app(username)
    shutil.rmtree(get_user_folder(username), ignore_errors=True)
    log_file = get_log_file(username)
    if os.path.exists(log_file):
        os.remove(log_file)
    del db[username]
    save_db(db)
    return jsonify({'success': True})

# ---------- PROXY / STATIC SERVE ----------
@app.route('/<username>/', defaults={'path': ''})
@app.route('/<username>/<path:path>')
def serve_user(username, path):
    if username not in db:
        return "User not found", 404

    data = db[username]

    # If app is running, proxy everything
    if data.get('status') == 'running' and username in processes and processes[username].poll() is None:
        port = data.get('port')
        if not port:
            return "No port assigned", 500

        target = f"http://localhost:{port}/{path}"
        if request.query_string:
            target += "?" + request.query_string.decode('utf-8')

        try:
            # Forward request
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']}
            resp = requests.request(
                method=request.method,
                url=target,
                headers=headers,
                data=request.get_data(),
                allow_redirects=False,
                timeout=30
            )
            response = Response(resp.content, resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ['content-encoding', 'content-length', 'transfer-encoding', 'connection']:
                    response.headers[k] = v
            return response
        except requests.exceptions.ConnectionError:
            return "App not responding", 502
        except Exception as e:
            return f"Proxy error: {e}", 500

    # Static serve
    folder = get_user_folder(username)
    if path == '':
        if os.path.exists(os.path.join(folder, 'index.html')):
            return send_from_directory(folder, 'index.html')
        else:
            files = os.listdir(folder)
            html = f"<h2>📁 Files for {username}</h2><ul>"
            for f in files:
                html += f'<li><a href="{f}">{f}</a></li>'
            html += "</ul>"
            return html

    if not os.path.exists(os.path.join(folder, path)):
        return "File not found", 404
    return send_from_directory(folder, path)

# ---------- HTML TEMPLATES ----------
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hosting Panel</title>
    <style>
        body { background: #0c1018; color: #fff; font-family: Arial; display: flex; justify-content: center; align-items: center; height: 100vh; margin:0; }
        .box { background: #161b25; padding: 40px; border-radius: 20px; border: 1px solid #2b3240; width: 350px; }
        h2 { text-align: center; color: #00e5ff; }
        input { width: 100%; padding: 14px; margin: 10px 0; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; }
        .btn { width: 100%; padding: 14px; background: #00e5ff; color: #000; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .error { color: #ff4d4d; text-align: center; margin-top: 10px; }
    </style>
</head>
<body>
<div class="box">
    <h2>🔐 Admin Panel</h2>
    <form method="POST">
        <input type="text" name="username" placeholder="Username" value="admin" />
        <input type="password" name="password" placeholder="Password" value="admin" />
        <button class="btn">Login</button>
    </form>
    {% if error %}<div class="error">{{ error }}</div>{% endif %}
</div>
</body>
</html>
"""

DASHBOARD_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hosting Panel</title>
    <style>
        * { box-sizing: border-box; }
        body { background: #0c1018; color: #fff; font-family: Arial; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        h1 { color: #00e5ff; }
        .card { background: #161b25; border: 1px solid #2b3240; border-radius: 15px; padding: 20px; margin: 20px 0; }
        .logout { float: right; color: #ff4d4d; text-decoration: none; }
        .drop-zone { border: 2px dashed #00e5ff; border-radius: 15px; padding: 40px; text-align: center; cursor: pointer; margin: 10px 0; transition: 0.3s; }
        .drop-zone.dragover { background: #1a2a3a; border-color: #fff; }
        .input-group { display: flex; gap: 10px; flex-wrap: wrap; margin: 15px 0; }
        .input-group input { flex: 1; padding: 12px; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; min-width: 150px; }
        .btn { background: #00e5ff; color: #000; border: none; padding: 12px 24px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .btn:hover { opacity: 0.85; }
        .btn-danger { background: #ff4d4d; color: #fff; }
        .btn-success { background: #00ff6a; color: #000; }
        .btn-info { background: #4d88ff; color: #fff; }

        /* Build Logs */
        #buildLogs { background: #000; color: #00ff6a; padding: 15px; border-radius: 8px; max-height: 350px; overflow-y: auto; font-family: monospace; font-size: 13px; white-space: pre-wrap; margin-top: 10px; border: 1px solid #2b3240; display: none; }
        #buildLogs .log-line { margin: 0; }
        #buildLogs .error { color: #ff4d4d; }
        #buildLogs .success { color: #00ff6a; }
        #buildLogs .info { color: #00e5ff; }
        #buildLogs .warn { color: #ffaa00; }

        #deployResult { margin-top: 15px; padding: 15px; background: #0c1018; border-radius: 8px; border: 1px solid #2b3240; display: none; }

        /* Status badge */
        .status { padding: 4px 12px; border-radius: 20px; font-size: 12px; font-weight: bold; }
        .status.running { background: #00ff6a33; color: #00ff6a; border: 1px solid #00ff6a; }
        .status.failed { background: #ff4d4d33; color: #ff4d4d; border: 1px solid #ff4d4d; }
        .status.starting { background: #ffaa0033; color: #ffaa00; border: 1px solid #ffaa00; }
        .status.static { background: #333; color: #aaa; border: 1px solid #555; }
        .status.stopped { background: #555; color: #aaa; border: 1px solid #666; }

        .app-item { background: #0c1018; padding: 15px; border-radius: 10px; margin: 10px 0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 10px; border: 1px solid #1a1f2b; }
        .app-item .info { display: flex; flex-direction: column; gap: 5px; }
        .app-item .info .name { font-weight: bold; font-size: 16px; }
        .app-item .info .link { color: #00e5ff; word-break: break-all; }
        .app-item .actions { display: flex; gap: 5px; flex-wrap: wrap; }

        .btn-sm { padding: 6px 12px; font-size: 12px; border: none; border-radius: 5px; cursor: pointer; font-weight: bold; }
        .btn-sm:hover { opacity: 0.8; }

        #fileList { margin: 10px 0; color: #aaa; font-size: 14px; }

        .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #00e5ff33; border-top: 2px solid #00e5ff; border-radius: 50%; animation: spin 0.8s linear infinite; margin-right: 8px; vertical-align: middle; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }

        @media (max-width: 600px) { .app-item { flex-direction: column; align-items: stretch; } }
    </style>
</head>
<body>
<div class="container">
    <h1>🚀 Hosting Panel <a href="/logout" class="logout">Logout</a></h1>

    <!-- Upload Section -->
    <div class="card">
        <h3>📤 Upload New Website</h3>
        <div class="drop-zone" id="dropZone">
            <p style="font-size:24px;">📂</p>
            <p>Drag & drop files here, or click to browse</p>
            <p style="font-size:12px;color:#555;">Supports .zip, .html, .py, .js, .css, .json, .txt</p>
        </div>
        <input type="file" id="fileInput" multiple style="display:none;">
        <div id="fileList"></div>
        <div class="input-group">
            <input type="text" id="usernameInput" placeholder="Username (leave blank for auto-gen)" />
            <button class="btn" id="deployBtn">🚀 Deploy</button>
        </div>

        <!-- Build Logs -->
        <div id="buildLogs"></div>
        <div id="deployResult"></div>
    </div>

    <!-- Deployed Sites -->
    <div class="card">
        <h3>📋 Deployed Sites</h3>
        <div id="siteList">
            {% for username, data in deployments.items() %}
                <div class="app-item" data-username="{{ username }}">
                    <div class="info">
                        <span class="name">{{ username }}</span>
                        <span class="link">
                            {% if data.link %}
                                <a href="{{ data.link }}" target="_blank">{{ data.link }}</a>
                            {% else %}
                                <span style="color:#555;">No link yet</span>
                            {% endif %}
                        </span>
                        <span class="status {{ data.status }}">
                            {% if data.status == 'running' %}🟢 Running
                            {% elif data.status == 'starting' %}🔄 Starting...
                            {% elif data.status == 'failed' %}❌ Failed
                            {% elif data.status == 'static' %}📁 Static
                            {% elif data.status == 'stopped' %}⏹ Stopped
                            {% else %}⚪ {{ data.status }}{% endif %}
                        </span>
                        {% if data.error %}
                            <span style="color:#ff4d4d;font-size:12px;">Error: {{ data.error }}</span>
                        {% endif %}
                    </div>
                    <div class="actions">
                        {% if data.status == 'running' %}
                            <button class="btn-sm btn-danger stop-btn" data-username="{{ username }}">⏹ Stop</button>
                            <button class="btn-sm btn-info restart-btn" data-username="{{ username }}">🔄 Restart</button>
                        {% elif data.status == 'starting' %}
                            <button class="btn-sm btn-info" disabled>⏳ Starting...</button>
                        {% elif data.status == 'failed' or data.status == 'stopped' %}
                            <button class="btn-sm btn-info restart-btn" data-username="{{ username }}">🔄 Restart</button>
                        {% endif %}
                        <button class="btn-sm btn-danger delete-btn" data-username="{{ username }}">🗑 Delete</button>
                        <button class="btn-sm btn-info log-btn" data-username="{{ username }}">📜 Logs</button>
                    </div>
                </div>
            {% else %}
                <p style="color:#555;">No sites deployed yet.</p>
            {% endfor %}
        </div>
    </div>
</div>

<script>
    // ---------- UPLOAD ----------
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const deployBtn = document.getElementById('deployBtn');
    const usernameInput = document.getElementById('usernameInput');
    const buildLogs = document.getElementById('buildLogs');
    const deployResult = document.getElementById('deployResult');
    let selectedFiles = [];
    let pollInterval = null;
    let currentUsername = null;

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.classList.add('dragover'); });
    dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.classList.remove('dragover');
        if (e.dataTransfer.files.length) {
            selectedFiles = Array.from(e.dataTransfer.files);
            updateFileList();
        }
    });
    fileInput.addEventListener('change', () => {
        if (fileInput.files.length) {
            selectedFiles = Array.from(fileInput.files);
            updateFileList();
        }
    });

    function updateFileList() {
        if (selectedFiles.length === 0) { fileList.innerHTML = ''; return; }
        let html = '<strong>Selected:</strong><ul style="margin:5px 0;padding-left:20px;">';
        selectedFiles.forEach(f => html += `<li>${f.name} (${(f.size/1024).toFixed(1)} KB)</li>`);
        html += '</ul>';
        fileList.innerHTML = html;
    }

    deployBtn.addEventListener('click', async () => {
        if (selectedFiles.length === 0) { alert('Select files first.'); return; }
        const username = usernameInput.value.trim() || '';

        const formData = new FormData();
        selectedFiles.forEach(f => formData.append('files', f));
        formData.append('username', username);

        deployBtn.disabled = true;
        deployBtn.textContent = '⏳ Deploying...';
        buildLogs.style.display = 'block';
        buildLogs.innerHTML = '<div class="log-line info">⏳ Uploading and starting deployment...</div>';
        deployResult.style.display = 'none';

        try {
            const res = await fetch('/deploy', { method: 'POST', body: formData });
            const data = await res.json();

            if (data.success) {
                currentUsername = data.username;
                buildLogs.innerHTML += `<div class="log-line success">✅ Upload complete. Username: ${data.username}</div>`;
                buildLogs.innerHTML += `<div class="log-line info">⏳ Waiting for build logs...</div>`;
                buildLogs.scrollTop = buildLogs.scrollHeight;

                // Start polling
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(pollStatus, 1500);
                // Poll immediately
                setTimeout(pollStatus, 500);
            } else {
                buildLogs.innerHTML += `<div class="log-line error">❌ Error: ${data.error}</div>`;
                deployBtn.disabled = false;
                deployBtn.textContent = '🚀 Deploy';
            }
        } catch (e) {
            buildLogs.innerHTML += `<div class="log-line error">❌ Network error: ${e.message}</div>`;
            deployBtn.disabled = false;
            deployBtn.textContent = '🚀 Deploy';
        }
    });

    function pollStatus() {
        if (!currentUsername) return;
        fetch(`/status/${currentUsername}`)
            .then(res => res.json())
            .then(data => {
                // Update logs
                if (data.logs) {
                    const lines = data.logs.split('\n');
                    let html = '';
                    for (let line of lines) {
                        if (line.includes('❌')) html += `<div class="log-line error">${escapeHtml(line)}</div>`;
                        else if (line.includes('✅') || line.includes('success') || line.includes('ready')) html += `<div class="log-line success">${escapeHtml(line)}</div>`;
                        else if (line.includes('⏳') || line.includes('Waiting') || line.includes('Starting')) html += `<div class="log-line info">${escapeHtml(line)}</div>`;
                        else if (line.includes('⚠️')) html += `<div class="log-line warn">${escapeHtml(line)}</div>`;
                        else if (line.trim()) html += `<div class="log-line">${escapeHtml(line)}</div>`;
                    }
                    buildLogs.innerHTML = html || '<div class="log-line info">⏳ No logs yet...</div>';
                    buildLogs.scrollTop = buildLogs.scrollHeight;
                }

                // Check status
                if (data.status === 'running') {
                    clearInterval(pollInterval);
                    deployResult.style.display = 'block';
                    deployResult.innerHTML = `
                        <p style="color:#00ff6a;font-size:18px;">✅ Deployment successful!</p>
                        <p><strong>Username:</strong> ${data.username}</p>
                        <p><strong>Link:</strong> <a href="${data.link}" target="_blank" style="color:#00e5ff;">${window.location.origin}${data.link}</a></p>
                    `;
                    deployBtn.disabled = false;
                    deployBtn.textContent = '🚀 Deploy';
                    // Reload site list
                    setTimeout(() => location.reload(), 2000);
                } else if (data.status === 'failed' || data.status === 'error') {
                    clearInterval(pollInterval);
                    deployResult.style.display = 'block';
                    deployResult.innerHTML = `
                        <p style="color:#ff4d4d;font-size:18px;">❌ Deployment failed!</p>
                        <p style="color:#ff4d4d;">Error: ${data.error || 'Unknown error'}</p>
                        <p style="color:#aaa;">Check logs above for details.</p>
                    `;
                    deployBtn.disabled = false;
                    deployBtn.textContent = '🚀 Deploy';
                } else if (data.status === 'static') {
                    clearInterval(pollInterval);
                    deployResult.style.display = 'block';
                    deployResult.innerHTML = `
                        <p style="color:#00ff6a;font-size:18px;">✅ Static site deployed!</p>
                        <p><strong>Username:</strong> ${data.username}</p>
                        <p><strong>Link:</strong> <a href="${data.link}" target="_blank" style="color:#00e5ff;">${window.location.origin}${data.link}</a></p>
                    `;
                    deployBtn.disabled = false;
                    deployBtn.textContent = '🚀 Deploy';
                    setTimeout(() => location.reload(), 2000);
                }
            })
            .catch(err => {
                console.error('Poll error:', err);
            });
    }

    function escapeHtml(str) {
        const div = document.createElement('div');
        div.textContent = str;
        return div.innerHTML;
    }

    // ---------- ACTION BUTTONS ----------
    document.querySelectorAll('.stop-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Stop this app?')) return;
            const username = btn.dataset.username;
            await fetch(`/stop/${username}`, { method: 'POST' });
            location.reload();
        });
    });

    document.querySelectorAll('.restart-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Restart this app?')) return;
            const username = btn.dataset.username;
            await fetch(`/restart/${username}`, { method: 'POST' });
            location.reload();
        });
    });

    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Delete this site permanently?')) return;
            const username = btn.dataset.username;
            await fetch(`/delete/${username}`, { method: 'POST' });
            location.reload();
        });
    });

    document.querySelectorAll('.log-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const username = btn.dataset.username;
            const res = await fetch(`/status/${username}`);
            const data = await res.json();
            alert(`📜 Logs for ${username}:\n\n${data.logs || 'No logs.'}`);
        });
    });
</script>
</body>
</html>
"""

# ---------- RUN ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🌐 HOSTING PANEL (Live Build Logs)")
    print("🔑 Login: admin / admin")
    print("📁 Uploads:", UPLOAD_FOLDER)
    print("📜 Logs:", LOGS_FOLDER)
    print("🚀 Running on port", port)
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)