#!/usr/bin/env python3
"""
copy_mattes.py — migrate the 12 `tag:apple.com,2026:photo:aux:` semantic matte
items from a donor HEIF into a target, together with everything they depend on.

Unlike copy_styles_item.py (which appends a standalone metadata item with no
properties), aux mattes are real coded images and need their whole support
structure carried across:

  * the hvc1 item bytes themselves
  * each matte's paired `mime` sidecar item (cdsc-linked XMP)
  * every ipco property they reference (auxC, hvcC, ispe, pixi, irot ...),
    appended to the target's ipco and RE-INDEXED, since property indices are
    per-file and will collide otherwise
  * new ipma entries using the remapped indices
  * auxl irefs to the target's primary + tmap, and cdsc for the sidecars

Pixel data is never re-encoded; item payloads are copied byte-for-byte.
"""
import argparse, struct, sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from copy_styles_item import (Heif, box, build_iinf, build_iloc, build_iref,
                              build_infe_uri, find_uri_item, _max_group_id,
                              STYLES_URI)

TEX_URI = 'tag:apple.com,2026:photo:metadata:texture_styles'
AUX_2026 = 'tag:apple.com,2026:photo:aux:'

def infe_for(iid, itype, name='', ctype=None):
    body = struct.pack('>HH', iid, 0) + itype.encode('latin-1')
    body += name.encode('utf-8') + b'\x00'
    if itype == 'mime':
        body += (ctype or '').encode('utf-8') + b'\x00'
    return box('infe', body, 2, 0)

def build_ipma(entries, version, flags):
    """entries: {item_id: [(essential, prop_index), ...]}"""
    payload = struct.pack('>I', len(entries))
    for iid in sorted(entries):
        props = entries[iid]
        payload += (struct.pack('>H', iid) if version < 1 else struct.pack('>I', iid))
        payload += bytes([len(props)])
        for ess, pi in props:
            if flags & 1:
                payload += struct.pack('>H', (ess << 15) | pi)
            else:
                payload += bytes([(ess << 7) | pi])
    return box('ipma', payload, version, flags)

def aux_urn(H, prop_box):
    return H.buf[prop_box.start+8:prop_box.end][4:].split(b'\x00')[0].decode('latin-1')

