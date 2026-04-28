import { useState, useRef, useEffect } from "react";
import mapboxgl from "mapbox-gl";
import "./index.css";

// ─── Constants ────────────────────────────────────────────────────────────────

const PLAN_STEPS = [
  { id: "upload",    label: "Route" },
  { id: "match",     label: "Match" },
  { id: "dates",     label: "Dates" },
  { id: "itinerary", label: "Itinerary" },
  { id: "checks",    label: "Checks" },
  { id: "report",    label: "Report" },
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

// ─── Elevation Profile ────────────────────────────────────────────────────────

function ElevationProfile({ route }) {
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

  const W = 460; const H = 80;
  const padL = 4; const padR = 4; const padT = 6; const padB = 4;
  const chartW = W - padL - padR; const chartH = H - padT - padB;

  const xScale = (d) => padL + (d / totalMi) * chartW;
  const yScale = (e) => padT + chartH - ((e - minElev) / elevRange) * chartH;

  const linePoints = data.map((d) => `${xScale(d.dist).toFixed(1)},${yScale(d.elev).toFixed(1)}`).join(" L ");
  const areaPath = `M ${xScale(0)},${(padT + chartH).toFixed(1)} L ${linePoints} L ${xScale(totalMi)},${(padT + chartH).toFixed(1)} Z`;

  return (
    <div className="pointer-events-none absolute bottom-6 left-1/2 -translate-x-1/2 w-[500px] rounded-3xl border border-white/30 bg-white/90 p-4 shadow-2xl backdrop-blur z-10">
      <p className="mb-2 text-[10px] uppercase tracking-[0.35em] text-slate-500">Elevation Profile</p>
      <div className="relative">
        <svg viewBox={`0 0 ${W} ${H}`} className="w-full" style={{ height: 80 }} preserveAspectRatio="none">
          <defs>
            <linearGradient id="elev-grad" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor="#10b981" stopOpacity="0.35" />
              <stop offset="100%" stopColor="#10b981" stopOpacity="0.05" />
            </linearGradient>
          </defs>
          <path d={areaPath} fill="url(#elev-grad)" />
          <path d={`M ${linePoints}`} fill="none" stroke="#059669" strokeWidth="1.8" strokeLinejoin="round" strokeLinecap="round" />
        </svg>
        <span className="absolute left-1 top-0 text-[9px] font-medium leading-none text-slate-500">{Math.round(maxElev).toLocaleString()} ft</span>
        <span className="absolute bottom-0 left-1 text-[9px] font-medium leading-none text-slate-500">{Math.round(minElev).toLocaleString()} ft</span>
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
        <button onClick={() => setInputMode("gpx")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${isGpx ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Upload GPX</button>
        <button onClick={() => setInputMode("name")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${!isGpx ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>Name + Region</button>
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
  const [aiBriefing, setAiBriefing] = useState(null);
  const [aiLoading, setAiLoading] = useState(false);

  const rawMiles = parseFloat(route?.length_miles || route?.distance_mi || selectedTrail?.length_miles || 0);
  const effectiveMiles = tripType === "out-and-back" ? rawMiles * 2 : rawMiles;
  const milesPerDay = numDays > 0 && effectiveMiles > 0 ? effectiveMiles / numDays : 0;

  const days = Array.from({ length: numDays }, (_, i) => ({
    day: i + 1,
    startMile: +(i * milesPerDay).toFixed(1),
    endMile: +((i + 1) * milesPerDay).toFixed(1),
    miles: +milesPerDay.toFixed(1),
  }));

  const fetchAiBriefing = async () => {
    if (!selectedTrail && !route) return;
    setAiLoading(true);
    setAiBriefing(null);
    try {
      const res = await fetch("/api/plan/itinerary", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          trail_name: selectedTrail?.name || "Custom route",
          area: selectedTrail?.area || "",
          total_miles: effectiveMiles,
          trip_type: tripType,
          num_days: numDays,
          days,
          checks: {},
        }),
      });
      const data = await res.json();
      setAiBriefing(data);
    } catch { setAiBriefing({ error: "Could not reach AI" }); }
    setAiLoading(false);
  };

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

      {/* AI Briefing */}
      <div className="rounded-2xl border border-slate-200 bg-white p-6 shadow-sm">
        <div className="flex items-center justify-between mb-4">
          <p className="text-xs uppercase tracking-[0.2em] text-slate-500">AI trip briefing</p>
          <button onClick={fetchAiBriefing} disabled={aiLoading}
            className="rounded-full border border-emerald-300 px-4 py-1.5 text-xs font-semibold text-emerald-700 hover:bg-emerald-50 transition disabled:opacity-50">
            {aiLoading ? "Generating…" : aiBriefing ? "Regenerate" : "Generate"}
          </button>
        </div>
        {aiLoading && (
          <div className="flex items-center gap-2 text-sm text-slate-400"><Spinner /> Asking AI for a trip briefing…</div>
        )}
        {aiBriefing?.error && !aiLoading && (
          <p className="text-sm text-red-500">{aiBriefing.error}</p>
        )}
        {aiBriefing?.text && !aiLoading && (
          <p className="text-sm text-slate-700 leading-relaxed whitespace-pre-line">{aiBriefing.text}</p>
        )}
        {!aiBriefing && !aiLoading && (
          <p className="text-sm text-slate-400">Hit Generate for an AI day-by-day trail briefing.</p>
        )}
      </div>

      <p className="text-xs text-slate-400">Camp markers are draggable on the report map.</p>

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

function MapCanvas({ routeFeature, firePerimeters, snowGeojson, waterGeojson, fallbackCenter, report, checks, selectedTrail, route, exploreMode = false, itineraryDays, routePoints, tripType, rawMiles, userWaterSpots, onAddWaterSpot, addWaterMode, setAddWaterMode, heightClass, onCampPositionsChange }) {
  const mapContainerRef = useRef(null);
  const mapRef = useRef(null);
  const campMarkersRef = useRef([]);
  const userWaterMarkersRef = useRef([]);
  const fireCount = getFireCount(firePerimeters);
  const styleUrl = "mapbox://styles/mapbox/outdoors-v12";
  // Keep mutable values in refs so map callbacks don't go stale
  const addWaterModeRef = useRef(addWaterMode);
  const onAddWaterSpotRef = useRef(onAddWaterSpot);
  useEffect(() => { addWaterModeRef.current = addWaterMode; }, [addWaterMode]);
  useEffect(() => { onAddWaterSpotRef.current = onAddWaterSpot; }, [onAddWaterSpot]);

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
          new mapboxgl.Popup().setLngLat(e.lngLat).setHTML(`<strong>${name}</strong><br/>${p.recency_tag || ""} · ${p.days_since_update != null ? p.days_since_update + " days ago" : ""}`).addTo(map);
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
          new mapboxgl.Popup().setLngLat(e.lngLat).setHTML(`<strong>${p.name}</strong><br/>${p.water_type} · ${p.distance_mi} mi from trail`).addTo(map);
        });
      }

      // Water-spot click handler — uses refs so closure is never stale
      if (!exploreMode) {
        map.on("click", (e) => {
          if (addWaterModeRef.current && onAddWaterSpotRef.current) {
            onAddWaterSpotRef.current({ lat: e.lngLat.lat, lng: e.lngLat.lng, name: "Water source" });
          }
        });
      }
    });

    return () => { map.remove(); mapRef.current = null; };
  }, [styleUrl]);

  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    if (map.getSource("route") && routeFeature) map.getSource("route").setData(routeFeature);
    if (map.getSource("fire") && firePerimeters) map.getSource("fire").setData(firePerimeters);
    if (map.getSource("snow") && snowGeojson) map.getSource("snow").setData(snowGeojson);
    if (map.getSource("water") && waterGeojson) map.getSource("water").setData(waterGeojson);
  }, [routeFeature, firePerimeters, snowGeojson, waterGeojson, styleUrl]);

  // Draggable camp markers
  useEffect(() => {
    const map = mapRef.current;
    if (!map || !itineraryDays?.length || !routePoints?.length) return;
    campMarkersRef.current.forEach(m => m.remove());
    campMarkersRef.current = [];
    const camps = itineraryDays.slice(0, -1);
    // Compute actual one-way route length from geometry so mirroring is accurate
    let onewayLen = rawMiles || 0;
    if (tripType === "out-and-back" && routePoints.length > 1) {
      let d = 0;
      for (let i = 1; i < routePoints.length; i++)
        d += haversineMiles(routePoints[i-1].lat, routePoints[i-1].lng, routePoints[i].lat, routePoints[i].lng);
      onewayLen = d;
    }
    const addMarkers = () => {
      camps.forEach((day) => {
        let targetMile = day.endMile;
        // For out-and-back, mirror return-leg positions back onto the outbound route
        if (tripType === "out-and-back" && onewayLen > 0 && targetMile > onewayLen) {
          targetMile = 2 * onewayLen - targetMile;
        }
        const pt = getPointAtMile(routePoints, Math.max(0, targetMile));
        if (!pt) return;
        const el = document.createElement("div");
        el.style.cssText = "width:26px;height:26px;background:#10b981;border:2.5px solid #065f46;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:11px;font-weight:700;color:white;cursor:grab;box-shadow:0 2px 8px rgba(0,0,0,0.3);user-select:none;";
        el.textContent = day.day;
        const popup = new mapboxgl.Popup({ offset: 20, closeButton: false }).setHTML(
          `<div style="font-size:12px"><strong>Camp — Day ${day.day}</strong><br/>Mile ${day.endMile} · ${day.miles} mi today</div>`
        );
        const marker = new mapboxgl.Marker({ element: el, draggable: true }).setLngLat([pt.lng, pt.lat]).setPopup(popup).addTo(map);
        el.addEventListener("mouseenter", () => marker.togglePopup());
        el.addEventListener("mouseleave", () => { if (marker.getPopup().isOpen()) marker.togglePopup(); });
        campMarkersRef.current.push(marker);
      });
    };
    if (map.isStyleLoaded()) addMarkers(); else map.once("load", addMarkers);
    return () => { campMarkersRef.current.forEach(m => m.remove()); campMarkersRef.current = []; map.off("load", addMarkers); };
  }, [itineraryDays, routePoints]);

  // User-added water spots
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    userWaterMarkersRef.current.forEach(m => m.remove());
    userWaterMarkersRef.current = [];
    const addUserMarkers = () => {
      (userWaterSpots || []).forEach((spot) => {
        const el = document.createElement("div");
        el.style.cssText = "width:24px;height:24px;background:#0891b2;border:2px solid #0e7490;border-radius:50%;display:flex;align-items:center;justify-content:center;font-size:13px;cursor:pointer;box-shadow:0 1px 5px rgba(0,0,0,0.25);";
        el.textContent = "💧";
        const popup = new mapboxgl.Popup({ offset: 20, closeButton: false }).setHTML(
          `<div style="font-size:12px"><strong>${spot.name || "Water source"}</strong><br/><em>User-added</em></div>`
        );
        const marker = new mapboxgl.Marker({ element: el }).setLngLat([spot.lng, spot.lat]).setPopup(popup).addTo(map);
        el.addEventListener("mouseenter", () => marker.togglePopup());
        el.addEventListener("mouseleave", () => { if (marker.getPopup().isOpen()) marker.togglePopup(); });
        userWaterMarkersRef.current.push(marker);
      });
    };
    if (map.isStyleLoaded()) addUserMarkers(); else map.once("load", addUserMarkers);
    return () => { userWaterMarkersRef.current.forEach(m => m.remove()); userWaterMarkersRef.current = []; map.off("load", addUserMarkers); };
  }, [userWaterSpots]);

  // Crosshair cursor in add-water mode
  useEffect(() => {
    const map = mapRef.current;
    if (!map) return;
    map.getCanvas().style.cursor = addWaterMode ? "crosshair" : "";
  }, [addWaterMode]);

  return (
    <div className={`relative ${heightClass || "h-[calc(100vh-9.25rem)]"} min-h-[36rem] overflow-hidden bg-slate-900`}>
      {import.meta.env.VITE_MAPBOX_TOKEN ? (
        <div ref={mapContainerRef} className="absolute inset-0 h-full w-full" />
      ) : (
        <div className="absolute inset-0 flex items-center justify-center bg-slate-100 text-slate-400">Mapbox token missing</div>
      )}

      {/* Route overview card */}
      <div className="pointer-events-none absolute left-6 top-6 max-w-xs rounded-3xl border border-white/30 bg-white/90 p-5 shadow-2xl backdrop-blur">
        <p className="text-xs uppercase tracking-[0.3em] text-slate-500">{exploreMode ? "Explore" : "Route Overview"}</p>
        <h3 className="mt-2 text-xl font-semibold text-slate-950">{selectedTrail?.name || "3D trip map"}</h3>
        <p className="mt-1 text-xs text-slate-500">{selectedTrail?.area || ""}</p>
        <div className={`mt-4 grid gap-3 text-sm ${exploreMode ? "grid-cols-2" : "grid-cols-2"}`}>
          <div className="rounded-2xl bg-emerald-50 p-3">
            <p className="text-[10px] uppercase tracking-[0.2em] text-emerald-700">Route</p>
            <p className="mt-1 font-semibold text-emerald-950">{routeFeature ? "Loaded" : "Point only"}</p>
          </div>
          {!exploreMode && (
            <div className="rounded-2xl bg-orange-50 p-3">
              <p className="text-[10px] uppercase tracking-[0.2em] text-orange-700">Fire</p>
              <p className="mt-1 font-semibold text-orange-950">{fireCount} areas</p>
            </div>
          )}
          {!exploreMode && (
            <div className="rounded-2xl bg-sky-50 p-3">
              <p className="text-[10px] uppercase tracking-[0.2em] text-sky-700">Snow</p>
              <p className="mt-1 font-semibold text-sky-950">{checks?.snow?.max_depth_in ?? "--"} in</p>
            </div>
          )}
          {!exploreMode && (
            <div className="rounded-2xl bg-cyan-50 p-3">
              <p className="text-[10px] uppercase tracking-[0.2em] text-cyan-700">Water</p>
              <p className="mt-1 font-semibold text-cyan-950">{checks?.water?.count ?? "--"} sources</p>
            </div>
          )}
          <div className="rounded-2xl bg-slate-50 p-3">
            <p className="text-[10px] uppercase tracking-[0.2em] text-slate-500">Distance</p>
            <p className="mt-1 font-semibold text-slate-900">{route?.distance_mi?.toFixed?.(1) || selectedTrail?.length_miles || "--"} mi</p>
          </div>
        </div>
      </div>

      {/* Legend */}
      <div className="pointer-events-none absolute bottom-6 left-6 rounded-2xl border border-white/30 bg-white/90 p-4 text-sm shadow-2xl backdrop-blur">
        <div className="flex items-center gap-3">
          <span className="h-1.5 w-10 rounded-full bg-teal-900 shadow-[0_0_0_6px_rgba(20,184,166,0.25)]" />
          <span className="font-medium text-slate-800">GPX/trail route</span>
        </div>
        {!exploreMode && (
          <>
            <div className="mt-3 flex items-center gap-3">
              <span className="h-4 w-10 rounded bg-orange-500/30 ring-2 ring-orange-700" />
              <span className="font-medium text-slate-800">Fire perimeter</span>
            </div>
            <div className="mt-3 flex items-center gap-3">
              <span className="h-4 w-4 rounded-full border-2 border-blue-500 bg-blue-200" />
              <span className="font-medium text-slate-800">Snow depth</span>
            </div>
            <div className="mt-3 flex items-center gap-3">
              <span className="h-4 w-4 rounded-full border-2 border-cyan-600 bg-cyan-200" />
              <span className="font-medium text-slate-800">Water source</span>
            </div>
          </>
        )}
      </div>

      {/* Briefing card (plan mode only) */}
      {!exploreMode && report?.bullets?.length ? (
        <div className="pointer-events-none absolute right-6 top-6 max-w-sm rounded-3xl border border-white/30 bg-white/90 p-5 shadow-2xl backdrop-blur">
          <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Briefing</p>
          <ul className="mt-4 space-y-3 text-sm text-slate-700">
            {report.bullets.slice(0, 4).map((b, i) => (
              <li key={i} className="flex gap-3">
                <span className="mt-1.5 h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
                <span>{b}</span>
              </li>
            ))}
          </ul>
        </div>
      ) : !exploreMode && checks ? (
        <div className="pointer-events-none absolute right-6 top-6 max-w-sm rounded-3xl border border-white/30 bg-white/90 p-5 shadow-2xl backdrop-blur">
          <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Telemetry</p>
          <div className="mt-4 space-y-2 text-sm text-slate-700">
            <p>Weather: {checks.weather?.forecast?.[0]?.short || "Loaded"}</p>
            <p>AQI: {checks.aqi?.observations?.[0]?.aqi ?? "—"}</p>
            <p>Snow: {checks.snow?.message || "Loaded"}</p>
          </div>
        </div>
      ) : null}

      {/* Add water source button (plan mode only) */}
      {!exploreMode && onAddWaterSpot && (
        <div className="pointer-events-auto absolute bottom-6 right-6 flex flex-col items-end gap-1">
          <button onClick={() => setAddWaterMode?.(m => !m)}
            className={`rounded-full px-4 py-2 text-xs font-semibold shadow-lg transition backdrop-blur ${addWaterMode ? "bg-cyan-600 text-white" : "bg-white/90 text-cyan-700 border border-cyan-200 hover:bg-cyan-50"}`}>
            {addWaterMode ? "Click map to place · tap again to stop" : "+ Add water source"}
          </button>
          {(userWaterSpots?.length || 0) > 0 && !addWaterMode && (
            <span className="text-[10px] text-white/70">{userWaterSpots.length} user-added</span>
          )}
        </div>
      )}

      <ElevationProfile route={route} />
    </div>
  );
}

