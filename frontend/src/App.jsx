import { useState, useRef, useEffect, useMemo } from "react";
import mapboxgl from "mapbox-gl";
import "./index.css";

// ─── Constants ────────────────────────────────────────────────────────────────

const PLAN_STEPS = [
  { id: "upload",      label: "Route" },
  { id: "match",       label: "Match" },
  { id: "dates",       label: "Dates" },
  { id: "itinerary",   label: "Itinerary" },
  { id: "checks",      label: "Checks" },
  { id: "report",      label: "Plan" },
  { id: "finalreport", label: "Report" },
];

// ─── Utilities ────────────────────────────────────────────────────────────────

function classNames(...classes) {
  return classes.filter(Boolean).join(" ");
}

function haversineMiles(lat1, lng1, lat2, lng2) {
  const R = 3958.8;
  const dLat = ((lat2 - lat1) * Math.PI) / 180;
  const dLng = ((lng2 - lng1) * Math.PI) / 180;
  const a =
    Math.sin(dLat / 2) * Math.sin(dLat / 2) +
    Math.cos((lat1 * Math.PI) / 180) *
      Math.cos((lat2 * Math.PI) / 180) *
      Math.sin(dLng / 2) *
      Math.sin(dLng / 2);
  return R * 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
}

function computeRisk(checks) {
  let status = "go";
  const reasons = [];
  for (const obs of checks.aqi?.observations || []) {
    if (obs.aqi >= 150) { status = "no-go"; reasons.push(`AQI ${obs.aqi} (${obs.category})`); }
    else if (obs.aqi >= 100 && status !== "no-go") { status = "caution"; reasons.push(`AQI ${obs.aqi} (${obs.category})`); }
  }
  // Only flag caution for fires within 5 miles and updated within the last year
  const nearbyRecentFires = (checks.fire?.perimeters?.features || []).filter(f => {
    const d = f.properties?.distance_from_midpoint_mi;
    const days = f.properties?.days_since_update;
    return (d == null || d <= 5) && (days == null || days <= 365);
  });
  if (nearbyRecentFires.length) {
    if (status === "go") status = "caution";
    reasons.push(`${nearbyRecentFires.length} fire perimeter(s) within 5 mi (past year)`);
  }
  const snowDepth = checks.snow?.max_depth_in;
  if (snowDepth >= 12 && status !== "no-go") { status = "caution"; reasons.push(`Snow depth ~${snowDepth} in`); }
  return { status, reasons };
}

function buildReport(trail, risk) {
  const bullets = [`Status: ${risk.status.toUpperCase()}`];
  if (trail) bullets.push(`Route: ${trail.name} in ${trail.area}`);
  for (const r of risk.reasons) bullets.push(r);
  return { format: "bullets", bullets };
}

function toLineFeature(layer) {
  if (!layer) return null;
  if (layer.type === "Feature" && layer.geometry?.type === "LineString") return layer;
  if (layer.type === "LineString") return { type: "Feature", geometry: layer, properties: {} };
  return null;
}

function routeToFeature(routeData) {
  const coords = routeData?.points?.map((p) => [p.lng, p.lat]).filter((c) => c.length === 2) || [];
  if (coords.length < 2) return null;
  return { type: "Feature", geometry: { type: "LineString", coordinates: coords }, properties: {} };
}

function getFireCount(fp) { return fp?.features?.length || 0; }

function addDays(dateStr, n) {
  if (!dateStr || n < 1) return "";
  const d = new Date(dateStr + "T12:00:00");
  d.setDate(d.getDate() + n - 1);
  return d.toISOString().split("T")[0];
}

function getPointAtMile(pts, targetMile) {
  if (!pts?.length) return null;
  let dist = 0;
  for (let i = 1; i < pts.length; i++) {
    const seg = haversineMiles(pts[i-1].lat, pts[i-1].lng, pts[i].lat, pts[i].lng);
    if (dist + seg >= targetMile) {
      const t = seg > 0 ? (targetMile - dist) / seg : 0;
      return { lat: pts[i-1].lat + t*(pts[i].lat - pts[i-1].lat), lng: pts[i-1].lng + t*(pts[i].lng - pts[i-1].lng) };
    }
    dist += seg;
  }
  return pts[pts.length - 1];
}

function interpolateRoutePoints(pts, n = 10) {
  if (!pts?.length) return [];
  if (pts.length >= n) {
    const step = Math.max(1, Math.floor(pts.length / n));
    return pts.filter((_, i) => i % step === 0).slice(0, n);
  }
  // Fewer source points than requested — interpolate between them
  const result = [];
  let totalLen = 0;
  for (let i = 1; i < pts.length; i++) totalLen += haversineMiles(pts[i-1].lat, pts[i-1].lng, pts[i].lat, pts[i].lng);
  if (totalLen === 0) return pts;
  const step = totalLen / (n - 1);
  let walked = 0;
  let seg = 0;
  for (let s = 0; s < n; s++) {
    const target = s * step;
    while (seg < pts.length - 2 && walked + haversineMiles(pts[seg].lat, pts[seg].lng, pts[seg+1].lat, pts[seg+1].lng) < target) {
      walked += haversineMiles(pts[seg].lat, pts[seg].lng, pts[seg+1].lat, pts[seg+1].lng);
      seg++;
    }
    const segLen = haversineMiles(pts[seg].lat, pts[seg].lng, pts[seg+1]?.lat ?? pts[seg].lat, pts[seg+1]?.lng ?? pts[seg].lng);
    const t = segLen > 0 ? Math.min(1, (target - walked) / segLen) : 0;
    const next = pts[seg+1] || pts[seg];
    result.push({ lat: pts[seg].lat + t * (next.lat - pts[seg].lat), lng: pts[seg].lng + t * (next.lng - pts[seg].lng) });
  }
  return result;
}

// Project a dragged lat/lng onto the nearest point on the route polyline.
// Only snaps if within maxRadiusMi miles to avoid hard-locking far drags.
function snapToTrail(pts, lat, lng, maxRadiusMi = 0.4) {
  if (!pts?.length) return { lat, lng };
  let bestDist = Infinity;
  let bestLat = lat, bestLng = lng;
  for (let i = 1; i < pts.length; i++) {
    const ax = pts[i-1].lng, ay = pts[i-1].lat;
    const bx = pts[i].lng,   by = pts[i].lat;
    const dx = bx - ax,      dy = by - ay;
    const lenSq = dx*dx + dy*dy;
    let t = lenSq > 0 ? ((lng - ax)*dx + (lat - ay)*dy) / lenSq : 0;
    t = Math.max(0, Math.min(1, t));
    const projLng = ax + t*dx;
    const projLat = ay + t*dy;
    const d = haversineMiles(lat, lng, projLat, projLng);
    if (d < bestDist) { bestDist = d; bestLat = projLat; bestLng = projLng; }
  }
  return bestDist <= maxRadiusMi ? { lat: bestLat, lng: bestLng } : { lat, lng };
}

function getDaytimePeriods(forecast) {
  return (forecast || []).filter(p => !/night|tonight/i.test(p.name || ""));
}

function checkStatus(key, data) {
  if (!data) return null;
  if (data.error) return { label: "Error", cls: "bg-red-50 text-red-700" };
  if (key === "weather") {
    const text = (data.forecast || []).slice(0, 3).map(f => f.short || "").join(" ").toLowerCase();
    if (/thunder|severe/.test(text)) return { label: "Warning", cls: "bg-amber-50 text-amber-700" };
    if (/shower|rain|drizzle|snow/.test(text)) return { label: "Caution", cls: "bg-yellow-50 text-yellow-700" };
    return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
  }
  if (key === "aqi") {
    const max = Math.max(0, ...(data.observations || []).map(o => o.aqi || 0));
    if (max >= 150) return { label: "Warning", cls: "bg-red-50 text-red-700" };
    if (max >= 100) return { label: "Caution", cls: "bg-amber-50 text-amber-700" };
    return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
  }
  if (key === "fire") {
    const nearby = (data.perimeters?.features || []).filter(f => {
      const d = f.properties?.distance_from_midpoint_mi;
      const days = f.properties?.days_since_update;
      return (d == null || d <= 5) && (days == null || days <= 365);
    });
    if (nearby.length > 0) return { label: `Caution (${nearby.length} nearby)`, cls: "bg-amber-50 text-amber-700" };
    const total = data.perimeters?.features?.length || 0;
    if (total > 0) return { label: `${total} (distant/old)`, cls: "bg-yellow-50 text-yellow-700" };
    return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
  }
  if (key === "snow") {
    const depth = data.max_depth_in ?? 0;
    if (depth >= 12) return { label: "Warning", cls: "bg-blue-50 text-blue-700" };
    if (depth > 0) return { label: "Caution", cls: "bg-sky-50 text-sky-700" };
    return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
  }
  if (key === "water") {
    if ((data.count ?? 1) === 0) return { label: "Warning", cls: "bg-amber-50 text-amber-700" };
    return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
  }
  return { label: "Good", cls: "bg-emerald-50 text-emerald-700" };
}

// ─── Small shared components ──────────────────────────────────────────────────

function Spinner({ size = 4 }) {
  return (
    <svg className={`h-${size} w-${size} animate-spin text-slate-400`} viewBox="0 0 24 24" fill="none">
      <circle className="opacity-25" cx="12" cy="12" r="10" stroke="currentColor" strokeWidth="4" />
      <path className="opacity-75" fill="currentColor" d="M4 12a8 8 0 018-8v4a4 4 0 00-4 4H4z" />
    </svg>
  );
}

function BackBtn({ label = "← Dashboard", onClick }) {
  return (
    <button onClick={onClick} className="text-xs uppercase tracking-[0.3em] text-slate-500 hover:text-emerald-600 transition">
      {label}
    </button>
  );
}

// ─── Report helpers ───────────────────────────────────────────────────────────

function findNearestRouteIdx(pts, lat, lng) {
  let best = 0, bestD = Infinity;
  for (let i = 0; i < pts.length; i++) {
    const d = haversineMiles(lat, lng, pts[i].lat, pts[i].lng);
    if (d < bestD) { bestD = d; best = i; }
  }
  return best;
}

function computeSegmentElevation(segPts) {
  let gain = 0, drop = 0;
  for (let i = 1; i < segPts.length; i++) {
    if (segPts[i].ele == null || segPts[i - 1].ele == null) continue;
    const dElev = (segPts[i].ele - segPts[i - 1].ele) * 3.28084;
    if (dElev > 0) gain += dElev; else drop += -dElev;
  }
  return { gainFt: Math.round(gain), dropFt: Math.round(drop) };
}

function MiniElevProfile({ pts }) {
  if (!pts?.length) return <p className="text-xs text-slate-400">No data</p>;
  const elPts = pts.filter(p => p.ele != null).map(p => p.ele * 3.28084);
  if (elPts.length < 2) return <p className="text-xs text-slate-400">No elevation data</p>;
  const W = 300, H = 44;
  const padL = 2, padR = 2, padT = 3, padB = 3;
  const chartW = W - padL - padR, chartH = H - padT - padB;
  const minE = Math.min(...elPts), maxE = Math.max(...elPts);
  const range = maxE - minE || 1;
  const yS = (e) => padT + chartH - ((e - minE) / range) * chartH;
  const step = Math.max(1, Math.floor(elPts.length / 80));
  const sampled = elPts.filter((_, i) => i % step === 0);
  const linePoints = sampled.map((e, i) => `${(padL + (i / (sampled.length - 1)) * chartW).toFixed(1)},${yS(e).toFixed(1)}`).join(" L ");
  const areaPath = `M ${padL},${padT + chartH} L ${linePoints} L ${padL + chartW},${padT + chartH} Z`;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} style={{ width: "100%", height: H }} preserveAspectRatio="none">
      <defs>
        <linearGradient id="day-eg" x1="0" y1="0" x2="0" y2="1">
          <stop offset="0%" stopColor="#10b981" stopOpacity="0.3" />
          <stop offset="100%" stopColor="#10b981" stopOpacity="0.04" />
        </linearGradient>
      </defs>
      <path d={areaPath} fill="url(#day-eg)" />
      <path d={`M ${linePoints}`} fill="none" stroke="#059669" strokeWidth="1.8" strokeLinejoin="round" />
    </svg>
  );
}

// ─── Export utilities ─────────────────────────────────────────────────────────

function downloadFile(content, filename, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const a = document.createElement("a");
  a.href = url; a.download = filename; a.click();
  URL.revokeObjectURL(url);
}

function buildGpx(routePoints, campPositions, itineraryDays, userWaterSpots, name) {
  const wpts = [];
  (itineraryDays || []).slice(0, -1).forEach(day => {
    const p = campPositions?.[day.day];
    if (!p) return;
    wpts.push(`  <wpt lat="${p.lat.toFixed(7)}" lon="${p.lng.toFixed(7)}"><name>Camp Day ${day.day}</name><sym>Campsite</sym></wpt>`);
  });
  (userWaterSpots || []).forEach(s => {
    wpts.push(`  <wpt lat="${s.lat.toFixed(7)}" lon="${s.lng.toFixed(7)}"><name>${s.name || "Water source"}</name><sym>Water</sym></wpt>`);
  });
  const trkpts = (routePoints || []).map(p =>
    `      <trkpt lat="${p.lat.toFixed(7)}" lon="${p.lng.toFixed(7)}">${p.ele != null ? `<ele>${p.ele.toFixed(1)}</ele>` : ""}</trkpt>`
  ).join("\n");
  return `<?xml version="1.0" encoding="UTF-8"?>\n<gpx version="1.1" creator="Backcountry Trip Planner" xmlns="http://www.topografix.com/GPX/1/1">\n  <metadata><name>${name}</name></metadata>\n${wpts.join("\n")}\n  <trk><name>${name}</name><trkseg>\n${trkpts}\n  </trkseg></trk>\n</gpx>`;
}

function buildKml(routePoints, campPositions, itineraryDays, userWaterSpots, name) {
  const marks = [];
  const coords = (routePoints || []).map(p => `${p.lng.toFixed(7)},${p.lat.toFixed(7)},${(p.ele || 0).toFixed(1)}`).join(" ");
  marks.push(`    <Placemark><name>${name} — Route</name><Style><LineStyle><color>ff0f766e</color><width>3</width></LineStyle></Style><LineString><tessellate>1</tessellate><coordinates>${coords}</coordinates></LineString></Placemark>`);
  (itineraryDays || []).slice(0, -1).forEach(day => {
    const p = campPositions?.[day.day];
    if (!p) return;
    marks.push(`    <Placemark><name>Camp Day ${day.day}</name><Style><IconStyle><Icon><href>http://maps.google.com/mapfiles/kml/shapes/campsite.png</href></Icon></IconStyle></Style><Point><coordinates>${p.lng.toFixed(7)},${p.lat.toFixed(7)},0</coordinates></Point></Placemark>`);
  });
  (userWaterSpots || []).forEach(s => {
    marks.push(`    <Placemark><name>${s.name || "Water source"}</name><Style><IconStyle><color>ffebb230</color><Icon><href>http://maps.google.com/mapfiles/kml/shapes/water.png</href></Icon></IconStyle></Style><Point><coordinates>${s.lng.toFixed(7)},${s.lat.toFixed(7)},0</coordinates></Point></Placemark>`);
  });
  return `<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">\n  <Document>\n    <name>${name}</name>\n${marks.join("\n")}\n  </Document>\n</kml>`;
}

function buildGeoJson(routePoints, campPositions, itineraryDays, userWaterSpots, name) {
  const features = [];
  if (routePoints?.length) {
    features.push({ type: "Feature", properties: { name, type: "route" },
      geometry: { type: "LineString", coordinates: routePoints.map(p => p.ele != null ? [p.lng, p.lat, p.ele] : [p.lng, p.lat]) } });
  }
  (itineraryDays || []).slice(0, -1).forEach(day => {
    const p = campPositions?.[day.day];
    if (!p) return;
    features.push({ type: "Feature", properties: { name: `Camp Day ${day.day}`, type: "camp", day: day.day },
      geometry: { type: "Point", coordinates: [p.lng, p.lat] } });
  });
  (userWaterSpots || []).forEach(s => {
    features.push({ type: "Feature", properties: { name: s.name || "Water source", type: "water" },
      geometry: { type: "Point", coordinates: [s.lng, s.lat] } });
  });
  return JSON.stringify({ type: "FeatureCollection", features }, null, 2);
}

// ─── 2D Report Map ─────────────────────────────────────────────────────────────

