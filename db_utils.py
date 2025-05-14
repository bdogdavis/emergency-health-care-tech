# db_utils.py
import sqlite3
from datetime import datetime, timezone
import uuid

DATABASE_NAME = 'urgent_care_members.db'

def get_db_connection():
    conn = sqlite3.connect(DATABASE_NAME, check_same_thread=False)
    conn.row_factory = sqlite3.Row # Allows accessing columns by name
    return conn

def create_tables():
    with get_db_connection() as conn:
        c = conn.cursor()
        c.execute('''
            CREATE TABLE IF NOT EXISTS members (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                email TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                children INTEGER DEFAULT 0,
                certificate_id TEXT,
                certificate_status TEXT DEFAULT 'pending_payment', -- pending_payment, active, expired, revoked
                certificate_expiry_date TEXT, -- ISO format YYYY-MM-DD HH:MM:SS
                medical_answers_encrypted TEXT,
                stripe_customer_id TEXT,
                stripe_subscription_id TEXT,
                subscription_status TEXT DEFAULT 'incomplete', -- incomplete, active, past_due, canceled, trialing
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        ''')
        conn.commit()

def add_member(user_id, name, email, password_hash, children, cert_id, initial_expiry_iso, medical_answers_encrypted, stripe_customer_id, stripe_subscription_id):
    with get_db_connection() as conn:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute('''
            INSERT INTO members (id, name, email, password_hash, children, certificate_id, certificate_expiry_date,
                                 medical_answers_encrypted, stripe_customer_id, stripe_subscription_id, created_at, updated_at,
                                 subscription_status, certificate_status)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        ''', (user_id, name, email, password_hash, children, cert_id, initial_expiry_iso,
              medical_answers_encrypted, stripe_customer_id, stripe_subscription_id, now_iso, now_iso,
              'incomplete', 'pending_payment'))
        conn.commit()

def get_member_by_email(email):
    with get_db_connection() as conn:
        member = conn.execute("SELECT * FROM members WHERE email =?", (email,)).fetchone()
    return member

def get_member_by_stripe_customer_id(customer_id):
    with get_db_connection() as conn:
        member = conn.execute("SELECT * FROM members WHERE stripe_customer_id =?", (customer_id,)).fetchone()
    return member

def get_member_by_stripe_subscription_id(subscription_id):
     with get_db_connection() as conn:
        member = conn.execute("SELECT * FROM members WHERE stripe_subscription_id =?", (subscription_id,)).fetchone()
     return member

def update_member_subscription_details(email, stripe_customer_id, stripe_subscription_id, subscription_status='incomplete'):
    with get_db_connection() as conn:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute('''
            UPDATE members
            SET stripe_customer_id =?, stripe_subscription_id =?, subscription_status =?, updated_at =?
            WHERE email =?
        ''', (stripe_customer_id, stripe_subscription_id, subscription_status, now_iso, email))
        conn.commit()

def update_subscription_status(stripe_subscription_id, sub_status, cert_status, cert_expiry_iso=None):
    with get_db_connection() as conn:
        now_iso = datetime.now(timezone.utc).isoformat()
        if cert_expiry_iso:
            conn.execute('''
                UPDATE members
                SET subscription_status =?, certificate_status =?, certificate_expiry_date =?, updated_at =?
                WHERE stripe_subscription_id =?
            ''', (sub_status, cert_status, cert_expiry_iso, now_iso, stripe_subscription_id))
        else: # Don't update expiry if not provided (e.g. for failure)
            conn.execute('''
                UPDATE members
                SET subscription_status =?, certificate_status =?, updated_at =?
                WHERE stripe_subscription_id =?
            ''', (sub_status, cert_status, now_iso, stripe_subscription_id))
        conn.commit()

def update_children_count(email, new_children_count):
    with get_db_connection() as conn:
        now_iso = datetime.now(timezone.utc).isoformat()
        conn.execute('''
            UPDATE members
            SET children =?, updated_at =?
            WHERE email =?
        ''', (new_children_count, now_iso, email))
        conn.commit()

# Initialize database and tables when this module is imported
create_tables()
