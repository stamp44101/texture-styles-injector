#!/usr/bin/env python3
"""Replace an existing uri item's payload in-place (donor payload -> target item).
Handles the size change by rebuilding mdat + iloc, like copy_styles_item does."""
import sys, struct, os
sys.path.insert(0,os.path.dirname(os.path.abspath(__file__)))
from copy_styles_item import Heif, find_uri_item, build_iinf, build_iloc, build_iref, box

def replace_payload(target_path, out_path, uri, new_payload, verbose=True):
    T=Heif(target_path)
    log=(lambda *a: print(*a)) if verbose else (lambda *a: None)
    tid=find_uri_item(T,uri)
    if tid is None: raise SystemExit(f"target has no item {uri}")
    old=T.item_bytes(tid)
    log(f"item {tid}: {len(old)} -> {len(new_payload)} bytes ({len(new_payload)-len(old):+d})")

    locs={i:dict(ctm=v['ctm'],dri=v['dri'],base=v['base'],
                 extents=[list(x) for x in v['extents']]) for i,v in T.locs.items()}
    order=sorted(T.items)
    iinf_new=build_iinf(T._iloc_order and order, T._infe_raw, T.meta.find('iinf').version)
    iref_new=build_iref([list(r) for r in T.refs], getattr(T,'iref_version',0))

    # rebuild mdat: keep every item byte-identical except the replaced one,
    # which we move to the end so offsets stay simple.
    keep=[]
    for i in T._iloc_order:
        if i==tid or locs[i]['ctm']!=0: continue
        keep.append(i)
    def assemble(locs):
        iloc_new=build_iloc(T._iloc_order,locs,T.iloc_version,T.off_sz,T.len_sz,T.base_sz,T.idx_sz)
        parts=[]
        for c in T.meta.children:
            parts.append({'iinf':iinf_new,'iloc':iloc_new,'iref':iref_new}.get(c.type,c.raw))
        return box('meta',b''.join(parts),T.meta.version,T.meta.flags)
    probe=assemble(locs)
    pre=[b.raw for b in T.top if b.type not in ('meta','mdat')]
    mdat_start=sum(len(x) for x in pre)+len(probe)+8

    blob=b''; newloc={}
    for i in keep:
        e=locs[i]; segs=[]
        for x in e['extents']:
            data=T.buf[e['base']+x[1]:e['base']+x[1]+x[2]]
            segs.append((mdat_start+len(blob),len(data))); blob+=data
        newloc[i]=segs
    tgt_off=mdat_start+len(blob); blob+=new_payload

    for i,segs in newloc.items():
        locs[i]['base']=0
        locs[i]['extents']=[[locs[i]['extents'][j][0],segs[j][0],segs[j][1]] for j in range(len(segs))]
    locs[tid]['base']=0; locs[tid]['ctm']=0
    locs[tid]['extents']=[[locs[tid]['extents'][0][0],tgt_off,len(new_payload)]]

    final=assemble(locs)
    if len(final)!=len(probe):
        d=len(final)-len(probe)
        for i,e in locs.items():
            if e['ctm']!=0: continue
            for x in e['extents']: x[1]+=d
        final=assemble(locs)
    out=b''.join(pre)+final+struct.pack('>I',len(blob)+8)+b'mdat'+blob
    open(out_path,'wb').write(out)
    log(f"wrote {out_path} {len(out)} bytes")
    return out_path

if __name__=='__main__':
    import argparse
    a=argparse.ArgumentParser()
    a.add_argument('--target',required=True); a.add_argument('--out',required=True)
    a.add_argument('--uri',required=True); a.add_argument('--payload',required=True)
    x=a.parse_args()
    replace_payload(x.target,x.out,x.uri,open(x.payload,'rb').read())
