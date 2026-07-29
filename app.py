import os
import sqlite3
import shutil
import zipfile
import subprocess
import threading
import time
import signal
import requests
import sys
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, abort
from werkzeug.utils import secure_filename
from werkzeug.middleware.proxy_fix import ProxyFix

# ------------------------------------------------------------
# App Configuration
# ------------------------------------------------------------
app = Flask(__name__)
app.secret_key = 'super-secret-key-change-in-production'
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'panel.db')

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# ------------------------------------------------------------
# Database Setup
# ------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS users (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    username TEXT UNIQUE,
                    password TEXT
                )''')
    c.execute('''CREATE TABLE IF NOT EXISTS projects (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT UNIQUE,
                    runtime TEXT,
                    status TEXT,
                    folder_path TEXT,
                    created_at TEXT,
                    port INTEGER,
                    pid INTEGER,
                    url TEXT
                )''')
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        c.execute("INSERT INTO users (username, password) VALUES ('admin', 'admin')")
    conn.commit()
    conn.close()

init_db()

# ------------------------------------------------------------
# Helper Functions
# ------------------------------------------------------------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_free_port(start=5001):
    import socket
    port = start
    while True:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(('127.0.0.1', port)) != 0:
                return port
            port += 1

def log_message(project_id, message):
    log_file = os.path.join(LOGS_FOLDER, f'{project_id}.log')
    timestamp = datetime.now().strftime('%H:%M:%S')
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f'{timestamp} {message}\n')

def update_project_status(project_id, status):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET status = ? WHERE id = ?", (status, project_id))
    conn.commit()
    conn.close()

def update_project_pid(project_id, pid):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET pid = ? WHERE id = ?", (pid, project_id))
    conn.commit()
    conn.close()

def update_project_port(project_id, port):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET port = ? WHERE id = ?", (port, project_id))
    conn.commit()
    conn.close()

def update_project_url(project_id, url):
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET url = ? WHERE id = ?", (url, project_id))
    conn.commit()
    conn.close()

def get_project(project_id):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE id = ?", (project_id,))
    row = c.fetchone()
    conn.close()
    return row

def get_all_projects():
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM projects ORDER BY created_at DESC")
    rows = c.fetchall()
    conn.close()
    return rows

def detect_runtime(folder_path):
    """Detect runtime: 'python' if requirements.txt and startup file exist, else 'html' if index.html exists."""
    files = os.listdir(folder_path)
    if 'requirements.txt' in files:
        startup_files = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
        for sf in startup_files:
            if sf in files:
                return 'python', sf
    if 'index.html' in files:
        return 'html', 'index.html'
    return None, None

def build_project(project_id):
    """Background thread to build and start the project."""
    project = get_project(project_id)
    if not project:
        return
    folder_path = project['folder_path']
    log_message(project_id, "Build started.")
    update_project_status(project_id, 'Building')

    runtime, startup_file = detect_runtime(folder_path)
    if not runtime:
        log_message(project_id, "ERROR: Runtime not detected.")
        update_project_status(project_id, 'Failed')
        return

    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET runtime = ? WHERE id = ?", (runtime, project_id))
    conn.commit()
    conn.close()

    log_message(project_id, f"Runtime detected: {runtime}")

    if runtime == 'python':
        # Install requirements
        requirements_path = os.path.join(folder_path, 'requirements.txt')
        if os.path.exists(requirements_path):
            log_message(project_id, "Installing requirements...")
            try:
                subprocess.run([sys.executable, '-m', 'pip', 'install', '-r', requirements_path],
                               cwd=folder_path, check=True, capture_output=True, text=True)
                log_message(project_id, "Requirements installed successfully.")
            except subprocess.CalledProcessError as e:
                log_message(project_id, f"ERROR: Requirements installation failed.\n{e.stderr}")
                update_project_status(project_id, 'Failed')
                return
        else:
            log_message(project_id, "No requirements.txt found, skipping.")

        # Start Python app
        port = get_free_port()
        log_message(project_id, f"Starting Python app on port {port}...")
        env = os.environ.copy()
        env['PORT'] = str(port)
        try:
            proc = subprocess.Popen([sys.executable, startup_file],
                                    cwd=folder_path, env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
            time.sleep(2)
            if proc.poll() is not None:
                out, _ = proc.communicate()
                log_message(project_id, f"ERROR: App failed to start.\n{out}")
                update_project_status(project_id, 'Failed')
                return
            update_project_port(project_id, port)
            update_project_pid(project_id, proc.pid)
            with app.app_context():
                base_url = request.host_url.rstrip('/')
                project_name = project['name']
                url = f"{base_url}/sites/{project_name}/"
                update_project_url(project_id, url)
            log_message(project_id, f"Website started successfully on {url}")
            update_project_status(project_id, 'Running')
            processes[project_id] = proc
        except Exception as e:
            log_message(project_id, f"ERROR: Failed to start app: {str(e)}")
            update_project_status(project_id, 'Failed')

    elif runtime == 'html':
        port = get_free_port()
        log_message(project_id, f"Starting static server on port {port}...")
        try:
            proc = subprocess.Popen([sys.executable, '-m', 'http.server', str(port)],
                                    cwd=folder_path,
                                    stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                    text=True)
            time.sleep(1)
            if proc.poll() is not None:
                out, _ = proc.communicate()
                log_message(project_id, f"ERROR: Static server failed to start.\n{out}")
                update_project_status(project_id, 'Failed')
                return
            update_project_port(project_id, port)
            update_project_pid(project_id, proc.pid)
            with app.app_context():
                base_url = request.host_url.rstrip('/')
                project_name = project['name']
                url = f"{base_url}/sites/{project_name}/"
                update_project_url(project_id, url)
            log_message(project_id, f"Website started successfully on {url}")
            update_project_status(project_id, 'Running')
            processes[project_id] = proc
        except Exception as e:
            log_message(project_id, f"ERROR: Failed to start static server: {str(e)}")
            update_project_status(project_id, 'Failed')

# ------------------------------------------------------------
# Process Management
# ------------------------------------------------------------
processes = {}  # project_id -> subprocess.Popen

def stop_process(project_id):
    if project_id in processes:
        proc = processes[project_id]
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=5)
        del processes[project_id]
    conn = get_db()
    c = conn.cursor()
    c.execute("UPDATE projects SET pid = 0, status = 'Stopped', url = '' WHERE id = ?", (project_id,))
    conn.commit()
    conn.close()
    log_message(project_id, "Process stopped.")

def delete_project(project_id):
    stop_process(project_id)
    project = get_project(project_id)
    if project:
        folder = project['folder_path']
        if os.path.exists(folder):
            shutil.rmtree(folder)
        log_file = os.path.join(LOGS_FOLDER, f'{project_id}.log')
        if os.path.exists(log_file):
            os.remove(log_file)
        conn = get_db()
        c = conn.cursor()
        c.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        conn.commit()
        conn.close()

def start_project(project_id):
    project = get_project(project_id)
    if not project:
        return
    log_file = os.path.join(LOGS_FOLDER, f'{project_id}.log')
    if os.path.exists(log_file):
        os.remove(log_file)
    update_project_status(project_id, 'Building')
    thread = threading.Thread(target=build_project, args=(project_id,))
    thread.daemon = True
    thread.start()

# ------------------------------------------------------------
# Routes - Login
# ------------------------------------------------------------
@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        conn = get_db()
        c = conn.cursor()
        c.execute("SELECT * FROM users WHERE username = ? AND password = ?", (username, password))
        user = c.fetchone()
        conn.close()
        if user:
            session['logged_in'] = True
            session['username'] = username
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error="Invalid Username or Password")
    return render_template_string(LOGIN_TEMPLATE, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

# ------------------------------------------------------------
# Routes - Dashboard
# ------------------------------------------------------------
@app.route('/')
@app.route('/dashboard')
def dashboard():
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    projects = get_all_projects()
    return render_template_string(DASHBOARD_TEMPLATE, projects=projects)

# ------------------------------------------------------------
# Routes - Upload
# ------------------------------------------------------------
@app.route('/upload', methods=['POST'])
def upload():
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401

    uploaded_files = request.files.getlist('files')
    if not uploaded_files:
        return jsonify({'error': 'No files uploaded'}), 400

    # Check for ZIP file
    zip_file = None
    other_files = []
    for f in uploaded_files:
        if f.filename.endswith('.zip'):
            zip_file = f
        else:
            other_files.append(f)

    # Determine project name
    if zip_file:
        base_name = os.path.splitext(zip_file.filename)[0]
        base_name = secure_filename(base_name)
    else:
        # Use first file's folder name if present, else base name
        first = uploaded_files[0]
        # For files from folder upload, filename contains relative path
        # e.g., "myproject/index.html" -> project name "myproject"
        if '/' in first.filename:
            base_name = first.filename.split('/')[0]
        elif '\\' in first.filename:
            base_name = first.filename.split('\\')[0]
        else:
            base_name = os.path.splitext(secure_filename(first.filename))[0]
        # Sanitize
        base_name = secure_filename(base_name)

    # Ensure unique name
    conn = get_db()
    c = conn.cursor()
    original_name = base_name
    counter = 1
    while True:
        c.execute("SELECT id FROM projects WHERE name = ?", (base_name,))
        if c.fetchone() is None:
            break
        base_name = f"{original_name}{counter}"
        counter += 1
    project_name = base_name

    project_folder = os.path.join(UPLOAD_FOLDER, project_name)
    os.makedirs(project_folder, exist_ok=True)

    if zip_file:
        zip_path = os.path.join(project_folder, zip_file.filename)
        zip_file.save(zip_path)
        try:
            with zipfile.ZipFile(zip_path, 'r') as zf:
                zf.extractall(project_folder)
            os.remove(zip_path)
        except zipfile.BadZipFile:
            shutil.rmtree(project_folder)
            return jsonify({'error': 'Invalid ZIP file'}), 400
    else:
        # Save files preserving directory structure
        for f in other_files:
            # Get relative path (filename may contain subdirectories)
            rel_path = f.filename
            # Normalize to use forward slashes
            rel_path = rel_path.replace('\\', '/')
            # Secure each part
            parts = rel_path.split('/')
            # Filter out empty parts and ensure each part is safe
            safe_parts = []
            for part in parts:
                if part in ('', '.', '..'):
                    continue
                safe_parts.append(secure_filename(part))
            if not safe_parts:
                continue
            # Rebuild path
            safe_path = os.path.join(*safe_parts)
            full_path = os.path.join(project_folder, safe_path)
            os.makedirs(os.path.dirname(full_path), exist_ok=True)
            f.save(full_path)

    created_at = datetime.now().isoformat()
    c.execute("""INSERT INTO projects (name, runtime, status, folder_path, created_at, port, pid, url)
                 VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
              (project_name, '', 'Uploading', project_folder, created_at, 0, 0, ''))
    project_id = c.lastrowid
    conn.commit()
    conn.close()

    log_message(project_id, "Upload completed. Starting build...")

    thread = threading.Thread(target=build_project, args=(project_id,))
    thread.daemon = True
    thread.start()

    return jsonify({'project_id': project_id, 'project_name': project_name})

# ------------------------------------------------------------
# Routes - Project Actions
# ------------------------------------------------------------
@app.route('/project/<int:project_id>/start', methods=['POST'])
def start_project_route(project_id):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if project['status'] in ('Running', 'Building'):
        return jsonify({'error': 'Project already running or building'}), 400
    start_project(project_id)
    return jsonify({'status': 'started'})

@app.route('/project/<int:project_id>/stop', methods=['POST'])
def stop_project_route(project_id):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Project not found'}), 404
    if project['status'] != 'Running':
        return jsonify({'error': 'Project is not running'}), 400
    stop_process(project_id)
    return jsonify({'status': 'stopped'})

@app.route('/project/<int:project_id>/delete', methods=['POST'])
def delete_project_route(project_id):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    delete_project(project_id)
    return jsonify({'status': 'deleted'})

@app.route('/project/<int:project_id>/logs')
def project_logs(project_id):
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    project = get_project(project_id)
    if not project:
        abort(404)
    log_file = os.path.join(LOGS_FOLDER, f'{project_id}.log')
    logs = []
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f:
            logs = f.readlines()
    return render_template_string(LOGS_TEMPLATE, project=project, logs=logs)

@app.route('/project/<int:project_id>/status')
def project_status(project_id):
    if not session.get('logged_in'):
        return jsonify({'error': 'Unauthorized'}), 401
    project = get_project(project_id)
    if not project:
        return jsonify({'error': 'Not found'}), 404
    # Check if process is still alive
    if project['pid'] and project['pid'] != 0 and project['status'] == 'Running':
        try:
            os.kill(project['pid'], 0)
        except OSError:
            # Process died
            update_project_status(project_id, 'Stopped')
            conn = get_db()
            c = conn.cursor()
            c.execute("UPDATE projects SET url = '' WHERE id = ?", (project_id,))
            conn.commit()
            conn.close()
            project = get_project(project_id)
    # Read logs
    log_file = os.path.join(LOGS_FOLDER, f'{project_id}.log')
    logs = []
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f:
            logs = f.readlines()
    return jsonify({
        'status': project['status'],
        'url': project['url'],
        'runtime': project['runtime'],
        'logs': logs[-20:]  # last 20 lines
    })

# ------------------------------------------------------------
# Routes - Reverse Proxy for Websites
# ------------------------------------------------------------
@app.route('/sites/<project_name>/', defaults={'path': ''})
@app.route('/sites/<project_name>/<path:path>')
def proxy_website(project_name, path):
    conn = get_db()
    c = conn.cursor()
    c.execute("SELECT * FROM projects WHERE name = ?", (project_name,))
    project = c.fetchone()
    conn.close()
    if not project:
        abort(404)
    if project['status'] != 'Running':
        return "Website is not running.", 503
    port = project['port']
    if not port:
        return "Port not assigned.", 500
    target_url = f"http://127.0.0.1:{port}/{path}"
    try:
        headers = {k: v for k, v in request.headers.items()}
        headers.pop('Host', None)
        resp = requests.request(
            method=request.method,
            url=target_url,
            headers=headers,
            data=request.get_data(),
            params=request.args,
            allow_redirects=False,
            stream=True
        )
        return (resp.content, resp.status_code, resp.headers.items())
    except requests.exceptions.ConnectionError:
        return "Backend server unreachable.", 502
    except Exception as e:
        return f"Proxy error: {str(e)}", 500

# ------------------------------------------------------------
# Templates
# ------------------------------------------------------------
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Website Hosting Panel - Login</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #fff; font-family: Arial, sans-serif; display: flex; justify-content: center; align-items: center; height: 100vh; }
        .login-box { width: 380px; padding: 40px 30px; border: 1px solid #e0e0e0; border-radius: 8px; box-shadow: 0 2px 8px rgba(0,0,0,0.05); text-align: center; }
        .login-box .logo { font-size: 48px; margin-bottom: 10px; }
        .login-box h1 { font-size: 24px; font-weight: normal; color: #000; margin-bottom: 4px; }
        .login-box .subtitle { font-size: 14px; color: #666; margin-bottom: 24px; }
        .login-box input[type="text"], .login-box input[type="password"] { width: 100%; padding: 10px 12px; margin-bottom: 16px; border: 1px solid #ccc; border-radius: 4px; font-size: 14px; }
        .login-box button { width: 100%; padding: 10px; background: #1a73e8; border: none; border-radius: 4px; color: #fff; font-size: 16px; cursor: pointer; }
        .login-box button:hover { background: #1557b0; }
        .error { color: #d32f2f; margin-top: 12px; font-size: 14px; }
    </style>
</head>
<body>
    <div class="login-box">
        <div class="logo">🖥️</div>
        <h1>Website Hosting Panel</h1>
        <div class="subtitle">Login to continue</div>
        <form method="POST" action="/login" id="loginForm">
            <input type="text" name="username" placeholder="Username" required>
            <input type="password" name="password" placeholder="Password" required>
            <button type="submit">Login</button>
            {% if error %}
            <div class="error">{{ error }}</div>
            {% endif %}
        </form>
    </div>
    <script>
        document.getElementById('loginForm').addEventListener('submit', function(e) {
            e.preventDefault();
            var form = e.target;
            var data = new FormData(form);
            fetch('/login', {
                method: 'POST',
                body: data
            }).then(response => {
                if (response.redirected) {
                    window.location.href = response.url;
                } else {
                    return response.text();
                }
            }).then(html => {
                document.documentElement.innerHTML = html;
            }).catch(err => {});
        });
    </script>
</body>
</html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Dashboard - Website Hosting Panel</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #fff; font-family: Arial, sans-serif; color: #000; }
        .navbar { background: #f8f9fa; border-bottom: 1px solid #e0e0e0; padding: 12px 24px; display: flex; justify-content: space-between; align-items: center; }
        .navbar .brand { font-size: 18px; font-weight: bold; }
        .navbar .logout a { color: #1a73e8; text-decoration: none; }
        .container { max-width: 1000px; margin: 30px auto; padding: 0 20px; }
        .upload-section { border: 2px dashed #ccc; border-radius: 8px; padding: 40px 20px; text-align: center; margin-bottom: 30px; background: #fafafa; }
        .upload-section .icon { font-size: 48px; }
        .upload-section .drag-text { font-size: 18px; margin: 10px 0; }
        .upload-section .or { color: #999; margin: 8px 0; }
        .upload-section .btn-choose { background: #1a73e8; color: #fff; border: none; padding: 10px 24px; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .upload-section .btn-choose:hover { background: #1557b0; }
        .upload-section .supported { margin-top: 12px; font-size: 12px; color: #888; }
        .upload-section .progress { margin-top: 15px; display: none; }
        .upload-section .progress .bar { width: 100%; height: 6px; background: #e0e0e0; border-radius: 3px; }
        .upload-section .progress .bar-inner { height: 100%; width: 0%; background: #1a73e8; border-radius: 3px; transition: width 0.2s; }
        .upload-section .progress .status-text { font-size: 14px; margin-top: 6px; color: #333; }
        .project-list { display: grid; grid-template-columns: repeat(auto-fill, minmax(280px, 1fr)); gap: 20px; }
        .project-card { border: 1px solid #e0e0e0; border-radius: 8px; padding: 16px; box-shadow: 0 1px 4px rgba(0,0,0,0.04); background: #fff; }
        .project-card .name { font-size: 18px; font-weight: bold; margin-bottom: 4px; }
        .project-card .runtime { font-size: 13px; color: #555; }
        .project-card .status { font-weight: bold; margin: 6px 0; }
        .project-card .status.running { color: #2e7d32; }
        .project-card .status.stopped { color: #c62828; }
        .project-card .status.building { color: #f57c00; }
        .project-card .status.failed { color: #b71c1c; }
        .project-card .created { font-size: 12px; color: #999; margin-top: 4px; }
        .project-card .link { margin: 6px 0; }
        .project-card .link a { color: #1a73e8; text-decoration: none; }
        .project-card .link a:hover { text-decoration: underline; }
        .project-card .actions { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
        .project-card .actions button { background: #f0f0f0; border: 1px solid #ccc; border-radius: 4px; padding: 6px 12px; cursor: pointer; font-size: 12px; }
        .project-card .actions button:hover { background: #e0e0e0; }
        .project-card .actions .start { background: #e8f5e9; color: #2e7d32; border-color: #a5d6a7; }
        .project-card .actions .start:hover { background: #c8e6c9; }
        .project-card .actions .stop { background: #ffebee; color: #c62828; border-color: #ef9a9a; }
        .project-card .actions .stop:hover { background: #ffcdd2; }
        .project-card .actions .delete { background: #fff3e0; color: #e65100; border-color: #ffcc80; }
        .project-card .actions .delete:hover { background: #ffe0b2; }
        .project-card .actions .logs { background: #e3f2fd; color: #0d47a1; border-color: #90caf9; }
        .project-card .actions .logs:hover { background: #bbdefb; }
        .project-card .actions button:disabled { opacity: 0.5; cursor: not-allowed; }
        @media (max-width: 600px) {
            .container { padding: 0 12px; }
            .project-list { grid-template-columns: 1fr; }
            .navbar { flex-direction: column; align-items: flex-start; gap: 8px; }
        }
    </style>
</head>
<body>
    <nav class="navbar">
        <div class="brand">🖥️ Website Hosting Panel</div>
        <div class="logout"><a href="/logout">Logout</a></div>
    </nav>

    <div class="container">
        <!-- Upload Section -->
        <div class="upload-section" id="uploadArea">
            <div class="icon">📂</div>
            <div class="drag-text">Drag & Drop Website Here</div>
            <div class="or">or</div>
            <input type="file" id="fileInput" multiple style="display:none;" accept=".zip,.html,.css,.js,.py,.txt">
            <button class="btn-choose" onclick="document.getElementById('fileInput').click();">Choose Files</button>
            <div class="supported">Supported: ZIP, Python Project, HTML Website</div>
            <div class="progress" id="uploadProgress">
                <div class="bar"><div class="bar-inner" id="progressBar"></div></div>
                <div class="status-text" id="uploadStatus">Uploading...</div>
            </div>
        </div>

        <!-- Project List -->
        <div class="project-list" id="projectList">
            {% for project in projects %}
            <div class="project-card" data-id="{{ project.id }}">
                <div class="name">{{ project.name }}</div>
                <div class="runtime">{{ project.runtime or 'Pending' }}</div>
                <div class="status {{ project.status.lower() }}">{{ project.status }}</div>
                <div class="created">Created: {{ project.created_at[:16].replace('T', ' ') }}</div>
                <div class="link">
                    {% if project.status == 'Running' and project.url %}
                    <a href="{{ project.url }}" target="_blank">🔗 Open Website</a>
                    {% endif %}
                </div>
                <div class="actions">
                    <button class="start" data-id="{{ project.id }}" {% if project.status in ['Running','Building'] %}disabled{% endif %}>Start</button>
                    <button class="stop" data-id="{{ project.id }}" {% if project.status != 'Running' %}disabled{% endif %}>Stop</button>
                    <button class="logs" data-id="{{ project.id }}">Build Logs</button>
                    <button class="delete" data-id="{{ project.id }}">Delete</button>
                </div>
            </div>
            {% endfor %}
        </div>
    </div>

    <script>
        const fileInput = document.getElementById('fileInput');
        const uploadArea = document.getElementById('uploadArea');
        const progressDiv = document.getElementById('uploadProgress');
        const progressBar = document.getElementById('progressBar');
        const statusText = document.getElementById('uploadStatus');

        function uploadFiles(files) {
            if (!files.length) return;
            const formData = new FormData();
            for (let f of files) {
                formData.append('files', f);
            }
            progressDiv.style.display = 'block';
            progressBar.style.width = '0%';
            statusText.textContent = 'Uploading...';

            fetch('/upload', {
                method: 'POST',
                body: formData
            })
            .then(response => response.json())
            .then(data => {
                if (data.error) {
                    statusText.textContent = 'Error: ' + data.error;
                    return;
                }
                statusText.textContent = 'Upload complete. Building...';
                progressBar.style.width = '100%';
                const projectId = data.project_id;
                pollStatus(projectId);
            })
            .catch(err => {
                statusText.textContent = 'Upload failed: ' + err.message;
            });
        }

        fileInput.addEventListener('change', function(e) {
            uploadFiles(this.files);
            this.value = '';
        });

        uploadArea.addEventListener('dragover', function(e) {
            e.preventDefault();
            this.style.borderColor = '#1a73e8';
        });
        uploadArea.addEventListener('dragleave', function(e) {
            e.preventDefault();
            this.style.borderColor = '#ccc';
        });
        uploadArea.addEventListener('drop', function(e) {
            e.preventDefault();
            this.style.borderColor = '#ccc';
            const files = e.dataTransfer.files;
            uploadFiles(files);
        });

        function pollStatus(projectId) {
            const interval = setInterval(() => {
                fetch(`/project/${projectId}/status`)
                .then(res => res.json())
                .then(data => {
                    const status = data.status;
                    statusText.textContent = 'Status: ' + status;
                    if (['Running', 'Failed', 'Stopped'].includes(status)) {
                        clearInterval(interval);
                        setTimeout(() => location.reload(), 1000);
                    }
                })
                .catch(err => {});
            }, 1500);
        }

        document.getElementById('projectList').addEventListener('click', function(e) {
            const target = e.target.closest('button');
            if (!target) return;
            const projectId = target.dataset.id;
            if (!projectId) return;

            if (target.classList.contains('start')) {
                fetch(`/project/${projectId}/start`, { method: 'POST' })
                .then(res => res.json())
                .then(data => { if (data.status === 'started') location.reload(); });
            } else if (target.classList.contains('stop')) {
                fetch(`/project/${projectId}/stop`, { method: 'POST' })
                .then(res => res.json())
                .then(data => { if (data.status === 'stopped') location.reload(); });
            } else if (target.classList.contains('delete')) {
                if (confirm('Delete Website?')) {
                    fetch(`/project/${projectId}/delete`, { method: 'POST' })
                    .then(res => res.json())
                    .then(data => { if (data.status === 'deleted') location.reload(); });
                }
            } else if (target.classList.contains('logs')) {
                window.location.href = `/project/${projectId}/logs`;
            }
        });

        // Poll for building projects on load
        document.querySelectorAll('.project-card').forEach(card => {
            const statusEl = card.querySelector('.status');
            if (statusEl && statusEl.textContent.trim() === 'Building') {
                const id = card.dataset.id;
                pollStatus(id);
            }
        });
    </script>
</body>
</html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Build Logs - {{ project.name }}</title>
    <style>
        * { box-sizing: border-box; margin: 0; padding: 0; }
        body { background: #fff; font-family: 'Courier New', monospace; padding: 20px; }
        .header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; flex-wrap: wrap; gap: 10px; }
        .header h2 { font-weight: normal; }
        .header .actions button { background: #f0f0f0; border: 1px solid #ccc; border-radius: 4px; padding: 6px 16px; cursor: pointer; }
        .header .actions button:hover { background: #e0e0e0; }
        .log-container { background: #f9f9f9; border: 1px solid #ddd; border-radius: 4px; padding: 12px; max-height: 600px; overflow-y: auto; white-space: pre-wrap; word-wrap: break-word; font-size: 13px; line-height: 1.6; }
        .log-container .log-line { border-bottom: 1px solid #eee; padding: 2px 0; }
        .back-link { margin-top: 16px; display: inline-block; color: #1a73e8; text-decoration: none; }
        .back-link:hover { text-decoration: underline; }
    </style>
</head>
<body>
    <div class="header">
        <h2>Build Logs - {{ project.name }}</h2>
        <div class="actions">
            <button onclick="location.reload()">Refresh</button>
            <button onclick="window.close()">Close</button>
        </div>
    </div>
    <div class="log-container" id="logContainer">
        {% for line in logs %}
        <div class="log-line">{{ line }}</div>
        {% endfor %}
    </div>
    <a href="/dashboard" class="back-link">← Back to Dashboard</a>
    <script>
        const container = document.getElementById('logContainer');
        container.scrollTop = container.scrollHeight;
    </script>
</body>
</html>
"""

# ------------------------------------------------------------
# Main
# ------------------------------------------------------------
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000, debug=False, threaded=True)