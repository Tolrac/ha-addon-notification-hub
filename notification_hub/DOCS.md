# Centre de notifications

Une notification, ça s'envoie… mais à qui, quand, et pour combien de temps ? Le Centre de notifications
répond à ces trois questions pour toutes tes automatisations, avec l'application compagnon Home Assistant.

## Ce qu'il fait

- **À qui** : une personne précise, toutes les personnes **présentes**, les personnes **les plus proches** de la
  maison (avec un seuil de tolérance), ou tout le monde.
- **Quand** : garder la notification **en attente** jusqu'au retour d'un occupant.
- **Combien de temps** :
  - une notification avec le même `tag` **remplace** la précédente ;
  - appuyer sur un bouton d'action sur un téléphone **efface** la notification sur tous les autres ;
  - des **conditions d'annulation** retirent la notification quand elles deviennent vraies (par exemple « la
    maison est de nouveau occupée »).

Les personnes, leur présence, leur distance et leurs téléphones sont **détectés automatiquement** à partir des
entités `person` (Paramètres → Personnes) et de la zone `zone.home`. Aucun capteur d'occupation ni intégration
Proximity n'est nécessaire.

## Envoyer une notification

Dans une automatisation, utilise l'action `event` :

```yaml
actions:
  - event: notification_hub_send
    event_data:
      title: Lessive terminée
      message: Pense à étendre le linge.
      target: present
      hold_until_home: true
      tag: lave_linge
```

### Champs de `event_data`

| Champ | Description |
|---|---|
| `title` | Titre (défaut « Home Assistant »). |
| `message` | Texte de la notification (les templates Jinja sont acceptés). |
| `target` | `present` (défaut), `nearest`, `all`, une personne (`person.david` ou `david`), ou une liste. |
| `hold_until_home` | `true` : n'envoie que lorsqu'un destinataire est à la maison, sinon garde en attente. |
| `tag` | Identifiant de remplacement : une notification plus récente avec le même tag remplace l'ancienne. |
| `actions` | Boutons : `title` (obligatoire), `key` (facultatif), `do` (actions HA à exécuter), ou `uri` (ouvre un lien). |
| `cancel_when` | Conditions d'annulation (voir plus bas). |
| `ttl_minutes` | Durée de suivi (défaut : option `ttl_hours`). |
| `data` | Données supplémentaires transmises à l'application compagnon (`image`, `channel`, `importance`, …). Par défaut : canal « Centre de notifications », `importance: high`, `priority: high`, `ttl: 0` (notification en bandeau avec ses boutons visibles). |

### Cibles

- `present` : personnes dont l'état est « à la maison ».
- `nearest` : la personne la plus proche de la maison, **plus** celles qui sont à moins de `proximity_threshold`
  mètres derrière elle (les positions de deux téléphones ne sont jamais synchronisées).
- `all` : toutes les personnes.
- `person.xxx` : une personne précise. Une liste combine plusieurs cibles.

Avec `hold_until_home: true`, seuls les destinataires qui sont à la maison reçoivent la notification. Si aucun ne
l'est, elle reste en attente et part dès que l'un d'eux rentre.

### Boutons d'action

```yaml
      actions:
        - title: Tout éteindre
          do:
            - action: light.turn_off
              target:
                entity_id: all
        - title: Détails
          uri: /lovelace/home
```

Quand un bouton `do` est appuyé, les actions sont exécutées, puis la notification est effacée sur tous les
appareils qui l'avaient reçue.

Exemple complet : porte d'entrée ouverte alors que la maison est vide, avec photo et bouton pour ouvrir la caméra.

```yaml
triggers:
  - trigger: state
    entity_id: binary_sensor.door_sensor_ouverture
    to: "on"
actions:
  - action: camera.snapshot
    target:
      entity_id: camera.ezcube_pro_d42498709
    data:
      filename: /config/www/snapshots/entree.jpg
  - delay: "00:00:02"
  - event: notification_hub_send
    event_data:
      title: Porte d'entrée ouverte
      message: La porte d'entrée vient de s'ouvrir alors que la maison est vide.
      target: all
      tag: porte_entree
      data:
        image: https://exemple.duckdns.org/local/snapshots/entree.jpg
      actions:
        - title: Ouvrir la caméra
          uri: https://exemple.duckdns.org/dashboard-cameras/camera-salon
      cancel_when:
        - home_occupied: true
        - entity_id: binary_sensor.door_sensor_ouverture
          new_state: "off"
```

`home_occupied: true` remplace une condition « personne à la maison » écrite à la main : si la maison est déjà
occupée à l'envoi, la notification est ignorée ; si quelqu'un rentre ensuite, elle est annulée. Les personnes
sont détectées automatiquement (aucune entité `person` à lister). Le délai laisse au snapshot le temps d'être écrit
avant l'envoi de l'image.

### Conditions d'annulation

`cancel_when` est une liste ; **une seule** condition vraie annule la notification (et l'efface des téléphones).
Si l'une est déjà vraie à l'envoi, la notification est ignorée.

| Condition | Exemple |
|---|---|
| Maison occupée / vide | `- home_occupied: true` |
| État d'une entité | `- entity_id: light.salon` + `state: "off"` (ou une liste d'états) |
| État différent de | `- entity_id: vacuum.robot` + `not_state: cleaning` |
| Seuil numérique | `- entity_id: sensor.temp` + `above: 25` ou `below: 5` |
| Attribut | ajouter `attribute: nom_attribut` |

### Effacer depuis une automatisation

