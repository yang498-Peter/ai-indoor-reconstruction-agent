"""Dataset-independent, read-only measurement assistant for presentation work.

This helper never authors a scene, classifies rooms, or writes acceptance states.
Coordinates in its cache are [aligned x, aligned depth, height], in metres.
"""
import argparse
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import numpy as np


def sha256(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def atomic_json(path, value):
    path=Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, suffix='.tmp', delete=False) as f:
        json.dump(value, f, ensure_ascii=False, indent=2, allow_nan=False)
        temporary=f.name
    os.replace(temporary, path)


def aligned_points(xyz, origin, yaw):
    q=np.asarray(xyz, dtype=np.float64)-np.asarray(origin)
    c,s=math.cos(math.radians(yaw)),math.sin(math.radians(yaw))
    return np.column_stack((q[:,0]*c-q[:,1]*s,-q[:,0]*s-q[:,1]*c,q[:,2])).astype(np.float32)


def prepare(args):
    import laspy
    source=args.cloud.resolve(strict=True);work=args.work.resolve()
    if work==source.parent or source.parent in work.parents:
        raise ValueError('OUTPUT_INSIDE_CAPTURE: choose a work directory outside the source folder')
    if source.suffix.lower() not in ('.las','.laz'):
        raise ValueError('Only LAS/LAZ is supported; no implicit format or unit conversion')
    work.mkdir(parents=True,exist_ok=True)
    binding={'source':str(source),'sourceSha256':sha256(source),'origin':args.origin,'yawDegrees':args.yaw_deg,'lengthUnit':'metre','sourceUp':'Z','cacheAxes':['x','depth','height']}
    meta=work/'evidence-cache.json';cache=work/'evidence-cache.npz'
    if meta.exists():
        old=json.loads(meta.read_text(encoding='utf-8'))
        if any(old.get(k)!=v for k,v in binding.items()):
            raise ValueError('WORK_BINDING_MISMATCH: use a fresh work directory for another capture or transform')
        if cache.exists() and sha256(cache)==old.get('cacheSha256'):
            return {'reused':True,'work':str(work),'points':old['pointCount']}
    las=laspy.read(source)
    p=aligned_points(np.column_stack((las.x,las.y,las.z)),args.origin,args.yaw_deg)
    if not len(p) or not np.isfinite(p).all():
        raise ValueError('EMPTY_OR_NONFINITE_CLOUD')
    if all(k in set(las.point_format.dimension_names) for k in ['red','green','blue']):
        col=np.column_stack((las.red,las.green,las.blue))
        col=(col/257 if col.max()>255 else col).astype(np.uint8)
    else:
        col=np.full((len(p),3),160,np.uint8)
    with tempfile.NamedTemporaryFile(dir=work,suffix='.npz',delete=False) as f:
        np.savez(f,points=p,colors=col);temporary=f.name
    os.replace(temporary,cache)
    binding.update(pointCount=len(p),cacheSha256=sha256(cache),bounds=[p.min(0).tolist(),p.max(0).tolist()])
    atomic_json(meta,binding)
    return {'reused':False,'work':str(work),'points':len(p),'bounds':binding['bounds']}


def select_region(points,bounds):
    x0,z0,x1,z1=bounds
    if not x0<x1 or not z0<z1:
        raise ValueError('INVALID_BOUNDS: expected xmin depthmin xmax depthmax')
    return (points[:,0]>=x0)&(points[:,0]<=x1)&(points[:,1]>=z0)&(points[:,1]<=z1)


def height_peaks(points,lo,hi):
    q=points[(points[:,2]>=lo)&(points[:,2]<hi),2]
    edges=np.arange(lo,hi+.005,.01)
    hist,edges=np.histogram(q,bins=edges)
    ids=np.argsort(hist)[::-1][:6]
    return {'count':len(q),'peaks':[{'heightM':round(float((edges[i]+edges[i+1])/2),4),'points':int(hist[i])} for i in ids if hist[i]>0],
            'quantilesM':np.quantile(q,[.1,.5,.9]).tolist() if len(q) else [],
            'warning':'Height histogram only; not a fitted floor, step, or independent accuracy claim.'}


