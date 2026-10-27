import os, sys, json, shutil, subprocess, threading, time, webbrowser, urllib.parse
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

import music_swap as ms

HERE = ms.HERE
TOOL = os.path.join(HERE, 'music_swap.py')
MUSIC = os.path.join(HERE, 'music')
BACKUP = os.path.join(HERE, 'backup')
FIRST = os.path.join(BACKUP, 'Master.first-seen.bank')
UNDO = os.path.join(BACKUP, 'Master.before-last-swap.bank')
CACHE = os.path.join(HERE, 'cache')
HISTORY = os.path.join(HERE, 'history.json')
PORT = 8765

BROWSE = '''
import tkinter as tk
from tkinter import filedialog
root = tk.Tk(); root.withdraw(); root.attributes('-topmost', True)
print(filedialog.askopenfilename(title="Castlevania Belmont's Curse.exe", filetypes=[('Game', '*.exe')]) or '')
'''

lock = threading.Lock()
cache = {'key': None, 'tracks': None}


def game():
    d = ms.find_game()
    return (d, ms.game_paths(d)) if d else (None, None)


def run(args):
    d, _ = game()
    return subprocess.run([sys.executable, TOOL, '--game', d] + args, capture_output=True, text=True,
                          creationflags=ms.NO_WINDOW)


def game_open(exe):
    r = subprocess.run(['tasklist', '/FI', 'IMAGENAME eq ' + exe, '/NH'], capture_output=True, text=True,
                       creationflags=ms.NO_WINDOW)
    return exe.lower() in r.stdout.lower()


def read_history():
    try:
        return json.load(open(HISTORY, encoding='utf8'))
    except (OSError, ValueError):
        return {}


def write_history(h):
    json.dump(h, open(HISTORY, 'w', encoding='utf8'), ensure_ascii=False, indent=1)


def tracks(bank):
    st = os.stat(bank); key = (st.st_size, st.st_mtime)
    if cache['key'] != key:
        r = run(['list', '--json'])
        if r.returncode: raise RuntimeError(r.stderr[-1500:])
        cache.update(key=key, tracks=json.loads(r.stdout))
    return cache['tracks']


