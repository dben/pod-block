// pod-block: episode selection, bulk actions, menus and toasts.
// Every page works without this file; it only makes things quicker.
(() => {
  "use strict";
  const $$ = (sel, root = document) => Array.from(root.querySelectorAll(sel));
  const LONG_PRESS_MS = 450;

  // ---- toasts ---------------------------------------------------------------

  function hideLater(el, ms = 5000) {
    setTimeout(() => {
      el.classList.add("leaving");
      setTimeout(() => el.remove(), 250);
    }, ms);
  }

  function toast(text, { kind = "ok", action, onAction } = {}) {
    const el = document.createElement("div");
    el.className = `toast toast-${kind}`;
    el.textContent = text;
    if (action) {
      const btn = document.createElement("button");
      btn.type = "button";
      btn.className = "toast-action";
      btn.textContent = action;
      btn.addEventListener("click", () => { el.remove(); onAction(); });
      el.append(btn);
    }
    document.getElementById("toasts").append(el);
    hideLater(el, action ? 8000 : 5000);
  }

  window.podblock = { toast };

  // ---- menus and confirmations --------------------------------------------

  document.addEventListener("click", (e) => {
    for (const menu of $$("details.menu[open]")) {
      if (!menu.contains(e.target) || e.target.closest(".menu-pop button")) menu.open = false;
    }
  });

  document.addEventListener("toggle", (e) => {
    if (e.target.matches?.("details.menu") && e.target.open) {
      for (const other of $$("details.menu[open]")) if (other !== e.target) other.open = false;
    }
  }, true);

  document.addEventListener("click", async (e) => {
    const btn = e.target.closest("[data-copy]");
    if (!btn) return;
    const field = document.querySelector(btn.dataset.copy);
    try {
      await navigator.clipboard.writeText(field.value);
    } catch {
      field.select();
      document.execCommand("copy");  // older browsers, and pages not served over HTTPS
    }
    toast("Copied.");
  });

  document.addEventListener("submit", (e) => {
    const msg = e.target.dataset.confirm;
    if (msg && !confirm(msg)) e.preventDefault();
  }, true);

  // ---- selection ------------------------------------------------------------

  let anchor = null;      // the last box ticked or unticked, where a range starts
  let shiftHeld = false;  // shift was down for the click that changed a box
  let pressTimer = null;
  let pressStart = null;
  let swallowClick = false;

  const boxes = () => $$("[data-select-list] .ep-check");
  const bar = () => document.getElementById("bulk");

  function update() {
    const all = boxes();
    const ticked = all.filter((b) => b.checked);
    for (const b of all) b.closest(".ep").classList.toggle("selected", b.checked);
    for (const el of $$("[data-selected-count]")) el.textContent = ticked.length.toLocaleString();
    const selectAll = document.querySelector("[data-select-all]");
    if (selectAll) {
      selectAll.checked = ticked.length > 0 && ticked.length === all.length;
      selectAll.indeterminate = ticked.length > 0 && ticked.length < all.length;
    }
    bar()?.classList.toggle("active", ticked.length > 0);
    document.body.classList.toggle("selecting", ticked.length > 0);
  }

  function setRange(from, to, value) {
    const all = boxes();
    const [i, j] = [all.indexOf(from), all.indexOf(to)].sort((a, b) => a - b);
    if (i < 0) { to.checked = value; return; }
    for (let k = i; k <= j; k++) all[k].checked = value;
  }

  function toggle(box, range) {
    const value = !box.checked;
    if (range && anchor && anchor !== box && anchor.isConnected) setRange(anchor, box, value);
    else box.checked = value;
    anchor = box;
    update();
  }

  function clearSelection() {
    for (const b of boxes()) b.checked = false;
    anchor = null;
    update();
  }

  document.addEventListener("pointerdown", (e) => { shiftHeld = e.shiftKey; }, true);
  document.addEventListener("keydown", (e) => {
    shiftHeld = e.shiftKey;
    if (e.key === "Escape" && document.body.classList.contains("selecting")) clearSelection();
  }, true);

  // Shift-clicking the artwork would also select the page's text.
  document.addEventListener("mousedown", (e) => {
    if (e.shiftKey && e.target.closest(".ep")) e.preventDefault();
  });

  document.addEventListener("change", (e) => {
    if (e.target.matches("[data-select-list] .ep-check")) {
      if (shiftHeld && anchor && anchor !== e.target && anchor.isConnected) {
        setRange(anchor, e.target, e.target.checked);
      }
      anchor = e.target;
      shiftHeld = false;
      update();
    } else if (e.target.matches("[data-select-all]")) {
      for (const b of boxes()) b.checked = e.target.checked;
      anchor = null;
      update();
    }
  });

  // While anything is selected, a click or tap anywhere on a row toggles it.
  document.addEventListener("click", (e) => {
    if (swallowClick) {
      swallowClick = false;
      e.preventDefault();
      e.stopPropagation();
      return;
    }
    if (e.target.closest("[data-clear]")) { clearSelection(); return; }
    const row = e.target.closest("[data-select-list] .ep");
    if (!row || !document.body.classList.contains("selecting")) return;
    if (e.target.closest("button, summary, details, label, input")) return;
    const link = e.target.closest("a");
    if (link && !link.matches(".ep-link")) return;
    e.preventDefault();  // while selecting, the title selects rather than opens
    toggle(row.querySelector(".ep-check"), e.shiftKey);
  }, true);

  // Press and hold a row to select it; once selecting, press and hold
  // another to select everything in between.
  document.addEventListener("pointerdown", (e) => {
    swallowClick = false;  // only the click that ends a long press is swallowed
    if (e.pointerType !== "touch") return;
    const row = e.target.closest("[data-select-list] .ep");
    if (!row || e.target.closest("a, button, summary, details, input, label")) return;
    pressStart = [e.clientX, e.clientY];
    pressTimer = setTimeout(() => {
      pressTimer = null;
      swallowClick = true;
      const box = row.querySelector(".ep-check");
      const selecting = document.body.classList.contains("selecting");
      if (selecting && anchor && anchor !== box && anchor.isConnected) {
        setRange(anchor, box, true);
        anchor = box;
        update();
      } else {
        toggle(box, false);
      }
      navigator.vibrate?.(10);
    }, LONG_PRESS_MS);
  });

  const cancelPress = () => { clearTimeout(pressTimer); pressTimer = null; };
  document.addEventListener("pointermove", (e) => {
    if (pressTimer && Math.hypot(e.clientX - pressStart[0], e.clientY - pressStart[1]) > 10) cancelPress();
  });
  document.addEventListener("pointerup", cancelPress);
  document.addEventListener("pointercancel", cancelPress);
  document.addEventListener("contextmenu", (e) => {
    if (e.target.closest("[data-select-list] .ep") && matchMedia("(hover: none)").matches) e.preventDefault();
  });

  // A row being processed refreshes itself; keep its tick across the swap.
  const ticked = new Set();
  document.addEventListener("htmx:beforeSwap", (e) => {
    const row = e.detail.target;
    if (row.matches?.(".ep") && row.querySelector(".ep-check")?.checked) ticked.add(row.dataset.id);
  });
  document.addEventListener("htmx:afterSettle", () => {
    for (const id of ticked) {
      const box = document.querySelector(`[data-select-list] .ep[data-id="${id}"] .ep-check`);
      if (box) box.checked = true;
    }
    ticked.clear();
    update();
  });

  // ---- play the original from a section ---------------------------------------

  document.addEventListener("click", (e) => {
    const btn = e.target.closest("[data-seek]");
    const player = document.querySelector("audio[data-original]");
    if (!btn || !player) return;
    const at = parseFloat(btn.dataset.seek);
    const go = () => { player.currentTime = at; player.play(); };
    if (player.readyState >= 1) go();
    else { player.addEventListener("loadedmetadata", go, { once: true }); player.load(); }
    player.scrollIntoView({ block: "nearest", behavior: "smooth" });
  });
  document.addEventListener("DOMContentLoaded", () => {
    update();
    for (const el of $$("[data-autohide]")) hideLater(el);
  });

  // ---- archive and unarchive without a page load -----------------------------

  const INVERSE = { archive: "unarchive", unarchive: "archive" };
  const COUNT_MOVES = { archive: ["active", "archived"], unarchive: ["archived", "active"] };

  async function post(url, data) {
    const res = await fetch(url, { method: "POST", body: data, headers: { Accept: "application/json" } });
    if (!res.ok) throw new Error(`${res.status}`);
    return res.json();
  }

  function bump(name, by) {
    for (const el of $$(`[data-count="${name}"]`)) {
      const n = parseInt(el.textContent.replace(/\D/g, ""), 10) || 0;
      el.textContent = Math.max(0, n + by).toLocaleString();
    }
  }

  function dropRows(ids) {
    for (const id of ids) {
      const row = document.querySelector(`[data-select-list] .ep[data-id="${id}"]`);
      if (!row) continue;
      row.classList.add("leaving");
      setTimeout(() => { row.remove(); update(); }, 200);
    }
    setTimeout(() => {
      if (!document.querySelector("[data-select-list] .ep")) location.reload();
    }, 260);
  }

  document.addEventListener("submit", async (e) => {
    const form = e.target;
    const action = e.submitter?.value;
    if (e.defaultPrevented || !form.matches("[data-bulk]") || !INVERSE[action]) return;
    e.preventDefault();
    const data = new FormData(form);
    data.set("action", action);
    if (!data.getAll("ids").length) return;
    form.closest("details.menu")?.removeAttribute("open");
    let out;
    try {
      out = await post(form.getAttribute("action"), data);
    } catch {
      toast("That didn't work. Try again.", { kind: "error" });
      return;
    }
    const n = out.changed.length;
    const [from, to] = COUNT_MOVES[action];
    bump(from, -n);
    bump(to, n);
    anchor = null;
    dropRows(out.changed);
    const verb = action === "archive" ? "Archived" : "Unarchived";
    toast(`${verb} ${n.toLocaleString()} ${n === 1 ? "episode" : "episodes"}.`, {
      action: n ? "Undo" : null,
      onAction: async () => {
        const undo = new FormData();
        undo.set("action", INVERSE[action]);
        for (const id of out.changed) undo.append("ids", id);
        try { await post(form.getAttribute("action"), undo); } catch { toast("Couldn't undo that.", { kind: "error" }); return; }
        location.reload();
      },
    });
  });
})();
