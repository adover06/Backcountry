import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { Link } from "react-router-dom";
import { authedFetch } from "../api/client";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

// California, as an opening view.
const INITIAL_VIEW = { center: [-119.4179, 37.7783], zoom: 5.4 };

const LIGHT_STYLE = "mapbox://styles/mapbox/outdoors-v12";
const DARK_STYLE = "mapbox://styles/mapbox/dark-v11";

// Basemaps. Satellite + the DEM gives the Google-Earth read: real tree cover, rock,
// snow and drainages with the terrain actually raised, which is far more useful for
// judging a route than a topo tint.
const BASEMAPS = {
  terrain: { label: "Terrain", light: LIGHT_STYLE, dark: DARK_STYLE },
  satellite: {
    label: "Satellite",
    light: "mapbox://styles/mapbox/satellite-streets-v12",
    dark: "mapbox://styles/mapbox/satellite-streets-v12",
  },
};

// ── Shareable URL state ──────────────────────────────────────────────────────
//
// Everything that defines what you are looking at goes in the query string, so a
// link reproduces the view: the open trail, the filters, where the map is pointing,
// which basemap, and whether it is tilted. Written with replaceState so panning
// does not fill the back button with history.

const DEFAULT_FILTERS = {
  q: "",
  lengthMin: "",
  lengthMax: "",
  gainMin: "",
  gainMax: "",
  difficulty: [],
  steepness: [],
  features: [],
  featuresMode: "any",
  routeType: "",
  wildernessArea: "",
  // Defaults to hiking: this is a hiking and backpacking app, and without it a
  // search for a hike returns jeep roads. Visible as a selected chip, one click
  // to clear, so nothing is filtered silently.
  activity: "hiking",
  month: "",
  sort: "relevance",
};

function readUrlState() {
  if (typeof window === "undefined") return {};
  const p = new URLSearchParams(window.location.search);
  const list = (key) => (p.get(key) ? p.get(key).split(",").filter(Boolean) : []);
  const view = p.get("c")?.split(",").map(Number);

  return {
    trailId: p.get("trail") || null,
    basemap: BASEMAPS[p.get("map")] ? p.get("map") : null,
    pitched: p.get("3d") === "1",
    view:
      view && view.length === 2 && view.every(Number.isFinite)
        ? { center: view, zoom: Number(p.get("z")) || 11, pitch: Number(p.get("p")) || 0, bearing: Number(p.get("b")) || 0 }
        : null,
    filters: {
      ...DEFAULT_FILTERS,
      q: p.get("q") || "",
      lengthMin: p.get("lmin") || "",
      lengthMax: p.get("lmax") || "",
      gainMin: p.get("gmin") || "",
      gainMax: p.get("gmax") || "",
      difficulty: list("diff"),
      steepness: list("steep"),
      features: list("feat"),
      featuresMode: p.get("fmode") === "all" ? "all" : "any",
      routeType: p.get("rt") || "",
      wildernessArea: p.get("wa") || "",
      activity: p.has("act") ? p.get("act") : "hiking",
      month: p.get("mo") || "",
      sort: p.get("sort") || "relevance",
    },
  };
}

function writeUrlState({ filters, selectedId, map, basemap, pitched }) {
  if (typeof window === "undefined") return;
  const p = new URLSearchParams();

  if (selectedId) p.set("trail", selectedId);
  if (filters.q) p.set("q", filters.q);
  if (filters.lengthMin) p.set("lmin", filters.lengthMin);
  if (filters.lengthMax) p.set("lmax", filters.lengthMax);
  if (filters.gainMin) p.set("gmin", filters.gainMin);
  if (filters.gainMax) p.set("gmax", filters.gainMax);
  if (filters.difficulty.length) p.set("diff", filters.difficulty.join(","));
  if (filters.steepness.length) p.set("steep", filters.steepness.join(","));
  if (filters.features.length) {
    p.set("feat", filters.features.join(","));
    if (filters.featuresMode === "all") p.set("fmode", "all");
  }
  if (filters.routeType) p.set("rt", filters.routeType);
  if (filters.wildernessArea) p.set("wa", filters.wildernessArea);
  if (filters.activity !== "hiking") p.set("act", filters.activity);
  if (filters.month) p.set("mo", String(filters.month));
  if (filters.sort && filters.sort !== "relevance") p.set("sort", filters.sort);
  if (basemap && basemap !== "terrain") p.set("map", basemap);
  if (pitched) p.set("3d", "1");

  if (map) {
    const c = map.getCenter();
    p.set("c", `${c.lng.toFixed(5)},${c.lat.toFixed(5)}`);
    p.set("z", map.getZoom().toFixed(2));
    const pitch = map.getPitch();
    const bearing = map.getBearing();
    if (pitch > 1) p.set("p", pitch.toFixed(0));
    if (Math.abs(bearing) > 1) p.set("b", bearing.toFixed(0));
  }

  const next = `${window.location.pathname}?${p.toString()}`;
  if (next !== window.location.pathname + window.location.search) {
    window.history.replaceState(null, "", next);
  }
}

function initialBasemap() {
  try {
    const saved = localStorage.getItem("opentrails-basemap");
    if (saved && BASEMAPS[saved]) return saved;
  } catch {
    // storage unavailable
  }
  return "terrain";
}

const URL_STATE = typeof window !== "undefined" ? readUrlState() : {};

function initialTheme() {
  try {
    const saved = localStorage.getItem("opentrails-theme");
    if (saved === "dark" || saved === "light") return saved;
  } catch {
    // private mode / blocked storage — fall through to the OS preference
  }
  return window.matchMedia?.("(prefers-color-scheme: dark)").matches ? "dark" : "light";
}

// Read from CSS so light and dark share one source of truth and the map lines
// always match the chips. Re-read whenever the theme flips.
// Activity is partly published and partly derived, so each chip says what it means.
// "Hiking" is not "hiking is permitted" — that is true of every trail carrying use
// data — it is "no motors allowed", which is what separates a trail from a road.
const ACTIVITIES = [
  ["hiking", "Hiking", "Foot trails — excludes routes open to 4WD, ATV or motorcycles"],
  ["backpacking", "Backpacking", "In designated wilderness, or 8+ miles — derived, not a published field"],
  ["bike", "Biking", "Bikes permitted (USFS allowed-use)"],
  ["horse", "Horse", "Horses permitted (USFS allowed-use)"],
  ["motorized", "Motorized", "Jeep, ATV and OHV routes — hidden from the other categories"],
];

const DIFFICULTY_VARS = {
  easy: "--d-easy",
  moderate: "--d-moderate",
  hard: "--d-hard",
  strenuous: "--d-strenuous",
  "very strenuous": "--d-very",
};

/** Mapbox `match` expression colouring each line by its difficulty band. */
function difficultyColorExpression(palette) {
  return [
    "match",
    ["get", "difficulty"],
    "easy", palette.easy,
    "moderate", palette.moderate,
    "hard", palette.hard,
    "strenuous", palette.strenuous,
    "very strenuous", palette["very strenuous"],
    palette.unknown,
  ];
}

function readPalette() {
  if (typeof window === "undefined") return { easy: "#16a34a", unknown: "#94a3b8" };
  const cs = getComputedStyle(document.documentElement);
  const out = {};
  for (const [key, cssVar] of Object.entries(DIFFICULTY_VARS)) {
    out[key] = cs.getPropertyValue(cssVar).trim() || "#888";
  }
  out.unknown = cs.getPropertyValue("--d-unknown").trim() || "#94a3b8";
  return out;
}

const DIFFICULTIES = ["easy", "moderate", "hard", "strenuous", "very strenuous"];

// Steepness is a separate axis from effort. Human difficulty ratings correlate with
// climb-per-mile (r=+0.57) far more than with total effort (r=+0.34), so both are
// offered rather than collapsed into one label.
const STEEPNESS = ["gentle", "moderate", "steep", "very steep"];

const FEATURE_LABELS = {
  lake: "Lake",
  waterfall: "Waterfall",
  peak: "Summit",
  viewpoint: "Viewpoint",
  hot_spring: "Hot spring",
  spring: "Spring",
  cave: "Cave",
  arch: "Arch",
  glacier: "Glacier",
  // GNIS landform classes. These are already in the index and were rendering as
  // raw slugs ("hot_spring" reads fine, "pillar" does not).
  pass: "Pass",
  ridge: "Ridge",
  basin: "Basin",
  island: "Island",
  beach: "Beach",
  bay: "Bay",
  pillar: "Rock pillar",
  cliff: "Cliff",
  marsh: "Marsh",
};

// Access facilities worth a row in the detail panel, most trip-changing first.
// `trailhead` is rendered separately above, since it is the one everybody needs.
const ACCESS_ROWS = [
  ["water", "Drinking water"],
  ["campground", "Campground"],
  ["backcountry_camp", "Backcountry camping"],
  ["shelter", "Shelter"],
  ["visitor_center", "Visitor center"],
];

// Below this zoom the map draws one dot per trail instead of full lines. Measured
// against the live API on a dense Sierra viewport: lines are 7,901,299 bytes in
// 5.47s, dots are 73,453 bytes in 0.021s — 108x smaller and 260x faster. Lines only
// become worth their cost once you are zoomed in far enough to read trail shape.
const DOT_ZOOM_MAX = 11;

const MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"];

// Distinct hues for routes held in the comparison tray.
const COMPARE_COLORS = ["#7c3aed", "#0891b2", "#db2777", "#65a30d"];
const MAX_COMPARE = 4;

/* Inline Lucide-style icons. Emoji are font-dependent and render inconsistently
   across platforms, so structural UI never uses them. */
const iconBase = {
  width: 16,
  height: 16,
  viewBox: "0 0 24 24",
  fill: "none",
  stroke: "currentColor",
  strokeWidth: 2,
  strokeLinecap: "round",
  strokeLinejoin: "round",
  "aria-hidden": true,
};

const SunIcon = (p) => (
  <svg {...iconBase} {...p}>
    <circle cx="12" cy="12" r="4" />
    <path d="M12 2v2M12 20v2M4.93 4.93l1.41 1.41M17.66 17.66l1.41 1.41M2 12h2M20 12h2M6.34 17.66l-1.41 1.41M19.07 4.93l-1.41 1.41" />
  </svg>
);
const MoonIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z" />
  </svg>
);
const PlusIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M12 5v14M5 12h14" />
  </svg>
);
const CheckIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M20 6 9 17l-5-5" />
  </svg>
);
const CloseIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M18 6 6 18M6 6l12 12" />
  </svg>
);
const SearchIcon = (p) => (
  <svg {...iconBase} {...p}>
    <circle cx="11" cy="11" r="7" />
    <path d="m20 20-3.5-3.5" />
  </svg>
);

function classNames(...parts) {
  return parts.filter(Boolean).join(" ");
}

function haversineMi(a, b) {
  const R = 3958.8;
  const dLat = ((b[1] - a[1]) * Math.PI) / 180;
  const dLng = ((b[0] - a[0]) * Math.PI) / 180;
  const la1 = (a[1] * Math.PI) / 180;
  const la2 = (b[1] * Math.PI) / 180;
  const h = Math.sin(dLat / 2) ** 2 + Math.cos(la1) * Math.cos(la2) * Math.sin(dLng / 2) ** 2;
  return 2 * R * Math.asin(Math.sqrt(Math.min(1, h)));
}

function flattenGeometry(geometry) {
  if (!geometry) return [];
  if (geometry.type === "LineString") return geometry.coordinates || [];
  if (geometry.type === "MultiLineString") return (geometry.coordinates || []).flat();
  return [];
}

/** Coordinate at `targetMi` measured along the trail — drives the map scrubber. */
function pointAtMile(coords, targetMi) {
  if (!coords || coords.length < 2) return null;
  if (targetMi <= 0) return coords[0];
  let travelled = 0;
  for (let i = 1; i < coords.length; i += 1) {
    const step = haversineMi(coords[i - 1], coords[i]);
    if (travelled + step >= targetMi) {
      const t = step > 0 ? (targetMi - travelled) / step : 0;
      return [
        coords[i - 1][0] + t * (coords[i][0] - coords[i - 1][0]),
        coords[i - 1][1] + t * (coords[i][1] - coords[i - 1][1]),
      ];
    }
    travelled += step;
  }
  return coords[coords.length - 1];
}

/** Build the query string shared by /search and /map. */
function buildParams(filters, bbox) {
  const params = new URLSearchParams();
  if (bbox) params.set("bbox", bbox.map((n) => n.toFixed(4)).join(","));
  if (filters.q) params.set("q", filters.q);
  if (filters.lengthMin || filters.lengthMax)
    params.set("length", `${filters.lengthMin || ""},${filters.lengthMax || ""}`);
  if (filters.gainMin || filters.gainMax)
    params.set("gain", `${filters.gainMin || ""},${filters.gainMax || ""}`);
  if (filters.difficulty.length) params.set("difficulty", filters.difficulty.join(","));
  if (filters.steepness.length) params.set("steepness", filters.steepness.join(","));
  if (filters.features.length) {
    params.set("features", filters.features.join(","));
    params.set("features_mode", filters.featuresMode);
  }
  if (filters.routeType) params.set("route_type", filters.routeType);
  if (filters.wildernessArea) params.set("wilderness_area", filters.wildernessArea);
  if (filters.activity) params.set("activity", filters.activity);
  if (filters.month) params.set("month", String(filters.month));
  if (filters.sort) params.set("sort", filters.sort);
  return params;
}

// ── Elevation profile ────────────────────────────────────────────────────────

