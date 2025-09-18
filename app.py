from flask import Flask, request, jsonify
import requests
import json
import os
from datetime import datetime
from openai import OpenAI
import logging

app = Flask(__name__)

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# -------------------------------
# CONFIG
# -------------------------------
# SECURITY WARNING: Remove these hardcoded keys before deploying to production!
GUPSHUP_API_KEY = os.environ.get("GUPSHUP_API_KEY", "your-gupshup-api-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "your-openai-api-key")
OWNER_NUMBER = os.environ.get("OWNER_NUMBER", "919742216585")
SOURCE_NUMBER = os.environ.get("SOURCE_NUMBER", "917834811114")

# Initialize OpenAI client with error handling
try:
    client = OpenAI(api_key=OPENAI_API_KEY)
except Exception as e:
    logger.error(f"Failed to initialize OpenAI client: {e}")
    client = None

# Seating config
TABLES = 6
SEATS_PER_TABLE = 4
TOTAL_SEATS = TABLES * SEATS_PER_TABLE

# In-memory user states (for flows) with timeout
user_state = {}
USER_STATE_TIMEOUT = 300  # 5 minutes

# File paths
DATA_DIR = "data"
LOG_FILE = os.path.join(DATA_DIR, "conversations.json")
BOOKINGS_FILE = os.path.join(DATA_DIR, "bookings.json")
CAKES_FILE = os.path.join(DATA_DIR, "cakes.json")
REVIEWS_FILE = os.path.join(DATA_DIR, "reviews.json")

# Create data directory if it doesn't exist
os.makedirs(DATA_DIR, exist_ok=True)

# Menu data
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
    """Load data from JSON file with error handling"""
    try:
        if not os.path.exists(file_path):
            return []
        with open(file_path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception as e:
        logger.error(f"Error loading data from {file_path}: {e}")
        return []

def save_data(file_path, data):
    """Save data to JSON file with error handling"""
    try:
        with open(file_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        return True
    except Exception as e:
        logger.error(f"Error saving data to {file_path}: {e}")
        return False

def log_interaction(sender, message, reply):
    """Log user interactions"""
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
    """Remove expired user states"""
    current_time = datetime.now()
    expired_users = []
    
    for user, state in user_state.items():
        if "timestamp" in state:
            try:
                state_time = datetime.fromisoformat(state["timestamp"])
                if (current_time - state_time).seconds > USER_STATE_TIMEOUT:
                    expired_users.append(user)
            except ValueError:
                # Invalid timestamp format, remove this state
                expired_users.append(user)
    
    for user in expired_users:
        user_state.pop(user, None)

# -------------------------------
# Gupshup message sender
# -------------------------------
def send_gupshup_message(phone_number, message_text):
    """Send WhatsApp message via Gupshup API"""
    if not GUPSHUP_API_KEY or GUPSHUP_API_KEY == "your-gupshup-api-key":
        logger.warning("Gupshup API key not configured")
        return False
        
    url = "https://api.gupshup.io/sm/api/v1/msg"
    headers = {
        "Content-Type": "application/x-www-form-urlencoded",
        "apikey": GUPSHUP_API_KEY
    }
    
    # Truncate message if too long
    if len(message_text) > 4000:
        message_text = message_text[:3900] + "...\n\n(Message truncated)"
    
    payload = {
        "channel": "whatsapp",
        "source": SOURCE_NUMBER,
        "destination": phone_number,
        "message": json.dumps({"type": "text", "text": message_text}),
        "isHSM": "false"
    }

    try:
        response = requests.post(url, headers=headers, data=payload, timeout=10)
        if response.status_code == 200:
            logger.info(f"✅ Message sent to {phone_number}")
            return True
        else:
            logger.error(f"❌ Failed to send message: {response.status_code} - {response.text}")
            return False
    except requests.RequestException as e:
        logger.error(f"❌ Network error sending message: {e}")
        return False

# -------------------------------
# Menu functions
# -------------------------------
def get_menu_category(category):
    """Get menu items for a specific category"""
    category_lower = category.lower().strip()
    
    if category_lower in MENU_DATA:
        items = "\n".join(MENU_DATA[category_lower])
        return f"📋 {category.title()} Menu:\n\n{items}\n\n👉 Need anything else? Type 'menu' to see all categories."
    else:
        available_categories = ", ".join(MENU_DATA.keys())
        return f"❌ Category '{category}' not found.\n\nAvailable categories: {available_categories}"

# -------------------------------
# Review functions
# -------------------------------
def save_review(sender, message_text):
    """Save customer review"""
    review_text = "No comment"
    rating = None
    
    # Parse review and rating from message
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
                except (ValueError, IndexError):
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
    """Get reviews based on mode"""
    reviews = load_data(REVIEWS_FILE)
    if not reviews:
        return "📭 No reviews yet."

    if mode == "today":
        today = datetime.now().date()
        todays_reviews = []
        for review in reviews:
            try:
                review_date = datetime.fromisoformat(review["timestamp"]).date()
                if review_date == today:
                    todays_reviews.append(review)
            except (ValueError, KeyError):
                continue
                
        if not todays_reviews:
            return "📭 No reviews today."
        reviews = todays_reviews

    review_list = []
    for r in reviews:
        rating_str = f" {r['rating']}⭐" if r.get("rating") else ""
        customer_id = r.get("customer", "Unknown")[-4:] if r.get("customer") else "Unknown"
        review_list.append(f'- "{r["review"]}"{rating_str} – {customer_id}')
    
    title = "📢 Reviews Today:" if mode == "today" else "📢 All Reviews:"
    return f"{title}\n\n" + "\n".join(review_list)

# -------------------------------
# Booking functions
# -------------------------------
def check_table_availability(date, time, people):
    """Check if tables are available for booking"""
    bookings = load_data(BOOKINGS_FILE)
    
    # Count existing bookings for the same date and time
    booked_seats = 0
    for booking in bookings:
        booking_date = booking.get("date", "").lower().strip()
        booking_time = booking.get("time", "").lower().strip()
        if booking_date == date.lower().strip() and booking_time == time.lower().strip():
            booked_seats += booking.get("people", 0)
    
    available_seats = TOTAL_SEATS - booked_seats
    return available_seats >= people, available_seats

# -------------------------------
# Report generator
# -------------------------------
def generate_report():
    """Generate daily report for owner"""
    bookings = load_data(BOOKINGS_FILE)
    cakes = load_data(CAKES_FILE)
    reviews = load_data(REVIEWS_FILE)
    
    # Today's stats
    today = datetime.now().date()
    today_bookings = 0
    today_cakes = 0
    today_reviews = 0
    
    for b in bookings:
        try:
            if datetime.fromisoformat(b["timestamp"]).date() == today:
                today_bookings += 1
        except (ValueError, KeyError):
            continue
            
    for c in cakes:
        try:
            if datetime.fromisoformat(c["timestamp"]).date() == today:
                today_cakes += 1
        except (ValueError, KeyError):
            continue
            
    for r in reviews:
        try:
            if datetime.fromisoformat(r["timestamp"]).date() == today:
                today_reviews += 1
        except (ValueError, KeyError):
            continue
    
    return (
        f"📊 Daily Report - {today.strftime('%d %B %Y')}\n\n"
        f"🪑 Bookings Today: {today_bookings}\n"
        f"🎂 Cake Orders Today: {today_cakes}\n"
        f"⭐ Reviews Today: {today_reviews}\n\n"
        f"📈 Total Stats:\n"
        f"🪑 Total Bookings: {len(bookings)}\n"
        f"🎂 Total Cake Orders: {len(cakes)}\n"
        f"⭐ Total Reviews: {len(reviews)}\n"
    )

# -------------------------------
# AI response function
# -------------------------------
def get_ai_response(message):
    """Get response from OpenAI"""
    if not client:
        return "🤖 AI assistant is currently unavailable. Please try the menu options."
    
    try:
        system_prompt = (
            "You are a helpful assistant for The Estate Deli restaurant in Bangalore. "
            "Keep responses brief, friendly, and restaurant-focused. "
            "If asked about menu items, suggest they type 'menu' for full details. "
            "For bookings or orders, direct them to use the numbered options."
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
        return "🤖 Sorry, I didn't quite understand. Please try using the menu options by typing 'menu'."

# -------------------------------
# Main webhook handler
# -------------------------------
@app.route("/webhook", methods=["POST"])
def webhook():
    """Handle incoming WhatsApp messages"""
    try:
        # Clean expired states
        clean_expired_states()
        
        # Parse incoming data
        raw_data = request.data.decode("utf-8", errors="replace")
        logger.info(f"📩 Incoming webhook data: {raw_data[:500]}...")
        
        try:
            data = json.loads(raw_data)
        except json.JSONDecodeError as e:
            logger.error(f"JSON parse error: {e}")
            return jsonify({"status": "error", "message": "Invalid JSON"}), 400

        # Extract message and sender
        message_text = None
        sender_number = None

        # Handle different webhook formats
        if "payload" in data:
            payload = data["payload"]
            
            # Extract message text
            if isinstance(payload, dict):
                inner_payload = payload.get("payload", {})
                if isinstance(inner_payload, dict):
                    message_text = (inner_payload.get("text") or 
                                  inner_payload.get("body") or 
                                  inner_payload.get("message"))
                
                # Extract sender
                sender_obj = payload.get("sender", {})
                if isinstance(sender_obj, dict):
                    sender_number = (sender_obj.get("phone") or 
                                   sender_obj.get("mobile") or 
                                   sender_obj.get("number"))

        # Handle proxy messages (for testing)
        if message_text and message_text.lower().startswith("proxy "):
            parts = message_text.split(" ", 2)
            if len(parts) >= 3:
                message_text = parts[2].strip()

        if not message_text or not sender_number:
            logger.warning("No valid message or sender found")
            return jsonify({"status": "ok", "message": "No message to process"}), 200

        message_text = message_text.strip()
        normalized = message_text.lower()
        logger.info(f"✅ Processing: {sender_number} -> {message_text}")

        # -------------------------------
        # Owner commands
        # -------------------------------
        if sender_number == OWNER_NUMBER:
            if normalized.startswith("report"):
                reply_text = generate_report()
                send_gupshup_message(sender_number, reply_text)
                return jsonify({"status": "success"}), 200
                
            if normalized.startswith("reviews"):
                mode = "today" if "today" in normalized else "all"
                reply_text = get_reviews(mode)
                send_gupshup_message(sender_number, reply_text)
                return jsonify({"status": "success"}), 200

        # -------------------------------
        # Handle stateful flows
        # -------------------------------
        if sender_number in user_state:
            state = user_state[sender_number]

            # Cake Order Flow
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
                    
                    # Save cake order
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
                        "We'll have it ready for you! 🙏\n\n"
                        "Would you like to leave a review?\n"
                        "Type: review: <your feedback> rating: <1-5>"
                    )
                    
                    # Notify owner
                    owner_msg = (
                        f"📢 New Cake Order!\n\n"
                        f"Customer: {sender_number}\n"
                        f"Flavour: {state['flavour']}\n"
                        f"Date: {state['date']}\n"
                        f"Time: {state['time']}"
                    )
                    send_gupshup_message(OWNER_NUMBER, owner_msg)
                    
                    # Clear state
                    user_state.pop(sender_number, None)

                send_gupshup_message(sender_number, reply_text)
                return jsonify({"status": "success"}), 200

            # Table Booking Flow
            elif state.get("flow") == "booking":
                if state.get("step") == 1:
                    try:
                        people_count = int(message_text)
                        if people_count <= 0:
                            raise ValueError("Invalid number")
                        
                        state["people"] = people_count
                        state["step"] = 2
                        state["timestamp"] = datetime.now().isoformat()
                        reply_text = f"📅 Booking for {people_count} people. What date would you prefer? (e.g., Today, Tomorrow, 25th Sep)"
                    except ValueError:
                        reply_text = "⚠️ Please enter a valid number of people (e.g., 4)"
                        
                elif state.get("step") == 2:
                    state["date"] = message_text
                    state["step"] = 3
                    state["timestamp"] = datetime.now().isoformat()
                    reply_text = "⏰ Perfect! What time would you like to book the table?"
                    
                elif state.get("step") == 3:
                    state["time"] = message_text
                    
                    # Check availability
                    is_available, available_seats = check_table_availability(
                        state["date"], state["time"], state["people"]
                    )
                    
                    if not is_available:
                        reply_text = (
                            f"⚠️ Sorry, we don't have enough seats available for {state['people']} people "
                            f"at {state['time']} on {state['date']}.\n\n"
                            f"Available seats: {available_seats}\n\n"
                            "Would you like to try a different time or date?"
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
                            f"✅ Table booking confirmed!\n\n"
                            f"👥 People: {state['people']}\n"
                            f"📅 Date: {state['date']}\n"
                            f"⏰ Time: {state['time']}\n\n"
                            "We look forward to serving you! 🙏\n\n"
                            "Would you like to leave a review?\n"
                            "Type: review: <your feedback> rating: <1-5>"
                        )
                        
                        # Notify owner
                        owner_msg = (
                            f"📢 New Table Booking!\n\n"
                            f"Customer: {sender_number}\n"
                            f"People: {state['people']}\n"
                            f"Date: {state['date']}\n"
                            f"Time: {state['time']}"
                        )
                        send_gupshup_message(OWNER_NUMBER, owner_msg)
                    
                    # Clear state
                    user_state.pop(sender_number, None)

                send_gupshup_message(sender_number, reply_text)
                return jsonify({"status": "success"}), 200

        # -------------------------------
        # Handle regular commands
        # -------------------------------
        
        # Greetings and main menu
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

        # Menu options
        elif normalized in ["1", "menu"]:
            categories = list(MENU_DATA.keys())
            category_list = "\n".join([f"• {cat.title()}" for cat in categories])
            reply_text = (
                f"📋 Our Menu Categories:\n\n{category_list}\n\n"
                "👉 Reply with a category name to see items and prices."
            )

        # Check if message matches a menu category
        elif normalized in MENU_DATA.keys():
            reply_text = get_menu_category(normalized)

        # Cake order
        elif normalized in ["2", "cake", "cake order", "cakes", "order cake"]:
            user_state[sender_number] = {
                "flow": "cake", 
                "step": 1, 
                "timestamp": datetime.now().isoformat()
            }
            reply_text = "🎂 I'd love to help you order a cake! What flavour would you like?\n\n(e.g., Chocolate, Vanilla, Red Velvet, etc.)"

        # Table booking
        elif normalized in ["3", "book", "reservation", "table", "book table"]:
            user_state[sender_number] = {
                "flow": "booking", 
                "step": 1, 
                "timestamp": datetime.now().isoformat()
            }
            reply_text = f"🪑 I'll help you book a table! How many people will be joining?\n\n(We have {TOTAL_SEATS} seats available)"

        # Opening hours
        elif normalized in ["4", "hours", "timing", "time", "open", "opening hours"]:
            reply_text = "🕘 We're open every day!\n\n⏰ 11:00 AM - 11:00 PM\n\nSee you soon! ☕"

        # Location
        elif normalized in ["5", "location", "address", "where"]:
            reply_text = (
                "📍 The Estate Deli\n\n"
                "#3162, 60 Feet Road, 12th Cross,\n"
                "HAL 2nd Stage, Defence Colony,\n"
                "Indiranagar, Bengaluru - 560008\n\n"
                "🗺️ Google Maps: https://share.google/CxHVtC53L9wvzHQ01"
            )

        # Review handling
        elif normalized in ["6", "review", "feedback"] or "review:" in normalized:
            if "review:" in normalized:
                review = save_review(sender_number, message_text)
                rating_str = f" {review['rating']}⭐" if review.get("rating") else ""
                reply_text = f"✅ Thank you for your review!{rating_str}\n\nYour feedback helps us improve! 💙"
                
                # Notify owner
                owner_msg = f"📢 New Review!\n\n{review['review']}{rating_str}\n\nFrom: {sender_number}"
                send_gupshup_message(OWNER_NUMBER, owner_msg)
            else:
                reply_text = (
                    "⭐ We'd love to hear your feedback!\n\n"
                    "Please use this format:\n"
                    "review: <your feedback> rating: <1-5>\n\n"
                    "Example:\n"
                    "review: Great food and service! rating: 5"
                )

        # Cancel/reset
        elif normalized in ["cancel", "reset", "stop", "exit"]:
            if sender_number in user_state:
                user_state.pop(sender_number)
                reply_text = "❌ Current process cancelled. How can I help you today? Type 'menu' to see options."
            else:
                reply_text = "👋 How can I help you today? Type 'menu' to see all options."

        # AI fallback for unknown messages
        else:
            reply_text = get_ai_response(message_text)

        # Log interaction and send response
        log_interaction(sender_number, message_text, reply_text)
        send_gupshup_message(sender_number, reply_text)
        
        return jsonify({"status": "success"}), 200

    except Exception as e:
        logger.error(f"Webhook error: {e}")
        return jsonify({"status": "error", "message": "Internal server error"}), 500

# -------------------------------
# Health check endpoint
# -------------------------------
@app.route("/health", methods=["GET"])
def health_check():
    """Health check endpoint"""
    return jsonify({
        "status": "healthy",
        "timestamp": datetime.now().isoformat(),
        "version": "2.0"
    }), 200

# -------------------------------
# Run the application
# -------------------------------
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    debug = os.environ.get("DEBUG", "False").lower() == "true"
    
    logger.info(f"Starting The Estate Deli WhatsApp Bot on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)