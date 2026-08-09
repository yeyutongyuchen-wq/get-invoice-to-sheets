import os
import uuid
import sqlite3
import base64
from datetime import datetime, timedelta
from typing import List, Optional

from flask import Flask, request, jsonify, g
from flask_cors import CORS
from openai import OpenAI
from pydantic import BaseModel
import requests
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
CORS(app)

# ===================== 配置 =====================
OPENROUTER_API_KEY = os.getenv("OPENAI_API_KEY")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com"  # 上线后改为 https://api-m.paypal.com

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# ===================== 数据库 =====================
def get_db():
    db = getattr(g, "_database", None)
    if db is None:
        db = g._database = sqlite3.connect("getinvoice.db")
        db.row_factory = sqlite3.Row
    return db

@app.teardown_appcontext
def close_connection(exception):
    db = getattr(g, "_database", None)
    if db is not None:
        db.close()

def init_db():
    with app.app_context():
        db = get_db()
        db.execute("""
            CREATE TABLE IF NOT EXISTS download_tokens (
                token TEXT PRIMARY KEY,
                order_id TEXT,
                email TEXT,
                created_at TEXT,
                expires_at TEXT,
                used INTEGER DEFAULT 0
            )
        """)
        db.commit()

init_db()

# ===================== Pydantic 模型 =====================
class LineItem(BaseModel):
    description: str
    quantity: float
    unit_price: float
    amount: float

class InvoiceExtraction(BaseModel):
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    vendor_name: Optional[str] = None
    invoice_total: float
    currency: Optional[str] = "USD"
    line_items: List[LineItem]

# ===================== 发票识别接口 =====================
@app.route("/upload", methods=["POST"])
def upload():
    if "file" not in request.files:
        return jsonify({"error": "No file uploaded"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"error": "No selected file"}), 400

    image_bytes = file.read()
    base64_image = base64.b64encode(image_bytes).decode("utf-8")
    mime = file.mimetype or "image/jpeg"

    try:
        completion = client.beta.chat.completions.parse(
            model="qwen/qwen2.5-vl-72b-instruct",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert invoice data extractor. "
                        "Extract structured data from the invoice image accurately. "
                        "Rules you must follow strictly:\n"
                        "1. Extract every product or service as a line item.\n"
                        "2. If there is any tax (Sales Tax, VAT, GST, etc.), "
                        "you MUST add it as an additional line item. "
                        "Example: description='Sales Tax 6.25%', quantity=1, unit_price=9.06, amount=9.06.\n"
                        "3. Do not invent any information that is not clearly present in the invoice.\n"
                        "4. The sum of all line item amounts (including tax) should match the invoice_total as closely as possible.\n"
                        "5. If a field is not found, return null or empty string."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "text",
                            "text": (
                                "Extract all data from this invoice. "
                                "Remember: tax must be included as a separate line item if it exists."
                            )
                        },
                        {
                            "type": "image_url",
                            "image_url": {
                                "url": f"data:{mime};base64,{base64_image}"
                            }
                        }
                    ]
                }
            ],
            response_format=InvoiceExtraction,
        )
        result = completion.choices[0].message.parsed
        return jsonify(result.model_dump())
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== PayPal 相关 =====================
def get_paypal_access_token():
    url = f"{PAYPAL_API_BASE}/v1/oauth2/token"
    response = requests.post(
        url,
        headers={"Accept": "application/json", "Accept-Language": "en_US"},
        data={"grant_type": "client_credentials"},
        auth=HTTPBasicAuth(PAYPAL_CLIENT_ID, PAYPAL_CLIENT_SECRET)
    )
    response.raise_for_status()
    return response.json()["access_token"]

@app.route("/api/create-order", methods=["POST"])
def create_order():
    try:
        access_token = get_paypal_access_token()
        url = f"{PAYPAL_API_BASE}/v2/checkout/orders"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        # 本地测试用，正式上线请改成你的真实域名
        return_url = "https://get-invoice-to-sheets.pages.dev"
        cancel_url = "https://get-invoice-to-sheets.pages.dev"

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": "USD",
                    "value": "1.99"
                },
                "description": "GetInvoiceToSheets - Unlock full CSV download"
            }],
            "application_context": {
                "return_url": return_url,
                "cancel_url": cancel_url,
                "brand_name": "GetInvoiceToSheets",
                "user_action": "PAY_NOW",
                "shipping_preference": "NO_SHIPPING"
            }
        }

        response = requests.post(url, headers=headers, json=payload)
        response.raise_for_status()
        order = response.json()

        approve_link = None
        for link in order.get("links", []):
            if link.get("rel") == "approve":
                approve_link = link.get("href")
                break

        if not approve_link:
            return jsonify({"error": "No approve link found"}), 500

        return jsonify({
            "id": order["id"],
            "approve_url": approve_link
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/capture-order/<order_id>", methods=["POST"])
def capture_order(order_id):
    try:
        access_token = get_paypal_access_token()
        url = f"{PAYPAL_API_BASE}/v2/checkout/orders/{order_id}/capture"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }
        response = requests.post(url, headers=headers)
        response.raise_for_status()
        result = response.json()

        if result.get("status") != "COMPLETED":
            return jsonify({"error": "Payment not completed"}), 400

        email = "unknown@example.com"
        try:
            email = result["payer"]["email_address"]
        except Exception:
            pass

        token = str(uuid.uuid4())
        now = datetime.utcnow()
        expires = now + timedelta(minutes=30)

        db = get_db()
        db.execute(
            "INSERT INTO download_tokens (token, order_id, email, created_at, expires_at) VALUES (?, ?, ?, ?, ?)",
            (token, order_id, email, now.isoformat(), expires.isoformat())
        )
        db.commit()

        return jsonify({
            "status": "COMPLETED",
            "download_token": token,
            "email": email,
            "expires_in_minutes": 30
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route("/api/verify-token", methods=["POST"])
def verify_token():
    data = request.get_json() or {}
    token = data.get("token")
    if not token:
        return jsonify({"valid": False}), 400

    db = get_db()
    row = db.execute(
        "SELECT * FROM download_tokens WHERE token = ? AND used = 0",
        (token,)
    ).fetchone()

    if not row:
        return jsonify({"valid": False})

    expires = datetime.fromisoformat(row["expires_at"])
    if datetime.utcnow() > expires:
        return jsonify({"valid": False, "reason": "expired"})

    return jsonify({"valid": True, "email": row["email"]})

# ===================== 启动 =====================
if __name__ == "__main__":
    print("Server starting on http://127.0.0.1:5000")
    app.run(debug=True, port=5000)