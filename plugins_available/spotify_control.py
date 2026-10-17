###Plugin prompt in utilization. Status: Functioning even without the Spotify API connection.
"""
Plugin JARVIS: controle completo do Spotify (aplicativo desktop aberto no Windows).

Coloque este arquivo em plugins/ e reinicie o JARVIS.

Dois modos, escolhidos automaticamente:
  1) API (recomendado): tocar música/artista/álbum/playlist por nome, fila, curtir,
     volume exato, shuffle, repetir, pular trecho. Exige `pip install spotipy`, um app
     em developer.spotify.com e rodar UMA vez:  python spotify_control.py --auth
     (controle por API exige conta Premium).
  2) Local (sem configurar nada): teclas de mídia + atalhos do app + título da janela.
     Play/pause, próxima/anterior, volume aproximado, shuffle, repetir, curtir,
     "o que está tocando". NÃO consegue tocar uma música específica sozinho.
"""

import csv
import ctypes
import io
import json
import os
import subprocess
import sys
import time
import urllib.parse
from ctypes import wintypes
from pathlib import Path

PLUGIN = {
    "name": "spotify_control",
    "description": (
        "Controla o aplicativo Spotify do usuário, como se fosse ele usando. Use SEMPRE que o "
        "pedido mencionar Spotify, música, tocar/pausar/pular música, volume da música, "
        "playlist, curtir música ou 'o que está tocando'. Exemplos: 'toca Coldplay no Spotify', "
        "'pausa a música', 'próxima', 'volta a música', 'aumenta o volume', 'coloca o volume em "
        "40', 'curte essa música', 'ativa o aleatório', 'toca minha playlist treino', 'adiciona "
        "Yellow na fila', 'que música é essa?', 'abre o Spotify'. NÃO use open_app nem "
        "controle de teclado genérico para o Spotify; use esta ferramenta. "
        "action: play, pause, toggle, next, previous, volume_up, volume_down, set_volume, mute, "
        "shuffle, repeat, like, unlike, seek_forward, seek_back, queue, now_playing, open, close. "
        "Para tocar algo específico use action=play com query (nome da música, artista, álbum "
        "ou playlist) e media_type adequado."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "play | pause | toggle | next | previous | volume_up | volume_down | "
                    "set_volume | mute | shuffle | repeat | like | unlike | seek_forward | "
                    "seek_back | queue | now_playing | open | close"
                ),
            },
            "query": {
                "type": "STRING",
                "description": "Nome da música/artista/álbum/playlist para tocar ou enfileirar "
                               "(ex.: 'Yellow Coldplay'). Vazio para apenas retomar.",
            },
            "media_type": {
                "type": "STRING",
                "description": "track (padrão), artist, album, playlist ou liked "
                               "(músicas curtidas do usuário).",
            },
            "level": {
                "type": "INTEGER",
                "description": "set_volume: 0-100. volume_up/down: quanto mudar (padrão 10). "
                               "seek_forward/back: segundos (padrão 15).",
            },
            "mode": {
                "type": "STRING",
                "description": "shuffle: on/off. repeat: off, all (playlist) ou one (música). "
                               "Vazio alterna.",
            },
        },
        "required": ["action"],
    },
}

# --------------------------------------------------------------------------- config

DATA_DIR = Path.home() / ".jarvis" / "spotify"
CONFIG_PATH = DATA_DIR / "config.json"
CACHE_PATH = DATA_DIR / "token_cache"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPE = (
    "user-read-playback-state user-modify-playback-state user-read-currently-playing "
    "user-library-read user-library-modify playlist-read-private"
)

ALIASES = {
    "resume": "play", "continue": "play", "search": "play", "search_play": "play",
    "stop": "pause", "skip": "next", "forward": "next", "prev": "previous", "back": "previous",
    "louder": "volume_up", "quieter": "volume_down", "unmute": "mute", "favorite": "like",
    "save": "like", "current": "now_playing", "launch": "open", "start": "open",
    "quit": "close", "exit": "close",
}

_prev_volume = {"value": 50}


def _load_cfg():
    cid = os.environ.get("SPOTIPY_CLIENT_ID")
    secret = os.environ.get("SPOTIPY_CLIENT_SECRET")
    if cid and secret:
        return {"client_id": cid, "client_secret": secret}
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        if data.get("client_id") and data.get("client_secret"):
            return data
    except Exception:
        pass
    return None


