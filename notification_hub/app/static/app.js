"use strict";
const $ = (s) => document.querySelector(s);
const esc = (t) => String(t ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const S = { data: null, error: null, ai: null, open: new Set() };

const STATUS = {
  sent: "Envoyée", held: "En attente", replaced: "Remplacée", cancelled: "Annulée",
  handled: "Traitée", ignored: "Ignorée", expired: "Expirée", failed: "Échec",
};
const TARGETS = { present: "présents", home: "présents", nearest: "plus proches", all: "tous" };

async function api(path, opts = {}) {
  const res = await fetch(path, { ...opts, headers: { "Content-Type": "application/json" } });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `Erreur ${res.status}`);
  return body;
}

function toast(text, isError = false) {
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " error" : "");
  el.textContent = text;
  $("#toast-zone").appendChild(el);
  setTimeout(() => el.remove(), isError ? 6000 : 3500);
}

function fmtDistance(m) {
  if (m == null) return "position inconnue";
  if (m < 1000) return `${Math.round(m)} m`;
  return `${(m / 1000).toFixed(1).replace(".", ",")} km`;
}

function fmtTime(ts) {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const diff = (Date.now() - d.getTime()) / 1000;
  if (diff < 60) return "à l'instant";
  if (diff < 3600) return `il y a ${Math.floor(diff / 60)} min`;
  const same = d.toDateString() === new Date().toDateString();
  const hm = d.toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" });
  return same ? `aujourd'hui à ${hm}` : `${d.toLocaleDateString("fr-FR", { day: "numeric", month: "short" })} à ${hm}`;
}

function targetLabel(t) {
  return (t || []).map((x) => TARGETS[String(x).toLowerCase()] || String(x).replace(/^person\./, "")).join(", ");
}

function renderPeople() {
  const d = S.data;
  const box = $("#people");
  if (!d) { box.innerHTML = '<div class="card empty">Chargement…</div>'; return; }
  const house = `<div class="card house">
      <div class="avatar ${d.occupied ? "home" : ""}">${d.occupied ? "✓" : "–"}</div>
      <div><b>${d.occupied ? "Maison occupée" : "Maison vide"}</b>
      <div class="person-sub">Seuil de proximité : ${Math.round(d.threshold)} m</div></div></div>`;
  if (!d.people.length) {
    box.innerHTML = house + '<div class="card empty">Aucune personne trouvée.<br>Ajoute-en dans Paramètres → Personnes.</div>';
    return;
  }
  box.innerHTML = house + d.people.map((p) => {
    const sub = p.home ? "À la maison" : `Absent · ${fmtDistance(p.distance)}`;
    const devices = p.devices.length
      ? p.devices.map((x) => `<span class="chip">📱 ${esc(x.replace(/^mobile_app_/, ""))}</span>`).join("")
      : '<span class="chip warn">Aucun appareil mobile trouvé</span>';
    return `<div class="card person">
      <div class="avatar ${p.home ? "home" : ""}">${esc((p.name || "?").trim().charAt(0).toUpperCase())}</div>
      <div class="person-body">
        <div class="person-name">${esc(p.name)}</div>
        <div class="person-sub">${esc(sub)}</div>
        <div class="chips">${p.nearest && !p.home ? '<span class="chip accent">Plus proche</span>' : ""}${devices}</div>
      </div></div>`;
  }).join("");
}

function fmtFull(ts) {
  return ts ? new Date(ts * 1000).toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit", second: "2-digit" }) : "";
}

function condLabel(c) {
  if ("home_occupied" in c) return c.home_occupied ? "maison occupée" : "maison vide";
  const key = ["state", "not_state", "above", "below"].find((k) => k in c);
  return `${c.entity_id}${c.attribute ? "." + c.attribute : ""} ${key ? key.replace("_", " ") + " " + [].concat(c[key]).join(" ou ") : ""}`;
}

