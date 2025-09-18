# app.py
import os
import json
import logging
import random
import re
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# Optional OpenAI client
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("estate_deli_bot")

# -----------------------
# Environment / files
# -----------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # must include "whatsapp:+..."
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
    except Exception as e:
        logger.warning(f"OpenAI init failed: {e}")

# Seating
TABLES = 6
SEATS_PER_TABLE = 4
TOTAL_SEATS = TABLES * SEATS_PER_TABLE

# In-memory user state
user_state = {}
USER_STATE_TIMEOUT = 300

# Data (enhanced)
CAKE_FLAVOURS = [
    "Chocolate", "Vanilla", "Strawberry", "Red Velvet", "Black Forest",
    "Butterscotch", "Pineapple", "Mango", "Coffee", "Caramel"
]

MENU_DATA = {
    "coffee": ["Espresso", "Americano", "Cappuccino", "Latte", "Mocha", "Flat White"],
    "matcha": ["Matcha Latte", "Iced Matcha", "Matcha Smoothie"],
    "signature hot beverages": ["Spiced Chai Latte", "Golden Turmeric Latte", "Hot Chocolate Supreme"],
    "signature iced beverages": ["Iced Vanilla Latte", "Cold Brew Float", "Frappuccino Special"],
    "mocktails": ["Virgin Mojito", "Fruit Punch", "Lemon Mint Cooler"],
    "desserts": ["Tiramisu", "Crème Brûlée", "Cookies", "Pastries", "Ice Cream"],
    "snacks": ["Chocolate Brownie", "Cupcakes", "Club Sandwich", "Mini Pizza", "Caesar Salad"]
}

PRICES = {
    "espresso": "₹120", "americano": "₹140", "cappuccino": "₹160", "latte": "₹180", "mocha": "₹200",
    "matcha latte": "₹220", "iced matcha": "₹240", "matcha smoothie": "₹260",
    "spiced chai latte": "₹180", "golden turmeric latte": "₹200", "hot chocolate supreme": "₹220",
    "iced vanilla latte": "₹200", "cold brew float": "₹240", "frappuccino special": "₹280",
    "virgin mojito": "₹180", "fruit punch": "₹160", "lemon mint cooler": "₹140",
    "tiramisu": "₹250", "crème brûlée": "₹300", "creme brulee": "₹300", "cookies": "₹100", "pastries": "₹120", "ice cream": "₹150",
    "chocolate brownie": "₹150", "cupcakes": "₹80", "club sandwich": "₹200", "mini pizza": "₹180", "caesar salad": "₹220"
}

# -----------------------
# Utilities
# -----------------------
def load_data(path):
    try:
        if not os.path.exists(path):
            return []
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"load_data error {e}")
        return []

def save_data(path, data):
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"save_data error {e}")
        return False

def log_interaction(sender, message, reply):
    entry = {"sender": sender, "message": message, "reply": reply, "timestamp": datetime.now().isoformat()}
    logs = load_data(LOG_FILE)
    logs.append(entry)
    save_data(LOG_FILE, logs)

def clean_expired_states():
    now = datetime.now()
    to_del = []
    for u, s in list(user_state.items()):
        try:
            ts = datetime.fromisoformat(s.get("timestamp"))
            if (now - ts).seconds > USER_STATE_TIMEOUT:
                to_del.append(u)
        except Exception:
            to_del.append(u)
    for u in to_del:
        user_state.pop(u, None)

def normalize_phone(p):
    if not p:
        return p
    return p.replace("whatsapp:", "").strip()

def simple_price_lookup(item_text):
    key = item_text.strip().lower()
    return PRICES.get(key)

