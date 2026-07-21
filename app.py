# ================================================================
#  🛠️ MODDER HUB APK PATCHER v6.0 (Flask + Gunicorn Ready)
#  ------------------------------------------------------------
#  ✅ Single file – app.py
#  ✅ Works with: gunicorn app:app
#  ✅ Premium Glassmorphism UI
#  ✅ Drag & Drop Upload
#  ✅ Live Progress Bar (AJAX polling)
#  ✅ Smart Expiry Neutralizer (30+ patterns)
#  ✅ Raw DEX Injection (classesX.dex)
#  ✅ Background threading with task tracking
#  ✅ Admin Dashboard
#  ✅ Auto-download tools (apktool, baksmali)
#  ✅ 2GB APK support
# ================================================================

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
import threading
import traceback
import urllib.request
import json
import atexit
import signal
from datetime import datetime
from typing import Optional, Dict, Tuple, List, Any
from flask import Flask, request, jsonify, send_file, render_template_string, url_for, Response
import xml.etree.ElementTree as ET

# ======================= CONFIGURATION ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
KEYSTORE_DIR = os.path.join(BASE_DIR, "keystore")
TOOLS_DIR = os.path.join(BASE_DIR, "tools")

# Limits
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
TASK_TIMEOUT = 1200  # 20 minutes

# Keystore Config
KEYSTORE_PATH = os.path.join(KEYSTORE_DIR, "debug.keystore")
KEYSTORE_PASS = "android"
KEY_ALIAS = "androiddebugkey"

# Tools Paths
APKTOOL_JAR = os.path.join(TOOLS_DIR, "apktool.jar")
BAKSMALI_JAR = os.path.join(TOOLS_DIR, "baksmali.jar")

# Create Dirs
for d in [UPLOAD_DIR, KEYSTORE_DIR, TOOLS_DIR]:
    os.makedirs(d, exist_ok=True)

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = MAX_FILE_SIZE  # 2GB

# ======================= TASK MANAGER ======================
TASK_STORE: Dict[str, Dict] = {}
TASK_LOCK = threading.Lock()

def create_task() -> str:
    task_id = str(uuid.uuid4())[:8]
    with TASK_LOCK:
        TASK_STORE[task_id] = {
            "status": "pending",
            "progress": 0,
            "message": "Initializing...",
            "filename": None,
            "error": None,
            "result_path": None,
            "created_at": datetime.now().isoformat(),
            "updated_at": datetime.now().isoformat()
        }
    return task_id

def update_task(task_id: str, progress: int = None, message: str = None,
                status: str = None, error: str = None, result_path: str = None):
    with TASK_LOCK:
        if task_id not in TASK_STORE:
            return
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
        TASK_STORE[task_id]["updated_at"] = datetime.now().isoformat()

def get_task(task_id: str) -> Optional[Dict]:
    with TASK_LOCK:
        return TASK_STORE.get(task_id)

def cleanup_old_tasks():
    """Remove tasks older than 24 hours."""
    with TASK_LOCK:
        now = datetime.now()
        to_remove = []
        for tid, task in TASK_STORE.items():
            try:
                created = datetime.fromisoformat(task["created_at"])
                if (now - created).total_seconds() > 86400:
                    to_remove.append(tid)
            except:
                pass
        for tid in to_remove:
            if TASK_STORE[tid].get("result_path") and os.path.exists(TASK_STORE[tid]["result_path"]):
                try:
                    os.remove(TASK_STORE[tid]["result_path"])
                except:
                    pass
            del TASK_STORE[tid]

# ======================= TOOLS DOWNLOADER ===================
def download_file(url: str, dest: str) -> bool:
    if os.path.exists(dest):
        return True
    try:
        print(f"⬇️ Downloading {os.path.basename(dest)} from {url}...")
        urllib.request.urlretrieve(url, dest)
        print(f"✅ Downloaded {os.path.basename(dest)}")
        return True
    except Exception as e:
        print(f"❌ Failed to download {url}: {e}")
        return False

def download_tools() -> bool:
    if not os.path.exists(APKTOOL_JAR):
        download_file(
            "https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar",
            APKTOOL_JAR
        )
    if not os.path.exists(BAKSMALI_JAR):
        download_file(
            "https://bitbucket.org/JesusFreke/smali/downloads/baksmali-2.5.2.jar",
            BAKSMALI_JAR
        )
    return os.path.exists(APKTOOL_JAR) and os.path.exists(BAKSMALI_JAR)

