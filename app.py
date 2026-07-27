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
import urllib.parse

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key_change_me_in_production'

# ---------- कॉन्फ़िगरेशन ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')
MAX_UPLOAD_SIZE = 100 * 1024 * 1024
AUTO_RESTART_MAX = 3
AUTO_RESTART_INTERVAL = 10
GIT_CLONE_TIMEOUT = 120

# ---------- फ्रेमवर्क डिटेक्शन ----------
FRAMEWORK_DETECTORS = {
    'flask': lambda f: os.path.exists(os.path.join(f, 'app.py')) or os.path.exists(os.path.join(f, 'main.py')),
    'fastapi': lambda f: os.path.exists(os.path.join(f, 'main.py')) and 'FastAPI' in open(os.path.join(f, 'main.py')).read(),
    'django': lambda f: os.path.exists(os.path.join(f, 'manage.py')),
    'bottle': lambda f: os.path.exists(os.path.join(f, 'app.py')) and 'bottle' in open(os.path.join(f, 'app.py')).read(),
    'nodejs': lambda f: os.path.exists(os.path.join(f, 'package.json')),
    'static': lambda f: os.path.exists(os.path.join(f, 'index.html')),
    'php': lambda f: any(f.endswith('.php') for _, _, files in os.walk(f) for f in files),
    'go': lambda f: any(f.endswith('.go') for _, _, files in os.walk(f) for f in files),
    'rust': lambda f: any(f.endswith('.rs') for _, _, files in os.walk(f) for f in files),
    'java': lambda f: any(f.endswith('.java') for _, _, files in os.walk(f) for f in files),
}

STARTUP_PRIORITY = {
    'python': ['run_app.py', 'app.py', 'main.py', 'server.py', 'manage.py', 'index.py', 'start.py'],
    'node': ['index.js', 'server.js', 'main.js', 'app.js', 'start.js'],
    'php': ['index.php', 'main.php', 'server.php'],
    'go': ['main.go', 'server.go'],
    'rust': ['main.rs', 'server.rs'],
    'java': ['Main.java', 'Application.java'],
    'static': ['index.html'],
}

AUTO_INSTALL_PACKAGES = {
    'flask': 'flask',
    'fastapi': 'fastapi uvicorn',
    'django': 'django',
    'bottle': 'bottle',
    'requests': 'requests',
    'numpy': 'numpy',
    'pandas': 'pandas',
    'pillow': 'Pillow',
    'sqlalchemy': 'sqlalchemy',
    'pymongo': 'pymongo',
    'psycopg2': 'psycopg2-binary',
    'mysqlclient': 'mysqlclient',
    'redis': 'redis',
    'celery': 'celery',
    'gunicorn': 'gunicorn',
    'uvicorn': 'uvicorn',
    'httpx': 'httpx',
    'aiohttp': 'aiohttp',
    'websockets': 'websockets',
    'python-telegram-bot': 'python-telegram-bot',
    'pyTelegramBotAPI': 'pyTelegramBotAPI',
    'pyrogram': 'pyrogram',
    'telethon': 'telethon',
    'aiogram': 'aiogram',
    'discord.py': 'discord.py',
    'flask-cors': 'Flask-CORS',
    'flask-socketio': 'flask-socketio',
    'flask-sqlalchemy': 'flask-sqlalchemy',
    'flask-migrate': 'flask-migrate',
    'flask-login': 'flask-login',
    'flask-mail': 'flask-mail',
    'flask-wtf': 'flask-wtf',
    'flask-security': 'flask-security',
    'flask-restful': 'flask-restful',
    'flask-restx': 'flask-restx',
    'flask-jwt-extended': 'flask-jwt-extended',
    'flask-caching': 'flask-caching',
    'flask-session': 'flask-session',
    'flask-talisman': 'flask-talisman',
    'flask-compress': 'flask-compress',
    'flask-limiter': 'flask-limiter',
    'flask-sse': 'flask-sse',
}

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
            framework TEXT,
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

def detect_framework(folder):
    for name, detector in FRAMEWORK_DETECTORS.items():
        try:
            if detector(folder):
                return name
        except:
            continue
    if os.path.exists(os.path.join(folder, 'app.py')) or os.path.exists(os.path.join(folder, 'main.py')):
        return 'flask'
    if os.path.exists(os.path.join(folder, 'manage.py')):
        return 'django'
    if os.path.exists(os.path.join(folder, 'package.json')):
        return 'nodejs'
    if os.path.exists(os.path.join(folder, 'index.html')):
        return 'static'
    return 'unknown'