def region(args):
    import matplotlib
    matplotlib.use('Agg')
    import matplotlib.pyplot as plt
    from PIL import Image,ImageDraw
    work=args.work.resolve();meta=json.loads((work/'evidence-cache.json').read_text(encoding='utf-8'))
    cache_path=work/'evidence-cache.npz'
    if sha256(cache_path)!=meta['cacheSha256']:
        raise ValueError('STALE_CACHE: run prepare to rebuild the bound cache')
    if not re.fullmatch(r'[A-Za-z0-9_-]+',args.name):
        raise ValueError('INVALID_REGION_NAME')
    cache=np.load(cache_path);mask=select_region(cache['points'],args.bounds);p=cache['points'][mask];rgb=cache['colors'][mask]
    if not len(p):raise ValueError('EMPTY_REGION: inspect axes and bounds')
    fig,axs=plt.subplots(1,3,figsize=(16,6));bands=args.bands
    for ax,(lo,hi) in zip(axs,zip(bands[::2],bands[1::2])):
        ids=np.flatnonzero((p[:,2]>=lo)&(p[:,2]<hi));ids=ids[::max(1,math.ceil(len(ids)/180000))]
        ax.scatter(p[ids,0],p[ids,1],c=rgb[ids]/255,s=.5)
        x0,z0,x1,z1=args.bounds;ax.set(xlim=(x0,x1),ylim=(z1,z0),title=f'{args.name}: {lo:g}..{hi:g} m');ax.set_aspect('equal');ax.grid(alpha=.2)
    fig.tight_layout();image_path=work/f'{args.name}-bands.jpg';fig.savefig(image_path,dpi=110);plt.close(fig)
    photo_records=[]
    for start in range(0,len(args.photo),6):
        sheet=Image.new('RGB',(1500,1040),'#eee');draw=ImageDraw.Draw(sheet)
        for j,source in enumerate(args.photo[start:start+6]):
            source=source.resolve(strict=True)
            with Image.open(source) as im:
                original_size=im.size;im=im.convert('RGB');im.thumbnail((490,465));x=j%3*500;y=j//3*520;sheet.paste(im,(x,y+40))
            draw.text((x+5,y+5),f'{start+j+1}: {source.name[:48]}',fill='black')
            photo_records.append({'path':str(source),'sha256':sha256(source),'size':original_size,'binding':'selected observation; pose/projection not validated by this tool'})
        sheet.save(work/f'{args.name}-photos-{start//6}.jpg',quality=87)
    result={'sourceSha256':meta['sourceSha256'],'cacheSha256':meta['cacheSha256'],'bounds':args.bounds,'pointCount':len(p),'heightSummary':height_peaks(p,*args.height_range),'photos':photo_records,'bandsImage':image_path.name,'bandsImageSha256':sha256(image_path),'status':'OBSERVATIONS_ONLY'}
    atomic_json(work/f'{args.name}-observations.json',result)
    return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);subs=parser.add_subparsers(dest='command',required=True)
    p=subs.add_parser('prepare',help='Bind one metre/Z-up LAS or LAZ and cache its aligned points')
    p.add_argument('--cloud',type=Path,required=True);p.add_argument('--work',type=Path,required=True)
    p.add_argument('--origin',type=float,nargs=3,required=True);p.add_argument('--yaw-deg',type=float,required=True)
    p.add_argument('--length-unit',choices=['metre'],required=True);p.add_argument('--up-axis',choices=['Z'],required=True);p.set_defaults(run=prepare)
    p=subs.add_parser('region',help='One cached ROI, three raw bands, height hints and selected photo contacts')
    p.add_argument('--work',type=Path,required=True);p.add_argument('--name',required=True);p.add_argument('--bounds',type=float,nargs=4,required=True)
    p.add_argument('--bands',type=float,nargs=6,default=[0,.4,.65,.9,1.6,2.5]);p.add_argument('--height-range',type=float,nargs=2,default=[-.1,.4]);p.add_argument('--photo',type=Path,action='append',default=[]);p.set_defaults(run=region)
    args=parser.parse_args()
    try:
        if hasattr(args,'bands') and any(a>=b for a,b in zip(args.bands[::2],args.bands[1::2])):raise ValueError('INVALID_HEIGHT_BAND')
        if hasattr(args,'height_range') and args.height_range[1]-args.height_range[0]<.01:raise ValueError('INVALID_HEIGHT_RANGE')
        print(json.dumps(args.run(args),ensure_ascii=False,allow_nan=False))
    except (ValueError,OSError,KeyError) as error:
        parser.exit(2,json.dumps({'error':str(error),'status':'FAILED'},ensure_ascii=False)+'\n')


if __name__=='__main__':main()