function ReportMap2D({ routeFeature, firePerimeters, snowGeojson, waterGeojson, fallbackCenter, campPositions, itineraryDays, userWaterSpots }) {
  const containerRef = useRef(null);
  const mapRef = useRef(null);
  const campMarkersRef = useRef([]);
  const waterMarkersRef = useRef([]);
  const [layerVis, setLayerVis] = useState({ fire: true, snow: true, water: true, camps: true, userWater: true });
  const [capturing, setCapturing] = useState(false);
  const [scaleInfo, setScaleInfo] = useState(null);
  const [coverageProvider, setCoverageProvider] = useState(null);

  // Refs so marker effect doesn't stale-close over props
  const campPositionsRef = useRef(campPositions);
  const itineraryDaysRef = useRef(itineraryDays);
  const userWaterSpotsRef = useRef(userWaterSpots);
  useEffect(() => { campPositionsRef.current = campPositions; }, [campPositions]);
  useEffect(() => { itineraryDaysRef.current = itineraryDays; }, [itineraryDays]);
  useEffect(() => { userWaterSpotsRef.current = userWaterSpots; }, [userWaterSpots]);

  // Map init
  useEffect(() => {
    if (!containerRef.current) return;
    const token = import.meta.env.VITE_MAPBOX_TOKEN;
    if (!token) return;
    mapboxgl.accessToken = token;
    const routeCoords = routeFeature?.geometry?.coordinates || [];
    const center = routeCoords[0] || fallbackCenter || [-120.1287, 38.8649];
    const map = new mapboxgl.Map({
      container: containerRef.current,
      style: "mapbox://styles/mapbox/outdoors-v12",
      center, zoom: 9, pitch: 0, bearing: 0,
      preserveDrawingBuffer: true,
    });
    mapRef.current = map;

    // Scale bar
    const updateScale = () => {
      const zoom = map.getZoom();
      const lat = map.getCenter().lat;
      const mPerPx = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, zoom);
      const targetM = mPerPx * 120;
      const exp = Math.floor(Math.log10(targetM));
      const nice = [1, 2, 5].map(n => n * Math.pow(10, exp)).find(c => c >= targetM) || Math.pow(10, exp + 1);
      const barPx = Math.round(nice / mPerPx);
      const mi = nice / 1609.34;
      const label = mi >= 0.2 ? `${mi % 1 === 0 ? mi : mi.toFixed(1)} mi` : `${Math.round(nice)} m`;
      setScaleInfo({ barPx: Math.min(barPx, 200), label });
    };
    map.on("load", updateScale);
    map.on("move", updateScale);

    map.on("load", () => {
      if (routeCoords.length >= 2) {
        map.addSource("route", { type: "geojson", data: routeFeature });
        map.addLayer({ id: "route-glow", type: "line", source: "route", paint: { "line-color": "#14b8a6", "line-width": 8, "line-opacity": 0.25, "line-blur": 6 } });
        map.addLayer({ id: "route-line", type: "line", source: "route", paint: { "line-color": "#0f766e", "line-width": 3 } });
        const b = routeCoords.reduce((b, c) => b.extend(c), new mapboxgl.LngLatBounds(routeCoords[0], routeCoords[0]));
        map.fitBounds(b, { padding: 60, duration: 0 });
      }
      if (firePerimeters?.features?.length) {
        map.addSource("fire", { type: "geojson", data: firePerimeters });
        map.addLayer({ id: "fire-fill", type: "fill", source: "fire", paint: { "fill-color": "#f97316", "fill-opacity": 0.22 } });
        map.addLayer({ id: "fire-out",  type: "line", source: "fire", paint: { "line-color": "#ea580c", "line-width": 1.5 } });
      }
      if (snowGeojson?.features?.length) {
        map.addSource("snow", { type: "geojson", data: snowGeojson });
        map.addLayer({ id: "snow-pt", type: "circle", source: "snow", paint: { "circle-radius": 8, "circle-color": "#bfdbfe", "circle-stroke-color": "#3b82f6", "circle-stroke-width": 2 } });
      }
      if (waterGeojson?.features?.length) {
        map.addSource("water-osm", { type: "geojson", data: waterGeojson });
        map.addLayer({ id: "water-pt", type: "circle", source: "water-osm", paint: { "circle-radius": 7, "circle-color": "#22d3ee", "circle-stroke-color": "#0369a1", "circle-stroke-width": 1.5 } });
      }

      // Camp markers
      campMarkersRef.current.forEach(m => m.remove());
      campMarkersRef.current = [];
      (itineraryDaysRef.current || []).slice(0, -1).forEach(day => {
        const pos = campPositionsRef.current?.[day.day];
        if (!pos) return;
        const el = document.createElement("div");
        el.style.cssText = "width:22px;height:22px;background:#10b981;border:2.5px solid white;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:10px;font-weight:700;color:white;box-shadow:0 2px 6px rgba(0,0,0,0.3);";
        el.textContent = day.day;
        campMarkersRef.current.push(new mapboxgl.Marker({ element: el }).setLngLat([pos.lng, pos.lat]).addTo(map));
      });

      // User water spots
      waterMarkersRef.current.forEach(m => m.remove());
      waterMarkersRef.current = [];
      (userWaterSpotsRef.current || []).forEach(spot => {
        const el = document.createElement("div");
        el.style.cssText = "width:26px;height:26px;background:#0891b2;border:2px solid white;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;box-shadow:0 2px 6px rgba(0,0,0,0.3);";
        el.textContent = "💧";
        waterMarkersRef.current.push(new mapboxgl.Marker({ element: el }).setLngLat([spot.lng, spot.lat]).addTo(map));
      });
    });

    return () => {
      campMarkersRef.current.forEach(m => m.remove());
      waterMarkersRef.current.forEach(m => m.remove());
      map.remove(); mapRef.current = null;
    };
  }, []);

  // Coverage layer for ReportMap2D
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (map.getLayer("coverage-layer")) map.removeLayer("coverage-layer");
      if (map.getSource("coverage-tiles")) map.removeSource("coverage-tiles");
      if (!coverageProvider) return;
      map.addSource("coverage-tiles", {
        type: "raster",
        tiles: [`/api/proxy/coverage/${coverageProvider}/{z}/{x}/{y}`],
        tileSize: 256,
      });
      map.addLayer({ id: "coverage-layer", type: "raster", source: "coverage-tiles", paint: { "raster-opacity": 0.55 } });
    };
    if (map.isStyleLoaded()) apply(); else map.once("load", apply);
  }, [coverageProvider]);

  const toggleLayer = (ids, visible) => {
    const map = mapRef.current;
    if (!map) return;
    ids.forEach(id => { if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", visible ? "visible" : "none"); });
  };

  const toggleMarkers = (markersRef, visible) => {
    markersRef.current.forEach(m => { m.getElement().style.display = visible ? "" : "none"; });
  };

  const handleExportPng = async () => {
    const map = mapRef.current;
    if (!map) return;
    setCapturing(true);
    await new Promise(r => setTimeout(r, 200));
    try {
      const url = map.getCanvas().toDataURL("image/png");
      const a = document.createElement("a");
      a.href = url; a.download = "trail-map.png"; a.click();
    } finally { setCapturing(false); }
  };

  const LAYER_DEFS = [
    { key: "fire",      ids: ["fire-fill", "fire-out"], label: "Fire",      color: "#ea580c" },
    { key: "snow",      ids: ["snow-pt"],               label: "Snow",      color: "#3b82f6" },
    { key: "water",     ids: ["water-pt"],              label: "Water",     color: "#0891b2" },
    { key: "camps",     ids: [],                        label: "Camps",     color: "#059669", markers: campMarkersRef },
    { key: "userWater", ids: [],                        label: "My Water",  color: "#0891b2", markers: waterMarkersRef },
  ];

  return (
    <div className="rounded-2xl overflow-hidden border border-slate-200 shadow-sm">
      {/* Layer toggles */}
      <div className="flex items-center gap-3 px-4 py-2.5 bg-white border-b border-slate-100 flex-wrap">
        <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">Layers</span>
        {LAYER_DEFS.map(({ key, ids, label, color, markers }) => (
          <label key={key} className="flex items-center gap-1.5 cursor-pointer select-none">
            <input type="checkbox" checked={layerVis[key]} onChange={e => {
              setLayerVis(p => ({ ...p, [key]: e.target.checked }));
              if (ids.length) toggleLayer(ids, e.target.checked);
              if (markers) toggleMarkers(markers, e.target.checked);
            }} className="rounded accent-emerald-600 w-3.5 h-3.5" />
            <span className="text-xs font-semibold" style={{ color }}>{label}</span>
          </label>
        ))}
        <div className="flex items-center gap-1 ml-1 pl-2 border-l border-slate-100">
          <span className="text-[10px] font-semibold text-slate-400 mr-0.5">Cell</span>
          {["tmobile", "att", "verizon"].map(p => (
            <button key={p}
              onClick={() => setCoverageProvider(prev => prev === p ? null : p)}
              className={`rounded-full px-1.5 py-0.5 text-[10px] font-bold transition-colors ${
                coverageProvider === p
                  ? p === "tmobile" ? "bg-pink-600 text-white"
                    : p === "att"     ? "bg-blue-600 text-white"
                    : "bg-red-600 text-white"
                  : "bg-slate-100 text-slate-500 hover:bg-slate-200"
              }`}>
              {p === "tmobile" ? "T‑Mo" : p === "att" ? "AT&T" : "Vz"}
            </button>
          ))}
        </div>
        <button onClick={handleExportPng} disabled={capturing}
          className="ml-auto rounded-full bg-slate-800 px-3 py-1.5 text-[11px] font-semibold text-white hover:bg-slate-700 transition disabled:opacity-50">
          {capturing ? "Capturing…" : "↓ PNG"}
        </button>
      </div>

      {/* Map container */}
      <div className="relative">
        <div ref={containerRef} style={{ height: 380 }} className="w-full" />

        {/* Scale bar — spans bottom of map */}
        {scaleInfo && (
          <div className="absolute bottom-7 left-1/2 -translate-x-1/2 flex flex-col items-center gap-0.5 pointer-events-none z-10">
            <div className="relative" style={{ width: scaleInfo.barPx }}>
              <div className="absolute left-0 bottom-0 h-3 w-0.5 bg-slate-800" />
              <div className="absolute right-0 bottom-0 h-3 w-0.5 bg-slate-800" />
              <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-slate-800" />
              {/* Half-way tick */}
              <div className="absolute bottom-0 h-2 w-0.5 bg-slate-800" style={{ left: "50%" }} />
            </div>
            <span className="text-[10px] font-semibold text-slate-800 bg-white/90 px-1.5 py-0.5 rounded shadow-sm border border-slate-200/70">
              {scaleInfo.label}
            </span>
          </div>
        )}
      </div>
    </div>
  );
}

// ─── Elevation Profile ────────────────────────────────────────────────────────

function ElevationProfile({ route, onHoverMile }) {
  const [hoverInfo, setHoverInfo] = useState(null); // { pct, mile, elev }

  if (!route?.points?.length) return null;
  const pts = route.points.filter((p) => p.ele != null);
  if (pts.length < 2) return null;

  const data = [];
  let cumDist = 0;
  for (let i = 0; i < pts.length; i++) {
    if (i > 0) cumDist += haversineMiles(pts[i - 1].lat, pts[i - 1].lng, pts[i].lat, pts[i].lng);
    data.push({ dist: cumDist, elev: pts[i].ele * 3.28084 });
  }

  const totalMi = data[data.length - 1].dist;
  const elevs = data.map((d) => d.elev);
  const minElev = Math.min(...elevs);
  const maxElev = Math.max(...elevs);
  const elevRange = maxElev - minElev || 1;

  const W = 420; const H = 68;
  const padL = 4; const padR = 4; const padT = 6; const padB = 4;
  const chartW = W - padL - padR; const chartH = H - padT - padB;

  const xScale = (d) => padL + (d / totalMi) * chartW;
  const yScale = (e) => padT + chartH - ((e - minElev) / elevRange) * chartH;

  const linePoints = data.map((d) => `${xScale(d.dist).toFixed(1)},${yScale(d.elev).toFixed(1)}`).join(" L ");
  const areaPath = `M ${xScale(0)},${(padT + chartH).toFixed(1)} L ${linePoints} L ${xScale(totalMi)},${(padT + chartH).toFixed(1)} Z`;

  const getElevAtDist = (targetDist) => {
    for (let i = 1; i < data.length; i++) {
      if (data[i].dist >= targetDist) {
        const t = (data[i].dist - data[i - 1].dist) > 0
          ? (targetDist - data[i - 1].dist) / (data[i].dist - data[i - 1].dist)
          : 0;
        return data[i - 1].elev + t * (data[i].elev - data[i - 1].elev);
      }
    }
    return data[data.length - 1].elev;
  };

  const handleMouseMove = (e) => {
    const rect = e.currentTarget.getBoundingClientRect();
    const pct = Math.max(0, Math.min(1, (e.clientX - rect.left) / rect.width));
    const mile = pct * totalMi;
    const elev = getElevAtDist(mile);
    setHoverInfo({ pct, mile, elev });
    if (onHoverMile) onHoverMile(mile);
  };

  const handleMouseLeave = () => {
    setHoverInfo(null);
    if (onHoverMile) onHoverMile(null);
  };

  const hoverSvgX = hoverInfo !== null ? padL + hoverInfo.pct * chartW : null;

  return (
    <div
      className="pointer-events-auto absolute bottom-2 left-1/2 -translate-x-1/2 w-[440px] rounded-3xl border border-white/30 bg-white/90 p-4 shadow-2xl backdrop-blur z-10"
      style={{ cursor: "crosshair" }}
      onMouseMove={handleMouseMove}
      onMouseLeave={handleMouseLeave}
    >
      <p className="mb-2 text-[10px] uppercase tracking-[0.35em] text-slate-500">Elevation Profile</p>
      <div className="relative">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: H }} preserveAspectRatio="none">
          <defs>
            <linearGradient id="elev-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity="0.35" />
              <stop offset="100%" stopColor="#10b981" stopOpacity="0.05" />
            </linearGradient>
          </defs>
          <path d={areaPath} fill="url(#elev-grad)" />
          <path d={`M ${linePoints}`} fill="none" stroke="#059669" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
          {hoverSvgX !== null && (
            <>
              <line x1={hoverSvgX} y1={padT} x2={hoverSvgX} y2={padT + chartH} stroke="#10b981" strokeWidth="1" strokeDasharray="3,2" opacity="0.75" />
              <circle cx={hoverSvgX} cy={yScale(hoverInfo.elev)} r="3.5" fill="#10b981" stroke="white" strokeWidth="1.5" />
            </>
          )}
        </svg>
        <span className="absolute left-1 top-0 text-[9px] font-medium leading-none text-slate-500">{Math.round(maxElev).toLocaleString()} ft</span>
        <span className="absolute bottom-0 left-1 text-[9px] font-medium leading-none text-slate-500">{Math.round(minElev).toLocaleString()} ft</span>
        {hoverInfo && (
          <div
            className="absolute pointer-events-none rounded px-1.5 py-0.5 text-[9px] font-semibold text-white"
            style={{
              top: 0,
              left: `${Math.max(2, Math.min(78, hoverInfo.pct * 100 - 8))}%`,
              background: "rgba(15,23,42,0.88)",
              transform: "translateY(-120%)",
              whiteSpace: "nowrap",
            }}
          >
            {Math.round(hoverInfo.elev).toLocaleString()} ft · {hoverInfo.mile.toFixed(1)} mi
          </div>
        )}
      </div>
      <div className="mt-0.5 flex justify-between text-[9px] uppercase tracking-[0.2em] text-slate-400">
        <span>0 mi</span>
        <span className="text-slate-500 font-medium normal-case tracking-normal text-[9px]">+{Math.round(route.elev_gain_ft ?? 0).toLocaleString()} ft gain</span>
        <span>{totalMi.toFixed(1)} mi</span>
      </div>
    </div>
  );
}

// ─── Trail Search Input (shared between UploadStep and ExploreView) ───────────