def main(donor, target, out, verbose=True, with_texture=True):
    D = Heif(donor); T = Heif(target)
    log = (lambda *a: print(*a)) if verbose else (lambda *a: None)

    d_props = list(D.meta.find('iprp').find('ipco').children)
    t_props = list(T.meta.find('iprp').find('ipco').children)
    d_ipma = D.ipma_entries(); t_ipma = T.ipma_entries()

    # --- locate the 2026 matte items in the donor -------------------
    aux_idx = {i for i, c in enumerate(d_props, 1)
               if c.type == 'auxC' and AUX_2026 in aux_urn(D, c)}
    mattes = [iid for iid, pl in d_ipma.items()
              if any(pi in aux_idx for _, pi in pl)]
    mattes.sort()
    if not mattes:
        raise SystemExit(f"donor has no {AUX_2026} mattes")
    log(f"donor mattes: {len(mattes)} items {mattes}")

    # their cdsc sidecars
    sidecar = {}
    for typ, frm, to in D.refs:
        if typ == 'cdsc' and len(to) == 1 and to[0] in mattes:
            sidecar[to[0]] = frm
    log(f"sidecars: {len(sidecar)}")

    copy_items = mattes + sorted(sidecar.values())

    # --- remap the ipco properties they need ------------------------
    needed = []
    for iid in mattes:
        for ess, pi in d_ipma[iid]:
            if pi not in needed: needed.append(pi)
    log(f"donor properties needed: {sorted(needed)}")
    pmap = {}
    new_props = []
    for pi in needed:
        pmap[pi] = len(t_props) + len(new_props) + 1
        new_props.append(d_props[pi-1].raw)
    log(f"property remap: {pmap}")
    if max(pmap.values()) > 127:
        # ipma would need 16-bit indices; only safe if target already uses them
        t_ipma_box = T.meta.find('iprp').find('ipma')
        if not (t_ipma_box.flags & 1):
            raise SystemExit(f"remapped index {max(pmap.values())} exceeds 7-bit "
                             "ipma and target uses 8-bit entries; refusing")

    # --- allocate new item ids --------------------------------------
    next_id = max(max(T.items), _max_group_id(T)) + 1
    idmap = {}
    for iid in copy_items:
        idmap[iid] = next_id; next_id += 1
    log(f"id map: {idmap}")

    # --- assemble new iinf ------------------------------------------
    infe_raw = dict(T._infe_raw)
    for old, new in idmap.items():
        v = D.items[old]
        infe_raw[new] = infe_for(new, v['type'], v['name'], v['ctype'])
    order = sorted(T.items)
    anchor = find_uri_item(T, STYLES_URI)
    pos = order.index(anchor) if anchor is not None else len(order)
    order = order[:pos] + sorted(idmap.values()) + order[pos:]

    # optionally bring the texture_styles metadata item too
    tex_src = find_uri_item(D, TEX_URI)
    tex_id = None
    if with_texture and tex_src is not None and find_uri_item(T, TEX_URI) is None:
        tex_id = next_id; next_id += 1
        infe_raw[tex_id] = build_infe_uri(tex_id, D.items[tex_src]['name'], TEX_URI)
        a2 = find_uri_item(T, STYLES_URI)
        ins = order.index(a2) + 1 if a2 is not None else len(order)
        order = order[:ins] + [tex_id] + order[ins:]
        log(f"also copying texture_styles item -> id {tex_id}")

    iinf_new = build_iinf(order, infe_raw, T.meta.find('iinf').version)

    # --- ipma ---------------------------------------------------------
    ipma_box = T.meta.find('iprp').find('ipma')
    ent = {k: list(v) for k, v in t_ipma.items()}
    for old, new in idmap.items():
        if old in d_ipma:
            ent[new] = [(ess, pmap[pi]) for ess, pi in d_ipma[old]]
    ipma_new = build_ipma(ent, ipma_box.version, ipma_box.flags)
    ipco_new = box('ipco', b''.join(c.raw for c in t_props) + b''.join(new_props))
    iprp_new = box('iprp', ipco_new + ipma_new)

    # --- irefs --------------------------------------------------------
    refs = [list(r) for r in T.refs]
    prim = T.primary()
    tmaps = T.tmap_items()
    anchors = ([prim] if prim else []) + tmaps
    for old in mattes:
        refs.append(['auxl', idmap[old], anchors])
    for old, sc in sidecar.items():
        refs.append(['cdsc', idmap[sc], [idmap[old]]])
    if tex_id is not None:
        refs.append(['cdsc', tex_id, anchors])
    iref_new = build_iref(refs, getattr(T, 'iref_version', 0))

    # --- iloc + mdat ---------------------------------------------------
    locs = {i: dict(ctm=v['ctm'], dri=v['dri'], base=v['base'],
                    extents=[list(x) for x in v['extents']])
            for i, v in T.locs.items()}
    payloads = {}
    for old, new in idmap.items():
        payloads[new] = D.item_bytes(old)
    if tex_id is not None:
        payloads[tex_id] = D.item_bytes(tex_src)
    for new, data in payloads.items():
        locs[new] = dict(ctm=0, dri=0, base=0, extents=[[0, 0, len(data)]])
    iloc_order = [i for i in order if i in locs]

    def assemble(locs):
        iloc_new = build_iloc(iloc_order, locs, T.iloc_version,
                              T.off_sz, T.len_sz, T.base_sz, T.idx_sz)
        parts = []
        for c in T.meta.children:
            parts.append({'iinf': iinf_new, 'iloc': iloc_new,
                          'iref': iref_new, 'iprp': iprp_new}.get(c.type, c.raw))
        return box('meta', b''.join(parts), T.meta.version, T.meta.flags)

    probe = assemble(locs)
    pre = [b.raw for b in T.top if b.type not in ('meta', 'mdat')]
    mdat_start = sum(len(x) for x in pre) + len(probe) + 8
    shift = mdat_start - T.mdat.payload_start
    for iid, e in locs.items():
        if iid in payloads or e['ctm'] != 0: continue
        for x in e['extents']: x[1] += shift
    blob = T.buf[T.mdat.payload_start:T.mdat.end]
    for new in sorted(payloads):
        locs[new]['extents'][0][1] = mdat_start + len(blob)
        blob += payloads[new]
    final = assemble(locs)
    if len(final) != len(probe):
        d = len(final) - len(probe)
        for iid, e in locs.items():
            if e['ctm'] != 0: continue
            for x in e['extents']: x[1] += d
        final = assemble(locs)
    out_bytes = b''.join(pre) + final + struct.pack('>I', len(blob)+8) + b'mdat' + blob
    open(out, 'wb').write(out_bytes)
    log(f"wrote {out} {len(out_bytes)} bytes ({len(out_bytes)-len(T.buf):+d})")
    return out

if __name__ == '__main__':
    a = argparse.ArgumentParser()
    a.add_argument('--donor', required=True)
    a.add_argument('--target', required=True)
    a.add_argument('--out', required=True)
    a.add_argument('--no-texture', action='store_true')
    x = a.parse_args()
    main(x.donor, x.target, x.out, with_texture=not x.no_texture)
