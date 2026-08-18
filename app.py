#!/usr/bin/env python3
import json
import base64
import time
import requests
import os
import sys
import urllib3
import gzip
import random
import hashlib
import shutil
import traceback
import threading
import asyncio
import logging
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad, unpad
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import List, Dict, Optional, Tuple, Any, Set
import re
import io
import secrets

# ---------- FLASK ----------
from flask import Flask, request, jsonify

# ---------- QR CODE ----------
try:
    import qrcode
    from PIL import Image
    QR_AVAILABLE = True
except ImportError:
    QR_AVAILABLE = False
    print("⚠️ qrcode or Pillow not installed. Install: pip install qrcode Pillow")

# ---------- TELEGRAM ----------
try:
    from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardButton, InlineKeyboardMarkup
    from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, CallbackQueryHandler
    TELEGRAM_AVAILABLE = True
except ImportError:
    TELEGRAM_AVAILABLE = False
    print("⚠️ python-telegram-bot not installed. Install: pip install python-telegram-bot")

# =========================================================
#  ⚙️ ON/OFF SWITCH
# =========================================================
TELEGRAM_BOT_MODE = True

# =========================================================
#  📌 CONFIG
# =========================================================
BOT_TOKEN = "8958507490:AAG-VeSJq-Rbb1NAxQ1VA4x3HX-X066jQwY"
OWNER_ID = 5674825926
SUPPORT_USERNAME = "@Card_hacker_12"
DATA_DIR = "data"
os.makedirs(DATA_DIR, exist_ok=True)

STORAGE_FILE = os.path.join(DATA_DIR, "accounts_storage.json")
BOT_DATA_FILE = os.path.join(DATA_DIR, "bot_data.json")
USED_ACCOUNTS_FILE = os.path.join(DATA_DIR, "used_accounts.json")
UPLOAD_FOLDER = os.path.join(DATA_DIR, "riyaj_account")
LOG_FILE = os.path.join(DATA_DIR, "bot.log")

UPI_ID = "ratan143@fam"
COST_PER_SUCCESS = 0.5
RESELLER_COST = 0.1
RESELLER_FEE = 100

ADD_FUND_AMOUNTS = [20, 50, 80, 100, 150, 200]

os.makedirs(UPLOAD_FOLDER, exist_ok=True)
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# =========================================================
#  🔐 GLOBAL VARIABLE FOR URL
# =========================================================
MAIN_BOT_URL = None

# =========================================================
#  🔐 FLASK APP
# =========================================================
app = Flask(__name__)

# =========================================================
#  🔐 ORIGINAL Follow.py CODE
# =========================================================
KEY = b"Yg&tc%DEuh6%Zc^8"
IV = b"6oyZDr22E3ychjM%"
CLIENT_SECRET = "2ee44819e9b4598845141067b281621874d0d5d7af9d8f7e00c1e54715b7d1e3"

class Color:
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    PURPLE = '\033[95m'
    CYAN = '\033[96m'
    WHITE = '\033[97m'
    BOLD = '\033[1m'
    RESET = '\033[0m'
    ORANGE = '\033[38;5;208m'

DEBUG_MODE = False

def print_centered(text, color=Color.WHITE):
    try:
        cols = shutil.get_terminal_size().columns
        padding = max(0, (cols - len(text)) // 2)
        print(' ' * padding + color + text + Color.RESET)
    except:
        print(color + text + Color.RESET)

def clear_screen():
    os.system('cls' if os.name == 'nt' else 'clear')

def display_banner():
    clear_screen()
    banner = """
                  ▄▄▄▄▄▄     ▄▄▄▄▄▄     ▄▄▄▄             
                  ██▀▀▀▀██   ▀▀██▀▀    ██▀▀██            
                  ██    ██     ██     ██    ██           
                  ███████      ██     ██    ██           
                  ██  ▀██▄     ██     ██    ██           
                  ██    ██   ▄▄██▄▄    ██▄▄██            
                  ▀▀    ▀▀▀  ▀▀▀▀▀▀     ▀▀▀▀             
                               © SPIDEERIO GAMING         
"""
    print(Color.ORANGE, end="")
    print_centered(banner, Color.ORANGE)
    print(Color.RESET, end="")
    print_centered("=== Craftland Follower By @Spideerio & Flexbasei ===", Color.CYAN)
    print()
    print_centered("Dont Forget to Subscribe & Join ", Color.YELLOW)
    print_centered("Telegram : @Flexbasei & @Spideerio_YT 💝", Color.PURPLE)
    print("\n" + "-" * shutil.get_terminal_size().columns + "\n")

def debug_print(*args, **kwargs):
    if DEBUG_MODE:
        print(f"{Color.YELLOW}[DEBUG]{Color.RESET}", *args, **kwargs)

def format_token(token):
    token = token.strip()
    if not token.startswith("Bearer "):
        return f"Bearer {token}"
    return token

def sha256_hash(password):
    return hashlib.sha256(password.encode('utf-8')).hexdigest().upper()

def EnC_Vr(N):
    if N < 0: return b''
    H = []
    while True:
        BesTo = N & 0x7F
        N >>= 7
        if N:
            BesTo |= 0x80
        H.append(BesTo)
        if not N:
            break
    return bytes(H)

def CrEaTe_LenGTh(field_number, value):
    field_header = (field_number << 3) | 2
    encoded_value = value.encode() if isinstance(value, str) else value
    return EnC_Vr(field_header) + EnC_Vr(len(encoded_value)) + encoded_value

def CrEaTe_VarianT(field_number, value):
    field_header = (field_number << 3) | 0
    return EnC_Vr(field_header) + EnC_Vr(value)

def CrEaTe_ProTo(fields):
    packet = bytearray()
    for field, value in fields.items():
        if isinstance(value, dict):
            nested_packet = CrEaTe_ProTo(value)
            packet.extend(CrEaTe_LenGTh(field, nested_packet))
        elif isinstance(value, int):
            packet.extend(CrEaTe_VarianT(field, value))
        elif isinstance(value, str) or isinstance(value, bytes):
            packet.extend(CrEaTe_LenGTh(field, value))
    return packet

def E_AEs(Pc):
    Z = bytes.fromhex(Pc)
    key = bytes([89, 103, 38, 116, 99, 37, 68, 69, 117, 104, 54, 37, 90, 99, 94, 56])
    iv = bytes([54, 111, 121, 90, 68, 114, 50, 50, 69, 51, 121, 99, 104, 106, 77, 37])
    K = AES.new(key, AES.MODE_CBC, iv)
    return K.encrypt(pad(Z, AES.block_size))

def get_real_device():
    devices = [
        {"model": "SM-S918B", "brand": "samsung", "manufacturer": "samsung", "android": "14", "api": "34"},
        {"model": "Pixel 7 Pro", "brand": "google", "manufacturer": "google", "android": "14", "api": "34"},
        {"model": "OnePlus 11", "brand": "OnePlus", "manufacturer": "OnePlus", "android": "14", "api": "34"},
        {"model": "Xiaomi 13", "brand": "Xiaomi", "manufacturer": "Xiaomi", "android": "14", "api": "34"}
    ]
    return random.choice(devices)

def get_real_ip():
    return f"{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}.{random.randint(1,255)}"

def get_real_android_id():
    return ''.join(random.choices('abcdef0123456789', k=32))

def get_region_carrier(region):
    carriers = {"IND": ["Jio", "Airtel", "Vodafone Idea"], "US": ["Verizon", "AT&T", "T-Mobile"]}
    return random.choice(carriers.get(region, ["Airtel"]))

REAL_GPUS = ["Adreno 730", "Adreno 740", "Adreno 660"]
REAL_GPU_VERSIONS = ["OpenGL ES 3.2", "OpenGL ES 3.1"]
REAL_NETWORKS = ["WIFI", "5G", "4G"]

def build_major_login_payload(access_token, open_id, region="IND"):
    now = time.strftime('%Y-%m-%d %H:%M:%S')
    device = get_real_device()
    android_id = get_real_android_id()
    ip = get_real_ip()
    carrier = get_region_carrier(region)
    network = random.choice(REAL_NETWORKS)
    gpu = random.choice(REAL_GPUS)
    gpu_version = random.choice(REAL_GPU_VERSIONS)

    payload = {
        3: now,
        4: "free fire",
        5: 4,
        7: "1.126.5B9",
        8: f"Android OS {device['android']} / API-{device['api']}",
        9: device['model'],
        10: carrier,
        11: network,
        12: random.choice([1080, 1440, 2400]),
        13: random.choice([1920, 2400, 3200]),
        14: str(random.randint(200, 500)),
        15: f"ARM64 FP ASIMD AES | {random.randint(2000, 4000)} | {random.randint(2, 8)}",
        16: random.randint(4000, 12000),
        17: gpu,
        18: gpu_version,
        19: f"Google|{android_id}",
        20: ip,
        21: "en",
        22: open_id,
        23: "4",
        24: "Handheld",
        29: access_token,
        30: 1,
        41: carrier,
        42: network,
        57: "",
        73: 1,
        74: f"/data/app/com.dts.freefireth-{''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=22))}==/lib/arm64",
        76: 0,
        77: f"{android_id}|/data/app/com.dts.freefireth-{''.join(random.choices('abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789', k=22))}==/base.apk",
        78: 2,
        79: 1,
        81: "ARM64",
        83: "2019120776",
        86: "OpenGLES3",
        87: random.choice([255, 511, 1023]),
        88: 4,
        89: b'{"cur_rate":null,"support_etc2":true}',
        92: 0,
        93: "android",
        94: "",
        95: 1,
        97: 0,
        98: 0,
        99: "4",
        100: "4"
    }
    payload_bytes = CrEaTe_ProTo(payload)
    encrypted = E_AEs(payload_bytes.hex())
    return encrypted

def decode_jwt_token(jwt_token):
    try:
        parts = jwt_token.split('.')
        if len(parts) >= 2:
            payload_part = parts[1]
            padding = 4 - len(payload_part) % 4
            if padding != 4:
                payload_part += '=' * padding
            decoded = base64.urlsafe_b64decode(payload_part)
            data = json.loads(decoded)
            return data.get('account_id') or data.get('external_id')
    except:
        pass
    return "N/A"

def generate_jwt_from_uid_pass(uid, password):
    try:
        url = "https://ffmconnect.live.gop.garenanow.com/api/v2/oauth/guest/token:grant"
        payload = {
            "client_id": 100067,
            "client_secret": CLIENT_SECRET,
            "client_type": 2,
            "password": password,
            "response_type": "token",
            "uid": int(uid)
        }
        headers = {
            "User-Agent": "GarenaMSDK/4.0.42(SM-S918B ;Android 14;en;US;app 1.126.6 2019120776;)",
            "Accept": "application/json",
            "Content-Type": "application/json; charset=utf-8",
            "Accept-Encoding": "identity",
            "Connection": "keep-alive"
        }
        response = requests.post(url, json=payload, headers=headers, verify=False, timeout=10)
        if response.status_code != 200:
            return None
        data = response.json()
        if data.get("code") != 0:
            return None
        token_data = data.get("data", {})
        access_token = token_data.get("access_token")
        open_id = token_data.get("open_id")
        if not access_token or not open_id:
            return None

        url = "https://loginbp.ggpolarbear.com/MajorLogin"
        payload_data = build_major_login_payload(access_token, open_id)
        headers = {
            "Accept-Encoding": "gzip",
            "Authorization": "Bearer",
            "Content-Type": "application/x-www-form-urlencoded",
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_I005DA Build/PI)",
            "Accept": "*/*",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB54",
            "X-Unity-Version": "2018.4.11f1",
            "Connection": "keep-alive",
            "Host": "loginbp.ggpolarbear.com"
        }
        response = requests.post(url, headers=headers, data=payload_data, verify=False, timeout=10)
        if response.status_code == 200:
            response_data = response.text
            jwt_start = response_data.find("eyJ")
            if jwt_start != -1:
                jwt_token = response_data[jwt_start:]
                second_dot = jwt_token.find(".", jwt_token.find(".") + 1)
                if second_dot != -1:
                    jwt_token = jwt_token[:second_dot + 44]
                    if len(jwt_token.split('.')) == 3:
                        return jwt_token
        return None
    except:
        return None

