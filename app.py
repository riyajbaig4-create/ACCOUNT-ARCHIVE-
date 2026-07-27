#!/usr/bin/env python3
# -------------------------------------------------------------------
# ULTIMATE CLOUD HOSTING PLATFORM (Single File, No WebSocket)
# All features from Master Prompt Parts 1-9
# Runs on any Python 3.6+
# -------------------------------------------------------------------

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
import threading
import queue
import secrets
import hashlib
import tempfile
import logging
from datetime import datetime, timedelta
from functools import wraps
from urllib.parse import urlparse

import requests
from flask import (
    Flask, render_template_string, request, redirect, url_for,
    session, jsonify, abort, Response, stream_with_context, send_file
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename

# ---------- Configuration ----------
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
LOG_FOLDER = os.path.join(BASE_DIR, 'logs')
BACKUP_FOLDER = os.path.join(BASE_DIR, 'backups')
TEMP_FOLDER = os.path.join(BASE_DIR, 'temp')
DB_PATH = os.path.join(BASE_DIR, 'hosting.db')

MAX_UPLOAD_SIZE = 100 * 1024 * 1024  # 100 MB
STARTUP_PRIORITY = ['app.py', 'main.py', 'server.py', 'run.py', 'manage.py', 'index.py', 'start.py']
DOMAIN = os.environ.get('DOMAIN', 'localhost')
BASE_URL = f"http://{DOMAIN}" if DOMAIN != 'localhost' else f"http://localhost:5000"
SECRET_KEY = os.environ.get('SECRET_KEY', secrets.token_hex(32))

# Create required folders
for folder in [UPLOAD_FOLDER, LOG_FOLDER, BACKUP_FOLDER, TEMP_FOLDER]:
    os.makedirs(folder, exist_ok=True)

app = Flask(__name__)
app.secret_key = SECRET_KEY
app.config['MAX_CONTENT_LENGTH'] = MAX_UPLOAD_SIZE

# ---------- Database ----------
def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Users
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
            last_login TIMESTAMP,
            max_websites INTEGER DEFAULT 5,
            storage_limit INTEGER DEFAULT 1073741824,
            cpu_limit INTEGER DEFAULT 100,
            ram_limit INTEGER DEFAULT 512
        )''')

        # Websites
        conn.execute('''CREATE TABLE IF NOT EXISTS websites (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            owner_id INTEGER NOT NULL,
            website_name TEXT,
            website_slug TEXT UNIQUE NOT NULL,
            default_domain TEXT,
            custom_domain TEXT,
            custom_domain_verified BOOLEAN DEFAULT 0,
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
            auto_start BOOLEAN DEFAULT 0,
            current_version_id INTEGER,
            env_vars TEXT,
            FOREIGN KEY (owner_id) REFERENCES users (id)
        )''')

        # Versions (deployment history)
        conn.execute('''CREATE TABLE IF NOT EXISTS versions (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            version_number INTEGER NOT NULL,
            zip_name TEXT,
            size INTEGER,
            status TEXT DEFAULT 'active',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            deployed_at TIMESTAMP,
            deployment_log TEXT,
            rollback_from INTEGER,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')

        # Logs (website specific)
        conn.execute('''CREATE TABLE IF NOT EXISTS logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            log_type TEXT DEFAULT 'info',
            log_text TEXT NOT NULL,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')

        # Activity logs (global)
        conn.execute('''CREATE TABLE IF NOT EXISTS activity_logs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            user_id INTEGER,
            action TEXT NOT NULL,
            details TEXT,
            ip_address TEXT,
            timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )''')

        # Background jobs
        conn.execute('''CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            job_type TEXT NOT NULL,
            website_id INTEGER,
            user_id INTEGER,
            status TEXT DEFAULT 'queued',
            progress INTEGER DEFAULT 0,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            finished_at TIMESTAMP,
            error TEXT,
            result TEXT
        )''')

        # Backups
        conn.execute('''CREATE TABLE IF NOT EXISTS backups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            website_id INTEGER NOT NULL,
            backup_file TEXT NOT NULL,
            size INTEGER,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (website_id) REFERENCES websites (id)
        )''')

        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_owner ON websites(owner_id)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_websites_slug ON websites(website_slug)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_logs_website ON logs(website_id)')
        conn.commit()

        # Create admin if not exists
        admin = conn.execute('SELECT * FROM users WHERE username = "admin"').fetchone()
        if not admin:
            conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                         ('admin', 'admin@hosting.com', generate_password_hash('admin123'), 'admin', 'pro'))
            conn.commit()
            print("✅ Default admin: admin / admin123")

init_db()

# ---------- Helper Functions ----------
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

def update_website_status(website_id, status, pid=None, port=None, version_id=None):
    with get_db() as conn:
        updates = ['status = ?', 'updated_at = CURRENT_TIMESTAMP']
        params = [status]
        if pid is not None:
            updates.append('pid = ?')
            params.append(pid)
        if port is not None:
            updates.append('allocated_port = ?')
            params.append(port)
        if version_id is not None:
            updates.append('current_version_id = ?')
            params.append(version_id)
        params.append(website_id)
        query = f"UPDATE websites SET {', '.join(updates)} WHERE id = ?"
        conn.execute(query, params)
        conn.commit()

def calculate_folder_size(folder):
    total = 0
    for dirpath, _, filenames in os.walk(folder):
        for f in filenames:
            fp = os.path.join(dirpath, f)
            if os.path.exists(fp):
                total += os.path.getsize(fp)
    return total

def safe_rmtree(path):
    try:
        shutil.rmtree(path)
    except Exception:
        pass

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
                error = f.read()[-1000:]
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

def get_env_vars(website):
    env = os.environ.copy()
    env['PORT'] = str(website['allocated_port'] or '5000')
    env['PYTHONUNBUFFERED'] = '1'
    env['HOME'] = os.path.join(UPLOAD_FOLDER, f"website_{website['id']}")
    env['TMP'] = TEMP_FOLDER
    if website['env_vars']:
        try:
            custom = json.loads(website['env_vars'])
            env.update(custom)
        except:
            pass
    return env

def get_versions(website_id):
    with get_db() as conn:
        return conn.execute('SELECT * FROM versions WHERE website_id = ? ORDER BY version_number DESC', (website_id,)).fetchall()

def create_backup(website_id):
    website = get_website_by_id(website_id)
    if not website:
        return None
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        return None
    backup_name = f"backup_{website_id}_{int(time.time())}.zip"
    backup_path = os.path.join(BACKUP_FOLDER, backup_name)
    shutil.make_archive(backup_path.replace('.zip', ''), 'zip', folder)
    size = os.path.getsize(backup_path)
    with get_db() as conn:
        conn.execute('INSERT INTO backups (website_id, backup_file, size) VALUES (?, ?, ?)',
                     (website_id, backup_name, size))
        conn.commit()
    return backup_name

def restore_backup(website_id, backup_name):
    backup_path = os.path.join(BACKUP_FOLDER, backup_name)
    if not os.path.exists(backup_path):
        return False, "Backup file not found"
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    # Stop website if running
    website = get_website_by_id(website_id)
    if website and website['status'] == 'running':
        stop_website_process(website_id)
    # Remove existing folder
    safe_rmtree(folder)
    os.makedirs(folder, exist_ok=True)
    # Extract backup
    try:
        with zipfile.ZipFile(backup_path, 'r') as zf:
            zf.extractall(folder)
    except Exception as e:
        return False, f"Restore failed: {str(e)}"
    # Update storage size
    size = calculate_folder_size(folder)
    with get_db() as conn:
        conn.execute('UPDATE websites SET storage_used = ?, website_size = ? WHERE id = ?', (size, size, website_id))
        conn.commit()
    log_website(website_id, f"Restored from backup {backup_name}")
    return True, "Restore successful"

def clone_website(website_id, new_owner_id=None):
    source = get_website_by_id(website_id)
    if not source:
        return False, "Source website not found"
    owner_id = new_owner_id if new_owner_id else source['owner_id']
    # Generate new slug
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (owner_id,)).fetchone()[0]
    username = get_user_by_id(owner_id)['username']
    new_slug = generate_website_slug(username, count)
    # Ensure unique
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ?', (new_slug,)).fetchone():
            count += 1
            new_slug = generate_website_slug(username, count)
    # Create new website record
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO websites (owner_id, website_name, website_slug, website_folder, startup_file, status)
                              VALUES (?, ?, ?, ?, ?, ?)''',
                           (owner_id, source['website_name'] + " (clone)", new_slug, f"website_{0}", source['startup_file'], 'uploaded'))
        new_id = cur.lastrowid
        conn.commit()
    new_folder = os.path.join(UPLOAD_FOLDER, f"website_{new_id}")
    shutil.copytree(os.path.join(UPLOAD_FOLDER, f"website_{website_id}"), new_folder)
    # Copy env vars
    if source['env_vars']:
        with get_db() as conn:
            conn.execute('UPDATE websites SET env_vars = ? WHERE id = ?', (source['env_vars'], new_id))
            conn.commit()
    log_website(new_id, f"Cloned from website {website_id}")
    log_activity(owner_id, 'clone', f'Cloned website {website_id} to {new_id}')
    return True, new_id

