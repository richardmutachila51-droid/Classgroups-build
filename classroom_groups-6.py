import kivy
kivy.require('2.0.0')

from kivy.app import App
from kivy.uix.screenmanager import ScreenManager, Screen
from kivy.uix.floatlayout import FloatLayout
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.label import Label
from kivy.uix.textinput import TextInput
from kivy.uix.button import Button
from kivy.uix.widget import Widget
from kivy.graphics import Color, Ellipse, Line
from kivy.core.window import Window
from kivy.clock import Clock
from kivy.utils import get_color_from_hex
from kivy.animation import Animation
from kivy.core.audio import SoundLoader
from kivy.uix.scrollview import ScrollView
from kivy.uix.gridlayout import GridLayout

import socket
import threading
import json
import random
import time
import os
import hashlib
import base64
import urllib.request
import urllib.error

# ─────────────────────────────────────────────
# OWNER LOCK SYSTEM
# ─────────────────────────────────────────────
_sx = base64.b64decode('cG9seW1vcnBoaWM=').decode()
_hx_store = ['ZTViMDNjMTJiZDhlOGJkMzEwMTkzMmZmOTQ4NjNiYWIxYTdjZTRkMTNiYjdlMDI4ZTgzZTc0NjhkMDJmZjlhNw==']

def _vx(raw):
    attempt = hashlib.sha256((_sx + raw + _sx[::-1]).encode()).hexdigest()
    return base64.b64encode(attempt.encode()).decode() == _hx_store[0]

def _update_secret(new_raw):
    new_hash = hashlib.sha256((_sx + new_raw + _sx[::-1]).encode()).hexdigest()
    _hx_store[0] = base64.b64encode(new_hash.encode()).decode()

FIREBASE_URL = 'https://classgroups-lock-default-rtdb.firebaseio.com'

def firebase_get(path):
    try:
        url = f'{FIREBASE_URL}/{path}.json'
        req = urllib.request.urlopen(url, timeout=5)
        data = json.loads(req.read().decode())
        return data
    except Exception:
        return None

def firebase_set(path, value):
    try:
        url = f'{FIREBASE_URL}/{path}.json'
        data = json.dumps(value).encode()
        req = urllib.request.Request(url, data=data, method='PUT')
        req.add_header('Content-Type', 'application/json')
        urllib.request.urlopen(req, timeout=5)
        return True
    except Exception:
        return False

owner_state = {"is_locked": False, "polling": False}

def start_lock_polling(on_lock_change):
    def _poll():
        last_state = None
        while owner_state["polling"]:
            try:
                state = firebase_get('applock/locked')
                if state is not None and state != last_state:
                    last_state = state
                    Clock.schedule_once(lambda dt, s=state: on_lock_change(s))
            except Exception:
                pass
            time.sleep(3)
    owner_state["polling"] = True
    threading.Thread(target=_poll, daemon=True).start()

def stop_lock_polling():
    owner_state["polling"] = False

# ─────────────────────────────────────────────
# NETWORK CONSTANTS
# ─────────────────────────────────────────────
PORT = 45678
BROADCAST_PORT = 45679
MAX_PLAYERS = 100

# ─────────────────────────────────────────────
# SHARED STATE
# ─────────────────────────────────────────────
app_state = {
    "role": None,           # "host" or "player"
    "session_code": "",
    "my_name": "",
    "my_number": None,
    "my_group": None,
    "num_groups": 0,
    "players": {},          # {number: {"name": ..., "picked": bool}}
    "groups": {},           # {group_id: [numbers]}
    "phase": "lobby",       # lobby | picking | results
    "host_ip": "",
    "client_socket": None,
    "server_clients": {},   # {conn: player_number}
    "picked_count": 0,
}

Window.clearcolor = (0, 0, 0, 1)

# ─────────────────────────────────────────────
# VIBRATION HELPER
# ─────────────────────────────────────────────
def vibrate(duration=0.1):
    try:
        from android.permissions import request_permissions, Permission
        from jnius import autoclass
        Context = autoclass('android.content.Context')
        PythonActivity = autoclass('org.kivy.android.PythonActivity')
        vibrator = PythonActivity.mActivity.getSystemService(Context.VIBRATOR_SERVICE)
        vibrator.vibrate(int(duration * 1000))
    except Exception:
        pass  # Not on Android or no permission

