#!/usr/bin/env python3
"""
copy_styles_item.py — copy an Apple Photographic Styles HEIF metadata item
(item_type 'uri ', uri tag:apple.com,2023:photo:metadata:styles) from a donor
HEIF file into a target HEIF file.

Rewrites iinf / iloc / iref and appends the item payload to mdat.
Pixel data is never re-encoded: every existing item's bytes are copied verbatim
and its iloc extents are re-pointed, nothing more.

Design notes / invariants:
  * The styles item has NO ipma entry (verified on IMG_0305), so iprp is
    untouched. If a donor is found whose styles item HAS properties, the tool
    refuses rather than silently dropping them.
  * iloc construction_method 0 (file offset) and 1 (idat-relative) are both
    handled. ctm=1 extents are left completely alone -- they address idat,
    which we copy byte-for-byte and whose position we do not depend on.
  * All existing irefs are preserved verbatim. The new cdsc iref is appended.
  * meta box grows, so every ctm=0 extent shifts. We recompute all of them.

Usage:
  copy_styles_item.py --donor D.HEIC --target T.HEIC --out O.HEIC [--verify]
  copy_styles_item.py --inspect F.HEIC
"""
import argparse, struct, sys, os

STYLES_URI = 'tag:apple.com,2023:photo:metadata:styles'

# ---------------------------------------------------------------- box model

FULL = {'meta','iinf','infe','iloc','iref','ipma','hdlr','pitm','dinf','dref','url '}
CONTAINER = {'meta','iinf','iprp','ipco','dinf'}

class Box:
    __slots__=('type','start','size','hdr','children','version','flags','_buf')
    def __init__(self, typ, start, size, hdr, buf):
        self.type=typ; self.start=start; self.size=size; self.hdr=hdr
        self.children=[]; self.version=None; self.flags=None; self._buf=buf
    @property
    def end(self): return self.start+self.size
    @property
    def payload_start(self): return self.start+self.hdr
    @property
    def payload(self): return self._buf[self.payload_start:self.end]
    @property
    def raw(self): return self._buf[self.start:self.end]
    def find(self,t):
        for c in self.children:
            if c.type==t: return c
        return None
    def __repr__(self): return f"<{self.type}@{self.start}+{self.size}>"

def parse_boxes(buf, start, end):
    out=[]; off=start
    while off < end-7:
        size=struct.unpack('>I',buf[off:off+4])[0]
        typ=buf[off+4:off+8].decode('latin-1'); hdr=8
        if size==1:
            size=struct.unpack('>Q',buf[off+8:off+16])[0]; hdr=16
        elif size==0:
            size=end-off
        if size<hdr or off+size>end: break
        b=Box(typ,off,size,hdr,buf)
        body=off+hdr
        if typ in FULL:
            vf=struct.unpack('>I',buf[body:body+4])[0]
            b.version=vf>>24; b.flags=vf&0xFFFFFF; body+=4; b.hdr+=4
        if typ in CONTAINER:
            b.children=parse_boxes(buf,body,off+size)
        out.append(b); off+=size
    return out

def box(typ, payload, version=None, flags=0):
    body = payload if version is None else struct.pack('>I',(version<<24)|flags)+payload
    return struct.pack('>I',len(body)+8)+typ.encode('latin-1')+body

# ---------------------------------------------------------------- meta parse

