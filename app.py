#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import json
import sqlite3
import uuid
import shutil
import zipfile
import subprocess
import threading
import time
import signal
import socket
import requests
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, session, jsonify, send_from_directory, Response, abort

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'change-this-secret-key-in-production')

# ---------- CONFIG ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOGS_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(LOGS_FOLDER, exist_ok=True)

# ---------- DATABASE ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS websites (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                slug TEXT UNIQUE NOT NULL,
                runtime TEXT,
                status TEXT DEFAULT 'stopped',
                port INTEGER,
                pid INTEGER,
                upload_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                folder_path TEXT
            )
        ''')
        conn.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                website_id INTEGER,
                log_text TEXT,
                timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (website_id) REFERENCES websites(id)
            )
        ''')
        # insert default admin if not exists
        cur = conn.execute("SELECT * FROM users WHERE username='admin'")
        if not cur.fetchone():
            conn.execute("INSERT INTO users (username, password) VALUES ('admin', 'admin')")
        conn.commit()
init_db()

# ---------- HELPERS ----------
def get_user_folder(slug):
    folder = os.path.join(UPLOAD_FOLDER, slug)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_log_file(slug):
    return os.path.join(LOGS_FOLDER, f"{slug}.log")

def write_log(slug, text):
    log_file = get_log_file(slug)
    with open(log_file, 'a', encoding='utf-8') as f:
        f.write(f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] {text}\n")

def read_log(slug):
    log_file = get_log_file(slug)
    if os.path.exists(log_file):
        with open(log_file, 'r', encoding='utf-8') as f:
            return f.read()
    return ""

def find_free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def is_port_open(port, timeout=5):
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(timeout)
            s.connect(('localhost', port))
            return True
    except:
        return False

# ---------- PROCESS MANAGEMENT ----------
processes = {}  # slug -> subprocess.Popen

def start_website_process(slug, port):
    folder = get_user_folder(slug)
    runtime = detect_runtime(folder)
    if not runtime:
        return None, "No runtime detected"

    if runtime == 'python':
        start_file = detect_python_start_file(folder)
        if not start_file:
            return None, "No Python start file found"
        # install requirements
        req_file = os.path.join(folder, 'requirements.txt')
        if os.path.exists(req_file):
            write_log(slug, "📦 Installing requirements...")
            try:
                proc = subprocess.Popen(
                    ['pip', 'install', '-r', 'requirements.txt'],
                    cwd=folder,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    text=True
                )
                for line in proc.stdout:
                    write_log(slug, line.strip())
                proc.wait()
                if proc.returncode != 0:
                    write_log(slug, "❌ pip install failed")
                    return None, "pip install failed"
                write_log(slug, "✅ Requirements installed")
            except Exception as e:
                write_log(slug, f"❌ pip install error: {e}")
                return None, str(e)

        env = os.environ.copy()
        env['PORT'] = str(port)
        env['HOST'] = '0.0.0.0'
        log_file = get_log_file(slug)
        try:
            proc = subprocess.Popen(
                ['python3', start_file],
                cwd=folder,
                stdout=open(log_file, 'a'),
                stderr=subprocess.STDOUT,
                env=env,
                preexec_fn=os.setsid if os.name != 'nt' else None
            )
            processes[slug] = proc
            write_log(slug, f"✅ Process started with PID {proc.pid} on port {port}")
            # wait for port to be open
            for _ in range(30):
                if is_port_open(port):
                    write_log(slug, "✅ Server is ready")
                    return proc.pid, None
                time.sleep(1)
            # timeout
            write_log(slug, "❌ Timeout waiting for server to start")
            stop_website_process(slug)
            return None, "Timeout waiting for server"
        except Exception as e:
            write_log(slug, f"❌ Error starting process: {e}")
            return None, str(e)

    elif runtime == 'static':
        # no process needed, just serve static
        write_log(slug, "📁 Static website (no process)")
        return 0, None  # pid = 0 means static

    return None, "Unsupported runtime"

