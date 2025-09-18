# app.py
import os
import json
import logging
from datetime import datetime
from flask import Flask, request, jsonify
import requests

# Optional: OpenAI usage (if you want AI fallback)
try:
    from openai import OpenAI
except Exception:
    OpenAI = None

# -------------------------------
# Basic config & logging
# -------------------------------
app = Flask(__name__)
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("estate_deli_bot")

# -------------------------------
# Environment / secrets (set these in Render)
# -------------------------------
TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")      # required for sending messages
TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")        # required for sending messages
TWILIO_WHATSAPP_NUMBER = os.environ.get("TWILIO_WHATSAPP_NUMBER")  # e.g. "whatsapp:+14155238886" (the sandbox number)
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")              # optional for AI fallback
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "")              # owner number for notifications (without whatsapp: prefix)
SOURCE_NUMBER = os.environ.get("SOURCE_NUMBER", "")            # unused here but kept for compatibility

DATA_DIR = os.environ.get("DATA_DIR", "data")
LOG_FILE = os.path.join(DATA_DIR, "conversations.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")
CAKES_FILE = os.path.join(DATA_DIR, "cakes.json")
REVIEWS_FILE = os.path.join(DATA_DIR, "reviews.json")

os.makedirs(DATA_DIR, exist_ok=True)

# Initialize OpenAI client if provided
client = None
if OPENAI_API_KEY and OpenAI:
    try:
        client = OpenAI(api_key=OPENAI_API_KEY)
    except Exception as e:
        logger.warning(f"OpenAI init failed: {e}")
        client = None

# Seating config
TABLES = 6
SEATS_PER_TABLE = 4
TOTAL_SEATS = TABLES * SEATS_PER_TABLE

# In-memory user states (for flows) with timeout
user_state = {}
USER_STATE_TIMEOUT = 300  # seconds

# Menu data (unchanged)
MENU_DATA = {
    "coffee": [
        "☕ Espresso - ₹120",
        "☕ Americano - ₹140",
        "☕ Cappuccino - ₹160",
        "☕ Latte - ₹180",
        "☕ Mocha - ₹200"
    ],
    "matcha": [
        "🍵 Matcha Latte - ₹220",
        "🍵 Iced Matcha - ₹240",
        "🍵 Matcha Smoothie - ₹260"
    ],
    "signature hot beverages": [
        "🔥 Spiced Chai Latte - ₹180",
        "🔥 Golden Turmeric Latte - ₹200",
        "🔥 Hot Chocolate Supreme - ₹220"
    ],
    "signature iced beverages": [
        "🧊 Iced Vanilla Latte - ₹200",
        "🧊 Cold Brew Float - ₹240",
        "🧊 Frappuccino Special - ₹280"
    ],
    "mocktails": [
        "🍹 Virgin Mojito - ₹180",
        "🍹 Fruit Punch - ₹160",
        "🍹 Lemon Mint Cooler - ₹140"
    ]
}

# -------------------------------
# Helper functions for data storage
# -------------------------------
def load_data(file_path):
    try:
        if not os.path.exists(file_path):
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading data from {file_path}: {e}")
        return []

def save_data(file_path, data):
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving data to {file_path}: {e}")
        return False

def log_interaction(sender, message, reply):
    log_entry = {
        "sender": sender,
        "message": message,
        "reply": reply,
        "timestamp": datetime.now().isoformat()
    }
    logs = load_data(LOG_FILE)
    logs.append(log_entry)
    save_data(LOG_FILE, logs)

def clean_expired_states():
    current_time = datetime.now()
    expired = []
    for u, s in list(user_state.items()):
        if "timestamp" in s:
            try:
                st = datetime.fromisoformat(s["timestamp"])
                if (current_time - st).seconds > USER_STATE_TIMEOUT:
                    expired.append(u)
            except Exception:
                expired.append(u)
    for u in expired:
        user_state.pop(u, None)

