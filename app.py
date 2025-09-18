# app.py
import os
import json
import logging
import random
import re
from datetime import datetime
from flask import Flask, request, jsonify
import requests

try:
    from openai import OpenAI
except Exception:
    OpenAI = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("estate_deli_bot")

# -------------------------------
# ENV CONFIG
# -------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # e.g. whatsapp:+14155238886
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")

DATA_DIR = os.environ.get("DATA_DIR", "data")
LOG_FILE = os.path.join(DATA_DIR, "conversations.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")
CAKES_FILE = os.path.join(DATA_DIR, "cakes.json")
REVIEWS_FILE = os.path.join(DATA_DIR, "reviews.json")
os.makedirs(DATA_DIR, exist_ok=True)

client = None
if OPENAI_API_KEY and OpenAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception:
        client = None

# -------------------------------
# CONSTANTS
# -------------------------------
TABLES = 6
SEATS_PER_TABLE = 4
TOTAL_SEATS = TABLES * SEATS_PER_TABLE

user_state = {}
USER_STATE_TIMEOUT = 300

CAKE_FLAVOURS = [
    "Chocolate", "Vanilla", "Strawberry", "Red Velvet", "Black Forest",
    "Butterscotch", "Pineapple", "Mango", "Coffee", "Caramel",
    "Lemon", "Fruit Cake", "Truffle", "Oreo", "Cheesecake"
]

MENU_DATA = {
    "coffee": [
        "☕ Espresso - ₹120",
        "☕ Americano - ₹140", 
        "☕ Cappuccino - ₹160",
        "☕ Latte - ₹180",
        "☕ Mocha - ₹200",
        "☕ Macchiato - ₹170",
        "☕ Flat White - ₹190"
    ],
    "matcha": [
        "🍵 Matcha Latte - ₹220",
        "🍵 Iced Matcha - ₹240", 
        "🍵 Matcha Smoothie - ₹260",
        "🍵 Matcha Frappe - ₹280"
    ],
    "signature hot beverages": [
        "🔥 Spiced Chai Latte - ₹180",
        "🔥 Golden Turmeric Latte - ₹200",
        "🔥 Hot Chocolate Supreme - ₹220",
        "🔥 Masala Chai - ₹150",
        "🔥 Green Tea - ₹120"
    ],
    "signature iced beverages": [
        "🧊 Iced Vanilla Latte - ₹200",
        "🧊 Cold Brew Float - ₹240",
        "🧊 Frappuccino Special - ₹280",
        "🧊 Iced Tea - ₹140",
        "🧊 Cold Coffee - ₹180"
    ],
    "mocktails": [
        "🍹 Virgin Mojito - ₹180",
        "🍹 Fruit Punch - ₹160", 
        "🍹 Lemon Mint Cooler - ₹140",
        "🍹 Blue Lagoon - ₹200",
        "🍹 Pina Colada - ₹220"
    ],
    "snacks": [
        "🍰 Chocolate Brownie - ₹150",
        "🧁 Cupcakes - ₹80",
        "🥪 Club Sandwich - ₹200", 
        "🍕 Mini Pizza - ₹180",
        "🥗 Caesar Salad - ₹220"
    ],
    "desserts": [
        "🍰 Tiramisu - ₹250",
        "🍮 Crème Brûlée - ₹300",
        "🍪 Cookies - ₹100",
        "🧁 Pastries - ₹120",
        "🍨 Ice Cream - ₹150"
    ]
}

# -------------------------------
# HELPERS
# -------------------------------
def normalize_phone(p):
    if not p:
        return p
    return p.replace("whatsapp:", "").strip()

def load_data(path):
    if not os.path.exists(path): return []
    try:
        with open(path, "r", encoding="utf-8") as f: return json.load(f)
    except: return []