class Heif:
    def __init__(self, path):
        self.path=path
        self.buf=open(path,'rb').read()
        self.top=parse_boxes(self.buf,0,len(self.buf))
        self.ftyp=next((b for b in self.top if b.type=='ftyp'),None)
        self.meta=next((b for b in self.top if b.type=='meta'),None)
        self.mdat=next((b for b in self.top if b.type=='mdat'),None)
        if not self.meta: raise ValueError(f"{path}: no meta box")
        if not self.mdat: raise ValueError(f"{path}: no mdat box")
        self.items=self._iinf()
        self.locs=self._iloc()
        self.refs=self._iref()

    # -- iinf ------------------------------------------------------
    def _iinf(self):
        buf=self.buf; iinf=self.meta.find('iinf')
        out={}
        if not iinf: return out
        p=iinf.payload_start
        if iinf.version==0:
            cnt=struct.unpack('>H',buf[p:p+2])[0]; p+=2
        else:
            cnt=struct.unpack('>I',buf[p:p+4])[0]; p+=4
        self._infe_raw={}
        for _ in range(cnt):
            sz=struct.unpack('>I',buf[p:p+4])[0]
            assert buf[p+4:p+8]==b'infe', 'non-infe in iinf'
            ver=buf[p+8]
            q=p+12
            if ver==2: iid=struct.unpack('>H',buf[q:q+2])[0]; q+=2
            elif ver==3: iid=struct.unpack('>I',buf[q:q+4])[0]; q+=4
            else: raise ValueError(f'infe version {ver} unsupported')
            q+=2  # protection_index
            itype=buf[q:q+4].decode('latin-1'); q+=4
            def cstr(x):
                e=buf.index(b'\x00',x); return buf[x:e].decode('utf-8','replace'), e+1
            name,q=cstr(q)
            ctype=uri=None
            if itype=='mime' and q<p+sz: ctype,q=cstr(q)
            elif itype=='uri ' and q<p+sz: uri,q=cstr(q)
            out[iid]=dict(type=itype,name=name,ctype=ctype,uri=uri)
            self._infe_raw[iid]=buf[p:p+sz]
            p+=sz
        return out

    # -- iloc ------------------------------------------------------
    def _iloc(self):
        buf=self.buf; il=self.meta.find('iloc')
        out={}
        if not il: return out
        p=il.payload_start
        b0,b1=buf[p],buf[p+1]
        self.off_sz=b0>>4; self.len_sz=b0&0xF
        self.base_sz=b1>>4; self.idx_sz=b1&0xF
        self.iloc_version=il.version
        p+=2
        if il.version<2: cnt=struct.unpack('>H',buf[p:p+2])[0]; p+=2
        else: cnt=struct.unpack('>I',buf[p:p+4])[0]; p+=4
        def rd(n,q):
            if n==0: return 0,q
            return int.from_bytes(buf[q:q+n],'big'), q+n
        self._iloc_order=[]
        for _ in range(cnt):
            if il.version<2: iid=struct.unpack('>H',buf[p:p+2])[0]; p+=2
            else: iid=struct.unpack('>I',buf[p:p+4])[0]; p+=4
            ctm=0
            if il.version in (1,2):
                ctm=struct.unpack('>H',buf[p:p+2])[0]&0xF; p+=2
            dri=struct.unpack('>H',buf[p:p+2])[0]; p+=2
            base,p=rd(self.base_sz,p)
            n=struct.unpack('>H',buf[p:p+2])[0]; p+=2
            ex=[]
            for _ in range(n):
                idx=0
                if il.version in (1,2) and self.idx_sz: idx,p=rd(self.idx_sz,p)
                eo,p=rd(self.off_sz,p)
                el,p=rd(self.len_sz,p)
                ex.append([idx,eo,el])
            out[iid]=dict(ctm=ctm,dri=dri,base=base,extents=ex)
            self._iloc_order.append(iid)
        return out

    # -- iref ------------------------------------------------------
    def _iref(self):
        buf=self.buf; ir=self.meta.find('iref')
        out=[]
        if not ir: return out
        self.iref_version=ir.version
        p=ir.payload_start
        large = ir.version==1
        while p < ir.end-7:
            sz=struct.unpack('>I',buf[p:p+4])[0]
            typ=buf[p+4:p+8].decode('latin-1'); q=p+8
            if large:
                frm=struct.unpack('>I',buf[q:q+4])[0]; q+=4
                n=struct.unpack('>H',buf[q:q+2])[0]; q+=2
                to=[struct.unpack('>I',buf[q+4*i:q+4*i+4])[0] for i in range(n)]
            else:
                frm=struct.unpack('>H',buf[q:q+2])[0]; q+=2
                n=struct.unpack('>H',buf[q:q+2])[0]; q+=2
                to=[struct.unpack('>H',buf[q+2*i:q+2*i+2])[0] for i in range(n)]
            out.append([typ,frm,to]); p+=sz
        return out

    # -- helpers ---------------------------------------------------
    def idat_start(self):
        idat=self.meta.find('idat')
        return idat.payload_start if idat else None

    def item_bytes(self, iid):
        e=self.locs[iid]
        if e['ctm']==0: origin=0
        elif e['ctm']==1:
            s=self.idat_start()
            if s is None: raise ValueError('ctm=1 without idat')
            origin=s
        else: raise ValueError(f"ctm {e['ctm']} unsupported")
        out=b''
        for idx,eo,el in e['extents']:
            s=origin+e['base']+eo
            out+=self.buf[s:s+el]
        return out

    def ipma_entries(self):
        iprp=self.meta.find('iprp')
        if not iprp: return {}
        ipma=iprp.find('ipma')
        if not ipma: return {}
        buf=self.buf; q=ipma.payload_start
        cnt=struct.unpack('>I',buf[q:q+4])[0]; q+=4
        out={}
        for _ in range(cnt):
            if ipma.version<1: iid=struct.unpack('>H',buf[q:q+2])[0]; q+=2
            else: iid=struct.unpack('>I',buf[q:q+4])[0]; q+=4
            n=buf[q]; q+=1
            props=[]
            for _ in range(n):
                if ipma.flags&1:
                    v=struct.unpack('>H',buf[q:q+2])[0]; q+=2
                    props.append((v>>15,v&0x7FFF))
                else:
                    v=buf[q]; q+=1
                    props.append((v>>7,v&0x7F))
            out[iid]=props
        return out

    def find_styles_item(self):
        for iid,v in self.items.items():
            if v['type']=='uri ' and v['uri']==STYLES_URI:
                return iid
        return None

    def primary(self):
        pi=self.meta.find('pitm')
        if not pi: return None
        return struct.unpack('>H',self.buf[pi.payload_start:pi.payload_start+2])[0]

    def tmap_items(self):
        return [i for i,v in self.items.items() if v['type']=='tmap']

    def grid_dims(self, iid):
        g=self.item_bytes(iid)
        if len(g)<8: return None
        flags=g[1]
        if flags&1: W,H=struct.unpack('>II',g[4:12])
        else: W,H=struct.unpack('>HH',g[4:8])
        return W,H

