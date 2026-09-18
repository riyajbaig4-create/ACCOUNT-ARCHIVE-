import telebot
from telebot import types
import json
import os
import jwt
import requests
import asyncio
import threading
from datetime import datetime
from flask import Flask, request, jsonify
from Crypto.Cipher import AES
from Crypto.Util.Padding import pad
import binascii
import aiohttp
import like_pb2
import uid_generator_pb2
import visit_count_pb2
from google.protobuf.message import DecodeError
from collections import OrderedDict

# ==================== CONFIG ====================
BOT_TOKEN = "8995844626:AAEJP6lRMkYgB_1th_G8YRUthHYzYTc9Y3Y"
ADMIN_ID = 5674825926
VALID_API_KEYS = {"Anurag"}
daily_limit = 20
used_count = 0

TOKEN_FILES = {
    "BD": "token_bd.json",
    "IND": "token_ind.json",
    "BR": "token_br.json"
}

# ==================== FLASK APP ====================
app = Flask(__name__)

# ==================== TELEGRAM BOT ====================
bot = telebot.TeleBot(BOT_TOKEN)
user_sessions = {}


# ==================== TOKEN FUNCTIONS ====================

def check_expiry(token):
    try:
        decoded = jwt.decode(token, options={"verify_signature": False})
        exp = decoded.get("exp")
        if not exp:
            return -1
        return (datetime.fromtimestamp(exp) - datetime.now()).days
    except:
        return -1


def load_tokens(region):
    filename = TOKEN_FILES.get(region.upper())
    if not filename or not os.path.exists(filename):
        return []
    try:
        with open(filename, "r") as f:
            return json.load(f)
    except:
        return []


def save_tokens(region, tokens):
    filename = TOKEN_FILES.get(region.upper())
    if not filename:
        return False
    with open(filename, "w") as f:
        json.dump(tokens, f, indent=2)
    return True


def add_tokens(region, token_list):
    tokens = load_tokens(region)
    existing = {t["token"] for t in tokens}
    added = 0
    duplicate = 0
    invalid = 0
    new_tokens = []
    for token in token_list:
        token = token.strip().strip('"').strip("'").strip(",")
        if not token:
            continue
        if token in existing:
            duplicate += 1
            continue
        days = check_expiry(token)
        if days <= 0:
            invalid += 1
            continue
        new_tokens.append({"token": token})
        existing.add(token)
        added += 1
    final = tokens + new_tokens
    save_tokens(region, final)
    return {"added": added, "duplicate": duplicate, "invalid": invalid,
            "before": len(tokens), "after": len(final)}


def update_tokens(region, token_list):
    old_tokens = load_tokens(region)
    old_count = len(old_tokens)
    seen = set()
    new_tokens = []
    duplicate = 0
    invalid = 0
    for token in token_list:
        token = token.strip().strip('"').strip("'").strip(",")
        if not token:
            continue
        if token in seen:
            duplicate += 1
            continue
        days = check_expiry(token)
        if days <= 0:
            invalid += 1
            continue
        new_tokens.append({"token": token})
        seen.add(token)
    save_tokens(region, new_tokens)
    return {"removed": old_count, "added": len(new_tokens),
            "duplicate": duplicate, "invalid": invalid,
            "before": old_count, "after": len(new_tokens)}


def clean_expired(region):
    tokens = load_tokens(region)
    valid = [t for t in tokens if check_expiry(t["token"]) > 0]
    removed = len(tokens) - len(valid)
    save_tokens(region, valid)
    return removed, len(valid)


def get_status():
    report = {}
    for region in TOKEN_FILES:
        tokens = load_tokens(region)
        valid = sum(1 for t in tokens if check_expiry(t["token"]) > 0)
        expiring = sum(1 for t in tokens if 0 < check_expiry(t["token"]) < 7)
        expired = len(tokens) - valid
        report[region] = {"total": len(tokens), "valid": valid,
                          "expiring_soon": expiring, "expired": expired}
    return report


