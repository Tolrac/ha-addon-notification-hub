"""Centre de notifications pour Home Assistant.

Les automatisations envoient un événement `notification_hub_send` ; le centre décide à qui (présents,
plus proches, personne précise), quand (mise en attente jusqu'au retour) et pour combien de temps
(remplacement par tag, annulation croisée, conditions d'annulation) la notification est envoyée.
"""
import asyncio
import json
import logging
import math
import os
import re
import time
import unicodedata
import uuid
from pathlib import Path

from aiohttp import ClientSession, WSMsgType, web

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("notification_hub")

BASE = Path(__file__).parent
OPTIONS_FILE = Path(os.environ.get("OPTIONS_FILE", "/data/options.json"))
OPTIONS = json.loads(OPTIONS_FILE.read_text()) if OPTIONS_FILE.exists() else {}
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
STATE_FILE = DATA_DIR / "notifications.json"

TOKEN = os.environ.get("SUPERVISOR_TOKEN", "")
WS_URL = os.environ.get("HA_WS_URL", "ws://supervisor/core/websocket")
ALLOWED_PEERS = {"172.30.32.2", "127.0.0.1", "::1"}

THRESHOLD = float(OPTIONS.get("proximity_threshold", 200))
EXTRA_ZONES = [z.strip() for z in OPTIONS.get("extra_home_zones") or [] if z and z.strip()]
OCCUPANCY_ENTITY = (OPTIONS.get("occupancy_entity") or "").strip()
TTL_SECONDS = float(OPTIONS.get("ttl_hours", 24)) * 3600
OVERRIDES = {
    (o.get("person") or "").strip(): [s.strip().removeprefix("notify.") for s in (o.get("notify") or "").split(",") if s.strip()]
    for o in OPTIONS.get("people_overrides") or []
}
HISTORY = 100

EV_SEND = "notification_hub_send"
EV_CLEAR = "notification_hub_clear"
EV_ACTION = "mobile_app_notification_action"

ACTIVE = ("held", "sent")


# --------------------------------------------------------------------------- utilitaires
def slugify(text):
    text = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", "_", text).strip("_")


def haversine(lat1, lon1, lat2, lon2):
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def as_list(value):
    if value is None or value == "":
        return []
    return value if isinstance(value, list) else [value]


def apply_compat(d):
    """Accepte aussi les champs de l'ancien « Notifier » (action, callback, until, image_url, click_url, ...)."""
    d = dict(d)
    act = str(d.pop("action", "") or "")
    if act and "target" not in d:
        if act == "send_to_present":
            d["target"] = "present"
            d.setdefault("fallback", "nearest")
        elif act == "send_to_nearest":
            d["target"] = "nearest"
        elif act == "send_when_present":
            d["target"] = "present"
            d["hold_until_home"] = True
        elif act.startswith("send_to_") and len(act) > 8:
            d["target"] = act[8:]
    callback = d.pop("callback", None)
    if callback and "actions" not in d:
        d["actions"] = [{"title": c.get("title"), "event": c.get("event")} for c in as_list(callback) if isinstance(c, dict)]
    until = d.pop("until", None)
    if until and "cancel_when" not in d:
        d["cancel_when"] = [
            {"entity_id": u.get("entity_id"), "state": u.get("new_state")} for u in as_list(until) if isinstance(u, dict) and u.get("entity_id")
        ]
    extra = dict(d.get("data") if isinstance(d.get("data"), dict) else {})
    if d.get("image_url"):
        extra.setdefault("image", d["image_url"])
    if d.get("click_url"):
        extra.setdefault("clickAction", d["click_url"])
        extra.setdefault("url", d["click_url"])
    if d.get("color"):
        extra.setdefault("color", d["color"])
    if d.get("icon"):
        extra.setdefault("notification_icon", d["icon"])
    if d.get("persistent") in (True, "true", "True"):
        extra.setdefault("persistent", "true")
        extra.setdefault("sticky", "true")
    if isinstance(d.get("timeout"), (int, float)) and d["timeout"] > 0:
        extra.setdefault("timeout", int(d["timeout"]))
    d["data"] = extra
    return d


class HAError(Exception):
    pass