def _get_sp(interactive=False):
    """Retorna um cliente spotipy autenticado, ou None se a API não estiver configurada."""
    cfg = _load_cfg()
    if not cfg:
        return None
    try:
        from spotipy import Spotify
        from spotipy.oauth2 import SpotifyOAuth
    except ImportError:
        return None
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    auth = SpotifyOAuth(
        client_id=cfg["client_id"], client_secret=cfg["client_secret"],
        redirect_uri=REDIRECT_URI, scope=SCOPE, cache_path=str(CACHE_PATH),
        open_browser=interactive,
    )
    if not auth.get_cached_token():
        if not interactive:
            return None
        auth.get_access_token(as_dict=False)
    return Spotify(auth_manager=auth)


# --------------------------------------------------------------------------- Windows helpers

_NO_WINDOW = 0x08000000
_EXT_KEYS = {0x25, 0x26, 0x27, 0x28}
VK = {
    "ctrl": 0x11, "shift": 0x10, "alt": 0x12, "left": 0x25, "up": 0x26,
    "right": 0x27, "down": 0x28, "s": 0x53, "r": 0x52, "b": 0x42,
    "play_pause": 0xB3, "next": 0xB0, "prev": 0xB1, "mute": 0xAD,
}


def _key(vk, up=False):
    flags = (0x2 if up else 0) | (0x1 if vk in _EXT_KEYS else 0)
    ctypes.windll.user32.keybd_event(vk, 0, flags, 0)


def _hotkey(*names):
    codes = [VK[n] for n in names]
    for c in codes:
        _key(c)
    for c in reversed(codes):
        _key(c, up=True)
    time.sleep(0.05)


def _spotify_info():
    """(pids, título da janela) dos processos Spotify.exe. pids vazio = app fechado."""
    r = subprocess.run(
        ["tasklist", "/FO", "CSV", "/NH", "/V", "/FI", "IMAGENAME eq Spotify.exe"],
        capture_output=True, text=True, encoding="oem", errors="replace",
        creationflags=_NO_WINDOW,
    )
    pids, title = set(), ""
    for row in csv.reader(io.StringIO(r.stdout)):
        if len(row) < 9 or row[0].lower() != "spotify.exe":
            continue
        try:
            pids.add(int(row[1]))
        except ValueError:
            continue
        t = row[-1].strip()
        if t and t.upper() not in ("N/A", "N/D", "N/E"):
            title = t
    return pids, title


def _focus(pids):
    u = ctypes.windll.user32
    u.IsWindowVisible.argtypes = [wintypes.HWND]
    u.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u.SetForegroundWindow.argtypes = [wintypes.HWND]
    found = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

    def cb(hwnd, _):
        if u.IsWindowVisible(hwnd) and u.GetWindowTextLengthW(hwnd) > 0:
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value in pids:
                found.append(hwnd)
        return True

    u.EnumWindows(proc_type(cb), 0)
    if not found:
        return False
    hwnd = found[0]
    u.ShowWindow(hwnd, 9)  # SW_RESTORE
    _key(VK["alt"]); _key(VK["alt"], up=True)  # permite trazer a janela para frente
    u.SetForegroundWindow(hwnd)
    time.sleep(0.4)
    return True


def _is_playing(title):
    return bool(title) and not title.lower().startswith("spotify")


# --------------------------------------------------------------------------- modo API

def _device_id(sp):
    devs = sp.devices().get("devices", [])
    for d in devs:
        if d.get("is_active"):
            return d["id"]
    for d in devs:
        if d.get("type") == "Computer":
            return d["id"]
    return devs[0]["id"] if devs else None


def _np_text(sp):
    pb = sp.current_playback()
    item = (pb or {}).get("item")
    if not item:
        return "Não há nada tocando agora."
    artists = ", ".join(a["name"] for a in item.get("artists", []))
    estado = "Tocando" if pb.get("is_playing") else "Pausado"
    return f"{estado}: {item['name']}" + (f", de {artists}." if artists else ".")


def _find(sp, query, media_type):
    kind = media_type if media_type in ("track", "artist", "album", "playlist") else "track"
    if kind == "playlist":
        q = query.lower()
        for pl in sp.current_user_playlists(limit=50).get("items") or []:
            if pl and q in pl["name"].lower():
                return kind, pl
    items = [i for i in sp.search(q=query, type=kind, limit=5)[kind + "s"]["items"] if i]
    return kind, (items[0] if items else None)


def _label(kind, item):
    if kind in ("track", "album"):
        artists = ", ".join(a["name"] for a in item.get("artists", []))
        return f"{item['name']}, de {artists}" if artists else item["name"]
    return item["name"]


