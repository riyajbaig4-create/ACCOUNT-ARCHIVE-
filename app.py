import os
import sys
import zipfile
import shutil
import subprocess
import threading
import time
import json
import hashlib
import secrets
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import urljoin, urlparse

from flask import (
    Flask, render_template_string, request, redirect, url_for, 
    session, send_from_directory, jsonify, Response, abort
)
import sqlite3
import requests

# ------------------ कॉन्फ़िगरेशन ------------------
BASE_DIR = Path(__file__).resolve().parent
UPLOAD_FOLDER = BASE_DIR / 'uploads'
LOGS_FOLDER = BASE_DIR / 'logs'
DB_PATH = BASE_DIR / 'hosting.db'
SECRET_KEY = secrets.token_hex(16)
DEBUG = False  # प्रोडक्शन के लिए False

# सुनिश्चित करें कि फोल्डर्स मौजूद हैं
UPLOAD_FOLDER.mkdir(exist_ok=True)
LOGS_FOLDER.mkdir(exist_ok=True)

# ------------------ डेटाबेस ------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    # Users
    c.execute('''
        CREATE TABLE IF NOT EXISTS users (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT UNIQUE NOT NULL,
            password TEXT NOT NULL
        )
    ''')
    # Websites
    c.execute('''
        CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            slug TEXT UNIQUE NOT NULL,
            name TEXT NOT NULL,
            runtime TEXT,
            status TEXT DEFAULT 'Stopped',
            upload_date TEXT,
            port INTEGER,
            pid INTEGER,
            folder_path TEXT,
            startup_file TEXT
        )
    ''')
    # Logs (build/deploy logs)
    c.execute('''
        CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER,
            log_text TEXT,
            timestamp TEXT,
            FOREIGN KEY(website_id) REFERENCES websites(id)
        )
    ''')
    # डिफ़ॉल्ट admin यूज़र डालें
    c.execute("SELECT * FROM users WHERE username='admin'")
    if not c.fetchone():
        hashed = hashlib.sha256('admin'.encode()).hexdigest()
        c.execute("INSERT INTO users (username, password) VALUES (?, ?)", ('admin', hashed))
    conn.commit()
    conn.close()

init_db()

# ------------------ Flask ऐप ------------------
app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['UPLOAD_FOLDER'] = str(UPLOAD_FOLDER)
app.config['MAX_CONTENT_LENGTH'] = 500 * 1024 * 1024  # 500 MB

# हेल्पर फ़ंक्शंस
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def get_website_by_slug(slug):
    conn = get_db()
    site = conn.execute("SELECT * FROM websites WHERE slug=?", (slug,)).fetchone()
    conn.close()
    return site

def update_website_status(slug, status, pid=None, port=None):
    conn = get_db()
    if pid is not None:
        conn.execute("UPDATE websites SET status=?, pid=?, port=? WHERE slug=?", (status, pid, port, slug))
    else:
        conn.execute("UPDATE websites SET status=? WHERE slug=?", (status, slug))
    conn.commit()
    conn.close()

def log_message(website_id, msg):
    conn = get_db()
    now = datetime.now().isoformat()
    conn.execute("INSERT INTO logs (website_id, log_text, timestamp) VALUES (?, ?, ?)",
                 (website_id, msg, now))
    conn.commit()
    conn.close()

def get_latest_logs(website_id, limit=100):
    conn = get_db()
    rows = conn.execute("SELECT log_text, timestamp FROM logs WHERE website_id=? ORDER BY id DESC LIMIT ?", (website_id, limit)).fetchall()
    conn.close()
    return list(reversed(rows))  # पुराने से नए

def generate_slug(name):
    # स्लग बनाएं: name + नंबर
    base = ''.join(e for e in name if e.isalnum()).lower()
    if not base:
        base = 'site'
    conn = get_db()
    # check existing slugs
    count = conn.execute("SELECT COUNT(*) FROM websites WHERE slug LIKE ?", (base + '%',)).fetchone()[0]
    conn.close()
    if count == 0:
        return base
    else:
        return f"{base}{count}"

def find_free_port():
    import socket
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(('', 0))
        return s.getsockname()[1]