# --------------------------------------------------------------------------- moteur
class Hub:
    def __init__(self, session):
        self.session = session
        self.ws = None
        self._id = 0
        self._pending = {}
        self._tasks = set()
        self.states = {}
        self.reg = {"t": 0.0, "tracker_notify": {}, "services": set(), "person_trackers": {}}
        self.items = []
        self.lock = asyncio.Lock()
        self._load()

    # ---- persistance
    def _load(self):
        try:
            self.items = json.loads(STATE_FILE.read_text())
        except Exception:
            self.items = []

    def _save(self):
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            self.items = self.items[-HISTORY:]
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.items, ensure_ascii=False))
            tmp.replace(STATE_FILE)
        except Exception as err:
            log.warning("Sauvegarde impossible : %s", err)

    # ---- connexion Home Assistant
    @property
    def connected(self):
        return self.ws is not None and not self.ws.closed

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._tasks.add(task)

        def done(t):
            self._tasks.discard(t)
            if not t.cancelled() and t.exception():
                log.error("Erreur de traitement : %r", t.exception())

        task.add_done_callback(done)

    async def call(self, type_, timeout=30, **payload):
        if not self.connected:
            raise HAError("Home Assistant n'est pas connecté")
        self._id += 1
        i = self._id
        fut = asyncio.get_running_loop().create_future()
        self._pending[i] = fut
        try:
            await self.ws.send_json({"id": i, "type": type_, **payload})
            return await asyncio.wait_for(fut, timeout)
        finally:
            self._pending.pop(i, None)

    async def call_service(self, domain, service, data=None, target=None):
        payload = {"domain": domain, "service": service, "service_data": data or {}}
        if target:
            payload["target"] = target
        return await self.call("call_service", **payload)

    async def run(self):
        while True:
            try:
                async with self.session.ws_connect(WS_URL, max_msg_size=0, heartbeat=30) as ws:
                    await ws.receive_json()
                    await ws.send_json({"type": "auth", "access_token": TOKEN})
                    if (await ws.receive_json()).get("type") != "auth_ok":
                        raise HAError("Authentification Home Assistant refusée")
                    self.ws = ws
                    reader = asyncio.create_task(self._read(ws))
                    try:
                        await self._bootstrap()
                        log.info("Connecté à Home Assistant (%d entités)", len(self.states))
                        await reader
                    finally:
                        reader.cancel()
            except asyncio.CancelledError:
                raise
            except Exception as err:
                log.warning("Connexion Home Assistant perdue : %s", err)
            finally:
                self.ws = None
                for fut in self._pending.values():
                    if not fut.done():
                        fut.set_exception(HAError("Connexion perdue"))
            await asyncio.sleep(5)

    async def _read(self, ws):
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            d = json.loads(msg.data)
            kind = d.get("type")
            if kind == "result":
                fut = self._pending.get(d.get("id"))
                if fut and not fut.done():
                    if d.get("success"):
                        fut.set_result(d.get("result"))
                    else:
                        fut.set_exception(HAError((d.get("error") or {}).get("message", "erreur")))
            elif kind == "event":
                self._on_event(d["event"])

    async def _bootstrap(self):
        self.states = {s["entity_id"]: s for s in await self.call("get_states")}
        for et in ("state_changed", EV_SEND, EV_CLEAR, EV_ACTION):
            await self.call("subscribe_events", event_type=et)
        await self.refresh_registry(force=True)
        self._spawn(self.release_held())
        self._spawn(self.check_cancellations())

    def _on_event(self, ev):
        et, data = ev.get("event_type"), ev.get("data") or {}
        if et == "state_changed":
            eid, old, new = data.get("entity_id"), data.get("old_state"), data.get("new_state")
            if new is None:
                self.states.pop(eid, None)
            else:
                self.states[eid] = new
            self._spawn(self.on_state_changed(eid, old, new))
        elif et == EV_SEND:
            self._spawn(self.handle_send(data))
        elif et == EV_CLEAR:
            self._spawn(self.handle_clear(data))
        elif et == EV_ACTION:
            self._spawn(self.handle_action(data))

    # ---- registres : appareils mobiles
    async def refresh_registry(self, force=False):
        if not force and time.time() - self.reg["t"] < 60:
            return
        devices = {d["id"]: d for d in await self.call("config/device_registry/list")}
        entities = await self.call("config/entity_registry/list")
        services = await self.call("get_services")
        tracker_notify = {}
        for e in entities:
            if e["entity_id"].startswith("device_tracker.") and e.get("platform") == "mobile_app":
                dev = devices.get(e.get("device_id")) or {}
                name = dev.get("name_by_user") or dev.get("name") or ""
                if name:
                    tracker_notify[e["entity_id"]] = "mobile_app_" + slugify(name)
        # Configuration complète des personnes : l'attribut d'état n'indique que les appareils au statut connu.
        try:
            persons = await self.call("person/list")
        except HAError:
            persons = None
        person_trackers = {p["id"]: p.get("device_trackers") or [] for p in ((persons or {}).get("storage") or []) if "id" in p}
        self.reg = {
            "t": time.time(),
            "tracker_notify": tracker_notify,
            "services": set((services or {}).get("notify", {})),
            "person_trackers": person_trackers,
        }

    async def devices_of(self, person_id):
        try:
            await self.refresh_registry()
        except HAError:
            pass
        services = self.reg["services"]
        if person_id in OVERRIDES:
            return [s for s in OVERRIDES[person_id] if s in services]
        st = self.states.get(person_id) or {}
        out = []
        attrs = st.get("attributes") or {}
        trackers = list(dict.fromkeys([*(self.reg.get("person_trackers", {}).get(attrs.get("id")) or []), *(attrs.get("device_trackers") or [])]))
        for tracker in trackers:
            svc = self.reg["tracker_notify"].get(tracker) or "mobile_app_" + tracker.split(".", 1)[1]
            if svc in services and svc not in out:
                out.append(svc)
        return out

    # ---- présence et distance
    def _home_names(self):
        names = {"home"}
        for z in ["zone.home", *EXTRA_ZONES]:
            st = self.states.get(z)
            names.add(z.split(".", 1)[-1].lower())
            if st:
                fn = (st.get("attributes") or {}).get("friendly_name")
                if fn:
                    names.add(fn.lower())
        return names

    def is_home(self, st):
        return bool(st) and str(st.get("state", "")).lower() in self._home_names()

    def distance(self, st):
        if self.is_home(st):
            return 0.0
        a = st.get("attributes") or {}
        home = (self.states.get("zone.home") or {}).get("attributes") or {}
        try:
            return haversine(float(a["latitude"]), float(a["longitude"]), float(home["latitude"]), float(home["longitude"]))
        except (KeyError, TypeError, ValueError):
            return None

    def people(self):
        out = []
        for eid, st in self.states.items():
            if not eid.startswith("person."):
                continue
            out.append(
                {
                    "id": eid,
                    "name": (st.get("attributes") or {}).get("friendly_name") or eid,
                    "state": st.get("state"),
                    "home": self.is_home(st),
                    "distance": self.distance(st),
                }
            )
        return sorted(out, key=lambda p: p["name"].lower())

    def occupied(self):
        if OCCUPANCY_ENTITY and OCCUPANCY_ENTITY in self.states:
            return self.states[OCCUPANCY_ENTITY].get("state") == "on"
        return any(p["home"] for p in self.people())

    def nearest_ids(self, people):
        cand = [p for p in people if p["distance"] is not None]
        if not cand:
            return []
        m = min(p["distance"] for p in cand)
        return [p["id"] for p in cand if p["distance"] <= m + THRESHOLD]

    def resolve(self, target, hold=False):
        people = {p["id"]: p for p in self.people()}
        out, unknown = [], []

        def add(pid):
            if pid not in out:
                out.append(pid)

        for raw in as_list(target) or ["present"]:
            t = str(raw).strip().lower()
            if t in ("present", "home"):
                for p in people.values():
                    if p["home"]:
                        add(p["id"])
            elif t == "all":
                for pid in people:
                    add(pid)
            elif t == "nearest":
                for pid in self.nearest_ids(list(people.values())):
                    add(pid)
            else:
                pid = t if t.startswith("person.") else "person." + t
                if pid not in people:  # nom affiché (« David Gillet » → david_gillet)
                    pid = next((q["id"] for q in people.values() if slugify(q["name"]) == slugify(t)), pid)
                if pid in people:
                    add(pid)
                else:
                    unknown.append(str(raw))
        if hold:
            out = [pid for pid in out if people[pid]["home"]]
        return out, unknown

    # ---- conditions d'annulation
    def cond_true(self, c):
        if "home_occupied" in c:
            return self.occupied() == bool(c["home_occupied"])
        st = self.states.get(c.get("entity_id"))
        if not st:
            return False
        val = (st.get("attributes") or {}).get(c["attribute"]) if "attribute" in c else st.get("state")
        if "state" in c:
            return str(val) in [str(a) for a in as_list(c["state"])]
        if "not_state" in c:
            return str(val) not in [str(a) for a in as_list(c["not_state"])]
        try:
            num = float(val)
        except (TypeError, ValueError):
            return False
        if "above" in c and num > float(c["above"]):
            return True
        return "below" in c and num < float(c["below"])

    def watched(self, item):
        ents = {c["entity_id"] for c in item["cancel_when"] if "entity_id" in c}
        return ents, any("home_occupied" in c for c in item["cancel_when"])

    async def on_state_changed(self, eid, old, new):
        if eid.startswith("person.") or eid == OCCUPANCY_ENTITY:
            if self.is_home(old) != self.is_home(new) or eid == OCCUPANCY_ENTITY:
                await self.release_held()
                await self.check_cancellations()
                return
        if any(eid in self.watched(i)[0] for i in self.items if i["active"] and i["cancel_when"]):
            await self.check_cancellations()

    async def check_cancellations(self):
        async with self.lock:
            for item in self.items:
                if item["active"] and item["cancel_when"] and any(self.cond_true(c) for c in item["cancel_when"]):
                    await self._cancel(item, "Condition d'annulation remplie", "cancelled")

    # ---- envoi
    def _build(self, data):
        data = apply_compat(data)
        iid = uuid.uuid4().hex[:10]
        actions = []
        for a in as_list(data.get("actions")):
            if not isinstance(a, dict) or not a.get("title"):
                continue
            key = slugify(str(a.get("key") or a.get("id") or a.get("event") or a["title"])) or f"a{len(actions) + 1}"
            actions.append({"key": key, "title": str(a["title"]), "do": a.get("do"), "uri": a.get("uri"), "event": a.get("event")})
        cancel = [c for c in as_list(data.get("cancel_when")) if isinstance(c, dict) and ("entity_id" in c or "home_occupied" in c)]
        ttl = data.get("ttl_minutes")
        life = float(ttl) * 60 if isinstance(ttl, (int, float)) and ttl > 0 else TTL_SECONDS
        now = time.time()
        return {
            "id": iid,
            "title": str(data.get("title") or "Home Assistant"),
            "message": str(data.get("message") or ""),
            "tag": str(data.get("tag") or f"nh_{iid}"),
            "target": as_list(data.get("target")) or ["present"],
            "hold": bool(data.get("hold_until_home")),
            "fallback": data.get("fallback"),
            "data": data.get("data") if isinstance(data.get("data"), dict) else {},
            "actions": actions,
            "cancel_when": cancel,
            "status": "held",
            "note": "",
            "active": True,
            "created": now,
            "expires": now + life,
            "sent_at": None,
            "recipients": [],
            "devices": [],
            "history": [{"t": now, "status": "held", "note": "Événement reçu"}],
        }

    def _set(self, item, **kw):
        """Met à jour une notification et ajoute une ligne à son historique (affiché dans « Détails »)."""
        item.update(**kw)
        note = item.get("note") or ""
        if item["status"] == "sent" and item["devices"]:
            note = ("Envoyée à : " + ", ".join(item["devices"])) + (" — " + note if note else "")
        item.setdefault("history", []).append({"t": time.time(), "status": item["status"], "note": note})

    def _payload(self, item):
        data = dict(item["data"])
        data["tag"] = item["tag"]
        # Canal dédié en importance élevée : sans cela, MIUI et d'autres replient la notification et cachent ses boutons.
        data.setdefault("channel", "Centre de notifications")
        data.setdefault("importance", "high")
        data.setdefault("priority", "high")
        data.setdefault("ttl", 0)
        if item["actions"]:
            data["actions"] = [
                {"action": "URI", "title": a["title"], "uri": a["uri"]}
                if a.get("uri")
                else {"action": a["event"] if a.get("event") else f"NH|{item['id']}|{a['key']}", "title": a["title"]}
                for a in item["actions"]
            ]
        return {"title": item["title"], "message": item["message"], "data": data}

    async def _deliver(self, item):
        ids, unknown = self.resolve(item["target"], item["hold"])
        if not ids and item.get("fallback") and not item["hold"]:
            ids, _ = self.resolve(item["fallback"])
        if unknown:
            log.warning("Destinataires inconnus : %s", ", ".join(unknown))
        if not ids:
            if item["hold"]:
                item["status"], item["note"] = "held", "En attente : personne à notifier n'est à la maison"
            else:
                self._set(item, status="ignored", active=False, note="Aucun destinataire pour cette cible")
            return
        devices = []
        for pid in ids:
            for svc in await self.devices_of(pid):
                if svc not in devices:
                    devices.append(svc)
        names = {p["id"]: p["name"] for p in self.people()}
        item["recipients"] = [names.get(pid, pid) for pid in ids]
        if not devices:
            self._set(item, status="failed", active=False, note="Aucun appareil mobile trouvé pour : " + ", ".join(item["recipients"]))
            return
        payload, ok, errors = self._payload(item), [], []
        for svc in devices:
            try:
                await self.call_service("notify", svc, payload)
                ok.append(svc)
            except HAError as err:
                errors.append(f"{svc} : {err}")
        item["devices"] = ok
        if ok:
            self._set(item, status="sent", sent_at=time.time(), note="")
            if errors:
                item["note"] = "Échec partiel : " + " ; ".join(errors)
        else:
            self._set(item, status="failed", active=False, note="Envoi impossible : " + " ; ".join(errors))

    async def _clear_devices(self, devices, tag):
        for svc in devices:
            try:
                await self.call_service("notify", svc, {"message": "clear_notification", "data": {"tag": tag}})
            except HAError as err:
                log.warning("Effacement impossible sur %s : %s", svc, err)

    async def _cancel(self, item, note, status):
        if item["status"] == "sent":
            await self._clear_devices(item["devices"], item["tag"])
        self._set(item, status=status, note=note, active=False)
        self._save()
        log.info("Notification %s (%s) : %s", item["id"], item["tag"], note)

    async def handle_send(self, data):
        if not (data.get("message") or data.get("title")):
            log.warning("Événement %s ignoré : message manquant", EV_SEND)
            return None
        async with self.lock:
            item = self._build(data)
            if any(self.cond_true(c) for c in item["cancel_when"]):
                self._set(item, status="ignored", active=False, note="Condition d'annulation déjà remplie")
            else:
                await self._deliver(item)
            # une notification plus récente avec le même tag remplace l'ancienne
            for old in self.items:
                if old["active"] and old["tag"] == item["tag"]:
                    stale = [d for d in old["devices"] if d not in item["devices"]] if old["status"] == "sent" else []
                    await self._clear_devices(stale, old["tag"])
                    self._set(old, status="replaced", active=False, note="Remplacée par une notification plus récente")
            self.items.append(item)
            self._save()
            log.info("Notification %s (%s) : %s", item["id"], item["tag"], item["status"])
            return item

    async def release_held(self):
        async with self.lock:
            changed = False
            for item in self.items:
                if item["status"] == "held" and item["active"] and self.resolve(item["target"], True)[0]:
                    await self._deliver(item)
                    changed = True
            if changed:
                self._save()

    async def expire(self):
        now = time.time()
        async with self.lock:
            changed = False
            for item in self.items:
                if item["active"] and now > item["expires"]:
                    if item["status"] == "held":
                        self._set(item, status="expired", active=False, note="Expirée avant la fin de l'attente")
                    else:
                        item["active"] = False
                    changed = True
            if changed:
                self._save()

    async def handle_clear(self, data):
        tag, iid = data.get("tag"), data.get("id")
        async with self.lock:
            for item in self.items:
                if item["active"] and ((tag and item["tag"] == tag) or (iid and item["id"] == iid)):
                    await self._cancel(item, "Annulée par une automatisation", "cancelled")

    async def handle_action(self, data):
        act = str(data.get("action", ""))
        parts = act.split("|", 2)
        async with self.lock:
            item = action = None
            if len(parts) == 3 and parts[0] == "NH":
                item = next((i for i in self.items if i["id"] == parts[1]), None)
                action = next((a for a in (item or {}).get("actions", []) if a["key"] == parts[2]), None)
            else:  # bouton « callback » : l'identifiant de l'action est l'événement choisi par l'utilisateur
                for i in reversed(self.items):
                    a = next((x for x in i["actions"] if x.get("event") and x["event"] == act), None)
                    if a and i["active"]:
                        item, action = i, a
                        break
            if not item or not action or not item["active"]:
                return
            await self._run_action(item, action)

    async def _run_action(self, item, action, from_ui=False):
        """Exécute un bouton (verrou pris) : actions `do`, événement éventuel, effacement des téléphones, statut « traitée »."""
        notes = []
        for step in as_list(action.get("do")):
            if not isinstance(step, dict):
                continue
            name = str(step.get("action") or step.get("service") or "")
            if "." not in name:
                continue
            domain, service = name.split(".", 1)
            try:
                await self.call_service(domain, service, step.get("data"), step.get("target"))
            except HAError as err:
                notes.append(f"{name} : {err}")
        if from_ui and action.get("event"):
            # un bouton « callback » appuyé depuis l'interface déclenche le même événement que sur le téléphone
            try:
                await self.call("fire_event", event_type="mobile_app_notification_action", event_data={"action": action["event"]})
            except HAError as err:
                notes.append(f"événement : {err}")
        if item["status"] == "sent":
            await self._clear_devices(item["devices"], item["tag"])
        where = " (depuis l'interface)" if from_ui else ""
        self._set(item, status="handled", active=False, note=f"Action « {action['title']} » exécutée{where}" + (" ; erreurs : " + " ; ".join(notes) if notes else ""))
        self._save()
        log.info("Notification %s : action %s", item["id"], action["key"])

    async def action_by_id(self, iid, key):
        async with self.lock:
            item = next((i for i in self.items if i["id"] == iid), None)
            action = next((a for a in (item or {}).get("actions", []) if a["key"] == key), None)
            if not item or not action or not item["active"]:
                return False
            await self._run_action(item, action, from_ui=True)
            return True

    async def cancel_by_id(self, iid):
        async with self.lock:
            item = next((i for i in self.items if i["id"] == iid), None)
            if not item or not item["active"]:
                return False
            await self._cancel(item, "Annulée depuis l'interface", "cancelled")
            return True

    async def delete_item(self, iid):
        """Supprime une notification de la liste (la retire d'abord des téléphones si elle est active)."""
        async with self.lock:
            item = next((i for i in self.items if i["id"] == iid), None)
            if not item:
                return False
            if item["active"]:
                await self._cancel(item, "Supprimée depuis l'interface", "cancelled")
            self.items = [i for i in self.items if i["id"] != iid]
            self._save()
            return True

    async def clear_history(self, scope):
        """scope « finished » : supprime les notifications terminées ; « all » : toutes (les actives sont retirées des téléphones)."""
        async with self.lock:
            removed = 0
            for item in list(self.items):
                if item["active"] and scope == "all":
                    await self._cancel(item, "Supprimée depuis l'interface", "cancelled")
                if not item["active"]:
                    self.items.remove(item)
                    removed += 1
            self._save()
            return removed

    # ---- vue pour l'interface
    async def view(self):
        people = self.people()
        nearest = set(self.nearest_ids(people))
        out = []
        for p in people:
            devices = await self.devices_of(p["id"])
            out.append({**p, "devices": devices, "nearest": p["id"] in nearest})
        items = [
            {k: i[k] for k in ("id", "title", "message", "tag", "target", "hold", "status", "note", "active", "created", "sent_at", "recipients", "devices", "cancel_when")}
            | {"history": i.get("history", []), "data": i.get("data", {}), "expires": i.get("expires")}
            | {"actions": [{"key": a["key"], "title": a["title"], "uri": a.get("uri")} for a in i["actions"]]}
            for i in reversed(self.items)
        ]
        return {
            "connected": self.connected,
            "occupied": self.occupied(),
            "threshold": THRESHOLD,
            "people": out,
            "notifications": items[:60],
        }


