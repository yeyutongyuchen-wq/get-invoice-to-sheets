import os
import uuid
import json
import base64
import requests
from datetime import datetime
from typing import List, Optional

from flask import Flask, request, jsonify, Response
from flask_cors import CORS
from openai import OpenAI
from pydantic import BaseModel
from requests.auth import HTTPBasicAuth

app = Flask(__name__)
CORS(app)

# ===================== 配置 =====================
OPENROUTER_API_KEY = os.getenv("OPENAI_API_KEY")
PAYPAL_CLIENT_ID = os.getenv("PAYPAL_CLIENT_ID")
PAYPAL_CLIENT_SECRET = os.getenv("PAYPAL_CLIENT_SECRET")
PAYPAL_API_BASE = "https://api-m.sandbox.paypal.com"  # 上线后改为 https://api-m.paypal.com

CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID")
CF_KV_NAMESPACE_ID = os.getenv("CF_KV_NAMESPACE_ID")
CF_API_TOKEN = os.getenv("CF_API_TOKEN")

RESEND_API_KEY = os.getenv("RESEND_API_KEY")

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

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

# ===================== Cloudflare KV =====================
def kv_put(key: str, value: dict, expiration_ttl: int = 3600):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{key}"
    headers = {
        "Authorization": f"Bearer {CF_API_TOKEN}",
        "Content-Type": "application/json"
    }
    params = {"expiration_ttl": expiration_ttl}
    resp = requests.put(url, headers=headers, params=params, data=json.dumps(value))
    resp.raise_for_status()
    return True

def kv_get(key: str):
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/storage/kv/namespaces/{CF_KV_NAMESPACE_ID}/values/{key}"
    headers = {"Authorization": f"Bearer {CF_API_TOKEN}"}
    resp = requests.get(url, headers=headers)
    if resp.status_code == 404:
        return None
    resp.raise_for_status()
    return resp.json()

# ===================== CSV & 邮件 =====================
def build_csv_content(data: dict) -> str:
    date = data.get("invoice_date") or ""
    supplier = data.get("vendor_name") or ""
    invoice_total = data.get("invoice_total") or 0
    line_items = data.get("line_items") or []

    lines = ["Date,Supplier,Description,Unit Price,Quantity,Line Total,Invoice Total"]
    if not line_items:
        lines.append(f'"{date}","{supplier}",,,,,"{invoice_total}"')
    else:
        for idx, item in enumerate(line_items):
            inv_col = f'"{invoice_total}"' if idx == 0 else ""
            lines.append(
                f'"{date}","{supplier}","{item.get("description","")}","{item.get("unit_price",0)}","{item.get("quantity",0)}","{item.get("amount",0)}",{inv_col}'
            )
    return "\uFEFF" + "\n".join(lines)

def send_csv_email(to_email: str, invoice_id: str, csv_content: str) -> bool:
    if not to_email or not RESEND_API_KEY:
        print("Skip email: missing email or RESEND_API_KEY")
        return False

    url = "https://api.resend.com/emails"
    headers = {
        "Authorization": f"Bearer {RESEND_API_KEY}",
        "Content-Type": "application/json"
    }

    b64_csv = base64.b64encode(csv_content.encode("utf-8")).decode("utf-8")

    payload = {
        "from": "GetInvoiceToSheets <onboarding@resend.dev>",
        "to": [to_email],
        "subject": "Your invoice CSV is ready – GetInvoiceToSheets",
        "html": """
            <p>Hi,</p>
            <p>Thanks for your purchase.</p>
            <p>Your invoice has been converted to CSV. The file is attached to this email.</p>
            <p>You can also download it again within 1 hour from the website.</p>
            <p>– GetInvoiceToSheets</p>
        """,
        "attachments": [
            {
                "filename": f"invoice_{invoice_id[:8]}.csv",
                "content": b64_csv
            }
        ]
    }

    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"Email sent to {to_email}")
        return True
    except Exception as e:
        print(f"Send email failed: {e}")
        return False