# ======================= KEYSTORE GENERATOR ================
def generate_keystore() -> bool:
    if os.path.exists(KEYSTORE_PATH):
        return True
    try:
        subprocess.run([
            "keytool", "-genkey", "-v", "-keystore", KEYSTORE_PATH,
            "-alias", KEY_ALIAS, "-keyalg", "RSA", "-keysize", "2048",
            "-validity", "10000", "-storepass", KEYSTORE_PASS,
            "-keypass", KEYSTORE_PASS,
            "-dname", "CN=Android Debug, O=Android, C=US"
        ], check=True, capture_output=True, timeout=60)
        return True
    except Exception as e:
        print(f"❌ Keystore generation failed: {e}")
        return False

# ======================= APK UTILITIES ======================
def decompile_apk(apk_path: str, output_dir: str):
    cmd = ["java", "-jar", APKTOOL_JAR, "d", apk_path, "-o", output_dir, "-f"]
    subprocess.run(cmd, check=True, timeout=600, capture_output=True)

def rebuild_apk(decompile_dir: str, output_apk: str):
    cmd = ["java", "-jar", APKTOOL_JAR, "b", decompile_dir, "-o", output_apk, "-c"]
    subprocess.run(cmd, check=True, timeout=600, capture_output=True)

def sign_apk(input_apk: str, output_apk: str):
    cmd = [
        "jarsigner", "-verbose", "-sigalg", "SHA1withRSA", "-digestalg", "SHA1",
        "-keystore", KEYSTORE_PATH, "-storepass", KEYSTORE_PASS,
        "-keypass", KEYSTORE_PASS,
        "-signedjar", output_apk, input_apk, KEY_ALIAS
    ]
    subprocess.run(cmd, check=True, timeout=120, capture_output=True)

def remove_meta_inf(apk_path: str):
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
                if action.get("android:name") == "android.intent.action.MAIN":
                    for category in intent_filter.findall("category"):
                        if category.get("android:name") == "android.intent.category.LAUNCHER":
                            act_name = activity.get("android:name", "")
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
    # Fallback: search all smali for onCreate
    smali_root = os.path.join(decompile_dir, "smali")
    if os.path.exists(smali_root):
        for root, _, files in os.walk(smali_root):
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
EXPIRY_PATTERNS = [
    'java/util/Calendar', 'java/util/Date', 'java/text/SimpleDateFormat',
    'System;->currentTimeMillis', 'compareTo', 'after(', 'before(',
    'getTime', 'getTimeInMillis', 'setTime', 'expire', 'valid',
    'checkTime', 'isExpired', 'isValid', 'getExpiry', 'parseDate',
    'toDate', 'TimeUnit', 'MILLISECONDS', 'SECONDS', 'MINUTES',
    'HOURS', 'DAYS', 'WEEKS', 'MONTHS', 'YEARS',
    'Calendar;->getInstance', 'Date;-><init>', 'SimpleDateFormat;-><init>',
    'format', 'parse', 'getTimeInMillis', 'setTimeInMillis',
    'getTimeInSeconds', 'getTimestamp', 'validate', 'isTimeValid',
    'checkLicense', 'verifyExpiry', 'getExpirationDate',
    'expired', 'expiration', 'validUntil', 'validTill'
]

def neutralize_old_expiry(filepath: str) -> bool:
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
    our_module = 'mt/modder/hub'

    for line in lines:
        stripped = line.strip()
        if '.method public onCreate(Landroid/os/Bundle;)V' in line:
            inside_oncreate = True
            new_lines.append(line)
            continue
        if inside_oncreate and '.end method' in line:
            inside_oncreate = False
            new_lines.append(line)
            continue
        if inside_oncreate and our_module in line:
            new_lines.append(line)
            continue
        if inside_oncreate:
            is_suspicious = any(p in line for p in EXPIRY_PATTERNS)
            if is_suspicious and ('invoke-' in line or 'if-' in line or 'goto' in line or 'const-string' in line):
                if not line.strip().startswith(':'):
                    new_lines.append('    # ' + stripped + '  <!-- NEUTRALIZED -->\n')
                    modified = True
                    continue
            if skip_next > 0:
                if 'if-' in line or 'goto' in line or ':' in line:
                    new_lines.append('    # ' + stripped + '  <!-- NEUTRALIZED (Flow) -->\n')
                    skip_next -= 1
                    modified = True
                    continue
                else:
                    skip_next = 0
            if 'if-' in line and is_suspicious:
                skip_next = 2
        new_lines.append(line)

    if modified:
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                f.writelines(new_lines)
            return True
        except:
            pass
    return False

