import os
import io
import json
import threading

from flask import Flask, render_template, request, jsonify, send_file
from flask_sock import Sock
from anthropic import Anthropic
from dotenv import load_dotenv

try:
    from gtts import gTTS
    TTS_OK = True
except ImportError:
    TTS_OK = False

load_dotenv()

app = Flask(__name__)
app.config['SOCK_SERVER_OPTIONS'] = {'ping_interval': 25}
sock = Sock(app)
ai = Anthropic(api_key=os.getenv('ANTHROPIC_API_KEY'))

# ── Shared state ───────────────────────────────────────────
_lock           = threading.Lock()
esp_clients     = {}
browser_clients = []

# ── Universal profile state ────────────────────────────────
_u_lock         = threading.Lock()
robot_phone_ws  = None
operator_wss    = []
cam_subscribers = []

# ══════════════════════════════════════════════════════════
# HELPERS
# ══════════════════════════════════════════════════════════
def broadcast(data: dict):
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

def u_broadcast(data: dict, exclude=None):
    msg = json.dumps(data)
    dead = []
    with _u_lock:
        clients = list(operator_wss)
    for ws in clients:
        if ws is exclude:
            continue
        try:
            ws.send(msg)
        except Exception:
            dead.append(ws)
    with _u_lock:
        for d in dead:
            if d in operator_wss:
                operator_wss.remove(d)

# ══════════════════════════════════════════════════════════
# HTTP ROUTES
# ══════════════════════════════════════════════════════════
@app.route('/')
def index():
    return render_template('index.html')

@app.route('/companion')
def companion():
    return render_template('companion.html')

@app.route('/delivery')
def delivery():
    return render_template('delivery.html')

@app.route('/universal')
def universal():
    return render_template('universal.html')

@app.route('/robot-phone')
def robot_phone_page():
    return render_template('robot_phone.html')

@app.route('/api/esp_list')
def esp_list():
    with _lock:
        return jsonify({
            k: {'caps': v['caps'], 'sensors': v['sensors']}
            for k, v in esp_clients.items()
        })

# ── AI proxy ───────────────────────────────────────────────
@app.route('/api/ai', methods=['POST'])
def ai_proxy():
    try:
        body = request.json
        resp = ai.messages.create(
            model='claude-sonnet-4-20250514',
            max_tokens=600,
            system=body.get('system', 'Ты дружелюбный робот-помощник. Отвечай коротко на русском.'),
            messages=body.get('messages', [])
        )
        return jsonify({'ok': True, 'reply': resp.content[0].text})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── AI Vision ──────────────────────────────────────────────
@app.route('/api/vision', methods=['POST'])
def vision():
    try:
        body = request.json
        b64  = body.get('image', '')
        hist = body.get('history', [])
        if not b64:
            return jsonify({'ok': False, 'error': 'no image'}), 400
        msgs = list(hist[-4:]) if hist else []
        msgs.append({
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}},
                {'type': 'text',  'text': (
                    'Ты дерзкий шутливый робот Пит. '
                    'Посмотри на кадр с камеры. '
                    'Опиши что видишь 1-2 предложениями с юмором по-русски.'
                )}
            ]
        })
        resp = ai.messages.create(
            model='claude-sonnet-4-20250514', max_tokens=150,
            system='Ты весёлый дерзкий робот Пит.',
            messages=msgs
        )
        return jsonify({'ok': True, 'reply': resp.content[0].text})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── TTS ────────────────────────────────────────────────────
@app.route('/api/tts', methods=['POST'])
def tts():
    if not TTS_OK:
        return jsonify({'ok': False, 'error': 'gTTS not installed'}), 500
    try:
        text = request.json.get('text', '')
        lang = request.json.get('lang', 'ru')
        if not text:
            return jsonify({'ok': False, 'error': 'no text'}), 400
        buf = io.BytesIO()
        gTTS(text=text, lang=lang, slow=False).write_to_fp(buf)
        buf.seek(0)
        return send_file(buf, mimetype='audio/mpeg')
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ══════════════════════════════════════════════════════════
# WEBSOCKET: ESP32
# ══════════════════════════════════════════════════════════
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
                broadcast({'type': 'esp_online', 'id': esp_id, 'caps': data.get('caps', [])})
                u_broadcast({'type': 'esp_online', 'id': esp_id, 'caps': data.get('caps', [])})
                print(f'[ESP] {esp_id} connected: {data.get("caps")}')

            elif t == 'sensors' and esp_id:
                sensors = data.get('data', {})
                with _lock:
                    if esp_id in esp_clients:
                        esp_clients[esp_id]['sensors'] = sensors
                broadcast({'type': 'sensors', 'id': esp_id, 'data': sensors})
                u_broadcast({'type': 'sensors', 'id': esp_id, 'data': sensors})

            elif t == 'gps' and esp_id:
                d = {'type': 'gps', 'id': esp_id,
                     'lat': data.get('lat'), 'lng': data.get('lng'),
                     'speed': data.get('speed', 0)}
                broadcast(d)
                u_broadcast(d)

            elif t in ('nav_progress', 'nav_done', 'config_ack') and esp_id:
                data['id'] = esp_id
                broadcast(data)
                u_broadcast(data)

    finally:
        if esp_id:
            with _lock:
                esp_clients.pop(esp_id, None)
            broadcast({'type': 'esp_offline', 'id': esp_id})
            u_broadcast({'type': 'esp_offline', 'id': esp_id})
            print(f'[ESP] {esp_id} disconnected')

