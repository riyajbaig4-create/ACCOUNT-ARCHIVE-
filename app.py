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
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, send_from_directory, session, jsonify, abort

app = Flask(__name__)
app.secret_key = 'super-secret-key-change-this'

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')
USERS_FILE = os.path.join(BASE_DIR, 'users.json')
STATIC_FOLDER = os.path.join(BASE_DIR, 'static')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs(STATIC_FOLDER, exist_ok=True)

# ---------- USER DATABASE ----------
def load_users():
    if os.path.exists(USERS_FILE):
        with open(USERS_FILE, 'r') as f:
            return json.load(f)
    return {}

def save_users(users):
    with open(USERS_FILE, 'w') as f:
        json.dump(users, f, indent=2)

users_db = load_users()

# ---------- HELPERS ----------
def get_user_folder(username):
    folder = os.path.join(UPLOAD_FOLDER, username)
    os.makedirs(folder, exist_ok=True)
    return folder

def generate_username():
    return str(uuid.uuid4())[:8]

def is_safe_path(path):
    abs_path = os.path.abspath(path)
    return abs_path.startswith(os.path.abspath(UPLOAD_FOLDER))

def extract_zip(zip_path, extract_to):
    with zipfile.ZipFile(zip_path, 'r') as zip_ref:
        zip_ref.extractall(extract_to)
    os.remove(zip_path)

# ---------- ROUTES ----------
@app.route('/')
def index():
    return render_template_string(HTML_INDEX)

@app.route('/deploy', methods=['POST'])
def deploy():
    # get username and password from form
    username = request.form.get('username', '').strip()
    password = request.form.get('password', '').strip()
    if not username:
        username = generate_username()
    if not password:
        password = 'password'   # default

    # check if username already exists
    if username in users_db:
        return jsonify({'error': 'Username already taken. Choose another.'}), 400

    # handle files
    files = request.files.getlist('files')
    if not files or all(f.filename == '' for f in files):
        return jsonify({'error': 'No files uploaded.'}), 400

    user_folder = get_user_folder(username)
    # clear folder if exists (should not, but just in case)
    shutil.rmtree(user_folder, ignore_errors=True)
    os.makedirs(user_folder)

    # save files
    for file in files:
        if file.filename == '':
            continue
        if file.filename.lower().endswith('.zip'):
            # save zip temporarily
            temp_path = os.path.join(user_folder, file.filename)
            file.save(temp_path)
            extract_zip(temp_path, user_folder)
        else:
            file.save(os.path.join(user_folder, file.filename))

    # save user credentials
    users_db[username] = {
        'password': password,
        'created_at': datetime.now().isoformat(),
        'folder': user_folder
    }
    save_users(users_db)

    # generate link
    link = request.host_url + username
    return jsonify({
        'success': True,
        'username': username,
        'password': password,
        'link': link
    })

@app.route('/<username>/', defaults={'path': ''})
@app.route('/<username>/<path:path>')
def serve_user_site(username, path):
    # check if user exists
    if username not in users_db:
        abort(404)

    # check login
    if not session.get(f'logged_in_{username}'):
        # if user has index.html or index.py, we still need login first
        return redirect(url_for('login_page', username=username))

    user_folder = get_user_folder(username)
    if path == '':
        # try index.html, then index.py, else list files
        if os.path.exists(os.path.join(user_folder, 'index.html')):
            return send_from_directory(user_folder, 'index.html')
        elif os.path.exists(os.path.join(user_folder, 'index.py')):
            return run_python_script(user_folder, 'index.py')
        else:
            # list files
            return render_template_string(FILE_LIST_HTML, username=username, files=os.listdir(user_folder))
    else:
        # serve file or run .py
        file_path = os.path.join(user_folder, path)
        if not os.path.exists(file_path):
            abort(404)
        if os.path.isdir(file_path):
            # if directory, redirect to /username/path/
            return redirect(url_for('serve_user_site', username=username, path=path + '/'))
        if path.endswith('.py'):
            return run_python_script(user_folder, path)
        else:
            return send_from_directory(user_folder, path)

def run_python_script(folder, filename):
    """Run a python script and return its output as HTTP response."""
    try:
        result = subprocess.run(
            ['python3', filename],
            cwd=folder,
            capture_output=True,
            text=True,
            timeout=30
        )
        # if stdout is HTML, return it; else wrap in pre
        if result.stdout.strip().startswith('<!DOCTYPE html>') or result.stdout.strip().startswith('<html'):
            return result.stdout
        else:
            return f"<pre>{result.stdout}</pre>" + (f"<pre style='color:red'>Error: {result.stderr}</pre>" if result.stderr else "")
    except subprocess.TimeoutExpired:
        return "<h3>Script timed out after 30 seconds.</h3>"
    except Exception as e:
        return f"<h3>Error running script: {e}</h3>"