# ---------------------------------------------------------------- builders

def build_iinf(items_order, infe_raw, version):
    payload=b''
    if version==0: payload+=struct.pack('>H',len(items_order))
    else: payload+=struct.pack('>I',len(items_order))
    for iid in items_order:
        payload+=infe_raw[iid]
    return box('iinf',payload,version,0)

def _max_group_id(H):
    """altr/EntityToGroup ids share the item id space -- never reuse one."""
    g=H.meta.find('grpl')
    if not g: return 0
    best=0; off=g.start+8
    while off < g.end-7:
        sz=struct.unpack('>I',H.buf[off:off+4])[0]
        if sz<20: break
        best=max(best, struct.unpack('>I',H.buf[off+12:off+16])[0])
        off+=sz
    return best

def build_infe_uri(iid, name, uri):
    # infe version 2 (16-bit item_ID)
    body = struct.pack('>HH',iid,0) + b'uri ' \
         + name.encode('utf-8')+b'\x00' + uri.encode('utf-8')+b'\x00'
    return box('infe', body, 2, 0)

def build_iloc(order, locs, version, off_sz, len_sz, base_sz, idx_sz):
    payload = bytes([ (off_sz<<4)|len_sz, (base_sz<<4)|idx_sz ])
    if version<2: payload+=struct.pack('>H',len(order))
    else: payload+=struct.pack('>I',len(order))
    for iid in order:
        e=locs[iid]
        if version<2: payload+=struct.pack('>H',iid)
        else: payload+=struct.pack('>I',iid)
        if version in (1,2): payload+=struct.pack('>H',e['ctm']&0xF)
        payload+=struct.pack('>H',e['dri'])
        if base_sz: payload+=e['base'].to_bytes(base_sz,'big')
        payload+=struct.pack('>H',len(e['extents']))
        for idx,eo,el in e['extents']:
            if version in (1,2) and idx_sz: payload+=idx.to_bytes(idx_sz,'big')
            payload+=eo.to_bytes(off_sz,'big')
            payload+=el.to_bytes(len_sz,'big')
    return box('iloc',payload,version,0)

