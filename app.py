import streamlit as st
import sqlite3
from datetime import datetime, timedelta
import uuid
import stripe
import os
from fpdf import FPDF
import io
import bcrypt
from cryptography.fernet import Fernet

# --- Stripe Configuration ---
stripe.api_key = st.secrets["stripe_secret_key"]
BASE_PRICE_ID = st.secrets["base_price_id"]
CHILD_PRICE_ID = st.secrets["child_price_id"]
FERNET_KEY = st.secrets["fernet_key"]
fernet = Fernet(FERNET_KEY.encode())

# --- Database Setup ---
conn = sqlite3.connect('urgent_care_members.db', check_same_thread=False)
c = conn.cursor()
c.execute('''
    CREATE TABLE IF NOT EXISTS members (
        id TEXT PRIMARY KEY,
        name TEXT,
        email TEXT UNIQUE,
        password_hash TEXT,
        children INTEGER,
        total_payment REAL,
        certificate_id TEXT,
        certificate_expiry DATE,
        medical_answers TEXT,
        stripe_customer_id TEXT,
        stripe_subscription_id TEXT
    )
''')
conn.commit()

# --- Helper Functions ---
def calculate_payment(children):
    return 20 + (5 * children)

def generate_certificate():
    return str(uuid.uuid4())

def is_certificate_valid(expiry_date):
    return datetime.strptime(expiry_date, '%Y-%m-%d') > datetime.now()

def create_stripe_subscription(email, children):
    customer = stripe.Customer.create(email=email)
    items = [
        {"price": BASE_PRICE_ID, "quantity": 1},
        {"price": CHILD_PRICE_ID, "quantity": children}
    ]
    subscription = stripe.Subscription.create(
        customer=customer.id,
        items=items,
        payment_behavior="default_incomplete",
        expand=["latest_invoice.payment_intent"]
    )
    return customer.id, subscription.id, subscription.latest_invoice.payment_intent.client_secret

def update_stripe_children(subscription_id, new_quantity):
    subscription = stripe.Subscription.retrieve(subscription_id)
    items = subscription["items"]["data"]
    for item in items:
        if item["price"]["id"] == CHILD_PRICE_ID:
            stripe.Subscription.modify_item(
                item["id"],
                quantity=new_quantity
            )
    return True

def generate_certificate_pdf(name, cert_id, expiry):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(200, 10, "Emergency Urgent Care Certificate", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", '', 12)
    pdf.cell(200, 10, f"Name: {name}", ln=True)
    pdf.cell(200, 10, f"Certificate ID: {cert_id}", ln=True)
    pdf.cell(200, 10, f"Valid Until: {expiry}", ln=True)
    pdf.ln(10)
    pdf.multi_cell(200, 10, "This certificate entitles the holder to access emergency urgent care services at partnered locations, contingent on an active paid subscription.")
    pdf_output = io.BytesIO()
    pdf.output(pdf_output)
    return pdf_output.getvalue()

# --- Session Initialization ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.email = ""

# --- Streamlit UI ---
st.title("Emergency Urgent Care Membership")
st.write("Pay $20/month (+$5 per child) to get urgent care access at local clinics.")

menu = ["Register", "Login", "Check Certificate", "Manage Children"]
choice = st.sidebar.selectbox("Navigation", menu)

if choice == "Register":
    with st.form(key='register_form'):
        name = st.text_input("Full Name")
        email = st.text_input("Email")
        password = st.text_input("Password", type="password")
        children = st.number_input("Number of Children", min_value=0, step=1)

        st.write("### Medical Questionnaire")
        q1 = st.text_area("Do you have any chronic conditions?")
        q2 = st.text_area("List any allergies:")
        q3 = st.text_area("Current medications:")

        if st.form_submit_button("Register & Subscribe"):
            password_hash = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
            customer_id, subscription_id, client_secret = create_stripe_subscription(email, children)
            payment = calculate_payment(children)
            cert_id = generate_certificate()
            expiry = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
            med_plain = f"Chronic: {q1} | Allergies: {q2} | Meds: {q3}"
            med_answers = fernet.encrypt(med_plain.encode()).decode()
            user_id = str(uuid.uuid4())
            c.execute('''
                INSERT INTO members (id, name, email, password_hash, children, total_payment, certificate_id, certificate_expiry, medical_answers, stripe_customer_id, stripe_subscription_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, name, email, password_hash, children, payment, cert_id, expiry, med_answers, customer_id, subscription_id))
            conn.commit()
            st.success(f"Registered! Your certificate ID is: {cert_id}")
            st.info("This certificate is valid for 30 days from today.")
            st.write("Use the Stripe payment interface below to complete your subscription.")
            st.write(f"Payment Intent Client Secret: {client_secret}")

elif choice == "Login":
    email = st.text_input("Email")
    password = st.text_input("Password", type="password")
    if st.button("Login"):
        c.execute("SELECT password_hash FROM members WHERE email=?", (email,))
        row = c.fetchone()
        if row and bcrypt.checkpw(password.encode(), row[0].encode()):
            st.session_state.logged_in = True
            st.session_state.email = email
            st.success("Login successful.")
        else:
            st.error("Invalid email or password.")

elif choice == "Check Certificate" and st.session_state.logged_in:
    c.execute("SELECT name, certificate_id, certificate_expiry FROM members WHERE email=?", (st.session_state.email,))
    result = c.fetchone()
    if result:
        name, cert_id, expiry = result
        valid = is_certificate_valid(expiry)
        st.write(f"Name: {name}")
        st.write(f"Certificate ID: {cert_id}")
        st.write(f"Certificate Expiry: {expiry}")
        if valid:
            st.success("Certificate is valid.")
            pdf_bytes = generate_certificate_pdf(name, cert_id, expiry)
            st.download_button("Download Certificate PDF", data=pdf_bytes, file_name=f"certificate_{cert_id}.pdf", mime="application/pdf")
        else:
            st.error("Certificate has expired.")
    else:
        st.error("No certificate found.")

elif choice == "Manage Children" and st.session_state.logged_in:
    new_count = st.number_input("Update number of children", min_value=0, step=1)
    if st.button("Update Children Count"):
        c.execute("SELECT stripe_subscription_id FROM members WHERE email=?", (st.session_state.email,))
        result = c.fetchone()
        if result:
            subscription_id = result[0]
            try:
                update_stripe_children(subscription_id, new_count)
                c.execute("UPDATE members SET children=?, total_payment=? WHERE email=?", (new_count, calculate_payment(new_count), st.session_state.email))
                conn.commit()
                st.success("Child count updated and Stripe subscription modified successfully.")
            except Exception as e:
                st.error(f"Stripe update failed: {e}")
        else:
            st.error("Subscription not found.")
else:
    if not st.session_state.logged_in and choice in ["Check Certificate", "Manage Children"]:
        st.warning("Please log in first to access this page.")
