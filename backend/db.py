"""
db.py — PostgreSQL connection using Neon free tier.
Handles connection pooling and table creation on startup.
"""

import os
import psycopg2
from psycopg2.extras import RealDictCursor


def get_connection():
    """Return a new database connection using the DATABASE_URL env variable."""
    return psycopg2.connect(
        os.environ['DATABASE_URL'],
        cursor_factory=RealDictCursor
    )


def init_db():
    """
    Create the orders and order_items tables if they don't exist.
    Called once when the Flask app starts.
    """
    conn = get_connection()
    cur  = conn.cursor()

    cur.execute("""
        CREATE TABLE IF NOT EXISTS orders (
            id               SERIAL PRIMARY KEY,
            customer_name    VARCHAR(255) NOT NULL,
            phone            VARCHAR(50)  NOT NULL,
            delivery_method  VARCHAR(20)  NOT NULL CHECK (delivery_method IN ('pickup', 'delivery')),
            address          TEXT,
            delivery_note    TEXT,
            total_amount     INTEGER      NOT NULL,   -- stored in RWF (no decimals)
            payment_status   VARCHAR(20)  NOT NULL DEFAULT 'pending',
            order_status     VARCHAR(20)  NOT NULL DEFAULT 'new',
            receipt_url      TEXT,                   -- Firebase Storage download URL
            created_at       TIMESTAMPTZ  NOT NULL DEFAULT NOW()
        );
    """)

    cur.execute("""
        CREATE TABLE IF NOT EXISTS order_items (
            id           SERIAL PRIMARY KEY,
            order_id     INTEGER      NOT NULL REFERENCES orders(id) ON DELETE CASCADE,
            product_name VARCHAR(255) NOT NULL,
            print_name   VARCHAR(255),
            size         VARCHAR(50),
            quantity     INTEGER      NOT NULL,
            price        INTEGER      NOT NULL    -- unit price in RWF
        );
    """)

    conn.commit()
    cur.close()
    conn.close()
    print("Database tables ready.")