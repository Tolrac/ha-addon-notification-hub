"""Test de bout en bout : faux HA + serveur réel, scénarios de la vidéo (voir docs/TESTING.md).

Usage : python dev/smoke_test.py      (lance lui-même le faux HA et le serveur, puis les arrête)
"""
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
MOCK = "http://127.0.0.1:18123"
HUB = "http://127.0.0.1:8099"
FAILS = []


def http(url, data=None, method=None):
    req = urllib.request.Request(url, data=json.dumps(data).encode() if data is not None else None, method=method)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read() or b"{}")


def state(eid, st, attrs=None):
    http(MOCK + "/_mock/state", {"entity_id": eid, "state": st, "attributes": attrs or {}})
    time.sleep(0.4)


def send(**data):
    http(MOCK + "/_mock/event", {"event_type": "notification_hub_send", "data": data})
    time.sleep(0.5)


def calls(domain="notify"):
    return [c for c in http(MOCK + "/_mock/calls") if c["domain"] == domain]


def notifs():
    return http(HUB + "/api/state")["notifications"]


def check(name, cond, detail=""):
    print(("  OK   " if cond else "  ÉCHEC"), name, "" if cond else detail)
    if not cond:
        FAILS.append(name)


def reset():
    http(MOCK + "/_mock/reset", {})
    time.sleep(0.5)


