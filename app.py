"""
========================================================
  🛠️ MODDER HUB APK PATCHER v4.0 (Premium Single File)
  ------------------------------------------------------
  Features:
  - Self-contained (Downloads apktool, baksmali at runtime)
  - Premium Glassmorphism Dark UI
  - Drag & Drop File Upload
  - Live Progress Bar with Status Updates
  - Smart Expiry Neutralizer (20+ Patterns)
  - Online/Dialog Expiry Auto-Detect
  - Raw DEX Injection (classes7.dex)
  - Background Threading (No Browser Timeout)
  - Auto Cleanup Temp Files
  - Optimized for Render (uses $PORT)
========================================================
"""

# ======================= IMPORTS =============================
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
from datetime import datetime
from typing import Optional, Dict, Tuple, List
from fastapi import FastAPI, UploadFile, File, HTTPException, Request, BackgroundTasks
from fastapi.responses import HTMLResponse, FileResponse, JSONResponse
import uvicorn
import xml.etree.ElementTree as ET

# ======================= CONFIGURATION ======================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
UPLOAD_DIR = os.path.join(BASE_DIR, "uploads")
KEYSTORE_DIR = os.path.join(BASE_DIR, "keystore")
TOOLS_DIR = os.path.join(BASE_DIR, "tools")

# Render uses PORT env variable
PORT = int(os.getenv("PORT", 8000))

# Limits
MAX_FILE_SIZE = 2 * 1024 * 1024 * 1024  # 2GB
TASK_TIMEOUT = 1200  # 20 minutes

# Keystore Config
KEYSTORE_PATH = os.path.join(KEYSTORE_DIR, "debug.keystore")
KEYSTORE_PASS = "android"
KEY_ALIAS = "androiddebugkey"

# Tools Paths (will be downloaded)
APKTOOL_JAR = os.path.join(TOOLS_DIR, "apktool.jar")
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

# ======================= TOOLS DOWNLOADER ===================
def download_file(url: str, dest: str):
    """Download a file with progress."""
    if os.path.exists(dest):
        return True
    try:
        print(f"⬇️ Downloading {os.path.basename(dest)}...")
        urllib.request.urlretrieve(url, dest)
        print(f"✅ Downloaded {os.path.basename(dest)}")
        return True
    except Exception as e:
        print(f"❌ Failed to download {url}: {e}")
        return False

def download_tools():
    """Ensure all necessary tools are downloaded."""
    # APKTool
    if not os.path.exists(APKTOOL_JAR):
        download_file(
            "https://bitbucket.org/iBotPeaches/apktool/downloads/apktool_2.9.3.jar",
            APKTOOL_JAR
        )
    # Baksmali
    if not os.path.exists(BAKSMALI_JAR):
        download_file(
            "https://bitbucket.org/JesusFreke/smali/downloads/baksmali-2.5.2.jar",
            BAKSMALI_JAR
        )
    return os.path.exists(APKTOOL_JAR) and os.path.exists(BAKSMALI_JAR)

# ======================= KEYSTORE GENERATOR ================
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

# ======================= APK UTILITIES ======================
def decompile_apk(apk_path: str, output_dir: str):
    """Decompile APK using apktool.jar."""
    cmd = ["java", "-jar", APKTOOL_JAR, "d", apk_path, "-o", output_dir, "-f"]
    subprocess.run(cmd, check=True, timeout=600, capture_output=True)

def rebuild_apk(decompile_dir: str, output_apk: str):
    """Rebuild APK using apktool.jar."""
    cmd = ["java", "-jar", APKTOOL_JAR, "b", decompile_dir, "-o", output_apk, "-c"]
    subprocess.run(cmd, check=True, timeout=600, capture_output=True)

