import { useState, useRef, useEffect } from "react";
import "./index.css";

const steps = [
  { id: "upload", title: "Upload GPX Route", subtitle: "Your trip starts with a route. We'll handle the rest." },
  { id: "dates", title: "Set the Trip Window", subtitle: "Forecasts are accurate within the next 10 days." },
  { id: "match", title: "Confirm Trail Match", subtitle: "We match your GPX to the closest trail data in Northern CA." },
  { id: "checks", title: "Pre-Trip Checks", subtitle: "Weather, air quality, fire perimeters, and snow conditions." },
  { id: "report", title: "Trip Readiness Report", subtitle: "Bullet summary and map overview." },
];

const toneTags = ["Forecast window ≤ 10 days", "Northern CA only", "GPX required", "Bullet report"];

function classNames(...classes) {
  return classes.filter(Boolean).join(" ");
}

function ChatPanel({ messages, onUserMessage }) {
  const scrollRef = useRef(null);
  const [input, setInput] = useState("");
  
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages]);
  
  const handleSubmit = (e) => {
    e.preventDefault();
    if (input.trim() && onUserMessage) {
      onUserMessage(input.trim());
      setInput("");
    }
  };
  
  return (
    <aside className="relative flex h-screen flex-col overflow-hidden bg-[#0f1a15] text-white">
      <div className="pointer-events-none absolute inset-0 opacity-40">
        <div className="absolute -left-24 top-12 h-64 w-64 rounded-full bg-emerald-500/20 blur-3xl" />
        <div className="absolute -bottom-20 right-0 h-72 w-72 rounded-full bg-teal-400/20 blur-3xl" />
      </div>
      <div className="relative z-10 px-8 py-8">
        <p className="text-xs uppercase tracking-[0.5em] text-emerald-300">Trip Planner</p>
        <h1 className="mt-4 text-3xl font-semibold leading-tight">Backcountry Readiness</h1>
        <p className="mt-3 text-sm text-white/70">A multi-step preflight assistant for trail trips in the next 10 days.</p>
        <div className="mt-6 flex flex-wrap gap-2">
          {toneTags.map((tag) => (
            <span key={tag} className="rounded-full border border-emerald-300/30 bg-white/5 px-3 py-1 text-[11px] uppercase tracking-[0.2em]">
              {tag}
            </span>
          ))}
        </div>
      </div>
      <div ref={scrollRef} className="relative z-10 flex-1 overflow-y-auto px-8 pb-4">
        <div className="space-y-4">
          {messages.map((msg, idx) => (
            <div key={`${msg.role}-${idx}`} className={classNames(
              "max-w-[85%] rounded-2xl p-4 text-sm leading-relaxed shadow-lg shadow-black/5",
              msg.role === "user" ? "ml-auto bg-emerald-600/20 text-emerald-50" : msg.role === "system" ? "bg-white/10 text-emerald-100" : "bg-white/5"
            )}>
              <p className="mb-2 text-[11px] uppercase tracking-[0.3em] text-white/60">{msg.role === "system" ? "System" : msg.role === "user" ? "You" : "Advisor"}</p>
              <p>{msg.text}</p>
            </div>
          ))}
        </div>
      </div>
      <form onSubmit={handleSubmit} className="relative z-10 border-t border-white/10 px-4 py-3">
        <div className="flex gap-2">
          <input
            type="text"
            value={input}
            onChange={(e) => setInput(e.target.value)}
            placeholder="Ask about trails, weather..."
            className="flex-1 rounded-xl border border-white/10 bg-white/5 px-4 py-2 text-sm text-white placeholder-white/40 focus:border-emerald-500 focus:outline-none"
          />
          <button type="submit" className="rounded-xl bg-emerald-600 px-4 py-2 text-sm font-semibold text-white hover:bg-emerald-700">
            Send
          </button>
        </div>
      </form>
    </aside>
  );
}

function StepHeader({ step, stepIndex }) {
  return (
    <div>
      <p className="text-xs uppercase tracking-[0.35em] text-emerald-700">Step {stepIndex + 1} of {steps.length}</p>
      <h2 className="mt-3 text-4xl font-semibold text-slate-900">{step.title}</h2>
      <p className="mt-3 max-w-2xl text-sm text-slate-600">{step.subtitle}</p>
    </div>
  );
}

