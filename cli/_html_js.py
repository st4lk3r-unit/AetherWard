_HTML_JS = r"""<script>
// ── State ─────────────────────────────────────────────────────────────────────
const srcs = {}, mkrs = {};
const _relLines = {}, _sampleLines = [];
const _sourceSamples = {}, _sourceSamplesByAp = {};
const _networkRelations = {};
const _sourceSampleFetches = {};
let map = null, totalUpd = 0;
const STEPS = 7;
let wStep = 1;
const W = {
  configName:'my-config',
  mode:'wardriver', antennas:[{id:'wlan0',preset:'wifi24',freqMin:2400000000,freqMax:2500000000,backend:'plugins.wifi_nl80211.NL80211Backend',x:0,y:0,z:0}],
  gps:'gpsd', lat:0, lon:0, alt:0,
  sync:'software', syncDev:'',
  outputPolicy:'default', output:'',
  // mode-specific advanced config (mirrors CLI wizard custom)
  channels:'1,2,3,4,5,6,7,8,9,10,11,12,13', hopInterval:0.1,
  triChannel:6, corrWindow:0.001, groupTimeout:0.05,
  senseChannel:6, historyLen:100, calibFrames:50,
  sensitivity:0.05, hysteresis:0.4, emaAlpha:0.3,
};

const PRESETS = {
  wifi24:{l:'WiFi 2.4 GHz',lo:2400000000,hi:2500000000},
  wifi5: {l:'WiFi 5 GHz',  lo:5150000000,hi:5850000000},
  wifi6e:{l:'WiFi 6E',     lo:5925000000,hi:7125000000},
  bt:    {l:'Bluetooth',   lo:2400000000,hi:2500000000},
  lte700:{l:'LTE 700',     lo:699000000, hi:960000000},
  gsm900:{l:'GSM 900',     lo:880000000, hi:960000000},
  custom:{l:'Custom',      lo:null,      hi:null},
};
const BACKENDS = [
  {id:'plugins.wifi_nl80211.NL80211Backend', l:'WiFi / NL80211 (monitor mode)'},
  {id:'plugins.rtlsdr.RTLSDRBackend',        l:'RTL-SDR dongle'},
  {id:'plugins.hackrf.HackRFBackend',        l:'HackRF'},
  {id:'null',                                l:'Null / simulated'},
];

// ── Tabs ──────────────────────────────────────────────────────────────────────
function tab(btn) {
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.querySelectorAll('nav button').forEach(b=>b.classList.remove('active'));
  const name = btn.dataset.tab;
  document.getElementById('panel-'+name).classList.add('active');
  btn.classList.add('active');
  if (name==='map')       { initMap(); if(map) map.invalidateSize(); refreshMapStats(); _renderPathsList(); _updatePathsToggleBtn(); }
  if (name==='positions') renderPositions();
  if (name==='enu')       { requestAnimationFrame(enuRender); }
  if (name==='tdoa3d')    { loadConfigs(); requestAnimationFrame(tdoa3dRender); }
  if (name==='sessions')  loadSessions();
  if (name==='configs')  loadConfigs();
  if (name==='solve')    { loadSessions(); loadConfigs(); }
  if (name==='run')      loadConfigs();
  if (name==='settings') {
    fetch('/api/status').then(r=>r.json()).then(d=>{
      const tb=document.getElementById('settings-sys-tb');
      if(tb) tb.innerHTML=Object.entries(d).map(([k,v])=>
        `<tr><td style="color:var(--mu);width:160px">${k}</td><td>${v}</td></tr>`).join('');
    });
    loadDetect();
  }
}

// ── Map ───────────────────────────────────────────────────────────────────────
// ── Map filter state ──────────────────────────────────────────────────────────
let _mapFilter='all';
let _mapRoleFilter='all', _mapShowRelations=true, _selectedSourceId=null;
const _PATH_COLORS=['#ff3c3c','#3c9eff','#3cff6e','#ffcc3c','#cc3cff','#ff3c8e','#ff8c3c','#3cffee'];
let _loadedPaths=[];

function _updateMapCenter(){
  if(!map) return;
  const c=map.getCenter(), el=document.getElementById('map-center');
  if(el) el.textContent=c.lat.toFixed(6)+',  '+c.lng.toFixed(6);
}
function _pointInCurrentView(lat, lon){
  if(!map||lat==null||lon==null) return false;
  try{return map.getBounds().contains([+lat,+lon]);}catch(e){return false;}
}
function _sourceShownInCurrentView(rec){
  if(!map||!rec||!rec.lat||!rec.lon) return false;
  const id=String(rec.id||rec.bssid||'?');
  const mk=mkrs[id];
  return !!(mk&&map.hasLayer(mk)&&_pointInCurrentView(rec.lat,rec.lon));
}
const _TILE_SOURCES = {
  'CartoDB Dark':   {url:'https://{s}.basemaps.cartocdn.com/dark_all/{z}/{x}/{y}{r}.png',    opts:{subdomains:'abcd',maxZoom:19,attribution:'&copy; <a href="https://openstreetmap.org">OSM</a> &copy; <a href="https://carto.com">CARTO</a>'}},
  'CartoDB No Lbl': {url:'https://{s}.basemaps.cartocdn.com/dark_nolabels/{z}/{x}/{y}{r}.png',opts:{subdomains:'abcd',maxZoom:19,attribution:'&copy; <a href="https://openstreetmap.org">OSM</a> &copy; <a href="https://carto.com">CARTO</a>'}},
  'Stadia Dark':    {url:'https://tiles.stadiamaps.com/tiles/alidade_smooth_dark/{z}/{x}/{y}{r}.png',opts:{maxZoom:20,attribution:'&copy; <a href="https://stadiamaps.com">Stadia Maps</a> &copy; <a href="https://openstreetmap.org">OSM</a>'}},
  'ESRI Dark Gray': {url:'https://server.arcgisonline.com/ArcGIS/rest/services/Canvas/World_Dark_Gray_Base/MapServer/tile/{z}/{y}/{x}',opts:{maxZoom:16,attribution:'&copy; <a href="https://esri.com">Esri</a>'}},
};
let _tileLayer = null;
function _switchTile(name) {
  const src = _TILE_SOURCES[name]; if (!src || !map) return;
  if (_tileLayer) { map.removeLayer(_tileLayer); }
  _tileLayer = L.tileLayer(src.url, src.opts).addTo(map);
  localStorage.setItem('aw_tile', name);
  document.querySelectorAll('.tile-btn').forEach(b => b.classList.toggle('tile-btn-active', b.dataset.tile === name));
}
function initMap() {
  if (map) return;
  map = L.map('map',{preferCanvas:true}).setView([48.8566,2.3522],13);
  const saved = localStorage.getItem('aw_tile') || 'CartoDB Dark';
  _switchTile(Object.keys(_TILE_SOURCES).includes(saved) ? saved : 'CartoDB Dark');
  // Keep network relation overlays above route/path polylines but below markers.
  // Otherwise AP↔client links can be visually swallowed by route previews.
  if(!map.getPane('aw-relations')){
    map.createPane('aw-relations');
    map.getPane('aw-relations').style.zIndex = 455;
  }
  if(!map.getPane('aw-samples')){
    map.createPane('aw-samples');
    map.getPane('aw-samples').style.zIndex = 460;
  }
  map.on('move',_updateMapCenter);
  map.on('moveend zoomend',()=>{_refreshRelationshipLines();refreshMapStats();});
  map.on('click',_clearSampleLines);
  _updateMapCenter();
  Object.values(srcs).forEach(updateMarker);
  _refreshRelationshipLines();
  // Activate saved button after DOM is ready
  document.querySelectorAll('.tile-btn').forEach(b => b.classList.toggle('tile-btn-active', b.dataset.tile === saved));
}
function mkColor(m){return m==='tdoa'?'#ff3c3c':m==='rssi_centroid'?'#f0c040':m==='manual'?'#706082':'#60a5fa'}
function _normId(v){return String(v||'').trim().toLowerCase();}
function _badId(v){v=_normId(v);return !v||v==='ff:ff:ff:ff:ff:ff'||v==='00:00:00:00:00:00'||v==='none'||v==='null';}
function _cleanSSID(v){return String(v||'').trim().toLowerCase()==='defaultssid'?'':v;}
function _displaySSID(v){v=_cleanSSID(v);return (v==null||String(v)==='')?'<empty SSID>':String(v);}
function _firstSpecificRecVal(rec, keys){
  for(const k of keys){const v=_directRecVal(rec,k); if(v!=null&&v!==''&&!(Array.isArray(v)&&!v.length)) return v;}
  return undefined;
}
function _boolish(v){return v===true||v===1||v==='1'||String(v).toLowerCase()==='true';}
const _REC_ALIASES={
  associated_bssid:['associated_bssid','associated','associated_ap','ap_bssid','ap_mac','ap'],
  linked_client:['linked_client','linked_station','associated_client','client','station','sta'],
  bssid:['bssid','associated_bssid','associated','associated_ap','ap_bssid','ap_mac','ap'],
  client:['client','station','sta','linked_client'],
  station:['station','client','sta','linked_client'],
  source_role:['source_role','role','kind'],
  id:['id','identifier','mac','bssid','client','station'],
  identifier:['identifier','id','mac','bssid','client','station'],
};
function _directRecVal(rec,k){
  if(!rec) return undefined;
  if(rec[k]!=null&&rec[k]!==''&&!(Array.isArray(rec[k])&&!rec[k].length)) return rec[k];
  const m=rec.metadata||{};
  if(m[k]!=null&&m[k]!==''&&!(Array.isArray(m[k])&&!m[k].length)) return m[k];
  return undefined;
}
function _recVal(rec,k){
  const keys=_REC_ALIASES[k]||[k];
  for(const kk of keys){
    const v=_directRecVal(rec,kk);
    if(v!=null&&v!==''&&!(Array.isArray(v)&&!v.length)) return v;
  }
  return undefined;
}
function _srcRole(rec){
  const r=String(_recVal(rec,'source_role')||_recVal(rec,'role')||_recVal(rec,'kind')||'').toLowerCase();
  if(['ap','access_point','access-point'].includes(r)) return 'ap';
  if(['client','station','sta'].includes(r)) return 'client';
  const st=String(_recVal(rec,'frame_subtype')||'').toLowerCase();
  if(st==='beacon'||st==='probe_resp') return 'ap';
  const assoc=_firstSpecificRecVal(rec,['associated_bssid','associated','associated_ap','ap_bssid','ap_mac','ap']);
  const directClient=_directRecVal(rec,'client')||_directRecVal(rec,'station')||_directRecVal(rec,'sta');
  if(st==='probe_req'||assoc||directClient) return 'client';
  const linked=_firstSpecificRecVal(rec,['linked_client','linked_station','associated_client']);
  if(linked) return 'ap';
  const id=_normId(_directRecVal(rec,'id')||_directRecVal(rec,'identifier'));
  const bssid=_normId(_directRecVal(rec,'bssid'));
  if(id&&bssid&&id!==bssid) return 'client';
  if(id&&bssid&&id===bssid) return 'ap';
  if((_cleanSSID(rec.ssid)||rec.auth_mode||rec.security)&&bssid) return 'ap';
  return 'unknown';
}
function _roleColor(role){return role==='ap'?'#a855f7':role==='client'?'#00d4c8':'#706082';}
function _roleLabel(role){return role==='ap'?'AP':role==='client'?'Client':'Other';}
function _sourcePrimaryId(rec){return _normId(_directRecVal(rec,'id')||_directRecVal(rec,'identifier')||_directRecVal(rec,'client')||_directRecVal(rec,'station')||_directRecVal(rec,'bssid'));}
function _apIdFor(rec){
  const id=_sourcePrimaryId(rec);
  let ap=_normId(_firstSpecificRecVal(rec,['associated_bssid','associated','associated_ap','ap_bssid','ap_mac','ap']));
  if(ap&&ap!==id&&!_badId(ap)) return ap;
  // For client-originated data frames, bssid is often the AP.  Use it only as
  // a guarded fallback when it is not the source itself.
  const bssid=_normId(_directRecVal(rec,'bssid'));
  if(_srcRole(rec)==='client'&&bssid&&bssid!==id&&!_badId(bssid)) return bssid;
  return '';
}
function _candidateIds(rec){
  return [_directRecVal(rec,'id'),_directRecVal(rec,'identifier'),_directRecVal(rec,'bssid'),_directRecVal(rec,'client'),_directRecVal(rec,'station'),_directRecVal(rec,'mac')]
    .map(_normId).filter(v=>v&&!_badId(v));
}
function _findSourceByMac(mac, wantRole){
  const n=_normId(mac); if(!n||_badId(n)) return null;
  let unknown=null;
  for(const rec of Object.values(srcs)){
    const role=_srcRole(rec);
    const ids=_candidateIds(rec);
    if(ids.includes(n)){
      if(!wantRole||role===wantRole) return rec;
      if(role==='unknown') unknown=unknown||rec;
    }
  }
  return unknown;
}
function mkIcon(rec, selected=false){
  const method=mkColor(rec?.pos_method), role=_srcRole(rec), fill=_roleColor(role);
  const ap=role==='ap', sz=selected?18:(ap?15:13), r=ap?'4px':'50%';
  const pulse=selected?`box-shadow:0 0 0 3px rgba(255,60,60,.38),0 0 14px ${fill}cc`:`box-shadow:0 0 8px ${fill}88`;
  return L.divIcon({className:'',
    html:`<div title="${_roleLabel(role)}" style="width:${sz}px;height:${sz}px;border-radius:${r};background:${fill};border:2px solid ${method};${pulse}"></div>`,
    iconSize:[sz,sz],iconAnchor:[Math.round(sz/2),Math.round(sz/2)]});
}

let _mapSearchSSID='', _mapSearchMAC='', _mapSearchRadius=0, _mapSearchCenter=null;

function _haversine(lat1,lon1,lat2,lon2){
  const R=6371000,dLat=(lat2-lat1)*Math.PI/180,dLon=(lon2-lon1)*Math.PI/180;
  const a=Math.sin(dLat/2)**2+Math.cos(lat1*Math.PI/180)*Math.cos(lat2*Math.PI/180)*Math.sin(dLon/2)**2;
  return R*2*Math.atan2(Math.sqrt(a),Math.sqrt(1-a));
}
function _markerVisible(rec){
  if(_mapFilter!=='all'&&_mapFilter!==rec.pos_method) return false;
  if(_mapRoleFilter!=='all'&&_mapRoleFilter!==_srcRole(rec)) return false;
  if(_mapSearchSSID&&!(rec.ssid||'').toLowerCase().includes(_mapSearchSSID.toLowerCase())) return false;
  if(_mapSearchMAC){
    const q=_mapSearchMAC.toLowerCase();
    const hay=['id','identifier','bssid','associated_bssid','client','station','linked_client','ssid'].map(k=>String(_recVal(rec,k)||'').toLowerCase()).join(' ');
    if(!hay.includes(q)) return false;
  }
  if(_mapSearchRadius>0&&_mapSearchCenter&&rec.lat&&rec.lon){
    if(_haversine(_mapSearchCenter.lat,_mapSearchCenter.lon,rec.lat,rec.lon)>_mapSearchRadius) return false;
  }
  return true;
}
function applyMapFilters(){
  if(!map) return;
  Object.entries(mkrs).forEach(([id,mk])=>{
    const rec=srcs[id]; if(!rec) return;
    if(_markerVisible(rec)) mk.addTo(map); else mk.remove();
  });
  _refreshRelationshipLines();
  if(_selectedSourceId) _showSourceSamples(_selectedSourceId);
  refreshMapStats();
}
function setRadiusCenter(){
  if(!map){alert('Open the Map tab first.');return;}
  const c=map.getCenter();
  _mapSearchCenter={lat:c.lat,lon:c.lng};
  const btn=document.getElementById('map-s-center-btn');
  if(btn){btn.style.color='var(--acc)';btn.style.borderColor='var(--acc)';}
  applyMapFilters();
}

let _mapSearchHydrateTimer=null;
const _mapSearchHydrated={};
function mapMacSearchChanged(v){
  _mapSearchMAC=String(v||'').trim();
  applyMapFilters();
  if(_mapSearchHydrateTimer) clearTimeout(_mapSearchHydrateTimer);
  _mapSearchHydrateTimer=setTimeout(_hydrateMapSearchSource,280);
}
function _looksLikeSourceQuery(q){
  q=String(q||'').trim().toLowerCase();
  if(!q) return false;
  if(q.includes(':')||q.includes('-')) return q.replace(/[^0-9a-f]/g,'').length>=6;
  return q.length>=8;
}
function _rowsToSearchSourceRecord(q, rows){
  rows=(rows||[]).filter(r=>r&&r.lat!=null&&r.lon!=null);
  if(!rows.length) return null;
  const id=_normId(q);
  const first=rows.find(r=>String(_cleanSSID(r.ssid)||'').trim())||rows[0];
  let sw=0, lat=0, lon=0, best=rows[0];
  rows.forEach(r=>{
    const w=Math.max(1, Math.min(80, 110+(Number(r.rssi??-85))));
    sw+=w; lat+=(+r.lat)*w; lon+=(+r.lon)*w;
    if((r.rssi??-999)>(best.rssi??-999)) best=r;
  });
  if(!sw){lat=best.lat;lon=best.lon;} else {lat/=sw;lon/=sw;}
  const meta=first.metadata||{};
  const rec={...meta,...first,id:id||String(first.id||first.bssid||q),identifier:id||String(first.id||q),
    lat,lon,t:best.t||first.t||0,rssi:best.rssi??first.rssi,
    samples:first.sampled_total||rows.length,raw_samples:first.sampled_total||rows.length,
    pos_method:'session_sample_centroid',source_role:first.source_role||meta.source_role||first.role||meta.role||undefined};
  if(!rec.ssid) rec.ssid=first.ssid||meta.ssid||'';
  if(!rec.protocol) rec.protocol=first.protocol||meta.protocol||'';
  return rec;
}
function _hydrateMapSearchSource(){
  const q=String(_mapSearchMAC||'').trim();
  const key=_normId(q);
  if(!key||!_looksLikeSourceQuery(q)||!_loadedPaths.length) return;
  if(_mapSearchHydrated[key]==='loading'||_mapSearchHydrated[key]==='done') return;
  // If the source is already present in solved markers, filtering was enough.
  const already=Object.values(srcs).some(r=>{
    const hay=['id','identifier','bssid','associated_bssid','client','station','linked_client','ssid'].map(k=>String(_recVal(r,k)||'').toLowerCase()).join(' ');
    return hay.includes(key);
  });
  if(already) return;
  _mapSearchHydrated[key]='loading';
  const paths=[...new Set(_loadedPaths.map(p=>p.path).filter(Boolean))];
  let all=[];
  let chain=Promise.resolve();
  paths.forEach(path=>{
    chain=chain.then(()=>{
      const u='/api/session/source_samples?path='+encodeURIComponent(path)+'&source='+encodeURIComponent(q)+'&role=&max_obs=800';
      return fetch(u).then(r=>r.json()).then(rows=>{
        if(Array.isArray(rows)&&rows.length){
          _indexSamplesFromRecords(path+'#search:'+key,rows,{replace:true});
          all=all.concat(rows);
        }
      }).catch(()=>{});
    });
  });
  chain.finally(()=>{
    _mapSearchHydrated[key]='done';
    const rec=_rowsToSearchSourceRecord(q, all);
    if(rec){
      srcs[rec.id]=rec;
      updateMarker(rec);
      sysLog('Hydrated searched source from loaded session paths: '+rec.id+' ('+(rec.samples||all.length)+' sample records)');
      applyMapFilters();
    } else {
      refreshMapStats();
    }
  });
}

function _recordSourceId(r){
  const m=r.metadata||{};
  return String(r.id||r.identifier||m.identifier||m.id||m.bssid||m.station||m.client||r.bssid||r.station||r.client||r.mac||m.mac||'');
}
function _recordApId(r){
  const ap=_normId(_firstSpecificRecVal(r,['associated_bssid','associated','associated_ap','ap_bssid','ap_mac','ap']));
  const sid=_normId(_recordSourceId(r));
  if(ap&&ap!==sid&&!_badId(ap)) return ap;
  const bssid=_normId(_directRecVal(r,'bssid'));
  if(bssid&&bssid!==sid&&!_badId(bssid)) return bssid;
  return '';
}
function _addSampleIndex(key, sample){
  key=_normId(key); if(!key||_badId(key)) return;
  (_sourceSamples[key]||= []).push(sample);
}
function _relationPairsFromRecord(r){
  const pairs=[];
  const sid=_normId(_recordSourceId(r));
  const role=_srcRole(r);
  const add=(client,ap)=>{
    client=_normId(client); ap=_normId(ap);
    if(_badId(client)||_badId(ap)||client===ap) return;
    pairs.push({client,ap});
  };
  const ap=_apIdFor(r);
  if(role==='client'&&ap) add(sid,ap);
  const linked=_normId(_firstSpecificRecVal(r,['linked_client','linked_station','associated_client']));
  if(role==='ap'&&linked) add(linked,sid);

  // Infer infrastructure relations directly from 802.11 address roles when
  // the capture preserved addr1/addr2/addr3 and DS flags.
  const a1=_normId(_directRecVal(r,'addr1'));
  const a2=_normId(_directRecVal(r,'addr2'));
  const a3=_normId(_directRecVal(r,'addr3'));
  const to=_boolish(_recVal(r,'to_ds')), from=_boolish(_recVal(r,'from_ds'));
  const st=String(_recVal(r,'frame_subtype')||'').toLowerCase();
  if(to&&!from) add(a2,a1);
  else if(from&&!to) add(a1,a2);
  else if(['assoc_req','reassoc_req','association_req','reassociation_req'].includes(st)) add(a2,a1||a3);
  else if(['assoc_resp','reassoc_resp','association_resp','reassociation_resp'].includes(st)) add(a1,a2||a3);
  return pairs;
}
function _relationKey(client,ap){return [_normId(client),_normId(ap)].join('->');}
function _indexRelationsFromRecords(path,recs){
  Object.keys(_networkRelations).forEach(k=>{_networkRelations[k]=(_networkRelations[k]||[]).filter(x=>x.path!==path&&!String(x.path||'').startsWith(path+'#')); if(!_networkRelations[k].length) delete _networkRelations[k];});
  recs.forEach((r,i)=>{
    _relationPairsFromRecord(r).forEach(pair=>{
      const key=_relationKey(pair.client,pair.ap);
      (_networkRelations[key]||=[]).push({path,client:pair.client,ap:pair.ap,idx:i});
    });
  });
}
function _indexSamplesFromRecords(path,recs,opts){
  opts=opts||{};
  const replace=opts.replace!==false;
  // Rebuild only entries for this path, so reloading a session does not double-count dots.
  if(replace){
    for(const bucket of [_sourceSamples,_sourceSamplesByAp]){
      Object.keys(bucket).forEach(k=>{bucket[k]=(bucket[k]||[]).filter(s=>s.path!==path&&!String(s.path||'').startsWith(path+'#')); if(!bucket[k].length) delete bucket[k];});
    }
  }
  recs.forEach((r,i)=>{
    if((r.record_type==='gps'||r.source==='gps')||r.lat==null||r.lon==null) return;
    const sample={path,lat:+r.lat,lon:+r.lon,t:r.t||0,rssi:r.rssi??null,idx:i};
    const sid=_recordSourceId(r);
    _addSampleIndex(sid,sample);
    const ap=_recordApId(r);
    if(ap) (_sourceSamplesByAp[ap]||=[]).push(sample);
  });
  _indexRelationsFromRecords(path,recs);
}
function _uniqSamples(samples){
  const seen=new Set(), out=[];
  samples.forEach(s=>{
    const k=[s.path,(+s.lat).toFixed(7),(+s.lon).toFixed(7),Math.round((s.t||0)*1000),s.idx].join('|');
    if(!seen.has(k)){seen.add(k);out.push(s);}
  });
  return out;
}
function _baseSamplePath(path){
  return String(path||'').split('#')[0];
}
function _colorForSamplePath(path){
  const base=_baseSamplePath(path);
  const hit=_loadedPaths.find(p=>p.path===base);
  return hit?.color || '#ff2020';
}
function _samplesForSource(rec){
  if(!rec) return [];
  const id=_sourcePrimaryId(rec);
  let arr=[...(_sourceSamples[id]||[])];
  if(_srcRole(rec)==='ap'){
    for(const k of _candidateIds(rec)) arr=arr.concat(_sourceSamplesByAp[k]||[]);
  }
  return _uniqSamples(arr);
}
function _clearSampleLines(){
  _sampleLines.splice(0).forEach(l=>l.remove());
  _selectedSourceId=null;
  Object.entries(mkrs).forEach(([id,m])=>{const rec=srcs[id]; if(rec)m.setIcon(mkIcon(rec,false));});
  const stat=document.getElementById('map-link-count'); if(stat) stat.textContent='No selected source';
  refreshMapStats();
}
function _updateSelectedSampleStat(){
  const stat=document.getElementById('map-link-count'); if(!stat) return;
  if(!_selectedSourceId){stat.textContent='No selected source';return;}
  const rec=srcs[_selectedSourceId];
  if(!rec){stat.textContent='No selected source';return;}
  const key=_sourcePrimaryId(rec);
  const samples=_samplesForSource(rec);
  const inView=samples.filter(s=>_pointInCurrentView(s.lat,s.lon)).length;
  if(samples.length) stat.textContent=`${inView}/${samples.length} selected sample link${samples.length!==1?'s':''} in view`;
  else if(_sourceSampleFetches[key]==='loading') stat.textContent='Loading linked samples…';
  else stat.textContent='No linked samples loaded';
}
function _drawSourceSampleLines(id){
  _sampleLines.splice(0).forEach(l=>l.remove());
  const rec=srcs[id];
  if(!rec||!rec.lat||!rec.lon||!_markerVisible(rec)){_clearSampleLines();return;}
  Object.entries(mkrs).forEach(([mid,m])=>{const r=srcs[mid]; if(r)m.setIcon(mkIcon(r,mid===id));});
  const samples=_samplesForSource(rec);
  samples.forEach(s=>{
    const color=_colorForSamplePath(s.path);
    const line=L.polyline([[rec.lat,rec.lon],[s.lat,s.lon]],{pane:'aw-samples',color,weight:1.6,opacity:.68,interactive:false}).addTo(map);
    const dot=L.circleMarker([s.lat,s.lon],{pane:'aw-samples',radius:3,color,fillColor:color,fillOpacity:.9,weight:1,interactive:false}).addTo(map);
    _sampleLines.push(line,dot);
  });
  _updateSelectedSampleStat();
  refreshMapStats();
}
function _loadSamplesForSource(id,rec){
  const key=_sourcePrimaryId(rec)||_normId(id);
  if(!key||_sourceSampleFetches[key]==='loading'||_sourceSampleFetches[key]==='done') return Promise.resolve();
  const paths=[...new Set(_loadedPaths.map(p=>p.path).filter(Boolean))];
  if(!paths.length) return Promise.resolve();
  _sourceSampleFetches[key]='loading';
  _updateSelectedSampleStat();
  const role=_srcRole(rec);
  let chain=Promise.resolve();
  paths.forEach(path=>{
    chain=chain.then(()=>{
      const u='/api/session/source_samples?path='+encodeURIComponent(path)+'&source='+encodeURIComponent(key)+'&role='+encodeURIComponent(role)+'&max_obs=1500';
      return fetch(u).then(r=>r.json()).then(rows=>{
        if(Array.isArray(rows)&&rows.length){
          _indexSamplesFromRecords(path+'#samples:'+key,rows,{replace:true});
          _refreshRelationshipLines();
        }
      }).catch(()=>{});
    });
  });
  return chain.finally(()=>{_sourceSampleFetches[key]='done'; if(_selectedSourceId===id)_drawSourceSampleLines(id);});
}
function _showSourceSamples(id){
  if(!map) return;
  const rec=srcs[id];
  if(!rec||!rec.lat||!rec.lon||!_markerVisible(rec)){_clearSampleLines();return;}
  _selectedSourceId=id;
  _drawSourceSampleLines(id);
  if(!_samplesForSource(rec).length) _loadSamplesForSource(id,rec);
}
function _visibleSourceRec(rec){
  return !!(rec&&rec.lat!=null&&rec.lon!=null&&_markerVisible(rec)&&_sourceShownInCurrentView(rec));
}
function _relationIdFor(rec){
  return _normId(_recVal(rec,'id')||_recVal(rec,'identifier')||_recVal(rec,'bssid')||_recVal(rec,'client')||_recVal(rec,'station'));
}
function _addRelationLine(keep, a, b, label){
  if(!map||!_visibleSourceRec(a)||!_visibleSourceRec(b)) return;
  const aid=_relationIdFor(a), bid=_relationIdFor(b);
  if(!aid||!bid||aid===bid) return;
  const key=[aid,bid].sort().join('<>');
  keep.add(key);
  const latlngs=[[a.lat,a.lon],[b.lat,b.lon]];
  if(_relLines[key]) _relLines[key].setLatLngs(latlngs);
  else _relLines[key]=L.polyline(latlngs,{pane:'aw-relations',color:'#ff8c42',weight:2.2,opacity:.88,dashArray:'5 5',interactive:false}).addTo(map);
  try{_relLines[key].bringToFront();}catch(e){}
}
function _refreshRelationshipLines(){
  if(!map) return;
  const keep=new Set();
  if(_mapShowRelations){
    Object.entries(srcs).forEach(([sid,rec])=>{
      _relationPairsFromRecord(rec).forEach(pair=>{
        const client=_findSourceByMac(pair.client,'client');
        const ap=_findSourceByMac(pair.ap,'ap');
        if(client&&ap) _addRelationLine(keep, client, ap, 'traffic');
      });
    });
    Object.values(_networkRelations).forEach(list=>{
      (list||[]).forEach(pair=>{
        const client=_findSourceByMac(pair.client,'client');
        const ap=_findSourceByMac(pair.ap,'ap');
        if(client&&ap) _addRelationLine(keep, client, ap, 'indexed');
      });
    });
  }
  Object.keys(_relLines).forEach(k=>{if(!keep.has(k)){_relLines[k].remove();delete _relLines[k];}});
  refreshMapStats();
}
function setMapRoleFilter(btn){
  _mapRoleFilter=btn.dataset.role;
  document.querySelectorAll('.mfpill-role').forEach(b=>b.classList.toggle('active',b===btn));
  applyMapFilters();
}
function toggleMapRelations(btn){
  _mapShowRelations=!_mapShowRelations;
  if(btn){btn.textContent=_mapShowRelations?'Relations ●':'Relations ○';btn.style.opacity=_mapShowRelations?'1':'.5';}
  _refreshRelationshipLines();
}
function updateMarker(rec){
  if(!map||!rec.lat||!rec.lon)return;
  const id=String(rec.id||rec.bssid||'?'), lbl=_displaySSID(rec.ssid||'').slice(0,36);
  const role=_srcRole(rec), roleTxt=_roleLabel(role), apId=_apIdFor(rec);
  const res=rec.residual_dBm!=null?`<span style="color:var(--ylw)">Residual:</span> ${rec.residual_dBm} dB<br>`:
            rec.residual_m!=null?`<span style="color:var(--ylw)">Residual:</span> ${(+rec.residual_m).toFixed(2)} m<br>`:
            rec.rss_residual!=null?`<span style="color:var(--ylw)">Residual:</span> ${rec.rss_residual} dB<br>`:'';
  rec.ssid=_cleanSSID(rec.ssid);
  const sec=rec.auth_mode||rec.security||'';
  const ch=rec.channel?` &nbsp; <span style="color:var(--mu)">Ch:</span> ${_xmlEsc(rec.channel)}`:'';
  const bssid=rec.bssid&&rec.bssid!==id?`<span style="color:var(--mu)">BSSID/AP:</span> ${_xmlEsc(rec.bssid)}<br>`:'';
  const linkedClient=_recVal(rec,'linked_client');
  const rel=role==='client'&&apId?`<span style="color:var(--mu)">Linked AP:</span> ${_xmlEsc(apId)}<br>`:
            linkedClient?`<span style="color:var(--mu)">Linked client:</span> ${_xmlEsc(linkedClient)}<br>`:'';
  const samples=_samplesForSource(rec).length;
  const detail=[
    `<span style="color:var(--mu)">Role:</span> <b style="color:${_roleColor(role)}">${roleTxt}</b><br>`,
    sec?`<span style="color:var(--mu)">Auth:</span> <b>${_xmlEsc(sec)}</b><br>`:'',
    rec.protocol?`<span style="color:var(--mu)">Protocol:</span> ${_xmlEsc(rec.protocol)}<br>`:'',
    rec.frame_subtype?`<span style="color:var(--mu)">Frame:</span> ${_xmlEsc(rec.frame_subtype)}<br>`:'',
    Array.isArray(rec.akm_suites)&&rec.akm_suites.length?`<span style="color:var(--mu)">AKM:</span> ${_xmlEsc(rec.akm_suites.join(', '))}<br>`:'',
    Array.isArray(rec.pairwise_ciphers)&&rec.pairwise_ciphers.length?`<span style="color:var(--mu)">Cipher:</span> ${_xmlEsc(rec.pairwise_ciphers.join(', '))}<br>`:'',
  ].join('');
  const pop=`<b>${_xmlEsc(lbl)}</b><br><small style="color:var(--mu)">${_xmlEsc(id)}</small><br>${bssid}${rel}${detail}
    <span style="color:var(--mu)">Method:</span> <b style="color:var(--acc)">${_xmlEsc(rec.pos_method||'?')}</b><br>
    ${(+rec.lat).toFixed(6)}, ${(+rec.lon).toFixed(6)}<br>
    <span style="color:var(--mu)">Samples:</span> ${rec.samples||'?'} &nbsp;
    <span style="color:var(--mu)">Linked dots:</span> ${samples} &nbsp;
    <span style="color:var(--mu)">Freq:</span> ${rec.freq_mhz||'?'} MHz${ch}<br>${res}
    <a href="#" onclick="_showSourceSamples('${esc(id)}');return false" style="color:var(--acc);font-size:.78rem">Show sample links</a>
    &nbsp; <a href="#" onclick="hideSource('${esc(id)}');return false" style="color:var(--acc);font-size:.78rem">Hide</a>`;
  if(mkrs[id]){mkrs[id].setLatLng([rec.lat,rec.lon]).setIcon(mkIcon(rec,_selectedSourceId===id));mkrs[id].getPopup().setContent(pop);}
  else{
    mkrs[id]=L.marker([rec.lat,rec.lon],{icon:mkIcon(rec,_selectedSourceId===id)}).addTo(map).bindPopup(pop);
    mkrs[id].on('click',e=>{if(e&&e.originalEvent)L.DomEvent.stopPropagation(e);_showSourceSamples(id);});
    map.panTo([rec.lat,rec.lon]);
  }
  if(!_markerVisible(rec)) mkrs[id].remove();
  _refreshRelationshipLines();
  refreshMapStats();
}
function hideSource(id){
  if(mkrs[id]){mkrs[id].remove();delete mkrs[id];}
  if(_selectedSourceId===id)_clearSampleLines();
  _refreshRelationshipLines();
  if(map) map.closePopup();
  refreshMapStats();
}
function mapFilter(btn){
  _mapFilter=btn.dataset.m;
  document.querySelectorAll('.mfpill[data-m]').forEach(b=>b.classList.toggle('active',b===btn));
  applyMapFilters();
}
function mapFilterLegend(el){
  el.classList.toggle('hidden');
  const m=el.dataset.m, hide=el.classList.contains('hidden');
  Object.entries(mkrs).forEach(([id,mk])=>{
    if((srcs[id]?.pos_method||'')=== m){ if(hide) mk.remove(); else if(_markerVisible(srcs[id])) mk.addTo(map); }
  });
  _refreshRelationshipLines();
  refreshMapStats();
}
let _pathsVisible=true;

function _setPathVisible(p, on){
  if(on){p.layer.addTo(map);p.dots.forEach(d=>d.addTo(map));}
  else{p.layer.remove();p.dots.forEach(d=>d.remove());}
  p.visible=on;
  refreshMapStats();
}
function _renderPathsList(){
  const el=document.getElementById('map-paths-list'); if(!el) return;
  if(!_loadedPaths.length){
    el.innerHTML='<span style="color:var(--mu);font-size:.7rem">Use Sessions → Map Path to load a decimated, Pi-safe route/observation overlay.</span>';
    return;
  }
  el.innerHTML=_loadedPaths.map((p,i)=>{
    const on=p.visible!==false;
    const dot=on
      ?`<span style="display:inline-block;width:10px;height:10px;border-radius:50%;background:${p.color};flex-shrink:0"></span>`
      :`<span style="display:inline-block;width:10px;height:10px;border-radius:50%;border:2px solid ${p.color};background:transparent;flex-shrink:0"></span>`;
    const nameStyle=`flex:1;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:.7rem;${on?'color:var(--txt)':'color:var(--mu);text-decoration:line-through'}`;
    const sub=p.overview?`${p.points||0} preview pts`:(p.gps?`${p.gps} GPS / ${p.obs||0} frame pts`:`${p.points||0} frame pts`); // displayed points are decimated for fat sessions
    return `<div style="display:flex;align-items:center;gap:.3rem;margin-bottom:.25rem;cursor:pointer" onclick="toggleOnePath(${i})" title="Click to show/hide">
      ${dot}
      <span style="${nameStyle}" title="${p.name}">${p.name}</span>
      <span style="font-size:.62rem;color:var(--mu);white-space:nowrap">${sub}</span>
      <button style="background:transparent;border:none;color:var(--mu);cursor:pointer;padding:0 .2rem;font-size:.82rem" onclick="event.stopPropagation();removeLoadedPath(${i})">✕</button>
    </div>`;
  }).join('');
}
function toggleOnePath(i){
  const p=_loadedPaths[i]; if(!p) return;
  _setPathVisible(p, p.visible===false);
  // sync global toggle state: all-on → ●, any-off → ○
  _pathsVisible=_loadedPaths.every(p=>p.visible!==false);
  _updatePathsToggleBtn();
  _renderPathsList();
}
function _updatePathsToggleBtn(){
  const btn=document.getElementById('paths-toggle-btn'); if(!btn) return;
  btn.textContent=_pathsVisible?'Paths ●':'Paths ○';
  btn.style.opacity=_pathsVisible?'1':'.5';
}
function toggleAllPaths(){
  _pathsVisible=!_pathsVisible;
  _loadedPaths.forEach(p=>_setPathVisible(p,_pathsVisible));
  _updatePathsToggleBtn();
  _renderPathsList();
}
function addPathFromSession(path, name, opts){
  opts=opts||{};
  const overview=!!opts.overview;
  const existingIdx=_loadedPaths.findIndex(p=>p.path===path);
  if(existingIdx>=0){
    // Manual Map Path upgrades a cheap bulk overview to the full decimated RF-dot view.
    if(!overview&&_loadedPaths[existingIdx].overview){removeLoadedPath(existingIdx);}
    else return Promise.resolve({skipped:true,path});
  }
  initMap();
  const url=overview
    ?('/api/session/records?map=1&overview=1&max_points='+(opts.maxPoints||900)+'&path='+encodeURIComponent(path))
    :('/api/session/records?map=1&max_gps=2500&max_obs=6000&path='+encodeURIComponent(path));
  return fetch(url)
    .then(r=>r.json()).then(recs=>{
      if(!overview) _indexSamplesFromRecords(path,recs);
      if(_selectedSourceId){
        const srec=srcs[_selectedSourceId], sk=srec?_sourcePrimaryId(srec):'';
        if(sk&&_sourceSampleFetches[sk]==='done') delete _sourceSampleFetches[sk];
        _showSourceSamples(_selectedSourceId);
      }
      // Keep file order. Frame timestamps and GPS timestamps may come from
      // different clocks, so sorting by t can fold the trail back on itself.
      const georecs=recs.filter(r=>r.lat!=null&&r.lon!=null);
      if(!georecs.length) return {empty:true,path};
      const gpsrecs=georecs.filter(r=>r.record_type==='gps'||r.source==='gps'||r.record_type==='route');
      const obsrecs=overview?[]:georecs.filter(r=>!(r.record_type==='gps'||r.source==='gps'||r.record_type==='route'));
      const routrecs=gpsrecs.length?gpsrecs:obsrecs;
      const color=_PATH_COLORS[_loadedPaths.length%_PATH_COLORS.length];
      const on=_pathsVisible;
      const layer=L.polyline(routrecs.map(r=>[r.lat,r.lon]),
        {color,opacity:overview?.42:.55,weight:overview?2.4:2,dashArray:gpsrecs.length?'':'6 4'});
      if(on) layer.addTo(map);
      // GPS breadcrumbs draw the route. Observation dots are drawn only for
      // manual Map Path. Bulk solve gets cheap route previews so a Pi does not
      // create tens of thousands of Leaflet markers.
      const dotrecs=overview?gpsrecs:gpsrecs.concat(obsrecs);
      const dots=dotrecs.map(r=>{
        const isGps=(r.record_type==='gps'||r.source==='gps'||r.record_type==='route');
        const stale=r.gps_held||r.gps_hold_s!=null;
        const dot=L.circleMarker([r.lat,r.lon],{radius:overview?2.4:(isGps?3:4),color,fillColor:color,fillOpacity:overview?.48:(isGps ? .55 : .78),weight:overview?1:(isGps?1.1:1.5),dashArray:stale?'2 3':null});
        const ts=r.t?new Date(r.t*1000).toISOString().slice(11,19):'?';
        const freq=r.freq?(r.freq/1e6).toFixed(0)+' MHz':'?';
        const coords=`<span style="font-family:monospace;font-size:.7rem;color:var(--mu)">${(+r.lat).toFixed(6)}, ${(+r.lon).toFixed(6)}</span>`;
        const held=stale?`<br><span style="color:var(--ylw)">GPS held:</span> ${r.gps_hold_s??r.gps_age_s??'?'}s`:'';
        const pop=overview
          ?`<b>Bulk route preview</b><br>${coords}<br><span style="color:var(--mu)">${ts}</span>`
          :isGps
            ?`<b>GPS breadcrumb</b><br>${coords}<br><span style="color:var(--mu)">${ts}</span>`
            :`${_displaySSID(r.ssid)?`<b>${_xmlEsc(_displaySSID(r.ssid))}</b><br>`:''}${r.id?`<small style="color:var(--mu)">${r.id}</small><br>`:''}`
              +`<span style="color:var(--ylw)">RSSI:</span> ${r.rssi??'?'} dBm &nbsp;<span style="color:var(--mu)">Freq:</span> ${freq}`
              +`${r.protocol?` <span style="color:var(--mu)">(${r.protocol})</span>`:''}`
              +`${held}<br>${coords}<br><span style="color:var(--mu)">${ts}</span>`;
        dot.bindPopup(pop,{maxWidth:230,className:'aw-path-tip'});
        if(on) dot.addTo(map);
        return dot;
      });
      const firstPath=_loadedPaths.length===0;
      _loadedPaths.push({name,path,color,layer,dots,visible:on,gps:gpsrecs.length,obs:obsrecs.length,points:dotrecs.length,overview});
      if(firstPath&&layer.getBounds&&layer.getBounds().isValid&&layer.getBounds().isValid()){
        try{map.fitBounds(layer.getBounds().pad(0.08));}catch(e){}
      }
      _renderPathsList();
      refreshMapStats();
    });
}
function _dropSamplePath(path){
  for(const bucket of [_sourceSamples,_sourceSamplesByAp]){
    Object.keys(bucket).forEach(k=>{bucket[k]=(bucket[k]||[]).filter(s=>s.path!==path&&!String(s.path||'').startsWith(path+'#')); if(!bucket[k].length) delete bucket[k];});
  }
  Object.keys(_networkRelations).forEach(k=>{_networkRelations[k]=(_networkRelations[k]||[]).filter(x=>x.path!==path&&!String(x.path||'').startsWith(path+'#')); if(!_networkRelations[k].length) delete _networkRelations[k];});
}
function removeLoadedPath(i){
  const p=_loadedPaths[i]; if(!p) return;
  p.layer.remove(); p.dots.forEach(d=>d.remove());
  _dropSamplePath(p.path);
  _loadedPaths.splice(i,1);
  if(_selectedSourceId) _showSourceSamples(_selectedSourceId);
  _renderPathsList();
  refreshMapStats();
}
function clearAllPaths(){
  _loadedPaths.forEach(p=>{p.layer.remove();p.dots.forEach(d=>d.remove());_dropSamplePath(p.path);});
  _loadedPaths=[];
  _clearSampleLines();
  _renderPathsList();
  refreshMapStats();
}
function refreshMapStats(){
  const srcEl=document.getElementById('map-count');
  const relEl=document.getElementById('map-rel-count');
  const pathEl=document.getElementById('map-path-count');
  if(!map){return;}
  let vis=0, ap=0, cli=0, unk=0;
  Object.entries(mkrs).forEach(([id,m])=>{
    const rec=srcs[id];
    if(rec&&map.hasLayer(m)&&_pointInCurrentView(rec.lat,rec.lon)){
      vis++; const r=_srcRole(rec); if(r==='ap')ap++; else if(r==='client')cli++; else unk++;
    }
  });
  if(srcEl) srcEl.textContent=`${vis} source${vis!==1?'s':''} in view (${ap} AP / ${cli} client / ${unk} other)`;
  if(relEl){
    const n=Object.values(_relLines).filter(l=>map.hasLayer(l)).length;
    relEl.textContent=n+' AP↔client link'+(n!==1?'s':'')+' in view';
  }
  if(pathEl){
    let pts=0, routes=0;
    _loadedPaths.forEach(p=>{
      if(p.visible===false) return;
      if(p.layer&&map.hasLayer(p.layer)) routes++;
      pts += (p.dots||[]).filter(d=>map.hasLayer(d)&&_pointInCurrentView(d.getLatLng().lat,d.getLatLng().lng)).length;
    });
    pathEl.textContent=routes+' path'+(routes!==1?'s':'')+', '+pts+' path point'+(pts!==1?'s':'')+' in view';
  }
  _updateSelectedSampleStat();
}
function refreshMapCount(){refreshMapStats();}


// ── Map export ────────────────────────────────────────────────────────────────
function _visibleSrcs(){
  return Object.values(srcs).filter(r=>r.lat&&r.lon&&mkrs[r.id]&&map&&map.hasLayer(mkrs[r.id])&&_markerVisible(r));
}
function _dlBlob(data,name,type){
  const a=document.createElement('a');
  a.href=URL.createObjectURL(new Blob([data],{type}));
  a.download=name; document.body.appendChild(a); a.click();
  setTimeout(()=>{URL.revokeObjectURL(a.href);a.remove();},500);
}
function _csvEsc(s){s=String(s||'');return(s.includes(',')||s.includes('"'))?'"'+s.replace(/"/g,'""')+'"':s;}
function _freqToChannel(mhz){
  if(!mhz) return 0;
  if(mhz>=2412&&mhz<=2484) return Math.round((mhz-2412)/5)+1;
  if(mhz>=5180) return Math.round((mhz-5000)/5);
  return 0;
}
function exportMapJSONL(){
  const rows=_visibleSrcs();
  if(!rows.length){alert('No visible sources to export.');return;}
  _dlBlob(rows.map(r=>JSON.stringify(r)).join('\n'),'aetherward-positions.jsonl','application/x-ndjson');
  {const dd=document.getElementById('map-export-dd'); if(dd) dd.style.display='none';}
}
function exportMapWigle(){
  const rows=_visibleSrcs();
  if(!rows.length){alert('No visible sources to export.');return;}
  const hdr='WigleWifi-1.4,appRelease=AetherWard,model=,release=,device=,display=,board=,brand=,star=,body=\n'
    +'MAC,SSID,AuthMode,FirstSeen,Channel,Frequency,RSSI,CurrentLatitude,CurrentLongitude,AltitudeMeters,AccuracyMeters,Type\n';
  const csv=rows.map(r=>{
    const ts=r.t?new Date(r.t*1000).toISOString().replace('T',' ').slice(0,19):'';
    const ch=_freqToChannel(r.freq_mhz||0);
    const freq=Math.round((r.freq_mhz||0)*1000); // kHz
    const rssi=r.rssi!=null?r.rssi:-65;
    const acc=r.residual_m!=null?(+r.residual_m).toFixed(1):0;
    return [r.id||'',_csvEsc(_cleanSSID(r.ssid)||''),_csvEsc(r.auth_mode||r.security||''),ts,ch,freq,rssi,
            (+r.lat).toFixed(7),(+r.lon).toFixed(7),0,acc,'WIFI'].join(',');
  }).join('\n');
  _dlBlob(hdr+csv,'aetherward-wigle.csv','text/csv');
  {const dd=document.getElementById('map-export-dd'); if(dd) dd.style.display='none';}
}
function _xmlEsc(s){return String(s||'').replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;');}
function exportMapGeoJSON(){
  const rows=_visibleSrcs();
  if(!rows.length){alert('No visible sources to export.');return;}
  const features=rows.map(r=>({
    type:'Feature',
    geometry:{type:'Point',coordinates:[(+r.lon),(+r.lat),0]},
    properties:{id:r.id||'',ssid:_cleanSSID(r.ssid)||'',auth_mode:r.auth_mode||'',security:r.security||'',
                bssid:r.bssid||'',channel:r.channel??null,pos_method:r.pos_method||'',
                rssi:r.rssi??null,freq_mhz:r.freq_mhz??null,samples:r.samples??null,
                residual_m:r.residual_m??null},
  }));
  _dlBlob(JSON.stringify({type:'FeatureCollection',features},null,2),
    'aetherward-positions.geojson','application/json');
}
function exportMapCSV(){
  const rows=_visibleSrcs();
  if(!rows.length){alert('No visible sources to export.');return;}
  const fields=['id','ssid','auth_mode','security','bssid','channel','lat','lon','pos_method','rssi','freq_mhz','samples','residual_m','rssi_at_1m'];
  const hdr=fields.join(',');
  const csv=rows.map(r=>fields.map(f=>_csvEsc(r[f]??'')).join(',')).join('\n');
  _dlBlob(hdr+'\n'+csv,'aetherward-positions.csv','text/csv');
}
function exportMapKML(){
  const rows=_visibleSrcs();
  if(!rows.length){alert('No visible sources to export.');return;}
  const placemarks=rows.map(r=>
    `<Placemark><name>${_xmlEsc(r.id||'')}</name>`+
    `<description>SSID: ${_xmlEsc(_cleanSSID(r.ssid)||'<empty SSID>')}  Auth: ${_xmlEsc(r.auth_mode||r.security||'')}  RSSI: ${r.rssi??'?'} dBm  `+
    `Samples: ${r.samples??'?'}  Method: ${r.pos_method||'?'}</description>`+
    `<Point><coordinates>${(+r.lon).toFixed(7)},${(+r.lat).toFixed(7)},0</coordinates></Point></Placemark>`
  ).join('\n');
  _dlBlob(`<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2"><Document>\n${placemarks}\n</Document></kml>`,
    'aetherward-positions.kml','application/vnd.google-earth.kml+xml');
}

// ── SSE ───────────────────────────────────────────────────────────────────────
function connectSSE(){
  const es=new EventSource('/api/events');
  es.onmessage=e=>{
    const rec=JSON.parse(e.data);
    if(rec.type==='position'){
      srcs[rec.id]=rec; totalUpd++; updateMarker(rec); updateHeader(); appendLog(rec);
      if(document.getElementById('panel-positions').classList.contains('active')) renderPositions();
    } else if(rec.type==='source_removed'){
      delete srcs[rec.id];
      if(mkrs[rec.id]){mkrs[rec.id].remove();delete mkrs[rec.id];}
      if(_selectedSourceId===rec.id)_clearSampleLines();
      _refreshRelationshipLines();
      updateHeader(); refreshMapStats();
      if(document.getElementById('panel-positions').classList.contains('active')) renderPositions();
    } else if(rec.type==='session_path_ready'){
      addPathFromSession(rec.path, rec.name||rec.path.split('/').pop(), {overview:!!rec.overview, maxPoints:900});
      if(rec.overview) sysLog('Loaded route preview: '+(rec.name||rec.path.split('/').pop()));
    } else if(rec.type==='log'){
      if(rec.source==='solve') appendSolveLog(rec.text);
      else appendRunLog(rec.text);
    }
  };
  es.onerror=()=>setTimeout(connectSSE,3000);
}

// ── Header / dashboard ────────────────────────────────────────────────────────
function updateHeader(){
  const all=Object.values(srcs);
  document.getElementById('hdr-src').textContent=all.length+' sources';
  document.getElementById('hdr-upd').textContent=totalUpd+' updates';
  document.getElementById('s-total').textContent=all.length;
  document.getElementById('s-rss').textContent=all.filter(r=>r.pos_method==='rss_trilateration').length;
  document.getElementById('s-cen').textContent=all.filter(r=>r.pos_method==='rssi_centroid').length;
  document.getElementById('s-upd').textContent=totalUpd;
}
function loadStatus(){
  fetch('/api/status').then(r=>r.json()).then(d=>{
    const tb=document.getElementById('sys-tb');
    if(tb) tb.innerHTML=Object.entries(d).map(([k,v])=>
      `<tr><td style="color:var(--mu);width:160px">${k}</td><td>${v}</td></tr>`).join('');
    document.getElementById('dot').className='dot'+(d.solve_running||d.run_running?'':' off');
    document.getElementById('hdr-label').textContent=d.run_running?'capturing':d.solve_running?'solving':'idle';
    document.getElementById('btn-start').disabled=d.solve_running;
    document.getElementById('btn-stop').disabled=!d.solve_running;
    document.getElementById('run-btn-start').disabled=d.run_running;
    document.getElementById('run-btn-stop').disabled=!d.run_running;
  });
}
function loadBanner(){
  fetch('/api/banner').then(r=>r.text()).then(h=>{
    const el=document.getElementById('banner-hero');
    if(el){el.innerHTML=h;}
  }).catch(()=>{const el=document.getElementById('banner-hero');if(el)el.style.display='none';});
}
function renderBannerCanvas(){
  const canvas=document.getElementById('logo-canvas');
  if(!canvas) return;
  fetch('/api/banner').then(r=>r.text()).then(html=>{
    const tmp=document.createElement('div');
    tmp.innerHTML=html;
    // Walk DOM tree collecting (char, color) pairs
    const chars=[];
    function walk(node,color){
      if(node.nodeType===3){
        for(const ch of node.textContent) chars.push({ch,color});
      } else if(node.nodeType===1){
        const c=(node.style&&node.style.color)||color;
        node.childNodes.forEach(n=>walk(n,c));
      }
    }
    tmp.childNodes.forEach(n=>walk(n,'transparent'));
    // Split into lines
    const lines=[[]];
    chars.forEach(({ch,color})=>{
      if(ch==='\n'){lines.push([]);}
      else{lines[lines.length-1].push({ch,color});}
    });
    while(lines.length>1&&lines[lines.length-1].length===0) lines.pop();
    if(!lines.length) return;
    const FS=8,CW=FS*0.602,CH=FS;
    const W=Math.max(...lines.map(l=>l.length))*CW;
    const H=lines.length*CH;
    canvas.width=Math.ceil(W); canvas.height=Math.ceil(H);
    const ctx=canvas.getContext('2d');
    ctx.fillStyle='#0e0303';
    ctx.fillRect(0,0,canvas.width,canvas.height);
    ctx.font=FS+"px 'Courier New',monospace";
    ctx.textBaseline='top';
    lines.forEach((line,row)=>{
      line.forEach(({ch,color},col)=>{
        if(color&&color!=='transparent'){
          ctx.fillStyle=color;
          ctx.fillText(ch,col*CW,row*CH);
        }
      });
    });
    canvas.style.display='block';
    const fb=document.getElementById('logo-svg-fb');
    if(fb) fb.style.display='none';
    // Generate favicon from banner canvas (scaled to 64x64 square, center-cropped)
    try {
      const fav=document.createElement('canvas');
      fav.width=64; fav.height=64;
      const fc=fav.getContext('2d');
      fc.fillStyle='#0d0c12'; fc.fillRect(0,0,64,64);
      const bw=canvas.width, bh=canvas.height;
      const sz=Math.min(bw,bh);
      const sx=Math.round((bw-sz)/2), sy=Math.round((bh-sz)/2);
      fc.drawImage(canvas,sx,sy,sz,sz,0,0,64,64);
      const link=document.querySelector('link[rel="icon"]');
      if(link){link.href=fav.toDataURL('image/png');link.type='image/png';}
    } catch(e){}
  }).catch(()=>{
    const fb=document.getElementById('logo-svg-fb');
    if(fb) fb.style.display='';
  });
}

// ── Positions table ───────────────────────────────────────────────────────────
function badgeCls(m){return m==='rss_trilateration'?'b-rss':m==='tdoa'?'b-tdoa':m==='rssi_centroid'?'b-cen':'b-man';}
function renderPositions(){
  const tb=document.getElementById('src-tb'); if(!tb)return;
  const rows=Object.values(srcs).sort((a,b)=>(b.samples||0)-(a.samples||0));
  tb.innerHTML=rows.length===0
    ?'<tr><td colspan="8" style="color:var(--mu);text-align:center;padding:1.25rem">No sources yet — start the solver</td></tr>'
    :rows.map(r=>{
      const ss=_displaySSID(r.ssid);
      const lbl=ss?`<b>${_xmlEsc(ss)}</b> <span style="color:var(--mu)">${r.id}</span>`:r.id;
      const res=r.residual_dBm!=null?r.residual_dBm+' dB':r.residual_m!=null?(+r.residual_m).toFixed(1)+' m':'—';
      return `<tr>
        <td>${lbl}</td>
        <td><span class="badge ${badgeCls(r.pos_method)}">${r.pos_method||'?'}</span></td>
        <td>${r.lat!=null?(+r.lat).toFixed(6):'—'}</td>
        <td>${r.lon!=null?(+r.lon).toFixed(6):'—'}</td>
        <td>${r.samples||'—'}</td><td>${res}</td><td>${r.freq_mhz||'—'}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-edit" onclick="openSrcModal(${JSON.stringify(r).replace(/"/g,'&quot;')})">Edit</button>
          <button class="btn btn-del"  onclick="delSrc('${r.id.replace(/'/g,"\\'")}')">Del</button>
        </td></tr>`;
    }).join('');
}

// ── Source modal ──────────────────────────────────────────────────────────────
let _srcEdit = null;
function openSrcModal(rec){
  _srcEdit = rec;
  document.getElementById('src-modal-title').textContent = rec ? 'Edit source' : 'Add source';
  document.getElementById('src-id').value    = rec?.id||'';
  document.getElementById('src-id').readOnly = !!rec;
  document.getElementById('src-ssid').value  = _cleanSSID(rec?.ssid)||'';
  document.getElementById('src-lat').value   = rec?.lat??'';
  document.getElementById('src-lon').value   = rec?.lon??'';
  document.getElementById('src-freq').value  = rec?.freq_mhz||'';
  document.getElementById('src-method').value= rec?.pos_method||'manual';
  document.getElementById('src-err').style.display='none';
  document.getElementById('src-modal').classList.add('open');
}
function closeSrcModal(){document.getElementById('src-modal').classList.remove('open');}
function saveSrc(){
  const id=document.getElementById('src-id').value.trim();
  const lat=parseFloat(document.getElementById('src-lat').value);
  const lon=parseFloat(document.getElementById('src-lon').value);
  if(!id){document.getElementById('src-err').textContent='ID is required.';document.getElementById('src-err').style.display='block';return;}
  if(isNaN(lat)||isNaN(lon)){document.getElementById('src-err').textContent='Latitude and longitude are required.';document.getElementById('src-err').style.display='block';return;}
  const rec={id,lat,lon,ssid:document.getElementById('src-ssid').value||undefined,
    freq_mhz:parseFloat(document.getElementById('src-freq').value)||undefined,
    pos_method:document.getElementById('src-method').value};
  const ep=_srcEdit?'/api/source/edit':'/api/source/add';
  fetch(ep,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(rec)})
    .then(r=>r.json()).then(d=>{
      if(d.error){document.getElementById('src-err').textContent=d.error;document.getElementById('src-err').style.display='block';return;}
      closeSrcModal(); renderSources();
    });
}
function delSrc(id){
  if(!confirm(`Delete source "${id}"?`))return;
  fetch('/api/source/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({id})})
    .then(()=>renderSources());
}

// ── Solve ─────────────────────────────────────────────────────────────────────
function startSolve(){
  const session=document.getElementById('sl-session').value;
  if(!session){alert('Select a session file first.');return;}
  fetch('/api/solve/start',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({session,config:document.getElementById('sl-config').value||null,
      n_exp:parseFloat(document.getElementById('sl-nexp').value),
      min_obs:parseInt(document.getElementById('sl-minobs').value),
      follow:!!document.getElementById('sl-follow')?.checked})})
    .then(()=>{
      loadStatus();
      sysLog('Solver started: '+session.split('/').pop());
      addPathFromSession(session, session.split('/').pop());
    });
}
function stopSolve(){fetch('/api/solve/stop',{method:'POST'}).then(()=>{loadStatus();sysLog('Solver stopped.');});}

function appendLog(rec){
  const el=document.getElementById('sl-log'); if(!el)return;
  const ts=new Date().toISOString().slice(11,19);
  el.innerHTML+=`<div class="log-upd">${ts} [${rec.pos_method}] ${_displaySSID(rec.ssid)||rec.id||'?'} ${(+rec.lat).toFixed(5)}, ${(+rec.lon).toFixed(5)} s=${rec.samples??'?'}</div>`;
  el.scrollTop=el.scrollHeight;
}
function sysLog(msg){
  const el=document.getElementById('sl-log'); if(!el)return;
  el.innerHTML+=`<div class="log-sys">${new Date().toISOString().slice(11,19)} ${msg}</div>`;
  el.scrollTop=el.scrollHeight;
}
function appendSolveLog(text){
  const el=document.getElementById('sl-log'); if(!el)return;
  el.innerHTML+=`<div class="log-run">${new Date().toISOString().slice(11,19)} ${text}</div>`;
  el.scrollTop=el.scrollHeight;
}

// ── Run ───────────────────────────────────────────────────────────────────────
function startRun(){
  const cfg=document.getElementById('run-config').value;
  if(!cfg){alert('Select a config first.');return;}
  const log=!!document.getElementById('run-log-file')?.checked;
  fetch('/api/run/start',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({config:cfg,log})})
    .then(()=>loadStatus());
}
function stopRun(){fetch('/api/run/stop',{method:'POST'}).then(()=>loadStatus());}
function appendRunLog(text){
  const el=document.getElementById('run-log'); if(!el)return;
  el.innerHTML+=`<div class="log-run">${text}</div>`;
  el.scrollTop=el.scrollHeight;
}

// ── Sessions ──────────────────────────────────────────────────────────────────
let _bulkPathQueue=[], _bulkPathBusy=false;
function _sessionDisplayName(s){return (s.folder?`${s.folder}/`:'')+(s.name||String(s.path||'').split('/').pop());}
function _queueBulkPathPreviews(sessions){
  const allowed=new Set(['wardriver','tdoa_raw','unknown']);
  const existing=new Set(_loadedPaths.map(p=>p.path));
  let added=0;
  (sessions||[]).forEach(s=>{
    if(!s||!s.path||existing.has(s.path)) return;
    if(s.stype&&!allowed.has(s.stype)) return;
    existing.add(s.path);
    _bulkPathQueue.push({path:s.path,name:_sessionDisplayName(s)});
    added++;
  });
  if(added) sysLog(`Queued ${added} bulk route preview${added!==1?'s':''}.`);
  _drainBulkPathQueue();
}
function _drainBulkPathQueue(){
  if(_bulkPathBusy) return;
  const item=_bulkPathQueue.shift();
  if(!item) return;
  _bulkPathBusy=true;
  addPathFromSession(item.path,item.name,{overview:true,maxPoints:900})
    .finally(()=>{_bulkPathBusy=false;setTimeout(_drainBulkPathQueue,20);});
}
function solveAllSessions(){
  const btn=document.getElementById('sess-solve-all-btn');
  btn.disabled=true; btn.textContent='Solving…';
  fetch('/api/solve/batch',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({max_cells:256,
      n_exp:parseFloat(document.getElementById('sl-nexp')?.value||'2.5'),
      min_obs:parseInt(document.getElementById('sl-minobs')?.value||'3')})})
    .then(r=>r.json()).then(d=>{
      btn.disabled=false; btn.textContent='Solve All';
      if(d.error){alert('Error: '+d.error);return;}
      const note=document.createElement('span');
      note.style='font-size:.74rem;color:var(--grn);margin-left:.5rem';
      note.textContent='✓ bulk solve started (route previews will auto-load)';
      btn.after(note); setTimeout(()=>note.remove(),5000);
      loadStatus();
      sysLog('Bulk solve started. Loading lightweight route previews for every session; use Sessions → Map Path to upgrade one file with RF dots.');
      if(Array.isArray(d.sessions)) _queueBulkPathPreviews(d.sessions);
      else fetch('/api/sessions').then(r=>r.json()).then(_queueBulkPathPreviews).catch(()=>{});
    }).catch(err=>{btn.disabled=false;btn.textContent='Solve All';alert('Failed: '+err);});
}
function fmtSz(b){return b<1024?b+' B':b<1048576?(b/1024).toFixed(1)+' KB':(b/1048576).toFixed(1)+' MB';}
let _sessCwd='';
let _sessAllData=[];

function _sessSubfolders(data, cwd){
  const seen=new Set(), result=[];
  for(const s of data){
    const f=s.folder||'';
    if(cwd===''){
      if(f){const seg=f.split('/')[0];if(!seen.has(seg)){seen.add(seg);result.push(seg);}}
    } else {
      if(f===cwd||!f.startsWith(cwd+'/')) continue;
      const rest=f.slice(cwd.length+1);
      const seg=rest.split('/')[0];
      if(seg&&!seen.has(seg)){seen.add(seg);result.push(seg);}
    }
  }
  return result.sort();
}

function _sessFilesHere(data, cwd){
  return data.filter(s=>(s.folder||'')===(cwd));
}

function sessNavigate(folder){_sessCwd=folder;_sessRender();}

function loadSessions(){
  fetch('/api/sessions').then(r=>r.json()).then(data=>{
    _sessAllData=data;
    _sessRender();
    // populate solve/path dropdowns with all sessions
    const solvable=data.filter(s=>s.stype==='wardriver'||s.stype==='tdoa_raw'||s.stype==='unknown');
    const enuOnly =data.filter(s=>s.stype==='enu'||s.stype==='sensing');
    const sel=document.getElementById('sl-session');
    if(sel){
      const cur=sel.value;
      sel.innerHTML='<option value="">— select —</option>'+solvable.map(s=>`<option value="${s.path}"${s.path===cur?' selected':''}>${(s.folder?s.folder+'/':'')+s.name}</option>`).join('');
    }
    const note=document.getElementById('sl-enu-note');
    if(note){
      if(enuOnly.length){
        note.style.display='';
        note.innerHTML=`${enuOnly.length} ENU/sensing session(s) — use <a href="#" onclick="tab(document.querySelector('[data-tab=sessions]'));return false" style="color:var(--acc)">Sessions</a> → View ENU`;
      } else {
        note.style.display='none';
      }
    }
  });
}

function _sessRender(){
  const data=_sessAllData;
  const tb=document.getElementById('sess-tb');
  if(!tb) return;
  const typeLabel={wardriver:'wardriver',enu:'ENU/TDOA',sensing:'sensing',tdoa_raw:'TDOA raw',unknown:'?'};
  const typeCls  ={wardriver:'b-rss',enu:'b-tdoa',sensing:'b-cen',tdoa_raw:'b-tdoa',unknown:'b-man'};
  // breadcrumb
  const bc=document.getElementById('sess-breadcrumb');
  if(bc){
    if(_sessCwd===''){bc.textContent='';}
    else{
      const parts=_sessCwd.split('/');
      let html='<span style="cursor:pointer;color:var(--acc)" onclick="sessNavigate(\'\')">sessions</span>';
      let acc='';
      parts.forEach((p,i)=>{
        acc+=(acc?'/':'')+p;
        const a=acc;
        html+=` / <span style="cursor:pointer;color:var(--acc)" onclick="sessNavigate('${a}')">${p}</span>`;
      });
      bc.innerHTML=html;
    }
  }
  function sessActions(s){
    const p=s.path.replace(/'/g,"\\'"), n=s.name.replace(/'/g,"\\'");
    const dl=`<button class="btn btn-s" style="padding:.2rem .5rem;font-size:.73rem" onclick="downloadSession('${p}','${n}')">Download</button>`;
    const ren=`<button class="btn btn-edit" onclick="renameSession('${p}','${n}')">Rename</button>`;
    const del=`<button class="btn btn-del"  onclick="deleteSession('${p}','${n}')">Del</button>`;
    const _bs='padding:.2rem .5rem;font-size:.73rem';
    if(s.stype==='wardriver'||s.stype==='tdoa_raw')
      return `<button class="btn btn-s" style="${_bs}" onclick="quickSolve('${p}')">Solve</button>`
            +`<button class="btn btn-s" style="${_bs}" onclick="quickMapPath('${p}','${n}')">Map Path</button>${dl}${ren}${del}`;
    if(s.stype==='enu')
      return `<button class="btn btn-s" style="${_bs}" onclick="quickViewEnu('${p}')">View ENU</button>`
            +`<button class="btn btn-s" style="${_bs}" onclick="quickView3d('${p}')">View 3D</button>${dl}${ren}${del}`;
    if(s.stype==='sensing')
      return `<button class="btn btn-s" style="${_bs}" onclick="quickViewEnu('${p}')">View ENU</button>${dl}${ren}${del}`;
    return `${dl}${ren}${del}`;
  }
  const subfolders=_sessSubfolders(data,_sessCwd);
  const files=_sessFilesHere(data,_sessCwd);
  const rows=[];
  if(_sessCwd!==''){
    const parent=_sessCwd.includes('/')?_sessCwd.slice(0,_sessCwd.lastIndexOf('/')):'';
    rows.push(`<tr style="cursor:pointer" onclick="sessNavigate('${parent}')">
      <td colspan="6" style="color:var(--acc);font-family:monospace;padding:.35rem .6rem">↑ ..</td></tr>`);
  }
  subfolders.forEach(sf=>{
    const full=_sessCwd?_sessCwd+'/'+sf:sf;
    const count=data.filter(s=>(s.folder||'')===(full)||(s.folder||'').startsWith(full+'/')).length;
    rows.push(`<tr style="cursor:pointer" onclick="sessNavigate('${full}')">
      <td colspan="6" style="color:var(--acc);font-family:monospace;padding:.35rem .6rem">
        📁 ${sf}/ <span style="color:var(--mu);font-size:.72rem">${count} file${count!==1?'s':''}</span>
      </td></tr>`);
  });
  files.forEach(s=>{
    rows.push(`<tr>
      <td style="font-family:monospace;font-size:.82rem">${s.name}</td>
      <td><span class="badge ${typeCls[s.stype]||'b-man'}">${typeLabel[s.stype]||'?'}</span></td>
      <td>${fmtSz(s.size)}</td><td>${s.records??'?'}</td>
      <td style="color:var(--mu)">${s.mtime}</td>
      <td style="white-space:nowrap">${sessActions(s)}</td></tr>`);
  });
  if(rows.length===0){
    rows.push('<tr><td colspan="6" style="color:var(--mu);text-align:center;padding:1.25rem">No session files found</td></tr>');
  }
  tb.innerHTML=rows.join('');
}

function quickMapPath(path,name){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='map'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-map').classList.add('active');
  initMap(); if(map) map.invalidateSize();
  addPathFromSession(path,name||path.split('/').pop());
}

function quickSolve(path){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='solve'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-solve').classList.add('active');
  loadSessions(); loadConfigs();
  setTimeout(()=>{document.getElementById('sl-session').value=path; const f=document.getElementById('sl-follow'); if(f) f.checked=false; startSolve();},300);
}
function quickViewEnu(path){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='enu'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-enu').classList.add('active');
  fetch('/api/session/records?raw=1&path='+encodeURIComponent(path))
    .then(r=>r.json()).then(pts=>{
      const enuPts=pts.filter(r=>r.x_enu!=null||r.y_enu!=null);
      if(enuPts.length){
        _enuRecs=enuPts.map((r,i)=>({
          id:    r.id||('rec-'+i),
          x_enu: r.x_enu??0, y_enu: r.y_enu??0, z_enu: r.z_enu??0,
          rssi:  r.rssi??-80, method: r.method??r.type??'', t: r.t??0,
        }));
        const co=document.getElementById('enu-coords-xy'); if(co) co.textContent='';
        enuRender();
      } else {
        // Sensing session — project direction vectors onto unit circle
        const _SENSING_TYPES=new Set(['presence','motion','absence']);
        const senRecs=pts.filter(r=>r.type&&_SENSING_TYPES.has(r.type));
        if(senRecs.length){
          _enuRecs=senRecs.map((r,i)=>{
            // direction is a [dx,dy,dz] unit vector from the antenna toward the source
            const d=Array.isArray(r.direction)?r.direction:[0,0,0];
            const mag=Math.sqrt(d[0]*d[0]+d[1]*d[1])||1;
            return {id:r.antenna_id||('ant-'+i), x_enu:d[0]/mag, y_enu:d[1]/mag, z_enu:0,
                    rssi:-(r.variance??5)*5, method:r.type, t:r.t??0};
          });
          enuRender();
          const co=document.getElementById('enu-coords-xy');
          if(co) co.textContent='Sensing: direction vectors projected onto unit circle (X=East, Y=North)';
        } else {
          sysLog('No spatial records found in this session.');
        }
      }
    });
}
function quickView3d(path){
  document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='tdoa3d'));
  document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
  document.getElementById('panel-tdoa3d').classList.add('active');
  loadConfigs();
  fetch('/api/session/records?raw=1&path='+encodeURIComponent(path))
    .then(r=>r.json()).then(pts=>{
      _t3Sources=pts.filter(r=>r.x_enu!=null||r.y_enu!=null).map((r,i)=>({
        id:r.id||('rec-'+i), x:r.x_enu||0, y:r.y_enu||0, z:r.z_enu||0,
        rssi:r.rssi??null, method:r.method||r.type||'tdoa',
      }));
      tdoa3dRender();
      if(!_t3Sources.length) sysLog('No ENU records found for 3D view.');
    });
}
function deleteSession(path,name){
  if(!confirm(`Delete session "${name}"?\nThis cannot be undone.`))return;
  fetch('/api/session/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path})})
    .then(r=>r.json()).then(d=>{if(d.error)alert(d.error);else loadSessions();});
}
function downloadSession(path, name){
  const a=document.createElement('a');
  a.href='/api/session/download?path='+encodeURIComponent(path);
  a.download=name; document.body.appendChild(a); a.click(); a.remove();
}
function sessImportSelected(input){
  const files=Array.from(input.files); if(!files.length) return;
  let done=0;
  files.forEach(f=>{
    const r=new FileReader();
    r.onload=e=>{
      fetch('/api/session/import',{method:'POST',headers:{'Content-Type':'application/json'},
        body:JSON.stringify({name:f.name, content:e.target.result})})
      .then(r=>r.json()).then(d=>{
        if(d.error) alert('Import "'+f.name+'": '+d.error);
        if(++done===files.length) loadSessions();
      });
    };
    r.readAsText(f);
  });
  input.value='';
}
function renameSession(path,name){
  const n=prompt('New name (without .jsonl):',name.replace('.jsonl',''));
  if(!n)return;
  fetch('/api/session/rename',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({path,name:n})})
    .then(r=>r.json()).then(d=>{if(d.error)alert(d.error);else loadSessions();});
}

// ── Configs ───────────────────────────────────────────────────────────────────
function loadConfigs(){
  fetch('/api/configs').then(r=>r.json()).then(data=>{
    const el=document.getElementById('cfg-list');
    if(el) el.innerHTML=data.length===0
      ?`<div class="card" style="color:var(--mu)">No configs yet. Click <b style="color:var(--txt)">+ New config</b> or <b style="color:var(--txt)">▶ Wizard</b>.</div>`
      :data.map(c=>`<div class="card">
          <div style="display:flex;align-items:center;justify-content:space-between;flex-wrap:wrap;gap:.5rem">
            <div>
              <span style="font-weight:600;color:var(--txt)">${c.name}</span>
              <span style="font-size:.77rem;color:var(--mu);margin-left:.7rem">
                mode: <b style="color:var(--acc)">${c.mode}</b> &nbsp;•&nbsp; ${c.antennas} antenna(s)
              </span>
            </div>
            <div style="display:flex;gap:.4rem">
              <button class="btn btn-edit" onclick="openEditCfg('${esc(c.name)}')">Edit</button>
              <button class="btn btn-del"  onclick="deleteCfg('${esc(c.name)}')">Delete</button>
            </div>
          </div></div>`).join('');
    ['sl-config','run-config'].forEach(id=>{
      const sel=document.getElementById(id); if(!sel)return;
      const cur=sel.value;
      sel.innerHTML='<option value="">— none —</option>'+
        data.map(c=>`<option value="${c.name}"${c.name===cur?' selected':''}>${c.name}</option>`).join('');
    });
    const tdoa3dSel=document.getElementById('tdoa3d-config');
    if(tdoa3dSel){
      const cur=tdoa3dSel.value;
      tdoa3dSel.innerHTML='<option value="">— no config (receivers) —</option>'+
        data.map(c=>`<option value="${c.name}"${c.name===cur?' selected':''}>${c.name}</option>`).join('');
    }
  });
}
function esc(s){s=String(s??'');return s.replace(/'/g,"\\'").replace(/"/g,'&quot;');}

// ── Config editor ─────────────────────────────────────────────────────────────
const CFG_TPL=`# AetherWard configuration
array_id = "my-array"
mode = "wardriver"

[[antennas]]
id = "wlan0"
backend = "plugins.wifi_nl80211.NL80211Backend"
backend_config = {interface = "wlan0"}
frequency_range = [2400000000, 2500000000]
position = [0.0, 0.0, 0.0]
orientation_euler = [0.0, 0.0, 0.0]
gain_dbi = 0.0

[gps]
backend = "gpsd"
host = "localhost"
port = 2947

[sync]
source = "software"

[mode_config]
channels = [1, 6, 11]
hop_interval = 0.1
# output_path omitted: runtime writes a timestamped file under ~/.aetherward/sessions
store_raw_frames = true

[output]
format = "jsonl"
path_policy = "default"
`;

function openNewCfg(){
  document.getElementById('modal-title').textContent='New config';
  const n=document.getElementById('cfg-name');
  n.value=''; n.readOnly=false;
  document.getElementById('cfg-content').value=CFG_TPL;
  document.getElementById('cfg-err').style.display='none';
  document.getElementById('cfg-modal').classList.add('open');
  n.focus();
}
function openEditCfg(name){
  fetch('/api/config/raw?name='+encodeURIComponent(name)).then(r=>r.json()).then(d=>{
    if(d.error){alert('Cannot load: '+d.error);return;}
    document.getElementById('modal-title').textContent='Edit: '+name;
    const n=document.getElementById('cfg-name'); n.value=name; n.readOnly=true;
    document.getElementById('cfg-content').value=d.content;
    document.getElementById('cfg-err').style.display='none';
    document.getElementById('cfg-modal').classList.add('open');
    document.getElementById('cfg-content').focus();
  });
}
function closeCfgModal(){document.getElementById('cfg-modal').classList.remove('open');}
function saveCfg(){
  const name=document.getElementById('cfg-name').value.trim();
  const content=document.getElementById('cfg-content').value;
  const err=document.getElementById('cfg-err');
  err.style.display='none';
  if(!name){err.textContent='Enter a config name.';err.style.display='block';return;}
  if(!content.trim()){err.textContent='Content is empty.';err.style.display='block';return;}
  fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})})
    .then(r=>r.json()).then(d=>{
      if(d.error){err.textContent=d.error;err.style.display='block';return;}
      closeCfgModal(); loadConfigs(); sysLog('Config saved: '+name);
    });
}
function deleteCfg(name){
  if(!confirm(`Delete config "${name}"?`))return;
  fetch('/api/config/delete',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name})})
    .then(r=>r.json()).then(d=>{if(d.error){alert(d.error);return;} loadConfigs();});
}

// ── Wizard ────────────────────────────────────────────────────────────────────
function openWizard(){
  wStep=1;
  const wn=document.getElementById('wiz-name');
  if(wn && !wn.value.trim()) wn.value='my-config';
  wizRenderProgress(); wizShowStep();
  document.getElementById('wiz-modal').classList.add('open');
  if(wn) setTimeout(()=>wn.focus(),0);
  // if we already have cached detect data, use it immediately
  if(_detCache) _applyDetect(_detCache);
  // always refresh so the wizard shows current hardware state
  fetch('/api/detect').then(r=>r.json()).then(d=>{
    _detCache=d; _applyDetect(d);
  }).catch(()=>wizRenderAnts());
}
function closeWizard(){document.getElementById('wiz-modal').classList.remove('open');}

function wizRenderProgress(){
  const c=document.getElementById('wiz-prog'); if(!c)return;
  let h='';
  for(let i=1;i<=STEPS;i++){
    const cls=i<wStep?'done':i===wStep?'active':'';
    h+=`<div class="wiz-dot ${cls}">${i}</div>`;
    if(i<STEPS) h+=`<div class="wiz-line ${i<wStep?'done':''}"></div>`;
  }
  c.innerHTML=h;
}
function wizShowStep(){
  for(let i=1;i<=STEPS;i++){
    const el=document.getElementById('wiz-s'+i);
    if(el) el.classList.toggle('active',i===wStep);
  }
  document.getElementById('wiz-prev').disabled=(wStep===1);
  document.getElementById('wiz-step-lbl').textContent=`Step ${wStep} of ${STEPS}`;
  const isLast=wStep===STEPS;
  document.getElementById('wiz-next').style.display=isLast?'none':'';
  document.getElementById('wiz-save').style.display=isLast?'':'none';
  if(wStep===6) wizShowAdvanced();
  if(isLast) wizGenToml();
}
function wizNext(){
  if(wStep===1){
    const wn=document.getElementById('wiz-name');
    const err=document.getElementById('wiz-name-err');
    if(!wn || !wn.value.trim()){
      if(err) err.style.display='block';
      if(wn) wn.focus();
      return;
    }
  }
  if(wStep<STEPS){wStep++;wizRenderProgress();wizShowStep();}
}
function wizPrev(){
  if(wStep>1){wStep--;wizRenderProgress();wizShowStep();}
}
function wizSetMode(m){
  W.mode=m;
  ['wardriver','trilateration','array_sensing'].forEach(k=>{
    const el=document.getElementById('wc-'+k);
    if(el) el.classList.toggle('sel',k===m);
  });
  wizShowAdvanced();
}

function wizNameChanged(v){
  const s=String(v||'').trim();
  W.configName=s;
  const err=document.getElementById('wiz-name-err');
  if(err && s) err.style.display='none';
  if(wStep===STEPS) wizGenToml();
}

function wizOutputPolicyChange(){
  const sel=document.getElementById('wiz-output-policy');
  const box=document.getElementById('wiz-output-custom');
  const hint=document.getElementById('wiz-output-hint');
  const policy=sel?.value||'default';
  W.outputPolicy=policy;
  if(box) box.style.display=policy==='custom'?'':'none';
  if(hint){
    if(policy==='default') hint.textContent='Default: create a new timestamped file in ~/.aetherward/sessions/ for every run.';
    else if(policy==='custom') hint.textContent='Custom: use the exact file path below; repeated runs append to the same file.';
    else hint.textContent='No file output: captures are not written as a session file.';
  }
}
function wizShowAdvanced(){
  document.getElementById('wiz-adv-wardriver').style.display=    W.mode==='wardriver'?'':'none';
  document.getElementById('wiz-adv-trilateration').style.display= W.mode==='trilateration'?'':'none';
  document.getElementById('wiz-adv-array-sensing').style.display= W.mode==='array_sensing'?'':'none';
}
function wizSetSync(s){
  W.sync=s;
  ['software','ntp','pps','gpsdo'].forEach(k=>{
    const el=document.getElementById('wts-'+k);
    if(el) el.classList.toggle('sel',k===s);
  });
  document.getElementById('wiz-sync-dev-wrap').style.display=(s==='pps'||s==='gpsdo')?'':'none';
}
function wizGpsChange(){
  const v=document.getElementById('wiz-gps').value;
  document.getElementById('wiz-gps-static').style.display=(v==='static')?'':'none';
}
function wizRenderAnts(){
  const n=parseInt(document.getElementById('wiz-ant-count').value)||1;
  while(W.antennas.length<n) W.antennas.push({id:'wlan'+W.antennas.length,preset:'wifi24',freqMin:2400000000,freqMax:2500000000,backend:'plugins.wifi_nl80211.NL80211Backend',x:0,y:0,z:0});
  W.antennas=W.antennas.slice(0,n);
  const c=document.getElementById('wiz-ants'); if(!c)return;
  const needPos=(W.mode==='trilateration'||W.mode==='array_sensing');
  c.innerHTML=W.antennas.map((a,i)=>`
    <div class="ant-card">
      <div class="ant-card-hdr">Antenna ${i+1}</div>
      <div class="field-row">
        <div class="fg" style="flex:1;min-width:110px">
          <label>Interface / ID <span class="tip" data-tip="Wireless interface name (e.g. wlan0).\nDetected interfaces are shown in the dropdown.\nType any name if yours is not listed.">?</span></label>
          <input value="${a.id}" list="det-ifaces" oninput="W.antennas[${i}].id=this.value" style="font-family:monospace">
        </div>
        <div class="fg" style="flex:1;min-width:150px">
          <label>Frequency range <span class="tip" data-tip="Choose a preset or select Custom to enter Hz values manually.">?</span></label>
          <select onchange="wizPreset(${i},this.value)">
            ${Object.entries(PRESETS).map(([k,v])=>`<option value="${k}"${k===a.preset?' selected':''}>${v.l}</option>`).join('')}
          </select>
        </div>
        <div class="fg" style="flex:1;min-width:110px" id="ant-custom-${i}" style="display:${a.preset==='custom'?'flex':'none'}">
          <label>Min Hz</label><input type="number" value="${a.freqMin}" oninput="W.antennas[${i}].freqMin=+this.value">
        </div>
        <div class="fg" style="flex:1;min-width:110px" id="ant-customhi-${i}" style="display:${a.preset==='custom'?'flex':'none'}">
          <label>Max Hz</label><input type="number" value="${a.freqMax}" oninput="W.antennas[${i}].freqMax=+this.value">
        </div>
        <div class="fg" style="flex:1;min-width:130px">
          <label>Backend</label>
          <select onchange="W.antennas[${i}].backend=this.value">
            ${BACKENDS.map(b=>`<option value="${b.id}"${b.id===a.backend?' selected':''}>${b.l}</option>`).join('')}
          </select>
        </div>
      </div>
      ${needPos?`<div class="field-row" style="margin-top:.5rem">
        <div class="fg"><label>X (m) <span class="tip" data-tip="ENU offset in metres from array origin.\nX=East, Y=North, Z=Up.">?</span></label><input type="number" step="any" value="${a.x}" oninput="W.antennas[${i}].x=+this.value"></div>
        <div class="fg"><label>Y (m)</label><input type="number" step="any" value="${a.y}" oninput="W.antennas[${i}].y=+this.value"></div>
        <div class="fg"><label>Z (m)</label><input type="number" step="any" value="${a.z}" oninput="W.antennas[${i}].z=+this.value"></div>
      </div>`:''}
    </div>`).join('');
}
function wizPreset(i,k){
  const p=PRESETS[k]; W.antennas[i].preset=k;
  if(p.lo!==null){W.antennas[i].freqMin=p.lo;W.antennas[i].freqMax=p.hi;}
  document.getElementById('ant-custom-'+i).style.display=k==='custom'?'flex':'none';
  document.getElementById('ant-customhi-'+i).style.display=k==='custom'?'flex':'none';
}
function wizGenToml(){
  const name=(document.getElementById('wiz-name')?.value||W.configName||'my-config').trim()||'my-config';
  const gps=document.getElementById('wiz-gps').value;
  const sync=document.getElementById('wiz-sync-dev')?.value||'';
  const outPolicy=document.getElementById('wiz-output-policy')?.value||W.outputPolicy||'default';
  const outInput=(document.getElementById('wiz-output')?.value||W.output||'').trim();
  const useCustomOut=outPolicy==='custom';
  const customOut=useCustomOut ? (outInput||`~/.aetherward/sessions/${name}.jsonl`) : '';
  const fileOutput=outPolicy!=='none';
  const lines=[
    `# AetherWard configuration — generated by web wizard`,
    `array_id = "${name}"`,
    `mode = "${W.mode}"`,``,
  ];
  W.antennas.forEach((a,i)=>{
    lines.push(`[[antennas]]`);
    lines.push(`id = "${a.id}"`);
    lines.push(`backend = "${a.backend}"`);
    // backend_config: NL80211 needs interface name
    if(a.backend.includes('NL80211')) lines.push(`backend_config = {interface = "${a.id}"}`);
    lines.push(`frequency_range = [${a.freqMin}, ${a.freqMax}]`);
    // default antenna spacing for array modes (0.5 m apart on X axis)
    const x=a.x||+(i*0.5).toFixed(2), y=a.y||0, z=a.z||0;
    lines.push(`position = [${x}, ${y}, ${z}]`);
    lines.push(`orientation_euler = [0.0, 0.0, 0.0]`);
    lines.push(`gain_dbi = 0.0`);
    lines.push(``);
  });
  lines.push(`[gps]`);
  lines.push(`backend = "${gps}"`);
  if(gps==='gpsd'){lines.push(`host = "localhost"`);lines.push(`port = 2947`);}
  if(gps==='static'){
    lines.push(`lat = ${parseFloat(document.getElementById('wiz-lat').value)||0}`);
    lines.push(`lon = ${parseFloat(document.getElementById('wiz-lon').value)||0}`);
    lines.push(`alt = ${parseFloat(document.getElementById('wiz-alt').value)||0}`);
  }
  lines.push(``);
  lines.push(`[sync]`);
  lines.push(`source = "${W.sync}"`);
  if(sync) lines.push(`device = "${sync}"`);
  lines.push(``);
  // mode-specific config block (reads live values from advanced step inputs)
  const ch=document.getElementById('wiz-channels')?.value||W.channels||'1,2,3,4,5,6,7,8,9,10,11,12,13';
  const hop=parseFloat(document.getElementById('wiz-hop')?.value)||W.hopInterval||0.1;
  const triCh=parseInt(document.getElementById('wiz-tri-channel')?.value)||W.triChannel||6;
  const corrWin=parseFloat(document.getElementById('wiz-corr-window')?.value)||W.corrWindow||0.001;
  const grpTo=parseFloat(document.getElementById('wiz-group-timeout')?.value)||W.groupTimeout||0.05;
  const senseCh=parseInt(document.getElementById('wiz-sense-channel')?.value)||W.senseChannel||6;
  const histLen=parseInt(document.getElementById('wiz-history-len')?.value)||W.historyLen||100;
  const calibFr=parseInt(document.getElementById('wiz-calib-frames')?.value)||W.calibFrames||50;
  const sens=parseFloat(document.getElementById('wiz-sensitivity')?.value)||W.sensitivity||0.05;
  const hyst=parseFloat(document.getElementById('wiz-hysteresis')?.value)||W.hysteresis||0.4;
  const ema=parseFloat(document.getElementById('wiz-ema-alpha')?.value)||W.emaAlpha||0.3;
  lines.push(`[mode_config]`);
  if(W.mode==='wardriver'){
    const chArr='['+ch.split(',').map(c=>c.trim()).filter(Boolean).join(', ')+']';
    lines.push(`channels = ${chArr}`);
    lines.push(`hop_interval = ${hop}`);
    if(useCustomOut) lines.push(`output_path = "${customOut}"`);
    if(fileOutput) lines.push(`store_raw_frames = true`);
  } else if(W.mode==='trilateration'){
    const refId=W.antennas[0]?.id||'wlan0';
    lines.push(`channel = ${triCh}`);
    lines.push(`reference_antenna = "${refId}"`);
    lines.push(`correlation_window = ${corrWin}`);
    lines.push(`group_timeout = ${grpTo}`);
  } else if(W.mode==='array_sensing'){
    lines.push(`channel = ${senseCh}`);
    lines.push(`history_len = ${histLen}`);
    lines.push(`calibration_frames = ${calibFr}`);
    lines.push(`sensitivity = ${sens}`);
    lines.push(`hysteresis = ${hyst}`);
    lines.push(`ema_alpha = ${ema}`);
  }
  lines.push(``);
  lines.push(`[output]`);
  if(!fileOutput){
    lines.push(`format = "none"`);
  } else {
    lines.push(`format = "jsonl"`);
    if(useCustomOut) lines.push(`path = "${customOut}"`);
    else lines.push(`path_policy = "default"`);
  }
  document.getElementById('wiz-toml').value=lines.join('\n');
}
function wizSave(){
  const name=(document.getElementById('wiz-name')?.value||'').trim();
  const content=document.getElementById('wiz-toml').value;
  const err=document.getElementById('wiz-err'); err.style.display='none';
  if(!name){err.textContent='Enter a config name.';err.style.display='block';return;}
  fetch('/api/config/save',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({name,content})})
    .then(r=>r.json()).then(d=>{
      if(d.error){err.textContent=d.error;err.style.display='block';return;}
      closeWizard(); loadConfigs();
      // switch to configs tab
      document.querySelectorAll('nav button').forEach(b=>b.classList.toggle('active',b.dataset.tab==='configs'));
      document.querySelectorAll('.panel').forEach(p=>p.classList.remove('active'));
      document.getElementById('panel-configs').classList.add('active');
    });
}

// close modals on backdrop click
['cfg-modal','src-modal','wiz-modal'].forEach(id=>{
  document.getElementById(id).addEventListener('click',e=>{if(e.target===e.currentTarget)e.currentTarget.classList.remove('open');});
});

// ── Tooltip system (body-level fixed, never clipped) ──────────────────────────
let _tip=null;
document.addEventListener('mouseover',e=>{
  const el=e.target.closest('.tip'); if(!el)return;
  const text=el.dataset.tip; if(!text)return;
  _tip=document.createElement('div'); _tip.className='tip-popup'; _tip.textContent=text.replace(/\\n/g,'\n');
  document.body.appendChild(_tip);
  const r=el.getBoundingClientRect(), tw=_tip.offsetWidth, th=_tip.offsetHeight;
  let left=r.left+r.width/2-tw/2, top=r.top-th-8;
  left=Math.max(8,Math.min(left,window.innerWidth-tw-8));
  if(top<8) top=r.bottom+8;
  _tip.style.left=left+'px'; _tip.style.top=top+'px';
});
document.addEventListener('mouseout',e=>{
  const el=e.target.closest('.tip');
  if(el&&!el.contains(e.relatedTarget)){if(_tip){_tip.remove();_tip=null;}}
});

// ── Hardware detect shared helpers ────────────────────────────────────────────
let _detCache=null;

function _renderDetectHtml(d){
  const serial=d.serial||[];
  const serialV=serial.length?serial.join(', '):'none found';
  const gpsdHint=serial.length&&!d.gpsd
    ?` <span style="color:var(--mu);font-size:.75rem">→ sudo gpsd ${serial[0]} -F /var/run/gpsd.sock</span>`
    :'';
  const items=[
    {l:'WiFi interfaces', v:d.wifi_ifaces?.length?d.wifi_ifaces.join(', '):'none found', ok:d.wifi_ifaces?.length>0},
    {l:'Serial ports (GPS)', v:serialV+gpsdHint, ok:serial.length>0},
    {l:'gpsd (localhost:2947)', v:d.gpsd?'running':'not detected', ok:d.gpsd},
    {l:'RTL-SDR (pyrtlsdr)', v:d.rtlsdr?'available':'not installed', ok:d.rtlsdr},
    {l:'C core (libaw.so)', v:d.c_core?'loaded':'Python fallback active', ok:d.c_core},
    {l:'PPS device (/dev/pps*)', v:d.pps?'found':'not found', ok:d.pps},
  ];
  return items.map(i=>`<div style="display:flex;gap:.55rem;align-items:baseline;font-size:.81rem;margin-bottom:.35rem">
    <span style="color:${i.ok?'var(--grn)':'var(--mu)'}">●</span>
    <span style="color:var(--mu);min-width:170px">${i.l}</span>
    <span style="color:${i.ok?'var(--txt)':'var(--mu)'}">${i.v}</span>
  </div>`).join('');
}

function _applyDetect(d){
  const html=_renderDetectHtml(d);
  // update settings panel (full detail)
  const el=document.getElementById('settings-detect');
  if(el) el.innerHTML=html;
  // wizard: compact interface hint in antenna step only
  const hint=document.getElementById('wiz-iface-hint');
  const ilist=document.getElementById('wiz-iface-list');
  if(hint&&ilist){
    if(d.wifi_ifaces?.length){
      ilist.textContent=d.wifi_ifaces.join(', ');
      hint.style.display='';
    } else {
      hint.style.display='none';
    }
  }
  // populate datalist with detected interfaces
  const dl=document.getElementById('det-ifaces');
  if(dl&&d.wifi_ifaces?.length){
    dl.innerHTML=d.wifi_ifaces.map(f=>`<option value="${f}">`).join('');
  }
  // pre-fill antenna IDs from detected ifaces
  if(d.wifi_ifaces?.length){
    d.wifi_ifaces.forEach((iface,i)=>{ if(i<W.antennas.length) W.antennas[i].id=iface; });
  }
  wizRenderAnts();
}

// ── Settings tab ──────────────────────────────────────────────────────────────
function loadDetect(){
  fetch('/api/detect').then(r=>r.json()).then(d=>{ _detCache=d; _applyDetect(d); }).catch(()=>{});
}

// ── Dashboard banner (full) ───────────────────────────────────────────────────
function loadBanner(){
  fetch('/api/banner').then(r=>r.text()).then(h=>{
    const el=document.getElementById('banner-hero');
    if(el) el.innerHTML=h;
  }).catch(()=>{const el=document.getElementById('banner-hero');if(el)el.style.display='none';});
}

// ── ENU 3-D viewer ────────────────────────────────────────────────────────────
let _enuRecs = [];
const _enuHov = document.getElementById('enu-hover');

function enuLoadFile(){ document.getElementById('enu-file-input').click(); }
function enuClear(){ _enuRecs=[]; enuRender(); }

function enuFileSelected(input){
  const f=input.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=e=>{
    _enuRecs=[];
    for(const line of e.target.result.split('\n')){
      const t=line.trim(); if(!t) continue;
      try{ const rec=JSON.parse(t);
        if(rec.x_enu!=null||rec.y_enu!=null) _enuRecs.push(rec);
      } catch(ex){}
    }
    enuRender();
  };
  r.readAsText(f);
  input.value='';
}

function enuRender(){
  _enuDrawXY(); _enuDrawXZ(); _enuDrawYZ(); _enuTable();
}

function _enuBounds(axis1, axis2){
  if(!_enuRecs.length) return {min1:-5,max1:5,min2:-5,max2:5};
  const v1=_enuRecs.map(r=>r[axis1]||0), v2=_enuRecs.map(r=>r[axis2]||0);
  const pad=0.5;
  let mn1=Math.min(...v1)-pad, mx1=Math.max(...v1)+pad;
  let mn2=Math.min(...v2)-pad, mx2=Math.max(...v2)+pad;
  // square it up
  const span=Math.max(mx1-mn1, mx2-mn2);
  const c1=(mn1+mx1)/2, c2=(mn2+mx2)/2;
  return {min1:c1-span/2, max1:c1+span/2, min2:c2-span/2, max2:c2+span/2};
}

function _enuColor(rec){
  const m=rec.method||rec.pos_method||'';
  if(m==='tdoa')           return '#ff1c1c';
  if(m==='presence')       return '#3fb950';
  if(m==='motion')         return '#e3b341';
  if(m==='absence')        return '#7d5858';
  if(m==='rss_trilateration') return '#4a9eff';
  return '#cc8888';
}

function _enuDraw(canvasId, xKey, yKey, xLabel, yLabel){
  const cv=document.getElementById(canvasId); if(!cv) return;
  const dpr=window.devicePixelRatio||1;
  const cssW=cv.clientWidth||160, cssH=cv.clientHeight||160;
  const intW=Math.round(cssW*dpr), intH=Math.round(cssH*dpr);
  if(cv.width!==intW||cv.height!==intH){cv.width=intW;cv.height=intH;}
  const ctx=cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const W=cssW, H=cssH;
  const PAD=28;
  ctx.clearRect(0,0,W,H);
  ctx.fillStyle='#0b0808'; ctx.fillRect(0,0,W,H);

  const b=_enuBounds(xKey, yKey);
  const toX=v=>PAD+(v-b.min1)/(b.max1-b.min1)*(W-PAD*2);
  const toY=v=>H-PAD-(v-b.min2)/(b.max2-b.min2)*(H-PAD*2);

  // Grid
  ctx.strokeStyle='#2a0d0d'; ctx.lineWidth=1;
  const nLines=5;
  for(let i=0;i<=nLines;i++){
    const gx=PAD+i*(W-PAD*2)/nLines, gy=PAD+i*(H-PAD*2)/nLines;
    ctx.beginPath(); ctx.moveTo(gx,PAD); ctx.lineTo(gx,H-PAD); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(PAD,gy); ctx.lineTo(W-PAD,gy); ctx.stroke();
  }

  // Axes labels
  ctx.fillStyle='#4a3030'; ctx.font='9px monospace'; ctx.textAlign='center';
  ctx.fillText(xLabel, W/2, H-4);
  ctx.save(); ctx.translate(10,H/2); ctx.rotate(-Math.PI/2);
  ctx.fillText(yLabel,0,0); ctx.restore();

  // Origin cross
  const ox=toX(0), oy=toY(0);
  ctx.strokeStyle='#3d1515'; ctx.lineWidth=1;
  ctx.beginPath(); ctx.moveTo(PAD,oy); ctx.lineTo(W-PAD,oy); ctx.stroke();
  ctx.beginPath(); ctx.moveTo(ox,PAD); ctx.lineTo(ox,H-PAD); ctx.stroke();

  // Points
  _enuRecs.forEach(rec=>{
    const x=toX(rec[xKey]||0), y=toY(rec[yKey]||0);
    const rssi=rec.rssi||rec.rssi_at_1m||-70;
    const r=Math.max(3, Math.min(10, 3 + (rssi+30)/10));
    const col=_enuColor(rec);
    ctx.beginPath(); ctx.arc(x,y,r,0,Math.PI*2);
    ctx.fillStyle=col+'99'; ctx.fill();
    ctx.strokeStyle=col; ctx.lineWidth=1.2; ctx.stroke();
  });

  // Tick values
  ctx.fillStyle='#4a3030'; ctx.font='8px monospace'; ctx.textAlign='center';
  for(let i=0;i<=nLines;i++){
    const val=b.min1+(b.max1-b.min1)*i/nLines;
    ctx.fillText(val.toFixed(1), PAD+i*(W-PAD*2)/nLines, H-PAD+11);
  }
  ctx.textAlign='right';
  for(let i=0;i<=nLines;i++){
    const val=b.min2+(b.max2-b.min2)*i/nLines;
    ctx.fillText(val.toFixed(1), PAD-3, H-PAD-i*(H-PAD*2)/nLines+3);
  }
}

function _enuDrawXY(){ _enuDraw('enu-canvas-xy','x_enu','y_enu','East (m)','North (m)'); }
function _enuDrawXZ(){ _enuDraw('enu-canvas-xz','x_enu','z_enu','E','Up'); }
function _enuDrawYZ(){ _enuDraw('enu-canvas-yz','y_enu','z_enu','N','Up'); }

function _enuTable(){
  const tb=document.getElementById('enu-tb'); if(!tb) return;
  tb.innerHTML=_enuRecs.length===0
    ?'<tr><td colspan="7" style="color:var(--mu);text-align:center;padding:1rem">Load a JSONL file with x_enu / y_enu / z_enu fields</td></tr>'
    :_enuRecs.map(r=>`<tr>
        <td style="font-family:monospace;font-size:.78rem">${r.id||'—'}</td>
        <td>${(r.x_enu||0).toFixed(3)}</td><td>${(r.y_enu||0).toFixed(3)}</td>
        <td>${(r.z_enu||0).toFixed(3)}</td>
        <td>${r.rssi!=null?r.rssi.toFixed(1)+' dBm':'—'}</td>
        <td><span class="badge b-man" style="color:${_enuColor(r)}">${r.method||r.pos_method||'—'}</span></td>
        <td style="color:var(--mu)">${r.t?new Date(r.t*1000).toISOString().slice(11,19):'—'}</td>
      </tr>`).join('');
}

// Hover probe on XY canvas
function enuMouseMove(ev, view){
  const cv=document.getElementById('enu-canvas-xy'); if(!cv) return;
  const rect=cv.getBoundingClientRect();
  const W=rect.width, H=rect.height, PAD=28;
  const px=ev.clientX-rect.left;
  const py=ev.clientY-rect.top;
  const b=_enuBounds('x_enu','y_enu');
  const worldX=b.min1+(px-PAD)/(W-PAD*2)*(b.max1-b.min1);
  const worldY=b.min2+(H-PAD-py)/(H-PAD*2)*(b.max2-b.min2);
  // Find nearest point
  let best=null, bestD=Infinity;
  _enuRecs.forEach(r=>{
    const dx=(r.x_enu||0)-worldX, dy=(r.y_enu||0)-worldY;
    const d=Math.sqrt(dx*dx+dy*dy);
    if(d<bestD){ bestD=d; best=r; }
  });
  if(best&&bestD<(b.max1-b.min1)*0.05){
    _enuHov.style.display='block';
    _enuHov.style.left=(ev.clientX+14)+'px';
    _enuHov.style.top=(ev.clientY-28)+'px';
    _enuHov.textContent=`${best.id||'?'}  X:${(best.x_enu||0).toFixed(2)} Y:${(best.y_enu||0).toFixed(2)} Z:${(best.z_enu||0).toFixed(2)}  ${best.rssi!=null?best.rssi.toFixed(1)+' dBm':''}`;
  } else {
    _enuHov.style.display='none';
  }
}
function enuMouseLeave(){ _enuHov.style.display='none'; }

// ── TDOA 3D viewer ────────────────────────────────────────────────────────────
let _t3Sources=[], _t3Receivers=[];
let _t3Yaw=0.45, _t3Pitch=0.32, _t3Zoom=55;
let _t3Drag=null, _t3ShowLines=false;

function tdoa3dLoadFile(){ document.getElementById('tdoa3d-file').click(); }
function tdoa3dClear(){ _t3Sources=[]; _t3Receivers=[]; tdoa3dRender(); }

function tdoa3dFileSelected(input){
  const f=input.files[0]; if(!f) return;
  const r=new FileReader();
  r.onload=e=>{
    _t3Sources=[];
    for(const line of e.target.result.split('\n')){
      const t=line.trim(); if(!t) continue;
      try{
        const rec=JSON.parse(t);
        if(rec.x_enu!=null||rec.y_enu!=null)
          _t3Sources.push({id:rec.id||'?',x:rec.x_enu||0,y:rec.y_enu||0,z:rec.z_enu||0,
            rssi:rec.rssi??null,method:rec.method||rec.pos_method||'tdoa'});
      }catch(ex){}
    }
    tdoa3dRender();
  };
  r.readAsText(f); input.value='';
}

function tdoa3dLoadConfig(){
  const name=document.getElementById('tdoa3d-config').value;
  if(!name){_t3Receivers=[];tdoa3dRender();return;}
  fetch('/api/config/raw?name='+encodeURIComponent(name))
    .then(r=>r.json()).then(d=>{
      _t3Receivers=d.error?[]:_parseTdoa3dReceivers(d.content||'');
      tdoa3dRender();
    });
}
function _parseTdoa3dReceivers(toml){
  const out=[];
  for(const b of toml.split('[[antennas]]').slice(1)){
    const idM=b.match(/^\s*id\s*=\s*"([^"]+)"/m);
    const posM=b.match(/^\s*position\s*=\s*\[([^\]]+)\]/m);
    if(idM&&posM){
      const [x,y,z]=posM[1].split(',').map(parseFloat);
      out.push({id:idM[1],x:x||0,y:y||0,z:z||0});
    }
  }
  return out;
}

function _t3Project(px,py,pz,CW,CH){
  const cy=Math.cos(_t3Yaw),sy=Math.sin(_t3Yaw);
  const cp=Math.cos(_t3Pitch),sp=Math.sin(_t3Pitch);
  const rx=px*cy-py*sy, ry=px*sy+py*cy, rz=pz;
  const ry2=ry*cp-rz*sp, rz2=ry*sp+rz*cp;
  return{sx:CW/2+rx*_t3Zoom, sy:CH/2-ry2*_t3Zoom, depth:rz2};
}

function tdoa3dRender(){
  const cv=document.getElementById('tdoa3d-canvas'); if(!cv) return;
  // Sync internal resolution to CSS size (fixes full-screen blur / stretch)
  const dpr=window.devicePixelRatio||1;
  const cssW=cv.clientWidth||560, cssH=cv.clientHeight||480;
  const intW=Math.round(cssW*dpr), intH=Math.round(cssH*dpr);
  if(cv.width!==intW||cv.height!==intH){cv.width=intW;cv.height=intH;}
  const ctx=cv.getContext('2d');
  ctx.setTransform(dpr,0,0,dpr,0,0);
  const CW=cssW, CH=cssH;
  ctx.clearRect(0,0,CW,CH);
  ctx.fillStyle='#0c0c10'; ctx.fillRect(0,0,CW,CH);

  const allPts=[..._t3Sources,..._t3Receivers];
  let gMax=4;
  if(allPts.length) gMax=Math.max(4,...allPts.map(p=>Math.abs(p.x)),...allPts.map(p=>Math.abs(p.y)),...allPts.map(p=>Math.abs(p.z)))+1;

  // Grid at Z=0
  const gN=6;
  ctx.strokeStyle='rgba(44,28,58,.7)'; ctx.lineWidth=0.8;
  for(let i=-gN;i<=gN;i++){
    const t=gMax*i/gN;
    const a=_t3Project(t,-gMax,0,CW,CH), b=_t3Project(t,gMax,0,CW,CH);
    ctx.beginPath();ctx.moveTo(a.sx,a.sy);ctx.lineTo(b.sx,b.sy);ctx.stroke();
    const c=_t3Project(-gMax,t,0,CW,CH), d=_t3Project(gMax,t,0,CW,CH);
    ctx.beginPath();ctx.moveTo(c.sx,c.sy);ctx.lineTo(d.sx,d.sy);ctx.stroke();
  }

  // Axes (X=blue/east, Y=green/north, Z=purple/up)
  const axL=gMax*0.55;
  [[axL,0,0,'#60a5fa','E'],[0,axL,0,'#22d3a0','N'],[0,0,axL,'#a855f7','U']].forEach(([dx,dy,dz,col,lbl])=>{
    const o=_t3Project(0,0,0,CW,CH), e=_t3Project(dx,dy,dz,CW,CH);
    ctx.strokeStyle=col; ctx.lineWidth=1.8;
    ctx.beginPath();ctx.moveTo(o.sx,o.sy);ctx.lineTo(e.sx,e.sy);ctx.stroke();
    ctx.fillStyle=col; ctx.font='bold 11px monospace'; ctx.textAlign='center';
    ctx.fillText(lbl,e.sx,e.sy-5);
  });

  // Connection lines (receiver→source)
  if(_t3ShowLines&&_t3Sources.length&&_t3Receivers.length){
    _t3Receivers.forEach(rx=>{
      _t3Sources.forEach(src=>{
        const rp=_t3Project(rx.x,rx.y,rx.z,CW,CH), sp=_t3Project(src.x,src.y,src.z,CW,CH);
        ctx.strokeStyle='rgba(96,165,250,.1)'; ctx.lineWidth=0.7;
        ctx.beginPath();ctx.moveTo(rp.sx,rp.sy);ctx.lineTo(sp.sx,sp.sy);ctx.stroke();
      });
    });
  }

  // Collect for depth sort
  const drawList=[
    ..._t3Receivers.map(r=>({type:'rx',p:_t3Project(r.x,r.y,r.z,CW,CH),rec:r})),
    ..._t3Sources.map(r=>({type:'src',p:_t3Project(r.x,r.y,r.z,CW,CH),rec:r})),
  ];
  drawList.sort((a,b)=>b.p.depth-a.p.depth);

  drawList.forEach(({type,p,rec})=>{
    if(type==='rx'){
      const s=7;
      ctx.fillStyle='rgba(0,212,200,.18)'; ctx.fillRect(p.sx-s,p.sy-s,s*2,s*2);
      ctx.strokeStyle='#00d4c8'; ctx.lineWidth=1.6; ctx.strokeRect(p.sx-s,p.sy-s,s*2,s*2);
      ctx.fillStyle='#00d4c8'; ctx.font='9px monospace'; ctx.textAlign='left';
      ctx.fillText(rec.id,p.sx+s+3,p.sy+3);
    } else {
      const rssi=rec.rssi??null;
      const r2=rssi!=null?Math.max(4,Math.min(12,4+(rssi+30)/10)):5;
      ctx.beginPath(); ctx.arc(p.sx,p.sy,r2,0,Math.PI*2);
      ctx.fillStyle='rgba(255,60,60,.25)'; ctx.fill();
      ctx.strokeStyle='#ff3c3c'; ctx.lineWidth=1.5; ctx.stroke();
      const grd=ctx.createRadialGradient(p.sx,p.sy,0,p.sx,p.sy,r2*2.5);
      grd.addColorStop(0,'rgba(255,60,60,.18)'); grd.addColorStop(1,'rgba(255,60,60,0)');
      ctx.fillStyle=grd; ctx.beginPath(); ctx.arc(p.sx,p.sy,r2*2.5,0,Math.PI*2); ctx.fill();
    }
  });

  // Origin marker
  const o=_t3Project(0,0,0,CW,CH);
  ctx.fillStyle='rgba(255,255,255,.25)';
  ctx.beginPath();ctx.arc(o.sx,o.sy,2.5,0,Math.PI*2);ctx.fill();

  _t3Table();
}

function _t3Table(){
  const tb=document.getElementById('tdoa3d-tb'); if(!tb) return;
  const rows=[
    ..._t3Receivers.map(r=>({...r,role:'receiver'})),
    ..._t3Sources.map(r=>({...r,role:'source'})),
  ];
  tb.innerHTML=rows.length===0
    ?'<tr><td colspan="6" style="color:var(--mu);text-align:center;padding:1.1rem">Load a JSONL file with x_enu / y_enu / z_enu records</td></tr>'
    :rows.map(r=>`<tr>
        <td style="font-family:monospace;font-size:.78rem">${r.id||'—'}</td>
        <td><span class="badge" style="background:${r.role==='receiver'?'#002828':'#280606'};color:${r.role==='receiver'?'#00d4c8':'#ff6060'}">${r.role}</span></td>
        <td>${(r.x||0).toFixed(3)}</td><td>${(r.y||0).toFixed(3)}</td>
        <td>${(r.z||0).toFixed(3)}</td>
        <td>${r.rssi!=null?r.rssi.toFixed(1)+' dBm':'—'}</td>
      </tr>`).join('');
}

function tdoa3dMouseDown(e){
  _t3Drag={x:e.clientX,y:e.clientY};
  e.currentTarget.classList.add('tdoa3d-dragging');
}
function tdoa3dMouseMove(e){
  if(!_t3Drag) return;
  _t3Yaw  +=(e.clientX-_t3Drag.x)*0.007;
  _t3Pitch+=(e.clientY-_t3Drag.y)*0.007;
  _t3Pitch=Math.max(-Math.PI/2+0.05,Math.min(Math.PI/2-0.05,_t3Pitch));
  _t3Drag={x:e.clientX,y:e.clientY};
  tdoa3dRender();
}
function tdoa3dMouseUp(){
  _t3Drag=null;
  const cv=document.getElementById('tdoa3d-canvas');
  if(cv) cv.classList.remove('tdoa3d-dragging');
}
function tdoa3dWheel(e){
  e.preventDefault();
  _t3Zoom=Math.max(8,Math.min(350,_t3Zoom*(e.deltaY>0?0.88:1.12)));
  tdoa3dRender();
}

// ── Init ──────────────────────────────────────────────────────────────────────
connectSSE();
loadStatus();
loadBanner();
renderBannerCanvas();
loadDetect();
setInterval(loadStatus,5000);
</script>
</body>
</html>"""
