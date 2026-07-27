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
            return filename
    return None

def install_requirements(folder, website_id):
    req_file = os.path.join(folder, 'requirements.txt')
    if not os.path.exists(req_file):
        return True, "No requirements.txt"
    
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}_install.log")
    try:
        cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt']
        with open(log_file, 'w') as f:
            proc = subprocess.Popen(cmd, cwd=folder, stdout=f, stderr=subprocess.STDOUT)
            proc.wait()
        if proc.returncode != 0:
            with open(log_file, 'r') as f:
                error = f.read()[-500:]
            return False, f"Installation failed: {error}"
        return True, "Installation successful"
    except Exception as e:
        return False, f"Installation error: {str(e)}"

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

def start_website_process(website_id):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        log_website(website_id, "Folder missing", 'error')
        update_website_status(website_id, 'failed')
        return False, "Folder not found"
    
    startup = find_startup_file(folder)
    is_static = False
    
    if not startup:
        if os.path.exists(os.path.join(folder, 'index.html')):
            is_static = True
        else:
            log_website(website_id, "No startup file or index.html", 'error')
            update_website_status(website_id, 'failed')
            return False, "No startup file detected. Please upload a valid Python project or static site with index.html."
    
    if not is_static and startup:
        success, msg = install_requirements(folder, website_id)
        if not success:
            log_website(website_id, f"Requirements failed: {msg}", 'error')
            update_website_status(website_id, 'failed')
            return False, msg
    
    port = get_next_available_port()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    
    env = os.environ.copy()
    env['PORT'] = str(port)
    env['PYTHONUNBUFFERED'] = '1'
    
    if is_static:
        cmd = [sys.executable, '-m', 'http.server', str(port)]
    else:
        cmd = [sys.executable, startup]
    
    try:
        if os.name == 'nt':
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=open(log_file, 'a'), stderr=subprocess.STDOUT,
                                    creationflags=subprocess.CREATE_NEW_PROCESS_GROUP)
        else:
            proc = subprocess.Popen(cmd, cwd=folder, env=env,
                                    stdout=open(log_file, 'a'), stderr=subprocess.STDOUT,
                                    preexec_fn=os.setsid)
        
        time.sleep(2)
        healthy, health_msg = health_check(port, timeout=5)
        
        if healthy:
            update_website_status(website_id, 'running', proc.pid, port)
            log_website(website_id, f"Started on port {port} (PID {proc.pid})")
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ?, last_started = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (startup if startup else 'static', website_id))
                conn.commit()
            return True, f"Running on port {port}"
        else:
            try:
                if os.name == 'nt':
                    subprocess.run(['taskkill', '/PID', str(proc.pid), '/F'], capture_output=True)
                else:
                    os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except:
                pass
            update_website_status(website_id, 'crashed')
            log_website(website_id, f"Health check failed: {health_msg}", 'error')
            return False, f"Health check failed: {health_msg}"
            
    except Exception as e:
        log_website(website_id, f"Start error: {str(e)}", 'error')
        update_website_status(website_id, 'failed')
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
    log_website(website_id, f"Stopped (PID {pid})")
    return True, "Stopped"

# ---------- प्रॉक्सी रूट ----------
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

# ---------- फ्लास्क रूट्स ----------
@app.route('/')
def index():
    if 'user_id' in session:
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_TEMPLATE)

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