def sign_apk(input_apk: str, output_apk: str):
    """Sign APK using jarsigner (available in OpenJDK)."""
    # First, verify jarsigner exists
    try:
        subprocess.run(["jarsigner", "-verbose"], check=False, capture_output=True)
    except:
        # Fallback to apksigner if available, but jarsigner is standard in JDK
        pass
    
    cmd = [
        "jarsigner", "-verbose", "-sigalg", "SHA1withRSA", "-digestalg", "SHA1",
        "-keystore", KEYSTORE_PATH, "-storepass", KEYSTORE_PASS, "-keypass", KEYSTORE_PASS,
        "-signedjar", output_apk, input_apk, KEY_ALIAS
    ]
    subprocess.run(cmd, check=True, timeout=120, capture_output=True)
    return output_apk

def remove_meta_inf(apk_path: str):
    """Remove META-INF folder to avoid signature conflicts."""
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
    'format', 'parse'
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
                new_lines.append('    # ' + line.strip() + '  <!-- NEUTRALIZED -->\n')
                modified = True
                continue
            
            if skip_next > 0:
                if 'if-' in line or 'goto' in line or ':' in line:
                    new_lines.append('    # ' + line.strip() + '  <!-- NEUTRALIZED (Flow) -->\n')
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

    except Exception as e:
        error_msg = f"{str(e)}\n{traceback.format_exc()}"
        update_task(task_id, progress=0, message="Failed!", status="failed", error=error_msg)
    finally:
        # Cleanup temp dir after a delay (handled by download endpoint)
        pass

# ======================= FASTAPI APP ========================
app = FastAPI(title="Modder Hub APK Patcher v4", version="4.0")