class FreeFireWorkshop:
    def __init__(self, token):
        self.token = format_token(token)
        self.base_url = "https://client.ind.freefiremobile.com"
        self.session = requests.Session()
        self.session.verify = False
        self.session.headers.update({
            "User-Agent": "UnityPlayer/2022.3.47f1 (UnityWebRequest/1.0, libcurl/8.5.0-DEV)",
            "Accept": "*/*",
            "Accept-Encoding": "deflate, gzip",
            "Authorization": self.token,
            "X-GA": "v1 1",
            "ReleaseVersion": "OB54",
            "Content-Type": "application/x-www-form-urlencoded",
            "X-Unity-Version": "2022.3.47f1"
        })
        self.account_id = self._extract_account_id(token)

    def _extract_account_id(self, token):
        try:
            clean = token.strip()
            if clean.startswith("Bearer "):
                clean = clean[7:]
            parts = clean.split('.')
            if len(parts) >= 2:
                payload = parts[1]
                payload += '=' * (4 - len(payload) % 4)
                decoded = base64.b64decode(payload)
                data = json.loads(decoded)
                return data.get('account_id', 'Unknown')
        except:
            return 'Unknown'
        return 'Unknown'

    def _aes_encrypt(self, data):
        cipher = AES.new(KEY, AES.MODE_CBC, IV)
        return cipher.encrypt(pad(data, AES.block_size))

    def _aes_decrypt(self, data):
        try:
            cipher = AES.new(KEY, AES.MODE_CBC, IV)
            return unpad(cipher.decrypt(data), AES.block_size)
        except:
            return None

    def _make_varint(self, value):
        result = []
        while value > 0x7f:
            result.append((value & 0x7f) | 0x80)
            value >>= 7
        result.append(value & 0x7f)
        return bytes(result)

    def _make_varint_field(self, field_num, value):
        key = (field_num << 3) | 0
        return self._make_varint(key) + self._make_varint(value)

    def _make_delimited_field(self, field_num, value):
        key = (field_num << 3) | 2
        length = len(value)
        return self._make_varint(key) + self._make_varint(length) + value

    def _build_protobuf(self, fields):
        result = b""
        for field_num, value in fields.items():
            if isinstance(value, str):
                encoded = value.encode('utf-8')
                result += self._make_delimited_field(field_num, encoded)
            elif isinstance(value, int):
                result += self._make_varint_field(field_num, value)
            elif isinstance(value, bytes):
                result += self._make_delimited_field(field_num, value)
            elif isinstance(value, list):
                for item in value:
                    if isinstance(item, int):
                        result += self._make_varint_field(field_num, item)
                    elif isinstance(item, str):
                        encoded = item.encode('utf-8')
                        result += self._make_delimited_field(field_num, encoded)
        return result

    def _process_response(self, response):
        if response.status_code != 200 or len(response.content) == 0:
            return None
        data = response.content
        if data[:2] == b'\x1f\x8b':
            try:
                decompressed = gzip.decompress(data)
                decrypted = self._aes_decrypt(decompressed)
                return decrypted if decrypted else decompressed
            except:
                return data
        else:
            decrypted = self._aes_decrypt(data)
            return decrypted if decrypted else data

    def _send_request(self, endpoint, request_data):
        encrypted = self._aes_encrypt(request_data)
        return self.session.post(f"{self.base_url}/{endpoint}", data=encrypted, verify=False)

    def parse_response_deep(self, data):
        if not data or isinstance(data, str):
            return data
        result = {}
        i = 0
        while i < len(data):
            try:
                key, length = self._read_varint(data, i)
                i += length
                field_num = key >> 3
                wire_type = key & 0x7
                if wire_type == 0:
                    value, length = self._read_varint(data, i)
                    i += length
                    result[field_num] = value
                elif wire_type == 2:
                    length, l_len = self._read_varint(data, i)
                    i += l_len
                    value = data[i:i+length]
                    i += length
                    try:
                        nested = self.parse_response_deep(value)
                        if nested:
                            result[field_num] = nested
                        else:
                            result[field_num] = value.decode('utf-8', errors='ignore')
                    except:
                        result[field_num] = value.decode('utf-8', errors='ignore')
                else:
                    break
            except:
                break
        return result

    def _read_varint(self, data, pos):
        result = 0
        shift = 0
        length = 0
        while pos + length < len(data):
            byte = data[pos + length]
            length += 1
            result |= (byte & 0x7f) << shift
            if not (byte & 0x80):
                break
            shift += 7
        return result, length

    def get_account_info(self, target_id):
        request_data = self._build_protobuf({1: target_id, 2: 11})
        return self._send_request("GetAccountInfoByAccountID", request_data)

    def get_workshop_author_info(self, author_id, language="en", access_token=""):
        request_data = self._build_protobuf({1: author_id, 2: language, 3: access_token})
        return self._send_request("GetWorkshopAuthorInfo", request_data)

    def follow_user(self, target_id):
        request_data = self._build_protobuf({1: target_id})
        return self._send_request("Follow", request_data)

    def get_follower_count(self, target_id):
        response = self.get_workshop_author_info(target_id)
        if response is None or response.status_code != 200:
            return None
        processed = self._process_response(response)
        if not processed:
            return None
        parsed = self.parse_response_deep(processed)
        followers = 0
        if 7 in parsed and isinstance(parsed[7], dict) and 2 in parsed[7]:
            followers = parsed[7][2]
        return followers

    def get_account_details(self, target_id):
        debug_print(f"Fetching account details for target_id: {target_id}")
        response = self.get_account_info(target_id)
        if response is None or response.status_code != 200:
            debug_print(f"Response error: {response.status_code if response else 'None'}")
            return None
        processed = self._process_response(response)
        if not processed:
            debug_print("Failed to process response")
            return None
        parsed = self.parse_response_deep(processed)
        debug_print(f"Parsed response structure: {json.dumps(parsed, indent=2, default=str)[:500]}...")
        username = 'Unknown'
        if 3 in parsed:
            field3 = parsed[3]
            if isinstance(field3, bytes):
                try:
                    username = field3.decode('utf-8')
                except:
                    username = str(field3)
            elif isinstance(field3, dict):
                if field3:
                    first_key = list(field3.keys())[0]
                    username = field3[first_key]
                    if isinstance(username, bytes):
                        try:
                            username = username.decode('utf-8')
                        except:
                            username = str(username)
                    elif isinstance(username, dict):
                        if 6 in username:
                            username = username[6]
                            if isinstance(username, bytes):
                                try:
                                    username = username.decode('utf-8')
                                except:
                                    username = str(username)
                        else:
                            username = list(username.values())[0] if username else 'Unknown'
            elif isinstance(field3, str):
                username = field3
        if username != 'Unknown' and len(username) > 10 and '=' not in username:
            try:
                decoded = base64.b64decode(username).decode('utf-8', errors='ignore')
                if decoded:
                    username = decoded
            except:
                pass
        level = parsed.get(6, 0)
        region = 'Unknown'
        if 5 in parsed:
            region = parsed[5]
            if isinstance(region, bytes):
                try:
                    region = region.decode('utf-8')
                except:
                    region = str(region)
            elif not isinstance(region, str):
                region = str(region)
        guild = ''
        if 13 in parsed:
            guild = parsed[13]
            if isinstance(guild, bytes):
                try:
                    guild = guild.decode('utf-8')
                except:
                    guild = str(guild)
            elif not isinstance(guild, str):
                guild = str(guild)
        return {'username': username, 'level': level, 'region': region, 'guild': guild}

# =========================================================
#  📦 BotData CLASS
# =========================================================
class BotData:
    def __init__(self, data_file=BOT_DATA_FILE):
        self.data_file = data_file
        self.data = self.load()
        
    def load(self):
        if os.path.exists(self.data_file):
            try:
                with open(self.data_file, 'r', encoding='utf-8') as f:
                    return json.load(f)
            except:
                pass
        return self.get_default_data()
    
    def get_default_data(self):
        return {
            "owner_id": OWNER_ID,
            "admins": {},
            "users": {},
            "cost_per_success": COST_PER_SUCCESS,
            "reseller_cost": RESELLER_COST,
            "pending_payments": [],
            "total_users": 0,
            "refer_reward": 0
        }
    
    def save(self):
        try:
            with open(self.data_file, 'w', encoding='utf-8') as f:
                json.dump(self.data, f, indent=2)
            return True
        except Exception as e:
            print(f"Error saving bot data: {e}")
            return False
    
    def get_user(self, user_id):
        user_id = str(user_id)
        if user_id not in self.data["users"]:
            self.data["users"][user_id] = {
                "balance": 0,
                "username": "Unknown",
                "referred_by": None,
                "referrals": [],
                "reseller": False,
                "api_key": None
            }
            self.save()
        return self.data["users"][user_id]
    
    def get_balance(self, user_id):
        if self.is_owner(user_id):
            return "♾️ Unlimited"
        return self.get_user(user_id).get("balance", 0)
    
    def get_balance_raw(self, user_id):
        if self.is_owner(user_id):
            return 999999999
        return self.get_user(user_id).get("balance", 0)
    
    def add_balance(self, user_id, amount):
        if self.is_owner(user_id):
            return "♾️ Unlimited"
        user = self.get_user(user_id)
        user["balance"] = user.get("balance", 0) + amount
        self.save()
        return user["balance"]
    
    def deduct_balance(self, user_id, amount):
        if self.is_owner(user_id):
            return True
        user = self.get_user(user_id)
        if user.get("balance", 0) >= amount:
            user["balance"] = user.get("balance", 0) - amount
            self.save()
            return True
        return False
    
    def get_cost(self, user_id):
        user = self.get_user(user_id)
        if user.get("reseller", False):
            return self.data.get("reseller_cost", RESELLER_COST)
        else:
            return self.data.get("cost_per_success", COST_PER_SUCCESS)
    
    def set_cost(self, cost):
        self.data["cost_per_success"] = max(0, cost)
        self.save()
        return self.data["cost_per_success"]
    
    def get_refer_reward(self):
        return 2 * self.get_cost(0)
    
    def add_referral(self, user_id, referrer_id):
        user_id = str(user_id)
        referrer_id = str(referrer_id)
        
        user = self.get_user(user_id)
        if user.get("referred_by"):
            return False, "❌ Already referred!"
        
        if user_id == referrer_id:
            return False, "❌ Cannot refer yourself!"
        
        user["referred_by"] = referrer_id
        self.save()
        
        reward = self.get_refer_reward()
        self.add_balance(referrer_id, reward)
        
        referrer = self.get_user(referrer_id)
        if "referrals" not in referrer:
            referrer["referrals"] = []
        if user_id not in referrer["referrals"]:
            referrer["referrals"].append(user_id)
            self.save()
        
        return True, f"✅ +{reward} coins added (2 followers worth!)"
    
    def get_referral_info(self, user_id):
        user = self.get_user(user_id)
        return {
            "referrals": user.get("referrals", []),
            "referred_by": user.get("referred_by"),
            "reward": self.get_refer_reward()
        }
    
    def is_admin(self, user_id):
        return str(user_id) in self.data["admins"] or self.is_owner(user_id)
    
    def is_owner(self, user_id):
        return user_id == self.data["owner_id"]
    
    def get_admin_info(self, user_id):
        user_id = str(user_id)
        if user_id in self.data["admins"]:
            return self.data["admins"][user_id]
        return None
    
    def add_admin(self, user_id, limit=100):
        user_id = str(user_id)
        if user_id not in self.data["admins"] and not self.is_owner(int(user_id)):
            self.data["admins"][user_id] = {"limit": limit, "used": 0}
            self.save()
            return True
        return False
    
    def remove_admin(self, user_id):
        user_id = str(user_id)
        if user_id in self.data["admins"]:
            del self.data["admins"][user_id]
            self.save()
            return True
        return False
    
    def set_admin_limit(self, user_id, limit):
        user_id = str(user_id)
        if user_id in self.data["admins"]:
            self.data["admins"][user_id]["limit"] = limit
            self.save()
            return True
        return False
    
    def use_admin_limit(self, user_id, count=1):
        user_id = str(user_id)
        if user_id in self.data["admins"]:
            admin = self.data["admins"][user_id]
            if admin["used"] + count <= admin["limit"]:
                admin["used"] += count
                self.save()
                return True
        return False
    
    def get_admin_remaining(self, user_id):
        user_id = str(user_id)
        if user_id in self.data["admins"]:
            admin = self.data["admins"][user_id]
            return admin["limit"] - admin["used"]
        return 0
    
    def add_payment(self, user_id, amount, transaction_id, purpose="add_fund"):
        payment = {
            "user_id": str(user_id),
            "amount": amount,
            "transaction_id": transaction_id,
            "timestamp": datetime.now().isoformat(),
            "status": "pending",
            "purpose": purpose
        }
        self.data["pending_payments"].append(payment)
        self.save()
        return payment
    
    def get_pending_payments(self):
        return [p for p in self.data["pending_payments"] if p.get("status") == "pending"]
    
    def approve_payment(self, transaction_id):
        for payment in self.data["pending_payments"]:
            if payment["transaction_id"] == transaction_id and payment.get("status") == "pending":
                payment["status"] = "approved"
                if payment.get("purpose") == "reseller":
                    user_id = payment["user_id"]
                    self.make_reseller(user_id)
                else:
                    self.add_balance(payment["user_id"], payment["amount"])
                self.save()
                return payment
        return None
    
    def reject_payment(self, transaction_id):
        for payment in self.data["pending_payments"]:
            if payment["transaction_id"] == transaction_id and payment.get("status") == "pending":
                payment["status"] = "rejected"
                self.save()
                return payment
        return None
    
    def make_reseller(self, user_id):
        user = self.get_user(user_id)
        if not user.get("reseller", False):
            user["reseller"] = True
            if not user.get("api_key"):
                user["api_key"] = secrets.token_hex(16)
            self.save()
            return True
        return False
    
    def get_api_key(self, user_id):
        user = self.get_user(user_id)
        if user.get("reseller", False) or self.is_owner(user_id) or self.is_admin(user_id):
            if not user.get("api_key"):
                user["api_key"] = secrets.token_hex(16)
                self.save()
            return user["api_key"]
        return None
    
    def generate_api_key(self, user_id):
        user = self.get_user(user_id)
        if self.is_owner(user_id) or self.is_admin(user_id) or user.get("reseller", False):
            if user.get("api_key"):
                return user["api_key"]
            api_key = secrets.token_hex(16)
            user["api_key"] = api_key
            self.save()
            return api_key
        return None
    
    def reset_api_key(self, user_id):
        user = self.get_user(user_id)
        if self.is_owner(user_id) or self.is_admin(user_id) or user.get("reseller", False):
            api_key = secrets.token_hex(16)
            user["api_key"] = api_key
            self.save()
            return api_key
        return None
    
    def is_reseller(self, user_id):
        return self.get_user(user_id).get("reseller", False)
    
    def get_total_accounts(self):
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    return len(data.get("accounts", []))
            except:
                pass
        return 0

