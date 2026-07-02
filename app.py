# app.py
import streamlit as st
import pandas as pd
import requests
import gzip
from io import BytesIO
import plotly.graph_objects as plotly_go
import time 

from config import STRIKE_STEP
from upstox_data import UpstoxDataFetcher
from strategy import run_non_directional_backtest, plot_payoff_graph

st.set_page_config(page_title="Non-Directional Options Backtester", layout="wide")
st.title("⚖️ Advanced Non-Directional Strategy Tester")

st.sidebar.header("Criteria Selection")
ui_access_token = st.sidebar.text_input("Upstox Access Token", type="password", help="Paste your daily generated token here")
symbol = st.sidebar.selectbox("Underlying Index", ["NIFTY", "SENSEX"])
backtest_date = st.sidebar.date_input("Backtest Date")
expiry_date = st.sidebar.date_input("Options Expiry Date")
num_lots = st.sidebar.number_input("Number of Lots to Trade", min_value=1, max_value=500, value=10, step=1)

@st.cache_data(show_spinner=False)
def load_upstox_instruments():
    url = 'https://assets.upstox.com/market-quote/instruments/exchange/complete.csv.gz'
    response = requests.get(url)
    with gzip.open(BytesIO(response.content), 'rt') as f:
        return pd.read_csv(f)

def color_profit(val):
    if pd.isna(val) or val == '': return ''
    try:
        color = '#00a65a' if float(val) > 0 else '#f56954' if float(val) < 0 else 'black'
        return f'color: {color}; font-weight: bold'
    except ValueError:
        return ''

if st.button("🚀 Run Strangle Adjustments"):
    if not ui_access_token:
        st.sidebar.error("⚠️ Please enter your Upstox Access Token first.")
    else:
        date_str = backtest_date.strftime('%Y-%m-%d')
        exp_date_str = expiry_date.strftime('%Y-%m-%d')
        step = STRIKE_STEP.get(symbol, 50)
        
        with st.spinner("Downloading Instruments Master..."):
            inst_df = load_upstox_instruments()
            
        fetcher = UpstoxDataFetcher(access_token=ui_access_token)
        idx_key = "BSE_INDEX|SENSEX" if symbol == "SENSEX" else "NSE_INDEX|Nifty 50"
        
        with st.spinner("Fetching Spot Timeline..."):
            spot_df = fetcher.get_historical_data(idx_key, '1minute', date_str, date_str)
            
        if spot_df.empty:
            st.error(f"No underlying market data found for {symbol} on {date_str}. (Check if your token is valid!)")
        else:
            st.info("Live Execution Engine Running...")
            
            terminal_container = st.empty() 
            live_logs = [f"[{date_str} 09:15:00] Market Open. Waiting for 09:45 AM deployment..."]
            terminal_container.code('\n'.join(live_logs), language='bash')
            
            def live_ui_update(message):
                live_logs.append(message)
                terminal_container.code('\n'.join(live_logs), language='bash')
                
            # Execute Strategy with the selected lots
            results, trades_df, active_data = run_non_directional_backtest(
                spot_df, inst_df, fetcher, symbol, date_str, exp_date_str, num_lots, live_ui_update
            )
                
            if not results:
                st.warning("Could not execute strategy (Premium conditions not met at 9:45 AM).")
            else:
                st.success(f"Backtest Complete! Status: {results['Status']}")
                st.metric(label="Total Strategy PnL (Rs)", value=f"₹ {round(results['Total PnL'], 2)}")
                
                st.subheader("Trade Execution Log")
                if not trades_df.empty:
                    display_df = trades_df.copy()
                    
                    cols_to_format = ['Profit', 'Entry Price', 'Exit Price', 'Difference']
                    for col in cols_to_format:
                        if col in display_df.columns:
                            display_df[col] = display_df[col].apply(lambda x: f"{float(x):.2f}" if pd.notnull(x) and x != '' else "")
                    
                    styled_df = display_df.style.map(color_profit, subset=['Profit'])
                    st.dataframe(styled_df, use_container_width=True, hide_index=True)
                else:
                    st.write("No trades were closed.")
                    
                st.subheader("📈 Intraday Strategy PnL Curve")
                if 'mtm_history' in active_data and not active_data['mtm_history'].empty:
                    mtm_df = active_data['mtm_history']
                    fig_pnl = plotly_go.Figure()
                    fig_pnl.add_trace(plotly_go.Scatter(
                        x=mtm_df['timestamp'], 
                        y=mtm_df['mtm'], 
                        mode='lines', 
                        line=dict(color='#00a65a', width=2),
                        fill='tozeroy',
                        fillcolor='rgba(0, 166, 90, 0.1)'
                    ))
                    fig_pnl.add_hline(y=0, line_dash="dash", line_color="black")
                    fig_pnl.update_layout(
                        title="Realized + Floating MTM PnL", 
                        margin=dict(l=0, r=0, t=40, b=0),
                        yaxis_title="Profit & Loss (₹)",
                        xaxis_title="Time"
                    )
                    st.plotly_chart(fig_pnl, use_container_width=True)
                else:
                    st.warning("MTM Data array not found. Ensure you are using the latest strategy.py file.")
                
                col1, col2 = st.columns(2)
                
                with col1:
                    st.subheader("Underlying Index Chart (5-Min TF)")
                    spot_5m = spot_df.set_index('timestamp').resample('5min', label='left', closed='left').agg({
                        'open': 'first', 'high': 'max', 'low': 'min', 'close': 'last'
                    }).dropna().reset_index()

                    fig_spot = plotly_go.Figure(data=[plotly_go.Candlestick(
                        x=spot_5m['timestamp'], open=spot_5m['open'], high=spot_5m['high'], low=spot_5m['low'], close=spot_5m['close']
                    )])
                    fig_spot.update_layout(title=f"{symbol} 5-Minute Candles", margin=dict(l=0, r=0, t=30, b=0), xaxis_rangeslider_visible=False)
                    st.plotly_chart(fig_spot, use_container_width=True)
                    
                with col2:
                    st.subheader("Combined Premium Decay Chart")
                    if 'sell_ce_hist' in active_data and 'sell_pe_hist' in active_data:
                        ce_df = active_data['sell_ce_hist']
                        pe_df = active_data['sell_pe_hist']
                        combined = pd.merge(ce_df[['timestamp', 'close']], pe_df[['timestamp', 'close']], on='timestamp', suffixes=('_ce', '_pe'))
                        combined['total_premium'] = combined['close_ce'] + combined['close_pe']
                        
                        fig_prem = plotly_go.Figure(data=[plotly_go.Scatter(x=combined['timestamp'], y=combined['total_premium'], mode='lines', line=dict(color='orange'))])
                        fig_prem.update_layout(title="Short Legs Combined Premium", margin=dict(l=0, r=0, t=30, b=0))
                        st.plotly_chart(fig_prem, use_container_width=True)
                        
                st.subheader("Strategy Payoff Graph (At Expiry)")
                try:
                    fig_payoff = plot_payoff_graph(trades_df.to_dict('records'))
                    st.plotly_chart(fig_payoff, use_container_width=True)
                except Exception as e:
                    st.error("Waiting for complete trade records to plot payoff graph...")