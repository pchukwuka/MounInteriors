"""
app.py — MOUN Digital Platform Flask Backend
Endpoints:
  POST /api/orders          — Submit a new order (receipt upload + DB save)
  GET  /api/orders          — Admin: list all orders
  GET  /api/orders/<id>     — Admin: single order with items
  PUT  /api/orders/<id>     — Admin: update payment/order status
  GET  /admin               — Admin dashboard (password + email OTP protected)
  POST /admin/send-otp      — Send OTP to admin email
  POST /admin/verify-otp    — Verify OTP and issue session token
  GET  /health              — Health check for Cloud Run
"""

import os
import json
import random
import string
import smtplib
import secrets
from datetime import datetime, timezone, timedelta
from functools import wraps
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

from flask import Flask, request, jsonify, render_template_string, abort
from flask_cors import CORS
from dotenv import load_dotenv

from db import get_connection, init_db
from storage import upload_receipt

# ── Setup ──────────────────────────────────────────────────────
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

CORS(app, origins="*", supports_credentials=True)

# ── In-memory OTP store ────────────────────────────────────────
# { token: { 'otp': '123456', 'expires_at': datetime, 'verified': bool } }
otp_store = {}


# ── Email helper ───────────────────────────────────────────────

def send_otp_email(otp_code: str) -> bool:
    """
    Send OTP to admin email using Gmail SMTP.
    Set SMTP_EMAIL, SMTP_PASSWORD, and ADMIN_EMAIL in your .env file.
    """
    smtp_email    = os.environ.get('SMTP_EMAIL', '')
    smtp_password = os.environ.get('SMTP_PASSWORD', '')
    admin_email   = os.environ.get('ADMIN_EMAIL', smtp_email)

    if not smtp_email or not smtp_password:
        app.logger.error("SMTP_EMAIL or SMTP_PASSWORD not set.")
        return False

    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = f'MOUN Admin — Your verification code: {otp_code}'
        msg['From']    = smtp_email
        msg['To']      = admin_email

        text_body = f"""
MOUN Admin Dashboard — Verification Code

Your one-time code is: {otp_code}

This code expires in 10 minutes.
Do not share this code with anyone.

— MOUN Security
        """.strip()

        html_body = f"""
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#F5EDE0;font-family:sans-serif;">
  <div style="max-width:480px;margin:40px auto;background:#fff;border-radius:12px;overflow:hidden;">
    <div style="background:#1E150A;padding:24px 32px;">
      <h1 style="color:#C9A97A;font-size:22px;margin:0;font-weight:500;">moun.</h1>
      <p style="color:rgba(245,239,230,0.6);font-size:12px;margin:4px 0 0;">Admin Dashboard</p>
    </div>
    <div style="padding:32px;">
      <p style="color:#1E150A;font-size:15px;margin:0 0 8px;">Your verification code is:</p>
      <div style="background:#F5EDE0;border-radius:10px;padding:20px;text-align:center;margin:16px 0;">
        <span style="font-size:36px;font-weight:700;letter-spacing:10px;color:#1E150A;">{otp_code}</span>
      </div>
      <p style="color:#7A6A52;font-size:13px;margin:0 0 6px;">This code expires in <strong>10 minutes</strong>.</p>
      <p style="color:#7A6A52;font-size:13px;margin:0;">Do not share this code with anyone.</p>
    </div>
    <div style="background:#F5EDE0;padding:16px 32px;text-align:center;">
      <p style="color:#C9A97A;font-size:11px;margin:0;">© 2025 MOUN — Interior Design & Home Decor, Kigali</p>
    </div>
  </div>
</body>
</html>
        """.strip()

        msg.attach(MIMEText(text_body, 'plain'))
        msg.attach(MIMEText(html_body, 'html'))

        with smtplib.SMTP_SSL('smtp.gmail.com', 465) as server:
            server.login(smtp_email, smtp_password)
            server.sendmail(smtp_email, admin_email, msg.as_string())

        app.logger.info(f"OTP sent to {admin_email}")
        return True

    except Exception as e:
        app.logger.error(f"Failed to send OTP: {e}")
        return False


def generate_otp() -> str:
    return ''.join(random.choices(string.digits, k=6))


def generate_session_token() -> str:
    return secrets.token_hex(32)


def clean_expired_otps():
    now     = datetime.now(timezone.utc)
    expired = [k for k, v in otp_store.items() if v['expires_at'] < now]
    for k in expired:
        del otp_store[k]


# ── Admin session decorator ────────────────────────────────────