function TrailSearchInput({ trailName, setTrailName, onNameSearch, onSuggestionSelect }) {
  const [suggestions, setSuggestions] = useState([]);
  const [suggestLoading, setSuggestLoading] = useState(false);
  const [showDropdown, setShowDropdown] = useState(false);
  const [activeIdx, setActiveIdx] = useState(-1);
  const debounceRef = useRef(null);
  const dropdownRef = useRef(null);

  const handleInput = (e) => {
    const val = e.target.value;
    setTrailName(val);
    setActiveIdx(-1);
    if (debounceRef.current) clearTimeout(debounceRef.current);
    if (!val.trim() || val.trim().length < 2) { setSuggestions([]); setShowDropdown(false); return; }
    debounceRef.current = setTimeout(async () => {
      setSuggestLoading(true);
      try {
        const res = await fetch(`/api/trail/suggest?q=${encodeURIComponent(val.trim())}`);
        const data = await res.json();
        setSuggestions(Array.isArray(data) ? data : []);
        setShowDropdown(Array.isArray(data) && data.length > 0);
      } catch { setSuggestions([]); setShowDropdown(false); }
      finally { setSuggestLoading(false); }
    }, 300);
  };

  const pick = (trail) => { setSuggestions([]); setShowDropdown(false); setActiveIdx(-1); if (onSuggestionSelect) onSuggestionSelect(trail); };

  const handleKeyDown = (e) => {
    if (!showDropdown || !suggestions.length) return;
    if (e.key === "ArrowDown") { e.preventDefault(); setActiveIdx((p) => Math.min(p + 1, suggestions.length - 1)); }
    else if (e.key === "ArrowUp") { e.preventDefault(); setActiveIdx((p) => Math.max(p - 1, -1)); }
    else if (e.key === "Enter" && activeIdx >= 0) { e.preventDefault(); pick(suggestions[activeIdx]); }
    else if (e.key === "Escape") { setShowDropdown(false); }
  };

  useEffect(() => {
    const handler = (e) => { if (dropdownRef.current && !dropdownRef.current.contains(e.target)) setShowDropdown(false); };
    document.addEventListener("mousedown", handler);
    return () => document.removeEventListener("mousedown", handler);
  }, []);

  return (
    <div className="space-y-3">
      <div className="relative" ref={dropdownRef}>
        <div className="relative">
          <input type="text" value={trailName} onChange={handleInput} onKeyDown={handleKeyDown}
            onFocus={() => suggestions.length > 0 && setShowDropdown(true)}
            placeholder="e.g., Aloha Lake, Desolation Wilderness"
            autoComplete="off"
            className="w-full rounded-2xl border border-slate-200 px-4 py-3 text-sm pr-10 focus:border-emerald-400 focus:outline-none focus:ring-2 focus:ring-emerald-200"
          />
          {suggestLoading && <span className="absolute right-3 top-1/2 -translate-y-1/2"><Spinner /></span>}
        </div>
        {showDropdown && suggestions.length > 0 && (
          <ul className="absolute left-0 right-0 top-full z-50 mt-1 overflow-hidden rounded-2xl border border-slate-200 bg-white shadow-xl shadow-slate-200/60">
            {suggestions.map((trail, idx) => (
              <li key={trail.id} onMouseDown={(e) => { e.preventDefault(); pick(trail); }} onMouseEnter={() => setActiveIdx(idx)}
                className={`flex cursor-pointer items-start gap-3 px-4 py-3 transition ${idx === activeIdx ? "bg-emerald-50" : "hover:bg-slate-50"} ${idx < suggestions.length - 1 ? "border-b border-slate-100" : ""}`}>
                <span className="mt-0.5 flex h-5 w-5 shrink-0 items-center justify-center rounded-full bg-emerald-100 text-[10px] font-bold text-emerald-700">{idx + 1}</span>
                <span className="flex-1 min-w-0">
                  <span className="block truncate text-sm font-semibold text-slate-900">{trail.name}</span>
                  <span className="block truncate text-xs text-slate-500">{trail.area}{trail.length_miles ? ` · ${trail.length_miles} mi` : ""}</span>
                </span>
              </li>
            ))}
          </ul>
        )}
      </div>
      <button onClick={onNameSearch} disabled={!trailName.trim()}
        className="w-full rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        Search Trails
      </button>
    </div>
  );
}

// ─── Plan mode step components ────────────────────────────────────────────────