function detailsHtml(n) {
  const rows = [];
  if (n.recipients.length) rows.push(["Destinataires", n.recipients.join(", ")]);
  if (n.devices.length) rows.push(["Appareils", n.devices.join(", ")]);
  rows.push(["Cible", targetLabel(n.target)]);
  if (n.expires) rows.push(["Suivie jusqu'à", fmtFull(n.expires)]);
  if (n.cancel_when.length) rows.push(["Annulée si", n.cancel_when.map(condLabel).join(" ; ")]);
  if (Object.keys(n.data || {}).length) rows.push(["Données", JSON.stringify(n.data)]);
  const hist = (n.history || []).map((h) =>
    `<li><span class="h-time">${esc(fmtFull(h.t))}</span><span class="badge ${esc(h.status)}">${esc(STATUS[h.status] || h.status)}</span>${h.note ? `<span class="h-note">${esc(h.note)}</span>` : ""}</li>`).join("");
  return `<div class="details">
    <dl>${rows.map(([k, v]) => `<dt>${esc(k)}</dt><dd>${esc(v)}</dd>`).join("")}</dl>
    ${hist ? `<ul class="timeline">${hist}</ul>` : ""}
  </div>`;
}

function renderNotifs() {
  const d = S.data;
  const box = $("#notifs");
  if (!d) { box.innerHTML = '<div class="card empty">Chargement…</div>'; return; }
  if (!d.notifications.length) {
    box.innerHTML = `<div class="card empty">Aucune notification pour le moment.<br>
      Utilise « Envoyer un test », ou l'événement <b>notification_hub_send</b> depuis une automatisation
      (menu ⋮ → Exemples).</div>`;
    return;
  }
  box.innerHTML = d.notifications.map((n) => {
    const meta = [
      fmtTime(n.sent_at || n.created),
      n.recipients.length ? `→ ${n.recipients.join(", ")}` : `cible : ${targetLabel(n.target)}`,
      `tag : ${n.tag}`,
    ];
    if (n.hold) meta.push("attend le retour");
    if (n.cancel_when.length) meta.push(`${n.cancel_when.length} condition(s) d'annulation`);
    const buttons = n.active && n.actions.length
      ? `<div class="card-foot">${n.actions.map((a) => a.uri
        ? `<button class="btn-ghost" data-uri="${esc(a.uri)}" type="button">${esc(a.title)} ↗</button>`
        : `<button class="btn-ghost" data-act="${esc(n.id)}" data-key="${esc(a.key)}" type="button">${esc(a.title)}</button>`).join("")}</div>`
      : "";
    if (n.actions.length && !n.active) meta.push("boutons : " + n.actions.map((a) => a.title).join(", "));
    return `<div class="card">
      <div class="notif-head">
        <div class="notif-title">${esc(n.title)}</div>
        <span class="badge ${esc(n.status)}">${esc(STATUS[n.status] || n.status)}</span>
        <div class="actions-right">
          ${n.active ? `<button class="btn-ghost" data-cancel="${esc(n.id)}" type="button">Annuler</button>` : ""}
          <button class="icon-btn danger" data-delete="${esc(n.id)}" type="button" title="Supprimer de la liste" aria-label="Supprimer"><svg viewBox="0 0 24 24" width="18" height="18" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M3 6h18M8 6V4h8v2M6 6l1 14h10l1-14M10 10v6M14 10v6"/></svg></button>
        </div>
      </div>
      <div class="notif-msg">${esc(n.message)}</div>
      ${n.note ? `<div class="note">${esc(n.note)}</div>` : ""}
      <div class="notif-meta">${meta.map((m) => `<span>${esc(m)}</span>`).join("")}</div>
      ${buttons}
      <div class="card-foot"><button class="btn-ghost" data-detail="${esc(n.id)}" type="button">${S.open.has(n.id) ? "▲ Masquer les détails" : "▼ Détails"}</button></div>
      ${S.open.has(n.id) ? detailsHtml(n) : ""}
      ${S.ai ? `<div class="card-foot"><button class="btn-ghost" data-auto="${esc(n.id)}" type="button">✨ Automatiser</button></div>` : ""}
    </div>`;
  }).join("");
}

function renderHeader() {
  const d = S.data;
  if (S.error) $("#status-line").textContent = "Connexion impossible";
  else if (d) $("#status-line").textContent = `${d.people.length} personne(s) · maison ${d.occupied ? "occupée" : "vide"}`;
  const banner = $("#banner");
  if (S.error) { banner.textContent = S.error; banner.classList.add("error"); banner.classList.remove("hidden"); }
  else if (d && !d.connected) { banner.textContent = "Connexion à Home Assistant en cours…"; banner.classList.remove("error", "hidden"); }
  else banner.classList.add("hidden");
}

