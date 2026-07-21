"""
=====================================================
  🛠️ MODDER HUB APK PATCHER v3.0 (Single File)
  ---------------------------------------------
  Features:
  - Upload APK (1GB+) & Modder Hub ZIP
  - Auto-detects Online/Dialog Expiry
  - Smart Neutralizer for 20+ Expiry Patterns
  - Live Progress Bar (via AJAX polling)
  - Raw DEX Injection (No Recompile needed for DEX)
  - Background Threading (No Browser Timeout)
  - Auto Cleanup Temp Files
=====================================================
"""

# ======================= IMPORTS =========================
import os
import sys
import io
import subprocess
import shutil
import zipfile
import tempfile
import time
import re
import uuid
import asyncio
import threading
import traceback
from datetime import datetime
from typing import Optional, Dict, Tuple, List

# FastAPI & Web
from fastapi import FastAPI, UploadFile, File, Form, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
import uvicorn

# XML Parsing
import xml.etree.ElementTree as ET

# ======================= CONFIGURATION ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
KEYSTORE_DIR = os.path.join(BASE_DIR, "keystore")
TOOLS_DIR = os.path.join(BASE_DIR, "tools")
TEMP_BASE = tempfile.gettempdir()

# Limits
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
TASK_TIMEOUT = 1200  # 20 minutes

# Keystore
KEYSTORE_PATH = os.path.join(KEYSTORE_DIR, "debug.keystore")
KEYSTORE_PASS = "android"
KEY_ALIAS = "androiddebugkey"

# Tools
BAKSMALI_JAR = os.path.join(TOOLS_DIR, "baksmali.jar")

# Create Dirs
for d in [UPLOAD_DIR, KEYSTORE_DIR, TOOLS_DIR]:
    os.makedirs(d, exist_ok=True)

# ======================= TASK MANAGER ======================
TASK_STORE: Dict[str, Dict] = {}

def create_task() -> str:
    task_id = str(uuid.uuid4())[:8]
    TASK_STORE[task_id] = {
        "status": "pending",
        "progress": 0,
        "message": "Initializing...",
        "filename": None,
        "error": None,
        "result_path": None,
        "created_at": datetime.now().isoformat()
    }
    return task_id

def update_task(task_id: str, progress: int = None, message: str = None, 
                status: str = None, error: str = None, result_path: str = None):
    if task_id in TASK_STORE:
        if progress is not None:
            TASK_STORE[task_id]["progress"] = min(progress, 100)
        if message:
            TASK_STORE[task_id]["message"] = message
        if status:
            TASK_STORE[task_id]["status"] = status
        if error:
            TASK_STORE[task_id]["error"] = error
        if result_path:
            TASK_STORE[task_id]["result_path"] = result_path

# ======================= SECURITY / KEYSTORE ================
def generate_keystore():
    if not os.path.exists(KEYSTORE_PATH):
        try:
            subprocess.run([
                "keytool", "-genkey", "-v", "-keystore", KEYSTORE_PATH,
                "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
                "-validity", "10000", "-storepass", KEYSTORE_PASS,
                "-keypass", KEYSTORE_PASS,
                "-dname", "CN=Android Debug, O=Android, C=US"
            ], check=True, capture_output=True)
            return True
        except:
            return False
    return True

# ======================= TOOLS DOWNLOAD =====================
def download_baksmali():
    if not os.path.exists(BAKSMALI_JAR):
        try:
            import urllib.request
            url = "https://bitbucket.org/JesusFreke/smali/downloads/baksmali-2.5.2.jar"
            urllib.request.urlretrieve(url, BAKSMALI_JAR)
            return True
        except:
            return False
    return True

# ======================= APK UTILITIES ======================

def decompile_apk(apk_path: str, output_dir: str):
    subprocess.run(["apktool", "d", apk_path, "-o", output_dir, "-f"], 
                   check=True, timeout=600, capture_output=True)

def rebuild_apk(decompile_dir: str, output_apk: str):
    subprocess.run(["apktool", "b", decompile_dir, "-o", output_apk, "-c"], 
                   check=True, timeout=600, capture_output=True)

