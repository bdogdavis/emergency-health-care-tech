# app.py
import streamlit as st
import bcrypt
from datetime import datetime, timedelta, timezone
import uuid
import stripe
from fpdf import FPDF
import io
from cryptography.fernet import Fernet
import db_utils  # Our database utility module

# --- Configuration ---
# --- Configuration ---
try:
    stripe.api_key = st.secrets["stripe_secret_key"]
    BASE_PRICE_ID = st.secrets["base_price_id"]
    CHILD_PRICE_ID = st.secrets["child_price_id"]
    FERNET_KEY = st.secrets["fernet_key"].encode()  # Ensure it's bytes
    APP_BASE_URL = st.secrets["app_base_url"]  # Fixed assignment
    fernet = Fernet(FERNET_KEY)
except KeyError as e:
    st.error(f"Configuration error: Missing secret key {e}. Please check your secrets.toml.")
    st.stop()
except Exception as e:
    st.error(f"An error occurred during configuration: {e}")
    st.stop()

# --- Helper Functions ---
def calculate_total_monthly_cost(children):
    return 20 + (5 * children)

def generate_unique_id():
    return str(uuid.uuid4())

def is_certificate_valid_from_db(expiry_date_str, status):
    if status != 'active':
        return False
    if not expiry_date_str:
        return False
    try:
        # Ensure expiry_date_str is parsed correctly, assuming ISO format from DB
        expiry_date = datetime.fromisoformat(expiry_date_str.replace('Z', '+00:00'))
        if expiry_date.tzinfo is None:  # If no timezone, assume UTC
            expiry_date = expiry_date.replace(tzinfo=timezone.utc)
        return expiry_date > datetime.now(timezone.utc)
    except ValueError:
        st.error(f"Error parsing certificate expiry date: {expiry_date_str}")
        return False

def create_stripe_checkout_session(email, children_count, user_id_for_metadata):
    try:
        line_items = [{"price": BASE_PRICE_ID, "quantity": 1}]  # Always include base price
        if children_count > 0:
            line_items.append({"price": CHILD_PRICE_ID, "quantity": children_count})

        checkout_session = stripe.checkout.Session.create(
            customer_email=email,  # Stripe can create or use existing customer by email
            payment_method_types=['card'],
            line_items=line_items,
            mode='subscription',
            success_url=f"{APP_BASE_URL}?checkout_status=success&session_id={{CHECKOUT_SESSION_ID}}&user_id={user_id_for_metadata}",
            cancel_url=f"{APP_BASE_URL}?checkout_status=cancel&user_id={user_id_for_metadata}",
            metadata={'app_user_id': user_id_for_metadata}
        )
        return checkout_session.id, checkout_session.url
    except stripe.error.StripeError as e:
        st.error(f"Stripe Checkout session creation failed: {e}")
        return None, None

def update_stripe_subscription_children(subscription_id, new_quantity):
    try:
        sub = stripe.Subscription.retrieve(subscription_id, expand=['items'])
        child_item_id = None
        base_item_id = None

        for item in sub['items']['data']:
            if item['price']['id'] == CHILD_PRICE_ID:
                child_item_id = item['id']
            elif item['price']['id'] == BASE_PRICE_ID:
                base_item_id = item['id']

        items_to_update = []
        if base_item_id:
            items_to_update.append({'id': base_item_id, 'quantity': 1})  # Keep base
        else:  # Should not happen if subscription was set up correctly
            st.error("Base subscription item not found. Cannot update children.")
            return False

        if child_item_id:  # Child item exists
            if new_quantity > 0:
                items_to_update.append({'id': child_item_id, 'quantity': new_quantity})
            else:  # new_quantity is 0, remove it
                items_to_update.append({'id': child_item_id, 'deleted': True})
        elif new_quantity > 0:  # No child item exists, and new_quantity > 0, so add it
            items_to_update.append({'price': CHILD_PRICE_ID, 'quantity': new_quantity})
        # If child_item_id is None and new_quantity is 0, do nothing for child item.

        if not items_to_update:  # Should at least have the base item
            st.error("No items to update for the subscription.")
            return False

        stripe.Subscription.modify(
            subscription_id,
            items=items_to_update,
            proration_behavior='create_prorations',  # Or 'none' if you don't want prorations
            payment_behavior='default_incomplete'  # Handles SCA if needed
        )
        return True
    except stripe.error.StripeError as e:
        st.error(f"Stripe subscription update failed: {e}")
        return False