async function refresh() {
  try {
    S.data = await api("api/state");
    S.error = null;
  } catch (err) {
    S.error = err.message;
  }
  renderHeader(); renderPeople(); renderNotifs();
}

$("#notifs").addEventListener("click", async (e) => {
  const auto = e.target.closest("[data-auto]");
  if (auto) {
    const n = (S.data?.notifications || []).find((x) => x.id === auto.dataset.auto);
    if (n) openAI(promptFor(n));
    return;
  }
  const det = e.target.closest("[data-detail]");
  if (det) {
    const id = det.dataset.detail;
    if (S.open.has(id)) S.open.delete(id); else S.open.add(id);
    renderNotifs();
    return;
  }
  const uri = e.target.closest("[data-uri]");
  if (uri) {
    window.open(uri.dataset.uri, "_blank", "noopener");
    return;
  }
  const act = e.target.closest("[data-act]");
  if (act) {
    act.disabled = true;
    try {
      await api(`api/notification/${act.dataset.act}/action/${encodeURIComponent(act.dataset.key)}`, { method: "POST", body: "{}" });
      toast("Action exécutée");
    } catch (err) { toast(err.message, true); }
    refresh();
    return;
  }
  const del = e.target.closest("[data-delete]");
  if (del) {
    del.disabled = true;
    try {
      await api(`api/notification/${del.dataset.delete}`, { method: "DELETE" });
      toast("Notification supprimée");
    } catch (err) { toast(err.message, true); }
    refresh();
    return;
  }
  const btn = e.target.closest("[data-cancel]");
  if (!btn) return;
  btn.disabled = true;
  try {
    await api(`api/cancel/${btn.dataset.cancel}`, { method: "POST" });
    toast("Notification annulée");
  } catch (err) { toast(err.message, true); }
  refresh();
});

/* Effacement de l'historique */
async function clearHistory(scope) {
  try {
    const r = await api("api/clear", { method: "POST", body: JSON.stringify({ scope }) });
    toast(r.removed ? `${r.removed} notification(s) supprimée(s)` : "Rien à supprimer");
  } catch (err) { toast(err.message, true); }
  refresh();
}

function confirmClearAll() {
  const active = (S.data?.notifications || []).filter((n) => n.active).length;
  openDialog(`<h3>Tout effacer ?</h3>
    <div class="muted">Toutes les notifications seront supprimées de la liste${active ? `, et les ${active} notification(s) active(s) seront retirées des téléphones` : ""}. Cette action est définitive.</div>
    <div class="actions"><button class="btn-ghost" id="c-no" type="button">Annuler</button><button class="btn-primary" id="c-yes" type="button">Tout effacer</button></div>`);
  $("#c-no").onclick = closeDialog;
  $("#c-yes").onclick = () => { closeDialog(); clearHistory("all"); };
}

/* Automations IA, affiché dans un panneau latéral (si l'application est installée et démarrée) */
function parentHass() {
  try { return window.parent.document.querySelector("home-assistant").hass || null; } catch (_) { return null; }
}

async function detectAI() {
  const previous = S.ai;
  let found = null;
  try {
    const hass = parentHass();
    if (hass) {
      const list = await hass.callWS({ type: "supervisor/api", endpoint: "/addons", method: "get" });
      const a = (list.addons || []).find((x) => /ia_automations$/.test(x.slug) && x.state === "started");
      if (a) {
        const info = await hass.callWS({ type: "supervisor/api", endpoint: `/addons/${a.slug}/info`, method: "get" });
        if (info.ingress_url) found = { slug: a.slug, url: info.ingress_url, name: info.name };
      }
    }
  } catch (_) { found = previous; } // erreur passagère : on garde le dernier état connu
  const changed = JSON.stringify(found) !== JSON.stringify(previous);
  S.ai = found;
  $("#ai-create").classList.toggle("hidden", !S.ai);
  if (changed) renderNotifs();
}

function promptFor(n) {
  const dest = n.target && n.target.length ? `, destinataires : ${targetLabel(n.target)}` : "";
  const hold = n.hold ? ", gardée en attente jusqu'au retour d'un destinataire" : "";
  return `Crée une automatisation qui envoie la notification « ${n.title} » (message : « ${n.message} »${dest}${hold}, tag : ${n.tag}) quand `;
}