def copy(src, dst):
    os.makedirs(os.path.dirname(dst), exist_ok=True)
    shutil.copyfile(src, dst + '.part'); os.replace(dst + '.part', dst)


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a): pass

    def reply(self, code, obj, ctype='application/json'):
        b = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
        self.send_response(code); self.send_header('Content-Type', ctype + '; charset=utf-8')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        u = urllib.parse.urlparse(self.path); q = urllib.parse.parse_qs(u.query)
        if u.path == '/':
            return self.reply(200, open(os.path.join(HERE, 'index.html'), 'rb').read(), 'text/html')
        if u.path == '/api/state':
            d, g = game()
            setup = {'game': bool(d), 'ffmpeg': bool(ms.find_ffmpeg())}
            if not (setup['game'] and setup['ffmpeg']):
                return self.reply(200, {'setup': setup})
            try:
                if not os.path.exists(FIRST): copy(g['bank'], FIRST)
                return self.reply(200, {'setup': setup, 'tracks': tracks(g['bank']), 'history': read_history(),
                                        'game_open': game_open(g['exe']), 'has_undo': os.path.exists(UNDO),
                                        'bank_mb': round(os.path.getsize(g['bank']) / 2**20),
                                        'ver': int(os.path.getmtime(g['bank'])), 'game_dir': os.path.dirname(d)})
            except Exception as e:
                return self.reply(500, {'error': str(e)})
        if u.path == '/api/listen':
            p = os.path.join(MUSIC, os.path.basename(q.get('m', [''])[0]))
            if not os.path.isfile(p): return self.reply(404, {'error': 'not_found'})
            return self.reply(200, open(p, 'rb').read(), 'audio/mpeg')
        if u.path == '/api/current':
            d, g = game()
            track = q.get('t', [''])[0]
            if not d or track not in {t['track'] for t in tracks(g['bank'])}: return self.reply(404, {'error': 'not_found'})
            st = os.stat(g['bank'])
            p = os.path.join(CACHE, '%s-%d-%d.mp3' % (track, st.st_size, int(st.st_mtime)))
            if not os.path.exists(p):
                os.makedirs(CACHE, exist_ok=True)
                for old in os.listdir(CACHE):
                    if old.startswith(track + '-'): os.remove(os.path.join(CACHE, old))
                r = run(['extract', track, p])
                if r.returncode or not os.path.exists(p): return self.reply(500, {'error': (r.stdout + r.stderr)[-800:]})
            return self.reply(200, open(p, 'rb').read(), 'audio/mpeg')
        self.reply(404, {'error': 'not_found'})

    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        body = self.rfile.read(int(self.headers.get('Content-Length', 0)))
        if u.path == '/api/upload':
            name = os.path.basename(urllib.parse.parse_qs(u.query)['name'][0])
            os.makedirs(MUSIC, exist_ok=True)
            open(os.path.join(MUSIC, name), 'wb').write(body)
            return self.reply(200, {'music': name})
        data = json.loads(body or b'{}')
        if u.path == '/api/browse':
            r = subprocess.run([sys.executable, '-c', BROWSE], capture_output=True, text=True, creationflags=ms.NO_WINDOW)
            path = r.stdout.strip()
            if r.returncode: return self.reply(200, {'error': 'no_dialog'})
            if not path: return self.reply(200, {'ok': False})
            data = {'game_dir': path}
        if u.path in ('/api/config', '/api/browse'):
            cfg = ms.load_config()
            if data.get('game_dir'):
                d = ms.data_dir_of(data['game_dir'].strip().strip('"'))
                if not d: return self.reply(200, {'error': 'bad_game'})
                cfg['game_dir'] = os.path.dirname(d)
                cache['key'] = None
            if data.get('ffmpeg'): cfg['ffmpeg'] = data['ffmpeg'].strip().strip('"')
            ms.save_config(cfg)
            return self.reply(200, {'ok': True})
        if not lock.acquire(blocking=False):
            return self.reply(409, {'error': 'busy'})
        try:
            d, g = game()
            if not d: return self.reply(409, {'error': 'no_game'})
            if game_open(g['exe']): return self.reply(409, {'error': 'game_open'})
            bank = g['bank']; new = bank + '.new'
            if u.path in ('/api/swap', '/api/restore_track'):
                track = data['track']
                if u.path == '/api/swap':
                    music = os.path.join(MUSIC, os.path.basename(data['music']))
                    args = ['swap', track, music, new]
                    if data.get('loop'): args += ['--loop-start', str(float(data['loop']))]
                else:
                    args = ['restore', track, FIRST, new]
                r = run(args)
                log = (r.stdout + r.stderr).strip()
                if r.returncode or not os.path.exists(new):
                    if os.path.exists(new): os.remove(new)
                    return self.reply(200, {'ok': False, 'log': log})
                copy(bank, UNDO)
                os.replace(new, bank)
                h = read_history()
                if u.path == '/api/swap':
                    h[track] = {'music': os.path.basename(music), 'when': time.strftime('%Y-%m-%d %H:%M')}
                else:
                    h.pop(track, None)
                write_history(h)
                return self.reply(200, {'ok': True, 'log': log})
            if u.path == '/api/restore':
                src = {'undo': UNDO, 'all': FIRST}[data['what']]
                if not os.path.exists(src): return self.reply(404, {'error': 'missing', 'file': src})
                if data['what'] == 'all': copy(bank, UNDO)
                copy(src, bank)
                h = read_history()
                if data['what'] == 'all': h = {}
                elif h: h.pop(max(h, key=lambda k: h[k]['when']))
                write_history(h)
                return self.reply(200, {'ok': True, 'log': ''})
            self.reply(404, {'error': 'not_found'})
        except Exception as e:
            self.reply(500, {'error': repr(e)})
        finally:
            lock.release()


def open_browser(url):
    for exe in (shutil.which('chrome'), os.path.expandvars(r'%ProgramFiles%\Google\Chrome\Application\chrome.exe'),
                os.path.expandvars(r'%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe'),
                os.path.expandvars(r'%LocalAppData%\Google\Chrome\Application\chrome.exe')):
        if exe and os.path.isfile(exe):
            subprocess.Popen([exe, url]); return
    webbrowser.open(url)


if __name__ == '__main__':
    srv = ThreadingHTTPServer(('127.0.0.1', PORT), Handler)
    print('Music Swapper running at http://127.0.0.1:%d  (close this window to stop it)' % PORT)
    if '--no-browser' not in sys.argv: threading.Timer(0.8, open_browser, ['http://127.0.0.1:%d' % PORT]).start()
    srv.serve_forever()
