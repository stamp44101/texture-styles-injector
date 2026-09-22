#!/usr/bin/env python3
"""Rewrite EXIF ASCII tags inside a HEIF Exif item, rebuilding the TIFF block so
replacement strings may differ in length. Only IFD0 + ExifIFD ASCII tags are touched;
every other tag, including MakerNote, is copied verbatim."""
import sys, os, struct
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from copy_styles_item import Heif
from replace_item import replace_payload

SZ={1:1,2:1,3:2,4:4,5:8,7:1,9:4,10:8,11:4,12:8}

def parse_ifd(t, off, bo):
    n=struct.unpack(bo+'H', t[off:off+2])[0]; p=off+2; ents=[]
    for _ in range(n):
        tag,typ,cnt=struct.unpack(bo+'HHI', t[p:p+8])
        sz=SZ.get(typ,1)*cnt
        vo = p+8 if sz<=4 else struct.unpack(bo+'I', t[p+8:p+12])[0]
        ents.append([tag,typ,cnt,t[vo:vo+sz]])
        p+=12
    nxt=struct.unpack(bo+'I', t[p:p+4])[0]
    return ents, nxt

def build_ifd(ents, bo, base_off, trailing=b''):
    """Return (ifd_bytes, heap_bytes). Values >4 bytes go to heap placed after IFD."""
    n=len(ents)
    ifd_len = 2 + 12*n + 4
    heap=b''
    out=struct.pack(bo+'H', n)
    for tag,typ,cnt,raw in ents:
        sz=SZ.get(typ,1)*cnt
        if sz<=4:
            val=raw+b'\x00'*(4-len(raw))
        else:
            val=struct.pack(bo+'I', base_off+ifd_len+len(heap))
            heap+=raw
            if len(raw)&1: heap+=b'\x00'
        out+=struct.pack(bo+'HHI',tag,typ,cnt)+val
    out+=struct.pack(bo+'I',0)
    return out, heap

def patch(exif_payload, repl):
    skip=struct.unpack('>I', exif_payload[:4])[0]
    head=exif_payload[:4+skip]
    t=exif_payload[4+skip:]
    bo='>' if t[:2]==b'MM' else '<'
    ifd0_off=struct.unpack(bo+'I', t[4:8])[0]
    ents0,_=parse_ifd(t, ifd0_off, bo)
    sub_ents=None; sub_idx=None
    for i,(tag,typ,cnt,raw) in enumerate(ents0):
        if tag==0x8769:
            sub_off=struct.unpack(bo+'I',raw)[0]
            sub_ents,_=parse_ifd(t, sub_off, bo); sub_idx=i
    def apply(ents):
        for e in ents:
            if e[0] in repl and e[1]==2:
                s=repl[e[0]].encode('ascii')+b'\x00'
                e[2]=len(s); e[3]=s
    apply(ents0)
    if sub_ents: apply(sub_ents)

    # layout: header(8) | IFD0 | IFD0 heap | ExifIFD | ExifIFD heap
    bo_hdr = (b'MM\x00*' if bo=='>' else b'II*\x00')+struct.pack(bo+'I',8)
    # iterate to fix the ExifIFD pointer (its offset depends on IFD0 size, which is fixed)
    ifd0_bytes,_ = build_ifd(ents0, bo, 8)
    ifd0_len=len(ifd0_bytes)
    heap0_len_guess=sum((SZ.get(e[1],1)*e[2]+1)//2*2 for e in ents0 if SZ.get(e[1],1)*e[2]>4)
    sub_start = 8+ifd0_len+heap0_len_guess
    if sub_ents is not None:
        ents0[sub_idx][3]=struct.pack(bo+'I', sub_start)
    ifd0_bytes, heap0 = build_ifd(ents0, bo, 8)
    assert len(heap0)==heap0_len_guess, (len(heap0),heap0_len_guess)
    body = bo_hdr + ifd0_bytes + heap0
    if sub_ents is not None:
        sub_bytes, heap1 = build_ifd(sub_ents, bo, sub_start)
        body += sub_bytes + heap1
    return head + body

if __name__=='__main__':
    import argparse
    a=argparse.ArgumentParser()
    a.add_argument('--target',required=True); a.add_argument('--out',required=True)
    a.add_argument('--model'); a.add_argument('--host'); a.add_argument('--lens')
    x=a.parse_args()
    H=Heif(x.target)
    eid=[i for i,v in H.items.items() if v['type']=='Exif'][0]
    repl={}
    if x.model: repl[0x0110]=x.model
    if x.host:  repl[0x013c]=x.host
    if x.lens:  repl[0xa434]=x.lens
    new=patch(H.item_bytes(eid), repl)
    # Exif item has no uri; replace by item id
    import replace_item
    from copy_styles_item import build_iinf, build_iloc, build_iref, box
    # reuse replace_payload by temporarily targeting the Exif item id
    orig=replace_item.find_uri_item
    replace_item.find_uri_item=lambda H,u: eid
    replace_item.replace_payload(x.target, x.out, 'EXIF', new)
    replace_item.find_uri_item=orig
    print("patched EXIF ->", x.out)
