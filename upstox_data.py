# upstox_data.py
import requests
import pandas as pd
from datetime import datetime
import time
import urllib.parse

class UpstoxDataFetcher:
    def __init__(self, access_token):
        self.headers = {
            'Accept': 'application/json',
            'Authorization': f'Bearer {access_token}'
        }
        self.expired_cache = {}  

    def get_historical_data(self, instrument_key, interval, from_date, to_date, retries=5):
        """Fetches historical OHLCV data from Upstox API v2 with robust Rate Limit handling."""
        encoded_key = urllib.parse.quote(instrument_key)
        
        # 1. Determine if the request includes today's date
        today_str = datetime.now().strftime('%Y-%m-%d')
        is_today = (to_date == today_str or from_date == today_str)

        # 2. Route the URL dynamically based on whether it is today or a past date
        if is_today:
            # The intraday endpoint does not take from_date and to_date parameters
            url = f'https://api.upstox.com/v2/historical-candle/intraday/{encoded_key}/{interval}'
        else:
            url = f'https://api.upstox.com/v2/historical-candle/{encoded_key}/{interval}/{to_date}/{from_date}'
        
        for attempt in range(retries):
            response = requests.get(url, headers=self.headers)
            
            if response.status_code == 429:
                time.sleep(1.5)
                continue
                
            # 3. Only try the expired instruments endpoint if it's a historical date
            if response.status_code in [400, 404] and not is_today:
                url_expired = f'https://api.upstox.com/v2/expired-instruments/historical-candle/{encoded_key}/{interval}/{to_date}/{from_date}'
                response = requests.get(url_expired, headers=self.headers)
                
            if response.status_code == 200:
                data = response.json().get('data', {}).get('candles', [])
                if not data:
                    return pd.DataFrame()
                
                df = pd.DataFrame(data, columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'oi'])
                df['timestamp'] = pd.to_datetime(df['timestamp']).dt.tz_convert('Asia/Kolkata').dt.tz_localize(None)
                df.sort_values('timestamp', inplace=True)
                df.reset_index(drop=True, inplace=True)
                return df
            
            if response.status_code != 429:
                print(f"Historical Data Error {response.status_code}: {response.text}")
                break
                
        return pd.DataFrame()

    def fetch_expired_options(self, underlying_key, expiry_date):
        """Fetches and caches all expired option contracts for a given expiry date."""
        cache_key = f"{underlying_key}_{expiry_date}"
        if cache_key in self.expired_cache:
            return self.expired_cache[cache_key]
            
        encoded_key = urllib.parse.quote(underlying_key)
        url = f'https://api.upstox.com/v2/expired-instruments/option/contract?instrument_key={encoded_key}&expiry_date={expiry_date}'
        
        response = requests.get(url, headers=self.headers)
        
        if response.status_code == 200:
            data = response.json().get('data', [])
            self.expired_cache[cache_key] = data
            return data
            
        print(f"Expired Options API Error {response.status_code}: {response.text}")
        return []

    def get_option_instrument_key(self, inst_df, symbol, expiry_date, strike, option_type):
        """Searches the instruments dataframe safely, with dynamic fallback to the Expired API."""
        exp_date_obj = pd.to_datetime(expiry_date).date()
        
        # 1. Search the live master CSV first (fastest)
        filtered = inst_df[
            (inst_df['instrument_type'] == 'OPTIDX') &
            (inst_df['name'] == symbol) &
            (pd.to_datetime(inst_df['expiry']).dt.date == exp_date_obj) &
            (abs(inst_df['strike'] - float(strike)) < 0.1) & 
            (inst_df['tradingsymbol'].str.endswith(option_type))
        ]
        
        if not filtered.empty:
            return filtered.iloc[0]['instrument_key']
            
        # 2. Fallback: Query the Expired Contracts API for historical testing
        idx_map = {
            "NIFTY": "NSE_INDEX|Nifty 50", 
            "BANKNIFTY": "NSE_INDEX|Nifty Bank", 
            "FINNIFTY": "NSE_INDEX|Nifty Fin Service",
            "SENSEX": "BSE_INDEX|SENSEX"
        }
        underlying_key = idx_map.get(symbol)
        
        if underlying_key:
            exp_date_str = exp_date_obj.strftime('%Y-%m-%d')
            expired_contracts = self.fetch_expired_options(underlying_key, exp_date_str)
            
            for contract in expired_contracts:
                c_strike = float(contract.get('strike_price', contract.get('strike', 0)))
                c_symbol = contract.get('trading_symbol', contract.get('tradingsymbol', ''))
                c_type = contract.get('instrument_type', '')
                
                if abs(c_strike - float(strike)) < 0.1:
                    if c_symbol.endswith(option_type) or c_type == option_type:
                        return contract.get('instrument_key')
                    
        return None