function ElevationProfile({ profile, gain, loss, onScrub }) {
  const svgRef = useRef(null);
  const [cursor, setCursor] = useState(null);

  if (!profile?.length) {
    return (
      <p className="text-xs text-[var(--fg-3)] py-6 text-center">
        No elevation profile available for this trail.
      </p>
    );
  }

  const width = 640;
  const height = 150;
  const pad = { top: 12, right: 8, bottom: 22, left: 42 };

  const maxX = Math.max(...profile.map((p) => p.mi)) || 1;
  const ys = profile.map((p) => p.ft);
  const minY = Math.min(...ys);
  const maxY = Math.max(...ys);
  const spanY = Math.max(1, maxY - minY);

  const px = (mi) => pad.left + (mi / maxX) * (width - pad.left - pad.right);
  const py = (ft) => pad.top + (1 - (ft - minY) / spanY) * (height - pad.top - pad.bottom);

  const area =
    profile.map((p, i) => `${i ? "L" : "M"}${px(p.mi).toFixed(1)},${py(p.ft).toFixed(1)}`).join("") +
    `L${px(maxX).toFixed(1)},${py(minY).toFixed(1)}L${px(0).toFixed(1)},${py(minY).toFixed(1)}Z`;

  // Colour each step by its grade, so the steep pitches are visible at a glance
  // instead of being averaged into one line.
  const segments = profile.slice(1).map((p, i) => {
    const prev = profile[i];
    const rise = p.ft - prev.ft;
    const run = Math.max(1e-4, p.mi - prev.mi) * 5280;
    const grade = (rise / run) * 100;
    const color =
      grade >= 20 ? "#dc2626"
      : grade >= 12 ? "#ea580c"
      : grade >= 6 ? "#ca8a04"
      : grade <= -6 ? "#0284c7"
      : "#059669";
    return { d: `M${px(prev.mi)},${py(prev.ft)}L${px(p.mi)},${py(p.ft)}`, color };
  });

  const handleMove = (event) => {
    const svg = svgRef.current;
    if (!svg) return;
    const rect = svg.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / rect.width;
    const xInSvg = ratio * width;
    const mi = Math.max(0, Math.min(maxX, ((xInSvg - pad.left) / (width - pad.left - pad.right)) * maxX));
    // Snap to the nearest sampled point so the readout is real data, not interpolation.
    let nearest = profile[0];
    for (const point of profile) {
      if (Math.abs(point.mi - mi) < Math.abs(nearest.mi - mi)) nearest = point;
    }
    setCursor(nearest);
    onScrub?.(nearest.mi);
  };

  const handleLeave = () => {
    setCursor(null);
    onScrub?.(null);
  };

  return (
    <div>
      <div className="flex items-baseline gap-3 mb-1 text-xs">
        <span className="font-semibold text-[var(--accent)]">↑ {gain?.toLocaleString()} ft</span>
        {loss != null && <span className="text-[var(--fg-2)]">↓ {loss.toLocaleString()} ft</span>}
        <span className="text-[var(--fg-3)]">{minY.toLocaleString()}–{maxY.toLocaleString()} ft</span>
        <span className="ml-auto font-medium text-[var(--fg)] tabular-nums">
          {cursor ? `${cursor.mi.toFixed(2)} mi · ${cursor.ft.toLocaleString()} ft` : "hover to trace"}
        </span>
      </div>

      <svg
        ref={svgRef}
        viewBox={`0 0 ${width} ${height}`}
        className="w-full cursor-crosshair touch-none"
        onMouseMove={handleMove}
        onMouseLeave={handleLeave}
        role="img"
        aria-label="Elevation profile — hover to trace the route on the map"
      >
        <defs>
          <linearGradient id="elev-fill" x1="0" y1="0" x2="0" y2="1">
            <stop offset="0%" stopColor="var(--accent)" stopOpacity="0.28" />
            <stop offset="100%" stopColor="var(--accent)" stopOpacity="0.02" />
          </linearGradient>
        </defs>

        {[0, 0.5, 1].map((t) => {
          const ft = minY + t * spanY;
          return (
            <g key={t}>
              <line x1={pad.left} y1={py(ft)} x2={width - pad.right} y2={py(ft)} stroke="var(--line)" strokeWidth="1" />
              <text x={pad.left - 6} y={py(ft) + 3} textAnchor="end" className="fill-[var(--fg-3)]" fontSize="9">
                {Math.round(ft).toLocaleString()}
              </text>
            </g>
          );
        })}

        <path d={area} fill="url(#elev-fill)" />
        {segments.map((seg, i) => (
          <path key={i} d={seg.d} fill="none" stroke={seg.color} strokeWidth="2" strokeLinecap="round" />
        ))}

        {cursor && (
          <g>
            <line x1={px(cursor.mi)} y1={pad.top} x2={px(cursor.mi)} y2={height - pad.bottom} stroke="var(--fg)" strokeWidth="1" strokeDasharray="3 3" />
            <circle cx={px(cursor.mi)} cy={py(cursor.ft)} r="4.5" fill="var(--fg)" stroke="var(--panel)" strokeWidth="2" />
          </g>
        )}

        <text x={width - pad.right} y={height - 5} textAnchor="end" className="fill-[var(--fg-3)]" fontSize="9">
          {maxX.toFixed(1)} mi
        </text>
      </svg>

      <div className="flex flex-wrap items-center gap-x-3 gap-y-0.5 mt-1 text-[9px] text-[var(--fg-3)]">
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 rounded bg-[#059669] inline-block" />flat</span>
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 rounded bg-[#ca8a04] inline-block" />6–12%</span>
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 rounded bg-[#ea580c] inline-block" />12–20%</span>
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 rounded bg-[#dc2626] inline-block" />20%+</span>
        <span className="flex items-center gap-1"><i className="h-0.5 w-3 rounded bg-[#0284c7] inline-block" />descent</span>
      </div>
    </div>
  );
}

const ChevronLeft = (p) => (
  <svg {...iconBase} {...p}><path d="m15 18-6-6 6-6" /></svg>
);
const ChevronRight = (p) => (
  <svg {...iconBase} {...p}><path d="m9 18 6-6-6-6" /></svg>
);

/** Full-screen photo viewer. Keeps the user in the app — leaving for Commons to
 *  see the next picture is the clunky path. The source link stays available, but
 *  as an explicit choice rather than the only way to view an image. */
function Lightbox({ photos, index, onClose, onIndex }) {
  const dialogRef = useRef(null);

  useEffect(() => {
    const onKey = (e) => {
      if (e.key === "Escape") onClose();
      else if (e.key === "ArrowRight") onIndex((index + 1) % photos.length);
      else if (e.key === "ArrowLeft") onIndex((index - 1 + photos.length) % photos.length);
    };
    window.addEventListener("keydown", onKey);
    // Focus the dialog so keys work immediately and screen readers announce it.
    dialogRef.current?.focus();
    // Don't let the page scroll behind the overlay.
    const previousOverflow = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {
      window.removeEventListener("keydown", onKey);
      document.body.style.overflow = previousOverflow;
    };
  }, [index, photos.length, onClose, onIndex]);

  const photo = photos[index];
  if (!photo) return null;
  const caption = photo.title.replace(/^File:/, "").replace(/\.[a-z0-9]+$/i, "");

  return (
    <div
      ref={dialogRef}
      role="dialog"
      aria-modal="true"
      aria-label={`Photo ${index + 1} of ${photos.length}: ${caption}`}
      tabIndex={-1}
      onClick={onClose}
      className="fixed inset-0 z-[100] flex flex-col bg-black/92 backdrop-blur-sm outline-none"
    >
      <div className="flex items-center justify-between px-4 py-3 text-white/80">
        <span className="text-xs tabular-nums">
          {index + 1} / {photos.length}
        </span>
        <button
          type="button"
          onClick={onClose}
          aria-label="Close photo viewer"
          className="cursor-pointer rounded-md p-1.5 transition-colors duration-150 hover:bg-white/10 hover:text-white"
        >
          <CloseIcon width={20} height={20} />
        </button>
      </div>

      {/* Stop propagation so clicking the image itself doesn't dismiss. */}
      <div
        className="relative flex flex-1 items-center justify-center px-4 pb-2 min-h-0"
        onClick={(e) => e.stopPropagation()}
      >
        {photos.length > 1 && (
          <button
            type="button"
            onClick={() => onIndex((index - 1 + photos.length) % photos.length)}
            aria-label="Previous photo"
            className="absolute left-4 z-10 grid h-11 w-11 cursor-pointer place-items-center rounded-full bg-white/10 text-white transition-colors duration-150 hover:bg-white/20"
          >
            <ChevronLeft width={22} height={22} />
          </button>
        )}

        <img
          src={photo.url || photo.thumb}
          alt={caption}
          className="max-h-full max-w-full rounded-lg object-contain shadow-2xl"
        />

        {photos.length > 1 && (
          <button
            type="button"
            onClick={() => onIndex((index + 1) % photos.length)}
            aria-label="Next photo"
            className="absolute right-4 z-10 grid h-11 w-11 cursor-pointer place-items-center rounded-full bg-white/10 text-white transition-colors duration-150 hover:bg-white/20"
          >
            <ChevronRight width={22} height={22} />
          </button>
        )}
      </div>

      <div
        className="px-6 pb-5 pt-2 text-center text-white/70"
        onClick={(e) => e.stopPropagation()}
      >
        <p className="text-sm text-white/90">{caption}</p>
        <p className="mt-1 text-[11px]">
          {photo.artist ? `${photo.artist} · ` : ""}
          {photo.license}
          {photo.distance_mi != null && ` · ${photo.distance_mi} mi from trail`}
          {photo.descriptionurl && (
            <>
              {" · "}
              <a
                href={photo.descriptionurl}
                target="_blank"
                rel="noreferrer"
                className="underline hover:text-white"
              >
                view on Commons
              </a>
            </>
          )}
        </p>
      </div>

      {photos.length > 1 && (
        <div
          className="flex justify-center gap-1.5 pb-5"
          onClick={(e) => e.stopPropagation()}
        >
          {photos.map((thumb, i) => (
            <button
              key={thumb.title}
              type="button"
              onClick={() => onIndex(i)}
              aria-label={`Go to photo ${i + 1}`}
              aria-current={i === index}
              className={classNames(
                "h-1.5 cursor-pointer rounded-full transition-all duration-200",
                i === index ? "w-6 bg-white" : "w-1.5 bg-white/35 hover:bg-white/60"
              )}
            />
          ))}
        </div>
      )}
    </div>
  );
}

const ShareIcon = (p) => (
  <svg {...iconBase} {...p}>
    <circle cx="18" cy="5" r="3" />
    <circle cx="6" cy="12" r="3" />
    <circle cx="18" cy="19" r="3" />
    <path d="M8.6 13.5l6.8 4M15.4 6.5l-6.8 4" />
  </svg>
);

const TicketIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M3 9V7a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2v2a2 2 0 0 0 0 6v2a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-2a2 2 0 0 0 0-6z" />
    <path d="M13 5v14" strokeDasharray="2 3" />
  </svg>
);

const TreeIcon = (p) => (
  <svg {...iconBase} {...p}>
    <path d="M12 3 6.5 12h3L5 19h14l-4.5-7h3z" />
    <path d="M12 19v2" />
  </svg>
);

/** What a dot is, shown at the cursor.

 *  Everything here comes from the dot's own properties, so it renders on the first
 *  mousemove with no request. The trail line arrives a moment later from a separate
 *  fetch; the card does not wait for it. */
function DotHoverCard({ card }) {
  if (!card) return null;

  // Flip to the other side of the cursor near the right/bottom edge so the card is
  // never clipped by the map container.
  const flipX = card.x > window.innerWidth - 260;
  const flipY = card.y > window.innerHeight - 160;

  return (
    <div
      className="pointer-events-none absolute z-20 hidden w-[228px] rounded-xl border border-[var(--line)] bg-[var(--panel)]/95 px-3 py-2.5 shadow-xl backdrop-blur sm:block"
      style={{
        left: flipX ? card.x - 240 : card.x + 14,
        top: flipY ? card.y - 130 : card.y + 14,
      }}
    >
      <p className="text-[13px] font-semibold leading-snug text-[var(--fg)]">
        {card.name || "Unnamed trail"}
      </p>
      <div className="mt-1.5 flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-[11px] text-[var(--fg-2)]">
        {card.length_miles != null && (
          <span>
            <span className="text-[var(--fg)] font-medium">{card.length_miles}</span> mi
          </span>
        )}
        {card.gain_ft != null && (
          <span>
            <span className="text-[var(--fg)] font-medium">
              {Math.round(card.gain_ft).toLocaleString()}
            </span>{" "}
            ft gain
          </span>
        )}
      </div>
      <div className="mt-1.5 flex items-center gap-2">
        {card.difficulty && (() => {
          // The CSS variable rather than the JS palette: this component sits
          // outside the page's state, and a var tracks the theme on its own.
          const ink = `var(${
            DIFFICULTY_VARS[String(card.difficulty).toLowerCase()] || "--d-unknown"
          })`;
          return (
            <span
              className="rounded-full px-1.5 py-0.5 text-[10px] font-medium"
              style={{
                background: `color-mix(in srgb, ${ink} 22%, transparent)`,
                color: ink,
              }}
            >
              {card.difficulty}
            </span>
          );
        })()}
        <span className="text-[10px] text-[var(--fg-3)]">
          {card.marker_kind === "trailhead" ? "at trailhead" : "approx. location"}
        </span>
      </div>
    </div>
  );
}

/** Designated wilderness. Land status rather than proximity, so it states the
 *  designation outright — and it changes the rules of the trip, not just the view. */
function WildernessNotice({ wilderness }) {
  if (!wilderness || !wilderness.name) return null;

  // Containment is exact, unlike the 35 mi proximity join behind PermitNotice, so
  // this states the designation outright. A trail that only clips a boundary is
  // reported as partly inside rather than being claimed for the area.
  const partial = wilderness.fully_inside === false;
  const permit = wilderness.permit;

  return (
    <div className="rounded-xl border border-emerald-600/40 bg-emerald-500/10 px-3.5 py-3">
      <div className="flex items-start gap-2.5">
        <span className="text-emerald-600 [:root[data-theme=dark]_&]:text-emerald-400">
          <TreeIcon width={15} height={15} />
        </span>
        <div className="min-w-0 flex-1">
          <p className="text-sm font-semibold text-emerald-800 [:root[data-theme=dark]_&]:text-emerald-300">
            {wilderness.name}
          </p>
          <p className="mt-0.5 text-xs text-[var(--fg-2)]">
            {partial
              ? `Designated wilderness · trail is partly inside (${Math.round(
                  (wilderness.inside_fraction || 0) * 100
                )}%)`
              : "Designated wilderness"}
          </p>
          <p className="mt-1 text-[10px] leading-relaxed text-[var(--fg-3)]">
            No bicycles or motorised transport. Group size limits and overnight
            permits usually apply — check with the managing agency.
          </p>
          {permit && (
            <p className="mt-1 text-[11px] text-[var(--fg-2)]">
              Permits: {permit.name}
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

/** Permit requirement. Placed above elevation and scenery because it is the one
 *  fact that can make a hike impossible regardless of how good it looks. */
function PermitNotice({ permits }) {
  if (!permits || permits.length === 0) return null;

  // A keyword match against the trail name is definite; proximity alone is not,
  // since one coordinate stands in for a whole wilderness.
  const confirmed = permits.filter((p) => !p.advisory);
  const nearby = permits.filter((p) => p.advisory);
  const lead = confirmed[0] || nearby[0];
  const isConfirmed = Boolean(confirmed.length);

  return (
    <div
      className={classNames(
        "rounded-xl border px-3.5 py-3",
        isConfirmed
          ? "border-amber-500/45 bg-amber-500/10"
          : "border-[var(--line)] bg-[var(--sunken)]"
      )}
    >
      <div className="flex items-start gap-2.5">
        <span className={isConfirmed ? "text-amber-500" : "text-[var(--fg-3)]"}>
          <TicketIcon width={15} height={15} />
        </span>
        <div className="min-w-0 flex-1">
          <p
            className={classNames(
              "text-sm font-semibold",
              isConfirmed ? "text-amber-700 [:root[data-theme=dark]_&]:text-amber-300" : "text-[var(--fg)]"
            )}
          >
            {isConfirmed ? "Permit required" : "Permit may be required"}
          </p>

          <p className="mt-0.5 text-xs text-[var(--fg-2)]">
            {lead.name}
            {lead.rec_area && lead.rec_area !== lead.name && ` · ${lead.rec_area}`}
            {!isConfirmed && ` · ${lead.distance_mi} mi away`}
          </p>

          {lead.fee && <p className="mt-1 text-[11px] text-[var(--fg-3)]">Fee: {lead.fee}</p>}

          {!isConfirmed && (
            <p className="mt-1 text-[11px] text-[var(--fg-3)]">
              Matched by proximity, not confirmed for this trail — check before you go.
            </p>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-3">
            {lead.url && (
              <a
                href={lead.url}
                target="_blank"
                rel="noreferrer"
                className={classNames(
                  "text-xs font-medium underline",
                  isConfirmed ? "text-amber-700 [:root[data-theme=dark]_&]:text-amber-300" : "text-[var(--accent)]"
                )}
              >
                Reserve on Recreation.gov →
              </a>
            )}
            {lead.phone && <span className="text-[11px] text-[var(--fg-3)]">{lead.phone}</span>}
          </div>

          {permits.length > 1 && (
            <p className="mt-1.5 text-[11px] text-[var(--fg-3)]">
              {permits.length - 1} other permit area{permits.length > 2 ? "s" : ""} nearby
            </p>
          )}
        </div>
      </div>
    </div>
  );
}

function PhotoStrip({ trailId }) {
  const [state, setState] = useState({ loading: true, photos: [], error: null });
  const [lightbox, setLightbox] = useState(null);

  useEffect(() => {
    let cancelled = false;
    setLightbox(null);
    setState({ loading: true, photos: [], error: null });
    authedFetch(`/api/discover/trail/${trailId}/photos`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error("photo lookup failed"))))
      .then((data) => {
        if (cancelled) return;
        setState({
          loading: false,
          photos: data.photos || [],
          // "unavailable" is not "none" — say which.
          error: data.status === "unavailable" ? data.message || "unavailable" : null,
        });
      })
      .catch((err) => !cancelled && setState({ loading: false, photos: [], error: err.message }));
    return () => {
      cancelled = true;
    };
  }, [trailId]);

  if (state.loading) {
    return <div className="h-24 rounded-xl bg-[var(--chip)] animate-pulse" />;
  }
  if (state.error) {
    return (
      <p className="text-[11px] text-[var(--fg-3)] italic">
        Photos could not be loaded ({state.error}).
      </p>
    );
  }
  if (!state.photos.length) {
    return <p className="text-[11px] text-[var(--fg-3)] italic">No open-licensed photos near this trail.</p>;
  }

  return (
    <div>
      <div className="flex gap-2 overflow-x-auto pb-1">
        {state.photos.map((photo, i) => (
          <button
            key={photo.title}
            type="button"
            onClick={() => setLightbox(i)}
            aria-label={`Open photo ${i + 1} of ${state.photos.length}`}
            title={`${photo.title.replace(/^File:/, "")} — ${photo.license}`}
            className="group relative shrink-0 cursor-pointer overflow-hidden rounded-lg border border-[var(--line)] transition-all duration-200 hover:border-[var(--accent)]"
          >
            <img
              src={photo.thumb}
              alt={photo.title.replace(/^File:/, "").replace(/\.[a-z0-9]+$/i, "")}
              loading="lazy"
              className="h-24 w-36 object-cover transition-transform duration-300 group-hover:scale-[1.04]"
            />
            {i === 0 && state.photos.length > 1 && (
              <span className="absolute bottom-1 right-1 rounded bg-black/65 px-1.5 py-0.5 text-[10px] font-medium text-white">
                {state.photos.length}
              </span>
            )}
          </button>
        ))}
      </div>

      {lightbox !== null && (
        <Lightbox
          photos={state.photos}
          index={lightbox}
          onIndex={setLightbox}
          onClose={() => setLightbox(null)}
        />
      )}
      {/* Proximity is not depiction: say how the photos were chosen. */}
      <p className="mt-1 text-[10px] text-[var(--fg-3)]">
        Wikimedia Commons, taken near this trail · CC-licensed · click to enlarge
      </p>
    </div>
  );
}

// ── Filter controls ──────────────────────────────────────────────────────────

function RangeRow({ label, unit, min, max, onMin, onMax, step = 1 }) {
  return (
    <div>
      <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">{label}</p>
      <div className="flex items-center gap-2">
        <input
          type="number"
          value={min}
          step={step}
          min={0}
          onChange={(e) => onMin(e.target.value)}
          placeholder="min"
          className="w-full rounded-lg border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] placeholder:text-[var(--fg-3)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
        />
        <span className="text-[var(--fg-3)]">–</span>
        <input
          type="number"
          value={max}
          step={step}
          min={0}
          onChange={(e) => onMax(e.target.value)}
          placeholder="max"
          className="w-full rounded-lg border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] placeholder:text-[var(--fg-3)] px-2 py-1.5 text-sm focus:border-[var(--accent)] focus:outline-none"
        />
        <span className="text-xs text-[var(--fg-3)] w-6 shrink-0">{unit}</span>
      </div>
    </div>
  );
}

function TrailCard({ trail, active, comparing, compareFull, palette, onHover, onSelect, onCompare }) {
  const difficulty = trail.difficulty?.label;
  const color = palette[difficulty] || palette.unknown;

  return (
    <div
      onMouseEnter={() => onHover(trail.id)}
      onMouseLeave={() => onHover(null)}
      onClick={() => onSelect(trail)}
      role="button"
      tabIndex={0}
      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && onSelect(trail)}
      className={classNames(
        "w-full text-left rounded-xl border px-3.5 py-3 transition-all duration-200 cursor-pointer hover:shadow-[var(--e2)]",
        active
          ? "border-[var(--accent)] bg-[color-mix(in_srgb,var(--accent)_12%,var(--panel))] shadow-[var(--e1)]"
          : "border-[var(--line)] bg-[var(--panel)] hover:border-[var(--line-strong)]"
      )}
    >
      <div className="flex items-start justify-between gap-2">
        <p className="font-semibold text-[var(--fg)] text-sm leading-snug">{trail.name}</p>
        <div className="flex items-center gap-1.5 shrink-0">
          <button
            type="button"
            onClick={(e) => {
              e.stopPropagation();
              onCompare(trail);
            }}
            disabled={!comparing && compareFull}
            title={comparing ? "Remove from comparison" : compareFull ? "Comparison is full" : "Add to comparison"}
            className={classNames(
              "grid h-6 w-6 place-items-center cursor-pointer rounded-md border transition-all duration-200",
              comparing
                ? "bg-[var(--fg)] text-white border-transparent"
                : compareFull
                ? "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                : "border-[var(--line)] text-[var(--fg-2)] hover:border-[var(--line-strong)]"
            )}
          >
            {comparing ? <CheckIcon width={12} height={12} /> : <PlusIcon width={12} height={12} />}
          </button>
          <span className="h-2 w-2 rounded-full mt-0.5" style={{ background: color }} />
        </div>
      </div>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-xs text-[var(--fg-2)]">
        <span>{trail.length_miles} mi</span>
        {trail.gain_ft != null ? (
          <span>↑ {trail.gain_ft.toLocaleString()} ft</span>
        ) : (
          <span className="text-[var(--fg-3)] italic">gain unknown</span>
        )}
        {trail.steepness && (
          <span className="text-[var(--fg-2)]">{trail.steepness.ft_per_mi} ft/mi</span>
        )}
        {difficulty && <span className="capitalize" style={{ color }}>{difficulty}</span>}
        {trail.route_type === "loop" && <span className="text-[var(--fg-3)]">loop</span>}
      </div>

      {trail.features?.length > 0 && (
        <div className="mt-2 flex flex-wrap gap-1">
          {trail.features.slice(0, 4).map((f) => (
            <span key={f} className="rounded-full bg-[var(--chip)] px-2 py-0.5 text-[10px] text-[var(--fg-2)]">
              {FEATURE_LABELS[f] || f}
            </span>
          ))}
        </div>
      )}
    </div>
  );
}

function CompareTray({ trails, palette, onRemove, onClear, onFocus }) {
  if (!trails.length) return null;

  // Best value in each column gets marked, so the tray answers "which one" and not
  // just "here are the numbers".
  const best = {
    length_miles: Math.min(...trails.map((t) => t.length_miles ?? Infinity)),
    gain_ft: Math.max(...trails.map((t) => t.gain_ft ?? -Infinity)),
    max_elevation_ft: Math.max(...trails.map((t) => t.max_elevation_ft ?? -Infinity)),
    features: Math.max(...trails.map((t) => (t.features || []).length)),
  };

  const Row = ({ label, render }) => (
    <tr className="border-t border-[var(--line)]">
      <th className="text-left font-normal text-[10px] uppercase tracking-[0.15em] text-[var(--fg-3)] py-1.5 pr-3 align-middle whitespace-nowrap">
        {label}
      </th>
      {trails.map((t) => (
        <td key={t.id} className="py-1.5 px-3 text-sm text-[var(--fg)] whitespace-nowrap">
          {render(t)}
        </td>
      ))}
    </tr>
  );

  return (
    <div className="absolute bottom-0 left-0 right-0 z-20 border-t border-[var(--line)] bg-[var(--glass)] backdrop-blur-xl shadow-[var(--e4)]">
      <div className="flex items-center justify-between px-4 pt-2.5">
        <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-2)]">
          Comparing {trails.length} route{trails.length > 1 ? "s" : ""}
        </p>
        <button type="button" onClick={onClear} className="text-xs text-[var(--fg-2)] hover:text-[var(--fg)] underline">
          Clear
        </button>
      </div>

      <div className="overflow-x-auto px-4 pb-3">
        <table className="w-full border-collapse">
          <thead>
            <tr>
              <th />
              {trails.map((t, i) => (
                <th key={t.id} className="px-3 pb-1 text-left align-bottom">
                  <div className="flex items-center gap-1.5">
                    <span className="h-2.5 w-2.5 rounded-sm shrink-0" style={{ background: COMPARE_COLORS[i % COMPARE_COLORS.length] }} />
                    <button
                      type="button"
                      onClick={() => onFocus(t)}
                      className="text-sm font-semibold text-[var(--fg)] hover:text-emerald-700 text-left truncate max-w-[190px]"
                      title={t.name}
                    >
                      {t.name}
                    </button>
                    <button
                      type="button"
                      onClick={() => onRemove(t.id)}
                      className="rounded p-0.5 text-[var(--fg-3)] transition-colors duration-150 hover:text-[var(--fg)]"
                      aria-label={`Remove ${t.name} from comparison`}
                    >
                      <CloseIcon width={14} height={14} />
                    </button>
                  </div>
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            <Row
              label="Length"
              render={(t) => (
                <span className={t.length_miles === best.length_miles ? "font-semibold text-emerald-700" : ""}>
                  {t.length_miles} mi
                </span>
              )}
            />
            <Row
              label="Gain"
              render={(t) =>
                t.gain_ft == null ? (
                  <span className="text-[var(--fg-3)] italic">unknown</span>
                ) : (
                  <span className={t.gain_ft === best.gain_ft ? "font-semibold text-emerald-700" : ""}>
                    {t.gain_ft.toLocaleString()} ft
                  </span>
                )
              }
            />
            <Row
              label="Steepness"
              render={(t) =>
                t.steepness ? (
                  <span className="capitalize">
                    {t.steepness.label}
                    <span className="text-[var(--fg-3)] ml-1">{t.steepness.ft_per_mi} ft/mi</span>
                  </span>
                ) : (
                  <span className="text-[var(--fg-3)] italic">unknown</span>
                )
              }
            />
            <Row
              label="Effort"
              render={(t) =>
                t.difficulty ? (
                  <span className="capitalize" style={{ color: palette[t.difficulty.label] }}>
                    {t.difficulty.label}
                  </span>
                ) : (
                  <span className="text-[var(--fg-3)] italic">unknown</span>
                )
              }
            />
            <Row
              label="High point"
              render={(t) =>
                t.max_elevation_ft == null ? (
                  <span className="text-[var(--fg-3)] italic">—</span>
                ) : (
                  <span className={t.max_elevation_ft === best.max_elevation_ft ? "font-semibold text-emerald-700" : ""}>
                    {t.max_elevation_ft.toLocaleString()} ft
                  </span>
                )
              }
            />
            <Row
              label="Scenery"
              render={(t) =>
                (t.features || []).length ? (
                  <span className="flex flex-wrap gap-1">
                    {t.features.slice(0, 4).map((f) => (
                      <span key={f} className="rounded-full bg-[var(--chip)] px-1.5 py-0.5 text-[10px] text-[var(--fg-2)]">
                        {FEATURE_LABELS[f] || f}
                      </span>
                    ))}
                  </span>
                ) : (
                  <span className="text-[var(--fg-3)] italic">none found</span>
                )
              }
            />
            <Row label="Area" render={(t) => <span className="text-xs text-[var(--fg-2)]">{t.mgmt_area || "—"}</span>} />
          </tbody>
        </table>
      </div>
    </div>
  );
}

// ── Page ─────────────────────────────────────────────────────────────────────

export default function DiscoverPage() {
  const mapContainer = useRef(null);
  const map = useRef(null);
  const hoveredRef = useRef(null);
  const resizeObserver = useRef(null);
  const styleFallback = useRef(null);

  const [mapReady, setMapReady] = useState(false);
  const [bbox, setBbox] = useState(null);
  // Mode, not raw zoom: re-rendering on every fractional zoom would refetch
  // constantly, and only the crossing of the threshold changes what we request.
  const [dotMode, setDotMode] = useState(true);
  const [hoverTrailId, setHoverTrailId] = useState(null);
  const [hoverCard, setHoverCard] = useState(null);
  const [dotCount, setDotCount] = useState(0);
  // Route planning. `planModeRef` mirrors the state because the map click handler
  // is registered once and would otherwise close over the initial value forever.
  const [planMode, setPlanMode] = useState(false);
  const [planStart, setPlanStart] = useState(null);
  const [planEnd, setPlanEnd] = useState(null);
  const [planOutAndBack, setPlanOutAndBack] = useState(true);
  const [planRoute, setPlanRoute] = useState(null);
  const [planLoading, setPlanLoading] = useState(false);
  const [planError, setPlanError] = useState(null);
  const planModeRef = useRef(false);
  useEffect(() => {
    planModeRef.current = planMode;
    if (map.current) {
      map.current.getCanvas().style.cursor = planMode ? "crosshair" : "";
    }
  }, [planMode]);
  const [results, setResults] = useState([]);
  const [total, setTotal] = useState(0);
  const [facets, setFacets] = useState(null);
  const [status, setStatus] = useState(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(null);
  const [selected, setSelected] = useState(null);
  const [hovered, setHovered] = useState(null);
  const [truncated, setTruncated] = useState(false);
  const [scrubPoint, setScrubPoint] = useState(null);
  const [compare, setCompare] = useState([]);
  const [theme, setTheme] = useState(initialTheme);
  // Bumped whenever the basemap style reloads, so the search effect re-runs and
  // repopulates the GeoJSON sources that setStyle() discarded.
  const [styleEpoch, setStyleEpoch] = useState(0);
  const [palette, setPalette] = useState(() => readPalette());
  const [basemap, setBasemap] = useState(() => URL_STATE.basemap || initialBasemap());
  const [pitched, setPitched] = useState(() => Boolean(URL_STATE.pitched));

  const [filters, setFilters] = useState(() => URL_STATE.filters || DEFAULT_FILTERS);
  const [copied, setCopied] = useState(false);

  // Tailwind reads the class off <html>; persist the choice so it survives reloads.
  useEffect(() => {
    // `data-theme` (not a `.dark` class): Tailwind v4 does not read the v3
    // `darkMode: "class"` option, and the planner's existing dark rules already
    // key off this attribute — one switch drives both surfaces.
    document.documentElement.setAttribute("data-theme", theme);
    setPalette(readPalette());
    try {
      localStorage.setItem("opentrails-theme", theme);
    } catch {
      // storage unavailable; the theme still applies for this session
    }
  }, [theme]);

  // Repaint trail lines with the new palette (the basemap swap below reinstalls
  // layers, but this also covers a palette change without a style reload).
  useEffect(() => {
    if (!map.current?.getLayer?.("trails-line")) return;
    map.current.setPaintProperty("trails-line", "line-color", difficultyColorExpression(palette));
  }, [palette, styleEpoch]);

  // Swap the basemap when the theme changes, then reinstall our layers.
  useEffect(() => {
    if (!map.current || !mapReady) return;
    const wanted = BASEMAPS[basemap][theme === "dark" ? "dark" : "light"];
    if (map.current.__styleUrl === wanted) return;
    map.current.__styleUrl = wanted;
    map.current.setStyle(wanted);
    try {
      localStorage.setItem("opentrails-basemap", basemap);
    } catch {
      // storage unavailable
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [theme, basemap, mapReady]);

  // 3D tilt. Terrain is always loaded; this just leans the camera into it.
  useEffect(() => {
    if (!map.current || !mapReady) return;
    map.current.easeTo({
      pitch: pitched ? 62 : 0,
      bearing: pitched ? -18 : 0,
      duration: 900,
    });
  }, [pitched, mapReady]);

  const update = useCallback((patch) => setFilters((f) => ({ ...f, ...patch })), []);

  const inCompare = useCallback((id) => compare.some((t) => t.id === id), [compare]);

  const toggleCompare = useCallback(async (trail) => {
    setCompare((current) => {
      if (current.some((t) => t.id === trail.id)) {
        return current.filter((t) => t.id !== trail.id);
      }
      if (current.length >= MAX_COMPARE) return current;
      return [...current, trail];
    });
  }, []);

  const toggleIn = useCallback((key, value) => {
    setFilters((f) => {
      const current = f[key];
      return {
        ...f,
        [key]: current.includes(value) ? current.filter((v) => v !== value) : [...current, value],
      };
    });
  }, []);

  // Index status — used to tell the user what the data does and does not cover.
  useEffect(() => {
    authedFetch("/api/discover/status")
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => setStatus({ available: false, error: "Could not reach the trail index" }));
  }, []);

  // ── Map setup ──────────────────────────────────────────────────────────────
  useEffect(() => {
    if (map.current || !mapContainer.current || !MAPBOX_TOKEN) {
      if (!MAPBOX_TOKEN) setMapReady(true); // unblock the rest of the UI
      return;
    }

    mapboxgl.accessToken = MAPBOX_TOKEN;
    map.current = new mapboxgl.Map({
      container: mapContainer.current,
      style: BASEMAPS[basemap][theme === "dark" ? "dark" : "light"],
      center: URL_STATE.view?.center || INITIAL_VIEW.center,
      zoom: URL_STATE.view?.zoom ?? INITIAL_VIEW.zoom,
      pitch: URL_STATE.view?.pitch || 0,
      bearing: URL_STATE.view?.bearing || 0,
    });

    // Record the style we opened with, so the theme effect does not immediately
    // re-set the same style and race the initial layer install.
    map.current.__styleUrl = BASEMAPS[basemap][theme === "dark" ? "dark" : "light"];

    map.current.addControl(new mapboxgl.NavigationControl({ visualizePitch: true }), "top-right");
    map.current.addControl(new mapboxgl.ScaleControl({ unit: "imperial" }), "bottom-right");

    map.current.on("error", (e) => console.error("[map error]", e?.error?.message || e));

    // Sources and layers are installed on `style.load`, not `load`.
    // `load` additionally waits for the first full render and does not reliably
    // fire in every environment — observed here with a fully parsed 148-layer
    // style, tiles loaded, and `isStyleLoaded()` still false — which left the app
    // permanently stuck with no layers and no viewport. `style.load` is the
    // documented point at which sources and layers may be added. Guarded so it
    // runs exactly once no matter which trigger arrives first.
    let layersInstalled = false;
    const installLayers = () => {
      if (layersInstalled || !map.current) return;
      layersInstalled = true;
      // Near-black ink reads as "selected" on a light map; on the dark basemap it
      // disappears, so invert it there.
      const isDark = document.documentElement.getAttribute("data-theme") === "dark";
      const routeInk = isDark ? "#f8fafc" : "#0f172a";
      const routeInkContrast = isDark ? "#0b1015" : "#ffffff";
      // Clusters are counts, not trails, so they take a neutral accent rather than
      // any difficulty colour — colouring them would imply a difficulty they do
      // not have.
      const planInk = isDark ? "#67d0ff" : "#0b70b8";
      const clusterInk = isDark ? "#5fbf8c" : "#3aa568";
      const clusterInkLow = isDark ? "#31694f" : "#9fd9ba";
      const clusterInkHigh = isDark ? "#8ee6b4" : "#1d7a45";
      const clusterText = isDark ? "#eafff3" : "#062514";
      const labelInk = isDark ? "#e8f2ec" : "#12261b";
      const labelHalo = isDark ? "rgba(6,14,10,0.85)" : "rgba(255,255,255,0.9)";
      try {
      map.current.addSource("trails", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });

      // Wide invisible hit area makes thin trail lines clickable.
      map.current.addLayer({
        id: "trails-hit",
        type: "line",
        source: "trails",
        paint: { "line-color": "#000", "line-opacity": 0, "line-width": 14 },
      });

      map.current.addLayer({
        id: "trails-line",
        type: "line",
        source: "trails",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": difficultyColorExpression(readPalette()),
          // No feature-state here: trail ids are non-numeric strings
          // ("nps:YOSE:..."), which Mapbox feature-state does not accept, and the
          // invalid expression silently prevented the whole layer from drawing.
          // Highlighting is done with a filtered overlay layer instead.
          "line-width": ["interpolate", ["linear"], ["zoom"], 6, 1.6, 11, 3, 15, 4.5],
          "line-opacity": 0.9,
        },
      });

      // Highlight overlay — driven by a filter on the hovered/selected id.
      map.current.addLayer({
        id: "trails-highlight",
        type: "line",
        source: "trails",
        layout: { "line-cap": "round", "line-join": "round" },
        filter: ["==", ["get", "id"], "__none__"],
        paint: { "line-color": routeInk, "line-width": 5, "line-opacity": 1 },
      });

      // One point per trail, clustered. A thousand identical dots is not
      // information — a cluster labelled "240" is, and it collapses a wall of
      // overlapping marks into something you can read at a glance.
      map.current.addSource("trail-dots", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
        cluster: true,
        clusterRadius: 40,
        // Above this, show individual trails: by then they are far enough apart to
        // be distinct and you are close enough to care which is which.
        clusterMaxZoom: 10,
        clusterProperties: {
          // Carried so a cluster can say something about what is inside it.
          longest: ["max", ["coalesce", ["get", "length_miles"], 0]],
        },
      });

      map.current.addLayer({
        id: "trail-dots-cluster",
        type: "circle",
        source: "trail-dots",
        maxzoom: DOT_ZOOM_MAX,
        filter: ["has", "point_count"],
        paint: {
          // Density reads through value, not size alone: a sparse cluster stays
          // translucent and lets the terrain through, a dense one darkens.
          "circle-color": [
            "interpolate", ["linear"], ["get", "point_count"],
            1, clusterInkLow, 100, clusterInk, 800, clusterInkHigh,
          ],
          "circle-opacity": 0.62,
          // Area, not radius, tracks count — doubling the radius quadruples the
          // apparent size and badly overstates a small difference. Kept small on
          // purpose: these sat on top of the map and buried the hillshade.
          "circle-radius": [
            "interpolate", ["linear"], ["sqrt", ["get", "point_count"]],
            1, 9, 4, 14, 10, 19, 30, 26,
          ],
          "circle-stroke-width": 1,
          "circle-stroke-color": "rgba(255,255,255,0.28)",
        },
      });

      map.current.addLayer({
        id: "trail-dots-cluster-count",
        type: "symbol",
        source: "trail-dots",
        maxzoom: DOT_ZOOM_MAX,
        filter: ["has", "point_count"],
        layout: {
          "text-field": ["get", "point_count_abbreviated"],
          "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
          "text-size": ["interpolate", ["linear"], ["get", "point_count"], 1, 11, 500, 15],
          "text-allow-overlap": true,
        },
        paint: {
          "text-color": clusterText,
          "text-halo-color": labelHalo,
          "text-halo-width": 1,
        },
      });

      map.current.addLayer({
        id: "trail-dots-point",
        type: "circle",
        source: "trail-dots",
        maxzoom: DOT_ZOOM_MAX,
        filter: ["!", ["has", "point_count"]],
        paint: {
          "circle-color": difficultyColorExpression(readPalette()),
          // Size carries trail length, so scanning the map tells you where the
          // long days are without reading a single label.
          "circle-radius": [
            "interpolate", ["linear"], ["coalesce", ["get", "length_miles"], 0],
            0, 3.5, 3, 5, 10, 7, 30, 10,
          ],
          // A surveyed trailhead is a stronger claim than a computed midpoint, and
          // the ring says which one you are looking at.
          "circle-stroke-width": [
            "case", ["==", ["get", "marker_kind"], "trailhead"], 2.5, 1,
          ],
          "circle-stroke-color": [
            "case",
            ["==", ["get", "marker_kind"], "trailhead"], "rgba(255,255,255,0.9)",
            "rgba(0,0,0,0.55)",
          ],
          "circle-opacity": 0.95,
        },
      });

      map.current.addLayer({
        id: "trail-dots-label",
        type: "symbol",
        source: "trail-dots",
        filter: ["!", ["has", "point_count"]],
        minzoom: 9.5,
        maxzoom: DOT_ZOOM_MAX,
        layout: {
          "text-field": ["get", "name"],
          "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
          "text-size": 11,
          "text-offset": [0, 1.1],
          "text-anchor": "top",
          "text-optional": true,
          "text-max-width": 11,
        },
        paint: {
          "text-color": labelInk,
          "text-halo-color": labelHalo,
          "text-halo-width": 1.4,
        },
      });

      // The line revealed under the cursor. Separate from `selected-route` so a
      // hover never disturbs what the user has actually selected.
      map.current.addSource("hover-route", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });

      map.current.addLayer({
        id: "hover-route-line",
        type: "line",
        source: "hover-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": routeInk, "line-width": 4, "line-opacity": 0.95 },
      });

      // Composed hike from the routing graph.
      map.current.addSource("plan-route", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.current.addLayer({
        id: "plan-route-casing",
        type: "line",
        source: "plan-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": routeInkContrast, "line-width": 9, "line-opacity": 0.9 },
      });
      map.current.addLayer({
        id: "plan-route-line",
        type: "line",
        source: "plan-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": planInk, "line-width": 5 },
      });

      map.current.addSource("plan-points", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.current.addLayer({
        id: "plan-points-circle",
        type: "circle",
        source: "plan-points",
        paint: {
          "circle-radius": 7,
          "circle-color": ["case", ["==", ["get", "role"], "start"], planInk, routeInk],
          "circle-stroke-width": 2.5,
          "circle-stroke-color": routeInkContrast,
        },
      });
      map.current.addLayer({
        id: "plan-points-label",
        type: "symbol",
        source: "plan-points",
        layout: {
          "text-field": ["case", ["==", ["get", "role"], "start"], "A", "B"],
          "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
          "text-size": 10,
        },
        paint: { "text-color": routeInkContrast },
      });

      // Selected route drawn on top of everything else.
      map.current.addSource("selected-route", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.current.addLayer({
        id: "selected-route-glow",
        type: "line",
        source: "selected-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": routeInk, "line-width": 9, "line-opacity": 0.18, "line-blur": 3 },
      });
      map.current.addLayer({
        id: "selected-route-line",
        type: "line",
        source: "selected-route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": routeInk, "line-width": 3 },
      });

      // Routes held in the comparison tray, each in its own colour.
      map.current.addSource("compare-routes", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.current.addLayer({
        id: "compare-routes-line",
        type: "line",
        source: "compare-routes",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: {
          "line-color": ["get", "color"],
          "line-width": 4,
          "line-opacity": 0.95,
        },
      });

      // Marker that tracks the elevation-profile cursor along the trail.
      map.current.addSource("scrub-point", {
        type: "geojson",
        data: { type: "FeatureCollection", features: [] },
      });
      map.current.addLayer({
        id: "scrub-halo",
        type: "circle",
        source: "scrub-point",
        paint: { "circle-radius": 13, "circle-color": routeInk, "circle-opacity": 0.2 },
      });
      map.current.addLayer({
        id: "scrub-dot",
        type: "circle",
        source: "scrub-point",
        paint: {
          "circle-radius": 6,
          "circle-color": routeInk,
          "circle-stroke-width": 2.5,
          "circle-stroke-color": routeInkContrast,
        },
      });

      map.current.on("mousemove", "trails-hit", (e) => {
        map.current.getCanvas().style.cursor = "pointer";
        const id = e.features?.[0]?.id;
        if (id != null && id !== hoveredRef.current) setHovered(String(id));
      });
      map.current.on("mouseleave", "trails-hit", () => {
        map.current.getCanvas().style.cursor = "";
        setHovered(null);
      });
      map.current.on("click", "trails-hit", (e) => {
        if (planModeRef.current) return;   // the click is placing a route point
        const id = e.features?.[0]?.properties?.id;
        if (id) loadTrail(id);
      });

      map.current.on("mousemove", "trail-dots-point", (e) => {
        map.current.getCanvas().style.cursor = "pointer";
        const f = e.features?.[0];
        const id = f?.properties?.id;
        if (id == null) return;
        // Card position follows the cursor; the payload is what the dot already
        // carries, so the card appears instantly and does not wait on a fetch.
        setHoverCard({
          x: e.point.x,
          y: e.point.y,
          name: f.properties.name,
          length_miles: f.properties.length_miles,
          gain_ft: f.properties.gain_ft,
          difficulty: f.properties.difficulty,
          marker_kind: f.properties.marker_kind,
        });
        if (id !== hoveredRef.current) {
          setHovered(String(id));
          setHoverTrailId(String(id));
        }
      });
      map.current.on("mouseleave", "trail-dots-point", () => {
        map.current.getCanvas().style.cursor = "";
        setHovered(null);
        setHoverTrailId(null);
        setHoverCard(null);
      });
      map.current.on("click", "trail-dots-point", (e) => {
        if (planModeRef.current) return;
        const id = e.features?.[0]?.properties?.id;
        if (id) loadTrail(id);
      });

      // Clicking a cluster zooms to the level where it breaks apart, which is the
      // only useful thing a cluster can do.
      map.current.on("click", "trail-dots-cluster", (e) => {
        const feature = e.features?.[0];
        const clusterId = feature?.properties?.cluster_id;
        const source = map.current.getSource("trail-dots");
        if (clusterId == null || !source?.getClusterExpansionZoom) return;
        source.getClusterExpansionZoom(clusterId, (err, zoom) => {
          if (err) return;
          map.current.easeTo({
            center: feature.geometry.coordinates,
            zoom: Math.min(zoom + 0.4, 16),
            duration: 500,
          });
        });
      });
      map.current.on("mouseenter", "trail-dots-cluster", () => {
        map.current.getCanvas().style.cursor = "pointer";
      });
      map.current.on("mouseleave", "trail-dots-cluster", () => {
        map.current.getCanvas().style.cursor = "";
      });

      // Placing route points. Registered on the map rather than a layer, because
      // you pick a spot on the ground, not a feature.
      map.current.on("click", (e) => {
        if (!planModeRef.current) return;
        const point = [
          Number(e.lngLat.lng.toFixed(6)),
          Number(e.lngLat.lat.toFixed(6)),
        ];
        setPlanStart((currentStart) => {
          if (!currentStart) return point;
          setPlanEnd(point);
          return currentStart;
        });
      });

      const sync = () => {
        const b = map.current.getBounds();
        const next = [b.getWest(), b.getSouth(), b.getEast(), b.getNorth()].map(
          (v) => Math.round(v * 1e4) / 1e4
        );
        // Only replace the array when the viewport actually moved. A fresh
        // reference on every `moveend` re-runs the search effect, whose cleanup
        // aborts the in-flight request — and `resize()` emits `moveend`, so the
        // ResizeObserver kept the app in a permanent abort loop showing 0 results.
        setBbox((current) =>
          current && current.every((v, i) => v === next[i]) ? current : next
        );
        setDotMode(map.current.getZoom() < DOT_ZOOM_MAX);
      };
      // Terrain relief. For choosing a route, seeing where the mountains actually
      // are matters more than any styling — and it is what makes a dark basemap
      // read as landscape rather than a flat grey sheet. Guarded: terrain is a
      // progressive enhancement, never a reason to lose the map.
      try {
        if (!map.current.getSource("mapbox-dem")) {
          map.current.addSource("mapbox-dem", {
            type: "raster-dem",
            url: "mapbox://mapbox.mapbox-terrain-dem-v1",
            tileSize: 512,
            // 14 is the DEM's native maximum; going higher just upsamples, but
            // asking for it lets Mapbox use the finest tiles available when tilted.
            maxzoom: 14,
          });
        }
        // Slightly more relief under satellite, where there is no topo shading to
        // carry the shape.
        map.current.setTerrain({
          source: "mapbox-dem",
          exaggeration: basemap === "satellite" ? 1.5 : 1.25,
        });

        if (!map.current.getLayer("hillshade")) {
          // Kept under satellite too: the imagery shows texture but flattens relief
          // at low sun angles, and the shading is what makes shape readable.
          map.current.addLayer(
            {
              id: "hillshade",
              type: "hillshade",
              source: "mapbox-dem",
              paint: {
                // Dark needs far more separation than it was getting: a #3a4a44
                // highlight on a near-black ground is invisible, which is why the
                // dark terrain view lost all sense of depth.
                "hillshade-exaggeration": isDark ? 0.75 : 0.45,
                "hillshade-shadow-color": isDark ? "#000000" : "#4a5a52",
                "hillshade-highlight-color": isDark ? "#8fa8a0" : "#ffffff",
                "hillshade-accent-color": isDark ? "#1d2b2e" : "#7d9689",
              },
            },
            "trails-hit"
          );
        }

        // Contour lines, drawn ourselves rather than relying on the basemap.
        // outdoors-v12 has them; dark-v11 and satellite do not, so switching theme
        // or basemap silently removed every elevation cue. Sourcing them directly
        // means contours are present on all three.
        if (!map.current.getSource("mapbox-terrain")) {
          map.current.addSource("mapbox-terrain", {
            type: "vector",
            url: "mapbox://mapbox.mapbox-terrain-v2",
          });
        }
        if (!map.current.getLayer("contours")) {
          map.current.addLayer(
            {
              id: "contours",
              type: "line",
              source: "mapbox-terrain",
              "source-layer": "contour",
              minzoom: 10,
              paint: {
                "line-color": isDark ? "#7fe3cf" : "#8a6a3a",
                // Index contours (every 5th) are drawn heavier, as on a paper map.
                "line-width": [
                  "case",
                  ["==", ["get", "index"], 5], 1.1,
                  ["==", ["get", "index"], 10], 1.1,
                  0.55,
                ],
                "line-opacity": [
                  "interpolate", ["linear"], ["zoom"],
                  10, 0,
                  12, isDark ? 0.3 : 0.35,
                  15, isDark ? 0.45 : 0.5,
                ],
              },
            },
            "trails-hit"
          );
        }
        if (!map.current.getLayer("contour-labels")) {
          map.current.addLayer(
            {
              id: "contour-labels",
              type: "symbol",
              source: "mapbox-terrain",
              "source-layer": "contour",
              minzoom: 12.5,
              filter: [">", ["get", "index"], 0],
              layout: {
                "symbol-placement": "line",
                "text-field": ["concat", ["to-string", ["get", "ele"]], " m"],
                "text-size": 10,
                "text-max-angle": 25,
                "symbol-spacing": 320,
              },
              paint: {
                "text-color": isDark ? "#9fe8d8" : "#7a5c2e",
                "text-halo-color": isDark ? "#000000" : "#ffffff",
                "text-halo-width": 1.2,
              },
            },
            "trails-hit"
          );
        }

        // Sky adds the horizon and atmospheric depth that sells a tilted view.
        if (!map.current.getLayer("sky")) {
          map.current.addLayer({
            id: "sky",
            type: "sky",
            paint: {
              "sky-type": "atmosphere",
              "sky-atmosphere-sun": [0.0, 88.0],
              "sky-atmosphere-sun-intensity": isDark ? 4 : 12,
              "sky-atmosphere-color": isDark ? "#0a1418" : "#a8c8e8",
            },
          });
        }

        map.current.setFog(
          isDark
            ? { color: "#0b0b0d", "high-color": "#101318", "horizon-blend": 0.08, "space-color": "#000000", "star-intensity": 0.15 }
            : { color: "#f7f5f1", "high-color": "#dbe6ef", "horizon-blend": 0.06, "space-color": "#e8eef5", "star-intensity": 0 }
        );
      } catch (terrainErr) {
        console.warn("[map] terrain unavailable", terrainErr);
      }

      map.current.on("moveend", sync);
      sync();
      map.current.resize();
      // Basemap tiles are not requested until the transform changes — the same
      // root cause as `load` never firing. A repaint alone is not enough; nudging
      // the zoom by a hair forces the source to load its tiles, after which the
      // map behaves normally.
      requestAnimationFrame(() => {
        if (!map.current) return;
        map.current.resize();
        map.current.jumpTo({
          center: map.current.getCenter(),
          zoom: map.current.getZoom() + 0.0001,
        });
        map.current.triggerRepaint();
      });
      setMapReady(true);
      } catch (err) {
        console.error("[map] layer install failed", err);
        // Never leave the UI stuck on "0 trails": fall back to a statewide search.
        setBbox([-124.5, 32.5, -114.1, 42.1]);
        setMapReady(true);
      }
    };

    map.current.on("style.load", () => {
      // setStyle() discards every custom source and layer, so allow reinstall.
      layersInstalled = false;
      installLayers();
      setStyleEpoch((n) => n + 1);
    });
    map.current.on("load", installLayers);
    // Last resort: if neither event arrives but the style did parse, proceed anyway.
    const installFallback = setTimeout(() => {
      if (map.current?.getStyle()?.layers?.length) installLayers();
    }, 2000);
    styleFallback.current = installFallback;

    // The map is created inside a flex child whose height is not settled on the
    // first paint, so Mapbox sizes its canvas to a 300px default and never
    // recovers. Watch the container and resize with it.
    const observer = new ResizeObserver(() => map.current?.resize());
    observer.observe(mapContainer.current);
    resizeObserver.current = observer;
    return () => {
      clearTimeout(styleFallback.current);
      resizeObserver.current?.disconnect();
      resizeObserver.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Highlight the hovered (or selected) trail by filtering the overlay layer.
  useEffect(() => {
    if (!map.current?.getLayer?.("trails-highlight")) return;
    const next = hovered || selected?.id || "__none__";
    map.current.setFilter("trails-highlight", ["==", ["get", "id"], next]);
    hoveredRef.current = next;
  }, [hovered, selected]);

  // A shared link names a trail; open its panel on load.
  const sharedTrailOpened = useRef(false);
  useEffect(() => {
    if (sharedTrailOpened.current || !URL_STATE.trailId) return;
    sharedTrailOpened.current = true;
    loadTrail(URL_STATE.trailId, { fit: !URL_STATE.view });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Keep the address bar in step with the view, so the link is always current.
  useEffect(() => {
    if (!mapReady) return;
    const sync = () =>
      writeUrlState({
        filters,
        selectedId: selected?.id || null,
        map: map.current,
        basemap,
        pitched,
      });
    sync();
    map.current?.on("moveend", sync);
    return () => map.current?.off("moveend", sync);
  }, [filters, selected, basemap, pitched, mapReady]);

  // Draw the selected trail as its own emphasised layer.
  useEffect(() => {
    const source = map.current?.getSource?.("selected-route");
    if (!source) return;
    source.setData(
      selected?.geometry
        ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: selected.geometry, properties: {} }] }
        : { type: "FeatureCollection", features: [] }
    );
  }, [selected, styleEpoch]);

  // Paint the compared routes.
  useEffect(() => {
    const source = map.current?.getSource?.("compare-routes");
    if (!source) return;
    source.setData({
      type: "FeatureCollection",
      features: compare
        .filter((t) => t.geometry)
        .map((t, i) => ({
          type: "Feature",
          geometry: t.geometry,
          properties: { color: COMPARE_COLORS[i % COMPARE_COLORS.length], name: t.name },
        })),
    });
  }, [compare, styleEpoch]);

  // Move the scrub marker as the profile cursor moves.
  const selectedCoords = useMemo(() => flattenGeometry(selected?.geometry), [selected]);

  // The elevation axis and the drawn line measure the same walk differently. The
  // profile is sampled at one point per 0.05 mi, and a chord through those samples
  // is shorter than the arc through every vertex — measured across the index, the
  // profile ends at a median 94.4% of the drawn length, and 54.7% of trails are
  // more than 5% short. Scrubbing to the far right therefore stopped short of the
  // end of the trail. Both are monotonic along the same path, so mapping *fraction
  // of the profile* onto *fraction of the drawn line* lines the two ends up and
  // stays correct in between.
  const scrubScale = useMemo(() => {
    const profile = selected?.elevation?.profile;
    if (!profile?.length || selectedCoords.length < 2) return null;
    const profileMi = profile.reduce((max, p) => Math.max(max, p?.mi ?? 0), 0);
    if (profileMi <= 0) return null;
    let drawnMi = 0;
    for (let i = 1; i < selectedCoords.length; i += 1) {
      drawnMi += haversineMi(selectedCoords[i - 1], selectedCoords[i]);
    }
    return drawnMi > 0 ? { profileMi, drawnMi } : null;
  }, [selected, selectedCoords]);

  const handleScrub = useCallback(
    (mi) => {
      if (mi == null) {
        setScrubPoint(null);
        return;
      }
      const target = scrubScale
        ? (mi / scrubScale.profileMi) * scrubScale.drawnMi
        : mi;
      setScrubPoint(pointAtMile(selectedCoords, target));
    },
    [selectedCoords, scrubScale]
  );

  useEffect(() => {
    const source = map.current?.getSource?.("scrub-point");
    if (!source) return;
    source.setData(
      scrubPoint
        ? { type: "FeatureCollection", features: [{ type: "Feature", geometry: { type: "Point", coordinates: scrubPoint }, properties: {} }] }
        : { type: "FeatureCollection", features: [] }
    );
  }, [scrubPoint]);

  // ── Reveal a trail's line when its dot is hovered ───────────────────────────
  // Geometry is fetched per trail rather than shipped with the dots: one line is a
  // few KB, whereas every line in the viewport is megabytes. Results are cached so
  // moving back over a dot is instant, and a stale response can never overwrite a
  // newer hover.
  const hoverGeomCache = useRef(new Map());
  useEffect(() => {
    const source = map.current?.getSource?.("hover-route");
    if (!source) return;
    const empty = { type: "FeatureCollection", features: [] };

    if (!hoverTrailId) {
      source.setData(empty);
      return;
    }

    const cached = hoverGeomCache.current.get(hoverTrailId);
    if (cached) {
      source.setData(cached);
      return;
    }

    let cancelled = false;
    const controller = new AbortController();
    (async () => {
      try {
        // Hover only happens in dot mode, i.e. below zoom 11, where the z10 tier
        // is ~28x smaller than full resolution and visually identical.
        const res = await authedFetch(
          `/api/discover/trail/${encodeURIComponent(hoverTrailId)}?detail=z10`,
          { signal: controller.signal }
        );
        if (!res.ok) return;
        const trail = await res.json();
        if (!trail.geometry) return;
        const collection = {
          type: "FeatureCollection",
          features: [{ type: "Feature", properties: {}, geometry: trail.geometry }],
        };
        hoverGeomCache.current.set(hoverTrailId, collection);
        // The pointer may have moved on while this was in flight.
        if (!cancelled) map.current?.getSource?.("hover-route")?.setData(collection);
      } catch (err) {
        if (err.name !== "AbortError") console.warn("[map] hover geometry", err);
      }
    })();

    return () => {
      cancelled = true;
      controller.abort();
    };
  }, [hoverTrailId]);

  // ── Route planning: A + B -> a composed hike from the trail graph ───────────
  useEffect(() => {
    if (!planStart || !planEnd) {
      setPlanRoute(null);
      setPlanError(null);
      return;
    }
    const controller = new AbortController();
    setPlanLoading(true);
    setPlanError(null);
    (async () => {
      try {
        const params = new URLSearchParams({
          start: planStart.join(","),
          end: planEnd.join(","),
          out_and_back: String(planOutAndBack),
          snap: "0.5",
        });
        const res = await authedFetch(`/api/discover/graph/route?${params}`, {
          signal: controller.signal,
        });
        const data = await res.json();
        if (!res.ok) throw new Error(data?.detail || `Routing failed (${res.status})`);
        if (!data.ok) {
          setPlanRoute(null);
          setPlanError(data.detail || "No route found.");
        } else {
          setPlanRoute(data);
        }
      } catch (err) {
        if (err.name !== "AbortError") setPlanError(err.message);
      } finally {
        setPlanLoading(false);
      }
    })();
    return () => controller.abort();
  }, [planStart, planEnd, planOutAndBack]);

  // Draw the composed route and its two endpoints.
  useEffect(() => {
    const routeSource = map.current?.getSource?.("plan-route");
    const pointSource = map.current?.getSource?.("plan-points");
    if (!routeSource || !pointSource) return;

    routeSource.setData(
      planRoute?.geometry
        ? {
            type: "FeatureCollection",
            features: [
              { type: "Feature", properties: {}, geometry: planRoute.geometry },
            ],
          }
        : { type: "FeatureCollection", features: [] }
    );

    const points = [];
    if (planStart) {
      points.push({
        type: "Feature",
        properties: { role: "start" },
        geometry: { type: "Point", coordinates: planStart },
      });
    }
    if (planEnd) {
      points.push({
        type: "Feature",
        properties: { role: "end" },
        geometry: { type: "Point", coordinates: planEnd },
      });
    }
    pointSource.setData({ type: "FeatureCollection", features: points });
  }, [planRoute, planStart, planEnd]);

  const clearPlan = useCallback(() => {
    setPlanStart(null);
    setPlanEnd(null);
    setPlanRoute(null);
    setPlanError(null);
  }, []);

  // ── Dots: fetched once per filter set, never per pan ────────────────────────
  // Statewide dots are 2.4 MB. Re-requesting that on every pan is what made the map
  // feel slow; the whole set is small enough to hold client-side and let Mapbox
  // cull and cluster, so panning and zooming become pure client work.
  useEffect(() => {
    if (!mapReady) return;
    const controller = new AbortController();
    (async () => {
      try {
        const params = buildParams(filters, null);
        const res = await authedFetch(`/api/discover/dots?${params}`, {
          signal: controller.signal,
        });
        if (!res.ok) return;
        const geo = await res.json();
        setDotCount(geo.returned || 0);
        map.current?.getSource("trail-dots")?.setData({
          type: "FeatureCollection",
          features: geo.features || [],
        });
      } catch (err) {
        if (err.name !== "AbortError") console.warn("[map] dots", err);
      }
    })();
    return () => controller.abort();
  }, [filters, mapReady, styleEpoch]);

  // ── Fetch results whenever the viewport or filters change ───────────────────
  useEffect(() => {
    // Without a Mapbox token there is no viewport, so search statewide instead of
    // waiting forever for a bbox that will never arrive.
    if (MAPBOX_TOKEN && (!mapReady || !bbox)) return;
    const controller = new AbortController();
    const timer = setTimeout(async () => {
      setLoading(true);
      setError(null);
      try {
        const params = buildParams(filters, bbox);
        params.set("limit", "60");

        // Lines are viewport-scoped because they are large; dots are not fetched
        // here at all (see the dots effect) because panning must not refetch them.
        const requests = [
          authedFetch(`/api/discover/search?${params}`, { signal: controller.signal }),
        ];
        if (!dotMode) {
          requests.push(
            authedFetch(`/api/discover/map?${params}`, { signal: controller.signal })
          );
        }
        const [listRes, mapRes] = await Promise.all(requests);

        if (!listRes.ok) throw new Error(`Search failed (${listRes.status})`);
        const list = await listRes.json();
        setResults(list.results || []);
        setTotal(list.total || 0);
        setFacets(list.facets || null);

        const empty = { type: "FeatureCollection", features: [] };
        if (dotMode) {
          // Dots own the map at this zoom; clear the line source so the two
          // representations of the same trails are never drawn together.
          map.current?.getSource("trails")?.setData(empty);
          setTruncated(false);
        } else if (mapRes?.ok) {
          const geo = await mapRes.json();
          setTruncated(Boolean(geo.truncated));
          map.current?.getSource("trails")?.setData({
            type: "FeatureCollection",
            features: geo.features || [],
          });
          // The dots source is deliberately NOT emptied here. Emptying it left the
          // map blank on the way back out, because the dots effect keys on filters
          // rather than zoom and never refilled it. The dot layers carry a
          // `maxzoom` instead, so they hide and reappear on their own.
          map.current?.getSource("hover-route")?.setData(empty);
        }
      } catch (err) {
        if (err.name !== "AbortError") setError(err.message);
      } finally {
        setLoading(false);
      }
    }, 250); // debounce panning

    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [bbox, filters, mapReady, styleEpoch, dotMode]);

  const mapMissing = !MAPBOX_TOKEN;

  const loadTrail = useCallback(
    async (id, { fit = true } = {}) => {
      try {
        const res = await authedFetch(`/api/discover/trail/${id}`);
        if (!res.ok) throw new Error("Could not load trail");
        const trail = await res.json();
        setSelected(trail);
        if (fit && trail.bbox && map.current) {
          map.current.fitBounds(
            [
              [trail.bbox[0], trail.bbox[1]],
              [trail.bbox[2], trail.bbox[3]],
            ],
            { padding: 120, maxZoom: 14, duration: 800 }
          );
        }
      } catch (err) {
        setError(err.message);
      }
    },
    []
  );

  const activeFilterCount =
    filters.difficulty.length +
    filters.steepness.length +
    filters.features.length +
    (filters.lengthMin || filters.lengthMax ? 1 : 0) +
    (filters.gainMin || filters.gainMax ? 1 : 0) +
    (filters.routeType ? 1 : 0) +
    (filters.wildernessArea ? 1 : 0) +
    (filters.activity && filters.activity !== "hiking" ? 1 : 0) +
    (filters.month ? 1 : 0);

  const availableFeatures = useMemo(() => {
    const counts = facets?.features || {};
    const keys = new Set([...Object.keys(FEATURE_LABELS), ...Object.keys(counts)]);
    return [...keys].sort((a, b) => (counts[b] || 0) - (counts[a] || 0));
  }, [facets]);

  return (
    <div className="h-dvh flex flex-col bg-[var(--sunken)]">
      {/* Header */}
      <header className="shrink-0 border-b border-[var(--line)] bg-[var(--panel)] px-3 py-2.5 sm:px-5 sm:py-3 flex flex-wrap items-center gap-2 sm:gap-4 z-20">
        <h1 className="text-[15px] font-semibold tracking-[-0.01em] text-[var(--fg)]">OpenTrails</h1>
        {/* `flex-1` is basis:0, which beats `w-full` and kept the search box
            squeezed onto the title row on a phone. Below sm it is a full-basis
            item so flex-wrap drops it onto its own line. */}
        <div className="order-last basis-full flex-none sm:order-none sm:basis-auto sm:flex-1 sm:max-w-md">
          <input
            value={filters.q}
            onChange={(e) => update({ q: e.target.value })}
            placeholder="Search trails by name…"
            className="w-full rounded-full border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] placeholder:text-[var(--fg-3)] px-4 py-2 text-sm focus:border-[var(--accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={async () => {
            // The address bar is already current, so the link is just this URL.
            try {
              await navigator.clipboard.writeText(window.location.href);
              setCopied(true);
              setTimeout(() => setCopied(false), 2000);
            } catch {
              // Clipboard blocked (insecure origin, permissions) — select it
              // instead so the user can copy manually rather than nothing happening.
              window.prompt("Copy this link", window.location.href);
            }
          }}
          title="Copy a link to this exact view"
          aria-label="Copy a link to this view"
          className="flex cursor-pointer items-center gap-1.5 rounded-full border border-[var(--line)] px-3 py-1 text-xs text-[var(--fg-2)] transition-colors duration-200 hover:border-[var(--line-strong)] hover:text-[var(--fg)]"
        >
          <ShareIcon width={13} height={13} />
          {copied ? "Copied" : "Share"}
        </button>

        <button
          type="button"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          aria-label="Toggle colour theme"
          className="rounded-full border border-[var(--line)] px-2.5 py-1 text-sm text-[var(--fg-2)] hover:border-[var(--line-strong)]"
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </button>
        <Link to="/plan" className="hidden sm:inline text-sm text-[var(--fg-2)] hover:text-[var(--fg)]:text-[var(--fg)]">
          Trip planner
        </Link>
        <Link to="/trips" className="hidden sm:inline text-sm text-[var(--fg-2)] hover:text-[var(--fg)]:text-[var(--fg)]">
          My trips
        </Link>
      </header>

      <div className="flex-1 flex flex-col md:flex-row min-h-0">
        {/* Filters + results */}
        <aside className="order-2 md:order-1 w-full md:w-[380px] md:shrink-0 flex-1 md:flex-none border-t md:border-t-0 md:border-r border-[var(--line)] bg-[var(--panel)] flex flex-col min-h-0">
          <div className="px-4 py-3 border-b border-[var(--line)] space-y-3.5 overflow-y-auto max-h-[52%]">
            <div>
              <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
                Activity
              </p>
              <div className="flex flex-wrap gap-1.5">
                {ACTIVITIES.map(([key, label, help]) => {
                  const on = filters.activity === key;
                  const count = facets?.activity?.[key];
                  return (
                    <button
                      key={key}
                      type="button"
                      title={help}
                      // Selecting the active chip clears it, so "show me
                      // everything, jeep roads included" is one click away.
                      onClick={() => update({ activity: on ? "" : key })}
                      disabled={count === 0 && !on}
                      className={classNames(
                        "cursor-pointer rounded-full px-2.5 py-1 text-xs border transition-all duration-200",
                        on
                          ? "border-transparent bg-[var(--accent)] text-[var(--accent-fg)]"
                          : count === 0
                          ? "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                          : "border-[var(--line)] text-[var(--fg-2)] hover:border-[var(--line-strong)]"
                      )}
                    >
                      {label}
                      {count != null && (
                        <span className="ml-1 opacity-60">{count}</span>
                      )}
                    </button>
                  );
                })}
              </div>
            </div>

            <RangeRow
              label="Length"
              unit="mi"
              min={filters.lengthMin}
              max={filters.lengthMax}
              onMin={(v) => update({ lengthMin: v })}
              onMax={(v) => update({ lengthMax: v })}
              step={0.5}
            />
            <RangeRow
              label="Elevation gain"
              unit="ft"
              min={filters.gainMin}
              max={filters.gainMax}
              onMin={(v) => update({ gainMin: v })}
              onMax={(v) => update({ gainMax: v })}
              step={100}
            />

            <div>
              <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">Difficulty</p>
              <div className="flex flex-wrap gap-1.5">
                {DIFFICULTIES.map((d) => {
                  const on = filters.difficulty.includes(d);
                  const count = facets?.difficulty?.[d];
                  return (
                    <button
                      key={d}
                      type="button"
                      onClick={() => toggleIn("difficulty", d)}
                      disabled={count === 0 && !on}
                      className={classNames(
                        "cursor-pointer rounded-full px-2.5 py-1 text-xs capitalize border transition-all duration-200",
                        on
                          ? "text-white border-transparent"
                          : count === 0
                          ? "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                          : "border-[var(--line)] text-[var(--fg-2)] hover:border-[var(--line-strong)]"
                      )}
                      style={on ? { background: palette[d] } : undefined}
                    >
                      {d}
                      {count != null && <span className="ml-1 opacity-60">{count}</span>}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
                Steepness <span className="normal-case tracking-normal text-[var(--fg-3)]">· climb per mile</span>
              </p>
              <div className="flex flex-wrap gap-1.5">
                {STEEPNESS.map((level) => {
                  const on = filters.steepness.includes(level);
                  const count = facets?.steepness?.[level];
                  return (
                    <button
                      key={level}
                      type="button"
                      onClick={() => toggleIn("steepness", level)}
                      disabled={count === 0 && !on}
                      className={classNames(
                        "cursor-pointer rounded-full px-2.5 py-1 text-xs capitalize border transition-all duration-200",
                        on
                          ? "bg-[var(--accent)] text-[var(--accent-fg)] border-transparent"
                          : count === 0
                          ? "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                          : "border-[var(--line)] text-[var(--fg-2)] hover:border-[var(--line-strong)]"
                      )}
                    >
                      {level}
                      {count != null && <span className="ml-1 opacity-60">{count}</span>}
                    </button>
                  );
                })}
              </div>
            </div>

            <div>
              <div className="flex items-center justify-between mb-1.5">
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)]">Features</p>
                {filters.features.length > 1 && (
                  <button
                    type="button"
                    onClick={() =>
                      update({ featuresMode: filters.featuresMode === "any" ? "all" : "any" })
                    }
                    className="text-[10px] rounded-full border border-[var(--line)] px-2 py-0.5 text-[var(--fg-2)]"
                  >
                    match {filters.featuresMode}
                  </button>
                )}
              </div>
              {status?.coverage?.scenery_uncomputed > 0 && (
                <p className="mb-2 rounded-lg border border-amber-500/35 bg-amber-500/10 px-2.5 py-1.5 text-[11px] text-amber-700 [:root[data-theme=dark]_&]:text-amber-300">
                  Scenery data not built yet for{" "}
                  {status.coverage.scenery_uncomputed.toLocaleString()} trails — these filters will
                  under-report until the enrichment stage runs.
                </p>
              )}
              <div className="flex flex-wrap gap-1.5">
                {availableFeatures.map((f) => {
                  const on = filters.features.includes(f);
                  const count = facets?.features?.[f] || 0;
                  return (
                    <button
                      key={f}
                      type="button"
                      onClick={() => toggleIn("features", f)}
                      disabled={!count && !on}
                      className={classNames(
                        "cursor-pointer rounded-full px-2.5 py-1 text-xs border transition-all duration-200",
                        on
                          ? "bg-[var(--accent)] text-[var(--accent-fg)] border-transparent"
                          : count
                          ? "border-[var(--line)] text-[var(--fg-2)] hover:border-[var(--line-strong)]"
                          : "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                      )}
                    >
                      {FEATURE_LABELS[f] || f}
                      {count > 0 && <span className="ml-1 opacity-60">{count}</span>}
                    </button>
                  );
                })}
              </div>
            </div>

            {Object.keys(facets?.wilderness_area || {}).length > 0 && (
              <div>
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
                  Wilderness
                </p>
                <select
                  value={filters.wildernessArea}
                  onChange={(e) => update({ wildernessArea: e.target.value })}
                  className="w-full rounded-lg border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] px-2 py-1.5 text-sm"
                >
                  <option value="">Any area</option>
                  {Object.entries(facets?.wilderness_area || {}).map(([area, count]) => (
                    <option key={area} value={area}>
                      {area} ({count})
                    </option>
                  ))}
                </select>
              </div>
            )}

            <div className="flex gap-2">
              <div className="flex-1">
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">Route</p>
                <select
                  value={filters.routeType}
                  onChange={(e) => update({ routeType: e.target.value })}
                  className="w-full rounded-lg border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] px-2 py-1.5 text-sm"
                >
                  <option value="">Any</option>
                  <option value="loop">Loop</option>
                  <option value="out-and-back">Out &amp; back</option>
                </select>
              </div>
              <div className="flex-1">
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
                  Open in
                </p>
                <select
                  value={filters.month}
                  onChange={(e) => update({ month: e.target.value })}
                  className="w-full rounded-lg border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] px-2 py-1.5 text-sm"
                >
                  <option value="">Any month</option>
                  {MONTHS.map((m, i) => (
                    <option key={m} value={i + 1}>
                      {m}
                    </option>
                  ))}
                </select>
              </div>
            </div>

            {activeFilterCount > 0 && (
              <button
                type="button"
                onClick={() =>
                  setFilters({
                    q: filters.q,
                    lengthMin: "",
                    lengthMax: "",
                    gainMin: "",
                    gainMax: "",
                    difficulty: [],
                    steepness: [],
                    features: [],
                    featuresMode: "any",
                    routeType: "",
                    wildernessArea: "",
                    activity: "hiking",
                    month: "",
                    sort: filters.sort,
                  })
                }
                className="text-xs text-[var(--fg-2)] underline"
              >
                Clear {activeFilterCount} filter{activeFilterCount > 1 ? "s" : ""}
              </button>
            )}
          </div>

          {/* Results */}
          <div className="px-4 py-2.5 border-b border-[var(--line)] flex items-center justify-between">
            <p className="text-xs text-[var(--fg-2)]">
              {loading ? "Searching…" : `${total.toLocaleString()} trail${total === 1 ? "" : "s"} in view`}
            </p>
            <select
              value={filters.sort}
              onChange={(e) => update({ sort: e.target.value })}
              className="text-xs border-none text-[var(--fg-2)] focus:outline-none bg-transparent [&>option]:bg-[var(--panel)] [&>option]:text-[var(--fg)]"
            >
              <option value="relevance">Relevance</option>
              <option value="length">Longest</option>
              <option value="length_asc">Shortest</option>
              <option value="gain">Most gain</option>
              <option value="difficulty">Hardest</option>
              <option value="steepness">Steepest</option>
              <option value="name">Name</option>
            </select>
          </div>

          <div className="flex-1 overflow-y-auto px-4 py-3 space-y-2">
            {error && (
              <div className="rounded-lg border border-red-500/35 bg-red-500/10 px-3 py-2 text-xs text-red-700 [:root[data-theme=dark]_&]:text-red-300">
                {error}
              </div>
            )}
            {truncated && (
              <div className="rounded-lg border border-[var(--line)] bg-[var(--sunken)] px-3 py-2 text-[11px] text-[var(--fg-2)]">
                Showing the first {results.length} of {total.toLocaleString()} matches — zoom in to
                see the rest.
              </div>
            )}
            {!loading && !results.length && !error && (
              <p className="text-xs text-[var(--fg-3)] text-center py-8">
                No trails match here. Try panning the map or relaxing a filter.
              </p>
            )}
            {results.map((trail) => (
              <TrailCard
                key={trail.id}
                trail={trail}
                active={hovered === trail.id || selected?.id === trail.id}
                comparing={inCompare(trail.id)}
                palette={palette}
                compareFull={compare.length >= MAX_COMPARE}
                onHover={setHovered}
                onSelect={(t) => loadTrail(t.id)}
                onCompare={toggleCompare}
              />
            ))}
          </div>
        </aside>

        {/* Map */}
        <main className="order-1 md:order-2 relative min-w-0 h-[42vh] shrink-0 md:h-auto md:flex-1 md:shrink">
          {/* h-full/w-full rather than `absolute inset-0`: mapbox-gl.css sets
              `.mapboxgl-map { position: relative }`, which overrides Tailwind's
              `absolute`, leaving inset-0 inert and the container 0px tall — and
              Mapbox never fires `load` on a zero-height container. */}
          <div ref={mapContainer} className="h-full w-full" />

          <DotHoverCard card={hoverCard} />

          {mapMissing && (
            <div className="absolute inset-0 grid place-items-center bg-[var(--chip)] px-8">
              <div className="max-w-sm rounded-2xl border border-amber-500/35 bg-amber-500/10 p-5 text-sm text-amber-800 [:root[data-theme=dark]_&]:text-amber-200">
                <p className="font-semibold mb-1.5">Map needs a Mapbox token</p>
                <p className="text-[13px] leading-relaxed">
                  Search, filters, elevation profiles and comparison all work without it —
                  only the map is blank. Add{" "}
                  <code className="font-mono text-xs bg-[var(--panel)]/70 px-1 rounded">VITE_MAPBOX_TOKEN</code>{" "}
                  to <code className="font-mono text-xs bg-[var(--panel)]/70 px-1 rounded">frontend/.env</code>{" "}
                  (or the compose env) and rebuild.
                </p>
                <a
                  href="https://account.mapbox.com/access-tokens/"
                  target="_blank"
                  rel="noreferrer"
                  className="mt-2 inline-block text-xs underline"
                >
                  Get a free token →
                </a>
              </div>
            </div>
          )}

          <CompareTray
            trails={compare}
            palette={palette}
            onRemove={(id) => setCompare((c) => c.filter((t) => t.id !== id))}
            onClear={() => setCompare([])}
            onFocus={(t) => loadTrail(t.id)}
          />

          {/* Basemap + 3D controls */}
          <div className="absolute left-4 top-4 z-10 flex flex-col gap-2">
            <div className="flex overflow-hidden rounded-lg border border-[var(--line)] bg-[var(--glass)] backdrop-blur-xl shadow-[var(--e2)]">
              {Object.entries(BASEMAPS).map(([key, config]) => (
                <button
                  key={key}
                  type="button"
                  onClick={() => setBasemap(key)}
                  aria-pressed={basemap === key}
                  className={classNames(
                    "cursor-pointer px-3 py-1.5 text-xs font-medium transition-colors duration-200",
                    basemap === key
                      ? "bg-[var(--accent)] text-[var(--accent-fg)]"
                      : "text-[var(--fg-2)] hover:text-[var(--fg)]"
                  )}
                >
                  {config.label}
                </button>
              ))}
            </div>

            <button
              type="button"
              onClick={() => setPitched((p) => !p)}
              aria-pressed={pitched}
              title={pitched ? "Back to flat view" : "Tilt into 3D terrain"}
              className={classNames(
                "cursor-pointer self-start rounded-lg border px-3 py-1.5 text-xs font-medium transition-all duration-200 backdrop-blur-xl shadow-[var(--e2)]",
                pitched
                  ? "border-transparent bg-[var(--accent)] text-[var(--accent-fg)]"
                  : "border-[var(--line)] bg-[var(--glass)] text-[var(--fg-2)] hover:text-[var(--fg)]"
              )}
            >
              {pitched ? "2D" : "3D"}
            </button>

            <button
              type="button"
              onClick={() => {
                setPlanMode((on) => {
                  if (on) clearPlan();
                  return !on;
                });
              }}
              aria-pressed={planMode}
              title="Compose a hike between two points on the trail network"
              className={classNames(
                "cursor-pointer self-start rounded-lg border px-3 py-1.5 text-xs font-medium transition-all duration-200 backdrop-blur-xl shadow-[var(--e2)]",
                planMode
                  ? "border-transparent bg-[var(--accent)] text-[var(--accent-fg)]"
                  : "border-[var(--line)] bg-[var(--glass)] text-[var(--fg-2)] hover:text-[var(--fg)]"
              )}
            >
              {planMode ? "Planning…" : "Plan a hike"}
            </button>
          </div>

          {planMode && (
            <div className="absolute inset-x-3 top-[7.5rem] z-10 w-auto md:inset-x-auto md:left-4 md:top-[8.5rem] md:w-[300px] max-h-[60%] overflow-y-auto rounded-xl border border-[var(--line)] bg-[var(--panel)]/95 p-3.5 shadow-xl backdrop-blur">
              <div className="flex items-start justify-between gap-2">
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                  Plan a hike
                </p>
                <button
                  type="button"
                  onClick={clearPlan}
                  className="cursor-pointer text-[11px] text-[var(--fg-3)] underline hover:text-[var(--fg)]"
                >
                  Reset
                </button>
              </div>

              {!planStart && (
                <p className="mt-1.5 text-xs text-[var(--fg-2)]">
                  Click the map to set your <strong className="text-[var(--fg)]">start</strong>.
                </p>
              )}
              {planStart && !planEnd && (
                <p className="mt-1.5 text-xs text-[var(--fg-2)]">
                  Now click your <strong className="text-[var(--fg)]">destination</strong>.
                </p>
              )}

              {planLoading && (
                <p className="mt-2 text-xs text-[var(--fg-2)]">
                  Routing…{" "}
                  <span className="text-[var(--fg-3)]">
                    the first request builds the trail network and can take a minute.
                  </span>
                </p>
              )}

              {planError && !planLoading && (
                <p className="mt-2 rounded-lg border border-amber-500/40 bg-amber-500/10 px-2.5 py-2 text-[11px] leading-relaxed text-amber-800 [:root[data-theme=dark]_&]:text-amber-200">
                  {planError}
                </p>
              )}

              {planRoute && !planLoading && (
                <div className="mt-2.5">
                  <div className="flex items-baseline gap-3">
                    <span className="text-lg font-semibold text-[var(--fg)]">
                      {planRoute.miles} mi
                    </span>
                    <span className="text-xs text-[var(--fg-2)]">
                      ↑ {Math.round(planRoute.gain_ft).toLocaleString()} ft
                    </span>
                  </div>

                  <label className="mt-2 flex cursor-pointer items-center gap-2 text-xs text-[var(--fg-2)]">
                    <input
                      type="checkbox"
                      checked={planOutAndBack}
                      onChange={(e) => setPlanOutAndBack(e.target.checked)}
                      className="cursor-pointer accent-[var(--accent)]"
                    />
                    Return the same way
                    <span className="text-[var(--fg-3)]">
                      ({planRoute.one_way_miles} mi each way)
                    </span>
                  </label>

                  <p className="mt-2.5 text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                    {planRoute.trail_names?.length || 0} trail
                    {planRoute.trail_names?.length === 1 ? "" : "s"}
                  </p>
                  <ol className="mt-1 max-h-40 space-y-0.5 overflow-y-auto text-xs">
                    {(planRoute.segments || []).map((seg, i) => (
                      <li key={i} className="flex justify-between gap-2">
                        <span className="truncate text-[var(--fg)]">{seg.name}</span>
                        <span className="shrink-0 text-[var(--fg-3)]">{seg.miles} mi</span>
                      </li>
                    ))}
                  </ol>
                </div>
              )}
            </div>
          )}

          {/* Legend */}
          <div
            style={{ bottom: compare.length ? "16.5rem" : "1rem" }}
            className="absolute left-4 hidden sm:block rounded-xl bg-[var(--glass)] backdrop-blur-xl px-3 py-2.5 shadow-[var(--e2)] text-[11px] transition-all">
            <p className="uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">Difficulty</p>
            <div className="space-y-1">
              {DIFFICULTIES.map((d) => (
                <div key={d} className="flex items-center gap-2">
                  {/* The swatch matches what is actually on the map at this zoom:
                      a dot while browsing, a line once trails are drawn. */}
                  {dotMode ? (
                    <span
                      className="h-2 w-2 rounded-full shrink-0"
                      style={{ background: palette[d] }}
                    />
                  ) : (
                    <span className="h-0.5 w-5 rounded" style={{ background: palette[d] }} />
                  )}
                  <span className="capitalize text-[var(--fg-2)]">{d}</span>
                </div>
              ))}
              <div className="flex items-center gap-2 pt-0.5 border-t border-[var(--line)] mt-1">
                {dotMode ? (
                  <span
                    className="h-2 w-2 rounded-full shrink-0"
                    style={{ background: palette.unknown }}
                  />
                ) : (
                  <span className="h-0.5 w-5 rounded" style={{ background: palette.unknown }} />
                )}
                <span className="text-[var(--fg-2)]">unrated</span>
              </div>
            </div>

            {dotMode && (
              <div className="mt-2 pt-2 border-t border-[var(--line)] space-y-1 text-[10px] text-[var(--fg-3)]">
                <div className="flex items-center gap-2">
                  <span className="flex shrink-0 items-end gap-1">
                    <span className="h-1.5 w-1.5 rounded-full bg-[var(--fg-3)]" />
                    <span className="h-2.5 w-2.5 rounded-full bg-[var(--fg-3)]" />
                  </span>
                  <span>bigger dot = longer trail</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="grid h-4 w-4 shrink-0 place-items-center rounded-full bg-[var(--accent)]/70 text-[7px] font-semibold text-[var(--accent-fg)]">
                    12
                  </span>
                  <span>trails grouped — click to zoom in</span>
                </div>
                <div className="flex items-center gap-2">
                  <span className="h-2.5 w-2.5 shrink-0 rounded-full border-2 border-white/90 bg-[var(--fg-3)]" />
                  <span>white ring = mapped trailhead</span>
                </div>
              </div>
            )}
          </div>

          {/* Detail panel */}
          {selected && (
            // z-30: full width on a phone, the panel would otherwise interleave
            // with the map control buttons and they would show through it. On a
            // 42vh map, covering the controls while a trail is open is correct.
            <div className="absolute inset-x-3 top-3 z-30 w-auto md:inset-x-auto md:right-4 md:top-4 md:w-[400px] max-h-[calc(100%-1.5rem)] md:max-h-[calc(100%-2rem)] overflow-y-auto rounded-2xl bg-[var(--panel)] shadow-[var(--shadow-[var(--e2)])]">
              <div className="px-5 py-4 border-b border-[var(--line)] flex items-start justify-between gap-3">
                <div>
                  <h2 className="font-semibold text-[var(--fg)] leading-snug">{selected.name}</h2>
                  {selected.mgmt_area && (
                    <p className="text-xs text-[var(--fg-2)] mt-0.5">{selected.mgmt_area}</p>
                  )}
                </div>
                <button
                  type="button"
                  onClick={() => setSelected(null)}
                  className="rounded-md p-1 text-[var(--fg-3)] transition-colors duration-150 hover:bg-[var(--chip)] hover:text-[var(--fg)]"
                  aria-label="Close trail details"
                >
                  <CloseIcon />
                </button>
              </div>

              <div className="px-5 py-4 space-y-4">
                <PhotoStrip trailId={selected.id} />

                <div className="grid grid-cols-3 gap-3 text-center">
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">Length</p>
                    <p className="font-semibold text-[var(--fg)] mt-0.5">{selected.length_miles} mi</p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">Gain</p>
                    <p className="font-semibold text-[var(--fg)] mt-0.5">
                      {selected.gain_ft != null ? `${selected.gain_ft.toLocaleString()} ft` : "—"}
                    </p>
                  </div>
                  <div>
                    <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">Peak</p>
                    <p className="font-semibold text-[var(--fg)] mt-0.5">
                      {selected.max_elevation_ft != null
                        ? `${selected.max_elevation_ft.toLocaleString()} ft`
                        : "—"}
                    </p>
                  </div>
                </div>

                {(selected.difficulty || selected.steepness) && (
                  <div className="rounded-xl bg-[var(--sunken)] px-3.5 py-2.5 space-y-2">
                    {selected.difficulty && (
                      <div>
                        <div className="flex items-center justify-between">
                          <span className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                            Effort
                          </span>
                          <span
                            className="text-sm font-semibold capitalize"
                            style={{ color: palette[selected.difficulty.label] }}
                          >
                            {selected.difficulty.label}
                          </span>
                        </div>
                        <p className="text-[10px] text-[var(--fg-3)]">{selected.difficulty.formula}</p>
                      </div>
                    )}
                    {selected.steepness && (
                      <div className="pt-1.5 border-t border-[var(--line)]">
                        <div className="flex items-center justify-between">
                          <span className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">
                            Steepness
                          </span>
                          <span className="text-sm font-semibold capitalize text-[var(--fg)]">
                            {selected.steepness.label}
                            <span className="ml-1.5 font-normal text-xs text-[var(--fg-2)]">
                              {selected.steepness.ft_per_mi} ft/mi
                            </span>
                          </span>
                        </div>
                        <p className="text-[10px] text-[var(--fg-3)]">{selected.steepness.basis}</p>
                      </div>
                    )}
                  </div>
                )}

                {selected.technical && (
                  <div className="rounded-xl border border-[var(--line)] bg-[var(--sunken)] px-3.5 py-2.5">
                    {selected.technical.sac_label && (
                      <p className="text-xs font-medium text-[var(--fg)]">
                        {selected.technical.sac_label}
                      </p>
                    )}
                    {selected.technical.visibility_label && (
                      <p className="mt-0.5 text-xs text-[var(--fg-2)]">
                        {selected.technical.visibility_label}
                      </p>
                    )}
                    <p className="mt-1 text-[10px] text-[var(--fg-3)]">
                      OpenStreetMap terrain assessment
                    </p>
                  </div>
                )}

                <WildernessNotice wilderness={selected.wilderness} />

                <PermitNotice permits={selected.permits} />

                <ElevationProfile
                  profile={selected.elevation?.profile}
                  gain={selected.gain_ft}
                  loss={selected.elevation?.loss_ft}
                  onScrub={handleScrub}
                />

                {selected.nearby?.length > 0 && (
                  <div>
                    <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
                      Along the way
                    </p>
                    <div className="space-y-1">
                      {selected.nearby.slice(0, 6).map((n, i) => (
                        <div key={i} className="flex justify-between text-xs">
                          <span className="text-[var(--fg)]">
                            {n.name || FEATURE_LABELS[n.kind] || n.kind}
                          </span>
                          <span className="text-[var(--fg-3)]">{n.distance_mi} mi</span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}

                {(selected.access || {}).trailhead && (
                  <div className="flex items-baseline justify-between gap-3 text-xs">
                    <span className="text-[var(--fg-3)]">Trailhead</span>
                    <span className="text-right text-[var(--fg)]">
                      {/* OSM trailheads are frequently unnamed; a located but
                          unnamed trailhead is still worth showing. */}
                      {selected.access.trailhead.name || "Mapped trailhead"}
                      <span className="text-[var(--fg-3)]">
                        {` · ${selected.access.trailhead.distance_mi} mi`}
                      </span>
                      {selected.access.parking && (
                        <span className="text-[var(--fg-3)]"> · parking</span>
                      )}
                    </span>
                  </div>
                )}

                {ACCESS_ROWS.map(([key, label]) => {
                  const hit = (selected.access || {})[key];
                  if (!hit) return null;
                  const detail = hit.details || {};
                  return (
                    <div
                      key={key}
                      className="flex items-baseline justify-between gap-3 text-xs"
                    >
                      <span className="text-[var(--fg-3)]">{label}</span>
                      <span className="text-right text-[var(--fg)]">
                        {hit.name || label}
                        <span className="text-[var(--fg-3)]">
                          {` · ${hit.distance_mi} mi`}
                          {detail.water ? " · water" : ""}
                          {detail.fee === "Y" ? " · fee" : ""}
                        </span>
                      </span>
                    </div>
                  );
                })}

                <dl className="text-xs space-y-1.5 pt-1 border-t border-[var(--line)]">
                  {selected.grade && (
                    <div className="flex justify-between">
                      <dt className="text-[var(--fg-2)]">Typical grade</dt>
                      <dd className="text-[var(--fg)]">{selected.grade.label}</dd>
                    </div>
                  )}
                  {selected.surface && (
                    <div className="flex justify-between">
                      <dt className="text-[var(--fg-2)]">Surface</dt>
                      <dd className="text-[var(--fg)]">{selected.surface}</dd>
                    </div>
                  )}
                  {selected.season && !selected.season.year_round && (
                    <div className="flex justify-between">
                      <dt className="text-[var(--fg-2)]">Season</dt>
                      <dd className="text-[var(--fg)]">{selected.season.label}</dd>
                    </div>
                  )}
                  {selected.trail_class_label && (
                    <div className="flex justify-between">
                      <dt className="text-[var(--fg-2)]">Trail class</dt>
                      <dd className="text-[var(--fg)] capitalize">{selected.trail_class_label}</dd>
                    </div>
                  )}
                </dl>

                <div className="flex gap-2">
                  <button
                    type="button"
                    onClick={() => toggleCompare(selected)}
                    disabled={!inCompare(selected.id) && compare.length >= MAX_COMPARE}
                    className={classNames(
                      "flex-1 rounded-xl px-4 py-2.5 text-sm font-semibold border transition",
                      inCompare(selected.id)
                        ? "bg-[var(--fg)] text-white border-transparent"
                        : compare.length >= MAX_COMPARE
                        ? "border-[var(--line)] text-[var(--fg-3)] cursor-not-allowed"
                        : "border-[var(--line-strong)] text-[var(--fg)] hover:border-[var(--line-strong)]"
                    )}
                  >
                    {inCompare(selected.id) ? "In comparison" : "Compare"}
                  </button>
                  <Link
                    to={`/plan?trail=${encodeURIComponent(selected.id)}`}
                    className="flex-1 rounded-xl bg-[var(--accent)] px-4 py-2.5 text-center text-sm font-semibold text-[var(--accent-fg)] hover:opacity-90"
                  >
                    Conditions
                  </Link>
                </div>
              </div>
            </div>
          )}

          {/* Data provenance — what this index does and does not cover. */}
          {status?.available && (
            <div
              style={{ bottom: compare.length ? "16.5rem" : "1rem" }}
              className="absolute right-16 rounded-lg bg-[var(--glass)] backdrop-blur-xl px-2.5 py-1.5 text-[10px] text-[var(--fg-2)] shadow transition-all">
              {status.count.toLocaleString()} trails · {status.sources?.trails || "USFS"} ·
              elevation {status.sources?.elevation || "DEM"}
            </div>
          )}
        </main>
      </div>
    </div>
  );
}