# -------------------------------
# Twilio send function
# -------------------------------
def send_twilio_message(to_phone, message_text):
    """
    Send a WhatsApp message through Twilio REST API.
    `to_phone` should be in E.164 format with whatsapp: prefix: e.g. 'whatsapp:+919876543210'
    `TWILIO_WHATSAPP_NUMBER` should be like 'whatsapp:+1415xxxxxxx'
    """
    if not (TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN and TWILIO_WHATSAPP_NUMBER):
        logger.warning("Twilio credentials or from number not configured.")
        return False

    url = f"https://api.twilio.com/2010-04-01/Accounts/{TWILIO_ACCOUNT_SID}/Messages.json"
    payload = {
        "From": TWILIO_WHATSAPP_NUMBER,
        "To": to_phone,
        "Body": message_text
    }
    try:
        resp = requests.post(url, data=payload, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN), timeout=10)
        if resp.status_code in (200, 201):
            logger.info(f"✅ Sent WhatsApp to {to_phone} via Twilio")
            return True
        else:
            logger.error(f"❌ Twilio send failed: {resp.status_code} - {resp.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"❌ Twilio network error: {e}")
        return False

# -------------------------------
# Menu & review & booking helpers (unchanged logic)
# -------------------------------
def get_menu_category(category):
    category_lower = category.lower().strip()
    if category_lower in MENU_DATA:
        items = "\n".join(MENU_DATA[category_lower])
        return f"📋 {category.title()} Menu:\n\n{items}\n\n👉 Need anything else? Type 'menu' to see all categories."
    else:
        available = ", ".join(MENU_DATA.keys())
        return f"❌ Category '{category}' not found.\n\nAvailable categories: {available}"

def save_review(sender, message_text):
    review_text = "No comment"
    rating = None
    if "review:" in message_text.lower():
        parts = message_text.lower().split("review:")
        if len(parts) > 1:
            review_part = parts[1]
            if "rating:" in review_part:
                review_text = review_part.split("rating:")[0].strip()
                rating_part = review_part.split("rating:")[1].strip()
                try:
                    rating = int(rating_part.split()[0])
                    if rating < 1 or rating > 5:
                        rating = None
                except Exception:
                    rating = None
            else:
                review_text = review_part.strip()

    review = {
        "customer": sender,
        "review": review_text,
        "rating": rating,
        "timestamp": datetime.now().isoformat()
    }
    reviews = load_data(REVIEWS_FILE)
    reviews.append(review)
    save_data(REVIEWS_FILE, reviews)
    return review

def get_reviews(mode="all"):
    reviews = load_data(REVIEWS_FILE)
    if not reviews:
        return "📭 No reviews yet."
    if mode == "today":
        today = datetime.now().date()
        todays = []
        for r in reviews:
            try:
                if datetime.fromisoformat(r["timestamp"]).date() == today:
                    todays.append(r)
            except Exception:
                continue
        if not todays:
            return "📭 No reviews today."
        reviews = todays
    out = []
    for r in reviews:
        rating_str = f" {r['rating']}⭐" if r.get("rating") else ""
        cid = r.get("customer", "Unknown")[-4:] if r.get("customer") else "Unknown"
        out.append(f'- "{r["review"]}"{rating_str} – {cid}')
    title = "📢 Reviews Today:" if mode == "today" else "📢 All Reviews:"
    return f"{title}\n\n" + "\n".join(out)

def check_table_availability(date, time, people):
    bookings = load_data(BOOKINGS_FILE)
    booked_seats = 0
    for b in bookings:
        booking_date = b.get("date", "").lower().strip()
        booking_time = b.get("time", "").lower().strip()
        if booking_date == date.lower().strip() and booking_time == time.lower().strip():
            booked_seats += b.get("people", 0)
    available = TOTAL_SEATS - booked_seats
    return available >= people, available

def generate_report():
    bookings = load_data(BOOKINGS_FILE)
    cakes = load_data(CAKES_FILE)
    reviews = load_data(REVIEWS_FILE)
    today = datetime.now().date()
    tb = sum(1 for b in bookings if (lambda x: (datetime.fromisoformat(x["timestamp"]).date() if "timestamp" in x else None))(b) == today)
    tc = sum(1 for c in cakes if (lambda x: (datetime.fromisoformat(x["timestamp"]).date() if "timestamp" in x else None))(c) == today)
    tr = sum(1 for r in reviews if (lambda x: (datetime.fromisoformat(x["timestamp"]).date() if "timestamp" in x else None))(r) == today)
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
# AI fallback (optional)
# -------------------------------
def get_ai_response(message):
    if not client:
        return "🤖 AI assistant unavailable. Try menu options."
    try:
        system_prompt = (
            "You are a helpful assistant for The Estate Deli restaurant in Bangalore. Keep responses brief and friendly."
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            max_tokens=200,
            temperature=0.7
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return "🤖 Sorry, I didn't quite understand. Please try 'menu'."

# -------------------------------
# Webhook handler (supports Twilio form POSTs and JSON payloads)
# -------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        clean_expired_states()

        # raw logging for debugging
        raw_data = request.get_data(as_text=True)
        logger.info(f"RAW WEBHOOK (first 2000 chars): {raw_data[:2000]}")

        message_text = None
        sender_number = None

        # Twilio sends x-www-form-urlencoded with 'From' and 'Body'
        if request.form:
            # e.g., From: 'whatsapp:+919876543210', Body: 'hi'
            frm = request.form.get("From")
            body = request.form.get("Body")
            if frm:
                sender_number = frm.replace("whatsapp:", "").strip()
            if body:
                message_text = body.strip()

        # Also handle JSON POSTs (in case you use another provider)
        if not message_text:
            data = None
            try:
                data = request.get_json(silent=True)
            except Exception:
                data = None

            if data:
                # Try common sendpulse / gupshup shapes:
                # - sendpulse: data['payload']['message']['text']
                # - gupshup: data['payload']['payload']['text'] (old)
                # - generic: data.get('message') or data.get('text')
                payload = data.get("payload") if isinstance(data, dict) else None
                if payload:
                    # sendpulse style
                    msg = payload.get("message") or payload.get("payload")
                    if isinstance(msg, dict):
                        message_text = message_text or msg.get("text") or msg.get("body")
                    contact = payload.get("contact") or payload.get("sender")
                    if isinstance(contact, dict):
                        sender_number = sender_number or (contact.get("id") or contact.get("phone") or contact.get("number"))
                # fallback generic
                message_text = message_text or data.get("text") or data.get("message")
                sender_number = sender_number or data.get("from") or data.get("sender") or sender_number

        if not message_text or not sender_number:
            logger.warning("No valid message or sender found in webhook.")
            return jsonify({"status":"ok", "message":"no message to process"}), 200

        message_text = message_text.strip()
        sender_number = sender_number.strip()
        normalized = message_text.lower()
        logger.info(f"Processing incoming message from {sender_number}: {message_text}")

        # Owner commands (if owner sends messages)
        if sender_number == OWNER_NUMBER:
            if normalized.startswith("report"):
                reply_text = generate_report()
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                return jsonify({"status":"success"}), 200
            if normalized.startswith("reviews"):
                mode = "today" if "today" in normalized else "all"
                reply_text = get_reviews(mode)
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                return jsonify({"status":"success"}), 200

        # If user is in a stateful flow
        if sender_number in user_state:
            state = user_state[sender_number]

            # cake flow
            if state.get("flow") == "cake":
                if state.get("step") == 1:
                    state["flavour"] = message_text
                    state["step"] = 2
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "🎂 Perfect! What pickup date would you prefer? (e.g., 25th Sep, Tomorrow)"
                elif state.get("step") == 2:
                    state["date"] = message_text
                    state["step"] = 3
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "⏰ Great! What time should we keep it ready for pickup?"
                elif state.get("step") == 3:
                    state["time"] = message_text
                    cake_order = {
                        "customer": sender_number,
                        "flavour": state["flavour"],
                        "date": state["date"],
                        "time": state["time"],
                        "timestamp": datetime.now().isoformat()
                    }
                    cakes = load_data(CAKES_FILE)
                    cakes.append(cake_order)
                    save_data(CAKES_FILE, cakes)
                    reply_text = (
                        f"✅ Cake order confirmed!\n\n"
                        f"🎂 Flavour: {state['flavour']}\n"
                        f"📅 Date: {state['date']}\n"
                        f"⏰ Time: {state['time']}\n\n"
                        "Would you like to leave a review? Type: review: <text> rating: <1-5>"
                    )
                    owner_msg = (
                        f"📢 New Cake Order!\n\nCustomer: {sender_number}\nFlavour: {state['flavour']}\nDate: {state['date']}\nTime: {state['time']}"
                    )
                    if OWNER_NUMBER:
                        send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
                    user_state.pop(sender_number, None)
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200

            # booking flow
            elif state.get("flow") == "booking":
                if state.get("step") == 1:
                    try:
                        people_count = int(message_text)
                        if people_count <= 0:
                            raise ValueError()
                        state["people"] = people_count
                        state["step"] = 2
                        state["timestamp"] = datetime.now().isoformat()
                        reply_text = f"📅 Booking for {people_count} people. What date would you prefer? (e.g., Today, Tomorrow, 25th Sep)"
                    except Exception:
                        reply_text = "⚠️ Please enter a valid number of people (e.g., 4)"
                elif state.get("step") == 2:
                    state["date"] = message_text
                    state["step"] = 3
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "⏰ Perfect! What time would you like to book the table?"
                elif state.get("step") == 3:
                    state["time"] = message_text
                    is_avail, available_seats = check_table_availability(state["date"], state["time"], state["people"])
                    if not is_avail:
                        reply_text = (
                            f"⚠️ Sorry, we don't have enough seats for {state['people']} at {state['time']} on {state['date']}.\n"
                            f"Available seats: {available_seats}\n\nWould you like to try a different time or date?"
                        )
                    else:
                        booking = {
                            "customer": sender_number,
                            "people": state["people"],
                            "date": state["date"],
                            "time": state["time"],
                            "timestamp": datetime.now().isoformat()
                        }
                        bookings = load_data(BOOKINGS_FILE)
                        bookings.append(booking)
                        save_data(BOOKINGS_FILE, bookings)
                        reply_text = (
                            f"✅ Table booking confirmed!\n\n"
                            f"👥 People: {state['people']}\n"
                            f"📅 Date: {state['date']}\n"
                            f"⏰ Time: {state['time']}\n\n"
                            "Would you like to leave a review? Type: review: <your feedback> rating: <1-5>"
                        )
                        owner_msg = (
                            f"📢 New Table Booking!\n\nCustomer: {sender_number}\nPeople: {state['people']}\nDate: {state['date']}\nTime: {state['time']}"
                        )
                        if OWNER_NUMBER:
                            send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
                    user_state.pop(sender_number, None)
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200

        # Regular commands
        if normalized in ["hi", "hello", "hey", "start", "menu"]:
            reply_text = (
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

        elif normalized in ["1", "menu"]:
            categories = list(MENU_DATA.keys())
            category_list = "\n".join([f"• {cat.title()}" for cat in categories])
            reply_text = (
                f"📋 Our Menu Categories:\n\n{category_list}\n\n"
                "👉 Reply with a category name to see items and prices."
            )

        elif normalized in MENU_DATA.keys():
            reply_text = get_menu_category(normalized)

        elif normalized in ["2", "cake", "cake order", "cakes", "order cake"]:
            user_state[sender_number] = {"flow": "cake", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = "🎂 I'd love to help you order a cake! What flavour would you like?\n\n(e.g., Chocolate, Vanilla, Red Velvet, etc.)"

        elif normalized in ["3", "book", "reservation", "table", "book table"]:
            user_state[sender_number] = {"flow": "booking", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = f"🪑 I'll help you book a table! How many people will be joining?\n\n(We have {TOTAL_SEATS} seats available)"

        elif normalized in ["4", "hours", "timing", "time", "open", "opening hours"]:
            reply_text = "🕘 We're open every day!\n\n⏰ 11:00 AM - 11:00 PM\n\nSee you soon! ☕"

        elif normalized in ["5", "location", "address", "where"]:
            reply_text = (
                "📍 The Estate Deli\n\n"
                "#3162, 60 Feet Road, 12th Cross,\n"
                "HAL 2nd Stage, Defence Colony,\n"
                "Indiranagar, Bengaluru - 560008\n\n"
                "🗺️ Google Maps: https://share.google/CxHVtC53L9wvzHQ01"
            )

        elif normalized in ["6", "review", "feedback"] or "review:" in normalized:
            if "review:" in normalized:
                review = save_review(sender_number, message_text)
                rating_str = f" {review['rating']}⭐" if review.get("rating") else ""
                reply_text = f"✅ Thank you for your review!{rating_str}\n\nYour feedback helps us improve! 💙"
                owner_msg = f"📢 New Review!\n\n{review['review']}{rating_str}\n\nFrom: {sender_number}"
                if OWNER_NUMBER:
                    send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
            else:
                reply_text = (
                    "⭐ We'd love to hear your feedback!\n\n"
                    "Please use this format:\n"
                    "review: <your feedback> rating: <1-5>\n\n"
                    "Example:\n"
                    "review: Great food and service! rating: 5"
                )

        elif normalized in ["cancel", "reset", "stop", "exit"]:
            if sender_number in user_state:
                user_state.pop(sender_number, None)
                reply_text = "❌ Current process cancelled. How can I help you today? Type 'menu' to see options."
            else:
                reply_text = "👋 How can I help you today? Type 'menu' to see all options."

        else:
            reply_text = get_ai_response(message_text)

        # log and send
        log_interaction(sender_number, message_text, reply_text)
        send_twilio_message(f"whatsapp:{sender_number}", reply_text)

        return jsonify({"status":"success"}), 200

    except Exception as e:
        logger.exception(f"Webhook handler error: {e}")
        return jsonify({"status":"error", "message":"internal server error"}), 500

# Health check
@app.route("/health", methods=["GET"])
def health():
    return jsonify({"status":"healthy", "timestamp": datetime.now().isoformat(), "version":"2.0"}), 200

# Run locally
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    logger.info(f"Starting The Estate Deli WhatsApp Bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
