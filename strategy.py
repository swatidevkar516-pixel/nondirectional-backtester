# strategy.py
import pandas as pd
import numpy as np
import math
from datetime import datetime, time
import plotly.graph_objects as plotly_go
from config import STRIKE_STEP, LOT_SIZES, MARGIN_PER_HEDGED_LOT

# --- INTRADAY MEMORY CACHE (Fixes the Freezing Issue) ---
global_hist_data_cache = {}

# --- RESTORED BLACK-SCHOLES DELTA ESTIMATOR ---
def norm_cdf(x):
    return (1.0 + math.erf(x / math.sqrt(2.0))) / 2.0

def estimate_delta(spot, strike, dte_days, o_type, iv=0.15):
    if dte_days <= 0: dte_days = 0.001 
    t = dte_days / 365.0
    d1 = (math.log(spot / strike) + (0.5 * iv**2) * t) / (iv * math.sqrt(t))
    ce_delta = norm_cdf(d1)
    if o_type == "CE": return ce_delta
    else: return ce_delta - 1.0
        
def find_strike_by_delta(spot_price, target_delta, o_type, step, fetcher, inst_df, symbol, exp_date_str, date_str, target_time):
    atm = round(spot_price / step) * step
    current_date = datetime.strptime(date_str, '%Y-%m-%d')
    exp_date = datetime.strptime(exp_date_str, '%Y-%m-%d')
    dte_days = (exp_date - current_date).days
    time_fraction = (15.5 - (target_time.hour + target_time.minute/60)) / 24.0
    total_dte = max(dte_days + time_fraction, 0.001)

    target_delta_abs = abs(target_delta)
    best_strike = None
    closest_diff = 999

    for i in range(1, 40): 
        current_strike = atm + (i * step) if o_type == "CE" else atm - (i * step)
        calc_delta = abs(estimate_delta(spot_price, current_strike, total_dte, o_type))
        diff = abs(calc_delta - target_delta_abs)
        
        if diff < closest_diff:
            closest_diff = diff
            best_strike = current_strike
            
        if calc_delta < target_delta_abs:
            break
            
    if best_strike:
        prem, df = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, target_time, best_strike, o_type, date_str)
        if prem: return best_strike, prem, df
        
    return None, None, None

# --- CORE PREMIUM FUNCTIONS ---
def get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, target_time, strike, o_type, date_str):
    opt_key = fetcher.get_option_instrument_key(inst_df, symbol, exp_date_str, strike, o_type)
    if not opt_key: return None, None
    
    # Speed Optimization: Check memory before calling API
    cache_key = f"{opt_key}_{date_str}"
    if cache_key not in global_hist_data_cache:
        df = fetcher.get_historical_data(opt_key, '1minute', date_str, date_str)
        global_hist_data_cache[cache_key] = df
        
    df = global_hist_data_cache[cache_key]
    
    if df.empty: return None, None
    row = df[df['timestamp'].dt.time == target_time]
    if row.empty: return None, None
    return row.iloc[0]['close'], df

