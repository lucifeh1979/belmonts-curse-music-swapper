import os, sys, re, glob, json, struct, zlib, subprocess, tempfile, shutil, argparse, ctypes as C

NO_WINDOW = 0x08000000
HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(HERE, 'config.json')
TL = 48000
RATES = {1: 8000, 2: 11000, 3: 11025, 4: 16000, 5: 22050, 6: 24000, 7: 32000, 8: 44100, 9: 48000}
GAME_GLOB = "*Belmont*Curse*"


def load_config():
    try:
        return json.load(open(CONFIG, encoding='utf8'))
    except (OSError, ValueError):
        return {}


def save_config(cfg):
    json.dump(cfg, open(CONFIG, 'w', encoding='utf8'), indent=1)


def data_dir_of(game_dir):
    if not game_dir or not os.path.isdir(game_dir): return None
    if os.path.isfile(os.path.join(game_dir, 'StreamingAssets', 'Master.bank')): return game_dir
    for d in glob.glob(os.path.join(game_dir, '*_Data')):
        if os.path.isfile(os.path.join(d, 'StreamingAssets', 'Master.bank')): return d
    return None


def steam_libraries():
    libs = []
    try:
        import winreg
        k = winreg.OpenKey(winreg.HKEY_CURRENT_USER, r'Software\Valve\Steam')
        steam = winreg.QueryValueEx(k, 'SteamPath')[0]
        libs.append(steam)
        vdf = open(os.path.join(steam, 'steamapps', 'libraryfolders.vdf'), encoding='utf8', errors='ignore').read()
        libs += [p.replace('\\\\', '\\') for p in re.findall(r'"path"\s+"([^"]+)"', vdf)]
    except OSError:
        pass
    return libs


def find_game():
    cfg = load_config()
    if data_dir_of(cfg.get('game_dir')): return data_dir_of(cfg['game_dir'])
    cands = []
    for lib in steam_libraries():
        cands += glob.glob(os.path.join(lib, 'steamapps', 'common', GAME_GLOB))
    for drive in 'CDEFGHIJKLMNOPQRSTUVWXYZ':
        root = drive + ':\\'
        if not os.path.exists(root): continue
        for pat in (GAME_GLOB, os.path.join('Games', GAME_GLOB), os.path.join('Program Files*', GAME_GLOB),
                    os.path.join('SteamLibrary', 'steamapps', 'common', GAME_GLOB),
                    os.path.join('Program Files*', 'Steam', 'steamapps', 'common', GAME_GLOB)):
            cands += glob.glob(os.path.join(root, pat))
    for c in cands:
        d = data_dir_of(c)
        if d:
            cfg['game_dir'] = os.path.dirname(d); save_config(cfg)
            return d
    return None


def find_ffmpeg():
    cfg = load_config()
    for c in (os.path.join(HERE, 'ffmpeg', 'ffmpeg.exe'), cfg.get('ffmpeg'), os.path.join(HERE, 'ffmpeg.exe'),
              shutil.which('ffmpeg'), r'C:\ffmpeg\bin\ffmpeg.exe'):
        if c and os.path.isfile(c): return c
    return None


def game_paths(data_dir):
    return {
        'bank': os.path.join(data_dir, 'StreamingAssets', 'Master.bank'),
        'strings': os.path.join(data_dir, 'StreamingAssets', 'Master.strings.bank'),
        'plugins': os.path.join(data_dir, 'Plugins', 'x86_64'),
        'exe': os.path.basename(data_dir)[:-len('_Data')] + '.exe',
    }


def fmod_version(dll):
    v = C.windll.version
    size = v.GetFileVersionInfoSizeW(dll, None)
    if size:
        buf = C.create_string_buffer(size)
        if v.GetFileVersionInfoW(dll, 0, size, buf):
            p = C.c_void_p(); n = C.c_uint()
            if v.VerQueryValueW(buf, '\\', C.byref(p), C.byref(n)):
                ms, ls = struct.unpack_from('<II', C.string_at(p.value, 16), 8)
                parts = [ms >> 16, ms & 0xFFFF, ls >> 16, ls & 0xFFFF]
                while len(parts) > 3 and parts[0] == 0: parts.pop(0)
                return (parts[0] << 16) | (parts[1] << 8) | parts[2]
    return 0x00020309


