# ============================================================
#  OpenCode on Kaggle CPU — ONE CELL — full remote control   (v3 · on-screen keys + model picker)
#  • ttyd   : real terminal in the browser (mouse, paste) -> tmux (windows: web / tui / shell)
#  • opencode web UI        • file browser (download anything from /kaggle/working)
#  • CONTROL API: the website presses keys / switches models / adds API keys for you
#  • 4 Cloudflare quick tunnels (secret random URLs) -> live.json on Hugging Face
#  • your control website polls live.json and auto-connects
#  • state -> HF every 15 min + final save before the 12 h limit
#  Interactive: cell returns after setup, everything keeps running in tmux.
#  Headless 12 h: Save Version -> "Save & Run All" (batch mode keeps the cell alive).
# ============================================================
import os, sys, subprocess, time, datetime, threading, json, secrets, re, tarfile, socket, shutil, shlex, warnings
warnings.filterwarnings("ignore")
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HF_TOKEN       = "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"
HF_REPO        = "amer224/opencode-kaggle-state"            # "user/name" or just "name" (created under your HF account)
SAVE_EVERY_MIN = 15
DEADLINE_H     = 11.5
DEFAULT_MODEL  = ""    # "provider/model" or "" -> OpenCode default. Change any time from the Models tab.
P_WEB, P_TTY, P_FILES, P_CTL = 4096, 7681, 8088, 8090
COLS, ROWS = 120, 36
TTYD_VER = "1.7.7"
EXTRA_ENV = {}   # extra provider keys injected from the control site settings
for _k, _v in EXTRA_ENV.items():
    if _v: os.environ[_k] = _v

T0 = time.time()
UTC = datetime.timezone.utc
os.environ.update({"HF_TOKEN": HF_TOKEN, "HF_HUB_DISABLE_XET": "1", "HF_HUB_DISABLE_PROGRESS_BARS": "1", "HF_HUB_DISABLE_TELEMETRY": "1", "PYTHONWARNINGS": "ignore"})
HOME, OUT = os.path.expanduser("~"), "/kaggle/working"
WORK = f"{OUT}/workspace"; os.makedirs(WORK, exist_ok=True)
IS_BATCH = os.environ.get("KAGGLE_KERNEL_RUN_TYPE", "Interactive").lower() == "batch"
SECRET = secrets.token_urlsafe(10)
DEVNULL = subprocess.DEVNULL
os.environ["PATH"] = f"{HOME}/.opencode/bin:{HOME}/.local/bin:/usr/local/bin:" + os.environ["PATH"]
OC_CFG_DIR = f"{HOME}/.config/opencode"; OC_CFG = f"{OC_CFG_DIR}/opencode.json"
AUTH_JSON  = f"{HOME}/.local/share/opencode/auth.json"
ENV_FILE   = f"{HOME}/.oc_env"

def log(*a): print(f"[{datetime.datetime.now():%H:%M:%S}]", *a, flush=True)
def now(): return datetime.datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")

def sh(cmd, timeout=120):
    """Short foreground command. Never inherits notebook stdin, always times out."""
    try:
        r = subprocess.run(cmd, shell=True, text=True, executable="/bin/bash", stdin=DEVNULL,
                           stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=timeout)
        return r.stdout or ""
    except subprocess.TimeoutExpired:
        return f"[timeout after {timeout}s]"

def bg(cmd, logfile):
    """Long-running daemon: own session, output to a file, NO pipe back to the notebook."""
    f = open(logfile, "ab", buffering=0)
    return subprocess.Popen(cmd, shell=True, executable="/bin/bash", stdin=DEVNULL, stdout=f, stderr=f,
                            start_new_session=True, close_fds=True)

def port_open(port, host="127.0.0.1"):
    with socket.socket() as s:
        s.settimeout(1)
        return s.connect_ex((host, port)) == 0

def wait_for(pred, secs, label):
    print(f"   waiting for {label} ", end="", flush=True)
    for i in range(secs):
        try:
            if pred(): print(" ok", flush=True); return True
        except Exception: pass
        if i % 3 == 0: print(".", end="", flush=True)
        time.sleep(1)
    print(f" gave up after {secs}s", flush=True); return False

def read_json(p, default):
    try:
        with open(p) as f: return json.load(f)
    except Exception: return default