# =========================================================
#  📦 QR CODE GENERATOR
# =========================================================
def generate_qr_png(upi_id: str, amount: int, order_id: str) -> bytes:
    upi_link = f"upi://pay?pa={upi_id}&pn=Craftland&am={amount}&cu=INR&tn={order_id}"
    
    qr = qrcode.QRCode(
        version=1,
        box_size=8,
        border=2
    )
    qr.add_data(upi_link)
    qr.make(fit=True)
    
    img = qr.make_image(fill_color="black", back_color="white")
    
    img_bytes = io.BytesIO()
    img.save(img_bytes, format='PNG')
    img_bytes.seek(0)
    return img_bytes.getvalue()

# =========================================================
#  📦 FollowerBot CLASS
# =========================================================
class FollowerBot:
    def __init__(self, tokens_file=None, telegram_mode=False, chat_id=None, bot_instance=None, loop=None, user_id=None, bot_data=None):
        self.tokens_file = tokens_file
        self.tokens = []
        self.results = {'success': 0, 'failed': 0, 'already': 0, 'insufficient': 0, 'errors': []}
        self.target_id = None
        self.initial_followers = 0
        self.current_followers = 0
        self.processed = 0
        self.total = 0
        self.start_time = None
        self.uid_pass_list = []
        self.target_username = None
        self.target_level = None
        self.target_region = None
        self.telegram_mode = telegram_mode
        self.chat_id = chat_id
        self.bot = bot_instance
        self.loop = loop if loop else asyncio.get_event_loop() if telegram_mode else None
        self.last_progress_msg = ""
        self.user_id = user_id
        self.bot_data = bot_data
        self.success_count = 0

    def _send_telegram_message(self, text):
        if self.telegram_mode and self.bot and self.chat_id and self.loop:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self.bot.send_message(chat_id=self.chat_id, text=text),
                    self.loop
                )
                future.result(timeout=5)
            except Exception as e:
                print(f"Telegram send error: {e}")

    def extract_tokens(self, data):
        tokens = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    if 'jwt_token' in item:
                        tokens.append(item['jwt_token'])
                    elif 'token' in item:
                        tokens.append(item['token'])
                elif isinstance(item, str):
                    tokens.append(item)
        elif isinstance(data, dict):
            if 'tokens' in data:
                return self.extract_tokens(data['tokens'])
            elif 'jwt_tokens' in data:
                return self.extract_tokens(data['jwt_tokens'])
            else:
                for value in data.values():
                    if isinstance(value, list):
                        tokens.extend(self.extract_tokens(value))
        return tokens

    def parse_uid_pass_line(self, line):
        line = line.strip()
        if not line:
            return None
        if ':' in line and '|' in line:
            parts = line.split('|')
            uid_pass = parts[0]
            if ':' in uid_pass:
                uid, password = uid_pass.split(':', 1)
                return {'uid': uid.strip(), 'password': password.strip()}
        elif ':' in line:
            uid, password = line.split(':', 1)
            return {'uid': uid.strip(), 'password': password.strip()}
        elif '|' in line:
            uid, password = line.split('|', 1)
            return {'uid': uid.strip(), 'password': password.strip()}
        return None

    def load_uid_pass_from_file(self, file_path):
        if not os.path.exists(file_path):
            print(f"{Color.RED}❌ File not found: {file_path}{Color.RESET}")
            return []
        accounts = []
        existing_set = set()
        if os.path.exists(STORAGE_FILE):
            try:
                with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                    data = json.load(f)
                    for acc in data.get("accounts", []):
                        uid = str(acc.get('uid', '')).strip()
                        pwd = str(acc.get('password', '')).strip()
                        if uid and pwd:
                            existing_set.add((uid, pwd))
            except:
                pass
        try:
            with open(file_path, 'r') as f:
                data = f.read()
            new_accounts = []
            if data.strip().startswith('[') or data.strip().startswith('{'):
                try:
                    json_data = json.loads(data)
                    if isinstance(json_data, list):
                        for item in json_data:
                            if isinstance(item, dict):
                                uid = str(item.get('uid', '')).strip()
                                password = str(item.get('password', '')).strip()
                                if uid and password and (uid, password) not in existing_set:
                                    new_accounts.append({'uid': uid, 'password': password})
                    elif isinstance(json_data, dict):
                        uid = str(json_data.get('uid', '')).strip()
                        password = str(json_data.get('password', '')).strip()
                        if uid and password and (uid, password) not in existing_set:
                            new_accounts.append({'uid': uid, 'password': password})
                    return new_accounts
                except:
                    pass
            lines = data.split('\n')
            for line in lines:
                parsed = self.parse_uid_pass_line(line)
                if parsed:
                    uid = parsed['uid'].strip()
                    password = parsed['password'].strip()
                    if uid and password and (uid, password) not in existing_set:
                        new_accounts.append({'uid': uid, 'password': password})
            return new_accounts
        except Exception as e:
            print(f"{Color.RED}Error loading UID/PASS file: {e}{Color.RESET}")
            return []

    def load_tokens_from_file(self, file_path):
        if not os.path.exists(file_path):
            print(f"{Color.RED}❌ File not found: {file_path}{Color.RESET}")
            return []
        tokens = []
        try:
            with open(file_path, 'r') as f:
                data = json.load(f)
            tokens = self.extract_tokens(data)
            if not tokens:
                return []
            formatted_tokens = []
            for token in tokens:
                if token and len(token) > 50 and '.' in token:
                    formatted_tokens.append(format_token(token))
            return formatted_tokens
        except:
            return []

    def load_tokens_or_accounts(self):
        if self.tokens_file:
            tokens = self.load_tokens_from_file(self.tokens_file)
            if tokens:
                self.tokens = tokens
                return len(tokens)
        accounts = self.load_uid_pass_from_file(self.tokens_file)
        if accounts:
            self.uid_pass_list = accounts
            return len(accounts)
        return 0

    def process_account_with_token(self, token, target_id, current_followers):
        try:
            client = FreeFireWorkshop(token)
            response = client.follow_user(target_id)
            if response is None:
                return {'status': 'failed', 'account_id': client.account_id, 'message': 'Network error', 'followers': current_followers}
            if response.status_code == 401:
                return {'status': 'failed', 'account_id': client.account_id, 'message': 'Invalid token', 'followers': current_followers}
            processed = client._process_response(response)
            status = None
            if processed:
                parsed = client.parse_response_deep(processed)
                if 5 in parsed and isinstance(parsed[5], dict) and 8 in parsed[5]:
                    status = parsed[5][8]
            if status == "INSUFFICIENT" or "WORKSHOP_INSUFFICIENT" in str(status):
                return {'status': 'insufficient', 'account_id': client.account_id, 'message': 'Need matches', 'followers': current_followers}
            elif status and "ALREADY" in status.upper():
                return {'status': 'already', 'account_id': client.account_id, 'message': 'Already', 'followers': current_followers}
            if response.status_code == 200:
                new_followers = client.get_follower_count(target_id)
                if new_followers is not None:
                    return {'status': 'success', 'account_id': client.account_id, 'message': 'Followed', 'followers': new_followers}
                return {'status': 'success', 'account_id': client.account_id, 'message': 'Followed', 'followers': current_followers}
            return {'status': 'failed', 'account_id': client.account_id, 'message': f'HTTP {response.status_code}', 'followers': current_followers}
        except Exception as e:
            return {'status': 'failed', 'account_id': client.account_id if 'client' in locals() else 'Unknown', 'message': str(e)[:50], 'followers': current_followers}

    def process_uid_pass_account(self, account, target_id, current_followers):
        try:
            uid = account['uid']
            password = account['password']
            if not self.telegram_mode:
                sys.stdout.write(f'\r{Color.YELLOW}🔄 Generating JWT for UID: {uid}{Color.RESET}')
                sys.stdout.flush()
            jwt_token = generate_jwt_from_uid_pass(uid, password)
            if not jwt_token:
                if not self.telegram_mode:
                    sys.stdout.write('\r\033[K')
                return {'status': 'failed', 'account_id': uid, 'message': 'JWT generation failed', 'followers': current_followers}
            if not self.telegram_mode:
                sys.stdout.write('\r\033[K')
            return self.process_account_with_token(jwt_token, target_id, current_followers)
        except Exception as e:
            if not self.telegram_mode:
                sys.stdout.write('\r\033[K')
            return {'status': 'failed', 'account_id': account.get('uid', 'Unknown'), 'message': str(e)[:50], 'followers': current_followers}

    def print_progress(self):
        if self.telegram_mode:
            return
        elapsed = time.time() - self.start_time if self.start_time else 0
        progress = (self.processed / self.total * 100) if self.total > 0 else 0
        bar_length = 30
        filled = int(bar_length * progress / 100)
        bar = '█' * filled + '░' * (bar_length - filled)
        sys.stdout.write('\r\033[K')
        status = f"[{bar}] {progress:3.0f}% | {self.processed:2}/{self.total} | "
        status += f"✅{self.results['success']} ⚠️{self.results['already']} 🔒{self.results['insufficient']} ❌{self.results['failed']} | 👥{self.current_followers}"
        if elapsed > 0:
            rate = self.processed / elapsed if elapsed > 0 else 0
            eta = (self.total - self.processed) / rate if rate > 0 else 0
            status += f" | ⏱{int(elapsed//60)}m{int(elapsed%60)}s"
            if eta > 0:
                status += f" | ETA:{int(eta//60)}m{int(eta%60)}s"
        sys.stdout.write(status)
        sys.stdout.flush()

    def print_summary(self):
        increase = self.current_followers - self.initial_followers
        if self.telegram_mode:
            success_count = self.results.get('success', 0)
            if self.user_id and self.bot_data:
                cost_per_success = self.bot_data.get_cost(self.user_id)
            else:
                cost_per_success = COST_PER_SUCCESS
            total_cost = success_count * cost_per_success
            
            summary = (
                f"✨ Premium Summary ✨\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 Target : {self.target_id}\n"
                f"🆔 Username : {self.target_username or 'Unknown'}\n"
                f"📊 Level : {self.target_level or 0}\n"
                f"🌍 Region : {self.target_region or 'Unknown'}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👥 Before : {self.initial_followers}\n"
                f"👥 After : {self.current_followers}\n"
                f"📈 Increase : +{increase}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"✅ Success : {self.results['success']}\n"
                f"⚠️ Already : {self.results['already']}\n"
                f"🔒 Insufficient : {self.results['insufficient']}\n"
                f"❌ Failed : {self.results['failed']}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Cost : ₹{total_cost:.2f}\n"
                f"   ({success_count} × ₹{cost_per_success:.2f})\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👑 FINAL = {self.current_followers}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"⏱️ Time : {time.time()-self.start_time:.2f}s\n"
                f"🚀 Speed : {self.total/(time.time()-self.start_time):.2f}/s"
            )
            self._send_telegram_message(summary)
            
            if self.user_id and self.bot_data:
                cost = cost_per_success
                success_count = self.results.get('success', 0)
                if cost > 0 and success_count > 0 and not self.bot_data.is_admin(self.user_id):
                    total_cost = cost * success_count
                    if self.bot_data.deduct_balance(self.user_id, total_cost):
                        balance = self.bot_data.get_balance(self.user_id)
                        self._send_telegram_message(f"💸 ₹{total_cost:.2f} deducted | Balance : {balance}")
                    else:
                        self._send_telegram_message(f"⚠️ Insufficient balance")
                elif self.bot_data.is_admin(self.user_id):
                    self._send_telegram_message("🔄 Unlimited balance (Admin)")
        else:
            color = Color.CYAN
            print("\n\n")
            print(f"{color}{'═'*70}{Color.RESET}")
            print(f"{Color.YELLOW}{Color.BOLD}  📊 FINAL SUMMARY{Color.RESET}")
            print(f"{color}{'═'*70}{Color.RESET}")
            print(f"  {Color.WHITE}Target ID:{Color.RESET} {Color.CYAN}{self.target_id}{Color.RESET}")
            if self.target_username:
                print(f"  {Color.WHITE}Username:{Color.RESET} {Color.GREEN}{self.target_username}{Color.RESET}")
            if self.target_level is not None:
                print(f"  {Color.WHITE}Level:{Color.RESET} {Color.BLUE}{self.target_level}{Color.RESET}")
            if self.target_region:
                print(f"  {Color.WHITE}Region:{Color.RESET} {Color.PURPLE}{self.target_region}{Color.RESET}")
            print(f"  {Color.WHITE}Accounts:{Color.RESET} {Color.BLUE}{self.total}{Color.RESET}")
            print()
            print(f"  {Color.GREEN}✅ Success:{Color.RESET} {self.results['success']}")
            print(f"  {Color.YELLOW}⚠️ Already:{Color.RESET} {self.results['already']}")
            print(f"  {Color.PURPLE}🔒 Insufficient:{Color.RESET} {self.results['insufficient']}")
            print(f"  {Color.RED}❌ Failed:{Color.RESET} {self.results['failed']}")
            print()
            print(f"  {Color.WHITE}👥 Before Followers:{Color.RESET} {self.initial_followers}")
            print(f"  {Color.WHITE}👥 After Followers:{Color.RESET} {self.current_followers}")
            if increase > 0:
                print(f"  {Color.GREEN}📈 Increase: +{increase}{Color.RESET}")
            elif increase < 0:
                print(f"  {Color.RED}📉 Decrease: {increase}{Color.RESET}")
            else:
                print(f"  {Color.YELLOW}➖ No change{Color.RESET}")
            print(f"\n{color}{'═'*70}{Color.RESET}")
            print(f"{Color.CYAN}  Credit: @Spideerio_yt & @Flexbasei{Color.RESET}")
            print(f"{color}{'═'*70}{Color.RESET}")

    def run(self, target_id, follow_count=None):
        self.target_id = target_id
        
        if follow_count and follow_count > 0:
            if self.uid_pass_list:
                self.uid_pass_list = self.uid_pass_list[:follow_count]
            elif self.tokens:
                self.tokens = self.tokens[:follow_count]
        
        self.total = len(self.tokens) if self.tokens else len(self.uid_pass_list)
        self.processed = 0
        self.start_time = time.time()
        
        if self.total == 0:
            if not self.telegram_mode:
                print(f"{Color.RED}❌ No accounts available{Color.RESET}")
            else:
                self._send_telegram_message("❌ No accounts available.")
            return
        
        if not self.telegram_mode:
            print(f"{Color.CYAN}▶ Target:{Color.RESET} {target_id}")
            print(f"{Color.CYAN}▶ Follow Count:{Color.RESET} {self.total}")
            print()
            print(f"{Color.WHITE}📡 Fetching target info...{Color.RESET}")
        
        if self.tokens:
            test_client = FreeFireWorkshop(self.tokens[0])
        elif self.uid_pass_list:
            first_uid = self.uid_pass_list[0]['uid']
            first_pass = self.uid_pass_list[0]['password']
            first_jwt = generate_jwt_from_uid_pass(first_uid, first_pass)
            if not first_jwt:
                if not self.telegram_mode:
                    print(f"{Color.YELLOW}⚠ Could not generate JWT for initial stats{Color.RESET}")
                test_client = FreeFireWorkshop("Bearer dummy")
            else:
                test_client = FreeFireWorkshop(first_jwt)
        else:
            if not self.telegram_mode:
                print(f"{Color.RED}✗ No accounts available{Color.RESET}")
            return
        
        self.initial_followers = test_client.get_follower_count(target_id) or 0
        self.current_followers = self.initial_followers
        account_details = test_client.get_account_details(target_id)
        if account_details:
            self.target_username = account_details.get('username', 'Unknown')
            self.target_level = account_details.get('level', 0)
            self.target_region = account_details.get('region', 'Unknown')
            if not self.telegram_mode:
                print(f"{Color.GREEN}✅ Username:{Color.RESET} {self.target_username}")
                print(f"{Color.GREEN}✅ Level:{Color.RESET} {self.target_level}")
                print(f"{Color.GREEN}✅ Region:{Color.RESET} {self.target_region}")
                print(f"{Color.GREEN}✅ Followers:{Color.RESET} {self.current_followers}")
        else:
            if not self.telegram_mode:
                print(f"{Color.YELLOW}⚠ Could not fetch target details{Color.RESET}")
                print(f"{Color.GREEN}✅ Followers:{Color.RESET} {self.current_followers}")
        if not self.telegram_mode:
            print()

        if self.telegram_mode:
            self._send_telegram_message(f"🚀 Starting Follow\nTarget: {target_id}\nAccounts: {self.total}")

        max_workers = min(10, self.total)
        if not self.telegram_mode:
            print(f"{Color.WHITE}⚡ Processing with {max_workers} threads...{Color.RESET}\n")
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {}
            if self.tokens:
                for token in self.tokens:
                    future = executor.submit(self.process_account_with_token, token, target_id, self.current_followers)
                    futures[future] = token
            else:
                for account in self.uid_pass_list:
                    future = executor.submit(self.process_uid_pass_account, account, target_id, self.current_followers)
                    futures[future] = account
            for future in as_completed(futures):
                self.processed += 1
                result = future.result()
                if result['status'] == 'success':
                    self.results['success'] += 1
                    if result.get('followers', 0) > self.current_followers:
                        self.current_followers = result['followers']
                elif result['status'] == 'already':
                    self.results['already'] += 1
                elif result['status'] == 'insufficient':
                    self.results['insufficient'] += 1
                else:
                    self.results['failed'] += 1
                    self.results['errors'].append(result)
                if not self.telegram_mode:
                    self.print_progress()
        self.print_summary()
        results = {
            'timestamp': datetime.now().isoformat(),
            'target_id': target_id,
            'target_username': self.target_username,
            'target_level': self.target_level,
            'target_region': self.target_region,
            'total_accounts': self.total,
            'results': self.results,
            'initial_followers': self.initial_followers,
            'final_followers': self.current_followers,
            'increase': self.current_followers - self.initial_followers
        }
        try:
            with open('follow_results.json', 'w') as f:
                json.dump(results, f, indent=2)
            if not self.telegram_mode:
                print(f"\n{Color.GREEN}✅ Results saved to follow_results.json{Color.RESET}")
        except:
            pass