async function openAI(prompt = "") {
  if (!S.ai) return;
  try {
    const r = await parentHass().callWS({ type: "supervisor/api", endpoint: "/ingress/session", method: "post" });
    if (r && r.session) document.cookie = `ingress_session=${r.session};path=/api/hassio_ingress/;SameSite=Strict${location.protocol === "https:" ? ";Secure" : ""}`;
  } catch (_) { /* la session existe peut-être déjà */ }
  const q = new URLSearchParams({ embed: "1", context: "notification_hub" });
  if (prompt) q.set("prompt", prompt);
  $("#drawer-frame").src = `${S.ai.url}?${q}`;
  $("#drawer").classList.remove("hidden");
  document.querySelector(".fab-container").classList.add("shifted");
}

function closeAI() {
  $("#drawer").classList.add("hidden");
  $("#drawer-frame").src = "about:blank";
  document.querySelector(".fab-container").classList.remove("shifted");
}

$("#ai-create").addEventListener("click", () => openAI("Crée une automatisation qui m'envoie une notification quand "));
$("#drawer-close").addEventListener("click", closeAI);
window.addEventListener("message", (e) => {
  if (e.origin === location.origin && e.data && e.data.type === "ia-automation-created") toast(`Automatisation « ${e.data.alias} » créée`);
});

/* Dialogues */
function openDialog(html) {
  const d = $("#dialog");
  d.innerHTML = `<div class="dialog">${html}</div>`;
  d.classList.remove("hidden");
  d.onclick = (e) => { if (e.target === d) closeDialog(); };
}
function closeDialog() { $("#dialog").classList.add("hidden"); $("#dialog").innerHTML = ""; }

function openTest() {
  const people = (S.data?.people || []).map((p) => `<option value="${esc(p.id)}">${esc(p.name)}</option>`).join("");
  openDialog(`<h3>Envoyer une notification de test</h3>
    <div class="muted">Elle suit les mêmes règles qu'une vraie notification.</div>
    <label>Titre<input id="t-title" type="text" value="Test"></label>
    <label>Message<textarea id="t-msg" rows="2">Notification de test du Centre de notifications</textarea></label>
    <label>Destinataires
      <select id="t-target">
        <option value="present">Personnes à la maison</option>
        <option value="nearest">Personnes les plus proches de la maison</option>
        <option value="all">Toutes les personnes</option>
        ${people}
      </select></label>
    <label class="check"><input id="t-hold" type="checkbox"> Garder en attente jusqu'au retour d'un destinataire</label>
    <div class="actions"><button class="btn-ghost" id="t-cancel" type="button">Fermer</button><button class="btn-primary" id="t-send" type="button">Envoyer</button></div>`);
  $("#t-cancel").onclick = closeDialog;
  $("#t-send").onclick = async () => {
    const btn = $("#t-send");
    btn.disabled = true;
    try {
      const r = await api("api/test", { method: "POST", body: JSON.stringify({
        title: $("#t-title").value, message: $("#t-msg").value, target: $("#t-target").value, hold_until_home: $("#t-hold").checked,
      }) });
      closeDialog();
      toast(r.status === "sent" ? `Envoyée à ${r.recipients.join(", ")}` : `${STATUS[r.status] || r.status}${r.note ? " : " + r.note : ""}`, r.status === "failed");
    } catch (err) { toast(err.message, true); btn.disabled = false; }
    refresh();
  };
}

const EXAMPLES = {
  "Lessive terminée": `# Machine à laver : attendre le retour d'un occupant
actions:
  - event: notification_hub_send
    event_data:
      title: Lessive terminée
      message: Pense à étendre le linge.
      target: present
      hold_until_home: true
      tag: lave_linge`,
  "Lumières, maison vide": `# Maison vide avec des lumières allumées : prévenir le plus proche
actions:
  - event: notification_hub_send
    event_data:
      title: Maison vide
      message: Des lumières sont encore allumées.
      target: nearest
      tag: lumieres_allumees
      actions:
        - title: Tout éteindre
          do:
            - action: light.turn_off
              target:
                entity_id: all
      cancel_when:
        - home_occupied: true`,
  "Aspirateur démarre": `# L'aspirateur démarre : bouton pour le renvoyer à sa base
actions:
  - event: notification_hub_send
    event_data:
      title: Aspirateur en marche
      message: Le nettoyage a commencé.
      target: present
      tag: aspirateur
      actions:
        - title: Retour à la base
          do:
            - action: vacuum.return_to_base
              target:
                entity_id: vacuum.mon_aspirateur`,
  "Aspirateur terminé": `# Même tag : remplace la notification « en marche »
actions:
  - event: notification_hub_send
    event_data:
      title: Nettoyage terminé
      message: "Surface nettoyée : {{ states('sensor.aspirateur_surface') }} m²"
      target: present
      hold_until_home: true
      tag: aspirateur
      data:
        image: /local/carte_aspirateur.png`,
};