def write_json(p, d, mode=None):
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w") as f: json.dump(d, f, indent=2)
    if mode is not None: os.chmod(p, mode)
def current_model(): return read_json(OC_CFG, {}).get("model") or None

def kill_old():
    # pkill -f 'cloudflared' would match (and kill) the very shell running it -> [c] regex trick
    sh(f"tmux kill-server 2>/dev/null; pkill -f '[c]loudflared tunnel'; pkill -f '[t]tyd '; pkill -f '[h]ttp.server {P_FILES}'; true", timeout=20)
    time.sleep(1)

# ------------------------------------------------------------------ 1/6
log("== 1/6 deps ==")
if not shutil.which("tmux"):
    sh("apt-get install -y -qq tmux fonts-dejavu-core >/dev/null 2>&1 || (apt-get update -qq >/dev/null 2>&1 && apt-get install -y -qq tmux fonts-dejavu-core >/dev/null 2>&1); true", timeout=400)
sh(f"{sys.executable} -m pip install -q pyte pillow 'huggingface_hub>=0.25' >/dev/null 2>&1; {sys.executable} -m pip uninstall -y -q hf_xet hf-xet >/dev/null 2>&1; true", timeout=400)
for m in [k for k in list(sys.modules) if k.startswith(("huggingface_hub", "hf_xet"))]: del sys.modules[m]
if not os.path.exists("/usr/local/bin/cloudflared"):
    sh("curl -fsSL --retry 3 --max-time 180 -o /usr/local/bin/cloudflared https://github.com/cloudflare/cloudflared/releases/latest/download/cloudflared-linux-amd64 && chmod +x /usr/local/bin/cloudflared", timeout=240)
if not os.path.exists("/usr/local/bin/ttyd"):
    sh(f"curl -fsSL --retry 3 --max-time 180 -o /usr/local/bin/ttyd https://github.com/tsl0922/ttyd/releases/download/{TTYD_VER}/ttyd.x86_64 && chmod +x /usr/local/bin/ttyd", timeout=240)
log("tmux:", sh("tmux -V").strip() or "MISSING", "| ttyd:", sh("ttyd --version").strip() or "MISSING",
    "| cloudflared:", (sh("cloudflared --version").strip().split(" (")[0] or "MISSING"), "| mode:", "BATCH" if IS_BATCH else "INTERACTIVE")

# ------------------------------------------------------------------ 2/6
log("== 2/6 Hugging Face storage ==")
import logging; logging.getLogger("huggingface_hub").setLevel(logging.ERROR)
from huggingface_hub import HfApi, hf_hub_download, create_repo, whoami
api = HfApi(token=HF_TOKEN); USER = whoami(token=HF_TOKEN)["name"]
REPO = HF_REPO if "/" in HF_REPO else f"{USER}/{HF_REPO}"
create_repo(REPO, repo_type="dataset", private=True, exist_ok=True, token=HF_TOKEN)
STATE_DIRS = [f"{HOME}/.local/share/opencode", f"{HOME}/.config/opencode", WORK]
for p in STATE_DIRS: os.makedirs(p, exist_ok=True)
EXCLUDE = ("/bin/", "/log/", "/node_modules/", "/.git/", "/cache/", "/.venv/", "/__pycache__/")
try:
    tb = hf_hub_download(REPO, "state.tar.gz", repo_type="dataset", token=HF_TOKEN, force_download=True)
    with tarfile.open(tb) as t:
        try: t.extractall("/", filter="fully_trusted")
        except TypeError: t.extractall("/")
    log(f"restored state.tar.gz ({os.path.getsize(tb)//1024} KB)")
except Exception as e: log("no previous state:", type(e).__name__)

if DEFAULT_MODEL:
    _c = read_json(OC_CFG, {}); _c["$schema"] = "https://opencode.ai/config.json"; _c["model"] = DEFAULT_MODEL; write_json(OC_CFG, _c)

LIVE = {"terminal": None, "web": None, "files": None, "ctl": None, "ctl_token": SECRET, "started": None, "deadline_utc": None,
        "last_save": None, "mode": "batch" if IS_BATCH else "interactive", "ended": None, "opencode_version": None,
        "host": socket.gethostname(), "heartbeat": None, "model": current_model()}