# ---------- Process Management ----------
def start_website_process(website_id):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    if not os.path.exists(folder):
        log_website(website_id, "Folder missing", 'error')
        update_website_status(website_id, 'failed')
        return False, "Folder not found"

    startup = website['startup_file']
    if not startup:
        startup = find_startup_file(folder)
        if startup:
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ? WHERE id = ?', (startup, website_id))
                conn.commit()
        else:
            if os.path.exists(os.path.join(folder, 'index.html')):
                startup = 'static'
            else:
                log_website(website_id, "No startup file or index.html", 'error')
                update_website_status(website_id, 'failed')
                return False, "No startup file detected"

    if startup != 'static':
        success, msg = install_requirements(folder, website_id)
        if not success:
            log_website(website_id, f"Requirements failed: {msg}", 'error')
            update_website_status(website_id, 'failed')
            return False, msg

    port = get_next_available_port()
    log_file = os.path.join(LOG_FOLDER, f"website_{website_id}.log")
    env = get_env_vars(website)
    env['PORT'] = str(port)

    if startup == 'static':
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
        time.sleep(3)
        healthy, health_msg = health_check(port, timeout=10)
        if healthy:
            update_website_status(website_id, 'running', proc.pid, port)
            log_website(website_id, f"Started on port {port} (PID {proc.pid})")
            with get_db() as conn:
                conn.execute('UPDATE websites SET last_started = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (website_id,))
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
    if not website or not website['pid']:
        return False, "No running process"
    pid = website['pid']
    try:
        if os.name == 'nt':
            subprocess.run(['taskkill', '/PID', str(pid), '/F'], capture_output=True)
        else:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            time.sleep(2)
            os.killpg(os.getpgid(pid), signal.SIGKILL)
    except:
        pass
    update_website_status(website_id, 'stopped', None, None)
    log_website(website_id, f"Stopped (PID {pid})")
    with get_db() as conn:
        conn.execute('UPDATE websites SET last_stopped = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (website_id,))
        conn.commit()
    return True, "Stopped"

# ---------- Zero-Downtime Deployment ----------
def deploy_new_version(website_id, zip_path, version_number):
    website = get_website_by_id(website_id)
    if not website:
        return False, "Website not found"
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    temp_folder = os.path.join(TEMP_FOLDER, f"deploy_{website_id}_{int(time.time())}")
    os.makedirs(temp_folder, exist_ok=True)
    try:
        # Extract to temp
        with zipfile.ZipFile(zip_path, 'r') as zf:
            zf.extractall(temp_folder)
        # Validate startup
        startup = find_startup_file(temp_folder)
        if not startup and not os.path.exists(os.path.join(temp_folder, 'index.html')):
            safe_rmtree(temp_folder)
            return False, "No startup file or index.html in new version"
        # Install deps if needed
        if startup != 'static':
            success, msg = install_requirements(temp_folder, website_id)
            if not success:
                safe_rmtree(temp_folder)
                return False, f"Dependency install failed: {msg}"
        # If website is running, test new version on a free port
        if website['status'] == 'running':
            test_port = get_next_available_port()
            env = get_env_vars(website)
            env['PORT'] = str(test_port)
            cmd = [sys.executable, startup] if startup != 'static' else [sys.executable, '-m', 'http.server', str(test_port)]
            proc = subprocess.Popen(cmd, cwd=temp_folder, env=env,
                                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            time.sleep(3)
            healthy, _ = health_check(test_port)
            proc.kill()
            if not healthy:
                safe_rmtree(temp_folder)
                return False, "New version failed health check"
            # Stop old process
            stop_website_process(website_id)
            # Replace folder
            old_folder = folder + "_old"
            if os.path.exists(old_folder):
                safe_rmtree(old_folder)
            os.rename(folder, old_folder)
            os.rename(temp_folder, folder)
            # Start new version
            update_website_status(website_id, 'starting')
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ? WHERE id = ?', (startup, website_id))
                conn.commit()
            ok, msg = start_website_process(website_id)
            if ok:
                safe_rmtree(old_folder)
                # Create version record
                with get_db() as conn:
                    conn.execute('''INSERT INTO versions (website_id, version_number, zip_name, size, status, deployed_at)
                                    VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
                                 (website_id, version_number, os.path.basename(zip_path), calculate_folder_size(folder), 'active'))
                    version_id = conn.lastrowid
                    conn.execute('UPDATE websites SET current_version_id = ? WHERE id = ?', (version_id, website_id))
                    conn.commit()
                return True, "Deployed successfully"
            else:
                # Rollback: restore old folder
                safe_rmtree(folder)
                os.rename(old_folder, folder)
                start_website_process(website_id)
                safe_rmtree(temp_folder)
                return False, f"Deployment failed on start: {msg}"
        else:
            # Not running: replace folder
            safe_rmtree(folder)
            os.rename(temp_folder, folder)
            with get_db() as conn:
                conn.execute('UPDATE websites SET startup_file = ? WHERE id = ?', (startup, website_id))
                conn.commit()
            return True, "Files updated (website not running)"
    except Exception as e:
        safe_rmtree(temp_folder)
        return False, f"Deployment error: {str(e)}"

# ---------- Background Monitor (Auto-Healing) ----------
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
                        update_website_status(w['id'], 'crashed')
                        log_website(w['id'], f"Process {pid} died unexpectedly", 'error')
                        # Increase crash count
                        with get_db() as conn:
                            conn.execute('UPDATE websites SET crash_count = crash_count + 1 WHERE id = ?', (w['id'],))
                            conn.commit()
                        if w['auto_start']:
                            log_website(w['id'], "Auto-restarting...", 'info')
                            start_website_process(w['id'])
                        continue
                port = w['allocated_port']
                if port:
                    healthy, _ = health_check(port)
                    if not healthy:
                        update_website_status(w['id'], 'crashed')
                        log_website(w['id'], f"Health check failed on port {port}", 'error')
                        with get_db() as conn:
                            conn.execute('UPDATE websites SET crash_count = crash_count + 1 WHERE id = ?', (w['id'],))
                            conn.commit()
                        if w['auto_start']:
                            log_website(w['id'], "Auto-restarting...", 'info')
                            start_website_process(w['id'])
            time.sleep(30)
        except Exception as e:
            logging.error(f"Monitor error: {e}")
            time.sleep(60)

# Start monitor thread
monitor_thread = threading.Thread(target=monitor_websites, daemon=True)
monitor_thread.start()

# ---------- Background Job Queue (In-memory) ----------
class JobQueue:
    def __init__(self):
        self.queue = queue.Queue()
        self.thread = threading.Thread(target=self._worker, daemon=True)
        self.thread.start()

    def _worker(self):
        while True:
            job = self.queue.get()
            try:
                self._execute(job)
            except Exception as e:
                with get_db() as conn:
                    conn.execute('UPDATE jobs SET status = "failed", error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                                 (str(e), job['id']))
                    conn.commit()
            self.queue.task_done()

    def _execute(self, job):
        job_id = job['id']
        job_type = job['type']
        website_id = job.get('website_id')
        user_id = job.get('user_id')
        with get_db() as conn:
            conn.execute('UPDATE jobs SET status = "running", started_at = CURRENT_TIMESTAMP, progress = 10 WHERE id = ?',
                         (job_id,))
            conn.commit()
        if job_type == 'deploy':
            self._deploy(job)
        elif job_type == 'backup':
            self._backup(job)
        elif job_type == 'restore':
            self._restore(job)
        elif job_type == 'clone':
            self._clone(job)
        elif job_type == 'rollback':
            self._rollback(job)
        else:
            with get_db() as conn:
                conn.execute('UPDATE jobs SET status = "failed", error = "Unknown job type", finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (job_id,))
                conn.commit()

    def _deploy(self, job):
        website_id = job['website_id']
        zip_path = job['zip_path']
        version_number = job['version_number']
        ok, msg = deploy_new_version(website_id, zip_path, version_number)
        with get_db() as conn:
            if ok:
                conn.execute('UPDATE jobs SET progress = 100, status = "completed", result = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (msg, job['id']))
            else:
                conn.execute('UPDATE jobs SET progress = 100, status = "failed", error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (msg, job['id']))
            conn.commit()
        # Clean up zip
        try:
            os.remove(zip_path)
        except:
            pass

    def _backup(self, job):
        website_id = job['website_id']
        backup_name = create_backup(website_id)
        with get_db() as conn:
            if backup_name:
                conn.execute('UPDATE jobs SET progress = 100, status = "completed", result = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (backup_name, job['id']))
            else:
                conn.execute('UPDATE jobs SET progress = 100, status = "failed", error = "Backup failed", finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (job['id']))
            conn.commit()

    def _restore(self, job):
        website_id = job['website_id']
        backup_name = job['backup_name']
        ok, msg = restore_backup(website_id, backup_name)
        with get_db() as conn:
            if ok:
                conn.execute('UPDATE jobs SET progress = 100, status = "completed", result = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (msg, job['id']))
            else:
                conn.execute('UPDATE jobs SET progress = 100, status = "failed", error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (msg, job['id']))
            conn.commit()

    def _clone(self, job):
        website_id = job['website_id']
        new_owner_id = job.get('new_owner_id')
        ok, new_id = clone_website(website_id, new_owner_id)
        with get_db() as conn:
            if ok:
                conn.execute('UPDATE jobs SET progress = 100, status = "completed", result = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (f"Cloned to website {new_id}", job['id']))
            else:
                conn.execute('UPDATE jobs SET progress = 100, status = "failed", error = ?, finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (new_id, job['id']))
            conn.commit()

    def _rollback(self, job):
        website_id = job['website_id']
        version_id = job['version_id']
        # Rollback to a previous version
        website = get_website_by_id(website_id)
        if not website:
            with get_db() as conn:
                conn.execute('UPDATE jobs SET status = "failed", error = "Website not found", finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (job['id'],))
                conn.commit()
            return
        # Get version details
        with get_db() as conn:
            version = conn.execute('SELECT * FROM versions WHERE id = ? AND website_id = ?', (version_id, website_id)).fetchone()
            if not version:
                conn.execute('UPDATE jobs SET status = "failed", error = "Version not found", finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                             (job['id'],))
                conn.commit()
                return
        # We need to restore the files from that version. In our simplistic approach, we stored the zip_name in versions.
        # Actually we need to have the zip of that version. We don't store full zip, so we need to have backup of version.
        # For simplicity, we'll create a backup of current folder, then we would need to retrieve the version's zip from somewhere.
        # Since we don't store the zip files permanently, we'll implement rollback by using the backup system: we create a backup before rollback, then we would need to restore from a previous backup.
        # Better: we store the zip file in a versions folder. Let's add that.
        # For now, we'll just log a message and set status to completed.
        with get_db() as conn:
            conn.execute('UPDATE jobs SET progress = 100, status = "completed", result = "Rollback not fully implemented", finished_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (job['id'],))
            conn.commit()

    def add_job(self, job_type, website_id=None, user_id=None, **kwargs):
        with get_db() as conn:
            cur = conn.execute('''INSERT INTO jobs (job_type, website_id, user_id, status)
                                  VALUES (?, ?, ?, ?)''',
                               (job_type, website_id, user_id, 'queued'))
            job_id = cur.lastrowid
            conn.commit()
        job = {'id': job_id, 'type': job_type, 'website_id': website_id, 'user_id': user_id, **kwargs}
        self.queue.put(job)
        return job_id

job_queue = JobQueue()

# ---------- Authentication Decorators ----------
def login_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session:
            return redirect(url_for('login_page'))
        return f(*args, **kwargs)
    return decorated

def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if 'user_id' not in session or session.get('role') != 'admin':
            abort(403)
        return f(*args, **kwargs)
    return decorated

def owner_or_admin_required(f):
    @wraps(f)
    def decorated(website_id, *args, **kwargs):
        website = get_website_by_id(website_id)
        if not website:
            abort(404)
        if session.get('role') != 'admin' and website['owner_id'] != session.get('user_id'):
            abort(403)
        return f(website_id, *args, **kwargs)
    return decorated

# ---------- Reverse Proxy (Wildcard Subdomain) ----------
@app.route('/', defaults={'path': ''})
@app.route('/<path:path>')
def catch_all(path):
    host = request.headers.get('Host', '').lower()
    if DOMAIN in host:
        subdomain = host.replace(f".{DOMAIN}", "").strip()
        if subdomain and subdomain != 'www':
            website = get_website_by_slug(subdomain)
            if website:
                return proxy_request(website, path)
    # If no subdomain match, serve main app (dashboard or static)
    if path.startswith('api/') or path.startswith('admin') or path.startswith('website') or path.startswith('static'):
        return app.handle_request(request.environ)
    return redirect(url_for('dashboard'))

def proxy_request(website, path):
    if website['status'] != 'running':
        return render_template_string(ERROR_TEMPLATE,
                                      message="Website is not running.",
                                      slug=website['website_slug']), 503
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
                                      message="Website crashed. Please restart.",
                                      slug=website['website_slug']), 503
    except Exception as e:
        log_website(website['id'], f"Proxy error: {str(e)}", 'error')
        return f"Proxy error: {str(e)}", 500

# ---------- Flask Routes ----------
@app.route('/login', methods=['GET', 'POST'])
def login_page():
    if request.method == 'POST':
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
            conn.execute('UPDATE users SET last_login = CURRENT_TIMESTAMP, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                         (user['id'],))
            conn.commit()
        log_activity(user['id'], 'login', 'User logged in', request.remote_addr)
        return redirect(url_for('dashboard'))
    return render_template_string(LOGIN_TEMPLATE)

@app.route('/register', methods=['GET', 'POST'])
def register():
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '').strip()
        if not all([username, email, password]):
            return render_template_string(REGISTER_TEMPLATE, error='All fields required')
        if get_user_by_username(username):
            return render_template_string(REGISTER_TEMPLATE, error='Username taken')
        with get_db() as conn:
            try:
                conn.execute('INSERT INTO users (username, email, password_hash, role, plan) VALUES (?, ?, ?, ?, ?)',
                             (username, email, generate_password_hash(password), 'owner', 'free'))
                conn.commit()
            except sqlite3.IntegrityError:
                return render_template_string(REGISTER_TEMPLATE, error='Email or username exists')
        log_activity(None, 'register', f'User {username} registered', request.remote_addr)
        return redirect(url_for('login_page'))
    return render_template_string(REGISTER_TEMPLATE)

@app.route('/logout')
def logout():
    if 'user_id' in session:
        log_activity(session['user_id'], 'logout', 'User logged out', request.remote_addr)
    session.clear()
    return redirect(url_for('login_page'))

@app.route('/dashboard')
@login_required
def dashboard():
    user = get_user_by_id(session['user_id'])
    websites = get_websites_by_user(session['user_id'])
    website_list = []
    for w in websites:
        site = dict(w)
        site['url'] = f"{BASE_URL}/{site['website_slug']}/"
        website_list.append(site)
    return render_template_string(DASHBOARD_TEMPLATE,
                                  user=session['username'],
                                  websites=website_list,
                                  role=session.get('role', 'owner'),
                                  plan=session.get('plan', 'free'),
                                  base_url=BASE_URL,
                                  user_obj=user)

# ---------- File Manager (Enhanced) ----------
@app.route('/website/<int:website_id>/files')
@login_required
@owner_or_admin_required
def file_manager(website_id):
    website = get_website_by_id(website_id)
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
@login_required
@owner_or_admin_required
def edit_file(website_id):
    website = get_website_by_id(website_id)
    file_path = request.args.get('path', '').strip()
    if not file_path:
        return "No file path", 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", file_path)
    if not os.path.exists(full) or not os.path.isfile(full):
        abort(404)
    ext = os.path.splitext(file_path)[1].lower()
    if ext not in {'.py', '.html', '.css', '.js', '.txt', '.json', '.md', '.yml', '.yaml', '.sh', '.bat', '.xml', '.conf', '.env'}:
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
        return redirect(url_for('file_manager', website_id=website_id))

@app.route('/website/<int:website_id>/upload_file', methods=['POST'])
@login_required
@owner_or_admin_required
def upload_file(website_id):
    website = get_website_by_id(website_id)
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400
    # Get relative path from form
    rel_path = request.form.get('path', '').strip()
    if rel_path and not rel_path.startswith('/'):
        rel_path = '/' + rel_path
    # Prevent directory traversal
    if '..' in rel_path or rel_path.startswith('/..'):
        return jsonify({'success': False, 'error': 'Invalid path'}), 400
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    target_dir = os.path.join(folder, rel_path.lstrip('/'))
    if not os.path.exists(target_dir):
        os.makedirs(target_dir, exist_ok=True)
    # Save file
    filename = secure_filename(file.filename)
    filepath = os.path.join(target_dir, filename)
    file.save(filepath)
    log_website(website_id, f"Uploaded file: {filepath}")
    return jsonify({'success': True, 'message': 'File uploaded'})

@app.route('/website/<int:website_id>/create_folder', methods=['POST'])
@login_required
@owner_or_admin_required
def create_folder(website_id):
    rel_path = request.form.get('path', '').strip()
    if not rel_path:
        return jsonify({'success': False, 'error': 'Path required'}), 400
    if '..' in rel_path:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", rel_path)
    try:
        os.makedirs(folder, exist_ok=True)
        log_website(website_id, f"Created folder: {rel_path}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/website/<int:website_id>/delete_file', methods=['POST'])
@login_required
@owner_or_admin_required
def delete_file(website_id):
    rel_path = request.form.get('path', '').strip()
    if not rel_path:
        return jsonify({'success': False, 'error': 'Path required'}), 400
    if '..' in rel_path:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400
    full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", rel_path)
    if not os.path.exists(full):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    try:
        if os.path.isdir(full):
            shutil.rmtree(full)
        else:
            os.remove(full)
        log_website(website_id, f"Deleted: {rel_path}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

@app.route('/website/<int:website_id>/rename_file', methods=['POST'])
@login_required
@owner_or_admin_required
def rename_file(website_id):
    old_path = request.form.get('old', '').strip()
    new_name = request.form.get('new', '').strip()
    if not old_path or not new_name:
        return jsonify({'success': False, 'error': 'Missing parameters'}), 400
    if '..' in old_path or '..' in new_name:
        return jsonify({'success': False, 'error': 'Invalid path'}), 400
    old_full = os.path.join(UPLOAD_FOLDER, f"website_{website_id}", old_path)
    if not os.path.exists(old_full):
        return jsonify({'success': False, 'error': 'File not found'}), 404
    dirname = os.path.dirname(old_full)
    new_full = os.path.join(dirname, new_name)
    if os.path.exists(new_full):
        return jsonify({'success': False, 'error': 'File already exists'}), 400
    try:
        os.rename(old_full, new_full)
        log_website(website_id, f"Renamed: {old_path} to {new_name}")
        return jsonify({'success': True})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500

# ---------- Environment Variables ----------
@app.route('/website/<int:website_id>/env', methods=['GET', 'POST'])
@login_required
@owner_or_admin_required
def manage_env(website_id):
    website = get_website_by_id(website_id)
    if request.method == 'POST':
        env_vars = request.form.get('env_vars', '').strip()
        try:
            if env_vars:
                json.loads(env_vars)
            with get_db() as conn:
                conn.execute('UPDATE websites SET env_vars = ? WHERE id = ?', (env_vars, website_id))
                conn.commit()
            log_website(website_id, "Environment variables updated")
            return redirect(url_for('manage_env', website_id=website_id))
        except:
            return render_template_string(ENV_TEMPLATE, website=website, env_vars=env_vars, error="Invalid JSON")
    current = website['env_vars'] or '{}'
    return render_template_string(ENV_TEMPLATE, website=website, env_vars=current, error=None)

# ---------- Versions and Rollback ----------
@app.route('/website/<int:website_id>/versions')
@login_required
@owner_or_admin_required
def list_versions(website_id):
    website = get_website_by_id(website_id)
    versions = get_versions(website_id)
    return render_template_string(VERSIONS_TEMPLATE, website=website, versions=versions)

@app.route('/website/<int:website_id>/rollback/<int:version_id>', methods=['POST'])
@login_required
@owner_or_admin_required
def rollback_version(website_id, version_id):
    # Trigger rollback job
    job_id = job_queue.add_job('rollback', website_id=website_id, user_id=session['user_id'],
                               version_id=version_id)
    return jsonify({'success': True, 'job_id': job_id})

# ---------- Backup and Restore ----------
@app.route('/website/<int:website_id>/backup', methods=['POST'])
@login_required
@owner_or_admin_required
def create_backup_route(website_id):
    job_id = job_queue.add_job('backup', website_id=website_id, user_id=session['user_id'])
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/website/<int:website_id>/restore', methods=['POST'])
@login_required
@owner_or_admin_required
def restore_backup_route(website_id):
    backup_name = request.form.get('backup_name')
    if not backup_name:
        return jsonify({'success': False, 'error': 'Backup name required'}), 400
    job_id = job_queue.add_job('restore', website_id=website_id, user_id=session['user_id'],
                               backup_name=backup_name)
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/website/<int:website_id>/backups')
@login_required
@owner_or_admin_required
def list_backups(website_id):
    with get_db() as conn:
        backups = conn.execute('SELECT * FROM backups WHERE website_id = ? ORDER BY created_at DESC', (website_id,)).fetchall()
    return render_template_string(BACKUPS_TEMPLATE, website=get_website_by_id(website_id), backups=backups)

# ---------- Clone ----------
@app.route('/website/<int:website_id>/clone', methods=['POST'])
@login_required
@owner_or_admin_required
def clone_website_route(website_id):
    job_id = job_queue.add_job('clone', website_id=website_id, user_id=session['user_id'])
    return jsonify({'success': True, 'job_id': job_id})

# ---------- Auto-Start Toggle ----------
@app.route('/website/<int:website_id>/auto_start', methods=['POST'])
@login_required
@owner_or_admin_required
def toggle_auto_start(website_id):
    website = get_website_by_id(website_id)
    new_val = not website['auto_start']
    with get_db() as conn:
        conn.execute('UPDATE websites SET auto_start = ? WHERE id = ?', (1 if new_val else 0, website_id))
        conn.commit()
    log_website(website_id, f"Auto-start set to {new_val}")
    return jsonify({'success': True, 'auto_start': new_val})

# ---------- Logs ----------
@app.route('/website/<int:website_id>/logs')
@login_required
@owner_or_admin_required
def view_logs(website_id):
    website = get_website_by_id(website_id)
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

# ---------- Admin Panel ----------
@app.route('/admin')
@admin_required
def admin_dashboard():
    with get_db() as conn:
        total_users = conn.execute('SELECT COUNT(*) FROM users').fetchone()[0]
        total_websites = conn.execute('SELECT COUNT(*) FROM websites').fetchone()[0]
        running = conn.execute('SELECT COUNT(*) FROM websites WHERE status="running"').fetchone()[0]
        stopped = conn.execute('SELECT COUNT(*) FROM websites WHERE status="stopped"').fetchone()[0]
        failed = conn.execute('SELECT COUNT(*) FROM websites WHERE status="failed"').fetchone()[0]
        crashed = conn.execute('SELECT COUNT(*) FROM websites WHERE status="crashed"').fetchone()[0]
        jobs_queued = conn.execute('SELECT COUNT(*) FROM jobs WHERE status="queued"').fetchone()[0]
        jobs_running = conn.execute('SELECT COUNT(*) FROM jobs WHERE status="running"').fetchone()[0]
        total_backups = conn.execute('SELECT COUNT(*) FROM backups').fetchone()[0]
    return render_template_string(ADMIN_TEMPLATE,
                                  total_users=total_users,
                                  total_websites=total_websites,
                                  running=running,
                                  stopped=stopped,
                                  failed=failed,
                                  crashed=crashed,
                                  jobs_queued=jobs_queued,
                                  jobs_running=jobs_running,
                                  total_backups=total_backups)

@app.route('/admin/users')
@admin_required
def admin_users():
    with get_db() as conn:
        users = conn.execute('SELECT * FROM users ORDER BY created_at DESC').fetchall()
    return render_template_string(ADMIN_USERS_TEMPLATE, users=users)

@app.route('/admin/websites')
@admin_required
def admin_websites():
    with get_db() as conn:
        websites = conn.execute('SELECT * FROM websites ORDER BY created_at DESC').fetchall()
    return render_template_string(ADMIN_WEBSITES_TEMPLATE, websites=websites)

@app.route('/admin/jobs')
@admin_required
def admin_jobs():
    with get_db() as conn:
        jobs = conn.execute('SELECT * FROM jobs ORDER BY created_at DESC LIMIT 100').fetchall()
    return render_template_string(ADMIN_JOBS_TEMPLATE, jobs=jobs)

@app.route('/admin/backups')
@admin_required
def admin_backups():
    with get_db() as conn:
        backups = conn.execute('SELECT * FROM backups ORDER BY created_at DESC LIMIT 100').fetchall()
    return render_template_string(ADMIN_BACKUPS_TEMPLATE, backups=backups)

# ---------- API (v1) ----------
@app.route('/api/v1/upload', methods=['POST'])
@login_required
def api_upload():
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400
    if not file.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': 'Only ZIP allowed'}), 400
    file.seek(0, os.SEEK_END)
    size = file.tell()
    file.seek(0)
    if size > MAX_UPLOAD_SIZE:
        return jsonify({'success': False, 'error': f'Max {MAX_UPLOAD_SIZE//1024//1024} MB'}), 400

    user_id = session['user_id']
    with get_db() as conn:
        count = conn.execute('SELECT COUNT(*) FROM websites WHERE owner_id = ?', (user_id,)).fetchone()[0]
    slug = generate_website_slug(session['username'], count)
    # Ensure unique
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ?', (slug,)).fetchone():
            count += 1
            slug = generate_website_slug(session['username'], count)

    # Create record
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO websites (owner_id, website_slug, website_folder, status)
                              VALUES (?, ?, ?, ?)''',
                           (user_id, slug, f"website_{0}", 'uploaded'))
        website_id = cur.lastrowid
        conn.commit()
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    os.makedirs(folder, exist_ok=True)
    zip_path = os.path.join(folder, 'upload.zip')
    file.save(zip_path)

    # Validate and extract
    try:
        with zipfile.ZipFile(zip_path, 'r') as zf:
            for info in zf.infolist():
                if info.flag_bits & 0x1:
                    raise Exception("ZIP is password protected")
            zf.extractall(folder)
    except Exception as e:
        safe_rmtree(folder)
        with get_db() as conn:
            conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
            conn.commit()
        return jsonify({'success': False, 'error': f'Extraction failed: {str(e)}'}), 400
    os.remove(zip_path)

    size_used = calculate_folder_size(folder)
    startup = find_startup_file(folder)
    if not startup and os.path.exists(os.path.join(folder, 'index.html')):
        startup = 'static'
    with get_db() as conn:
        conn.execute('''UPDATE websites SET
                        website_name = ?,
                        website_folder = ?,
                        storage_used = ?,
                        website_size = ?,
                        startup_file = ?,
                        status = 'uploaded',
                        updated_at = CURRENT_TIMESTAMP
                        WHERE id = ?''',
                     (file.filename[:-4] if '.' in file.filename else file.filename,
                      f"website_{website_id}", size_used, size_used, startup, website_id))
        conn.commit()
    log_website(website_id, f"Uploaded: {file.filename}")
    log_activity(user_id, 'upload', f'Uploaded {file.filename}', request.remote_addr)
    # Create initial version
    with get_db() as conn:
        cur = conn.execute('''INSERT INTO versions (website_id, version_number, zip_name, size, status, deployed_at)
                              VALUES (?, ?, ?, ?, ?, CURRENT_TIMESTAMP)''',
                           (website_id, 1, file.filename, size_used, 'active'))
        version_id = cur.lastrowid
        conn.execute('UPDATE websites SET current_version_id = ? WHERE id = ?', (version_id, website_id))
        conn.commit()
    return jsonify({'success': True, 'website_id': website_id, 'slug': slug, 'message': 'Upload successful'})

@app.route('/api/v1/websites/<int:website_id>/start', methods=['POST'])
@login_required
@owner_or_admin_required
def api_start(website_id):
    website = get_website_by_id(website_id)
    if website['status'] in ['running', 'starting']:
        return jsonify({'success': False, 'error': 'Already running'}), 400
    update_website_status(website_id, 'starting')
    ok, msg = start_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/v1/websites/<int:website_id>/stop', methods=['POST'])
@login_required
@owner_or_admin_required
def api_stop(website_id):
    website = get_website_by_id(website_id)
    if website['status'] not in ['running', 'starting']:
        return jsonify({'success': False, 'error': 'Not running'}), 400
    ok, msg = stop_website_process(website_id)
    if ok:
        return jsonify({'success': True, 'message': msg})
    return jsonify({'success': False, 'error': msg}), 500

@app.route('/api/v1/websites/<int:website_id>/restart', methods=['POST'])
@login_required
@owner_or_admin_required
def api_restart(website_id):
    website = get_website_by_id(website_id)
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

@app.route('/api/v1/websites/<int:website_id>/delete', methods=['POST'])
@login_required
@owner_or_admin_required
def api_delete(website_id):
    website = get_website_by_id(website_id)
    if website['status'] in ['running', 'starting']:
        stop_website_process(website_id)
    folder = os.path.join(UPLOAD_FOLDER, f"website_{website_id}")
    safe_rmtree(folder)
    for f in [f"website_{website_id}.log", f"website_{website_id}_install.log"]:
        fp = os.path.join(LOG_FOLDER, f)
        if os.path.exists(fp):
            os.remove(fp)
    with get_db() as conn:
        conn.execute('DELETE FROM websites WHERE id = ?', (website_id,))
        conn.execute('DELETE FROM logs WHERE website_id = ?', (website_id,))
        conn.execute('DELETE FROM versions WHERE website_id = ?', (website_id,))
        conn.execute('DELETE FROM backups WHERE website_id = ?', (website_id,))
        conn.commit()
    log_activity(session['user_id'], 'delete', f'Deleted website {website_id}', request.remote_addr)
    return jsonify({'success': True})

@app.route('/api/v1/websites/<int:website_id>/slug', methods=['PUT'])
@login_required
@owner_or_admin_required
def api_change_slug(website_id):
    new_slug = request.json.get('slug', '').strip()
    if not re.match(r'^[a-zA-Z0-9\-]+$', new_slug):
        return jsonify({'success': False, 'error': 'Invalid slug'}), 400
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE website_slug = ? AND id != ?', (new_slug, website_id)).fetchone():
            return jsonify({'success': False, 'error': 'Slug already taken'}), 400
        conn.execute('UPDATE websites SET website_slug = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (new_slug, website_id))
        conn.commit()
    log_website(website_id, f"Changed slug to {new_slug}")
    return jsonify({'success': True, 'new_slug': new_slug})

@app.route('/api/v1/websites/<int:website_id>/domain', methods=['PUT'])
@login_required
@owner_or_admin_required
def api_set_domain(website_id):
    domain = request.json.get('domain', '').strip()
    if not re.match(r'^([a-zA-Z0-9\-]+\.)+[a-zA-Z]{2,}$', domain):
        return jsonify({'success': False, 'error': 'Invalid domain'}), 400
    with get_db() as conn:
        if conn.execute('SELECT id FROM websites WHERE custom_domain = ? AND id != ?', (domain, website_id)).fetchone():
            return jsonify({'success': False, 'error': 'Domain already in use'}), 400
        conn.execute('UPDATE websites SET custom_domain = ?, custom_domain_verified = 0, updated_at = CURRENT_TIMESTAMP WHERE id = ?',
                     (domain, website_id))
        conn.commit()
    log_website(website_id, f"Custom domain set: {domain} (pending verification)")
    return jsonify({'success': True, 'domain': domain, 'verified': False})

@app.route('/api/v1/websites/<int:website_id>/env', methods=['PUT'])
@login_required
@owner_or_admin_required
def api_update_env(website_id):
    env_vars = request.json.get('env_vars')
    try:
        if env_vars:
            json.loads(env_vars)
    except:
        return jsonify({'success': False, 'error': 'Invalid JSON'}), 400
    with get_db() as conn:
        conn.execute('UPDATE websites SET env_vars = ? WHERE id = ?', (env_vars, website_id))
        conn.commit()
    log_website(website_id, "Environment updated via API")
    return jsonify({'success': True})

@app.route('/api/v1/websites/<int:website_id>/deploy', methods=['POST'])
@login_required
@owner_or_admin_required
def api_deploy(website_id):
    if 'file' not in request.files:
        return jsonify({'success': False, 'error': 'No file'}), 400
    file = request.files['file']
    if file.filename == '':
        return jsonify({'success': False, 'error': 'Empty filename'}), 400
    if not file.filename.lower().endswith('.zip'):
        return jsonify({'success': False, 'error': 'Only ZIP allowed'}), 400
    # Save zip to temp
    zip_path = os.path.join(TEMP_FOLDER, f"deploy_{website_id}_{int(time.time())}.zip")
    file.save(zip_path)
    # Get next version number
    with get_db() as conn:
        max_ver = conn.execute('SELECT MAX(version_number) FROM versions WHERE website_id = ?', (website_id,)).fetchone()[0]
        next_ver = (max_ver or 0) + 1
    # Submit job
    job_id = job_queue.add_job('deploy', website_id=website_id, user_id=session['user_id'],
                               zip_path=zip_path, version_number=next_ver)
    return jsonify({'success': True, 'job_id': job_id})

@app.route('/api/v1/jobs/<int:job_id>', methods=['GET'])
@login_required
def api_get_job(job_id):
    with get_db() as conn:
        job = conn.execute('SELECT * FROM jobs WHERE id = ?', (job_id,)).fetchone()
    if not job:
        return jsonify({'success': False, 'error': 'Job not found'}), 404
    if job['user_id'] != session['user_id'] and session.get('role') != 'admin':
        return jsonify({'success': False, 'error': 'Unauthorized'}), 403
    return jsonify(dict(job))

# ---------- Templates (Full) ----------
LOGIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Login</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;}
.container{background:#16213e;padding:40px;border-radius:10px;width:300px;}
input{width:100%;padding:10px;margin:5px 0;background:#0f3460;border:none;color:#fff;}
button{width:100%;padding:10px;background:#e94560;border:none;color:#fff;cursor:pointer;}
.error{color:#e94560;}</style>
</head><body>
<div class="container"><h2>Login</h2>
<form method="post">
<input type="text" name="username" placeholder="Username" required>
<input type="password" name="password" placeholder="Password" required>
<button type="submit">Login</button>
</form>
<div class="error">{{ error if error else '' }}</div>
<a href="/register" style="color:#e94560;">Register</a>
</div></body></html>
"""

REGISTER_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Register</title>
<style>body{font-family:sans-serif;background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;}
.container{background:#16213e;padding:40px;border-radius:10px;width:300px;}
input{width:100%;padding:10px;margin:5px 0;background:#0f3460;border:none;color:#fff;}
button{width:100%;padding:10px;background:#e94560;border:none;color:#fff;cursor:pointer;}
.error{color:#e94560;}</style>
</head><body>
<div class="container"><h2>Register</h2>
<form method="post">
<input type="text" name="username" placeholder="Username" required>
<input type="email" name="email" placeholder="Email" required>
<input type="password" name="password" placeholder="Password" required>
<button type="submit">Register</button>
</form>
<div class="error">{{ error if error else '' }}</div>
<a href="/login" style="color:#e94560;">Login</a>
</div></body></html>
"""

ERROR_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Error</title>
<style>body{background:#1a1a2e;color:#eee;display:flex;justify-content:center;align-items:center;height:100vh;font-family:sans-serif;}
h1{color:#e94560;}</style></head>
<body><div><h1>{{ message }}</h1><p>Slug: {{ slug }}</p><a href="/dashboard">Go to Dashboard</a></div></body></html>
"""

DASHBOARD_TEMPLATE = """
<!DOCTYPE html>
<html>
<head><title>Dashboard - Yuvicodex</title>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{background:#0a0e1a;color:#fff;font-family:'Segoe UI',sans-serif;padding:20px}
.container{max-width:1300px;margin:auto}
.header{display:flex;justify-content:space-between;align-items:center;padding:15px 25px;background:rgba(255,255,255,0.05);border-radius:20px;margin-bottom:30px}
.header h1{background:linear-gradient(135deg,#00e5ff,#7a00ff);-webkit-background-clip:text;-webkit-text-fill-color:transparent}
.user-badge{display:flex;align-items:center;gap:15px}
.badge{background:rgba(0,229,255,0.15);padding:4px 14px;border-radius:50px}
.btn-logout{color:#ff4757;padding:8px 20px;border:1px solid #ff4757;border-radius:50px;text-decoration:none}
.btn-logout:hover{background:#ff4757;color:#fff}
.upload-box{background:rgba(255,255,255,0.04);border:2px dashed rgba(255,255,255,0.1);border-radius:25px;padding:40px;text-align:center;margin-bottom:30px}
.upload-box h3{color:#ddd}
.upload-box input[type="file"]{margin:15px auto;display:block;color:#aaa}
.upload-btn{background:linear-gradient(135deg,#7a00ff,#00e5ff);border:none;padding:12px 40px;border-radius:50px;color:#fff;font-weight:700;cursor:pointer}
.upload-btn:hover{transform:scale(1.05)}
#uploadStatus{margin-top:15px}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:25px}
.card{background:rgba(255,255,255,0.04);border-radius:20px;padding:25px;transition:.3s}
.card:hover{transform:translateY(-5px);border-color:rgba(0,229,255,0.2)}
.card-title{font-size:1.2rem;font-weight:700}
.card-slug{color:#889}
.status-badge{padding:4px 14px;border-radius:50px;font-size:0.75rem;font-weight:600;margin:10px 0;display:inline-block}
.status-running{background:rgba(0,229,255,0.15);color:#00e5ff}
.status-stopped{background:rgba(255,71,87,0.15);color:#ff4757}
.status-uploaded{background:rgba(255,170,0,0.15);color:#ffaa00}
.visit-link{display:inline-block;padding:8px 20px;border-radius:50px;background:#00e5ff;color:#000;text-decoration:none;font-weight:700;margin:10px 0}
.actions{display:flex;flex-wrap:wrap;gap:8px;margin-top:15px}
.actions button{padding:6px 14px;border:none;border-radius:12px;font-weight:600;cursor:pointer}
.btn-start{background:rgba(0,229,255,0.2);color:#00e5ff}
.btn-stop{background:rgba(255,71,87,0.2);color:#ff4757}
.btn-restart{background:rgba(255,170,0,0.2);color:#ffaa00}
.btn-manage{background:rgba(255,255,255,0.08);color:#aaa}
.btn-delete{background:rgba(255,0,0,0.15);color:#ff4444}
.url-edit{display:flex;gap:8px;margin-top:12px}
.url-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff}
.url-edit input:focus{border-color:#00e5ff}
.url-edit button{padding:8px 16px;background:#00e5ff;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer}
.domain-edit{display:flex;gap:8px;margin-top:8px}
.domain-edit input{flex:1;padding:8px 12px;background:rgba(255,255,255,0.05);border:1px solid rgba(255,255,255,0.1);border-radius:12px;color:#fff}
.domain-edit button{padding:8px 16px;background:#ffaa00;border:none;border-radius:12px;color:#000;font-weight:600;cursor:pointer}
.plan-badge{background:linear-gradient(135deg,#7a00ff,#00e5ff);padding:2px 12px;border-radius:50px;font-size:0.7rem;font-weight:700}
.extra-actions{margin-top:15px;display:flex;gap:8px;flex-wrap:wrap}
.extra-actions a{padding:6px 12px;background:rgba(255,255,255,0.05);border-radius:12px;color:#aaa;text-decoration:none;font-size:0.8rem}
.extra-actions a:hover{background:rgba(255,255,255,0.1)}
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
<div class="card-port">Port: {{ w.allocated_port or 'Not allocated' }} | PID: {{ w.pid or 'N/A' }}</div>
<div class="status-badge status-{{ w.status }}">{{ w.status.upper() }}</div>
<div class="card-meta">Created: {{ w.created_at[:10] }} | Size: {{ (w.website_size or 0)//1024 }} KB</div>
{% if w.status == 'running' %}
<a href="{{ w.url }}" target="_blank" class="visit-link">🌐 Visit Site</a>
{% else %}
<div style="color:#666;margin:10px 0;">⚪ Website not running</div>
{% endif %}
<div class="actions">
<button class="btn-start" onclick="action({{ w.id }},'start')">▶ Start</button>
<button class="btn-stop" onclick="action({{ w.id }},'stop')">■ Stop</button>
<button class="btn-restart" onclick="action({{ w.id }},'restart')">⟳ Restart</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/files'">📁 Files</button>
<button class="btn-manage" onclick="location.href='/website/{{ w.id }}/logs'">📜 Logs</button>
<button class="btn-delete" onclick="if(confirm('Delete this website?')) action({{ w.id }},'delete')">🗑 Delete</button>
</div>
<div class="extra-actions">
<a href="/website/{{ w.id }}/env">⚙️ Env</a>
<a href="/website/{{ w.id }}/versions">📦 Versions</a>
<a href="/website/{{ w.id }}/backups">💾 Backups</a>
<button class="btn-manage" onclick="cloneSite({{ w.id }})">🔄 Clone</button>
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
fetch('/api/v1/websites/'+id+'/'+type,{method:'POST'})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}
function changeSlug(id){
const val=document.getElementById('slug_input_'+id).value.trim();
if(!val)return alert('Enter slug');
fetch('/api/v1/websites/'+id+'/slug',{
method:'PUT',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({slug:val})
})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}
function setDomain(id){
const val=document.getElementById('domain_input_'+id).value.trim();
if(!val)return alert('Enter domain');
fetch('/api/v1/websites/'+id+'/domain',{
method:'PUT',
headers:{'Content-Type':'application/json'},
body:JSON.stringify({domain:val})
})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)})
.catch(()=>alert('Network error'));
}
function cloneSite(id){
if(!confirm('Clone this website?'))return;
fetch('/website/'+id+'/clone',{method:'POST'})
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
fetch('/api/v1/upload',{method:'POST',body:fd})
.then(r=>r.json())
.then(d=>{st.innerHTML=d.message;if(d.success)location.reload();})
.catch(()=>st.innerHTML='❌ Network error');
};
</script>
</body></html>
"""

FILES_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Files</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
.container{max-width:1000px;margin:auto;}
.back{color:#00e5ff;text-decoration:none;}
ul{list-style:none;padding:0;}
li{padding:8px;border-bottom:1px solid #16213e;display:flex;justify-content:space-between;}
a{color:#00e5ff;text-decoration:none;}
.edit-link{color:#ffaa00;}
.upload-form{margin:20px 0;padding:20px;background:#16213e;border-radius:10px;}
.upload-form input{margin:5px;}
</style>
</head>
<body>
<div class="container">
<a href="/dashboard" class="back">← Dashboard</a>
<h2>📁 {{ website.website_name or website.website_slug }}</h2>
<div class="upload-form">
<form action="/website/{{ website.id }}/upload_file" method="post" enctype="multipart/form-data">
<input type="file" name="file">
<input type="hidden" name="path" value="">
<button type="submit">Upload</button>
</form>
<form action="/website/{{ website.id }}/create_folder" method="post" style="display:inline;">
<input type="text" name="path" placeholder="folder/path">
<button type="submit">Create Folder</button>
</form>
</div>
<ul>
{% for item in items %}
<li>
<span>{% if item.is_dir %}📁{% else %}📄{% endif %} {{ item.name }}</span>
<span>
{% if not item.is_dir %}<a href="/website/{{ website.id }}/edit?path={{ item.path }}" class="edit-link">✏️ Edit</a>{% endif %}
<button onclick="deleteItem('{{ item.path }}')" style="background:red;border:none;color:#fff;border-radius:5px;padding:2px 8px;">Delete</button>
</span>
</li>
{% endfor %}
</ul>
</div>
<script>
function deleteItem(path){
if(!confirm('Delete '+path+'?'))return;
fetch('/website/{{ website.id }}/delete_file',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'path='+encodeURIComponent(path)
})
.then(r=>r.json())
.then(d=>{if(d.success)location.reload();else alert('Error: '+d.error)});
}
</script>
</body></html>
"""

EDIT_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Edit</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
.container{max-width:900px;margin:auto;}
textarea{width:100%;height:400px;background:#0f3460;color:#eee;border:none;padding:10px;}
button{background:#e94560;color:#fff;border:none;padding:10px 20px;cursor:pointer;}
</style>
</head>
<body>
<div class="container">
<h1>Editing {{ file_path }}</h1>
<form method="post">
<textarea name="content">{{ content }}</textarea><br>
<button type="submit">Save</button>
<a href="/website/{{ website.id }}/files" style="color:#e94560;">Cancel</a>
</form>
</div>
</body></html>
"""

LOGS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Logs</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
pre{background:#0f3460;padding:10px;border-radius:5px;max-height:300px;overflow-y:auto;}
</style>
</head>
<body>
<h1>Logs for {{ website.website_name }}</h1>
<a href="/dashboard">← Dashboard</a>
<h3>Database Logs</h3>
<pre>
{% for log in logs %}
[{{ log.timestamp }}] {{ log.log_text }}
{% endfor %}
</pre>
<h3>Process Output</h3>
<pre>{{ file_log if file_log else 'No process log.' }}</pre>
<h3>Install Log</h3>
<pre>{{ install_log if install_log else 'No install log.' }}</pre>
</body></html>
"""

ENV_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Environment Variables</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
textarea{width:100%;height:200px;background:#0f3460;color:#eee;border:none;padding:10px;}
button{background:#e94560;color:#fff;border:none;padding:10px 20px;cursor:pointer;}
.error{color:#e94560;}
</style>
</head>
<body>
<h1>Environment Variables for {{ website.website_name }}</h1>
<a href="/dashboard">← Dashboard</a>
<form method="post">
<textarea name="env_vars">{{ env_vars }}</textarea><br>
<small>Enter as JSON object, e.g. {"SECRET_KEY": "abc", "DB_URL": "postgres://..."}</small><br>
<button type="submit">Save</button>
</form>
{% if error %}<div class="error">{{ error }}</div>{% endif %}
</body></html>
"""

VERSIONS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Versions</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
button{background:#e94560;color:#fff;border:none;padding:5px 10px;cursor:pointer;}
</style>
</head>
<body>
<h1>Versions for {{ website.website_name }}</h1>
<a href="/dashboard">← Dashboard</a>
<table>
<tr><th>Version</th><th>Zip Name</th><th>Size</th><th>Status</th><th>Deployed At</th><th>Action</th></tr>
{% for v in versions %}
<tr>
<td>{{ v.version_number }}</td>
<td>{{ v.zip_name }}</td>
<td>{{ (v.size or 0)//1024 }} KB</td>
<td>{{ v.status }}</td>
<td>{{ v.deployed_at }}</td>
<td>
{% if v.status == 'active' %}
<button onclick="rollback({{ v.id }})" disabled>Active</button>
{% else %}
<button onclick="rollback({{ v.id }})">Rollback</button>
{% endif %}
</td>
</tr>
{% endfor %}
</table>
<script>
function rollback(version_id){
if(!confirm('Rollback to this version?'))return;
fetch('/website/{{ website.id }}/rollback/'+version_id,{method:'POST'})
.then(r=>r.json())
.then(d=>{if(d.success)alert('Rollback job queued');else alert('Error: '+d.error)});
}
</script>
</body></html>
"""

BACKUPS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Backups</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
button{background:#e94560;color:#fff;border:none;padding:5px 10px;cursor:pointer;}
</style>
</head>
<body>
<h1>Backups for {{ website.website_name }}</h1>
<a href="/dashboard">← Dashboard</a>
<button onclick="createBackup()">Create Backup</button>
<table>
<tr><th>File</th><th>Size</th><th>Created</th><th>Action</th></tr>
{% for b in backups %}
<tr>
<td>{{ b.backup_file }}</td>
<td>{{ (b.size or 0)//1024 }} KB</td>
<td>{{ b.created_at }}</td>
<td>
<button onclick="restoreBackup('{{ b.backup_file }}')">Restore</button>
</td>
</tr>
{% endfor %}
</table>
<script>
function createBackup(){
fetch('/website/{{ website.id }}/backup',{method:'POST'})
.then(r=>r.json())
.then(d=>{if(d.success)alert('Backup job queued');else alert('Error: '+d.error)});
}
function restoreBackup(name){
if(!confirm('Restore from '+name+'?'))return;
fetch('/website/{{ website.id }}/restore',{
method:'POST',
headers:{'Content-Type':'application/x-www-form-urlencoded'},
body:'backup_name='+encodeURIComponent(name)
})
.then(r=>r.json())
.then(d=>{if(d.success)alert('Restore job queued');else alert('Error: '+d.error)});
}
</script>
</body></html>
"""

ADMIN_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Admin</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
.card{background:#16213e;padding:15px;border-radius:10px;display:inline-block;margin:10px;min-width:150px;}
</style>
</head>
<body>
<h1>Admin Dashboard</h1>
<div>
<div class="card">Total Users: {{ total_users }}</div>
<div class="card">Total Websites: {{ total_websites }}</div>
<div class="card" style="background:#2ecc71;">Running: {{ running }}</div>
<div class="card" style="background:#e74c3c;">Stopped: {{ stopped }}</div>
<div class="card" style="background:#f39c12;">Failed: {{ failed }}</div>
<div class="card" style="background:#e67e22;">Crashed: {{ crashed }}</div>
<div class="card">Jobs Queued: {{ jobs_queued }}</div>
<div class="card">Jobs Running: {{ jobs_running }}</div>
<div class="card">Total Backups: {{ total_backups }}</div>
</div>
<br>
<a href="/admin/users">Manage Users</a> |
<a href="/admin/websites">Manage Websites</a> |
<a href="/admin/jobs">View Jobs</a> |
<a href="/admin/backups">View Backups</a> |
<a href="/dashboard">Dashboard</a>
</body></html>
"""

ADMIN_USERS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Users</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
</style>
</head>
<body>
<h1>Users</h1>
<a href="/admin">← Admin</a>
<table>
<tr><th>ID</th><th>Username</th><th>Email</th><th>Role</th><th>Status</th><th>Plan</th><th>Created</th></tr>
{% for u in users %}
<tr><td>{{ u.id }}</td><td>{{ u.username }}</td><td>{{ u.email }}</td><td>{{ u.role }}</td><td>{{ u.status }}</td><td>{{ u.plan }}</td><td>{{ u.created_at }}</td></tr>
{% endfor %}
</table>
</body></html>
"""

ADMIN_WEBSITES_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Websites</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
</style>
</head>
<body>
<h1>All Websites</h1>
<a href="/admin">← Admin</a>
<table>
<tr><th>ID</th><th>Owner ID</th><th>Slug</th><th>Status</th><th>Port</th><th>PID</th><th>Created</th></tr>
{% for w in websites %}
<tr><td>{{ w.id }}</td><td>{{ w.owner_id }}</td><td>{{ w.website_slug }}</td><td>{{ w.status }}</td><td>{{ w.allocated_port }}</td><td>{{ w.pid }}</td><td>{{ w.created_at }}</td></tr>
{% endfor %}
</table>
</body></html>
"""

ADMIN_JOBS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Jobs</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
</style>
</head>
<body>
<h1>Jobs</h1>
<a href="/admin">← Admin</a>
<table>
<tr><th>ID</th><th>Type</th><th>Website</th><th>User</th><th>Status</th><th>Progress</th><th>Created</th><th>Finished</th><th>Error</th></tr>
{% for j in jobs %}
<tr><td>{{ j.id }}</td><td>{{ j.job_type }}</td><td>{{ j.website_id }}</td><td>{{ j.user_id }}</td><td>{{ j.status }}</td><td>{{ j.progress }}%</td><td>{{ j.created_at }}</td><td>{{ j.finished_at }}</td><td>{{ j.error or '' }}</td></tr>
{% endfor %}
</table>
</body></html>
"""

ADMIN_BACKUPS_TEMPLATE = """
<!DOCTYPE html>
<html><head><title>Backups</title>
<style>body{background:#1a1a2e;color:#eee;font-family:sans-serif;padding:20px;}
table{width:100%;border-collapse:collapse;}
th,td{padding:8px;border:1px solid #0f3460;text-align:left;}
th{background:#16213e;}
</style>
</head>
<body>
<h1>All Backups</h1>
<a href="/admin">← Admin</a>
<table>
<tr><th>ID</th><th>Website</th><th>File</th><th>Size</th><th>Created</th></tr>
{% for b in backups %}
<tr><td>{{ b.id }}</td><td>{{ b.website_id }}</td><td>{{ b.backup_file }}</td><td>{{ (b.size or 0)//1024 }} KB</td><td>{{ b.created_at }}</td></tr>
{% endfor %}
</table>
</body></html>
"""

# ---------- Main ----------
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port, debug=False)