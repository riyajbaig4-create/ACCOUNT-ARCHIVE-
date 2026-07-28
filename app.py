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
import requests
import signal
import sys
import socket
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, session, jsonify, abort, Response, stream_with_context

app = Flask(__name__)
app.secret_key = 'super-secret-key-12345'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
PROCESSES_FILE = os.path.join(BASE_DIR, 'processes.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# ---------- MAIN PANEL CREDENTIALS ----------
MAIN_USER = "admin"
MAIN_PASS = "admin"

# ---------- DATABASE ----------
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

users_db = load_users()

def load_processes():
    if os.path.exists(PROCESSES_FILE):
        with open(PROCESSES_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_processes(procs):
    with open(PROCESSES_FILE, 'w') as f:
        json.dump(procs, f, indent=2)

# ---------- HELPERS ----------
def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_user_log_file(username):
    return os.path.join(LOGS_FOLDER, f"{username}.log")

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
    os.remove(zip_path)

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

# ---------- PROCESS MANAGER ----------
processes = load_processes()
process_handles = {}
status_callbacks = {}

def append_log(username, line):
    log_file = get_user_log_file(username)
    with open(log_file, 'a') as f:
        f.write(line + '\n')

def start_user_app(username, folder):
    # Detect main file
    app_files = ['app.py', 'main.py', 'server.py', 'index.py']
    main_file = None
    for f in app_files:
        if os.path.exists(os.path.join(folder, f)):
            main_file = f
            break

    if not main_file:
        # Static site
        processes[username] = {'status': 'static', 'port': None}
        save_processes(processes)
        return None, None

    # Install requirements if exists
    req_file = os.path.join(folder, 'requirements.txt')
    if os.path.exists(req_file):
        append_log(username, "📦 Installing dependencies from requirements.txt...")
        try:
            proc = subprocess.Popen(
                ['pip', 'install', '-r', 'requirements.txt'],
                cwd=folder,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True
            )
            for line in proc.stdout:
                append_log(username, line.strip())
            proc.wait()
            if proc.returncode != 0:
                append_log(username, "❌ Dependency installation failed.")
                processes[username] = {'status': 'failed', 'error': 'pip install failed'}
                save_processes(processes)
                return None, "pip install failed"
        except Exception as e:
            append_log(username, f"❌ Error installing dependencies: {e}")
            return None, str(e)

    # Find free port
    port = find_free_port()
    log_file = get_user_log_file(username)

    # Clear previous log
    with open(log_file, 'w') as f:
        f.write(f"--- Starting {main_file} at {datetime.now()} ---\n")

    # Start subprocess
    try:
        env = os.environ.copy()
        env['PORT'] = str(port)
        env['HOST'] = '0.0.0.0'

        with open(log_file, 'a') as log_f:
            proc = subprocess.Popen(
                ['python3', main_file],
                cwd=folder,
                stdout=log_f,
                stderr=log_f,
                env=env,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )

        processes[username] = {
            'pid': proc.pid,
            'port': port,
            'file': main_file,
            'started_at': datetime.now().isoformat(),
            'status': 'starting'
        }
        process_handles[username] = proc
        save_processes(processes)

        append_log(username, f"✅ Process started with PID {proc.pid} on port {port}")
        append_log(username, "⏳ Waiting for server to become ready...")

        # Wait for port to be ready
        if wait_for_port(port, timeout=30):
            processes[username]['status'] = 'running'
            save_processes(processes)
            append_log(username, "✅ Server is running and ready!")
            return port, None
        else:
            # Timeout - check if process is still alive
            if proc.poll() is None:
                # Still running but not listening? kill it
                try:
                    if os.name != 'nt':
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    else:
                        proc.terminate()
                except:
                    pass
                proc.wait(timeout=2)
            processes[username]['status'] = 'failed'
            save_processes(processes)
            append_log(username, "❌ Server failed to start: port not listening within 30s")
            return None, "Server failed to start (port not listening)"
    except Exception as e:
        processes[username] = {'status': 'error', 'error': str(e)}
        save_processes(processes)
        append_log(username, f"❌ Error: {e}")
        return None, str(e)

def stop_user_app(username):
    if username in process_handles:
        proc = process_handles[username]
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except:
            pass
        del process_handles[username]
    if username in processes:
        del processes[username]
        save_processes(processes)
    append_log(username, "🛑 App stopped.")
    return True

def is_app_running(username):
    return username in process_handles and process_handles[username].poll() is None

def get_log_content(username):
    log_file = get_user_log_file(username)
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            return f.read()
    return ""

# ---------- REVERSE PROXY ----------
def proxy_request(username, path, method, headers, data, query_string):
    if username not in processes:
        return None, "App not found"
    port = processes[username]['port']
    if port is None:
        return None, "No port assigned"
    target_url = f"http://localhost:{port}/{path}"
    if query_string:
        target_url += "?" + query_string

    try:
        headers.pop('Host', None)
        headers.pop('Content-Length', None)
        resp = requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=data,
            allow_redirects=False,
            timeout=30
        )
        return resp, None
    except requests.exceptions.ConnectionError:
        return None, "App not responding (Connection refused)"
    except Exception as e:
        return None, str(e)

# ---------- MAIN PANEL ROUTES ----------
@app.route('/', methods=['GET', 'POST'])
def main_login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        if username == MAIN_USER and password == MAIN_PASS:
            session['logged_in'] = True
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_HTML, error="Invalid credentials")
    if session.get('logged_in'):
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('main_login'))
    return render_template_string(DASHBOARD_HTML, users=users_db, processes=processes)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('main_login'))