def push_live():
    LIVE["heartbeat"] = now(); LIVE["model"] = current_model()
    try: api.upload_file(path_or_fileobj=json.dumps(LIVE, indent=1).encode(), path_in_repo="live.json", repo_id=REPO, repo_type="dataset", token=HF_TOKEN, commit_message="live")
    except Exception as e: log("live.json push failed:", type(e).__name__, str(e)[:100])
_lock = threading.Lock()
def save_to_hf(msg="autosave"):
    with _lock:
        tmp = "/tmp/state.tar.gz"
        with tarfile.open(tmp, "w:gz") as t:
            for d in STATE_DIRS:
                if os.path.isdir(d): t.add(d, arcname=d.lstrip("/"), filter=lambda ti: None if any(x in "/"+ti.name+"/" for x in EXCLUDE) else ti)
        for i in range(3):
            try:
                api.upload_file(path_or_fileobj=tmp, path_in_repo="state.tar.gz", repo_id=REPO, repo_type="dataset", token=HF_TOKEN, commit_message=f"{msg} {datetime.datetime.now(UTC):%Y-%m-%d %H:%M}Z")
                LIVE["last_save"] = now(); push_live()
                log(f"saved {os.path.getsize(tmp)//1024} KB -> HF ({msg})"); return True
            except Exception as e: log("save failed:", type(e).__name__, str(e)[:120]); time.sleep(10)
        return False

# ------------------------------------------------------------------ 3/6
log("== 3/6 install OpenCode ==")
if not shutil.which("opencode"):
    sh("curl -fsSL https://opencode.ai/install | bash", timeout=600)
OC = shutil.which("opencode") or f"{HOME}/.opencode/bin/opencode"
OC_VER = sh(f"{OC} --version", timeout=30).strip().splitlines()[-1:] or ["NOT FOUND"]
LIVE["opencode_version"] = OC_VER[0]; log("opencode:", OC_VER[0], "| default model:", current_model() or "(opencode picks)")

# ------------------------------------------------------------------ 4/6
log("== 4/6 services + tunnels ==")
kill_old()
with open(ENV_FILE, "w") as f:
    f.write("export TERM=xterm-256color COLORTERM=truecolor OPENCODE_DISABLE_AUTOUPDATE=1\n")
    for k in ["OPENAI_API_KEY","ANTHROPIC_API_KEY","OPENROUTER_API_KEY","GROQ_API_KEY","GEMINI_API_KEY","HF_TOKEN","PATH"]:
        if os.environ.get(k): f.write(f"export {k}={shlex.quote(os.environ[k])}\n")
open(f"{HOME}/.tmux.conf","w").write("set -g mouse on\nset -g history-limit 50000\nset -g default-terminal 'tmux-256color'\nset -ga terminal-overrides ',*:Tc'\nset -g status-style bg=colour236,fg=colour250\nset -g escape-time 10\nsetw -g aggressive-resize on\n")

def web_cmd(serve=False):
    return f"source {ENV_FILE}; cd {WORK}; {OC} {'serve' if serve else 'web'} --port {P_WEB} --hostname 0.0.0.0 2>&1 | tee -a {OUT}/opencode_web.log"
def tui_cmd(model=None):
    m = model or current_model()
    return f"source {ENV_FILE}; cd {WORK}; {OC}" + (f" --model {shlex.quote(m)}" if m else "")
def run_in(window, cmd):
    """(re)start a tmux window running cmd; when cmd exits the window drops to a shell so errors stay readable."""
    wrapped = f"bash -c {shlex.quote(cmd + '; exec bash')}"
    if window in sh("tmux list-windows -t oc -F '#W'").split():
        sh(f"tmux respawn-window -k -t oc:{window} {shlex.quote(wrapped)}", timeout=20)
    else:
        sh(f"tmux new-window -d -t oc -n {window} -c {WORK} {shlex.quote(wrapped)}", timeout=20)
def healthy(): return sh(f"curl -s --max-time 3 http://127.0.0.1:{P_WEB}/global/health", timeout=8).startswith("{")

# window 0 "web": opencode server + web UI (independent of any browser)
sh(f"tmux new-session -d -s oc -n web -x {COLS} -y {ROWS} -c {WORK}", timeout=30)
run_in("web", web_cmd())
if not wait_for(healthy, 120, "opencode web"):
    log("'opencode web' not answering. log tail:\n" + sh(f"tail -n 8 {OUT}/opencode_web.log")[-700:])
    log("-> falling back to 'opencode serve'")
    run_in("web", web_cmd(serve=True)); wait_for(healthy, 60, "opencode serve")
