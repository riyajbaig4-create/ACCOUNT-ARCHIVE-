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
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, session, jsonify, abort, Response

app = Flask(__name__)
app.secret_key = 'super-secret-key-12345'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
PROCESSES_FILE = os.path.join(BASE_DIR, 'processes.json')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)

# ---------- MAIN PANEL CREDENTIALS ----------
MAIN_USER = "admin"
MAIN_PASS = "admin"

# ---------- DATABASE FUNCTIONS ----------
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

def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
    os.remove(zip_path)

# ---------- PROCESS MANAGER ----------
processes = load_processes()
process_handles = {}

def start_user_app(username, folder):
    """Start the user's app in a subprocess and return the port."""
    # Check for Python files
    app_files = ['app.py', 'main.py', 'index.py', 'server.py']
    main_file = None
    for f in app_files:
        if os.path.exists(os.path.join(folder, f)):
            main_file = f
            break

    if not main_file:
        # Static website - serve directly, no process needed
        return None

    # Find free port
    port = find_free_port()
    
    # Start subprocess
    try:
        env = os.environ.copy()
        env['PORT'] = str(port)
        env['HOST'] = '0.0.0.0'
        
        proc = subprocess.Popen(
            ['python3', main_file],
            cwd=folder,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        
        # Save process info
        processes[username] = {
            'pid': proc.pid,
            'port': port,
            'file': main_file,
            'started_at': datetime.now().isoformat(),
            'status': 'running'
        }
        process_handles[username] = proc
        save_processes(processes)
        
        # Wait a moment for the server to start
        time.sleep(2)
        return port
    except Exception as e:
        print(f"Error starting app for {username}: {e}")
        return None

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
    return True

def is_app_running(username):
    return username in process_handles and process_handles[username].poll() is None

# ---------- REVERSE PROXY HANDLER ----------
def proxy_request(username, path, method, headers, data, query_string):
    if username not in processes:
        return None, None
    
    port = processes[username]['port']
    target_url = f"http://localhost:{port}/{path}"
    if query_string:
        target_url += "?" + query_string
    
    # Forward request to user's app
    try:
        # Remove host header to avoid confusion
        headers.pop('Host', None)
        headers.pop('Content-Length', None)
        
        # Forward the request
        resp = requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=data,
            allow_redirects=False,
            timeout=30
        )
        
        # Return the response
        return resp, None
    except requests.exceptions.ConnectionError:
        return None, "App not responding"
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

# ---------- UPLOAD ROUTE ----------
@app.route('/deploy', methods=['POST'])
def deploy():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    
    username = request.form.get('username', '').strip()
    if not username:
        username = str(uuid.uuid4())[:8]
    
    if username in users_db:
        return jsonify({'error': 'Username already taken. Choose another.'}), 400
    
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files uploaded.'}), 400
    
    # Stop any existing app for this user (should not happen)
    if username in processes:
        stop_user_app(username)
    
    user_folder = get_user_folder(username)
    # Clean folder
    shutil.rmtree(user_folder, ignore_errors=True)
    os.makedirs(user_folder)
    
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
    
    # Save user info
    users_db[username] = {
        'created_at': datetime.now().isoformat(),
        'folder': user_folder
    }
    save_users(users_db)
    
    # Start the app
    port = start_user_app(username, user_folder)
    
    link = request.host_url + username
    if port:
        status = f"✅ Running on port {port}"
    else:
        status = "📁 Static site (no server process)"
    
    return jsonify({
        'success': True,
        'username': username,
        'link': link,
        'status': status,
        'port': port
    })

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
    port = start_user_app(username, folder)
    return jsonify({'success': True, 'port': port})

@app.route('/delete/<username>', methods=['POST'])
def delete_app(username):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    if username not in users_db:
        return jsonify({'error': 'User not found'}), 404
    stop_user_app(username)
    shutil.rmtree(get_user_folder(username), ignore_errors=True)
    del users_db[username]
    save_users(users_db)
    return jsonify({'success': True})