def stop_website_process(slug):
    if slug in processes:
        proc = processes[slug]
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
            proc.wait(timeout=5)
        except:
            pass
        del processes[slug]
    write_log(slug, "🛑 Process stopped")
    return True

def detect_runtime(folder):
    if os.path.exists(os.path.join(folder, 'index.html')):
        return 'static'
    # check for python files
    python_files = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
    for f in python_files:
        if os.path.exists(os.path.join(folder, f)):
            return 'python'
    return None

def detect_python_start_file(folder):
    python_files = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
    for f in python_files:
        if os.path.exists(os.path.join(folder, f)):
            return f
    return None

# ---------- ROUTES ----------
@app.route('/', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        with get_db() as conn:
            cur = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (username, password))
            user = cur.fetchone()
            if user:
                session['user_id'] = user['id']
                session['username'] = user['username']
                return redirect('/dashboard')
        return render_template_string(LOGIN_HTML, error="Invalid credentials")
    if 'user_id' in session:
        return redirect('/dashboard')
    return render_template_string(LOGIN_HTML, error=None)

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect('/')
    with get_db() as conn:
        websites = conn.execute("SELECT * FROM websites ORDER BY upload_date DESC").fetchall()
    return render_template_string(DASHBOARD_HTML, websites=websites, host_url=request.host_url)

@app.route('/logout')
def logout():
    session.clear()
    return redirect('/')

# ---------- UPLOAD ----------
@app.route('/upload', methods=['POST'])
def upload():
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401

    if 'files' not in request.files:
        return jsonify({'error': 'No files'}), 400

    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files selected'}), 400

    # generate slug
    slug = request.form.get('slug', '').strip()
    if not slug:
        slug = str(uuid.uuid4())[:8]
    else:
        # sanitize
        slug = ''.join(c for c in slug if c.isalnum() or c in '-_')
        if not slug:
            slug = str(uuid.uuid4())[:8]

    # check if slug exists
    with get_db() as conn:
        cur = conn.execute("SELECT id FROM websites WHERE slug=?", (slug,))
        if cur.fetchone():
            return jsonify({'error': 'Slug already taken'}), 400

    # create folder
    folder = get_user_folder(slug)
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)

    # clear log
    log_file = get_log_file(slug)
    if os.path.exists(log_file):
        os.remove(log_file)

    write_log(slug, "📤 Upload started")

    # save files
    for file in files:
        if file.filename == '':
            continue
        if file.filename.lower().endswith('.zip'):
            temp_path = os.path.join(folder, file.filename)
            file.save(temp_path)
            write_log(slug, f"📦 Extracting {file.filename}...")
            try:
                with zipfile.ZipFile(temp_path, 'r') as z:
                    z.extractall(folder)
                os.remove(temp_path)
                write_log(slug, "✅ ZIP extracted")
            except Exception as e:
                write_log(slug, f"❌ ZIP extract failed: {e}")
                shutil.rmtree(folder, ignore_errors=True)
                return jsonify({'error': f'ZIP extract failed: {e}'}), 400
        else:
            file.save(os.path.join(folder, file.filename))

    # detect runtime
    runtime = detect_runtime(folder)
    if not runtime:
        write_log(slug, "❌ No index.html or Python file found")
        shutil.rmtree(folder, ignore_errors=True)
        return jsonify({'error': 'No index.html or Python file found'}), 400

    write_log(slug, f"🔍 Runtime detected: {runtime}")

    # insert into DB
    with get_db() as conn:
        cur = conn.execute(
            "INSERT INTO websites (name, slug, runtime, status, folder_path) VALUES (?, ?, ?, ?, ?)",
            (slug, slug, runtime, 'building', folder)
        )
        website_id = cur.lastrowid
        conn.commit()

    # start deployment in background
    def deploy():
        try:
            if runtime == 'python':
                write_log(slug, "🐍 Python detected")
                port = find_free_port()
                write_log(slug, f"🔌 Using port {port}")
                pid, error = start_website_process(slug, port)
                if pid is None:
                    write_log(slug, f"❌ Start failed: {error}")
                    with get_db() as conn:
                        conn.execute("UPDATE websites SET status='failed', port=?, pid=? WHERE id=?", (port, None, website_id))
                        conn.commit()
                    return
                # update DB
                with get_db() as conn:
                    conn.execute("UPDATE websites SET status='running', port=?, pid=?, folder_path=? WHERE id=?",
                                 (port, pid, folder, website_id))
                    conn.commit()
                write_log(slug, f"✅ Website started successfully!")
                write_log(slug, f"🔗 URL: {request.host_url}{slug}/")
            elif runtime == 'static':
                write_log(slug, "📁 Static website")
                with get_db() as conn:
                    conn.execute("UPDATE websites SET status='running', port=0, pid=0 WHERE id=?", (website_id,))
                    conn.commit()
                write_log(slug, "✅ Static site ready")
                write_log(slug, f"🔗 URL: {request.host_url}{slug}/")
        except Exception as e:
            write_log(slug, f"❌ Deployment error: {e}")
            with get_db() as conn:
                conn.execute("UPDATE websites SET status='failed' WHERE id=?", (website_id,))
                conn.commit()

    threading.Thread(target=deploy, daemon=True).start()

    # return slug so frontend can poll
    return jsonify({'success': True, 'slug': slug, 'message': 'Deployment started'})