function Stepper({ activeStep, onSelect }) {
  return (
    <div className="flex items-center gap-2 text-xs uppercase tracking-[0.3em]">
      {steps.map((step, idx) => (
        <button key={step.id} type="button" onClick={() => onSelect(step.id)} className={classNames("flex h-10 w-10 items-center justify-center rounded-full border text-[11px] font-semibold", activeStep === step.id ? "border-emerald-500 bg-emerald-50 text-emerald-700" : "border-slate-200 text-slate-500 hover:border-emerald-300")}>
          {idx + 1}
        </button>
      ))}
    </div>
  );
}

function UploadStep({ selectedFile, fileInputRef, onFileChange, onUpload, inputMode, setInputMode, trailName, setTrailName, onNameSearch }) {
  const isGpxMode = inputMode === "gpx";
  
  return (
    <div className="space-y-8">
      <div className="flex gap-4 p-1 bg-slate-100 rounded-xl w-fit">
        <button onClick={() => setInputMode("gpx")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${isGpxMode ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>
          Upload GPX
        </button>
        <button onClick={() => setInputMode("name")} className={`px-4 py-2 rounded-lg text-sm font-medium transition ${!isGpxMode ? "bg-white shadow text-slate-900" : "text-slate-500"}`}>
          Name + Region
        </button>
      </div>
      
      <div className="rounded-[32px] border border-emerald-100 bg-gradient-to-br from-white via-white to-emerald-50/40 p-10 shadow-lg shadow-emerald-200/30">
        {isGpxMode ? (
          <>
            <div className="flex items-start justify-between gap-6">
              <div>
                <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">GPX Upload</p>
                <h3 className="mt-3 text-2xl font-semibold text-slate-900">Drop your GPX file here</h3>
                <p className="mt-2 text-sm text-slate-600">We&apos;ll compute distance, elevation gain, and key coordinates along your route.</p>
              </div>
              <div className="rounded-2xl bg-emerald-900/5 px-4 py-2 text-xs uppercase tracking-[0.3em] text-emerald-700">Secure ingest</div>
            </div>
            <div className="mt-8 rounded-3xl border border-dashed border-emerald-300 bg-white/80 px-6 py-10 text-center">
              <input ref={fileInputRef} type="file" accept=".gpx" onChange={onFileChange} className="hidden" id="gpx-upload" />
              <label htmlFor="gpx-upload" className="cursor-pointer block text-sm text-slate-500 mb-4">
                {selectedFile ? selectedFile.name : "Drag & drop GPX or click to upload"}
              </label>
              <button onClick={onUpload} disabled={!selectedFile} className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
                Parse Route
              </button>
            </div>
          </>
        ) : (
          <>
            <div className="flex items-start justify-between gap-6">
              <div>
                <p className="text-xs uppercase tracking-[0.4em] text-emerald-700">Trail Search</p>
                <h3 className="mt-3 text-2xl font-semibold text-slate-900">Search by name</h3>
                <p className="mt-2 text-sm text-slate-600">Enter trail name and region to find matching trails.</p>
              </div>
            </div>
            <div className="mt-8 space-y-4">
              <input
                type="text"
                value={trailName}
                onChange={(e) => setTrailName(e.target.value)}
                placeholder="e.g., Aloha Lake, Desolation Wilderness"
                className="w-full rounded-2xl border border-slate-200 px-4 py-3 text-sm"
              />
              <button onClick={onNameSearch} disabled={!trailName.trim()} className="w-full rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
                Search Trails
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

function DatesStep({ startDate, endDate, onStartDateChange, onEndDateChange, onNext }) {
  const isValid = startDate && endDate && new Date(startDate) <= new Date(endDate);
  return (
    <div className="space-y-8">
      <div className="grid grid-cols-2 gap-6">
        <div className="rounded-2xl bg-white p-6 shadow-sm">
          <label className="text-xs uppercase tracking-[0.2em] text-slate-500">Start date</label>
          <input type="date" className="mt-4 w-full rounded-2xl border border-slate-200 px-4 py-3 text-sm" value={startDate} onChange={(e) => onStartDateChange(e.target.value)} />
        </div>
        <div className="rounded-2xl bg-white p-6 shadow-sm">
          <label className="text-xs uppercase tracking-[0.2em] text-slate-500">End date</label>
          <input type="date" className="mt-4 w-full rounded-2xl border border-slate-200 px-4 py-3 text-sm" value={endDate} onChange={(e) => onEndDateChange(e.target.value)} />
        </div>
      </div>
      <div className="rounded-2xl border border-emerald-100 bg-emerald-50 p-5 text-sm text-emerald-800">Trips must begin within 10 days to ensure the most accurate NOAA forecasts.</div>
      <button onClick={onNext} disabled={!isValid} className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        Continue to trail match
      </button>
    </div>
  );
}

function MatchStep({ route, trailMatch, selectedTrailId, onTrailSelect, onNext, loading }) {
  const autoSelected = trailMatch?.auto_selected;
  const shortlist = trailMatch?.shortlist || [];
  
  return (
    <div className="space-y-8">
      {autoSelected && (
        <div className="rounded-2xl bg-white p-6 shadow-sm">
          <p className="text-sm text-slate-600">We think your GPX matches:</p>
          <h3 className="mt-3 text-2xl font-semibold text-slate-900">{autoSelected.name}</h3>
          <p className="mt-2 text-slate-500">{autoSelected.area} · {autoSelected.distance_mi} mi</p>
          <div className="mt-5 flex flex-wrap gap-3">
            <button onClick={() => { onTrailSelect(autoSelected.id); onNext(); }} className="rounded-full bg-emerald-600 px-5 py-2 text-sm font-semibold text-white">Yes, that&apos;s it</button>
          </div>
        </div>
      )}
      {shortlist.length > 0 && (
        <div className="grid gap-4 md:grid-cols-3">
          {shortlist.map((trail) => (
            <div key={trail.id} onClick={() => onTrailSelect(trail.id)} className={`cursor-pointer rounded-2xl border p-5 transition ${selectedTrailId === trail.id ? "border-emerald-500 bg-emerald-50" : "border-slate-200 bg-white hover:border-emerald-300"}`}>
              <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Candidate</p>
              <h4 className="mt-3 text-lg font-semibold text-slate-900">{trail.name}</h4>
              <p className="text-slate-500">{trail.area} · {trail.distance_mi} mi</p>
            </div>
          ))}
        </div>
      )}
      {!autoSelected && shortlist.length === 0 && !loading && route && (
        <div className="rounded-2xl bg-white p-6 shadow-sm text-center py-12">
          <p className="text-slate-500">No matching trails found. Continuing without trail match.</p>
        </div>
      )}
      <button onClick={onNext} className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700">
        {loading ? "Running checks..." : "Run pre-trip checks"}
      </button>
    </div>
  );
}

function ChecksStep({ checks, loading, onNext }) {
  if (!checks && !loading) {
    return (
      <div className="space-y-8">
        <div className="rounded-2xl bg-white p-6 shadow-sm text-center py-12">
          <p className="text-slate-500">Loading pre-trip checks...</p>
        </div>
      </div>
    );
  }
  
  const checkItems = [
    { label: "Weather", data: checks?.weather },
    { label: "AQI", data: checks?.aqi },
    { label: "Fire", data: checks?.fire },
    { label: "Snow", data: checks?.snow },
  ];
  
  return (
    <div className="space-y-8">
      <div className="grid gap-4 md:grid-cols-2">
        {checkItems.map(({ label, data }) => {
          const status = data?.overall_status || "Unknown";
          const statusClass = status === "Good" ? "bg-emerald-50 text-emerald-700" : status === "Caution" ? "bg-amber-50 text-amber-700" : "bg-red-50 text-red-700";
          return (
            <div key={label} className="rounded-2xl bg-white p-6 shadow-sm">
              <div className="flex items-center justify-between">
                <p className="text-sm font-semibold text-slate-900">{label}</p>
                <span className={`rounded-full px-3 py-1 text-xs font-semibold ${statusClass}`}>{status}</span>
              </div>
              <p className="mt-3 text-sm text-slate-600">{data?.summary || "Loading..."}</p>
            </div>
          );
        })}
      </div>
      <button onClick={onNext} disabled={loading} className="rounded-full bg-emerald-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-emerald-500/20 transition hover:bg-emerald-700 disabled:opacity-50">
        {loading ? "Generating report..." : "View trip report"}
      </button>
    </div>
  );
}

function ReportStep({ planResult }) {
  const report = planResult?.report;
  const mapLayers = planResult?.map_layers;
  
  return (
    <div className="space-y-8">
      {report && (
        <div className="rounded-3xl bg-white p-6 shadow-sm">
          <p className="text-xs uppercase tracking-[0.3em] text-slate-500">AI Summary</p>
          <ul className="mt-5 space-y-3 text-sm text-slate-700">
            {report.bullets?.map((bullet, idx) => (
              <li key={idx} className="flex gap-3">
                <span className="mt-1 h-2 w-2 shrink-0 rounded-full bg-emerald-500" />
                <span>{bullet}</span>
              </li>
            ))}
          </ul>
        </div>
      )}
      <div className="rounded-3xl border border-slate-200 bg-gradient-to-br from-emerald-50 to-white p-6">
        <div className="flex items-center justify-between">
          <div>
            <p className="text-xs uppercase tracking-[0.3em] text-slate-500">Mapbox Preview</p>
            <h3 className="mt-2 text-2xl font-semibold text-slate-900">3D route overview</h3>
            <p className="mt-2 text-sm text-slate-600">Route line, fire perimeters, and terrain visualization will render here.</p>
          </div>
          {mapLayers ? (
            <div className="rounded-2xl bg-emerald-600/10 px-4 py-2 text-xs font-semibold text-emerald-700">Data loaded</div>
          ) : (
            <div className="rounded-2xl bg-amber-600/10 px-4 py-2 text-xs font-semibold text-amber-700">Demo layer</div>
          )}
        </div>
        <div className="mt-6 h-64 rounded-2xl border border-emerald-100 bg-white/70 flex items-center justify-center text-slate-400">
          {mapLayers ? `Route: ${mapLayers.route_coordinates?.length || 0} points` : "Mapbox canvas placeholder"}
        </div>
      </div>
    </div>
  );
}

export default function App() {
  const [activeStep, setActiveStep] = useState(steps[0].id);
  const [messages, setMessages] = useState([
    { role: "system", text: "Trip readiness planner is online. We'll guide you step by step." },
    { role: "assistant", text: "Upload your GPX route and I'll extract distance, elevation, and key coordinates." },
    { role: "assistant", text: "Once we have the route, we'll scan weather, AQI, fire, and snow for the next 10 days." },
  ]);
  
  const [route, setRoute] = useState(null);
  const [startDate, setStartDate] = useState("2026-04-30");
  const [endDate, setEndDate] = useState("2026-05-02");
  const [trailMatch, setTrailMatch] = useState(null);
  const [selectedTrailId, setSelectedTrailId] = useState(null);
  const [checks, setChecks] = useState(null);
  const [planResult, setPlanResult] = useState(null);
  const [loading, setLoading] = useState(false);
  const [selectedFile, setSelectedFile] = useState(null);
  const [inputMode, setInputMode] = useState("gpx");
  const [trailName, setTrailName] = useState("");
  const [gpxRoute, setGpxRoute] = useState(null); // route from GPX parsing
  const [nameSearchResults, setNameSearchResults] = useState([]);
  const fileInputRef = useRef(null);
  
  const stepIndex = steps.findIndex((s) => s.id === activeStep);
  const step = steps[stepIndex];
  
  const addMessage = (role, text) => setMessages((prev) => [...prev, { role, text }]);
  
  const handleUserMessage = async (text) => {
    addMessage("user", text);
    
    // Simple AI responses based on keywords
    let response = "I can help you plan your backcountry trip. Upload a GPX file to get started, or ask about weather conditions.";
    
    const lower = text.toLowerCase();
    if (lower.includes("weather") || lower.includes("forecast")) {
      response = "Weather forecasts are available once you set your trip dates. We'll pull NOAA data for your route location.";
    } else if (lower.includes("gpx") || lower.includes("upload") || lower.includes("file")) {
      response = "Click 'Choose File' to upload a GPX route, or drag and drop it into the upload area. The GPX should contain track points with lat/lng coordinates.";
    } else if (lower.includes("fire") || lower.includes("aqi") || lower.includes("air")) {
      response = "We'll check active fire perimeters and air quality index when you run pre-trip checks. This requires a GPX route first.";
    } else if (lower.includes("trail") || lower.includes("match")) {
      response = "We match your GPX to trails in our Northern CA database. You can confirm the match or select a different candidate.";
    } else if (lower.includes("hello") || lower.includes("hi") || lower.includes("hey")) {
      response = "Welcome to Backcountry Readiness! Let's plan your trip. Upload a GPX file to get started.";
    }
    
    setTimeout(() => addMessage("assistant", response), 500);
  };
  
  const goToStep = (stepId) => setActiveStep(stepId);
  
  const handleFileChange = (e) => {
    const file = e.target.files?.[0];
    if (file) setSelectedFile(file);
  };
  
  const handleUpload = async () => {
    if (!selectedFile) return;
    
    const formData = new FormData();
    formData.append("file", selectedFile);
    
    try {
      const res = await fetch("/api/route/parse", { method: "POST", body: formData });
      const data = await res.json();
      if (data.route) {
        setGpxRoute(data.route);
        addMessage("assistant", `Route parsed: ${data.route.distance_mi?.toFixed(1) || "?"} miles, ${data.route.elev_gain_ft?.toLocaleString() || "?"} ft elevation gain.`);
        goToStep("dates");
      } else {
        alert(data.detail || "Failed to parse route");
      }
    } catch (err) {
      alert("Error uploading file: " + err.message);
    }
  };
  
  const handleNameSearch = async () => {
    if (!trailName.trim()) return;
    
    try {
      // Call trail match with just the name hint, no route
      const res = await fetch("/api/trail/match", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ route: { points: [] }, name_hint: trailName }),
      });
      const data = await res.json();
      
      if (data.auto_selected) {
        addMessage("assistant", `Found: ${data.auto_selected.name} in ${data.auto_selected.area}. Continue to dates to set your trip window.`);
        setRoute(null); // No GPX route, just trail
        setSelectedTrailId(data.auto_selected.id);
        goToStep("dates");
      } else if (data.shortlist?.length) {
        setNameSearchResults(data.shortlist);
        addMessage("assistant", `Found ${data.shortlist.length} trails matching "${trailName}". Select one to continue.`);
      } else {
        addMessage("assistant", `No trails found matching "${trailName}". Try a different name or region.`);
      }
    } catch (err) {
      addMessage("assistant", "Error searching for trails: " + err.message);
    }
  };
  
  const handleNameSelect = (trail) => {
    setSelectedTrailId(trail.id);
    setTrailName(trail.name);
    setTrailMatch({ auto_selected: trail, shortlist: [trail] });
    setNameSearchResults([]);
    addMessage("assistant", `Selected: ${trail.name}. Continue to set your trip dates.`);
    goToStep("dates");
  };
  
  const handleDatesNext = async () => {
    goToStep("match");
    setLoading(true);
    
    // If we have a GPX route, match it. Otherwise skip to checks with selected trail
    if (gpxRoute) {
      addMessage("assistant", "Matching your route to known trails in Northern CA...");
      
      try {
        const res = await fetch("/api/trail/match", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ route: gpxRoute, name_hint: "" }),
        });
        const data = await res.json();
        setTrailMatch(data);
        if (data.auto_selected) {
          setSelectedTrailId(data.auto_selected.id);
          addMessage("assistant", `Best match: ${data.auto_selected.name} in ${data.auto_selected.area}. Confirm to continue.`);
        } else if (data.shortlist?.length) {
          addMessage("assistant", `Found ${data.shortlist.length} candidate trails.`);
        } else {
          addMessage("assistant", "No matching trails found. You can proceed without a trail match.");
        }
      } catch (err) {
        addMessage("assistant", "Could not match trail. You can proceed without trail matching.");
      }
    } else if (selectedTrailId) {
      // Using name search trail - skip match step, go to checks
      addMessage("assistant", "Using selected trail. Running pre-trip checks...");
      goToStep("checks");
      await runPreTripChecks();
      return;
    }
    setLoading(false);
  };
  
  const runPreTripChecks = async () => {
    setLoading(true);
    addMessage("assistant", "Running pre-trip checks for weather, AQI, fire, and snow...");
    
    try {
      let data;
      
      if (selectedFile) {
        // GPX upload path
        const formData = new FormData();
        formData.append("file", selectedFile);
        formData.append("start_date", startDate);
        formData.append("end_date", endDate);
        formData.append("selected_trail_id", selectedTrailId || "");
        
        const res = await fetch("/api/plan", { method: "POST", body: formData });
        data = await res.json();
      } else if (selectedTrailId && trailMatch?.auto_selected) {
        // Name search path - call checks directly for the trail
        const trail = trailMatch.auto_selected;
        const weatherRes = await fetch("/api/checks/weather", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ lat: trail.lat, lng: trail.lng }),
        });
        const weather = await weatherRes.json();
        
        data = {
          checks: {
            weather,
            aqi: { error: "AQI requires API key" },
            fire: { error: "Fire check requires GPX route" },
            snow: { message: "Snow data unavailable" },
          },
          risk: { status: "go", reasons: [] },
        };
      } else {
        throw new Error("No trail selected. Please upload a GPX or search for a trail.");
      }
      
      setPlanResult(data);
      setChecks(data.checks);
      
      const risk = data.risk?.status || data.risk?.overall;
      addMessage("assistant", `Pre-trip checks complete. Overall status: ${risk || "Unknown"}`);
    } catch (err) {
      addMessage("assistant", "Error running checks: " + err.message);
    }
    setLoading(false);
  };
  
  const handleChecksNext = () => {
    goToStep("report");
  };
  
  const renderStep = () => {
    switch (step.id) {
      case "upload":
        return (
          <>
            <UploadStep 
              selectedFile={selectedFile} 
              fileInputRef={fileInputRef} 
              onFileChange={handleFileChange} 
              onUpload={handleUpload}
              inputMode={inputMode}
              setInputMode={setInputMode}
              trailName={trailName}
              setTrailName={setTrailName}
              onNameSearch={handleNameSearch}
            />
            {nameSearchResults.length > 0 && (
              <div className="grid gap-4 md:grid-cols-3 mt-6">
                {nameSearchResults.map((trail) => (
                  <div key={trail.id} onClick={() => handleNameSelect(trail)} className="cursor-pointer rounded-2xl border border-slate-200 bg-white p-5 hover:border-emerald-300 transition">
                    <p className="text-xs uppercase tracking-[0.2em] text-slate-500">Candidate</p>
                    <h4 className="mt-3 text-lg font-semibold text-slate-900">{trail.name}</h4>
                    <p className="text-slate-500">{trail.area} · {trail.distance_mi} mi</p>
                  </div>
                ))}
              </div>
            )}
          </>
        );
      case "dates":
        return <DatesStep startDate={startDate} endDate={endDate} onStartDateChange={setStartDate} onEndDateChange={setEndDate} onNext={handleDatesNext} />;
      case "match":
        return <MatchStep route={route} trailMatch={trailMatch} selectedTrailId={selectedTrailId} onTrailSelect={setSelectedTrailId} onNext={handleMatchNext} loading={loading} />;
      case "checks":
        return <ChecksStep checks={checks} loading={loading} onNext={handleChecksNext} />;
      case "report":
        return <ReportStep planResult={planResult} />;
      default:
        return null;
    }
  };
  
  const next = (step.id === "upload" ? handleUpload : 
                     step.id === "dates" ? handleDatesNext : 
                     step.id === "match" ? handleMatchNext : 
                     step.id === "checks" ? handleChecksNext : null);
  
  return (
    <div className="grid min-h-screen grid-cols-[3fr_5fr]">
      <ChatPanel messages={messages} onUserMessage={handleUserMessage} />
      <main className="h-screen overflow-y-auto bg-[#f6f3ee]">
        <div className="px-12 py-10 space-y-10">
          <Stepper activeStep={activeStep} onSelect={goToStep} />
          <StepHeader step={step} stepIndex={stepIndex} />
          {renderStep()}
        </div>
      </main>
    </div>
  );
}