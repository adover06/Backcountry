import React, { useCallback, useEffect, useRef, useState } from "react";
import mapboxgl from "mapbox-gl";
import { Link } from "react-router-dom";
import { authedFetch } from "../api/client";

const MAPBOX_TOKEN = import.meta.env.VITE_MAPBOX_TOKEN;

// Hues walk by the golden angle so neighbouring legs land far apart on the wheel —
// picking each colour at random gives near-duplicates often enough to defeat the
// point, and re-running a route would reshuffle the map under you.
//
// Keyed by name, matching how the server groups legs. Happy Isles to Half Dome
// leaves the John Muir Trail for the Mist Trail and rejoins it, and those two
// stretches must read as one trail in two colours' worth of map — keying by
// trail_id would paint them differently, because overlapping source records give
// each run a different id.
const GOLDEN_ANGLE = 137.508;

function colorSegments(segments) {
  const byTrail = new Map();
  return segments.map((segment) => {
    if (!byTrail.has(segment.name)) {
      byTrail.set(segment.name, `hsl(${(byTrail.size * GOLDEN_ANGLE) % 360}, 68%, 45%)`);
    }
    return { ...segment, color: byTrail.get(segment.name) };
  });
}

/**
 * Routing-graph inspector.
 *
 * The graph is the answer to "Half Dome Trail is 2 miles" — it composes segments
 * into an actual hike. Until now it could only be exercised from a Python shell,
 * which made it impossible to see where it works and where it does not. Click two
 * points and it shows the route, the trails it strings together, and the real
 * distance and climb.
 *
 * Deliberately a debug surface, not a product one: it exposes node and edge counts,
 * snap distances and failure reasons, because the useful question here is "why did
 * that not route" rather than "which hike should I do".
 */