def get_startup_files(folder, framework):
    if framework in ['python', 'flask', 'fastapi', 'django', 'bottle']:
        return STARTUP_PRIORITY['python']
    elif framework == 'nodejs':
        return STARTUP_PRIORITY['node']
    elif framework == 'php':
        return STARTUP_PRIORITY['php']
    elif framework == 'go':
        return STARTUP_PRIORITY['go']
    elif framework == 'rust':
        return STARTUP_PRIORITY['rust']
    elif framework == 'java' or framework == 'spring':
        return STARTUP_PRIORITY['java']
    else:
        return STARTUP_PRIORITY['static']

def find_startup_file(folder, framework):
    candidates = get_startup_files(folder, framework)
    for fname in candidates:
        if os.path.exists(os.path.join(folder, fname)):
            return fname
    if framework == 'nodejs' and os.path.exists(os.path.join(folder, 'package.json')):
        try:
            with open(os.path.join(folder, 'package.json'), 'r') as f:
                data = json.load(f)
                if 'scripts' in data and 'start' in data['scripts']:
                    return 'package.json'
        except:
            pass
    if os.path.exists(os.path.join(folder, 'index.html')):
        return 'index.html'
    py_files = [f for f in os.listdir(folder) if f.endswith('.py') and not f.startswith('.')]
    if py_files:
        return py_files[0]
    html_files = [f for f in os.listdir(folder) if f.endswith('.html') and not f.startswith('.')]
    if html_files:
        return html_files[0]
    return None

def get_start_command(framework, startup_file, port):
    if framework in ['python', 'flask', 'fastapi', 'bottle']:
        return [sys.executable, startup_file]
    elif framework == 'django':
        return [sys.executable, 'manage.py', 'runserver', f'0.0.0.0:{port}']
    elif framework == 'nodejs':
        if startup_file == 'package.json':
            return ['npm', 'start']
        else:
            return ['node', startup_file]
    elif framework == 'php':
        return ['php', '-S', f'0.0.0.0:{port}', startup_file]
    elif framework == 'go':
        return ['go', 'run', startup_file]
    elif framework == 'rust':
        return ['cargo', 'run', '--bin', startup_file.replace('.rs', '')]
    elif framework == 'java' or framework == 'spring':
        return ['java', startup_file]
    else:
        return [sys.executable, '-m', 'http.server', str(port)]