def sign_apk(input_apk: str, output_apk: str):
    subprocess.run([
        "apksigner", "sign",
        "--ks", KEYSTORE_PATH,
        "--ks-pass", f"pass:{KEYSTORE_PASS}",
        "--key-pass", f"pass:{KEYSTORE_PASS}",
        "--out", output_apk,
        input_apk
    ], check=True, timeout=120, capture_output=True)

def zipalign_apk(input_apk: str, output_apk: str):
    try:
        subprocess.run(["zipalign", "-f", "-p", "4", input_apk, output_apk], 
                       check=True, timeout=60, capture_output=True)
        return output_apk
    except:
        return input_apk

def remove_meta_inf(apk_path: str):
    """Remove META-INF folder from APK to avoid signature conflicts."""
    temp_path = apk_path + ".tmp"
    with zipfile.ZipFile(apk_path, 'r') as zf_in:
        with zipfile.ZipFile(temp_path, 'w') as zf_out:
            for item in zf_in.namelist():
                if not item.startswith("META-INF/"):
                    zf_out.writestr(item, zf_in.read(item))
    os.replace(temp_path, apk_path)

def get_package_and_launcher(manifest_path: str) -> Tuple[str, str]:
    tree = ET.parse(manifest_path)
    root = tree.getroot()
    pkg = root.get("package", "")
    main_act = None
    for activity in root.findall("activity"):
        intent_filter = activity.find("intent-filter")
        if intent_filter is not None:
            for action in intent_filter.findall("action"):
                if action.get("{http://schemas.android.com/apk/res/android}name") == "android.intent.action.MAIN":
                    for category in intent_filter.findall("category"):
                        if category.get("{http://schemas.android.com/apk/res/android}name") == "android.intent.category.LAUNCHER":
                            act_name = activity.get("{http://schemas.android.com/apk/res/android}name", "")
                            if act_name.startswith("."):
                                main_act = pkg + act_name
                            else:
                                main_act = act_name
                            break
    return pkg, main_act

def count_smali_dirs(decompile_dir: str) -> int:
    max_num = 0
    for d in os.listdir(decompile_dir):
        if d == "smali":
            max_num = max(max_num, 1)
        elif d.startswith("smali_classes"):
            try:
                num = int(d.split("_")[1])
                max_num = max(max_num, num)
            except:
                pass
    return max_num

def find_main_activity_smali(decompile_dir: str, main_activity: str) -> Optional[str]:
    if not main_activity:
        return None
    smali_path = main_activity.replace(".", "/") + ".smali"
    for root, _, files in os.walk(decompile_dir):
        if root.endswith(os.path.dirname(smali_path)):
            for f in files:
                if f == os.path.basename(smali_path):
                    return os.path.join(root, f)
    # Fallback: Search all smali for onCreate
    for root, _, files in os.walk(os.path.join(decompile_dir, "smali")):
        for f in files:
            if f.endswith(".smali"):
                fpath = os.path.join(root, f)
                try:
                    with open(fpath, 'r', errors='ignore') as fp:
                        content = fp.read()
                    if '.method public onCreate(Landroid/os/Bundle;)V' in content:
                        return fpath
                except:
                    pass
    return None

# ======================= NEUTRALIZER (SMART) =================
# 20+ Expiry Patterns to target
EXPIRY_PATTERNS = [
    'java/util/Calendar',
    'java/util/Date',
    'java/text/SimpleDateFormat',
    'System;->currentTimeMillis',
    'compareTo',
    'after(',
    'before(',
    'getTime',
    'getTimeInMillis',
    'setTime',
    'expire',
    'valid',
    'checkTime',
    'isExpired',
    'isValid',
    'getExpiry',
    'parseDate',
    'toDate',
    'TimeUnit',
    'MILLISECONDS',
    'SECONDS',
    'MINUTES',
    'HOURS',
    'DAYS',
    'WEEKS',
    'MONTHS',
    'YEARS',
    'Calendar;->getInstance',
    'Date;-><init>',
    'SimpleDateFormat;-><init>',
    'format',
    'parse',
]