def parse_token_file(content):
    tokens = []
    content = content.strip()
    try:
        data = json.loads(content)
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and "token" in item:
                    tokens.append(item["token"])
                elif isinstance(item, str):
                    tokens.append(item)
        elif isinstance(data, dict) and "token" in data:
            tokens.append(data["token"])
        return tokens
    except:
        pass
    for line in content.split("\n"):
        line = line.strip().strip('"').strip("'").strip(",")
        if line and len(line) > 50 and line.startswith("eyJ"):
            tokens.append(line)
    return tokens


# ==================== ENCRYPTION / PROTOBUF ====================

def encrypt_message(plaintext):
    try:
        key = b'Yg&tc%DEuh6%Zc^8'
        iv = b'6oyZDr22E3ychjM%'
        cipher = AES.new(key, AES.MODE_CBC, iv)
        padded_message = pad(plaintext, AES.block_size)
        encrypted_message = cipher.encrypt(padded_message)
        return binascii.hexlify(encrypted_message).decode('utf-8')
    except Exception as e:
        print(f"Error encrypting: {e}")
        return None


def create_protobuf_message(user_id, region):
    try:
        message = like_pb2.like()
        message.uid = int(user_id)
        message.region = region
        return message.SerializeToString()
    except Exception as e:
        print(f"Error protobuf: {e}")
        return None


async def send_request(encrypted_uid, token, url):
    try:
        edata = bytes.fromhex(encrypted_uid)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB54"
        }
        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=edata, headers=headers) as response:
                return await response.text()
    except Exception as e:
        print(f"Error send_request: {e}")
        return None


async def send_multiple_requests(uid, region, url):
    try:
        protobuf_message = create_protobuf_message(uid, region)
        if protobuf_message is None:
            return None
        encrypted_uid = encrypt_message(protobuf_message)
        if encrypted_uid is None:
            return None
        tokens = load_tokens(region)
        if not tokens:
            return None
        tasks = []
        for i in range(100):
            token = tokens[i % len(tokens)]["token"]
            tasks.append(send_request(encrypted_uid, token, url))
        results = await asyncio.gather(*tasks, return_exceptions=True)
        return results
    except Exception as e:
        print(f"Error send_multiple: {e}")
        return None


def create_protobuf(uid):
    try:
        message = uid_generator_pb2.uid_generator()
        message.saturn_ = int(uid)
        message.garena = 1
        return message.SerializeToString()
    except Exception as e:
        print(f"Error uid protobuf: {e}")
        return None


def enc(uid):
    protobuf_data = create_protobuf(uid)
    if protobuf_data is None:
        return None
    return encrypt_message(protobuf_data)


def make_request(encrypt, region, token):
    try:
        if region == "IND":
            url = "https://client.ind.freefiremobile.com/GetPlayerPersonalShow"
        elif region in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/GetPlayerPersonalShow"
        else:
            url = "https://clientbp.ggpolarbear.com/GetPlayerPersonalShow"
        edata = bytes.fromhex(encrypt)
        headers = {
            "User-Agent": "Dalvik/2.1.0 (Linux; U; Android 9; ASUS_Z01QD Build/PI)",
            "Connection": "Keep-Alive",
            "Accept-Encoding": "gzip",
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/x-www-form-urlencoded",
            "Expect": "100-continue",
            "X-Unity-Version": "2018.4.11f1",
            "X-GA": "v1 1",
            "ReleaseVersion": "OB54"
        }
        response = requests.post(url, data=edata, headers=headers, verify=False)
        binary = response.content
        decoded = visit_count_pb2.Info()
        decoded.ParseFromString(binary)
        return decoded
    except DecodeError as e:
        print(f"DecodeError: {e}")
        return None
    except Exception as e:
        print(f"Error make_request: {e}")
        return None


# ==================== FLASK API ROUTES ====================

