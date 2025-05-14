import streamlit as st
import sqlite3
from datetime import datetime, timedelta
import uuid
import stripe
import os
from fpdf import FPDF
import io

# --- Stripe Configuration ---
stripe.api_key = st.secrets["stripe_secret_key"]
BASE_PRICE_ID = st.secrets["base_price_id"]
CHILD_PRICE_ID = st.secrets["child_price_id"]

# --- Database Setup ---
conn = sqlite3.connect('urgent_care_members.db', check_same_thread=False)
c = conn.cursor()
c.execute('''
    CREATE TABLE IF NOT EXISTS members (
        id TEXT PRIMARY KEY,
        name TEXT,
        email TEXT,
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

# --- Streamlit UI ---
st.title("Emergency Urgent Care Membership")
st.write("Pay $20/month (+$5 per child) to get urgent care access at local clinics.")

menu = ["Register", "Check Certificate", "Manage Children"]
choice = st.sidebar.selectbox("Navigation", menu)

if choice == "Register":
    with st.form(key='register_form'):
        name = st.text_input("Full Name")
        email = st.text_input("Email")
        children = st.number_input("Number of Children", min_value=0, step=1)

        st.write("### Medical Questionnaire")
        q1 = st.text_area("Do you have any chronic conditions?")
        q2 = st.text_area("List any allergies:")
        q3 = st.text_area("Current medications:")

        if st.form_submit_button("Register & Subscribe"):
            customer_id, subscription_id, client_secret = create_stripe_subscription(email, children)

            payment = calculate_payment(children)
            cert_id = generate_certificate()
            expiry = (datetime.now() + timedelta(days=30)).strftime('%Y-%m-%d')
            med_answers = f"Chronic: {q1} | Allergies: {q2} | Meds: {q3}"

            user_id = str(uuid.uuid4())
            c.execute('''
                INSERT INTO members (id, name, email, children, total_payment, certificate_id, certificate_expiry, medical_answers, stripe_customer_id, stripe_subscription_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (user_id, name, email, children, payment, cert_id, expiry, med_answers, customer_id, subscription_id))
            conn.commit()

            st.success(f"Registered! Your certificate ID is: {cert_id}")
            st.info("This certificate is valid for 30 days from today.")
            st.write("Use the Stripe payment interface below to complete your subscription.")
            st.write(f"Payment Intent Client Secret: {client_secret}")

elif choice == "Check Certificate":
    cert_input = st.text_input("Enter your certificate ID")
    if st.button("Check Status"):
        c.execute("SELECT name, certificate_expiry FROM members WHERE certificate_id=?", (cert_input,))
        result = c.fetchone()
        if result:
            name, expiry = result
            valid = is_certificate_valid(expiry)
            st.write(f"Name: {name}")
            st.write(f"Certificate Expiry: {expiry}")
            if valid:
                st.success("Certificate is valid.")
                pdf_bytes = generate_certificate_pdf(name, cert_input, expiry)
                st.download_button("Download Certificate PDF", data=pdf_bytes, file_name=f"certificate_{cert_input}.pdf", mime="application/pdf")
            else:
                st.error("Certificate has expired.")
        else:
            st.error("Certificate ID not found.")

elif choice == "Manage Children":
    email = st.text_input("Enter your registered email")
    new_count = st.number_input("Update number of children", min_value=0, step=1)
    if st.button("Update Children Count"):
        c.execute("SELECT stripe_subscription_id FROM members WHERE email=?", (email,))
        result = c.fetchone()
        if result:
            subscription_id = result[0]
            try:
                update_stripe_children(subscription_id, new_count)
                c.execute("UPDATE members SET children=?, total_payment=? WHERE email=?", (new_count, calculate_payment(new_count), email))
                conn.commit()
                st.success("Child count updated and Stripe subscription modified successfully.")
            except Exception as e:
                st.error(f"Stripe update failed: {e}")
        else:
            st.error("Email not found.")

