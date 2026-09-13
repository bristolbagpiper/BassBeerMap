"""Compare every displayed listing with independently published CAMRA locations.
Agreement is evidence of consistency, not proof of a physical building location.
No correction is applied by this script.
"""
import csv,json,re,math,collections
from pathlib import Path
ROOT=Path(__file__).resolve().parent.parent

def norm(s):return re.sub('[^a-z0-9]','',re.sub(r'^the\s+','',s.strip().lower()).replace('&','and'))
def pc(s):return re.sub(r'\s+','',s or '').upper()
def variants(s):
 s=re.sub(r'\s*\(PMC\)','',s,flags=re.I)
 out=[s];m=re.search(r'\(was\s+([^)]*)\)',s,re.I)
 if m:out.extend([m.group(1),s[:m.start()].strip()])
 return {norm(x) for x in out}
def namematch(a,b):
 raw_a=a;raw_b=b;b=norm(b)
 for a in variants(raw_a):
  if a==b:return True
  for suf in ['inn','hotel','pub','bar','club','publichouse']:
   if a+suf==b or b+suf==a:return True
 generic={'the','ye','old','olde','inn','inne','arms','hotel','pub','bar','club','restaurant','tavern','house','ale','at','of','and','pmc'}
 raw_variants=[re.sub(r'\s*\(PMC\)','',raw_a,flags=re.I)]
 renamed=re.search(r'\(was\s+([^)]*)\)',raw_a,re.I)
 if renamed:raw_variants.append(renamed.group(1))
 right={t for t in re.findall(r'[a-z0-9]+',raw_b.lower()) if t not in generic}
 for value in raw_variants:
  left={t for t in re.findall(r'[a-z0-9]+',value.lower()) if t not in generic}
  if left and right and (left<=right or right<=left):return True
 return False

def dist(a,b):
 lat1,lon1,lat2,lon2=map(math.radians,(*a,*b))
 h=math.sin((lat2-lat1)/2)**2+math.cos(lat1)*math.cos(lat2)*math.sin((lon2-lon1)/2)**2
 return 6371000*2*math.asin(min(1,math.sqrt(h)))

def run():
 rows=list(csv.DictReader((ROOT/'pubs.csv').open(encoding='utf-8', newline='')));stored=json.loads((ROOT/'venue-coordinates.json').read_text())['venues'];over=json.loads((ROOT/'venue-coordinate-overrides.json').read_text())['venues'];post=json.loads((ROOT/'pub-coordinates.json').read_text())['coordinates']
 venues={};coverage=[]
 cache=ROOT/'audit/camra-cache-v2'
 cached_files=list(cache.glob('*.json')) if cache.exists() else []
 if not cached_files:
  raise SystemExit('CAMRA cache is missing; run audit/fetch_camra.py before rebuilding pin-audit artifacts.')
 for p in cached_files:
  j=json.loads(p.read_text());
  s,n,w,e=j['bounds']
  inside=[c for c in j['venues'] if c.get('Latitude') is not None and c.get('Longitude') is not None and s<=float(c['Latitude'])<=n and w<=float(c['Longitude'])<=e]
  if len(inside)>=j['total']:coverage.append(j['bounds'])
  for c in j['venues']:venues[c['IncID']]=c
 bypc=collections.defaultdict(list)
 for c in venues.values():bypc[pc(c.get('Postcode'))].append(c)
 output=[]
 for i,row in enumerate(rows,1):
  k='|'.join(row[f].strip().lower() for f in ['pub_name','place_name','postcode']);v=over.get(k,stored.get(k));fallback=post.get(row['postcode']);current=v or fallback
  point=(current['lat'],current['lng']) if current else None
  candidates=[c for c in bypc[pc(row['postcode'])] if namematch(row['pub_name'],c['Name'])]
  status='not_yet_checked';cover=bool(fallback and any(s<=fallback['lat']<=n and w<=fallback['lng']<=e for s,n,w,e in coverage))
  if cover: status='no_unambiguous_reference'
  c=None
  if len(candidates)==1:
   c=candidates[0];d=dist(point,(float(c['Latitude']),float(c['Longitude']))) if point else None
   if not v:status='postcode_pin_reference_found'
   elif d is not None and d<=50:status='reference_agrees_within_50m'
   else:status='coordinate_disagreement'
  elif len(candidates)>1:status='ambiguous_reference'
  # A directory postcode can itself be stale or mistyped. When there is no
  # exact-postcode match, an independently mapped, open CAMRA venue with the
  # same name and a point within 50m corroborates the physical pin while
  # separately surfacing the postcode conflict for correction.
  if not candidates and v and point:
   nearby=[c for c in venues.values() if c.get('PremisesStatus')!='X' and c.get('Latitude') is not None and c.get('Longitude') is not None and namematch(row['pub_name'],c['Name']) and dist(point,(float(c['Latitude']),float(c['Longitude'])))<=50]
   nearby_points={(round(float(c['Latitude']),6),round(float(c['Longitude']),6)) for c in nearby}
   if len(nearby_points)==1:
    c=nearby[0];d=dist(point,(float(c['Latitude']),float(c['Longitude'])))
    status='reference_agrees_within_50m_postcode_conflict'
  rec=dict(row_number=i,venue_key=k,**row,pin_type='venue' if v else 'postcode',current_lat=current['lat'] if current else '',current_lng=current['lng'] if current else '',current_source=v.get('source','') if v else 'postcode',audit_status=status,reference_name='',reference_street='',reference_town='',reference_postcode='',reference_lat='',reference_lng='',difference_metres='',reference_url='',reference_status='',checked_at='2026-09-11' if status!='not_yet_checked' else '')
  if c:rec.update(reference_name=c['Name'],reference_street=c['Street'],reference_town=c['Town'],reference_postcode=c['Postcode'],reference_lat=c['Latitude'],reference_lng=c['Longitude'],difference_metres=round(d,1) if d is not None else '',reference_url=f"https://camra.org.uk/pubs/{c['IncID']}",reference_status=c['PremisesStatus'])
  output.append(rec)
 with (ROOT/'audit/pin-audit.csv').open('w',encoding='utf-8',newline='') as f:
  w=csv.DictWriter(f,fieldnames=list(output[0]));w.writeheader();w.writerows(output)
 (ROOT/'audit/pin-audit.json').write_text(json.dumps(output,indent=2))
 print('Reference records',len(venues),'coverage cells',len(coverage),dict(collections.Counter(x['audit_status'] for x in output)))
 return output
if __name__=='__main__':run()
