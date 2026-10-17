#Still barely functional, easy to go-through. Simple security test to further put it better.
"""
Plugin JARVIS: guarda de voz (só atende o dono) + palavra de segurança.

Coloque em plugins/ e reinicie. Dependências (uma vez):
    pip install webrtcvad-wheels resemblyzer numpy pyaudio

Como funciona
  - Uma thread abre o microfone (16 kHz) e guarda os últimos ~10 s de áudio.
  - O JARVIS é instruído (pela descrição do plugin) a chamar action="check" no início de
    TODO pedido. O plugin compara a voz do último trecho falado com a sua "impressão de
    voz" (embedding do resemblyzer). Voz diferente -> o JARVIS recusa com um pedido de
    desculpas. Na dúvida (pouca fala captada, erro) ele ATENDE: nunca trava por incerteza.
  - Tudo é configurado conversando com o JARVIS: cadastrar voz, palavra de segurança,
    sensibilidade, nome do dono, ligar/desligar.
  - A palavra de segurança (guardada só como hash) libera o acesso por 30 min se o
    reconhecimento errar com você, e autoriza mudanças nas configurações.

Limites honestos: é uma barreira de cortesia, não segurança forte. O áudio continua indo
ao Gemini, o modelo pode "esquecer" de chamar a verificação, e uma gravação da sua voz
pode enganar o reconhecimento. Use fones para o JARVIS não ouvir a própria voz.

Socorro se travar:  python voice_guard.py --disable   (ou --reset, --enable, --status, --test)
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import unicodedata
from collections import deque
from pathlib import Path

PLUGIN = {
    "name": "voice_guard",
    "description": (
        "Guarda de voz: verifica se quem fala é o dono. OBRIGATÓRIO: no INÍCIO de TODO pedido "
        "do usuário, antes de responder ou chamar qualquer outra ferramenta, chame esta "
        "ferramenta com action='check'. Se o resultado começar com 'ACESSO NEGADO', diga "
        "APENAS a frase indicada, não atenda o pedido e não chame outras ferramentas. Se "
        "começar com 'VOZ_VERIFICADA' ou 'GUARDA_INATIVO', atenda normalmente e NUNCA "
        "mencione a verificação. Também serve para configurar por conversa: 'cadastrar minha "
        "voz' (action='enroll'; avise antes que vai ouvir ~20 segundos de fala livre), "
        "'minha palavra de segurança é X' (action='set_safety_word', new_word=X; nunca repita "
        "a palavra em voz alta), 'desbloquear / palavra de segurança X' (action='unlock', "
        "phrase=X), 'ativar/desativar proteção de voz' (enable/disable), 'bloquear de novo' "
        "(lock), 'ficar mais rígido/flexível' (set_threshold, value=up|down ou um número "
        "0.5-0.95), 'meu nome é ...' (set_owner_name, value=nome), 'apagar minha voz' (reset), "
        "'status da proteção de voz' (status). Se uma alteração vier com 'NÃO AUTORIZADO', "
        "peça a palavra de segurança e repita a ação com phrase=<palavra>."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": "check | enroll | set_safety_word | unlock | lock | enable | "
                               "disable | set_threshold | set_owner_name | reset | status",
            },
            "phrase": {
                "type": "STRING",
                "description": "Palavra de segurança dita pelo usuário (para unlock ou para "
                               "autorizar uma alteração).",
            },
            "new_word": {"type": "STRING", "description": "Nova palavra de segurança."},
            "value": {
                "type": "STRING",
                "description": "set_threshold: up, down ou número 0.5-0.95. "
                               "set_owner_name: o nome.",
            },
            "seconds": {
                "type": "INTEGER",
                "description": "enroll: duração da gravação (12-40, padrão 20).",
            },
        },
        "required": ["action"],
    },
}

# --------------------------------------------------------------------------- constantes

DATA_DIR = Path.home() / ".jarvis" / "voice_guard"
CONFIG_PATH = DATA_DIR / "config.json"
RATE = 16000
CHUNK = 1024
FRAME = 480                      # 30 ms
DEFAULT_OWNER = "Dan"
DEFAULT_THRESHOLD = 0.70
UNLOCK_SECONDS = 30 * 60
MAX_WORD_FAILS = 5
FAIL_LOCKOUT = 5 * 60

ALIASES = {
    "verify": "check", "register": "enroll", "cadastrar": "enroll",
    "set_word": "set_safety_word", "set_password": "set_safety_word",
    "off": "disable", "on": "enable", "clear": "reset", "delete": "reset",
}
SENSITIVE = {"enroll", "set_safety_word", "disable", "set_threshold", "set_owner_name", "reset"}

OK_TEXT = "VOZ_VERIFICADA. Atenda o pedido normalmente e não mencione esta verificação."
OFF_TEXT = "GUARDA_INATIVO. Atenda o pedido normalmente e não mencione esta verificação."

_ring = deque(maxlen=int(10 * RATE / CHUNK))
_sinks = []
_state = {"unlocked_until": 0.0, "fails": 0, "lock_until": 0.0,
          "last_state": None, "last_score": None, "last_time": None}
_lock = threading.Lock()
_listener = {"thread": None, "error": None}
_enc = {"obj": None}
_player = {"obj": None}


# --------------------------------------------------------------------------- config

def _load():
    try:
        return json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _save(cfg):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(cfg), encoding="utf-8")
    tmp.replace(CONFIG_PATH)


def _owner(cfg):
    return cfg.get("owner_name") or DEFAULT_OWNER


def _log(msg):
    p = _player["obj"]
    if p:
        try:
            p.write_log(f"JARVIS: {msg}")
        except Exception:
            pass


def _deny_text(cfg):
    return (f"ACESSO NEGADO. Diga APENAS esta frase, sem atender nem comentar o pedido e sem "
            f"chamar outras ferramentas: \"Mil perdões, eu não posso te ajudar. "
            f"Eu só falo com o {_owner(cfg)}.\"")


# --------------------------------------------------------------------------- palavra de segurança

def _norm(text):
    t = unicodedata.normalize("NFKD", text or "")
    t = "".join(c for c in t if not unicodedata.combining(c)).lower()
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9 ]", " ", t)).strip()


def _hash(word, salt):
    return hashlib.pbkdf2_hmac("sha256", _norm(word).encode(), salt, 200_000).hex()


def _word_ok(cfg, phrase):
    """Confere a palavra, com bloqueio temporário após várias tentativas erradas."""
    now = time.time()
    if now < _state["lock_until"] or not cfg.get("word_hash") or not phrase:
        return False
    salt = bytes.fromhex(cfg["salt"])
    # aceita a frase exata ou contendo a palavra (o Gemini às vezes manda a frase inteira)
    candidates = [phrase]
    words = _norm(phrase).split()
    wlen = int(cfg.get("word_len", 1)) or 1
    candidates += [" ".join(words[i:i + wlen]) for i in range(max(1, len(words) - wlen + 1))]
    ok = any(hmac.compare_digest(_hash(c, salt), cfg["word_hash"]) for c in candidates if c)
    if ok:
        _state["fails"] = 0
        return True
    _state["fails"] += 1
    if _state["fails"] >= MAX_WORD_FAILS:
        _state["lock_until"] = now + FAIL_LOCKOUT
        _state["fails"] = 0
    return False


def _unlocked():
    return time.time() < _state["unlocked_until"]


# --------------------------------------------------------------------------- microfone

def _listen_loop():
    while True:
        pa = stream = None
        try:
            import pyaudio
            pa = pyaudio.PyAudio()
            stream = pa.open(format=pyaudio.paInt16, channels=1, rate=RATE, input=True,
                             frames_per_buffer=CHUNK)
            _listener["error"] = None
            while True:
                data = stream.read(CHUNK, exception_on_overflow=False)
                _ring.append(data)
                for s in list(_sinks):
                    s.append(data)
        except Exception as e:
            _listener["error"] = str(e)
            time.sleep(3)
        finally:
            try:
                if stream:
                    stream.close()
                if pa:
                    pa.terminate()
            except Exception:
                pass


def _ensure_listener():
    with _lock:
        t = _listener["thread"]
        if t is None or not t.is_alive():
            t = threading.Thread(target=_listen_loop, daemon=True, name="voice_guard_mic")
            _listener["thread"] = t
            t.start()
            threading.Thread(target=_get_encoder, daemon=True).start()  # aquece o modelo


def _get_encoder():
    with _lock:
        if _enc["obj"] is None:
            from resemblyzer import VoiceEncoder
            _enc["obj"] = VoiceEncoder(device="cpu", verbose=False)
        return _enc["obj"]


# --------------------------------------------------------------------------- áudio -> voz

def _speech_mask(a):
    import numpy as np
    n = len(a) // FRAME
    fr = a[:n * FRAME].reshape(n, FRAME).astype(np.float32)
    rms = np.sqrt((fr ** 2).mean(axis=1))
    floor = float(np.percentile(rms, 20)) if n else 0.0
    thr = max(150.0, min(floor * 3.0, 700.0))
    return rms > thr, n


def _last_burst(window_s=8.0, max_s=4.0, min_speech_s=1.0):
    import numpy as np
    a = np.frombuffer(b"".join(list(_ring)), dtype=np.int16)[-int(window_s * RATE):]
    if len(a) < FRAME * 20:
        return None
    sp, n = _speech_mask(a)
    idx = np.flatnonzero(sp)
    if len(idx) == 0:
        return None
    end = start = int(idx[-1])
    gap, i = 0, end
    while i >= 0:
        if sp[i]:
            start, gap = i, 0
        else:
            gap += 1
            if gap > 15:          # pausa > 450 ms encerra o trecho
                break
        i -= 1
    if sp[start:end + 1].sum() * FRAME / RATE < min_speech_s:
        return None
    return a[start * FRAME:(end + 1) * FRAME][-int(max_s * RATE):]


def _embed(audio_int16):
    import numpy as np
    from resemblyzer import preprocess_wav
    wav = preprocess_wav(audio_int16.astype(np.float32) / 32768.0, source_sr=RATE)
    if len(wav) < RATE * 0.8:
        return None
    return _get_encoder().embed_utterance(wav)


def _verify_now(cfg):
    """('owner' | 'stranger' | 'unknown', score)."""
    import numpy as np
    vp = cfg.get("voiceprint")
    if not vp:
        return "unknown", None
    audio = _last_burst()
    if audio is None:
        return "unknown", None
    emb = _embed(audio)
    if emb is None:
        return "unknown", None
    score = float(np.dot(emb, np.array(vp, dtype=np.float32)))
    thr = float(cfg.get("threshold", DEFAULT_THRESHOLD))
    return ("owner" if score >= thr else "stranger"), score


def _authorized(cfg, p):
    if _unlocked():
        return True
    if cfg.get("word_hash") and _word_ok(cfg, (p.get("phrase") or "")):
        return True
    if cfg.get("voiceprint"):
        try:
            st, _ = _verify_now(cfg)
            return st == "owner"      # incerteza NÃO basta para mudar configurações
        except Exception:
            return False
    return False


# --------------------------------------------------------------------------- ações

def _do_check(cfg):
    if not cfg.get("enabled") or not cfg.get("voiceprint") or _unlocked():
        return OFF_TEXT
    _ensure_listener()
    try:
        st, score = _verify_now(cfg)
    except Exception as e:
        return f"{OFF_TEXT} (verificação indisponível: {e})"
    _state.update(last_state=st, last_score=score, last_time=time.time())
    if st == "stranger":
        _log(f"voz não reconhecida (pontuação {score:.2f}); pedido recusado.")
        return _deny_text(cfg)
    return OK_TEXT


def _do_enroll(cfg, seconds):
    import numpy as np
    _ensure_listener()
    time.sleep(1.5)
    if _listener["error"]:
        return f"Não consegui abrir o microfone: {_listener['error']}"
    seconds = max(12, min(40, int(seconds or 20)))
    sink = []
    _sinks.append(sink)
    try:
        time.sleep(seconds)
    finally:
        _sinks.remove(sink)
    if not sink:
        return "Não captei áudio do microfone."
    a = np.frombuffer(b"".join(sink), dtype=np.int16)
    sp, n = _speech_mask(a)
    fr = a[:n * FRAME].reshape(n, FRAME)
    speech = fr[sp].reshape(-1)
    if len(speech) < RATE * 8:
        return ("Captei pouca fala (menos de 8 segundos). Tente de novo, falando de forma "
                "contínua e num lugar mais silencioso.")
    emb = _embed(speech)
    if emb is None:
        return "Não consegui extrair sua voz. Tente de novo."
    # consistência: compara janelas de ~3 s com o resultado geral
    sims = []
    step = RATE * 3
    for i in range(0, len(speech) - step + 1, step):
        e = _embed(speech[i:i + step])
        if e is not None:
            sims.append(float(np.dot(e, emb)))
    cfg["voiceprint"] = [float(x) for x in emb]
    cfg["enabled"] = True
    cfg.setdefault("threshold", DEFAULT_THRESHOLD)
    _save(cfg)
    note = ""
    if sims and min(sims) < 0.65:
        note = " A gravação teve variações grandes; se der erro de reconhecimento, refaça num ambiente silencioso."
    extra = "" if cfg.get("word_hash") else " Sugira ao usuário definir agora uma palavra de segurança."
    _log("voz do dono cadastrada; proteção ativada.")
    return f"Voz cadastrada e proteção de voz ativada.{note}{extra}"


def _parse_threshold(cfg, value):
    cur = float(cfg.get("threshold", DEFAULT_THRESHOLD))
    v = _norm(str(value))
    if v in ("up", "mais", "rigido", "mais rigido", "aumentar", "subir"):
        new = cur + 0.03
    elif v in ("down", "menos", "flexivel", "mais flexivel", "diminuir", "baixar"):
        new = cur - 0.03
    else:
        try:
            new = float(str(value).replace(",", "."))
        except ValueError:
            return None
        if new > 1:
            new /= 100.0
    return round(max(0.5, min(0.95, new)), 2)


def _dispatch(p):
    action = (p.get("action") or "check").strip().lower()
    action = ALIASES.get(action, action)
    cfg = _load()
    owner = _owner(cfg)

    if action == "check":
        return _do_check(cfg)

    if action == "status":
        if _state["last_time"]:
            last = f" Última verificação: {_state['last_state']}"
            if _state["last_score"] is not None:
                last += f" com pontuação {_state['last_score']:.2f}"
            last += "."
        else:
            last = ""
        return (f"Voz {'cadastrada' if cfg.get('voiceprint') else 'não cadastrada'}; proteção "
                f"{'ativada' if cfg.get('enabled') else 'desativada'}; palavra de segurança "
                f"{'definida' if cfg.get('word_hash') else 'não definida'}; sensibilidade "
                f"{cfg.get('threshold', DEFAULT_THRESHOLD)}; "
                f"{'sessão desbloqueada' if _unlocked() else 'sessão normal'}.{last}")

    if action == "unlock":
        if not cfg.get("word_hash"):
            return "Ainda não há palavra de segurança definida."
        if time.time() < _state["lock_until"]:
            return "Muitas tentativas erradas. Aguarde alguns minutos."
        if _word_ok(cfg, p.get("phrase") or ""):
            _state["unlocked_until"] = time.time() + UNLOCK_SECONDS
            _log("sessão desbloqueada pela palavra de segurança (30 min).")
            return "Palavra correta. Acesso liberado por 30 minutos."
        return "Palavra incorreta."

    if action == "lock":
        _state["unlocked_until"] = 0.0
        return "Proteção reativada."

    if action == "enable":
        if not cfg.get("voiceprint"):
            return "Primeiro preciso cadastrar sua voz."
        cfg["enabled"] = True
        _save(cfg)
        _ensure_listener()
        return "Proteção de voz ativada."

    if action not in SENSITIVE:
        return f"Não conheço a ação {action}."

    # ações sensíveis: se já existe configuração, exige voz do dono ou palavra de segurança
    if (cfg.get("voiceprint") or cfg.get("word_hash")) and not _authorized(cfg, p):
        return (f"NÃO AUTORIZADO. Diga apenas: \"Não consegui confirmar que é você. Se for o "
                f"{owner}, diga a palavra de segurança.\"")

    if action == "enroll":
        return _do_enroll(cfg, p.get("seconds"))

    if action == "set_safety_word":
        word = p.get("new_word") or ""
        if len(_norm(word)) < 4:
            return "A palavra precisa ter pelo menos 4 letras. Peça outra."
        salt = secrets.token_bytes(16)
        cfg["salt"] = salt.hex()
        cfg["word_hash"] = _hash(word, salt)
        cfg["word_len"] = len(_norm(word).split())
        _save(cfg)
        return "Palavra de segurança definida. Não repita a palavra em voz alta."

    if action == "disable":
        cfg["enabled"] = False
        _save(cfg)
        return "Proteção de voz desativada."

    if action == "set_threshold":
        new = _parse_threshold(cfg, p.get("value"))
        if new is None:
            return "Não entendi o valor. Use up, down ou um número entre 0.5 e 0.95."
        cfg["threshold"] = new
        _save(cfg)
        return f"Sensibilidade ajustada para {new}. Quanto maior, mais rígido."

    if action == "set_owner_name":
        name = (p.get("value") or "").strip()
        if not name:
            return "Qual nome devo usar?"
        cfg["owner_name"] = name[:40]
        _save(cfg)
        return f"Certo, vou dizer que só falo com {cfg['owner_name']}."

    if action == "reset":
        cfg.pop("voiceprint", None)
        cfg["enabled"] = False
        _save(cfg)
        return "Sua impressão de voz foi apagada e a proteção foi desativada."

    return "Ação inválida."


def run(parameters: dict, player=None, session_memory=None) -> str:
    _player["obj"] = player
    try:
        return _dispatch(parameters or {})
    except Exception as e:
        if (parameters or {}).get("action", "check") == "check":
            return f"{OFF_TEXT} (guarda de voz com erro: {e})"
        return f"Sir, o guarda de voz falhou: {e}"


# --------------------------------------------------------------------------- partida e CLI

try:
    if _load().get("enabled"):
        _ensure_listener()
except Exception:
    pass


def _cli():
    cfg = _load()
    arg = sys.argv[1] if len(sys.argv) > 1 else ""
    if arg in ("--disable", "--enable", "--reset"):
        if arg == "--reset":
            cfg.pop("voiceprint", None)
        cfg["enabled"] = arg == "--enable"
        _save(cfg)
        print("ok:", arg)
    elif arg == "--status":
        print(_dispatch({"action": "status"}))
    elif arg == "--test":
        _ensure_listener()
        print("Fale uma frase por ~5 segundos...")
        time.sleep(6)
        print(_verify_now(cfg))
    else:
        print(__doc__)


if __name__ == "__main__":
    _cli()