def _api(sp, action, query, media_type, level, mode):
    """Retorna texto para falar, ou None para cair no modo local."""
    dev = _device_id(sp)
    if dev is None:
        return None  # app aberto mas sem dispositivo visível: usa modo local

    if action == "play":
        if media_type == "liked":
            items = sp.current_user_saved_tracks(limit=50).get("items", [])
            uris = [i["track"]["uri"] for i in items if i.get("track")]
            if not uris:
                return "Você ainda não tem músicas curtidas."
            sp.start_playback(device_id=dev, uris=uris)
            return "Tocando suas músicas curtidas."
        if query:
            kind, item = _find(sp, query, media_type)
            if not item:
                return f"Não encontrei {query} no Spotify."
            if kind == "track":
                sp.start_playback(device_id=dev, uris=[item["uri"]])
            else:
                sp.start_playback(device_id=dev, context_uri=item["uri"])
            return f"Tocando {_label(kind, item)}."
        sp.start_playback(device_id=dev)
        return "Retomando a música."

    if action == "queue":
        if not query:
            return "Qual música devo adicionar à fila?"
        kind, item = _find(sp, query, "track")
        if not item:
            return f"Não encontrei {query}."
        sp.add_to_queue(item["uri"], device_id=dev)
        return f"Adicionei {_label(kind, item)} à fila."

    if action == "pause":
        sp.pause_playback(device_id=dev)
        return "Música pausada."

    if action == "toggle":
        pb = sp.current_playback()
        if pb and pb.get("is_playing"):
            sp.pause_playback(device_id=dev)
            return "Música pausada."
        sp.start_playback(device_id=dev)
        return "Retomando a música."

    if action in ("next", "previous"):
        (sp.next_track if action == "next" else sp.previous_track)(device_id=dev)
        time.sleep(0.8)
        return _np_text(sp)

    if action == "now_playing":
        return _np_text(sp)

    if action in ("set_volume", "volume_up", "volume_down", "mute"):
        pb = sp.current_playback() or {}
        cur = (pb.get("device") or {}).get("volume_percent", 50)
        step = level if level else 10
        if action == "set_volume":
            if level is None:
                return "Para qual volume, de 0 a 100?"
            new = level
        elif action == "volume_up":
            new = cur + step
        elif action == "volume_down":
            new = cur - step
        else:  # mute alterna
            if cur > 0:
                _prev_volume["value"] = cur
                new = 0
            else:
                new = _prev_volume["value"]
        new = max(0, min(100, int(new)))
        sp.volume(new, device_id=dev)
        return "Volume no mudo." if new == 0 else f"Volume em {new} por cento."

    if action == "shuffle":
        pb = sp.current_playback() or {}
        state = {"on": True, "off": False}.get(mode, not pb.get("shuffle_state", False))
        sp.shuffle(state, device_id=dev)
        return "Modo aleatório ligado." if state else "Modo aleatório desligado."

    if action == "repeat":
        pb = sp.current_playback() or {}
        mapa = {"off": "off", "all": "context", "one": "track"}
        if mode in mapa:
            state = mapa[mode]
        else:
            state = "off" if pb.get("repeat_state", "off") != "off" else "context"
        sp.repeat(state, device_id=dev)
        return {"off": "Repetição desligada.", "context": "Repetindo a playlist.",
                "track": "Repetindo esta música."}[state]

    if action in ("like", "unlike"):
        item = (sp.current_playback() or {}).get("item")
        if not item:
            return "Não há música tocando para curtir."
        if action == "like":
            sp.current_user_saved_tracks_add([item["id"]])
            return f"Curti {item['name']}."
        sp.current_user_saved_tracks_delete([item["id"]])
        return f"Removi {item['name']} das curtidas."

    if action in ("seek_forward", "seek_back"):
        pb = sp.current_playback() or {}
        if not pb.get("item"):
            return "Não há nada tocando."
        delta = (level or 15) * 1000 * (1 if action == "seek_forward" else -1)
        sp.seek_track(max(0, pb.get("progress_ms", 0) + delta), device_id=dev)
        return "Pronto."

    return None


# --------------------------------------------------------------------------- modo local

