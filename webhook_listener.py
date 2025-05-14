from fastapi import FastAPI, Request, HTTPException
import stripe
import sqlite3
import os

app = FastAPI()
stripe.api_key = os.getenv("STRIPE_SECRET_KEY")
endpoint_secret = os.getenv("STRIPE_WEBHOOK_SECRET")

@app.post("/webhook")
async def stripe_webhook(request: Request):
    payload = await request.body()
    sig_header = request.headers.get("stripe-signature")

    try:
        event = stripe.Webhook.construct_event(payload, sig_header, endpoint_secret)
    except stripe.error.SignatureVerificationError:
        raise HTTPException(status_code=400, detail="Invalid signature")

    conn = sqlite3.connect('urgent_care_members.db')
    c = conn.cursor()

    if event["type"] == "invoice.payment_succeeded":
        subscription_id = event["data"]["object"]["subscription"]
        c.execute("UPDATE members SET certificate_expiry=? WHERE stripe_subscription_id=?", (
            datetime.now() + timedelta(days=30), subscription_id))
        conn.commit()

    elif event["type"] == "invoice.payment_failed":
        subscription_id = event["data"]["object"]["subscription"]
        c.execute("UPDATE members SET certificate_expiry=? WHERE stripe_subscription_id=?", (
            datetime.now() - timedelta(days=1), subscription_id))
        conn.commit()

    return {"status": "success"}