function ReportStep({ planResult, selectedTrail, itineraryDays, routePoints, tripType, rawMiles, userWaterSpots, onAddWaterSpot, addWaterMode, setAddWaterMode, isDark }) {
  const route = planResult?.route;
  const mapLayers = planResult?.map_layers;
  const checks = planResult?.checks;
  const report = planResult?.report;
  const firePerimeters = mapLayers?.fire_perimeters || checks?.fire?.perimeters || null;
  const snowGeojson = checks?.snow?.geojson || null;
  const waterGeojson = checks?.water?.geojson || null;
  const fallbackCenter = selectedTrail ? [selectedTrail.lng, selectedTrail.lat] : null;

  // Build route feature; for out-and-back with dense GPS data, append reversed coords
  let routeFeature = routeToFeature(route) || toLineFeature(mapLayers?.route) || toLineFeature(selectedTrail?.geometry);
  if (tripType === "out-and-back" && routeFeature?.geometry?.coordinates?.length >= 10) {
    const fwd = routeFeature.geometry.coordinates;
    routeFeature = { ...routeFeature, geometry: { type: "LineString", coordinates: [...fwd, ...[...fwd].reverse().slice(1)] } };
  }
  const daytimePeriods = getDaytimePeriods(checks?.weather?.forecast);
  const risk = planResult?.risk || computeRisk(checks || {});

  const riskColor = risk.status === "no-go" ? { bg: "bg-red-50 border-red-200", text: "text-red-700", label: "No-Go" }
    : risk.status === "caution" ? { bg: "bg-amber-50 border-amber-200", text: "text-amber-700", label: "Caution" }
    : { bg: "bg-emerald-50 border-emerald-200", text: "text-emerald-700", label: "Good to Go" };

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
        addWaterMode={addWaterMode}
        setAddWaterMode={setAddWaterMode}
        heightClass="h-[58vh]"
      />

      <div className="bg-[#f6f3ee] border-t border-slate-200">
        <div className="mx-auto max-w-7xl px-6 py-10 sm:px-8 lg:px-12 space-y-10">

          {/* Trip status */}
          <div className={`rounded-2xl border p-5 ${riskColor.bg}`}>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Trip status</p>
            <p className={`mt-1 text-2xl font-bold ${riskColor.text}`}>{riskColor.label}</p>
            {risk.reasons?.length > 0 && (
              <ul className="mt-3 space-y-1">
                {risk.reasons.map((r, i) => <li key={i} className="text-sm text-slate-700 flex gap-2"><span className="mt-0.5 h-1.5 w-1.5 shrink-0 rounded-full bg-current inline-block" />{r}</li>)}
              </ul>
            )}
          </div>

          {/* Per-day forecast */}
          {itineraryDays?.length > 0 && (
            <div>
              <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-5">Day-by-day forecast</p>
              <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-3">
                {itineraryDays.map((day, i) => {
                  const period = daytimePeriods[i];
                  const wText = (period?.short || "").toLowerCase();
                  const isWarn = /thunder|severe/.test(wText);
                  const isCaution = /shower|rain|drizzle|snow/.test(wText);
                  return (
                    <div key={day.day} className="rounded-2xl bg-white p-5 shadow-sm">
                      <div className="flex items-start justify-between">
                        <div>
                          <p className="text-xs uppercase tracking-[0.2em] text-slate-400">Day {day.day}</p>
                          <p className="mt-0.5 text-xs text-slate-400">{period?.name || "—"}</p>
                        </div>
                        {(isWarn || isCaution) && (
                          <span className={`rounded-full px-2 py-0.5 text-[10px] font-semibold ${isWarn ? "bg-amber-100 text-amber-700" : "bg-yellow-100 text-yellow-700"}`}>
                            {isWarn ? "Storm" : "Rain"}
                          </span>
                        )}
                      </div>
                      <p className="mt-3 font-semibold text-slate-900 text-sm">{period?.short || "No forecast available"}</p>
                      {period && <p className="mt-1 text-xs text-slate-500">{period.temp}{period.temp_unit} · {period.wind}</p>}
                      <div className="mt-3 pt-3 border-t border-slate-100 text-xs text-slate-400">
                        Mile {day.startMile} → {day.endMile} · {day.miles} mi
                      </div>
                    </div>
                  );
                })}
              </div>
            </div>
          )}

          {/* Conditions summary */}
          <div>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500 mb-5">Conditions summary</p>
            <div className="grid gap-4 sm:grid-cols-2 lg:grid-cols-4">
              {[
                { label: "Fire areas", value: String(checks?.fire?.perimeters?.features?.length || 0), sub: "in region", warn: (checks?.fire?.perimeters?.features?.length || 0) > 0 },
                { label: "Snow depth", value: `${checks?.snow?.max_depth_in ?? "--"} in`, sub: "at highest point", warn: (checks?.snow?.max_depth_in || 0) > 0 },
                { label: "AQI", value: String(checks?.aqi?.observations?.[0]?.aqi ?? "--"), sub: checks?.aqi?.observations?.[0]?.category || "", warn: (checks?.aqi?.observations?.[0]?.aqi || 0) >= 100 },
                { label: "Water sources", value: String(checks?.water?.count ?? "--"), sub: checks?.water?.nearest_mi != null ? `nearest ${checks.water.nearest_mi} mi` : "", warn: (checks?.water?.count ?? 1) === 0 },
              ].map(c => (
                <div key={c.label} className={`rounded-2xl p-5 shadow-sm ${c.warn ? "bg-amber-50" : "bg-white"}`}>
                  <p className="text-xs uppercase tracking-[0.2em] text-slate-400">{c.label}</p>
                  <p className={`mt-1 text-2xl font-bold ${c.warn ? "text-amber-800" : "text-slate-900"}`}>{c.value}</p>
                  <p className="mt-0.5 text-xs text-slate-500">{c.sub}</p>
                </div>
              ))}
            </div>
          </div>

        </div>
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
  const [inputMode, setInputMode] = useState("gpx");
  const [trailName, setTrailName] = useState("");
  const [nameSearchResults, setNameSearchResults] = useState([]);
  const [selectedFile, setSelectedFile] = useState(null);
  const [userWaterSpots, setUserWaterSpots] = useState([]);
  const [addWaterMode, setAddWaterMode] = useState(false);
  const fileInputRef = useRef(null);

  // Derived values
  const endDate = addDays(startDate, numDays);
  const rawMiles = parseFloat(gpxRoute?.length_miles || gpxRoute?.distance_mi || selectedTrail?.length_miles || 0);
  const effectiveMiles = tripType === "out-and-back" ? rawMiles * 2 : rawMiles;
  const milesPerDay = numDays > 0 && effectiveMiles > 0 ? effectiveMiles / numDays : 0;
  const itineraryDays = Array.from({ length: numDays }, (_, i) => ({
    day: i + 1, startMile: +(i * milesPerDay).toFixed(1), endMile: +((i + 1) * milesPerDay).toFixed(1), miles: +milesPerDay.toFixed(1),
  }));
  const routePoints = gpxRoute?.points || (selectedTrail?.geometry?.coordinates || []).map(c => ({ lat: c[1], lng: c[0] }));

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

  return (
    <div className={`min-h-screen ${isDark ? "bg-black text-slate-100" : "bg-[#f6f3ee] text-slate-900"}`}>
      <header className={`sticky top-0 z-30 border-b backdrop-blur ${isDark ? "border-neutral-800 bg-black/95" : "border-slate-200/70 bg-[#f6f3ee]/95"}`}>
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
                  activeStep === s.id ? "bg-emerald-600 text-white" : idx < stepIndex ? "bg-emerald-50 text-emerald-700 hover:bg-emerald-100" : "bg-white text-slate-500 hover:bg-slate-50")}>
                {idx + 1}. {s.label}
              </button>
            ))}
          </div>
        </div>
      </header>

      <main>
        {isReportStep ? (
        <ReportStep
          planResult={planResult}
          selectedTrail={selectedTrail}
          itineraryDays={itineraryDays}
          routePoints={routePoints}
          tripType={tripType}
          rawMiles={rawMiles}
          userWaterSpots={userWaterSpots}
          onAddWaterSpot={(spot) => setUserWaterSpots(prev => [...prev, spot])}
          addWaterMode={addWaterMode}
          setAddWaterMode={setAddWaterMode}
          isDark={isDark}
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
