"""
SV Tiffins — Flask Backend
Production-ready single-file backend for food ordering system.
Handles: User Auth, Order Creation, Order History
Database: MongoDB Atlas
"""

import os
import re
import secrets
import hashlib
from datetime import datetime, timezone

import bcrypt
from flask import Flask, request, jsonify
from flask_cors import CORS
from pymongo import MongoClient, DESCENDING
from pymongo.errors import DuplicateKeyError
from bson import ObjectId
from dotenv import load_dotenv

# ─── Load environment variables ───────────────────────────────────────────────
load_dotenv()

# ─── App setup ────────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app, origins="*", supports_credentials=True)

# ─── MongoDB Connection ────────────────────────────────────────────────────────
MONGO_URI = "mongodb+srv://harshavardhansss567:MJTq6vqF6H5dZcQY@cluster0.b1drhgu.mongodb.net/?retryWrites=true&w=majority&appName=Cluster0"
DB_NAME   = "food_delivery_db"

client = MongoClient(MONGO_URI)
db     = client[DB_NAME]

users_col  = db["users"]
orders_col = db["orders"]

# Ensure unique index on phone
users_col.create_index("phone", unique=True)
# Index for fast token lookups
users_col.create_index("token")
# Index for fast user order lookups
orders_col.create_index("user_id")


# ══════════════════════════════════════════════════════════════════════════════
#  HELPER FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════