# --------------------------------------------------------------------------- routes
@web.middleware
async def only_ingress(request, handler):
    peer = request.transport.get_extra_info("peername")
    if peer and peer[0] not in ALLOWED_PEERS:
        return web.Response(status=403, text="Accès refusé")
    return await handler(request)


@web.middleware
async def errors(request, handler):
    try:
        return await handler(request)
    except HAError as err:
        return web.json_response({"error": str(err)}, status=502)
    except (ValueError, json.JSONDecodeError) as err:
        return web.json_response({"error": f"Requête invalide : {err}"}, status=400)
    except web.HTTPException:
        raise
    except Exception as err:
        log.exception("Erreur non gérée")
        return web.json_response({"error": f"Erreur interne : {err}"}, status=500)


async def index(request):
    return web.FileResponse(BASE / "static" / "index.html")


async def static_file(request):
    root = (BASE / "static").resolve()
    path = (root / request.match_info["name"]).resolve()
    if root not in path.parents or not path.is_file():
        raise web.HTTPNotFound()
    return web.FileResponse(path)


async def api_state(request):
    return web.json_response(await request.app["hub"].view())


async def api_test(request):
    data = await request.json()
    hub = request.app["hub"]
    item = await hub.handle_send(
        {
            "title": (data.get("title") or "Test").strip(),
            "message": (data.get("message") or "Notification de test").strip(),
            "target": data.get("target") or "present",
            "hold_until_home": bool(data.get("hold_until_home")),
            "actions": [{"key": "ok", "title": "OK"}],
        }
    )
    return web.json_response({"id": item["id"], "status": item["status"], "note": item["note"], "recipients": item["recipients"]})