@app.route('/upload', methods=['POST'])
def upload_website():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    if user['status'] != 'active':
        return jsonify({'success': False, 'error': 'Account disabled'}), 403
    
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file uploaded'}), 400
    
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400
    
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'File too large (max {MAX_UPLOAD_SIZE//1024//1024} MB)'}), 400
    
    if not file.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': 'Only ZIP files allowed'}), 400
    
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (user_id,)).fetchone()[0]
    
    slug = generate_website_slug(session['username'], count)
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ?', (slug,)).fetchone():
            count += 1
            slug = generate_website_slug(session['username'], count)
    
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
    
    zip_path = os.path.join(folder, 'upload.zip')
    try:
        file.save(zip_path)
    except Exception as e:
        rollback_upload(website_id, folder)
        return jsonify({'success': False, 'error': f'Failed to save: {str(e)}'}), 500
    
    valid, msg = validate_zip(zip_path)
    if not valid:
        rollback_upload(website_id, folder)
        return jsonify({'success': False, 'error': msg}), 400
    
    ok, msg = extract_zip(zip_path, folder)
    if not ok:
        rollback_upload(website_id, folder)
        return jsonify({'success': False, 'error': msg}), 400
    
    os.remove(zip_path)
    size_used = calculate_folder_size(folder)
    
    with get_db() as conn:
        conn.execute('''UPDATE websites SET 
                        website_name = ?, 
                        website_folder = ?,
                        storage_used = ?,
                        website_size = ?,
                        status = 'uploaded',
                        updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?''',
                     (file.filename[:-4] if '.' in file.filename else file.filename,
                      f"website_{website_id}", size_used, size_used, website_id))
        conn.commit()
    
    log_website(website_id, f"Uploaded: {file.filename}")
    log_activity(user_id, 'upload', f'Uploaded {file.filename}', request.remote_addr)
    
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug})

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
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    
    if website['status'] in ['running', 'starting']:
        stop_website_process(website_id)
    
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    shutil.rmtree(folder, ignore_errors=True)
    
    for f in [f"website_{website_id}.log", f"website_{website_id}_install.log"]:
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
    
    return render_template_string(LOGS_TEMPLATE, website=website, logs=logs, file_log=file_log, install_log=install_log)

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
        if conn.execute('SELECT id FROM websites WHERE website_slug = ? AND id != ?', (new_slug, website_id)).fetchone():
            return jsonify({'success': False, 'error': 'Slug already taken'}), 400
        conn.execute('UPDATE websites SET website_slug = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (new_slug, website_id))
        conn.commit()
    
    log_website(website_id, f"Changed slug to {new_slug}")
    return jsonify({'success': True, 'new_slug': new_slug})

@app.route('/website/<int:website_id>/custom_domain', methods=['POST'])
def set_custom_domain(website_id):
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401
    
    website = get_website_by_id(website_id)
    if not website or website['owner_id'] != session['user_id']:
        if session.get('role') != 'admin':
            return jsonify({'success': False, 'error': 'Not found'}), 404
    
    domain = request.form.get('domain', '').strip()
    if not domain or not re.match(r'^([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}$', domain):
        return jsonify({'success': False, 'error': 'Invalid domain'}), 400
    
    with get_db() as conn:
        conn.execute('UPDATE websites SET custom_domain = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (domain, website_id))
        conn.commit()
    
    log_website(website_id, f"Custom domain set: {domain}")
    return jsonify({'success': True, 'domain': domain})

# ---------- प्रीमियम UI टेम्प्लेट्स ----------
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
.upload-box input[type="file"]{margin:15px auto;display:block;color:#aaa}
.upload-btn{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 40px;border-radius:50px;color:#fff;font-size:1rem;font-weight:700;cursor:pointer;transition:.3s}
.upload-btn:hover{transform:scale(1.05);box-shadow:0 0 40px rgba(0,229,255,0.2)}
#uploadStatus{margin-top:15px;font-weight:500}
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
.domain-edit{display:flex;gap:8px;margin-top:8px}
.domain-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff;outline:none;font-size:0.85rem}
.domain-edit input:focus{border-color:#ffaa00}
.domain-edit button{padding:8px 16px;background:#ffaa00;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer;transition:.2s}
.domain-edit button:hover{transform:scale(1.05)}
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
<h3>📤 Upload Website (ZIP)</h3>
<input type="file" id="zipFile" accept=".zip">
<button class="upload-btn" id="uploadBtn">Upload & Deploy</button>
<div id="uploadStatus"></div>
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
<button class="btn-delete" onclick="if(confirm('Delete this website?')) action({{ w.id }},'delete')">🗑 Delete</button>
</div>
<div class="url-edit">
<input type="text" id="slug_input_{{ w.id }}" value="{{ w.website_slug }}" placeholder="new-slug">
<button onclick="changeSlug({{ w.id }})">Change</button>
</div>
<div class="domain-edit">
<input type="text" id="domain_input_{{ w.id }}" value="{{ w.custom_domain or '' }}" placeholder="custom.domain.com">
<button onclick="setDomain({{ w.id }})">Set Domain</button>
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
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}
function setDomain(id){
const val=document.getElementById('domain_input_'+id).value.trim();
if(!val)return alert('Enter domain');
fetch('/website/'+id+'/custom_domain',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'domain='+encodeURIComponent(val)
})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}
document.getElementById('uploadBtn').onclick=function(){
const file=document.getElementById('zipFile').files[0];
if(!file)return alert('Select ZIP');
const fd=new FormData();fd.append('file',file);
const st=document.getElementById('uploadStatus');
st.innerHTML='⏳ Uploading...';
fetch('/upload',{method:'POST',body:fd})
.then(r=>r.json())
.then(d=>{if(d.success){st.innerHTML='✅ Uploaded!';location.reload()}else st.innerHTML='❌ '+d.error})
.catch(()=>st.innerHTML='❌ Network error');
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
</div>
</body>
</html>
"""

# ---------- सर्वर स्टार्ट ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)