def _local(action, query, media_type, level, mode, pids, title):
    playing = _is_playing(title)

    if action == "now_playing":
        return f"Tocando {title}." if playing else "Não há nada tocando agora."

    if action == "play" and (query or media_type == "liked"):
        if query:
            os.startfile("spotify:search:" + urllib.parse.quote(query))
            return (f"Abri a busca por {query} no Spotify, mas para tocar direto preciso da "
                    "API do Spotify configurada.")
        return "Para tocar suas curtidas preciso da API do Spotify configurada."
    if action == "queue":
        return "Para adicionar à fila preciso da API do Spotify configurada."

    if action == "play":
        if playing:
            return "Já está tocando."
        _hotkey("play_pause"); return "Retomando a música."
    if action == "pause":
        if not playing:
            return "A música já está pausada."
        _hotkey("play_pause"); return "Música pausada."
    if action == "toggle":
        _hotkey("play_pause"); return "Feito."
    if action == "next":
        _hotkey("next"); return "Próxima música."
    if action == "previous":
        _hotkey("prev"); return "Voltando a música."

    # tudo abaixo precisa da janela do Spotify em primeiro plano
    if not _focus(pids):
        return "Não consegui trazer a janela do Spotify para frente."

    if action in ("volume_up", "volume_down"):
        steps = max(1, (level or 15) // 5)
        for _ in range(steps):
            _hotkey("ctrl", "up" if action == "volume_up" else "down")
        return "Volume ajustado."
    if action == "set_volume":
        if level is None:
            return "Para qual volume, de 0 a 100?"
        for _ in range(22):
            _hotkey("ctrl", "down")
        for _ in range(round(max(0, min(100, level)) / 5)):
            _hotkey("ctrl", "up")
        return f"Volume ajustado para cerca de {level} por cento."
    if action == "mute":
        _hotkey("mute"); return "Alternei o mudo do sistema."
    if action == "shuffle":
        _hotkey("ctrl", "s"); return "Alternei o modo aleatório."
    if action == "repeat":
        _hotkey("ctrl", "r"); return "Alternei a repetição."
    if action in ("like", "unlike"):
        _hotkey("alt", "shift", "b"); return "Alternei o curtir da música atual."
    if action in ("seek_forward", "seek_back"):
        for _ in range(max(1, (level or 15) // 15)):
            _hotkey("shift", "right" if action == "seek_forward" else "left")
        return "Pronto."
    return f"Não sei executar a ação {action} sem a API do Spotify."


# --------------------------------------------------------------------------- entrada

def _dispatch(parameters):
    action = (parameters.get("action") or "").strip().lower()
    action = ALIASES.get(action, action)
    query = (parameters.get("query") or "").strip()
    media_type = (parameters.get("media_type") or "track").strip().lower()
    mode = (parameters.get("mode") or "").strip().lower()
    mode = {"ligar": "on", "desligar": "off", "ligado": "on", "desligado": "off",
            "playlist": "all", "música": "one", "musica": "one", "tudo": "all"}.get(mode, mode)
    try:
        level = int(parameters["level"]) if parameters.get("level") is not None else None
    except (TypeError, ValueError):
        level = None

    if os.name != "nt":
        return "Sir, este plugin do Spotify foi feito para Windows."

    pids, title = _spotify_info()

    if action == "open":
        if pids:
            _focus(pids)
            return "O Spotify já está aberto."
        os.startfile("spotify:")
        for _ in range(16):
            time.sleep(0.5)
            if _spotify_info()[0]:
                return "Spotify aberto."
        return "Pedi para abrir o Spotify, mas ele demorou a responder."

    if action == "close":
        if not pids:
            return "O Spotify já está fechado."
        subprocess.run(["taskkill", "/IM", "Spotify.exe", "/F"], capture_output=True,
                       creationflags=_NO_WINDOW)
        return "Spotify fechado."

    if not pids:
        return "O Spotify não está aberto. Peça para eu abrir o Spotify primeiro."

    sp = _get_sp()
    if sp:
        try:
            out = _api(sp, action, query, media_type, level, mode)
            if out is not None:
                return out
        except Exception as e:
            err = str(e)
            if "premium" in err.lower():
                return ("O Spotify só permite esse controle remoto para contas Premium. "
                        "Vou usar o controle local quando possível.")
            if "Restriction violated" in err:
                return "O Spotify já está nesse estado."
            # qualquer outro erro da API: tenta o modo local abaixo

    return _local(action, query, media_type, level, mode, pids, title)


def run(parameters: dict, player=None, session_memory=None) -> str:
    try:
        result_text = _dispatch(parameters or {})
    except Exception as e:
        return f"Sir, o plugin do Spotify falhou: {e}"
    if player:
        try:
            player.write_log(f"JARVIS: {result_text}")
        except Exception:
            pass
    return result_text


# --------------------------------------------------------------------------- configuração única

def _cli_auth():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not _load_cfg():
        print("Crie um app em https://developer.spotify.com/dashboard e adicione este "
              f"Redirect URI:\n  {REDIRECT_URI}\n")
        cid = input("Client ID: ").strip()
        secret = input("Client Secret: ").strip()
        CONFIG_PATH.write_text(json.dumps({"client_id": cid, "client_secret": secret}),
                               encoding="utf-8")
    sp = _get_sp(interactive=True)
    if sp is None:
        print("Falhou. Rode: pip install spotipy")
        sys.exit(1)
    print("Autorizado como:", sp.current_user().get("display_name"))


if __name__ == "__main__":
    if "--auth" in sys.argv:
        _cli_auth()
    else:
        print(__doc__)
