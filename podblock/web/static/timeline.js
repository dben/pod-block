// pod-block timeline: an episode's waveform with its cut sections, to check them and change them.
// Nothing is written until Save; the page asks before leaving with unsaved changes.
(() => {
  "use strict";
  const root = document.querySelector("[data-timeline]");
  if (!root) return;
  const data = JSON.parse(document.getElementById("timeline-data").textContent);
  const $ = (name) => document.querySelector(`[data-tl="${name}"]`);
  const audio = root.querySelector("audio[data-original]");
  const scroll = $("scroll"), track = $("track"), wave = $("wave"), canvas = $("canvas");
  const labels = $("labels"), plays = $("plays"), playhead = $("playhead"), list = $("list");
  const falseBox = $("false"), falseList = $("false-list");
  const overview = $("overview"), ocanvas = overview.querySelector("canvas"), owindow = overview.querySelector(".tl-window");
  const savebar = $("savebar");
  const duration = data.duration;
  const editable = data.editable;
  const MIN_LEN = 1.5;  // the shortest block the detector keeps
  const MAX_PPS = 40;   // pixels per second, zoomed all the way in
  const RULER = 18;     // px under the waveform for times

  // Intro, Outro and Keep are this show's own; Not a repeat is for every show. Local is for sections marked by hand.
  const VALUES = [["auto", null], ["intro", "Intro: cut it and everything before"], ["outro", "Outro: cut it and everything after"],
                  ["kept", "Keep (this show)"], ["ignored", "Not a repeat"]];
  const SHORT = { intro: "Intro", outro: "Outro", show: "Local", kept: "Keep", ignored: "Not a repeat" };  // for labels on the timeline
  const BLOCK_TYPES = [["show", "Local"], ["intro", "Intro"], ["outro", "Outro"]];

  const sections = [
    ...data.sections.map((s) => ({ ...s, saved: s.value, deleted: false })),
    ...data.manual.map((m) => ({ ...m, manual: true, removed: false })),
  ];
  // False positives that play here: not on the timeline, only in a list of their own, to undo the flag.
  const falses = data.not_repeats.map((x) => ({ ...x, value: "ignored", saved: "ignored", bumpers: [] }));
  let peaks = new Uint8Array(0);
  let perSecond = 10;
  let pps = 0, fitPps = 0;
  let selected = null;
  let made = 0;
  let playing = null;  // the section played from its own button: playback stops at its end

  // ---- small helpers --------------------------------------------------------

  const clamp = (v, lo, hi) => Math.min(hi, Math.max(lo, v));
  function clock(t, tenths = false) {
    t = Math.max(0, t);
    const h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60);
    const s = tenths ? (t % 60).toFixed(1).padStart(4, "0") : String(Math.floor(t % 60)).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }
  function parseClock(text) {
    const parts = text.trim().split(":").map(Number);
    if (!parts.length || parts.some((p) => !Number.isFinite(p))) return null;
    return parts.reduce((acc, p) => acc * 60 + p, 0);
  }
  function h(tag, attrs = {}, ...children) {
    const el = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) {
      if (k === "class") el.className = v;
      else if (k.startsWith("on")) el.addEventListener(k.slice(2), v);
      else if (v === true) el.setAttribute(k, "");
      else if (v !== false && v != null) el.setAttribute(k, v);
    }
    el.append(...children.filter((c) => c != null));
    return el;
  }
  const icon = (d) => {
    const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("class", "icon");
    svg.innerHTML = d;
    return svg;
  };
  const PLAY = '<path d="M8 5.5v13l10-6.5z"/>';
  const KIND_ICONS = {  // for labels shrunk to a chip when there is no room for the dropdown
    intro: '<path d="M5 5v14M19 12H9m0 0 4-4m-4 4 4 4"/>',
    outro: '<path d="M19 5v14M5 12h10m0 0-4-4m4 4-4 4"/>',
    break: '<rect x="4" y="7" width="16" height="10" rx="2"/><path d="M8 14l1.5-4 1.5 4m-2.4-1.2h1.8M14 10v4h1a2 2 0 0 0 0-4z"/>',
    repeated: '<path d="M17 3l3 3-3 3M4 11V9a3 3 0 0 1 3-3h13M7 21l-3-3 3-3m13-2v2a3 3 0 0 1-3 3H4"/>',
    show: '<path d="m12 4 2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 16.4 7.2 19l.9-5.4-3.9-3.8 5.4-.8z"/>',
    ignored: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="2.5"/>',
    manual: '<circle cx="6" cy="7" r="2.5"/><circle cx="6" cy="17" r="2.5"/><path d="M8 8.5 20 17M8 15.5 20 7"/>',
    removed: '<path d="M6 6l12 12M18 6 6 18"/>',
  };
  const LABEL_W = { full: 124, text: 130, chip: 30 };  // px each kind of label takes
  const GAP = 6;
  const PAUSE = '<path d="M8 5v14M16 5v14"/>';
  const toast = (text, kind) => window.podblock?.toast(text, { kind });

  const isNew = (s) => s.manual && s.isNew;
  const isCut = (s) => !s.removed && !s.deleted && !(!s.manual && (s.value === "ignored" || s.value === "kept"));
  const allOwn = (s) => !s.manual && s.own.length > 0 && s.own.length === s.blocks.length;  // only blocks marked by hand
  function kind(s) {
    if (s.manual) return s.removed ? "removed" : "manual";
    if (s.deleted) return "removed";
    if (s.value === "auto") return s.is_break ? "break" : "repeated";
    return s.value === "kept" ? "ignored" : s.value;
  }
  function name(s) {
    if (s.manual) return isNew(s) ? "New section" : "Cut by hand";
    if (s.value === "auto") return s.is_break ? "Ad break" : "Repeat";
    return { intro: "Intro", outro: "Outro", show: "Local", kept: "Kept", ignored: "Not a repeat" }[s.value];
  }

  // ---- what has changed -------------------------------------------------------

  function changes() {
    const types = [], add = [], remove = [], del = [];
    for (const s of [...sections, ...falses]) {
      if (isNew(s)) add.push({ start: s.start, end: s.end, as_block: !!s.asBlock, type: s.blockType || "show" });
      else if (s.manual && s.removed) remove.push(s.edit);
      else if (s.deleted) del.push(...s.own);
      else if (!s.manual && s.value !== s.saved) {
        types.push({ type: s.value, blocks: s.blocks, bumpers: s.bumpers.map((b) => b.block), start: s.start, end: s.end });
      }
    }
    return { types, add, remove, delete: del };
  }
  const unsaved = () => { const c = changes(); return c.types.length + c.add.length + c.remove.length + c.delete.length; };

  function refreshBar() {
    const n = unsaved();
    savebar.hidden = n === 0;
    savebar.querySelector("[data-tl=dirty]").textContent = `${n} unsaved ${n === 1 ? "change" : "changes"}`;
    document.body.classList.toggle("has-savebar", n > 0);
  }

  window.addEventListener("beforeunload", (e) => {
    if (unsaved()) { e.preventDefault(); e.returnValue = ""; }
  });

  // ---- building the regions, labels, play buttons and list ----------------------

  function valueSelect(s, short = false) {
    const sel = h("select", { class: "tl-select", "aria-label": "What this section is", disabled: !editable });
    const values = s.saved === "show" ? [...VALUES, ["show", "Local: this show"]] : VALUES;  // set from the command line
    for (const [value, text] of values) {
      const found = s.is_break ? "Ad break" : "Repeat";
      sel.append(h("option", { value }, short ? SHORT[value] || found : text || `${found} (as found)`));
    }
    sel.value = s.value;
    sel.addEventListener("change", () => { s.value = sel.value; sync(s); });
    return sel;
  }

  function playButton(s, cls) {
    const btn = h("button", { type: "button", class: cls, onclick: (e) => { e.stopPropagation(); playSection(s); } });
    s.buttons.push(btn);
    showPlaying(btn, s);
    return btn;
  }

  function showPlaying(btn, s) {
    const on = playing === s && !audio.paused;
    btn.replaceChildren(icon(on ? PAUSE : PLAY));
    btn.setAttribute("aria-label", on ? "Pause" : `Play from ${clock(s.start)}`);
  }
  const showAllPlaying = () => { for (const s of [...sections, ...falses]) for (const btn of s.buttons || []) showPlaying(btn, s); };

  function timeEditor(s, which) {
    const input = h("input", { class: "input tl-time-input mono", value: clock(s[which], true), inputmode: "decimal",
                               "aria-label": which === "start" ? "Starts at" : "Ends at" });
    const set = (t) => {
      if (t == null) { input.value = clock(s[which], true); return; }
      if (which === "start") s.start = clamp(t, 0, s.end - MIN_LEN);
      else s.end = clamp(t, s.start + MIN_LEN, duration);
      input.value = clock(s[which], true);
      place();
      sync(s, false);
    };
    input.addEventListener("change", () => set(parseClock(input.value)));
    const nudge = (by, text) => h("button", { type: "button", class: "btn btn-ghost btn-xs", onclick: () => set(s[which] + by) }, text);
    return h("div", { class: "tl-time-edit" },
      h("span", { class: "tl-time-label" }, which === "start" ? "Start" : "End"),
      nudge(-1, "−1s"), input, nudge(1, "+1s"),
      h("button", { type: "button", class: "btn btn-ghost btn-xs", title: "Set to where the player is",
                    onclick: () => set(audio.currentTime) }, "Here"));
  }

  function card(s) {
    const times = h("span", { class: "tl-item-times mono" }, `${clock(s.start)} – ${clock(s.end)}`,
                    h("span", { class: "muted" }, ` · ${clock(s.end - s.start)}`));
    const head = h("div", { class: "tl-item-head" }, h("span", { class: "tl-dot" }), h("b", { class: "tl-item-name" }, name(s)),
                   times, s.custom ? h("span", { class: "tl-pill", title: "Changed or marked by hand, for this show's episodes" },
                                       "Custom (this show)") : null);
    const li = h("li", { class: `tl-item k-${kind(s)}${isNew(s) ? " tl-item-new" : ""}`, "data-id": s.id },
                 head, playButton(s, "icon-btn tl-item-play"));
    li.addEventListener("click", (e) => { if (!e.target.closest("button, select, input, label")) focusSection(s); });
    if (!s.manual) {
      const row = h("div", { class: "tl-item-row" }, allOwn(s) ? null : valueSelect(s));
      if (s.bumpers.length) row.append(h("span", { class: "hint" }, "Found from the bumpers around it"));
      if (editable && s.custom && !allOwn(s) && s.saved !== "auto" && s.value === s.saved) {
        row.append(h("button", { type: "button", class: "btn btn-ghost btn-xs danger", title: "Go back to how it was found",
                                 onclick: () => { s.value = "auto"; render(); } }, "Remove"));
      }
      if (editable && s.own.length) {
        row.append(h("button", { type: "button", class: "btn btn-ghost btn-xs danger",
                                 onclick: () => { s.deleted = !s.deleted; render(); } }, s.deleted ? "Undo" : "Delete"));
      }
      li.append(row);
    } else if (isNew(s)) {
      const asBlock = h("input", { type: "checkbox", checked: !!s.asBlock });
      const blockType = h("select", { class: "tl-select", "aria-label": "What it is", hidden: !s.asBlock });
      for (const [value, text] of BLOCK_TYPES) blockType.append(h("option", { value }, text));
      blockType.value = s.blockType || "show";
      asBlock.addEventListener("change", () => { s.asBlock = asBlock.checked; blockType.hidden = !asBlock.checked; });
      blockType.addEventListener("change", () => { s.blockType = blockType.value; });
      li.append(timeEditor(s, "start"), timeEditor(s, "end"),
        h("div", { class: "tl-item-row" },
          h("label", { class: "tl-check" }, asBlock, " Catch it in this show's other episodes too"), blockType,
          h("button", { type: "button", class: "btn btn-ghost btn-xs danger", onclick: () => drop(s) }, "Remove")));
    } else if (editable) {
      li.append(h("div", { class: "tl-item-row" },
        h("button", { type: "button", class: "btn btn-ghost btn-xs", onclick: () => { s.removed = !s.removed; render(); } },
          s.removed ? "Undo" : "Don't cut this"),
        s.removed ? h("span", { class: "hint" }, "Will be put back") : null));
    }
    return li;
  }

  // A false positive: play it, or undo the flag so it is cut again, in every show. A section just set to
  // Not a repeat is listed here too, off the timeline, until it is saved or put back.
  function falseCard(s) {
    s.buttons = [];
    const pending = s.saved !== "ignored";
    const undone = !pending && s.value !== s.saved;
    const back = () => { s.value = pending ? s.saved : undone ? "ignored" : "auto"; render(); };
    return h("li", { class: `tl-item k-${undone ? "repeated" : "ignored"}` },
      h("div", { class: "tl-item-head" }, h("span", { class: "tl-dot" }), h("b", { class: "tl-item-name" }, "Not a repeat"),
        h("span", { class: "tl-item-times mono" }, `${clock(s.start)} – ${clock(s.end)}`,
          h("span", { class: "muted" }, ` · ${clock(s.end - s.start)}`)),
        h("span", { class: "tl-pill" }, "Custom (all shows)")),
      playButton(s, "icon-btn tl-item-play"),
      editable ? h("div", { class: "tl-item-row" },
        h("button", { type: "button", class: "btn btn-ghost btn-xs", onclick: back }, pending || undone ? "Undo" : "Cut again"),
        pending ? h("span", { class: "hint" }, "Won't be cut in any show") : null) : null);
  }

  function render() {
    for (const el of root.querySelectorAll(".tl-region, .tl-label, .tl-play, .tl-bumper")) el.remove();
    list.replaceChildren();
    const live = sections.filter((s) => !(isNew(s) && s.dropped)).sort((a, b) => a.start - b.start);
    const flagged = live.filter((s) => !s.manual && s.value === "ignored");  // listed below, off the timeline
    for (const s of flagged) s.region = s.label = s.play = s.card = null;
    const ordered = live.filter((s) => !flagged.includes(s));
    for (const s of ordered) {
      s.buttons = [];
      s.region = h("div", { class: `tl-region k-${kind(s)}${isNew(s) ? " tl-new" : ""}`, "data-id": s.id, title: name(s) });
      s.region.addEventListener("pointerdown", (e) => { if (!e.target.closest(".tl-handle")) select(s); });
      if (isNew(s) && editable) {
        for (const side of ["start", "end"]) {
          const handle = h("span", { class: `tl-handle tl-handle-${side}`, "aria-hidden": "true" });
          handle.addEventListener("pointerdown", (e) => dragEdge(e, s, side));
          s.region.append(handle);
        }
      }
      wave.append(s.region);
      s.label = h("div", { class: `tl-label k-${kind(s)}` },
        !s.manual && !allOwn(s) && editable ? valueSelect(s, true) : h("span", { class: "tl-label-text" }, name(s)),
        h("button", { type: "button", class: "tl-chip", title: `${name(s)}, ${clock(s.start)}: click to zoom in`,
                      "aria-label": `${name(s)} at ${clock(s.start)}`, onclick: () => focusSection(s) },
          icon(KIND_ICONS[kind(s)] || KIND_ICONS.repeated)));
      labels.append(s.label);
      s.play = playButton(s, `tl-play k-${kind(s)}`);
      plays.append(s.play);
      s.bands = (s.bumpers || []).map((b) => {
        const band = h("div", { class: `tl-bumper k-${kind(s)}`, title: "Bumper" });
        wave.append(band);
        return [band, b];
      });
      s.card = card(s);
      list.append(s.card);
    }
    if (!ordered.length) list.append(h("li", { class: "muted tl-empty" }, "Nothing is cut from this episode yet."));
    const listed = [...flagged, ...falses].sort((a, b) => a.start - b.start);
    falseBox.hidden = !listed.length;
    if (flagged.some((s) => !s.shownFlagged)) falseBox.open = true;  // show where a section just went
    for (const s of sections) s.shownFlagged = flagged.includes(s);
    falseBox.querySelector("[data-tl=false-count]").textContent = listed.length;
    falseList.replaceChildren(...listed.map(falseCard));
    place();
    drawOverview();
    refreshBar();
    if (selected) select(selected, false);
  }

  // A section's look changed (its type, or its times): update it in place.
  function sync(s, rebuild = true) {
    if (rebuild) { render(); return; }
    s.card.querySelector(".tl-item-times").firstChild.textContent = `${clock(s.start)} – ${clock(s.end)}`;
    s.card.querySelector(".tl-item-times .muted").textContent = ` · ${clock(s.end - s.start)}`;
    drawOverview();
    refreshBar();
  }

  function addSection(start, end) {
    const s = { id: `n${++made}`, start, end, manual: true, isNew: true, asBlock: false, blockType: "repeated" };
    sections.push(s);
    return s;
  }
  function drop(s) {
    sections.splice(sections.indexOf(s), 1);
    if (selected === s) selected = null;
    if (playing === s) { playing = null; audio.pause(); }
    render();
  }

  function select(s, scrollCard = true) {
    selected = s;
    for (const el of root.querySelectorAll(".is-selected")) el.classList.remove("is-selected");
    for (const el of [s.region, s.card, s.label]) el?.classList.add("is-selected");
    place();
    if (scrollCard) s.card?.scrollIntoView({ block: "nearest", behavior: "smooth" });
  }

  // Bring a section into view, zooming in if it would be too thin to see.
  function focusSection(s) {
    const w = scroll.clientWidth;
    if ((s.end - s.start) * pps < 60) setZoom(clamp(w / 3 / Math.max(s.end - s.start, 1), fitPps, MAX_PPS));
    scroll.scrollLeft = s.start * pps - Math.max(40, (w - (s.end - s.start) * pps) / 2);
    select(s, false);
  }

  // ---- layout and drawing -------------------------------------------------------

  const xAt = (t) => t * pps;
  function timeAt(clientX) {
    const r = scroll.getBoundingClientRect();
    return clamp((clientX - r.left + scroll.scrollLeft) / pps, 0, duration);
  }

  // Labels go in two rows above the waveform: a full dropdown where there is room, else a small
  // icon chip, else nothing (the list below always has every control). The selected one goes first.
  function place() {
    const shown = sections.filter((s) => s.region);
    const rows = [[], []];
    const plays = [];
    const free = (taken, a, b) => taken.every(([x, y]) => b + GAP <= x || a >= y + GAP);
    const order = [...shown].sort((a, b) => (b === selected) - (a === selected) || a.start - b.start);
    for (const s of order) {
      const left = xAt(s.start), w = Math.max(2, xAt(s.end) - left);
      Object.assign(s.region.style, { left: `${left}px`, width: `${w}px` });
      for (const [band, b] of s.bands) {
        Object.assign(band.style, { left: `${xAt(b.start)}px`, width: `${Math.max(2, xAt(b.end) - xAt(b.start))}px` });
      }
      const fullWidth = s.manual || !editable ? LABEL_W.text : LABEL_W.full;
      let placed = null;
      for (const [mode, width] of [["full", fullWidth], ["chip", LABEL_W.chip]]) {
        const row = rows.findIndex((taken) => free(taken, left, left + width));
        if (row >= 0) { rows[row].push([left, left + width]); placed = { mode, row }; break; }
      }
      s.label.hidden = !placed;
      s.label.style.left = `${left}px`;
      s.label.classList.toggle("tl-compact", placed?.mode === "chip");
      s.label.classList.toggle("tl-row-2", placed?.row === 1);
      const centre = left + w / 2;
      s.play.style.left = `${centre}px`;
      s.play.hidden = !free(plays, centre - 16, centre + 16);
      if (!s.play.hidden) plays.push([centre - 16, centre + 16]);
    }
    playhead.style.left = `${xAt(audio.currentTime || 0)}px`;
  }

  function setZoom(next, anchorTime = null, anchorX = 0) {
    fitPps = scroll.clientWidth / duration;
    pps = clamp(next, fitPps, MAX_PPS);
    track.style.width = `${Math.max(scroll.clientWidth, Math.round(duration * pps))}px`;
    if (anchorTime != null) scroll.scrollLeft = anchorTime * pps - anchorX;
    place();
    draw();
    drawOverview();
  }

  function color(name) { return getComputedStyle(root).getPropertyValue(name).trim() || "#888"; }

  function tickStep() {
    for (const step of [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600]) if (step * pps >= 70) return step;
    return 7200;
  }

  function loudest(from, to) {
    let m = 0;
    for (let i = Math.max(0, from); i < to && i < peaks.length; i++) if (peaks[i] > m) m = peaks[i];
    return m;
  }

  function draw() {
    const w = scroll.clientWidth, height = wave.clientHeight, dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(height * dpr)) {
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(height * dpr);
      canvas.style.width = `${w}px`;
      canvas.style.height = `${height}px`;
    }
    const left = scroll.scrollLeft;
    canvas.style.left = `${left}px`;  // the canvas only ever covers what is in view
    const g = canvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, height);
    const mid = (height - RULER) / 2, room = mid - 6;
    g.fillStyle = color("--wave");
    for (let px = 0; px < w; px++) {
      const t0 = (left + px) / pps, t1 = (left + px + 1) / pps;
      const i0 = Math.floor(t0 * perSecond), i1 = Math.max(i0 + 1, Math.ceil(t1 * perSecond));
      const bar = Math.max(0.5, (loudest(i0, i1) / 255) * room);
      g.fillRect(px, mid - bar, 1, bar * 2);
    }
    g.fillStyle = color("--border");
    g.fillRect(0, height - RULER, w, 1);
    g.fillStyle = color("--faint");
    g.font = "11px ui-monospace, SFMono-Regular, Menlo, monospace";
    const step = tickStep();
    for (let t = Math.ceil(left / pps / step) * step; t * pps < left + w; t += step) {
      const px = Math.round(t * pps - left);
      g.fillRect(px, height - RULER, 1, 5);
      g.fillText(clock(t), px + 4, height - 5);
    }
  }

  function drawOverview() {
    const w = overview.clientWidth, height = overview.clientHeight, dpr = window.devicePixelRatio || 1;
    if (!w) return;
    ocanvas.width = Math.round(w * dpr);
    ocanvas.height = Math.round(height * dpr);
    ocanvas.style.width = `${w}px`;
    ocanvas.style.height = `${height}px`;
    const g = ocanvas.getContext("2d");
    g.setTransform(dpr, 0, 0, dpr, 0, 0);
    g.clearRect(0, 0, w, height);
    const mid = (height - 6) / 2, per = duration / w;
    g.fillStyle = color("--wave-faint");
    for (let px = 0; px < w; px++) {
      const i0 = Math.floor(px * per * perSecond), i1 = Math.ceil((px + 1) * per * perSecond);
      const bar = Math.max(0.5, (loudest(i0, i1) / 255) * (mid - 2));
      g.fillRect(px, mid - bar, 1, bar * 2);
    }
    for (const s of sections) {
      if (!s.region || !isCut(s)) continue;
      g.fillStyle = getComputedStyle(s.region).getPropertyValue("--c").trim() || "#888";
      g.fillRect(s.start / per, height - 5, Math.max(2, (s.end - s.start) / per), 5);
    }
    const total = duration * pps;
    const shown = scroll.clientWidth / total;
    owindow.hidden = shown >= 0.999;
    Object.assign(owindow.style, { left: `${(scroll.scrollLeft / total) * w}px`, width: `${shown * w}px` });
  }

  let framed = false;
  scroll.addEventListener("scroll", () => {
    if (framed) return;
    framed = true;
    requestAnimationFrame(() => { framed = false; draw(); drawOverview(); });
  });

  new ResizeObserver(() => {
    const atFit = pps <= fitPps * 1.001;
    const centre = (scroll.scrollLeft + scroll.clientWidth / 2) / (pps || 1);
    setZoom(atFit ? 0 : pps, centre, scroll.clientWidth / 2);
  }).observe(scroll);

  function zoomBy(factor, clientX) {
    const r = scroll.getBoundingClientRect();
    const anchorX = clientX == null ? scroll.clientWidth / 2 : clientX - r.left;
    setZoom(pps * factor, (scroll.scrollLeft + anchorX) / pps, anchorX);
  }
  $("zoom-in").addEventListener("click", () => zoomBy(2));
  $("zoom-out").addEventListener("click", () => zoomBy(0.5));
  $("fit").addEventListener("click", () => setZoom(0, 0, 0));
  scroll.addEventListener("wheel", (e) => {
    if (!(e.ctrlKey || e.metaKey)) return;
    e.preventDefault();
    zoomBy(e.deltaY < 0 ? 1.25 : 0.8, e.clientX);
  }, { passive: false });

  function moveTo(clientX) {
    const r = overview.getBoundingClientRect();
    const t = clamp((clientX - r.left) / r.width, 0, 1) * duration;
    scroll.scrollLeft = t * pps - scroll.clientWidth / 2;
  }
  overview.addEventListener("pointerdown", (e) => {
    overview.setPointerCapture(e.pointerId);
    moveTo(e.clientX);
    const move = (ev) => moveTo(ev.clientX);
    overview.addEventListener("pointermove", move);
    overview.addEventListener("pointerup", () => overview.removeEventListener("pointermove", move), { once: true });
  });

  // ---- playing ------------------------------------------------------------------

  const playBtn = $("play"), timeText = $("time"), preview = $("preview");

  function seek(t, play = false) {
    const go = () => {
      audio.currentTime = clamp(t, 0, duration);
      if (play) audio.play();
      place();
    };
    if (audio.readyState >= 1) go();
    else { audio.addEventListener("loadedmetadata", go, { once: true }); audio.load(); }
  }

  // Play a section from just before it until its end; its button pauses it, and plays on from there.
  function playSection(s) {
    select(s);
    if (playing === s && !audio.paused) { audio.pause(); return; }
    const within = audio.currentTime >= s.start - 2 && audio.currentTime < s.end;
    const resume = playing === s && within;
    playing = s;
    if (resume) audio.play();
    else seek(s.start - 2, true);
  }

  // The main button, the space bar and clicks on the waveform play on with no end.
  const togglePlay = () => { playing = null; audio.paused ? audio.play() : audio.pause(); showAllPlaying(); };
  playBtn.addEventListener("click", togglePlay);
  audio.addEventListener("play", () => { playBtn.replaceChildren(icon(PAUSE)); playBtn.setAttribute("aria-label", "Pause"); showAllPlaying(); tick(); });
  audio.addEventListener("pause", () => { playBtn.replaceChildren(icon(PLAY)); playBtn.setAttribute("aria-label", "Play"); showAllPlaying(); });
  audio.addEventListener("timeupdate", () => { if (audio.paused) showTime(); });

  function showTime() {
    const t = audio.currentTime || 0;
    timeText.textContent = `${clock(t)} / ${clock(duration)}`;
    playhead.style.left = `${xAt(t)}px`;
  }

  function tick() {
    if (audio.paused) return;
    const t = audio.currentTime;
    if (playing && t >= playing.end) {
      audio.pause();
      playing = null;
      showAllPlaying();
      showTime();
      return;
    }
    if (preview.checked) {  // hear the episode as it would be cut, unsaved changes and all; not the section being played
      const inCut = sections.find((s) => s !== playing && s.region && isCut(s) && t >= s.start && t < s.end - 0.05);
      if (inCut) audio.currentTime = inCut.end;
    }
    showTime();
    const x = xAt(audio.currentTime) - scroll.scrollLeft;
    if (x < 0 || x > scroll.clientWidth - 20) scroll.scrollLeft = xAt(audio.currentTime) - scroll.clientWidth * 0.2;
    requestAnimationFrame(tick);
  }

  document.addEventListener("keydown", (e) => {
    if (e.target.closest("input, select, textarea, button, [contenteditable]")) return;
    if (e.key === " ") { e.preventDefault(); togglePlay(); }
    if ((e.key === "Delete" || e.key === "Backspace") && selected && isNew(selected)) drop(selected);
  });

  // ---- marking a section by hand ----------------------------------------------------

  // Drag across the waveform (mouse or pen) to mark a section, even over one already there.
  // A plain click on a section selects it; anywhere else it moves the player there.
  wave.addEventListener("pointerdown", (e) => {
    if (e.button !== 0 || e.target.closest(".tl-handle")) return;
    const from = timeAt(e.clientX), startX = e.clientX;
    const onSection = !!e.target.closest(".tl-region");
    const click = () => { if (!onSection) { playing = null; showAllPlaying(); seek(from); } };
    if (!editable || e.pointerType === "touch") {
      wave.addEventListener("pointerup", click, { once: true });
      return;
    }
    wave.setPointerCapture(e.pointerId);
    let s = null;
    const move = (ev) => {
      const t = timeAt(ev.clientX);
      if (!s && Math.abs(ev.clientX - startX) > 4) { s = addSection(from, from); render(); }
      if (s) { s.start = Math.min(from, t); s.end = Math.max(from, t); place(); }
    };
    const up = () => {
      wave.removeEventListener("pointermove", move);
      if (!s) { click(); return; }
      if (s.end - s.start < MIN_LEN) { drop(s); toast(`Sections need to be at least ${MIN_LEN} seconds long.`, "error"); return; }
      render();
      select(s);
    };
    wave.addEventListener("pointermove", move);
    wave.addEventListener("pointerup", up, { once: true });
  });

  function dragEdge(e, s, side) {
    e.stopPropagation();
    e.preventDefault();
    const handle = e.target;
    handle.setPointerCapture(e.pointerId);
    select(s, false);
    const move = (ev) => {
      const t = timeAt(ev.clientX);
      if (side === "start") s.start = clamp(t, 0, s.end - MIN_LEN);
      else s.end = clamp(t, s.start + MIN_LEN, duration);
      place();
    };
    handle.addEventListener("pointermove", move);
    handle.addEventListener("pointerup", () => {
      handle.removeEventListener("pointermove", move);
      render();
    }, { once: true });
  }

  $("add")?.addEventListener("click", () => {
    const at = audio.currentTime || (scroll.scrollLeft + scroll.clientWidth / 3) / pps;
    const start = clamp(at, 0, duration - MIN_LEN);
    const s = addSection(start, Math.min(duration, start + 20));
    render();
    focusSection(s);
    select(s);
  });

  // ---- saving ---------------------------------------------------------------------

  $("discard").addEventListener("click", () => {
    for (let i = sections.length - 1; i >= 0; i--) {
      const s = sections[i];
      if (isNew(s)) sections.splice(i, 1);
      else if (s.manual) s.removed = false;
      else { s.value = s.saved; s.deleted = false; }
    }
    for (const s of falses) s.value = s.saved;
    selected = null;
    render();
  });

  $("save").addEventListener("click", async (e) => {
    const btn = e.currentTarget;
    btn.disabled = true;
    try {
      const res = await fetch(data.save, {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify(changes()),
      });
      const out = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(out.error || "Couldn't save that. Try again.");
      for (const s of sections) { s.saved = s.value; if (isNew(s)) s.dropped = true; }  // nothing left unsaved
      location.reload();
    } catch (err) {
      toast(err.message, "error");
      btn.disabled = false;
    }
  });

  // ---- start ------------------------------------------------------------------------

  fetch(data.peaks, { headers: { Accept: "application/json" } })
    .then((res) => (res.ok ? res.json() : Promise.reject(new Error(String(res.status)))))
    .then((got) => { peaks = Uint8Array.from(got.peaks); perSecond = got.per_second; root.classList.remove("tl-loading"); draw(); drawOverview(); })
    .catch(() => { root.classList.remove("tl-loading"); toast("Couldn't draw the waveform.", "error"); });

  root.classList.add("tl-ready");
  setZoom(0, 0, 0);
  render();
  showTime();
})();