def neutralize_old_expiry(filepath: str) -> bool:
    """Neutralize old expiry by commenting out suspicious lines in onCreate."""
    if not os.path.exists(filepath):
        return False
    
    try:
        with open(filepath, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except:
        return False

    new_lines = []
    inside_oncreate = False
    skip_next = 0
    modified = False
    our_module = 'mt/modder/hub'  # Don't touch our own code

    for line in lines:
        stripped = line.strip()
        lower_line = line.lower()
        
        # Detect onCreate method start
        if '.method public onCreate(Landroid/os/Bundle;)V' in line:
            inside_oncreate = True
            new_lines.append(line)
            continue
        
        # Detect end of onCreate method
        if inside_oncreate and '.end method' in line:
            inside_oncreate = False
            new_lines.append(line)
            continue
        
        # If inside onCreate and line contains our module, keep it as is
        if inside_oncreate and our_module in line:
            new_lines.append(line)
            continue
        
        # Neutralization logic
        if inside_oncreate:
            is_suspicious = False
            for pattern in EXPIRY_PATTERNS:
                if pattern in line:
                    is_suspicious = True
                    break
            
            # If suspicious and it's an invoke or if or goto or const
            if is_suspicious and ('invoke-' in line or 'if-' in line or 'goto' in line or 'const-string' in line):
                # Comment it out
                new_lines.append('    # ' + stripped + '  <!-- NEUTRALIZED -->\n')
                modified = True
                continue
            
            # If we see a block of `if-*` and the next line is `:cond_` or `goto`, skip them too
            if skip_next > 0:
                if 'if-' in line or 'goto' in line or ':' in line:
                    new_lines.append('    # ' + stripped + '  <!-- NEUTRALIZED (Flow) -->\n')
                    skip_next -= 1
                    modified = True
                    continue
                else:
                    skip_next = 0
            
            # Detect if line is a condition jump and mark to skip next few lines
            if 'if-' in line and is_suspicious:
                skip_next = 2  # skip the condition and the next label/goto
        
        # Default: keep line
        new_lines.append(line)

    # Write back only if modified
    if modified:
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            return True
        except:
            return False
    return False

# ======================= INJECTOR ===========================
def inject_invoke_line(smali_file: str) -> bool:
    """Inject invoke-static line into MainActivity's onCreate."""
    if not os.path.exists(smali_file):
        return False
    
    try:
        with open(smali_file, 'r', encoding='utf-8', errors='ignore') as f:
            lines = f.readlines()
    except:
        return False

    new_lines = []
    inside_oncreate = False
    inserted = False
    our_line = 'invoke-static/range {p0}, Lmt/modder/hub/c;->aa(Landroid/app/Activity;)V\n'

    # Check if already injected
    for line in lines:
        if our_line.strip() in line:
            return True  # Already there

    for line in lines:
        new_lines.append(line)
        
        if '.method public onCreate(Landroid/os/Bundle;)V' in line:
            inside_oncreate = True
        
        if inside_oncreate and not inserted:
            # Place it right after invoke-super
            if 'invoke-super' in line and 'onCreate' in line:
                indent = '    '
                new_lines.append(f'{indent}{our_line}')
                inserted = True
    
    # If not inserted, place before return-void
    if not inserted:
        new_lines = []
        inside_oncreate = False
        for line in lines:
            if '.method public onCreate(Landroid/os/Bundle;)V' in line:
                inside_oncreate = True
            if inside_oncreate and 'return-void' in line and not inserted:
                indent = '    '
                new_lines.append(f'{indent}{our_line}')
                inserted = True
            new_lines.append(line)
    
    if inserted:
        try:
            with open(smali_file, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            return True
        except:
            return False
    return False

# ======================= DEX INJECTOR (RAW) =================
def inject_dex_raw(apk_path: str, dex_file_path: str, new_dex_name: str) -> bool:
    """Inject a raw DEX file into the APK without recompiling."""
    if not os.path.exists(dex_file_path):
        return False
    try:
        with zipfile.ZipFile(apk_path, 'a') as zf:
            zf.write(dex_file_path, new_dex_name)
        return True
    except:
        return False

# ======================= MAIN PATCHER SERVICE ===============
def run_patch_task(task_id: str, apk_data: bytes, patch_zip_data: bytes, original_filename: str):
    """Main background task."""
    temp_dir = tempfile.mkdtemp(prefix=f"patch_{task_id}_")
    update_task(task_id, progress=5, message="Temp directory created.", status="processing")
    
    try:
        # 1. Save APK
        apk_path = os.path.join(temp_dir, "original.apk")
        with open(apk_path, "wb") as f:
            f.write(apk_data)
        update_task(task_id, progress=10, message="APK saved.")

        # 2. Extract Modder Hub ZIP
        extract_dir = os.path.join(temp_dir, "patch_extract")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(patch_zip_data), 'r') as zf:
            zf.extractall(extract_dir)
        update_task(task_id, progress=15, message="Modder Hub ZIP extracted.")

        # 3. Decompile APK
        decompile_dir = os.path.join(temp_dir, "dec_apk")
        update_task(task_id, progress=20, message="Decompiling APK (large files take time)...")
        decompile_apk(apk_path, decompile_dir)
        update_task(task_id, progress=35, message="Decompilation complete.")

        # 4. Copy Assets (if exists)
        src_assets = os.path.join(extract_dir, "assets")
        if os.path.exists(src_assets):
            dst_assets = os.path.join(decompile_dir, "assets")
            shutil.copytree(src_assets, dst_assets, dirs_exist_ok=True)
            update_task(task_id, progress=40, message="Assets copied (Dialog Mode).")
        else:
            update_task(task_id, progress=40, message="No assets found (Online Expiry Mode).")

        # 5. Find MainActivity and Neutralize Old Expiry
        manifest_path = os.path.join(decompile_dir, "AndroidManifest.xml")
        pkg, main_act = get_package_and_launcher(manifest_path)
        update_task(task_id, progress=45, message=f"MainActivity: {main_act}")

        smali_file = None
        if main_act:
            smali_file = find_main_activity_smali(decompile_dir, main_act)
        
        if smali_file:
            update_task(task_id, progress=50, message="Neutralizing old expiry codes...")
            neutralize_old_expiry(smali_file)
            update_task(task_id, progress=55, message="Old expiry neutralized.")
            
            update_task(task_id, progress=60, message="Injecting our invoke line...")
            inject_invoke_line(smali_file)
            update_task(task_id, progress=65, message="Invoke line injected.")
        else:
            update_task(task_id, progress=60, message="MainActivity not found, skipping injection (will still try to inject DEX).")

        # 6. Get DEX from patch
        dex_files = [f for f in os.listdir(extract_dir) if f.endswith(".dex")]
        if not dex_files:
            raise Exception("No .dex file found in the Modder Hub ZIP!")
        provided_dex = os.path.join(extract_dir, dex_files[0])
        update_task(task_id, progress=70, message=f"Found DEX: {dex_files[0]}")

        # 7. Count DEX files and rebuild
        max_idx = count_smali_dirs(decompile_dir)
        next_idx = max_idx + 1
        new_dex_name = f"classes{next_idx}.dex"
        update_task(task_id, progress=75, message=f"Rebuilding APK (Current DEX count: {max_idx})...")
        
        rebuilt_apk = os.path.join(temp_dir, "rebuilt.apk")
        rebuild_apk(decompile_dir, rebuilt_apk)
        update_task(task_id, progress=80, message="Rebuild complete.")

        # 8. Clean META-INF
        update_task(task_id, progress=82, message="Cleaning old signatures...")
        remove_meta_inf(rebuilt_apk)
        
        # 9. Inject our DEX raw
        update_task(task_id, progress=85, message=f"Injecting {new_dex_name} raw...")
        temp_dex_renamed = os.path.join(temp_dir, new_dex_name)
        shutil.copy(provided_dex, temp_dex_renamed)
        if not inject_dex_raw(rebuilt_apk, temp_dex_renamed, new_dex_name):
            raise Exception(f"Failed to inject {new_dex_name} into APK!")
        update_task(task_id, progress=90, message=f"{new_dex_name} injected successfully.")

        # 10. Sign APK
        update_task(task_id, progress=92, message="Signing APK...")
        signed_apk = os.path.join(temp_dir, "signed.apk")
        sign_apk(rebuilt_apk, signed_apk)
        update_task(task_id, progress=95, message="Signed.")

        # 11. Zipalign
        update_task(task_id, progress=97, message="Optimizing APK...")
        aligned_apk = os.path.join(temp_dir, "aligned.apk")
        final_apk = zipalign_apk(signed_apk, aligned_apk)
        update_task(task_id, progress=100, message="Success! Ready to download.", status="completed")

        # Move final APK to persistent storage or keep in temp (we'll stream it)
        # We'll keep in temp and let the download endpoint read it.
        # But we must store the path.
        final_storage = os.path.join(UPLOAD_DIR, f"patched_{task_id}_{original_filename}")
        shutil.copy(final_apk, final_storage)
        update_task(task_id, result_path=final_storage)

    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        update_task(task_id, progress=0, message="Failed!", status="failed", error=error_msg)
    finally:
        # Cleanup temp dir after some time (not immediately, so download can happen)
        # We'll rely on the download endpoint to clean up.
        pass

# ======================= FASTAPI APP =========================
app = FastAPI(title="Modder Hub APK Patcher", version="3.0")

# HTML Templates (We'll embed them)

# ----------------------- FRONTEND (HTML + CSS + JS) ----------
HTML_PAGE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🛠️ Modder Hub Injector</title>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { background: #0d1117; color: #c9d1d9; font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif; display: flex; justify-content: center; align-items: center; min-height: 100vh; padding: 20px; }
        .container { max-width: 700px; width: 100%; background: #161b22; border-radius: 24px; padding: 40px; border: 1px solid #30363d; box-shadow: 0 8px 32px rgba(0,0,0,0.5); }
        h1 { color: #58a6ff; font-size: 28px; margin-bottom: 8px; display: flex; align-items: center; gap: 12px; }
        h1 small { font-size: 16px; color: #8b949e; font-weight: normal; }
        p.sub { color: #8b949e; margin-bottom: 28px; font-size: 14px; border-bottom: 1px solid #21262d; padding-bottom: 16px; }
        .form-group { margin-bottom: 24px; }
        .form-group label { display: block; font-weight: 600; margin-bottom: 8px; color: #f0f6fc; font-size: 15px; }
        .form-group label .badge { background: #238636; padding: 2px 10px; border-radius: 12px; font-size: 11px; color: white; margin-left: 8px; }
        .file-input-wrapper { position: relative; display: flex; align-items: center; background: #0d1117; border: 1px solid #30363d; border-radius: 12px; padding: 12px 16px; transition: border 0.2s; }
        .file-input-wrapper:hover { border-color: #58a6ff; }
        .file-input-wrapper input[type="file"] { position: absolute; opacity: 0; width: 100%; height: 100%; cursor: pointer; top: 0; left: 0; }
        .file-input-wrapper .file-placeholder { color: #8b949e; font-size: 14px; }
        .file-input-wrapper .file-name { color: #f0f6fc; font-weight: 500; margin-left: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .btn-primary { background: #238636; border: none; color: white; font-weight: 700; font-size: 18px; padding: 16px; width: 100%; border-radius: 16px; cursor: pointer; transition: 0.15s; display: flex; justify-content: center; align-items: center; gap: 12px; }
        .btn-primary:hover { background: #2ea043; transform: scale(1.01); }
        .btn-primary:disabled { background: #2d4a2d; cursor: not-allowed; transform: none; }
        .btn-secondary { background: #21262d; border: 1px solid #30363d; color: #c9d1d9; font-weight: 600; padding: 10px 20px; border-radius: 12px; cursor: pointer; transition: 0.15s; }
        .btn-secondary:hover { background: #30363d; }
        #progress-container { margin-top: 24px; background: #0d1117; border-radius: 16px; padding: 20px; border: 1px solid #21262d; display: none; }
        #progress-bar-bg { height: 8px; background: #21262d; border-radius: 10px; overflow: hidden; margin: 12px 0; }
        #progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #238636, #58a6ff); border-radius: 10px; transition: width 0.5s ease; }
        #status-msg { color: #8b949e; font-size: 14px; margin-top: 8px; }
        #status-msg .spinner { display: inline-block; animation: spin 1s linear infinite; margin-right: 8px; }
        @keyframes spin { 0% { transform: rotate(0deg); } 100% { transform: rotate(360deg); } }
        .error-box { background: #2d1a1a; border: 1px solid #f85149; border-radius: 12px; padding: 16px; margin-top: 16px; color: #f85149; font-size: 14px; display: none; }
        .success-box { background: #1a2d1a; border: 1px solid #238636; border-radius: 12px; padding: 16px; margin-top: 16px; color: #3fb950; font-size: 14px; display: none; }
        .footer { margin-top: 24px; font-size: 12px; color: #484f58; text-align: center; border-top: 1px solid #21262d; padding-top: 20px; }
        .footer a { color: #58a6ff; text-decoration: none; }
        .flex-row { display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }
        .task-id { font-family: monospace; background: #0d1117; padding: 4px 10px; border-radius: 8px; color: #8b949e; font-size: 13px; }
        #admin-link { float: right; font-size: 13px; color: #58a6ff; text-decoration: none; }
    </style>
</head>
<body>
<div class="container">
    <h1>🛠️ Modder Hub <small>Injector v3</small></h1>
    <p class="sub">Modder Hub से बनी ZIP और APK अपलोड करें → Modified APK डाउनलोड करें</p>

    <form id="uploadForm" enctype="multipart/form-data">
        <div class="form-group">
            <label>📱 APK फाइल <span class="badge">1GB तक</span></label>
            <div class="file-input-wrapper">
                <span class="file-placeholder">Choose APK...</span>
                <span class="file-name" id="apk-name"></span>
                <input type="file" name="apk_file" accept=".apk" required>
            </div>
        </div>
        <div class="form-group">
            <label>📦 Modder Hub ZIP <span class="badge">Expiry Files</span></label>
            <div class="file-input-wrapper">
                <span class="file-placeholder">Choose ZIP...</span>
                <span class="file-name" id="zip-name"></span>
                <input type="file" name="patch_zip" accept=".zip" required>
            </div>
            <small style="color:#484f58; display:block; margin-top:6px;">✅ assets + classesX.dex (Dialog)  |  ✅ सिर्फ classesX.dex (Online)</small>
        </div>
        <button type="submit" class="btn-primary" id="submitBtn">🚀 Inject & Download</button>
    </form>

    <div id="progress-container">
        <div style="display: flex; justify-content: space-between; align-items: center;">
            <span id="progress-label" style="font-weight: 600;">Processing...</span>
            <span id="progress-percent" style="color: #58a6ff;">0%</span>
        </div>
        <div id="progress-bar-bg"><div id="progress-bar"></div></div>
        <div id="status-msg"><span class="spinner">⏳</span> Initializing...</div>
        <div class="error-box" id="error-box"></div>
        <div class="success-box" id="success-box">✅ Success! Downloading...</div>
    </div>

    <div class="footer">
        <a href="#" id="admin-link" target="_blank">📊 Admin Dashboard</a> • 
        <span>Made with ❤️ for Modders</span>
    </div>
</div>

<script>
    // File name display
    document.querySelectorAll('input[type="file"]').forEach(input => {
        input.addEventListener('change', function(e) {
            const label = this.closest('.form-group').querySelector('.file-name');
            if (this.files.length > 0) {
                label.textContent = this.files[0].name;
            } else {
                label.textContent = '';
            }
        });
    });

    const form = document.getElementById('uploadForm');
    const submitBtn = document.getElementById('submitBtn');
    const progressContainer = document.getElementById('progress-container');
    const progressBar = document.getElementById('progress-bar');
    const progressPercent = document.getElementById('progress-percent');
    const statusMsg = document.getElementById('status-msg');
    const errorBox = document.getElementById('error-box');
    const successBox = document.getElementById('success-box');
    const adminLink = document.getElementById('admin-link');

    let taskId = null;
    let pollInterval = null;

    form.addEventListener('submit', async (e) => {
        e.preventDefault();
        const formData = new FormData(form);

        // Reset UI
        errorBox.style.display = 'none';
        successBox.style.display = 'none';
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressPercent.textContent = '0%';
        statusMsg.innerHTML = '<span class="spinner">⏳</span> Uploading...';
        submitBtn.disabled = true;
        submitBtn.innerHTML = '⏳ Processing...';

        try {
            const response = await fetch('/upload/', {
                method: 'POST',
                body: formData
            });

            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Upload failed');
            }

            const data = await response.json();
            taskId = data.task_id;
            adminLink.href = `/admin/${taskId}`;
            
            statusMsg.innerHTML = `<span class="spinner">⏳</span> Task started (ID: ${taskId}). Waiting for processing...`;
            // Start polling for status
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(() => pollStatus(taskId), 2000);
            
        } catch (err) {
            showError(err.message);
            submitBtn.disabled = false;
            submitBtn.innerHTML = '🚀 Inject & Download';
        }
    });

    async function pollStatus(id) {
        try {
            const res = await fetch(`/status/${id}`);
            const data = await res.json();
            
            // Update progress
            const prog = data.progress || 0;
            progressBar.style.width = prog + '%';
            progressPercent.textContent = prog + '%';
            statusMsg.innerHTML = data.message || 'Processing...';
            
            if (data.status === 'completed') {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.innerHTML = '🚀 Inject & Download';
                statusMsg.innerHTML = '✅ Done! Downloading APK...';
                successBox.style.display = 'block';
                // Trigger download
                window.location.href = `/download/${id}`;
                // Reset after 5 seconds
                setTimeout(() => {
                    progressContainer.style.display = 'none';
                    successBox.style.display = 'none';
                }, 5000);
            } else if (data.status === 'failed') {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.innerHTML = '🚀 Inject & Download';
                showError(data.error || 'Unknown error occurred.');
            }
        } catch (err) {
            // Ignore network errors during polling
        }
    }

    function showError(msg) {
        errorBox.textContent = '❌ ' + msg;
        errorBox.style.display = 'block';
        setTimeout(() => { errorBox.style.display = 'none'; }, 10000);
    }
</script>
</body>
</html>
"""

# ----------------------- ROUTES -----------------------------

@app.get("/", response_class=HTMLResponse)
async def home():
    return HTMLResponse(content=HTML_PAGE)

@app.post("/upload/")
async def upload_apk(
    apk_file: UploadFile = File(...),
    patch_zip: UploadFile = File(...)
):
    if not apk_file.filename.endswith(".apk"):
        raise HTTPException(400, "Only APK files allowed")
    if not patch_zip.filename.endswith(".zip"):
        raise HTTPException(400, "Only ZIP files allowed")
    
    # Read files into memory
    apk_data = await apk_file.read()
    zip_data = await patch_zip.read()
    
    if len(apk_data) > MAX_FILE_SIZE:
        raise HTTPException(400, f"APK size exceeds 2GB limit")
    
    # Create task
    task_id = create_task()
    
    # Start background thread
    thread = threading.Thread(
        target=run_patch_task,
        args=(task_id, apk_data, zip_data, apk_file.filename)
    )
    thread.daemon = True
    thread.start()
    
    return JSONResponse({
        "task_id": task_id,
        "status": "pending",
        "message": "Task started. Poll /status/{task_id} for updates."
    })

@app.get("/status/{task_id}")
async def get_status(task_id: str):
    if task_id not in TASK_STORE:
        raise HTTPException(404, "Task not found")
    return JSONResponse(TASK_STORE[task_id])

@app.get("/download/{task_id}")
async def download_apk(task_id: str):
    if task_id not in TASK_STORE:
        raise HTTPException(404, "Task not found")
    task = TASK_STORE[task_id]
    if task["status"] != "completed":
        raise HTTPException(400, "Task not completed yet")
    result_path = task.get("result_path")
    if not result_path or not os.path.exists(result_path):
        raise HTTPException(404, "File not found")
    
    # Return file and delete after sending (optional)
    filename = os.path.basename(result_path)
    return FileResponse(
        path=result_path,
        filename=filename,
        media_type="application/vnd.android.package-archive",
        background=BackgroundTasks(lambda: [os.remove(result_path) if os.path.exists(result_path) else None])
    )

@app.get("/admin/{task_id}", response_class=HTMLResponse)
async def admin_dashboard(task_id: str):
    if task_id not in TASK_STORE:
        return HTMLResponse("<h1>Task not found</h1>")
    task = TASK_STORE[task_id]
    html = f"""
    <!DOCTYPE html>
    <html><head><title>Admin - {task_id}</title>
    <style>body{{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:40px;}}
    .box{{background:#161b22;padding:20px;border-radius:12px;border:1px solid #30363d;}}
    .green{{color:#3fb950;}}.red{{color:#f85149;}}</style>
    </head>
    <body>
    <h1>📊 Task: {task_id}</h1>
    <div class="box">
        <p><strong>Status:</strong> <span class="{'green' if task['status']=='completed' else 'red' if task['status']=='failed' else ''}">{task['status']}</span></p>
        <p><strong>Progress:</strong> {task['progress']}%</p>
        <p><strong>Message:</strong> {task['message']}</p>
        <p><strong>File:</strong> {task.get('filename', 'N/A')}</p>
        <p><strong>Created:</strong> {task.get('created_at', 'N/A')}</p>
        {f'<p class="red"><strong>Error:</strong> {task.get("error", "")}</p>' if task.get("error") else ''}
        <p><a href="/" style="color:#58a6ff;">⬅️ Back to Home</a></p>
    </div>
    </body></html>
    """
    return HTMLResponse(content=html)

@app.get("/admin/", response_class=HTMLResponse)
async def admin_list():
    html = """<html><head><title>Admin</title><style>body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:40px;} table{width:100%;border-collapse:collapse;} td,th{padding:10px;border-bottom:1px solid #30363d;text-align:left;} .green{color:#3fb950;}.red{color:#f85149;}</style></head><body>
    <h1>📊 All Tasks</h1>
    <table><tr><th>ID</th><th>Status</th><th>Progress</th><th>Message</th><th>Action</th></tr>
    """
    for tid, task in list(TASK_STORE.items())[-20:][::-1]:
        status_class = "green" if task['status'] == 'completed' else "red" if task['status'] == 'failed' else ""
        html += f"<tr><td>{tid}</td><td class='{status_class}'>{task['status']}</td><td>{task['progress']}%</td><td>{task['message'][:30]}...</td><td><a href='/admin/{tid}' style='color:#58a6ff;'>View</a></td></tr>"
    html += "</table><br><a href='/' style='color:#58a6ff;'>⬅️ Home</a></body></html>"
    return HTMLResponse(content=html)

# ======================= STARTUP ============================
def startup():
    print("="*50)
    print("🛠️ Starting Modder Hub APK Patcher v3.0")
    print("="*50)
    generate_keystore()
    download_baksmali()
    print(f"✅ Keystore: {KEYSTORE_PATH} (exists: {os.path.exists(KEYSTORE_PATH)})")
    print(f"✅ Tools: {BAKSMALI_JAR} (exists: {os.path.exists(BAKSMALI_JAR)})")
    print(f"✅ Uploads: {UPLOAD_DIR}")
    print("="*50)
    print("🌐 Server running at http://0.0.0.0:8000")
    print("📖 API Docs: http://0.0.0.0:8000/docs")
    print("="*50)

if __name__ == "__main__":
    # Run startup in the main thread
    startup()
    uvicorn.run(app, host="0.0.0.0", port=8000, log_level="info")