# ---------- STATUS / LOGS ----------
@app.route('/status/<slug>')
def status(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        website = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
        if not website:
            return jsonify({'error': 'Not found'}), 404
    return jsonify({
        'status': website['status'],
        'runtime': website['runtime'],
        'port': website['port'],
        'pid': website['pid'],
        'logs': read_log(slug),
        'url': f"{request.host_url}{slug}/" if website['status'] == 'running' else None
    })

@app.route('/logs/<slug>')
def logs(slug):
    if 'user_id' not in session:
        return abort(401)
    return jsonify({'logs': read_log(slug)})

# ---------- START / STOP / DELETE ----------
@app.route('/start/<slug>', methods=['POST'])
def start_website(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        website = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
        if not website:
            return jsonify({'error': 'Not found'}), 404
        if website['status'] == 'running':
            return jsonify({'error': 'Already running'}), 400

        # if static, just mark running
        if website['runtime'] == 'static':
            conn.execute("UPDATE websites SET status='running' WHERE id=?", (website['id'],))
            conn.commit()
            return jsonify({'success': True})

        # python: start process
        folder = website['folder_path']
        port = find_free_port()
        write_log(slug, f"🔄 Starting manually...")
        pid, error = start_website_process(slug, port)
        if pid is None:
            write_log(slug, f"❌ Start failed: {error}")
            conn.execute("UPDATE websites SET status='failed', port=?, pid=? WHERE id=?", (port, None, website['id']))
            conn.commit()
            return jsonify({'error': error}), 500
        conn.execute("UPDATE websites SET status='running', port=?, pid=?, folder_path=? WHERE id=?",
                     (port, pid, folder, website['id']))
        conn.commit()
        write_log(slug, f"✅ Website started")
        return jsonify({'success': True})

@app.route('/stop/<slug>', methods=['POST'])
def stop_website(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        website = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
        if not website:
            return jsonify({'error': 'Not found'}), 404
        if website['status'] != 'running':
            return jsonify({'error': 'Not running'}), 400

        if website['runtime'] == 'python':
            stop_website_process(slug)
        # update status
        conn.execute("UPDATE websites SET status='stopped', port=0, pid=0 WHERE id=?", (website['id'],))
        conn.commit()
        write_log(slug, "⏹ Stopped manually")
        return jsonify({'success': True})

@app.route('/delete/<slug>', methods=['POST'])
def delete_website(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    with get_db() as conn:
        website = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
        if not website:
            return jsonify({'error': 'Not found'}), 404

        # stop if running
        if website['status'] == 'running':
            stop_website_process(slug)

        # delete files
        folder = website['folder_path']
        if os.path.exists(folder):
            shutil.rmtree(folder, ignore_errors=True)
        log_file = get_log_file(slug)
        if os.path.exists(log_file):
            os.remove(log_file)

        # delete DB record
        conn.execute("DELETE FROM websites WHERE id=?", (website['id'],))
        conn.commit()
        return jsonify({'success': True})

# ---------- STATIC / PROXY ----------
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def serve_site(slug, path):
    # check if exists
    with get_db() as conn:
        website = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
        if not website:
            return "Website not found", 404

    # if running python, proxy
    if website['runtime'] == 'python' and website['status'] == 'running':
        port = website['port']
        if not port:
            return "Port not assigned", 500
        target = f"http://localhost:{port}/{path}"
        if request.query_string:
            target += "?" + request.query_string.decode('utf-8')
        try:
            headers = {k: v for k, v in request.headers.items() if k.lower() not in ['host', 'content-length']}
            resp = requests.request(
                method=request.method,
                url=target,
                headers=headers,
                data=request.get_data(),
                allow_redirects=False,
                timeout=30
            )
            # forward response
            response = Response(resp.content, resp.status_code)
            for k, v in resp.headers.items():
                if k.lower() not in ['content-encoding', 'content-length', 'transfer-encoding', 'connection']:
                    response.headers[k] = v
            return response
        except Exception as e:
            return f"Proxy error: {e}", 502

    # static or stopped
    folder = website['folder_path']
    if not path:
        if os.path.exists(os.path.join(folder, 'index.html')):
            return send_from_directory(folder, 'index.html')
        # list files
        if os.path.isdir(folder):
            files = os.listdir(folder)
            html = f"<h2>Files for {slug}</h2><ul>"
            for f in files:
                html += f'<li><a href="{f}">{f}</a></li>'
            html += "</ul>"
            return html
        return "No index.html", 404
    else:
        return send_from_directory(folder, path)

# ---------- HTML TEMPLATES ----------
LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Hosting Panel - Login</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: white; padding: 40px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 350px; }
        h2 { text-align: center; margin-bottom: 30px; color: #333; }
        input { width: 100%; padding: 12px; margin: 8px 0; border: 1px solid #ddd; border-radius: 4px; box-sizing: border-box; }
        button { width: 100%; padding: 12px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; font-size: 16px; }
        button:hover { background: #0056b3; }
        .error { color: red; text-align: center; margin: 10px 0; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>🔐 Login</h2>
        <form method="POST">
            <input type="text" name="username" placeholder="Username" value="admin" required>
            <input type="password" name="password" placeholder="Password" value="admin" required>
            <button type="submit">Login</button>
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
    <title>Hosting Panel - Dashboard</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f5f5f5; margin: 0; padding: 20px; }
        .container { max-width: 1200px; margin: 0 auto; }
        header { background: white; padding: 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); margin-bottom: 20px; display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; }
        .btn { padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; font-size: 14px; }
        .btn-primary { background: #007bff; color: white; }
        .btn-success { background: #28a745; color: white; }
        .btn-danger { background: #dc3545; color: white; }
        .btn-warning { background: #ffc107; color: #333; }
        .btn-secondary { background: #6c757d; color: white; }
        .btn-sm { padding: 4px 10px; font-size: 12px; }
        .card { background: white; border-radius: 8px; padding: 20px; margin-bottom: 20px; box-shadow: 0 2px 4px rgba(0,0,0,0.1); }
        .upload-area { border: 2px dashed #ccc; padding: 30px; text-align: center; border-radius: 8px; cursor: pointer; transition: 0.3s; }
        .upload-area.dragover { border-color: #007bff; background: #e9f5ff; }
        .website-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(300px, 1fr)); gap: 20px; }
        .website-card { background: white; border-radius: 8px; padding: 16px; box-shadow: 0 2px 4px rgba(0,0,0,0.08); border-left: 4px solid #007bff; }
        .website-card .name { font-size: 18px; font-weight: bold; margin-bottom: 8px; }
        .website-card .status { display: inline-block; padding: 2px 10px; border-radius: 12px; font-size: 12px; font-weight: bold; }
        .status-running { background: #d4edda; color: #155724; }
        .status-stopped { background: #e2e3e5; color: #383d41; }
        .status-building { background: #fff3cd; color: #856404; }
        .status-failed { background: #f8d7da; color: #721c24; }
        .website-card .actions { margin-top: 12px; display: flex; gap: 5px; flex-wrap: wrap; }
        .website-card .info { font-size: 14px; color: #555; margin: 4px 0; }
        .website-card .link { color: #007bff; text-decoration: none; }
        .website-card .link:hover { text-decoration: underline; }
        .file-list { margin: 10px 0; }
        #buildLogs { background: #1e1e1e; color: #d4d4d4; padding: 12px; border-radius: 4px; max-height: 300px; overflow-y: auto; font-family: monospace; font-size: 13px; margin-top: 10px; display: none; }
        .log-line { margin: 2px 0; }
        .log-error { color: #f48771; }
        .log-success { color: #6a9955; }
        .log-info { color: #569cd6; }
        .log-warn { color: #dcdcaa; }
        .upload-controls { display: flex; gap: 10px; flex-wrap: wrap; margin: 15px 0; }
        .upload-controls input[type="text"] { flex: 1; padding: 8px; border: 1px solid #ccc; border-radius: 4px; min-width: 150px; }
        .file-list-item { display: inline-block; background: #eee; padding: 4px 10px; margin: 4px; border-radius: 12px; font-size: 13px; }
        @media (max-width: 600px) { .website-grid { grid-template-columns: 1fr; } }
    </style>
</head>
<body>
<div class="container">
    <header>
        <h2>🚀 Hosting Panel</h2>
        <div>
            <span style="margin-right: 15px;">👤 {{ session.username }}</span>
            <a href="/logout" class="btn btn-danger">Logout</a>
        </div>
    </header>

    <!-- Upload Section -->
    <div class="card">
        <h3>📤 Upload Website</h3>
        <div class="upload-area" id="dropZone">
            <p style="font-size: 24px;">📂</p>
            <p>Drag & drop ZIP or files here, or click to browse</p>
            <p style="font-size: 12px; color: #777;">Supports .zip, .html, .py, .css, .js, etc.</p>
        </div>
        <input type="file" id="fileInput" multiple style="display:none;">
        <div id="fileList" class="file-list"></div>
        <div class="upload-controls">
            <input type="text" id="slugInput" placeholder="Custom slug (optional)">
            <button class="btn btn-primary" id="deployBtn">🚀 Deploy</button>
        </div>
        <div id="buildLogs"></div>
        <div id="deployResult" style="margin-top: 10px;"></div>
    </div>

    <!-- Website List -->
    <div class="card">
        <h3>📋 Deployed Websites</h3>
        <div class="website-grid" id="websiteGrid">
            {% for website in websites %}
                <div class="website-card" data-slug="{{ website.slug }}">
                    <div class="name">{{ website.name }}</div>
                    <div class="info">Runtime: {{ website.runtime }}</div>
                    <div class="info">
                        Status: <span class="status status-{{ website.status }}">{{ website.status }}</span>
                    </div>
                    <div class="info">Uploaded: {{ website.upload_date }}</div>
                    <div class="info">
                        Link: 
                        {% if website.status == 'running' %}
                            <a href="{{ host_url }}{{ website.slug }}/" target="_blank" class="link">{{ host_url }}{{ website.slug }}/</a>
                        {% else %}
                            <span style="color: #999;">Not running</span>
                        {% endif %}
                    </div>
                    <div class="actions">
                        <button class="btn btn-success btn-sm start-btn" data-slug="{{ website.slug }}">▶ Start</button>
                        <button class="btn btn-warning btn-sm stop-btn" data-slug="{{ website.slug }}">⏹ Stop</button>
                        <button class="btn btn-secondary btn-sm logs-btn" data-slug="{{ website.slug }}">📜 Logs</button>
                        <button class="btn btn-danger btn-sm delete-btn" data-slug="{{ website.slug }}">🗑 Delete</button>
                    </div>
                </div>
            {% else %}
                <p>No websites deployed yet.</p>
            {% endfor %}
        </div>
    </div>
</div>

<!-- Log Modal -->
<div id="logModal" style="display:none; position:fixed; top:0; left:0; width:100%; height:100%; background:rgba(0,0,0,0.5); z-index:1000; align-items:center; justify-content:center;">
    <div style="background:white; border-radius:8px; padding:20px; max-width:800px; width:90%; max-height:80%; overflow:auto;">
        <h3>📜 Build Logs <span id="logSlug" style="font-weight:normal;font-size:14px;color:#555;"></span></h3>
        <pre id="logContent" style="background:#1e1e1e;color:#d4d4d4;padding:10px;border-radius:4px;white-space:pre-wrap;word-wrap:break-word;max-height:400px;overflow:auto;"></pre>
        <button class="btn btn-danger" id="closeLogModal">Close</button>
    </div>
</div>

<script>
    // ---------- UPLOAD ----------
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const slugInput = document.getElementById('slugInput');
    const deployBtn = document.getElementById('deployBtn');
    const buildLogs = document.getElementById('buildLogs');
    const deployResult = document.getElementById('deployResult');
    let selectedFiles = [];
    let pollInterval = null;
    let currentSlug = null;

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
        let html = '<strong>Selected files:</strong> ';
        selectedFiles.forEach(f => html += `<span class="file-list-item">${f.name} (${(f.size/1024).toFixed(1)} KB)</span>`);
        fileList.innerHTML = html;
    }

    deployBtn.addEventListener('click', async () => {
        if (selectedFiles.length === 0) { alert('Select files first.'); return; }
        const slug = slugInput.value.trim();

        const formData = new FormData();
        selectedFiles.forEach(f => formData.append('files', f));
        formData.append('slug', slug);

        deployBtn.disabled = true;
        deployBtn.textContent = '⏳ Deploying...';
        buildLogs.style.display = 'block';
        buildLogs.innerHTML = '<div class="log-line log-info">⏳ Starting deployment...</div>';
        deployResult.innerHTML = '';

        try {
            const res = await fetch('/upload', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                currentSlug = data.slug;
                buildLogs.innerHTML += `<div class="log-line log-success">✅ Upload complete. Slug: ${data.slug}</div>`;
                // start polling
                if (pollInterval) clearInterval(pollInterval);
                pollInterval = setInterval(pollStatus, 1500);
                setTimeout(pollStatus, 500);
            } else {
                buildLogs.innerHTML += `<div class="log-line log-error">❌ Error: ${data.error}</div>`;
                deployBtn.disabled = false;
                deployBtn.textContent = '🚀 Deploy';
            }
        } catch (e) {
            buildLogs.innerHTML += `<div class="log-line log-error">❌ Network error: ${e.message}</div>`;
            deployBtn.disabled = false;
            deployBtn.textContent = '🚀 Deploy';
        }
    });

    function pollStatus() {
        if (!currentSlug) return;
        fetch(`/status/${currentSlug}`)
            .then(res => res.json())
            .then(data => {
                // Update logs
                if (data.logs) {
                    const lines = data.logs.split('\n');
                    let html = '';
                    for (let line of lines) {
                        if (line.includes('❌')) html += `<div class="log-line log-error">${escapeHtml(line)}</div>`;
                        else if (line.includes('✅') || line.includes('success') || line.includes('ready')) html += `<div class="log-line log-success">${escapeHtml(line)}</div>`;
                        else if (line.includes('⏳') || line.includes('Waiting') || line.includes('Starting')) html += `<div class="log-line log-info">${escapeHtml(line)}</div>`;
                        else if (line.includes('⚠️')) html += `<div class="log-line log-warn">${escapeHtml(line)}</div>`;
                        else if (line.trim()) html += `<div class="log-line">${escapeHtml(line)}</div>`;
                    }
                    buildLogs.innerHTML = html || '<div class="log-line log-info">⏳ No logs yet...</div>';
                    buildLogs.scrollTop = buildLogs.scrollHeight;
                }

                if (data.status === 'running') {
                    clearInterval(pollInterval);
                    deployResult.innerHTML = `
                        <div style="color: #28a745; font-weight: bold;">✅ Deployment successful!</div>
                        <div>Link: <a href="${data.url}" target="_blank">${data.url}</a></div>
                    `;
                    deployBtn.disabled = false;
                    deployBtn.textContent = '🚀 Deploy';
                    // reload page after 2s to update list
                    setTimeout(() => location.reload(), 2000);
                } else if (data.status === 'failed') {
                    clearInterval(pollInterval);
                    deployResult.innerHTML = `<div style="color: #dc3545; font-weight: bold;">❌ Deployment failed. Check logs above.</div>`;
                    deployBtn.disabled = false;
                    deployBtn.textContent = '🚀 Deploy';
                }
            })
            .catch(err => console.error('Poll error:', err));
    }

    function escapeHtml(text) {
        const div = document.createElement('div');
        div.textContent = text;
        return div.innerHTML;
    }

    // ---------- LOG MODAL ----------
    const logModal = document.getElementById('logModal');
    const logContent = document.getElementById('logContent');
    const logSlug = document.getElementById('logSlug');
    const closeLogModal = document.getElementById('closeLogModal');

    document.querySelectorAll('.logs-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const slug = e.target.dataset.slug;
            logSlug.textContent = `(${slug})`;
            logContent.textContent = 'Loading...';
            logModal.style.display = 'flex';
            try {
                const res = await fetch(`/logs/${slug}`);
                const data = await res.json();
                logContent.textContent = data.logs || 'No logs.';
            } catch (err) {
                logContent.textContent = 'Error loading logs.';
            }
        });
    });
    closeLogModal.addEventListener('click', () => { logModal.style.display = 'none'; });
    logModal.addEventListener('click', (e) => { if (e.target === logModal) logModal.style.display = 'none'; });

    // ---------- ACTION BUTTONS ----------
    document.querySelectorAll('.start-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const slug = e.target.dataset.slug;
            if (!confirm(`Start ${slug}?`)) return;
            const res = await fetch(`/start/${slug}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });

    document.querySelectorAll('.stop-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const slug = e.target.dataset.slug;
            if (!confirm(`Stop ${slug}?`)) return;
            const res = await fetch(`/stop/${slug}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });

    document.querySelectorAll('.delete-btn').forEach(btn => {
        btn.addEventListener('click', async (e) => {
            const slug = e.target.dataset.slug;
            if (!confirm(`Delete ${slug} permanently?`)) return;
            const res = await fetch(`/delete/${slug}`, { method: 'POST' });
            if (res.ok) location.reload();
        });
    });
</script>
</body>
</html>
"""

# ---------- MAIN ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    print("="*60)
    print("🌐 Website Hosting Panel Started")
    print(f"🔗 Visit: http://localhost:{port}")
    print("🔑 Login: admin / admin")
    print("📁 Upload folder:", UPLOAD_FOLDER)
    print("📜 Logs folder:", LOGS_FOLDER)
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)