function UploadStep({ selectedFile, fileInputRef, onFileChange, onUpload, inputMode, setInputMode, trailName, setTrailName, onNameSearch, onSuggestionSelect }) {
  const isGpx = inputMode === "gpx";
  return (
    <div className="space-y-8">
      <div className="flex gap-4 p-1 bg-slate-100 rounded-xl w-fit">
        <button onClick={() => setInputMode("name")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${!isGpx ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Name + Region</button>
        <button onClick={() => setInputMode("gpx")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${isGpx ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Upload GPX</button>
      </div>
      <div className="rounded-[32px] border border-emerald-100 bg-gradient-to-br from-white via-white to-emerald-50/40 p-10 shadow-lg shadow-emerald-200/30">
        {isGpx ? (
          <>
            <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">GPX Upload</p>
            <h3 className="mt-3 text-2xl font-semibold text-slate-900">Drop your GPX file here</h3>
            <p className="mt-2 text-sm text-slate-600">We&apos;ll compute distance, elevation gain, and key coordinates along your route.</p>
            <div className="mt-8 rounded-3xl border border-dashed border-emerald-300 bg-white/80 px-6 py-10 text-center">
              <input ref={fileInputRef} type="file" accept=".gpx" onChange={onFileChange} className="hidden" id="gpx-upload" />
              <label htmlFor="gpx-upload" className="cursor-pointer block text-sm text-slate-500 mb-4">
                {selectedFile ? selectedFile.name : "Drag & drop GPX or click to upload"}
              </label>
              <button onClick={onUpload} disabled={!selectedFile}
                className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
                Parse Route
              </button>
            </div>
          </>
        ) : (
          <>
            <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">Trail Search</p>
            <h3 className="mt-3 text-2xl font-semibold text-slate-900">Search by name</h3>
            <p className="mt-2 text-sm text-slate-600">Start typing to see matching trails, or hit Search to browse all results.</p>
            <div className="mt-8">
              <TrailSearchInput trailName={trailName} setTrailName={setTrailName} onNameSearch={onNameSearch} onSuggestionSelect={onSuggestionSelect} />
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DatesStep({ startDate, onStartDateChange, endDate, onNext }) {
  return (
    <div className="space-y-8">
      <div className="rounded-2xl bg-white p-6 shadow-sm max-w-sm">
        <label className="text-xs uppercase tracking-[0.2em] text-slate-500">Start date</label>
        <input type="date" className="mt-4 w-full rounded-2xl border border-slate-200 px-4 py-3 text-sm" value={startDate} onChange={(e) => onStartDateChange(e.target.value)} />
      </div>
      {startDate && endDate && (
        <div className="rounded-2xl border border-slate-100 bg-slate-50 px-5 py-4 flex gap-8 text-sm max-w-sm">
          <div><p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">Return date</p><p className="mt-1 font-semibold text-slate-900">{endDate}</p></div>
        </div>
      )}
      <div className="rounded-2xl border border-emerald-100 bg-emerald-50 p-5 text-sm text-emerald-800 max-w-sm">Trips must begin within 10 days for the most accurate NOAA forecasts.</div>
      <button onClick={onNext} disabled={!startDate}
        className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        Continue to itinerary
      </button>
    </div>
  );
}

function MatchStep({ route, trailMatch, selectedTrailId, onTrailSelect, onNext, onUseCustomGpx, hasCustomGpx, loading }) {
  const autoSelected = trailMatch?.auto_selected;
  const shortlist = trailMatch?.shortlist || [];

  return (
    <div className="space-y-8">
      {autoSelected && (
        <div className="rounded-2xl bg-white p-6 shadow-sm">
          <p className="text-sm text-slate-600">Best trail match:</p>
          <h3 className="mt-3 text-2xl font-semibold text-slate-900">{autoSelected.name}</h3>
          <p className="mt-2 text-slate-500">{autoSelected.area} · {autoSelected.length_miles} mi</p>
          <p className="mt-1 text-xs text-slate-400">Confidence: {trailMatch?.confidence}</p>
          <div className="mt-5 flex flex-wrap gap-3">
            <button onClick={() => { onTrailSelect(autoSelected.id); onNext(); }}
              className="rounded-full bg-emerald-600 px-5 py-2 text-sm font-semibold text-white">
              Yes, use this trail
            </button>
          </div>
        </div>
      )}
      {shortlist.length > 1 && (
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500 mb-4">Other candidates</p>
          <div className="grid gap-4 md:grid-cols-3">
            {shortlist.filter(t => t.id !== autoSelected?.id).map((trail) => (
              <div key={trail.id} onClick={() => onTrailSelect(trail.id)}
                className={`cursor-pointer rounded-2xl border p-5 transition ${selectedTrailId === trail.id ? "border-emerald-500 bg-emerald-50" : "border-slate-200 bg-white hover:border-emerald-300"}`}>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Candidate</p>
                <h4 className="mt-3 text-lg font-semibold text-slate-900">{trail.name}</h4>
                <p className="text-slate-500">{trail.area} · {trail.length_miles} mi</p>
              </div>
            ))}
          </div>
        </div>
      )}
      {hasCustomGpx && (
        <div className="rounded-2xl border border-slate-200 bg-white p-5">
          <p className="text-sm font-semibold text-slate-900">Custom GPX route</p>
          <p className="mt-1 text-sm text-slate-500">Skip trail matching and use your exact uploaded GPX file as the route.</p>
          <button onClick={onUseCustomGpx} className="mt-4 rounded-full border border-slate-300 px-5 py-2 text-sm font-semibold text-slate-700 hover:bg-slate-50 transition">
            Use this exact GPX
          </button>
        </div>
      )}
      <button onClick={onNext} disabled={loading}
        className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        {loading ? "Matching…" : "Continue with selected trail"}
      </button>
    </div>
  );
}

function ItineraryStep({ route, selectedTrail, startDate, numDays, setNumDays, tripType, setTripType, onNext }) {
  const rawMiles = parseFloat(route?.length_miles || route?.distance_mi || selectedTrail?.length_miles || 0);
  const effectiveMiles = tripType === "out-and-back" ? rawMiles * 2 : rawMiles;
  const milesPerDay = numDays > 0 && effectiveMiles > 0 ? effectiveMiles / numDays : 0;

  const days = Array.from({ length: numDays }, (_, i) => ({
    day: i + 1,
    startMile: +(i * milesPerDay).toFixed(1),
    endMile: +((i + 1) * milesPerDay).toFixed(1),
    miles: +milesPerDay.toFixed(1),
  }));

  const btnBase = "rounded-full px-5 py-2 text-sm font-semibold transition";
  const btnActive = "bg-emerald-600 text-white";
  const btnInactive = "border border-slate-200 text-slate-600 hover:bg-slate-50";

  return (
    <div className="space-y-8">
      <div className="rounded-[32px] border border-emerald-100 bg-gradient-to-br from-white via-white to-emerald-50/40 p-8 shadow-lg shadow-emerald-200/30 space-y-7">
        {/* Route type */}
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500 mb-3">Route type</p>
          <div className="flex gap-3">
            <button onClick={() => setTripType("loop")} className={`${btnBase} ${tripType === "loop" ? btnActive : btnInactive}`}>Loop</button>
            <button onClick={() => setTripType("out-and-back")} className={`${btnBase} ${tripType === "out-and-back" ? btnActive : btnInactive}`}>Out &amp; Back</button>
          </div>
          {tripType === "out-and-back" && rawMiles > 0 && (
            <p className="mt-2 text-xs text-slate-500">{rawMiles.toFixed(1)} mi one way → {(rawMiles * 2).toFixed(1)} mi round trip</p>
          )}
        </div>

        {/* Number of days */}
        <div>
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500 mb-3">Trip length</p>
          <div className="flex items-center gap-5">
            <button onClick={() => setNumDays((d) => Math.max(1, d - 1))}
              className="h-10 w-10 rounded-full border border-slate-200 text-xl font-semibold text-slate-600 hover:bg-slate-50 transition flex items-center justify-center">−</button>
            <span className="text-3xl font-bold text-slate-900 w-10 text-center">{numDays}</span>
            <button onClick={() => setNumDays((d) => d + 1)}
              className="h-10 w-10 rounded-full border border-slate-200 text-xl font-semibold text-slate-600 hover:bg-slate-50 transition flex items-center justify-center">+</button>
            <span className="text-sm text-slate-500">{numDays === 1 ? "day" : "days"}</span>
          </div>
        </div>

        {/* Summary bar */}
        {effectiveMiles > 0 && milesPerDay > 0 && (
          <div className="rounded-2xl bg-slate-50 px-5 py-4 flex gap-8 text-sm">
            <div><p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">Total</p><p className="mt-1 font-semibold text-slate-900">{effectiveMiles.toFixed(1)} mi</p></div>
            <div><p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">Per day</p><p className="mt-1 font-semibold text-slate-900">~{milesPerDay.toFixed(1)} mi</p></div>
            <div><p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">Days</p><p className="mt-1 font-semibold text-slate-900">{numDays}</p></div>
            {startDate && <div><p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">Start</p><p className="mt-1 font-semibold text-slate-900">{startDate}</p></div>}
          </div>
        )}
      </div>

      {/* Day breakdown */}
      {days.length > 0 && (
        <div className="space-y-3">
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Daily segments (equidistant)</p>
          {days.map((day) => (
            <div key={day.day} className="rounded-2xl bg-white p-5 shadow-sm flex items-center justify-between">
              <div>
                <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Day {day.day}</p>
                <p className="mt-1 font-semibold text-slate-900">Mile {day.startMile} → {day.endMile}</p>
                <p className="text-xs text-slate-400 mt-0.5">Camp at mile {day.endMile}</p>
              </div>
              <span className="rounded-full bg-emerald-50 px-3 py-1 text-sm font-semibold text-emerald-700">{day.miles} mi</span>
            </div>
          ))}
        </div>
      )}

      <p className="text-xs text-slate-400">Camp markers are draggable on the report map. An AI situation brief will generate automatically once checks complete.</p>

      <button onClick={onNext}
        className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700">
        Continue to pre-trip checks
      </button>
    </div>
  );
}

function ChecksStep({ checks, checksLoading, onNext }) {
  const anyLoading = checksLoading && Object.values(checksLoading).some(Boolean);
  const items = [
    { key: "weather", label: "Weather" },
    { key: "aqi",     label: "AQI" },
    { key: "fire",    label: "Fire" },
    { key: "snow",    label: "Snow" },
    { key: "water",   label: "Water" },
  ];

  const getSummary = (label, data) => {
    if (!data) return null;
    if (data.error) return data.error;
    if (label === "Weather" && data.forecast?.length) {
      const n = data.forecast[0];
      return `${n.short} · ${n.temp} ${n.temp_unit} · ${n.wind}`;
    }
    if (label === "AQI" && data.observations?.length) {
      const o = data.observations[0];
      return `${o.parameter} AQI ${o.aqi} (${o.category})`;
    }
    if (label === "Fire" && data.perimeters?.features) return `${data.perimeters.features.length} fire perimeters loaded`;
    if (label === "Snow" && data.message) return `${data.message}${data.max_depth_in != null ? ` Max: ${data.max_depth_in} in` : ""}`;
    if (label === "Water" && data.message) return data.message;
    return null;
  };

  return (
    <div className="space-y-8">
      <div className="grid gap-4 md:grid-cols-2">
        {items.map(({ key, label }) => {
          const isLoading = checksLoading?.[key];
          const data = checks?.[key];
          const summary = getSummary(label, data);
          const st = isLoading ? null : checkStatus(key, data);
          return (
            <div key={key} className="rounded-2xl bg-white p-6 shadow-sm">
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-slate-900">{label}</p>
                {isLoading ? (
                  <span className="flex items-center gap-1.5 rounded-full bg-slate-100 px-3 py-1 text-xs font-semibold text-slate-500"><Spinner /> Fetching</span>
                ) : st ? (
                  <span className={`rounded-full px-3 py-1 text-xs font-semibold ${st.cls}`}>{st.label}</span>
                ) : null}
              </div>
              <p className="mt-3 text-sm text-slate-600">{isLoading ? "" : (summary ?? "—")}</p>
            </div>
          );
        })}
      </div>
      <button onClick={onNext} disabled={anyLoading}
        className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        {anyLoading ? "Running checks…" : "View trip report"}
      </button>
    </div>
  );
}

// ─── Map components ───────────────────────────────────────────────────────────

function MapCanvas({ routeFeature, firePerimeters, snowGeojson, waterGeojson, fallbackCenter, report, checks, selectedTrail, route, exploreMode = false, itineraryDays, routePoints, tripType, rawMiles, userWaterSpots, onAddWaterSpot, onRemoveWaterSpot, addWaterMode, setAddWaterMode, heightClass, onCampPositionsChange, campPositions }) {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const campMarkersRef = useRef([]);
  const userWaterMarkersRef = useRef([]);
  const hoverMarkerRef = useRef(null);
  const fireCount = getFireCount(firePerimeters);
  const styleUrl = "mapbox://styles/mapbox/outdoors-v12";

  // Local UI state
  const [is3D, setIs3D] = useState(true);
  const [showHelp, setShowHelp] = useState(false);
  const [scaleInfo, setScaleInfo] = useState(null);
  const [hoveredMile, setHoveredMile] = useState(null);
  const [coverageProvider, setCoverageProvider] = useState(null); // null = off

  // Ref so remove-button closures inside marker DOM don't go stale
  const onRemoveWaterSpotRef = useRef(onRemoveWaterSpot);
  useEffect(() => { onRemoveWaterSpotRef.current = onRemoveWaterSpot; }, [onRemoveWaterSpot]);

  useEffect(() => {
    if (!mapContainerRef.current) return;
    const token = import.meta.env.VITE_MAPBOX_TOKEN;
    if (!token) return;
    if (mapRef.current) {
      mapRef.current.remove();
      mapRef.current = null;
    }
    mapboxgl.accessToken = token;

    const routeCoords = routeFeature?.geometry?.coordinates || [];
    const center = routeCoords[0] || fallbackCenter || [-120.1287, 38.8649];

    const map = new mapboxgl.Map({
      container: mapContainerRef.current,
      style: styleUrl,
      center, zoom: 9, pitch: 45, bearing: -12,
    });
    mapRef.current = map;

    map.on("load", () => {
      map.addSource("mapbox-dem", { type: "raster-dem", url: "mapbox://mapbox.terrain-rgb", tileSize: 512, maxzoom: 14 });
      map.setTerrain({ source: "mapbox-dem", exaggeration: 1.2 });
      map.addLayer({ id: "sky", type: "sky", paint: { "sky-type": "atmosphere", "sky-atmosphere-sun": [0.0, 0.0], "sky-atmosphere-sun-intensity": 15 } });

      if (routeCoords.length) {
        const bounds = routeCoords.reduce((b, c) => b.extend(c), new mapboxgl.LngLatBounds(routeCoords[0], routeCoords[0]));
        map.fitBounds(bounds, { padding: 80, duration: 900 });
        map.addSource("route", { type: "geojson", data: routeFeature });
        map.addLayer({ id: "route-glow", type: "line", source: "route", paint: { "line-color": "#14b8a6", "line-width": 8, "line-opacity": 0.3, "line-blur": 8 } });
        map.addLayer({ id: "route-line", type: "line", source: "route", paint: { "line-color": "#0f766e", "line-width": 3, "line-opacity": 0.95 } });
      } else {
        const fc = fallbackCenter || [-120.1287, 38.8649];
        map.addSource("trail-point", { type: "geojson", data: { type: "Feature", geometry: { type: "Point", coordinates: fc }, properties: {} } });
        map.addLayer({ id: "trail-point-circle", type: "circle", source: "trail-point", paint: { "circle-radius": 10, "circle-color": "#10b981", "circle-opacity": 0.8, "circle-stroke-color": "#065f46", "circle-stroke-width": 2 } });
        map.flyTo({ center: fc, zoom: 11 });
      }

      if (!exploreMode && firePerimeters?.features?.length) {
        map.addSource("fire", { type: "geojson", data: firePerimeters });
        map.addLayer({ id: "fire-fill", type: "fill", source: "fire", paint: { "fill-color": ["case", ["==", ["get", "recency_tag"], "active"], "#ef4444", ["==", ["get", "recency_tag"], "recent"], "#f97316", "#fbbf24"], "fill-opacity": 0.18 } });
        map.addLayer({ id: "fire-outline", type: "line", source: "fire", paint: { "line-color": ["case", ["==", ["get", "recency_tag"], "active"], "#dc2626", "#f97316"], "line-width": 1.5 } });
        map.addLayer({ id: "fire-label", type: "symbol", source: "fire", layout: { "text-field": ["coalesce", ["get", "IncidentName"], ["get", "poly_IncidentName"], "Fire"], "text-size": 11, "text-anchor": "center" }, paint: { "text-color": "#7c2d12", "text-halo-color": "#fff", "text-halo-width": 2 } });
        map.on("click", "fire-fill", (e) => {
          const p = e.features[0].properties;
          const name = p.IncidentName || p.poly_IncidentName || "Fire perimeter";
          new mapboxgl.Popup().setLngLat(e.lngLat).setHTML(`<div style="color:#1e293b;font-size:12px;"><strong>${name}</strong><br/><span style="color:#92400e;">${p.recency_tag || ""}</span>${p.days_since_update != null ? " · " + p.days_since_update + " days ago" : ""}</div>`).addTo(map);
        });
        if (routeCoords.length) { map.moveLayer("route-glow"); map.moveLayer("route-line"); }
      }

      if (!exploreMode && snowGeojson) {
        map.addSource("snow", { type: "geojson", data: snowGeojson });
        map.addLayer({ id: "snow-point", type: "circle", source: "snow", paint: { "circle-radius": 10, "circle-color": "#bfdbfe", "circle-opacity": 0.85, "circle-stroke-color": "#3b82f6", "circle-stroke-width": 2 } });
        map.addLayer({ id: "snow-label", type: "symbol", source: "snow", layout: { "text-field": ["concat", ["to-string", ["get", "max_depth_in"]], " in snow"], "text-size": 12, "text-offset": [0, 1.8], "text-anchor": "top" }, paint: { "text-color": "#1e3a8a", "text-halo-color": "#eff6ff", "text-halo-width": 2 } });
      }

      if (!exploreMode && waterGeojson?.features?.length) {
        map.addSource("water", { type: "geojson", data: waterGeojson });
        map.addLayer({ id: "water-point", type: "circle", source: "water", paint: {
          "circle-radius": 8,
          "circle-color": ["match", ["get", "water_type"], "spring", "#34d399", "lake", "#38bdf8", "pond", "#38bdf8", "reservoir", "#38bdf8", "river", "#0ea5e9", "stream", "#0ea5e9", "#22d3ee"],
          "circle-opacity": 0.9,
          "circle-stroke-color": "#0369a1",
          "circle-stroke-width": 1.5,
        }});
        map.addLayer({ id: "water-label", type: "symbol", source: "water", layout: { "text-field": ["get", "name"], "text-size": 11, "text-offset": [0, 1.6], "text-anchor": "top" }, paint: { "text-color": "#0c4a6e", "text-halo-color": "#f0f9ff", "text-halo-width": 2 } });
        map.on("click", "water-point", (e) => {
          const p = e.features[0].properties;
          new mapboxgl.Popup().setLngLat(e.lngLat).setHTML(`<div style="color:#1e293b;font-size:12px;"><strong>${p.name}</strong><br/><span style="color:#0369a1;">${p.water_type}</span> · ${p.distance_mi} mi from trail</div>`).addTo(map);
        });
      }

    });

    return () => { map.remove(); mapRef.current = null; };
  }, [styleUrl]);

  // Adaptive scale bar — updates on every map move/zoom
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const update = () => {
      const zoom = map.getZoom();
      const lat = map.getCenter().lat;
      const metersPerPx = 156543.03392 * Math.cos(lat * Math.PI / 180) / Math.pow(2, zoom);
      const targetM = metersPerPx * 90;
      const exp = Math.floor(Math.log10(targetM));
      const nice = [1, 2, 5].map(n => n * Math.pow(10, exp)).find(c => c >= targetM) || Math.pow(10, exp + 1);
      const barPx = Math.round(nice / metersPerPx);
      const ft = nice * 3.28084;
      const mi = nice / 1609.34;
      let label;
      if (mi >= 0.2)      label = `${mi % 1 === 0 ? mi : mi.toFixed(mi < 2 ? 1 : 0)} mi`;
      else if (ft < 1000) label = `${Math.round(ft)} ft`;
      else                label = `${nice >= 1000 ? (nice/1000).toFixed(nice % 1000 === 0 ? 0 : 1) + " km" : nice + " m"}`;
      setScaleInfo({ barPx: Math.min(barPx, 220), label });
    };
    map.on("load", update);
    map.on("move", update);
    if (map.isStyleLoaded()) update();
    return () => { map.off("load", update); map.off("move", update); };
  }, []);

  // Water source click handler — re-registered whenever addWaterMode changes so it
  // always closes over the current values. Cleanup removes the old listener each time.
  useEffect(() => {
    const map = mapRef.current;
    if (!map || exploreMode) return;
    const handleClick = (e) => {
      if (!addWaterMode || !onAddWaterSpot) return;
      if (e.originalEvent?.target?.closest?.(".mapboxgl-popup")) return;
      onAddWaterSpot({ lat: e.lngLat.lat, lng: e.lngLat.lng, name: "Water source" });
      setAddWaterMode?.(false);
    };
    map.on("click", handleClick);
    return () => map.off("click", handleClick);
  }, [addWaterMode, onAddWaterSpot, exploreMode, setAddWaterMode]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;

    const apply = () => {
      if (routeFeature) {
        const coords = routeFeature.geometry?.coordinates || [];
        if (map.getSource("route")) {
          map.getSource("route").setData(routeFeature);
        } else if (coords.length >= 2) {
          // Route arrived after map loaded with no geometry — swap fallback point for route line
          if (map.getLayer("trail-point-circle")) map.removeLayer("trail-point-circle");
          if (map.getSource("trail-point")) map.removeSource("trail-point");
          map.addSource("route", { type: "geojson", data: routeFeature });
          map.addLayer({ id: "route-glow", type: "line", source: "route", paint: { "line-color": "#14b8a6", "line-width": 8, "line-opacity": 0.3, "line-blur": 8 } });
          map.addLayer({ id: "route-line", type: "line", source: "route", paint: { "line-color": "#0f766e", "line-width": 3, "line-opacity": 0.95 } });
          const bounds = coords.reduce((b, c) => b.extend(c), new mapboxgl.LngLatBounds(coords[0], coords[0]));
          map.fitBounds(bounds, { padding: 80, duration: 900 });
        }
      }
      if (map.getSource("fire") && firePerimeters) map.getSource("fire").setData(firePerimeters);
      if (map.getSource("snow") && snowGeojson) map.getSource("snow").setData(snowGeojson);
      if (map.getSource("water") && waterGeojson) map.getSource("water").setData(waterGeojson);
    };

    if (map.isStyleLoaded()) apply(); else map.once("load", apply);
  }, [routeFeature, firePerimeters, snowGeojson, waterGeojson]);

  // Draggable camp markers
  const onCampPositionsChangeRef = useRef(onCampPositionsChange);
  useEffect(() => { onCampPositionsChangeRef.current = onCampPositionsChange; }, [onCampPositionsChange]);
  // Keep latest campPositions in a ref so the marker effect can read stored positions
  // without listing campPositions as a dependency (which would cause re-init loops).
  const campPositionsRef = useRef(campPositions);
  useEffect(() => { campPositionsRef.current = campPositions; }, [campPositions]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map || !itineraryDays?.length || !routePoints?.length) return;
    campMarkersRef.current.forEach(m => m.remove());
    campMarkersRef.current = [];
    const camps = itineraryDays.slice(0, -1);
    let onewayLen = rawMiles || 0;
    if (tripType === "out-and-back" && routePoints.length > 1) {
      let d = 0;
      for (let i = 1; i < routePoints.length; i++)
        d += haversineMiles(routePoints[i-1].lat, routePoints[i-1].lng, routePoints[i].lat, routePoints[i].lng);
      onewayLen = d;
    }
    const addMarkers = () => {
      // positions is a mutable object shared across all dragend closures for this
      // marker batch so each drag updates the full set before calling the parent.
      const positions = {};
      camps.forEach((day) => {
        // Prefer a previously-dragged position; fall back to equidistant trail point.
        const stored = campPositionsRef.current?.[day.day];
        let initPt;
        if (stored) {
          initPt = stored;
        } else {
          let targetMile = day.endMile;
          if (tripType === "out-and-back" && onewayLen > 0 && targetMile > onewayLen) {
            targetMile = 2 * onewayLen - targetMile;
          }
          initPt = getPointAtMile(routePoints, Math.max(0, targetMile));
        }
        if (!initPt) return;
        positions[day.day] = { lat: initPt.lat, lng: initPt.lng };

        const el = document.createElement("div");
        el.style.cssText = "width:26px;height:26px;background:#10b981;border:2.5px solid #065f46;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:white;cursor:grab;box-shadow:0 2px 8px rgba(0,0,0,0.3);user-select:none;";
        el.textContent = day.day;
        const popup = new mapboxgl.Popup({ offset: 20, closeButton: false }).setHTML(
          `<div style="font-size:12px;color:#1e293b;"><strong>Camp — Day ${day.day}</strong><br/>Mile ${day.endMile} · ${day.miles} mi today<br/><em style="color:#64748b;">Drag to reposition along trail</em></div>`
        );
        const marker = new mapboxgl.Marker({ element: el, draggable: true }).setLngLat([initPt.lng, initPt.lat]).setPopup(popup).addTo(map);
        el.addEventListener("mouseenter", () => marker.togglePopup());
        el.addEventListener("mouseleave", () => { if (marker.getPopup().isOpen()) marker.togglePopup(); });
        marker.on("dragend", () => {
          const ll = marker.getLngLat();
          // Snap back to nearest trail point within 0.4 mi
          const snapped = snapToTrail(routePoints, ll.lat, ll.lng);
          marker.setLngLat([snapped.lng, snapped.lat]);
          positions[day.day] = { lat: snapped.lat, lng: snapped.lng };
          if (onCampPositionsChangeRef.current) onCampPositionsChangeRef.current({ ...positions });
        });
        campMarkersRef.current.push(marker);
      });
      if (onCampPositionsChangeRef.current) onCampPositionsChangeRef.current({ ...positions });
    };
    // Camp markers are HTML overlays — no need to gate on isStyleLoaded.
    addMarkers();
    return () => { campMarkersRef.current.forEach(m => m.remove()); campMarkersRef.current = []; };
  }, [itineraryDays, routePoints]);

  // User-added water spots — Mapbox HTML markers live in an overlay div above the
  // canvas and do not require the style to be loaded, so we add them immediately.
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    userWaterMarkersRef.current.forEach(m => m.remove());
    userWaterMarkersRef.current = [];
    (userWaterSpots || []).forEach((spot, idx) => {
      // Wrapper keeps the × button absolutely positioned without distorting the circle
      const wrapper = document.createElement("div");
      wrapper.style.cssText = "position:relative;width:28px;height:28px;";

      const el = document.createElement("div");
      el.style.cssText = "width:28px;height:28px;background:#0891b2;border:2px solid #0e7490;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:14px;cursor:pointer;box-shadow:0 1px 5px rgba(0,0,0,0.3);";
      el.textContent = "💧";

      // Remove button — appears on hover via CSS class toggle
      const removeBtn = document.createElement("button");
      removeBtn.style.cssText = "position:absolute;top:-5px;right:-5px;width:16px;height:16px;background:#ef4444;border:1.5px solid #fff;border-radius:50%;color:white;font-size:11px;font-weight:700;line-height:1;cursor:pointer;display:none;align-items:center;justify-content:center;padding:0;z-index:1;";
      removeBtn.textContent = "×";
      removeBtn.title = "Remove water source";
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        if (onRemoveWaterSpotRef.current) onRemoveWaterSpotRef.current(spot.lng, spot.lat);
      });

      wrapper.appendChild(el);
      wrapper.appendChild(removeBtn);
      wrapper.addEventListener("mouseenter", () => { removeBtn.style.display = "flex"; });
      wrapper.addEventListener("mouseleave", () => { removeBtn.style.display = "none"; });

      const popup = new mapboxgl.Popup({ offset: 22, closeButton: false }).setHTML(
        `<div style="font-size:12px;color:#1e293b;"><strong>${spot.name || "Water source"}</strong><br/><em style="color:#64748b;">User-added · click × to remove</em></div>`
      );
      const marker = new mapboxgl.Marker({ element: wrapper }).setLngLat([spot.lng, spot.lat]).setPopup(popup).addTo(map);
      el.addEventListener("mouseenter", () => marker.togglePopup());
      el.addEventListener("mouseleave", () => { if (marker.getPopup().isOpen()) marker.togglePopup(); });
      userWaterMarkersRef.current.push(marker);
    });
    return () => { userWaterMarkersRef.current.forEach(m => m.remove()); userWaterMarkersRef.current = []; };
  }, [userWaterSpots]);

  // Crosshair cursor in add-water mode
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.getCanvas().style.cursor = addWaterMode ? "crosshair" : "";
  }, [addWaterMode]);

  // Cell coverage overlay — adds/removes/swaps a raster layer when provider changes
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const apply = () => {
      if (map.getLayer("coverage-layer")) map.removeLayer("coverage-layer");
      if (map.getSource("coverage-tiles")) map.removeSource("coverage-tiles");
      if (!coverageProvider) return;
      map.addSource("coverage-tiles", {
        type: "raster",
        tiles: [`/api/proxy/coverage/${coverageProvider}/{z}/{x}/{y}`],
        tileSize: 256,
        attribution: `${coverageProvider} coverage`,
      });
      map.addLayer({
        id: "coverage-layer",
        type: "raster",
        source: "coverage-tiles",
        paint: { "raster-opacity": 0.55 },
      });
    };
    if (map.isStyleLoaded()) apply(); else map.once("load", apply);
  }, [coverageProvider]);

  // Elevation profile hover dot — moves a green circle along the route
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    const pts = route?.points;
    if (hoveredMile === null || !pts?.length) {
      if (hoverMarkerRef.current) { hoverMarkerRef.current.remove(); hoverMarkerRef.current = null; }
      return;
    }
    const pt = getPointAtMile(pts, hoveredMile);
    if (!pt) return;
    if (!hoverMarkerRef.current) {
      const el = document.createElement("div");
      el.style.cssText = "width:14px;height:14px;background:#10b981;border:2.5px solid white;border-radius:50%;box-shadow:0 2px 8px rgba(0,0,0,0.45);pointer-events:none;";
      hoverMarkerRef.current = new mapboxgl.Marker({ element: el }).setLngLat([pt.lng, pt.lat]).addTo(map);
    } else {
      hoverMarkerRef.current.setLngLat([pt.lng, pt.lat]);
    }
  }, [hoveredMile, route]);

  return (
    <div className={`relative ${heightClass || "h-[calc(100vh-9.25rem)]"} min-h-[36rem] overflow-hidden bg-slate-900`}>
      {import.meta.env.VITE_MAPBOX_TOKEN ? (
        <div ref={mapContainerRef} className="absolute inset-0 h-full w-full" />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-100 text-slate-400">Mapbox token missing</div>
      )}

      {/* ── Route overview card (top-left) ── */}
      <div className="pointer-events-none absolute left-5 top-5 w-52 rounded-2xl border border-slate-200/80 p-4 shadow-xl backdrop-blur-sm" style={{ backgroundColor: "rgba(255,255,255,0.96)", color: "#0f172a" }}>
        <p className="text-[10px] font-semibold uppercase tracking-[0.3em]" style={{ color: "#94a3b8" }}>{exploreMode ? "Explore" : "Route Overview"}</p>
        <h3 className="mt-1.5 text-base font-bold leading-snug" style={{ color: "#0f172a" }}>{selectedTrail?.name || "3D trip map"}</h3>
        {selectedTrail?.area && <p className="mt-0.5 text-xs truncate" style={{ color: "#64748b" }}>{selectedTrail.area}</p>}
        <div className="mt-3 grid grid-cols-2 gap-2">
          <div className="rounded-xl px-2.5 py-2" style={{ backgroundColor: "#ecfdf5" }}>
            <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "#047857" }}>Route</p>
            <p className="mt-0.5 text-xs font-bold" style={{ color: "#064e3b" }}>{routeFeature ? "Loaded" : "Point"}</p>
          </div>
          {!exploreMode && (
            <div className="rounded-xl px-2.5 py-2" style={{ backgroundColor: "#fff7ed" }}>
              <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "#c2410c" }}>Fire</p>
              <p className="mt-0.5 text-xs font-bold" style={{ color: "#7c2d12" }}>{fireCount} areas</p>
            </div>
          )}
          {!exploreMode && (
            <div className="rounded-xl px-2.5 py-2" style={{ backgroundColor: "#f0f9ff" }}>
              <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "#0369a1" }}>Snow</p>
              <p className="mt-0.5 text-xs font-bold" style={{ color: "#0c4a6e" }}>{checks?.snow?.max_depth_in ?? "--"} in</p>
            </div>
          )}
          {!exploreMode && (
            <div className="rounded-xl px-2.5 py-2" style={{ backgroundColor: "#ecfeff" }}>
              <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "#0e7490" }}>Water</p>
              <p className="mt-0.5 text-xs font-bold" style={{ color: "#164e63" }}>{checks?.water?.count ?? "--"} src</p>
            </div>
          )}
          <div className="rounded-xl px-2.5 py-2" style={{ backgroundColor: "#f8fafc" }}>
            <p className="text-[10px] font-semibold uppercase tracking-wider" style={{ color: "#64748b" }}>Distance</p>
            <p className="mt-0.5 text-xs font-bold" style={{ color: "#1e293b" }}>{route?.length_miles?.toFixed?.(1) || selectedTrail?.length_miles || "--"} mi</p>
          </div>
        </div>
      </div>

      {/* ── Legend (bottom-left) ── */}
      <div className="pointer-events-none absolute bottom-5 left-5 rounded-xl border border-slate-200/80 px-3 py-2.5 shadow-lg backdrop-blur-sm" style={{ backgroundColor: "rgba(255,255,255,0.96)" }}>
        <p className="mb-2 text-[10px] font-semibold uppercase tracking-[0.3em]" style={{ color: "#94a3b8" }}>Legend</p>
        <div className="space-y-1.5">
          <div className="flex items-center gap-2.5">
            <span className="h-1.5 w-8 shrink-0 rounded-full bg-teal-800 shadow-[0_0_0_4px_rgba(20,184,166,0.2)]" />
            <span className="text-xs font-medium" style={{ color: "#334155" }}>GPX / trail route</span>
          </div>
          {!exploreMode && (
            <>
              <div className="flex items-center gap-2.5">
                <span className="h-3.5 w-8 shrink-0 rounded bg-orange-400/30 ring-1 ring-orange-600" />
                <span className="text-xs font-medium" style={{ color: "#334155" }}>Fire perimeter</span>
              </div>
              <div className="flex items-center gap-2.5">
                <span className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-blue-400 bg-blue-100" />
                <span className="text-xs font-medium" style={{ color: "#334155" }}>Snow depth</span>
              </div>
              <div className="flex items-center gap-2.5">
                <span className="h-3.5 w-3.5 shrink-0 rounded-full border-2 border-cyan-500 bg-cyan-100" />
                <span className="text-xs font-medium" style={{ color: "#334155" }}>Water source</span>
              </div>
              <div className="flex items-center gap-2.5">
                <span className="h-3.5 w-3.5 shrink-0 rounded-full bg-emerald-500 ring-2 ring-emerald-800 flex items-center justify-center text-[8px] font-bold text-white leading-none">1</span>
                <span className="text-xs font-medium" style={{ color: "#334155" }}>Camp marker</span>
              </div>
            </>
          )}
        </div>
      </div>

      {/* ── Briefing card (top-right, nudged left of controls) ── */}
      {!exploreMode && checks && (
        <div className="pointer-events-none absolute right-14 top-5 w-48 rounded-2xl border border-slate-200/80 p-4 shadow-xl backdrop-blur-sm" style={{ backgroundColor: "rgba(255,255,255,0.96)" }}>
          <p className="text-[10px] font-semibold uppercase tracking-[0.3em]" style={{ color: "#94a3b8" }}>Briefing</p>
          <div className="mt-2.5 space-y-1.5">
            {[
              { label: "Weather", value: checks.weather?.forecast?.[0]?.short || "—", extra: true },
              { label: "AQI",     value: checks.aqi?.observations?.[0]?.aqi ?? "—" },
              { label: "Snow",    value: checks.snow?.max_depth_in != null ? `${checks.snow.max_depth_in} in` : "None" },
              { label: "Water",   value: `${checks.water?.count ?? "—"} sources` },
            ].map(({ label, value, extra }) => (
              <div key={label} className="flex justify-between text-xs">
                <span className="font-semibold" style={{ color: "#475569" }}>{label}</span>
                <span className="font-medium text-right ml-2 leading-tight" style={{ color: "#1e293b" }}>{value}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Right-side control strip ── */}
      <div className="pointer-events-auto absolute right-3 top-1/2 -translate-y-1/2 flex flex-col gap-1 z-10">
        <button
          title={is3D ? "Switch to flat 2D view" : "Switch to 3D terrain view"}
          onClick={() => {
            const map = mapRef.current;
            if (!map) return;
            const next = !is3D;
            setIs3D(next);
            map.easeTo({ pitch: next ? 45 : 0, bearing: next ? -12 : 0, duration: 600 });
          }}
          className="w-9 h-9 rounded-xl bg-white/95 shadow border border-slate-200 text-[11px] font-bold text-slate-700 hover:bg-emerald-50 hover:border-emerald-300 hover:text-emerald-700 transition-colors flex items-center justify-center">
          {is3D ? "2D" : "3D"}
        </button>

        <div className="h-px bg-slate-200 mx-1.5 my-0.5" />

        <button title="Zoom in" onClick={() => mapRef.current?.zoomIn({ duration: 250 })}
          className="w-9 h-9 rounded-xl bg-white/95 shadow border border-slate-200 text-xl font-light text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-colors flex items-center justify-center leading-none">
          +
        </button>
        <button title="Zoom out" onClick={() => mapRef.current?.zoomOut({ duration: 250 })}
          className="w-9 h-9 rounded-xl bg-white/95 shadow border border-slate-200 text-xl font-light text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-colors flex items-center justify-center leading-none">
          −
        </button>

        <div className="h-px bg-slate-200 mx-1.5 my-0.5" />

        <button title="Reset to north" onClick={() => mapRef.current?.resetNorth({ duration: 500 })}
          className="w-9 h-9 rounded-xl bg-white/95 shadow border border-slate-200 text-sm text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-colors flex items-center justify-center font-semibold">
          N↑
        </button>

        {routeFeature?.geometry?.coordinates?.length > 0 && (
          <button title="Fit route to view" onClick={() => {
            const map = mapRef.current;
            if (!map) return;
            const coords = routeFeature.geometry.coordinates;
            const bounds = coords.reduce((b, c) => b.extend(c), new mapboxgl.LngLatBounds(coords[0], coords[0]));
            map.fitBounds(bounds, { padding: 80, duration: 700 });
          }}
          className="w-9 h-9 rounded-xl bg-white/95 shadow border border-slate-200 text-slate-600 hover:bg-slate-50 hover:text-slate-900 transition-colors flex items-center justify-center text-sm font-semibold">
          ⊙
        </button>
        )}

        <div className="h-px bg-slate-200 mx-1.5 my-0.5" />

        <button title="Map controls help" onClick={() => setShowHelp(h => !h)}
          className={`w-9 h-9 rounded-xl shadow border text-xs font-bold transition-colors flex items-center justify-center ${showHelp ? "bg-slate-800 text-white border-slate-700" : "bg-white/95 border-slate-200 text-slate-500 hover:bg-slate-50 hover:text-slate-700"}`}>
          ?
        </button>
      </div>

      {/* ── Help panel ── */}
      {showHelp && (
        <div className="pointer-events-auto absolute right-14 top-1/2 -translate-y-1/2 w-56 rounded-2xl border border-slate-200 bg-white/98 p-4 shadow-2xl backdrop-blur-sm z-10">
          <p className="text-[10px] font-semibold uppercase tracking-[0.3em] text-slate-400 mb-3">Map Controls</p>
          <div className="space-y-1.5 text-xs">
            {[
              ["Drag", "Pan the map"],
              ["Scroll / pinch", "Zoom in & out"],
              ["Ctrl + drag", "Rotate & tilt"],
              ["Shift + drag", "Box zoom"],
              ["Double-click", "Zoom in"],
              ["Right-drag", "Rotate bearing"],
            ].map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <span className="w-28 shrink-0 font-semibold text-slate-700">{k}</span>
                <span className="text-slate-500">{v}</span>
              </div>
            ))}
          </div>
          <div className="mt-3 pt-3 border-t border-slate-100 space-y-1.5 text-xs">
            {[
              ["2D / 3D", "Toggle terrain tilt"],
              ["N↑", "Reset to north"],
              ["⊙", "Re-fit route"],
              ["💧 hover ×", "Remove water source"],
              ["Camp marker", "Drag to reposition"],
            ].map(([k, v]) => (
              <div key={k} className="flex gap-2">
                <span className="w-28 shrink-0 font-semibold text-slate-700">{k}</span>
                <span className="text-slate-500">{v}</span>
              </div>
            ))}
          </div>
        </div>
      )}

      {/* ── Scale bar (bottom-right, above water button) ── */}
      {scaleInfo && (
        <div className="pointer-events-none absolute bottom-16 right-14 flex flex-col items-end gap-1 z-10">
          <div className="relative" style={{ width: scaleInfo.barPx }}>
            <div className="absolute left-0 bottom-0 h-2.5 w-0.5 bg-slate-700" />
            <div className="absolute right-0 bottom-0 h-2.5 w-0.5 bg-slate-700" />
            <div className="absolute bottom-0 left-0 right-0 h-0.5 bg-slate-700" />
          </div>
          <span className="text-[11px] font-semibold text-slate-800 bg-white/90 border border-slate-200/80 rounded-md px-1.5 py-0.5 leading-tight shadow-sm" style={{ marginTop: scaleInfo.barPx > 0 ? 2 : 0 }}>
            {scaleInfo.label}
          </span>
        </div>
      )}

      {/* ── Cell coverage toggle (bottom-right, above water button) ── */}
      <div className="pointer-events-auto absolute bottom-[4.5rem] right-3 flex flex-col items-end gap-1 z-10">
        <div className="flex items-center gap-1.5 rounded-full bg-white/95 border border-slate-200 shadow px-3 py-1.5">
          <span className="text-[11px] font-semibold text-slate-600">Coverage</span>
          {["tmobile", "att", "verizon"].map(p => (
            <button key={p}
              onClick={() => setCoverageProvider(prev => prev === p ? null : p)}
              className={`rounded-full px-2 py-0.5 text-[10px] font-bold transition-colors ${
                coverageProvider === p
                  ? p === "tmobile" ? "bg-pink-600 text-white"
                    : p === "att"     ? "bg-blue-600 text-white"
                    : "bg-red-600 text-white"
                  : "bg-slate-100 text-slate-500 hover:bg-slate-200"
              }`}>
              {p === "tmobile" ? "T‑Mo" : p === "att" ? "AT&T" : "Vz"}
            </button>
          ))}
          {coverageProvider && (
            <button onClick={() => setCoverageProvider(null)}
              className="text-[10px] text-slate-400 hover:text-slate-600 font-bold ml-0.5">✕</button>
          )}
        </div>
      </div>

      {/* ── Add water source button (plan mode only) ── */}
      {!exploreMode && onAddWaterSpot && (
        <div className="pointer-events-auto absolute bottom-5 right-3 flex flex-col items-end gap-1.5">
          <button onClick={() => setAddWaterMode?.(m => !m)}
            className={`rounded-full px-4 py-2 text-xs font-semibold shadow-lg transition-colors ${addWaterMode ? "bg-cyan-600 text-white ring-2 ring-cyan-300" : "bg-white/95 text-cyan-700 border border-cyan-300 hover:bg-cyan-50"}`}>
            {addWaterMode ? "Click map to place · click to stop" : "+ Add water source"}
          </button>
          {(userWaterSpots?.length || 0) > 0 && !addWaterMode && (
            <span className="text-[11px] font-medium text-slate-700 bg-white/90 border border-slate-200 rounded-full px-2.5 py-0.5 shadow-sm">
              {userWaterSpots.length} added · hover to remove
            </span>
          )}
        </div>
      )}

      <ElevationProfile route={route} onHoverMile={setHoveredMile} />
    </div>
  );
}

