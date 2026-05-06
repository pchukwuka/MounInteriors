"""
app.py — MOUN Digital Platform Flask Backend
Endpoints:
  POST /api/orders          — Submit a new order (receipt upload + DB save)
  GET  /api/orders          — Admin: list all orders
  GET  /api/orders/<id>     — Admin: single order with items
  PUT  /api/orders/<id>     — Admin: update payment/order status
  GET  /admin               — Admin dashboard (password-protected HTML page)
  GET  /health              — Health check for Cloud Run
"""

import os
import json
from datetime import datetime, timezone
from functools import wraps

from flask import Flask, request, jsonify, render_template_string, abort
from flask_cors import CORS
from dotenv import load_dotenv

from db import get_connection, init_db
from storage import upload_receipt

# ── Setup ──────────────────────────────────────────────────────
load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'dev-secret-change-in-production')

# Allow requests from any origin (your frontend HTML file / hosted domain)
CORS(app, origins="*", supports_credentials=True)


# ── Admin auth decorator ───────────────────────────────────────

def require_admin(f):
    """
    Simple token-based auth for admin endpoints.
    Pass ?password=YOUR_ADMIN_PASSWORD in the URL.
    """
    @wraps(f)
    def decorated(*args, **kwargs):
        password       = request.args.get('password') or request.headers.get('X-Admin-Password')
        admin_password = os.environ.get('ADMIN_PASSWORD', '')

        if not admin_password or password != admin_password:
            return jsonify({'error': 'Unauthorised'}), 401

        return f(*args, **kwargs)
    return decorated


# ── Health check ──────────────────────────────────────────────

@app.route('/health')
def health():
    """Used by Google Cloud Run to verify the container is running."""
    return jsonify({'status': 'ok'}), 200


# ── POST /api/orders ─────────────────────────────────────────

@app.route('/api/orders', methods=['POST'])
def create_order():
    """
    Accepts multipart/form-data with:
      - order_data  (JSON string) — customer details + cart items
      - receipt     (file)        — payment screenshot/PDF

    Saves to PostgreSQL, uploads receipt to Firebase Storage.
    Returns the new order ID.
    """

    # ── 1. Parse order_data JSON ──
    raw = request.form.get('order_data')
    if not raw:
        return jsonify({'error': 'order_data is required'}), 400

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return jsonify({'error': 'order_data must be valid JSON'}), 400

    # ── 2. Validate required fields ──
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

    # ── 3. Save receipt locally (Firebase not set up yet) ──
    # When you get Firebase credentials, replace this block with:
    # receipt_url = upload_receipt(receipt_file.stream, receipt_file.filename)
    receipt_url = None
    receipt_file = request.files.get('receipt')

    if receipt_file and receipt_file.filename:
        try:
            import uuid
            from werkzeug.utils import secure_filename

            # Create receipts folder if it doesn't exist
            receipts_dir = os.path.join(os.path.dirname(__file__), 'receipts')
            os.makedirs(receipts_dir, exist_ok=True)

            # Save with a unique filename
            ext           = receipt_file.filename.rsplit('.', 1)[-1].lower() if '.' in receipt_file.filename else 'jpg'
            unique_name   = f"{uuid.uuid4().hex}.{ext}"
            save_path     = os.path.join(receipts_dir, unique_name)
            receipt_file.save(save_path)

            # Store as a local URL path so admin can view it
            receipt_url = f"/receipts/{unique_name}"
            app.logger.info(f"Receipt saved locally: {save_path}")

        except Exception as e:
            app.logger.error(f"Receipt save failed: {e}")
            # Don't block the order if receipt save fails — just log it

    # ── 4. Save to PostgreSQL ──
    conn = get_connection()
    cur  = conn.cursor()

    try:
        # Insert order row
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

        # Insert order_items rows
        for item in items:
            # Parse options string: "Print: Blue Ankara, Size: M"
            options_str   = item.get('options', '')
            print_name    = None
            size          = None

            for part in options_str.split(','):
                part = part.strip()
                if part.lower().startswith('print:'):
                    print_name = part.split(':', 1)[1].strip()
                elif part.lower().startswith('size:'):
                    size = part.split(':', 1)[1].strip()
                elif part.lower().startswith('colour:'):
                    # Store colour in print_name column for accessories
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
        'success':    True,
        'order_id':   order_id,
        'receipt_url': receipt_url,
        'message':    'Order saved successfully',
    }), 201