# ---------- DEPLOY ----------
@app.route('/deploy', methods=['POST'])
def deploy():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    username = request.form.get('username', '').strip()
    if not username:
        username = str(uuid.uuid4())[:8]

    if username in users_db:
        return jsonify({'error': 'Username already taken.'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files uploaded.'}), 400

    if username in processes:
        stop_user_app(username)

    user_folder = get_user_folder(username)
    shutil.rmtree(user_folder, ignore_errors=True)
    os.makedirs(user_folder)

    # Clear log
    log_file = get_user_log_file(username)
    if os.path.exists(log_file):
        os.remove(log_file)

    # Save files
    for file in files:
        if file.filename == '':
            continue
        if file.filename.lower().endswith('.zip'):
            temp_path = os.path.join(user_folder, file.filename)
            file.save(temp_path)
            extract_zip(temp_path, user_folder)
        else:
            file.save(os.path.join(user_folder, file.filename))

    users_db[username] = {
        'created_at': datetime.now().isoformat(),
        'folder': user_folder
    }
    save_users(users_db)

    # Start app in background to allow streaming logs
    def run_deployment():
        start_user_app(username, user_folder)

    threading.Thread(target=run_deployment).start()

    # Return immediately with username so frontend can poll status
    return jsonify({
        'success': True,
        'username': username,
        'message': 'Deployment started. Check status.'
    })

# ---------- STATUS API (polling) ----------
@app.route('/status/<username>')
def status(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in users_db:
        return jsonify({'error': 'User not found'}), 404

    proc_info = processes.get(username, {})
    status = proc_info.get('status', 'unknown')
    port = proc_info.get('port')
    error = proc_info.get('error')
    logs = get_log_content(username)

    # Also check if process is still alive for 'running' status
    if status == 'running' and not is_app_running(username):
        status = 'failed'
        proc_info['status'] = 'failed'
        save_processes(processes)
        append_log(username, "❌ Process died unexpectedly.")

    return jsonify({
        'username': username,
        'status': status,
        'port': port,
        'error': error,
        'logs': logs,
        'link': request.host_url + username if status == 'running' else None
    })

# ---------- STOP / RESTART / DELETE ----------
@app.route('/stop/<username>', methods=['POST'])
def stop_app(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in users_db:
        return jsonify({'error': 'User not found'}), 404
    stop_user_app(username)
    return jsonify({'success': True})

@app.route('/restart/<username>', methods=['POST'])
def restart_app(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in users_db:
        return jsonify({'error': 'User not found'}), 404
    stop_user_app(username)
    folder = get_user_folder(username)
    threading.Thread(target=start_user_app, args=(username, folder)).start()
    return jsonify({'success': True, 'message': 'Restart initiated.'})

@app.route('/delete/<username>', methods=['POST'])
def delete_app(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in users_db:
        return jsonify({'error': 'User not found'}), 404
    stop_user_app(username)
    shutil.rmtree(get_user_folder(username), ignore_errors=True)
    log_file = get_user_log_file(username)
    if os.path.exists(log_file):
        os.remove(log_file)
    del users_db[username]
    save_users(users_db)
    return jsonify({'success': True})

# ---------- PROXY ROUTE ----------
@app.route('/<username>/', defaults={'path': ''})
@app.route('/<username>/<path:path>')
def serve_user_site(username, path):
    if username not in users_db:
        return "User site not found", 404

    proc_info = processes.get(username, {})
    if proc_info.get('status') == 'running' and is_app_running(username):
        method = request.method
        headers = dict(request.headers)
        data = request.get_data()
        query_string = request.query_string.decode('utf-8')

        resp, err = proxy_request(username, path, method, headers, data, query_string)
        if err:
            return f"Proxy error: {err}", 502
        if resp:
            response = Response(resp.content, resp.status_code)
            exclude_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            for key, value in resp.headers.items():
                if key.lower() not in exclude_headers:
                    response.headers[key] = value
            return response
        else:
            return "App not reachable", 502
    else:
        # Static or failed
        user_folder = get_user_folder(username)
        if path == '':
            if os.path.exists(os.path.join(user_folder, 'index.html')):
                return send_from_directory(user_folder, 'index.html')
            else:
                files = os.listdir(user_folder)
                file_list = "<ul>"
                for f in files:
                    file_list += f'<li><a href="{f}">{f}</a></li>'
                file_list += "</ul>"
                return f"<h2>📁 Files for {username}</h2>{file_list}"
        else:
            file_path = os.path.join(user_folder, path)
            if not os.path.exists(file_path):
                return "File not found", 404
            if os.path.isdir(file_path):
                return redirect(url_for('serve_user_site', username=username, path=path + '/'))
            return send_from_directory(user_folder, path)

# ---------- HTML (Dashboard with live logs) ----------
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Panel Login</title>
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
    <title>Dashboard</title>
    <style>
        body { background: #0c1018; color: #fff; font-family: Arial; padding: 20px; }
        .container { max-width: 1000px; margin: 0 auto; }
        h1 { color: #00e5ff; }
        .card { background: #161b25; border: 1px solid #2b3240; border-radius: 15px; padding: 20px; margin: 20px 0; }
        .drop-zone { border: 2px dashed #00e5ff; border-radius: 15px; padding: 40px; text-align: center; cursor: pointer; margin: 10px 0; }
        .btn { background: #00e5ff; color: #000; border: none; padding: 10px 18px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .btn-danger { background: #ff4d4d; color: #fff; }
        .btn-success { background: #00ff6a; color: #000; }
        .btn-info { background: #4d88ff; color: #fff; }
        .link { color: #00e5ff; word-break: break-all; }
        .app-item { background: #0c1018; padding: 15px; border-radius: 10px; margin: 10px 0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        .status { padding: 4px 12px; border-radius: 20px; font-size: 12px; }
        .status.running { background: #00ff6a33; color: #00ff6a; border: 1px solid #00ff6a; }
        .status.failed { background: #ff4d4d33; color: #ff4d4d; border: 1px solid #ff4d4d; }
        .status.starting { background: #ffaa0033; color: #ffaa00; border: 1px solid #ffaa00; }
        .status.static { background: #333; color: #aaa; border: 1px solid #555; }
        .logout { float: right; color: #ff4d4d; text-decoration: none; }
        .input-group { display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0; }
        .input-group input { flex: 1; padding: 12px; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; min-width: 150px; }
        #fileList { margin: 10px 0; color: #aaa; }
        #deployStatus { margin-top: 20px; padding: 15px; background: #0c1018; border-radius: 8px; border: 1px solid #2b3240; display: none; }
        #logOutput { background: #000; color: #00ff6a; padding: 10px; border-radius: 5px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 12px; white-space: pre-wrap; margin-top: 10px; }
        .btn-group { display: flex; gap: 5px; flex-wrap: wrap; }
        .log-popup { display: none; position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 9999; justify-content: center; align-items: center; }
        .log-popup-content { background: #0c1018; border: 1px solid #2b3240; border-radius: 15px; padding: 30px; max-width: 90%; max-height: 80%; overflow: auto; width: 800px; }
        .log-popup-content pre { background: #000; color: #00ff6a; padding: 15px; border-radius: 8px; font-size: 12px; max-height: 500px; overflow: auto; white-space: pre-wrap; }
        .close-btn { float: right; background: #ff4d4d; color: #fff; border: none; padding: 8px 16px; border-radius: 5px; cursor: pointer; }
        .spinner { border: 4px solid rgba(0, 229, 255, 0.1); border-left: 4px solid #00e5ff; border-radius: 50%; width: 30px; height: 30px; animation: spin 1s linear infinite; display: inline-block; margin-right: 10px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
    </style>
</head>
<body>
<div class="container">
    <h1>🚀 Hosting Panel <a href="/logout" class="logout">Logout</a></h1>
    
    <div class="card">
        <h3>📤 Upload New Website</h3>
        <div class="drop-zone" id="dropZone">
            <p>📂 Drag & drop files or click to browse</p>
            <p style="font-size:12px;color:#555;">Supports .zip, .html, .py, .js, .css</p>
        </div>
        <input type="file" id="fileInput" multiple style="display:none;">
        <div id="fileList"></div>
        <div class="input-group">
            <input type="text" id="usernameInput" placeholder="Username (leave blank for auto)" />
            <button class="btn" id="deployBtn">🚀 Deploy</button>
        </div>
        <div id="deployStatus">
            <h4>Deployment Logs</h4>
            <div id="logOutput"></div>
            <div id="deployResult" style="margin-top:10px;"></div>
        </div>
    </div>

    <div class="card">
        <h3>📋 Deployed Sites</h3>
        <div id="siteList">
            {% for username, user in users.items() %}
                <div class="app-item" data-username="{{ username }}">
                    <div>
                        <strong>{{ username }}</strong><br />
                        <a href="{{ request.host_url + username }}" target="_blank" class="link">{{ request.host_url + username }}</a>
                        <span class="status {% if username in processes %}{{ processes[username]['status'] }}{% else %}static{% endif %}">
                            {% if username in processes %}
                                {{ processes[username]['status'] }}
                            {% else %}
                                Static
                            {% endif %}
                        </span>
                    </div>
                    <div class="btn-group">
                        <button class="btn btn-info log-btn" data-username="{{ username }}">📜 Logs</button>
                        {% if username in processes %}
                            <button class="btn btn-danger restart-btn" data-username="{{ username }}">🔄 Restart</button>
                            <button class="btn btn-danger stop-btn" data-username="{{ username }}">⏹ Stop</button>
                        {% endif %}
                        <button class="btn btn-danger delete-btn" data-username="{{ username }}">🗑 Delete</button>
                    </div>
                </div>
            {% endfor %}
        </div>
    </div>
</div>

<!-- Log Popup -->
<div class="log-popup" id="logPopup">
    <div class="log-popup-content">
        <button class="close-btn" id="logCloseBtn">✕ Close</button>
        <h3>📜 Logs</h3>
        <pre id="logContent">Loading...</pre>
    </div>
</div>

<script>
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const deployBtn = document.getElementById('deployBtn');
    const deployStatus = document.getElementById('deployStatus');
    const logOutput = document.getElementById('logOutput');
    const deployResult = document.getElementById('deployResult');
    const usernameInput = document.getElementById('usernameInput');
    let selectedFiles = [];
    let pollInterval = null;

    dropZone.addEventListener('click', () => fileInput.click());
    dropZone.addEventListener('dragover', (e) => { e.preventDefault(); dropZone.style.borderColor = '#fff'; });
    dropZone.addEventListener('dragleave', () => dropZone.style.borderColor = '#00e5ff');
    dropZone.addEventListener('drop', (e) => {
        e.preventDefault();
        dropZone.style.borderColor = '#00e5ff';
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
        let html = '<strong>Selected:</strong><ul style="margin:5px 0;padding-left:20px;color:#aaa;">';
        selectedFiles.forEach(f => html += `<li>${f.name} (${(f.size/1024).toFixed(1)} KB)</li>`);
        html += '</ul>';
        fileList.innerHTML = html;
    }

    deployBtn.addEventListener('click', async () => {
        if (selectedFiles.length === 0) { alert('Select files first.'); return; }
        const username = usernameInput.value.trim();

        const formData = new FormData();
        selectedFiles.forEach(f => formData.append('files', f));
        formData.append('username', username);

        deployBtn.disabled = true;
        deployBtn.textContent = 'Deploying...';
        deployStatus.style.display = 'block';
        logOutput.textContent = '⏳ Uploading...';
        deployResult.innerHTML = '';

        try {
            const res = await fetch('/deploy', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                logOutput.textContent += '\n✅ Upload complete. Starting deployment...';
                // Start polling status
                const uname = data.username;
                let attempts = 0;
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(async () => {
                    try {
                        const statusRes = await fetch(`/status/${uname}`);
                        const statusData = await statusRes.json();
                        logOutput.textContent = statusData.logs || 'No logs yet.';
                        logOutput.scrollTop = logOutput.scrollHeight;

                        if (statusData.status === 'running') {
                            clearInterval(pollInterval);
                            deployResult.innerHTML = `
                                <p style="color:#00ff6a;">✅ Deployment successful!</p>
                                <p><strong>Username:</strong> ${statusData.username}</p>
                                <p><strong>Link:</strong> <a href="${statusData.link}" target="_blank" class="link">${statusData.link}</a></p>
                            `;
                            deployBtn.disabled = false;
                            deployBtn.textContent = '🚀 Deploy';
                            // Reload site list
                            setTimeout(() => location.reload(), 1500);
                        } else if (statusData.status === 'failed' || statusData.status === 'error') {
                            clearInterval(pollInterval);
                            deployResult.innerHTML = `<p style="color:#ff4d4d;">❌ Deployment failed. Check logs above for details.</p>`;
                            deployBtn.disabled = false;
                            deployBtn.textContent = '🚀 Deploy';
                        } else if (statusData.status === 'unknown' && attempts > 60) {
                            // timeout after 2 minutes
                            clearInterval(pollInterval);
                            deployResult.innerHTML = `<p style="color:#ffaa00;">⏳ Deployment taking too long. Check logs.</p>`;
                            deployBtn.disabled = false;
                            deployBtn.textContent = '🚀 Deploy';
                        }
                        attempts++;
                    } catch (e) {
                        console.error('Polling error:', e);
                    }
                }, 2000);
            } else {
                logOutput.textContent = '❌ Error: ' + data.error;
                deployBtn.disabled = false;
                deployBtn.textContent = '🚀 Deploy';
            }
        } catch(e) {
            logOutput.textContent = '❌ Network Error: ' + e.message;
            deployBtn.disabled = false;
            deployBtn.textContent = '🚀 Deploy';
        }
    });

    // Action buttons (stop, restart, delete)
    document.querySelectorAll('.stop-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Stop this app?')) return;
            const username = e.target.dataset.username;
            const res = await fetch(`/stop/${username}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });

    document.querySelectorAll('.restart-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Restart this app?')) return;
            const username = e.target.dataset.username;
            const res = await fetch(`/restart/${username}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });

    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            if (!confirm('Delete this site permanently?')) return;
            const username = e.target.dataset.username;
            const res = await fetch(`/delete/${username}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });

    // Log viewer
    const logPopup = document.getElementById('logPopup');
    const logContent = document.getElementById('logContent');
    const logCloseBtn = document.getElementById('logCloseBtn');

    document.querySelectorAll('.log-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const username = e.target.dataset.username;
            logContent.textContent = 'Loading...';
            logPopup.style.display = 'flex';
            try {
                const res = await fetch(`/status/${username}`);
                const data = await res.json();
                logContent.textContent = data.logs || 'No logs.';
            } catch(e) {
                logContent.textContent = 'Error loading logs.';
            }
        });
    });

    logCloseBtn.addEventListener('click', () => { logPopup.style.display = 'none'; });
    logPopup.addEventListener('click', (e) => { if (e.target === logPopup) logPopup.style.display = 'none'; });
</script>
</body>
</html>
"""

# ---------- CLEANUP ----------
def cleanup_processes():
    for username in list(process_handles.keys()):
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(process_handles[username].pid), signal.SIGTERM)
            else:
                process_handles[username].terminate()
        except:
            pass

import atexit
atexit.register(cleanup_processes)

# ---------- RUN ----------
if __name__ == '__main__':
    print("="*60)
    print("🌐 ADVANCED WEBSITE HOSTING PANEL (Live Logs)")
    print("🔑 Main Login: admin / admin")
    print("📁 Uploads folder:", UPLOAD_FOLDER)
    print("📜 Logs folder:", LOGS_FOLDER)
    print("🚀 Running at http://0.0.0.0:5000")
    print("⚡ Live deployment logs, auto-install requirements, auto-detect Python/Static")
    print("="*60)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)