# -----------------------
# Report generator (new)
# -----------------------
def generate_report():
    bookings = load_data(BOOKINGS_FILE)
    cakes = load_data(CAKES_FILE)
    reviews = load_data(REVIEWS_FILE)

    today = datetime.now().date()
    today_bookings = 0
    today_cakes = 0
    today_reviews = 0

    for b in bookings:
        try:
            if "timestamp" in b and datetime.fromisoformat(b["timestamp"]).date() == today:
                today_bookings += 1
        except Exception:
            continue

    for c in cakes:
        try:
            if "timestamp" in c and datetime.fromisoformat(c["timestamp"]).date() == today:
                today_cakes += 1
        except Exception:
            continue

    for r in reviews:
        try:
            if "timestamp" in r and datetime.fromisoformat(r["timestamp"]).date() == today:
                today_reviews += 1
        except Exception:
            continue

    return (
        f"📊 Daily Report - {today.strftime('%d %b %Y')}\n\n"
        f"🪑 Bookings Today: {today_bookings}\n"
        f"🎂 Cake Orders Today: {today_cakes}\n"
        f"⭐ Reviews Today: {today_reviews}\n\n"
        f"📈 Total Stats:\n"
        f"🪑 Total Bookings: {len(bookings)}\n"
        f"🎂 Total Cake Orders: {len(cakes)}\n"
        f"⭐ Total Reviews: {len(reviews)}"
    )