# =========================================================
#  📋 API GUIDE - REAL URL AUTO DETECT
# =========================================================
def get_render_url():
    """Render se URL fetch karo"""
    return os.environ.get('RENDER_EXTERNAL_URL')

def get_api_guide(api_key: str, user_id: int, main_bot_url: str) -> str:
    """Generate API guide with real URL"""
    guide = f"""
📡 CRAFTLAND API GUIDE
━━━━━━━━━━━━━━━━━━━━━━━

🔑 YOUR API KEY
━━━━━━━━━━━━━━━━━━━━━━━
{api_key}
━━━━━━━━━━━━━━━━━━━━━━━

🔗 MAIN BOT URL
━━━━━━━━━━━━━━━━━━━━━━━
{main_bot_url}
━━━━━━━━━━━━━━━━━━━━━━━

✅ Real URL automatically detected from Render!
📌 Use this URL in your Selling Bot.

━━━━━━━━━━━━━━━━━━━━━━━
📌 API KEY KYA HAI?
━━━━━━━━━━━━━━━━━━━━━━━

API Key ek unique password hai jo aapke bot ko Craftland Follower Bot se connect karta hai.

━━━━━━━━━━━━━━━━━━━━━━━
📌 RESELLER SELLING BOT KAISE BANAYE?
━━━━━━━━━━━━━━━━━━━━━━━

1. Python install karein:
   pip install python-telegram-bot requests

2. Selling bot code:
━━━━━━━━━━━━━━━━━━━━━━━
# selling_bot.py
import os, json, requests
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup
from telegram.ext import Application, CommandHandler, CallbackQueryHandler, MessageHandler, filters, ContextTypes

BOT_TOKEN = "YOUR_BOT_TOKEN"
API_KEY = "{api_key}"
MAIN_BOT_URL = "{main_bot_url}"

PRODUCTS = [(1, "20 Followers", 20, 20), (2, "40 Followers", 40, 40), (3, "60 Followers", 60, 60), (4, "80 Followers", 80, 80), (5, "100 Followers", 100, 100)]

def get_products():
    try:
        resp = requests.post(f"{{MAIN_BOT_URL}}/api/products", json={{"api_key": API_KEY}}, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            if data.get("status") == "success":
                return data["products"]
    except: pass
    return None

def place_order(product_id, target_id, user_id=None):
    try:
        payload = {{"api_key": API_KEY, "product_id": product_id, "target_id": target_id, "user_id": user_id or "unknown"}}
        resp = requests.post(f"{{MAIN_BOT_URL}}/api/follow", json=payload, timeout=30)
        return resp.json()
    except:
        return {{"status": "error", "error": "network_error", "message": "Could not connect to main bot"}}

app = Application.builder().token(BOT_TOKEN).build()

async def start(update, context):
    await update.message.reply_text("🌟 Welcome to Craftland Follower Shop!", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("🛒 Start Follower")]], resize_keyboard=True))

async def handle_text(update, context):
    if update.message.text == "🛒 Start Follower":
        products = get_products()
        if not products:
            await update.message.reply_text("❌ Could not connect to main bot.")
            return
        keyboard = []
        for p in products:
            status = p["status"]
            label = f"{{p['name']}} - ₹{{p['price']}}" + (" ✅" if status == "available" else " ❌ Sold Out")
            keyboard.append([InlineKeyboardButton(label, callback_data=f"product_{{p['id']}}")])
        await update.message.reply_text("🛒 SELECT PRODUCT", reply_markup=InlineKeyboardMarkup(keyboard))

async def handle_callback(update, context):
    query = update.callback_query
    await query.answer()
    data = query.data
    if data.startswith("product_"):
        product_id = int(data.split("_")[1])
        products = get_products()
        if not products:
            await query.edit_message_text("❌ Cannot reach main bot.")
            return
        selected = next((p for p in products if p["id"] == product_id), None)
        if not selected or selected["status"] != "available":
            await query.edit_message_text("❌ Product not available.")
            return
        context.user_data["buying_product"] = product_id
        await query.edit_message_text(f"✅ Selected: {{selected['name']}}\n📝 Send Target UID:")

async def handle_target_uid(update, context):
    if context.user_data.get("buying_product"):
        target_uid = update.message.text.strip()
        if not target_uid.isdigit():
            await update.message.reply_text("❌ Send numeric UID.")
            return
        product_id = context.user_data.pop("buying_product")
        result = place_order(product_id, target_uid, user_id=update.effective_user.id)
        if result.get("status") == "success":
            await update.message.reply_text(f"✅ Order Successful!\n👤 Target: {{target_uid}}\n✅ Followers Added: {{result.get('followers_added', 0)}}\n💰 Cost: ₹{{result.get('cost', 0.0):.2f}}\n💳 Balance Remaining: ₹{{result.get('balance_remaining', 0):.2f}}")
        else:
            await update.message.reply_text(f"❌ Order Failed!\nReason: {{result.get('message', 'Unknown error')}}")
    else:
        await update.message.reply_text("❌ Use 'Start Follower' button first.")

def main():
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_target_uid))
    app.add_handler(CallbackQueryHandler(handle_callback))
    print("🤖 Selling Bot started...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

if __name__ == "__main__":
    main()
━━━━━━━━━━━━━━━━━━━━━━━

3. Bot run karein:
   python selling_bot.py

━━━━━━━━━━━━━━━━━━━━━━━
📡 API ENDPOINTS
━━━━━━━━━━━━━━━━━━━━━━━

1. GET PRODUCTS
   POST {main_bot_url}/api/products
   {{
     "api_key": "{api_key}"
   }}

2. PLACE ORDER
   POST {main_bot_url}/api/follow
   {{
     "api_key": "{api_key}",
     "product_id": 1,
     "target_id": "14423134156",
     "user_id": "7018768597"
   }}

━━━━━━━━━━━━━━━━━━━━━━━
💳 BALANCE KAISE ADD KAREIN?
━━━━━━━━━━━━━━━━━━━━━━━

1️⃣ Main bot mein "💰 Add Fund" click karein
2️⃣ Amount select karein
3️⃣ QR Code scan karein ya UPI se pay karein
4️⃣ Transaction ID bhejein
5️⃣ Owner approve karega
6️⃣ Balance add ho jayegi

📱 UPI: {UPI_ID}

━━━━━━━━━━━━━━━━━━━━━━━
📞 SUPPORT
━━━━━━━━━━━━━━━━━━━━━━━

👤 Contact : {SUPPORT_USERNAME}

━━━━━━━━━━━━━━━━━━━━━━━
🔑 YOUR API KEY: {api_key}
🆔 YOUR USER ID: {user_id}
🔗 MAIN BOT URL: {main_bot_url}

━━━━━━━━━━━━━━━━━━━━━━━
"""
    return guide