@app.route('/like', methods=['GET'])
def handle_requests():
    global used_count

    api_key = request.args.get("key")
    if api_key not in VALID_API_KEYS:
        result = OrderedDict([("error", "Invalid or missing API key"), ("status", 3)])
        return app.response_class(
            response=json.dumps(result, separators=(',', ':')),
            status=401, mimetype='application/json'
        )

    uid = request.args.get("uid")
    region = request.args.get("region", "").upper()
    if not uid or not region:
        return {"error": "UID and region are required"}, 400

    try:
        tokens = load_tokens(region)
        if not tokens:
            raise Exception("Failed to load tokens.")
        token = tokens[0]['token']
        encrypted_uid = enc(uid)
        if encrypted_uid is None:
            raise Exception("Encryption of UID failed.")
        before = make_request(encrypted_uid, region, token)
        if before is None:
            raise Exception("Failed to get initial info.")
        before_like = before.AccountInfo.Likes

        if region == "IND":
            url = "https://client.ind.freefiremobile.com/LikeProfile"
        elif region in {"BR", "US", "SAC", "NA"}:
            url = "https://client.us.freefiremobile.com/LikeProfile"
        else:
            url = "https://clientbp.ggpolarbear.com/LikeProfile"

        asyncio.run(send_multiple_requests(uid, region, url))

        after = make_request(encrypted_uid, region, token)
        if after is None:
            raise Exception("Failed to get final info.")
        after_like = after.AccountInfo.Likes
        like_given = after_like - before_like
        status = 1 if like_given > 0 else 2

        if status == 1:
            used_count += 1

        remaining = max(daily_limit - used_count, 0)

        result = OrderedDict([
            ("LikesGivenByAPI", like_given),
            ("LikesafterCommand", after_like),
            ("LikesbeforeCommand", before_like),
            ("PlayerNickname", after.AccountInfo.PlayerNickname),
            ("Level", after.AccountInfo.Levels),
            ("Region", after.AccountInfo.PlayerRegion),
            ("UID", after.AccountInfo.UID),
            ("status", status),
            ("daily_limit", daily_limit),
            ("used", used_count),
            ("remaining", remaining)
        ])

        return app.response_class(
            response=json.dumps(result, separators=(',', ':')),
            status=200, mimetype='application/json'
        )

    except Exception as e:
        print(f"Error: {e}")
        return {"error": str(e)}, 500


@app.route('/remain', methods=['GET'])
def remain_info():
    global used_count
    remaining = max(daily_limit - used_count, 0)
    return jsonify({
        "daily_limit": daily_limit,
        "remaining": remaining,
        "used": used_count,
        "reset_info": "4:00 AM IST"
    })


@app.route('/', methods=['GET'])
def home():
    return jsonify({"status": "ok", "bot": "running"})


# ==================== TELEGRAM KEYBOARDS ====================

def main_menu():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🌍 BD", callback_data="region_BD"),
        types.InlineKeyboardButton("🇮🇳 IND", callback_data="region_IND"),
        types.InlineKeyboardButton("🇧🇷 BR", callback_data="region_BR")
    )
    kb.row(
        types.InlineKeyboardButton("📊 Status", callback_data="status"),
        types.InlineKeyboardButton("🧹 Clean All", callback_data="clean_menu")
    )
    return kb