def generate_certificate_pdf_bytes(name, cert_id, expiry_str, status):
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Arial", 'B', 16)
    pdf.cell(0, 10, "Emergency Urgent Care Certificate", ln=True, align='C')
    pdf.ln(10)
    pdf.set_font("Arial", '', 12)
    pdf.cell(0, 10, f"Name: {name}", ln=True)
    pdf.cell(0, 10, f"Certificate ID: {cert_id}", ln=True)
    pdf.cell(0, 10, f"Valid Until: {expiry_str or 'N/A'}", ln=True)  # Handle None or empty
    pdf.cell(0, 10, f"Status: {status.title()}", ln=True)
    pdf.ln(10)
    pdf.multi_cell(0, 10, "This certificate entitles the holder to access emergency urgent care services at partnered locations, contingent on an active paid subscription and valid certificate status.")

    # Save PDF to a BytesIO object
    buffer = io.BytesIO()
    pdf.output(buffer)
    buffer.seek(0)
    return buffer.getvalue()

# --- Session State Initialization ---
if "logged_in" not in st.session_state:
    st.session_state.logged_in = False
    st.session_state.email = ""
    st.session_state.name = ""
    st.session_state.user_id = ""

# --- UI ---
st.set_page_config(layout="centered")
st.title("Emergency Urgent Care Membership")
st.markdown("Pay **$20/month** (plus **$5 per child per month**) for urgent care access at local clinics.")
st.markdown("---")

# Handle Checkout Redirects
query_params = st.query_params
if "checkout_status" in query_params:
    status = query_params.get("checkout_status")
    user_id_from_query = query_params.get("user_id")

    if status == "success":
        session_id = query_params.get("session_id")
        st.success("Subscription payment setup initiated! Your account will be activated shortly once payment is confirmed.")
        st.info("Please check your email for confirmation from Stripe. Your certificate status will update upon successful payment processing via webhooks.")
        # The webhook for checkout.session.completed will handle actual DB updates.
    elif status == "cancel":
        st.warning("Subscription process was cancelled. You can try registering again if you wish.")
    # Clear query params to avoid re-showing message on refresh
    st.query_params.clear()

# Navigation
if st.session_state.logged_in:
    menu_options = ["My Dashboard", "Manage Children", "Medical Questionnaire", "View Certificate", "Logout"]
else:
    menu_options = ["Register", "Login"]

choice = st.sidebar.selectbox("Navigation", menu_options)