def run_non_directional_backtest(spot_df, inst_df, fetcher, symbol, date_str, exp_date_str, number_of_lots, ui_log_callback=None):
    # Clear cache at start of new backtest to keep memory clean
    global_hist_data_cache.clear()
    
    step = STRIKE_STEP.get(symbol, 50)
    base_lot_size = LOT_SIZES.get(symbol, 65)
    
    # MODIFIED: Dynamically size based on UI lot selection
    trade_qty = number_of_lots * base_lot_size
    strategy_deployed_capital = number_of_lots * MARGIN_PER_HEDGED_LOT
    
    open_trades = []
    closed_trades = []
    active_data = {} 
    
    mtm_history = []
    cumulative_realized_pnl = 0 
    
    target_entry_time = time(9, 45)
    
    positions_opened = False
    cooldown_until = None       
    sl_timestamps = []          
    
    adj_05_loss_triggered = False
    adj_05_profit_triggered = False 
    current_mode = "matrix" 
    
    timeline = spot_df[spot_df['timestamp'].dt.time >= target_entry_time]['timestamp'].tolist()
    
    def try_strangle_entry(current_time_obj, current_spot, time_str_log, mode="matrix"):
        atm = round(current_spot / step) * step
        
        if mode == "delta":
            c_strike, c_prem, c_df = find_strike_by_delta(current_spot, 0.10, "CE", step, fetcher, inst_df, symbol, exp_date_str, date_str, current_time_obj)
            p_strike, p_prem, p_df = find_strike_by_delta(current_spot, 0.10, "PE", step, fetcher, inst_df, symbol, exp_date_str, date_str, current_time_obj)
            
            # --- 0 DTE GREEK SAFEGUARD ---
            if c_prem and c_prem > 35:
                for i in range(1, 25):
                    adj_strike = c_strike + (i * step)
                    cp, cdf = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, adj_strike, "CE", date_str)
                    if cp and cp <= 35:
                        c_strike, c_prem, c_df = adj_strike, cp, cdf
                        break
                        
            if p_prem and p_prem > 35:
                for i in range(1, 25):
                    adj_strike = p_strike - (i * step)
                    pp, pdf = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, adj_strike, "PE", date_str)
                    if pp and pp <= 35:
                        p_strike, p_prem, p_df = adj_strike, pp, pdf
                        break
            
            if not c_strike or not p_strike:
                if ui_log_callback and current_time_obj.minute % 15 == 0: 
                    ui_log_callback(f"[{time_str_log}] SCAN: 0.10 Delta strikes missing data. Still scanning...")
                return False
                
            selected_ce = {'strike': c_strike, 'prem': c_prem, 'df': c_df}
            selected_pe = {'strike': p_strike, 'prem': p_prem, 'df': p_df}
            scan_log_str = "0.10 Delta"
            leg_label = "0.10 Proxy"
            
        else:
            ce_candidates = []
            pe_candidates = []
            
            # Scans up to 15 OTM strikes
            for i in range(1, 16):
                c_strike = atm + (i * step)
                c_prem, c_df = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, c_strike, "CE", date_str)
                if c_prem and c_prem <= 35: ce_candidates.append({'strike': c_strike, 'prem': c_prem, 'df': c_df})
                    
                p_strike = atm - (i * step)
                p_prem, p_df = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, p_strike, "PE", date_str)
                if p_prem and p_prem <= 35: pe_candidates.append({'strike': p_strike, 'prem': p_prem, 'df': p_df})
                    
            if not ce_candidates or not pe_candidates: 
                if ui_log_callback and current_time_obj.minute % 15 == 0: 
                    ui_log_callback(f"[{time_str_log}] SCAN: Top 15 OTM premiums > ₹35. Waiting for decay...")
                return False
                
            best_pair = None
            highest_combined_premium = -1 
            
            for ce in ce_candidates:
                for pe in pe_candidates:
                    diff = abs(ce['prem'] - pe['prem'])
                    max_premium = max(ce['prem'], pe['prem'])
                    
                    if max_premium > 20: max_allowed_diff = 6
                    elif max_premium < 15: max_allowed_diff = 3
                    else: max_allowed_diff = 5  
                        
                    if diff <= max_allowed_diff:
                        combined_premium = ce['prem'] + pe['prem']
                        if combined_premium > highest_combined_premium:
                            highest_combined_premium = combined_premium
                            best_pair = (ce, pe)
                            
            if not best_pair:
                if ui_log_callback and current_time_obj.minute % 15 == 0: 
                    ui_log_callback(f"[{time_str_log}] SCAN: Pair difference rule failed. Still scanning...")
                return False
                
            selected_ce, selected_pe = best_pair
            scan_log_str = "Top 15 OTM Max Prem"
            leg_label = "Main"
            
        has_active_hedges = any(t['Action Type'] == 'Buy' for t in open_trades)

        open_trades.append({'Action Type': 'Sell', 'Qty': trade_qty, 'Entry Time': time_str_log, 'Strike': f"{selected_ce['strike']}CE", 'Expiry': exp_date_str, 'Entry Price': selected_ce['prem'], 'Type': 'CE', 'Leg': leg_label, 'Raw Strike': selected_ce['strike'], 'Hist': selected_ce['df']})
        open_trades.append({'Action Type': 'Sell', 'Qty': trade_qty, 'Entry Time': time_str_log, 'Strike': f"{selected_pe['strike']}PE", 'Expiry': exp_date_str, 'Entry Price': selected_pe['prem'], 'Type': 'PE', 'Leg': leg_label, 'Raw Strike': selected_pe['strike'], 'Hist': selected_pe['df']})
        active_data['sell_ce_hist'] = selected_ce['df']
        active_data['sell_pe_hist'] = selected_pe['df']
        
        if ui_log_callback: 
            ui_log_callback(f"[{time_str_log}] ENTRY EXECUTED ({scan_log_str}): Sold {selected_ce['strike']}CE @ ₹{selected_ce['prem']} | Sold {selected_pe['strike']}PE @ ₹{selected_pe['prem']}")
            
        if not has_active_hedges:
            h_c_strike = selected_ce['strike'] + (2*step)
            h_c_prem, h_c_hist = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, h_c_strike, "CE", date_str)
            
            for i in range(3, 60):
                hc_s = atm + (i * step)
                hcp, hcd = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, hc_s, "CE", date_str)
                if hcp and hcp <= 2:
                    h_c_strike, h_c_prem, h_c_hist = hc_s, hcp, hcd
                    break
                    
            if not h_c_prem: h_c_prem, h_c_hist = 2, selected_ce['df'] 
            
            h_p_strike = selected_pe['strike'] - (2*step)
            h_p_prem, h_p_hist = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, h_p_strike, "PE", date_str)
            
            for i in range(3, 60):
                hp_s = atm - (i * step)
                hpp, hpd = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, current_time_obj, hp_s, "PE", date_str)
                if hpp and hpp <= 2:
                    h_p_strike, h_p_prem, h_p_hist = hp_s, hpp, hpd
                    break
                    
            if not h_p_prem: h_p_prem, h_p_hist = 2, selected_pe['df']
            
            open_trades.append({'Action Type': 'Buy', 'Qty': trade_qty, 'Entry Time': time_str_log, 'Strike': f"{h_c_strike}CE", 'Expiry': exp_date_str, 'Entry Price': h_c_prem, 'Type': 'CE', 'Leg': 'Hedge', 'Raw Strike': h_c_strike, 'Hist': h_c_hist})
            open_trades.append({'Action Type': 'Buy', 'Qty': trade_qty, 'Entry Time': time_str_log, 'Strike': f"{h_p_strike}PE", 'Expiry': exp_date_str, 'Entry Price': h_p_prem, 'Type': 'PE', 'Leg': 'Hedge', 'Raw Strike': h_p_strike, 'Hist': h_p_hist})
            if ui_log_callback: 
                ui_log_callback(f"[{time_str_log}] HEDGE EXECUTED: Bought {h_c_strike}CE @ ₹{h_c_prem} | Bought {h_p_strike}PE @ ₹{h_p_prem}")
            
        return True

    # MAIN EXECUTION TIMELINE LOOP
    for current_time in timeline:
        curr_time_obj = current_time.time()
        time_str = current_time.strftime("%H:%M:%S")
        spot_curr = spot_df[spot_df['timestamp'] == current_time].iloc[0]['close']
        
        if not positions_opened:
            if cooldown_until and current_time < cooldown_until:
                continue
            elif cooldown_until:
                if ui_log_callback: ui_log_callback(f"[{time_str}] Cooldown Finished. Resuming strategy scans...")
                cooldown_until = None
            
            if try_strangle_entry(curr_time_obj, spot_curr, time_str, mode=current_mode):
                positions_opened = True
            continue

        current_mtm_points = 0
        leg_prices = {}
        
        for trade in open_trades:
            hist = trade['Hist']
            curr_row = hist[hist['timestamp'] == current_time]
            
            current_close = trade.get('Current Close', trade['Entry Price'])
            current_high = trade.get('Current High', trade['Entry Price'])
            
            if not curr_row.empty:
                current_close = curr_row.iloc[0]['close']
                current_high = curr_row.iloc[0]['high']
                trade['Current Close'] = current_close
                trade['Current High'] = current_high
                
            if trade['Action Type'] == 'Sell': current_mtm_points += (trade['Entry Price'] - current_close)
            else: current_mtm_points += (current_close - trade['Entry Price'])
                
            if trade['Leg'] != 'Hedge': leg_prices[trade['Type']] = current_close

        floating_rupee_pnl = current_mtm_points * trade_qty
        total_system_pnl = cumulative_realized_pnl + floating_rupee_pnl
        
        # MODIFIED: PnL Percent is strictly measured against the dynamic capital sizing
        pnl_percent = (total_system_pnl / strategy_deployed_capital) * 100
        
        mtm_history.append({'timestamp': current_time, 'mtm': total_system_pnl, 'pnl_percent': pnl_percent})
        
        force_proxy_shift = False
        if pnl_percent <= -0.50 and not adj_05_loss_triggered:
            adj_05_loss_triggered = True
            current_mode = "delta"  
            force_proxy_shift = True
        elif pnl_percent >= 0.50 and not adj_05_profit_triggered:
            adj_05_profit_triggered = True
            current_mode = "delta"  
            force_proxy_shift = True
            
        exit_reason = None
        re_enter = False
        proxy_shift_triggered_now = False 
        
        if pnl_percent <= -1.0:
            exit_reason = f"Max Capital Protection (-1.0% System Loss)"
            
        elif pnl_percent >= 2.0:
            exit_reason = f"Max Daily Profit Reached (+2.0% System Profit)"
            
        elif curr_time_obj >= time(14, 50) and curr_time_obj < time(15, 8):
            if curr_time_obj == time(14, 50) and pnl_percent > 1.0:
                exit_reason = f"Conditional Time Exit: Profit > 1.0% ({round(pnl_percent,2)}%) at 14:50"
                
        elif curr_time_obj >= time(15, 8):
            exit_reason = "Final Time Exit (15:8)"
            
        else:
            for trade in open_trades:
                if trade['Action Type'] == 'Sell':
                    current_high_val = trade.get('Current High', trade['Entry Price'])
                    sl_level = trade['Entry Price'] + 10 if trade['Entry Price'] < 20 else trade['Entry Price'] * 1.50
                    sl_type = "10-Point" if trade['Entry Price'] < 20 else "50%"
                    if current_high_val >= sl_level:
                        exit_reason = f"{sl_type} SL Hit"
                        if trade['Leg'] != 'Hedge': sl_timestamps.append(current_time)
                        re_enter = True
                        break
                        
            if not exit_reason:
                for trade in open_trades:
                    if trade['Action Type'] == 'Sell':
                        if trade['Type'] == 'CE' and spot_curr > trade['Raw Strike']:
                            exit_reason = f"CE ITM Cross Breach ({spot_curr})"
                            re_enter = True
                            break
                        elif trade['Type'] == 'PE' and spot_curr < trade['Raw Strike']:
                            exit_reason = f"PE ITM Cross Breach ({spot_curr})"
                            re_enter = True
                            break
                            
            if not exit_reason:
                for trade in open_trades:
                    if trade['Action Type'] == 'Sell':
                        current_close_val = trade.get('Current Close', trade['Entry Price'])
                        decay_target = trade['Entry Price'] * 0.30 
                        if curr_time_obj < time(14, 0) and current_close_val <= decay_target:
                            exit_reason = f"{trade['Type']} Leg 70% Decay Target Reached"
                            re_enter = True
                            break
                            
            if not exit_reason and 'CE' in leg_prices and 'PE' in leg_prices:
                if abs(leg_prices['CE'] - leg_prices['PE']) > 25:
                    exit_reason = f"Premium Divergence violation (>₹25)"
                    re_enter = True
                    
            if not exit_reason and force_proxy_shift:
                if pnl_percent <= -0.50:
                    exit_reason = "System Loss Buffer (-0.50%) Shift"
                else:
                    exit_reason = "System Profit Booster (+0.50%) Shift"
                proxy_shift_triggered_now = True

        if exit_reason:
            if ui_log_callback: ui_log_callback(f"[{time_str}] ALERT: {exit_reason}")
            
            should_break = False
            
            if "Max" in exit_reason or "Time" in exit_reason:
                legs_to_close = open_trades
                open_trades = []
                should_break = True
                re_enter = False 
            else:
                legs_to_close = [t for t in open_trades if t['Action Type'] == 'Sell']
                open_trades = [t for t in open_trades if t['Action Type'] == 'Buy'] 
                positions_opened = False 
            
            for trade in legs_to_close:
                trade['Exit Time'] = time_str
                
                if trade['Action Type'] == 'Sell':
                    sl_level = trade['Entry Price'] + 10 if trade['Entry Price'] < 20 else trade['Entry Price'] * 1.50
                    current_high_val = trade.get('Current High', trade['Entry Price'])
                    
                    if current_high_val >= sl_level:
                        trade['Exit Price'] = sl_level
                        base_remark = exit_reason if "SL" in exit_reason else f"{exit_reason} (Capped @ SL {round(sl_level,2)})"
                        trade['Remark'] = f"[{trade['Leg']}] {base_remark}"
                    else:
                        trade['Exit Price'] = trade.get('Current Close', trade['Entry Price'])
                        trade['Remark'] = f"[{trade['Leg']}] {exit_reason}"
                else:
                    trade['Exit Price'] = trade.get('Current Close', trade['Entry Price'])
                    trade['Remark'] = f"[{trade['Leg']}] {exit_reason}"
                
                trade['Difference'] = round(trade['Exit Price'] - trade['Entry Price'], 2)
                
                if trade['Action Type'] == 'Sell':
                    trade['Profit'] = round((trade['Entry Price'] - trade['Exit Price']) * trade['Qty'], 2)
                else:
                    trade['Profit'] = round((trade['Exit Price'] - trade['Entry Price']) * trade['Qty'], 2)
                    
                closed_trades.append(trade)
                cumulative_realized_pnl += trade['Profit']
            
            if "SL Hit" in exit_reason:
                sl_timestamps = [t for t in sl_timestamps if (current_time - t).total_seconds() <= 1800]
                if len(sl_timestamps) >= 3: 
                    if ui_log_callback: ui_log_callback(f"[{time_str}] SYSTEM: 3 SL hits in 30 mins. Entering 30-min Cooldown.")
                    cooldown_until = current_time + pd.Timedelta(minutes=30)
                    sl_timestamps = []
                    re_enter = False 
                    
            if proxy_shift_triggered_now:
                if ui_log_callback: ui_log_callback(f"[{time_str}] SHIFT: Replacing closed legs with 0.10 Delta Proxies.")
                c_s, c_p, c_h = find_strike_by_delta(spot_curr, 0.10, "CE", step, fetcher, inst_df, symbol, exp_date_str, date_str, curr_time_obj)
                p_s, p_p, p_h = find_strike_by_delta(spot_curr, 0.10, "PE", step, fetcher, inst_df, symbol, exp_date_str, date_str, curr_time_obj)
                
                if c_p and c_p > 35:
                    for i in range(1, 25):
                        adj_s = c_s + (i * step)
                        cp, cdf = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, curr_time_obj, adj_s, "CE", date_str)
                        if cp and cp <= 35:
                            c_s, c_p, c_h = adj_s, cp, cdf
                            break
                            
                if p_p and p_p > 35:
                    for i in range(1, 25):
                        adj_s = p_s - (i * step)
                        pp, pdf = get_premium_at_time(fetcher, inst_df, symbol, exp_date_str, curr_time_obj, adj_s, "PE", date_str)
                        if pp and pp <= 35:
                            p_s, p_p, p_h = adj_s, pp, pdf
                            break
                
                if c_s and p_s: 
                    open_trades.append({'Action Type': 'Sell', 'Qty': trade_qty, 'Entry Time': time_str, 'Strike': f"{c_s}CE", 'Expiry': exp_date_str, 'Entry Price': c_p, 'Type': 'CE', 'Leg': '0.10 Proxy', 'Raw Strike': c_s, 'Hist': c_h})
                    open_trades.append({'Action Type': 'Sell', 'Qty': trade_qty, 'Entry Time': time_str, 'Strike': f"{p_s}PE", 'Expiry': exp_date_str, 'Entry Price': p_p, 'Type': 'PE', 'Leg': '0.10 Proxy', 'Raw Strike': p_s, 'Hist': p_h})
                    active_data['sell_ce_hist'] = c_h
                    active_data['sell_pe_hist'] = p_h
                    positions_opened = True 
                else:
                    if ui_log_callback: ui_log_callback(f"[{time_str}] DATA WARNING: 0.10 Delta strikes missing data. Retrying proxy setup next minute.")
                    positions_opened = False
                
            if should_break:
                break 

    # EOD SYSTEM FLUSH
    if open_trades:
        flush_time = time_str
        for trade in open_trades:
            trade['Exit Time'] = flush_time
            trade['Exit Price'] = trade.get('Current Close', trade['Entry Price'])
            trade['Difference'] = round(trade['Exit Price'] - trade['Entry Price'], 2)
            trade['Remark'] = f"[{trade['Leg']}] Final Time Exit (15:8) / EOD Flush"
            if trade['Action Type'] == 'Sell':
                trade['Profit'] = round((trade['Entry Price'] - trade['Exit Price']) * trade['Qty'], 2)
            else:
                trade['Profit'] = round((trade['Exit Price'] - trade['Entry Price']) * trade['Qty'], 2)
            closed_trades.append(trade)

    active_data['mtm_history'] = pd.DataFrame(mtm_history)

    if not closed_trades:
        return {'Total PnL': 0, 'Status': "Execution complete - No closed trades"}, pd.DataFrame(), active_data
        
    df = pd.DataFrame(closed_trades)
    columns_order = ['Leg', 'Action Type', 'Qty', 'Entry Time', 'Exit Time', 'Strike', 'Expiry', 'Profit', 'Entry Price', 'Exit Price', 'Difference', 'Remark']
    df = df[columns_order]
    
    total_pnl = df['Profit'].sum()
    results = {'Total PnL': total_pnl, 'Status': "Completed"}
    
    return results, df, active_data

