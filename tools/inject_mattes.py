#!/usr/bin/env python3
"""
inject_mattes.py — end-to-end: generate semantic mattes for an image with
Apple's Vision framework, encode them as HEVC, and inject them into the HEIF
file together with the texture_styles item.

Unlike copy_mattes.py (which transplants a donor's mattes and therefore only
works when the donor and target share an aspect ratio), this derives every
matte from the target image itself, so the masks actually line up with the
pixels and the geometry check passes for any shape.

Pipeline:
  1. decode target -> PNG
  2. genmattes (Vision: person segmentation + face landmarks) -> 12 PGMs
  3. ffmpeg -> one mono HEVC still per matte
  4. rewrite iinf/iloc/iref/iprp(ipco+ipma)/mdat to add them + texture item

Requires: swiftc-built ./gen/genmattes, ffmpeg, sips.
"""
import argparse, os, plistlib, struct, subprocess, sys, tempfile, shutil
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from copy_styles_item import (Heif, box, build_iinf, build_iloc, build_iref,
                              build_infe_uri, find_uri_item, _max_group_id,
                              STYLES_URI)
from copy_mattes import build_ipma, infe_for, TEX_URI, AUX_2026
from patch_exif import parse_ifd

HERE = os.path.dirname(os.path.abspath(__file__))
GEN = os.path.join(HERE, 'gen', 'genmattes')

# order matters only for readability; each gets its own auxC
MATTES = ['semanticnosematte','semanticskinmattev2','semanticnonfaceskinmatte',
          'semanticlipsmatte','semanticteethmattev2','semanticpersonmatte',
          'semanticglassesmattev2','semanticeyebrowsmatte','semantictattoomatte',
          'semantichandsmatte','semanticearsmatte','semanticfaceskinmatte']

def run(cmd, **kw):
    r = subprocess.run(cmd, capture_output=True, **kw)
    if r.returncode != 0:
        raise SystemExit(f"{cmd[0]} failed:\n{r.stderr.decode()[:800]}")
    return r

def exif_orientation(H):
    """EXIF Orientation of the target, or 1. Values 5-8 swap displayed axes."""
    eids=[i for i,v in H.items.items() if v['type']=='Exif']
    if not eids: return 1
    e=H.item_bytes(eids[0]); sk=struct.unpack('>I',e[:4])[0]; t=e[4+sk:]
    bo='>' if t[:2]==b'MM' else '<'
    try:
        ents,_=parse_ifd(t,struct.unpack(bo+'I',t[4:8])[0],bo)
        for tag,typ,cnt,raw in ents:
            if tag==0x0112: return struct.unpack(bo+'H',raw[:2])[0]
    except Exception:
        pass
    return 1

def matte_size(pw, ph):
    """Pick a matte resolution matching the primary's aspect ratio.
    Apple uses 768x576 for 4:3; keep the short side at 576 and scale, rounding
    to even numbers so HEVC chroma subsampling is happy."""
    if pw >= ph:
        h = 576; w = int(round(576 * pw / ph / 2)) * 2
    else:
        w = 576; h = int(round(576 * ph / pw / 2)) * 2
    return w, h

def read_pgm(path):
    d=open(path,'rb').read()
    parts=[]; i=0
    while len(parts)<4:
        while d[i:i+1].isspace(): i+=1
        if d[i:i+1]==b'#':
            while d[i:i+1]!=b'\n': i+=1
            continue
        j=i
        while not d[j:j+1].isspace(): j+=1
        parts.append(d[i:j]); i=j
    i+=1
    w,h=int(parts[1]),int(parts[2])
    return w,h,d[i:i+w*h]

def rotate_pgm(path, ori, out):
    """Rotate a PGM from display orientation back into stored-buffer orientation."""
    w,h,px=read_pgm(path)
    if ori==6:      # stored buffer rotated 90 CW for display
        nw,nh=h,w
        new=bytes(px[(h-1-x)*w + y] for y in range(nh) for x in range(nw))
    elif ori==8:
        nw,nh=h,w
        new=bytes(px[x*w + (w-1-y)] for y in range(nh) for x in range(nw))
    elif ori in (5,7):
        nw,nh=h,w
        new=bytes(px[x*w + y] for y in range(nh) for x in range(nw))
    else:
        return path
    open(out,'wb').write(b"P5\n%d %d\n255\n"%(nw,nh)+new)
    return out

def encode_hevc(pgm, out, w, h):
    run(['ffmpeg','-y','-loglevel','error','-f','image2','-i',pgm,
         '-c:v','libx265','-x265-params','log-level=none','-pix_fmt','gray',
         '-frames:v','1','-tag:v','hvc1','-f','hevc',out])