# -----------------------
# Twilio send
# -----------------------
def send_twilio_message(to_phone, message_text):
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER):
        logger.warning("Twilio not configured")
        return False
    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = {"From": TWILIO_WHATSAPP_NUMBER, "To": f"whatsapp:{to_phone}", "Body": message_text}
    try:
        resp = requests.post(url, data=payload, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
        if resp.status_code in (200,201):
            logger.info(f"✅ Sent to whatsapp:{to_phone}")
            return True
        else:
            logger.error(f"Twilio send failed: {resp.status_code} - {resp.text}")
            return False
    except Exception as e:
        logger.error(f"Twilio network error: {e}")
        return False

# -----------------------
# Flow handlers
# -----------------------
def start_main_menu():
    return (
        "👋 Welcome to The Estate Deli!\n\n"
        "1️⃣ View Menu 📋\n"
        "2️⃣ Order Cake 🎂\n"
        "3️⃣ Book Table 🪑\n"
        "4️⃣ Opening Hours 🕘\n"
        "5️⃣ Location 📍\n"
        "6️⃣ Leave Review ⭐\n\n"
        "Reply with a number or the option name."
    )

def menu_categories_text():
    cats = ", ".join([c.title() for c in MENU_DATA.keys()])
    return f"📋 Menu categories: {cats}\n\nReply with a category name to explore it or type 'back' to return."

def handle_menu_context(sender, message):
    st = user_state.get(sender, {})
    category = st.get("category")
    if not category:
        user_state.pop(sender, None)
        return "Sorry — menu context lost. Type 'menu' to see categories."
    msg_l = message.strip().lower()
    if msg_l in ["back", "menu", "exit", "cancel"]:
        user_state.pop(sender, None)
        return start_main_menu()
    items = MENU_DATA.get(category, [])
    for it in items:
        if it.lower() in msg_l:
            price = simple_price_lookup(it)
            price_str = f" — {price}" if price else ""
            return f"{it}{price_str}\n\nTo order, type 'order {it}' or type 'back' to go to categories."
    m = re.match(r"order\s+(.+)", msg_l)
    if m:
        item = m.group(1).strip()
        return f"✅ Got your order request for *{item.title()}* from {category.title()}. We'll contact you to confirm pickup/delivery."
    return f"You're viewing {category.title()} items. Reply with the name of the item to see price or type 'order <item>' to order. Type 'back' to exit."

def handle_cake_flow(sender, message):
    state = user_state.get(sender, {})
    step = state.get("step", 1)
    msg = message.strip()
    lower = msg.lower()
    if step == 1:
        flavour = None
        for f in CAKE_FLAVOURS:
            if f.lower() in lower:
                flavour = f
                break
        if not flavour:
            flavour = msg.title()
        state["flavour"] = flavour
        state["step"] = 2
        state["timestamp"] = datetime.now().isoformat()
        user_state[sender] = state
        return "🎂 Nice! Any custom message to put on the cake? (Type 'no' for none)"
    elif step == 2:
        if lower in ["no", "none", "nope", "na"]:
            state["custom"] = ""
        else:
            state["custom"] = msg
        state["step"] = 3
        state["timestamp"] = datetime.now().isoformat()
        user_state[sender] = state
        return "📅 What pickup date would you like? (e.g., Tomorrow, 25th Dec)"
    elif step == 3:
        state["date"] = msg
        state["step"] = 4
        state["timestamp"] = datetime.now().isoformat()
        user_state[sender] = state
        return "⏰ What time should we keep it ready? (e.g., 6 PM)"
    elif step == 4:
        state["time"] = msg
        order = {
            "customer": sender,
            "flavour": state.get("flavour"),
            "custom_message": state.get("custom",""),
            "date": state.get("date"),
            "time": state.get("time"),
            "timestamp": datetime.now().isoformat()
        }
        cakes = load_data(CAKES_FILE)
        cakes.append(order)
        save_data(CAKES_FILE, cakes)
        if OWNER_NUMBER:
            send_twilio_message(normalize_phone(OWNER_NUMBER), f"📢 New Cake Order!\nCustomer: {sender}\nFlavour: {order['flavour']}\nPickup: {order['date']} {order['time']}")
        user_state.pop(sender, None)
        return f"✅ Cake confirmed: {order['flavour']} on {order['date']} at {order['time']}. We'll see you then!"
    else:
        user_state.pop(sender, None)
        return "Something went wrong with the cake flow — please type '2' or 'cake' to start a new cake order."

def handle_booking_flow(sender, message):
    state = user_state.get(sender, {})
    step = state.get("step", 1)
    msg = message.strip()
    lower = msg.lower()
    if step == 1:
        num = extract_number(msg)
        if num and 1 <= num <= 50:
            state["people"] = num
            state["step"] = 2
            state["timestamp"] = datetime.now().isoformat()
            user_state[sender] = state
            return f"📅 Booking for {num} people. What date would you prefer? (e.g., Tomorrow, 25th Dec)"
        else:
            return "⚠️ Please reply with the number of people (e.g., '4' or '4 people')."
    elif step == 2:
        state["date"] = msg
        state["step"] = 3
        state["timestamp"] = datetime.now().isoformat()
        user_state[sender] = state
        return "⏰ What time would you like to book the table? (e.g., 7 PM)"
    elif step == 3:
        state["time"] = msg
        is_avail, available = check_table_availability(state["date"], state["time"], state["people"])
        if not is_avail:
            user_state.pop(sender, None)
            return f"⚠️ Sorry, not enough seats for {state['people']} at that time. Available seats: {available}. Please try a different time or date."
        booking = {
            "customer": sender,
            "people": state["people"],
            "date": state["date"],
            "time": state["time"],
            "timestamp": datetime.now().isoformat()
        }
        bookings = load_data(BOOKINGS_FILE)
        bookings.append(booking)
        save_data(BOOKINGS_FILE, bookings)
        if OWNER_NUMBER:
            send_twilio_message(normalize_phone(OWNER_NUMBER), f"📢 New Booking: {booking['people']} people on {booking['date']} at {booking['time']} (Customer: {sender})")
        user_state.pop(sender, None)
        return f"✅ Booking confirmed for {booking['people']} people on {booking['date']} at {booking['time']}. See you!"
    else:
        user_state.pop(sender, None)
        return "Booking flow had an issue — please try again by typing '3' or 'book table'."

def extract_number(text):
    m = re.search(r'\d+', text)
    return int(m.group()) if m else None

def check_table_availability(date, time, people):
    bookings = load_data(BOOKINGS_FILE)
    booked = 0
    for b in bookings:
        try:
            if b.get("date","").lower().strip() == date.lower().strip() and b.get("time","").lower().strip() == time.lower().strip():
                booked += b.get("people",0)
        except Exception:
            continue
    available = TOTAL_SEATS - booked
    return (available >= people), available

def save_review(sender, message_text):
    r = {"customer": sender, "review": message_text.strip(), "timestamp": datetime.now().isoformat()}
    reviews = load_data(REVIEWS_FILE)
    reviews.append(r)
    save_data(REVIEWS_FILE, reviews)
    if OWNER_NUMBER:
        send_twilio_message(normalize_phone(OWNER_NUMBER), f"📢 New Review from {sender}: {message_text[:200]}")
    return r

# -----------------------
# AI fallback - only last resort
# -----------------------
def get_ai_response(message):
    if not client:
        return "Sorry, I didn't understand. Type 'menu' to see options or 'help' for quick commands."
    try:
        system_prompt = "You are a concise assistant for a cafe. Keep answers short and push users to use the menu numbers."
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role":"system","content":system_prompt},{"role":"user","content":message}],
            max_tokens=120, temperature=0.2
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI error: {e}")
        return "Sorry, I couldn't process that. Type 'menu' to see options."

# -----------------------
# Webhook
# -----------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        clean_expired_states()
        raw = request.get_data(as_text=True)
        logger.info(f"RAW WEBHOOK (first 1000 chars): {raw[:1000]}")

        sender = None
        message = None

        # Twilio sends form data: From and Body
        if request.form:
            frm = request.form.get("From")
            body = request.form.get("Body")
            if frm:
                sender = normalize_phone(frm)
            if body:
                message = body.strip()

        # fallback: JSON providers
        if not message:
            data = request.get_json(silent=True) or {}
            if isinstance(data, dict):
                payload = data.get("payload") or {}
                if payload:
                    contact = payload.get("contact") or payload.get("sender") or {}
                    sender = sender or (contact.get("id") or contact.get("phone") or contact.get("number"))
                    msg = payload.get("message") or payload.get("payload")
                    if isinstance(msg, dict):
                        message = message or (msg.get("text") or msg.get("body"))
                message = message or data.get("text") or (data.get("message") if isinstance(data.get("message"), str) else None)
                sender = sender or data.get("from") or data.get("sender")

        if not sender or not message:
            logger.warning("No sender/message found")
            return jsonify({"status":"ok","message":"no message"}), 200

        sender = sender.replace("whatsapp:", "").strip()
        text = message.strip()
        lower = text.lower()
        logger.info(f"Processing incoming message from {sender}: {text}")

        # built-in quick cancel/back
        if lower in ["cancel", "reset", "stop", "exit"]:
            if sender in user_state:
                user_state.pop(sender, None)
            send_twilio_message(sender, "Cancelled. " + start_main_menu())
            return jsonify({"status":"success"}), 200

        # --- OWNER COMMANDS (normalize and check) ---
        owner_normalized = None
        if OWNER_NUMBER:
            owner_normalized = OWNER_NUMBER.replace("whatsapp:", "").strip()
        if sender == owner_normalized:
            low = lower
            if low.startswith("report"):
                rpt = generate_report()
                send_twilio_message(sender, rpt)
                log_interaction(sender, text, rpt)
                return jsonify({"status":"success"}), 200
            if low.startswith("reviews"):
                mode = "today" if "today" in low else "all"
                reply = get_reviews(mode)
                send_twilio_message(sender, reply)
                log_interaction(sender, text, reply)
                return jsonify({"status":"success"}), 200
        # --- end owner commands ---

        # If user already in a specific flow, handle only that flow
        if sender in user_state:
            flow = user_state[sender].get("flow")
            if flow == "cake":
                reply = handle_cake_flow(sender, text)
                send_twilio_message(sender, reply)
                log_interaction(sender, text, reply)
                return jsonify({"status":"success"}), 200
            elif flow == "booking":
                reply = handle_booking_flow(sender, text)
                send_twilio_message(sender, reply)
                log_interaction(sender, text, reply)
                return jsonify({"status":"success"}), 200
            elif flow == "menu":
                # allow the user to set a category if not set yet
                state = user_state.get(sender, {})
                if not state.get("category"):
                    # check if they typed a category name
                    chosen = None
                    for cat in MENU_DATA.keys():
                        if cat in lower:
                            chosen = cat
                            break
                    if chosen:
                        user_state[sender]["category"] = chosen
                        items = MENU_DATA[chosen]
                        item_list = "\n".join([f"• {i} ({PRICES.get(i.lower(), 'Price on request')})" for i in items])
                        reply = f"📋 {chosen.title()} Menu:\n{item_list}\n\nReply with item name to ask price or 'order <item>' to order. Type 'back' to exit."
                        send_twilio_message(sender, reply)
                        log_interaction(sender, text, reply)
                        return jsonify({"status":"success"}), 200
                    else:
                        # if category not selected, prompt categories again
                        reply = menu_categories_text()
                        send_twilio_message(sender, reply)
                        log_interaction(sender, text, reply)
                        return jsonify({"status":"success"}), 200
                # if category set, handle menu context
                reply = handle_menu_context(sender, text)
                send_twilio_message(sender, reply)
                log_interaction(sender, text, reply)
                return jsonify({"status":"success"}), 200
            elif flow == "confirm_booking":
                # confirm booking branch
                if lower in ["yes", "y", "confirm"]:
                    cnt = user_state[sender].get("count")
                    user_state[sender] = {"flow": "booking", "step": 2, "people": cnt, "timestamp": datetime.now().isoformat()}
                    reply = f"Confirmed for {cnt} people. What date do you prefer?"
                    send_twilio_message(sender, reply)
                    log_interaction(sender, text, reply)
                    return jsonify({"status":"success"}), 200
                else:
                    user_state.pop(sender, None)
                    reply = "Okay, not booking. " + start_main_menu()
                    send_twilio_message(sender, reply)
                    log_interaction(sender, text, reply)
                    return jsonify({"status":"success"}), 200
            else:
                user_state.pop(sender, None)

        # Not in a flow: parse main commands strictly
        if lower in ["1", "menu"]:
            reply = menu_categories_text() + "\n\nType a category to explore it or type 'back' to return."
            user_state[sender] = {"flow": "menu", "category": None, "timestamp": datetime.now().isoformat()}
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        if lower in ["2", "cake", "order cake", "cakes"]:
            user_state[sender] = {"flow": "cake", "step": 1, "timestamp": datetime.now().isoformat()}
            sample = ", ".join(random.sample(CAKE_FLAVOURS, min(5, len(CAKE_FLAVOURS))))
            reply = f"🎂 Sure — we do cakes! Popular flavours: {sample}\nWhich flavour would you like?"
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        if lower in ["3", "book", "reservation", "book table", "table"]:
            user_state[sender] = {"flow": "booking", "step": 1, "timestamp": datetime.now().isoformat()}
            reply = f"🪑 How many people will be joining? (We have {TOTAL_SEATS} seats)"
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        if lower in ["4", "hours", "timing", "time", "open", "opening hours"]:
            reply = "🕘 We're open every day: 11:00 AM - 11:00 PM"
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        if lower in ["5", "location", "address", "where"]:
            reply = "📍 The Estate Deli\n#3162, 60 Feet Road, Indiranagar, Bengaluru - 560008\nGoogle Maps: https://share.google/CxHVtC53L9wvzHQ01"
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        if lower in ["6", "review", "feedback"]:
            reply = "⭐ We value feedback! Please type your feedback and optionally include 'rating: <1-5>'"
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        # If the user types a menu category name directly
        for cat in MENU_DATA.keys():
            if cat in lower:
                user_state[sender] = {"flow": "menu", "category": cat, "timestamp": datetime.now().isoformat()}
                items = MENU_DATA[cat]
                item_list = "\n".join([f"• {i} ({PRICES.get(i.lower(), 'Price on request')})" for i in items])
                reply = f"📋 {cat.title()} Menu:\n{item_list}\n\nReply with item name to ask price or 'order <item>' to order. Type 'back' to exit."
                send_twilio_message(sender, reply)
                log_interaction(sender, text, reply)
                return jsonify({"status":"success"}), 200

        # Bare number outside flow -> ask to confirm booking
        if extract_number(text) and not re.search(r'(am|pm|:)', text.lower()):
            num = extract_number(text)
            reply = f"I saw the number '{num}'. Do you want to book a table for {num} people? Reply 'yes' to confirm or type 'menu' to see options."
            user_state[sender] = {"flow": "confirm_booking", "count": num, "timestamp": datetime.now().isoformat()}
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        # Quick review catch
        if any(k in lower for k in ["good", "bad", "love", "hate", "review", "rating", "feedback"]):
            review = save_review(sender, text)
            reply = "✅ Thanks for your feedback! We appreciate it."
            send_twilio_message(sender, reply)
            log_interaction(sender, text, reply)
            return jsonify({"status":"success"}), 200

        # Default fallback
        reply = "Sorry, I didn't get that. Type 'menu' to see options or 'help' for quick commands."
        send_twilio_message(sender, reply)
        log_interaction(sender, text, reply)
        return jsonify({"status":"success"}), 200

    except Exception as e:
        logger.exception(f"webhook error: {e}")
        return jsonify({"status":"error", "message":"internal"}), 500

# Health
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"healthy", "time": datetime.now().isoformat()}), 200

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    logger.info("Starting Estate Deli bot...")
    app.run(host="0.0.0.0", port=port, debug=debug)
