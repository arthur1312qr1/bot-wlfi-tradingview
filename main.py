import os
import json
import hmac
import base64
import hashlib
import time
import traceback
from datetime import datetime
from flask import Flask, request, jsonify
import requests
from concurrent.futures import ThreadPoolExecutor
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

app = Flask(__name__)

# === CONFIGURAÇÕES ===
API_KEY = os.environ.get('BITGET_API_KEY', '')
API_SECRET = os.environ.get('BITGET_API_SECRET', '')
API_PASSPHRASE = os.environ.get('BITGET_API_PASSPHRASE', '')
BASE_URL = 'https://api.bitget.com'
TARGET_SYMBOL = 'WLFIUSDT'
PRODUCT_TYPE = 'USDT-FUTURES'
MARGIN_COIN = 'USDT'

# 🎯 TRADING
LEVERAGE = 4
POSITION_SIZE_PERCENT = 0.96
MIN_ORDER_VALUE = 5

# 🛡️ PROTEÇÕES (só ativas DENTRO de sinal do TradingView)
STOP_LOSS_PERCENT = 0.07  # 7% do capital = 1.75% preço com 4x
TRAILING_PROFIT_DROP = 0.25  # 25% de queda do pico (SEM alavancagem)
REENTRY_THRESHOLD = 0.003  # 0.3% SEM alavancagem (1.2% COM 4x)
MIN_PROFIT_FOR_TRAILING = 0.008  # 0.8% SEM alavancagem para ativar trailing

# ⚙️ SISTEMA
CACHE_TTL = 0.1  # 100ms
REQUEST_TIMEOUT = 10

# === TRACKING DE POSIÇÕES ===
position_tracker = {
    'entry_price': 0,
    'side': '',
    'size': 0,
    'stop_loss_price': 0,
    'last_check': 0,
    'peak_profit_percent': 0,
    'temporarily_closed': False,
    'reentry_price': 0,
    'reentry_attempts': 0,
    'last_trailing_action': 0,
    'tradingview_active': False,  # 🔥 CRÍTICO: Só opera se TV estiver ativo
    'tv_position': '',  # 'long', 'short', 'flat'
}

cache = {'time': 0, 'data': None}
last_webhook = {'time': 0, 'data': None}

def log(msg):
    timestamp = datetime.utcnow().strftime('[%H:%M:%S.%f')[:-3] + ']'
    print(f"{timestamp} {msg}", flush=True)

def get_session():
    session = requests.Session()
    retry = Retry(total=3, backoff_factor=0.3, status_forcelist=[500, 502, 503, 504])
    adapter = HTTPAdapter(max_retries=retry)
    session.mount('http://', adapter)
    session.mount('https://', adapter)
    return session

def generate_signature(timestamp, method, endpoint, body=''):
    """Gera assinatura para API Bitget V2"""
    message = timestamp + method + endpoint + body
    mac = hmac.new(API_SECRET.encode(), message.encode(), hashlib.sha256)
    return base64.b64encode(mac.digest()).decode()

def bitget_request(method, endpoint, params=None):
    timestamp = str(int(time.time() * 1000))
    
    # Para Bitget API V2:
    # GET: params vão na URL, assinatura SEM query string
    # POST: params vão no body JSON, assinatura COM body
    if method == 'POST' and params:
        body_str = json.dumps(params)
    else:
        body_str = ''
    
    # Gerar assinatura (endpoint SEM query string para GET)
    signature = generate_signature(timestamp, method, endpoint, body_str)
    
    headers = {
        'ACCESS-KEY': API_KEY,
        'ACCESS-SIGN': signature,
        'ACCESS-TIMESTAMP': timestamp,
        'ACCESS-PASSPHRASE': API_PASSPHRASE,
        'Content-Type': 'application/json',
        'locale': 'en-US'
    }
    
    url = BASE_URL + endpoint
    session = get_session()
    
    try:
        if method == 'GET':
            # Para GET: params vão como query parameters na URL
            response = session.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        else:
            # Para POST: params vão no body JSON
            response = session.post(url, headers=headers, json=params, timeout=REQUEST_TIMEOUT)
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        log(f"ERR {method} {endpoint}: {e}")
        if hasattr(e, 'response') and e.response is not None:
            try:
                err_data = e.response.json()
                log(f"API ERR {e.response.status_code}: {err_data}")
                if params:
                    log(f"Params sent: {json.dumps(params, indent=2)}")
            except:
                log(f"Response text: {e.response.text}")
        return None

