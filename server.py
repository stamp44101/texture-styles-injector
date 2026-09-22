#!/usr/bin/env python3
"""
Texture Styles injector — local web UI.

Runs ONLY on this Mac: the pipeline needs Apple's Vision.framework (via the
Swift helper), ffmpeg and sips. Nothing is uploaded anywhere; files are
processed in a temp dir and deleted when the download is served.

  python3 server.py         # then open http://127.0.0.1:8765
"""
import html, io, json, os, shutil, struct, sys, tempfile, threading, time, traceback, uuid
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from urllib.parse import parse_qs, urlparse

HERE = os.path.dirname(os.path.abspath(__file__))
TOOLS = os.path.join(HERE, 'tools')
sys.path.insert(0, TOOLS)

import inject_mattes
from copy_styles_item import Heif, find_uri_item
from copy_mattes import aux_urn, AUX_2026, TEX_URI

JOBS = {}          # id -> dict(state, log[], out, name, err)
JOBS_LOCK = threading.Lock()
MAX_BYTES = 80 * 1024 * 1024


# ----------------------------------------------------------------- analysis
def analyse(path):
    """Inspect an uploaded file and report whether it can be processed."""
    info = {'ok': False, 'problems': [], 'notes': [], 'facts': {}}
    try:
        H = Heif(path)
    except Exception as e:
        info['problems'].append(
            "ไม่ใช่ไฟล์ HEIF/HEIC ที่อ่านได้ — JPEG และ PNG ใช้ไม่ได้ "
            "เพราะไม่มีโครงสร้าง item ให้เขียน matte ลงไป")
        return info

    prim = H.primary()
    if prim is None:
        info['problems'].append("ไฟล์ไม่มี primary item (pitm)")
        return info
    ptype = H.items[prim]['type']
    info['facts']['primary_type'] = ptype
    if ptype != 'grid':
        info['problems'].append(
            f"primary item เป็น '{ptype}' ไม่ใช่ 'grid' — เครื่องมือยังรองรับ "
            "เฉพาะภาพแบบ grid (ซึ่งเป็นแบบที่กล้อง iPhone เขียน)")
        return info

    dims = H.grid_dims(prim)
    info['facts']['dimensions'] = f"{dims[0]}x{dims[1]}"
    info['facts']['aspect'] = round(dims[0] / dims[1], 4)
    info['facts']['items'] = len(H.items)

    # camera-original test: grid geometry vs EXIF
    exif_dims = None
    model = None
    try:
        from patch_exif import parse_ifd
        eids = [i for i, v in H.items.items() if v['type'] == 'Exif']
        if eids:
            e = H.item_bytes(eids[0]); sk = struct.unpack('>I', e[:4])[0]
            t = e[4 + sk:]
            bo = '>' if t[:2] == b'MM' else '<'
            ents, _ = parse_ifd(t, struct.unpack(bo + 'I', t[4:8])[0], bo)
            d = {x[0]: x for x in ents}
            if 0x0110 in d:
                model = d[0x0110][3].rstrip(b'\x00').decode('latin-1', 'replace')
            if 0x8769 in d:
                sub, _ = parse_ifd(t, struct.unpack(bo + 'I', d[0x8769][3])[0], bo)
                sd = {x[0]: x for x in sub}
                if 0xa002 in sd and 0xa003 in sd:
                    def num(x):
                        # parse_ifd yields [tag, type, count, raw]
                        typ, raw = x[1], x[3]
                        w = 2 if typ == 3 else 4
                        return struct.unpack(bo + ('H' if typ == 3 else 'I'), raw[:w])[0]
                    exif_dims = (num(sd[0xa002]), num(sd[0xa003]))
    except Exception:
        info['notes'].append("หมายเหตุ: อ่าน EXIF ไม่สำเร็จ ข้ามการตรวจว่าเป็นไฟล์ต้นฉบับ")
    info['facts']['model'] = model or '—'

    if exif_dims:
        info['facts']['exif_dimensions'] = f"{exif_dims[0]}x{exif_dims[1]}"
        # EXIF records the DISPLAYED size; a rotated original stores the axes
        # swapped, so accept either order.
        if tuple(dims) == tuple(exif_dims) or tuple(dims) == tuple(reversed(exif_dims)):
            info['notes'].append(
                "✓ เป็นไฟล์ต้นฉบับจากกล้อง (ขนาด grid ตรงกับ EXIF) — ดีที่สุด")
        else:
            info['notes'].append(
                f"⚠ น่าจะเป็นไฟล์ที่ผ่านการ export/แก้ไขมาแล้ว "
                f"(grid {dims[0]}x{dims[1]} ไม่ตรงกับ EXIF {exif_dims[0]}x{exif_dims[1]}) "
                "— ยังทำงานได้ แต่ไฟล์ที่ไม่ผ่านการแก้ไขจะให้ผลดีกว่า")

    if find_uri_item(H, TEX_URI) is not None:
        info['problems'].append(
            "ไฟล์นี้มี texture_styles item อยู่แล้ว — น่าจะถ่ายด้วย iPhone 18 Pro "
            "อยู่แล้ว ไม่ต้องใช้เครื่องมือนี้")
        return info

    props = list(H.meta.find('iprp').find('ipco').children)
    ip = H.ipma_entries()
    n2026 = sum(1 for _, pl in ip.items() for _, pi in pl
                if pi <= len(props) and props[pi - 1].type == 'auxC'
                and AUX_2026 in aux_urn(H, props[pi - 1]))
    if n2026:
        info['problems'].append(f"ไฟล์นี้มี matte 2026 อยู่แล้ว {n2026} ตัว")
        return info

    mw, mh = inject_mattes.matte_size(*dims)
    info['facts']['matte_size'] = f"{mw}x{mh}"
    info['ok'] = True
    return info