def build_iref(refs, version):
    payload=b''
    for typ,frm,to in refs:
        if version==1:
            body=struct.pack('>I',frm)+struct.pack('>H',len(to))+b''.join(struct.pack('>I',t) for t in to)
        else:
            body=struct.pack('>H',frm)+struct.pack('>H',len(to))+b''.join(struct.pack('>H',t) for t in to)
        payload+=box(typ,body)
    return box('iref',payload,version,0)

# ---------------------------------------------------------------- main op

def find_uri_item(H, uri):
    for iid,v in H.items.items():
        if v['type']=='uri ' and v['uri']==uri:
            return iid
    return None

def copy_item(donor_path, target_path, out_path, verbose=True, uri=STYLES_URI):
    D=Heif(donor_path); T=Heif(target_path)
    log=(lambda *a: print(*a)) if verbose else (lambda *a: None)

    src=find_uri_item(D, uri)
    if src is None:
        raise SystemExit(f"donor {donor_path} has no item with uri {uri}")
    payload=D.item_bytes(src)
    log(f"donor {os.path.basename(donor_path)}: item {src} <{uri}>, {len(payload)} bytes")

    # refuse if donor's styles item carries properties we would drop
    dprops=D.ipma_entries().get(src)
    if dprops:
        raise SystemExit(f"donor item has ipma properties {dprops}; "
                         "copying them is not implemented -- refusing rather than dropping")

    if find_uri_item(T, uri) is not None:
        raise SystemExit(f"target {target_path} already has an item with uri {uri}; refusing")

    new_id = max(max(T.items), _max_group_id(T))+1
    if new_id > 0xFFFF: raise SystemExit("item id overflow")
    log(f"target {os.path.basename(target_path)}: {len(T.items)} items, new item id {new_id}")

    # --- new iinf -------------------------------------------------
    # Apple orders the metadata items adjacently (styles, texture_styles)
    # with Exif LAST. Appending after Exif is structurally legal but does not
    # match any real file, so mirror the convention: insert directly after the
    # 2023 styles item when present, else before the first Exif item.
    order=sorted(T.items)
    anchor=find_uri_item(T, STYLES_URI)
    if anchor is not None:
        pos=order.index(anchor)+1
    else:
        exifs=[i for i in order if T.items[i]['type']=='Exif']
        pos=order.index(exifs[0]) if exifs else len(order)
    order=order[:pos]+[new_id]+order[pos:]
    log(f"iinf placement: inserted at index {pos} (after item {order[pos-1]}, before {order[pos+1] if pos+1<len(order) else 'END'})")
    donor_name=D.items[src]['name']
    infe_raw=dict(T._infe_raw)
    infe_raw[new_id]=build_infe_uri(new_id,donor_name,uri)
    iinf_new=build_iinf(order,infe_raw,T.meta.find('iinf').version)

    # --- new iref: preserve every existing ref, append cdsc -------
    refs=[list(r) for r in T.refs]
    targets=[]
    p=T.primary()
    if p is not None: targets.append(p)
    for t in T.tmap_items():
        if t not in targets: targets.append(t)
    refs.append(['cdsc',new_id,targets])
    log(f"new iref: cdsc {new_id} -> {targets}  (kept {len(T.refs)} existing refs)")
    iref_ver = getattr(T,'iref_version',0)
    iref_new = build_iref(refs, iref_ver)

    # --- iloc: placeholder to measure, then fix offsets -----------
    locs={iid:dict(ctm=v['ctm'],dri=v['dri'],base=v['base'],
                   extents=[list(x) for x in v['extents']])
          for iid,v in T.locs.items()}
    locs[new_id]=dict(ctm=0,dri=0,base=0,extents=[[0,0,len(payload)]])
    iloc_order=[i for i in order if i in locs]

    def assemble(locs):
        iloc_new=build_iloc(iloc_order,locs,T.iloc_version,
                            T.off_sz,T.len_sz,T.base_sz,T.idx_sz)
        parts=[]
        for c in T.meta.children:
            if c.type=='iinf': parts.append(iinf_new)
            elif c.type=='iloc': parts.append(iloc_new)
            elif c.type=='iref': parts.append(iref_new)
            else: parts.append(c.raw)
        if T.meta.find('iref') is None:
            parts.append(iref_new)
        meta_payload=b''.join(parts)
        meta_new=box('meta',meta_payload,T.meta.version,T.meta.flags)
        return meta_new

    meta_probe=assemble(locs)
    # new file layout: ftyp | [other pre-mdat boxes] | meta | mdat
    pre=[]
    for b in T.top:
        if b.type in ('meta','mdat'): continue
        pre.append(b.raw)
    mdat_payload_start_new = sum(len(x) for x in pre) + len(meta_probe) + 8
    old_mdat_payload_start = T.mdat.payload_start
    shift = mdat_payload_start_new - old_mdat_payload_start
    log(f"mdat payload moves {old_mdat_payload_start} -> {mdat_payload_start_new} (shift {shift:+d})")

    # re-point every ctm=0 extent; ctm=1 (idat-relative) untouched
    moved=0
    for iid,e in locs.items():
        if iid==new_id: continue
        if e['ctm']!=0: continue
        for x in e['extents']:
            x[1]+=shift; moved+=1
    log(f"re-pointed {moved} ctm=0 extents; left {sum(1 for e in locs.values() if e['ctm']==1)} ctm=1 items alone")

    # new item sits at end of mdat
    old_mdat_payload = T.buf[T.mdat.payload_start:T.mdat.end]
    locs[new_id]['extents'][0][1] = mdat_payload_start_new + len(old_mdat_payload)

    meta_final=assemble(locs)
    if len(meta_final)!=len(meta_probe):
        # offset width changed the encoding size; redo once with corrected shift
        delta=len(meta_final)-len(meta_probe)
        log(f"meta size changed by {delta}, correcting")
        for iid,e in locs.items():
            if iid==new_id or e['ctm']!=0: continue
            for x in e['extents']: x[1]+=delta
        locs[new_id]['extents'][0][1]+=delta
        meta_final=assemble(locs)
        assert len(meta_final)==len(meta_probe)+delta

    new_mdat_payload = old_mdat_payload + payload
    out = b''.join(pre) + meta_final + struct.pack('>I',len(new_mdat_payload)+8)+b'mdat'+new_mdat_payload
    with open(out_path,'wb') as f: f.write(out)
    log(f"wrote {out_path}  {len(out)} bytes ({len(out)-len(T.buf):+d})")
    return out_path, new_id