# ---------- PROXY ROUTE (THE MAGIC) ----------
@app.route('/<username>/', defaults={'path': ''})
@app.route('/<username>/<path:path>')
def serve_user_site(username, path):
    # Check if user exists
    if username not in users_db:
        return "User site not found", 404
    
    # Check if app is running (has process)
    if username in processes and is_app_running(username):
        # PROXY EVERYTHING to the user's app
        method = request.method
        headers = dict(request.headers)
        data = request.get_data()
        query_string = request.query_string.decode('utf-8')
        
        resp, err = proxy_request(username, path, method, headers, data, query_string)
        if err:
            return f"Proxy error: {err}", 500
        if resp:
            # Forward the response back to the client
            response = Response(resp.content, resp.status_code)
            # Forward headers (except a few)
            exclude_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            for key, value in resp.headers.items():
                if key.lower() not in exclude_headers:
                    response.headers[key] = value
            return response
        else:
            return "App not reachable", 502
    else:
        # No process running - serve as static files
        user_folder = get_user_folder(username)
        if path == '':
            # Try index.html
            if os.path.exists(os.path.join(user_folder, 'index.html')):
                return send_from_directory(user_folder, 'index.html')
            else:
                # List files
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

# ---------- HTML TEMPLATES ----------
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
        .container { max-width: 900px; margin: 0 auto; }
        h1 { color: #00e5ff; }
        .card { background: #161b25; border: 1px solid #2b3240; border-radius: 15px; padding: 20px; margin: 20px 0; }
        .drop-zone { border: 2px dashed #00e5ff; border-radius: 15px; padding: 40px; text-align: center; cursor: pointer; margin: 10px 0; }
        .btn { background: #00e5ff; color: #000; border: none; padding: 12px 20px; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .btn-danger { background: #ff4d4d; color: #fff; }
        .btn-success { background: #00ff6a; color: #000; }
        .link { color: #00e5ff; word-break: break-all; }
        .app-item { background: #0c1018; padding: 15px; border-radius: 10px; margin: 10px 0; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        .status { padding: 4px 12px; border-radius: 20px; font-size: 12px; }
        .status.running { background: #00ff6a33; color: #00ff6a; border: 1px solid #00ff6a; }
        .status.static { background: #333; color: #aaa; border: 1px solid #555; }
        .logout { float: right; color: #ff4d4d; text-decoration: none; }
        .input-group { display: flex; gap: 10px; flex-wrap: wrap; margin: 10px 0; }
        .input-group input { flex: 1; padding: 12px; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; min-width: 150px; }
        #fileList { margin: 10px 0; color: #aaa; }
        #result { margin-top: 20px; padding: 15px; background: #0c1018; border-radius: 8px; border: 1px solid #2b3240; }
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
        <div id="result"></div>
    </div>

    <div class="card">
        <h3>📋 Deployed Sites</h3>
        <div id="siteList">
            {% for username, user in users.items() %}
                <div class="app-item" data-username="{{ username }}">
                    <div>
                        <strong>{{ username }}</strong><br />
                        <a href="{{ request.host_url + username }}" target="_blank" class="link">{{ request.host_url + username }}</a>
                        <span class="status {% if username in processes %}running{% else %}static{% endif %}">
                            {% if username in processes %}🟢 Running{% else %}📁 Static{% endif %}
                        </span>
                    </div>
                    <div>
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

<script>
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const deployBtn = document.getElementById('deployBtn');
    const resultDiv = document.getElementById('result');
    const usernameInput = document.getElementById('usernameInput');
    let selectedFiles = [];

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
        resultDiv.innerHTML = '<p>⏳ Uploading...</p>';

        try {
            const res = await fetch('/deploy', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                resultDiv.innerHTML = `
                    <p style="color:#00ff6a;">✅ Deployed successfully!</p>
                    <p><strong>Username:</strong> ${data.username}</p>
                    <p><strong>Link:</strong> <a href="${data.link}" target="_blank" class="link">${data.link}</a></p>
                    <p><strong>Status:</strong> ${data.status}</p>
                `;
                setTimeout(() => location.reload(), 2000);
            } else {
                resultDiv.innerHTML = `<p style="color:#ff4d4d;">❌ Error: ${data.error}</p>`;
            }
        } catch(e) {
            resultDiv.innerHTML = `<p style="color:#ff4d4d;">❌ Network Error: ${e.message}</p>`;
        }
        deployBtn.disabled = false;
        deployBtn.textContent = '🚀 Deploy';
    });

    // Action buttons
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
</script>
</body>
</html>
"""

# ---------- CLEANUP ON EXIT ----------
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
    print("🌐 ADVANCED WEBSITE HOSTING PANEL")
    print("🔑 Main Login: admin / admin")
    print("📁 Uploads folder:", UPLOAD_FOLDER)
    print("🚀 Running at http://0.0.0.0:5000")
    print("⚡ Supports Flask, Static HTML, and more!")
    print("="*60)
    app.run(host='0.0.0.0', port=5000, debug=True, threaded=True)