def detect_runtime(folder_path):
    # Python detect: check for requirements.txt or .py files
    path = Path(folder_path)
    has_req = (path / 'requirements.txt').exists()
    has_py = any(path.glob('*.py'))
    if has_req or has_py:
        # find startup file
        candidates = ['app.py', 'main.py', 'server.py', 'run.py', 'start.py']
        for cand in candidates:
            if (path / cand).exists():
                return 'python', cand
        # if no candidate, take first .py
        py_files = list(path.glob('*.py'))
        if py_files:
            return 'python', py_files[0].name
        else:
            return 'python', None
    else:
        # Static HTML
        return 'html', None

def install_requirements(folder_path, log_callback):
    req_file = Path(folder_path) / 'requirements.txt'
    if not req_file.exists():
        log_callback("No requirements.txt found.")
        return True
    log_callback("Installing requirements from requirements.txt...")
    try:
        # use pip install -r
        proc = subprocess.Popen(
            [sys.executable, '-m', 'pip', 'install', '-r', str(req_file)],
            cwd=folder_path,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1
        )
        for line in proc.stdout:
            log_callback(line.strip())
        proc.wait()
        if proc.returncode != 0:
            log_callback(f"pip install failed with code {proc.returncode}")
            return False
        log_callback("Requirements installed successfully.")
        return True
    except Exception as e:
        log_callback(f"Error installing requirements: {str(e)}")
        return False

def start_python_app(slug, folder_path, startup_file, log_callback):
    # find free port
    port = find_free_port()
    log_callback(f"Starting Python app on port {port}")
    # run python startup_file
    cmd = [sys.executable, startup_file]
    log_file_path = LOGS_FOLDER / f"{slug}_app.log"
    with open(log_file_path, 'w') as f:
        proc = subprocess.Popen(
            cmd,
            cwd=folder_path,
            stdout=f,
            stderr=subprocess.STDOUT,
            text=True,
            env={**os.environ, 'PORT': str(port)}  # some apps use PORT
        )
    pid = proc.pid
    # update DB
    conn = get_db()
    conn.execute("UPDATE websites SET port=?, pid=?, status='Running' WHERE slug=?", (port, pid, slug))
    conn.commit()
    conn.close()
    log_callback(f"App started with PID {pid}")
    return True

def start_static_server(slug, folder_path, log_callback):
    # For static, we just serve via Flask proxy, no external process
    # We'll set port=0, pid=0, and status='Running'
    conn = get_db()
    conn.execute("UPDATE websites SET port=0, pid=0, status='Running' WHERE slug=?", (slug,))
    conn.commit()
    conn.close()
    log_callback("Static site is ready.")
    return True

def deploy_website(slug, folder_path, name, log_callback):
    # detect runtime
    runtime, startup = detect_runtime(folder_path)
    log_callback(f"Detected runtime: {runtime}")
    if runtime == 'python':
        log_callback(f"Startup file: {startup}")
        # install requirements
        if not install_requirements(folder_path, log_callback):
            # update status to failed
            conn = get_db()
            conn.execute("UPDATE websites SET status='Failed', runtime='python' WHERE slug=?", (slug,))
            conn.commit()
            conn.close()
            log_callback("Deployment failed due to requirements installation.")
            return False
        # start app
        if start_python_app(slug, folder_path, startup, log_callback):
            log_callback(f"Website started successfully at {url_for('serve_website', slug=slug, _external=True)}")
            return True
        else:
            conn = get_db()
            conn.execute("UPDATE websites SET status='Failed' WHERE slug=?", (slug,))
            conn.commit()
            conn.close()
            return False
    else:  # html
        log_callback("Static HTML site detected.")
        if start_static_server(slug, folder_path, log_callback):
            log_callback(f"Website available at {url_for('serve_website', slug=slug, _external=True)}")
            return True
        else:
            conn = get_db()
            conn.execute("UPDATE websites SET status='Failed' WHERE slug=?", (slug,))
            conn.commit()
            conn.close()
            return False