# ---------------------------------------------------------------- verify

def verify(path, expect_id=None, ref_target=None, verbose=True, uri=STYLES_URI):
    log=(lambda *a: print(*a)) if verbose else (lambda *a: None)
    H=Heif(path)
    problems=[]
    sid=find_uri_item(H, uri)
    log(f"parse OK: {len(H.items)} items, top boxes {[b.type for b in H.top]}")
    if sid is None: problems.append(f"no item with uri {uri} after copy")
    else: log(f"item <{uri}> present: id {sid}, {len(H.item_bytes(sid))} bytes")
    if expect_id and sid!=expect_id: problems.append(f"styles id {sid} != expected {expect_id}")
    # every extent must be in range and inside mdat/idat
    fsz=len(H.buf)
    for iid,e in H.locs.items():
        for idx,eo,el in e['extents']:
            if e['ctm']==0:
                s=e['base']+eo
                if s<0 or s+el>fsz: problems.append(f"item {iid} extent {s}+{el} out of file ({fsz})")
                elif not (H.mdat.payload_start<=s and s+el<=H.mdat.end):
                    problems.append(f"item {iid} extent {s}+{el} outside mdat")
            elif e['ctm']==1:
                idat=H.meta.find('idat')
                if not idat: problems.append(f"item {iid} ctm=1 but no idat")
                elif idat.payload_start+e['base']+eo+el > idat.end:
                    problems.append(f"item {iid} idat extent overflows")
    # primary must decode-resolve
    p=H.primary()
    if p is None: problems.append("no pitm")
    elif p not in H.items: problems.append(f"pitm {p} not an item")
    # bplist must round-trip
    if sid:
        import plistlib
        try:
            pl=plistlib.loads(H.item_bytes(sid))
            log(f"bplist OK: {len(pl)} top-level keys: {sorted(map(str,pl.keys()))}")
        except Exception as ex:
            problems.append(f"bplist parse failed: {ex}")
    # irefs intact
    log(f"irefs: {len(H.refs)}")
    for typ,frm,to in H.refs:
        if frm not in H.items: problems.append(f"iref {typ} from unknown item {frm}")
        for t in to:
            if t not in H.items: problems.append(f"iref {typ} {frm} -> unknown item {t}")
    return problems, H

