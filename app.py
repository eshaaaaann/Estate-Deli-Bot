# app.py
import os
import json
import logging
import random
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

# Cake flavours list
CAKE_FLAVOURS = [
    "Chocolate", "Vanilla", "Strawberry", "Red Velvet", "Black Forest",
    "Butterscotch", "Pineapple", "Mango", "Coffee", "Caramel",
    "Lemon", "Fruit Cake", "Truffle", "Oreo", "Cheesecake"
]

# Enhanced menu data with more comprehensive coverage
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

# Common phrases that might indicate booking intent
BOOKING_KEYWORDS = [
    "book", "reserve", "reservation", "table", "seat", "booking", 
    "book table", "reserve table", "table booking", "make reservation",
    "want to book", "need table", "table for"
]

# Common phrases for cake ordering
CAKE_KEYWORDS = [
    "cake", "order cake", "cake order", "want cake", "need cake",
    "birthday cake", "custom cake", "cake delivery", "order a cake"
]

# Common review keywords
REVIEW_KEYWORDS = [
    "review", "feedback", "rating", "comment", "experience",
    "service", "food was", "place is", "recommend", "loved",
    "great", "excellent", "good", "bad", "poor"
]

# Time-related keywords
TIME_KEYWORDS = [
    "today", "tomorrow", "evening", "afternoon", "morning", "night",
    "am", "pm", "o'clock", "hour", "minutes", "now", "later"
]

# Date-related keywords  
DATE_KEYWORDS = [
    "today", "tomorrow", "yesterday", "monday", "tuesday", "wednesday", 
    "thursday", "friday", "saturday", "sunday", "jan", "feb", "mar", 
    "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"
]

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
# Smart response helpers
# -------------------------------
def is_number(text):
    """Check if text represents a number"""
    try:
        int(text.strip())
        return True
    except ValueError:
        return False

def extract_number(text):
    """Extract number from text"""
    import re
    numbers = re.findall(r'\d+', text)
    if numbers:
        return int(numbers[0])
    return None

def contains_keywords(text, keywords):
    """Check if text contains any of the given keywords"""
    text_lower = text.lower()
    return any(keyword in text_lower for keyword in keywords)

def is_cake_flavour(text):
    """Check if text matches a cake flavour"""
    text_lower = text.lower().strip()
    return any(flavour.lower() in text_lower for flavour in CAKE_FLAVOURS)

def get_cake_flavour_from_text(text):
    """Extract cake flavour from text"""
    text_lower = text.lower().strip()
    for flavour in CAKE_FLAVOURS:
        if flavour.lower() in text_lower:
            return flavour
    # If exact match not found, return the text as is
    return text.strip()

def is_time_related(text):
    """Check if text seems to be time-related"""
    return contains_keywords(text, TIME_KEYWORDS) or any(char.isdigit() for char in text)

def is_date_related(text):
    """Check if text seems to be date-related"""
    return contains_keywords(text, DATE_KEYWORDS)

def get_random_cake_flavours(count=6):
    """Get random cake flavours for display"""
    return random.sample(CAKE_FLAVOURS, min(count, len(CAKE_FLAVOURS)))

def smart_intent_detection(text):
    """Detect user intent from message"""
    text_lower = text.lower().strip()
    
    # Check for booking intent
    if contains_keywords(text, BOOKING_KEYWORDS):
        return "booking"
    
    # Check for cake ordering intent
    if contains_keywords(text, CAKE_KEYWORDS):
        return "cake"
        
    # Check for review intent
    if contains_keywords(text, REVIEW_KEYWORDS) or "review:" in text_lower:
        return "review"
        
    # Check if it's just a number (could be people count)
    if is_number(text):
        return "number"
        
    # Check for menu categories
    if text_lower in MENU_DATA.keys():
        return "menu_category"
        
    return "unknown"

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
# Menu & review & booking helpers
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
    else:
        # If no explicit "review:" format, treat entire message as review
        review_text = message_text.strip()

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
    tc = sum(1 for c in cakes if (lambda x: (datetime.fromisoformat(x["timestamp"]).date() if "timestamp" in c else None))(c) == today)
    tr = sum(1 for r in reviews if (lambda x: (datetime.fromisoformat(x["timestamp"]).date() if "timestamp" in r else None))(r) == today)
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
# AI fallback (should be rarely used now)
# -------------------------------
def get_ai_response(message):
    if not client:
        return "🤖 I'm not sure I understand. Could you please try using one of our menu options? Type 'menu' to see all available options."
    try:
        system_prompt = (
            "You are a helpful assistant for The Estate Deli restaurant in Bangalore. "
            "Keep responses very brief and always direct users to use the numbered menu options. "
            "If they seem to want to book a table, tell them to type '3' or 'book table'. "
            "If they want to order a cake, tell them to type '2' or 'order cake'. "
            "If they want to see the menu, tell them to type '1' or 'menu'."
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": message}
            ],
            max_tokens=100,
            temperature=0.3
        )
        return response.choices[0].message.content.strip()
    except Exception as e:
        logger.error(f"OpenAI API error: {e}")
        return "🤖 I'm having trouble understanding. Please try typing 'menu' to see our options, or use numbers 1-6 for quick access to our services."