def hash_password(plain: str) -> str:
    """Hash password with bcrypt."""
    return bcrypt.hashpw(plain.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def check_password(plain: str, hashed: str) -> bool:
    """Verify bcrypt password."""
    return bcrypt.checkpw(plain.encode("utf-8"), hashed.encode("utf-8"))


def generate_token() -> str:
    """Generate a secure random session token."""
    return secrets.token_hex(32)


def validate_token(token: str):
    """
    Validate Authorization token.
    Returns the user document if valid, else None.
    """
    if not token:
        return None
    user = users_col.find_one({"token": token})
    return user


def get_token_from_request() -> str:
    """Extract token from Authorization header."""
    auth_header = request.headers.get("Authorization", "")
    # Support both "Bearer <token>" and raw "<token>"
    if auth_header.startswith("Bearer "):
        return auth_header[7:].strip()
    return auth_header.strip()


def calculate_subtotal(items: list) -> float:
    """
    Calculate subtotal from items list.
    Backend ALWAYS recalculates — never trusts frontend totals.
    """
    total = 0.0
    for item in items:
        price    = float(item.get("price", 0))
        quantity = int(item.get("quantity", 0))
        total   += price * quantity
    return round(total, 2)


def calculate_delivery(area: str) -> float:
    """
    Calculate delivery charge based on area.
    Area A → ₹0
    Area B → ₹7
    """
    mapping = {
        "Area A": 0.0,
        "Area B": 7.0,
    }
    if area not in mapping:
        return -1  # sentinel: invalid area
    return mapping[area]


def generate_order_number() -> str:
    """
    Generate sequential order number: ORD-0001, ORD-0002 ...
    Uses current count of orders to determine next number.
    """
    count = orders_col.count_documents({})
    return f"ORD-{str(count + 1).zfill(4)}"


def serialize_doc(doc: dict) -> dict:
    """Convert MongoDB document ObjectId to string for JSON serialization."""
    if doc is None:
        return None
    doc = dict(doc)
    if "_id" in doc:
        doc["_id"] = str(doc["_id"])
    if "user_id" in doc and isinstance(doc["user_id"], ObjectId):
        doc["user_id"] = str(doc["user_id"])
    # Format datetime fields
    for field in ("created_at",):
        if field in doc and isinstance(doc[field], datetime):
            doc[field] = doc[field].isoformat()
    return doc


def error_response(message: str, status: int = 400):
    return jsonify({"success": False, "message": message}), status


def success_response(data: dict, status: int = 200):
    return jsonify({"success": True, **data}), status


# ══════════════════════════════════════════════════════════════════════════════
#  VALIDATION HELPERS
# ══════════════════════════════════════════════════════════════════════════════

def validate_phone(phone: str) -> bool:
    """Phone must be exactly 10 digits."""
    return bool(re.fullmatch(r"\d{10}", phone or ""))


def validate_email(email: str) -> bool:
    """Basic email format check."""
    return bool(re.match(r"[^@]+@[^@]+\.[^@]+", email or ""))


def validate_items(items) -> tuple:
    """
    Validate items array.
    Returns (is_valid: bool, error_message: str)
    """
    if not items or not isinstance(items, list):
        return False, "items must be a non-empty array"

    total_qty = 0
    for i, item in enumerate(items):
        if not isinstance(item, dict):
            return False, f"Item at index {i} is invalid"
        if not item.get("name") or not str(item["name"]).strip():
            return False, f"Item at index {i} is missing a name"
        try:
            price = float(item.get("price", 0))
            if price <= 0:
                return False, f"Item '{item.get('name')}' has invalid price"
        except (TypeError, ValueError):
            return False, f"Item '{item.get('name')}' has non-numeric price"
        try:
            qty = int(item.get("quantity", 0))
            if qty <= 0:
                return False, f"Item '{item.get('name')}' has invalid quantity"
        except (TypeError, ValueError):
            return False, f"Item '{item.get('name')}' has non-integer quantity"
        total_qty += qty

    if total_qty < 2:
        return False, "Minimum order quantity of 2 items required"

    return True, ""


def validate_address(address) -> tuple:
    """
    Validate address object.
    Required: door_no, street, nearest_location, landmark
    Optional: house_no, apartment_name, flat_no, maps_link
    """
    if not address or not isinstance(address, dict):
        return False, "address is required"

    required = ["door_no", "street", "nearest_location", "landmark"]
    for field in required:
        if not address.get(field) or not str(address[field]).strip():
            return False, f"address.{field} is required"

    return True, ""


# ══════════════════════════════════════════════════════════════════════════════
#  ROUTES
# ══════════════════════════════════════════════════════════════════════════════

# ── Health Check ──────────────────────────────────────────────────────────────
@app.route("/api/health", methods=["GET"])
def health_check():
    return jsonify({"status": "running", "service": "SV Tiffins API"}), 200


# ── Register ──────────────────────────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def register():
    """
    POST /api/auth/register
    Body: { name, phone, email, password }
    """
    data = request.get_json(silent=True)
    if not data:
        return error_response("Request body must be JSON")

    name     = str(data.get("name", "")).strip()
    phone    = str(data.get("phone", "")).strip()
    email    = str(data.get("email", "")).strip().lower()
    password = str(data.get("password", ""))

    # Validate
    if not name:
        return error_response("name is required")
    if len(name) < 2:
        return error_response("name must be at least 2 characters")
    if not validate_phone(phone):
        return error_response("phone must be exactly 10 digits")
    if not validate_email(email):
        return error_response("email is invalid")
    if len(password) < 6:
        return error_response("password must be at least 6 characters")

    # Check duplicate phone
    if users_col.find_one({"phone": phone}):
        return error_response("phone number is already registered", 409)

    # Hash password & create user
    user_doc = {
        "name":          name,
        "phone":         phone,
        "email":         email,
        "password_hash": hash_password(password),
        "token":         None,
        "created_at":    datetime.now(timezone.utc),
    }

    try:
        result = users_col.insert_one(user_doc)
    except DuplicateKeyError:
        return error_response("phone number is already registered", 409)

    return success_response(
        {
            "message":  "Account created successfully",
            "user_id":  str(result.inserted_id),
            "user_name": name,
        },
        201,
    )


# ── Login ─────────────────────────────────────────────────────────────────────
@app.route("/api/auth/login", methods=["POST"])
def login():
    """
    POST /api/auth/login
    Body: { phone, password }
    Returns: { token, user_name, user_phone }
    """
    data = request.get_json(silent=True)
    if not data:
        return error_response("Request body must be JSON")

    phone    = str(data.get("phone", "")).strip()
    password = str(data.get("password", ""))

    if not validate_phone(phone):
        return error_response("phone must be exactly 10 digits")
    if not password:
        return error_response("password is required")

    # Find user
    user = users_col.find_one({"phone": phone})
    if not user:
        return error_response("Invalid phone number or password", 401)

    # Verify password
    if not check_password(password, user["password_hash"]):
        return error_response("Invalid phone number or password", 401)

    # Generate and store new token
    token = generate_token()
    users_col.update_one(
        {"_id": user["_id"]},
        {"$set": {"token": token}}
    )

    return success_response(
        {
            "message":    "Login successful",
            "token":      token,
            "user_name":  user["name"],
            "user_phone": user["phone"],
        }
    )


# ── Logout ────────────────────────────────────────────────────────────────────
@app.route("/api/auth/logout", methods=["POST"])
def logout():
    """
    POST /api/auth/logout
    Headers: Authorization: <token>
    Invalidates the current session token.
    """
    token = get_token_from_request()
    user  = validate_token(token)
    if not user:
        return error_response("Unauthorized", 401)

    users_col.update_one({"_id": user["_id"]}, {"$set": {"token": None}})
    return success_response({"message": "Logged out successfully"})


# ── Create Order ──────────────────────────────────────────────────────────────
@app.route("/api/orders", methods=["POST"])
def create_order():
    """
    POST /api/orders
    Headers: Authorization: <token>
    Body:
    {
      items: [{ name, price, quantity }],
      address: { door_no, street, nearest_location, landmark,
                 house_no?, apartment_name?, flat_no?, maps_link? },
      area: "Area A" | "Area B",
      payment_mode: "QR" | "COD",
      transaction_ref: "6-digit string" (required if QR)
    }
    """
    # ── Auth ──────────────────────────────────────────────
    token = get_token_from_request()
    user  = validate_token(token)
    if not user:
        return error_response("Unauthorized — please login", 401)

    # ── Parse body ────────────────────────────────────────
    data = request.get_json(silent=True)
    if not data:
        return error_response("Request body must be JSON")

    items           = data.get("items")
    address         = data.get("address")
    area            = str(data.get("area", "")).strip()
    payment_mode    = str(data.get("payment_mode", "")).strip().upper()
    transaction_ref = str(data.get("transaction_ref", "")).strip()

    # ── Validate items ────────────────────────────────────
    items_ok, items_err = validate_items(items)
    if not items_ok:
        return error_response(items_err)

    # ── Validate address ──────────────────────────────────
    addr_ok, addr_err = validate_address(address)
    if not addr_ok:
        return error_response(addr_err)

    # ── Validate area ─────────────────────────────────────
    delivery_charge = calculate_delivery(area)
    if delivery_charge == -1:
        return error_response('area must be "Area A" or "Area B"')

    # ── Validate payment mode ─────────────────────────────
    if payment_mode not in ("QR", "COD"):
        return error_response('payment_mode must be "QR" or "COD"')

    if payment_mode == "QR":
        if not re.fullmatch(r"\d{6}", transaction_ref):
            return error_response("transaction_ref must be exactly 6 digits for QR payment")
        payment_status = "waiting_confirmation"
    else:
        transaction_ref = None
        payment_status  = "pending"

    # ── Recalculate financials (NEVER trust frontend) ─────
    # Normalise items
    clean_items = []
    for item in items:
        clean_items.append({
            "name":     str(item["name"]).strip(),
            "price":    round(float(item["price"]), 2),
            "quantity": int(item["quantity"]),
        })

    subtotal    = calculate_subtotal(clean_items)
    final_total = round(subtotal + delivery_charge, 2)

    # ── Clean & build address ─────────────────────────────
    clean_address = {
        "door_no":          str(address["door_no"]).strip(),
        "street":           str(address["street"]).strip(),
        "nearest_location": str(address["nearest_location"]).strip(),
        "landmark":         str(address["landmark"]).strip(),
        # Optional fields
        "house_no":         str(address.get("house_no", "")).strip() or None,
        "apartment_name":   str(address.get("apartment_name", "")).strip() or None,
        "flat_no":          str(address.get("flat_no", "")).strip() or None,
        "maps_link":        str(address.get("maps_link", "")).strip() or None,
    }
    # Remove None optional fields for cleanliness
    clean_address = {k: v for k, v in clean_address.items() if v is not None or k in
                     ("door_no", "street", "nearest_location", "landmark")}

    # ── Generate order number ─────────────────────────────
    order_number = generate_order_number()

    # ── Build order document ──────────────────────────────
    order_doc = {
        "order_number":     order_number,
        "user_id":          user["_id"],
        "user_name":        user["name"],
        "user_phone":       user["phone"],
        "items":            clean_items,
        "subtotal":         subtotal,
        "delivery_charge":  delivery_charge,
        "final_total":      final_total,
        "area":             area,
        "address":          clean_address,
        "payment_mode":     payment_mode,
        "transaction_ref":  transaction_ref,
        "payment_status":   payment_status,
        "order_status":     "placed",
        "created_at":       datetime.now(timezone.utc),
    }

    # ── Save to MongoDB ───────────────────────────────────
    result = orders_col.insert_one(order_doc)

    return success_response(
        {
            "message":      "Order placed successfully",
            "order_number": order_number,
            "order_id":     str(result.inserted_id),
            "subtotal":     subtotal,
            "delivery":     delivery_charge,
            "final_total":  final_total,
            "payment_status": payment_status,
        },
        201,
    )


# ── Get My Orders ─────────────────────────────────────────────────────────────
@app.route("/api/orders/my", methods=["GET"])
def get_my_orders():
    """
    GET /api/orders/my
    Headers: Authorization: <token>
    Returns all orders for the authenticated user, latest first.
    """
    token = get_token_from_request()
    user  = validate_token(token)
    if not user:
        return error_response("Unauthorized — please login", 401)

    raw_orders = orders_col.find(
        {"user_id": user["_id"]},
        sort=[("created_at", DESCENDING)]
    )

    orders = []
    for order in raw_orders:
        o = serialize_doc(order)
        # Remove internal user_id from response (user already knows who they are)
        o.pop("user_id", None)
        # Remove password-hash-related noise (safety check)
        o.pop("password_hash", None)
        orders.append(o)

    return success_response(
        {
            "count":  len(orders),
            "orders": orders,
        }
    )


# ── Get Single Order ──────────────────────────────────────────────────────────
@app.route("/api/orders/<order_number>", methods=["GET"])
def get_order(order_number: str):
    """
    GET /api/orders/<order_number>
    Headers: Authorization: <token>
    Returns a single order (must belong to the authenticated user).
    """
    token = get_token_from_request()
    user  = validate_token(token)
    if not user:
        return error_response("Unauthorized — please login", 401)

    order = orders_col.find_one(
        {"order_number": order_number.upper(), "user_id": user["_id"]}
    )
    if not order:
        return error_response("Order not found", 404)

    o = serialize_doc(order)
    o.pop("user_id", None)

    return success_response({"order": o})


# ══════════════════════════════════════════════════════════════════════════════
#  ERROR HANDLERS
# ══════════════════════════════════════════════════════════════════════════════

@app.errorhandler(404)
def not_found(e):
    return jsonify({"success": False, "message": "Route not found"}), 404


@app.errorhandler(405)
def method_not_allowed(e):
    return jsonify({"success": False, "message": "Method not allowed"}), 405


@app.errorhandler(500)
def internal_error(e):
    return jsonify({"success": False, "message": "Internal server error"}), 500


# ══════════════════════════════════════════════════════════════════════════════
#  ENTRY POINT
# ══════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    port  = int(os.getenv("PORT", 5000))
    debug = os.getenv("FLASK_ENV", "production") == "development"
    print(f"🍽️  SV Tiffins API running on port {port}")
    app.run(host="0.0.0.0", port=port, debug=debug)