# ----------------------------------------------------------------- job
def run_job(jid, src, name):
    def log(msg):
        with JOBS_LOCK:
            JOBS[jid]['log'].append(str(msg))
    try:
        out = os.path.join(os.path.dirname(src), 'texture_' + name)
        if not out.lower().endswith(('.heic', '.heif')):
            out += '.HEIC'
        log('กำลังวิเคราะห์ภาพ...')
        inject_mattes.main(src, out, verbose=False, log=log)
        with JOBS_LOCK:
            JOBS[jid]['out'] = out
            JOBS[jid]['state'] = 'done'
    except SystemExit as e:
        with JOBS_LOCK:
            JOBS[jid]['state'] = 'error'; JOBS[jid]['err'] = str(e)
    except Exception:
        with JOBS_LOCK:
            JOBS[jid]['state'] = 'error'
            JOBS[jid]['err'] = traceback.format_exc(limit=3)


# ----------------------------------------------------------------- http
class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def _send(self, code, body, ctype='application/json; charset=utf-8', extra=None):
        if isinstance(body, str): body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra or {}).items(): self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a): pass

    def do_GET(self):
        p = urlparse(self.path)
        if p.path == '/':
            return self._send(200, open(os.path.join(HERE, 'static', 'index.html'),
                                        encoding='utf-8').read(),
                              'text/html; charset=utf-8')
        if p.path == '/status':
            jid = parse_qs(p.query).get('id', [''])[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
                if not j: return self._send(404, json.dumps({'error': 'unknown job'}))
                return self._send(200, json.dumps({
                    'state': j['state'], 'log': j['log'], 'err': j.get('err')}))
        if p.path == '/download':
            jid = parse_qs(p.query).get('id', [''])[0]
            with JOBS_LOCK:
                j = JOBS.get(jid)
            if not j or j['state'] != 'done':
                return self._send(404, json.dumps({'error': 'not ready'}))
            data = open(j['out'], 'rb').read()
            fn = os.path.basename(j['out'])
            self._send(200, data, 'image/heic',
                       {'Content-Disposition': f'attachment; filename="{fn}"'})
            shutil.rmtree(os.path.dirname(j['out']), ignore_errors=True)
            with JOBS_LOCK: JOBS.pop(jid, None)
            return
        return self._send(404, json.dumps({'error': 'not found'}))

    def do_POST(self):
        if urlparse(self.path).path != '/upload':
            return self._send(404, json.dumps({'error': 'not found'}))
        ln = int(self.headers.get('Content-Length', 0))
        if ln > MAX_BYTES:
            return self._send(413, json.dumps({'error': 'ไฟล์ใหญ่เกิน 80 MB'}))
        raw = self.rfile.read(ln)
        ctype = self.headers.get('Content-Type', '')
        if 'boundary=' not in ctype:
            return self._send(400, json.dumps({'error': 'bad form'}))
        b = ('--' + ctype.split('boundary=')[1].strip('"')).encode()
        name, payload = 'upload.HEIC', None
        for part in raw.split(b):
            if b'\r\n\r\n' not in part: continue
            head, body = part.split(b'\r\n\r\n', 1)
            hs = head.decode('utf-8', 'replace')
            if 'filename="' in hs:
                fn = hs.split('filename="')[1].split('"')[0]
                if fn: name = os.path.basename(fn)
                payload = body.rstrip(b'\r\n-')
        if not payload:
            return self._send(400, json.dumps({'error': 'ไม่พบไฟล์'}))

        d = tempfile.mkdtemp(prefix='texinj_')
        src = os.path.join(d, name)
        open(src, 'wb').write(payload)

        info = analyse(src)
        if not info['ok']:
            shutil.rmtree(d, ignore_errors=True)
            return self._send(200, json.dumps({'ok': False, **info},
                                              ensure_ascii=False))
        jid = uuid.uuid4().hex[:12]
        with JOBS_LOCK:
            JOBS[jid] = {'state': 'running', 'log': [], 'out': None}
        threading.Thread(target=run_job, args=(jid, src, name), daemon=True).start()
        return self._send(200, json.dumps({'ok': True, 'id': jid, **info},
                                          ensure_ascii=False))


if __name__ == '__main__':
    for tool, hint in ((os.path.join(TOOLS, 'gen', 'genmattes'),
                        'build it: swiftc -O tools/gen/genmattes.swift -o tools/gen/genmattes'),):
        if not os.path.exists(tool):
            print(f"missing {tool}\n  {hint}"); sys.exit(1)
    if not shutil.which('ffmpeg'):
        print("ffmpeg not found (brew install ffmpeg)"); sys.exit(1)
    port = int(os.environ.get('PORT', '8765'))
    print(f"→ http://127.0.0.1:{port}   (ไฟล์ไม่ถูกส่งออกนอกเครื่อง กด Ctrl-C เพื่อหยุด)")
    ThreadingHTTPServer(('127.0.0.1', port), H).serve_forever()