# ─────────────────────────────────────────────
# SERVER LOGIC
# ─────────────────────────────────────────────
class GameServer:
    def __init__(self):
        self.server_socket = None
        self.running = False
        self.lock = threading.Lock()
        self.next_number = 1

    def start(self, session_code):
        self.running = True
        self.server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.server_socket.bind(('', PORT))
        self.server_socket.listen(MAX_PLAYERS)
        threading.Thread(target=self._accept_loop, daemon=True).start()
        threading.Thread(target=self._broadcast_ip, args=(session_code,), daemon=True).start()

    def _broadcast_ip(self, session_code):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        host_ip = self._get_local_ip()
        app_state["host_ip"] = host_ip
        msg = json.dumps({"type": "announce", "code": session_code, "ip": host_ip}).encode()
        while self.running:
            try:
                sock.sendto(msg, ('<broadcast>', BROADCAST_PORT))
            except Exception:
                pass
            time.sleep(1)

    def _get_local_ip(self):
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(("8.8.8.8", 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return "127.0.0.1"

    def _accept_loop(self):
        while self.running:
            try:
                conn, addr = self.server_socket.accept()
                threading.Thread(target=self._handle_client, args=(conn,), daemon=True).start()
            except Exception:
                break

    def _handle_client(self, conn):
        with self.lock:
            number = self.next_number
            self.next_number += 1
            app_state["server_clients"][conn] = number
            app_state["players"][number] = {"name": f"Player{number}", "picked": False}

        # Send assigned number to this client
        self._send(conn, {"type": "assigned", "number": number})
        # Broadcast updated player list
        self._broadcast_players()

        buffer = ""
        while True:
            try:
                data = conn.recv(4096).decode()
                if not data:
                    break
                buffer += data
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        msg = json.loads(line.strip())
                        self._process(conn, msg)
            except Exception:
                break

        with self.lock:
            number = app_state["server_clients"].pop(conn, None)
            if number:
                app_state["players"].pop(number, None)
        self._broadcast_players()
        conn.close()

    def _process(self, conn, msg):
        mtype = msg.get("type")
        if mtype == "pick":
            number = msg.get("number")
            with self.lock:
                if number in app_state["players"] and not app_state["players"][number]["picked"]:
                    app_state["players"][number]["picked"] = True
                    app_state["picked_count"] = sum(1 for p in app_state["players"].values() if p["picked"])
            self._broadcast({"type": "picked", "number": number})
            # Check if all picked
            with self.lock:
                total = len(app_state["players"])
                picked = app_state["picked_count"]
            if total > 0 and picked >= total:
                self._do_grouping()

    def _do_grouping(self):
        with self.lock:
            numbers = list(app_state["players"].keys())
            random.shuffle(numbers)
            num_groups = app_state["num_groups"]
            groups = {i + 1: [] for i in range(num_groups)}
            for i, n in enumerate(numbers):
                groups[(i % num_groups) + 1].append(n)
            app_state["groups"] = groups
            # assign group to each player
            for gid, members in groups.items():
                for n in members:
                    if n in app_state["players"]:
                        app_state["players"][n]["group"] = gid
        self._broadcast({"type": "results", "groups": {str(k): v for k, v in app_state["groups"].items()}})

    def _broadcast_players(self):
        with self.lock:
            data = {"type": "players", "players": {str(k): v for k, v in app_state["players"].items()}}
        self._broadcast(data)

    def _broadcast(self, msg):
        dead = []
        with self.lock:
            clients = list(app_state["server_clients"].keys())
        for conn in clients:
            try:
                self._send(conn, msg)
            except Exception:
                dead.append(conn)

    def _send(self, conn, msg):
        conn.sendall((json.dumps(msg) + '\n').encode())

    def stop(self):
        self.running = False
        if self.server_socket:
            self.server_socket.close()

game_server = GameServer()

# ─────────────────────────────────────────────
# CLIENT LOGIC
# ─────────────────────────────────────────────
class GameClient:
    def __init__(self):
        self.sock = None
        self.running = False
        self.on_message = None  # callback

    def find_host(self, session_code, timeout=10):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind(('', BROADCAST_PORT))
        sock.settimeout(timeout)
        start = time.time()
        while time.time() - start < timeout:
            try:
                data, _ = sock.recvfrom(1024)
                msg = json.loads(data.decode())
                if msg.get("type") == "announce" and msg.get("code") == session_code:
                    sock.close()
                    return msg.get("ip")
            except socket.timeout:
                break
            except Exception:
                pass
        sock.close()
        return None

    def connect(self, ip):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.sock.connect((ip, PORT))
        app_state["client_socket"] = self.sock
        self.running = True
        threading.Thread(target=self._recv_loop, daemon=True).start()

    def _recv_loop(self):
        buffer = ""
        while self.running:
            try:
                data = self.sock.recv(4096).decode()
                if not data:
                    break
                buffer += data
                while '\n' in buffer:
                    line, buffer = buffer.split('\n', 1)
                    if line.strip():
                        msg = json.loads(line.strip())
                        if self.on_message:
                            Clock.schedule_once(lambda dt, m=msg: self.on_message(m))
            except Exception:
                break

    def send(self, msg):
        if self.sock:
            self.sock.sendall((json.dumps(msg) + '\n').encode())

    def stop(self):
        self.running = False
        if self.sock:
            self.sock.close()

game_client = GameClient()

# ─────────────────────────────────────────────
# SCREENS
# ─────────────────────────────────────────────

class StartScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        # Outer layout: owner button on left + main content on right
        outer = BoxLayout(orientation='horizontal')

        # Owner button on the left side
        owner_btn = Button(
            text='Owner',
            font_size='13sp',
            background_color=(0.18, 0.18, 0.18, 1),
            color=(0.7, 0.7, 0.7, 1),
            size_hint=(None, 1),
            width=70
        )
        owner_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'owner'))
        outer.add_widget(owner_btn)

        # Main content
        layout = BoxLayout(orientation='vertical', padding=40, spacing=20)

        title = Label(
            text='🎮 Class Groups',
            font_size='36sp',
            color=(1, 1, 1, 1),
            size_hint=(1, 0.3)
        )

        self.code_input = TextInput(
            hint_text='Enter session code',
            multiline=False,
            font_size='24sp',
            background_color=(0.15, 0.15, 0.15, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.5, 0.5, 0.5, 1),
            size_hint=(1, 0.15),
            halign='center'
        )

        host_btn = Button(
            text='HOST SESSION',
            font_size='20sp',
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1),
            size_hint=(1, 0.15)
        )
        host_btn.bind(on_press=self.host_session)

        join_btn = Button(
            text='JOIN SESSION',
            font_size='20sp',
            background_color=(0.1, 0.8, 0.4, 1),
            color=(1, 1, 1, 1),
            size_hint=(1, 0.15)
        )
        join_btn.bind(on_press=self.join_session)

        self.status = Label(
            text='',
            font_size='16sp',
            color=(1, 0.4, 0.4, 1),
            size_hint=(1, 0.1)
        )

        layout.add_widget(title)
        layout.add_widget(self.code_input)
        layout.add_widget(host_btn)
        layout.add_widget(join_btn)
        layout.add_widget(self.status)
        outer.add_widget(layout)
        self.add_widget(outer)

    def on_enter(self):
        # Start polling Firebase for lock state every time we return to start screen
        start_lock_polling(self._on_lock_change)

    def _on_lock_change(self, is_locked):
        owner_state['is_locked'] = is_locked
        if is_locked:
            self.manager.current = 'lockscreen'

    def host_session(self, *args):
        if owner_state.get('is_locked'):
            self.status.text = 'App is locked by owner.'
            return
        code = self.code_input.text.strip()
        if not code:
            self.status.text = 'Please enter a session code'
            return
        app_state["role"] = "host"
        app_state["session_code"] = code
        self.manager.current = 'host_setup'

    def join_session(self, *args):
        if owner_state.get('is_locked'):
            self.status.text = 'App is locked by owner.'
            return
        code = self.code_input.text.strip()
        if not code:
            self.status.text = 'Please enter a session code'
            return
        app_state["role"] = "player"
        app_state["session_code"] = code
        self.status.text = 'Searching for host...'
        threading.Thread(target=self._find_and_join, args=(code,), daemon=True).start()

    def _find_and_join(self, code):
        ip = game_client.find_host(code, timeout=15)
        if ip:
            try:
                game_client.connect(ip)
                Clock.schedule_once(lambda dt: self._go_to_lobby())
            except Exception as e:
                Clock.schedule_once(lambda dt: setattr(self.status, 'text', f'Connection failed: {e}'))
        else:
            Clock.schedule_once(lambda dt: setattr(self.status, 'text', 'Host not found. Check code.'))

    def _go_to_lobby(self):
        self.manager.current = 'lobby'


class HostSetupScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=30, spacing=15)
        self.add_widget(self.layout)

    def on_enter(self):
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='Session Code: ' + app_state.get("session_code", ""),
            font_size='22sp',
            color=(0.4, 0.8, 1, 1),
            size_hint=(1, None),
            height=50
        ))
        self.layout.add_widget(Label(
            text='⬇️  ONE STEP TO START  ⬇️',
            font_size='20sp',
            color=(1, 0.8, 0.2, 1),
            size_hint=(1, None),
            height=45
        ))
        self.layout.add_widget(Label(
            text='Hotspot Settings will open now.\nFind "Mobile Hotspot" and slide\nthe toggle to ON, then come back.',
            font_size='17sp',
            color=(0.9, 0.9, 0.9, 1),
            size_hint=(1, None),
            height=100,
            halign='center',
            valign='middle',
            text_size=(Window.width - 40, None)
        ))
        self.layout.add_widget(Label(
            text='[ OFF ]  ──────►  [ ON ]',
            font_size='22sp',
            color=(0.4, 1, 0.4, 1),
            size_hint=(1, None),
            height=50
        ))
        open_btn = Button(
            text='📶 OPEN HOTSPOT SETTINGS',
            font_size='18sp',
            background_color=(0.9, 0.5, 0.1, 1),
            color=(1, 1, 1, 1),
            size_hint=(1, None),
            height=60
        )
        open_btn.bind(on_press=self._open_hotspot)
        self.layout.add_widget(open_btn)
        self.layout.add_widget(Label(
            text='How many groups?',
            font_size='18sp',
            color=(1, 1, 1, 1),
            size_hint=(1, None),
            height=40
        ))
        self.groups_input = TextInput(
            hint_text='e.g. 4',
            multiline=False,
            font_size='30sp',
            background_color=(0.12, 0.12, 0.12, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.4, 0.4, 0.4, 1),
            size_hint=(1, None),
            height=65,
            input_filter='int',
            halign='center'
        )
        self.layout.add_widget(self.groups_input)
        done_btn = Button(
            text='✅  HOTSPOT IS ON — START SESSION',
            font_size='17sp',
            background_color=(0.15, 0.6, 1, 1),
            color=(1, 1, 1, 1),
            size_hint=(1, None),
            height=65
        )
        done_btn.bind(on_press=self.start_hosting)
        self.layout.add_widget(done_btn)
        self.status = Label(
            text='',
            font_size='13sp',
            color=(1, 0.3, 0.3, 1),
            size_hint=(1, None),
            height=35
        )
        self.layout.add_widget(self.status)
        # Auto-open hotspot settings when screen loads
        Clock.schedule_once(lambda dt: self._open_hotspot(), 0.6)

    def _open_hotspot(self, *args):
        try:
            from jnius import autoclass
            Intent = autoclass('android.content.Intent')
            PythonActivity = autoclass('org.kivy.android.PythonActivity')
            try:
                intent = Intent('android.settings.TETHER_SETTINGS')
                PythonActivity.mActivity.startActivity(intent)
            except Exception:
                intent = Intent('android.settings.WIRELESS_SETTINGS')
                PythonActivity.mActivity.startActivity(intent)
        except Exception:
            self.status.text = 'Please open Settings > Mobile Hotspot manually'

    def start_hosting(self, *args):
        try:
            n = int(self.groups_input.text.strip())
            if n < 2:
                self.status.text = 'Minimum 2 groups'
                return
            app_state["num_groups"] = n
        except ValueError:
            self.status.text = 'Enter number of groups first'
            return

        self.status.text = 'Starting session...'
        code = app_state["session_code"]
        game_server.start(code)

        def connect_host(dt):
            try:
                game_client.connect("127.0.0.1")
                self.manager.current = 'lobby'
            except Exception as e:
                self.status.text = f'Error: {e}'

        Clock.schedule_once(connect_host, 0.8)


class LobbyScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = FloatLayout()

        self.waiting_label = Label(
            text='Waiting for players...',
            font_size='20sp',
            color=(0.6, 0.6, 0.6, 1),
            size_hint=(1, None),
            height=40,
            pos_hint={'center_x': 0.5, 'top': 1}
        )

        self.circles_widget = CirclesWidget(size_hint=(1, 1))

        self.layout.add_widget(self.circles_widget)
        self.layout.add_widget(self.waiting_label)
        self.add_widget(self.layout)

        game_client.on_message = self.on_server_message

    def on_enter(self):
        game_client.on_message = self.on_server_message

    def on_server_message(self, msg):
        mtype = msg.get("type")
        if mtype == "assigned":
            app_state["my_number"] = msg["number"]
        elif mtype == "players":
            players = {int(k): v for k, v in msg["players"].items()}
            app_state["players"] = players
            self.circles_widget.update_players(players)
            count = len(players)
            self.waiting_label.text = f'Players connected: {count} | Tap a circle to pick!'
        elif mtype == "picked":
            number = msg["number"]
            self.circles_widget.mark_picked(number)
        elif mtype == "results":
            groups = {int(k): v for k, v in msg["groups"].items()}
            app_state["groups"] = groups
            # find my group
            my_num = app_state["my_number"]
            for gid, members in groups.items():
                if my_num in members:
                    app_state["my_group"] = gid
                    break
            self.manager.current = 'results'