if choice == "Register" and not st.session_state.logged_in:
    st.subheader("New Member Registration")
    with st.form(key='register_form'):
        reg_name = st.text_input("Full Name")
        reg_email = st.text_input("Email")
        reg_password = st.text_input("Password", type="password")
        reg_children = st.number_input("Number of Children", min_value=0, step=1)
        st.markdown(f"**Estimated Monthly Cost:** ${calculate_total_monthly_cost(reg_children)}")

        st.markdown("### Medical Questionnaire (Optional for now)")
        q1 = st.text_area("Do you have any chronic conditions? (e.g., Asthma, Diabetes)")
        q2 = st.text_area("List any known allergies (medications, food, environmental):")
        q3 = st.text_area("List any current medications you are taking regularly:")

        if st.form_submit_button("Register & Proceed to Payment Setup"):
            if not (reg_name and reg_email and reg_password):
                st.error("Please fill in all required fields: Name, Email, and Password.")
            elif db_utils.get_member_by_email(reg_email):
                st.error("This email is already registered. Please login or use a different email.")
            else:
                hashed_password = bcrypt.hashpw(reg_password.encode('utf-8'), bcrypt.gensalt()).decode('utf-8')
                user_id = generate_unique_id()
                certificate_id = generate_unique_id()  # For the certificate itself

                # Store medical answers encrypted
                medical_data_plain = f"Chronic Conditions: {q1}\nAllergies: {q2}\nCurrent Medications: {q3}"
                encrypted_medical_data = fernet.encrypt(medical_data_plain.encode('utf-8')).decode('utf-8')

                # Initial expiry is just a placeholder, actual expiry set by webhook
                initial_expiry = (datetime.now(timezone.utc) + timedelta(days=30)).isoformat()

                # Create Stripe Checkout Session
                checkout_session_id, checkout_url = create_stripe_checkout_session(reg_email, reg_children, user_id)

                if checkout_url:
                    # Add member to DB with 'incomplete' subscription status initially
                    # Stripe customer ID and subscription ID will be updated by webhook after checkout.session.completed
                    db_utils.add_member(user_id, reg_name, reg_email, hashed_password, reg_children,
                                       certificate_id, initial_expiry, encrypted_medical_data,
                                       None, None)  # Stripe IDs are None for now

                    st.info(f"Registration successful! User ID: {user_id}. You will now be redirected to Stripe to complete your subscription payment.")
                    st.markdown(f"""
                        <p>If you are not redirected automatically, <a href="{checkout_url}" target="_blank">click here to complete payment</a>.</p>
                        <meta http-equiv="refresh" content="5; url={checkout_url}">
                    """, unsafe_allow_html=True)
                    st.session_state.processing_registration = True  # Prevent re-submission
                else:
                    st.error("Could not initiate payment process. Please try again.")

elif choice == "Login" and not st.session_state.logged_in:
    st.subheader("Member Login")
    with st.form(key='login_form'):
        login_email = st.text_input("Email")
        login_password = st.text_input("Password", type="password")
        if st.form_submit_button("Login"):
            member = db_utils.get_member_by_email(login_email)
            if member and bcrypt.checkpw(login_password.encode('utf-8'), member["password_hash"].encode('utf-8')):
                st.session_state.logged_in = True
                st.session_state.email = member["email"]
                st.session_state.name = member["name"]
                st.session_state.user_id = member["id"]
                st.success(f"Welcome back, {st.session_state.name}!")
                st.rerun()  # Rerun to update sidebar and view
            else:
                st.error("Invalid email or password.")