# =========================================================
#  🤖 TELEGRAM BOT HANDLERS
# =========================================================
if TELEGRAM_AVAILABLE:
    bot_data = BotData()
    accounts_set: Set[Tuple[str, str]] = set()
    used_accounts: Dict[str, Set[str]] = {}

    def load_accounts_for_telegram():
        global accounts_set
        if not os.path.exists(STORAGE_FILE):
            accounts_set = set()
            return
        try:
            with open(STORAGE_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                accounts_list = data.get("accounts", [])
                accounts_set = {(str(acc['uid']), str(acc['password'])) for acc in accounts_list}
            print(f"✅ Telegram loaded {len(accounts_set)} accounts.")
        except Exception as e:
            print(f"❌ Error loading accounts: {e}")
            accounts_set = set()

    def save_accounts_for_telegram():
        try:
            accounts_list = [{"uid": uid, "password": pwd} for uid, pwd in accounts_set]
            with open(STORAGE_FILE, 'w', encoding='utf-8') as f:
                json.dump({"accounts": accounts_list}, f, indent=2)
            return True
        except Exception as e:
            print(f"❌ Error saving: {e}")
            return False

    def load_used_accounts():
        global used_accounts
        if not os.path.exists(USED_ACCOUNTS_FILE):
            used_accounts = {}
            return
        try:
            with open(USED_ACCOUNTS_FILE, 'r', encoding='utf-8') as f:
                data = json.load(f)
                used_accounts = {k: set(v) for k, v in data.items()}
            print(f"✅ Loaded used accounts for {len(used_accounts)} targets.")
        except Exception as e:
            print(f"❌ Error loading used accounts: {e}")
            used_accounts = {}

    def save_used_accounts():
        try:
            data = {k: list(v) for k, v in used_accounts.items()}
            with open(USED_ACCOUNTS_FILE, 'w', encoding='utf-8') as f:
                json.dump(data, f, indent=2)
        except Exception as e:
            print(f"❌ Error saving used accounts: {e}")

    def parse_accounts_from_content(content: str, filename: str) -> List[Tuple[str, str]]:
        accounts = []
        lines = content.strip().splitlines()
        if filename.lower().endswith('.json'):
            try:
                data = json.loads(content)
                if isinstance(data, list):
                    for item in data:
                        if isinstance(item, dict):
                            uid = item.get('uid')
                            pwd = item.get('password')
                            if uid and pwd:
                                accounts.append((str(uid).strip(), str(pwd).strip()))
                elif isinstance(data, dict):
                    uid = data.get('uid')
                    pwd = data.get('password')
                    if uid and pwd:
                        accounts.append((str(uid).strip(), str(pwd).strip()))
                return accounts
            except json.JSONDecodeError:
                pass
        for line in lines:
            line = line.strip()
            if not line:
                continue
            if ':' in line:
                parts = line.split(':', 1)
            elif '|' in line:
                parts = line.split('|', 1)
            else:
                continue
            if len(parts) == 2:
                uid = parts[0].strip()
                pwd = parts[1].strip()
                if uid and pwd:
                    accounts.append((uid, pwd))
        return accounts

    # =========================================================
    #  🎯 KEYBOARD
    # =========================================================
    def get_main_keyboard(user_id):
        is_owner = bot_data.is_owner(user_id)
        is_admin = bot_data.is_admin(user_id)
        is_reseller = bot_data.is_reseller(user_id)
        
        keyboard = [
            [KeyboardButton("🎯 Start Follower")],
            [KeyboardButton("💰 Add Fund"), KeyboardButton("👤 My Profile")],
            [KeyboardButton("🤝 Refer & Earn"), KeyboardButton("❓ Help")],
        ]
        
        if is_reseller or is_admin or is_owner:
            keyboard.append([KeyboardButton("🔑 API Key")])
            keyboard.append([KeyboardButton("📡 API Guide")])
        else:
            keyboard.append([KeyboardButton("💎 Buy Reseller")])
        
        keyboard.append([KeyboardButton("📞 Support")])
        
        if is_admin:
            keyboard.append([KeyboardButton("📤 Upload Accounts")])
            keyboard.append([KeyboardButton("📋 Admin Panel")])
        
        if is_owner:
            keyboard.append([KeyboardButton("👑 Owner Panel")])
            keyboard.append([KeyboardButton("📦 Total Accounts")])
            keyboard.append([KeyboardButton("💰 Add Balance")])
        
        return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

    # =========================================================
    #  🚀 START
    # =========================================================
    async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        
        user = bot_data.get_user(user_id)
        user["username"] = username
        bot_data.save()
        
        balance = bot_data.get_balance(user_id)
        
        welcome_msg = (
            f"🌟 Welcome to Craftland Follower\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance : {balance}\n"
            f"🆔 ID : {user_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔽 Use the buttons below"
        )
        
        await update.message.reply_text(
            welcome_msg,
            reply_markup=get_main_keyboard(user_id)
        )

    # =========================================================
    #  🔑 API KEY HANDLER
    # =========================================================
    async def handle_api_key(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        
        if not (bot_data.is_reseller(user_id) or bot_data.is_admin(user_id) or bot_data.is_owner(user_id)):
            await update.message.reply_text("❌ Only Resellers, Admins, and Owner can generate API Key.")
            return
        
        api_key = bot_data.generate_api_key(user_id)
        if api_key:
            keyboard = [
                [
                    InlineKeyboardButton("🔄 Reset API Key", callback_data="reset_api_key")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"🔑 YOUR API KEY\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{api_key}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"✅ This is your permanent API Key.\n"
                f"📌 Keep it secure. Do NOT share it publicly.\n"
                f"📡 Use '📡 API Guide' for complete documentation.",
                reply_markup=reply_markup
            )
        else:
            await update.message.reply_text("❌ Failed to generate API Key.")

    # =========================================================
    #  📡 API GUIDE HANDLER - REAL URL AUTO DETECT
    # =========================================================
    async def handle_api_guide(update: Update, context: ContextTypes.DEFAULT_TYPE):
        global MAIN_BOT_URL
        user_id = update.effective_user.id
        
        api_key = bot_data.get_api_key(user_id)
        if not api_key:
            api_key = bot_data.generate_api_key(user_id)
            if not api_key:
                await update.message.reply_text("❌ API Key generate nahi ho paayi.\n🔑 Pehle 'API Key' button click karein.")
                return
        
        # 🔥 REAL URL - Render se auto detect
        if not MAIN_BOT_URL:
            MAIN_BOT_URL = get_render_url()
        
        if not MAIN_BOT_URL:
            await update.message.reply_text(
                "❌ Bot URL detect nahi ho paaya!\n\n"
                "⚠️ Render pe deploy karo, URL auto detect ho jayega."
            )
            return
        
        guide = get_api_guide(api_key, user_id, MAIN_BOT_URL)
        
        if len(guide) > 4000:
            parts = guide.split('\n\n')
            current_msg = ""
            for part in parts:
                if len(current_msg) + len(part) + 2 > 4000:
                    await update.message.reply_text(current_msg)
                    current_msg = part + '\n\n'
                else:
                    current_msg += part + '\n\n'
            if current_msg:
                await update.message.reply_text(current_msg)
        else:
            await update.message.reply_text(guide)

    # =========================================================
    #  🤝 REFER & EARN
    # =========================================================
    async def handle_refer(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        bot_username = context.bot.username or "Hii_goog_bot"
        refer_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        referral_info = bot_data.get_referral_info(user_id)
        reward = referral_info['reward']
        total_referrals = len(referral_info['referrals'])
        earnings = total_referrals * reward
        
        msg = (
            f"🤝 REFERRAL SYSTEM\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🔗 Link :\n"
            f"{refer_link}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"👥 Referrals : {total_referrals}\n"
            f"💰 Earnings : ₹{earnings}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 1 Refer = +{reward}₹\n"
            f"   (2 followers worth)\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )
        
        keyboard = [
            [InlineKeyboardButton("📤 Share Link", switch_inline_query=f"Join Craftland Follower!\n\n{refer_link}")]
        ]
        reply_markup = InlineKeyboardMarkup(keyboard)
        
        await update.message.reply_text(msg, reply_markup=reply_markup)

    # =========================================================
    #  📝 HANDLE TEXT
    # =========================================================
    async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        text = update.message.text
        user_id = update.effective_user.id
        
        if text == "📞 Support":
            await update.message.reply_text(f"📞 SUPPORT\n━━━━━━━━━━━━━━━━━━━━━━━\n👤 Contact : {SUPPORT_USERNAME}\n━━━━━━━━━━━━━━━━━━━━━━━")
            return

        if text == "🔑 API Key":
            await handle_api_key(update, context)
            return

        if text == "📡 API Guide":
            await handle_api_guide(update, context)
            return

        if text == "🤝 Refer & Earn":
            await handle_refer(update, context)
            return

        if text == "🎯 Start Follower":
            cost = bot_data.get_cost(user_id)
            balance = bot_data.get_balance(user_id)
            
            msg = (
                f"🎯 CRAFTLAND FOLLOWER\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Cost : ₹{cost:.2f}/success\n"
                f"⚠️ Only success charged\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💳 Balance : {balance}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 Enter follower count :"
            )
            
            await update.message.reply_text(msg)
            context.user_data['waiting_for_follower_count'] = True
            return

        if text == "💰 Add Fund":
            keyboard = []
            row = []
            for amount in ADD_FUND_AMOUNTS:
                row.append(InlineKeyboardButton(f"₹{amount}", callback_data=f"addfund_{amount}"))
                if len(row) == 3:
                    keyboard.append(row)
                    row = []
            if row:
                keyboard.append(row)
            
            keyboard.append([InlineKeyboardButton("📝 Manual Deposit", callback_data="manual_deposit")])
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            balance = bot_data.get_balance(user_id)
            msg = (
                f"💰 ADD FUNDS\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 UPI : {UPI_ID}\n"
                f"💳 Balance : {balance}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚡ Select amount :"
            )
            
            await update.message.reply_text(msg, reply_markup=reply_markup)
            return

        if text == "👤 My Profile":
            is_admin = bot_data.is_admin(user_id)
            is_owner = bot_data.is_owner(user_id)
            is_reseller = bot_data.is_reseller(user_id)
            balance = bot_data.get_balance(user_id)
            
            msg = (
                f"👤 MY PROFILE\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 ID : {user_id}\n"
                f"💰 Balance : {balance}\n"
                f"👑 Owner : {'✅' if is_owner else '❌'}\n"
                f"🔑 Admin : {'✅' if is_admin else '❌'}\n"
                f"💎 Reseller : {'✅' if is_reseller else '❌'}\n"
            )
            
            if is_admin and not is_owner:
                remaining = bot_data.get_admin_remaining(user_id)
                admin_info = bot_data.get_admin_info(user_id)
                if admin_info:
                    msg += (
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"📊 Limit : {admin_info['limit']}\n"
                        f"📈 Used : {admin_info['used']}\n"
                        f"📉 Remaining : {remaining}\n"
                    )
            elif is_owner:
                msg += (
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"👑 Owner : Unlimited Balance\n"
                )
            
            if is_reseller or is_admin or is_owner:
                api_key = bot_data.get_api_key(user_id)
                if api_key:
                    msg += (
                        f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                        f"🔑 API Key : {api_key}\n"
                    )
            
            referral_info = bot_data.get_referral_info(user_id)
            reward = referral_info['reward']
            msg += (
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤝 Referrals : {len(referral_info['referrals'])}\n"
                f"💰 Reward : +{reward}₹ / refer\n"
                f"   (2 followers worth)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            await update.message.reply_text(msg)
            return

        if text == "💎 Buy Reseller":
            if bot_data.is_reseller(user_id):
                await update.message.reply_text("✅ You are already a Reseller!")
                return

            balance_raw = bot_data.get_balance_raw(user_id)
            cost = RESELLER_FEE
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Pay ₹100 & Become Reseller", callback_data="buy_reseller_confirm")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            await update.message.reply_text(
                f"💎 BUY RESELLER\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Fee : ₹{cost} (One Time)\n"
                f"📈 Rate : 1₹ = 10 Followers\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"Benefits:\n"
                f"• Sell followers at your price\n"
                f"• Keep up to 90% profit\n"
                f"• Get your own API Key\n"
                f"• Create your own bot\n\n"
                f"Pay ₹100 and become Reseller!",
                reply_markup=reply_markup
            )
            return

        if context.user_data.get('waiting_for_follower_count'):
            if text.isdigit():
                followers_needed = int(text)
                if followers_needed <= 0:
                    await update.message.reply_text("❌ Enter valid number")
                    return
                
                if len(accounts_set) < followers_needed:
                    await update.message.reply_text(f"❌ Only {len(accounts_set)} accounts available")
                    context.user_data['waiting_for_follower_count'] = False
                    return
                
                if not bot_data.is_admin(user_id):
                    balance_raw = bot_data.get_balance_raw(user_id)
                    cost_per = bot_data.get_cost(user_id)
                    needed_balance = followers_needed * cost_per
                    if balance_raw < needed_balance:
                        await update.message.reply_text(f"❌ Insufficient balance\n💰 Need ₹{needed_balance:.2f}, have ₹{balance_raw:.2f}\nUse 'Add Fund'")
                        context.user_data['waiting_for_follower_count'] = False
                        return
                
                context.user_data['followers_needed'] = followers_needed
                
                await update.message.reply_text(
                    f"✅ {followers_needed} followers\n\n"
                    f"🎯 Send Target UID :"
                )
                context.user_data['waiting_for_follower_count'] = False
                context.user_data['waiting_for_target_uid'] = True
            else:
                await update.message.reply_text("❌ Send number only")
            return

        if context.user_data.get('waiting_for_target_uid'):
            if text.isdigit():
                target_uid = text
                followers_needed = context.user_data.get('followers_needed', 0)
                
                if followers_needed <= 0:
                    await update.message.reply_text("❌ Restart please")
                    context.user_data['waiting_for_target_uid'] = False
                    return
                
                target_str = str(target_uid)
                used = used_accounts.get(target_str, set())
                available = [acc for acc in accounts_set if acc[0] not in used]
                if len(available) == 0:
                    await update.message.reply_text("❌ No unused accounts left for this target.")
                    context.user_data['waiting_for_target_uid'] = False
                    return
                
                if followers_needed > len(available):
                    await update.message.reply_text(f"⚠️ Only {len(available)} unused accounts available. Using them.")
                    followers_needed = len(available)
                
                chosen = available[:followers_needed]
                context.user_data['followers_needed'] = followers_needed
                
                await update.message.reply_text(
                    f"🚀 Starting...\n"
                    f"🎯 {target_uid} | 👥 {followers_needed}\n"
                    f"⏳ Please wait"
                )
                
                main_loop = asyncio.get_running_loop()
                user_id_copy = user_id
                followers_count = followers_needed
                chosen_copy = chosen

                def run_follow():
                    try:
                        bot_follower = FollowerBot(
                            telegram_mode=True,
                            chat_id=update.effective_chat.id,
                            bot_instance=context.bot,
                            loop=main_loop,
                            user_id=user_id_copy,
                            bot_data=bot_data
                        )
                        used_for_this = set()
                        for uid, pwd in chosen_copy:
                            bot_follower.uid_pass_list.append({'uid': uid, 'password': pwd})
                            used_for_this.add(uid)
                        bot_follower.run(int(target_uid), followers_count)
                        target_str = str(target_uid)
                        if target_str not in used_accounts:
                            used_accounts[target_str] = set()
                        used_accounts[target_str].update(used_for_this)
                        save_used_accounts()
                    except Exception as e:
                        print(f"Follow error: {e}")
                        asyncio.run_coroutine_threadsafe(
                            context.bot.send_message(
                                chat_id=update.effective_chat.id,
                                text=f"❌ Error : {str(e)}"
                            ),
                            main_loop
                        )

                thread = threading.Thread(target=run_follow, daemon=True)
                thread.start()
                context.user_data['waiting_for_target_uid'] = False
                context.user_data['followers_needed'] = 0
            else:
                await update.message.reply_text("❌ Send numeric UID")
            return

        if context.user_data.get('waiting_for_manual_deposit'):
            parts = text.strip().split()
            expected_amount = context.user_data.get('expected_amount')
            payment_purpose = context.user_data.get('payment_purpose', 'add_fund')
            
            if expected_amount:
                if len(parts) >= 1:
                    txn_id = ' '.join(parts)
                    if not txn_id:
                        await update.message.reply_text("❌ Please provide transaction ID")
                        return
                    amount = expected_amount
                else:
                    await update.message.reply_text("❌ Send transaction ID")
                    return
            else:
                if len(parts) >= 2 and parts[0].isdigit():
                    amount = int(parts[0])
                    if amount <= 0:
                        await update.message.reply_text("❌ Amount > 0")
                        return
                    txn_id = ' '.join(parts[1:])
                    if not txn_id:
                        await update.message.reply_text("❌ Please provide transaction ID")
                        return
                    payment_purpose = 'add_fund'
                else:
                    await update.message.reply_text("❌ Format : amount transaction_id")
                    return
            
            payment = bot_data.add_payment(user_id, amount, txn_id, purpose=payment_purpose)
            
            await update.message.reply_text(
                f"✅ Payment Request Sent\n"
                f"💰 ₹{amount} | 🆔 {txn_id}\n"
                f"⏳ Awaiting approval"
            )
            
            payment_msg = (
                f"💳 Payment Request\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👤 User : {user_id}\n"
                f"💰 Amount : ₹{amount}\n"
                f"🆔 TXN : {txn_id}\n"
                f"📌 Purpose : {payment_purpose}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Approve", callback_data=f"approve_{txn_id}"),
                    InlineKeyboardButton("❌ Cancel", callback_data=f"reject_{txn_id}")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            try:
                await context.bot.send_message(
                    chat_id=OWNER_ID, 
                    text=payment_msg,
                    reply_markup=reply_markup
                )
            except Exception as e:
                print(f"❌ Failed to send payment notification to owner: {e}")
            
            context.user_data['waiting_for_manual_deposit'] = False
            context.user_data['expected_amount'] = None
            context.user_data['payment_purpose'] = None
            return

        if text == "👑 Owner Panel":
            if not bot_data.is_owner(user_id):
                await update.message.reply_text("⛔ Unauthorized")
                return
            
            keyboard = [
                [InlineKeyboardButton("➕ Add Admin", callback_data="owner_add_admin"),
                 InlineKeyboardButton("➖ Remove Admin", callback_data="owner_remove_admin")],
                [InlineKeyboardButton("📋 List Admins", callback_data="owner_list_admins"),
                 InlineKeyboardButton("⚙️ Set Limit", callback_data="owner_set_limit")],
                [InlineKeyboardButton("💰 Set Cost", callback_data="owner_set_cost"),
                 InlineKeyboardButton("💵 Pending", callback_data="owner_pending")],
                [InlineKeyboardButton("📢 Broadcast", callback_data="owner_broadcast"),
                 InlineKeyboardButton("📤 Backup", callback_data="owner_backup")]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            
            pending = len(bot_data.get_pending_payments())
            
            msg = (
                f"👑 OWNER PANEL\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"👥 Admins : {len(bot_data.data['admins'])}\n"
                f"💵 Pending : {pending}\n"
                f"💸 Cost : ₹{bot_data.get_cost(0):.2f}\n"
                f"📦 Accounts : {bot_data.get_total_accounts()}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            
            await update.message.reply_text(msg, reply_markup=reply_markup)
            return

        if text == "📋 Admin Panel":
            if not bot_data.is_admin(user_id):
                await update.message.reply_text("⛔ Unauthorized")
                return
            
            if bot_data.is_owner(user_id):
                await update.message.reply_text("👑 You are Owner, use Owner Panel")
                return
            
            remaining = bot_data.get_admin_remaining(user_id)
            admin_info = bot_data.get_admin_info(user_id)
            
            if admin_info is None:
                await update.message.reply_text("❌ Admin info not found")
                return
            
            msg = (
                f"📋 ADMIN PANEL\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🆔 ID : {user_id}\n"
                f"📊 Limit : {admin_info['limit']}\n"
                f"📈 Used : {admin_info['used']}\n"
                f"📉 Remaining : {remaining}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            await update.message.reply_text(msg)
            return

        if text == "📦 Total Accounts":
            if not bot_data.is_owner(user_id):
                await update.message.reply_text("⛔ Unauthorized")
                return
            
            total = bot_data.get_total_accounts()
            await update.message.reply_text(
                f"📦 TOTAL ACCOUNTS\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📊 Stored : {total}\n"
                f"📦 Active : {len(accounts_set)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        if text == "💰 Add Balance":
            if not bot_data.is_owner(user_id):
                await update.message.reply_text("⛔ Unauthorized")
                return
            
            await update.message.reply_text(
                f"💰 ADD BALANCE\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📝 user_id amount\n"
                f"📌 7018768597 100\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            context.user_data['owner_action'] = 'add_balance'
            return

        if text == "📤 Upload Accounts":
            if not bot_data.is_admin(user_id):
                await update.message.reply_text("⛔ Unauthorized")
                return
            
            await update.message.reply_text(
                f"📤 UPLOAD ACCOUNTS\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📁 .txt or .json\n"
                f"📌 uid:password\n"
                f"📌 uid|password\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━"
            )
            return

        if text == "❓ Help":
            await update.message.reply_text(
                f"📖 HOW TO USE\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"1️⃣ Start Follower\n"
                f"2️⃣ Enter count\n"
                f"3️⃣ Send Target UID\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💰 Add Funds\n"
                f"1️⃣ Click 'Add Fund'\n"
                f"2️⃣ Select amount\n"
                f"3️⃣ Scan QR Code\n"
                f"4️⃣ Pay to UPI\n"
                f"5️⃣ Send TXN ID\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"🤝 Refer & Earn\n"
                f"Share link → Get coins\n"
                f"  (2 followers worth)\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"💎 Buy Reseller\n"
                f"Pay ₹100 → get 1₹=10 followers\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📞 Support : {SUPPORT_USERNAME}"
            )
            return

        else:
            await update.message.reply_text("❌ Use buttons", reply_markup=get_main_keyboard(user_id))

    # =========================================================
    #  📄 DOCUMENT HANDLER
    # =========================================================
    async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not bot_data.is_admin(user_id):
            await update.message.reply_text("⛔ Unauthorized")
            return
        
        document = update.message.document
        if not document:
            return
        
        file_name = document.file_name or ""
        if not (file_name.lower().endswith('.txt') or file_name.lower().endswith('.json')):
            await update.message.reply_text("⚠️ .txt or .json only")
            return
        
        try:
            file = await context.bot.get_file(document.file_id)
            file_content = await file.download_as_bytearray()
            content = file_content.decode('utf-8')
            save_path = os.path.join(UPLOAD_FOLDER, file_name)
            with open(save_path, 'wb') as f:
                f.write(file_content)
        except Exception as e:
            await update.message.reply_text(f"❌ Error : {e}")
            return

        new_accounts = parse_accounts_from_content(content, file_name)
        if not new_accounts:
            await update.message.reply_text("❌ No valid accounts")
            return

        new_unique_count = 0
        duplicate_count = 0
        for uid, pwd in new_accounts:
            if (uid, pwd) in accounts_set:
                duplicate_count += 1
            else:
                accounts_set.add((uid, pwd))
                new_unique_count += 1

        save_accounts_for_telegram()
        total_count = len(accounts_set)

        await update.message.reply_text(
            f"✅ FILE PROCESSED\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"📂 {file_name}\n"
            f"🆕 New : {new_unique_count}\n"
            f"🔁 Dup : {duplicate_count}\n"
            f"📦 Total : {total_count}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━"
        )

    # =========================================================
    #  🔄 CALLBACK HANDLER
    # =========================================================
    async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
        query = update.callback_query
        await query.answer()
        user_id = update.effective_user.id
        data = query.data

        if data == "buy_reseller_confirm":
            if bot_data.is_reseller(user_id):
                await query.edit_message_text("✅ You are already a Reseller!")
                return
            
            balance_raw = bot_data.get_balance_raw(user_id)
            cost = RESELLER_FEE
            
            if balance_raw >= cost:
                keyboard = [
                    [
                        InlineKeyboardButton("✅ Yes, Pay ₹100", callback_data="confirm_reseller_pay"),
                        InlineKeyboardButton("❌ No", callback_data="cancel_reseller_pay")
                    ]
                ]
                reply_markup = InlineKeyboardMarkup(keyboard)
                await query.edit_message_text(
                    f"💎 BUY RESELLER\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Fee : ₹{cost} (One Time)\n"
                    f"📈 Rate : 1₹ = 10 Followers\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"✅ Your balance: ₹{balance_raw}\n"
                    f"⚠️ ₹{cost} will be deducted.\n\n"
                    f"Confirm payment?",
                    reply_markup=reply_markup
                )
            else:
                order_id = f"RES{int(time.time())}{random.randint(100, 999)}"
                try:
                    qr_bytes = generate_qr_png(UPI_ID, cost, order_id)
                except Exception as e:
                    await query.edit_message_text(f"❌ QR generation failed: {e}")
                    return
                
                context.user_data['expected_amount'] = cost
                context.user_data['waiting_for_manual_deposit'] = True
                context.user_data['payment_purpose'] = 'reseller'
                
                caption = (
                    f"💎 BUY RESELLER\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Fee : ₹{cost} (One Time)\n"
                    f"📈 Rate : 1₹ = 10 Followers\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"❌ Insufficient balance!\n"
                    f"✅ Need ₹{cost}\n"
                    f"☐ Have ₹{balance_raw}\n\n"
                    f"Please add funds first.\n\n"
                    f"📱 UPI : {UPI_ID}\n"
                    f"📝 Send TXN ID after payment."
                )
                photo_bytes = io.BytesIO(qr_bytes)
                photo_bytes.name = f"qr_{order_id}.png"
                
                await query.delete_message()
                
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=photo_bytes,
                    caption=caption
                )
                await context.bot.send_message(
                    chat_id=update.effective_chat.id,
                    text=f"📝 After payment, send the transaction ID as a message.\nExample: TXN123456789\n\n⏳ Your reseller status will be activated upon approval."
                )
            return

        if data == "confirm_reseller_pay":
            if bot_data.is_reseller(user_id):
                await query.edit_message_text("✅ You are already a Reseller!")
                return
            
            if bot_data.get_balance_raw(user_id) < RESELLER_FEE:
                await query.edit_message_text("❌ Insufficient balance. Please add funds.")
                return
            
            if bot_data.deduct_balance(user_id, RESELLER_FEE):
                bot_data.make_reseller(user_id)
                api_key = bot_data.get_api_key(user_id)
                await query.edit_message_text(
                    f"✅ Congratulations! You are now a Reseller!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"💰 Fee : ₹{RESELLER_FEE} deducted\n"
                    f"📈 New Rate : 1₹ = 10 Followers\n"
                    f"🔑 API Key : {api_key}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"Use /start to see new options."
                )
            else:
                await query.edit_message_text("❌ Payment failed. Please try again.")
            return

        if data == "cancel_reseller_pay":
            await query.edit_message_text("❌ Reseller purchase cancelled.")
            return

        if data == "reset_api_key":
            if not (bot_data.is_reseller(user_id) or bot_data.is_admin(user_id) or bot_data.is_owner(user_id)):
                await query.edit_message_text("❌ Unauthorized")
                return
            
            keyboard = [
                [
                    InlineKeyboardButton("✅ Yes, Reset", callback_data="confirm_reset_api"),
                    InlineKeyboardButton("❌ No", callback_data="cancel_reset_api")
                ]
            ]
            reply_markup = InlineKeyboardMarkup(keyboard)
            current_api = bot_data.get_api_key(user_id) or "Not generated"
            await query.edit_message_text(
                f"🔄 RESET API KEY\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"⚠️ Are you sure?\n"
                f"Old API Key will stop working.\n\n"
                f"Current API Key:\n{current_api}\n\n"
                f"Confirm?",
                reply_markup=reply_markup
            )
            return

        if data == "confirm_reset_api":
            if not (bot_data.is_reseller(user_id) or bot_data.is_admin(user_id) or bot_data.is_owner(user_id)):
                await query.edit_message_text("❌ Unauthorized")
                return
            
            new_api_key = bot_data.reset_api_key(user_id)
            if new_api_key:
                await query.edit_message_text(
                    f"✅ API Key Reset Successfully!\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                    f"🔑 New API Key:\n{new_api_key}\n"
                    f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                    f"📌 Old API Key is now invalid."
                )
            else:
                await query.edit_message_text("❌ Failed to reset API Key.")
            return

        if data == "cancel_reset_api":
            await query.edit_message_text("❌ API Key reset cancelled.")
            return

        if data.startswith("addfund_"):
            amount = int(data.replace("addfund_", ""))
            order_id = f"CF{int(time.time())}{random.randint(100, 999)}"
            try:
                qr_bytes = generate_qr_png(UPI_ID, amount, order_id)
            except Exception as e:
                await query.edit_message_text(f"❌ QR generation failed: {e}")
                return
            context.user_data['expected_amount'] = amount
            context.user_data['waiting_for_manual_deposit'] = True
            context.user_data['payment_purpose'] = 'add_fund'
            await query.delete_message()
            photo_bytes = io.BytesIO(qr_bytes)
            photo_bytes.name = f"qr_{order_id}.png"
            await context.bot.send_photo(
                chat_id=update.effective_chat.id,
                photo=photo_bytes,
                caption=f"💰 Pay ₹{amount} to UPI : {UPI_ID}\n📝 Send TXN ID only.\nExample: TXN123456789"
            )
            return

        if data == "manual_deposit":
            context.user_data['expected_amount'] = None
            context.user_data['waiting_for_manual_deposit'] = True
            context.user_data['payment_purpose'] = 'add_fund'
            await query.edit_message_text(
                f"📝 Manual Deposit\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n"
                f"📱 UPI : {UPI_ID}\n"
                f"💰 Balance : {bot_data.get_balance(user_id)}\n"
                f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
                f"📝 amount transaction_id\n"
                f"📌 100 TXN123456789"
            )
            return

        if data.startswith("approve_"):
            if not bot_data.is_owner(user_id):
                await query.edit_message_text("⛔ Unauthorized")
                return
            txn_id = data.replace("approve_", "")
            payment = bot_data.approve_payment(txn_id)
            if payment:
                purpose = payment.get("purpose", "add_fund")
                msg = f"✅ Payment Approved\n━━━━━━━━━━━━━━━━━━━━━━━\n👤 {payment['user_id']}\n💰 ₹{payment['amount']}\n🆔 {txn_id}\n📌 Purpose : {purpose}\n━━━━━━━━━━━━━━━━━━━━━━━"
                await query.edit_message_text(msg)
                user_id_int = int(payment['user_id'])
                if purpose == "reseller":
                    api_key = bot_data.get_api_key(user_id_int)
                    await context.bot.send_message(chat_id=user_id_int, text=f"✅ Reseller activation successful!\n🔑 API Key: {api_key}")
                else:
                    balance = bot_data.get_balance(user_id_int)
                    await context.bot.send_message(chat_id=user_id_int, text=f"✅ Payment Approved\n💰 ₹{payment['amount']} added\n💳 Balance : {balance}")
            else:
                await query.edit_message_text(f"❌ TXN {txn_id} not found")
            return

        if data.startswith("reject_"):
            if not bot_data.is_owner(user_id):
                await query.edit_message_text("⛔ Unauthorized")
                return
            txn_id = data.replace("reject_", "")
            payment = bot_data.reject_payment(txn_id)
            if payment:
                await query.edit_message_text(f"❌ Payment Cancelled\n━━━━━━━━━━━━━━━━━━━━━━━\n👤 {payment['user_id']}\n💰 ₹{payment['amount']}\n🆔 {txn_id}\n━━━━━━━━━━━━━━━━━━━━━━━")
                try:
                    await context.bot.send_message(chat_id=int(payment['user_id']), text=f"❌ Payment Cancelled\n💰 ₹{payment['amount']} rejected")
                except: pass
            else:
                await query.edit_message_text(f"❌ TXN {txn_id} not found")
            return

        if not bot_data.is_owner(user_id):
            await query.edit_message_text("⛔ Unauthorized")
            return

        if data == "owner_add_admin":
            await query.edit_message_text("➕ Add Admin\n━━━━━━━━━━━━━━━━━━━━━━━\n📝 user_id limit\n📌 123456789 200\n━━━━━━━━━━━━━━━━━━━━━━━")
            context.user_data['owner_action'] = 'add_admin'
        elif data == "owner_remove_admin":
            admins = list(bot_data.data['admins'].keys())
            if not admins:
                await query.edit_message_text("❌ No admins")
                return
            msg = "➖ Remove Admin\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            for i, admin_id in enumerate(admins, 1):
                info = bot_data.data['admins'][admin_id]
                msg += f"{i}. {admin_id} (Limit:{info['limit']} Used:{info['used']})\n"
            msg += "\n📝 Send number"
            await query.edit_message_text(msg)
            context.user_data['owner_action'] = 'remove_admin'
            context.user_data['admin_list'] = admins
        elif data == "owner_list_admins":
            admins = bot_data.data['admins']
            if not admins:
                await query.edit_message_text("📋 No admins")
                return
            msg = "📋 ADMIN LIST\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            for admin_id, info in admins.items():
                remaining = info['limit'] - info['used']
                msg += f"🆔 {admin_id}\n   📊 {info['limit']} 📈 {info['used']} 📉 {remaining}\n"
            msg += "━━━━━━━━━━━━━━━━━━━━━━━"
            await query.edit_message_text(msg)
        elif data == "owner_set_limit":
            await query.edit_message_text("⚙️ Set Admin Limit\n━━━━━━━━━━━━━━━━━━━━━━━\n📝 user_id new_limit\n📌 123456789 500\n━━━━━━━━━━━━━━━━━━━━━━━")
            context.user_data['owner_action'] = 'set_limit'
        elif data == "owner_set_cost":
            await query.edit_message_text(f"💰 Set Cost\n━━━━━━━━━━━━━━━━━━━━━━━\n📝 amount\n📌 0.5\n💰 Current : ₹{bot_data.get_cost(0):.2f}\n━━━━━━━━━━━━━━━━━━━━━━━")
            context.user_data['owner_action'] = 'set_cost'
        elif data == "owner_pending":
            pending = bot_data.get_pending_payments()
            if not pending:
                await query.edit_message_text("✅ No pending")
                return
            msg = "💵 PENDING\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            for i, payment in enumerate(pending, 1):
                msg += f"{i}. {payment['user_id']} ₹{payment['amount']}\n   🆔 {payment['transaction_id']}\n"
            msg += "━━━━━━━━━━━━━━━━━━━━━━━"
            await query.edit_message_text(msg)
        elif data == "owner_broadcast":
            await query.edit_message_text("📢 BROADCAST\n━━━━━━━━━━━━━━━━━━━━━━━\n📝 Send message")
            context.user_data['owner_action'] = 'broadcast'
        elif data == "owner_backup":
            backup_files = [STORAGE_FILE, BOT_DATA_FILE, USED_ACCOUNTS_FILE]
            backup_msg = "📤 BACKUP\n━━━━━━━━━━━━━━━━━━━━━━━\n"
            for file in backup_files:
                if os.path.exists(file):
                    backup_name = f"{file}.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
                    try:
                        shutil.copy2(file, backup_name)
                        backup_msg += f"✅ {file} → {backup_name}\n"
                    except:
                        backup_msg += f"❌ Failed {file}\n"
                else:
                    backup_msg += f"⚠️ {file} not found\n"
            backup_msg += "━━━━━━━━━━━━━━━━━━━━━━━"
            await query.edit_message_text(backup_msg)

    # =========================================================
    #  👑 OWNER TEXT HANDLER
    # =========================================================
    async def handle_owner_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        if not bot_data.is_owner(user_id):
            return
        
        text = update.message.text.strip()
        action = context.user_data.get('owner_action')
        
        if action == 'add_balance':
            parts = text.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                target_user_id = parts[0]
                amount = int(parts[1])
                if amount <= 0:
                    await update.message.reply_text("❌ Amount > 0")
                    return
                bot_data.add_balance(target_user_id, amount)
                new_balance = bot_data.get_balance(target_user_id)
                await update.message.reply_text(f"✅ Balance Added\n━━━━━━━━━━━━━━━━━━━━━━━\n👤 {target_user_id}\n💰 +₹{amount}\n💳 {new_balance}\n━━━━━━━━━━━━━━━━━━━━━━━")
                try:
                    await context.bot.send_message(chat_id=int(target_user_id), text=f"💰 +₹{amount} Added\n💳 {new_balance}")
                except: pass
                context.user_data['owner_action'] = None
            else:
                await update.message.reply_text("❌ user_id amount")
            return

        if action == 'add_admin':
            parts = text.split()
            if parts:
                admin_id = parts[0]
                limit = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 100
                if bot_data.add_admin(admin_id, limit):
                    await update.message.reply_text(f"✅ Admin {admin_id} added (Limit:{limit})")
                else:
                    await update.message.reply_text(f"❌ Admin {admin_id} exists or is Owner")
                context.user_data['owner_action'] = None
            else:
                await update.message.reply_text("❌ user_id limit")

        elif action == 'remove_admin':
            if text.isdigit() and context.user_data.get('admin_list'):
                idx = int(text) - 1
                admin_list = context.user_data['admin_list']
                if 0 <= idx < len(admin_list):
                    admin_id = admin_list[idx]
                    if bot_data.remove_admin(admin_id):
                        await update.message.reply_text(f"✅ Admin {admin_id} removed")
                    else:
                        await update.message.reply_text(f"❌ Failed")
                else:
                    await update.message.reply_text("❌ Invalid")
                context.user_data['owner_action'] = None
                context.user_data['admin_list'] = None
            else:
                await update.message.reply_text("❌ Send number")

        elif action == 'set_limit':
            parts = text.split()
            if len(parts) == 2 and parts[0].isdigit() and parts[1].isdigit():
                admin_id = parts[0]
                limit = int(parts[1])
                if bot_data.set_admin_limit(admin_id, limit):
                    await update.message.reply_text(f"✅ Limit set to {limit} for {admin_id}")
                else:
                    await update.message.reply_text(f"❌ Admin {admin_id} not found")
                context.user_data['owner_action'] = None
            else:
                await update.message.reply_text("❌ user_id limit")

        elif action == 'set_cost':
            try:
                cost = float(text)
                if cost < 0:
                    await update.message.reply_text("❌ Cannot be negative")
                    return
                bot_data.set_cost(cost)
                await update.message.reply_text(f"✅ Cost set to ₹{cost:.2f}\n📌 Referral reward updated to ₹{2*cost:.2f}")
                context.user_data['owner_action'] = None
            except:
                await update.message.reply_text("❌ Send valid number")

        elif action == 'broadcast':
            users = bot_data.data['users']
            sent = 0
            failed = 0
            await update.message.reply_text(f"📢 Broadcast to {len(users)} users...")
            for user_id_str in users:
                try:
                    await context.bot.send_message(chat_id=int(user_id_str), text=f"📢 BROADCAST\n\n{text}")
                    sent += 1
                    await asyncio.sleep(0.05)
                except:
                    failed += 1
            await update.message.reply_text(f"✅ Sent : {sent} | ❌ Failed : {failed}")
            context.user_data['owner_action'] = None

    # =========================================================
    #  🚀 START WITH REFERRAL
    # =========================================================
    async def start_with_ref(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = update.effective_user.id
        username = update.effective_user.username or "Unknown"
        
        user = bot_data.get_user(user_id)
        user["username"] = username
        bot_data.save()
        
        args = context.args
        if args and args[0].startswith("ref_"):
            try:
                ref_id = args[0].replace("ref_", "")
                if ref_id.isdigit() and int(ref_id) != user_id:
                    result, msg = bot_data.add_referral(user_id, int(ref_id))
                    if result:
                        try:
                            await context.bot.send_message(chat_id=int(ref_id), text=f"✅ +{bot_data.get_refer_reward()} coins added (2 followers worth!)")
                        except: pass
            except: pass
        
        balance = bot_data.get_balance(user_id)
        
        welcome_msg = (
            f"🌟 Welcome to Craftland Follower\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n"
            f"💰 Balance : {balance}\n"
            f"🆔 ID : {user_id}\n"
            f"━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"🔽 Use the buttons below"
        )
        
        await update.message.reply_text(welcome_msg, reply_markup=get_main_keyboard(user_id))

# =========================================================
#  🚀 RUN TELEGRAM BOT IN BACKGROUND
# =========================================================
def run_telegram_bot():
    global MAIN_BOT_URL
    print("🚀 Starting Telegram Bot...")
    load_accounts_for_telegram()
    load_used_accounts()
    bot_data.save()
    
    # 🔥 Bot start hote hi URL detect karo
    MAIN_BOT_URL = get_render_url()
    
    if MAIN_BOT_URL:
        print(f"🌐 Main Bot URL detected: {MAIN_BOT_URL}")
    else:
        print("⚠️ RENDER_EXTERNAL_URL not found. Running without URL.")
    
    app_bot = Application.builder().token(BOT_TOKEN).build()
    
    app_bot.add_handler(CommandHandler("start", start_with_ref))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
    app_bot.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app_bot.add_handler(CallbackQueryHandler(handle_callback))
    app_bot.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_owner_text))
    
    print(f"✅ Bot Running!")
    print(f"👑 Owner: {OWNER_ID}")
    print(f"👥 Admins: {len(bot_data.data['admins'])}")
    print(f"💰 Cost: ₹{bot_data.get_cost(0):.2f}")
    print(f"📦 Accounts: {len(accounts_set)}")
    print(f"🔗 URL: {MAIN_BOT_URL}")
    
    app_bot.run_polling(allowed_updates=Update.ALL_TYPES)

# =========================================================
#  🌐 FLASK API ENDPOINTS
# =========================================================

@app.route('/')
def home():
    return "✅ Craftland Follower Bot is Running!"

@app.route('/api/products', methods=['POST'])
def api_products():
    try:
        data = request.get_json()
        api_key = data.get('api_key')
        
        if not api_key:
            return jsonify({"status": "error", "error": "missing_api_key", "message": "API Key required"})
        
        # 🔥 DEBUG: Print for checking
        print(f"🔑 Checking API Key: {api_key}")
        
        # 🔥 Validate API Key - REAL CHECK
        valid = False
        user_id = None
        for uid, user_data in bot_data.data["users"].items():
            if user_data.get("api_key") == api_key:
                valid = True
                user_id = uid
                break
        
        if not valid:
            return jsonify({"status": "error", "error": "invalid_api_key", "message": "Invalid API Key"})
        
        # Check balance
        balance = bot_data.get_balance_raw(int(user_id))
        
        products = [
            {"id": 1, "name": "20 Followers", "followers": 20, "price": 20, "status": "available" if balance >= 2 else "sold_out"},
            {"id": 2, "name": "40 Followers", "followers": 40, "price": 40, "status": "available" if balance >= 4 else "sold_out"},
            {"id": 3, "name": "60 Followers", "followers": 60, "price": 60, "status": "available" if balance >= 6 else "sold_out"},
            {"id": 4, "name": "80 Followers", "followers": 80, "price": 80, "status": "available" if balance >= 8 else "sold_out"},
            {"id": 5, "name": "100 Followers", "followers": 100, "price": 100, "status": "available" if balance >= 10 else "sold_out"},
        ]
        
        return jsonify({
            "status": "success",
            "role": "Reseller",
            "balance": balance,
            "products": products
        })
    except Exception as e:
        return jsonify({"status": "error", "error": "server_error", "message": str(e)})

@app.route('/api/follow', methods=['POST'])
def api_follow():
    try:
        data = request.get_json()
        api_key = data.get('api_key')
        product_id = data.get('product_id')
        target_id = data.get('target_id')
        user_id = data.get('user_id')
        
        if not api_key or not product_id or not target_id:
            return jsonify({"status": "error", "error": "missing_fields", "message": "Missing required fields"})
        
        # 🔥 Validate API Key - REAL CHECK
        valid = False
        bot_user_id = None
        for uid, user_data in bot_data.data["users"].items():
            if user_data.get("api_key") == api_key:
                valid = True
                bot_user_id = uid
                break
        
        if not valid:
            return jsonify({"status": "error", "error": "invalid_api_key", "message": "Invalid API Key"})
        
        # Check balance
        balance = bot_data.get_balance_raw(int(bot_user_id))
        cost_per_follow = bot_data.get_cost(int(bot_user_id))
        
        # Product costs (followers needed)
        product_costs = {1: 2, 2: 4, 3: 6, 4: 8, 5: 10}
        required_balance = product_costs.get(product_id, 0)
        
        if balance < required_balance:
            return jsonify({
                "status": "error",
                "error": "insufficient_balance",
                "message": "Sold Out - Insufficient balance",
                "balance": balance,
                "needed": required_balance
            })
        
        # Get accounts and follow
        target_str = str(target_id)
        used = used_accounts.get(target_str, set())
        available = [acc for acc in accounts_set if acc[0] not in used]
        
        followers_needed = product_costs.get(product_id, 2)
        if len(available) < followers_needed:
            return jsonify({
                "status": "error",
                "error": "no_accounts",
                "message": "Sold Out - No accounts available",
                "available": len(available)
            })
        
        # 🔥 Follow process
        chosen = list(available)[:followers_needed]
        
        def process_follow():
            try:
                bot_follower = FollowerBot(
                    telegram_mode=False,
                    user_id=int(bot_user_id),
                    bot_data=bot_data
                )
                for uid, pwd in chosen:
                    bot_follower.uid_pass_list.append({'uid': uid, 'password': pwd})
                bot_follower.run(int(target_id), followers_needed)
                
                # Update used accounts
                used_for_this = set()
                for uid, pwd in chosen:
                    used_for_this.add(uid)
                if target_str not in used_accounts:
                    used_accounts[target_str] = set()
                used_accounts[target_str].update(used_for_this)
                save_used_accounts()
            except Exception as e:
                print(f"Follow process error: {e}")
        
        thread = threading.Thread(target=process_follow, daemon=True)
        thread.start()
        
        return jsonify({
            "status": "success",
            "followers_added": followers_needed,
            "cost": required_balance * cost_per_follow,
            "balance_remaining": balance - required_balance
        })
    except Exception as e:
        return jsonify({"status": "error", "error": "server_error", "message": str(e)})

# =========================================================
#  🚀 MAIN - Start Flask + Telegram
# =========================================================
if __name__ == "__main__":
    # Start Telegram bot in background thread
    telegram_thread = threading.Thread(target=run_telegram_bot, daemon=True)
    telegram_thread.start()
    
    # Run Flask app
    print("🌐 Starting Flask server...")
    app.run(host='0.0.0.0', port=10000)