class CirclesWidget(Widget):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.circles = {}  # number -> {"pos": (x,y), "radius": r, "picked": bool}
        self.bind(size=self._redraw, pos=self._redraw)

    def update_players(self, players):
        count = len(players)
        if count == 0:
            self.circles = {}
            self.canvas.clear()
            return

        radius = max(15, min(50, int(200 / max(count, 1) ** 0.5)))

        # Add new players with random positions
        existing = set(self.circles.keys())
        for number in players:
            if number not in existing:
                x = random.uniform(radius + 10, self.width - radius - 10) if self.width > 0 else 100
                y = random.uniform(radius + 60, self.height - radius - 10) if self.height > 0 else 100
                self.circles[number] = {
                    "pos": (x, y),
                    "radius": radius,
                    "picked": players[number].get("picked", False)
                }
            else:
                self.circles[number]["radius"] = radius

        # Remove disconnected players
        for number in list(self.circles.keys()):
            if number not in players:
                del self.circles[number]

        self._redraw()

    def mark_picked(self, number):
        if number in self.circles:
            self.circles[number]["picked"] = True
            self._redraw()
            vibrate(0.15)

    def _redraw(self, *args):
        self.canvas.clear()
        with self.canvas:
            for number, data in self.circles.items():
                x, y = data["pos"]
                r = data["radius"]
                picked = data["picked"]
                is_mine = (number == app_state.get("my_number"))

                if picked:
                    Color(1, 1, 1, 1)  # white
                else:
                    Color(0.35, 0.35, 0.35, 1)  # grey

                Ellipse(pos=(x - r, y - r), size=(r * 2, r * 2))

                if is_mine and not picked:
                    Color(0.4, 0.8, 1, 0.8)
                    Line(circle=(x, y, r + 3), width=2)

    def on_touch_down(self, touch):
        my_number = app_state.get("my_number")
        if my_number is None:
            return False

        for number, data in self.circles.items():
            x, y = data["pos"]
            r = data["radius"]
            dist = ((touch.x - x) ** 2 + (touch.y - y) ** 2) ** 0.5
            if dist <= r:
                if data["picked"]:
                    # Already picked — just vibrate
                    vibrate(0.05)
                else:
                    # Pick it
                    game_client.send({"type": "pick", "number": number})
                    vibrate(0.2)
                return True
        return False


class ResultsScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=20, spacing=10)
        self.add_widget(self.layout)

    def on_enter(self):
        self.layout.clear_widgets()

        my_num = app_state.get("my_number")
        my_group = app_state.get("my_group")
        groups = app_state.get("groups", {})

        title = Label(
            text='🎉 Groups!',
            font_size='30sp',
            color=(1, 1, 1, 1),
            size_hint=(1, None),
            height=60
        )
        self.layout.add_widget(title)

        if my_num and my_group:
            my_label = Label(
                text=f'You are #{my_num} → Group {my_group}',
                font_size='22sp',
                color=(0.4, 1, 0.6, 1),
                size_hint=(1, None),
                height=50
            )
            self.layout.add_widget(my_label)

        scroll = ScrollView(size_hint=(1, 1))
        grid = GridLayout(cols=1, spacing=10, size_hint_y=None, padding=10)
        grid.bind(minimum_height=grid.setter('height'))

        colors = [
            (0.2, 0.6, 1, 1),
            (1, 0.4, 0.4, 1),
            (0.4, 1, 0.4, 1),
            (1, 0.8, 0.2, 1),
            (0.8, 0.4, 1, 1),
            (0.4, 0.9, 0.9, 1),
        ]

        for gid in sorted(groups.keys()):
            members = groups[gid]
            color = colors[(gid - 1) % len(colors)]
            members_str = ', '.join([f'#{n}' for n in sorted(members)])
            lbl = Label(
                text=f'Group {gid}: {members_str}',
                font_size='18sp',
                color=color,
                size_hint=(1, None),
                height=50,
                text_size=(Window.width - 40, None),
                halign='left',
                valign='middle'
            )
            grid.add_widget(lbl)

        scroll.add_widget(grid)
        self.layout.add_widget(scroll)

        restart_btn = Button(
            text='NEW SESSION',
            font_size='18sp',
            background_color=(0.3, 0.3, 0.3, 1),
            color=(1, 1, 1, 1),
            size_hint=(1, None),
            height=55
        )
        restart_btn.bind(on_press=self.restart)
        self.layout.add_widget(restart_btn)

    def restart(self, *args):
        game_client.stop()
        game_server.stop()

        # Reset state
        app_state.update({
            "role": None,
            "session_code": "",
            "my_name": "",
            "my_number": None,
            "my_group": None,
            "num_groups": 0,
            "players": {},
            "groups": {},
            "phase": "lobby",
            "host_ip": "",
            "client_socket": None,
            "server_clients": {},
            "picked_count": 0,
        })

        self.manager.current = 'start'


