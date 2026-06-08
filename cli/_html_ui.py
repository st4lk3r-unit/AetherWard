_HTML_UI = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>AetherWard</title>
<link rel="icon" type="image/svg+xml" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='6' fill='%230b0808'/%3E%3Ctext x='16' y='23' text-anchor='middle' font-family='system-ui' font-weight='900' font-size='15' fill='%23ff1c1c'%3EAW%3C/text%3E%3C/svg%3E">
<link rel="stylesheet" href="https://unpkg.com/leaflet@1.9.4/dist/leaflet.css"/>
<script src="https://unpkg.com/leaflet@1.9.4/dist/leaflet.js"></script>
<style>
:root{
  --acc:#ff3c3c;  --acc2:#c01818;  --accDm:#2a0505;
  --bg:#0d0c12;   --bg2:#14131c;   --bg3:#1c1b28;
  --bdr:#2c1c3a;  --bdr2:#1c1430;
  --txt:#c0b0d8;  --mu:#706082;    --mu2:#38284c;
  --grn:#22d3a0;  --ylw:#f0c040;   --blu:#60a5fa;
  --pur:#a855f7;  --org:#ff8c42;   --cyn:#00d4c8;
  --red2:#ff9090;
}
*{box-sizing:border-box;margin:0;padding:0}
body{font-family:'Segoe UI',system-ui,sans-serif;background:var(--bg);color:var(--txt);height:100vh;display:flex;flex-direction:column;overflow:hidden}
a{color:var(--acc)} code{background:var(--bg3);padding:.1rem .35rem;border-radius:4px;font-size:.85em;color:var(--org)}

header{background:var(--bg2);border-bottom:1px solid var(--bdr);padding:.35rem 1rem;display:flex;align-items:center;gap:.75rem;flex-shrink:0;height:3rem}
.logo-svg{width:2.1rem;height:2.1rem;flex-shrink:0}
#logo-canvas{height:2.2rem;width:auto;flex-shrink:0;image-rendering:pixelated;display:none}
.logo-title{color:var(--acc);font-weight:800;font-size:1.05rem;letter-spacing:.08em;text-shadow:0 0 18px rgba(255,60,60,.3);white-space:nowrap;flex-shrink:0}
.nav-sep{width:1px;background:var(--bdr);margin:.35rem .15rem;flex-shrink:0;align-self:stretch}
.dot{width:8px;height:8px;border-radius:50%;background:var(--grn);box-shadow:0 0 6px var(--grn);transition:background .4s}
.dot.off{background:var(--mu2);box-shadow:none}
#hdr-label{font-size:.78rem;color:var(--mu)}
#hdr-right{margin-left:auto;font-size:.78rem;color:var(--mu);display:flex;gap:1.25rem}

nav{background:var(--bg2);border-bottom:1px solid var(--bdr);display:flex;padding:0 1rem;flex-shrink:0;overflow-x:auto}
nav button{background:none;border:none;color:var(--mu);padding:.55rem .85rem;cursor:pointer;border-bottom:2px solid transparent;font-size:.82rem;white-space:nowrap;transition:color .15s}
nav button.active,nav button:hover{color:var(--txt)}
nav button.active{border-bottom-color:var(--acc);color:var(--acc)}

.panel{display:none;flex:1;overflow:hidden;min-height:0}
.panel.active{display:flex;flex-direction:column}
.scroll{overflow-y:auto;flex:1;padding:1.1rem}

#panel-map{padding:0;overflow:hidden}
#map-filterbar{display:flex;align-items:center;gap:.35rem;padding:.35rem .75rem;background:var(--bg2);border-bottom:1px solid var(--bdr);flex-shrink:0;flex-wrap:wrap;z-index:999;row-gap:.3rem}
.mfpill{background:var(--bg3);border:1px solid var(--bdr);color:var(--mu);border-radius:20px;padding:.18rem .6rem;font-size:.72rem;cursor:pointer;transition:all .15s;flex-shrink:0}
.mfpill.active,.mfpill:hover{background:var(--accDm);border-color:var(--acc);color:var(--acc)}
.lc{cursor:pointer;transition:opacity .15s}.lc.hidden{opacity:.35}
#map-wrap{flex:1;position:relative;min-height:0;overflow:hidden}
#map{position:absolute;inset:0}
#tile-switcher{position:absolute;bottom:.65rem;left:50%;transform:translateX(-50%);z-index:1000;display:flex;gap:.3rem;background:rgba(15,15,20,.75);border:1px solid var(--bdr);border-radius:20px;padding:.25rem .4rem;backdrop-filter:blur(4px)}
.tile-btn{background:transparent;border:none;color:var(--mu);font-size:.68rem;padding:.18rem .55rem;border-radius:14px;cursor:pointer;transition:all .15s;white-space:nowrap}
.tile-btn:hover{color:var(--txt)}
.tile-btn-active{background:var(--accDm);color:var(--acc)!important}
.mf-input{height:24px;font-size:.72rem;padding:.1rem .45rem;background:var(--bg3);border:1px solid var(--bdr);color:var(--txt);border-radius:12px;outline:none;transition:border-color .15s}
.mf-input:focus{border-color:var(--acc)}
.mf-input::placeholder{color:var(--mu)}

.card{background:var(--bg2);border:1px solid var(--bdr);border-radius:6px;padding:.9rem;margin-bottom:.8rem}
.card-title{color:var(--mu);font-size:.68rem;text-transform:uppercase;letter-spacing:.08em;margin-bottom:.55rem}

.stats{display:grid;grid-template-columns:repeat(auto-fit,minmax(120px,1fr));gap:.65rem;margin-bottom:.9rem}
.stat{background:var(--bg2);border:1px solid var(--bdr);border-radius:6px;padding:.8rem;text-align:center}
.stat-v{font-size:1.8rem;font-weight:700;color:var(--acc);line-height:1;text-shadow:0 0 14px rgba(255,60,60,.3)}
.stat-l{font-size:.68rem;color:var(--mu);margin-top:.25rem}

table{width:100%;border-collapse:collapse;font-size:.81rem}
th{text-align:left;padding:.4rem .65rem;color:var(--mu);font-weight:500;border-bottom:1px solid var(--bdr);white-space:nowrap}
td{padding:.4rem .65rem;border-bottom:1px solid var(--bdr2);white-space:nowrap}
tr:hover td{background:var(--bg3)}