# ══════════════════════════════════════════════════════════
# WEBSOCKET: Browser (delivery / companion)
# ══════════════════════════════════════════════════════════
@sock.route('/ws/browser')
def ws_browser(ws):
    with _lock:
        browser_clients.append(ws)
    with _lock:
        snapshot = {k: {'caps': v['caps'], 'sensors': v['sensors']}
                    for k, v in esp_clients.items()}
    for esp_id, info in snapshot.items():
        try:
            ws.send(json.dumps({'type': 'esp_online', 'id': esp_id, 'caps': info['caps']}))
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

# ══════════════════════════════════════════════════════════
# WEBSOCKET: Robot phone
# ══════════════════════════════════════════════════════════
@sock.route('/ws/robot-phone')
def ws_robot_phone(ws):
    global robot_phone_ws
    with _u_lock:
        robot_phone_ws = ws
    u_broadcast({'type': 'robot_online'})
    print('[ROBOT-PHONE] Connected')
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

            if t == 'frame':
                msg = json.dumps({'type': 'frame', 'data': data.get('data', '')})
                with _u_lock:
                    subs = list(cam_subscribers)
                dead = []
                for cws in subs:
                    try:
                        cws.send(msg)
                    except Exception:
                        dead.append(cws)
                with _u_lock:
                    for d in dead:
                        if d in cam_subscribers:
                            cam_subscribers.remove(d)

            elif t == 'gps':
                lat = data.get('lat')
                lng = data.get('lng')
                acc = data.get('acc', 0)
                # → операторам universal профиля
                u_broadcast({'type': 'robot_gps', 'lat': lat, 'lng': lng, 'acc': acc})
                # → браузерам delivery профиля (тоже нужно видеть робота)
                broadcast({'type': 'robot_gps', 'lat': lat, 'lng': lng, 'acc': acc})
                # → всем ESP (автопилот должен знать координаты)
                gps_for_esp = json.dumps({
                    'type': 'phone_gps', 'lat': lat, 'lng': lng,
                    'accuracy': acc, 'speed': 0
                })
                with _lock:
                    esps = list(esp_clients.values())
                for esp_info in esps:
                    try:
                        esp_info['ws'].send(gps_for_esp)
                    except Exception:
                        pass

            elif t in ('wake_word', 'voice_command', 'audio_chunk'):
                u_broadcast(data)

    finally:
        with _u_lock:
            if robot_phone_ws is ws:
                robot_phone_ws = None
        u_broadcast({'type': 'robot_offline'})
        print('[ROBOT-PHONE] Disconnected')

# ══════════════════════════════════════════════════════════
# WEBSOCKET: Universal operator
# ══════════════════════════════════════════════════════════
@sock.route('/ws/universal')
def ws_universal(ws):
    global robot_phone_ws
    with _u_lock:
        operator_wss.append(ws)
        is_online = robot_phone_ws is not None
    try:
        ws.send(json.dumps({'type': 'robot_online' if is_online else 'robot_offline'}))
        with _lock:
            snap = {k: {'caps': v['caps']} for k, v in esp_clients.items()}
        for eid, info in snap.items():
            ws.send(json.dumps({'type': 'esp_online', 'id': eid, 'caps': info['caps']}))
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
            t = data.get('type')

            if t == 'cam_subscribe':
                with _u_lock:
                    if ws not in cam_subscribers:
                        cam_subscribers.append(ws)

            elif t == 'cam_unsubscribe':
                with _u_lock:
                    if ws in cam_subscribers:
                        cam_subscribers.remove(ws)

            elif t == 'operator_voice':
                with _u_lock:
                    rp = robot_phone_ws
                if rp:
                    try:
                        rp.send(json.dumps({'type': 'play_operator_voice',
                                            'data': data.get('data', '')}))
                    except Exception:
                        pass

            elif t == 'tts_to_robot':
                with _u_lock:
                    rp = robot_phone_ws
                if rp:
                    try:
                        rp.send(json.dumps({'type': 'speak', 'text': data.get('text', '')}))
                    except Exception:
                        pass

            elif t == 'esp_cmd':
                # Браузер шлёт esp_cmd — пересылаем на нужный ESP
                # Тип для ESP определяется полем inner_type если есть,
                # иначе 'cmd' (движение)
                target = data.get('target')
                if target:
                    with _lock:
                        info = esp_clients.get(target)
                    if info:
                        cmd_data = {k: v for k, v in data.items()
                                    if k not in ('target',)}
                        # Если есть inner_type — это навигационная команда
                        if 'inner_type' in cmd_data:
                            cmd_data['type'] = cmd_data.pop('inner_type')
                        else:
                            cmd_data['type'] = 'cmd'
                        try:
                            info['ws'].send(json.dumps(cmd_data))
                        except Exception:
                            pass

    finally:
        with _u_lock:
            if ws in operator_wss:
                operator_wss.remove(ws)
            if ws in cam_subscribers:
                cam_subscribers.remove(ws)

# ══════════════════════════════════════════════════════════
if __name__ == '__main__':
    port = int(os.getenv('PORT', 5000))
    print(f'Starting on port {port}')
    app.run(host='0.0.0.0', port=port, debug=False, threaded=True)