# -------------------------------
# Webhook handler (supports Twilio form POSTs and JSON payloads)
# -------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    try:
        clean_expired_states()

        # raw logging for debugging
        raw_data = request.get_data(as_text=True)
        logger.info(f"RAW WEBHOOK (first 500 chars): {raw_data[:500]}")

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
                payload = data.get("payload") if isinstance(data, dict) else None
                if payload:
                    msg = payload.get("message") or payload.get("payload")
                    if isinstance(msg, dict):
                        message_text = message_text or msg.get("text") or msg.get("body")
                    contact = payload.get("contact") or payload.get("sender")
                    if isinstance(contact, dict):
                        sender_number = sender_number or (contact.get("id") or contact.get("phone") or contact.get("number"))
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
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200
            if normalized.startswith("reviews"):
                mode = "today" if "today" in normalized else "all"
                reply_text = get_reviews(mode)
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200

        # If user is in a stateful flow
        if sender_number in user_state:
            state = user_state[sender_number]

            # Enhanced cake flow
            if state.get("flow") == "cake":
                if state.get("step") == 1:  # Flavour selection
                    # Accept single word or phrase as flavour
                    if is_cake_flavour(message_text):
                        flavour = get_cake_flavour_from_text(message_text)
                    else:
                        flavour = message_text.title()  # Use whatever they typed
                    
                    state["flavour"] = flavour
                    state["step"] = 2
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "🎂 Great choice! Do you want any customization on the cake? (e.g., 'Happy Birthday John', 'Congratulations', or just type 'no' for no message)"
                
                elif state.get("step") == 2:  # Customization message
                    if normalized in ["no", "none", "nothing", "nope", "na"]:
                        state["custom_message"] = "No custom message"
                    else:
                        state["custom_message"] = message_text
                    
                    state["step"] = 3
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "📅 Perfect! What pickup date would you prefer? (e.g., 'today', 'tomorrow', '25th Dec')"
                
                elif state.get("step") == 3:  # Pickup date
                    state["date"] = message_text
                    state["step"] = 4
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "⏰ Almost done! What time should we keep it ready for pickup? (e.g., '6 PM', '18:00', 'evening')"
                
                elif state.get("step") == 4:  # Pickup time
                    state["time"] = message_text
                    
                    # Save enhanced cake order
                    cake_order = {
                        "customer": sender_number,
                        "flavour": state["flavour"],
                        "custom_message": state.get("custom_message", "No custom message"),
                        "date": state["date"],
                        "time": state["time"],
                        "timestamp": datetime.now().isoformat()
                    }
                    cakes = load_data(CAKES_FILE)
                    cakes.append(cake_order)
                    save_data(CAKES_FILE, cakes)
                    
                    custom_msg_display = f"\nCustomization: {state['custom_message']}" if state.get("custom_message", "").lower() != "no custom message" else ""
                    
                    reply_text = (
                        f"✅ Cake order confirmed!\n\n"
                        f"🎂 Flavour: {state['flavour']}{custom_msg_display}\n"
                        f"📅 Date: {state['date']}\n"
                        f"⏰ Time: {state['time']}\n\n"
                        "We'll have it ready for you! 🙏\n\n"
                        "Would you like to leave a review about our service? Just type your thoughts!"
                    )
                    
                    # Enhanced owner notification
                    owner_msg = (
                        f"📢 New Cake Order!\n\n"
                        f"Customer: {sender_number}\n"
                        f"Flavour: {state['flavour']}\n"
                        f"Custom Message: {state.get('custom_message', 'None')}\n"
                        f"Pickup Date: {state['date']}\n"
                        f"Pickup Time: {state['time']}"
                    )
                    if OWNER_NUMBER:
                        send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
                    
                    user_state.pop(sender_number, None)
                
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200

            # Enhanced booking flow
            elif state.get("flow") == "booking":
                if state.get("step") == 1:  # Number of people
                    # Handle both "5" and "5 people" and "five" etc.
                    people_count = None
                    if is_number(message_text):
                        people_count = int(message_text.strip())
                    else:
                        # Try to extract number from text
                        people_count = extract_number(message_text)
                    
                    if people_count and people_count > 0:
                        state["people"] = people_count
                        state["step"] = 2
                        state["timestamp"] = datetime.now().isoformat()
                        reply_text = f"📅 Perfect! Booking for {people_count} people. What date would you prefer? (e.g., 'today', 'tomorrow', '25th Dec')"
                    else:
                        reply_text = "⚠️ Please tell me how many people will be joining. Just type the number (e.g., '4' or '4 people')"
                
                elif state.get("step") == 2:  # Date
                    state["date"] = message_text
                    state["step"] = 3
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "⏰ Great! What time would you like to book the table? (e.g., '7 PM', '19:00', 'evening')"
                
                elif state.get("step") == 3:  # Time
                    state["time"] = message_text
                    
                    # Check availability
                    is_avail, available_seats = check_table_availability(state["date"], state["time"], state["people"])
                    if not is_avail:
                        reply_text = (
                            f"⚠️ Sorry, we don't have enough seats for {state['people']} people at {state['time']} on {state['date']}.\n\n"
                            f"Available seats: {available_seats}\n\n"
                            "Would you like to try a different time or date? Or type 'menu' to see other options."
                        )
                    else:
                        # Save booking
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
                            f"✅ Table reservation confirmed!\n\n"
                            f"👥 People: {state['people']}\n"
                            f"📅 Date: {state['date']}\n"
                            f"⏰ Time: {state['time']}\n\n"
                            "We look forward to serving you! 🙏\n\n"
                            "How was your experience booking with us? Feel free to share your thoughts!"
                        )
                        
                        # Owner notification
                        owner_msg = (
                            f"📢 New Table Booking!\n\n"
                            f"Customer: {sender_number}\n"
                            f"People: {state['people']}\n"
                            f"Date: {state['date']}\n"
                            f"Time: {state['time']}"
                        )
                        if OWNER_NUMBER:
                            send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
                    
                    user_state.pop(sender_number, None)
                
                send_twilio_message(f"whatsapp:{sender_number}", reply_text)
                log_interaction(sender_number, message_text, reply_text)
                return jsonify({"status":"success"}), 200

        # Smart intent detection for regular commands
        intent = smart_intent_detection(message_text)
        
        # Handle detected intents
        if intent == "booking" or normalized in BOOKING_KEYWORDS:
            user_state[sender_number] = {"flow": "booking", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = f"🪑 I'll help you book a table! How many people will be joining? (We have {TOTAL_SEATS} seats available)\n\nJust type the number, like '4' or '4 people'"

        elif intent == "cake" or normalized in CAKE_KEYWORDS:
            # Show random cake flavours
            random_flavours = get_random_cake_flavours(6)
            flavour_list = " • ".join(random_flavours)
            user_state[sender_number] = {"flow": "cake", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = f"🎂 I'd love to help you order a cake! Here are some popular flavours:\n\n{flavour_list}\n\nWhich flavour would you like? (You can pick from above or tell me any other flavour)"

        elif intent == "number" and not user_state.get(sender_number):
            # If someone just sends a number without context, assume they want to book
            people_count = int(message_text.strip())
            if people_count > 0 and people_count <= 20:  # reasonable limit
                user_state[sender_number] = {"flow": "booking", "step": 2, "people": people_count, "timestamp": datetime.now().isoformat()}
                reply_text = f"📅 Great! Booking for {people_count} people. What date would you prefer? (e.g., 'today', 'tomorrow', '25th Dec')"
            else:
                reply_text = "🤔 I see you entered a number. Are you looking to book a table? If yes, please let me know how many people (up to 20). Or type 'menu' to see all our options."

        elif intent == "review" or "review:" in normalized:
            if "review:" in normalized:
                review = save_review(sender_number, message_text)
                rating_str = f" {review['rating']}⭐" if review.get("rating") else ""
                reply_text = f"✅ Thank you for your review!{rating_str}\n\nYour feedback helps us improve! 💙"
                owner_msg = f"📢 New Review!\n\n{review['review']}{rating_str}\n\nFrom: {sender_number}"
                if OWNER_NUMBER:
                    send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
            elif contains_keywords(message_text, REVIEW_KEYWORDS):
                # Treat the message as a review even without format
                review = save_review(sender_number, message_text)
                reply_text = "✅ Thank you for sharing your feedback! We really appreciate it! 💙"
                owner_msg = f"📢 New Review/Feedback!\n\n{review['review']}\n\nFrom: {sender_number}"
                if OWNER_NUMBER:
                    send_twilio_message(f"whatsapp:{OWNER_NUMBER}", owner_msg)
            else:
                reply_text = (
                    "⭐ We'd love to hear your feedback!\n\n"
                    "You can either:\n"
                    "• Just tell us your thoughts naturally\n"
                    "• Use format: review: <your feedback> rating: <1-5>\n\n"
                    "Example: 'Great food and service!' or 'review: Loved the coffee! rating: 5'"
                )

        elif intent == "menu_category" or normalized in MENU_DATA.keys():
            reply_text = get_menu_category(normalized)

        # Regular numbered options and greetings
        elif normalized in ["hi", "hello", "hey", "start", "menu", "1"]:
            if normalized == "1":
                categories = list(MENU_DATA.keys())
                category_list = "\n".join([f"• {cat.title()}" for cat in categories])
                reply_text = (
                    f"📋 Our Menu Categories:\n\n{category_list}\n\n"
                    "👉 Reply with a category name to see items and prices."
                )
            else:
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

        elif normalized in ["2"]:
            random_flavours = get_random_cake_flavours(6)
            flavour_list = " • ".join(random_flavours)
            user_state[sender_number] = {"flow": "cake", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = f"🎂 I'd love to help you order a cake! Here are some popular flavours:\n\n{flavour_list}\n\nWhich flavour would you like? (You can pick from above or tell me any other flavour)"

        elif normalized in ["3"]:
            user_state[sender_number] = {"flow": "booking", "step": 1, "timestamp": datetime.now().isoformat()}
            reply_text = f"🪑 I'll help you book a table! How many people will be joining? (We have {TOTAL_SEATS} seats available)\n\nJust type the number, like '4' or '4 people'"

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

        elif normalized in ["6"]:
            reply_text = (
                "⭐ We'd love to hear your feedback!\n\n"
                "You can either:\n"
                "• Just tell us your thoughts naturally\n"
                "• Use format: review: <your feedback> rating: <1-5>\n\n"
                "Example: 'Great food and service!' or 'review: Loved the coffee! rating: 5'"
            )

        elif normalized in ["cancel", "reset", "stop", "exit"]:
            if sender_number in user_state:
                user_state.pop(sender_number, None)
                reply_text = "❌ Current process cancelled. How can I help you today? Type 'menu' to see options."
            else:
                reply_text = "👋 How can I help you today? Type 'menu' to see all options."

        # Enhanced fallback with suggestions
        else:
            # Try to give contextual suggestions based on message content
            suggestions = []
            
            if any(word in normalized for word in ["eat", "food", "hungry", "drink", "coffee", "tea"]):
                suggestions.append("Type '1' or 'menu' to see our food and drinks")
            
            if any(word in normalized for word in ["birthday", "celebration", "party"]):
                suggestions.append("Type '2' or 'cake' to order a custom cake")
            
            if any(word in normalized for word in ["visit", "come", "dine", "sit"]):
                suggestions.append("Type '3' or 'book' to reserve a table")
                
            if any(word in normalized for word in ["time", "open", "close", "hours"]):
                suggestions.append("Type '4' for our opening hours")
                
            if any(word in normalized for word in ["where", "location", "address", "directions"]):
                suggestions.append("Type '5' for our location")

            if suggestions:
                suggestion_text = "\n".join([f"• {s}" for s in suggestions])
                reply_text = f"I think you might be looking for:\n\n{suggestion_text}\n\nOr type 'menu' to see all options!"
            else:
                # Last resort - AI fallback or generic response
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