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

CF_ACCOUNT_ID = "a4f3350009511d06ed181ec82abeee12"
CF_KV_NAMESPACE_ID = "04fe72a9716144cb80987b80c82f50c9"
CF_API_TOKEN = "cfut_ctROCXTKj39qhu89NjHNZmTrQqOQSoqSnwpyCqsyf4ab30d2"

client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=OPENROUTER_API_KEY,
)

# ===================== Pydantic =====================
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

# ===================== 上传识别（只返回前3行） =====================
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
                        "2. If there is any tax (Sales Tax, VAT, GST, etc.), add it as an additional line item.\n"
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

        # 完整数据存入 Cloudflare KV（1小时）
        kv_put(f"invoice:{invoice_id}", {
            "data": full_data,
            "unlocked": False,
            "created_at": datetime.utcnow().isoformat()
        }, expiration_ttl=3600)

        # 只返回前3行预览
        preview_data = full_data.copy()
        preview_data["line_items"] = full_data["line_items"][:3]
        preview_data["invoice_id"] = invoice_id
        preview_data["is_preview"] = True
        preview_data["total_lines"] = len(full_data["line_items"])

        return jsonify(preview_data)

    except Exception as e:
        return jsonify({"error": str(e)}), 500

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
        if not invoice_id:
            return jsonify({"error": "invoice_id required"}), 400

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

        # 取出 custom_id（invoice_id）
        invoice_id = None
        try:
            invoice_id = result["purchase_units"][0]["payments"]["captures"][0].get("custom_id")
        except Exception:
            try:
                invoice_id = result["purchase_units"][0].get("custom_id")
            except Exception:
                pass

        if not invoice_id:
            return jsonify({"error": "invoice_id not found"}), 400

        # 标记已解锁
        record = kv_get(f"invoice:{invoice_id}")
        if record:
            record["unlocked"] = True
            kv_put(f"invoice:{invoice_id}", record, expiration_ttl=3600)

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

    data = record["data"]
    date = data.get("invoice_date") or ""
    supplier = data.get("vendor_name") or ""
    invoice_total = data.get("invoice_total") or 0

    csv_lines = ["Date,Supplier,Description,Unit Price,Quantity,Line Total,Invoice Total"]
    line_items = data.get("line_items") or []

    if not line_items:
        csv_lines.append(f'"{date}","{supplier}",,,,,"{invoice_total}"')
    else:
        for idx, item in enumerate(line_items):
            inv_col = f'"{invoice_total}"' if idx == 0 else ""
            csv_lines.append(
                f'"{date}","{supplier}","{item.get("description","")}","{item.get("unit_price",0)}","{item.get("quantity",0)}","{item.get("amount",0)}",{inv_col}'
            )

    csv_content = "\uFEFF" + "\n".join(csv_lines)
    return Response(
        csv_content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=invoice_{invoice_id[:8]}.csv"}
    )

# ===================== 启动 =====================
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