log("server:", sh(f"curl -s --max-time 3 http://127.0.0.1:{P_WEB}/global/health", timeout=8).strip() or "NOT UP (Models tab -> 'web log' shows why; TUI still works)")

# window 1 "tui": OpenCode TUI (standalone - most reliable) | window 2 "shell": plain bash
run_in("tui", tui_cmd())
run_in("shell", f"source {ENV_FILE}; clear")
sh("tmux select-window -t oc:tui")

# ---------------- CONTROL API: the website talks to this (secret token, CORS) ----------------
KEYMAP = {"up":"Up","down":"Down","left":"Left","right":"Right","enter":"Enter","esc":"Escape","tab":"Tab","btab":"BTab",
          "space":"Space","bspace":"BSpace","del":"DC","pgup":"PageUp","pgdn":"PageDown","home":"Home","end":"End",
          "ctrl-a":"C-a","ctrl-b":"C-b","ctrl-c":"C-c","ctrl-d":"C-d","ctrl-e":"C-e","ctrl-g":"C-g","ctrl-k":"C-k","ctrl-l":"C-l",
          "ctrl-n":"C-n","ctrl-o":"C-o","ctrl-p":"C-p","ctrl-r":"C-r","ctrl-t":"C-t","ctrl-u":"C-u","ctrl-w":"C-w","ctrl-x":"C-x","ctrl-z":"C-z",
          "f1":"F1","f2":"F2","f3":"F3","f4":"F4"}
ENV_NAMES = {"openai":"OPENAI_API_KEY","anthropic":"ANTHROPIC_API_KEY","openrouter":"OPENROUTER_API_KEY","groq":"GROQ_API_KEY",
             "google":"GEMINI_API_KEY","xai":"XAI_API_KEY","deepseek":"DEEPSEEK_API_KEY","mistral":"MISTRAL_API_KEY",
             "togetherai":"TOGETHER_API_KEY","cerebras":"CEREBRAS_API_KEY","huggingface":"HF_TOKEN","opencode":"OPENCODE_API_KEY"}
MODEL_RE = r"^[A-Za-z0-9_.-]+/[^\s]+$"
_mcache = {"t": 0, "list": []}
def list_models(refresh=False):
    if refresh or not _mcache["list"] or time.time() - _mcache["t"] > 900:
        out = sh(f"source {ENV_FILE}; cd {WORK}; {OC} models" + (" --refresh" if refresh else ""), timeout=150)
        ms = sorted({l.strip() for l in out.splitlines() if re.match(MODEL_RE, l.strip())})
        if ms: _mcache.update(t=time.time(), list=ms)
    return _mcache["list"]
def tmux_keys(window, keys, literal=False):
    subprocess.run(["tmux", "send-keys", "-t", f"oc:{window}"] + (["-l"] if literal else []) + list(keys),
                   stdin=DEVNULL, stdout=DEVNULL, stderr=DEVNULL, timeout=10)
def set_model(m):
    c = read_json(OC_CFG, {}); c["$schema"] = "https://opencode.ai/config.json"; c["model"] = m; write_json(OC_CFG, c)
    LIVE["model"] = m; threading.Thread(target=push_live, daemon=True).start()
    run_in("tui", tui_cmd(m)); sh("tmux select-window -t oc:tui"); log("model ->", m)
def set_api_key(provider, key):
    a = read_json(AUTH_JSON, {}); a[provider] = {"type": "api", "key": key}; write_json(AUTH_JSON, a, 0o600)
    if provider in ENV_NAMES:
        os.environ[ENV_NAMES[provider]] = key
        lines = [l for l in open(ENV_FILE).read().splitlines() if not l.startswith(f"export {ENV_NAMES[provider]}=")]
        lines.append(f"export {ENV_NAMES[provider]}={shlex.quote(key)}"); open(ENV_FILE, "w").write("\n".join(lines) + "\n")
    _mcache["t"] = 0; log("api key set for", provider)

