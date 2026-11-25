# Centre de notifications – application Home Assistant

Envoie les bonnes notifications aux bonnes personnes, au bon moment, et pour la bonne durée.
Inspiré du « Notifier » de la vidéo [« Les notifications sur Home Assistant, c'est compliqué ? »](https://www.youtube.com/watch?v=chJylIK0ASo)
(Horizon Domotique), réécrit comme une application Home Assistant autonome, sans AppDaemon.

## Les problèmes résolus

| Besoin | Solution |
|---|---|
| À qui ? | Une personne précise, les personnes **présentes**, ou les **plus proches** de la maison. |
| Quand ? | **Mise en attente** jusqu'au retour du premier occupant. |
| Remplacer | Une notification avec le même `tag` écrase l'ancienne (ex. aspirateur « démarré » puis « terminé »). |
| Agir une fois | Appuyer sur un bouton sur un téléphone **efface** la notification sur tous les autres. |
| Devenir inutile | Des **conditions d'annulation** retirent la notification (ex. la maison est de nouveau occupée). |
| Positions décalées | Un **seuil de proximité** (200 m par défaut) évite qu'un seul des deux occupants arrivant en voiture soit « le plus proche ». |

## Fonctionnement en bref

Les automatisations existantes envoient simplement un événement :

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

L'application écoute ces événements, détermine les destinataires et leurs téléphones (application compagnon
Home Assistant), puis gère le cycle de vie de la notification. Personnes, présence et distance sont détectées
automatiquement à partir des entités `person` et de la zone maison : **aucun capteur à créer**.

## Interface

Un lien **Notifications** dans la barre latérale, dans le style des applications Home Assistant :
personnes (présence, distance, téléphones), notifications récentes avec leur statut, envoi de test, exemples
d'automatisations prêts à copier.

## Avec Automations IA

Si l'application [Automations IA](https://github.com/Tolrac/ha-addon-ia-automations) est installée et démarrée, le
Centre l'affiche dans un panneau latéral pour **créer en une phrase l'automatisation d'une notification** : l'IA
(Groq) écrit l'action `notification_hub_send` avec les bons champs. Tu vérifies, puis tu ajoutes.

## Prérequis

- Home Assistant OS ou Supervised, architecture `aarch64` ou `amd64`.
- L'application compagnon Home Assistant sur le téléphone de chaque personne.
- Des entités `person` (Paramètres → Personnes) avec leurs appareils de suivi.

## Installation

1. Dans Home Assistant : Paramètres → Applications → Boutique des applications → ⋮ → **Dépôts**.
2. Ajouter `https://github.com/Tolrac/ha-addon-notification-hub`.
3. Installer **Centre de notifications**, puis le démarrer.

Les boutons d'action ne s'affichent pas sur certains Xiaomi : voir « Les boutons d'action n'apparaissent pas » dans
[notification_hub/DOCS.md](notification_hub/DOCS.md).

## Documentation

Toutes les options, les champs de l'événement `notification_hub_send` (cibles, boutons, conditions
d'annulation) et des exemples sont dans [notification_hub/DOCS.md](notification_hub/DOCS.md), aussi affichée
dans l'onglet « Documentation » de l'application.

## Tests

`python dev/smoke_test.py` lance un faux Home Assistant et le serveur, puis rejoue les scénarios de la vidéo
(présents, attente, plus proches, remplacement, annulation par condition, bouton d'action). Il demande
`aiohttp` (`pip install aiohttp`).

## Structure

```text
notification_hub/        L'application (config.yaml, Dockerfile, DOCS.md, app/)
dev/mock_ha.py           Faux Home Assistant (WebSocket) pour les tests
dev/smoke_test.py        Test de bout en bout
```

## Licence

[MIT](LICENSE)
