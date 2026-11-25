"""Faux Home Assistant pour tester le Centre de notifications en local (voir docs/TESTING.md).

Simule le WebSocket de HA (auth, get_states, abonnements, registres, services, call_service) et expose
des routes de pilotage :
  POST /_mock/state  {entity_id, state, attributes?}   -> change un état et diffuse state_changed
  POST /_mock/event  {event_type, data}                -> diffuse un événement
  GET  /_mock/calls                                    -> appels de service reçus
  POST /_mock/reset                                    -> remet les données de départ

Lancement : python dev/mock_ha.py   (port 18123)
"""
import copy
import json

from aiohttp import WSMsgType, web

HOME = {"latitude": 50.7223, "longitude": 4.8735}


def start_states():
    def person(eid, name, state, trackers, lat=None, lon=None):
        attrs = {"friendly_name": name, "device_trackers": trackers, "id": "p_" + eid.split(".")[1]}
        if lat is not None:
            attrs.update(latitude=lat, longitude=lon)
        return {"entity_id": eid, "state": state, "attributes": attrs}

    return {
        "zone.home": {"entity_id": "zone.home", "state": "0", "attributes": {"friendly_name": "Home", **HOME, "radius": 100}},
        "zone.maison": {"entity_id": "zone.maison", "state": "0", "attributes": {"friendly_name": "Maison", **HOME, "radius": 30}},
        "person.david": person("person.david", "David", "Maison", ["device_tracker.phone_a"], **{"lat": 50.7223, "lon": 4.8735}),
        "person.julie": person("person.julie", "Julie", "not_home", ["device_tracker.phone_j"], lat=50.80, lon=4.95),
        "light.salon": {"entity_id": "light.salon", "state": "on", "attributes": {"friendly_name": "Salon"}},
    }


STATES = start_states()
CALLS = []
SUBS = {}  # websocket -> {event_type: subscription id}

DEVICES = [
    {"id": "d1", "name": "Phone A", "name_by_user": None, "identifiers": [["mobile_app", "a"]]},
    {"id": "d2", "name": "Phone B ", "name_by_user": None, "identifiers": [["mobile_app", "b"]]},
    {"id": "d3", "name": "Phone J", "name_by_user": None, "identifiers": [["mobile_app", "j"]]},
]
ENTITIES = [
    {"entity_id": "device_tracker.phone_a", "platform": "mobile_app", "device_id": "d1"},
    {"entity_id": "device_tracker.phone_b", "platform": "mobile_app", "device_id": "d2"},
    {"entity_id": "device_tracker.phone_j", "platform": "mobile_app", "device_id": "d3"},
]
SERVICES = {"notify": {"mobile_app_phone_a": {}, "mobile_app_phone_b": {}, "mobile_app_phone_j": {}, "send_message": {}}}


async def broadcast(event_type, data):
    for ws, subs in list(SUBS.items()):
        if event_type in subs:
            await ws.send_json({"id": subs[event_type], "type": "event", "event": {"event_type": event_type, "data": data}})


async def ws_handler(request):
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    SUBS[ws] = {}
    await ws.send_json({"type": "auth_required"})
    async for m in ws:
        if m.type != WSMsgType.TEXT:
            continue
        d = json.loads(m.data)
        t = d.get("type")
        if t == "auth":
            await ws.send_json({"type": "auth_ok"})
            continue
        result = None
        if t == "get_states":
            result = list(STATES.values())
        elif t == "subscribe_events":
            SUBS[ws][d["event_type"]] = d["id"]
        elif t == "config/device_registry/list":
            result = DEVICES
        elif t == "config/entity_registry/list":
            result = ENTITIES
        elif t == "get_services":
            result = SERVICES
        elif t == "person/list":
            # David : le 2e téléphone n'est plus listé dans l'attribut d'état (statut « unknown »), mais reste configuré.
            result = {"storage": [{"id": "p_david", "device_trackers": ["device_tracker.phone_a", "device_tracker.phone_b"]}], "config": []}
        elif t == "call_service":
            CALLS.append({"domain": d["domain"], "service": d["service"], "data": d.get("service_data"), "target": d.get("target")})
        await ws.send_json({"id": d["id"], "type": "result", "success": True, "result": result})
    SUBS.pop(ws, None)
    return ws


async def set_state(request):
    d = await request.json()
    eid = d["entity_id"]
    old = copy.deepcopy(STATES.get(eid))
    new = copy.deepcopy(old) if old else {"entity_id": eid, "attributes": {}}
    new["state"] = d["state"]
    new["attributes"].update(d.get("attributes") or {})
    STATES[eid] = new
    await broadcast("state_changed", {"entity_id": eid, "old_state": old, "new_state": new})
    return web.json_response({"ok": True})


async def fire(request):
    d = await request.json()
    await broadcast(d["event_type"], d.get("data") or {})
    return web.json_response({"ok": True})


async def calls(request):
    return web.json_response(CALLS)


async def reset(request):
    old = copy.deepcopy(STATES)
    STATES.clear()
    STATES.update(start_states())
    for eid, new in STATES.items():
        await broadcast("state_changed", {"entity_id": eid, "old_state": old.get(eid), "new_state": new})
    CALLS.clear()
    return web.json_response({"ok": True})


app = web.Application()
app.router.add_get("/websocket", ws_handler)
app.router.add_post("/_mock/state", set_state)
app.router.add_post("/_mock/event", fire)
app.router.add_get("/_mock/calls", calls)
app.router.add_post("/_mock/reset", reset)

if __name__ == "__main__":
    web.run_app(app, port=18123, print=None)