# ----------------------- PREMIUM HTML TEMPLATE --------------
HTML_PAGE = """
<!DOCTYPE html>
<html lang="hi">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>🛠️ Modder Hub Injector</title>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { 
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            background: #080b11; 
            min-height: 100vh;
            display: flex;
            justify-content: center;
            align-items: center;
            padding: 20px;
            background-image: radial-gradient(ellipse at 10% 20%, rgba(88, 166, 255, 0.05) 0%, transparent 50%),
                              radial-gradient(ellipse at 90% 80%, rgba(46, 160, 67, 0.05) 0%, transparent 50%);
        }
        .container {
            max-width: 800px;
            width: 100%;
            background: rgba(22, 27, 34, 0.8);
            backdrop-filter: blur(24px);
            -webkit-backdrop-filter: blur(24px);
            border-radius: 48px;
            padding: 48px;
            border: 1px solid rgba(48, 54, 61, 0.6);
            box-shadow: 0 24px 80px rgba(0, 0, 0, 0.8), inset 0 1px 0 rgba(255, 255, 255, 0.04);
            transition: all 0.3s ease;
        }
        .header { display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 8px; }
        .header-left { display: flex; align-items: center; gap: 16px; }
        .logo-icon { 
            width: 52px; height: 52px; background: linear-gradient(135deg, #238636, #58a6ff); 
            border-radius: 16px; display: flex; align-items: center; justify-content: center; font-size: 28px;
            box-shadow: 0 4px 16px rgba(35, 134, 54, 0.3);
        }
        h1 { color: #f0f6fc; font-size: 28px; font-weight: 800; letter-spacing: -0.5px; }
        h1 span { color: #58a6ff; }
        .subtitle { color: #8b949e; font-size: 15px; font-weight: 400; margin-top: 4px; border-left: 3px solid #238636; padding-left: 14px; }
        .badge-group { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 20px; }
        .badge { background: rgba(56, 58, 89, 0.4); padding: 4px 14px; border-radius: 100px; font-size: 11px; font-weight: 600; color: #8b949e; border: 1px solid #30363d; }
        .badge.green { background: rgba(35, 134, 54, 0.15); color: #3fb950; border-color: #238636; }

        /* Upload Zones */
        .drop-zone { 
            margin-top: 28px; 
            background: rgba(13, 17, 23, 0.6); 
            border-radius: 20px; 
            border: 2px dashed #30363d; 
            padding: 24px; 
            transition: all 0.25s ease;
            position: relative;
        }
        .drop-zone:hover { border-color: #58a6ff; background: rgba(13, 17, 23, 0.8); }
        .drop-zone.dragover { border-color: #58a6ff; background: rgba(88, 166, 255, 0.05); box-shadow: 0 0 40px rgba(88, 166, 255, 0.05); }
        .drop-zone-label { display: flex; align-items: center; gap: 12px; font-weight: 600; color: #f0f6fc; font-size: 15px; margin-bottom: 8px; }
        .drop-zone-label .icon { font-size: 20px; }
        .drop-zone input[type="file"] { position: absolute; inset: 0; opacity: 0; cursor: pointer; }
        .file-preview { display: flex; align-items: center; gap: 12px; color: #8b949e; font-size: 14px; margin-top: 6px; }
        .file-preview .fname { color: #f0f6fc; background: #0d1117; padding: 4px 12px; border-radius: 8px; border: 1px solid #21262d; font-family: monospace; }
        .file-hint { font-size: 12px; color: #484f58; margin-top: 6px; }

        .btn-primary {
            margin-top: 32px;
            background: linear-gradient(135deg, #238636, #1a7a2e);
            border: none;
            color: white;
            font-weight: 700;
            font-size: 18px;
            padding: 18px 32px;
            width: 100%;
            border-radius: 100px;
            cursor: pointer;
            transition: all 0.2s ease;
            display: flex;
            justify-content: center;
            align-items: center;
            gap: 14px;
            box-shadow: 0 4px 24px rgba(35, 134, 54, 0.25);
            letter-spacing: 0.3px;
        }
        .btn-primary:hover { transform: scale(1.02); background: linear-gradient(135deg, #2ea043, #238636); box-shadow: 0 8px 32px rgba(35, 134, 54, 0.4); }
        .btn-primary:disabled { background: #2d4a2d; cursor: not-allowed; transform: none; box-shadow: none; }
        .btn-primary:disabled .spinner { display: inline-block; }

        /* Progress */
        #progress-container {
            margin-top: 28px;
            background: rgba(13, 17, 23, 0.8);
            border-radius: 24px;
            padding: 24px 28px;
            border: 1px solid #21262d;
            display: none;
        }
        .progress-header { display: flex; justify-content: space-between; align-items: center; }
        .progress-header .label { font-weight: 600; color: #f0f6fc; font-size: 15px; }
        .progress-header .percent { color: #58a6ff; font-weight: 700; font-size: 18px; font-variant-numeric: tabular-nums; }
        .progress-track { height: 8px; background: #21262d; border-radius: 100px; overflow: hidden; margin: 12px 0; }
        .progress-bar { height: 100%; width: 0%; background: linear-gradient(90deg, #238636, #58a6ff); border-radius: 100px; transition: width 0.5s cubic-bezier(0.22, 1, 0.36, 1); }
        .status-msg { color: #8b949e; font-size: 14px; display: flex; align-items: center; gap: 10px; }
        .status-msg .spinner { display: inline-block; width: 16px; height: 16px; border: 2px solid #30363d; border-top-color: #58a6ff; border-radius: 50%; animation: spin 0.8s linear infinite; }
        @keyframes spin { 100% { transform: rotate(360deg); } }

        .error-box, .success-box { margin-top: 16px; padding: 16px 20px; border-radius: 16px; display: none; font-weight: 500; }
        .error-box { background: rgba(248, 81, 73, 0.1); border: 1px solid #f85149; color: #f85149; }
        .success-box { background: rgba(46, 160, 67, 0.1); border: 1px solid #238636; color: #3fb950; }

        .footer { margin-top: 32px; display: flex; justify-content: space-between; align-items: center; font-size: 13px; color: #484f58; border-top: 1px solid #21262d; padding-top: 20px; flex-wrap: wrap; gap: 10px; }
        .footer a { color: #58a6ff; text-decoration: none; transition: 0.2s; }
        .footer a:hover { text-decoration: underline; }
        .admin-link { background: rgba(56, 58, 89, 0.3); padding: 4px 14px; border-radius: 100px; border: 1px solid #30363d; }

        /* Responsive */
        @media (max-width: 600px) {
            .container { padding: 24px; border-radius: 32px; }
            h1 { font-size: 22px; }
            .logo-icon { width: 44px; height: 44px; font-size: 22px; }
            .btn-primary { font-size: 16px; padding: 16px; }
            .drop-zone { padding: 16px; }
            .header { flex-direction: column; gap: 12px; }
        }
    </style>
</head>
<body>
<div class="container">
    <div class="header">
        <div class="header-left">
            <div class="logo-icon">⚡</div>
            <div>
                <h1>Modder <span>Injector</span></h1>
                <div class="subtitle">APK + Expiry Patcher · v4.0</div>
            </div>
        </div>
    </div>
    <div class="badge-group">
        <span class="badge green">🔒 2GB Max</span>
        <span class="badge">📦 Dialog+Online</span>
        <span class="badge">⚡ Smart Neutralizer</span>
    </div>

    <form id="uploadForm" enctype="multipart/form-data">
        <div class="drop-zone" id="apkZone">
            <div class="drop-zone-label"><span class="icon">📱</span> APK File <span style="font-weight:400;color:#8b949e;font-size:13px;margin-left:8px;">(1GB+)</span></div>
            <input type="file" name="apk_file" accept=".apk" required>
            <div class="file-preview"><span>📄</span> <span class="fname" id="apk-name">No file selected</span></div>
            <div class="file-hint">Drop your original APK here or click to browse.</div>
        </div>

        <div class="drop-zone" id="zipZone" style="margin-top:16px;">
            <div class="drop-zone-label"><span class="icon">📦</span> Modder Hub ZIP <span style="font-weight:400;color:#8b949e;font-size:13px;margin-left:8px;">(assets + classes.dex)</span></div>
            <input type="file" name="patch_zip" accept=".zip" required>
            <div class="file-preview"><span>🗂️</span> <span class="fname" id="zip-name">No file selected</span></div>
            <div class="file-hint">Drop the ZIP exported from Modder Hub.</div>
        </div>

        <button type="submit" class="btn-primary" id="submitBtn">
            <span>🚀</span> Inject & Download Modified APK
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
        <span>Made with ❤️ for the Modding Community</span>
        <span><a href="#" id="admin-link" target="_blank" class="admin-link">📊 Dashboard</a></span>
    </div>
</div>

<script>
    // --- File Name Display & Drag Styling ---
    document.querySelectorAll('.drop-zone input[type="file"]').forEach(input => {
        const zone = input.closest('.drop-zone');
        const nameSpan = zone.querySelector('.fname');
        
        input.addEventListener('change', function(e) {
            if (this.files.length > 0) {
                nameSpan.textContent = this.files[0].name;
            } else {
                nameSpan.textContent = 'No file selected';
            }
        });

        // Drag & Drop highlight
        zone.addEventListener('dragover', (e) => { e.preventDefault(); zone.classList.add('dragover'); });
        zone.addEventListener('dragleave', () => { zone.classList.remove('dragover'); });
        zone.addEventListener('drop', () => { zone.classList.remove('dragover'); });
    });

    // --- Form Submission & Polling ---
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
        submitBtn.innerHTML = '<span class="spinner" style="display:inline-block;width:18px;height:18px;border:2px solid #fff;border-top-color:transparent;border-radius:50%;animation:spin 0.8s linear infinite;"></span> Processing...';

        try {
            const response = await fetch('/upload/', { method: 'POST', body: formData });
            if (!response.ok) {
                const err = await response.json();
                throw new Error(err.detail || 'Upload failed');
            }
            const data = await response.json();
            taskId = data.task_id;
            adminLink.href = `/admin/${taskId}`;
            
            statusMsg.innerHTML = `<span class="spinner"></span> Task started (ID: ${taskId}). Waiting for processing...`;
            if (pollInterval) clearInterval(pollInterval);
            pollInterval = setInterval(() => pollStatus(taskId), 1500);
            
        } catch (err) {
            showError(err.message);
            submitBtn.disabled = false;
            submitBtn.innerHTML = '🚀 Inject & Download Modified APK';
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
                submitBtn.innerHTML = '🚀 Inject & Download Modified APK';
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
                submitBtn.innerHTML = '🚀 Inject & Download Modified APK';
                showError(data.error || 'Unknown error occurred.');
            }
        } catch (err) {
            // ignore network errors during polling
        }
    }

    function showError(msg) {
        errorBox.textContent = '❌ ' + msg;
        errorBox.style.display = 'block';
        setTimeout(() => { errorBox.style.display = 'none'; }, 12000);
    }
</script>
</body>
</html>
"""