def plot_payoff_graph(trades_log):
    if not trades_log: return plotly_go.Figure()
    
    final_exit_time = trades_log[-1]['Exit Time']
    final_portfolio = [t for t in trades_log if t['Exit Time'] == final_exit_time]
    previous_closed = [t for t in trades_log if t['Exit Time'] != final_exit_time]
    
    realized_pnl = sum(t['Profit'] for t in previous_closed)
    
    sells = [t for t in final_portfolio if t['Action Type'] == 'Sell']
    buys = [t for t in final_portfolio if t['Action Type'] == 'Buy']
    
    if not sells and not buys: 
        return plotly_go.Figure()
    
    center_strike = int(sells[0]['Strike'][:-2]) if sells else int(buys[0]['Strike'][:-2])
    spot_range = np.arange(center_strike - 1500, center_strike + 1500, 10)
    
    payoff = np.zeros(len(spot_range)) + realized_pnl
    
    for t in sells:
        strike = int(t['Strike'][:-2])
        if t['Strike'].endswith('CE'):
            payoff += np.where(spot_range > strike, strike - spot_range + t['Entry Price'], t['Entry Price']) * t['Qty']
        else:
            payoff += np.where(spot_range < strike, spot_range - strike + t['Entry Price'], t['Entry Price']) * t['Qty']
            
    for t in buys:
        strike = int(t['Strike'][:-2])
        if t['Strike'].endswith('CE'):
            payoff += np.where(spot_range > strike, spot_range - strike - t['Entry Price'], -t['Entry Price']) * t['Qty']
        else:
            payoff += np.where(spot_range < strike, strike - spot_range - t['Entry Price'], -t['Entry Price']) * t['Qty']

    fig = plotly_go.Figure(data=[plotly_go.Scatter(x=spot_range, y=payoff, mode='lines', fill='tozeroy', fillcolor='rgba(0, 255, 0, 0.2)')])
    fig.add_hline(y=0, line_dash="dash", line_color="black")
    fig.update_layout(title=f"EOD Payoff (Including ₹{round(realized_pnl,2)} Realized PnL)", xaxis_title="Index Price", yaxis_title="Profit & Loss (₹)")
    return fig