```yaml
  - event: notification_hub_clear
    event_data:
      tag: lumieres_allumees
```

## Compatibilité avec l'ancien « Notifier »

Les champs de l'ancien Notifier (AppDaemon) sont acceptés dans `event_data`, en plus des champs ci-dessus :

| Ancien champ | Équivalent |
|---|---|
| `action: send_to_present` | `target: present` ; si personne n'est à la maison, repli sur la personne la plus proche (`fallback: nearest`). |
| `action: send_to_nearest` | `target: nearest` |
| `action: send_to_<nom>` | `target: <nom>` (identifiant `person.<nom>` ou nom affiché, ex. `david_gillet`) |
| `action: send_when_present` | `target: present` + `hold_until_home: true` |
| `callback: [{title, event}]` | `actions` : l'identifiant du bouton est l'`event` ; l'appui déclenche l'événement `mobile_app_notification_action` (utilisable comme déclencheur) et efface la notification partout. |
| `until: [{entity_id, new_state}]` | `cancel_when: [{entity_id, state}]` |
| `image_url` | `data.image` |
| `click_url` | `data.clickAction` et `data.url` |
| `icon` (`mdi:…`) | `data.notification_icon` |
| `color` | `data.color` |
| `persistent: true` | `data.persistent` et `data.sticky` |
| `timeout` (secondes) | `data.timeout` |

Exemple : `event_data: {action: send_to_nearest, title: Maison vide, message: Lumières allumées, tag: lum, callback: [{title: Éteindre, event: turn_off_lights}], until: [{entity_id: light.salon, new_state: "off"}]}`.

## Configuration

| Option | Description | Défaut |
|---|---|---|
| `proximity_threshold` | Écart (en mètres) toléré derrière la personne la plus proche pour la cible `nearest`. | `200` |
| `extra_home_zones` | Zones supplémentaires considérées comme « à la maison » (ex. `zone.maison`). Une personne dans `zone.home` est toujours à la maison. | vide |
| `occupancy_entity` | Capteur binaire d'occupation facultatif (`on` = occupée). Sinon : au moins une personne à la maison. | vide |
| `people_overrides` | Forcer les services de notification d'une personne : `person` + `notify` (liste séparée par des virgules, sans `notify.`). | vide |
| `ttl_hours` | Durée pendant laquelle une notification en attente ou envoyée est suivie (annulation, expiration). | `24` |

Les appareils sont trouvés via les `device_trackers` de la personne : le service `notify.mobile_app_<nom de
l'appareil>` correspondant est utilisé. Si une personne n'a aucun appareil, l'interface le signale ; utilise alors
`people_overrides`.

## Les boutons d'action n'apparaissent pas

Les boutons sont envoyés avec la notification, mais c'est le téléphone qui les affiche. Une notification standard de
Home Assistant, envoyée directement à `notify.mobile_app_*`, a le même défaut : le centre n'y est pour rien.

**Xiaomi (MIUI / HyperOS)** : par défaut, le style de notification « Classique » masque les boutons. Réglages →
Notifications et barre d'état → **Style de notification** → choisir **Android**. Les boutons s'affichent alors
(testé sur un Xiaomi 14T Pro, Android 16). Sinon, vérifier : Réglages → Applications → Home Assistant →
Notifications (catégorie en « Urgent » avec bandeau autorisé), puis dérouler la notification (flèche ou glisser vers
le bas) ; l'historique de
l'application compagnon (Paramètres → Companion app → Notifications → Historique) montre les données reçues,
dont `actions`.

## Interface

Le lien **Notifications** de la barre latérale affiche les personnes (présence, distance, téléphones), les
notifications récentes avec leur statut (envoyée, en attente, remplacée, annulée, traitée, ignorée, expirée,
échec) et permet d'annuler une notification ou d'envoyer un test. Les boutons d'une notification active sont
cliquables sur sa carte : un bouton `do` ou événement exécute l'action (comme sur le téléphone, avec effacement partout), un
bouton `uri` ouvre le lien dans un nouvel onglet. Le lien **Détails** d'une carte affiche les destinataires, les
appareils, les conditions d'annulation, les données envoyées et l'historique daté (reçue, envoyée, remplacée, annulée…). Le menu ⋮ propose des exemples d'automatisations.

## Effacer des notifications

- **Annuler** (bouton d'une notification active) : la retire des téléphones et la marque « Annulée ».
- **Supprimer** (icône poubelle) : retire la notification de la liste ; si elle était active, elle est d'abord retirée des téléphones.
- Menu ⋮ → **Effacer l'historique** : supprime les notifications terminées ; les actives restent.
- Menu ⋮ → **Tout effacer…** : supprime tout, après confirmation ; les actives sont retirées des téléphones.

## Créer l'automatisation avec l'IA

Si l'application **Automations IA** est installée et démarrée, le bouton **✨ Créer l'automatisation (IA)** (et
**✨ Automatiser** sur chaque notification) ouvre un panneau latéral qui affiche son interface. Décris quand envoyer la
notification : l'IA écrit l'automatisation avec l'événement `notification_hub_send`, tu la vérifies (résumé, canevas,
étapes, YAML) puis tu l'ajoutes. Sur une carte, la phrase est pré-remplie avec le titre, le message, les destinataires
et le tag. Sans Automations IA, le bouton n'apparaît pas.

## Confidentialité

Tout reste dans Home Assistant : l'application n'appelle aucun service externe. Elle n'écoute que sur le réseau
interne (accès via la barre latérale). L'historique (100 dernières notifications) est enregistré dans le dossier
de données de l'application.