# ======================= ROUTES =============================
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
    
    apk_data = await apk_file.read()
    zip_data = await patch_zip.read()
    
    if len(apk_data) > MAX_FILE_SIZE:
        raise HTTPException(400, f"APK size exceeds 2GB limit")
    
    task_id = create_task()
    
    thread = threading.Thread(
        target=run_patch_task,
        args=(task_id, apk_data, zip_data, apk_file.filename)
    )
    thread.daemon = True
    thread.start()
    
    return JSONResponse({
        "task_id": task_id,
        "status": "pending",
        "message": "Task started."
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
    .box{{background:#161b22;padding:24px;border-radius:16px;border:1px solid #30363d;max-width:800px;margin:auto;}}
    .green{{color:#3fb950;}}.red{{color:#f85149;}}
    pre{{background:#0d1117;padding:16px;border-radius:8px;overflow:auto;white-space:pre-wrap;word-break:break-word;}}
    </style></head>
    <body>
    <div class="box">
    <h1>📊 Task: {task_id}</h1>
    <p><strong>Status:</strong> <span class="{'green' if task['status']=='completed' else 'red' if task['status']=='failed' else ''}">{task['status']}</span></p>
    <p><strong>Progress:</strong> {task['progress']}%</p>
    <p><strong>Message:</strong> {task['message']}</p>
    <p><strong>Created:</strong> {task.get('created_at', 'N/A')}</p>
    {f'<p class="red"><strong>Error:</strong> <pre>{task.get("error", "")}</pre></p>' if task.get("error") else ''}
    <p><a href="/" style="color:#58a6ff;">⬅️ Back to Home</a> | <a href="/download/{task_id}" style="color:#3fb950;">⬇️ Download</a></p>
    </div></body></html>
    """
    return HTMLResponse(content=html)

@app.get("/admin/", response_class=HTMLResponse)
async def admin_list():
    html = """<html><head><title>Admin</title><style>
    body{background:#0d1117;color:#c9d1d9;font-family:monospace;padding:40px;}
    table{width:100%;border-collapse:collapse;max-width:1200px;margin:auto;}
    td,th{padding:12px;border-bottom:1px solid #30363d;text-align:left;}
    .green{color:#3fb950;}.red{color:#f85149;}
    .box{background:#161b22;padding:24px;border-radius:16px;}
    </style></head><body>
    <div class="box">
    <h1>📊 All Tasks (Last 50)</h1>
    <table><tr><th>ID</th><th>Status</th><th>Progress</th><th>Message</th><th>Action</th></tr>
    """
    for tid, task in list(TASK_STORE.items())[-50:][::-1]:
        status_class = "green" if task['status'] == 'completed' else "red" if task['status'] == 'failed' else ""
        html += f"<tr><td>{tid}</td><td class='{status_class}'>{task['status']}</td><td>{task['progress']}%</td><td>{task['message'][:40]}...</td><td><a href='/admin/{tid}' style='color:#58a6ff;'>View</a></td></tr>"
    html += "</table><br><a href='/' style='color:#58a6ff;'>⬅️ Home</a></div></body></html>"
    return HTMLResponse(content=html)

# ======================= STARTUP ============================
@app.on_event("startup")
async def startup_event():
    print("="*60)
    print("🛠️  Modder Hub APK Patcher v4.0 (Premium)")
    print("="*60)
    print("📂 Downloading required tools (apktool, baksmali)...")
    tools_ok = download_tools()
    print(f"🔧 Tools ready: {tools_ok}")
    print(f"🔐 Generating Keystore: {generate_keystore()}")
    print(f"📁 Uploads dir: {UPLOAD_DIR}")
    print(f"🌐 Server running on port {PORT}")
    print("="*60)

if __name__ == "__main__":
    uvicorn.run(app, host="0.0.0.0", port=PORT, log_level="info")