def region_menu(region):
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton(f"➕ ADD ({region})", callback_data=f"add_{region}"),
        types.InlineKeyboardButton(f"🔄 UPDATE ({region})", callback_data=f"update_{region}")
    )
    kb.row(
        types.InlineKeyboardButton("📊 Status", callback_data=f"status_{region}"),
        types.InlineKeyboardButton("🧹 Clean", callback_data=f"clean_{region}")
    )
    kb.row(types.InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb


def back_menu():
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb


def clean_menu():
    kb = types.InlineKeyboardMarkup()
    kb.row(
        types.InlineKeyboardButton("🧹 BD", callback_data="clean_BD"),
        types.InlineKeyboardButton("🧹 IND", callback_data="clean_IND"),
        types.InlineKeyboardButton("🧹 BR", callback_data="clean_BR")
    )
    kb.row(types.InlineKeyboardButton("🔙 Main Menu", callback_data="main_menu"))
    return kb


# ==================== TELEGRAM COMMANDS ====================

@bot.message_handler(commands=['start'])
def start(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        bot.send_message(chat_id, "❌ Aap admin nahi hain")
        return
    bot.send_message(chat_id,
        "🤖 <b>FF Token Manager Bot</b>\n\nRegion select karein:",
        parse_mode='HTML',
        reply_markup=main_menu()
    )


@bot.message_handler(commands=['status'])
def status_cmd(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        return
    report = get_status()
    txt = "📊 <b>Status</b>\n\n"
    for r, s in report.items():
        txt += f"<b>{r}</b>: {s['total']} total | ✅ {s['valid']} | ❌ {s['expired']}\n"
    bot.send_message(chat_id, txt, parse_mode='HTML')


@bot.callback_query_handler(func=lambda call: True)
def callback(call):
    chat_id = call.message.chat.id
    message_id = call.message.message_id
    data = call.data

    if chat_id != ADMIN_ID:
        bot.answer_callback_query(call.id, "❌ Aap admin nahi hain")
        return

    bot.answer_callback_query(call.id)

    if data.startswith("region_"):
        region = data.split("_")[1]
        user_sessions[chat_id] = {"region": region}
        tokens = load_tokens(region)
        bot.edit_message_text(
            f"🌍 <b>{region}</b>\n\n📊 Current tokens: <b>{len(tokens)}</b>\n\nKya karna hai?",
            chat_id, message_id, parse_mode='HTML',
            reply_markup=region_menu(region)
        )

    elif data.startswith("add_"):
        region = data.split("_")[1]
        user_sessions[chat_id] = {"region": region, "action": "waiting_file_add"}
        tokens = load_tokens(region)
        bot.edit_message_text(
            f"➕ <b>ADD Mode ({region})</b>\n\n"
            f"📊 Current: <b>{len(tokens)}</b> tokens\n\n"
            f"Ab file bhejein. Naye tokens <b>purane ke saath judenge</b>.\n"
            f"Same token dobaara add nahi hoga.\n\n"
            f"<b>File format:</b>\n"
            f"• JSON: <code>[{{\"token\":\"eyJ...\"}}]</code>\n"
            f"• TXT: Har line ek token",
            chat_id, message_id, parse_mode='HTML',
            reply_markup=back_menu()
        )

    elif data.startswith("update_"):
        region = data.split("_")[1]
        user_sessions[chat_id] = {"region": region, "action": "waiting_file_update"}
        tokens = load_tokens(region)
        bot.edit_message_text(
            f"🔄 <b>UPDATE Mode ({region})</b>\n\n"
            f"📊 Current: <b>{len(tokens)}</b> tokens\n\n"
            f"⚠️ <b>Warning:</b> Purane saare tokens <b>hat jayenge</b>!\n"
            f"Sirf naye tokens rahenge.\n\n"
            f"Ab file bhejein.",
            chat_id, message_id, parse_mode='HTML',
            reply_markup=back_menu()
        )

    elif data == "status":
        report = get_status()
        text = "📊 <b>All Regions</b>\n\n"
        for r, s in report.items():
            text += f"<b>🌍 {r}</b>\n"
            text += f"  Total: {s['total']} | ✅ {s['valid']} | ⚠️ {s['expiring_soon']} | ❌ {s['expired']}\n\n"
        bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML', reply_markup=back_menu())

    elif data.startswith("status_"):
        region = data.split("_")[1]
        tokens = load_tokens(region)
        valid = sum(1 for t in tokens if check_expiry(t["token"]) > 0)
        expiring = sum(1 for t in tokens if 0 < check_expiry(t["token"]) < 7)
        expired = len(tokens) - valid
        text = (
            f"📊 <b>{region} Status</b>\n\n"
            f"Total: <b>{len(tokens)}</b>\n"
            f"✅ Valid: <b>{valid}</b>\n"
            f"⚠️ Expiring: <b>{expiring}</b>\n"
            f"❌ Expired: <b>{expired}</b>"
        )
        bot.edit_message_text(text, chat_id, message_id, parse_mode='HTML',
                              reply_markup=region_menu(region))

    elif data == "clean_menu":
        bot.edit_message_text("🧹 Region select karein:", chat_id, message_id,
                              reply_markup=clean_menu())

    elif data.startswith("clean_"):
        region = data.split("_")[1]
        removed, left = clean_expired(region)
        bot.send_message(chat_id,
            f"✅ <b>{region} Cleaned</b>\n\nRemoved: <b>{removed}</b>\nRemaining: <b>{left}</b>",
            parse_mode='HTML', reply_markup=region_menu(region)
        )

    elif data == "main_menu":
        bot.edit_message_text(
            "🤖 <b>FF Token Manager Bot</b>\n\nRegion select karein:",
            chat_id, message_id, parse_mode='HTML',
            reply_markup=main_menu()
        )


@bot.message_handler(content_types=['document'])
def handle_document(message):
    chat_id = message.chat.id
    if chat_id != ADMIN_ID:
        return

    session = user_sessions.get(chat_id, {})
    region = session.get("region")
    action = session.get("action")

    if not region or not action:
        bot.send_message(chat_id, "❌ Pehle region select karein aur ADD/UPDATE button dabayein")
        return

    document = message.document
    if document.file_size > 5 * 1024 * 1024:
        bot.send_message(chat_id, "❌ File bahut badi hai (max 5MB)")
        return

    bot.send_message(chat_id, "📥 File process ho rahi hai...")

    try:
        file_info = bot.get_file(document.file_id)
        file_url = f"https://api.telegram.org/file/bot{BOT_TOKEN}/{file_info.file_path}"
        r = requests.get(file_url, timeout=30)
        content = r.text
    except Exception as e:
        bot.send_message(chat_id, f"❌ File download fail: {e}")
        return

    tokens = parse_token_file(content)
    if not tokens:
        bot.send_message(chat_id, "❌ File mein valid token nahi mila")
        return

    if action == "waiting_file_add":
        result = add_tokens(region, tokens)
        bot.send_message(chat_id,
            f"✅ <b>ADD Complete ({region})</b>\n\n"
            f"📄 File: <code>{document.file_name}</code>\n\n"
            f"✅ Added: <b>{result['added']}</b>\n"
            f"⏭️ Duplicate (skip): <b>{result['duplicate']}</b>\n"
            f"❌ Invalid/Expired: <b>{result['invalid']}</b>\n\n"
            f"📊 Before: <b>{result['before']}</b>\n"
            f"📊 After: <b>{result['after']}</b>",
            parse_mode='HTML', reply_markup=region_menu(region)
        )

    elif action == "waiting_file_update":
        result = update_tokens(region, tokens)
        bot.send_message(chat_id,
            f"🔄 <b>UPDATE Complete ({region})</b>\n\n"
            f"📄 File: <code>{document.file_name}</code>\n\n"
            f"🗑️ Removed (old): <b>{result['removed']}</b>\n"
            f"✅ Added (new): <b>{result['added']}</b>\n"
            f"⏭️ Duplicate (skip): <b>{result['duplicate']}</b>\n"
            f"❌ Invalid/Expired: <b>{result['invalid']}</b>\n\n"
            f"📊 Before: <b>{result['before']}</b>\n"
            f"📊 After: <b>{result['after']}</b>",
            parse_mode='HTML', reply_markup=region_menu(region)
        )

    user_sessions[chat_id] = {"region": region}


# ==================== RUN BOTH ====================

def run_flask():
    app.run(host='0.0.0.0', port=5000, debug=False, use_reloader=False)


if __name__ == "__main__":
    # Flask alag thread mein chalao
    flask_thread = threading.Thread(target=run_flask, daemon=True)
    flask_thread.start()
    print("✅ Flask API chalu: http://0.0.0.0:5000")

    # Telegram bot main thread mein chalao
    print("🤖 Telegram Bot chalu...")
    print("   Telegram par /start bhejo")
    bot.infinity_polling()