# ------------------ रूट्स ------------------
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        hashed = hashlib.sha256(password.encode()).hexdigest()
        conn = get_db()
        user = conn.execute("SELECT * FROM users WHERE username=? AND password=?", (username, hashed)).fetchone()
        conn.close()
        if user:
            session['user_id'] = user['id']
            session['username'] = user['username']
            return redirect(url_for('dashboard'))
        else:
            return render_template_string(LOGIN_TEMPLATE, error="Invalid credentials")
    return render_template_string(LOGIN_TEMPLATE, error=None)

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/dashboard')
def dashboard():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    conn = get_db()
    sites = conn.execute("SELECT * FROM websites ORDER BY id DESC").fetchall()
    conn.close()
    # प्रत्येक साइट के लिए लिंक जनरेट करें
    site_list = []
    for site in sites:
        link = url_for('serve_website', slug=site['slug'], _external=True)
        site_dict = dict(site)
        site_dict['link'] = link
        site_list.append(site_dict)
    return render_template_string(DASHBOARD_TEMPLATE, sites=site_list, username=session.get('username'))

@app.route('/upload', methods=['GET', 'POST'])
def upload():
    if 'user_id' not in session:
        return redirect(url_for('login'))
    if request.method == 'POST':
        # चेक करें कि फाइलें हैं
        if 'zipfile' in request.files and request.files['zipfile'].filename != '':
            zip_file = request.files['zipfile']
            name = request.form.get('name', 'my_site')
            # create temp dir
            with tempfile.TemporaryDirectory() as tmpdir:
                zip_path = Path(tmpdir) / 'upload.zip'
                zip_file.save(str(zip_path))
                # extract
                extract_path = Path(tmpdir) / 'extracted'
                extract_path.mkdir()
                with zipfile.ZipFile(zip_path, 'r') as zf:
                    zf.extractall(extract_path)
                # move to final location
                slug = generate_slug(name)
                dest_folder = UPLOAD_FOLDER / slug
                shutil.copytree(extract_path, dest_folder)
            # create website record
            conn = get_db()
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO websites (slug, name, status, upload_date, folder_path) VALUES (?, ?, ?, ?, ?)",
                (slug, name, 'Building', now, str(dest_folder))
            )
            site_id = conn.lastrowid
            conn.commit()
            conn.close()
            # start deployment in background
            def deploy_thread():
                log_callback = lambda msg: log_message(site_id, msg)
                log_callback(f"Deployment started for {slug}")
                deploy_website(slug, str(dest_folder), name, log_callback)
            thread = threading.Thread(target=deploy_thread)
            thread.daemon = True
            thread.start()
            return redirect(url_for('build_logs', slug=slug))
        elif 'files[]' in request.files:
            files = request.files.getlist('files[]')
            if not files or files[0].filename == '':
                return "No files selected", 400
            name = request.form.get('name', 'my_site')
            slug = generate_slug(name)
            dest_folder = UPLOAD_FOLDER / slug
            dest_folder.mkdir(parents=True, exist_ok=True)
            # save files preserving structure
            for file in files:
                if file.filename == '':
                    continue
                # secure filename but keep relative paths? We'll use full path relative to upload root?
                # We'll use filename as relative path
                rel_path = file.filename
                # prevent path traversal
                rel_path = os.path.normpath(rel_path)
                if rel_path.startswith('..'):
                    continue
                target = dest_folder / rel_path
                target.parent.mkdir(parents=True, exist_ok=True)
                file.save(str(target))
            # create record
            conn = get_db()
            now = datetime.now().isoformat()
            conn.execute(
                "INSERT INTO websites (slug, name, status, upload_date, folder_path) VALUES (?, ?, ?, ?, ?)",
                (slug, name, 'Building', now, str(dest_folder))
            )
            site_id = conn.lastrowid
            conn.commit()
            conn.close()
            # deploy
            def deploy_thread():
                log_callback = lambda msg: log_message(site_id, msg)
                log_callback(f"Deployment started for {slug}")
                deploy_website(slug, str(dest_folder), name, log_callback)
            thread = threading.Thread(target=deploy_thread)
            thread.daemon = True
            thread.start()
            return redirect(url_for('build_logs', slug=slug))
        else:
            return "No file uploaded", 400
    return render_template_string(UPLOAD_TEMPLATE)

@app.route('/build_logs/<slug>')
def build_logs(slug):
    if 'user_id' not in session:
        return redirect(url_for('login'))
    site = get_website_by_slug(slug)
    if not site:
        abort(404)
    return render_template_string(LOGS_TEMPLATE, slug=slug, site_name=site['name'])

