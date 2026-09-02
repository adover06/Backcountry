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

function initialBasemap() {
  try {
    const saved = localStorage.getItem("opentrails-basemap");
    if (saved && BASEMAPS[saved]) return saved;
  } catch {
    // storage unavailable
  }
  return "terrain";
}

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
};

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
  const [basemap, setBasemap] = useState(initialBasemap);
  const [pitched, setPitched] = useState(false);

  const [filters, setFilters] = useState({
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
    month: "",
    sort: "relevance",
  });

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
      center: INITIAL_VIEW.center,
      zoom: INITIAL_VIEW.zoom,
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
        const id = e.features?.[0]?.properties?.id;
        if (id) loadTrail(id);
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

  const handleScrub = useCallback(
    (mi) => {
      if (mi == null) {
        setScrubPoint(null);
        return;
      }
      setScrubPoint(pointAtMile(selectedCoords, mi));
    },
    [selectedCoords]
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

        const [listRes, mapRes] = await Promise.all([
          authedFetch(`/api/discover/search?${params}`, { signal: controller.signal }),
          authedFetch(`/api/discover/map?${params}`, { signal: controller.signal }),
        ]);

        if (!listRes.ok) throw new Error(`Search failed (${listRes.status})`);
        const list = await listRes.json();
        setResults(list.results || []);
        setTotal(list.total || 0);
        setFacets(list.facets || null);

        if (mapRes.ok) {
          const geo = await mapRes.json();
          setTruncated(Boolean(geo.truncated));
          map.current?.getSource("trails")?.setData({
            type: "FeatureCollection",
            features: geo.features || [],
          });
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
  }, [bbox, filters, mapReady, styleEpoch]);

  const mapMissing = !MAPBOX_TOKEN;

  const loadTrail = useCallback(
    async (id) => {
      try {
        const res = await authedFetch(`/api/discover/trail/${id}`);
        if (!res.ok) throw new Error("Could not load trail");
        const trail = await res.json();
        setSelected(trail);
        if (trail.bbox && map.current) {
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
    (filters.month ? 1 : 0);

  const availableFeatures = useMemo(() => {
    const counts = facets?.features || {};
    const keys = new Set([...Object.keys(FEATURE_LABELS), ...Object.keys(counts)]);
    return [...keys].sort((a, b) => (counts[b] || 0) - (counts[a] || 0));
  }, [facets]);

  return (
    <div className="h-screen flex flex-col bg-[var(--sunken)]">
      {/* Header */}
      <header className="shrink-0 border-b border-[var(--line)] bg-[var(--panel)] px-5 py-3 flex items-center gap-4 z-20">
        <h1 className="text-[15px] font-semibold tracking-[-0.01em] text-[var(--fg)]">OpenTrails</h1>
        <div className="flex-1 max-w-md">
          <input
            value={filters.q}
            onChange={(e) => update({ q: e.target.value })}
            placeholder="Search trails by name…"
            className="w-full rounded-full border border-[var(--line)] bg-[var(--sunken)] text-[var(--fg)] placeholder:text-[var(--fg-3)] px-4 py-2 text-sm focus:border-[var(--accent)] focus:outline-none"
          />
        </div>
        <button
          type="button"
          onClick={() => setTheme((t) => (t === "dark" ? "light" : "dark"))}
          title={theme === "dark" ? "Switch to light" : "Switch to dark"}
          aria-label="Toggle colour theme"
          className="rounded-full border border-[var(--line)] px-2.5 py-1 text-sm text-[var(--fg-2)] hover:border-[var(--line-strong)]"
        >
          {theme === "dark" ? <SunIcon /> : <MoonIcon />}
        </button>
        <Link to="/plan" className="text-sm text-[var(--fg-2)] hover:text-[var(--fg)]:text-[var(--fg)]">
          Trip planner
        </Link>
        <Link to="/trips" className="text-sm text-[var(--fg-2)] hover:text-[var(--fg)]:text-[var(--fg)]">
          My trips
        </Link>
      </header>

      <div className="flex-1 flex min-h-0">
        {/* Filters + results */}
        <aside className="w-[380px] shrink-0 border-r border-[var(--line)] bg-[var(--panel)] flex flex-col min-h-0">
          <div className="px-4 py-3 border-b border-[var(--line)] space-y-3.5 overflow-y-auto max-h-[52%]">
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
        <main className="flex-1 relative min-w-0">
          {/* h-full/w-full rather than `absolute inset-0`: mapbox-gl.css sets
              `.mapboxgl-map { position: relative }`, which overrides Tailwind's
              `absolute`, leaving inset-0 inert and the container 0px tall — and
              Mapbox never fires `load` on a zero-height container. */}
          <div ref={mapContainer} className="h-full w-full" />

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
          </div>

          {/* Legend */}
          <div
            style={{ bottom: compare.length ? "16.5rem" : "1rem" }}
            className="absolute left-4 rounded-xl bg-[var(--glass)] backdrop-blur-xl px-3 py-2.5 shadow-[var(--e2)] text-[11px] transition-all">
            <p className="uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">Difficulty</p>
            <div className="space-y-1">
              {DIFFICULTIES.map((d) => (
                <div key={d} className="flex items-center gap-2">
                  <span className="h-0.5 w-5 rounded" style={{ background: palette[d] }} />
                  <span className="capitalize text-[var(--fg-2)]">{d}</span>
                </div>
              ))}
              <div className="flex items-center gap-2 pt-0.5 border-t border-[var(--line)] mt-1">
                <span className="h-0.5 w-5 rounded" style={{ background: palette.unknown }} />
                <span className="text-[var(--fg-2)]">unrated</span>
              </div>
            </div>
          </div>

          {/* Detail panel */}
          {selected && (
            <div className="absolute top-4 right-4 w-[400px] max-h-[calc(100%-2rem)] overflow-y-auto rounded-2xl bg-[var(--panel)] shadow-[var(--shadow-[var(--e2)])]">
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
