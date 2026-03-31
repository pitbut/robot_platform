import os
import json
import threading
from flask import Flask, render_template, request, jsonify
from flask_sock import Sock
from anthropic import Anthropic
from dotenv import load_dotenv

load_dotenv()

app = Flask(__name__)
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)
ai = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# ── Shared state ───────────────────────────────────────────
_lock = threading.Lock()
esp_clients  = {}   # esp_id -> {'ws', 'caps', 'sensors'}
browser_clients = []

def broadcast(data: dict):
    """Send data to all connected browsers."""
    msg = json.dumps(data)
    dead = []
    with _lock:
        clients = list(browser_clients)
    for ws in clients:
        try:
            ws.send(msg)
        except Exception:
            dead.append(ws)
    with _lock:
        for d in dead:
            if d in browser_clients:
                browser_clients.remove(d)

# ── HTTP routes ────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/companion')
def companion():
    return render_template('companion.html')

@app.route('/delivery')
def delivery():
    return render_template('delivery.html')

@app.route('/api/esp_list')
def esp_list():
    with _lock:
        return jsonify({
            k: {'caps': v['caps'], 'sensors': v['sensors']}
            for k, v in esp_clients.items()
        })

# ── AI proxy (API key NEVER goes to browser) ──────────────
@app.route('/api/ai', methods=['POST'])
def ai_proxy():
    try:
        body = request.json
        resp = ai.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=600,
            system=body.get(
                'system',
                'Ты дружелюбный робот-помощник. Отвечай коротко и по делу на русском языке.'
            ),
            messages=body.get('messages', [])
        )
        return jsonify({'ok': True, 'reply': resp.content[0].text})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── WebSocket: ESP32 side ──────────────────────────────────
@sock.route('/ws/esp')
def ws_esp(ws):
    esp_id = None
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                data = json.loads(raw)
            except Exception:
                continue

            t = data.get('type')

            if t == 'register':
                esp_id = data.get('id', f'esp_{id(ws)}')
                with _lock:
                    esp_clients[esp_id] = {
                        'ws': ws,
                        'caps': data.get('caps', []),
                        'sensors': {}
                    }
                broadcast({
                    'type': 'esp_online',
                    'id': esp_id,
                    'caps': data.get('caps', [])
                })
                print(f'[ESP] {esp_id} connected: {data.get("caps")}')

            elif t == 'sensors' and esp_id:
                sensors = data.get('data', {})
                with _lock:
                    if esp_id in esp_clients:
                        esp_clients[esp_id]['sensors'] = sensors
                broadcast({'type': 'sensors', 'id': esp_id, 'data': sensors})

            elif t == 'gps' and esp_id:
                broadcast({
                    'type': 'gps',
                    'id':    esp_id,
                    'lat':   data.get('lat'),
                    'lng':   data.get('lng'),
                    'speed': data.get('speed', 0)
                })

    finally:
        if esp_id:
            with _lock:
                esp_clients.pop(esp_id, None)
            broadcast({'type': 'esp_offline', 'id': esp_id})
            print(f'[ESP] {esp_id} disconnected')

# ── WebSocket: Browser side ────────────────────────────────
@sock.route('/ws/browser')
def ws_browser(ws):
    with _lock:
        browser_clients.append(ws)

    # Send current ESP snapshot to new browser
    with _lock:
        snapshot = {
            k: {'caps': v['caps'], 'sensors': v['sensors']}
            for k, v in esp_clients.items()
        }
    for esp_id, info in snapshot.items():
        try:
            ws.send(json.dumps({
                'type': 'esp_online',
                'id': esp_id,
                'caps': info['caps']
            }))
        except Exception:
            pass

    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            try:
                data = json.loads(raw)
            except Exception:
                continue

            # Forward command to target ESP
            target = data.get('target')
            if target:
                with _lock:
                    info = esp_clients.get(target)
                if info:
                    try:
                        info['ws'].send(json.dumps(data))
                    except Exception:
                        pass

    finally:
        with _lock:
            if ws in browser_clients:
                browser_clients.remove(ws)

# ──────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f'Starting on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