class Ctl(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    def log_message(self, *a): pass
    def _send(self, code, obj):
        b = json.dumps(obj).encode()
        self.send_response(code)
        for k, v in [("Content-Type", "application/json"), ("Access-Control-Allow-Origin", "*"), ("Access-Control-Allow-Headers", "*"),
                     ("Access-Control-Allow-Methods", "GET, POST, OPTIONS"), ("Cache-Control", "no-store"), ("Content-Length", str(len(b)))]:
            self.send_header(k, v)
        self.end_headers(); self.wfile.write(b)
    def _ok(self, q): return (self.headers.get("X-Token") or (q.get("t") or [""])[0]) == SECRET
    def do_OPTIONS(self): self._send(200, {})
    def do_GET(self):
        try:
            u = urlparse(self.path); q = parse_qs(u.query); p = u.path.rstrip("/")
            if not self._ok(q): return self._send(401, {"error": "bad token"})
            if p == "/ping":
                return self._send(200, {"ok": True, "version": OC_VER[0], "model": current_model(), "server": healthy(), "uptime_s": int(time.time() - T0),
                                        "windows": sh("tmux list-windows -t oc -F '#W'").split(), "providers": sorted(read_json(AUTH_JSON, {}).keys()),
                                        "env_keys": [k for k in ENV_NAMES.values() if os.environ.get(k)]})
            if p == "/models": return self._send(200, {"models": list_models("refresh" in q), "current": current_model()})
            if p == "/screen":
                w = re.sub(r"[^a-z]", "", (q.get("w") or ["tui"])[0]) or "tui"
                return self._send(200, {"text": sh(f"tmux capture-pane -p -t oc:{w}", timeout=10)})
            if p == "/logs":
                n = re.sub(r"[^a-z_]", "", (q.get("name") or ["web"])[0]) or "web"
                f = {"web": f"{OUT}/opencode_web.log", "ttyd": f"{OUT}/ttyd.log", "files": f"{OUT}/files.log"}.get(n, f"{OUT}/cf_{n}.log")
                return self._send(200, {"text": sh(f"tail -n 80 {shlex.quote(f)} 2>&1", timeout=10)})
            return self._send(404, {"error": "unknown endpoint"})
        except Exception as e: return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})
    def do_POST(self):
        try:
            u = urlparse(self.path); q = parse_qs(u.query); p = u.path.rstrip("/")
            if not self._ok(q): return self._send(401, {"error": "bad token"})
            n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}") if n else {}
            w = re.sub(r"[^a-z]", "", str(body.get("window", "tui"))) or "tui"
            if p == "/keys":
                for k in body.get("keys", [])[:40]:
                    k = str(k)
                    if k in KEYMAP: tmux_keys(w, [KEYMAP[k]])
                    else: tmux_keys(w, [k], literal=True)
                    time.sleep(0.06)
                return self._send(200, {"ok": True})
            if p == "/type":
                tmux_keys(w, [str(body.get("text", ""))], literal=True)
                if body.get("enter", True): time.sleep(0.15); tmux_keys(w, ["Enter"])
                return self._send(200, {"ok": True})
            if p == "/window": sh(f"tmux select-window -t oc:{w}"); return self._send(200, {"ok": True})
            if p == "/model":
                m = str(body.get("model", "")).strip()
                if not re.match(MODEL_RE, m): return self._send(400, {"error": "model must look like provider/model"})
                set_model(m); return self._send(200, {"ok": True, "model": m})
            if p == "/apikey":
                prov = re.sub(r"[^a-z0-9_-]", "", str(body.get("provider", "")).lower()); key = str(body.get("key", "")).strip()
                if not prov or not key: return self._send(400, {"error": "provider and key required"})
                set_api_key(prov, key)
                if body.get("restart", True): run_in("tui", tui_cmd()); sh("tmux select-window -t oc:tui")
                return self._send(200, {"ok": True, "provider": prov})
            if p == "/restart":
                what = str(body.get("what", "tui"))
                if what == "tui": run_in("tui", tui_cmd()); sh("tmux select-window -t oc:tui")
                elif what == "web": run_in("web", web_cmd(bool(body.get("serve"))))
                elif what == "shell": run_in("shell", f"source {ENV_FILE}; clear")
                else: return self._send(400, {"error": "what must be tui|web|shell"})
                return self._send(200, {"ok": True})
            if p == "/shot": return self._send(200, {"ok": True, "path": shot(show=False, window=w)})
            if p == "/save": threading.Thread(target=save_to_hf, args=("manual",), daemon=True).start(); return self._send(200, {"ok": True})
            return self._send(404, {"error": "unknown endpoint"})
        except Exception as e: return self._send(500, {"error": f"{type(e).__name__}: {str(e)[:200]}"})