def wait_ready():
    for _ in range(50):
        try:
            if http(HUB + "/api/state")["connected"]:
                return
        except Exception:
            pass
        time.sleep(0.2)
    raise SystemExit("Le serveur n'a pas démarré")


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    data_dir = tempfile.mkdtemp()
    env = {**os.environ, "HA_WS_URL": "ws://127.0.0.1:18123/websocket", "DATA_DIR": data_dir, "OPTIONS_FILE": data_dir + "/none.json"}
    opts = Path(data_dir) / "options.json"
    opts.write_text(json.dumps({"proximity_threshold": 200, "extra_home_zones": ["zone.maison"], "ttl_hours": 24}))
    env["OPTIONS_FILE"] = str(opts)
    procs = [subprocess.Popen([sys.executable, str(ROOT / "dev" / "mock_ha.py")], env=env)]
    time.sleep(1)
    procs.append(subprocess.Popen([sys.executable, str(ROOT / "notification_hub" / "app" / "server.py")], env=env))
    try:
        wait_ready()

        print("1. Cible « présents » : seul David (zone Maison, 2 téléphones) reçoit")
        reset(); time.sleep(1)
        send(title="Test", message="m", target="present", tag="t1")
        c = calls()
        check("2 appels, téléphones de David", sorted(x["service"] for x in c) == ["mobile_app_phone_a", "mobile_app_phone_b"], c)

        print("2. Mise en attente jusqu'au retour")
        reset(); state("person.david", "not_home", {"latitude": 50.9, "longitude": 4.9}); time.sleep(0.5)
        send(title="Lessive", message="m", target="present", hold_until_home=True, tag="lessive")
        check("aucun envoi tant que personne n'est là", len(calls()) == 0)
        check("statut « held »", notifs()[0]["status"] == "held")
        state("person.david", "Maison")
        time.sleep(0.5)
        check("envoyée au retour", len(calls()) == 2 and notifs()[0]["status"] == "sent", calls())

        print("3. Plus proches avec seuil de 200 m")
        reset(); state("person.david", "not_home", {"latitude": 50.7323, "longitude": 4.8735})  # ~1,1 km
        send(title="Lumières", message="m", target="nearest", tag="lum")
        c = calls()
        check("seul David (le plus proche) reçoit", sorted({x["service"] for x in c}) == ["mobile_app_phone_a", "mobile_app_phone_b"], c)

        print("4. Remplacement par tag")
        reset(); send(title="Démarre", message="m", target="all", tag="aspi")
        send(title="Terminé", message="m", target="all", tag="aspi")
        n = notifs()
        check("ancienne = remplacée, récente = envoyée", n[1]["status"] == "replaced" and n[0]["status"] == "sent", [x["status"] for x in n])

        print("5. Annulation par condition (maison occupée)")
        reset(); state("person.david", "not_home", {"latitude": 50.9, "longitude": 4.9})
        send(title="Maison vide", message="m", target="nearest", tag="vide", cancel_when=[{"home_occupied": True}])
        check("envoyée", notifs()[0]["status"] == "sent")
        before = len(calls())
        state("person.david", "Maison"); time.sleep(0.6)
        clears = [x for x in calls()[before:] if x["data"].get("message") == "clear_notification"]
        check("effacée sur tous les téléphones", len(clears) >= 1 and notifs()[0]["status"] == "cancelled", (clears, notifs()[0]["status"]))

        print("6. Bouton d'action : exécute l'action et efface partout")
        reset(); send(title="Lumières", message="m", target="all", tag="act", actions=[{"key": "off", "title": "Éteindre", "do": [{"action": "light.turn_off", "target": {"entity_id": "light.salon"}}]}])
        nid = notifs()[0]["id"]
        http(MOCK + "/_mock/event", {"event_type": "mobile_app_notification_action", "data": {"action": f"NH|{nid}|off"}}); time.sleep(0.6)
        check("light.turn_off appelé", any(x["service"] == "turn_off" for x in calls("light")))
        check("statut « handled »", notifs()[0]["status"] == "handled")

        print("7. Condition déjà remplie → ignorée ; cible inconnue → ignorée")
        reset(); send(title="x", message="m", target="all", cancel_when=[{"entity_id": "light.salon", "state": "on"}])
        check("ignorée", notifs()[0]["status"] == "ignored")

        print("8. Champs de l'ancien Notifier : callback, image_url, click_url, until")
        reset()
        send(action="send_to_nearest", title="Aspirateur", message="m", tag="c1", image_url="/local/x.png", click_url="/lovelace/vac",
             callback=[{"title": "Éteindre", "event": "turn_off_lights"}], until=[{"entity_id": "light.salon", "new_state": "off"}])
        c = calls()
        d = c[0]["data"]["data"] if c else {}
        check("image et clic transmis", d.get("image") == "/local/x.png" and d.get("clickAction") == "/lovelace/vac", d)
        check("bouton callback : identifiant = événement", d.get("actions", [{}])[0].get("action") == "turn_off_lights", d)
        http(MOCK + "/_mock/event", {"event_type": "mobile_app_notification_action", "data": {"action": "turn_off_lights"}}); time.sleep(0.6)
        check("appui sur le bouton callback : traitée", notifs()[0]["status"] == "handled", notifs()[0]["status"])
        reset(); send(action="send_to_present", title="until", message="m", tag="c2", until=[{"entity_id": "light.salon", "new_state": "off"}])
        check("until : envoyée", notifs()[0]["status"] == "sent")
        state("light.salon", "off"); time.sleep(0.4)
        check("until : annulée quand la lumière s'éteint", notifs()[0]["status"] == "cancelled", notifs()[0]["status"])
        reset(); state("person.david", "not_home", {"latitude": 50.9, "longitude": 4.9}); state("person.julie", "not_home", {"latitude": 50.95, "longitude": 4.95})
        send(action="send_to_present", title="repli", message="m", tag="c3")
        check("send_to_present, maison vide : repli sur le plus proche", notifs()[0]["status"] == "sent", notifs()[0])

        print("9. Effacement : supprimer une notification, effacer l'historique, tout effacer")
        http(HUB + "/api/clear", {"scope": "all"}); reset()
        send(title="A", message="m", target="all", tag="e1"); send(title="B", message="m", target="all", tag="e1")
        b = notifs()[0]
        before = len(calls())
        http(f"{HUB}/api/notification/{b['id']}", method="DELETE"); time.sleep(0.5)
        clears = [x for x in calls()[before:] if x["data"].get("message") == "clear_notification"]
        check("suppression d'une active : effacée des téléphones et retirée", len(clears) >= 1 and all(n["id"] != b["id"] for n in notifs()), (len(clears), [n["status"] for n in notifs()]))
        send(title="C", message="m", target="all", tag="e2")
        http(HUB + "/api/clear", {"scope": "finished"})
        check("effacer l'historique : seules les actives restent", [n["title"] for n in notifs()] == ["C"], [n["title"] for n in notifs()])
        before = len(calls())
        http(HUB + "/api/clear", {"scope": "all"}); time.sleep(0.5)
        check("tout effacer : liste vide et téléphones effacés", notifs() == [] and len(calls()) > before, (len(notifs()), len(calls()) - before))
    finally:
        for p in procs:
            p.terminate()
    print("\nRésultat :", "tout est OK" if not FAILS else f"{len(FAILS)} échec(s) : {FAILS}")
    sys.exit(1 if FAILS else 0)


if __name__ == "__main__":
    main()
