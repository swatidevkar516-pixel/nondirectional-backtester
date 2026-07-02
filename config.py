# config.py

# Replace with your actual Upstox Access Token
ACCESS_TOKEN = "eyJ0eXAiOiJKV1QiLCJrZXlfaWQiOiJza192MS4wIiwiYWxnIjoiSFMyNTYifQ.eyJzdWIiOiI0TkNOV0UiLCJqdGkiOiI2YTQ2N2FjMmVhZDk4OTYxZWE4ZTJhMDAiLCJpc011bHRpQ2xpZW50IjpmYWxzZSwiaXNQbHVzUGxhbiI6dHJ1ZSwiaWF0IjoxNzgzMDAzODQyLCJpc3MiOiJ1ZGFwaS1nYXRld2F5LXNlcnZpY2UiLCJleHAiOjE3ODMwMjk2MDB9.zLHADUn3Z1TkJfXbUYtd-hf1waIlfHZS7hbGpln3cvo"

STRIKE_STEP = {
    "NIFTY": 50,
    "BANKNIFTY": 100,
    "FINNIFTY": 50,
    "SENSEX": 100 
}

# Accurate 2026 Lot Sizes (Quantity per 1 Lot)
LOT_SIZES = {
    "NIFTY": 65,
    "BANKNIFTY": 30,
    "FINNIFTY": 60,
    "SENSEX": 20
}

# The user wants to deploy 20 Lakhs 
DEPLOYED_CAPITAL = 2000000 

# Updated to reflect new SEBI expiry day margin realities (₹1.3L - ₹1.6L)
MARGIN_PER_HEDGED_LOT = 150000