def extract_hvcc_and_nals(path):
    """Split a raw HEVC Annex-B stream into (hvcC box payload, length-prefixed NALs)."""
    data = open(path,'rb').read()
    starts=[]; i=0
    while True:
        j = data.find(b'\x00\x00\x01', i)
        if j < 0: break
        starts.append(j); i = j+3
    nals=[]
    for k,s in enumerate(starts):
        b = s+3
        e = starts[k+1] if k+1 < len(starts) else len(data)
        while e > b and data[e-1]==0: e -= 1
        nals.append(data[b:e])
    vps=sps=pps=None; payload=[]
    for n in nals:
        t = (n[0] >> 1) & 0x3F
        if t == 32: vps = n
        elif t == 33: sps = n
        elif t == 34: pps = n
        elif t < 32: payload.append(n)
    if not (vps and sps and pps and payload):
        raise SystemExit("could not find VPS/SPS/PPS + slice in encoded matte")
    # minimal hvcC (ISO/IEC 14496-15 8.3.3.1) carrying the three parameter sets
    prof = sps[3:15] if len(sps) >= 15 else sps[3:].ljust(12, b'\x00')
    hv  = b'\x01'                      # configurationVersion
    hv += prof[:1]                      # general_profile_space/tier/idc
    hv += prof[1:5]                     # general_profile_compatibility_flags
    hv += prof[5:11]                    # general_constraint_indicator_flags
    hv += prof[11:12] or b'\x00'        # general_level_idc
    hv += b'\xf0\x00'                   # min_spatial_segmentation
    hv += b'\xfc'                       # parallelismType
    hv += b'\xfc'                       # chromaFormat (monochrome->0, |0xfc)
    hv += b'\xf8'                       # bitDepthLumaMinus8
    hv += b'\xf8'                       # bitDepthChromaMinus8
    hv += b'\x00\x00'                   # avgFrameRate
    hv += b'\x0f'                       # constantFrameRate/numTemporalLayers/lengthSizeMinusOne=3
    hv += bytes([3])                    # numOfArrays
    for t, n in ((32,vps),(33,sps),(34,pps)):
        hv += bytes([0x80 | t]) + struct.pack('>H',1) + struct.pack('>H',len(n)) + n
    body = b''.join(struct.pack('>I',len(n)) + n for n in payload)
    return hv, body