# ─────────────────────────────────────────────
# OWNER SCREEN
# ─────────────────────────────────────────────
class OwnerScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=30, spacing=15)
        self.add_widget(self.layout)

    def on_enter(self):
        self.layout.clear_widgets()
        self.mode = 'verify'  # verify | locked_options | change_old | change_new1 | change_new2
        self._new_code_temp = ''
        self._build_verify()

    def _build_verify(self):
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='🔐 OWNER ACCESS',
            font_size='26sp', color=(1, 0.8, 0.2, 1),
            size_hint=(1, None), height=55
        ))
        self.layout.add_widget(Label(
            text='Enter secret code:',
            font_size='18sp', color=(0.8, 0.8, 0.8, 1),
            size_hint=(1, None), height=40
        ))
        self.code_input = TextInput(
            hint_text='Secret code',
            multiline=False, password=True,
            font_size='26sp',
            background_color=(0.12, 0.12, 0.12, 1),
            foreground_color=(1, 1, 1, 1),
            hint_text_color=(0.4, 0.4, 0.4, 1),
            size_hint=(1, None), height=60, halign='center'
        )
        self.layout.add_widget(self.code_input)
        confirm_btn = Button(
            text='CONFIRM', font_size='18sp',
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        confirm_btn.bind(on_press=self._verify_code)
        self.layout.add_widget(confirm_btn)
        back_btn = Button(
            text='← BACK', font_size='16sp',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=45
        )
        back_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'start'))
        self.layout.add_widget(back_btn)
        self.status = Label(text='', font_size='14sp', color=(1, 0.3, 0.3, 1),
                            size_hint=(1, None), height=35)
        self.layout.add_widget(self.status)

    def _verify_code(self, *args):
        entered = self.code_input.text.strip()
        if not entered:
            self.status.text = 'Please enter the secret code'
            return
        if _vx(entered):
            self._build_owner_options()
        else:
            self.status.text = 'Wrong code. Try again.'
            self.code_input.text = ''

    def _build_owner_options(self):
        self.layout.clear_widgets()
        is_locked = owner_state.get('is_locked', False)
        lock_label = '🔴 APP IS CURRENTLY LOCKED' if is_locked else '🟢 APP IS CURRENTLY UNLOCKED'
        lock_color = (1, 0.3, 0.3, 1) if is_locked else (0.3, 1, 0.3, 1)
        self.layout.add_widget(Label(
            text=lock_label, font_size='18sp', color=lock_color,
            size_hint=(1, None), height=50
        ))
        toggle_text = '🔓 UNLOCK APP FOR EVERYONE' if is_locked else '🔒 LOCK APP FOR EVERYONE'
        toggle_color = (0.1, 0.7, 0.3, 1) if is_locked else (0.85, 0.15, 0.15, 1)
        toggle_btn = Button(
            text=toggle_text, font_size='17sp',
            background_color=toggle_color,
            color=(1, 1, 1, 1), size_hint=(1, None), height=65
        )
        toggle_btn.bind(on_press=self._toggle_lock)
        self.layout.add_widget(toggle_btn)
        change_btn = Button(
            text='🔑 CHANGE SECRET CODE', font_size='17sp',
            background_color=(0.4, 0.3, 0.7, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        change_btn.bind(on_press=lambda x: self._build_change_code_old())
        self.layout.add_widget(change_btn)
        back_btn = Button(
            text='← BACK', font_size='16sp',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=45
        )
        back_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'start'))
        self.layout.add_widget(back_btn)
        self.status = Label(text='', font_size='14sp', color=(1, 0.8, 0.2, 1),
                            size_hint=(1, None), height=35)
        self.layout.add_widget(self.status)

    def _toggle_lock(self, *args):
        new_state = not owner_state.get('is_locked', False)
        self.status.text = 'Updating...'
        def _do(dt):
            ok = firebase_set('applock/locked', new_state)
            if ok:
                owner_state['is_locked'] = new_state
                self._build_owner_options()
            else:
                self.status.text = 'Failed. Check internet connection.'
        Clock.schedule_once(_do, 0.1)

    def _build_change_code_old(self):
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='Enter OLD secret code:', font_size='18sp',
            color=(0.8, 0.8, 0.8, 1), size_hint=(1, None), height=45
        ))
        self.old_input = TextInput(
            hint_text='Old code', multiline=False, password=True,
            font_size='26sp', background_color=(0.12, 0.12, 0.12, 1),
            foreground_color=(1, 1, 1, 1), hint_text_color=(0.4, 0.4, 0.4, 1),
            size_hint=(1, None), height=60, halign='center'
        )
        self.layout.add_widget(self.old_input)
        next_btn = Button(
            text='NEXT', font_size='18sp',
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        next_btn.bind(on_press=self._check_old_code)
        self.layout.add_widget(next_btn)
        back_btn = Button(
            text='← BACK', font_size='16sp',
            background_color=(0.25, 0.25, 0.25, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=45
        )
        back_btn.bind(on_press=lambda x: self._build_owner_options())
        self.layout.add_widget(back_btn)
        self.status = Label(text='', font_size='14sp', color=(1, 0.3, 0.3, 1),
                            size_hint=(1, None), height=35)
        self.layout.add_widget(self.status)

    def _check_old_code(self, *args):
        if _vx(self.old_input.text.strip()):
            self._build_change_code_new1()
        else:
            self.status.text = 'Wrong old code.'
            self.old_input.text = ''

    def _build_change_code_new1(self):
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='Enter NEW secret code:', font_size='18sp',
            color=(0.8, 0.8, 0.8, 1), size_hint=(1, None), height=45
        ))
        self.new1_input = TextInput(
            hint_text='New code', multiline=False, password=True,
            font_size='26sp', background_color=(0.12, 0.12, 0.12, 1),
            foreground_color=(1, 1, 1, 1), hint_text_color=(0.4, 0.4, 0.4, 1),
            size_hint=(1, None), height=60, halign='center'
        )
        self.layout.add_widget(self.new1_input)
        next_btn = Button(
            text='NEXT', font_size='18sp',
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        next_btn.bind(on_press=self._save_new1)
        self.layout.add_widget(next_btn)
        self.status = Label(text='', font_size='14sp', color=(1, 0.3, 0.3, 1),
                            size_hint=(1, None), height=35)
        self.layout.add_widget(self.status)

    def _save_new1(self, *args):
        val = self.new1_input.text.strip()
        if not val:
            self.status.text = 'Code cannot be empty'
            return
        self._new_code_temp = val
        self._build_change_code_new2()

    def _build_change_code_new2(self):
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='Confirm NEW secret code:', font_size='18sp',
            color=(0.8, 0.8, 0.8, 1), size_hint=(1, None), height=45
        ))
        self.new2_input = TextInput(
            hint_text='Confirm new code', multiline=False, password=True,
            font_size='26sp', background_color=(0.12, 0.12, 0.12, 1),
            foreground_color=(1, 1, 1, 1), hint_text_color=(0.4, 0.4, 0.4, 1),
            size_hint=(1, None), height=60, halign='center'
        )
        self.layout.add_widget(self.new2_input)
        confirm_btn = Button(
            text='CONFIRM CHANGE', font_size='18sp',
            background_color=(0.1, 0.7, 0.3, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        confirm_btn.bind(on_press=self._confirm_new_code)
        self.layout.add_widget(confirm_btn)
        self.status = Label(text='', font_size='14sp', color=(1, 0.3, 0.3, 1),
                            size_hint=(1, None), height=35)
        self.layout.add_widget(self.status)

    def _confirm_new_code(self, *args):
        val = self.new2_input.text.strip()
        if val != self._new_code_temp:
            self.status.text = 'Codes do not match. Try again.'
            self.new2_input.text = ''
            return
        _update_secret(val)
        self._new_code_temp = ''
        self.layout.clear_widgets()
        self.layout.add_widget(Label(
            text='✅ Secret code changed!',
            font_size='22sp', color=(0.3, 1, 0.3, 1),
            size_hint=(1, None), height=60
        ))
        done_btn = Button(
            text='DONE', font_size='18sp',
            background_color=(0.2, 0.6, 1, 1),
            color=(1, 1, 1, 1), size_hint=(1, None), height=55
        )
        done_btn.bind(on_press=lambda x: setattr(self.manager, 'current', 'start'))
        self.layout.add_widget(done_btn)


# ─────────────────────────────────────────────
# LOCK SCREEN
# ─────────────────────────────────────────────
class LockScreen(Screen):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.layout = BoxLayout(orientation='vertical', padding=40, spacing=20)
        self.add_widget(self.layout)
        self._countdown = 5
        self._clock_event = None

    def on_enter(self):
        self.layout.clear_widgets()
        self._countdown = 5
        self.layout.add_widget(Label(
            text='🔒', font_size='72sp', color=(1, 0.2, 0.2, 1),
            size_hint=(1, None), height=120
        ))
        self.layout.add_widget(Label(
            text='APP HAS BEEN LOCKED',
            font_size='28sp', color=(1, 0.2, 0.2, 1),
            size_hint=(1, None), height=60
        ))
        self.layout.add_widget(Label(
            text='The owner has locked this app.',
            font_size='16sp', color=(0.8, 0.8, 0.8, 1),
            size_hint=(1, None), height=40
        ))
        self.countdown_label = Label(
            text=f'Closing in {self._countdown}...',
            font_size='20sp', color=(1, 0.6, 0.2, 1),
            size_hint=(1, None), height=45
        )
        self.layout.add_widget(self.countdown_label)
        if self._clock_event:
            self._clock_event.cancel()
        self._clock_event = Clock.schedule_interval(self._tick, 1)

    def _tick(self, dt):
        self._countdown -= 1
        if self._countdown <= 0:
            self._clock_event.cancel()
            self._close_app()
        else:
            self.countdown_label.text = f'Closing in {self._countdown}...'

    def _close_app(self):
        try:
            App.get_running_app().stop()
        except Exception:
            import sys
            sys.exit(0)

# ─────────────────────────────────────────────
# APP
# ─────────────────────────────────────────────
class ClassroomGroupApp(App):
    def build(self):
        sm = ScreenManager()
        sm.add_widget(StartScreen(name='start'))
        sm.add_widget(HostSetupScreen(name='host_setup'))
        sm.add_widget(LobbyScreen(name='lobby'))
        sm.add_widget(ResultsScreen(name='results'))
        sm.add_widget(OwnerScreen(name='owner'))
        sm.add_widget(LockScreen(name='lockscreen'))
        return sm

if __name__ == '__main__':
    ClassroomGroupApp().run()
