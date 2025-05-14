# webhook_listener.py
from fastapi import FastAPI, Request, HTTPException, Header
import stripe
from datetime import datetime, timedelta, timezone
import db_utils # Our new database utility module
import os

app = FastAPI()

try:
    stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
    endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")
    if not stripe.api_key or not endpoint_secret:
        raise ValueError("Stripe API key or Webhook secret not set in environment variables.")
except Exception as e:
    print(f"Error during webhook listener configuration: {e}")
    # Consider exiting or logging fatal error if essential config is missing
    # For now, it will allow the app to start but webhooks will fail.

@app.post("/webhook")
async def stripe_webhook(request: Request, stripe_signature: str = Header(None)):
    payload = await request.body()

    if not endpoint_secret:
        print("ERROR: Stripe webhook secret is not configured.")
        raise HTTPException(status_code=500, detail="Webhook secret not configured.")

    try:
        event = stripe.Webhook.construct_event(payload, stripe_signature, endpoint_secret)
    except ValueError as e: # Invalid payload
        print(f"WEBHOOK ERROR (ValueError): {e}")
        raise HTTPException(status_code=400, detail=f"Invalid payload: {e}")
    except stripe.error.SignatureVerificationError as e: # Invalid signature
        print(f"WEBHOOK ERROR (SignatureVerificationError): {e}")
        raise HTTPException(status_code=400, detail=f"Invalid signature: {e}")
    except Exception as e:
        print(f"WEBHOOK ERROR (General): {e}")
        raise HTTPException(status_code=500, detail=f"Webhook processing error: {e}")

    print(f"Received event: type={event['type']}, id={event['id']}")

    # Handle the event
    if event['type'] == 'checkout.session.completed':
        session = event['data']['object']
        stripe_customer_id = session.get('customer')
        stripe_subscription_id = session.get('subscription')
        client_reference_id = session.get('client_reference_id') # If you pass user ID during checkout creation
        app_user_id = session.get('metadata', {}).get('app_user_id')


        if stripe_customer_id and stripe_subscription_id and app_user_id:
            # Update member record with Stripe IDs
            # The subscription might still be 'incomplete' if payment is processing
            # or 'trialing' or 'active'
            # We set it to 'active' optimistically, invoice.payment_succeeded will confirm
            # Or, retrieve subscription to get actual status
            try:
                subscription = stripe.Subscription.retrieve(stripe_subscription_id)
                sub_status = subscription.status # e.g., active, trialing, incomplete, past_due
                cert_status = 'pending_payment'
                if sub_status == 'active' or sub_status == 'trialing':
                    cert_status = 'active'
                
                expiry_timestamp = subscription.current_period_end
                expiry_dt_utc = datetime.fromtimestamp(expiry_timestamp, timezone.utc)
                expiry_iso = expiry_dt_utc.isoformat()

                # Find member by app_user_id (which we stored in metadata)
                # This is more reliable than email if email can change.
                # For now, assuming app_user_id is the primary key from our members table.
                # We need a function in db_utils: get_member_by_id(app_user_id)
                # And update_member_stripe_ids_by_id(app_user_id,...)
                # For now, let's assume we update by email if app_user_id is not directly usable as PK
                member = db_utils.get_member_by_email(session.get('customer_details', {}).get('email'))
                if member:
                     db_utils.update_member_subscription_details(
                         member['email'], stripe_customer_id, stripe_subscription_id, subscription_status=sub_status
                     )
                     db_utils.update_subscription_status(stripe_subscription_id, sub_status, cert_status, expiry_iso)
                     print(f"Checkout completed for {member['email']}. Sub ID: {stripe_subscription_id}, Status: {sub_status}")
                else:
                    print(f"ERROR: Member not found for checkout session {session.id} with email {session.get('customer_details', {}).get('email')}")

            except stripe.error.StripeError as e:
                print(f"Stripe API error during checkout.session.completed handling: {e}")
            except Exception as e:
                print(f"DB error during checkout.session.completed handling: {e}")
        else:
            print(f"Checkout session {session.id} completed but missing customer, subscription, or app_user_id in metadata.")


    elif event['type'] == 'invoice.payment_succeeded':
        invoice = event['data']['object']
        stripe_subscription_id = invoice.get('subscription')
        if stripe_subscription_id:
            try:
                # Retrieve the subscription to get the current period end
                subscription = stripe.Subscription.retrieve(stripe_subscription_id)
                expiry_timestamp = subscription.current_period_end
                expiry_dt_utc = datetime.fromtimestamp(expiry_timestamp, timezone.utc)
                expiry_iso = expiry_dt_utc.isoformat()
                
                db_utils.update_subscription_status(stripe_subscription_id, 'active', 'active', expiry_iso)
                print(f"Payment succeeded for subscription {stripe_subscription_id}. Certificate active until {expiry_iso}.")
            except stripe.error.StripeError as e:
                print(f"Stripe API error during invoice.payment_succeeded: {e}")
            except Exception as e:
                print(f"DB error during invoice.payment_succeeded: {e}")

    elif event['type'] == 'invoice.payment_failed':
        invoice = event['data']['object']
        stripe_subscription_id = invoice.get('subscription')
        if stripe_subscription_id:
            # Mark certificate as expired/payment_failed, subscription as past_due
            # Expiry date could be set to now or past to ensure invalidity
            expiry_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
            db_utils.update_subscription_status(stripe_subscription_id, 'past_due', 'expired', expiry_iso)
            print(f"Payment failed for subscription {stripe_subscription_id}. Certificate marked expired.")

    elif event['type'] == 'customer.subscription.updated':
        subscription = event['data']['object']
        stripe_subscription_id = subscription.id
        new_status = subscription.status # e.g., active, past_due, canceled
        
        cert_status = 'expired' # Default if not active
        if new_status == 'active' or new_status == 'trialing':
            cert_status = 'active'
        elif new_status == 'canceled':
            cert_status = 'revoked'
        
        expiry_timestamp = subscription.current_period_end
        expiry_dt_utc = datetime.fromtimestamp(expiry_timestamp, timezone.utc)
        expiry_iso = expiry_dt_utc.isoformat()

        # Update children count in local DB if it changed in Stripe
        # This requires comparing Stripe's quantity for child item with local DB
        member = db_utils.get_member_by_stripe_subscription_id(stripe_subscription_id)
        if member:
            children_on_stripe = 0
            for item in subscription.get('items', {}).get('data',):
                if item.get('price', {}).get('id') == os.getenv("CHILD_PRICE_ID"): # Compare with CHILD_PRICE_ID from env/secrets
                    children_on_stripe = item.get('quantity', 0)
                    break
            if member['children']!= children_on_stripe:
                db_utils.update_children_count(member['email'], children_on_stripe)
                print(f"Updated children count for sub {stripe_subscription_id} to {children_on_stripe} based on Stripe.")

        db_utils.update_subscription_status(stripe_subscription_id, new_status, cert_status, expiry_iso)
        print(f"Subscription {stripe_subscription_id} updated. Status: {new_status}. Cert expiry: {expiry_iso}.")

    elif event['type'] == 'customer.subscription.deleted': # Handles cancellations
        subscription = event['data']['object']
        stripe_subscription_id = subscription.id
        # Mark certificate as revoked/expired, subscription as canceled
        expiry_iso = (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()
        db_utils.update_subscription_status(stripe_subscription_id, 'canceled', 'revoked', expiry_iso)
        print(f"Subscription {stripe_subscription_id} deleted (canceled). Certificate revoked.")

    else:
        print(f"Unhandled event type {event['type']}")

    return {"status": "success"}

# To run this (example): uvicorn webhook_listener:app --reload --port 8000
# Ensure STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET are set as environment variables.