def main(target, out, verbose=True, log=None):
    if log is None:
        log = (lambda *a: print(*a)) if verbose else (lambda *a: None)
    if not os.path.exists(GEN):
        raise SystemExit(f"missing {GEN}; build it with: swiftc -O gen/genmattes.swift -o gen/genmattes")
    T = Heif(target)
    prim = T.primary()
    pd = T.grid_dims(prim) if T.items[prim]['type']=='grid' else None
    if not pd: raise SystemExit("target primary is not a grid; unsupported")
    mw, mh = matte_size(*pd)
    ori = exif_orientation(T)
    swap = ori in (5,6,7,8)
    # Vision sees the DISPLAYED image; the grid stores pre-rotation pixels.
    # Generate at display orientation, then rotate the masks back to match storage.
    gw, gh = (mh, mw) if swap else (mw, mh)
    log(f"primary {pd[0]}x{pd[1]} (ratio {pd[0]/pd[1]:.4f}) -> matte {mw}x{mh} "
        f"(ratio {mw/mh:.4f}) orientation={ori}{' (axes swapped)' if swap else ''}")

    tmp = tempfile.mkdtemp(prefix='mattes_')
    try:
        png = os.path.join(tmp,'src.png')
        run(['sips','-s','format','png',target,'--out',png])
        log("running Vision segmentation...")
        r = subprocess.run([GEN, png, tmp, str(gw), str(gh)], capture_output=True)
        if r.returncode != 0:
            raise SystemExit(f"genmattes failed:\n{r.stderr.decode()[:800]}")
        for line in r.stderr.decode().splitlines():
            if line.strip().startswith('semantic') or line.startswith('faces'):
                log("   "+line.strip())

        encoded={}
        for m in MATTES:
            pgm=os.path.join(tmp,m+'.pgm')
            if not os.path.exists(pgm): raise SystemExit(f"generator did not emit {m}")
            if swap:
                pgm=rotate_pgm(pgm, ori, os.path.join(tmp,m+'_rot.pgm'))
            hv=os.path.join(tmp,m+'.hevc')
            encode_hevc(pgm,hv,mw,mh)
            encoded[m]=extract_hvcc_and_nals(hv)
        log(f"encoded {len(encoded)} mattes as HEVC")

        # ---- build the new boxes -------------------------------------
        t_props=list(T.meta.find('iprp').find('ipco').children)
        t_ipma=T.ipma_entries()
        ipma_box=T.meta.find('iprp').find('ipma')

        # shared properties: one ispe + one pixi for all mattes
        new_props=[]
        def addprop(raw):
            new_props.append(raw)
            return len(t_props)+len(new_props)
        ispe_i = addprop(box('ispe', struct.pack('>II',mw,mh), 0, 0))
        pixi_i = addprop(box('pixi', bytes([1,8]), 0, 0))

        next_id=max(max(T.items),_max_group_id(T))+1
        ids={}; payloads={}; ent={k:list(v) for k,v in t_ipma.items()}
        infe_raw=dict(T._infe_raw)
        for m in MATTES:
            hv,body=encoded[m]
            hvcc_i=addprop(box('hvcC',hv))
            urn=AUX_2026+m
            auxc_i=addprop(box('auxC', urn.encode()+b'\x00', 0, 0))
            iid=next_id; next_id+=1
            ids[m]=iid
            payloads[iid]=body
            infe_raw[iid]=infe_for(iid,'hvc1')
            ent[iid]=[(1,hvcc_i),(0,ispe_i),(0,pixi_i),(1,auxc_i)]

        if max(len(t_props)+len(new_props), 0) > 127 and not (ipma_box.flags & 1):
            raise SystemExit("property count exceeds 7-bit ipma indices; target uses 8-bit entries")

        tex_id=None
        if find_uri_item(T,TEX_URI) is None:
            tex_id=next_id; next_id+=1
            infe_raw[tex_id]=build_infe_uri(tex_id,'metadata',TEX_URI)
            payloads[tex_id]=plistlib.dumps({
                'CaptureMode':'Still','CaptureType':'LF','FilmGrainSeed':277,
                'HardwareModel':'iPhone19,7','PortType':'PortTypeBack',
                'Preset':'Soft','TextureStylePeopleDataVersion':3},
                fmt=plistlib.FMT_BINARY)

        order=sorted(T.items)
        anchor=find_uri_item(T,STYLES_URI)
        pos=order.index(anchor) if anchor is not None else len(order)
        order=order[:pos]+sorted(ids.values())+order[pos:]
        if tex_id is not None:
            a2=find_uri_item(T,STYLES_URI)
            ins=order.index(a2)+1 if a2 is not None else len(order)
            order=order[:ins]+[tex_id]+order[ins:]

        iinf_new=build_iinf(order,infe_raw,T.meta.find('iinf').version)
        ipco_new=box('ipco', b''.join(c.raw for c in t_props)+b''.join(new_props))
        iprp_new=box('iprp', ipco_new+build_ipma(ent,ipma_box.version,ipma_box.flags))

        refs=[list(r) for r in T.refs]
        anchors=([prim] if prim else [])+T.tmap_items()
        for m in MATTES: refs.append(['auxl',ids[m],anchors])
        if tex_id is not None: refs.append(['cdsc',tex_id,anchors])
        iref_new=build_iref(refs,getattr(T,'iref_version',0))

        locs={i:dict(ctm=v['ctm'],dri=v['dri'],base=v['base'],
                     extents=[list(x) for x in v['extents']])
              for i,v in T.locs.items()}
        for i,d in payloads.items():
            locs[i]=dict(ctm=0,dri=0,base=0,extents=[[0,0,len(d)]])
        iloc_order=[i for i in order if i in locs]

        def assemble(locs):
            iloc_new=build_iloc(iloc_order,locs,T.iloc_version,
                                T.off_sz,T.len_sz,T.base_sz,T.idx_sz)
            parts=[]
            for c in T.meta.children:
                parts.append({'iinf':iinf_new,'iloc':iloc_new,'iref':iref_new,
                              'iprp':iprp_new}.get(c.type,c.raw))
            return box('meta',b''.join(parts),T.meta.version,T.meta.flags)

        probe=assemble(locs)
        pre=[b.raw for b in T.top if b.type not in ('meta','mdat')]
        mdat_start=sum(len(x) for x in pre)+len(probe)+8
        shift=mdat_start-T.mdat.payload_start
        for iid,e in locs.items():
            if iid in payloads or e['ctm']!=0: continue
            for x in e['extents']: x[1]+=shift
        blob=T.buf[T.mdat.payload_start:T.mdat.end]
        for i in sorted(payloads):
            locs[i]['extents'][0][1]=mdat_start+len(blob)
            blob+=payloads[i]
        final=assemble(locs)
        if len(final)!=len(probe):
            d=len(final)-len(probe)
            for iid,e in locs.items():
                if e['ctm']!=0: continue
                for x in e['extents']: x[1]+=d
            final=assemble(locs)
        data=b''.join(pre)+final+struct.pack('>I',len(blob)+8)+b'mdat'+blob
        open(out,'wb').write(data)
        log(f"wrote {out} {len(data)} bytes ({len(data)-len(T.buf):+d})")
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)

if __name__=='__main__':
    a=argparse.ArgumentParser()
    a.add_argument('--target',required=True); a.add_argument('--out',required=True)
    x=a.parse_args()
    main(x.target,x.out)