def save_data(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def log_interaction(sender, message, reply):
    entry = {"sender": sender, "message": message, "reply": reply, "timestamp": datetime.now().isoformat()}
    logs = load_data(LOG_FILE)
    logs.append(entry)
    save_data(LOG_FILE, logs)

def clean_expired_states():
    now = datetime.now()
    for u, s in list(user_state.items()):
        try:
            ts = datetime.fromisoformat(s.get("timestamp"))
            if (now - ts).seconds > USER_STATE_TIMEOUT:
                user_state.pop(u, None)
        except:
            user_state.pop(u, None)

# -------------------------------
# REPORT for OWNER
# -------------------------------
def generate_report():
    bookings = load_data(BOOKINGS_FILE)
    cakes = load_data(CAKES_FILE)
    reviews = load_data(REVIEWS_FILE)
    today = datetime.now().date()

    tb = sum(1 for b in bookings if "timestamp" in b and datetime.fromisoformat(b["timestamp"]).date() == today)
    tc = sum(1 for c in cakes if "timestamp" in c and datetime.fromisoformat(c["timestamp"]).date() == today)
    tr = sum(1 for r in reviews if "timestamp" in r and datetime.fromisoformat(r["timestamp"]).date() == today)

    return (
        f"📊 Daily Report - {today.strftime('%d %B %Y')}\n\n"
        f"🪑 Bookings Today: {tb}\n"
        f"🎂 Cake Orders Today: {tc}\n"
        f"⭐ Reviews Today: {tr}\n\n"
        f"📈 Total Stats:\n"
        f"🪑 Total Bookings: {len(bookings)}\n"
        f"🎂 Total Cake Orders: {len(cakes)}\n"
        f"⭐ Total Reviews: {len(reviews)}\n"
    )

# -------------------------------
# TWILIO SEND
# -------------------------------
def send_twilio_message(to_phone, msg):
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = {"From": TWILIO_WHATSAPP_NUMBER, "To": f"whatsapp:{to_phone}", "Body": msg}
    try:
        r = requests.post(url, data=payload, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        if r.status_code in (200,201): return True
        logger.error(f"Twilio error: {r.text}")
        return False
    except Exception as e:
        logger.error(f"Twilio exception: {e}")
        return False

# -------------------------------
# MAIN MENU
# -------------------------------
def main_menu():
    return (
        "👋 Welcome to The Estate Deli!\n\n"
        "How can I help you today?\n\n"
        "1️⃣ View Menu 📋\n"
        "2️⃣ Order Cake 🎂\n"
        "3️⃣ Book Table 🪑\n"
        "4️⃣ Opening Hours 🕘\n"
        "5️⃣ Location 📍\n"
        "6️⃣ Leave Review ⭐\n\n"
        "👉 Reply with the number or option name"
    )

# -------------------------------
# WEBHOOK
# -------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        clean_expired_states()
        raw = request.get_data(as_text=True)
        logger.info(f"RAW WEBHOOK (first 1000 chars): {raw[:1000]}")

        sender, message = None, None
        if request.form:
            sender = normalize_phone(request.form.get("From"))
            message = request.form.get("Body")

        if not message:
            data = request.get_json(silent=True) or {}
            message = data.get("text") or data.get("message")
            sender = sender or data.get("from") or data.get("sender")

        if not sender or not message:
            return jsonify({"status":"ok"}), 200

        sender = sender.strip()
        text = message.strip()
        lower = text.lower()

        # --- OWNER COMMANDS ---
        owner_norm = normalize_phone(OWNER_NUMBER)
        if sender == owner_norm:
            if lower.startswith("report"):
                rpt = generate_report()
                send_twilio_message(sender, rpt)
                return jsonify({"status":"success"}), 200
            if lower.startswith("reviews"):
                reviews = load_data(REVIEWS_FILE)
                reply = f"📢 Total Reviews: {len(reviews)}"
                send_twilio_message(sender, reply)
                return jsonify({"status":"success"}), 200
        # ----------------------

        # Greeting activation
        if lower in ["hi", "hello", "hey", "start", "menu"]:
            reply = main_menu()
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        # Context awareness (flows handled here) …
        # (keep your cake / booking / menu context logic same as before)
        # ---- shortened here but would include the full flows you had ----

        reply = "🤖 Sorry, I didn’t get that. Type 'menu' to see options."
        send_twilio_message(sender, reply)
        return jsonify({"status":"success"}), 200

    except Exception as e:
        logger.exception(f"webhook error: {e}")
        return jsonify({"status":"error"}), 500

# -------------------------------
@app.route("/health")
def health():
    return jsonify({"status":"healthy"}), 200

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT",5000)))