# ── GET /api/orders ──────────────────────────────────────────

@app.route('/api/orders', methods=['GET'])
@require_admin
def list_orders():
    """Return all orders, newest first."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        SELECT
            o.id,
            o.customer_name,
            o.phone,
            o.delivery_method,
            o.address,
            o.delivery_note,
            o.total_amount,
            o.payment_status,
            o.order_status,
            o.receipt_url,
            o.created_at,
            COUNT(oi.id) AS item_count
        FROM orders o
        LEFT JOIN order_items oi ON oi.order_id = o.id
        GROUP BY o.id
        ORDER BY o.created_at DESC
    """)

    orders = [dict(row) for row in cur.fetchall()]

    # Format dates as ISO strings for JSON
    for order in orders:
        if order.get('created_at'):
            order['created_at'] = order['created_at'].isoformat()

    cur.close()
    conn.close()

    return jsonify({'orders': orders}), 200


# ── GET /api/orders/<id> ─────────────────────────────────────

@app.route('/api/orders/<int:order_id>', methods=['GET'])
@require_admin
def get_order(order_id):
    """Return a single order with all its items."""
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("SELECT * FROM orders WHERE id = %s", (order_id,))
    order = cur.fetchone()

    if not order:
        cur.close()
        conn.close()
        return jsonify({'error': 'Order not found'}), 404

    order = dict(order)
    if order.get('created_at'):
        order['created_at'] = order['created_at'].isoformat()

    cur.execute("SELECT * FROM order_items WHERE order_id = %s", (order_id,))
    items = [dict(row) for row in cur.fetchall()]

    cur.close()
    conn.close()

    return jsonify({'order': order, 'items': items}), 200


# ── PUT /api/orders/<id> ─────────────────────────────────────

@app.route('/api/orders/<int:order_id>', methods=['PUT'])
@require_admin
def update_order(order_id):
    """
    Update payment_status or order_status.
    Body: { "payment_status": "confirmed", "order_status": "processing" }
    """
    data = request.get_json()
    if not data:
        return jsonify({'error': 'JSON body required'}), 400

    valid_payment_statuses = ('pending', 'confirmed', 'rejected')
    valid_order_statuses   = ('new', 'processing', 'shipped', 'delivered', 'cancelled')

    updates = []
    values  = []

    if 'payment_status' in data:
        if data['payment_status'] not in valid_payment_statuses:
            return jsonify({'error': f'Invalid payment_status. Must be one of: {valid_payment_statuses}'}), 400
        updates.append('payment_status = %s')
        values.append(data['payment_status'])

    if 'order_status' in data:
        if data['order_status'] not in valid_order_statuses:
            return jsonify({'error': f'Invalid order_status. Must be one of: {valid_order_statuses}'}), 400
        updates.append('order_status = %s')
        values.append(data['order_status'])

    if not updates:
        return jsonify({'error': 'Nothing to update'}), 400

    values.append(order_id)

    conn = get_connection()
    cur  = conn.cursor()

    cur.execute(
        f"UPDATE orders SET {', '.join(updates)} WHERE id = %s RETURNING id",
        values
    )
    updated = cur.fetchone()
    conn.commit()
    cur.close()
    conn.close()

    if not updated:
        return jsonify({'error': 'Order not found'}), 404

    return jsonify({'success': True, 'order_id': order_id}), 200


@app.route('/receipts/<filename>')
def serve_receipt(filename):
    """Serve locally saved receipt files for the admin dashboard."""
    from flask import send_from_directory
    receipts_dir = os.path.join(os.path.dirname(__file__), 'receipts')
    return send_from_directory(receipts_dir, filename)


# ── Serve frontend files ──────────────────────────────────────
# This serves index.html, script.js, styles.css and the images folder
# directly from Flask so the website runs on http://127.0.0.1:8080
# instead of file:/// — this fixes all CORS/fetch issues locally.