function openExamples(active = Object.keys(EXAMPLES)[0]) {
  const tabs = Object.keys(EXAMPLES).map((k) => `<button class="tab ${k === active ? "active" : ""}" data-ex="${esc(k)}" type="button">${esc(k)}</button>`).join("");
  openDialog(`<h3>Exemples d'automatisations</h3>
    <div class="muted">Colle l'action dans une automatisation (éditeur YAML). Voir la documentation pour tous les champs.</div>
    <div class="tabs">${tabs}</div>
    <pre class="code">${esc(EXAMPLES[active])}</pre>
    <div class="actions"><button class="btn-ghost" id="ex-close" type="button">Fermer</button><button class="btn-primary" id="ex-copy" type="button">Copier</button></div>`);
  $("#ex-close").onclick = closeDialog;
  $("#ex-copy").onclick = async () => {
    try { await navigator.clipboard.writeText(EXAMPLES[active]); toast("Exemple copié"); }
    catch (_) { toast("Copie impossible : sélectionne le texte à la main", true); }
  };
  document.querySelectorAll("[data-ex]").forEach((b) => (b.onclick = () => openExamples(b.dataset.ex)));
}

/* Menu ⋮ de l'en-tête */
function showMenu(anchor) {
  const m = $("#menu");
  if (!m.classList.contains("hidden")) { m.classList.add("hidden"); return; }
  m.innerHTML = '<button data-act="test" type="button">Envoyer un test</button>'
    + (S.ai ? '<button data-act="ai" type="button">Créer l\'automatisation (IA)</button>' : "")
    + '<button data-act="examples" type="button">Exemples d\'automatisations</button>'
    + '<button data-act="clear-finished" type="button">Effacer l\'historique</button>'
    + '<button data-act="clear-all" type="button">Tout effacer…</button>'
    + '<button data-act="refresh" type="button">Actualiser</button>';
  const r = anchor.getBoundingClientRect();
  m.style.top = r.bottom + 4 + "px";
  m.style.right = window.innerWidth - r.right + "px";
  m.classList.remove("hidden");
  m.onclick = (e) => {
    const act = e.target.dataset.act;
    m.classList.add("hidden");
    if (act === "test") openTest();
    else if (act === "examples") openExamples();
    else if (act === "ai") openAI("Crée une automatisation qui m'envoie une notification quand ");
    else if (act === "clear-finished") clearHistory("finished");
    else if (act === "clear-all") confirmClearAll();
    else if (act === "refresh") refresh();
  };
}
$("#header-menu").addEventListener("click", (e) => { e.stopPropagation(); showMenu(e.currentTarget); });
document.addEventListener("click", () => $("#menu").classList.add("hidden"));
$("#new-test").addEventListener("click", openTest);

/* Couleurs : on recopie le thème de Home Assistant depuis la page parente (même origine via ingress). */
const THEME_VARS = [
  "--primary-background-color", "--card-background-color", "--secondary-background-color", "--primary-text-color",
  "--secondary-text-color", "--divider-color", "--primary-color", "--text-primary-color", "--error-color",
  "--success-color", "--warning-color", "--ha-card-border-radius", "--ha-font-family-body",
];
function syncTheme() {
  try {
    const cs = getComputedStyle(window.parent.document.documentElement);
    THEME_VARS.forEach((v) => {
      const val = cs.getPropertyValue(v).trim();
      if (val) document.documentElement.style.setProperty(v, val);
      else document.documentElement.style.removeProperty(v);
    });
  } catch (_) { /* page parente inaccessible : couleurs par défaut du thème sombre de HA */ }
}
syncTheme();
try {
  new MutationObserver(syncTheme).observe(window.parent.document.documentElement, { attributes: true, attributeFilter: ["style", "class"] });
} catch (_) { /* idem */ }

renderPeople(); renderNotifs();
refresh();
detectAI();
setInterval(refresh, 5000);
setInterval(detectAI, 60000);