async def api_cancel(request):
    ok = await request.app["hub"].cancel_by_id(request.match_info["nid"])
    if not ok:
        return web.json_response({"error": "Notification introuvable ou déjà terminée."}, status=404)
    return web.json_response({"ok": True})


async def api_action(request):
    ok = await request.app["hub"].action_by_id(request.match_info["nid"], request.match_info["key"])
    if not ok:
        return web.json_response({"error": "Notification ou bouton introuvable, ou notification déjà terminée."}, status=404)
    return web.json_response({"ok": True})


async def api_delete(request):
    if not await request.app["hub"].delete_item(request.match_info["nid"]):
        return web.json_response({"error": "Notification introuvable."}, status=404)
    return web.json_response({"ok": True})


async def api_clear(request):
    data = await request.json()
    scope = "all" if data.get("scope") == "all" else "finished"
    return web.json_response({"removed": await request.app["hub"].clear_history(scope)})


async def no_cache(request, response):
    response.headers["Cache-Control"] = "no-store"


async def ttl_loop(hub):
    while True:
        await asyncio.sleep(60)
        try:
            await hub.expire()
        except Exception:
            log.exception("Erreur d'expiration")


async def on_start(app):
    app["session"] = ClientSession()
    app["hub"] = Hub(app["session"])
    app["tasks"] = [asyncio.create_task(app["hub"].run()), asyncio.create_task(ttl_loop(app["hub"]))]