def inspect(path):
    H=Heif(path)
    print("="*72); print(path, f"({len(H.buf)} bytes)")
    print("top:", [(b.type,b.size) for b in H.top])
    print("pitm:", H.primary())
    sid=H.find_styles_item()
    print("styles item:", sid if sid else "ABSENT")
    from collections import Counter
    print("item types:", dict(Counter(v['type'] for v in H.items.values())))
    for iid,v in sorted(H.items.items()):
        if v['type'] in ('uri ','tmap','grid','Exif') or v['uri']:
            extra=f" uri={v['uri']}" if v['uri'] else ""
            sz=sum(x[2] for x in H.locs[iid]['extents']) if iid in H.locs else 0
            d=""
            if v['type']=='grid':
                try: d=f" dims={H.grid_dims(iid)}"
                except Exception: pass
            print(f"   {iid:4d} {v['type']:5s} {sz:9d}{extra}{d}")
    print("irefs:")
    for typ,frm,to in H.refs: print(f"   {typ}: {frm} -> {to}")

if __name__=='__main__':
    ap=argparse.ArgumentParser()
    ap.add_argument('--donor'); ap.add_argument('--target'); ap.add_argument('--out')
    ap.add_argument('--inspect'); ap.add_argument('--verify-only')
    ap.add_argument('--uri', default=STYLES_URI,
                    help='which uri item to copy (default: the 2023 styles item)')
    a=ap.parse_args()
    if a.inspect: inspect(a.inspect)
    elif a.verify_only:
        pr,_=verify(a.verify_only,uri=a.uri)
        print("PROBLEMS:",pr if pr else "none")
        sys.exit(1 if pr else 0)
    else:
        if not(a.donor and a.target and a.out): ap.error("need --donor --target --out")
        out,nid=copy_item(a.donor,a.target,a.out,uri=a.uri)
        print("-"*40)
        pr,_=verify(out,expect_id=nid,uri=a.uri)
        print("PROBLEMS:",pr if pr else "none")
        sys.exit(1 if pr else 0)