try: _CTL.shutdown(); _CTL.server_close()          # re-run in the same kernel -> replace the old one
except Exception: pass
_CTL = ThreadingHTTPServer(("127.0.0.1", P_CTL), Ctl); _CTL.daemon_threads = True
threading.Thread(target=_CTL.serve_forever, daemon=True).start()

# ttyd: browser terminal -> tmux (writable, mouse on, secret base path)
THEME = json.dumps({"background": "#0c0c0c", "foreground": "#d4d4d4"})
TTYD_CMD = f"ttyd -W -p {P_TTY} -b /{SECRET} -t fontSize=14 -t disableLeaveAlert=true -t {shlex.quote('theme=' + THEME)} tmux new-session -A -s oc"
bg(TTYD_CMD, f"{OUT}/ttyd.log")
# file browser / downloads for /kaggle/working
bg(f"cd {OUT} && {sys.executable} -m http.server {P_FILES} --bind 127.0.0.1", f"{OUT}/files.log")
wait_for(lambda: port_open(P_TTY), 20, "ttyd :%d" % P_TTY)
wait_for(lambda: port_open(P_FILES), 10, "files :%d" % P_FILES)
wait_for(lambda: port_open(P_CTL), 10, "control api :%d" % P_CTL)

def tunnel(port, name):
    lf = f"{OUT}/cf_{name}.log"; open(lf, "w").close()
    return bg(f"cloudflared tunnel --no-autoupdate --protocol http2 --edge-ip-version auto --url http://127.0.0.1:{port}", lf)
PORTS = [(P_TTY, "terminal"), (P_WEB, "web"), (P_FILES, "files"), (P_CTL, "ctl")]
def grab():
    for port, name in PORTS:
        if not LIVE[name]:
            try: txt = open(f"{OUT}/cf_{name}.log").read()
            except Exception: txt = ""
            m = re.search(r"https://[a-z0-9-]+\.trycloudflare\.com", txt)
            if m: LIVE[name] = m.group(0) + (f"/{SECRET}/" if name == "terminal" else "/")
    return all(LIVE[n] for _, n in PORTS)
for port, name in PORTS: tunnel(port, name)
if not wait_for(grab, 90, "cloudflare tunnel URLs"):
    for port, name in PORTS:
        if not LIVE[name]:
            log(f"retrying tunnel '{name}' (log tail: {sh(f'tail -n 3 {OUT}/cf_{name}.log').strip()[-200:]})")
            sh(f"pkill -f '[c]loudflared.*127.0.0.1:{port}'; true"); tunnel(port, name)
    wait_for(grab, 90, "tunnel retry")

LIVE["started"] = now()
LIVE["deadline_utc"] = (datetime.datetime.now(UTC) + datetime.timedelta(hours=DEADLINE_H)).isoformat(timespec="seconds").replace("+00:00", "Z")
push_live()
print("\n" + "=" * 70)
for _, k in PORTS: print(f"  {k:9s}: {LIVE[k] or 'FAILED'}")
print(f"  live.json : https://huggingface.co/datasets/{REPO}/blob/main/live.json")
print("=" * 70 + "\n", flush=True)

# ------------------------------------------------------------------ 5/6
log("== 5/6 screenshot ==")
import pyte
from PIL import Image, ImageDraw, ImageFont
from IPython.display import display, Image as IPImage
def _font(b=False, s=16):
    p = f"/usr/share/fonts/truetype/dejavu/DejaVuSansMono{'-Bold' if b else ''}.ttf"
    return ImageFont.truetype(p, s) if os.path.exists(p) else ImageFont.load_default()
PAL = {"black":"#1e1e1e","red":"#f14c4c","green":"#23d18b","brown":"#f5f543","blue":"#3b8eea","magenta":"#d670d6","cyan":"#29b8db","white":"#cccccc","brightblack":"#666666","brightred":"#ff7b7b","brightgreen":"#89d185","brightyellow":"#f9f1a5","brightblue":"#6fa8ff","brightmagenta":"#ff9ff3","brightcyan":"#8be9fd","brightwhite":"#ffffff"}
def _col(c, d):
    if not c or c == "default": return d
    if c in PAL: return PAL[c]
    if len(c) == 6:
        try: int(c, 16); return "#" + c
        except ValueError: pass
    return d