def get_account_balance():
    endpoint = '/api/v2/mix/account/accounts'
    params = {'productType': PRODUCT_TYPE}
    data = bitget_request('GET', endpoint, params)
    
    if data and data.get('code') == '00000':
        for item in data.get('data', []):
            if item.get('marginCoin') == MARGIN_COIN:
                return float(item.get('available', 0))
    return 0

def get_current_price():
    endpoint = '/api/v2/mix/market/ticker'
    params = {'symbol': TARGET_SYMBOL, 'productType': PRODUCT_TYPE}
    data = bitget_request('GET', endpoint, params)
    
    if data and data.get('code') == '00000':
        ticker_data = data.get('data', [])
        if ticker_data and len(ticker_data) > 0:
            return float(ticker_data[0].get('lastPr', 0))
    return 0

def get_positions():
    endpoint = '/api/v2/mix/position/all-position'
    params = {'productType': PRODUCT_TYPE, 'marginCoin': MARGIN_COIN}
    data = bitget_request('GET', endpoint, params)
    
    long_size = 0
    short_size = 0
    
    if data and data.get('code') == '00000':
        for pos in data.get('data', []):
            if pos.get('symbol') == TARGET_SYMBOL:
                total = float(pos.get('total', 0))
                side = pos.get('holdSide', '')
                if side == 'long':
                    long_size = total
                elif side == 'short':
                    short_size = total
    
    return long_size, short_size

def get_cached_data():
    current_time = time.time()
    if current_time - cache['time'] < CACHE_TTL and cache['data']:
        return cache['data']
    
    balance = get_account_balance()
    price = get_current_price()
    long_size, short_size = get_positions()
    
    cache['time'] = current_time
    cache['data'] = (balance, price, (long_size, short_size))
    
    log(f"Data: BAL=${balance:.2f} PRICE=${price:.4f} L={long_size} S={short_size}")
    return cache['data']

def calculate_quantity(balance, price):
    capital = balance * POSITION_SIZE_PERCENT
    exposure = capital * LEVERAGE
    
    if exposure < MIN_ORDER_VALUE:
        log(f"Exposure {exposure:.2f} < MIN_ORDER_VALUE {MIN_ORDER_VALUE}")
        return 0
    
    quantity = exposure / price
    log(f"${balance:.2f}*{int(POSITION_SIZE_PERCENT*100)}%*{LEVERAGE}x=${exposure:.2f} QTY:{quantity:.1f}")
    
    return round(quantity, 0)

def open_position_market(symbol, side, size, is_reentry=False):
    """Abre posição A MERCADO (garante execução)"""
    if size <= 0:
        return False
    
    current_price = get_current_price()
    if current_price <= 0:
        log("Failed to get current price")
        return False
    
    endpoint = '/api/v2/mix/order/place-order'
    params = {
        'symbol': symbol,
        'productType': PRODUCT_TYPE,
        'marginMode': 'crossed',
        'marginCoin': MARGIN_COIN,
        'side': side,
        'orderType': 'market',
        'size': str(int(size))
    }
    
    response = bitget_request('POST', endpoint, params)
    
    if response and response.get('code') == '00000':
        action_type = "REENTRY" if is_reentry else "OPEN"
        log(f"{action_type} {side.upper()} MARKET OK @ ~${current_price:.4f}")
        
        # Atualizar tracker
        position_tracker['entry_price'] = current_price
        position_tracker['side'] = 'long' if side == 'buy' else 'short'
        position_tracker['size'] = size
        position_tracker['temporarily_closed'] = False
        position_tracker['reentry_attempts'] = 0
        position_tracker['last_trailing_action'] = time.time()
        
        # Calcular e colocar stop loss
        if position_tracker['side'] == 'long':
            stop_price = current_price * (1 - (STOP_LOSS_PERCENT / LEVERAGE))
        else:
            stop_price = current_price * (1 + (STOP_LOSS_PERCENT / LEVERAGE))
        
        position_tracker['stop_loss_price'] = stop_price
        log(f"🛡️ STOP: ${stop_price:.4f} | ENTRY: ${current_price:.4f}")
        
        return True
    
    return False

