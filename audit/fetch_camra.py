"""Read the public CAMRA map for the geographic cells containing our listings.
Uses the same read-only bounds-changed event as the public map. No login.
Caches factual identity/location fields only; does not copy descriptions/photos.
"""
import urllib.request,http.cookiejar,json,re,html,csv,math,time,threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent
CACHE=ROOT/'audit'/'camra-cache-v2';CACHE.mkdir(exist_ok=True)
LOCAL=threading.local()

def start():
 LOCAL.op=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
 LOCAL.op.addheaders=[("User-Agent","BassBeerMap coordinate audit (github.com/bristolbagpiper/BassBeerMap)")]
 s=LOCAL.op.open('https://camra.org.uk/pubs/location/54.775327/-1.572234?map=true',timeout=45).read().decode()
 LOCAL.token=re.search(r'<meta name="csrf-token" content="([^"]+)',s).group(1)
 for raw in re.findall(r'wire:snapshot="([^"]+)"',s):
  LOCAL.snapshot=html.unescape(raw)
  if 'bounds' in json.loads(LOCAL.snapshot)['data']:return
 raise ValueError('No map snapshot')

def fetch(bounds):
 if not hasattr(LOCAL,"op"):start()
 south,north,west,east=bounds
 body={'_token':LOCAL.token,'components':[{'snapshot':LOCAL.snapshot,'updates':{'hide_closed':False},'calls':[{'path':'','method':'__dispatch','params':['bounds-changed',[dict(south=south,north=north,west=west,east=east),{'lat':(south+north)/2,'lng':(west+east)/2}]]},{'path':'','method':'__dispatch','params':['map-loaded',[]]}]}]}
 req=urllib.request.Request('https://camra.org.uk/livewire/update',data=json.dumps(body).encode(),headers={'Content-Type':'application/json','X-Livewire':'','Accept':'application/json'})
 with LOCAL.op.open(req,timeout=90) as r:j=json.load(r)
 c=j['components'][0];LOCAL.snapshot=c['snapshot'];total=json.loads(LOCAL.snapshot)['data']['venueTotal']
 venues=[]
 for d in c['effects'].get('dispatches',[]):
  if d['name']=='venues-updated':
   records=d['params']['venues']
   for v in (records.values() if isinstance(records,dict) else records):
    if not isinstance(v,dict):continue
    venues.append({k:v.get(k) for k in ['IncID','PubID','Name','Town','Street','Posttown','Postcode','Latitude','Longitude','PremisesStatus']})
 venues=list({v['IncID']:v for v in venues}.values())
 return dict(bounds=bounds,total=total,venues=venues,checked_at='2026-09-11',source='https://camra.org.uk/pubs?map=true')

def cell(bounds):
 s,n,w,e=bounds
 if not any(s<=lat<=n and w<=lng<=e for lat,lng in TARGETS):return
 p=CACHE/('_'.join(map(str,bounds))+'.json')
 if p.exists():j=json.loads(p.read_text())
 else:
  for attempt in range(3):
   try:j=fetch(bounds);p.write_text(json.dumps(j));break
   except Exception as e:
    print('ERROR',bounds,type(e).__name__,str(e)[:100],flush=True)
    if attempt==2:return
    start()
  time.sleep(1)
 print('CELL',bounds,'returned',len(j['venues']),'total',j['total'],flush=True)
 s,n,w,e=bounds
 inside=[v for v in j['venues'] if v.get('Latitude') is not None and v.get('Longitude') is not None and s<=float(v['Latitude'])<=n and w<=float(v['Longitude'])<=e]
 if j['total']>len(inside):
  s,n,w,e=bounds
  if n-s<.04: print('INCOMPLETE',bounds,flush=True);return
  for b in [(s,(s+n)/2,w,(w+e)/2),(s,(s+n)/2,(w+e)/2,e),((s+n)/2,n,w,(w+e)/2),((s+n)/2,n,(w+e)/2,e)]:cell(b)

if __name__=='__main__':
 rows=list(csv.DictReader((ROOT/'pubs.csv').open(encoding='utf-8', newline='')));pc=json.loads((ROOT/'pub-coordinates.json').read_text())['coordinates']
 TARGETS=[(pc[r['postcode']]['lat'],pc[r['postcode']]['lng']) for r in rows if r['postcode'] in pc]
 cells=sorted({(math.floor(pc[r['postcode']]['lat']),math.floor(pc[r['postcode']]['lng'])) for r in rows if r['postcode'] in pc})
 def worker(item):
  i,(lat,lng)=item
  print('PROGRESS',i,'/',len(cells),flush=True);cell((lat,lat+1,lng,lng+1))
 with ThreadPoolExecutor(max_workers=3) as pool:
  for _ in pool.map(worker,enumerate(cells,1)):pass
