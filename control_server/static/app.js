"use strict";
/* Consola web de la red de telescopios. Sin dependencias externas. */
(() => {
  const NODE_COLORS = ["#3987e5", "#d95926"];   // identidad fija por nodo (orden del servidor)
  const EXTRA_COLOR = "#c3c2b7";
  const RADII = [0.5, 1, 2, 4, 8, 15, 30];
  const D2R = Math.PI / 180;

  const app = {
    ws: null, connected: false, authorized: false, controlRequired: false,
    state: null, scope: "all", tracking: true, reqId: 0,
    pending: new Map(), views: new Map(), scopeKey: "", renderQueued: false,
    sky: { radius: 4, data: null, dataCenter: null, dataRadius: null, fetching: false, lastFetch: 0, hits: [], polys: [] },
  };

  // ------------------------------------------------------------------ utilidades
  const $ = (sel, root = document) => root.querySelector(sel);
  function el(tag, props = {}, ...children) {
    const node = document.createElement(tag);
    for (const [k, v] of Object.entries(props)) {
      if (k === "class") node.className = v;
      else if (k === "text") node.textContent = v;
      else if (k === "style") node.style.cssText = v;
      else node.setAttribute(k, v);
    }
    for (const c of children) node.append(c);
    return node;
  }
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  function fmtRA(deg, dec = 0) {
    let s = (((deg % 360) + 360) % 360) / 15 * 3600;
    s = Math.round(s * 10 ** dec) / 10 ** dec;
    const h = Math.floor(s / 3600), m = Math.floor((s - h * 3600) / 60), sec = s - h * 3600 - m * 60;
    return `${pad(h % 24)}h ${pad(m)}m ${sec.toFixed(dec).padStart(dec ? 3 + dec : 2, "0")}s`;
  }
  function fmtDec(deg) {
    const sign = deg < 0 ? "−" : "+";
    let s = Math.round(Math.abs(deg) * 3600);
    const d = Math.floor(s / 3600), m = Math.floor((s - d * 3600) / 60), sec = s - d * 3600 - m * 60;
    return `${sign}${pad(d)}° ${pad(m)}′ ${pad(sec)}″`;
  }
  const fmtDeg = (x, digits = 1) => (x == null || Number.isNaN(x) ? "—" : `${x.toFixed(digits)}°`);
  function fmtAngle(deg) {
    if (deg == null) return "—";
    const a = Math.abs(deg);
    if (a < 1 / 60) return `${(a * 3600).toFixed(0)}″`;
    if (a < 1) return `${(a * 60).toFixed(1)}′`;
    return `${a.toFixed(2)}°`;
  }
  function fmtHours(deg) {
    const sign = deg < 0 ? "−" : "+";
    const mins = Math.round(Math.abs(deg) / 15 * 60);
    return `${sign}${Math.floor(mins / 60)}h ${pad(mins % 60)}m`;
  }
  function cardinal(az) {
    const names = ["N", "NE", "E", "SE", "S", "SO", "O", "NO"];
    return names[Math.round(((az % 360) + 360) % 360 / 45) % 8];
  }
  function nodeColor(index) { return NODE_COLORS[index] || EXTRA_COLOR; }
  function stateLabel(state) {
    return ({ idle: "Detenido", waiting: "Esperando inicio", slewing: "Moviéndose", tracking: "Siguiendo",
      parking: "Aparcando", parked: "Aparcado" })[state] || state || "—";
  }
  function setStatus(node, level, text) {
    node.className = node.className.replace(/status-\w+/g, "").trim() + ` status-${level}`;
    $(".status-text", node).textContent = text;
  }

  // ------------------------------------------------------------------ conexión
  function connect() {
    const proto = location.protocol === "https:" ? "wss" : "ws";
    const ws = new WebSocket(`${proto}://${location.host}/ws/ui`);
    app.ws = ws;
    ws.onopen = () => { app.connected = true; setStatus($("#conn"), "good", "Conectado"); };
    ws.onclose = () => {
      app.connected = false;
      setStatus($("#conn"), "critical", "Sin conexión con el servidor");
      for (const { reject } of app.pending.values()) reject(new Error("Conexión perdida"));
      app.pending.clear();
      setTimeout(connect, 2000);
    };
    ws.onmessage = (ev) => handleMessage(JSON.parse(ev.data));
  }

  function send(msg) {
    return new Promise((resolve, reject) => {
      if (!app.ws || app.ws.readyState !== WebSocket.OPEN) { reject(new Error("Sin conexión")); return; }
      const reqId = ++app.reqId;
      app.pending.set(reqId, { resolve, reject });
      app.ws.send(JSON.stringify({ ...msg, req_id: reqId }));
      setTimeout(() => {
        if (app.pending.delete(reqId)) reject(new Error("El servidor no respondió"));
      }, 30000);
    });
  }

  async function command(msg) {
    try {
      const result = await send(msg);
      toast(result.message || (result.ok ? "Hecho" : "Falló"), !result.ok);
      return result;
    } catch (err) {
      toast(err.message, true);
      return { ok: false };
    }
  }

  function handleMessage(msg) {
    if (msg.type === "welcome") {
      app.controlRequired = msg.control_required;
      app.authorized = msg.authorized;
      updateAuth();
      $("#events").replaceChildren();
      (msg.events || []).forEach(addEvent);
    } else if (msg.type === "state") {
      app.state = msg;
      scheduleRender();
    } else if (msg.type === "events") {
      msg.events.forEach(addEvent);
    } else if (msg.type === "result") {
      const p = app.pending.get(msg.req_id);
      if (p) { app.pending.delete(msg.req_id); p.resolve(msg); }
      if (msg.command === "auth") { app.authorized = !!msg.authorized; updateAuth(); }
    }
  }

  function updateAuth() {
    const readonly = app.controlRequired && !app.authorized;
    $("#auth-form").hidden = !readonly;
    $("#readonly-note").hidden = !readonly;
    document.body.classList.toggle("readonly", readonly);
  }

  // ------------------------------------------------------------------ avisos y bitácora
  let toastTimer = null;
  function toast(text, fail = false) {
    const t = $("#toast");
    t.textContent = text;
    t.classList.toggle("fail", fail);
    t.hidden = false;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => { t.hidden = true; }, 4500);
  }

  function addEvent(ev) {
    const list = $("#events");
    const time = new Date(ev.t * 1000);
    const li = el("li", { class: `ev-${ev.level}` },
      el("time", { text: `${pad(time.getHours())}:${pad(time.getMinutes())}:${pad(time.getSeconds())}` }),
      el("span", { text: ev.text }));
    list.prepend(li);
    while (list.children.length > 150) list.lastChild.remove();
  }

  // ------------------------------------------------------------------ controles
  function scopeNodes() { return app.scope === "all" ? "all" : [app.scope]; }

  function renderScope(nodes) {
    const key = nodes.map((n) => `${n.id}:${n.name}`).join("|");
    if (key === app.scopeKey) return;
    app.scopeKey = key;
    const box = $("#scope");
    box.replaceChildren();
    const options = [{ id: "all", name: "Ambos · sincronizados" }, ...nodes];
    options.forEach((opt) => {
      const idx = nodes.findIndex((n) => n.id === opt.id);
      const btn = el("button", { type: "button", role: "radio", "aria-checked": String(app.scope === opt.id) });
      if (idx >= 0) btn.append(el("span", { class: "swatch", style: `--node-color:${nodeColor(idx)}` }));
      btn.append(document.createTextNode(opt.name));
      btn.addEventListener("click", () => {
        app.scope = opt.id;
        box.querySelectorAll("button").forEach((b) => b.setAttribute("aria-checked", String(b === btn)));
      });
      box.append(btn);
    });
  }

  function setupControls() {
    $("#goto-form").addEventListener("submit", (e) => {
      e.preventDefault();
      const target = $("#target-input").value.trim();
      if (target) command({ type: "goto", target, nodes: scopeNodes() });
    });
    loadVisibleTargets();
    setInterval(loadVisibleTargets, 60000);
    let searchTimer = null;
    $("#target-input").addEventListener("input", (e) => {
      clearTimeout(searchTimer);
      const q = e.target.value.trim();
      if (q.length < 1) return;
      searchTimer = setTimeout(async () => {
        try {
          const res = await fetch(`/api/search?q=${encodeURIComponent(q)}`);
          const items = await res.json();
          $("#target-suggestions").replaceChildren(...items.map((it) => el("option", { value: it.label, label: it.detail })));
        } catch { /* sin sugerencias */ }
      }, 200);
    });
    document.querySelectorAll("[data-nudge]").forEach((btn) => btn.addEventListener("click", () =>
      command({ type: "nudge", direction: btn.dataset.nudge, arcmin: Number($("#nudge-step").value), nodes: scopeNodes() })));
    $("#btn-stop").addEventListener("click", () => command({ type: "stop", nodes: scopeNodes() }));
    $("#btn-park").addEventListener("click", () => {
      if (confirm("¿Aparcar los telescopios seleccionados en su posición de inicio?")) command({ type: "park", nodes: scopeNodes() });
    });
    $("#btn-track").addEventListener("click", async () => {
      const enabled = !app.tracking;
      const r = await command({ type: "tracking", enabled, nodes: scopeNodes() });
      if (r.ok) setTracking(enabled);
    });
    $("#btn-capture").addEventListener("click", () => command({ type: "capture" }));
    $("#auth-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      await command({ type: "auth", password: $("#auth-password").value });
      $("#auth-password").value = "";
    });
    $("#night-toggle").addEventListener("click", () => {
      const on = document.documentElement.classList.toggle("night");
      try { localStorage.setItem("night", on ? "1" : "0"); } catch { /* sin almacenamiento */ }
    });
    try { if (localStorage.getItem("night") === "1") document.documentElement.classList.add("night"); } catch { /* */ }
    $("#zoom-in").addEventListener("click", () => zoom(-1));
    $("#zoom-out").addEventListener("click", () => zoom(1));
    setInterval(() => {
      const d = new Date();
      $("#utc-clock").textContent = `${pad(d.getUTCHours())}:${pad(d.getUTCMinutes())}:${pad(d.getUTCSeconds())}`;
    }, 500);
  }

  // Sugerencias: objetos brillantes que ahora están sobre el límite de altura en todos los sitios.
  async function loadVisibleTargets() {
    const box = $("#quick-targets");
    try {
      const data = await (await fetch("/api/visible")).json();
      if (!data.targets.length) {
        const day = data.sites.filter((x) => x.sun_alt > -6).map((x) => x.name);
        box.replaceChildren(el("span", { class: "muted small", text: data.sites.length
          ? `Ningún objeto brillante está alto en todos los sitios ahora${day.length ? ` (es de día en ${day.join(" y ")})` : ""}.`
          : "Esperando la conexión de los telescopios…" }));
        return;
      }
      const day = data.sites.filter((x) => x.sun_alt > -6).map((x) => x.name);
      const note = day.length ? [el("span", { class: "muted small",
        text: `Es de día en ${day.join(" y ")}: con un telescopio pequeño solo se verán la Luna, planetas y estrellas muy brillantes.` })] : [];
      box.replaceChildren(...note, ...data.targets.map((t) => {
        const chip = el("button", { type: "button", class: "chip control", text: t.label,
          title: `${t.name} · ${t.type}${t.mag != null ? ` · mag ${t.mag.toFixed(1)}` : ""} · altura mínima ${t.min_alt.toFixed(0)}°` });
        chip.addEventListener("click", () => {
          $("#target-input").value = t.label;
          command({ type: "goto", target: t.label, nodes: scopeNodes() });
        });
        return chip;
      }));
    } catch { /* se reintenta en el próximo ciclo */ }
  }

  // Mover desplaza desde el objetivo actual: sin objetivo no hay desde dónde moverse.
  function updateNudge(st) {
    let hasTarget;
    if (app.scope === "all") hasTarget = !!st.target;
    else {
      const n = st.nodes.find((x) => x.id === app.scope);
      hasTarget = !!(n && n.telemetry && n.telemetry.target);
    }
    document.querySelectorAll("[data-nudge]").forEach((b) => {
      b.disabled = !hasTarget;
      b.title = hasTarget ? "" : "Primero apunta a un objeto";
    });
  }

  function setTracking(enabled) {
    app.tracking = enabled;
    const b = $("#btn-track");
    b.setAttribute("aria-pressed", String(enabled));
    b.textContent = `Seguimiento: ${enabled ? "sí" : "no"}`;
  }

  // ------------------------------------------------------------------ render general
  function scheduleRender() {
    if (app.renderQueued) return;
    app.renderQueued = true;
    requestAnimationFrame(() => { app.renderQueued = false; render(); });
  }

  function render() {
    const st = app.state;
    if (!st) return;
    renderScope(st.nodes);
    st.nodes.forEach((node, idx) => renderNode(ensureView(node, idx), node));
    renderSync(st);
    updateNudge(st);
    const online = st.nodes.filter((n) => n.online).map((n) => n.id).join("|");
    if (online !== app.onlineKey) { app.onlineKey = online; loadVisibleTargets(); }
    drawSkymap();
  }

  function renderSync(st) {
    const t = st.target;
    $("#target-name").textContent = t ? (t.full_name || t.name) : "—";
    $("#target-detail").textContent = t
      ? `${t.type || "Objeto"}${t.mag != null ? ` · mag ${t.mag.toFixed(1)}` : ""} · AR ${fmtRA(t.ra)} · Dec ${fmtDec(t.dec)}`
      : "Elige un objeto para apuntar los telescopios";

    const vis = $("#target-vis");
    vis.replaceChildren();
    st.nodes.forEach((n, idx) => {
      const tt = n.telemetry && n.telemetry.target;
      const text = !n.online ? "desconectado" : tt ? `${fmtDeg(tt.alt)} · ${tt.name}` : "sin objetivo";
      vis.append(el("div", { class: "vis-item" },
        el("span", { class: "swatch", style: `--node-color:${nodeColor(idx)}` }),
        el("span", { text: n.name }), el("strong", { text })));
    });

    const badge = $("#sync-badge"), detail = $("#sync-detail");
    const s = st.sync;
    if (!s) {
      setStatus(badge, "muted", "Esperando dos telescopios en línea");
      detail.textContent = "";
      return;
    }
    if (s.moving) setStatus(badge, "warning", "Moviéndose hacia el objetivo…");
    else if (s.status === "mismo") setStatus(badge, "good", "Mismo cuadrante");
    else if (s.status === "parcial") setStatus(badge, "warning", "Campos parcialmente superpuestos");
    else setStatus(badge, "serious", "Cuadrantes distintos");
    let text = `Separación ${fmtAngle(s.separation_deg)} · superposición ${(s.overlap * 100).toFixed(0)} %`;
    if (s.parallax_deg != null) text += ` · paralaje lunar esperado ${fmtAngle(s.parallax_deg)}`;
    detail.textContent = text;
  }

  // ------------------------------------------------------------------ paneles de nodo
  function ensureView(node, idx) {
    let v = app.views.get(node.id);
    if (v) return v;
    const root = $("#node-template").content.firstElementChild.cloneNode(true);
    root.style.setProperty("--node-color", nodeColor(idx));
    v = {
      root, color: nodeColor(idx), wasOnline: false, layers: { stars: true, catalog: true, reticle: true },
      img: $(".stream", root), overlay: $(".overlay", root), expDirty: false, gainDirty: false,
    };
    $(".node-name", root).textContent = node.name;
    v.img.alt = `Imagen en vivo del telescopio ${node.name}`;
    v.img.addEventListener("load", () => root.classList.add("has-frame"));
    $(".snapshot-link", root).href = `/snapshot/${encodeURIComponent(node.id)}.jpg`;
    root.querySelectorAll("[data-layer]").forEach((cb) => cb.addEventListener("change", () => {
      v.layers[cb.dataset.layer] = cb.checked;
      scheduleRender();
    }));
    $(".btn-align", root).addEventListener("click", () => {
      const current = app.state && app.state.target ? app.state.target.name : "";
      const target = prompt(`¿Qué objeto está centrado ahora en la imagen de ${node.name}? (p. ej. Vega, Júpiter, M42)`, current);
      if (target) command({ type: "align", node: node.id, target });
    });
    $(".viewer", root).addEventListener("click", (e) => {
      if (!v.lastNode || !v.lastNode.online || document.body.classList.contains("readonly")) return;
      const rect = v.img.getBoundingClientRect();
      const iw = v.img.naturalWidth || 4, ih = v.img.naturalHeight || 3;
      const s = Math.min(rect.width / iw, rect.height / ih);
      const x = (e.clientX - rect.left - (rect.width - iw * s) / 2) / (iw * s);
      const y = (e.clientY - rect.top - (rect.height - ih * s) / 2) / (ih * s);
      if (x < 0 || x > 1 || y < 0 || y > 1) return;
      command({ type: "center", node: node.id, x, y });
    });
    const exp = $(".exp-input", root), gain = $(".gain-input", root);
    exp.addEventListener("input", () => { v.expDirty = true; });
    gain.addEventListener("input", () => { v.gainDirty = true; });
    $(".btn-camera", root).addEventListener("click", async () => {
      await command({ type: "camera", nodes: [node.id], exposure_ms: Number(exp.value), gain: Number(gain.value) });
      v.expDirty = v.gainDirty = false;
    });
    $("#nodes").append(root);
    app.views.set(node.id, v);
    return v;
  }

  function renderNode(v, node) {
    const root = v.root;
    v.lastNode = node;
    const tel = node.telemetry, an = node.analysis, en = node.enrichment, info = node.info || {};
    $(".node-name", root).textContent = node.name;
    const site = info.site;
    $(".node-site", root).textContent = site
      ? `${Math.abs(site.latitude).toFixed(3)}° ${site.latitude >= 0 ? "N" : "S"}, ${Math.abs(site.longitude).toFixed(3)}° ${site.longitude >= 0 ? "E" : "O"} · ${Math.round(site.elevation_m)} m`
      : "Sitio sin datos";

    // Video: reinicia el flujo MJPEG cuando el nodo vuelve a conectarse.
    if (node.online && !v.wasOnline) v.img.src = `/stream/${encodeURIComponent(node.id)}.mjpg?t=${Date.now()}`;
    v.wasOnline = node.online;
    root.classList.toggle("offline", !node.online);
    $(".viewer-empty span", root).textContent = node.online ? "Esperando imagen…" : "Sin conexión";

    setStatus($(".badge-online", root), node.online ? "good" : "critical", node.online ? "En línea" : "Desconectado");
    const badgeState = $(".badge-state", root);
    if (tel && node.online) {
      const moving = tel.state === "slewing" || tel.state === "waiting" || tel.state === "parking";
      setStatus(badgeState, moving ? "warning" : tel.state === "tracking" ? "good" : "muted",
        `${stateLabel(tel.state)}${tel.aligned ? "" : " · sin alinear"}`);
      badgeState.hidden = false;
    } else {
      badgeState.hidden = true;
    }

    const p = tel && tel.pointing;
    const age = node.frame && node.frame.age_s;
    $(".hud-tl", root).textContent = tel && node.online
      ? `${stateLabel(tel.state)}${tel.start_in_s ? ` · inicio en ${tel.start_in_s.toFixed(1)} s` : ""}` : "";
    $(".hud-br", root).textContent = p
      ? `AR ${fmtRA(p.ra)}  Dec ${fmtDec(p.dec)}${age != null && node.online ? `  ·  retraso ${age.toFixed(1)} s` : ""}` : "";

    $(".summary", root).textContent = en ? en.summary : (node.online ? "Calculando…" : "");
    renderCoords(root, node, p, tel, en);
    renderAnalysis(root, an);
    renderInField(root, en);
    renderSky(root, node, en, tel, info);

    const exp = $(".exp-input", root), gain = $(".gain-input", root);
    if (tel && tel.camera) {
      if (!v.expDirty && document.activeElement !== exp) exp.value = tel.camera.exposure_ms;
      if (!v.gainDirty && document.activeElement !== gain) gain.value = tel.camera.gain;
    }
    drawOverlay(v, node);
  }

  function fillKV(dl, rows) {
    dl.replaceChildren(...rows.flatMap(([k, val]) => [el("dt", { text: k }), el("dd", { text: val })]));
  }

  function renderCoords(root, node, p, tel, en) {
    if (!p) { fillKV($(".coords", root), [["Posición", node.online ? "Sin telemetría" : "—"]]); return; }
    const rows = [
      ["AR (J2000)", fmtRA(p.ra, 1)],
      ["Dec (J2000)", fmtDec(p.dec)],
      ["Altitud", fmtDeg(p.alt, 2)],
      ["Azimut", `${fmtDeg(p.az, 2)} (${cardinal(p.az)})`],
      ["Ángulo horario", fmtHours(p.ha_deg)],
      ["Tiempo sidéreo", fmtRA(p.lst_deg)],
    ];
    if (en) {
      rows.push(["Constelación", `${en.constellation_name} (${en.constellation})`]);
      rows.push(["Campo", `${en.field.fov_w.toFixed(2)}° × ${en.field.fov_h.toFixed(2)}° · rotación ${en.field.rotation.toFixed(0)}°`]);
      const off = en.utc_offset_hours;
      rows.push(["Hora local", `${en.local_time} (UTC${off >= 0 ? "+" : "−"}${Math.abs(off)})`]);
    }
    if (tel.target) rows.push(["Objetivo", `${tel.target.name} · a ${fmtAngle(tel.target.error_deg)}`]);
    fillKV($(".coords", root), rows);
    if (en) {
      const names = ["Sup. izquierda", "Sup. derecha", "Inf. derecha", "Inf. izquierda"];
      fillKV($(".corner-list", root), en.field.corners.map((c, i) => [names[i], `${c.ra_str}  ${c.dec_str}`]));
    }
  }

  function tile(label, value, sub) {
    return el("div", { class: "tile" }, el("div", { class: "tile-label", text: label }),
      el("div", { class: "tile-value", text: value }), el("div", { class: "tile-sub", text: sub || "" }));
  }

  function renderAnalysis(root, an) {
    const tiles = $(".tiles", root);
    if (!an) {
      $(".scene", root).textContent = "Sin análisis todavía";
      tiles.replaceChildren();
      $(".notes", root).replaceChildren();
      return;
    }
    $(".scene", root).textContent = an.scene_label;
    tiles.replaceChildren(
      tile("Estrellas", String(an.n_stars), `ruido ${an.noise.toFixed(1)} ADU`),
      tile("Enfoque", an.focus, an.fwhm_px != null ? `FWHM ${an.fwhm_px.toFixed(1)} px` : ""),
      tile("Seguimiento", an.tracking_quality, an.elongation != null ? `elongación ${an.elongation.toFixed(2)}` : ""),
      tile("Fondo", `${Math.round(an.background)}`, `variación ${(an.background_variation * 100).toFixed(0)} %`),
      tile("Saturación", `${(an.saturated_fraction * 100).toFixed(1)} %`, ""),
      tile("Proceso", `${Math.round(an.processing_ms)} ms`, an.frame_ts ? `hace ${Math.max(0, Date.now() / 1000 - an.frame_ts).toFixed(0)} s` : ""),
    );
    $(".notes", root).replaceChildren(...(an.notes || []).map((n) => el("li", { text: n })));
  }

  function renderInField(root, en) {
    const list = $(".in-field", root);
    if (!en || !en.in_field.length) {
      list.replaceChildren(el("li", { class: "muted", text: en ? "Sin objetos de catálogo en el campo" : "—" }));
      return;
    }
    const kindLabel = (o) => (o.kind === "body" ? o.type : o.kind === "dso" ? "Cielo prof." : "Estrella");
    list.replaceChildren(...en.in_field.slice(0, 14).map((o) => el("li", {},
      el("span", { class: "kind", text: kindLabel(o) }),
      el("span", { text: o.kind === "dso" ? `${o.name} · ${o.type}` : o.name }),
      el("span", { class: "mag", text: o.mag != null ? `mag ${o.mag.toFixed(1)}` : "" }))));
  }

  function renderSky(root, node, en, tel, info) {
    const rows = [];
    if (en) {
      const s = en.sky;
      rows.push(["Cielo", `${s.twilight} (Sol a ${fmtDeg(s.sun_alt)})`]);
      rows.push(["Luna", `${s.moon_phase}, ${(s.moon_illuminated * 100).toFixed(0)} % · alt ${fmtDeg(s.moon_alt)}`]);
      rows.push(["Luna al campo", fmtDeg(s.moon_sep)]);
    }
    const m = info.mount;
    if (m) rows.push(["Montura", `${m.driver} (${m.type}) · límite ${m.min_altitude_deg}°`]);
    if (tel && tel.camera) rows.push(["Cámara", `${Math.round(tel.camera.exposure_ms)} ms · gan. ${tel.camera.gain} · ${tel.camera.fps} fps`]);
    if (node.clock_offset_s != null) {
      const off = node.clock_offset_s;
      rows.push(["Reloj", Math.abs(off) > 2 ? `desfase ${off.toFixed(1)} s ⚠ revisar NTP` : `sincronizado (${off.toFixed(2)} s)`]);
    }
    fillKV($(".sky", root), rows.length ? rows : [["Estado", node.online ? "Calculando…" : "Desconectado"]]);
  }

  // ------------------------------------------------------------------ superposición sobre el video
  function fitCanvas(canvas) {
    const rect = canvas.getBoundingClientRect();
    const dpr = window.devicePixelRatio || 1;
    const w = Math.max(1, Math.round(rect.width * dpr)), h = Math.max(1, Math.round(rect.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, rect.width, rect.height);
    return { ctx, w: rect.width, h: rect.height };
  }

  function label(ctx, text, x, y, color = "#ffffff") {
    ctx.font = "12px system-ui, sans-serif";
    ctx.lineWidth = 3;
    ctx.strokeStyle = "rgba(0,0,0,0.85)";
    ctx.strokeText(text, x, y);
    ctx.fillStyle = color;
    ctx.fillText(text, x, y);
  }

  function drawOverlay(v, node) {
    const { ctx, w, h } = fitCanvas(v.overlay);
    if (!node.online) return;
    const an = node.analysis, en = node.enrichment;
    const iw = v.img.naturalWidth || (an && an.width) || 4, ih = v.img.naturalHeight || (an && an.height) || 3;
    const s = Math.min(w / iw, h / ih), dw = iw * s, dh = ih * s, ox = (w - dw) / 2, oy = (h - dh) / 2;
    const at = (nx, ny) => [ox + nx * dw, oy + ny * dh];

    if (v.layers.reticle) {
      const [cx, cy] = at(0.5, 0.5);
      ctx.strokeStyle = "rgba(255,255,255,0.35)";
      ctx.lineWidth = 1;
      ctx.beginPath();
      ctx.moveTo(cx - 40, cy); ctx.lineTo(cx - 10, cy); ctx.moveTo(cx + 10, cy); ctx.lineTo(cx + 40, cy);
      ctx.moveTo(cx, cy - 40); ctx.lineTo(cx, cy - 10); ctx.moveTo(cx, cy + 10); ctx.lineTo(cx, cy + 40);
      ctx.stroke();
      ctx.beginPath(); ctx.arc(cx, cy, Math.min(dw, dh) * 0.12, 0, 2 * Math.PI); ctx.stroke();
    }
    if (v.layers.stars && an && an.stars) {
      ctx.strokeStyle = "#199e70";
      ctx.lineWidth = 1.5;
      for (const st of an.stars) {
        const [x, y] = at(st.x, st.y);
        const r = Math.max(5, Math.min(18, st.fwhm * s * 1.6));
        ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.stroke();
      }
    }
    if (v.layers.catalog && en) {
      const shown = en.in_field.filter((o) => o.kind !== "star" || (o.mag != null && o.mag < 6)).slice(0, 20);
      for (const o of shown) {
        const [x, y] = at(o.x, o.y);
        if (o.kind === "dso" && o.size_arcmin) {
          const r = (o.size_arcmin / 60) / en.field.fov_w * dw / 2;
          if (r > 6 && r < dw) {
            ctx.setLineDash([5, 4]);
            ctx.strokeStyle = "rgba(232,230,223,0.55)";
            ctx.lineWidth = 1;
            ctx.beginPath(); ctx.arc(x, y, r, 0, 2 * Math.PI); ctx.stroke();
            ctx.setLineDash([]);
          }
        }
        const lx = Math.min(Math.max(x + 8, 4), w - 150), ly = Math.min(Math.max(y - 8, 14), h - 4);
        label(ctx, o.name.split(" (")[0], lx, ly, "#e8e6df");
      }
    }
  }

  // ------------------------------------------------------------------ mapa del cielo
  function project(ra, dec, ra0, dec0) {
    const a = ra * D2R, d = dec * D2R, a0 = ra0 * D2R, d0 = dec0 * D2R;
    const cosc = Math.sin(d0) * Math.sin(d) + Math.cos(d0) * Math.cos(d) * Math.cos(a - a0);
    if (cosc <= 0.05) return null;
    const xi = Math.cos(d) * Math.sin(a - a0) / cosc;
    const eta = (Math.cos(d0) * Math.sin(d) - Math.sin(d0) * Math.cos(d) * Math.cos(a - a0)) / cosc;
    return [xi / D2R, eta / D2R];
  }
  function angSep(ra1, dec1, ra2, dec2) {
    const c = Math.sin(dec1 * D2R) * Math.sin(dec2 * D2R) + Math.cos(dec1 * D2R) * Math.cos(dec2 * D2R) * Math.cos((ra1 - ra2) * D2R);
    return Math.acos(Math.max(-1, Math.min(1, c))) / D2R;
  }

  function skyCenter() {
    const st = app.state;
    if (!st) return null;
    if (st.target) return [st.target.ra, st.target.dec];
    const n = st.nodes.find((x) => x.online && x.telemetry && x.telemetry.pointing);
    return n ? [n.telemetry.pointing.ra, n.telemetry.pointing.dec] : null;
  }

  async function fetchSky(center) {
    const sky = app.sky;
    sky.fetching = true;
    sky.lastFetch = Date.now();
    try {
      const radius = sky.radius * 1.5;
      const res = await fetch(`/api/skymap?ra=${center[0].toFixed(4)}&dec=${center[1].toFixed(4)}&radius=${Math.min(45, radius)}`);
      sky.data = await res.json();
      sky.dataCenter = center;
      sky.dataRadius = sky.radius;
    } catch { /* se reintenta en el próximo ciclo */ }
    sky.fetching = false;
    drawSkymap();
  }

  function zoom(dir) {
    const i = RADII.indexOf(app.sky.radius);
    app.sky.radius = RADII[Math.max(0, Math.min(RADII.length - 1, i + dir))];
    $("#zoom-label").textContent = `±${app.sky.radius}°`;
    drawSkymap();
  }

  function niceStep(target) {
    return [0.25, 0.5, 1, 2, 5, 10, 15, 30].find((s) => s >= target) || 30;
  }

  function drawSkymap() {
    const canvas = $("#skymap");
    const { ctx, w, h } = fitCanvas(canvas);
    const sky = app.sky;
    const center = skyCenter();
    renderSkyLegend();
    if (!center) {
      ctx.fillStyle = "#898781"; ctx.font = "13px system-ui, sans-serif"; ctx.textAlign = "center";
      ctx.fillText("Sin posición: conecta un telescopio o elige un objetivo", w / 2, h / 2);
      ctx.textAlign = "start";
      return;
    }
    const needFetch = !sky.data || sky.dataRadius !== sky.radius
      || angSep(center[0], center[1], sky.dataCenter[0], sky.dataCenter[1]) > sky.radius / 4;
    if (needFetch && !sky.fetching && Date.now() - sky.lastFetch > 700) fetchSky(center);

    const [ra0, dec0] = center;
    const scale = Math.min(w, h) / 2 / sky.radius;   // px por grado
    const toXY = (ra, dec) => {
      const p = project(ra, dec, ra0, dec0);
      return p ? [w / 2 - p[0] * scale, h / 2 - p[1] * scale] : null;   // Este a la izquierda
    };
    sky.hits = [];
    sky.polys = [];

    // Retícula de coordenadas (recesiva).
    const decStep = niceStep(sky.radius / 3);
    const raStep = niceStep(decStep / Math.max(0.1, Math.cos(dec0 * D2R)));
    const span = sky.radius * 2;
    ctx.strokeStyle = "#2c2c2a"; ctx.lineWidth = 1;
    ctx.font = "11px system-ui, sans-serif"; ctx.fillStyle = "#898781";
    for (let d = Math.ceil((dec0 - span) / decStep) * decStep; d <= dec0 + span; d += decStep) {
      if (d < -90 || d > 90) continue;
      ctx.beginPath();
      let started = false, lastPt = null;
      for (let k = -60; k <= 60; k++) {
        const pt = toXY(ra0 + k * (180 / 60) * Math.min(1, span / 30 + 0.05), d);
        if (!pt) { started = false; continue; }
        if (!started) { ctx.moveTo(pt[0], pt[1]); started = true; } else ctx.lineTo(pt[0], pt[1]);
        if (pt[0] > 0 && pt[0] < w && pt[1] > 10 && pt[1] < h) lastPt = pt;
      }
      ctx.stroke();
      if (lastPt) ctx.fillText(`${d >= 0 ? "+" : "−"}${Math.abs(d)}°`, 4, lastPt[1] - 3);
    }
    for (let r = Math.floor((ra0 - 180) / raStep) * raStep; r < ra0 + 180; r += raStep) {
      ctx.beginPath();
      let started = false, labelPt = null;
      for (let k = -60; k <= 60; k++) {
        const pt = toXY(r, Math.max(-89.9, Math.min(89.9, dec0 + k * span / 60)));
        if (!pt) { started = false; continue; }
        if (!started) { ctx.moveTo(pt[0], pt[1]); started = true; } else ctx.lineTo(pt[0], pt[1]);
        if (pt[1] > h - 20 && pt[1] < h && pt[0] > 20 && pt[0] < w - 40) labelPt = pt;
      }
      ctx.stroke();
      if (labelPt) {
        const hrs = ((r % 360) + 360) % 360 / 15;
        const hh = Math.floor(hrs + 1e-9), mm = Math.round((hrs - hh) * 60);
        ctx.fillText(`${hh}h${mm ? pad(mm) : ""}`, labelPt[0] + 3, h - 6);
      }
    }

    if (sky.data) {
      // Objetos de cielo profundo.
      for (const [ra, dec, mag, id, type, size] of sky.data.dsos) {
        const pt = toXY(ra, dec);
        if (!pt) continue;
        const r = Math.max(3, (size || 1) / 60 * scale / 2);
        ctx.strokeStyle = "rgba(195,194,183,0.55)"; ctx.lineWidth = 1;
        ctx.beginPath(); ctx.arc(pt[0], pt[1], r, 0, 2 * Math.PI); ctx.stroke();
        if (id.startsWith("M ") || r > 12) label(ctx, id, pt[0] + r + 3, pt[1] + 4, "#c3c2b7");
        sky.hits.push({ x: pt[0], y: pt[1], title: id, sub: `${type}${mag != null ? ` · mag ${mag.toFixed(1)}` : ""}`, ra, dec });
      }
      // Estrellas: el tamaño codifica el brillo.
      for (const [ra, dec, mag, name] of sky.data.stars) {
        const pt = toXY(ra, dec);
        if (!pt || pt[0] < -5 || pt[0] > w + 5 || pt[1] < -5 || pt[1] > h + 5) continue;
        const r = Math.max(0.7, Math.min(5, 0.7 + (6.5 - mag) * 0.55));
        ctx.fillStyle = "rgba(255,255,255,0.92)";
        ctx.beginPath(); ctx.arc(pt[0], pt[1], r, 0, 2 * Math.PI); ctx.fill();
        if (name) label(ctx, name.split(" (")[0], pt[0] + r + 3, pt[1] - 3, "#c3c2b7");
        if (mag < 5.5 || name) sky.hits.push({ x: pt[0], y: pt[1], title: name || "Estrella", sub: `mag ${mag.toFixed(1)}`, ra, dec });
      }
      // Sistema solar.
      for (const [ra, dec, mag, name, size] of sky.data.bodies) {
        const pt = toXY(ra, dec);
        if (!pt) continue;
        const r = Math.max(4, (size || 0) / 60 * scale / 2);
        ctx.fillStyle = "#e8e6df";
        ctx.beginPath(); ctx.arc(pt[0], pt[1], r, 0, 2 * Math.PI); ctx.fill();
        label(ctx, name, pt[0] + r + 4, pt[1] + 4, "#ffffff");
        sky.hits.push({ x: pt[0], y: pt[1], title: name, sub: `mag ${mag.toFixed(1)}`, ra, dec });
      }
    }

    // Campos de visión de cada telescopio (color = identidad del nodo, nombre como etiqueta directa).
    app.state.nodes.forEach((n, idx) => {
      const en = n.enrichment;
      if (!n.online || !en) return;
      const pts = en.field.corners.map((c) => toXY(c.ra, c.dec));
      if (pts.some((p) => !p)) return;
      ctx.strokeStyle = nodeColor(idx); ctx.lineWidth = 2;
      ctx.beginPath();
      pts.forEach((p, i) => (i ? ctx.lineTo(p[0], p[1]) : ctx.moveTo(p[0], p[1])));
      ctx.closePath(); ctx.stroke();
      // Etiquetas alternadas arriba/abajo para que no se encimen cuando los campos coinciden.
      const above = idx % 2 === 0;
      const anchor = pts.reduce((a, b) => ((above ? b[1] < a[1] : b[1] > a[1]) ? b : a));
      label(ctx, n.name, anchor[0] + 4, above ? anchor[1] - 6 : anchor[1] + 16, "#ffffff");
      const p = n.telemetry.pointing;
      sky.polys.push({ pts, title: n.name, sub: `AR ${fmtRA(p.ra)} · Dec ${fmtDec(p.dec)} · campo ${en.field.fov_w.toFixed(2)}°×${en.field.fov_h.toFixed(2)}°` });
    });

    // Objetivo común.
    const t = app.state.target;
    if (t) {
      const pt = toXY(t.ra, t.dec);
      if (pt) {
        ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 1.5;
        ctx.beginPath();
        ctx.moveTo(pt[0] - 14, pt[1]); ctx.lineTo(pt[0] - 5, pt[1]); ctx.moveTo(pt[0] + 5, pt[1]); ctx.lineTo(pt[0] + 14, pt[1]);
        ctx.moveTo(pt[0], pt[1] - 14); ctx.lineTo(pt[0], pt[1] - 5); ctx.moveTo(pt[0], pt[1] + 5); ctx.lineTo(pt[0], pt[1] + 14);
        ctx.stroke();
      }
    }
    // Rosa de los vientos: Norte arriba, Este a la izquierda.
    ctx.fillStyle = "#898781"; ctx.font = "12px system-ui, sans-serif";
    ctx.fillText("N ↑", w - 34, 18); ctx.fillText("← E", w - 34, 34);
  }

  function renderSkyLegend() {
    const st = app.state;
    const box = $("#skymap-legend");
    const key = st ? st.nodes.map((n) => n.id).join("|") : "";
    if (box.dataset.key === key) return;
    box.dataset.key = key;
    box.replaceChildren();
    if (!st) return;
    st.nodes.forEach((n, idx) => box.append(el("span", { class: "key" },
      el("span", { class: "line", style: `--c:${nodeColor(idx)}` }), document.createTextNode(`Campo ${n.name}`))));
    box.append(el("span", { class: "key" }, el("span", { class: "line", style: "--c:#ffffff" }), document.createTextNode("Objetivo")));
  }

  function pointInPoly(x, y, pts) {
    let inside = false;
    for (let i = 0, j = pts.length - 1; i < pts.length; j = i++) {
      const [xi, yi] = pts[i], [xj, yj] = pts[j];
      if ((yi > y) !== (yj > y) && x < (xj - xi) * (y - yi) / (yj - yi) + xi) inside = !inside;
    }
    return inside;
  }

  function setupSkymapHover() {
    const canvas = $("#skymap"), tip = $("#skymap-tip");
    canvas.addEventListener("pointermove", (e) => {
      const rect = canvas.getBoundingClientRect();
      const x = e.clientX - rect.left, y = e.clientY - rect.top;
      let best = null, bestD = 14;
      for (const hit of app.sky.hits) {
        const d = Math.hypot(hit.x - x, hit.y - y);
        if (d < bestD) { best = hit; bestD = d; }
      }
      let title, sub;
      if (best) {
        title = best.title;
        sub = `${best.sub} · AR ${fmtRA(best.ra)} · Dec ${fmtDec(best.dec)}`;
      } else {
        const poly = app.sky.polys.find((p) => pointInPoly(x, y, p.pts));
        if (poly) { title = `Campo de ${poly.title}`; sub = poly.sub; }
      }
      if (!title) { tip.hidden = true; return; }
      tip.replaceChildren(el("strong", { text: title }), el("div", { class: "muted", text: sub }));
      tip.hidden = false;
      tip.style.left = `${Math.min(x + 14, rect.width - 270)}px`;
      tip.style.top = `${y + 14}px`;
    });
    canvas.addEventListener("pointerleave", () => { tip.hidden = true; });
    canvas.addEventListener("wheel", (e) => { e.preventDefault(); zoom(e.deltaY > 0 ? 1 : -1); }, { passive: false });
    window.addEventListener("resize", scheduleRender);
  }

  // ------------------------------------------------------------------ inicio
  setupControls();
  setupSkymapHover();
  connect();
})();