def load_fmod(plugins):
    os.add_dll_directory(plugins)
    dll = os.path.join(plugins, 'fmodstudio.dll')
    return C.CDLL(dll), fmod_version(dll)


def riff_children(b, start, end):
    p = start
    while p + 8 <= end:
        cid, sz = struct.unpack_from('<4sI', b, p)
        yield cid, p, sz
        p += 8 + sz + (sz & 1)


class Fsb:
    def __init__(self, f, off):
        f.seek(off)
        self.h = bytearray(f.read(0x3c))
        if self.h[:4] != b'FSB5': raise SystemExit('FSB5 not found')
        ver, self.ns, shs, nts, self.ds, self.mode = struct.unpack_from('<IIIIII', self.h, 4)
        if self.mode != 15: raise SystemExit('only Vorbis (mode 15) is supported')
        sh = f.read(shs)
        self.nt = f.read(nts)
        self.dstart = off + 0x3c + shs + nts
        self.hd = []
        pos = 0
        for _ in range(self.ns):
            v = struct.unpack_from('<Q', sh, pos)[0]; pos += 8; n = v & 1; ch = []
            while n:
                c = struct.unpack_from('<I', sh, pos)[0]; pos += 4; n = c & 1; sz = (c >> 1) & 0xFFFFFF
                ch.append([c >> 25, sh[pos:pos + sz]]); pos += sz
            self.hd.append([v, ch])
        self.offs = [((v >> 7) & 0x7FFFFFF) * 32 for v, _ in self.hd] + [self.ds]
        no = struct.unpack_from('<%dI' % self.ns, self.nt, 0)
        self.names = [self.nt[o:self.nt.index(b'\0', o)].decode() for o in no]

    def info(self, i):
        v = self.hd[i][0]
        return RATES[(v >> 1) & 0xF], [1, 2, 6, 8][(v >> 5) & 3], v >> 34

    def index(self, track):
        track = track.removesuffix('.wav')
        if track not in self.names: raise SystemExit('track not found: ' + track)
        return self.names.index(track)