def require_admin_session(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        token = request.args.get('session') or request.headers.get('X-Session-Token')
        if not token or token not in otp_store:
            return jsonify({'error': 'Unauthorised — session required'}), 401
        entry = otp_store[token]
        if not entry.get('verified'):
            return jsonify({'error': 'Unauthorised — OTP not verified'}), 401
        if entry['expires_at'] < datetime.now(timezone.utc):
            del otp_store[token]
            return jsonify({'error': 'Session expired — please log in again'}), 401
        return f(*args, **kwargs)
    return decorated


# ── Health check ──────────────────────────────────────────────

@app.route('/health')
def health():
    return jsonify({'status': 'ok'}), 200


# ── POST /admin/send-otp ──────────────────────────────────────

@app.route('/admin/send-otp', methods=['POST'])
def send_otp():
    """
    Step 1 of admin login.
    Verifies password then sends 6-digit OTP to admin email.
    Body: { "password": "your-admin-password" }
    """
    data           = request.get_json() or {}
    password       = data.get('password', '')
    admin_password = os.environ.get('ADMIN_PASSWORD', '')

    if not admin_password or password != admin_password:
        return jsonify({'error': 'Incorrect password'}), 401

    otp        = generate_otp()
    temp_token = generate_session_token()
    expires_at = datetime.now(timezone.utc) + timedelta(minutes=10)

    clean_expired_otps()
    otp_store[temp_token] = {
        'otp':        otp,
        'expires_at': expires_at,
        'verified':   False,
    }

    sent = send_otp_email(otp)
    if not sent:
        del otp_store[temp_token]
        return jsonify({'error': 'Failed to send OTP email. Check SMTP settings in .env'}), 500

    admin_email = os.environ.get('ADMIN_EMAIL', os.environ.get('SMTP_EMAIL', ''))
    masked      = admin_email
    if '@' in admin_email:
        local, domain = admin_email.split('@', 1)
        masked = (local[0] + '***' + local[-1] if len(local) > 2 else '***') + '@' + domain

    return jsonify({
        'success':    True,
        'temp_token': temp_token,
        'email_hint': masked,
        'message':    f'OTP sent to {masked}. Expires in 10 minutes.',
    }), 200


# ── POST /admin/verify-otp ────────────────────────────────────

@app.route('/admin/verify-otp', methods=['POST'])
def verify_otp():
    """
    Step 2 of admin login.
    Verifies OTP and returns a verified session token.
    Body: { "temp_token": "...", "otp": "123456" }
    """
    data       = request.get_json() or {}
    temp_token = data.get('temp_token', '')
    otp_input  = data.get('otp', '').strip()

    clean_expired_otps()

    if temp_token not in otp_store:
        return jsonify({'error': 'Invalid or expired session. Please start again.'}), 401

    entry = otp_store[temp_token]

    if entry['expires_at'] < datetime.now(timezone.utc):
        del otp_store[temp_token]
        return jsonify({'error': 'OTP expired. Please request a new one.'}), 401

    if entry['verified']:
        return jsonify({'error': 'Token already used.'}), 400

    if otp_input != entry['otp']:
        return jsonify({'error': 'Incorrect OTP. Please try again.'}), 401

    # Mark verified — extend session to 2 hours
    entry['verified']   = True
    entry['expires_at'] = datetime.now(timezone.utc) + timedelta(hours=2)

    return jsonify({
        'success':       True,
        'session_token': temp_token,
        'message':       'OTP verified. Access granted.',
    }), 200


# ── POST /api/orders ─────────────────────────────────────────

@app.route('/api/orders', methods=['POST'])
def create_order():
    raw = request.form.get('order_data')
    if not raw:
        return jsonify({'error': 'order_data is required'}), 400

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({'error': 'order_data must be valid JSON'}), 400

    required = ['customer_name', 'phone', 'delivery_method', 'total_amount', 'items']
    for field in required:
        if not data.get(field):
            return jsonify({'error': f'Missing required field: {field}'}), 400

    if data['delivery_method'] not in ('pickup', 'delivery'):
        return jsonify({'error': 'delivery_method must be pickup or delivery'}), 400

    if data['delivery_method'] == 'delivery' and not data.get('address'):
        return jsonify({'error': 'address is required for delivery orders'}), 400

    items = data['items']
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({'error': 'items must be a non-empty list'}), 400

    receipt_url  = None
    receipt_file = request.files.get('receipt')

    if receipt_file and receipt_file.filename:
        # Use Firebase Storage if credentials are available, otherwise skip receipt storage.
        # Local disk is NOT used — Cloud Run containers are ephemeral.
        firebase_creds = os.environ.get('FIREBASE_CREDENTIALS')
        if firebase_creds:
            try:
                receipt_url = upload_receipt(receipt_file, receipt_file.filename)
            except Exception as e:
                app.logger.error(f"Firebase receipt upload failed: {e}")
                # Non-fatal: order is still saved, receipt just won't be stored
        else:
            app.logger.warning("FIREBASE_CREDENTIALS not set — receipt not stored.")

    conn = get_connection()
    cur  = conn.cursor()

    try:
        cur.execute("""
            INSERT INTO orders
              (customer_name, phone, delivery_method, address, delivery_note,
               total_amount, receipt_url)
            VALUES (%s, %s, %s, %s, %s, %s, %s)
            RETURNING id
        """, (
            str(data.get('customer_name') or '').strip() or 'Unknown',
            str(data.get('phone') or '').strip() or 'Unknown',
            data['delivery_method'],
            str(data.get('address') or '').strip() or None,
            str(data.get('delivery_note') or '').strip() or None,
            int(data['total_amount']),
            receipt_url,
        ))

        order_id = cur.fetchone()['id']

        for item in items:
            options_str = item.get('options', '')
            print_name  = None
            size        = None
            for part in options_str.split(','):
                part = part.strip()
                if part.lower().startswith('print:'):
                    print_name = part.split(':', 1)[1].strip()
                elif part.lower().startswith('size:'):
                    size = part.split(':', 1)[1].strip()
                elif part.lower().startswith('colour:'):
                    print_name = part.split(':', 1)[1].strip()

            cur.execute("""
                INSERT INTO order_items
                  (order_id, product_name, print_name, size, quantity, price)
                VALUES (%s, %s, %s, %s, %s, %s)
            """, (
                order_id,
                item.get('name', 'Unknown product'),
                print_name,
                size,
                int(item.get('qty', 1)),
                int(item.get('price_int', 0)),
            ))

        conn.commit()

    except Exception as e:
        conn.rollback()
        app.logger.error(f"Database error: {e}")
        return jsonify({'error': 'Failed to save order. Please try again.'}), 500

    finally:
        cur.close()
        conn.close()

    return jsonify({
        'success':     True,
        'order_id':    order_id,
        'receipt_url': receipt_url,
        'message':     'Order saved successfully',
    }), 201


# ── GET /api/orders ──────────────────────────────────────────

@app.route('/api/orders', methods=['GET'])
@require_admin_session
def list_orders():
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("""
        SELECT o.id, o.customer_name, o.phone, o.delivery_method,
               o.address, o.delivery_note, o.total_amount,
               o.payment_status, o.order_status, o.receipt_url,
               o.created_at, COUNT(oi.id) AS item_count
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        GROUP BY o.id
        ORDER BY o.created_at DESC
    """)
    orders = [dict(row) for row in cur.fetchall()]
    for order in orders:
        if order.get('created_at'):
            order['created_at'] = order['created_at'].isoformat()
    cur.close()
    conn.close()
    return jsonify({'orders': orders}), 200


# ── GET /api/orders/<id> ─────────────────────────────────────

@app.route('/api/orders/<int:order_id>', methods=['GET'])
@require_admin_session
def get_order(order_id):
    conn = get_connection()
    cur  = conn.cursor()
    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = cur.fetchone()
    if not order:
        cur.close(); conn.close()
        return jsonify({'error': 'Order not found'}), 404
    order = dict(order)
    if order.get('created_at'):
        order['created_at'] = order['created_at'].isoformat()
    cur.execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
    items = [dict(row) for row in cur.fetchall()]
    cur.close(); conn.close()
    return jsonify({'order': order, 'items': items}), 200


# ── PUT /api/orders/<id> ─────────────────────────────────────

@app.route('/api/orders/<int:order_id>', methods=['PUT'])
@require_admin_session
def update_order(order_id):
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    valid_payment = ('pending', 'confirmed', 'rejected')
    valid_order   = ('new', 'processing', 'shipped', 'delivered', 'cancelled')
    updates = []; values = []

    if 'payment_status' in data:
        if data['payment_status'] not in valid_payment:
            return jsonify({'error': 'Invalid payment_status'}), 400
        updates.append('payment_status = %s')
        values.append(data['payment_status'])

    if 'order_status' in data:
        if data['order_status'] not in valid_order:
            return jsonify({'error': 'Invalid order_status'}), 400
        updates.append('order_status = %s')
        values.append(data['order_status'])

    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400

    values.append(order_id)
    conn = get_connection(); cur = conn.cursor()
    cur.execute(f"UPDATE orders SET {', '.join(updates)} WHERE id = %s RETURNING id", values)
    updated = cur.fetchone()
    conn.commit(); cur.close(); conn.close()

    if not updated:
        return jsonify({'error': 'Order not found'}), 404
    return jsonify({'success': True, 'order_id': order_id}), 200



# ── Root redirect ──────────────────────────────────────────────
# The frontend is served separately (Vercel / Netlify / GitHub Pages).
# Visiting the backend root redirects to the admin dashboard.

@app.route('/')
def serve_index():
    from flask import redirect
    return redirect('/admin')


# ── GET /admin ───────────────────────────────────────────────

ADMIN_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>MOUN — Admin Orders</title>
  <style>
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body { font-family: system-ui, sans-serif; background: #f5f5f5; color: #222; }

    .login-screen {
      min-height: 100vh; background: #1E150A;
      display: flex; align-items: center; justify-content: center; padding: 20px;
    }
    .login-card {
      background: #fff; border-radius: 16px; padding: 40px 36px;
      width: 100%; max-width: 400px; text-align: center;
    }
    .login-logo { font-size: 28px; font-weight: 500; color: #1E150A; margin-bottom: 4px; }
    .login-logo span { color: #8B6C4A; }
    .login-subtitle { font-size: 12px; color: #7A6A52; margin-bottom: 28px; }
    .login-step-title { font-size: 16px; font-weight: 600; color: #1E150A; margin-bottom: 6px; }
    .login-step-sub { font-size: 12px; color: #7A6A52; margin-bottom: 20px; line-height: 1.5; }
    .login-input {
      width: 100%; padding: 12px 16px; border: 1.5px solid #E0D4C0;
      border-radius: 10px; font-size: 14px; margin-bottom: 12px;
      outline: none; transition: border-color 0.2s;
    }
    .login-input:focus { border-color: #8B6C4A; }
    .otp-input { font-size: 28px; font-weight: 700; letter-spacing: 12px; text-align: center; }
    .login-btn {
      width: 100%; padding: 13px; background: #8B6C4A; color: #F5EFE6;
      border: none; border-radius: 10px; font-size: 14px; font-weight: 500;
      cursor: pointer; transition: background 0.2s;
    }
    .login-btn:hover { background: #7A5C3A; }
    .login-btn:disabled { background: #C9B09A; cursor: not-allowed; }
    .login-error {
      background: #FDECEA; color: #B71C1C; border-radius: 8px;
      padding: 10px 14px; font-size: 13px; margin-bottom: 12px; display: none;
    }
    .login-success-msg {
      background: #EAF3DE; color: #2E7D32; border-radius: 8px;
      padding: 10px 14px; font-size: 13px; margin-bottom: 12px; display: none;
    }
    .resend-link {
      font-size: 12px; color: #8B6C4A; cursor: pointer;
      margin-top: 12px; display: inline-block; text-decoration: underline;
    }
    .step-indicator { display: flex; align-items: center; justify-content: center; gap: 8px; margin-bottom: 24px; }
    .step-dot { width: 8px; height: 8px; border-radius: 50%; background: #E0D4C0; }
    .step-dot.active { background: #8B6C4A; }
    .step-dot.done { background: #4CAF50; }

    .dashboard { display: none; }
    .header {
      background: #1E150A; color: #F7F3EE; padding: 1rem 2rem;
      display: flex; align-items: center; justify-content: space-between;
    }
    .header-logo { font-size: 1.2rem; font-weight: 500; color: #C9A97A; }
    .header-right { display: flex; align-items: center; gap: 1rem; }
    .header-sub { font-size: 0.8rem; opacity: 0.6; }
    .logout-btn {
      padding: 0.35rem 0.85rem; background: transparent;
      border: 1px solid rgba(201,169,122,0.4); color: #C9A97A;
      border-radius: 6px; font-size: 0.8rem; cursor: pointer;
    }
    .toolbar {
      padding: 1rem 2rem; display: flex; gap: 1rem;
      align-items: center; background: #fff;
      border-bottom: 1px solid #e0e0e0; flex-wrap: wrap;
    }
    .toolbar select { padding: 0.45rem 0.75rem; border: 1px solid #ccc; border-radius: 6px; font-size: 0.88rem; }
    .toolbar button { padding: 0.45rem 1rem; background: #8B6C4A; color: #fff; border: none; border-radius: 6px; cursor: pointer; font-size: 0.88rem; }
    .stats { display: flex; gap: 1rem; padding: 1rem 2rem; flex-wrap: wrap; }
    .stat-card { background: #fff; border-radius: 8px; padding: 1rem 1.5rem; min-width: 160px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    .stat-card .label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.06em; }
    .stat-card .value { font-size: 1.6rem; font-weight: 700; color: #1E150A; margin-top: 4px; }
    .table-wrap { padding: 0 2rem 2rem; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    th { background: #1E150A; color: #F7F3EE; padding: 0.75rem 1rem; text-align: left; font-size: 0.78rem; text-transform: uppercase; white-space: nowrap; }
    td { padding: 0.75rem 1rem; border-bottom: 1px solid #f0f0f0; font-size: 0.88rem; vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fafafa; }
    .badge { display: inline-block; padding: 2px 8px; border-radius: 20px; font-size: 0.72rem; font-weight: 700; text-transform: uppercase; }
    .badge--pending    { background: #FFF3CD; color: #856404; }
    .badge--confirmed  { background: #D4EDDA; color: #155724; }
    .badge--rejected   { background: #F8D7DA; color: #721C24; }
    .badge--new        { background: #D1ECF1; color: #0C5460; }
    .badge--processing { background: #CCE5FF; color: #004085; }
    .badge--shipped    { background: #E2D9F3; color: #432874; }
    .badge--delivered  { background: #D4EDDA; color: #155724; }
    .badge--cancelled  { background: #F8D7DA; color: #721C24; }
    .receipt-link { color: #8B6C4A; text-decoration: none; font-weight: 600; }
    .receipt-link:hover { text-decoration: underline; }
    .modal-overlay { display: none; position: fixed; inset: 0; background: rgba(0,0,0,0.6); z-index: 100; align-items: center; justify-content: center; }
    .modal-overlay.open { display: flex; }
    .modal { background: #fff; border-radius: 10px; width: 90%; max-width: 640px; max-height: 90vh; overflow-y: auto; padding: 1.5rem; position: relative; }
    .modal h2 { font-size: 1.1rem; margin-bottom: 1rem; color: #1E150A; }
    .modal-close { position: absolute; top: 1rem; right: 1rem; background: none; border: none; font-size: 1.25rem; cursor: pointer; color: #888; }
    .detail-row { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; font-size: 0.88rem; }
    .detail-row strong { min-width: 140px; color: #888; }
    .items-table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.85rem; }
    .items-table th { background: #f5f5f5; padding: 0.5rem; text-align: left; font-size: 0.75rem; color: #888; }
    .items-table td { padding: 0.5rem; border-top: 1px solid #f0f0f0; }
    .receipt-img { max-width: 100%; border-radius: 8px; margin-top: 1rem; border: 1px solid #eee; }
    .status-select { padding: 0.3rem 0.5rem; border-radius: 4px; border: 1px solid #ccc; font-size: 0.8rem; }
    .save-btn { padding: 0.3rem 0.65rem; background: #8B6C4A; color: #fff; border: none; border-radius: 4px; font-size: 0.78rem; cursor: pointer; margin-left: 4px; }
    #loading { text-align: center; padding: 3rem; color: #888; }
    #error-msg { color: #c0392b; padding: 1rem 2rem; }
  </style>
</head>
<body>

<!-- LOGIN SCREEN -->
<div class="login-screen" id="login-screen">
  <div class="login-card">
    <div class="login-logo">moun<span>.</span></div>
    <div class="login-subtitle">Admin Dashboard</div>
    <div class="step-indicator">
      <div class="step-dot active" id="dot-1"></div>
      <div class="step-dot" id="dot-2"></div>
    </div>

    <!-- Step 1: Password -->
    <div id="step-1">
      <div class="login-step-title">Enter your password</div>
      <div class="login-step-sub">Step 1 of 2 — Password verification</div>
      <div class="login-error" id="pw-error"></div>
      <input class="login-input" type="password" id="pw-input" placeholder="Admin password" autocomplete="current-password" />
      <button class="login-btn" id="pw-btn" onclick="submitPassword()">Continue →</button>
    </div>

    <!-- Step 2: OTP -->
    <div id="step-2" style="display:none;">
      <div class="login-step-title">Check your email</div>
      <div class="login-step-sub" id="otp-hint-text">Enter the 6-digit code sent to your email.</div>
      <div class="login-error" id="otp-error"></div>
      <div class="login-success-msg" id="otp-success-msg"></div>
      <input class="login-input otp-input" type="text" id="otp-input" placeholder="000000" maxlength="6" inputmode="numeric" autocomplete="one-time-code" />
      <button class="login-btn" id="otp-btn" onclick="submitOtp()">Verify & Enter Dashboard</button>
      <div><span class="resend-link" onclick="resendOtp()">Resend code</span></div>
    </div>
  </div>
</div>

<!-- DASHBOARD -->
<div class="dashboard" id="dashboard">
  <div class="header">
    <div class="header-logo">moun. <span style="font-size:0.75rem;opacity:0.5;font-weight:400;">admin</span></div>
    <div class="header-right">
      <span class="header-sub" id="last-updated"></span>
      <button class="logout-btn" onclick="logout()">Sign out</button>
    </div>
  </div>

  <div class="toolbar">
    <select id="filter-payment">
      <option value="">All payment statuses</option>
      <option value="pending">Pending</option>
      <option value="confirmed">Confirmed</option>
      <option value="rejected">Rejected</option>
    </select>
    <select id="filter-order">
      <option value="">All order statuses</option>
      <option value="new">New</option>
      <option value="processing">Processing</option>
      <option value="shipped">Shipped</option>
      <option value="delivered">Delivered</option>
      <option value="cancelled">Cancelled</option>
    </select>
    <button onclick="loadOrders()">🔄 Refresh</button>
  </div>

  <div class="stats">
    <div class="stat-card"><div class="label">Total Orders</div><div class="value" id="stat-total">—</div></div>
    <div class="stat-card"><div class="label">Pending Payment</div><div class="value" id="stat-pending">—</div></div>
    <div class="stat-card"><div class="label">Confirmed</div><div class="value" id="stat-confirmed">—</div></div>
    <div class="stat-card"><div class="label">Revenue (Confirmed)</div><div class="value" id="stat-revenue">—</div></div>
  </div>

  <div id="error-msg" hidden></div>
  <div id="loading">Loading orders...</div>
  <div class="table-wrap" id="table-wrap" hidden>
    <table>
      <thead>
        <tr>
          <th>#</th><th>Date</th><th>Customer</th><th>Phone</th>
          <th>Method</th><th>Items</th><th>Total</th>
          <th>Payment</th><th>Order Status</th><th>Receipt</th><th>Actions</th>
        </tr>
      </thead>
      <tbody id="orders-tbody"></tbody>
    </table>
  </div>
</div>

<!-- Modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div id="modal-body"></div>
  </div>
</div>

<script>
  const API_BASE    = window.location.origin;
  let SESSION_TOKEN = '';
  let TEMP_TOKEN    = '';
  let allOrders     = [];

  document.getElementById('pw-input').addEventListener('keydown',  e => { if (e.key === 'Enter') submitPassword(); });
  document.getElementById('otp-input').addEventListener('keydown', e => { if (e.key === 'Enter') submitOtp(); });

  async function submitPassword() {
    const pw    = document.getElementById('pw-input').value.trim();
    const btn   = document.getElementById('pw-btn');
    const errEl = document.getElementById('pw-error');
    if (!pw) { showErr(errEl, 'Please enter your password.'); return; }
    btn.disabled = true; btn.textContent = 'Sending code…'; errEl.style.display = 'none';
    try {
      const res  = await fetch(`${API_BASE}/admin/send-otp`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (!res.ok) { showErr(errEl, data.error || 'Incorrect password.'); btn.disabled = false; btn.textContent = 'Continue →'; return; }
      TEMP_TOKEN = data.temp_token;
      document.getElementById('step-1').style.display = 'none';
      document.getElementById('step-2').style.display = 'block';
      document.getElementById('dot-1').classList.replace('active','done');
      document.getElementById('dot-2').classList.add('active');
      document.getElementById('otp-hint-text').textContent = `A 6-digit code was sent to ${data.email_hint}. It expires in 10 minutes.`;
      document.getElementById('otp-input').focus();
    } catch { showErr(errEl, 'Network error. Please try again.'); btn.disabled = false; btn.textContent = 'Continue →'; }
  }

  async function submitOtp() {
    const otp   = document.getElementById('otp-input').value.trim();
    const btn   = document.getElementById('otp-btn');
    const errEl = document.getElementById('otp-error');
    if (otp.length !== 6) { showErr(errEl, 'Please enter the full 6-digit code.'); return; }
    btn.disabled = true; btn.textContent = 'Verifying…'; errEl.style.display = 'none';
    try {
      const res  = await fetch(`${API_BASE}/admin/verify-otp`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ temp_token: TEMP_TOKEN, otp }),
      });
      const data = await res.json();
      if (!res.ok) {
        showErr(errEl, data.error || 'Incorrect code.');
        btn.disabled = false; btn.textContent = 'Verify & Enter Dashboard';
        document.getElementById('otp-input').value = '';
        document.getElementById('otp-input').focus();
        return;
      }
      SESSION_TOKEN = data.session_token;
      document.getElementById('login-screen').style.display = 'none';
      document.getElementById('dashboard').style.display    = 'block';
      loadOrders();
    } catch { showErr(errEl, 'Network error.'); btn.disabled = false; btn.textContent = 'Verify & Enter Dashboard'; }
  }

  async function resendOtp() {
    const pw        = document.getElementById('pw-input').value.trim();
    const successEl = document.getElementById('otp-success-msg');
    const errEl     = document.getElementById('otp-error');
    errEl.style.display = 'none'; successEl.style.display = 'none';
    try {
      const res  = await fetch(`${API_BASE}/admin/send-otp`, {
        method: 'POST', headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ password: pw }),
      });
      const data = await res.json();
      if (res.ok) {
        TEMP_TOKEN = data.temp_token;
        successEl.textContent = `New code sent to ${data.email_hint}.`;
        successEl.style.display = 'block';
        document.getElementById('otp-input').value = '';
        document.getElementById('otp-input').focus();
      } else { showErr(errEl, data.error || 'Failed to resend.'); }
    } catch { showErr(errEl, 'Network error.'); }
  }

  function logout() {
    SESSION_TOKEN = ''; TEMP_TOKEN = ''; allOrders = [];
    document.getElementById('dashboard').style.display    = 'none';
    document.getElementById('login-screen').style.display = 'flex';
    document.getElementById('pw-input').value  = '';
    document.getElementById('otp-input').value = '';
    document.getElementById('step-1').style.display = 'block';
    document.getElementById('step-2').style.display = 'none';
    document.getElementById('dot-1').classList.add('active');
    document.getElementById('dot-1').classList.remove('done');
    document.getElementById('dot-2').classList.remove('active');
  }

  function showErr(el, msg) { el.textContent = msg; el.style.display = 'block'; }

  async function loadOrders() {
    document.getElementById('loading').hidden    = false;
    document.getElementById('table-wrap').hidden = true;
    document.getElementById('error-msg').hidden  = true;
    try {
      const res  = await fetch(`${API_BASE}/api/orders?session=${SESSION_TOKEN}`);
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || 'Failed to load orders');
      allOrders = data.orders;
      renderTable(allOrders); renderStats(allOrders);
      document.getElementById('last-updated').textContent = 'Last updated: ' + new Date().toLocaleTimeString();
    } catch (err) {
      const errEl = document.getElementById('error-msg');
      errEl.textContent = 'Error: ' + err.message; errEl.hidden = false;
    } finally { document.getElementById('loading').hidden = true; }
  }

  function renderStats(orders) {
    const pending   = orders.filter(o => o.payment_status === 'pending').length;
    const confirmed = orders.filter(o => o.payment_status === 'confirmed').length;
    const revenue   = orders.filter(o => o.payment_status === 'confirmed').reduce((s,o) => s + o.total_amount, 0);
    document.getElementById('stat-total').textContent     = orders.length;
    document.getElementById('stat-pending').textContent   = pending;
    document.getElementById('stat-confirmed').textContent = confirmed;
    document.getElementById('stat-revenue').textContent   = 'RWF ' + revenue.toLocaleString();
  }

  function renderTable(orders) {
    const pF = document.getElementById('filter-payment').value;
    const oF = document.getElementById('filter-order').value;
    const filtered = orders.filter(o => (!pF || o.payment_status===pF) && (!oF || o.order_status===oF));
    const tbody = document.getElementById('orders-tbody');
    tbody.innerHTML = '';
    if (!filtered.length) {
      tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#888;padding:2rem">No orders found.</td></tr>';
      document.getElementById('table-wrap').hidden = false; return;
    }
    filtered.forEach(order => {
      const date = new Date(order.created_at).toLocaleDateString('en-GB', {day:'2-digit',month:'short',year:'numeric',hour:'2-digit',minute:'2-digit'});
      const receipt = order.receipt_url ? `<a class="receipt-link" href="${order.receipt_url}" target="_blank">View 🖼</a>` : '<span style="color:#ccc">None</span>';
      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>#${order.id}</strong></td>
        <td style="white-space:nowrap">${date}</td>
        <td>${order.customer_name}</td><td>${order.phone}</td>
        <td>${order.delivery_method==='pickup'?'🏬 Pickup':'🚚 Delivery'}</td>
        <td>${order.item_count}</td>
        <td>RWF ${order.total_amount.toLocaleString()}</td>
        <td>
          <select class="status-select" data-id="${order.id}" data-type="payment">
            <option value="pending"   ${order.payment_status==='pending'  ?'selected':''}>Pending</option>
            <option value="confirmed" ${order.payment_status==='confirmed'?'selected':''}>Confirmed</option>
            <option value="rejected"  ${order.payment_status==='rejected' ?'selected':''}>Rejected</option>
          </select>
          <button class="save-btn" onclick="updateStatus(${order.id},'payment')">Save</button>
        </td>
        <td>
          <select class="status-select" data-id="${order.id}" data-type="order">
            <option value="new"        ${order.order_status==='new'       ?'selected':''}>New</option>
            <option value="processing" ${order.order_status==='processing'?'selected':''}>Processing</option>
            <option value="shipped"    ${order.order_status==='shipped'   ?'selected':''}>Shipped</option>
            <option value="delivered"  ${order.order_status==='delivered' ?'selected':''}>Delivered</option>
            <option value="cancelled"  ${order.order_status==='cancelled' ?'selected':''}>Cancelled</option>
          </select>
          <button class="save-btn" onclick="updateStatus(${order.id},'order')">Save</button>
        </td>
        <td>${receipt}</td>
        <td><button class="save-btn" onclick="viewOrder(${order.id})">Details</button></td>
      `;
      tbody.appendChild(tr);
    });
    document.getElementById('table-wrap').hidden = false;
  }

  async function updateStatus(orderId, type) {
    const select = document.querySelector(`.status-select[data-id="${orderId}"][data-type="${type}"]`);
    if (!select) return;
    const body = type==='payment' ? {payment_status:select.value} : {order_status:select.value};
    const res = await fetch(`${API_BASE}/api/orders/${orderId}?session=${SESSION_TOKEN}`, {
      method:'PUT', headers:{'Content-Type':'application/json'}, body:JSON.stringify(body),
    });
    if (res.ok) {
      const order = allOrders.find(o => o.id===orderId);
      if (order) { if (type==='payment') order.payment_status=select.value; else order.order_status=select.value; }
      renderStats(allOrders);
      select.style.outline = '2px solid #4CAF50';
      setTimeout(() => select.style.outline='', 1500);
    } else { alert('Update failed.'); }
  }

  async function viewOrder(orderId) {
    const res  = await fetch(`${API_BASE}/api/orders/${orderId}?session=${SESSION_TOKEN}`);
    const data = await res.json();
    if (!res.ok) { alert('Could not load order.'); return; }
    const {order, items} = data;
    const date = new Date(order.created_at).toLocaleString('en-GB');
    const itemRows = items.map(i => `
      <tr>
        <td>${i.product_name}</td><td>${i.print_name||'—'}</td><td>${i.size||'—'}</td>
        <td>${i.quantity}</td><td>RWF ${i.price.toLocaleString()}</td>
        <td>RWF ${(i.price*i.quantity).toLocaleString()}</td>
      </tr>`).join('');
    const receiptSection = order.receipt_url
      ? `<p style="margin-top:1rem"><strong>Receipt:</strong> <a class="receipt-link" href="${order.receipt_url}" target="_blank">Open ↗</a></p>
         <img class="receipt-img" src="${order.receipt_url}" alt="Receipt" onerror="this.style.display='none'" />`
      : '<p style="margin-top:1rem;color:#888">No receipt uploaded.</p>';
    document.getElementById('modal-body').innerHTML = `
      <h2>Order #${order.id}</h2>
      <div class="detail-row"><strong>Date:</strong> ${date}</div>
      <div class="detail-row"><strong>Customer:</strong> ${order.customer_name}</div>
      <div class="detail-row"><strong>Phone:</strong> ${order.phone}</div>
      <div class="detail-row"><strong>Method:</strong> ${order.delivery_method==='pickup'?'🏬 Pickup':'🚚 Delivery'}</div>
      ${order.address?`<div class="detail-row"><strong>Address:</strong> ${order.address}</div>`:''}
      ${order.delivery_note?`<div class="detail-row"><strong>Note:</strong> ${order.delivery_note}</div>`:''}
      <div class="detail-row"><strong>Total:</strong> RWF ${order.total_amount.toLocaleString()}</div>
      <div class="detail-row"><strong>Payment:</strong> ${order.payment_status}</div>
      <div class="detail-row"><strong>Status:</strong> ${order.order_status}</div>
      <table class="items-table">
        <thead><tr><th>Product</th><th>Variant</th><th>Size</th><th>Qty</th><th>Unit</th><th>Subtotal</th></tr></thead>
        <tbody>${itemRows}</tbody>
      </table>
      ${receiptSection}`;
    document.getElementById('modal-overlay').classList.add('open');
  }

  function closeModal() { document.getElementById('modal-overlay').classList.remove('open'); }

  document.getElementById('filter-payment').addEventListener('change', () => renderTable(allOrders));
  document.getElementById('filter-order').addEventListener('change',   () => renderTable(allOrders));
  document.getElementById('modal-overlay').addEventListener('click', e => {
    if (e.target===document.getElementById('modal-overlay')) closeModal();
  });
</script>
</body>
</html>
"""

@app.route('/admin')
def admin():
    """Admin dashboard with email OTP 2FA."""
    return render_template_string(ADMIN_HTML)


# ── Run ───────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)