.badge{display:inline-block;padding:.1rem .4rem;border-radius:9999px;font-size:.67rem;font-weight:600}
.b-rss{background:#0e1f40;color:#60a5fa}.b-cen{background:#2a2000;color:#f0c040}
.b-tdoa{background:#280606;color:#ff6060}.b-man{background:var(--bg3);color:var(--mu)}

.controls{display:flex;gap:.6rem;align-items:flex-end;flex-wrap:wrap;margin-bottom:.9rem}
.fg{display:flex;flex-direction:column;gap:.22rem}
label{font-size:.71rem;color:var(--mu)}
input,select,textarea{background:var(--bg3);border:1px solid var(--bdr);color:var(--txt);padding:.35rem .6rem;border-radius:6px;font-size:.82rem;font-family:inherit}
input:focus,select:focus,textarea:focus{outline:none;border-color:var(--acc);box-shadow:0 0 0 2px rgba(255,60,60,.1)}
textarea{resize:vertical}

.btn{border:none;padding:.38rem .8rem;border-radius:6px;cursor:pointer;font-size:.81rem;font-weight:600;transition:background .15s,color .15s}
.btn-p{background:var(--acc);color:#fff}.btn-p:hover{background:var(--acc2)}
.btn-stop{background:#3a0a0a;color:#ff8080;border:1px solid #5a1010}.btn-stop:hover{background:#5a1010}
.btn-s{background:var(--bg3);color:var(--txt);border:1px solid var(--bdr)}.btn-s:hover{background:var(--mu2);border-color:var(--mu)}
.btn-del{background:transparent;color:#ff6060;border:1px solid #4a1010;font-size:.73rem;padding:.22rem .55rem}.btn-del:hover{background:#280606}
.btn-edit{background:transparent;color:var(--mu);border:1px solid var(--bdr);font-size:.73rem;padding:.22rem .55rem}.btn-edit:hover{background:var(--bg3);color:var(--txt)}
button:disabled{opacity:.35;cursor:default}

.log-upd{color:var(--grn)}.log-sys{color:var(--mu)}.log-run{color:var(--blu)}

.map-overlay{position:absolute;top:.65rem;right:.65rem;z-index:1000}
.legend{background:rgba(14,13,20,.94);border:1px solid var(--bdr);border-radius:6px;padding:.65rem;font-size:.74rem;backdrop-filter:blur(6px)}
.li{display:flex;align-items:center;gap:.45rem;margin-bottom:.28rem}.li:last-child{margin-bottom:0}
.ld{width:10px;height:10px;border-radius:50%;flex-shrink:0}
.map-stat{background:rgba(14,13,20,.94);border:1px solid var(--bdr);border-radius:6px;padding:.45rem .7rem;font-size:.77rem;margin-top:.45rem;color:var(--txt);text-align:center}

/* modals */
.modal{position:fixed;inset:0;background:rgba(0,0,0,.82);z-index:9999;align-items:center;justify-content:center;display:none}
.modal.open{display:flex}
.modal-box{background:var(--bg2);border:1px solid var(--bdr);border-radius:8px;width:min(720px,96vw);max-height:92vh;display:flex;flex-direction:column;box-shadow:0 0 48px rgba(255,60,60,.1)}
.modal-box.sm{width:min(460px,96vw)}
.modal-hdr{padding:.85rem 1.2rem;border-bottom:1px solid var(--bdr);display:flex;align-items:center;justify-content:space-between;flex-shrink:0}
.modal-hdr-title{font-weight:600;color:var(--acc)}
.modal-hdr button{background:none;border:none;color:var(--mu);cursor:pointer;font-size:1.15rem;line-height:1;padding:.1rem .3rem}.modal-hdr button:hover{color:var(--txt)}
.modal-body{padding:.9rem 1.2rem;display:flex;flex-direction:column;gap:.7rem;flex:1;overflow-y:auto}
.modal-ftr{padding:.85rem 1.2rem;border-top:1px solid var(--bdr);display:flex;gap:.6rem;justify-content:flex-end;flex-shrink:0}

/* tooltips */
.tip{display:inline-flex;align-items:center;justify-content:center;width:15px;height:15px;border-radius:50%;background:var(--bg3);border:1px solid var(--bdr);color:var(--mu);font-size:.62rem;cursor:help;margin-left:.3rem;flex-shrink:0;vertical-align:middle}
.tip-popup{position:fixed;z-index:99999;background:var(--bg2);border:1px solid var(--bdr);color:var(--txt);font-size:.74rem;padding:.45rem .65rem;border-radius:6px;white-space:pre-wrap;max-width:280px;pointer-events:none;line-height:1.45;box-shadow:0 8px 28px rgba(0,0,0,.7)}

/* page header row */
.page-hdr{display:flex;justify-content:space-between;align-items:center;margin-bottom:.85rem;flex-wrap:wrap;gap:.5rem}
.page-hdr-title{font-size:.88rem;font-weight:600;color:var(--txt)}
.page-hdr-actions{display:flex;gap:.45rem;align-items:center;flex-wrap:wrap}

/* wizard */
.wiz-progress{display:flex;align-items:center;gap:0;margin-bottom:1.2rem}
.wiz-dot{width:26px;height:26px;border-radius:50%;border:2px solid var(--bdr);background:var(--bg3);color:var(--mu);font-size:.72rem;font-weight:700;display:flex;align-items:center;justify-content:center;flex-shrink:0;transition:.2s}
.wiz-dot.done{border-color:var(--pur);background:var(--pur);color:#fff}
.wiz-dot.active{border-color:var(--acc);background:var(--acc);color:#fff;box-shadow:0 0 12px rgba(255,60,60,.4)}
.wiz-line{flex:1;height:2px;background:var(--bdr2)}
.wiz-line.done{background:var(--pur)}
.wiz-step{display:none}.wiz-step.active{display:block}
.wiz-step-title{font-weight:600;color:var(--txt);margin-bottom:.65rem;font-size:.9rem}
.wiz-choices{display:flex;flex-direction:column;gap:.55rem}
.wiz-choice{display:flex;align-items:flex-start;gap:.75rem;padding:.75rem;border:1px solid var(--bdr);border-radius:6px;cursor:pointer;transition:border-color .15s}
.wiz-choice:hover{border-color:var(--mu2)}
.wiz-choice input[type=radio]{margin-top:.15rem;accent-color:var(--acc)}
.wiz-choice.sel{border-color:var(--acc);background:var(--accDm)}
.wiz-choice-title{font-weight:600;color:var(--txt);font-size:.85rem}
.wiz-choice-desc{font-size:.77rem;color:var(--mu);margin-top:.2rem;line-height:1.4}
.ant-card{border:1px solid var(--bdr);border-radius:6px;padding:.75rem;margin-bottom:.6rem}
.ant-card-hdr{font-weight:600;color:var(--acc);font-size:.8rem;margin-bottom:.6rem}
.field-row{display:flex;gap:.65rem;flex-wrap:wrap}
.field-row .fg{flex:1;min-width:120px}

/* banner */
#banner-hero{font-family:'Courier New',monospace;font-size:clamp(.38rem,1.1vw,.54rem);line-height:1.18;overflow-x:auto;overflow-y:hidden;padding:.6rem;background:var(--bg2);border:1px solid var(--bdr);border-radius:6px;margin-bottom:.9rem;user-select:none;white-space:pre;text-align:center}
/* ENU hover tooltip */
#enu-hover{position:fixed;z-index:9998;background:var(--bg2);border:1px solid var(--bdr);color:var(--txt);font-size:.72rem;padding:.3rem .5rem;border-radius:5px;pointer-events:none;display:none}
/* Leaflet popup theme */
.leaflet-popup-content-wrapper{background:var(--bg2)!important;color:var(--txt)!important;border:1px solid var(--bdr)!important;box-shadow:0 4px 20px rgba(0,0,0,.7)!important}
.leaflet-popup-tip{background:var(--bg2)!important}
.leaflet-popup-content b{color:var(--acc)}
/* path dot popup */
.aw-path-tip .leaflet-popup-content-wrapper{background:var(--bg2)!important;border:1px solid var(--bdr2)!important;font-size:.74rem;box-shadow:0 4px 18px rgba(0,0,0,.7)!important}
.aw-path-tip .leaflet-popup-tip{background:var(--bg2)!important}
/* 3D canvas grab cursor while dragging */
.tdoa3d-dragging{cursor:grabbing!important}
/* Custom scrollbars — chromium */
::-webkit-scrollbar{width:5px;height:5px}
::-webkit-scrollbar-track{background:var(--bg)}
::-webkit-scrollbar-thumb{background:var(--mu2);border-radius:3px}
::-webkit-scrollbar-thumb:hover{background:var(--mu)}
::-webkit-scrollbar-corner{background:var(--bg)}
/* Firefox */
*{scrollbar-width:thin;scrollbar-color:var(--mu2) var(--bg)}
/* dark Leaflet zoom controls */
.leaflet-control-zoom-in,.leaflet-control-zoom-out{background:var(--bg3)!important;color:var(--txt)!important;border:none!important}
.leaflet-control-zoom-in:hover,.leaflet-control-zoom-out:hover{background:var(--accDm)!important;color:var(--acc)!important}
.leaflet-bar{border:1px solid var(--bdr)!important;box-shadow:none!important}
/* remove native number spinners on map filter inputs */
.mf-input[type=number]::-webkit-inner-spin-button,.mf-input[type=number]::-webkit-outer-spin-button{-webkit-appearance:none;margin:0}
.mf-input[type=number]{-moz-appearance:textfield}
/* checkbox accent colour */
input[type=checkbox]{accent-color:var(--acc)}
/* log panel: allow flex override */
.log{background:var(--bg);border:1px solid var(--bdr);border-radius:6px;padding:.6rem;font-family:monospace;font-size:.77rem;overflow-y:auto;color:var(--mu);min-height:120px}
</style>
</head>
<body>

<header>
  <canvas id="logo-canvas"></canvas>
  <svg class="logo-svg" id="logo-svg-fb" viewBox="0 0 36 36" xmlns="http://www.w3.org/2000/svg">
    <polygon points="18,1.5 33,10 33,26 18,34.5 3,26 3,10" fill="#140a0a" stroke="#ff1c1c" stroke-width="1.4"/>
    <text x="18" y="24" text-anchor="middle" font-family="system-ui,sans-serif" font-weight="900" font-size="13" fill="#ff1c1c">AW</text>
  </svg>
  <span class="logo-title">AetherWard</span>
  <span class="dot off" id="dot"></span>
  <span id="hdr-label">idle</span>
  <div id="hdr-right">
    <span id="hdr-src">0 sources</span>
    <span id="hdr-upd">0 updates</span>
  </div>
</header>

<nav>
  <button class="active" data-tab="dashboard" onclick="tab(this)">Dashboard</button>
  <span class="nav-sep"></span>
  <button data-tab="map"       onclick="tab(this)">Map</button>
  <button data-tab="enu"       onclick="tab(this)">Array / ENU</button>
  <button data-tab="tdoa3d"    onclick="tab(this)">TDOA 3D</button>
  <span class="nav-sep"></span>
  <button data-tab="positions" onclick="tab(this)">Positions</button>
  <button data-tab="run"       onclick="tab(this)">Run</button>
  <button data-tab="solve"     onclick="tab(this)">Solve</button>
  <button data-tab="sessions"  onclick="tab(this)">Sessions</button>
  <span class="nav-sep"></span>
  <button data-tab="configs"   onclick="tab(this)">Configs</button>
  <button data-tab="settings"  onclick="tab(this)">Settings</button>
</nav>

<!-- Dashboard -->
<div class="panel active" id="panel-dashboard">
  <div class="scroll">
    <pre id="banner-hero">Loading…</pre>
    <div class="stats">
      <div class="stat"><div class="stat-v" id="s-total">0</div><div class="stat-l">Sources</div></div>
      <div class="stat"><div class="stat-v" id="s-rss">0</div><div class="stat-l">RSS trilat.</div></div>
      <div class="stat"><div class="stat-v" id="s-cen">0</div><div class="stat-l">Centroid</div></div>
      <div class="stat"><div class="stat-v" id="s-upd">0</div><div class="stat-l">Updates</div></div>
    </div>
    <div class="card">
      <div class="card-title">System status</div>
      <table><tbody id="sys-tb"></tbody></table>
    </div>
    <div style="text-align:center;margin-top:.5rem">
      <button class="btn btn-p" onclick="openWizard()">▶ Setup wizard</button>
    </div>
  </div>
</div>

<!-- Map -->
<div class="panel" id="panel-map">
  <div id="map-filterbar">
    <span style="font-size:.71rem;color:var(--mu);flex-shrink:0">Method:</span>
    <button class="mfpill active" data-m="all"               onclick="mapFilter(this)">All</button>
    <button class="mfpill"        data-m="rss_trilateration" onclick="mapFilter(this)">RSS</button>
    <button class="mfpill"        data-m="rssi_centroid"     onclick="mapFilter(this)">Centroid</button>
    <button class="mfpill"        data-m="tdoa"              onclick="mapFilter(this)">TDOA</button>
    <span class="nav-sep" style="height:20px;flex-shrink:0"></span>
    <span style="font-size:.71rem;color:var(--mu);flex-shrink:0">Role:</span>
    <button class="mfpill mfpill-role active" data-role="all"     onclick="setMapRoleFilter(this)">All</button>
    <button class="mfpill mfpill-role"        data-role="ap"      onclick="setMapRoleFilter(this)">AP only</button>
    <button class="mfpill mfpill-role"        data-role="client"  onclick="setMapRoleFilter(this)">Clients only</button>
    <button class="mfpill mfpill-role"        data-role="unknown" onclick="setMapRoleFilter(this)">Other</button>
    <span class="nav-sep" style="height:20px;flex-shrink:0"></span>
    <input id="map-s-ssid"   class="mf-input" placeholder="SSID…"   style="width:88px"  oninput="_mapSearchSSID=this.value;applyMapFilters()">
    <input id="map-s-mac"    class="mf-input" placeholder="MAC/ID…" style="width:102px;font-family:monospace" oninput="mapMacSearchChanged(this.value)">
    <input id="map-s-radius" class="mf-input" placeholder="Radius m" type="number" min="0" style="width:84px" oninput="_mapSearchRadius=parseFloat(this.value)||0;applyMapFilters()">
    <button class="btn btn-s" id="map-s-center-btn" onclick="setRadiusCenter()" title="Set radius centre to current map centre" style="height:24px;font-size:.71rem;padding:.1rem .5rem;border-radius:12px">⊕ centre</button>
    <div style="flex:1"></div>
    <button class="btn btn-s" id="cluster-toggle-btn" onclick="toggleMapClusters(this)" style="font-size:.72rem;padding:.2rem .55rem" title="Zoom-aware source clustering. Keeps all source records; only the map rendering is grouped.">Clusters ●</button>
    <button class="btn btn-s" id="relations-toggle-btn" onclick="toggleMapRelations(this)" style="font-size:.72rem;padding:.2rem .55rem">Relations ●</button>
    <button class="btn btn-s" id="paths-toggle-btn" onclick="toggleAllPaths()" style="font-size:.72rem;padding:.2rem .55rem">Paths ●</button>
  </div>
  <div id="map-wrap">
    <div id="map"></div>
    <div id="tile-switcher">
      <button class="tile-btn" data-tile="CartoDB Dark"   onclick="_switchTile('CartoDB Dark')">Dark</button>
      <button class="tile-btn" data-tile="CartoDB No Lbl" onclick="_switchTile('CartoDB No Lbl')">No Labels</button>
      <button class="tile-btn" data-tile="Stadia Dark"    onclick="_switchTile('Stadia Dark')">Stadia</button>
      <button class="tile-btn" data-tile="ESRI Dark Gray" onclick="_switchTile('ESRI Dark Gray')">ESRI</button>
    </div>
    <div class="map-overlay">
      <div class="legend">
        <div style="color:var(--acc);font-weight:600;margin-bottom:.45rem;font-size:.73rem;letter-spacing:.05em">ROLE</div>
        <div class="li"><div class="ld" style="background:var(--pur);border-radius:3px"></div><span style="color:var(--txt)">AP / BSSID</span></div>
        <div class="li"><div class="ld" style="background:var(--cyn)"></div><span style="color:var(--txt)">Client / STA</span></div>
        <div class="li"><div class="ld" style="background:var(--org);height:2px;border-radius:0"></div><span style="color:var(--txt)">AP↔client relation</span></div>
        <div class="li"><div class="ld" style="background:var(--acc);height:2px;border-radius:0"></div><span style="color:var(--txt)">Selected sample links</span></div>
      </div>
      <div class="legend" style="margin-top:.45rem">
        <div style="color:var(--acc);font-weight:600;margin-bottom:.45rem;font-size:.73rem;letter-spacing:.05em">METHOD</div>
        <div class="li lc" data-m="rss_trilateration" onclick="mapFilterLegend(this)"><div class="ld" style="background:var(--blu)"></div><span style="color:var(--txt)">RSS trilateration</span></div>
        <div class="li lc" data-m="rssi_centroid"     onclick="mapFilterLegend(this)"><div class="ld" style="background:var(--ylw)"></div><span style="color:var(--txt)">RSSI centroid</span></div>
        <div class="li lc" data-m="tdoa"              onclick="mapFilterLegend(this)"><div class="ld" style="background:var(--acc)"></div><span style="color:var(--txt)">TDOA</span></div>
        <div class="li lc" data-m="manual"            onclick="mapFilterLegend(this)"><div class="ld" style="background:var(--mu)"></div><span style="color:var(--txt)">Manual</span></div>
      </div>
      <div class="map-stat" id="map-count">0 sources in view</div>
      <div class="map-stat" id="map-rel-count">0 AP↔client links in view</div>
      <div class="map-stat" id="map-link-count">No selected source</div>
      <div class="map-stat" id="map-path-count">0 paths, 0 path points in view</div>
      <div class="map-stat" id="map-center" style="font-family:monospace;font-size:.68rem;letter-spacing:.03em;color:var(--mu);padding:.3rem .6rem">—</div>
      <div class="legend" style="margin-top:.45rem">
        <div style="color:var(--acc);font-weight:600;margin-bottom:.45rem;font-size:.73rem;letter-spacing:.05em">PATHS</div>
        <button class="btn btn-s" onclick="clearAllPaths()" style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem;margin-bottom:.3rem">✕ Clear Paths</button>
        <div id="map-paths-list" style="font-size:.7rem;max-height:150px;overflow-y:auto"></div>
      </div>
      <div class="legend" style="margin-top:.45rem">
        <div style="color:var(--acc);font-weight:600;margin-bottom:.45rem;font-size:.73rem;letter-spacing:.05em">EXPORT</div>
        <button class="btn btn-s" onclick="exportMapJSONL()"   style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem;margin-bottom:.3rem">⬇ JSONL</button>
        <button class="btn btn-s" onclick="exportMapGeoJSON()" style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem;margin-bottom:.3rem">⬇ GeoJSON</button>
        <button class="btn btn-s" onclick="exportMapCSV()"     style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem;margin-bottom:.3rem">⬇ CSV</button>
        <button class="btn btn-s" onclick="exportMapKML()"     style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem;margin-bottom:.3rem">⬇ KML</button>
        <button class="btn btn-s" onclick="exportMapWigle()"   style="display:block;width:100%;text-align:left;font-size:.73rem;padding:.3rem .5rem">⬇ WiGLE CSV</button>
      </div>
    </div>
  </div>
</div>

<!-- Array / ENU 3-D viewer -->
<div class="panel" id="panel-enu">
  <div style="flex-shrink:0;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem;padding:.45rem 1rem;background:var(--bg2);border-bottom:1px solid var(--bdr)">
    <span class="page-hdr-title">Array / ENU viewer
      <span class="tip" data-tip="Shows TDOA and array-sensing positions in the array's local ENU frame.\nEast (X) = right, North (Y) = up on the top-down plot.\nLoad a JSONL file with x_enu / y_enu / z_enu fields.\nWardriver sessions show as geo-dots; use the Map tab for those.">?</span>
    </span>
    <div style="display:flex;gap:.4rem;align-items:center">
      <button class="btn btn-s" onclick="enuLoadFile()">Load JSONL…</button>
      <button class="btn btn-s" onclick="enuClear()">Clear</button>
      <input type="file" id="enu-file-input" accept=".jsonl" style="display:none" onchange="enuFileSelected(this)">
    </div>
  </div>
  <div style="flex:0 0 70%;display:flex;gap:.6rem;padding:.55rem;overflow:hidden;min-height:0">
    <div class="card" style="flex:3;padding:.5rem;display:flex;flex-direction:column;min-width:0;overflow:hidden">
      <div class="card-title">Top-down (East / North)</div>
      <canvas id="enu-canvas-xy"
        style="flex:1;min-height:0;width:100%;background:var(--bg);border:1px solid var(--bdr);border-radius:4px;cursor:crosshair;display:block"
        onmousemove="enuMouseMove(event,'xy')" onmouseleave="enuMouseLeave()"></canvas>
      <div id="enu-coords-xy" style="flex-shrink:0;height:1.1rem;overflow:hidden;font-size:.72rem;color:var(--mu);margin-top:.3rem"></div>
    </div>
    <div style="flex:1;display:flex;flex-direction:column;gap:.5rem;min-width:0;overflow:hidden">
      <div class="card" style="flex:1;padding:.5rem;display:flex;flex-direction:column;min-width:0;overflow:hidden">
        <div class="card-title">Elevation (E / Up)</div>
        <canvas id="enu-canvas-xz"
          style="flex:1;min-height:0;width:100%;background:var(--bg);border:1px solid var(--bdr);border-radius:4px;display:block"></canvas>
      </div>
      <div class="card" style="flex:1;padding:.5rem;display:flex;flex-direction:column;min-width:0;overflow:hidden">
        <div class="card-title">Side (N / Up)</div>
        <canvas id="enu-canvas-yz"
          style="flex:1;min-height:0;width:100%;background:var(--bg);border:1px solid var(--bdr);border-radius:4px;display:block"></canvas>
      </div>
    </div>
  </div>
  <div style="flex:1;overflow-y:auto;min-height:0;border-top:1px solid var(--bdr)">
    <table>
      <thead><tr><th>ID</th><th>X (E) m</th><th>Y (N) m</th><th>Z (U) m</th><th>RSSI</th><th>Method</th><th>Time</th></tr></thead>
      <tbody id="enu-tb"></tbody>
    </table>
  </div>
</div>

<!-- TDOA 3D -->
<div class="panel" id="panel-tdoa3d">
  <div style="flex-shrink:0;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:.5rem;padding:.45rem 1rem;background:var(--bg2);border-bottom:1px solid var(--bdr)">
    <span class="page-hdr-title">TDOA 3D — Receiver &amp; Source Geometry
      <span class="tip" data-tip="3-D view of estimated transmitter positions (sources) and receiver (antenna) positions.\nLoad a JSONL with x_enu/y_enu/z_enu fields.\nSelect an array config to display receiver positions from antenna ENU offsets.\nDrag to orbit · Scroll to zoom.">?</span>
    </span>
    <div class="page-hdr-actions">
      <label style="font-size:.78rem;color:var(--mu);display:flex;align-items:center;gap:.35rem;cursor:pointer">
        <input type="checkbox" id="tdoa3d-lines" onchange="_t3ShowLines=this.checked;tdoa3dRender()">
        Lines
      </label>
      <select id="tdoa3d-config" onchange="tdoa3dLoadConfig()" style="font-size:.78rem;padding:.28rem .5rem">
        <option value="">— no config (receivers) —</option>
      </select>
      <button class="btn btn-s" onclick="tdoa3dLoadFile()">Load JSONL…</button>
      <input type="file" id="tdoa3d-file" accept=".jsonl" style="display:none" onchange="tdoa3dFileSelected(this)">
      <button class="btn btn-s" onclick="tdoa3dClear()">Clear</button>
    </div>
  </div>
  <div style="flex:1;display:flex;gap:0;overflow:hidden;min-height:0">
    <div style="flex:1;display:flex;flex-direction:column;padding:.55rem;overflow:hidden;min-height:0;min-width:0">
      <canvas id="tdoa3d-canvas"
        style="flex:1;min-height:0;width:100%;background:#0c0c10;border:1px solid var(--bdr);border-radius:6px;cursor:grab;display:block;touch-action:none"
        onmousedown="tdoa3dMouseDown(event)" onmousemove="tdoa3dMouseMove(event)"
        onmouseup="tdoa3dMouseUp()" onmouseleave="tdoa3dMouseUp()"
        onwheel="tdoa3dWheel(event)"></canvas>
      <div style="flex-shrink:0;font-size:.71rem;color:var(--mu);margin-top:.35rem;text-align:center">
        Drag to orbit &nbsp;·&nbsp; Scroll to zoom &nbsp;·&nbsp;
        <span style="color:var(--acc)">●</span> Sources &nbsp;
        <span style="color:var(--cyn)">■</span> Receivers &nbsp;
        <span style="color:var(--blu)">→</span> X/East &nbsp;
        <span style="color:var(--grn)">→</span> Y/North &nbsp;
        <span style="color:var(--pur)">↑</span> Z/Up
      </div>
    </div>
    <div style="flex:0 0 320px;overflow-y:auto;border-left:1px solid var(--bdr);min-height:0">
      <table>
        <thead><tr><th>ID</th><th>Role</th><th>X&nbsp;E (m)</th><th>Y&nbsp;N (m)</th><th>Z&nbsp;U (m)</th><th>RSSI</th></tr></thead>
        <tbody id="tdoa3d-tb"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Positions -->
<div class="panel" id="panel-positions">
  <div class="scroll">
    <div class="page-hdr">
      <span class="page-hdr-title">Positioned emitters</span>
      <div class="page-hdr-actions">
        <button class="btn btn-s" onclick="renderPositions()">Refresh</button>
        <button class="btn btn-p" onclick="openSrcModal(null)" title="Manually pin a known AP position on the map">+ Pin AP</button>
      </div>
    </div>
    <div class="card" style="padding:0;overflow:hidden">
      <table>
        <thead><tr><th>ID / SSID</th><th>Method</th><th>Lat</th><th>Lon</th><th>Samples</th><th>Residual</th><th>Freq</th><th></th></tr></thead>
        <tbody id="src-tb"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Run -->
<div class="panel" id="panel-run">
  <div style="flex-shrink:0;padding:.6rem 1rem;background:var(--bg2);border-bottom:1px solid var(--bdr)">
    <div class="controls" style="margin-bottom:.4rem">
      <div class="fg" style="flex:2;min-width:200px">
        <label>Config <span class="tip" data-tip="Select a saved config. Create one in the Configs tab or run the Setup wizard.">?</span></label>
        <select id="run-config"><option value="">— select —</option></select>
      </div>
      <div class="fg">
        <label>
          Run log
          <span class="tip" data-tip="When enabled, the run is tee'd to ~/.aetherward/logs/sessions-&lt;config&gt;-&lt;timestamp&gt;.log. Useful for debugging GPS stalls, Wi-Fi adapter recovery and hidden backend errors.">?</span>
        </label>
        <label style="display:flex;align-items:center;gap:.4rem;font-size:.82rem;color:var(--txt);margin-top:.35rem">
          <input id="run-log-file" type="checkbox" checked> file
        </label>
      </div>
      <div class="fg">
        <label>
          Capture session
          <span class="tip" data-tip="Starts the full aetherward pipeline.\nWardriver mode: channel-hopping scan, writes raw frames + parsed AP metadata to session JSONL.\nMonitor mode requires the interface to already be in monitor mode\n(or run aetherward with sufficient privileges).">?</span>
        </label>
        <div style="display:flex;gap:.5rem">
          <button class="btn btn-p"  id="run-btn-start" onclick="startRun()">▶ Run</button>
          <button class="btn btn-stop" id="run-btn-stop" onclick="stopRun()" disabled>■ Stop</button>
        </div>
      </div>
    </div>
    <div style="font-size:.75rem;color:var(--mu);line-height:1.4">
      <b style="color:var(--txt)">Run</b> captures raw frames + AP metadata → session JSONL. Enable run log for ~/.aetherward/logs diagnostics. &nbsp;
      <b style="color:var(--txt)">Solve</b> reads that file and computes positions — both tabs can run simultaneously.
    </div>
  </div>
  <div style="flex:1;overflow:hidden;min-height:0;display:flex;flex-direction:column;padding:.5rem">
    <div class="log" id="run-log" style="flex:1;height:auto;min-height:0"></div>
  </div>
</div>

<!-- Solve -->
<div class="panel" id="panel-solve">
  <div style="flex-shrink:0;padding:.6rem 1rem;background:var(--bg2);border-bottom:1px solid var(--bdr)">
    <div class="controls">
      <div class="fg" style="flex:2;min-width:180px">
        <label>Session file</label>
        <select id="sl-session"><option value="">— select —</option></select>
      </div>
      <div class="fg" style="flex:2;min-width:180px">
        <label>
          Array config
          <span class="tip" data-tip="Optional. Required for TDOA.\nProvides antenna ENU positions and reference antenna ID.\nWithout it, only RSS trilateration runs.">?</span>
        </label>
        <select id="sl-config"><option value="">— none —</option></select>
      </div>
      <div class="fg">
        <label>
          n-exp
          <span class="tip" data-tip="Path loss exponent.\n2.0 = free space / open sky\n2.5 = urban outdoor (default)\n3.0–3.5 = suburban / light NLOS\n3.5–5.0 = indoor / heavy NLOS">?</span>
        </label>
        <input id="sl-nexp" type="number" value="2.5" step="0.1" min="1" max="6" style="width:72px">
      </div>
      <div class="fg">
        <label>
          Min obs
          <span class="tip" data-tip="Minimum GPS-tagged observations\nrequired before running RSS trilateration.\nFewer than 3 always falls back to RSSI centroid.">?</span>
        </label>
        <input id="sl-minobs" type="number" value="3" min="1" max="30" style="width:66px">
      </div>
      <div class="fg">
        <label>Mode</label>
        <label style="display:flex;align-items:center;gap:.35rem;font-size:.78rem;color:var(--mu);height:32px">
          <input id="sl-follow" type="checkbox" style="accent-color:var(--acc)">
          Live follow
          <span class="tip" data-tip="Off: solve a finished session once and return to idle.
On: keep watching a growing capture file until Stop is pressed.">?</span>
        </label>
      </div>
      <div class="fg">
        <label>&nbsp;</label>
        <div style="display:flex;gap:.5rem">
          <button class="btn btn-p"    id="btn-start" onclick="startSolve()">▶ Start</button>
          <button class="btn btn-stop" id="btn-stop"  onclick="stopSolve()" disabled>■ Stop</button>
        </div>
      </div>
    </div>
    <div id="sl-enu-note" style="display:none;font-size:.72rem;color:var(--mu);margin-top:.3rem"></div>
    <div id="sl-progress-wrap" style="margin-top:.45rem;display:none">
      <div style="display:flex;align-items:center;gap:.6rem">
        <div style="flex:1;height:8px;border:1px solid var(--bdr);background:var(--bg);border-radius:999px;overflow:hidden">
          <div id="sl-progress-bar" style="height:100%;width:0%;background:var(--acc);transition:width .2s linear"></div>
        </div>
        <div id="sl-progress-pct" style="width:4.5rem;text-align:right;font-family:monospace;color:var(--mu);font-size:.72rem">idle</div>
      </div>
      <div id="sl-progress-text" style="margin-top:.2rem;font-family:monospace;color:var(--mu);font-size:.72rem;white-space:nowrap;overflow:hidden;text-overflow:ellipsis">idle</div>
    </div>
    <div class="card" style="margin-top:.55rem;padding:.55rem .7rem;background:var(--bg);border-color:var(--bdr)">
      <div style="display:flex;align-items:center;justify-content:space-between;gap:.6rem;flex-wrap:wrap">
        <div>
          <b style="color:var(--txt);font-size:.82rem">Solved DB</b>
          <div style="font-size:.71rem;color:var(--mu);margin-top:.12rem">Precomputed positions, solver sample-cells, and route previews in <code>~/.aetherward/solver</code>. Loading a DB avoids rescanning huge JSONL files for map/sample lookup.</div>
        </div>
        <div style="display:flex;gap:.35rem;align-items:center;flex-wrap:wrap">
          <select id="solver-db-select" style="min-width:270px"><option value="">— select solved DB —</option></select>
          <button class="btn btn-s" onclick="loadSolverDbs()">Refresh</button>
          <button class="btn btn-s" onclick="loadSolverDbIntoMap(false)" title="Replace current solved positions with this DB">Load</button>
          <button class="btn btn-s" onclick="loadSolverDbIntoMap(true)" title="Append this DB onto the current map">Append</button>
          <button class="btn btn-s" onclick="document.getElementById('solver-db-import-input').click()">Import DB</button>
          <input type="file" id="solver-db-import-input" accept=".sqlite,.db,.awdb" style="display:none" onchange="solverDbImportSelected(this)">
        </div>
      </div>
      <div id="solver-db-note" style="font-size:.72rem;color:var(--mu);font-family:monospace;margin-top:.35rem">No solved DB loaded.</div>
    </div>
  </div>
  <div style="flex:1;overflow:hidden;min-height:0;display:flex;flex-direction:column;padding:.5rem">
    <div class="log" id="sl-log" style="flex:1;height:auto;min-height:0"></div>
  </div>
</div>

<!-- Sessions -->
<div class="panel" id="panel-sessions">
  <div class="scroll">
    <div class="page-hdr">
      <span class="page-hdr-title">Session files</span>
      <div class="page-hdr-actions">
        <button class="btn btn-s" onclick="loadSessions()">Refresh</button>
        <button class="btn btn-s" onclick="document.getElementById('sess-import-input').click()">↑ Import</button>
        <input type="file" id="sess-import-input" accept=".jsonl" multiple style="display:none" onchange="sessImportSelected(this)">
        <label title="Optional: draw underconstrained evidence centroids on the map. They are not counted in the Positions tab." style="font-size:.72rem;color:var(--mu);display:flex;align-items:center;gap:.25rem"><input type="checkbox" id="sess-include-evidence"> evidence layer</label>
        <button class="btn btn-p" id="sess-solve-all-btn" onclick="solveAllSessions()">Solve All</button>
      </div>
    </div>
    <div style="font-size:.77rem;color:var(--mu);padding:.3rem .2rem .1rem;font-family:monospace" id="sess-breadcrumb"></div>
    <div class="card" style="padding:0;overflow-x:auto">
      <table>
        <thead><tr><th>Name</th><th>Type</th><th>Size</th><th>Records</th><th>Modified</th><th></th></tr></thead>
        <tbody id="sess-tb"></tbody>
      </table>
    </div>
  </div>
</div>

<!-- Configs -->
<div class="panel" id="panel-configs">
  <div class="scroll">
    <div class="page-hdr">
      <span class="page-hdr-title">Saved configurations</span>
      <div class="page-hdr-actions">
        <button class="btn btn-s"  onclick="loadConfigs()">Refresh</button>
        <button class="btn btn-s"  onclick="openNewCfg()">+ New config</button>
        <button class="btn btn-p"  onclick="openWizard()">▶ Wizard</button>
      </div>
    </div>
    <div id="cfg-list"></div>
  </div>
</div>

<!-- Settings -->
<div class="panel" id="panel-settings">
  <div class="scroll">
    <div class="page-hdr">
      <span class="page-hdr-title">Settings &amp; system info</span>
      <button class="btn btn-s" onclick="loadStatus()">Refresh</button>
    </div>
    <div class="card">
      <div class="card-title">System</div>
      <table><tbody id="settings-sys-tb"></tbody></table>
    </div>
    <div class="card">
      <div class="card-title">
        Detected hardware
        <span class="tip" data-tip="Scanned at page load and on refresh.\nWiFi interfaces: found via iw dev.\ngpsd: connection attempt to localhost:2947.\nRTL-SDR: pyrtlsdr import check.\nPPS: /dev/pps* device check.">?</span>
      </div>
      <div id="settings-detect"><span style="color:var(--mu);font-size:.82rem">Loading…</span></div>
    </div>
  </div>
</div>

<!-- ── Config editor modal ── -->
<div class="modal" id="cfg-modal">
  <div class="modal-box">
    <div class="modal-hdr">
      <span class="modal-hdr-title" id="modal-title">Config editor</span>
      <button onclick="closeCfgModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="fg">
        <label>Config name</label>
        <input id="cfg-name" type="text" placeholder="my-config" spellcheck="false" style="font-family:monospace">
      </div>
      <div class="fg" style="flex:1">
        <label>TOML content</label>
        <textarea id="cfg-content" rows="20" spellcheck="false"
          style="font-family:'Cascadia Code','Fira Mono',monospace;font-size:.81rem;min-height:300px"></textarea>
      </div>
      <div id="cfg-err" style="color:#ff6b6b;font-size:.79rem;display:none"></div>
    </div>
    <div class="modal-ftr">
      <button class="btn btn-s" onclick="closeCfgModal()">Cancel</button>
      <button class="btn btn-p" onclick="saveCfg()">Save</button>
    </div>
  </div>
</div>

<!-- ── Source add/edit modal ── -->
<div class="modal" id="src-modal">
  <div class="modal-box sm">
    <div class="modal-hdr">
      <span class="modal-hdr-title" id="src-modal-title">Source</span>
      <button onclick="closeSrcModal()">✕</button>
    </div>
    <div class="modal-body">
      <div class="field-row">
        <div class="fg" style="flex:2">
          <label>Source ID (MAC / unique key)</label>
          <input id="src-id" placeholder="aa:bb:cc:dd:ee:ff" spellcheck="false" style="font-family:monospace">
        </div>
        <div class="fg" style="flex:1">
          <label>SSID</label>
          <input id="src-ssid" placeholder="optional">
        </div>
      </div>
      <div class="field-row">
        <div class="fg">
          <label>Latitude</label>
          <input id="src-lat" type="number" step="any" placeholder="48.856600">
        </div>
        <div class="fg">
          <label>Longitude</label>
          <input id="src-lon" type="number" step="any" placeholder="2.352200">
        </div>
        <div class="fg">
          <label>Freq (MHz)</label>
          <input id="src-freq" type="number" step="any" placeholder="2412">
        </div>
      </div>
      <div class="fg">
        <label>Position method</label>
        <select id="src-method">
          <option value="manual">Manual</option>
          <option value="rss_trilateration">RSS trilateration</option>
          <option value="rssi_centroid">RSSI centroid</option>
          <option value="tdoa">TDOA</option>
        </select>
      </div>
      <div id="src-err" style="color:#ff6b6b;font-size:.79rem;display:none"></div>
    </div>
    <div class="modal-ftr">
      <button class="btn btn-s" onclick="closeSrcModal()">Cancel</button>
      <button class="btn btn-p" onclick="saveSrc()">Save</button>
    </div>
  </div>
</div>

<!-- ── Wizard modal ── -->
<div class="modal" id="wiz-modal">
  <div class="modal-box">
    <div class="modal-hdr">
      <span class="modal-hdr-title">Setup wizard</span>
      <button onclick="closeWizard()">✕</button>
    </div>
    <div class="modal-body">
      <datalist id="det-ifaces"></datalist>
      <!-- progress dots -->
      <div class="wiz-progress" id="wiz-prog"></div>

      <!-- Step 1: Config name -->
      <div class="wiz-step active" id="wiz-s1">
        <div class="wiz-step-title">Config name</div>
        <div class="fg" style="margin-bottom:1rem">
          <label>Config / array name <span class="tip" data-tip="First prompt by design: this becomes both the saved config filename and array_id in the generated TOML. Default session filenames also derive from it.">?</span></label>
          <input id="wiz-name" value="my-config" placeholder="van-roof" style="font-family:monospace" oninput="wizNameChanged(this.value)" autofocus>
          <div id="wiz-name-err" style="color:#ff6b6b;font-size:.79rem;margin-top:.35rem;display:none">Enter a config name before continuing.</div>
        </div>
        <div style="font-size:.77rem;color:var(--mu);line-height:1.45">
          This name is written into <code>array_id</code> and used as the config filename under <code>~/.aetherward/configs/</code>.
          Pick the rig/session profile name first so every generated default stays consistent.
        </div>
      </div>

      <!-- Step 2: Operating mode + session output -->
      <div class="wiz-step" id="wiz-s2">
        <div class="wiz-step-title">Operating mode and session output</div>
        <div class="wiz-choices">
          <label class="wiz-choice sel" id="wc-wardriver" onclick="wizSetMode('wardriver')">
            <input type="radio" name="wiz-mode" value="wardriver" checked>
            <div><div class="wiz-choice-title">Wardriver</div>
            <div class="wiz-choice-desc">Channel-hopping scan with multiple antennas. Channels are assigned by antenna frequency range. Writes raw frames and parsed AP metadata to a session JSONL file. Use <b>Solve</b> afterward to compute positions.</div></div>
          </label>
          <label class="wiz-choice" id="wc-trilateration" onclick="wizSetMode('trilateration')">
            <input type="radio" name="wiz-mode" value="trilateration">
            <div><div class="wiz-choice-title">Trilateration (TDOA)</div>
            <div class="wiz-choice-desc">All antennas tune to the same frequency simultaneously. Time-difference-of-arrival between antennas gives live transmitter position. Requires ≥3 antennas with synchronised clocks (PPS/GPSDO).</div></div>
          </label>
          <label class="wiz-choice" id="wc-array_sensing" onclick="wizSetMode('array_sensing')">
            <input type="radio" name="wiz-mode" value="array_sensing">
            <div><div class="wiz-choice-title">Array sensing</div>
            <div class="wiz-choice-desc">Passive RF fingerprinting. Detects presence, motion, and absence — not position. Uses CSI (Channel State Information) if available, RSSI variance fallback for any adapter.</div></div>
          </label>
        </div>
        <div class="fg" style="margin-top:1rem">
          <label>Session output <span class="tip" data-tip="Use the default sessions path to create a fresh timestamped file on each run. Choose custom only when you want one exact file path. Choose none to disable file output.">?</span></label>
          <select id="wiz-output-policy" onchange="wizOutputPolicyChange()">
            <option value="default" selected>Use default sessions path</option>
            <option value="custom">Custom path</option>
            <option value="none">No file output</option>
          </select>
          <div id="wiz-output-hint" style="font-size:.77rem;color:var(--mu);margin-top:.35rem">Default: create a new timestamped file in ~/.aetherward/sessions/ for every run.</div>
        </div>
        <div class="fg" id="wiz-output-custom" style="display:none;margin-top:.7rem">
          <label>Custom session file path</label>
          <input id="wiz-output" value="~/.aetherward/sessions/custom.jsonl" style="font-family:monospace" oninput="W.output=this.value">
        </div>
      </div>

      <!-- Step 3: Antennas -->
      <div class="wiz-step" id="wiz-s3">
        <div class="wiz-step-title">Antenna configuration</div>
        <div id="wiz-iface-hint" style="font-size:.77rem;color:var(--mu);margin-bottom:.6rem;display:none">
          Detected interfaces: <span id="wiz-iface-list"></span>
        </div>
        <div class="controls" style="margin-bottom:.75rem">
          <div class="fg">
            <label>Number of antennas <span class="tip" data-tip="Wardriver: one per channel slice.\nTDOA/array sensing: minimum 3 for a 2-D fix.">?</span></label>
            <input id="wiz-ant-count" type="number" value="1" min="1" max="8" style="width:66px" oninput="wizRenderAnts()">
          </div>
        </div>
        <div id="wiz-ants"></div>
      </div>

      <!-- Step 4: GPS -->
      <div class="wiz-step" id="wiz-s4">
        <div class="wiz-step-title">Position source (GPS)</div>
        <div class="fg" style="margin-bottom:.75rem">
          <label>Backend <span class="tip" data-tip="gpsd: hardware GNSS via gpsd daemon (recommended)\ngeoclue: system location service (D-Bus)\nmls: Mozilla Location Service — WiFi scan triangulation, no GPS hardware needed\nstatic: fixed lat/lon for stationary deployments\nnone: no GPS — frames will not be geo-tagged">?</span></label>
          <select id="wiz-gps" onchange="wizGpsChange()">
            <option value="gpsd">gpsd — hardware GNSS (recommended)</option>
            <option value="geoclue">geoclue — system location service</option>
            <option value="mls">mls — Mozilla Location Service (WiFi scan)</option>
            <option value="static">static — fixed coordinates</option>
            <option value="none">none — no GPS tagging</option>
          </select>
        </div>
        <div id="wiz-gps-static" style="display:none">
          <div class="field-row">
            <div class="fg"><label>Latitude</label><input id="wiz-lat" type="number" step="any" placeholder="48.856600"></div>
            <div class="fg"><label>Longitude</label><input id="wiz-lon" type="number" step="any" placeholder="2.352200"></div>
            <div class="fg"><label>Altitude (m)</label><input id="wiz-alt" type="number" step="any" value="0"></div>
          </div>
        </div>
      </div>

      <!-- Step 5: Time sync -->
      <div class="wiz-step" id="wiz-s5">
        <div class="wiz-step-title">Time synchronisation</div>
        <div class="wiz-choices">
          <label class="wiz-choice sel" id="wts-software" onclick="wizSetSync('software')">
            <input type="radio" name="wiz-sync" value="software" checked>
            <div><div class="wiz-choice-title">Software / NTP</div>
            <div class="wiz-choice-desc">~1 ms jitter → ~300 km TDOA error. <b>Use only for wardriver mode</b> — TDOA is unusable at this precision.</div></div>
          </label>
          <label class="wiz-choice" id="wts-ntp" onclick="wizSetSync('ntp')">
            <input type="radio" name="wiz-sync" value="ntp">
            <div><div class="wiz-choice-title">Disciplined NTP (LAN)</div>
            <div class="wiz-choice-desc">~100 µs jitter. Marginally usable for TDOA at short baselines.</div></div>
          </label>
          <label class="wiz-choice" id="wts-pps" onclick="wizSetSync('pps')">
            <input type="radio" name="wiz-sync" value="pps">
            <div><div class="wiz-choice-title">PPS (pulse-per-second from GNSS)</div>
            <div class="wiz-choice-desc">~100 ns jitter. Good TDOA accuracy. Requires PPS-capable GNSS receiver + pps-tools.</div></div>
          </label>
          <label class="wiz-choice" id="wts-gpsdo" onclick="wizSetSync('gpsdo')">
            <input type="radio" name="wiz-sync" value="gpsdo">
            <div><div class="wiz-choice-title">GPSDO (GPS-disciplined oscillator)</div>
            <div class="wiz-choice-desc">1–10 ns jitter. Best TDOA accuracy. Professional hardware required.</div></div>
          </label>
        </div>
        <div id="wiz-sync-dev-wrap" style="display:none;margin-top:.7rem">
          <div class="fg"><label>PPS device path</label><input id="wiz-sync-dev" placeholder="/dev/pps0" style="font-family:monospace"></div>
        </div>
      </div>

      <!-- Step 6: Advanced settings (mode-specific) -->
      <div class="wiz-step" id="wiz-s6">
        <div class="wiz-step-title">Advanced settings</div>

        <!-- Wardriver-specific -->
        <div id="wiz-adv-wardriver">
          <div class="field-row">
            <div class="fg" style="flex:2;min-width:200px">
              <label>WiFi channels to scan <span class="tip" data-tip="Comma-separated channel numbers to hop across.\n2.4 GHz: 1-13\n5 GHz: 36,40,44,48,52...177\nChannels are split automatically across antennas.">?</span></label>
              <input id="wiz-channels" value="1,2,3,4,5,6,7,8,9,10,11,12,13" style="font-family:monospace" oninput="W.channels=this.value">
            </div>
            <div class="fg">
              <label>Hop interval (s) <span class="tip" data-tip="Dwell time per channel.\n0.1 s = fast wardriving (default)\n0.5-1.0 s = deeper capture per channel\nTotal scan time = hop_interval × num_channels">?</span></label>
              <input id="wiz-hop" type="number" step="0.01" min="0.05" value="0.1" style="width:90px" oninput="W.hopInterval=+this.value">
            </div>
          </div>
          <div style="font-size:.77rem;color:var(--mu);margin-top:.7rem">
            Session output is selected on step 2. The default creates a fresh timestamped JSONL under ~/.aetherward/sessions/ on each run.
          </div>
        </div>

        <!-- Trilateration-specific -->
        <div id="wiz-adv-trilateration" style="display:none">
          <div class="field-row">
            <div class="fg">
              <label>Channel <span class="tip" data-tip="WiFi channel all antennas tune to simultaneously.\nAll antennas must be on the same channel for TDOA.">?</span></label>
              <input id="wiz-tri-channel" type="number" value="6" min="1" max="177" style="width:80px" oninput="W.triChannel=+this.value">
            </div>
            <div class="fg">
              <label>Correlation window (s) <span class="tip" data-tip="Max expected time difference between the earliest and latest arrival across all antennas for the same frame.\n~1 ms default (=300 km light travel, safely covers any real baseline).">?</span></label>
              <input id="wiz-corr-window" type="number" step="0.0001" min="0.0001" value="0.001" style="width:110px" oninput="W.corrWindow=+this.value">
            </div>
            <div class="fg">
              <label>Group timeout (s) <span class="tip" data-tip="Discard incomplete groups (fewer antennas than required) after this interval.\nSet to 5-10× the correlation window.\nDefault: 0.05 s">?</span></label>
              <input id="wiz-group-timeout" type="number" step="0.001" min="0.001" value="0.05" style="width:110px" oninput="W.groupTimeout=+this.value">
            </div>
          </div>
        </div>

        <!-- Array sensing-specific -->
        <div id="wiz-adv-array-sensing" style="display:none">
          <div class="field-row">
            <div class="fg">
              <label>Channel <span class="tip" data-tip="WiFi channel to monitor for CSI/RSSI variance.\nPick a channel with existing traffic (e.g. 1, 6, 11).">?</span></label>
              <input id="wiz-sense-channel" type="number" value="6" min="1" max="177" style="width:80px" oninput="W.senseChannel=+this.value">
            </div>
            <div class="fg">
              <label>History window <span class="tip" data-tip="Rolling buffer depth per antenna (frames).\nLarger = more stable baseline, slower to adapt.">?</span></label>
              <input id="wiz-history-len" type="number" value="100" min="10" style="width:90px" oninput="W.historyLen=+this.value">
            </div>
            <div class="fg">
              <label>Calibration frames <span class="tip" data-tip="Frames collected during quiet-environment baseline.\nNo events fire until calibration is complete.\nDefault 50 ≈ 5 s at 10 fps.">?</span></label>
              <input id="wiz-calib-frames" type="number" value="50" min="10" style="width:90px" oninput="W.calibFrames=+this.value">
            </div>
          </div>
          <div class="field-row" style="margin-top:.55rem">
            <div class="fg">
              <label>Sensitivity <span class="tip" data-tip="Minimum variance increase above baseline to trigger presence.\nLower = more sensitive, more false positives.\n0.05 = 5% variance increase (default).">?</span></label>
              <input id="wiz-sensitivity" type="number" step="0.01" min="0.001" value="0.05" style="width:90px" oninput="W.sensitivity=+this.value">
            </div>
            <div class="fg">
              <label>Hysteresis <span class="tip" data-tip="Fraction of sensitivity to fall below before firing absence.\nPrevents rapid on/off toggling.\n0.4 = return below 40% of threshold (default).">?</span></label>
              <input id="wiz-hysteresis" type="number" step="0.05" min="0.01" max="1" value="0.4" style="width:90px" oninput="W.hysteresis=+this.value">
            </div>
            <div class="fg">
              <label>EMA alpha <span class="tip" data-tip="Exponential moving average weight (0-1).\nHigher = faster response, more noise.\n0.3 = balanced smoothing (default).">?</span></label>
              <input id="wiz-ema-alpha" type="number" step="0.05" min="0.01" max="1" value="0.3" style="width:90px" oninput="W.emaAlpha=+this.value">
            </div>
          </div>
        </div>
      </div>

      <!-- Step 7: Review -->
      <div class="wiz-step" id="wiz-s7">
        <div class="wiz-step-title">Review &amp; save</div>
        <div style="font-size:.77rem;color:var(--mu);margin-bottom:.75rem">
          Config / array name is the first wizard prompt and is written as <code>array_id</code>. Go back to change it, or edit the TOML manually before saving.
        </div>
        <div class="fg">
          <label>Generated TOML <span class="tip" data-tip="You can edit this before saving.\nThe file will be written to ~/.aetherward/configs/<name>.toml">?</span></label>
          <textarea id="wiz-toml" rows="16" style="font-family:'Cascadia Code','Fira Mono',monospace;font-size:.79rem"></textarea>
        </div>
        <div id="wiz-err" style="color:#ff6b6b;font-size:.79rem;display:none"></div>
      </div>
    </div>
    <div class="modal-ftr" style="justify-content:space-between">
      <button class="btn btn-s" id="wiz-prev" onclick="wizPrev()" disabled>← Back</button>
      <span id="wiz-step-lbl" style="font-size:.8rem;color:var(--mu);align-self:center">Step 1 of 7</span>
      <div style="display:flex;gap:.5rem">
        <button class="btn btn-s" onclick="closeWizard()">Cancel</button>
        <button class="btn btn-p" id="wiz-next" onclick="wizNext()">Next →</button>
        <button class="btn btn-p" id="wiz-save" onclick="wizSave()" style="display:none">Save config</button>
      </div>
    </div>
  </div>
</div>

<div id="enu-hover"></div>

"""