class Bank:
    def __init__(self, path):
        self.path = path
        self.f = open(path, 'rb')
        meta = bytearray(self.f.read(64 << 20))
        self.sndh = meta.find(b'SNDH')
        self.fsb_off = struct.unpack_from('<I', meta, self.sndh + 12)[0]
        self.snd = meta.rfind(b'SND ', 0, self.fsb_off)
        self.proj = meta.find(b'PROJ') - 8
        self.meta = meta[:self.fsb_off]
        self.fsb = Fsb(self.f, self.fsb_off)

    def close(self):
        self.f.close()

    def write(self, out, idx, new_header, blob, patches):
        fsb = self.fsb
        hd = [list(x) for x in fsb.hd]
        hd[idx] = new_header
        delta = len(blob) - (fsb.offs[idx + 1] - fsb.offs[idx])
        for k in range(idx + 1, fsb.ns):
            o = fsb.offs[k] + delta
            hd[k][0] = (hd[k][0] & ~(0x7FFFFFF << 7)) | ((o // 32) << 7)
        sh = bytearray()
        for v, chs in hd:
            sh += struct.pack('<Q', v)
            for j, (t, d) in enumerate(chs):
                sh += struct.pack('<I', (t << 25) | (len(d) << 1) | (1 if j < len(chs) - 1 else 0)) + d
        nt = fsb.nt.rstrip(b'\0') + b'\0'
        while (0x3c + len(sh) + len(nt)) % 32: nt += b'\0'
        ds = fsb.ds + delta
        h = bytearray(fsb.h)
        struct.pack_into('<IIIII', h, 8, fsb.ns, len(sh), len(nt), ds, fsb.mode)
        size = 0x3c + len(sh) + len(nt) + ds
        m = bytearray(self.meta)
        for off, val in patches: struct.pack_into('<I', m, off, val)
        struct.pack_into('<I', m, self.sndh + 16, size)
        struct.pack_into('<I', m, self.snd + 4, (self.fsb_off - self.snd - 8) + size)
        struct.pack_into('<I', m, 4, self.fsb_off + size - 8)
        with open(out, 'wb') as w:
            w.write(m); w.write(h); w.write(sh); w.write(nt)
            self.f.seek(fsb.dstart); w.write(self.f.read(fsb.offs[idx])); w.write(blob)
            self.f.seek(fsb.dstart + fsb.offs[idx + 1])
            while True:
                b = self.f.read(1 << 24)
                if not b: break
                w.write(b)


def ogg_packets(path):
    d = open(path, 'rb').read(); i = 0; cur = b''; out = []
    while i < len(d):
        if d[i:i + 4] != b'OggS': raise SystemExit('bad ogg stream')
        gran = struct.unpack_from('<q', d, i + 6)[0]; nseg = d[i + 26]
        segs = d[i + 27:i + 27 + nseg]; p = i + 27 + nseg
        for s in segs:
            cur += d[p:p + s]; p += s
            if s < 255: out.append((cur, gran)); cur = b''
        i = p
    return out


def encode(ffmpeg, audio, rate, chans, crc, tmp):
    for q in (9, 10, 8, 7, 6, 5, 4, 3, 2, 1, 0):
        t = os.path.join(tmp, 'q.ogg')
        subprocess.run([ffmpeg, '-v', 'error', '-y', '-t', '2', '-i', audio, '-ar', str(rate), '-ac', str(chans),
                        '-c:a', 'libvorbis', '-q:a', str(q), t], check=True, creationflags=NO_WINDOW)
        if zlib.crc32(ogg_packets(t)[2][0]) == crc:
            out = os.path.join(tmp, 'new.ogg')
            subprocess.run([ffmpeg, '-v', 'error', '-y', '-i', audio, '-ar', str(rate), '-ac', str(chans),
                            '-c:a', 'libvorbis', '-q:a', str(q), out], check=True, creationflags=NO_WINDOW)
            return out, q
    raise SystemExit('no libvorbis quality produces the setup header FMOD expects (CRC %08x)' % crc)


def vorbis_blob(ogg, rate):
    pk = ogg_packets(ogg)
    n = pk[-1][1]
    audio = [p for p, _ in pk[3:]]
    blob = b''.join(struct.pack('<H', len(p)) + p for p in audio)
    blob += b'\0' * (-len(blob) % 32)
    g = 0; prev = None; starts = []; o = 0
    for p in audio:
        starts.append((g, o)); cur = 2048 if (p[0] >> 1) & 1 else 256
        if prev is not None: g += prev // 4 + cur // 4
        prev = cur; o += 2 + len(p)
    seek = []
    for k in range(1, -(-n // rate) + 1):
        t = min(k * rate, n)
        i = max(j for j, x in enumerate(starts) if x[0] < t)
        seek.append((starts[i][0], starts[i - 1][1]))
    return n, blob, b''.join(struct.pack('<II', *e) for e in seek)


def find_all(b, pat, lo=0, hi=None):
    hi = len(b) if hi is None else hi; r = []; j = b.find(pat, lo, hi)
    while j != -1:
        r.append(j); j = b.find(pat, j + 1, hi)
    return r


def timeline_patches(meta, proj, idx, old_end_hint, new_end, loop_start):
    lists = {}
    for cid, p, sz in riff_children(meta, proj + 12, proj + 8 + struct.unpack_from('<I', meta, proj + 4)[0]):
        if cid == b'LIST': lists[bytes(meta[p + 8:p + 12])] = (p, sz)
    wp, wsz = lists[b'WAVS']
    wave = None
    for cid, p, sz in riff_children(meta, wp + 12, wp + 8 + wsz):
        if cid == b'WAV ' and sz == 0x1e and struct.unpack_from('<I', meta, p + 8 + 22)[0] == idx:
            wave = bytes(meta[p + 8:p + 24])
    if not wave: raise SystemExit('waveform for sample %d not found in WAVS' % idx)
    ip, isz = lists[b'WAIS']
    insts = [bytes(meta[j - 16:j]) for j in find_all(meta, wave, ip, ip + 8 + isz)]
    if not insts: raise SystemExit('instrument for the waveform not found in WAIS')
    tp, tsz = lists[b'TLNS']
    out = []; found = 0
    for cid, p, sz in riff_children(meta, tp + 12, tp + 8 + tsz):
        lo, hi = p, p + 8 + sz
        for inst in insts:
            for j in find_all(meta, inst, lo, hi):
                st, ln = struct.unpack_from('<II', meta, j + 16)
                old_end = st + ln
                if abs(old_end - old_end_hint) > TL // 2:
                    continue
                found += 1
                d = new_end - old_end_hint
                ne = old_end + d
                out.append((j + 20, ln + d))
                for r in find_all(meta, b'\x02\x00\x1a\x00', lo, hi):
                    rs = struct.unpack_from('<I', meta, r + 20)[0]; rl = struct.unpack_from('<I', meta, r + 26)[0]
                    if rs + rl == old_end:
                        out += [(r + 20, loop_start), (r + 26, ne - loop_start)]
                for t in find_all(meta, b'TRNB', lo, hi):
                    for k in (32, 36):
                        v = struct.unpack_from('<I', meta, t + 8 + k)[0]
                        if old_end - 10 * TL <= v <= old_end:
                            out.append((t + 8 + k, ne - (old_end - v)))
    if not found:
        raise SystemExit('event timeline not found: this track is played in pieces (common for bosses);\n'
                         'only single-instrument tracks that run to the end can change length')
    return out


class Studio:
    def __init__(self, game, bank):
        g = game_paths(game)
        self.S, ver = load_fmod(g['plugins'])
        S = self.S
        self.sp = C.c_void_p(); self.ck(S.FMOD_Studio_System_Create(C.byref(self.sp), ver))
        core = C.c_void_p(); S.FMOD_Studio_System_GetCoreSystem(self.sp, C.byref(core))
        self.ck(S.FMOD_System_SetOutput(core, 4))
        self.ck(S.FMOD_Studio_System_Initialize(self.sp, 64, 2, 0, None))
        for b in (g['strings'], bank):
            h = C.c_void_p(); self.ck(S.FMOD_Studio_System_LoadBankFile(self.sp, b.encode(), 0, C.byref(h)))
        self.bank = h

    @staticmethod
    def ck(r):
        if r: raise RuntimeError('FMOD error %d' % r)

    def lengths(self):
        S = self.S; n = C.c_int(); S.FMOD_Studio_Bank_GetEventCount(self.bank, C.byref(n))
        arr = (C.c_void_p * n.value)(); got = C.c_int()
        S.FMOD_Studio_Bank_GetEventList(self.bank, arr, n, C.byref(got))
        buf = C.create_string_buffer(512); rl = C.c_int(); out = {}
        for e in arr[:got.value]:
            S.FMOD_Studio_EventDescription_GetPath(C.c_void_p(e), buf, 512, C.byref(rl))
            L = C.c_int(); S.FMOD_Studio_EventDescription_GetLength(C.c_void_p(e), C.byref(L))
            out[buf.value.decode()] = L.value
        return out

    def trace(self, event, start_ms, ms):
        S = self.S; ev = C.c_void_p(); self.ck(S.FMOD_Studio_System_GetEvent(self.sp, event.encode(), C.byref(ev)))
        ins = C.c_void_p(); S.FMOD_Studio_EventDescription_CreateInstance(ev, C.byref(ins))
        S.FMOD_Studio_EventInstance_Start(ins); S.FMOD_Studio_System_Update(self.sp)
        S.FMOD_Studio_EventInstance_SetTimelinePosition(ins, start_ms)
        pos = C.c_int(); pts = []
        for _ in range(int(ms / 10.6) + 1):
            S.FMOD_Studio_System_Update(self.sp)
            S.FMOD_Studio_EventInstance_GetTimelinePosition(ins, C.byref(pos)); pts.append(pos.value)
        S.FMOD_Studio_EventInstance_Release(ins)
        return pts


def isolated(game, args):
    r = subprocess.run([sys.executable, os.path.abspath(__file__), '--game', game, '_fmod'] + args,
                       capture_output=True, text=True, creationflags=NO_WINDOW)
    if r.returncode: raise SystemExit('FMOD test failed:\n' + r.stderr[-2000:])
    return json.loads(r.stdout.strip().splitlines()[-1])


def check_events(game, test, before, n, old_n, rate):
    after = isolated(game, ['len', test])
    changed = {e: (before[e], after[e]) for e in before if before[e] != after.get(e)}
    if not changed and n != old_n: raise SystemExit('no event changed length')
    for e, (x, y) in changed.items(): print('  %s: %.1f s -> %.1f s' % (e, x / 1000, y / 1000))
    if not changed: return
    target = [e for e, (x, y) in changed.items() if abs((y - x) - (n - old_n) * 1000 / rate) < 1000]
    if len(target) != 1 or len(changed) != 1: raise SystemExit('too many or too few events changed')
    ev = target[0]; old_ms = before[ev]; new_ms = after[ev]
    if new_ms > old_ms + 3000:
        pts = isolated(game, ['trace', test, ev, str(old_ms - 3000), '6000'])
        if min(pts[25:]) < old_ms - 3500: raise SystemExit('the music still loops at the old end')
        print('  ok: plays past the old end (%.1f s) without looping' % (old_ms / 1000))
    pts = isolated(game, ['trace', test, ev, str(max(0, new_ms - 4000)), '6000'])
    if min(pts[25:]) < new_ms - 5000: print('  ok: at the end it loops back to %.1f s' % (min(pts[25:]) / 1000))
    else: print('  warning: did not see the loop come back at the end')


def cmd_list(a):
    b = Bank(a.bank); fsb = b.fsb; out = []
    for i, n in enumerate(fsb.names):
        if n.startswith('Mus_') or 'Arrangement' in n:
            r, c, s = fsb.info(i)
            old = round(s * TL / r)
            try:
                timeline_patches(b.meta, b.proj, i, old, old, 0); free = True
            except SystemExit:
                free = False
            out.append({'track': n, 'seconds': round(s / r, 1), 'any_length': free})
    b.close()
    if a.json: print(json.dumps(out))
    else:
        for x in out: print('%-55s %5.0f s %s' % (x['track'], x['seconds'], '' if x['any_length'] else '(same length or shorter only)'))


def cmd_swap(a):
    ffmpeg = find_ffmpeg()
    if not ffmpeg: raise SystemExit('ffmpeg not found')
    b = Bank(a.bank); fsb = b.fsb
    idx = fsb.index(a.track)
    rate, chans, old_n = fsb.info(idx)
    v, chunks = fsb.hd[idx]
    vc = [list(c) for c in chunks if c[0] == 11]
    if not vc: raise SystemExit('sample has no Vorbis data')
    crc = struct.unpack_from('<I', vc[0][1], 0)[0]
    tmp = tempfile.mkdtemp()
    ogg, q = encode(ffmpeg, a.music, rate, chans, crc, tmp)
    n, blob, seek = vorbis_blob(ogg, rate)
    print('track %s (#%d) %d Hz %d ch: %.1f s -> %.1f s (libvorbis q%d)' % (a.track, idx, rate, chans, old_n / rate, n / rate, q))
    chunks = [[t, d[:4] + struct.pack('<I', len(seek)) + seek] if t == 11 else [t, d] for t, d in chunks]
    header = [(v & ((1 << 34) - 1)) | (n << 34), chunks]
    old_end = round(old_n * TL / rate); new_end = round(n * TL / rate)
    loop_start = round(a.loop_start * TL)
    if loop_start >= new_end: raise SystemExit('--loop-start is past the end of the music')
    try:
        patches = timeline_patches(b.meta, b.proj, idx, old_end, new_end, loop_start)
    except SystemExit as e:
        if n > old_n: raise SystemExit('%s\nthis track only accepts music up to %.1f s' % (e, old_n / rate))
        patches = None
    test = os.path.join(tmp, 'test.bank')
    before = isolated(a.game, ['len', a.bank])
    if patches is not None:
        b.write(test, idx, header, blob, patches)
        print('%d timeline fields adjusted' % len(patches))
        try:
            check_events(a.game, test, before, n, old_n, rate)
        except SystemExit as e:
            if n > old_n: raise
            print('  %s; replacing the audio only (music is shorter than the original)' % e)
            patches = None
    if patches is None:
        b.write(test, idx, header, blob, [])
        if isolated(a.game, ['len', test]) != before: raise SystemExit('the bank changed unexpectedly; nothing written')
        if n < old_n: print('  warning: the music is %.1f s and the event %.1f s; silence until the game loops' % (n / rate, old_n / rate))
        print('  ok: audio replaced, event untouched')
    b.close()
    shutil.move(test, a.out)
    shutil.rmtree(tmp, ignore_errors=True)
    print('written:', a.out)


def cmd_restore(a):
    b = Bank(a.bank); o = Bank(a.source)
    idx = b.fsb.index(a.track); io = o.fsb.index(a.track)
    if len(b.meta) != len(o.meta): raise SystemExit('the two banks have different layouts; cannot restore')
    r, c, n = b.fsb.info(idx); ro, co, no = o.fsb.info(io)

    def fields(bank, i, end):
        try: return sorted(off for off, _ in timeline_patches(bank.meta, bank.proj, i, end, end, 0))
        except SystemExit: return []
    fa = fields(b, idx, round(n * TL / r))
    if fa != fields(o, io, round(no * TL / ro)): raise SystemExit('timeline fields differ between the banks; cannot restore')
    patches = [(off, struct.unpack_from('<I', o.meta, off)[0]) for off in fa]
    o.f.seek(o.fsb.dstart + o.fsb.offs[io]); blob = o.f.read(o.fsb.offs[io + 1] - o.fsb.offs[io])
    blob += b'\0' * (-len(blob) % 32)
    v = (o.fsb.hd[io][0] & ~(0x7FFFFFF << 7)) | (b.fsb.offs[idx] // 32 << 7)
    tmp = tempfile.mkdtemp(); test = os.path.join(tmp, 'test.bank')
    b.write(test, idx, [v, [list(x) for x in o.fsb.hd[io][1]]], blob, patches)
    b.close(); o.close()
    print('track %s: %.1f s -> %.1f s (restored, %d timeline fields)' % (a.track, n / r, no / ro, len(fa)))
    before = isolated(a.game, ['len', a.bank]); after = isolated(a.game, ['len', test]); ref = isolated(a.game, ['len', a.source])
    for e in before:
        if before[e] != after.get(e):
            if after.get(e) != ref.get(e): raise SystemExit('event %s did not go back to the original length; nothing written' % e)
            print('  %s: %.1f s -> %.1f s' % (e, before[e] / 1000, after[e] / 1000))
    shutil.move(test, a.out); shutil.rmtree(tmp, ignore_errors=True)
    print('written:', a.out)


def cmd_extract(a):
    ffmpeg = find_ffmpeg()
    if not ffmpeg: raise SystemExit('ffmpeg not found')
    b = Bank(a.bank); fsb = b.fsb; idx = fsb.index(a.track)
    v, chunks = fsb.hd[idx]
    hd = struct.pack('<Q', v & ~(0x7FFFFFF << 7))
    for j, (t, d) in enumerate(chunks):
        hd += struct.pack('<I', (t << 25) | (len(d) << 1) | (1 if j < len(chunks) - 1 else 0)) + d
    b.f.seek(fsb.dstart + fsb.offs[idx]); data = b.f.read(fsb.offs[idx + 1] - fsb.offs[idx])
    b.close()
    names = struct.pack('<I', 4) + b'x\0\0\0'
    h = bytearray(fsb.h); struct.pack_into('<IIIII', h, 8, 1, len(hd), len(names), len(data), fsb.mode)
    tmp = tempfile.mkdtemp(); one = os.path.join(tmp, 'one.fsb'); pcm = os.path.join(tmp, 'pcm.raw')
    open(one, 'wb').write(bytes(h) + hd + names + data)
    S, ver = load_fmod(game_paths(a.game)['plugins'])

    def ck(r):
        if r: raise SystemExit('FMOD error %d' % r)
    sy = C.c_void_p(); ck(S.FMOD_System_Create(C.byref(sy), ver))
    ck(S.FMOD_System_SetOutput(sy, 2)); ck(S.FMOD_System_Init(sy, 8, 0, None))
    snd = C.c_void_p(); ck(S.FMOD_System_CreateSound(sy, one.encode(), 0, None, C.byref(snd)))
    sub = C.c_void_p(); ck(S.FMOD_Sound_GetSubSound(snd, 0, C.byref(sub)))
    typ, fmt, ch, bits = C.c_int(), C.c_int(), C.c_int(), C.c_int()
    ck(S.FMOD_Sound_GetFormat(sub, C.byref(typ), C.byref(fmt), C.byref(ch), C.byref(bits)))
    freq, pri = C.c_float(), C.c_int(); ck(S.FMOD_Sound_GetDefaults(sub, C.byref(freq), C.byref(pri)))
    size = C.c_uint(); ck(S.FMOD_Sound_GetLength(sub, C.byref(size), 4))
    p1, p2, l1, l2 = C.c_void_p(), C.c_void_p(), C.c_uint(), C.c_uint()
    ck(S.FMOD_Sound_Lock(sub, 0, size, C.byref(p1), C.byref(p2), C.byref(l1), C.byref(l2)))
    open(pcm, 'wb').write(C.string_at(p1, l1.value) + (C.string_at(p2, l2.value) if l2.value else b''))
    S.FMOD_Sound_Unlock(sub, p1, p2, l1, l2)
    S.FMOD_Sound_Release(snd); S.FMOD_System_Release(sy)
    f = {2: 's16le', 4: 's32le', 5: 'f32le'}.get(fmt.value)
    if not f: raise SystemExit('unexpected FMOD sample format %d' % fmt.value)
    subprocess.run([ffmpeg, '-v', 'error', '-y', '-f', f, '-ar', str(int(freq.value)), '-ac', str(ch.value), '-i', pcm,
                    '-b:a', '160k', a.out], check=True, creationflags=NO_WINDOW)
    shutil.rmtree(tmp, ignore_errors=True)
    print('written:', a.out)


def cmd_fmod(a):
    st = Studio(a.game, a.bank)
    if a.kind == 'len': print(json.dumps(st.lengths()))
    else: print(json.dumps(st.trace(a.event, a.start_ms, a.ms)))


def main():
    ap = argparse.ArgumentParser(description="Swap music in Castlevania: Belmont's Curse (FMOD Master.bank)")
    ap.add_argument('--game', help='game folder (default: auto-detect)')
    sp = ap.add_subparsers(dest='cmd', required=True)
    p = sp.add_parser('list', help='list music tracks'); p.add_argument('bank', nargs='?'); p.add_argument('--json', action='store_true')
    p = sp.add_parser('swap', help='replace a track with a music file')
    p.add_argument('track'); p.add_argument('music'); p.add_argument('out'); p.add_argument('--bank')
    p.add_argument('--loop-start', type=float, default=0.0, help='where the loop restarts, in seconds (default 0)')
    p = sp.add_parser('restore', help='put a track back the way it is in another bank')
    p.add_argument('track'); p.add_argument('source'); p.add_argument('out'); p.add_argument('--bank')
    p = sp.add_parser('extract', help='save the current music of a track as mp3')
    p.add_argument('track'); p.add_argument('out'); p.add_argument('--bank')
    p = sp.add_parser('_fmod'); p.add_argument('kind'); p.add_argument('bank')
    p.add_argument('event', nargs='?'); p.add_argument('start_ms', nargs='?', type=int); p.add_argument('ms', nargs='?', type=int)
    a = ap.parse_args()
    a.game = data_dir_of(a.game) if a.game else find_game()
    if not a.game: raise SystemExit("game not found; pass --game \"<game folder>\"")
    if getattr(a, 'bank', None) is None: a.bank = game_paths(a.game)['bank']
    {'list': cmd_list, 'swap': cmd_swap, 'restore': cmd_restore, 'extract': cmd_extract, '_fmod': cmd_fmod}[a.cmd](a)


if __name__ == '__main__':
    main()