@app.route('/api/logs/<slug>')
def api_logs(slug):
    # return latest logs as JSON
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    site = get_website_by_slug(slug)
    if not site:
        return jsonify({'error': 'Not found'}), 404
    logs = get_latest_logs(site['id'], limit=200)
    return jsonify(logs)

@app.route('/start/<slug>', methods=['POST'])
def start_site(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    site = get_website_by_slug(slug)
    if not site:
        return jsonify({'error': 'Not found'}), 404
    # only if not running
    if site['status'] == 'Running':
        return jsonify({'error': 'Already running'}), 400
    # start in background
    def start_thread():
        log_callback = lambda msg: log_message(site['id'], msg)
        log_callback(f"Manual start requested for {slug}")
        folder_path = site['folder_path']
        # detect runtime again
        runtime, startup = detect_runtime(folder_path)
        if runtime == 'python':
            if not install_requirements(folder_path, log_callback):
                update_website_status(slug, 'Failed')
                log_callback("Start failed due to requirements.")
                return
            if start_python_app(slug, folder_path, startup, log_callback):
                update_website_status(slug, 'Running')
                log_callback("Started successfully.")
            else:
                update_website_status(slug, 'Failed')
                log_callback("Start failed.")
        else:
            if start_static_server(slug, folder_path, log_callback):
                update_website_status(slug, 'Running')
                log_callback("Started successfully.")
            else:
                update_website_status(slug, 'Failed')
                log_callback("Start failed.")
    threading.Thread(target=start_thread, daemon=True).start()
    return jsonify({'status': 'ok'})

@app.route('/stop/<slug>', methods=['POST'])
def stop_site(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    site = get_website_by_slug(slug)
    if not site:
        return jsonify({'error': 'Not found'}), 404
    if site['status'] != 'Running':
        return jsonify({'error': 'Not running'}), 400
    pid = site['pid']
    if pid:
        try:
            os.kill(pid, 15)  # SIGTERM
            # wait a bit
            time.sleep(1)
            # check if still alive
            try:
                os.kill(pid, 0)
                # still alive, kill with SIGKILL
                os.kill(pid, 9)
            except OSError:
                pass
        except Exception as e:
            pass
    update_website_status(slug, 'Stopped', pid=None, port=None)
    return jsonify({'status': 'ok'})

@app.route('/delete/<slug>', methods=['POST'])
def delete_site(slug):
    if 'user_id' not in session:
        return jsonify({'error': 'Unauthorized'}), 401
    site = get_website_by_slug(slug)
    if not site:
        return jsonify({'error': 'Not found'}), 404
    # stop if running
    if site['status'] == 'Running':
        pid = site['pid']
        if pid:
            try:
                os.kill(pid, 15)
                time.sleep(0.5)
                os.kill(pid, 9)
            except:
                pass
    # delete files
    folder = Path(site['folder_path'])
    if folder.exists():
        shutil.rmtree(folder)
    # delete logs from DB
    conn = get_db()
    conn.execute("DELETE FROM logs WHERE website_id=?", (site['id'],))
    conn.execute("DELETE FROM websites WHERE slug=?", (slug,))
    conn.commit()
    conn.close()
    return jsonify({'status': 'ok'})

# प्रॉक्सी / स्टैटिक सर्विंग
@app.route('/<slug>/', defaults={'path': ''})
@app.route('/<slug>/<path:path>')
def serve_website(slug, path):
    # check if website exists
    site = get_website_by_slug(slug)
    if not site:
        abort(404)
    # if status is not Running, show placeholder
    if site['status'] != 'Running':
        return render_template_string(SITE_OFF_TEMPLATE, slug=slug), 503
    # if runtime is html, serve static files
    if site['runtime'] == 'html' or site['runtime'] == 'HTML':
        folder = Path(site['folder_path'])
        if path == '':
            # try index.html
            if (folder / 'index.html').exists():
                return send_from_directory(folder, 'index.html')
            else:
                # list directory? or 404
                return "No index.html", 404
        else:
            # security: ensure path doesn't escape
            safe_path = os.path.normpath(path)
            if safe_path.startswith('..'):
                abort(403)
            full_path = folder / safe_path
            if not full_path.exists():
                abort(404)
            if full_path.is_dir():
                # maybe serve index inside that dir
                if (full_path / 'index.html').exists():
                    return send_from_directory(full_path, 'index.html')
                else:
                    return "Directory listing not allowed", 403
            return send_from_directory(folder, safe_path)
    else:
        # Python app: proxy to localhost:port
        port = site['port']
        if not port:
            return "App not running on port", 500
        # forward request to localhost:port
        target_url = f"http://localhost:{port}/{path}"
        if request.query_string:
            target_url += '?' + request.query_string.decode()
        try:
            resp = requests.request(
                method=request.method,
                url=target_url,
                headers={key: value for key, value in request.headers if key != 'Host'},
                data=request.get_data(),
                stream=True,
                timeout=30
            )
            # return response
            excluded_headers = ['content-encoding', 'content-length', 'transfer-encoding', 'connection']
            headers = [(name, value) for name, value in resp.raw.headers.items()
                       if name.lower() not in excluded_headers]
            return Response(resp.raw, status=resp.status_code, headers=headers)
        except Exception as e:
            return f"Proxy error: {str(e)}", 502

# ------------------ टेम्पलेट्स (इनलाइन) ------------------
LOGIN_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Login - Hosting Panel</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); width: 300px; }
        h2 { margin-top: 0; }
        label { display: block; margin: 10px 0 5px; }
        input { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; }
        button { width: 100%; padding: 10px; background: #007bff; color: white; border: none; border-radius: 4px; cursor: pointer; }
        .error { color: red; }
    </style>
</head>
<body>
    <div class="login-box">
        <h2>Login</h2>
        {% if error %}
        <p class="error">{{ error }}</p>
        {% endif %}
        <form method="post">
            <label>Username</label>
            <input type="text" name="username" value="admin" required>
            <label>Password</label>
            <input type="password" name="password" value="admin" required>
            <button type="submit">Login</button>
        </form>
    </div>
</body>
</html>
'''

DASHBOARD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Dashboard - Hosting Panel</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f9f9f9; margin: 0; padding: 20px; }
        .header { background: white; padding: 15px 20px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px; }
        .header a { margin-left: 15px; text-decoration: none; color: #007bff; }
        .btn { background: #007bff; color: white; padding: 8px 16px; border: none; border-radius: 4px; cursor: pointer; text-decoration: none; display: inline-block; }
        .btn-danger { background: #dc3545; }
        .btn-warning { background: #ffc107; color: #212529; }
        .btn-success { background: #28a745; }
        .btn-secondary { background: #6c757d; }
        .card { background: white; border-radius: 8px; padding: 15px; margin-bottom: 15px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        .card h3 { margin: 0 0 10px; }
        .card .info { display: flex; flex-wrap: wrap; gap: 15px; margin-bottom: 10px; }
        .card .info span { background: #f1f1f1; padding: 4px 10px; border-radius: 12px; font-size: 0.9em; }
        .card .actions { display: flex; flex-wrap: wrap; gap: 8px; }
        .card .actions form { display: inline; }
        .status-Running { color: #28a745; font-weight: bold; }
        .status-Stopped { color: #6c757d; }
        .status-Building { color: #ffc107; }
        .status-Failed { color: #dc3545; }
        .link { word-break: break-all; }
    </style>
</head>
<body>
    <div class="header">
        <h2>Hosting Panel</h2>
        <div>
            <a href="{{ url_for('upload') }}" class="btn">Upload Website</a>
            <a href="#" class="btn btn-secondary">GitHub Deploy</a>
            <a href="{{ url_for('logout') }}" class="btn btn-danger">Logout</a>
            <span>Welcome, {{ username }}</span>
        </div>
    </div>

    <h3>Your Websites</h3>
    {% if sites %}
        {% for site in sites %}
        <div class="card">
            <div class="info">
                <h3>{{ site.name }}</h3>
                <span>Runtime: {{ site.runtime or 'N/A' }}</span>
                <span class="status-{{ site.status }}">Status: {{ site.status }}</span>
                <span>Uploaded: {{ site.upload_date }}</span>
            </div>
            <div class="link"><a href="{{ site.link }}" target="_blank">{{ site.link }}</a></div>
            <div class="actions">
                <form action="{{ url_for('start_site', slug=site.slug) }}" method="post" style="display:inline;">
                    <button type="submit" class="btn btn-success">Start</button>
                </form>
                <form action="{{ url_for('stop_site', slug=site.slug) }}" method="post" style="display:inline;">
                    <button type="submit" class="btn btn-warning">Stop</button>
                </form>
                <form action="{{ url_for('delete_site', slug=site.slug) }}" method="post" style="display:inline;" onsubmit="return confirm('Delete this website?');">
                    <button type="submit" class="btn btn-danger">Delete</button>
                </form>
                <a href="{{ url_for('build_logs', slug=site.slug) }}" class="btn btn-secondary">Build Logs</a>
            </div>
        </div>
        {% endfor %}
    {% else %}
        <p>No websites uploaded yet.</p>
    {% endif %}
</body>
</html>
'''

UPLOAD_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Upload Website</title>
    <style>
        body { font-family: Arial, sans-serif; background: #f9f9f9; padding: 20px; }
        .container { max-width: 600px; margin: auto; background: white; padding: 30px; border-radius: 8px; box-shadow: 0 2px 4px rgba(0,0,0,0.05); }
        label { display: block; margin: 15px 0 5px; }
        input[type="text"], input[type="file"] { width: 100%; padding: 8px; box-sizing: border-box; border: 1px solid #ddd; border-radius: 4px; }
        .btn { background: #007bff; color: white; padding: 10px 20px; border: none; border-radius: 4px; cursor: pointer; }
        .back { display: inline-block; margin-top: 15px; color: #007bff; text-decoration: none; }
        .note { color: #6c757d; font-size: 0.9em; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Upload Website</h2>
        <form method="post" enctype="multipart/form-data">
            <label>Website Name</label>
            <input type="text" name="name" placeholder="e.g., my-app" required>

            <label>Upload ZIP file</label>
            <input type="file" name="zipfile" accept=".zip">
            <p class="note">Or upload multiple files/folders (select all files in your project directory)</p>
            <input type="file" name="files[]" multiple webkitdirectory directory>
            <br><br>
            <button type="submit" class="btn">Upload & Deploy</button>
        </form>
        <a href="{{ url_for('dashboard') }}" class="back">← Back to Dashboard</a>
    </div>
</body>
</html>
'''

LOGS_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head>
    <title>Build Logs - {{ site_name }}</title>
    <style>
        body { font-family: monospace; background: #1e1e1e; color: #d4d4d4; padding: 20px; }
        .container { max-width: 900px; margin: auto; }
        h2 { color: #fff; }
        pre { background: #2d2d2d; padding: 15px; border-radius: 4px; white-space: pre-wrap; word-wrap: break-word; max-height: 600px; overflow-y: auto; }
        .back { color: #4a9eff; text-decoration: none; display: inline-block; margin-top: 10px; }
        .status { color: #ffcc00; }
    </style>
</head>
<body>
    <div class="container">
        <h2>Build Logs for {{ site_name }}</h2>
        <div id="logs"><pre>Loading logs...</pre></div>
        <a href="{{ url_for('dashboard') }}" class="back">← Back to Dashboard</a>
    </div>
    <script>
        function fetchLogs() {
            fetch('/api/logs/{{ slug }}')
                .then(res => res.json())
                .then(data => {
                    let html = '';
                    if (data.length === 0) html = 'No logs yet.';
                    else {
                        data.forEach(item => {
                            html += item.timestamp + ' - ' + item.log_text + '\\n';
                        });
                    }
                    document.getElementById('logs').innerHTML = '<pre>' + html + '</pre>';
                })
                .catch(err => console.error(err));
        }
        fetchLogs();
        setInterval(fetchLogs, 3000);
    </script>
</body>
</html>
'''

SITE_OFF_TEMPLATE = '''
<!DOCTYPE html>
<html>
<head><title>Site Offline</title></head>
<body>
    <h1>Website is not running</h1>
    <p>The site {{ slug }} is currently stopped or failed. Please start it from the dashboard.</p>
</body>
</html>
'''

# ------------------ मुख्य ------------------
if __name__ == '__main__':
    # सेटिंग्स: हम production के लिए 0.0.0.0 पर सुनेंगे
    # Use environment variable PORT if set
    port = int(os.environ.get('PORT', 5000))
    # For development, debug=True but we set debug=False for production
    app.run(host='0.0.0.0', port=port, debug=DEBUG)