function ReportStep({ planResult, selectedTrail, startDate, itineraryDays, routePoints, tripType, rawMiles, userWaterSpots, onAddWaterSpot, onRemoveWaterSpot, addWaterMode, setAddWaterMode, campPositions, onCampPositionsChange, isDark, onAiReport, onContinueToReport }) {
  const [aiReport, setAiReport] = useState(null);
  const [aiLoading, setAiLoading] = useState(false);
  const hasFiredRef = useRef(false);

  const route = planResult?.route;
  const mapLayers = planResult?.map_layers;
  const checks = planResult?.checks;
  const report = planResult?.report;
  const firePerimeters = mapLayers?.fire_perimeters || checks?.fire?.perimeters || null;
  const snowGeojson = checks?.snow?.geojson || null;
  const waterGeojson = checks?.water?.geojson || null;
  const fallbackCenter = selectedTrail ? [selectedTrail.lng, selectedTrail.lat] : null;

  let routeFeature = routeToFeature(route) || toLineFeature(mapLayers?.route) || toLineFeature(selectedTrail?.geometry);
  if (tripType === "out-and-back" && routeFeature?.geometry?.coordinates?.length >= 10) {
    const fwd = routeFeature.geometry.coordinates;
    const first = fwd[0], last = fwd[fwd.length - 1];
    const isClosedLoop = Math.abs(first[0] - last[0]) < 0.001 && Math.abs(first[1] - last[1]) < 0.001;
    if (!isClosedLoop) {
      routeFeature = { ...routeFeature, geometry: { type: "LineString", coordinates: [...fwd, ...[...fwd].reverse().slice(1)] } };
    }
  }

  const daytimePeriods = getDaytimePeriods(checks?.weather?.forecast);
  const risk = planResult?.risk || computeRisk(checks || {});
  const riskColor = risk.status === "no-go"
    ? { bg: "bg-red-50 border-red-200", text: "text-red-700", badge: "bg-red-600", label: "No-Go" }
    : risk.status === "caution"
    ? { bg: "bg-amber-50 border-amber-200", text: "text-amber-700", badge: "bg-amber-500", label: "Caution" }
    : { bg: "bg-emerald-50 border-emerald-200", text: "text-emerald-700", badge: "bg-emerald-600", label: "Good to Go" };

  const totalMiles = rawMiles > 0
    ? (tripType === "out-and-back" ? rawMiles * 2 : rawMiles)
    : parseFloat(route?.length_miles || selectedTrail?.length_miles || 0);

  // Build day date labels from startDate
  const dayDates = itineraryDays?.map((_, i) => {
    if (!startDate) return null;
    const d = new Date(startDate + "T12:00:00");
    d.setDate(d.getDate() + i);
    return d.toLocaleDateString("en-US", { weekday: "short", month: "short", day: "numeric" });
  }) || [];

  // Nearby fires (within 5mi, past year) for display
  const nearbyFires = (checks?.fire?.perimeters?.features || []).filter(f => {
    const d = f.properties?.distance_from_midpoint_mi;
    const days = f.properties?.days_since_update;
    return (d == null || d <= 5) && (days == null || days <= 365);
  });

  // Water sources from OSM, closest 5
  const osmWater = checks?.water?.geojson?.features?.slice(0, 5) || [];

  const callAiReport = async () => {
    if (!checks) return;
    setAiLoading(true);
    setAiReport(null);
    try {
      const res = await fetch("/api/plan/report", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trail_name: selectedTrail?.name || "Custom route",
          area: selectedTrail?.area || "",
          total_miles: totalMiles,
          trip_type: tripType,
          num_days: itineraryDays?.length || 1,
          days: itineraryDays || [],
          checks,
        }),
      });
      const data = await res.json();
      setAiReport(data);
      if (onAiReport) onAiReport(data);
    } catch { setAiReport({ error: "Could not reach AI" }); }
    setAiLoading(false);
  };

  // Auto-fire once when checks are available
  useEffect(() => {
    if (checks && !hasFiredRef.current) {
      hasFiredRef.current = true;
      callAiReport();
    }
  }, [checks]); // eslint-disable-line react-hooks/exhaustive-deps

  const ai = aiReport?.sections;

  return (
    <div>
      <MapCanvas
        routeFeature={routeFeature}
        firePerimeters={firePerimeters}
        snowGeojson={snowGeojson}
        waterGeojson={waterGeojson}
        fallbackCenter={fallbackCenter}
        report={report}
        checks={checks}
        selectedTrail={selectedTrail}
        route={route}
        exploreMode={false}
        itineraryDays={itineraryDays}
        routePoints={routePoints}
        tripType={tripType}
        rawMiles={rawMiles}
        userWaterSpots={userWaterSpots}
        onAddWaterSpot={onAddWaterSpot}
        onRemoveWaterSpot={onRemoveWaterSpot}
        addWaterMode={addWaterMode}
        setAddWaterMode={setAddWaterMode}
        campPositions={campPositions}
        onCampPositionsChange={onCampPositionsChange}
        heightClass="h-[58vh]"
      />

      <div className="bg-[#f6f3ee] border-t border-slate-200">
        <div className="mx-auto max-w-5xl px-6 py-10 sm:px-8 space-y-10">

          {/* ── Header row ── */}
          <div className="flex items-start justify-between gap-6 flex-wrap">
            <div>
              <p className="text-xs uppercase tracking-[0.4em] text-slate-400">Trip Report</p>
              <h2 className="mt-1 text-3xl font-semibold text-slate-900">{selectedTrail?.name || "Custom Route"}</h2>
              <p className="mt-1 text-sm text-slate-500">
                {selectedTrail?.area && <>{selectedTrail.area} · </>}
                {totalMiles > 0 && <>{totalMiles.toFixed(1)} mi · </>}
                {itineraryDays?.length || 0} days
                {startDate && <> · starts {startDate}</>}
              </p>
            </div>
            <span className={`rounded-full px-5 py-2 text-sm font-bold text-white ${riskColor.badge}`}>{riskColor.label}</span>
          </div>

          {/* ── Situation Brief (AI — auto-generated) ── */}
          <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
            <div className="flex items-center justify-between mb-4">
              <p className="text-xs uppercase tracking-[0.3em] text-slate-400">Situation Brief</p>
              {(aiReport || (!aiLoading)) && (
                <button onClick={callAiReport} disabled={aiLoading}
                  className="rounded-full border border-slate-200 px-3 py-1 text-[10px] font-semibold text-slate-500 hover:border-emerald-300 hover:text-emerald-700 transition disabled:opacity-40">
                  Regenerate
                </button>
              )}
            </div>

            {/* Loading state */}
            {aiLoading && (
              <div className="flex items-center gap-2 text-sm text-slate-400">
                <Spinner size={3} />Analyzing conditions…
              </div>
            )}

            {/* Error state */}
            {aiReport?.error && !aiLoading && (
              <p className="text-sm text-red-400">{aiReport.error}</p>
            )}

            {/* Situation Brief */}
            {ai?.situation_brief && !aiLoading && (
              <p className="text-sm text-slate-700 leading-relaxed">{ai.situation_brief}</p>
            )}

            {/* Risk flags below the brief */}
            {risk.reasons?.length > 0 && (
              <ul className="mt-4 space-y-1 pt-4 border-t border-slate-100">
                {risk.reasons.map((r, i) => (
                  <li key={i} className={`text-xs flex gap-2 ${riskColor.text}`}>
                    <span className="mt-1 h-1.5 w-1.5 shrink-0 rounded-full bg-current inline-block" />{r}
                  </li>
                ))}
              </ul>
            )}
          </div>

          {/* ── Conditions grid ── */}
          <div>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-4">Conditions</p>
            <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-4 mb-4">
              {[
                {
                  label: "Weather",
                  value: checks?.weather?.forecast?.[0]?.short || "—",
                  sub: checks?.weather?.forecast?.[0]?.temp != null ? `${checks.weather.forecast[0].temp}${checks.weather.forecast[0].temp_unit} · ${checks.weather.forecast[0].wind}` : "",
                  warn: /thunder|severe|shower|rain/i.test(checks?.weather?.forecast?.[0]?.short || ""),
                },
                {
                  label: "AQI",
                  value: checks?.aqi?.observations?.[0]?.aqi ?? "—",
                  sub: checks?.aqi?.observations?.[0]?.category || "",
                  warn: (checks?.aqi?.observations?.[0]?.aqi || 0) >= 100,
                },
                {
                  label: "Snow",
                  value: checks?.snow?.max_depth_in != null ? `${checks.snow.max_depth_in} in` : "—",
                  sub: "at highest point",
                  warn: (checks?.snow?.max_depth_in || 0) >= 6,
                },
                {
                  label: "Fire (nearby)",
                  value: nearbyFires.length || "0",
                  sub: nearbyFires.length ? `within 5 mi, past year` : "none within 5 mi",
                  warn: nearbyFires.length > 0,
                },
              ].map(c => (
                <div key={c.label} className={`rounded-xl p-4 ${c.warn ? "bg-amber-50 border border-amber-200" : "bg-white border border-slate-100"} shadow-sm`}>
                  <p className="text-[10px] uppercase tracking-[0.2em] text-slate-400">{c.label}</p>
                  <p className={`mt-1 text-xl font-bold ${c.warn ? "text-amber-800" : "text-slate-900"}`}>{c.value}</p>
                  <p className="mt-0.5 text-xs text-slate-500 truncate">{c.sub}</p>
                </div>
              ))}
            </div>
          </div>

          {/* ── Gear & Timing cards (AI structured output) ── */}
          {(ai?.gear_adds?.length > 0 || ai?.timing_notes?.length > 0 || aiLoading) && (
            <div className="grid gap-4 sm:grid-cols-2">
              {/* Gear */}
              <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
                <p className="text-xs uppercase tracking-[0.3em] text-slate-400 mb-3">Pack List Additions</p>
                {aiLoading ? (
                  <div className="flex items-center gap-2 text-sm text-slate-400"><Spinner size={3} />Generating…</div>
                ) : ai?.gear_adds?.length > 0 ? (
                  <ul className="space-y-2">
                    {ai.gear_adds.map((item, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm text-slate-700">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-emerald-500" />{item}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>

              {/* Timing */}
              <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
                <p className="text-xs uppercase tracking-[0.3em] text-slate-400 mb-3">Timing Notes</p>
                {aiLoading ? (
                  <div className="flex items-center gap-2 text-sm text-slate-400"><Spinner size={3} />Generating…</div>
                ) : ai?.timing_notes?.length > 0 ? (
                  <ul className="space-y-2">
                    {ai.timing_notes.map((note, i) => (
                      <li key={i} className="flex items-start gap-2 text-sm text-slate-700">
                        <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-500" />{note}
                      </li>
                    ))}
                  </ul>
                ) : null}
              </div>
            </div>
          )}

          {/* ── Day-by-day ── */}
          {itineraryDays?.length > 0 && (
            <div>
              <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-4">Day by Day</p>
              <div className="space-y-4">
                {itineraryDays.map((day, i) => {
                  const period = daytimePeriods[i];
                  const wText = (period?.short || "").toLowerCase();
                  const isWarn = /thunder|severe/.test(wText);
                  const isCaution = /shower|rain|drizzle|snow/.test(wText);
                  const camp = campPositions?.[day.day];
                  return (
                    <div key={day.day} className="rounded-2xl bg-white border border-slate-100 shadow-sm overflow-hidden">
                      {/* Day header */}
                      <div className="flex items-center justify-between px-5 py-3 border-b border-slate-100 bg-slate-50">
                        <div className="flex items-center gap-3">
                          <span className="h-7 w-7 rounded-full bg-emerald-600 text-white text-xs font-bold flex items-center justify-center">{day.day}</span>
                          <div>
                            <p className="text-sm font-semibold text-slate-900">{dayDates[i] || `Day ${day.day}`}</p>
                            <p className="text-xs text-slate-400">Mile {day.startMile} → {day.endMile} · {day.miles} mi</p>
                          </div>
                        </div>
                        {(isWarn || isCaution) && (
                          <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${isWarn ? "bg-amber-100 text-amber-700" : "bg-yellow-100 text-yellow-700"}`}>
                            {isWarn ? "Storm" : "Rain"}
                          </span>
                        )}
                      </div>

                      <div className="px-5 py-4 space-y-3">
                        {/* Weather for this day — from NWS */}
                        {period && (
                          <div className="flex items-start gap-3">
                            <span className="text-[10px] uppercase tracking-[0.2em] text-slate-400 mt-0.5 w-16 shrink-0">Weather</span>
                            <p className="text-sm text-slate-700">{period.short} · {period.temp}{period.temp_unit} · {period.wind}</p>
                          </div>
                        )}

                        {/* Camp location — from dragged marker */}
                        {day.day < (itineraryDays?.length || 0) && (
                          <div className="flex items-start gap-3">
                            <span className="text-[10px] uppercase tracking-[0.2em] text-slate-400 mt-0.5 w-16 shrink-0">Camp</span>
                            <p className="text-sm text-slate-700">
                              {camp
                                ? `${camp.lat.toFixed(4)}°N, ${Math.abs(camp.lng).toFixed(4)}°W`
                                : "Drag marker on map to set"}
                            </p>
                          </div>
                        )}

                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* ── Continue to final report ── */}
          {onContinueToReport && (
            <div className="border-t border-slate-100 pt-6 flex justify-end">
              <button onClick={onContinueToReport}
                className="rounded-full bg-emerald-600 px-8 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 hover:bg-emerald-700 transition">
                Continue to Report →
              </button>
            </div>
          )}

          {/* ── Water sources ── */}
          {(osmWater.length > 0 || userWaterSpots?.length > 0) && (
            <div>
              <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-4">Water Sources</p>
              <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {osmWater.map((f, i) => (
                  <div key={i} className="rounded-xl bg-white border border-cyan-100 p-4 shadow-sm">
                    <p className="text-xs font-semibold text-cyan-800">{f.properties?.name || f.properties?.water_type}</p>
                    <p className="text-xs text-slate-500 mt-0.5">{f.properties?.water_type} · {f.properties?.distance_mi} mi from trail</p>
                  </div>
                ))}
                {(userWaterSpots || []).map((s, i) => (
                  <div key={`u${i}`} className="rounded-xl bg-cyan-50 border border-cyan-200 p-4 shadow-sm">
                    <p className="text-xs font-semibold text-cyan-800">{s.name || "User-added"}</p>
                    <p className="text-xs text-slate-500 mt-0.5">{s.lat.toFixed(4)}°N, {Math.abs(s.lng).toFixed(4)}°W · User placed</p>
                  </div>
                ))}
              </div>
            </div>
          )}

        </div>
      </div>
    </div>
  );
}

// ─── Final Report Step ────────────────────────────────────────────────────────

function FinalReportStep({ planResult, selectedTrail, startDate, itineraryDays, routePoints, tripType, rawMiles, userWaterSpots, campPositions, aiReport }) {
  const checks = planResult?.checks;
  const mapLayers = planResult?.map_layers;
  const route = planResult?.route;
  const ai = aiReport?.sections;

  const firePerimeters = mapLayers?.fire_perimeters || checks?.fire?.perimeters || null;
  const snowGeojson    = checks?.snow?.geojson || null;
  const waterGeojson   = checks?.water?.geojson || null;
  const osmWater       = checks?.water?.geojson?.features || [];

  let routeFeature = routeToFeature(route) || toLineFeature(mapLayers?.route) || toLineFeature(selectedTrail?.geometry);
  if (tripType === "out-and-back" && routeFeature?.geometry?.coordinates?.length >= 10) {
    const fwd = routeFeature.geometry.coordinates;
    const first = fwd[0], last = fwd[fwd.length - 1];
    if (!(Math.abs(first[0] - last[0]) < 0.001 && Math.abs(first[1] - last[1]) < 0.001)) {
      routeFeature = { ...routeFeature, geometry: { type: "LineString", coordinates: [...fwd, ...[...fwd].reverse().slice(1)] } };
    }
  }
  const fallbackCenter = selectedTrail ? [selectedTrail.lng, selectedTrail.lat] : null;

  const totalMiles = rawMiles > 0
    ? (tripType === "out-and-back" ? rawMiles * 2 : rawMiles)
    : parseFloat(route?.length_miles || selectedTrail?.length_miles || 0);

  const daytimePeriods = getDaytimePeriods(checks?.weather?.forecast);

  const dayDates = itineraryDays?.map((_, i) => {
    if (!startDate) return null;
    const d = new Date(startDate + "T12:00:00");
    d.setDate(d.getDate() + i);
    return d.toLocaleDateString("en-US", { weekday: "long", month: "long", day: "numeric" });
  }) || [];

  // Build per-day route segments from camp positions (or equidistant split)
  const daySegments = (itineraryDays || []).map((day, i) => {
    let segPts = [];
    if (routePoints?.length) {
      const prevCampPos = i === 0 ? null : campPositions?.[i];
      const thisCampPos = day.day < (itineraryDays?.length || 0) ? campPositions?.[day.day] : null;
      const startIdx = prevCampPos ? findNearestRouteIdx(routePoints, prevCampPos.lat, prevCampPos.lng) : 0;
      const endIdx   = thisCampPos ? findNearestRouteIdx(routePoints, thisCampPos.lat, thisCampPos.lng) : routePoints.length - 1;
      segPts = routePoints.slice(Math.min(startIdx, endIdx), Math.max(startIdx, endIdx) + 1);
    }
    const { gainFt, dropFt } = computeSegmentElevation(segPts);
    const midPt = segPts[Math.floor(segPts.length / 2)] || null;
    return { ...day, segPts, gainFt, dropFt, midPt };
  });

  const nearbyRecentFires = (checks?.fire?.perimeters?.features || []).filter(f => {
    const d = f.properties?.distance_from_midpoint_mi;
    const days = f.properties?.days_since_update;
    return (d == null || d <= 5) && (days == null || days <= 365);
  });

  const handlePrint = () => window.print();

  return (
    <div className="bg-white min-h-screen">
      {/* Print CSS injected via style tag */}
      <style>{`
        @media print {
          .no-print { display: none !important; }
          body { background: white; }
          .page-break { page-break-before: always; }
          .avoid-break { page-break-inside: avoid; }
        }
      `}</style>

      {/* ── Report header ── */}
      <div className="border-b border-slate-100 bg-white sticky top-0 z-10 no-print">
        <div className="max-w-5xl mx-auto px-6 py-4 sm:px-8 flex items-center justify-between gap-4">
          <div>
            <p className="text-[10px] uppercase tracking-[0.4em] text-slate-400">Final Report</p>
            <h2 className="text-xl font-bold text-slate-900 mt-0.5">{selectedTrail?.name || "Custom Route"}</h2>
          </div>
          <button onClick={handlePrint}
            className="rounded-full bg-slate-800 px-5 py-2.5 text-sm font-semibold text-white hover:bg-slate-700 transition shrink-0">
            Print / Save PDF
          </button>
        </div>
      </div>

      <div className="max-w-5xl mx-auto px-6 pb-16 sm:px-8 space-y-12 pt-8">

        {/* ── Trip summary line ── */}
        <div className="flex flex-wrap gap-6 items-start">
          <div>
            <h1 className="text-3xl font-bold text-slate-900">{selectedTrail?.name || "Custom Route"}</h1>
            <p className="mt-1 text-slate-500 text-sm">
              {selectedTrail?.area && <>{selectedTrail.area} · </>}
              {totalMiles > 0 && <>{totalMiles.toFixed(1)} mi · </>}
              {itineraryDays?.length || 0} days
              {startDate && <> · starts {startDate}</>}
            </p>
          </div>
          {nearbyRecentFires.length > 0 && (
            <span className="rounded-full bg-amber-500 text-white px-4 py-1.5 text-sm font-bold">⚠ Fire Caution</span>
          )}
        </div>

        {/* ── 2D Map with layer toggles ── */}
        <section className="no-print">
          <p className="text-[10px] uppercase tracking-[0.4em] text-slate-400 mb-3">Interactive Map</p>
          <ReportMap2D
            routeFeature={routeFeature}
            firePerimeters={firePerimeters}
            snowGeojson={snowGeojson}
            waterGeojson={waterGeojson}
            fallbackCenter={fallbackCenter}
            campPositions={campPositions}
            itineraryDays={itineraryDays}
            userWaterSpots={userWaterSpots}
          />
          {/* Export strip */}
          <div className="mt-3 flex flex-wrap items-center gap-2">
            <span className="text-[10px] font-semibold uppercase tracking-widest text-slate-400">Export</span>
            {[
              { label: "GPX", fn: () => downloadFile(buildGpx(routePoints, campPositions, itineraryDays, userWaterSpots, selectedTrail?.name || "route"), "trail.gpx", "application/gpx+xml") },
              { label: "KML", fn: () => downloadFile(buildKml(routePoints, campPositions, itineraryDays, userWaterSpots, selectedTrail?.name || "route"), "trail.kml", "application/vnd.google-earth.kml+xml") },
              { label: "GeoJSON", fn: () => downloadFile(buildGeoJson(routePoints, campPositions, itineraryDays, userWaterSpots, selectedTrail?.name || "route"), "trail.geojson", "application/geo+json") },
            ].map(({ label, fn }) => (
              <button key={label} onClick={fn}
                className="rounded-full border border-slate-200 bg-white px-3 py-1.5 text-[11px] font-semibold text-slate-700 hover:border-emerald-400 hover:text-emerald-700 transition">
                ↓ {label}
              </button>
            ))}
            <button
              onClick={() => {
                downloadFile(buildKml(routePoints, campPositions, itineraryDays, userWaterSpots, selectedTrail?.name || "route"), "trail.kml", "application/vnd.google-earth.kml+xml");
                setTimeout(() => window.open("https://caltopo.com/map.html", "_blank"), 400);
              }}
              className="rounded-full border border-orange-200 bg-orange-50 px-3 py-1.5 text-[11px] font-semibold text-orange-700 hover:bg-orange-100 transition">
              ↗ Open in CalTopo
            </button>
            <span className="text-[10px] text-slate-400 ml-1">CalTopo: Add → Import File → select the KML</span>
          </div>
        </section>

        {/* ── Pack list ── */}
        {ai?.gear_adds?.length > 0 && (
          <section className="avoid-break">
            <p className="text-[10px] uppercase tracking-[0.4em] text-slate-400 mb-4">Pack List Additions</p>
            <div className="rounded-2xl border border-slate-100 bg-slate-50 p-6">
              <div className="grid gap-3 sm:grid-cols-2">
                {ai.gear_adds.map((item, i) => (
                  <label key={i} className="flex items-start gap-3 cursor-pointer group">
                    <span className="mt-0.5 h-4 w-4 shrink-0 rounded border-2 border-slate-300 group-hover:border-emerald-500 transition" />
                    <span className="text-sm text-slate-700">{item}</span>
                  </label>
                ))}
              </div>
            </div>
          </section>
        )}

        {/* ── Day-by-day ── */}
        <section>
          <p className="text-[10px] uppercase tracking-[0.4em] text-slate-400 mb-5">Day by Day</p>
          <div className="space-y-6">
            {daySegments.map((day, i) => {
              const period = daytimePeriods[i];
              const wText  = (period?.short || "").toLowerCase();
              const isWarn = /thunder|severe/.test(wText);
              const isCaution = /shower|rain|drizzle|snow/.test(wText);

              // Water near this day's midpoint
              const nearWater = day.midPt
                ? osmWater.filter(f => {
                    const coords = f.geometry?.coordinates;
                    if (!coords) return false;
                    return haversineMiles(day.midPt.lat, day.midPt.lng, coords[1], coords[0]) < 4;
                  }).slice(0, 4)
                : [];
              const nearUserWater = day.midPt
                ? (userWaterSpots || []).filter(s => haversineMiles(day.midPt.lat, day.midPt.lng, s.lat, s.lng) < 4)
                : [];

              return (
                <div key={day.day} className="rounded-2xl border border-slate-200 overflow-hidden shadow-sm avoid-break">
                  {/* ── Day header ── */}
                  <div className="flex items-center justify-between px-6 py-4 bg-slate-900 text-white">
                    <div className="flex items-center gap-4">
                      <span className="h-9 w-9 shrink-0 rounded-full bg-emerald-500 flex items-center justify-center text-sm font-bold">{day.day}</span>
                      <div>
                        <p className="font-semibold text-base">{dayDates[i] || `Day ${day.day}`}</p>
                        <p className="text-slate-400 text-xs mt-0.5">
                          {day.miles} mi &nbsp;·&nbsp;
                          <span className="text-emerald-400">↑{day.gainFt.toLocaleString()} ft</span>
                          &nbsp;·&nbsp;
                          <span className="text-sky-400">↓{day.dropFt.toLocaleString()} ft</span>
                        </p>
                      </div>
                    </div>
                    {(isWarn || isCaution) && (
                      <span className={`rounded-full px-3 py-1 text-xs font-semibold ${isWarn ? "bg-amber-500 text-white" : "bg-yellow-400 text-yellow-900"}`}>
                        {isWarn ? "⚠ Storm" : "🌧 Rain"}
                      </span>
                    )}
                  </div>

                  <div className="grid sm:grid-cols-3 divide-y sm:divide-y-0 sm:divide-x divide-slate-100">
                    {/* ── Weather column ── */}
                    <div className="px-5 py-4">
                      <p className="text-[10px] uppercase tracking-[0.3em] text-slate-400 mb-2">Weather</p>
                      {period ? (
                        <>
                          <p className="text-sm font-semibold text-slate-800">{period.short}</p>
                          <p className="text-xs text-slate-500 mt-1">{period.temp}{period.temp_unit} · {period.wind}</p>
                        </>
                      ) : (
                        <p className="text-xs text-slate-400">No forecast</p>
                      )}
                    </div>

                    {/* ── Elevation column ── */}
                    <div className="px-5 py-4">
                      <p className="text-[10px] uppercase tracking-[0.3em] text-slate-400 mb-2">Elevation</p>
                      <MiniElevProfile pts={day.segPts} />
                      <div className="flex justify-between text-[9px] text-slate-400 mt-1.5">
                        <span>Mi {day.startMile}</span>
                        <span className="text-emerald-600 font-medium">↑{day.gainFt.toLocaleString()} ↓{day.dropFt.toLocaleString()} ft</span>
                        <span>Mi {day.endMile}</span>
                      </div>
                    </div>

                    {/* ── Water column ── */}
                    <div className="px-5 py-4">
                      <p className="text-[10px] uppercase tracking-[0.3em] text-slate-400 mb-2">Water Sources</p>
                      {nearWater.length > 0 || nearUserWater.length > 0 ? (
                        <div className="space-y-1.5">
                          {nearWater.map((f, j) => (
                            <div key={j} className="flex items-center gap-2 text-xs">
                              <span className="text-cyan-500 shrink-0">💧</span>
                              <span className="text-slate-700 truncate">{f.properties?.name || f.properties?.water_type || "Water"}</span>
                              <span className="text-slate-400 shrink-0 ml-auto">{f.properties?.distance_mi} mi</span>
                            </div>
                          ))}
                          {nearUserWater.map((s, j) => (
                            <div key={`u${j}`} className="flex items-center gap-2 text-xs">
                              <span className="text-cyan-600 shrink-0">💧</span>
                              <span className="text-slate-700 truncate">{s.name}</span>
                              <span className="text-slate-400 shrink-0 ml-auto">user</span>
                            </div>
                          ))}
                        </div>
                      ) : (
                        <p className="text-xs text-slate-400">None found nearby</p>
                      )}
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </section>

        {/* ── AI timing notes ── */}
        {ai?.timing_notes?.length > 0 && (
          <section className="avoid-break">
            <p className="text-[10px] uppercase tracking-[0.4em] text-slate-400 mb-4">Timing Notes</p>
            <div className="rounded-2xl border border-slate-100 bg-slate-50 p-6 space-y-2">
              {ai.timing_notes.map((note, i) => (
                <div key={i} className="flex items-start gap-3 text-sm text-slate-700">
                  <span className="mt-1.5 h-1.5 w-1.5 shrink-0 rounded-full bg-sky-400" />
                  {note}
                </div>
              ))}
            </div>
          </section>
        )}

      </div>
    </div>
  );
}

// ─── Dashboard ────────────────────────────────────────────────────────────────

function DashboardView({ onNewPlan, onExplore, isDark }) {
  const sessions = []; // placeholder — will be populated from DB

  return (
    <div className={`min-h-screen ${isDark ? "bg-black text-slate-100" : "bg-[#f6f3ee] text-slate-900"}`}>
      <header className={`border-b sticky top-0 z-30 backdrop-blur ${isDark ? "border-neutral-800 bg-black/90" : "border-slate-200/70 bg-[#f6f3ee]/95"}`}>
        <div className="mx-auto max-w-7xl px-6 py-5 sm:px-8 lg:px-12 flex items-center justify-between">
          <div>
            <p className={`text-xs uppercase tracking-[0.5em] ${isDark ? "text-emerald-300" : "text-emerald-700"}`}>Backcountry</p>
            <h1 className={`text-xl font-semibold mt-0.5 ${isDark ? "text-white" : "text-slate-900"}`}>Trip Planner</h1>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-7xl px-6 py-12 sm:px-8 lg:px-12 space-y-12">
        {/* Hero */}
        <div>
          <h2 className="text-4xl font-semibold text-slate-900">Where are you heading?</h2>
          <p className="mt-3 text-slate-500 max-w-xl">Plan a full backpacking trip with weather, fire, and snow checks — or just explore a trail on the map.</p>
        </div>

        {/* CTAs */}
        <div className="grid gap-5 sm:grid-cols-2 max-w-2xl">
          <button onClick={onNewPlan}
            className="group rounded-[28px] bg-emerald-600 p-8 text-left shadow-xl shadow-emerald-500/20 hover:bg-emerald-700 transition">
            <p className="text-xs uppercase tracking-[0.4em] text-emerald-200">Full planning</p>
            <h3 className="mt-3 text-2xl font-semibold text-white">New Plan</h3>
            <p className="mt-2 text-sm text-emerald-100/80">Route, dates, itinerary, pre-trip checks, and a full report.</p>
            <div className="mt-6 flex items-center gap-2 text-emerald-200 text-sm font-semibold">Start planning <span>→</span></div>
          </button>

          <button onClick={onExplore}
            className="group rounded-[28px] border border-slate-200 bg-white p-8 text-left shadow-sm hover:border-emerald-300 hover:shadow-md transition">
            <p className="text-xs uppercase tracking-[0.4em] text-slate-400">Quick look</p>
            <h3 className="mt-3 text-2xl font-semibold text-slate-900">Explore</h3>
            <p className="mt-2 text-sm text-slate-500">Search a trail and view it on the 3D map without running checks.</p>
            <div className="mt-6 flex items-center gap-2 text-emerald-600 text-sm font-semibold">Open map <span>→</span></div>
          </button>
        </div>

        {/* Sessions */}
        <div>
          <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-5">Saved plans</p>
          {sessions.length === 0 ? (
            <div className="rounded-2xl border border-dashed border-slate-200 bg-white px-8 py-12 text-center">
              <p className="text-slate-400 text-sm">No saved plans yet. Start a new plan above.</p>
            </div>
          ) : (
            <div className="grid gap-4 sm:grid-cols-3">
              {sessions.map((s) => (
                <div key={s.id} className="rounded-2xl border border-slate-200 bg-white p-5">
                  <p className="font-semibold text-slate-900">{s.name}</p>
                  <p className="text-xs text-slate-400 mt-1">{s.date}</p>
                </div>
              ))}
            </div>
          )}
        </div>
      </main>
    </div>
  );
}

// ─── Explore mode ─────────────────────────────────────────────────────────────

function ExploreView({ onBack, isDark }) {
  const [step, setStep] = useState("search");
  const [trailName, setTrailName] = useState("");
  const [selectedTrail, setSelectedTrail] = useState(null);
  const [searchResults, setSearchResults] = useState([]);
  const [gpxRoute, setGpxRoute] = useState(null);
  const [inputMode, setInputMode] = useState("name");
  const [selectedFile, setSelectedFile] = useState(null);
  const fileInputRef = useRef(null);

  const handleNameSearch = async () => {
    if (!trailName.trim()) return;
    try {
      const res = await fetch("/api/trail/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: { points: [] }, name_hint: trailName }),
      });
      const data = await res.json();
      if (data.auto_selected) {
        setSelectedTrail(data.auto_selected);
        setSearchResults([]);
        setStep("map");
      } else if (data.shortlist?.length) {
        setSearchResults(data.shortlist);
      }
    } catch { /* ignore */ }
  };

  const handleSuggestionSelect = async (trail) => {
    setTrailName(trail.name);
    setSelectedTrail(trail);
    setStep("map");
    try {
      const res = await fetch("/api/trail/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: { points: [] }, name_hint: trail.name }),
      });
      const data = await res.json();
      const full = data.shortlist?.find((t) => t.id === trail.id) || data.auto_selected || trail;
      setSelectedTrail(full);
    } catch { /* ignore */ }
  };

  const handleGpxUpload = async () => {
    if (!selectedFile) return;
    const formData = new FormData();
    formData.append("file", selectedFile);
    try {
      const res = await fetch("/api/route/parse", { method: "POST", body: formData });
      const data = await res.json();
      if (data.route) { setGpxRoute(data.route); setStep("map"); }
    } catch { /* ignore */ }
  };

  const routeFeature = routeToFeature(gpxRoute) || toLineFeature(selectedTrail?.geometry);
  const fallbackCenter = selectedTrail ? [selectedTrail.lng, selectedTrail.lat] : null;

  if (step === "map") {
    return (
      <div className={`min-h-screen ${isDark ? "bg-black text-slate-100" : "bg-slate-900 text-white"}`}>
        <div className={`sticky top-0 z-30 border-b backdrop-blur px-6 py-3 flex items-center justify-between ${isDark ? "border-neutral-800 bg-black/95" : "border-white/10 bg-slate-900/95"}`}>
          <BackBtn label="← Dashboard" onClick={onBack} />
          <p className="text-xs uppercase tracking-[0.3em] text-slate-400">Explore — {selectedTrail?.name || "Custom GPX"}</p>
          <button onClick={() => setStep("search")} className="text-xs uppercase tracking-[0.3em] text-slate-400 hover:text-white transition">← Search</button>
        </div>
        <MapCanvas
          routeFeature={routeFeature}
          firePerimeters={null}
          snowGeojson={null}
          fallbackCenter={fallbackCenter}
          selectedTrail={selectedTrail}
          route={gpxRoute}
          exploreMode={true}
        />
      </div>
    );
  }

  return (
    <div className={`min-h-screen ${isDark ? "bg-black text-slate-100" : "bg-[#f6f3ee] text-slate-900"}`}>
      <header className={`sticky top-0 z-30 border-b backdrop-blur ${isDark ? "border-neutral-800 bg-black/95" : "border-slate-200/70 bg-[#f6f3ee]/95"}`}>
        <div className="mx-auto max-w-7xl px-6 py-4 sm:px-8 lg:px-12 flex items-center gap-6">
          <BackBtn label="← Dashboard" onClick={onBack} />
          <div>
            <p className={`text-xs uppercase tracking-[0.4em] ${isDark ? "text-emerald-300" : "text-emerald-700"}`}>Explore Mode</p>
            <p className={`text-sm mt-0.5 ${isDark ? "text-slate-300" : "text-slate-500"}`}>Find a trail and view it on the map</p>
          </div>
        </div>
      </header>

      <main className="mx-auto max-w-3xl px-6 py-10 sm:px-8 lg:px-12 space-y-8">
        <div className="flex gap-3 p-1 bg-slate-100 rounded-xl w-fit">
          <button onClick={() => setInputMode("name")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${inputMode === "name" ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Trail Name</button>
          <button onClick={() => setInputMode("gpx")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${inputMode === "gpx" ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Upload GPX</button>
        </div>

        {inputMode === "name" ? (
          <div className="rounded-[32px] border border-emerald-100 bg-gradient-to-br from-white via-white to-emerald-50/40 p-10 shadow-lg shadow-emerald-200/30">
            <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">Trail Search</p>
            <h3 className="mt-3 text-2xl font-semibold text-slate-900">Find a trail</h3>
            <div className="mt-6">
              <TrailSearchInput trailName={trailName} setTrailName={setTrailName} onNameSearch={handleNameSearch} onSuggestionSelect={handleSuggestionSelect} />
            </div>
          </div>
        ) : (
          <div className="rounded-[32px] border border-emerald-100 bg-gradient-to-br from-white via-white to-emerald-50/40 p-10 shadow-lg shadow-emerald-200/30">
            <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">GPX Upload</p>
            <h3 className="mt-3 text-2xl font-semibold text-slate-900">View your GPX route</h3>
            <div className="mt-8 rounded-3xl border border-dashed border-emerald-300 bg-white/80 px-6 py-10 text-center">
              <input ref={fileInputRef} type="file" accept=".gpx" onChange={(e) => setSelectedFile(e.target.files?.[0])} className="hidden" id="explore-gpx" />
              <label htmlFor="explore-gpx" className="cursor-pointer block text-sm text-slate-500 mb-4">
                {selectedFile ? selectedFile.name : "Click to upload GPX"}
              </label>
              <button onClick={handleGpxUpload} disabled={!selectedFile}
                className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
                View on Map
              </button>
            </div>
          </div>
        )}

        {searchResults.length > 0 && (
          <div className="grid gap-4 sm:grid-cols-2">
            {searchResults.map((trail) => (
              <div key={trail.id} onClick={() => handleSuggestionSelect(trail)}
                className="cursor-pointer rounded-2xl border border-slate-200 bg-white p-5 hover:border-emerald-300 transition">
                <h4 className="font-semibold text-slate-900">{trail.name}</h4>
                <p className="text-sm text-slate-500 mt-1">{trail.area} · {trail.length_miles} mi</p>
              </div>
            ))}
          </div>
        )}
      </main>
    </div>
  );
}

// ─── Plan mode ────────────────────────────────────────────────────────────────

export function PlanView({ onBack, isDark }) {
  const [activeStep, setActiveStep] = useState("upload");
  const [route, setRoute] = useState(null);
  const [gpxRoute, setGpxRoute] = useState(null);
  const [startDate, setStartDate] = useState("2026-04-30");
  const [trailMatch, setTrailMatch] = useState(null);
  const [selectedTrail, setSelectedTrail] = useState(null);
  const [selectedTrailId, setSelectedTrailId] = useState(null);
  const [useCustomGpx, setUseCustomGpx] = useState(false);
  const [numDays, setNumDays] = useState(3);
  const [tripType, setTripType] = useState("out-and-back");
  const [checks, setChecks] = useState(null);
  const [checksLoading, setChecksLoading] = useState({ weather: false, aqi: false, fire: false, snow: false, water: false });
  const [planResult, setPlanResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [inputMode, setInputMode] = useState("name");
  const [trailName, setTrailName] = useState("");
  const [nameSearchResults, setNameSearchResults] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [userWaterSpots, setUserWaterSpots] = useState([]);
  const [addWaterMode, setAddWaterMode] = useState(false);
  const handleRemoveWaterSpot = (lng, lat) =>
    setUserWaterSpots(prev => prev.filter(s => !(s.lng === lng && s.lat === lat)));
  const [campPositions, setCampPositions] = useState({});
  const [aiReport, setAiReport] = useState(null);
  const fileInputRef = useRef(null);

  // Derived values
  const endDate = addDays(startDate, numDays);
  const rawMiles = parseFloat(gpxRoute?.length_miles || gpxRoute?.distance_mi || selectedTrail?.length_miles || 0);
  const effectiveMiles = tripType === "out-and-back" ? rawMiles * 2 : rawMiles;
  const milesPerDay = numDays > 0 && effectiveMiles > 0 ? effectiveMiles / numDays : 0;
  // Memoized so the camp-marker effect in MapCanvas doesn't re-run on unrelated renders.
  const itineraryDays = useMemo(() => Array.from({ length: numDays }, (_, i) => ({
    day: i + 1, startMile: +(i * milesPerDay).toFixed(1), endMile: +((i + 1) * milesPerDay).toFixed(1), miles: +milesPerDay.toFixed(1),
  })), [numDays, milesPerDay]);
  const routePoints = useMemo(() =>
    gpxRoute?.points || (selectedTrail?.geometry?.coordinates || []).map(c => ({ lat: c[1], lng: c[0] })),
    [gpxRoute, selectedTrail]
  );

  const stepIndex = PLAN_STEPS.findIndex((s) => s.id === activeStep);
  const progress = ((stepIndex + 1) / PLAN_STEPS.length) * 100;
  const goTo = (id) => setActiveStep(id);

  // ── Upload handlers ──

  const handleFileChange = (e) => { const f = e.target.files?.[0]; if (f) setSelectedFile(f); };

  const handleUpload = async () => {
    if (!selectedFile) return;
    const formData = new FormData();
    formData.append("file", selectedFile);
    setLoading(true);
    try {
      const res = await fetch("/api/route/parse", { method: "POST", body: formData });
      const data = await res.json();
      if (data.route) {
        setRoute(data.route);
        setGpxRoute(data.route);
        // Immediately run trail match from GPX
        try {
          const mr = await fetch("/api/trail/match", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ route: data.route, name_hint: "" }),
          });
          const md = await mr.json();
          setTrailMatch(md);
          if (md.auto_selected) { setSelectedTrailId(md.auto_selected.id); setSelectedTrail(md.auto_selected); }
        } catch { /* best-effort */ }
        goTo("match");
      } else alert(data.detail || "Failed to parse route");
    } catch (err) { alert("Error uploading: " + err.message); }
    setLoading(false);
  };

  const handleNameSearch = async () => {
    if (!trailName.trim()) return;
    setGpxRoute(null);
    try {
      const res = await fetch("/api/trail/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: { points: [] }, name_hint: trailName }),
      });
      const data = await res.json();
      if (data.auto_selected) {
        setRoute(null);
        setSelectedTrailId(data.auto_selected.id);
        setSelectedTrail(data.auto_selected);
        setTrailMatch({ auto_selected: data.auto_selected, shortlist: data.shortlist || [data.auto_selected] });
        goTo("match");
      } else if (data.shortlist?.length) {
        setNameSearchResults(data.shortlist);
      }
    } catch { /* ignore */ }
  };

  const handleNameSelect = async (trail) => {
    setSelectedTrailId(trail.id);
    setTrailName(trail.name);
    setSelectedTrail(trail);
    setNameSearchResults([]);
    goTo("match");
    try {
      const res = await fetch("/api/trail/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: { points: [] }, name_hint: trail.name }),
      });
      const data = await res.json();
      const full = data.shortlist?.find((t) => t.id === trail.id) || data.auto_selected || trail;
      setTrailMatch({ auto_selected: full, shortlist: data.shortlist || [full] });
      setSelectedTrail(full);
      if (full.route_type) setTripType(full.route_type === "loop" ? "loop" : "out-and-back");
    } catch { setTrailMatch({ auto_selected: trail, shortlist: [trail] }); }
  };

  // ── Dates handler ──

  const handleDatesNext = () => goTo("itinerary");

  // ── Match handlers ──

  const handleMatchNext = () => { goTo("dates"); };

  const handleUseCustomGpx = () => {
    setUseCustomGpx(true);
    setSelectedTrail(null);
    setSelectedTrailId(null);
    goTo("dates");
  };

  // ── Checks ──

  const runPreTripChecks = async () => {
    setLoading(true);
    setChecks(null);
    let lat, lng;
    if (gpxRoute?.midpoint) { [lat, lng] = gpxRoute.midpoint; }
    else if (selectedTrail) { lat = selectedTrail.lat; lng = selectedTrail.lng; }
    else { setLoading(false); return; }

    const payload = { lat, lng, start_date: startDate, end_date: endDate };
    setChecksLoading({ weather: true, aqi: true, fire: true, snow: true, water: true });

    const postJson = (url, body) =>
      fetch(url, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) }).then((r) => r.json());

    const runCheck = async (key, url, body) => {
      const result = await postJson(url, body);
      setChecks((prev) => ({ ...(prev || {}), [key]: result }));
      setChecksLoading((prev) => ({ ...prev, [key]: false }));
      return result;
    };

    const sampleRoutePoints = routePoints?.length ? interpolateRoutePoints(routePoints, 12) : null;

    const [weather, aqi, fire, snow, water] = await Promise.all([
      runCheck("weather", "/api/checks/weather", payload),
      runCheck("aqi", "/api/checks/aqi", payload),
      runCheck("fire", "/api/checks/fire", { ...payload, radius: 50.0 }),
      runCheck("snow", "/api/checks/snow", { ...payload, radius: 5.0 }),
      runCheck("water", "/api/checks/water", { lat, lng, radius: 0.5, route_points: sampleRoutePoints }),
    ]);

    const allChecks = { weather, aqi, fire, snow, water };
    const risk = computeRisk(allChecks);
    const report = buildReport(selectedTrail, risk);
    setPlanResult({
      route: gpxRoute,
      selected_trail: selectedTrail,
      checks: allChecks,
      risk,
      report,
      map_layers: { fire_perimeters: fire?.perimeters },
    });
    setLoading(false);
  };

  const handleChecksNext = () => goTo("report");

  // ── Render ──

  const isReportStep = activeStep === "report";
  const isFinalReportStep = activeStep === "finalreport";

  return (
    <div className={`min-h-screen ${isDark ? "bg-black text-slate-100" : "bg-[#f6f3ee] text-slate-900"}`}>
      <header className={`no-print sticky top-0 z-30 border-b backdrop-blur ${isDark ? "border-neutral-800 bg-black/95" : "border-slate-200/70 bg-[#f6f3ee]/95"}`}>
        <div className="mx-auto max-w-7xl px-6 py-4 sm:px-8 lg:px-12">
          <div className="flex items-center justify-between gap-6">
            <div className="flex items-center gap-5">
              <BackBtn label="← Home" onClick={onBack} />
              <div>
                <p className={`text-xs uppercase tracking-[0.4em] ${isDark ? "text-emerald-300" : "text-emerald-700"}`}>Plan Mode</p>
                <p className={`text-sm mt-0.5 ${isDark ? "text-slate-300" : "text-slate-500"}`}>{PLAN_STEPS[stepIndex]?.label}</p>
              </div>
            </div>
            <div className="w-full max-w-xs">
              <div className="h-1.5 overflow-hidden rounded-full bg-slate-200">
                <div className="h-full rounded-full bg-emerald-600 transition-all duration-300" style={{ width: `${progress}%` }} />
              </div>
            </div>
          </div>
          <div className="mt-4 flex flex-wrap items-center gap-2">
            {PLAN_STEPS.map((s, idx) => (
              <button key={s.id} onClick={() => goTo(s.id)}
                className={classNames("rounded-full px-4 py-1.5 text-xs font-semibold uppercase tracking-[0.2em] transition",
                  activeStep === s.id
                    ? "bg-emerald-600 text-white"
                    : idx < stepIndex
                    ? isDark ? "bg-emerald-900/60 text-emerald-300 hover:bg-emerald-900" : "bg-emerald-50 text-emerald-700 hover:bg-emerald-100"
                    : isDark ? "bg-white/10 text-slate-300 hover:bg-white/20" : "bg-white text-slate-500 hover:bg-slate-50"
                )}>
                {idx + 1}. {s.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main>
        {isFinalReportStep ? (
          <FinalReportStep
            planResult={planResult}
            selectedTrail={selectedTrail}
            startDate={startDate}
            itineraryDays={itineraryDays}
            routePoints={routePoints}
            tripType={tripType}
            rawMiles={rawMiles}
            userWaterSpots={userWaterSpots}
            campPositions={campPositions}
            aiReport={aiReport}
          />
        ) : isReportStep ? (
        <ReportStep
          planResult={planResult}
          selectedTrail={selectedTrail}
          startDate={startDate}
          itineraryDays={itineraryDays}
          routePoints={routePoints}
          tripType={tripType}
          rawMiles={rawMiles}
          userWaterSpots={userWaterSpots}
          onAddWaterSpot={(spot) => setUserWaterSpots(prev => [...prev, spot])}
          onRemoveWaterSpot={handleRemoveWaterSpot}
          addWaterMode={addWaterMode}
          setAddWaterMode={setAddWaterMode}
          campPositions={campPositions}
          onCampPositionsChange={setCampPositions}
          isDark={isDark}
          onAiReport={setAiReport}
          onContinueToReport={() => goTo("finalreport")}
        />
        ) : (
          <div className="mx-auto max-w-7xl px-6 py-10 sm:px-8 lg:px-12 space-y-10">
            {activeStep === "upload" && (
              <UploadStep
                selectedFile={selectedFile} fileInputRef={fileInputRef}
                onFileChange={handleFileChange} onUpload={handleUpload}
                inputMode={inputMode} setInputMode={setInputMode}
                trailName={trailName} setTrailName={setTrailName}
                onNameSearch={handleNameSearch} onSuggestionSelect={handleNameSelect}
              />
            )}
            {activeStep === "upload" && nameSearchResults.length > 0 && (
              <div className="grid gap-4 md:grid-cols-3">
                {nameSearchResults.map((trail) => (
                  <div key={trail.id} onClick={() => handleNameSelect(trail)}
                    className="cursor-pointer rounded-2xl border border-slate-200 bg-white p-5 hover:border-emerald-300 transition">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Result</p>
                    <h4 className="mt-3 text-lg font-semibold text-slate-900">{trail.name}</h4>
                    <p className="text-slate-500">{trail.area} · {trail.length_miles} mi</p>
                  </div>
                ))}
              </div>
            )}
            {activeStep === "dates" && (
              <DatesStep startDate={startDate} onStartDateChange={setStartDate} endDate={endDate} onNext={handleDatesNext} />
            )}
            {activeStep === "match" && (
              <MatchStep
                route={gpxRoute} trailMatch={trailMatch}
                selectedTrailId={selectedTrailId} onTrailSelect={setSelectedTrailId}
                onNext={handleMatchNext} loading={loading}
                onUseCustomGpx={handleUseCustomGpx} hasCustomGpx={!!gpxRoute}
              />
            )}
            {activeStep === "itinerary" && (
              <ItineraryStep
                route={useCustomGpx ? gpxRoute : (gpxRoute || null)}
                selectedTrail={useCustomGpx ? null : selectedTrail}
                startDate={startDate}
                numDays={numDays} setNumDays={setNumDays}
                tripType={tripType} setTripType={setTripType}
                onNext={() => { goTo("checks"); runPreTripChecks(); }}
              />
            )}
            {activeStep === "checks" && (
              <ChecksStep checks={checks} checksLoading={checksLoading} onNext={handleChecksNext} />
            )}
          </div>
        )}
      </main>
    </div>
  );
}

// ─── Theme helpers ─────────────────────────────────────────────────────────────

const THEME_STORAGE_KEY = "backcountry-theme";

const getStoredTheme = () => {
  if (typeof window === "undefined") return null;
  return localStorage.getItem(THEME_STORAGE_KEY);
};

const getSystemTheme = () => {
  if (typeof window === "undefined") return "light";
  return window.matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light";
};

const resolveInitialTheme = () => {
  const stored = getStoredTheme();
  if (stored === "dark" || stored === "light") return stored;
  return getSystemTheme();
};

// ─── Root ─────────────────────────────────────────────────────────────────────

export default function App() {
  const [appMode, setAppMode] = useState("dashboard");
  const [theme, setTheme] = useState(resolveInitialTheme);
  const [manualTheme, setManualTheme] = useState(() => {
    const stored = getStoredTheme();
    return stored === "dark" || stored === "light";
  });

  const isDark = theme === "dark";

  useEffect(() => {
    if (typeof document === "undefined") return;
    document.documentElement.setAttribute("data-theme", theme);
    localStorage.setItem(THEME_STORAGE_KEY, theme);
  }, [theme]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    if (manualTheme) return;
    const media = window.matchMedia("(prefers-color-scheme: dark)");
    const handler = (event) => setTheme(event.matches ? "dark" : "light");
    if (media.addEventListener) media.addEventListener("change", handler);
    else media.addListener(handler);
    return () => {
      if (media.removeEventListener) media.removeEventListener("change", handler);
      else media.removeListener(handler);
    };
  }, [manualTheme]);

  const toggleTheme = () => {
    setTheme((prev) => (prev === "dark" ? "light" : "dark"));
    setManualTheme(true);
  };

  const modeView = appMode === "explore" ? (
    <ExploreView onBack={() => setAppMode("dashboard")} isDark={isDark} />
  ) : appMode === "plan" ? (
    <PlanView onBack={() => setAppMode("dashboard")} isDark={isDark} />
  ) : (
    <DashboardView onNewPlan={() => setAppMode("plan")} onExplore={() => setAppMode("explore")} isDark={isDark} />
  );

  return (
    <>
      {modeView}
      <button
        onClick={toggleTheme}
        className={`fixed bottom-6 right-6 z-50 flex items-center gap-2 rounded-full px-4 py-2 text-xs font-semibold shadow-2xl transition ${isDark ? "bg-emerald-400 text-slate-900 shadow-emerald-500/40" : "bg-white/90 text-slate-900 shadow-lg"}`}
        aria-label="Toggle dark mode"
      >
        {isDark ? "Light" : "Dark"} mode
      </button>
    </>
  );
}