@app.route('/')
def serve_index():
    """Serve the main website."""
    from flask import send_from_directory
    frontend_dir = os.path.join(os.path.dirname(__file__), '..')
    return send_from_directory(frontend_dir, 'index.html')


@app.route('/<path:filename>')
def serve_static(filename):
    """Serve CSS, JS, images and other static files."""
    from flask import send_from_directory
    # Don't intercept API or admin routes
    if filename.startswith('api/') or filename.startswith('admin') or filename.startswith('health') or filename.startswith('receipts/'):
        abort(404)
    frontend_dir = os.path.join(os.path.dirname(__file__), '..')
    return send_from_directory(frontend_dir, filename)


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

    .header {
      background: #2D200E;
      color: #F7F3EE;
      padding: 1rem 2rem;
      display: flex;
      align-items: center;
      justify-content: space-between;
    }
    .header h1 { font-size: 1.25rem; letter-spacing: 0.05em; }
    .header .subtitle { font-size: 0.8rem; opacity: 0.6; }

    .toolbar {
      padding: 1rem 2rem;
      display: flex;
      gap: 1rem;
      align-items: center;
      background: #fff;
      border-bottom: 1px solid #e0e0e0;
      flex-wrap: wrap;
    }
    .toolbar select, .toolbar input {
      padding: 0.45rem 0.75rem;
      border: 1px solid #ccc;
      border-radius: 6px;
      font-size: 0.88rem;
    }
    .toolbar button {
      padding: 0.45rem 1rem;
      background: #C06020;
      color: #fff;
      border: none;
      border-radius: 6px;
      cursor: pointer;
      font-size: 0.88rem;
    }
    .toolbar button:hover { background: #A05018; }

    .stats {
      display: flex;
      gap: 1rem;
      padding: 1rem 2rem;
      flex-wrap: wrap;
    }
    .stat-card {
      background: #fff;
      border-radius: 8px;
      padding: 1rem 1.5rem;
      min-width: 160px;
      box-shadow: 0 1px 3px rgba(0,0,0,0.08);
    }
    .stat-card .label { font-size: 0.75rem; color: #888; text-transform: uppercase; letter-spacing: 0.06em; }
    .stat-card .value { font-size: 1.6rem; font-weight: 700; color: #2D200E; margin-top: 4px; }

    .table-wrap { padding: 0 2rem 2rem; overflow-x: auto; }
    table { width: 100%; border-collapse: collapse; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }
    th { background: #2D200E; color: #F7F3EE; padding: 0.75rem 1rem; text-align: left; font-size: 0.78rem; letter-spacing: 0.06em; text-transform: uppercase; white-space: nowrap; }
    td { padding: 0.75rem 1rem; border-bottom: 1px solid #f0f0f0; font-size: 0.88rem; vertical-align: middle; }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #fafafa; }

    .badge {
      display: inline-block;
      padding: 2px 8px;
      border-radius: 20px;
      font-size: 0.72rem;
      font-weight: 700;
      text-transform: uppercase;
    }
    .badge--pending    { background: #FFF3CD; color: #856404; }
    .badge--confirmed  { background: #D4EDDA; color: #155724; }
    .badge--rejected   { background: #F8D7DA; color: #721C24; }
    .badge--new        { background: #D1ECF1; color: #0C5460; }
    .badge--processing { background: #CCE5FF; color: #004085; }
    .badge--shipped    { background: #E2D9F3; color: #432874; }
    .badge--delivered  { background: #D4EDDA; color: #155724; }
    .badge--cancelled  { background: #F8D7DA; color: #721C24; }

    .receipt-link { color: #C06020; text-decoration: none; font-weight: 600; }
    .receipt-link:hover { text-decoration: underline; }

    .modal-overlay {
      display: none;
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.6);
      z-index: 100;
      align-items: center;
      justify-content: center;
    }
    .modal-overlay.open { display: flex; }
    .modal {
      background: #fff;
      border-radius: 10px;
      width: 90%;
      max-width: 640px;
      max-height: 90vh;
      overflow-y: auto;
      padding: 1.5rem;
      position: relative;
    }
    .modal h2 { font-size: 1.1rem; margin-bottom: 1rem; color: #2D200E; }
    .modal-close {
      position: absolute;
      top: 1rem;
      right: 1rem;
      background: none;
      border: none;
      font-size: 1.25rem;
      cursor: pointer;
      color: #888;
    }
    .modal-close:hover { color: #222; }
    .detail-row { display: flex; gap: 0.5rem; margin-bottom: 0.5rem; font-size: 0.88rem; }
    .detail-row strong { min-width: 140px; color: #888; }
    .items-table { width: 100%; border-collapse: collapse; margin-top: 1rem; font-size: 0.85rem; }
    .items-table th { background: #f5f5f5; padding: 0.5rem; text-align: left; font-size: 0.75rem; color: #888; }
    .items-table td { padding: 0.5rem; border-top: 1px solid #f0f0f0; }
    .receipt-img { max-width: 100%; border-radius: 8px; margin-top: 1rem; border: 1px solid #eee; }

    .status-select {
      padding: 0.3rem 0.5rem;
      border-radius: 4px;
      border: 1px solid #ccc;
      font-size: 0.8rem;
      cursor: pointer;
    }
    .save-btn {
      padding: 0.3rem 0.65rem;
      background: #C06020;
      color: #fff;
      border: none;
      border-radius: 4px;
      font-size: 0.78rem;
      cursor: pointer;
      margin-left: 4px;
    }
    .save-btn:hover { background: #A05018; }

    #loading { text-align: center; padding: 3rem; color: #888; }
    #error-msg { color: #c0392b; padding: 1rem 2rem; }

    @media (max-width: 600px) {
      .toolbar, .stats { padding: 1rem; }
      .table-wrap { padding: 0 0 2rem; }
      th, td { padding: 0.5rem 0.6rem; }
    }
  </style>
</head>
<body>

<div class="header">
  <div>
    <h1>MOUN — Orders</h1>
    <div class="subtitle">Admin Dashboard</div>
  </div>
  <div class="subtitle" id="last-updated"></div>
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
        <th>#</th>
        <th>Date</th>
        <th>Customer</th>
        <th>Phone</th>
        <th>Method</th>
        <th>Items</th>
        <th>Total</th>
        <th>Payment</th>
        <th>Order Status</th>
        <th>Receipt</th>
        <th>Actions</th>
      </tr>
    </thead>
    <tbody id="orders-tbody"></tbody>
  </table>
</div>

<!-- Order detail modal -->
<div class="modal-overlay" id="modal-overlay">
  <div class="modal" id="modal-content">
    <button class="modal-close" onclick="closeModal()">✕</button>
    <div id="modal-body"></div>
  </div>
</div>

<script>
  // Get password from URL: /admin?password=YOUR_PASSWORD
  const params   = new URLSearchParams(window.location.search);
  const PASSWORD = params.get('password') || '';
  const API_BASE = window.location.origin;

  let allOrders = [];

  async function loadOrders() {
    document.getElementById('loading').hidden    = false;
    document.getElementById('table-wrap').hidden = true;
    document.getElementById('error-msg').hidden  = true;

    try {
      const res  = await fetch(`${API_BASE}/api/orders?password=${PASSWORD}`);
      const data = await res.json();

      if (!res.ok) {
        throw new Error(data.error || 'Failed to load orders');
      }

      allOrders = data.orders;
      renderTable(allOrders);
      renderStats(allOrders);

      document.getElementById('last-updated').textContent =
        'Last updated: ' + new Date().toLocaleTimeString();

    } catch (err) {
      const errEl = document.getElementById('error-msg');
      errEl.textContent = 'Error: ' + err.message + ' — Check your password in the URL (?password=...)';
      errEl.hidden = false;
    } finally {
      document.getElementById('loading').hidden = true;
    }
  }

  function renderStats(orders) {
    const pending   = orders.filter(o => o.payment_status === 'pending').length;
    const confirmed = orders.filter(o => o.payment_status === 'confirmed').length;
    const revenue   = orders
      .filter(o => o.payment_status === 'confirmed')
      .reduce((sum, o) => sum + o.total_amount, 0);

    document.getElementById('stat-total').textContent     = orders.length;
    document.getElementById('stat-pending').textContent   = pending;
    document.getElementById('stat-confirmed').textContent = confirmed;
    document.getElementById('stat-revenue').textContent   = 'RWF ' + revenue.toLocaleString();
  }

  function renderTable(orders) {
    const pFilter = document.getElementById('filter-payment').value;
    const oFilter = document.getElementById('filter-order').value;

    const filtered = orders.filter(o =>
      (!pFilter || o.payment_status === pFilter) &&
      (!oFilter || o.order_status   === oFilter)
    );

    const tbody = document.getElementById('orders-tbody');
    tbody.innerHTML = '';

    if (filtered.length === 0) {
      tbody.innerHTML = '<tr><td colspan="11" style="text-align:center;color:#888;padding:2rem">No orders found.</td></tr>';
      document.getElementById('table-wrap').hidden = false;
      return;
    }

    filtered.forEach(order => {
      const date = new Date(order.created_at).toLocaleDateString('en-GB', {
        day: '2-digit', month: 'short', year: 'numeric', hour: '2-digit', minute: '2-digit'
      });

      const receiptCell = order.receipt_url
        ? `<a class="receipt-link" href="${order.receipt_url}" target="_blank">View 🖼</a>`
        : '<span style="color:#ccc">None</span>';

      const tr = document.createElement('tr');
      tr.innerHTML = `
        <td><strong>#${order.id}</strong></td>
        <td style="white-space:nowrap">${date}</td>
        <td>${order.customer_name}</td>
        <td>${order.phone}</td>
        <td>${order.delivery_method === 'pickup' ? '🏫 Pickup' : '🚚 Delivery'}</td>
        <td>${order.item_count}</td>
        <td>RWF ${order.total_amount.toLocaleString()}</td>
        <td>
          <select class="status-select" data-id="${order.id}" data-type="payment">
            <option value="pending"   ${order.payment_status==='pending'   ?'selected':''}>Pending</option>
            <option value="confirmed" ${order.payment_status==='confirmed' ?'selected':''}>Confirmed</option>
            <option value="rejected"  ${order.payment_status==='rejected'  ?'selected':''}>Rejected</option>
          </select>
          <button class="save-btn" onclick="updateStatus(${order.id}, 'payment')">Save</button>
        </td>
        <td>
          <select class="status-select" data-id="${order.id}" data-type="order">
            <option value="new"        ${order.order_status==='new'        ?'selected':''}>New</option>
            <option value="processing" ${order.order_status==='processing' ?'selected':''}>Processing</option>
            <option value="shipped"    ${order.order_status==='shipped'    ?'selected':''}>Shipped</option>
            <option value="delivered"  ${order.order_status==='delivered'  ?'selected':''}>Delivered</option>
            <option value="cancelled"  ${order.order_status==='cancelled'  ?'selected':''}>Cancelled</option>
          </select>
          <button class="save-btn" onclick="updateStatus(${order.id}, 'order')">Save</button>
        </td>
        <td>${receiptCell}</td>
        <td><button class="save-btn" onclick="viewOrder(${order.id})">Details</button></td>
      `;
      tbody.appendChild(tr);
    });

    document.getElementById('table-wrap').hidden = false;
  }

  async function updateStatus(orderId, type) {
    const select = document.querySelector(
      `.status-select[data-id="${orderId}"][data-type="${type}"]`
    );
    if (!select) return;

    const body = type === 'payment'
      ? { payment_status: select.value }
      : { order_status:   select.value };

    const res = await fetch(`${API_BASE}/api/orders/${orderId}?password=${PASSWORD}`, {
      method:  'PUT',
      headers: { 'Content-Type': 'application/json' },
      body:    JSON.stringify(body),
    });

    if (res.ok) {
      const order = allOrders.find(o => o.id === orderId);
      if (order) {
        if (type === 'payment') order.payment_status = select.value;
        else                    order.order_status   = select.value;
      }
      renderStats(allOrders);
      select.style.outline = '2px solid #2ecc71';
      setTimeout(() => select.style.outline = '', 1500);
    } else {
      alert('Update failed. Please try again.');
    }
  }

  async function viewOrder(orderId) {
    const res  = await fetch(`${API_BASE}/api/orders/${orderId}?password=${PASSWORD}`);
    const data = await res.json();
    if (!res.ok) { alert('Could not load order details.'); return; }

    const { order, items } = data;
    const date = new Date(order.created_at).toLocaleString('en-GB');

    const itemRows = items.map(item => `
      <tr>
        <td>${item.product_name}</td>
        <td>${item.print_name || '—'}</td>
        <td>${item.size || '—'}</td>
        <td>${item.quantity}</td>
        <td>RWF ${item.price.toLocaleString()}</td>
        <td>RWF ${(item.price * item.quantity).toLocaleString()}</td>
      </tr>
    `).join('');

    const receiptSection = order.receipt_url
      ? `<p style="margin-top:1rem"><strong>Receipt:</strong>
           <a class="receipt-link" href="${order.receipt_url}" target="_blank">Open in new tab ↗</a>
         </p>
         <img class="receipt-img" src="${order.receipt_url}" alt="Payment receipt"
              onerror="this.style.display='none'" />`
      : '<p style="margin-top:1rem;color:#888">No receipt uploaded.</p>';

    document.getElementById('modal-body').innerHTML = `
      <h2>Order #${order.id}</h2>
      <div class="detail-row"><strong>Date:</strong> ${date}</div>
      <div class="detail-row"><strong>Customer:</strong> ${order.customer_name}</div>
      <div class="detail-row"><strong>Phone:</strong> ${order.phone}</div>
      <div class="detail-row"><strong>Method:</strong> ${order.delivery_method === 'pickup' ? '🏫 Pickup' : '🚚 Delivery'}</div>
      ${order.address ? `<div class="detail-row"><strong>Address:</strong> ${order.address}</div>` : ''}
      ${order.delivery_note ? `<div class="detail-row"><strong>Delivery Note:</strong> ${order.delivery_note}</div>` : ''}
      <div class="detail-row"><strong>Total:</strong> RWF ${order.total_amount.toLocaleString()}</div>
      <div class="detail-row"><strong>Payment:</strong> ${order.payment_status}</div>
      <div class="detail-row"><strong>Order Status:</strong> ${order.order_status}</div>

      <table class="items-table">
        <thead>
          <tr>
            <th>Product</th><th>Print/Colour</th><th>Size</th><th>Qty</th><th>Unit Price</th><th>Subtotal</th>
          </tr>
        </thead>
        <tbody>${itemRows}</tbody>
      </table>

      ${receiptSection}
    `;

    document.getElementById('modal-overlay').classList.add('open');
  }

  function closeModal() {
    document.getElementById('modal-overlay').classList.remove('open');
  }

  // Filter change listeners
  document.getElementById('filter-payment').addEventListener('change', () => renderTable(allOrders));
  document.getElementById('filter-order').addEventListener('change',   () => renderTable(allOrders));

  // Close modal on overlay click
  document.getElementById('modal-overlay').addEventListener('click', (e) => {
    if (e.target === document.getElementById('modal-overlay')) closeModal();
  });

  // Load on page open
  loadOrders();
</script>
</body>
</html>
"""

@app.route('/admin')
def admin():
    """
    Admin dashboard. Access via:
    https://your-cloud-run-url/admin?password=YOUR_ADMIN_PASSWORD
    """
    password       = request.args.get('password', '')
    admin_password = os.environ.get('ADMIN_PASSWORD', '')

    if not admin_password or password != admin_password:
        return """
            <html><body style="font-family:sans-serif;padding:2rem">
              <h2>MOUN Admin</h2>
              <p style="color:#888">Add <code>?password=YOUR_PASSWORD</code> to the URL to access the dashboard.</p>
            </body></html>
        """, 401

    return render_template_string(ADMIN_HTML)


# ── Run ───────────────────────────────────────────────────────

if __name__ == '__main__':
    init_db()
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)