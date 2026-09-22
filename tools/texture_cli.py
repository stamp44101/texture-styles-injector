#!/usr/bin/env python3
"""
texture_cli.py — one-shot CLI for agents.

Checks a HEIF file, reports why it can or cannot be processed, and injects the
Texture Styles data. Designed to be driven by an agent: every outcome is a
clear exit code plus a one-line reason, and --json gives machine-readable output.

  texture_cli.py check  <in.HEIC>           # inspect only, never writes
  texture_cli.py inject <in.HEIC> [-o out]  # check then process

Exit codes:
  0  success
  2  file rejected (not HEIF, wrong structure, already has Texture)
  3  processing failed
  4  missing prerequisite (ffmpeg / genmattes not built)
"""
import argparse, json, os, shutil, struct, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import inject_mattes
from copy_styles_item import Heif, find_uri_item
from copy_mattes import aux_urn, AUX_2026, TEX_URI


def prereqs():
    missing = []
    if not shutil.which('ffmpeg'):
        missing.append("ffmpeg not installed — run: brew install ffmpeg")
    gen = os.path.join(HERE, 'gen', 'genmattes')
    if not os.path.exists(gen):
        missing.append(
            "Vision helper not built — run: "
            f"swiftc -O {os.path.join(HERE,'gen','genmattes.swift')} -o {gen}")
    if sys.platform != 'darwin':
        missing.append("macOS required (uses Apple's Vision framework)")
    return missing


def check(path):
    """Return a dict describing whether this file can be processed."""
    out = {'file': path, 'ok': False, 'reason': None, 'facts': {}, 'warnings': []}
    if not os.path.exists(path):
        out['reason'] = 'file not found'
        return out
    try:
        H = Heif(path)
    except Exception:
        out['reason'] = ('not a readable HEIF/HEIC — JPEG and PNG cannot be used, '
                         'they have no item structure to hold the mattes')
        return out

    prim = H.primary()
    if prim is None:
        out['reason'] = 'no primary item (pitm)'
        return out
    ptype = H.items[prim]['type']
    out['facts']['primary_type'] = ptype
    if ptype != 'grid':
        out['reason'] = f"primary item is '{ptype}', only 'grid' is supported"
        return out

    dims = H.grid_dims(prim)
    out['facts']['dimensions'] = list(dims)
    out['facts']['aspect'] = round(dims[0] / dims[1], 4)
    out['facts']['items'] = len(H.items)

    if find_uri_item(H, TEX_URI) is not None:
        out['reason'] = ('already has a texture_styles item — this photo was most '
                         'likely taken on an iPhone 18 Pro and needs nothing')
        return out

    props = list(H.meta.find('iprp').find('ipco').children)
    ip = H.ipma_entries()
    n2026 = sum(1 for _, pl in ip.items() for _, pi in pl
                if pi <= len(props) and props[pi-1].type == 'auxC'
                and AUX_2026 in aux_urn(H, props[pi-1]))
    if n2026:
        out['reason'] = f'already has {n2026} of the 2026 aux mattes'
        return out

    # camera-original check, and model, via EXIF
    try:
        from patch_exif import parse_ifd
        eids = [i for i, v in H.items.items() if v['type'] == 'Exif']
        if eids:
            e = H.item_bytes(eids[0]); sk = struct.unpack('>I', e[:4])[0]
            t = e[4+sk:]
            bo = '>' if t[:2] == b'MM' else '<'
            ents, _ = parse_ifd(t, struct.unpack(bo+'I', t[4:8])[0], bo)
            d = {x[0]: x for x in ents}
            if 0x0110 in d:
                out['facts']['model'] = d[0x0110][3].rstrip(b'\x00').decode(
                    'latin-1', 'replace')
            if 0x8769 in d:
                sub, _ = parse_ifd(t, struct.unpack(bo+'I', d[0x8769][3])[0], bo)
                sd = {x[0]: x for x in sub}
                if 0xa002 in sd and 0xa003 in sd:
                    def num(x):
                        typ, raw = x[1], x[3]
                        w = 2 if typ == 3 else 4
                        return struct.unpack(bo+('H' if typ == 3 else 'I'), raw[:w])[0]
                    ed = (num(sd[0xa002]), num(sd[0xa003]))
                    out['facts']['exif_dimensions'] = list(ed)
                    if tuple(dims) != ed and tuple(dims) != tuple(reversed(ed)):
                        out['warnings'].append(
                            f'looks like an exported/edited copy (grid {dims[0]}x{dims[1]} '
                            f'vs EXIF {ed[0]}x{ed[1]}); an untouched original works better')
    except Exception:
        out['warnings'].append('could not read EXIF; skipped the camera-original check')

    mw, mh = inject_mattes.matte_size(*dims)
    out['facts']['matte_size'] = [mw, mh]
    out['ok'] = True
    out['reason'] = 'ready'
    return out


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('command', choices=['check', 'inject'])
    ap.add_argument('input')
    ap.add_argument('-o', '--out', help='output path (default: texture_<name> beside input)')
    ap.add_argument('--json', action='store_true', help='machine-readable output')
    a = ap.parse_args()

    miss = prereqs()
    if miss and a.command == 'inject':
        if a.json: print(json.dumps({'ok': False, 'reason': 'missing prerequisites',
                                     'missing': miss}, ensure_ascii=False))
        else:
            for m in miss: print(f"missing: {m}", file=sys.stderr)
        sys.exit(4)

    info = check(a.input)

    if a.command == 'check':
        if a.json:
            print(json.dumps(info, ensure_ascii=False, indent=2))
        else:
            print(f"{'OK  ' if info['ok'] else 'SKIP'} {a.input}")
            print(f"     {info['reason']}")
            for k, v in info['facts'].items(): print(f"     {k}: {v}")
            for w in info['warnings']: print(f"     warning: {w}")
        sys.exit(0 if info['ok'] else 2)

    if not info['ok']:
        if a.json: print(json.dumps(info, ensure_ascii=False))
        else: print(f"cannot process: {info['reason']}", file=sys.stderr)
        sys.exit(2)

    out = a.out or os.path.join(os.path.dirname(os.path.abspath(a.input)),
                                'texture_' + os.path.basename(a.input))
    logs = []
    try:
        inject_mattes.main(a.input, out, log=logs.append)
    except SystemExit as e:
        if a.json: print(json.dumps({'ok': False, 'reason': str(e)}, ensure_ascii=False))
        else: print(f"failed: {e}", file=sys.stderr)
        sys.exit(3)

    faces = next((l.strip() for l in logs if l.strip().startswith('faces')),
                 'faces: unknown')
    nfaces = None
    if faces.split(':')[-1].strip().isdigit():
        nfaces = int(faces.split(':')[-1].strip())
    res = {'ok': True, 'input': a.input, 'output': out, 'faces': nfaces,
           'facts': info['facts'], 'warnings': info['warnings'], 'log': logs}
    if a.json:
        print(json.dumps(res, ensure_ascii=False, indent=2))
    else:
        print(f"wrote {out}")
        print(f"  {faces}")
        for w in info['warnings']: print(f"  warning: {w}")
        if nfaces == 0:
            print("  note: no face detected — Film and Grain will work, "
                  "but Soft Skin needs a person in the shot")
        print("  send to iPhone with AirDrop only (other channels re-encode "
              "and strip the mattes)")
    sys.exit(0)


if __name__ == '__main__':
    main()