def shot(name=None, show=True, upload=True, window="tui"):
    ansi = sh(f"tmux capture-pane -p -e -t oc:{window}", timeout=15)
    scr = pyte.Screen(COLS, ROWS); pyte.Stream(scr).feed(ansi.replace("\n", "\r\n"))
    fn, fb = _font(), _font(True); cw = int(fn.getbbox("M")[2]) or 10; ch = 20; pad = 14
    img = Image.new("RGB", (COLS*cw + 2*pad, ROWS*ch + 2*pad), "#0c0c0c"); dr = ImageDraw.Draw(img)
    for y in range(ROWS):
        for x in range(COLS):
            c = scr.buffer[y][x]; fg, bg_ = _col(c.fg, "#d4d4d4"), _col(c.bg, "#0c0c0c")
            if c.reverse: fg, bg_ = bg_, fg
            X, Y = pad + x*cw, pad + y*ch
            if bg_ != "#0c0c0c": dr.rectangle([X, Y, X+cw, Y+ch], fill=bg_)
            if c.data and c.data != " ": dr.text((X, Y), c.data, font=fb if c.bold else fn, fill=fg)
    name = name or f"opencode_{datetime.datetime.now():%Y%m%d_%H%M%S}.png"; path = os.path.join(OUT, name); img.save(path); log("saved", path)
    if show: display(IPImage(path))
    if upload:
        try: api.upload_file(path_or_fileobj=path, path_in_repo=f"screenshots/{name}", repo_id=REPO, repo_type="dataset", token=HF_TOKEN)
        except Exception as e: log("upload failed:", type(e).__name__)
    return path
def send(keys, enter=True, wait=2, window="tui"):
    tmux_keys(window, [keys], literal=True)
    if enter: tmux_keys(window, ["Enter"])
    time.sleep(wait)
def status():
    for _, k in PORTS: print(f"  {k:9s}: {LIVE[k]}")
    print("  model    :", current_model() or "(default)")
    print("  windows  :", sh("tmux list-windows -t oc -F '#I:#W'").replace("\n", " "))
try: shot("opencode_terminal_screenshot.png")
except Exception as e: log("screenshot failed:", type(e).__name__, str(e)[:100])

# ------------------------------------------------------------------ 6/6
log("== 6/6 autosave ==")
save_to_hf("initial sync")
threading.Thread(target=list_models, daemon=True).start()   # warm the model list for the website
STOP = threading.Event()
def _loop():
    last_save, last_beat = time.time(), time.time()
    while not STOP.is_set():
        time.sleep(20)
        try:
            if (time.time() - T0) / 3600 >= DEADLINE_H:
                log("[deadline] final save"); save_to_hf("FINAL before 12h limit")
                LIVE.update({"terminal": None, "web": None, "files": None, "ctl": None, "ended": now()}); push_live(); STOP.set(); break
            if time.time() - last_save >= SAVE_EVERY_MIN * 60: save_to_hf("autosave"); last_save = time.time()
            elif time.time() - last_beat >= 300: push_live(); last_beat = time.time()   # heartbeat for the site
            if not port_open(P_TTY): log("ttyd died -> restarting"); bg(TTYD_CMD, f"{OUT}/ttyd.log")
        except Exception as e: log("loop error:", type(e).__name__, str(e)[:100])
threading.Thread(target=_loop, daemon=True).start()
log(f"autosave every {SAVE_EVERY_MIN} min, final save at {DEADLINE_H} h. Helpers: send('text'), shot(), save_to_hf(), status(), set_model('provider/model')")
if IS_BATCH:
    log("BATCH: staying alive until deadline (you can close the browser).")
    while not STOP.is_set(): time.sleep(30)
    sh("tmux kill-server; pkill -f '[c]loudflared tunnel'; pkill -f '[t]tyd '; true", timeout=20); log("done.")
else:
    log("INTERACTIVE: cell returned; everything keeps running in tmux while this notebook session is alive. Open your control website -> Models tab.")
