<<<<<<< HEAD
# Moun Interiors
=======
# MOUN — Interior Design & Home Décor

A full-stack e-commerce and client management platform for MOUN, a luxury interior design and home décor brand based in Kigali, Rwanda.

🌐 **Live site:** [mouninteriors.vercel.app](https://mouninteriors.vercel.app)

---

## Features

- **Product Catalogue** — Browse curated home décor collections with filtering by category
- **Shopping Cart** — Add products, manage quantities, and checkout seamlessly
- **Order System** — Customers submit orders with payment receipts; admin tracks every order
- **Quotation Requests** — Clients submit project details (service, budget, timeline) directly from the website and the team receives them instantly via email
- **Admin Dashboard** — Password + OTP protected dashboard to manage orders and quotation requests by status
- **WhatsApp Integration** — Quick contact and order sharing via WhatsApp

---

## Tech Stack

**Frontend**
- HTML, CSS, Vanilla JavaScript
- Hosted on Vercel

**Backend**
- Python, Flask
- PostgreSQL (via Neon)
- Firebase Storage (receipt uploads)
- Hosted on Google Cloud Run

**Email**
- Gmail SMTP (OTP + quotation notifications)

---

## Project Structure

```
MounInteriors/
├── index.html        # Main frontend
├── script.js         # Frontend logic
├── styles.css        # Styling
├── backend/
│   ├── app.py        # Flask API
│   ├── db.py         # Database connection
│   └── storage.py    # Firebase receipt upload
└── README.md
```

---

## Local Development

**Frontend**
```bash
# Just open index.html in your browser or use Live Server in VS Code
```

**Backend**
```bash
cd backend
pip install -r requirements.txt
python app.py
```

Create a `.env` file in the `backend/` folder:
```
ADMIN_PASSWORD=your_password
ADMIN_EMAIL=your_email@gmail.com
SMTP_EMAIL=your_email@gmail.com
SMTP_PASSWORD=your_app_password
DATABASE_URL=your_postgres_url
FIREBASE_CREDENTIALS=your_firebase_json
```

---

© 2026 MOUN — Kigali, Rwanda
```
>>>>>>> 20b1deb9435d26199b24344d112d618a84909c15