def install_dependencies(folder, framework, log_callback=None):
    logs = []
    def log(msg):
        logs.append(msg)
        if log_callback:
            log_callback(msg)
    
    if framework in ['python', 'flask', 'fastapi', 'django', 'bottle']:
        req_path = os.path.join(folder, 'requirements.txt')
        if os.path.exists(req_path):
            log(f"📦 Installing Python requirements from {req_path} ...")
            cmd = [sys.executable, '-m', 'pip', 'install', '-r', 'requirements.txt']
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                log(line.strip())
            proc.wait()
            if proc.returncode != 0:
                log(f"❌ Installation failed with code {proc.returncode}")
                return False, logs
            log("✅ Python dependencies installed")
        else:
            # Auto-install common packages
            packages = []
            for pkg in AUTO_INSTALL_PACKAGES:
                for root, dirs, files in os.walk(folder):
                    for file in files:
                        if file.endswith('.py'):
                            with open(os.path.join(root, file), 'r') as f:
                                content = f.read()
                                if pkg in content:
                                    packages.append(AUTO_INSTALL_PACKAGES[pkg])
                                    break
            if packages:
                packages = list(set(packages))
                log(f"📦 Auto-installing packages: {', '.join(packages)}")
                cmd = [sys.executable, '-m', 'pip', 'install'] + packages
                proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
                for line in proc.stdout:
                    log(line.strip())
                proc.wait()
                if proc.returncode != 0:
                    log(f"❌ Auto-install failed")
                    return False, logs
                log("✅ Auto-install successful")
    
    elif framework == 'nodejs':
        if os.path.exists(os.path.join(folder, 'package.json')):
            log("📦 Installing npm packages ...")
            cmd = ['npm', 'install']
            proc = subprocess.Popen(cmd, cwd=folder, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            for line in proc.stdout:
                log(line.strip())
            proc.wait()
            if proc.returncode != 0:
                log(f"❌ npm install failed")
                return False, logs
            log("✅ npm packages installed")
    
    return True, logs

def inject_compatibility_routes(filepath):
    """Inject /api/login, /api/signup, and ensure app.run uses PORT env."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            content = f.read()
        
        # Remove existing /api/login and /api/signup
        import re
        pattern = r'@app\.route\(\'/api/login\'[^\n]*\n.*?def api_login\(\):.*?(?=\n@app\.route|\Z)'
        content = re.sub(pattern, '', content, flags=re.DOTALL)
        pattern = r'@app\.route\(\'/api/signup\'[^\n]*\n.*?def api_signup\(\):.*?(?=\n@app\.route|\Z)'
        content = re.sub(pattern, '', content, flags=re.DOTALL)
        
        # Ensure import of jsonify
        if 'from flask import jsonify' not in content:
            lines = content.split('\n')
            new_lines = []
            for line in lines:
                if line.startswith('from flask import') and 'jsonify' not in line:
                    line = line.replace('from flask import', 'from flask import jsonify,')
                new_lines.append(line)
            content = '\n'.join(new_lines)
            if 'from flask import' not in content:
                content = 'from flask import jsonify, request, render_template_string, redirect, url_for, session, jsonify, abort, Response, stream_with_context\n' + content
        
        # Add routes (GET+POST)
        api_routes = '''
@app.route('/api/login', methods=['GET', 'POST'])
def api_login():
    data = request.get_json() if request.method == 'POST' else {}
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if username == 'admin' and password == 'admin123':
        return jsonify({'success': True, 'username': 'admin', 'role': 'admin'})
    else:
        return jsonify({'error': 'Invalid credentials'}), 401

@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    return jsonify({'success': True, 'message': 'Account created (dummy)'})
'''
        
        # Ensure app.run uses PORT env
        # Find if there is an if __name__ block
        if 'if __name__' in content:
            # Check if app.run is inside it
            match = re.search(r'if __name__\s*==\s*[\'"]__main__[\'"]\s*:\s*(.*?)(?=\n\S|$)', content, re.DOTALL)
            if match:
                block = match.group(0)
                if 'app.run' in block:
                    # Check if port is specified
                    if 'port=' not in block:
                        # Replace app.run with port from env
                        new_block = re.sub(r'app\.run\(', 'app.run(host=\'0.0.0.0\', port=int(os.environ.get(\'PORT\', 5000))', block)
                        content = content.replace(block, new_block)
                    else:
                        # Already has port, we can keep
                        pass
                else:
                    # No app.run in main block, add it
                    new_block = block + '\n    port = int(os.environ.get(\'PORT\', 5000))\n    app.run(host=\'0.0.0.0\', port=port)'
                    content = content.replace(block, new_block)
            else:
                # No if __name__ block, add one at the end
                content += '''
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
'''
        else:
            # Add if __name__ at end
            content += '''
if __name__ == '__main__':
    import os
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)
'''
        
        # Insert api routes before if __name__
        if 'if __name__' in content:
            content = content.replace('if __name__', api_routes + '\nif __name__')
        else:
            content = content + '\n' + api_routes
        
        with open(filepath, 'w', encoding='utf-8') as f:
            f.write(content)
        return True
    except Exception as e:
        print(f"Injection error: {e}")
        return False

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
    
    framework = detect_framework(folder)
    if framework == 'unknown':
        startup = find_startup_file(folder, 'python')
        if startup:
            framework = 'python'
        else:
            startup = find_startup_file(folder, 'static')
            if startup:
                framework = 'static'
            else:
                if log_callback: log_callback("❌ Could not detect framework")
                return False, "No recognizable framework found", []
    
    startup_file = find_startup_file(folder, framework)
    if not startup_file:
        if log_callback: log_callback("❌ No startup file found")
        return False, "No startup file detected", []
    
    with get_db() as conn:
        conn.execute('UPDATE websites SET framework = ?, startup_file = ? WHERE id = ?', (framework, startup_file, website_id))
        conn.commit()
    
    log_lines = []
    def log(msg):
        log_lines.append(msg)
        if log_callback:
            log_callback(msg)
    
    # Install dependencies
    success, install_logs = install_dependencies(folder, framework, log_callback)
    log_lines.extend(install_logs)
    if not success:
        update_website_status(website_id, 'failed')
        log_website(website_id, "Installation failed", 'error')
        return False, "Dependency installation failed", log_lines
    
    # Inject compatibility routes and fix PORT
    if framework in ['flask', 'fastapi', 'bottle']:
        main_file = os.path.join(folder, startup_file)
        if os.path.exists(main_file):
            injected = inject_compatibility_routes(main_file)
            if injected:
                log("✅ Auto-injected /api/login, /api/signup, and PORT fix")
                log_website(website_id, "Injected compatibility routes and PORT fix", 'info')
    
    # Allocate port
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
    
    cmd = get_start_command(framework, startup_file, port)
    if framework == 'django':
        cmd = [sys.executable, 'manage.py', 'runserver', f'0.0.0.0:{port}']
    
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
            # Still mark as running to allow debugging
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
# स्पेसिफिक रूट्स (पहले)
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

@app.route('/api/signup', methods=['POST'])
def api_signup():
    data = request.get_json()
    if not data:
        return jsonify({'error': 'Invalid request'}), 400
    username = data.get('username', '').strip()
    password = data.get('password', '').strip()
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if get_user_by_username(username):
        return jsonify({'error': 'Username already exists'}), 400
    email = f"{username}@hosting.com"
    with get_db() as conn:
        try:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         (username, email, generate_password_hash(password), 'owner', 'free'))
            conn.commit()
        except sqlite3.IntegrityError:
            return jsonify({'error': 'Username already exists'}), 400
    log_activity(None, 'register', f'User {username} registered')
    return jsonify({'success': True, 'message': 'Account created successfully'})

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

# ---------- Upload ----------
@app.route('/upload', methods=['POST'])
def upload_website():
    if 'user_id' not in session:
        return jsonify({'success': False, 'error': 'Not logged in'}), 401
    user_id = session['user_id']
    user = get_user_by_id(user_id)
    if user['status'] != 'active':
        return jsonify({'success': False, 'error': 'Account disabled'}), 403

    files = request.files.getlist('files[]')
    if not files:
        if 'file' in request.files:
            files = [request.files['file']]
        else:
            return jsonify({'success': False, 'error': 'No files uploaded'}), 400

    zip_files = [f for f in files if f.filename.lower().endswith('.zip')]
    non_zip_files = [f for f in files if not f.filename.lower().endswith('.zip')]

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
        os.remove(zip_path)

    for f in non_zip_files:
        file_path = os.path.join(folder, f.filename)
        try:
            f.save(file_path)
        except Exception as e:
            rollback_upload(website_id, folder)
            return jsonify({'success': False, 'error': f'Failed to save file: {str(e)}'}), 500

    framework = detect_framework(folder)
    startup_file = find_startup_file(folder, framework)
    if startup_file and framework in ['flask', 'fastapi', 'bottle']:
        main_file = os.path.join(folder, startup_file)
        if os.path.exists(main_file):
            injected = inject_compatibility_routes(main_file)
            if injected:
                log_website(website_id, "Injected compatibility routes and PORT fix")

    size_used = calculate_folder_size(folder)
    with get_db() as conn:
        conn.execute('''UPDATE websites SET 
                        website_name = ?, 
                        website_folder = ?,
                        storage_used = ?,
                        website_size = ?,
                        startup_file = ?,
                        framework = ?,
                        status = 'uploaded',
                        updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?''',
                     (os.path.basename(folder) or 'Website',
                      f"website_{website_id}", size_used, size_used, startup_file, framework, website_id))
        conn.commit()

    log_website(website_id, f"Uploaded {len(files)} file(s), framework: {framework}")
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

# ---------- Website Management API ----------
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
# PROXY – सबसे नीचे
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
    headers = {}
    for key, value in request.headers:
        if key.lower() in ['host', 'connection', 'content-length', 'transfer-encoding']:
            continue
        headers[key] = value
    headers['X-Forwarded-For'] = request.remote_addr
    headers['X-Forwarded-Host'] = request.host
    headers['X-Forwarded-Proto'] = request.scheme
    headers['X-Forwarded-Port'] = str(request.environ.get('REMOTE_PORT', '80'))
    cookies = request.cookies
    
    try:
        method = request.method
        data = request.get_data()
        resp = requests.request(
            method=method,
            url=target_url,
            headers=headers,
            data=data if method != 'GET' else None,
            cookies=cookies,
            stream=True,
            timeout=30,
            allow_redirects=False
        )
        if resp.status_code in [301, 302, 303, 307, 308]:
            location = resp.headers.get('Location')
            if location:
                parsed = urllib.parse.urlparse(location)
                if parsed.netloc == f"localhost:{port}" or parsed.netloc == '':
                    base_url = os.environ.get('BASE_URL', request.host_url.rstrip('/'))
                    new_location = f"{base_url}/{slug}/" + parsed.path.lstrip('/')
                    if parsed.query:
                        new_location += '?' + parsed.query
                    resp.headers['Location'] = new_location
        
        response_headers = [(k, v) for k, v in resp.headers.items() 
                           if k.lower() not in ['content-encoding', 'content-length', 'transfer-encoding']]
        
        def generate():
            for chunk in resp.iter_content(chunk_size=8192):
                if chunk:
                    yield chunk
        return Response(
            stream_with_context(generate()),
            status=resp.status_code,
            headers=response_headers
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

# ---------- TEMPLATES (same as before) ----------
# ... (templates remain unchanged - but for brevity I'm skipping them here, but they are the same as the previous code)

# ---------- सर्वर स्टार्ट ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)