# ===================== 上传识别 =====================
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
            model="openai/gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": (
                        "You are an expert invoice data extractor. "
                        "Extract structured data from the invoice image accurately. "
                        "Rules:\n"
                        "1. Extract every product or service as a line item.\n"
                        "2. If there is any tax (Sales Tax, VAT, GST, etc.), "
                        "add it as an additional line item.\n"
                        "3. Do not invent information.\n"
                        "4. The sum of line items should match invoice_total as closely as possible."
                    )
                },
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Extract all data from this invoice. Include tax as a line item if present."},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64_image}"}}
                    ]
                }
            ],
            response_format=InvoiceExtraction,
            max_tokens=4096,
        )
        result = completion.choices[0].message.parsed
        full_data = result.model_dump()

        invoice_id = str(uuid.uuid4())

        kv_put(f"invoice:{invoice_id}", {
            "data": full_data,
            "unlocked": False,
            "customer_email": "",
            "created_at": datetime.utcnow().isoformat()
        }, expiration_ttl=3600)

        preview_data = full_data.copy()
        preview_data["line_items"] = full_data["line_items"][:3]
        preview_data["invoice_id"] = invoice_id
        preview_data["is_preview"] = True
        preview_data["total_lines"] = len(full_data["line_items"])
        preview_data["unlocked"] = False

        return jsonify(preview_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== 获取发票数据 =====================
@app.route("/api/invoice/<invoice_id>", methods=["GET"])
def get_invoice(invoice_id):
    record = kv_get(f"invoice:{invoice_id}")
    if not record:
        return jsonify({"error": "Invoice not found or expired"}), 404

    data = record["data"]
    unlocked = record.get("unlocked", False)

    if not unlocked:
        preview = data.copy()
        preview["line_items"] = data["line_items"][:3]
        preview["invoice_id"] = invoice_id
        preview["is_preview"] = True
        preview["total_lines"] = len(data["line_items"])
        preview["unlocked"] = False
        return jsonify(preview)

    full = data.copy()
    full["invoice_id"] = invoice_id
    full["is_preview"] = False
    full["total_lines"] = len(data["line_items"])
    full["unlocked"] = True
    return jsonify(full)

# ===================== PayPal =====================
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
        data = request.get_json() or {}
        invoice_id = data.get("invoice_id")
        customer_email = (data.get("email") or "").strip()

        if not invoice_id:
            return jsonify({"error": "invoice_id required"}), 400

        # 把邮箱存进该发票记录
        record = kv_get(f"invoice:{invoice_id}")
        if record:
            record["customer_email"] = customer_email
            kv_put(f"invoice:{invoice_id}", record, expiration_ttl=3600)

        access_token = get_paypal_access_token()
        url = f"{PAYPAL_API_BASE}/v2/checkout/orders"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {access_token}"
        }

        return_url = "https://get-invoice-to-sheets.pages.dev"
        cancel_url = "https://get-invoice-to-sheets.pages.dev"

        payload = {
            "intent": "CAPTURE",
            "purchase_units": [{
                "amount": {
                    "currency_code": "USD",
                    "value": "1.99"
                },
                "description": "GetInvoiceToSheets - Unlock full CSV",
                "custom_id": invoice_id
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

        invoice_id = None
        try:
            invoice_id = result["purchase_units"][0]["payments"]["captures"][0].get("custom_id")
        except Exception:
            try:
                invoice_id = result["purchase_units"][0].get("custom_id")
            except Exception:
                pass

        if not invoice_id:
            return jsonify({"error": "invoice_id not found in payment"}), 400

        record = kv_get(f"invoice:{invoice_id}")
        if record:
            record["unlocked"] = True
            kv_put(f"invoice:{invoice_id}", record, expiration_ttl=3600)

            # 决定收件邮箱：优先用户填写的，其次 PayPal 付款邮箱
            to_email = (record.get("customer_email") or "").strip()
            if not to_email:
                try:
                    to_email = result.get("payer", {}).get("email_address", "")
                except Exception:
                    to_email = ""

            if to_email:
                csv_content = build_csv_content(record["data"])
                send_csv_email(to_email, invoice_id, csv_content)

        return jsonify({
            "status": "COMPLETED",
            "invoice_id": invoice_id
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ===================== 下载完整 CSV =====================
@app.route("/api/download/<invoice_id>", methods=["GET"])
def download_csv(invoice_id):
    record = kv_get(f"invoice:{invoice_id}")
    if not record:
        return jsonify({"error": "Invoice not found or expired"}), 404

    if not record.get("unlocked"):
        return jsonify({"error": "Payment required"}), 403

    csv_content = build_csv_content(record["data"])
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename=invoice_{invoice_id[:8]}.csv"
        }
    )

# ===================== 启动 =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