@app.route('/login/<username>', methods=['GET', 'POST'])
def login_page(username):
    if username not in users_db:
        abort(404)
    if request.method == 'POST':
        pwd = request.form.get('password')
        if pwd == users_db[username]['password']:
            session[f'logged_in_{username}'] = True
            return redirect(url_for('serve_user_site', username=username))
        else:
            return render_template_string(LOGIN_HTML, username=username, error='Invalid password')
    return render_template_string(LOGIN_HTML, username=username, error=None)

@app.route('/logout/<username>')
def logout_user(username):
    session.pop(f'logged_in_{username}', None)
    return redirect(url_for('serve_user_site', username=username))

# ---------- HTML TEMPLATES ----------
HTML_INDEX = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Website Hosting Panel</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0c1018; color: #fff; padding: 20px; }
        .container { max-width: 700px; margin: 0 auto; }
        h1 { text-align: center; color: #00e5ff; }
        .drop-zone { border: 2px dashed #00e5ff; border-radius: 15px; padding: 40px; text-align: center; background: #161b25; margin: 20px 0; cursor: pointer; transition: 0.3s; }
        .drop-zone.dragover { background: #1a2a3a; border-color: #fff; }
        .drop-zone i { font-size: 48px; color: #00e5ff; margin-bottom: 10px; }
        .drop-zone p { color: #aaa; }
        #fileList { margin: 10px 0; color: #aaa; }
        .form-group { margin: 15px 0; }
        .form-group label { display: block; margin-bottom: 5px; color: #aaa; }
        .form-group input { width: 100%; padding: 12px; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; }
        .btn { background: #00e5ff; color: #000; border: none; padding: 14px 30px; border-radius: 8px; font-weight: bold; cursor: pointer; width: 100%; font-size: 16px; }
        .btn:hover { opacity: 0.9; }
        #result { margin-top: 20px; padding: 15px; background: #161b25; border-radius: 8px; display: none; }
        .success { color: #00ff6a; }
        .error { color: #ff4d4d; }
        .link { word-break: break-all; color: #00e5ff; }
        .footer { text-align: center; margin-top: 40px; color: #555; }
    </style>
</head>
<body>
<div class="container">
    <h1>🚀 Website Hosting</h1>
    <p style="text-align:center;color:#aaa;">Upload your website (HTML, Python, JS, ZIP) and get a live link.</p>

    <div class="drop-zone" id="dropZone">
        <i>📂</i>
        <p>Drag & drop files here or click to browse</p>
        <p style="font-size:12px;color:#555;">Supports .html .py .js .css .zip and more</p>
    </div>
    <input type="file" id="fileInput" multiple style="display:none;">

    <div id="fileList"></div>

    <div class="form-group">
        <label>Choose a username (optional)</label>
        <input type="text" id="username" placeholder="Leave blank for auto-generate" />
    </div>
    <div class="form-group">
        <label>Set a password for your site</label>
        <input type="text" id="password" placeholder="Default: password" value="password" />
    </div>
    <button class="btn" id="deployBtn">🚀 Deploy Website</button>

    <div id="result"></div>
    <div class="footer">Your site will be available at: <span id="linkPreview">https://your-domain.com/username</span></div>
</div>

<script>
    const dropZone = document.getElementById('dropZone');
    const fileInput = document.getElementById('fileInput');
    const fileList = document.getElementById('fileList');
    const deployBtn = document.getElementById('deployBtn');
    const resultDiv = document.getElementById('result');
    const linkPreview = document.getElementById('linkPreview');

    let selectedFiles = [];

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
        if (selectedFiles.length === 0) {
            fileList.innerHTML = '';
            return;
        }
        let html = '<strong>Selected files:</strong><ul style="margin:5px 0;padding-left:20px;color:#aaa;">';
        selectedFiles.forEach(f => html += `<li>${f.name} (${(f.size/1024).toFixed(1)} KB)</li>`);
        html += '</ul>';
        fileList.innerHTML = html;
        // update preview link with username if entered
        const uname = document.getElementById('username').value.trim() || 'username';
        linkPreview.textContent = window.location.origin + '/' + uname;
    }

    document.getElementById('username').addEventListener('input', updateFileList);
    document.getElementById('password').addEventListener('input', updateFileList);

    deployBtn.addEventListener('click', async () => {
        if (selectedFiles.length === 0) {
            alert('Please select at least one file.');
            return;
        }
        const username = document.getElementById('username').value.trim() || '';
        const password = document.getElementById('password').value.trim() || 'password';

        const formData = new FormData();
        selectedFiles.forEach(f => formData.append('files', f));
        formData.append('username', username);
        formData.append('password', password);

        deployBtn.disabled = true;
        deployBtn.textContent = 'Deploying...';
        resultDiv.style.display = 'block';
        resultDiv.innerHTML = '<p>⏳ Uploading...</p>';

        try {
            const res = await fetch('/deploy', { method: 'POST', body: formData });
            const data = await res.json();
            if (data.success) {
                resultDiv.innerHTML = `
                    <p class="success">✅ Website deployed successfully!</p>
                    <p><strong>Username:</strong> ${data.username}</p>
                    <p><strong>Password:</strong> ${data.password}</p>
                    <p><strong>Link:</strong> <a href="${data.link}" target="_blank" class="link">${data.link}</a></p>
                    <p style="margin-top:10px;color:#aaa;">⚠️ Remember your password! You'll need it to access your site.</p>
                `;
                linkPreview.textContent = data.link;
            } else {
                resultDiv.innerHTML = `<p class="error">❌ Error: ${data.error}</p>`;
            }
        } catch (e) {
            resultDiv.innerHTML = `<p class="error">❌ Network error: ${e.message}</p>`;
        }
        deployBtn.disabled = false;
        deployBtn.textContent = '🚀 Deploy Website';
    });

    // update link preview on any change
    document.querySelectorAll('input').forEach(inp => inp.addEventListener('input', updateFileList));
    updateFileList();
</script>
</body>
</html>
"""

LOGIN_HTML = """
<!DOCTYPE html>
<html>
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Login - {{ username }}</title>
    <style>
        body { font-family: Arial, sans-serif; background: #0c1018; color: #fff; display: flex; justify-content: center; align-items: center; height: 100vh; margin: 0; }
        .login-box { background: #161b25; padding: 40px; border-radius: 20px; border: 1px solid #2b3240; width: 350px; }
        h2 { text-align: center; color: #00e5ff; }
        input { width: 100%; padding: 14px; margin: 10px 0; background: #0c1018; border: 1px solid #2b3240; color: white; border-radius: 8px; box-sizing: border-box; }
        .btn { width: 100%; padding: 14px; background: #00e5ff; color: #000; border: none; border-radius: 8px; font-weight: bold; cursor: pointer; }
        .error { color: #ff4d4d; text-align: center; margin-top: 10px; }
        .footer { text-align: center; margin-top: 20px; color: #555; font-size: 12px; }
    </style>
</head>
<body>
<div class="login-box">
    <h2>🔐 Login</h2>
    <p style="text-align:center;color:#aaa;">Website: {{ username }}</p>
    <form method="POST">
        <input type="password" name="password" placeholder="Enter password" required autofocus />
        <button class="btn">Login</button>
    </form>
    {% if error %}
        <div class="error">{{ error }}</div>
    {% endif %}
    <div class="footer">Default password: password</div>
</div>
</body>
</html>
"""

FILE_LIST_HTML = """
<!DOCTYPE html>
<html>
<head><meta charset="UTF-8"><title>Files - {{ username }}</title>
<style>
    body { background: #0c1018; color: #fff; font-family: Arial; padding: 20px; }
    a { color: #00e5ff; text-decoration: none; }
    a:hover { text-decoration: underline; }
    .file { padding: 8px 12px; border-bottom: 1px solid #222; }
    .dir { color: #f0c674; }
    .logout { float: right; background: #333; padding: 8px 16px; border-radius: 6px; }
</style>
</head>
<body>
    <div style="max-width:700px;margin:0 auto;">
        <h2>📁 Files for {{ username }}</h2>
        <a href="{{ url_for('logout_user', username=username) }}" class="logout">🚪 Logout</a>
        <div style="margin-top:20px;">
            {% for f in files %}
                <div class="file">
                    <a href="{{ url_for('serve_user_site', username=username, path=f) }}">
                        {% if f.endswith('/') %}📁{% else %}📄{% endif %} {{ f }}
                    </a>
                </div>
            {% endfor %}
        </div>
        <p style="color:#555;margin-top:20px;">Click on a file to view or run it.</p>
    </div>
</body>
</html>
"""

# ---------- RUN ----------
if __name__ == '__main__':
    print("="*60)
    print("🌐 WEBSITE HOSTING PLATFORM")
    print(f"📁 Uploads folder: {UPLOAD_FOLDER}")
    print(f"👥 Users DB: {USERS_FILE}")
    print("🚀 Starting server at http://0.0.0.0:5000")
    print("="*60)
    app.run(host='0.0.0.0', port=5000, debug=True)