export default function GraphPage() {
  const mapContainer = useRef(null);
  const map = useRef(null);
  const [ready, setReady] = useState(false);

  const [start, setStart] = useState(null);
  const [end, setEnd] = useState(null);
  const [result, setResult] = useState(null);
  const [segments, setSegments] = useState([]);
  const [busy, setBusy] = useState(false);
  const [status, setStatus] = useState(null);
  const [outAndBack, setOutAndBack] = useState(true);
  const [snap, setSnap] = useState(0.25);

  const startRef = useRef(null);
  const endRef = useRef(null);

  useEffect(() => {
    authedFetch("/api/discover/graph/status")
      .then((r) => r.json())
      .then(setStatus)
      .catch(() => setStatus({ loaded: false, error: "unreachable" }));
  }, []);

  // ── Map ────────────────────────────────────────────────────────────────────
  useEffect(() => {
    if (map.current || !mapContainer.current || !MAPBOX_TOKEN) return;
    mapboxgl.accessToken = MAPBOX_TOKEN;

    map.current = new mapboxgl.Map({
      container: mapContainer.current,
      style: "mapbox://styles/mapbox/outdoors-v12",
      center: [-119.5495, 37.7405],
      zoom: 12.2,
    });
    map.current.addControl(new mapboxgl.NavigationControl(), "top-right");
    map.current.addControl(new mapboxgl.ScaleControl({ unit: "imperial" }), "bottom-right");

    const install = () => {
      if (map.current.getSource("route")) return;
      map.current.addSource("route", { type: "geojson", data: empty() });
      map.current.addLayer({
        id: "route-casing",
        type: "line",
        source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#ffffff", "line-width": 8, "line-opacity": 0.9 },
      });
      map.current.addLayer({
        id: "route-line",
        type: "line",
        source: "route",
        layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": ["get", "color"], "line-width": 4 },
      });
      // One tag per leg, at the middle of that leg, so the map says which trail
      // each colour is without a trip back to the sidebar.
      map.current.addLayer({
        id: "route-label",
        type: "symbol",
        source: "route",
        layout: {
          "symbol-placement": "line-center",
          "text-field": ["get", "name"],
          "text-size": 11,
          "text-font": ["DIN Offc Pro Medium", "Arial Unicode MS Bold"],
          "text-padding": 4,
        },
        paint: {
          "text-color": ["get", "color"],
          "text-halo-color": "#ffffff",
          "text-halo-width": 2,
        },
      });

      map.current.addSource("pins", { type: "geojson", data: empty() });
      map.current.addLayer({
        id: "pins-halo",
        type: "circle",
        source: "pins",
        paint: { "circle-radius": 13, "circle-color": ["get", "color"], "circle-opacity": 0.22 },
      });
      map.current.addLayer({
        id: "pins-dot",
        type: "circle",
        source: "pins",
        paint: {
          "circle-radius": 7,
          "circle-color": ["get", "color"],
          "circle-stroke-width": 2.5,
          "circle-stroke-color": "#ffffff",
        },
      });

      map.current.resize();
      map.current.jumpTo({ center: map.current.getCenter(), zoom: map.current.getZoom() + 0.0001 });
      setReady(true);
    };

    map.current.on("style.load", install);
    map.current.on("load", install);
    const fallback = setTimeout(() => {
      if (map.current?.getStyle()?.layers?.length) install();
    }, 2000);

    map.current.on("click", (e) => {
      const point = [Number(e.lngLat.lng.toFixed(6)), Number(e.lngLat.lat.toFixed(6))];
      // First click sets the start; second sets the destination; third starts over.
      if (!startRef.current || (startRef.current && endRef.current)) {
        startRef.current = point;
        endRef.current = null;
        setStart(point);
        setEnd(null);
        setResult(null);
      } else {
        endRef.current = point;
        setEnd(point);
      }
    });

    return () => clearTimeout(fallback);
  }, []);

  const empty = () => ({ type: "FeatureCollection", features: [] });

  // Draw the two pins.
  useEffect(() => {
    const source = map.current?.getSource?.("pins");
    if (!source) return;
    const features = [];
    if (start) features.push(pin(start, "#16a34a"));
    if (end) features.push(pin(end, "#dc2626"));
    source.setData({ type: "FeatureCollection", features });
  }, [start, end, ready]);

  const pin = (coord, color) => ({
    type: "Feature",
    geometry: { type: "Point", coordinates: coord },
    properties: { color },
  });

  // Route whenever both ends are set.
  const route = useCallback(async () => {
    if (!start || !end) return;
    setBusy(true);
    setResult(null);
    setSegments([]);
    try {
      const params = new URLSearchParams({
        start: start.join(","),
        end: end.join(","),
        out_and_back: String(outAndBack),
        snap: String(snap),
      });
      const res = await authedFetch(`/api/discover/graph/route?${params}`);
      const data = await res.json();
      setResult(data);

      const legs = data.ok ? colorSegments(data.segments || []) : [];
      setSegments(legs);

      const source = map.current?.getSource?.("route");
      if (source) {
        source.setData(
          legs.length
            ? {
                type: "FeatureCollection",
                features: legs.map((leg, i) => ({
                  type: "Feature",
                  geometry: leg.geometry,
                  properties: { name: leg.name, color: leg.color, miles: leg.miles, index: i },
                })),
              }
            : data.ok
              // A route with no legs should still draw. Falling through to the
              // merged line beats an empty map that looks like a routing failure.
              ? {
                  type: "FeatureCollection",
                  features: [
                    { type: "Feature", geometry: data.geometry, properties: { name: "", color: "#7c3aed" } },
                  ],
                }
              : empty()
        );
      }
      // Refresh status: the first route is what builds the graph.
      authedFetch("/api/discover/graph/status").then((r) => r.json()).then(setStatus).catch(() => {});
    } catch (err) {
      setSegments([]);
      setResult({ ok: false, reason: "request_failed", detail: err.message });
    } finally {
      setBusy(false);
    }
  }, [start, end, outAndBack, snap]);

  useEffect(() => {
    if (start && end) route();
  }, [start, end, route]);

  const reset = () => {
    startRef.current = null;
    endRef.current = null;
    setStart(null);
    setEnd(null);
    setResult(null);
    setSegments([]);
    map.current?.getSource?.("route")?.setData(empty());
  };

  const preset = (a, b, label) => (
    <button
      type="button"
      onClick={() => {
        startRef.current = a;
        endRef.current = b;
        setStart(a);
        setEnd(b);
        map.current?.fitBounds(
          [
            [Math.min(a[0], b[0]), Math.min(a[1], b[1])],
            [Math.max(a[0], b[0]), Math.max(a[1], b[1])],
          ],
          { padding: 120, duration: 700 }
        );
      }}
      className="cursor-pointer rounded-md border border-[var(--line)] px-2 py-1 text-[11px] text-[var(--fg-2)] transition-colors duration-200 hover:border-[var(--line-strong)] hover:text-[var(--fg)]"
    >
      {label}
    </button>
  );

  return (
    <div className="h-screen flex flex-col bg-[var(--canvas)] text-[var(--fg)] font-[var(--font-ui)] antialiased">
      <header className="shrink-0 border-b border-[var(--line)] bg-[var(--panel)] px-5 py-3 flex items-center gap-4">
        <Link to="/" className="text-[15px] font-semibold tracking-[-0.01em]">
          OpenTrails
        </Link>
        <span className="rounded-full bg-[var(--chip)] px-2.5 py-0.5 text-[11px] text-[var(--fg-2)]">
          routing graph inspector
        </span>
        <div className="ml-auto text-[11px] text-[var(--fg-3)]">
          {status?.loaded ? (
            <>
              {status.nodes.toLocaleString()} nodes · {status.edges.toLocaleString()} edges ·{" "}
              {status.nodes_with_elevation.toLocaleString()} with elevation · built in{" "}
              {status.build_seconds}s
            </>
          ) : status?.error ? (
            <span className="text-red-500">graph unavailable: {status.error}</span>
          ) : (
            "graph not built yet — routing once will build it (~30s)"
          )}
        </div>
      </header>

      <div className="flex-1 flex min-h-0">
        <aside className="w-[360px] shrink-0 border-r border-[var(--line)] bg-[var(--panel)] overflow-y-auto p-4 space-y-4">
          <div>
            <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">
              How to use
            </p>
            <p className="text-xs leading-relaxed text-[var(--fg-2)]">
              Click the map to drop a start, click again for a destination. The graph
              finds the shortest path along real trail segments and reports what the
              hike actually is — not what any single segment says.
            </p>
          </div>

          <div>
            <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1.5">Try</p>
            <div className="flex flex-wrap gap-1.5">
              {preset([-119.5583, 37.7325], [-119.5332, 37.7459], "Happy Isles → Half Dome")}
              {preset([-119.5583, 37.7325], [-119.533, 37.7259], "→ Nevada Fall")}
              {preset([-116.137, 34.0027], [-116.1347, 33.9862], "Ryan Mountain")}
              {preset([-118.24, 36.587], [-118.2923, 36.5785], "Whitney Portal → summit")}
            </div>
          </div>

          <div className="space-y-2 rounded-lg border border-[var(--line)] bg-[var(--sunken)] p-3">
            <label className="flex items-center gap-2 text-xs text-[var(--fg-2)]">
              <input
                type="checkbox"
                checked={outAndBack}
                onChange={(e) => setOutAndBack(e.target.checked)}
              />
              Out and back (double the distance)
            </label>
            <label className="block text-xs text-[var(--fg-2)]">
              Snap radius
              <span className="ml-2 tabular-nums text-[var(--fg)]">{snap} mi</span>
              <input
                type="range"
                min="0.05"
                max="1"
                step="0.05"
                value={snap}
                onChange={(e) => setSnap(Number(e.target.value))}
                className="mt-1 w-full"
              />
              <span className="text-[10px] text-[var(--fg-3)]">
                How far from your click to look for a trail.
              </span>
            </label>
          </div>

          <div className="space-y-1 text-xs">
            <Row label="Start" value={start ? start.join(", ") : "click the map"} />
            <Row label="Destination" value={end ? end.join(", ") : "click again"} />
          </div>

          {busy && (
            <p className="text-xs text-[var(--fg-2)]">
              Routing… the first request builds the graph and takes about 30 seconds.
            </p>
          )}

          {result && !result.ok && (
            <div className="rounded-lg border border-amber-500/40 bg-amber-500/10 p-3 text-xs">
              <p className="font-medium text-amber-700 [:root[data-theme=dark]_&]:text-amber-300">
                {
                  {
                    no_trail_near_start: "No trail near the start",
                    no_trail_near_end: "No trail near the destination",
                    not_connected: "Points are not connected",
                    graph_unavailable: "Graph unavailable",
                  }[result.reason] || result.reason
                }
              </p>
              <p className="mt-1 text-[var(--fg-2)]">{result.detail}</p>
              {result.reason?.startsWith("no_trail") && (
                <p className="mt-1 text-[var(--fg-3)]">Try raising the snap radius.</p>
              )}
            </div>
          )}

          {result?.ok && (
            <div className="space-y-3 rounded-lg border border-[var(--line)] bg-[var(--sunken)] p-3">
              <div className="grid grid-cols-2 gap-2 text-center">
                <Stat label={outAndBack ? "Round trip" : "Distance"} value={`${result.miles} mi`} />
                <Stat
                  label="Gain"
                  value={result.gain_ft == null ? "unknown" : `${result.gain_ft.toLocaleString()} ft`}
                />
              </div>
              <div className="text-[11px] text-[var(--fg-3)]">
                one way {result.one_way_miles} mi · {result.segments_used} segments ·{" "}
                {result.node_count} nodes
              </div>
              <div>
                <p className="text-[11px] uppercase tracking-[0.18em] text-[var(--fg-3)] mb-1">
                  Trails strung together
                </p>
                {segments.length ? (
                  <ol className="space-y-1 text-xs text-[var(--fg)]">
                    {segments.map((leg, i) => (
                      <li key={`${leg.trail_id}-${i}`} className="flex items-center gap-2">
                        <span
                          className="h-2.5 w-2.5 shrink-0 rounded-full"
                          style={{ backgroundColor: leg.color }}
                        />
                        <span className="min-w-0 flex-1 truncate">{leg.name}</span>
                        <span className="shrink-0 tabular-nums text-[var(--fg-3)]">
                          {leg.miles} mi
                        </span>
                      </li>
                    ))}
                  </ol>
                ) : (
                  <ol className="space-y-0.5 text-xs text-[var(--fg)]">
                    {result.trail_names.map((n, i) => (
                      <li key={`${n}-${i}`} className="flex gap-2">
                        <span className="text-[var(--fg-3)] tabular-nums">{i + 1}.</span>
                        {n}
                      </li>
                    ))}
                  </ol>
                )}
              </div>
            </div>
          )}

          <button
            type="button"
            onClick={reset}
            className="w-full cursor-pointer rounded-lg border border-[var(--line)] px-3 py-1.5 text-xs text-[var(--fg-2)] transition-colors duration-200 hover:border-[var(--line-strong)] hover:text-[var(--fg)]"
          >
            Clear
          </button>
        </aside>

        <main className="flex-1 relative min-w-0">
          <div ref={mapContainer} className="h-full w-full" />
          {!MAPBOX_TOKEN && (
            <div className="absolute inset-0 grid place-items-center bg-[var(--sunken)] p-8 text-sm text-[var(--fg-2)]">
              VITE_MAPBOX_TOKEN is not set.
            </div>
          )}
        </main>
      </div>
    </div>
  );
}

function Row({ label, value }) {
  return (
    <div className="flex justify-between gap-3">
      <span className="text-[var(--fg-3)]">{label}</span>
      <span className="text-right tabular-nums text-[var(--fg)]">{value}</span>
    </div>
  );
}

function Stat({ label, value }) {
  return (
    <div>
      <p className="text-[10px] uppercase tracking-[0.18em] text-[var(--fg-3)]">{label}</p>
      <p className="mt-0.5 font-semibold text-[var(--fg)]">{value}</p>
    </div>
  );
}