async def on_stop(app):
    for t in app["tasks"]:
        t.cancel()
    await app["session"].close()


def make_app():
    app = web.Application(middlewares=[only_ingress, errors], client_max_size=1024 * 1024)
    app.on_response_prepare.append(no_cache)
    app.on_startup.append(on_start)
    app.on_cleanup.append(on_stop)
    # Le proxy ingress peut ajouter des « / » en trop devant le chemin : on les accepte tous.
    pre = "/{_pre:/*}"
    app.router.add_get(pre, index)
    app.router.add_get(pre + "app", index)
    app.router.add_get(pre + "api/state", api_state)
    app.router.add_post(pre + "api/test", api_test)
    app.router.add_post(pre + "api/cancel/{nid}", api_cancel)
    app.router.add_post(pre + "api/notification/{nid}/action/{key}", api_action)
    app.router.add_delete(pre + "api/notification/{nid}", api_delete)
    app.router.add_post(pre + "api/clear", api_clear)
    app.router.add_get(pre + "static/{name:.+}", static_file)
    return app


if __name__ == "__main__":
    log.info("Démarrage — seuil de proximité %s m, zones supplémentaires %s", THRESHOLD, EXTRA_ZONES or "aucune")
    web.run_app(make_app(), host="0.0.0.0", port=8099, print=None)