elif st.session_state.logged_in:  # Pages for logged-in users
    member_data = db_utils.get_member_by_email(st.session_state.email)
    if not member_data:
        st.error("Could not retrieve your member data. Please try logging out and in again.")
        st.session_state.logged_in = False  # Force logout
        st.rerun()

    if choice == "My Dashboard":
        st.subheader(f"Dashboard for {st.session_state.name}")
        if member_data:
            st.write(f"**Email:** {member_data['email']}")
            st.write(f"**Number of Children Registered:** {member_data['children']}")
            st.write(f"**Subscription Status:** {member_data['subscription_status'].title() if member_data['subscription_status'] else 'N/A'}")
            st.write(f"**Certificate ID:** {member_data['certificate_id']}")
            st.write(f"**Certificate Status:** {member_data['certificate_status'].title() if member_data['certificate_status'] else 'N/A'}")
            expiry_display = "N/A"
            if member_data['certificate_expiry_date']:
                try:
                    expiry_dt = datetime.fromisoformat(member_data['certificate_expiry_date'].replace('Z', '+00:00'))
                    expiry_display = expiry_dt.strftime('%Y-%m-%d %H:%M:%S %Z')
                except:
                    expiry_display = member_data['certificate_expiry_date']  # show raw if parse fails
            st.write(f"**Certificate Expires On:** {expiry_display}")

            if member_data['subscription_status'] == 'incomplete' and member_data['stripe_customer_id'] is None:
                st.warning("Your subscription setup is incomplete. If you started registration but didn't complete payment, please try registering again or contact support.")
            elif member_data['subscription_status'] == 'incomplete' and member_data['stripe_customer_id'] is not None:
                st.info("Your payment might be processing. Please check back shortly. If issues persist, contact support.")

    elif choice == "Manage Children":
        st.subheader("Manage Your Children")
        if member_data and member_data['stripe_subscription_id']:
            current_children = member_data['children']
            st.write(f"You currently have **{current_children}** children registered on your plan.")
            new_count = st.number_input("Update number of children for your subscription:", min_value=0, value=current_children, step=1)

            if st.button("Update Children Count"):
                if new_count != current_children:
                    if update_stripe_subscription_children(member_data['stripe_subscription_id'], new_count):
                        db_utils.update_children_count(st.session_state.email, new_count)
                        st.success(f"Children count updated to {new_count}. Your Stripe subscription has been modified. Changes will reflect in your next billing cycle.")
                        st.rerun()
                    else:
                        st.error("Failed to update children count in Stripe. Please try again or contact support.")
                else:
                    st.info("No change in children count.")
        else:
            st.warning("Your subscription is not active or not found. Cannot manage children.")

    elif choice == "Medical Questionnaire":
        st.subheader("Your Medical Information")
        if member_data:
            decrypted_answers = "No medical information submitted."
            if member_data['medical_answers_encrypted']:
                try:
                    decrypted_answers = fernet.decrypt(member_data['medical_answers_encrypted'].encode('utf-8')).decode('utf-8')
                except Exception as e:
                    st.error(f"Could not decrypt medical information. Error: {e}")
                    decrypted_answers = "Error decrypting information."

            st.markdown("Current Information:")
            st.text_area("Saved Answers:", value=decrypted_answers, height=200, disabled=True)
            st.info("To update your medical information, please contact support. (Self-service update not yet implemented).")
            # Future: Add a form here to allow updates, re-encrypt and save.

    elif choice == "View Certificate":
        st.subheader("Your Urgent Care Certificate")
        if member_data:
            is_valid = is_certificate_valid_from_db(member_data['certificate_expiry_date'], member_data['certificate_status'])

            st.write(f"**Name:** {member_data['name']}")
            st.write(f"**Certificate ID:** {member_data['certificate_id']}")
            expiry_display = "N/A"
            if member_data['certificate_expiry_date']:
                try:
                    expiry_dt = datetime.fromisoformat(member_data['certificate_expiry_date'].replace('Z', '+00:00'))
                    expiry_display = expiry_dt.strftime('%Y-%m-%d')  # Just date for display
                except:
                    expiry_display = member_data['certificate_expiry_date']

            st.write(f"**Expires On:** {expiry_display}")
            st.write(f"**Status:** {member_data['certificate_status'].title()}")

            if member_data['certificate_status'] == 'active' and is_valid:
                st.success("Your certificate is currently VALID.")
                pdf_bytes = generate_certificate_pdf_bytes(member_data['name'], member_data['certificate_id'], expiry_display, member_data['certificate_status'])
                st.download_button(
                    label="Download Certificate PDF",
                    data=pdf_bytes,
                    file_name=f"UrgentCare_Certificate_{member_data['certificate_id']}.pdf",
                    mime="application/pdf"
                )
            elif member_data['certificate_status'] == 'pending_payment':
                st.warning("Your certificate is PENDING PAYMENT. Please complete your subscription payment.")
            else:
                st.error("Your certificate is currently INVALID or EXPIRED.")
        else:
            st.error("Could not retrieve certificate information.")

    elif choice == "Logout":
        st.session_state.logged_in = False
        st.session_state.email = ""
        st.session_state.name = ""
        st.session_state.user_id = ""
        st.success("You have been logged out.")
        st.query_params.clear()  # Clear any checkout params on logout
        st.rerun()

# Handle unauthorized access to logged-in pages
elif choice in ["My Dashboard", "Manage Children", "Medical Questionnaire", "View Certificate"] and not st.session_state.logged_in:
    st.warning("Please log in to access this page.")

st.sidebar.markdown("---")
st.sidebar.info("This is a demo application. For actual medical emergencies, please contact your local emergency services.")