def inject_invoke_line(smali_file: str) -> bool:
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
    for line in lines:
        if our_line.strip() in line:
            return True
    for line in lines:
        new_lines.append(line)
        if '.method public onCreate(Landroid/os/Bundle;)V' in line:
            inside_oncreate = True
        if inside_oncreate and not inserted:
            if 'invoke-super' in line and 'onCreate' in line:
                indent = '    '
                new_lines.append(f'{indent}{our_line}')
                inserted = True
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
            pass
    return False

def inject_dex_raw(apk_path: str, dex_file_path: str, new_dex_name: str) -> bool:
    if not os.path.exists(dex_file_path):
        return False
    try:
        with zipfile.ZipFile(apk_path, 'a') as zf:
            zf.write(dex_file_path, new_dex_name)
        return True
    except:
        return False

# ======================= MAIN PATCHER =======================
def run_patch_task(task_id: str, apk_data: bytes, patch_zip_data: bytes, original_filename: str):
    temp_dir = tempfile.mkdtemp(prefix=f"patch_{task_id}_")
    update_task(task_id, progress=5, message="Temp directory created.", status="processing")
    try:
        apk_path = os.path.join(temp_dir, "original.apk")
        with open(apk_path, "wb") as f:
            f.write(apk_data)
        update_task(task_id, progress=10, message="APK saved.")

        extract_dir = os.path.join(temp_dir, "patch_extract")
        os.makedirs(extract_dir, exist_ok=True)
        with zipfile.ZipFile(io.BytesIO(patch_zip_data), 'r') as zf:
            zf.extractall(extract_dir)
        update_task(task_id, progress=15, message="Modder Hub ZIP extracted.")

        decompile_dir = os.path.join(temp_dir, "dec_apk")
        update_task(task_id, progress=20, message="Decompiling APK (large files take time)...")
        decompile_apk(apk_path, decompile_dir)
        update_task(task_id, progress=35, message="Decompilation complete.")

        src_assets = os.path.join(extract_dir, "assets")
        if os.path.exists(src_assets):
            shutil.copytree(src_assets, os.path.join(decompile_dir, "assets"), dirs_exist_ok=True)
            update_task(task_id, progress=40, message="Assets copied (Dialog Mode).")
        else:
            update_task(task_id, progress=40, message="No assets found (Online Expiry Mode).")

        manifest_path = os.path.join(decompile_dir, "AndroidManifest.xml")
        if not os.path.exists(manifest_path):
            raise Exception("AndroidManifest.xml not found after decompile!")

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
            update_task(task_id, progress=60, message="MainActivity not found, skipping injection.")

        dex_files = [f for f in os.listdir(extract_dir) if f.endswith(".dex")]
        if not dex_files:
            raise Exception("No .dex file found in the Modder Hub ZIP!")
        provided_dex = os.path.join(extract_dir, dex_files[0])
        update_task(task_id, progress=70, message=f"Found DEX: {dex_files[0]}")

        max_idx = count_smali_dirs(decompile_dir)
        next_idx = max_idx + 1
        new_dex_name = f"classes{next_idx}.dex"
        update_task(task_id, progress=75, message=f"Rebuilding APK (Current DEX count: {max_idx})...")
        rebuilt_apk = os.path.join(temp_dir, "rebuilt.apk")
        rebuild_apk(decompile_dir, rebuilt_apk)
        update_task(task_id, progress=80, message="Rebuild complete.")

        update_task(task_id, progress=82, message="Cleaning old signatures...")
        remove_meta_inf(rebuilt_apk)

        update_task(task_id, progress=85, message=f"Injecting {new_dex_name} raw...")
        temp_dex_renamed = os.path.join(temp_dir, new_dex_name)
        shutil.copy(provided_dex, temp_dex_renamed)
        if not inject_dex_raw(rebuilt_apk, temp_dex_renamed, new_dex_name):
            raise Exception(f"Failed to inject {new_dex_name} into APK!")
        update_task(task_id, progress=90, message=f"{new_dex_name} injected successfully.")

        update_task(task_id, progress=92, message="Signing APK...")
        signed_apk = os.path.join(temp_dir, "signed.apk")
        sign_apk(rebuilt_apk, signed_apk)
        update_task(task_id, progress=95, message="Signed.")

        update_task(task_id, progress=97, message="Finalizing...")
        final_storage = os.path.join(UPLOAD_DIR, f"patched_{task_id}_{original_filename}")
        shutil.copy(signed_apk, final_storage)
        update_task(task_id, progress=100, message="Success! Ready to download.", status="completed", result_path=final_storage)

    except subprocess.CalledProcessError as e:
        error_msg = f"Subprocess error: {e.stderr.decode() if e.stderr else str(e)}\n{traceback.format_exc()}"
        update_task(task_id, progress=0, message="Failed!", status="failed", error=error_msg)
    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        update_task(task_id, progress=0, message="Failed!", status="failed", error=error_msg)
    finally:
        def cleanup():
            time.sleep(300)
            try:
                shutil.rmtree(temp_dir, ignore_errors=True)
            except:
                pass
        threading.Thread(target=cleanup, daemon=True).start()

