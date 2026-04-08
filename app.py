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

# ══════════════════════════════════════════════════════════
# UNIVERSAL PROFILE — добавлено поверх существующего кода
# Существующие маршруты и WebSocket не тронуты
# ══════════════════════════════════════════════════════════
import io, base64

try:
    from gtts import gTTS
    TTS_OK = True
except ImportError:
    TTS_OK = False

from flask import send_file

# Состояние универсального профиля
_u_lock        = threading.Lock()
robot_phone_ws = None        # WebSocket телефона на роботе
operator_wss   = []          # WebSockets операторов (universal)
cam_subscribers= []          # Операторы подписанные на камеру

def u_broadcast(data: dict, exclude=None):
    """Отправить всем операторам universal-профиля."""
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

# ── Маршруты universal ─────────────────────────────────────
@app.route('/universal')
def universal():
    return render_template('universal.html')

@app.route('/robot-phone')
def robot_phone():
    return render_template('robot_phone.html')

# ── AI Vision (анализ кадра) ───────────────────────────────
@app.route('/api/vision', methods=['POST'])
def vision():
    try:
        body  = request.json
        b64   = body.get('image', '')
        hist  = body.get('history', [])
        if not b64:
            return jsonify({'ok': False, 'error': 'no image'}), 400
        msgs = list(hist[-4:]) if hist else []
        msgs.append({
            'role': 'user',
            'content': [
                {'type': 'image', 'source': {'type': 'base64', 'media_type': 'image/jpeg', 'data': b64}},
                {'type': 'text',  'text':  (
                    'Ты дерзкий шутливый робот по имени Пит. '
                    'Посмотри на этот кадр с камеры на борту. '
                    'Опиши что видишь одним-двумя предложениями с юмором. '
                    'Можешь пошутить над прохожими или ситуацией. '
                    'Отвечай по-русски от первого лица.'
                )}
            ]
        })
        resp = ai.messages.create(
            model='claude-sonnet-4-20250514', max_tokens=150,
            system='Ты весёлый дерзкий робот Пит. Комментируй кратко и с юмором.',
            messages=msgs
        )
        return jsonify({'ok': True, 'reply': resp.content[0].text})
    except Exception as e:
        return jsonify({'ok': False, 'error': str(e)}), 500

# ── TTS (текст → MP3 → BT-колонка через Audio()) ──────────
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

# ── WebSocket: Телефон на роботе ───────────────────────────
@sock.route('/ws/robot-phone')
def ws_robot_phone(ws):
    global robot_phone_ws
    with _u_lock:
        robot_phone_ws = ws
    u_broadcast({'type': 'robot_online'})
    print('[ROBOT-PHONE] Подключён')
    try:
        while True:
            raw = ws.receive()
            if raw is None:
                break
            # Разбираем сообщение
            try:
                data = json.loads(raw)
            except Exception:
                continue
            t = data.get('type')

            if t == 'frame':
                # JPEG кадр → всем подписанным операторам
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
                u_broadcast({'type': 'robot_gps', 'lat': data.get('lat'), 'lng': data.get('lng'), 'acc': data.get('acc', 0)})

            elif t == 'wake_word':
                u_broadcast({'type': 'wake_word', 'phrase': data.get('phrase', '')})

            elif t == 'voice_command':
                u_broadcast({'type': 'voice_command', 'cmd': data.get('cmd', '')})

            elif t == 'audio_chunk':
                # Аудио с микрофона робота → операторам
                u_broadcast({'type': 'audio_chunk', 'data': data.get('data', '')})

    finally:
        with _u_lock:
            if robot_phone_ws is ws:
                robot_phone_ws = None
        u_broadcast({'type': 'robot_offline'})
        print('[ROBOT-PHONE] Отключён')

# ── WebSocket: Оператор (universal) ───────────────────────
@sock.route('/ws/universal')
def ws_universal(ws):
    global robot_phone_ws
    with _u_lock:
        operator_wss.append(ws)
        # Сообщить текущий статус
        is_online = robot_phone_ws is not None
    try:
        ws.send(json.dumps({'type': 'robot_online' if is_online else 'robot_offline'}))
        # Также текущие ESP
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
                # Голос оператора → на телефон робота (воспроизвести там)
                with _u_lock:
                    rp = robot_phone_ws
                if rp:
                    try:
                        rp.send(json.dumps({'type': 'play_operator_voice', 'data': data.get('data', '')}))
                    except Exception:
                        pass

            elif t == 'tts_to_robot':
                # TTS текст → телефон робота воспроизводит
                with _u_lock:
                    rp = robot_phone_ws
                if rp:
                    try:
                        rp.send(json.dumps({'type': 'speak', 'text': data.get('text', '')}))
                    except Exception:
                        pass

            elif t == 'esp_cmd':
                # Команда ESP через universal → пересылаем через обычный механизм
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
        with _u_lock:
            if ws in operator_wss:
                operator_wss.remove(ws)
            if ws in cam_subscribers:
                cam_subscribers.remove(ws)