def close_position_market(symbol, side):
    """Fecha posição A MERCADO"""
    endpoint = '/api/v2/mix/order/place-order'
    
    close_side = 'sell' if side == 'long' else 'buy'
    
    params = {
        'symbol': symbol,
        'productType': PRODUCT_TYPE,
        'marginMode': 'crossed',
        'marginCoin': MARGIN_COIN,
        'side': close_side,
        'orderType': 'market',
        'size': str(int(position_tracker['size'])),
        'reduceOnly': 'YES'
    }
    
    response = bitget_request('POST', endpoint, params)
    
    if response and response.get('code') == '00000':
        log(f"CLOSE {close_side.upper()} MARKET OK")
        return True
    
    return False

def check_stop_loss():
    """
    🛡️ VERIFICAÇÃO DE STOP LOSS
    ⚠️ SÓ FUNCIONA SE TRADINGVIEW ESTIVER ATIVO
    """
    # 🔥 CRÍTICO: Não faz nada se TV não tiver sinal ativo
    if not position_tracker.get('tradingview_active', False):
        return
    
    if not position_tracker['side'] or position_tracker['size'] <= 0:
        return
    
    current_time = time.time()
    if current_time - position_tracker['last_check'] < 0.5:
        return
    
    position_tracker['last_check'] = current_time
    
    # Buscar dados atuais
    cache['time'] = 0  # Forçar refresh
    balance, current_price, (long_size, short_size) = get_cached_data()
    
    if current_price <= 0:
        return
    
    # Verificar se posição foi fechada manualmente
    tracked_side = position_tracker['side']
    tracked_size = position_tracker['size']
    actual_size = long_size if tracked_side == 'long' else short_size
    
    if actual_size == 0 and tracked_size > 0:
        log("⚠️ Position manually closed! Cleaning tracker")
        position_tracker['side'] = ''
        position_tracker['size'] = 0
        position_tracker['temporarily_closed'] = False
        return
    
    # Verificar stop loss
    entry_price = position_tracker['entry_price']
    stop_price = position_tracker['stop_loss_price']
    side = position_tracker['side']
    
    stop_triggered = False
    
    if side == 'long' and current_price <= stop_price:
        stop_triggered = True
        pnl = ((current_price - entry_price) / entry_price) * LEVERAGE * 100
        log(f"🚨 STOP LOSS TRIGGERED!")
        log(f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | Stop: ${stop_price:.4f}")
        log(f"Loss: {pnl:.2f}% | CLOSING MARKET")
        
    elif side == 'short' and current_price >= stop_price:
        stop_triggered = True
        pnl = ((entry_price - current_price) / entry_price) * LEVERAGE * 100
        log(f"🚨 STOP LOSS TRIGGERED!")
        log(f"Entry: ${entry_price:.4f} | Current: ${current_price:.4f} | Stop: ${stop_price:.4f}")
        log(f"Loss: {pnl:.2f}% | CLOSING MARKET")
    
    if stop_triggered:
        if close_position_market(TARGET_SYMBOL, side):
            log(f"✅ Stop loss executed")
            position_tracker['side'] = ''
            position_tracker['size'] = 0
            position_tracker['temporarily_closed'] = False
            # ⚠️ NÃO desativa TV - aguarda próximo sinal

def check_trailing_profit():
    """
    💰 TRAILING PROFIT
    ⚠️ SÓ FUNCIONA SE TRADINGVIEW ESTIVER ATIVO
    """
    # 🔥 CRÍTICO: Não faz nada se TV não tiver sinal ativo
    if not position_tracker.get('tradingview_active', False):
        return
    
    if not position_tracker['side'] or position_tracker['size'] <= 0:
        return
    
    if position_tracker['temporarily_closed']:
        # Verificar reentrada
        check_reentry()
        return
    
    current_time = time.time()
    if current_time - position_tracker['last_check'] < 0.5:
        return
    
    # Cooldown entre ações de trailing
    if current_time - position_tracker['last_trailing_action'] < 3:
        return
    
    # Buscar preço atual
    cache['time'] = 0
    balance, current_price, (long_size, short_size) = get_cached_data()
    
    if current_price <= 0:
        return
    
    entry_price = position_tracker['entry_price']
    side = position_tracker['side']
    
    # Calcular lucro SEM alavancagem (como você pediu)
    if side == 'long':
        pnl_percent = (current_price - entry_price) / entry_price
    else:
        pnl_percent = (entry_price - current_price) / entry_price
    
    # Atualizar pico de lucro
    if pnl_percent > position_tracker['peak_profit_percent']:
        position_tracker['peak_profit_percent'] = pnl_percent
        if pnl_percent >= MIN_PROFIT_FOR_TRAILING:
            if int(pnl_percent * 200) > int((pnl_percent - 0.005) * 200):
                log(f"📈 Peak profit: {pnl_percent*100:.2f}% (trailing active)")
    
    # Verificar se deve ativar trailing
    peak = position_tracker['peak_profit_percent']
    
    if peak < MIN_PROFIT_FOR_TRAILING:
        return
    
    # Calcular queda do pico (SEM alavancagem)
    drop = peak - pnl_percent
    drop_percent_of_peak = (drop / peak) if peak > 0 else 0
    
    # Acionar trailing se caiu 25% do pico
    if drop_percent_of_peak >= TRAILING_PROFIT_DROP:
        pnl_with_leverage = pnl_percent * LEVERAGE * 100
        log(f"💰 TRAILING PROFIT TRIGGERED!")
        log(f"Peak: {peak*100:.2f}% | Current: {pnl_percent*100:.2f}% | Drop: {drop_percent_of_peak*100:.1f}%")
        log(f"Locking profit at {pnl_with_leverage:.2f}% (with {LEVERAGE}x leverage)")
        
        if close_position_market(TARGET_SYMBOL, side):
            log(f"✅ Profit locked | Net gain: {pnl_with_leverage:.2f}%")
            position_tracker['temporarily_closed'] = True
            position_tracker['reentry_price'] = current_price
            position_tracker['reentry_attempts'] = 0
            position_tracker['last_trailing_action'] = current_time
            # Não limpa side/size - aguarda reentrada ou sinal do TV

def check_reentry():
    """
    🔄 REENTRADA
    ⚠️ SÓ FUNCIONA SE TRADINGVIEW AINDA ESTIVER ATIVO
    """
    # 🔥 CRÍTICO: Não reentra se TV fechou a posição
    if not position_tracker.get('tradingview_active', False):
        return
    
    if not position_tracker['temporarily_closed']:
        return
    
    # Limitar tentativas
    if position_tracker['reentry_attempts'] >= 3:
        return
    
    current_time = time.time()
    
    # Cooldown entre tentativas
    if current_time - position_tracker['last_trailing_action'] < 3:
        return
    
    # Buscar preço atual FRESCO
    cache['time'] = 0
    balance, current_price, _ = get_cached_data()
    
    if current_price <= 0:
        return
    
    entry_price = position_tracker['entry_price']  # Entrada ORIGINAL
    reentry_price = position_tracker['reentry_price']  # Preço que fechou
    side = position_tracker['side']
    
    # Calcular lucro atual vs entrada ORIGINAL (SEM alavancagem)
    if side == 'long':
        pnl_vs_original = (current_price - entry_price) / entry_price
    else:
        pnl_vs_original = (entry_price - current_price) / entry_price
    
    # Calcular ganho desde que fechou (SEM alavancagem)
    if side == 'long':
        gain_from_close = (current_price - reentry_price) / reentry_price
    else:
        gain_from_close = (reentry_price - current_price) / reentry_price
    
    # 🔥 CRÍTICO: Reentrada usa threshold SEM alavancagem (0.3%)
    if gain_from_close >= REENTRY_THRESHOLD:
        position_tracker['reentry_attempts'] += 1
        log(f"🔄 REENTRY TRIGGERED (attempt {position_tracker['reentry_attempts']}/3)")
        log(f"Close: ${reentry_price:.4f} | Current: ${current_price:.4f} | Gain: {gain_from_close*100:.2f}%")
        log(f"Price back to {pnl_vs_original*100:.2f}% profit vs original entry")
        
        # Recalcular quantidade com saldo atualizado
        quantity = calculate_quantity(balance, current_price)
        
        if quantity > 0:
            buy_side = 'buy' if side == 'long' else 'sell'
            if open_position_market(TARGET_SYMBOL, buy_side, quantity, is_reentry=True):
                log(f"✅ Reentered {side.upper()}")
                position_tracker['temporarily_closed'] = False
                position_tracker['peak_profit_percent'] = pnl_vs_original
                position_tracker['last_trailing_action'] = current_time

@app.route('/')
def home():
    return 'Bot WLFI Running'

@app.route('/health')
def health():
    # Verificar proteções SOMENTE se TV estiver ativo
    if position_tracker.get('tradingview_active', False):
        try:
            check_stop_loss()
            check_trailing_profit()
        except:
            pass
    return 'OK', 200

@app.route('/status')
def status():
    try:
        balance, current_price, (long_size, short_size) = get_cached_data()
        
        status_data = {
            'tradingview_active': position_tracker.get('tradingview_active', False),
            'tv_position': position_tracker.get('tv_position', 'flat'),
            'actual_position': 'long' if long_size > 0 else ('short' if short_size > 0 else 'flat'),
            'size': position_tracker.get('size', 0),
            'entry': position_tracker.get('entry_price', 0),
            'current': current_price,
            'stop': position_tracker.get('stop_loss_price', 0),
            'balance': balance
        }
        
        if position_tracker.get('side'):
            entry = position_tracker['entry_price']
            if position_tracker['side'] == 'long':
                pnl = (current_price - entry) / entry * 100
            else:
                pnl = (entry - current_price) / entry * 100
            status_data['pnl'] = f"{pnl:.2f}%"
            status_data['pnl_leveraged'] = f"{pnl * LEVERAGE:.2f}%"
        
        return jsonify(status_data), 200
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/test-credentials')
def test_credentials():
    """Endpoint de teste para verificar credenciais"""
    try:
        # Testar conexão simples
        endpoint = '/api/v2/spot/public/time'
        url = BASE_URL + endpoint
        
        response = requests.get(url, timeout=5)
        server_time = response.json()
        
        # Testar assinatura
        test_result = {
            'server_time': server_time,
            'api_key_length': len(API_KEY) if API_KEY else 0,
            'api_secret_length': len(API_SECRET) if API_SECRET else 0,
            'api_passphrase_length': len(API_PASSPHRASE) if API_PASSPHRASE else 0,
            'api_key_prefix': API_KEY[:8] if API_KEY and len(API_KEY) >= 8 else 'empty',
            'credentials_loaded': bool(API_KEY and API_SECRET and API_PASSPHRASE)
        }
        
        # Testar request autenticado
        balance_result = get_account_balance()
        test_result['balance_test'] = 'success' if balance_result >= 0 else 'failed'
        test_result['balance'] = balance_result
        
        return jsonify(test_result), 200
    except Exception as e:
        return jsonify({'error': str(e), 'traceback': traceback.format_exc()}), 500

@app.route('/webhook', methods=['POST'])
def webhook():
    try:
        data = request.get_json() if request.is_json else {}
        
        # Anti-duplicata
        current_time = time.time()
        data_str = json.dumps(data, sort_keys=True)
        
        if (current_time - last_webhook['time'] < 2 and 
            last_webhook['data'] == data_str):
            log("SKIP: Duplicate webhook (< 2s)")
            return jsonify({'status': 'duplicate'}), 200
        
        last_webhook['time'] = current_time
        last_webhook['data'] = data_str
        
        # Extrair dados
        market_position = data.get('marketPosition', '').lower()
        prev_market_position = data.get('prevMarketPosition', '').lower()
        timeframe = data.get('timeframe', '?')
        
        log(f">> TV:{market_position.upper()} [MP:{market_position}] [{timeframe}min] PREV:{prev_market_position}")
        
        # 🔥 ATUALIZAR STATUS DO TRADINGVIEW
        position_tracker['tv_position'] = market_position
        
        # Buscar dados
        cache['time'] = 0
        balance, current_price, (long_size, short_size) = get_cached_data()
        
        if balance <= 0 or current_price <= 0:
            log("ERR: Invalid data")
            return jsonify({'status': 'error'}), 500
        
        # === LÓGICA PRINCIPAL ===
        
        if market_position == 'long':
            # 🔥 TRADINGVIEW QUER LONG
            position_tracker['tradingview_active'] = True
            
            if long_size > 0:
                log("SKIP: Already LONG")
            else:
                if short_size > 0:
                    log("CLOSE SHORT -> OPEN LONG")
                    close_position_market(TARGET_SYMBOL, 'short')
                    time.sleep(0.5)
                else:
                    log("OPEN LONG")
                
                quantity = calculate_quantity(balance, current_price)
                if quantity > 0:
                    open_position_market(TARGET_SYMBOL, 'buy', quantity)
        
        elif market_position == 'short':
            # 🔥 TRADINGVIEW QUER SHORT
            position_tracker['tradingview_active'] = True
            
            if short_size > 0:
                log("SKIP: Already SHORT")
            else:
                if long_size > 0:
                    log("CLOSE LONG -> OPEN SHORT")
                    close_position_market(TARGET_SYMBOL, 'long')
                    time.sleep(0.5)
                else:
                    log("OPEN SHORT")
                
                quantity = calculate_quantity(balance, current_price)
                if quantity > 0:
                    open_position_market(TARGET_SYMBOL, 'sell', quantity)
        
        elif market_position == 'flat':
            # 🔥 TRADINGVIEW QUER FECHAR TUDO
            log("TV: CLOSE ALL POSITIONS")
            
            if long_size > 0:
                log("CLOSE LONG")
                close_position_market(TARGET_SYMBOL, 'long')
            
            if short_size > 0:
                log("CLOSE SHORT")
                close_position_market(TARGET_SYMBOL, 'short')
            
            # 🔥 DESATIVAR TODAS AS PROTEÇÕES
            position_tracker['tradingview_active'] = False
            position_tracker['side'] = ''
            position_tracker['size'] = 0
            position_tracker['temporarily_closed'] = False
            position_tracker['peak_profit_percent'] = 0
            log("⚠️ TradingView closed position - All protections DISABLED")
        
        return jsonify({'status': 'ok'}), 200
        
    except Exception as e:
        log(f"ERR webhook: {str(e)}")
        log(traceback.format_exc())
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 10000))
    log(f"Bot WLFI starting on port {port}...")
    log(f"Config: {LEVERAGE}x leverage, {int(POSITION_SIZE_PERCENT*100)}% position size")
    log(f"Protections: {STOP_LOSS_PERCENT*100}% stop loss, {TRAILING_PROFIT_DROP*100}% trailing")
    log(f"⚠️ Trailing/Stop ONLY active when TradingView signal is active")
    
    # 🔍 DEBUG: Verificar variáveis de ambiente
    api_key_len = len(API_KEY) if API_KEY else 0
    api_secret_len = len(API_SECRET) if API_SECRET else 0
    api_pass_len = len(API_PASSPHRASE) if API_PASSPHRASE else 0
    
    log(f"🔑 API_KEY length: {api_key_len} chars")
    log(f"🔑 API_SECRET length: {api_secret_len} chars")
    log(f"🔑 API_PASSPHRASE length: {api_pass_len} chars")
    
    if api_key_len == 0 or api_secret_len == 0 or api_pass_len == 0:
        log("❌ ERROR: Missing API credentials! Check environment variables.")
    else:
        log("✅ API credentials loaded")
    
    # Iniciar servidor
    executor = ThreadPoolExecutor(max_workers=2)
    app.run(host='0.0.0.0', port=port, threaded=True)