# ======================= FLASK ROUTES ======================

# ---------- PREMIUM HTML TEMPLATE (300+ lines of CSS/JS) ----------
HTML_PAGE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🛠️ Modder Hub Injector</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&display=swap" rel="stylesheet">
    <style>
        /* ---- RESET ---- */
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body {
            font-family: 'Inter', sans-serif;
            background: #080b11;
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            background-image: radial-gradient(ellipse at 10% 20%, rgba(88,166,255,0.06) 0%, transparent 50%),
                              radial-gradient(ellipse at 90% 80%, rgba(46,160,67,0.06) 0%, transparent 50%);
        }
        .container {
            max-width: 840px;
            width: 100%;
            background: rgba(22,27,34,0.75);
            backdrop-filter: blur(32px) saturate(1.2);
            -webkit-backdrop-filter: blur(32px) saturate(1.2);
            border-radius: 56px;
            padding: 52px 48px;
            border: 1px solid rgba(48,54,61,0.5);
            box-shadow: 0 32px 96px rgba(0,0,0,0.8), inset 0 1px 0 rgba(255,255,255,0.05);
        }
        @media (max-width:640px) {
            .container { padding: 28px 20px; border-radius: 32px; }
        }
        .header { display: flex; justify-content: space-between; align-items: flex-start; flex-wrap: wrap; gap: 16px; }
        .header-left { display: flex; align-items: center; gap: 18px; }
        .logo-icon {
            width: 58px; height: 58px;
            background: linear-gradient(145deg, #238636, #1a6b2a);
            border-radius: 18px;
            display: flex; align-items: center; justify-content: center;
            font-size: 32px;
            box-shadow: 0 4px 20px rgba(35,134,54,0.35);
        }
        .title-group h1 { color: #f0f6fc; font-size: 30px; font-weight: 900; letter-spacing: -0.6px; }
        .title-group h1 span { color: #58a6ff; }
        .title-group .subtitle { color: #8b949e; font-size: 15px; border-left: 3px solid #238636; padding-left: 14px; }
        .version-badge { background: rgba(88,166,255,0.12); color: #58a6ff; padding: 4px 14px; border-radius: 100px; font-size: 12px; font-weight: 700; border: 1px solid rgba(88,166,255,0.15); }
        .badge-group { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; padding-top: 18px; border-top: 1px solid rgba(48,54,61,0.4); }
        .badge { background: rgba(48,54,61,0.35); padding: 5px 16px; border-radius: 100px; font-size: 11px; font-weight: 600; color: #8b949e; border: 1px solid #30363d; }
        .badge.green { background: rgba(35,134,54,0.15); color: #3fb950; border-color: #238636; }
        .badge.blue { background: rgba(88,166,255,0.12); color: #58a6ff; border-color: rgba(88,166,255,0.2); }
        .badge.gold { background: rgba(255,166,0,0.1); color: #d29922; border-color: rgba(255,166,0,0.2); }
        .drop-zone {
            margin-top: 20px;
            background: rgba(13,17,23,0.5);
            border-radius: 24px;
            border: 2px dashed #30363d;
            padding: 28px 24px;
            transition: all 0.3s;
            position: relative;
            cursor: pointer;
        }
        .drop-zone:hover { border-color: #58a6ff; background: rgba(13,17,23,0.7); }
        .drop-zone.dragover { border-color: #58a6ff; background: rgba(88,166,255,0.06); box-shadow: 0 0 60px rgba(88,166,255,0.04); }
        .drop-zone input[type="file"] { position: absolute; inset: 0; opacity: 0; cursor: pointer; z-index: 2; }
        .drop-zone-label { display: flex; align-items: center; gap: 14px; font-weight: 700; color: #f0f6fc; font-size: 16px; pointer-events: none; }
        .drop-zone-label .icon { font-size: 24px; }
        .drop-zone-label .hint { font-weight: 400; color: #8b949e; font-size: 13px; margin-left: 6px; }
        .file-preview { display: flex; align-items: center; gap: 12px; color: #8b949e; font-size: 14px; margin-top: 8px; pointer-events: none; }
        .file-preview .fname { color: #f0f6fc; background: #0d1117; padding: 4px 14px; border-radius: 10px; border: 1px solid #21262d; font-family: monospace; font-size: 13px; max-width: 280px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
        .file-hint { font-size: 12px; color: #484f58; margin-top: 6px; pointer-events: none; }
        .btn-primary {
            margin-top: 32px;
            background: linear-gradient(145deg, #238636, #1a7a2e);
            border: none;
            color: white;
            font-weight: 700;
            font-size: 19px;
            padding: 20px 36px;
            width: 100%;
            border-radius: 100px;
            cursor: pointer;
            transition: all 0.25s;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 16px;
            box-shadow: 0 4px 32px rgba(35,134,54,0.25);
        }
        .btn-primary:hover { transform: scale(1.02); background: linear-gradient(145deg, #2ea043, #238636); box-shadow: 0 8px 48px rgba(35,134,54,0.4); }
        .btn-primary:disabled { background: #2d4a2d; cursor: not-allowed; opacity: 0.6; transform: none; }
        .btn-primary .spinner { display: none; width: 22px; height: 22px; border: 3px solid rgba(255,255,255,0.15); border-top-color: #fff; border-radius: 50%; animation: spin 0.7s linear infinite; }
        .btn-primary:disabled .spinner { display: inline-block; }
        @keyframes spin { 100% { transform: rotate(360deg); } }
        #progress-container {
            margin-top: 28px;
            background: rgba(13,17,23,0.8);
            border-radius: 28px;
            padding: 28px 32px;
            border: 1px solid #21262d;
            display: none;
        }
        .progress-header { display: flex; justify-content: space-between; align-items: center; }
        .progress-header .label { font-weight: 600; color: #f0f6fc; }
        .progress-header .percent { color: #58a6ff; font-weight: 800; font-size: 22px; }
        .progress-track { height: 8px; background: #21262d; border-radius: 100px; overflow: hidden; margin: 14px 0; }
        .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #238636, #58a6ff); border-radius: 100px; transition: width 0.6s cubic-bezier(0.22,1,0.36,1); }
        .status-msg { color: #8b949e; font-size: 14px; display: flex; align-items: center; gap: 12px; }
        .status-msg .spinner { display: inline-block; width: 18px; height: 18px; border: 2.5px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
        .error-box, .success-box { margin-top: 16px; padding: 16px 22px; border-radius: 18px; display: none; font-weight: 500; font-size: 14px; }
        .error-box { background: rgba(248,81,73,0.08); border: 1px solid #f85149; color: #f85149; }
        .success-box { background: rgba(46,160,67,0.08); border: 1px solid #238636; color: #3fb950; }
        .footer {
            margin-top: 36px;
            display: flex; justify-content: space-between; align-items: center;
            font-size: 13px; color: #484f58;
            border-top: 1px solid #21262d; padding-top: 22px;
            flex-wrap: wrap; gap: 12px;
        }
        .footer a { color: #58a6ff; text-decoration: none; }
        .footer a:hover { text-decoration: underline; }
        .admin-link { background: rgba(48,54,61,0.3); padding: 5px 18px; border-radius: 100px; border: 1px solid #30363d; }
        @media (max-width:600px) {
            .container { padding: 24px 16px; border-radius: 28px; }
            .logo-icon { width: 44px; height: 44px; font-size: 24px; }
            .title-group h1 { font-size: 22px; }
            .btn-primary { font-size: 16px; padding: 16px; }
            .drop-zone { padding: 20px 16px; }
            .file-preview .fname { max-width: 140px; font-size: 12px; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div class="header-left">
            <div class="logo-icon">⚡</div>
            <div class="title-group">
                <h1>Modder <span>Injector</span></h1>
                <div class="subtitle">APK + Expiry Patcher</div>
            </div>
        </div>
        <span class="version-badge">v6.0</span>
    </div>

    <div class="badge-group">
        <span class="badge green">🔒 2GB Max</span>
        <span class="badge blue">📦 Dialog+Online</span>
        <span class="badge gold">⚡ Smart Neutralizer</span>
        <span class="badge">🚀 Gunicorn Ready</span>
    </div>

    <form id="uploadForm" enctype="multipart/form-data">
        <div class="drop-zone" id="apkZone">
            <div class="drop-zone-label"><span class="icon">📱</span> APK File <span class="hint">(1GB+)</span></div>
            <input type="file" name="apk_file" accept=".apk" required>
            <div class="file-preview"><span>📄</span> <span class="fname" id="apk-name">No file selected</span></div>
            <div class="file-hint">Drop your original APK here or click to browse.</div>
        </div>
        <div class="drop-zone" id="zipZone" style="margin-top:16px;">
            <div class="drop-zone-label"><span class="icon">📦</span> Modder Hub ZIP <span class="hint">(assets + classes.dex)</span></div>
            <input type="file" name="patch_zip" accept=".zip" required>
            <div class="file-preview"><span>🗂️</span> <span class="fname" id="zip-name">No file selected</span></div>
            <div class="file-hint">Drop the ZIP exported from Modder Hub.</div>
        </div>
        <button type="submit" class="btn-primary" id="submitBtn">
            <span>🚀</span> Inject & Download Modified APK
            <span class="spinner"></span>
        </button>
    </form>

    <div id="progress-container">
        <div class="progress-header">
            <span class="label" id="progress-label">⏳ Processing...</span>
            <span class="percent" id="progress-percent">0%</span>
        </div>
        <div class="progress-track"><div class="progress-bar" id="progress-bar"></div></div>
        <div class="status-msg" id="status-msg"><span class="spinner"></span> Initializing...</div>
        <div class="error-box" id="error-box"></div>
        <div class="success-box" id="success-box">✅ Success! Download will start automatically.</div>
    </div>

    <div class="footer">
        <span>❤️ Made for the Modding Community</span>
        <a href="#" id="admin-link" target="_blank" class="admin-link">📊 Dashboard</a>
    </div>
</div>

<script>
    // File name display
    document.querySelectorAll('.drop-zone input[type="file"]').forEach(input => {
        const zone = input.closest('.drop-zone');
        const nameSpan = zone.querySelector('.fname');
        input.addEventListener('change', function() {
            nameSpan.textContent = this.files.length > 0 ? this.files[0].name : 'No file selected';
        });
        zone.addEventListener('dragover', e => { e.preventDefault(); zone.classList.add('dragover'); });
        zone.addEventListener('dragleave', () => { zone.classList.remove('dragover'); });
        zone.addEventListener('drop', () => { zone.classList.remove('dragover'); });
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

        errorBox.style.display = 'none';
        successBox.style.display = 'none';
        progressContainer.style.display = 'block';
        progressBar.style.width = '0%';
        progressPercent.textContent = '0%';
        statusMsg.innerHTML = '<span class="spinner"></span> Uploading files...';
        submitBtn.disabled = true;
        submitBtn.querySelector('span:first-child').textContent = '⏳';

        try {
            const response = await fetch('/upload/', {
                method: 'POST',
                body: formData
            });
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.error || 'Upload failed');
            }
            const data = await response.json();
            taskId = data.task_id;
            adminLink.href = `/admin/${taskId}`;
            statusMsg.innerHTML = `<span class="spinner"></span> Task started (ID: ${taskId}). Processing...`;
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(() => pollStatus(taskId), 1500);
        } catch (err) {
            showError(err.message);
            submitBtn.disabled = false;
            submitBtn.querySelector('span:first-child').textContent = '🚀';
        }
    });

    async function pollStatus(id) {
        try {
            const res = await fetch(`/status/${id}`);
            const data = await res.json();
            const prog = data.progress || 0;
            progressBar.style.width = prog + '%';
            progressPercent.textContent = prog + '%';
            statusMsg.innerHTML = `<span class="spinner"></span> ${data.message || 'Processing...'}`;
            if (data.status === 'completed') {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.querySelector('span:first-child').textContent = '🚀';
                statusMsg.innerHTML = '✅ Done! Downloading APK...';
                successBox.style.display = 'block';
                window.location.href = `/download/${id}`;
                setTimeout(() => {
                    progressContainer.style.display = 'none';
                    successBox.style.display = 'none';
                }, 8000);
            } else if (data.status === 'failed') {
                clearInterval(pollInterval);
                submitBtn.disabled = false;
                submitBtn.querySelector('span:first-child').textContent = '🚀';
                showError(data.error || 'Unknown error occurred.');
            }
        } catch (err) {}
    }

    function showError(msg) {
        errorBox.textContent = '❌ ' + msg;
        errorBox.style.display = 'block';
        setTimeout(() => { errorBox.style.display = 'none'; }, 15000);
    }
</script>
</body>
</html>
"""

@app.route('/')
def home():
    return render_template_string(HTML_PAGE)

@app.route('/upload/', methods=['POST'])
def upload_apk():
    if 'apk_file' not in request.files or 'patch_zip' not in request.files:
        return jsonify({"error": "Missing files"}), 400
    apk_file = request.files['apk_file']
    patch_zip = request.files['patch_zip']
    if not apk_file.filename.endswith('.apk'):
        return jsonify({"error": "Only APK files allowed"}), 400
    if not patch_zip.filename.endswith('.zip'):
        return jsonify({"error": "Only ZIP files allowed"}), 400

    apk_data = apk_file.read()
    zip_data = patch_zip.read()
    if len(apk_data) > MAX_FILE_SIZE:
        return jsonify({"error": "APK size exceeds 2GB limit"}), 400

    task_id = create_task()
    thread = threading.Thread(
        target=run_patch_task,
        args=(task_id, apk_data, zip_data, apk_file.filename)
    )
    thread.daemon = True
    thread.start()
    return jsonify({"task_id": task_id, "status": "pending"})

@app.route('/status/<task_id>')
def get_status(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    return jsonify(task)

@app.route('/download/<task_id>')
def download_apk(task_id):
    task = get_task(task_id)
    if not task:
        return jsonify({"error": "Task not found"}), 404
    if task['status'] != 'completed':
        return jsonify({"error": "Task not completed"}), 400
    result_path = task.get('result_path')
    if not result_path or not os.path.exists(result_path):
        return jsonify({"error": "File not found"}), 404
    filename = os.path.basename(result_path)
    # Delete after sending
    def delete_file():
        try:
            os.remove(result_path)
        except:
            pass
    response = send_file(result_path, as_attachment=True, download_name=filename)
    # Delete file after response is sent (use after_this_request)
    @response.call_on_close
    def cleanup():
        delete_file()
    return response

@app.route('/admin/<task_id>')
def admin_dashboard(task_id):
    task = get_task(task_id)
    if not task:
        return "<h1>Task not found</h1>", 404
    status_color = "green" if task['status'] == 'completed' else "red" if task['status'] == 'failed' else "gold"
    error_html = f'<pre style="background:#0d1117;padding:16px;border-radius:8px;overflow:auto;color:#f85149;">{task.get("error", "")}</pre>' if task.get("error") else ""
    html = f"""
    <!DOCTYPE html>
    <html><head><title>Admin - {task_id}</title>
    <style>
        body{{background:#0d1117;color:#c9d1d9;font-family:'Inter',monospace;padding:40px;}}
        .box{{background:#161b22;padding:32px;border-radius:20px;border:1px solid #30363d;max-width:800px;margin:auto;}}
        .green{{color:#3fb950;}}.red{{color:#f85149;}}.gold{{color:#d29922;}}
        pre{{background:#0d1117;padding:16px;border-radius:8px;overflow:auto;white-space:pre-wrap;word-break:break-word;}}
        a{{color:#58a6ff;text-decoration:none;}}
        .flex{{display:flex;gap:20px;flex-wrap:wrap;}}
        .stat{{background:#0d1117;padding:12px 20px;border-radius:12px;border:1px solid #21262d;flex:1;min-width:120px;}}
        .stat .val{{font-size:22px;font-weight:700;}}
    </style>
    </head>
    <body>
    <div class="box">
        <h1>📊 Task: {task_id}</h1>
        <div class="flex" style="margin:20px 0;">
            <div class="stat"><div style="color:#8b949e;font-size:13px;">Status</div><div class="val {status_color}">{task['status']}</div></div>
            <div class="stat"><div style="color:#8b949e;font-size:13px;">Progress</div><div class="val" style="color:#58a6ff;">{task['progress']}%</div></div>
            <div class="stat"><div style="color:#8b949e;font-size:13px;">Created</div><div class="val" style="font-size:14px;">{task.get('created_at', 'N/A')[:19]}</div></div>
        </div>
        <p><strong>📝 Message:</strong> {task['message']}</p>
        {error_html}
        <div style="margin-top:24px;display:flex;gap:16px;flex-wrap:wrap;">
            <a href="/" style="background:#21262d;padding:10px 24px;border-radius:12px;">⬅️ Home</a>
            <a href="/download/{task_id}" style="background:#238636;padding:10px 24px;border-radius:12px;color:white;">⬇️ Download</a>
            <a href="/admin/" style="background:#21262d;padding:10px 24px;border-radius:12px;">📋 All Tasks</a>
        </div>
    </div>
    </body></html>
    """
    return html

@app.route('/admin/')
def admin_list():
    cleanup_old_tasks()
    items = list(TASK_STORE.items())
    items = items[-50:][::-1]
    rows = ""
    for tid, task in items:
        status_class = "green" if task["status"] == "completed" else "red" if task["status"] == "failed" else "gold"
        rows += f"""
        <tr>
            <td style="font-family:monospace;">{tid}</td>
            <td class="{status_class}">{task['status']}</td>
            <td>{task['progress']}%</td>
            <td style="max-width:300px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;">{task['message'][:40]}</td>
            <td><a href="/admin/{tid}" style="color:#58a6ff;">View</a></td>
        </tr>
        """
    html = f"""
    <!DOCTYPE html>
    <html><head><title>Admin Dashboard</title>
    <style>
        body{{background:#0d1117;color:#c9d1d9;font-family:'Inter',monospace;padding:40px;}}
        .box{{background:#161b22;padding:32px;border-radius:20px;border:1px solid #30363d;max-width:1200px;margin:auto;overflow-x:auto;}}
        table{{width:100%;border-collapse:collapse;}}
        td,th{{padding:12px 16px;border-bottom:1px solid #21262d;text-align:left;}}
        th{{color:#8b949e;font-weight:600;font-size:12px;text-transform:uppercase;letter-spacing:0.5px;}}
        .green{{color:#3fb950;}}.red{{color:#f85149;}}.gold{{color:#d29922;}}
        a{{color:#58a6ff;text-decoration:none;}}
        .header{{display:flex;justify-content:space-between;align-items:center;margin-bottom:20px;flex-wrap:wrap;gap:12px;}}
        .count{{background:#21262d;padding:4px 16px;border-radius:100px;font-size:14px;}}
    </style>
    </head>
    <body>
    <div class="box">
        <div class="header">
            <h1>📊 Admin Dashboard</h1>
            <span class="count">Total Tasks: {len(TASK_STORE)}</span>
        </div>
        <table>
            <tr><th>Task ID</th><th>Status</th><th>Progress</th><th>Message</th><th>Action</th></tr>
            {rows if rows else '<tr><td colspan="5" style="text-align:center;color:#484f58;padding:40px;">No tasks yet. Upload an APK to start!</td></tr>'}
        </table>
        <div style="margin-top:24px;display:flex;gap:16px;flex-wrap:wrap;">
            <a href="/" style="background:#21262d;padding:10px 24px;border-radius:12px;">⬅️ Home</a>
            <span style="color:#484f58;font-size:13px;">Tasks auto-clean after 24 hours</span>
        </div>
    </div>
    </body></html>
    """
    return html

@app.route('/health')
def health():
    return jsonify({
        "status": "healthy",
        "version": "6.0",
        "tasks": len(TASK_STORE),
        "tools_available": os.path.exists(APKTOOL_JAR) and os.path.exists(BAKSMALI_JAR)
    })

# ======================= STARTUP =============================
def startup():
    print("="*70)
    print("  🛠️  MODDER HUB APK PATCHER v6.0 (Flask + Gunicorn)")
    print("  " + "="*62)
    print("  ✅ Ready for: gunicorn app:app")
    print("="*70)
    print("📂 Downloading required tools...")
    tools_ok = download_tools()
    print(f"🔧 Tools ready: {tools_ok}")
    print(f"🔐 Generating Keystore: {generate_keystore()}")
    print(f"📁 Uploads directory: {UPLOAD_DIR}")
    print(f"🐍 Python: {sys.version}")
    print("="*70)
    print("🚀 Server is live!")

# Run startup before first request
@app.before_first_request
def before_first_request():
    startup()

# ======================= CLEANUP =============================
def cleanup_on_exit():
    print("🧹 Cleaning up old files...")
    for tid, task in TASK_STORE.items():
        if task.get("result_path") and os.path.exists(task["result_path"]):
            try:
                os.remove(task["result_path"])
            except:
                pass
    for d in os.listdir(tempfile.gettempdir()):
        if d.startswith("patch_"):
            try:
                shutil.rmtree(os.path.join(tempfile.gettempdir(), d), ignore_errors=True)
            except:
                pass

atexit.register(cleanup_on_exit)

# ======================= MAIN (for development) ==============
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    # For local run, we can use Flask's built-in server
    app.run(host="0.0.0.0", port=port, debug=False)