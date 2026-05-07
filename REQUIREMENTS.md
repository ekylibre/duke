# Duke — Spécification des besoins

> Document de cadrage produit par `/sc:brainstorm` le 2026-05-07. Sert d'entrée à `/sc:design` pour la conception d'architecture.

## 1. Vision

Duke est un **chatbot d'assistance agricole** (texte + voix) pour les utilisateurs d'Ekylibre. Service Python autonome, communiquant via WebSocket avec un client JS embarqué dans Ekylibre. Il interprète une phrase en langage naturel français (souvent agricole/viticole) et :
1. crée des données dans Ekylibre via son API REST (saisie d'intervention au MVP),
2. répond à des questions factuelles en consultant les données de l'exploitation (Q&A lecture seule au MVP),
3. à terme, déclenche des actions métier déjà existantes (impression du grand livre, etc. — phase 2).

Remplace l'ancien gem Ruby `ekylibre/ekylibre-duke` (Watson Assistant + Azure STT) par une stack moderne, souveraine et découplée d'Ekylibre.

## 2. Choix technologiques validés

| Sujet | Décision |
|---|---|
| Langage backend | Python (API + WebSocket) |
| NLU | **Hybride spaCy + LLM** : spaCy (modèle FR + NER agricole custom) extrait les entités locales ; LLM appelé pour intent ambigu, génération de réponse, désambiguïsation |
| Topologie temps réel | WebSocket **direct** entre le client JS d'Ekylibre et Duke (pas de proxy ActionCable) |
| Saisie vocale | **STT navigateur** (Web Speech API) — Duke ne reçoit que du texte |
| Intégration Ekylibre | API REST v2 (`Authorization: simple-token <email> <token>`, header `HTTP_X_TENANT`) |

## 3. Périmètre MVP (P0)

**Inclus :**
- Saisie d'intervention en langage naturel français → création via `POST /api/v2/interventions`
- Q&A lecture seule sur les données de l'exploitation (interventions passées, stocks, parcelles, intrants)
- Confirmation systématique avant écriture (l'utilisateur valide la fiche reconstituée avant POST)
- Multi-tenant : chaque session est rattachée à un tenant Ekylibre + un utilisateur authentifié

**Exclus du MVP, prévus phase 2/3 (P1) :**
- Appel de fonctions métier Ekylibre (grand livre, autres impressions) — nécessite d'exposer un endpoint API côté Ekylibre
- Saisie vocale serveur (Whisper) si la Web Speech API s'avère insuffisante
- Modes de saisie multi-tours complexes (« et ajoute aussi… ») au-delà de la conversation simple
- Notifications push depuis Ekylibre vers Duke (alertes, rappels)

## 4. Exigences fonctionnelles

### F1 — Connexion et authentification
- Le client JS ouvre un WebSocket vers Duke en transmettant à l'établissement de la connexion : token API Ekylibre, identifiant de tenant, locale utilisateur (par défaut `fr`).
- Duke valide le token contre Ekylibre avant d'accepter la session (appel à un endpoint de validation, ex. `GET /api/v2/users/me`).
- Une session WS = un utilisateur authentifié sur un tenant. Pas de partage de session.

### F2 — Saisie d'intervention par langage naturel
- L'utilisateur tape ou dicte une phrase (ex: « j'ai pulvérisé 2L de Karaté Zeon sur la parcelle Pré du Moulin ce matin pendant 1h30 »).
- Duke extrait :
  - **Procédure** (`procedure_name`) — doit matcher un nom de la nomenclature Procedo (`spraying`, `sowing`, `vine_spraying_*`, etc.)
  - **Date et durée** (`started_at`, `stopped_at` ou `working_duration`)
  - **Cibles** : parcelles, cultures (résolues contre les `LandParcel` / `Activity` du tenant)
  - **Intrants** : produits + quantités + unités (résolus contre les `ProductNatureVariant` du lexique)
  - **Outils et opérateurs** si mentionnés
- Duke présente une **fiche reconstituée** à l'utilisateur (champs structurés) ; l'utilisateur peut corriger ou valider.
- Sur validation, Duke crée l'intervention via `POST /api/v2/interventions` et confirme le succès (avec l'ID retourné).
- En cas d'ambiguïté (ex: deux parcelles « Pré du Moulin »), Duke pose une question de désambiguïsation.

### F3 — Q&A lecture seule
- L'utilisateur pose une question (ex: « combien de Karaté Zeon me reste-t-il ? », « quelles parcelles ai-je traitées cette semaine ? »).
- Duke identifie l'intent de lecture, formule la requête vers l'API Ekylibre (endpoints REST pertinents) **ou** une requête SQL contrôlée si décidé en phase design.
- Duke génère une réponse en français, citant les données factuelles. Aucune écriture en base.
- Si la question dépasse le périmètre supporté, Duke le dit explicitement plutôt que d'inventer.

### F4 — Conversation et UX
- Historique de conversation conservé pour la durée de la session (volatile au MVP, persistance possible en phase 2).
- Streaming de la réponse (token par token) si LLM utilisé pour générer le texte final.
- Messages système clairs en français pour les erreurs (token expiré, tenant invalide, API Ekylibre indisponible).

## 5. Exigences non-fonctionnelles

| | Cible MVP |
|---|---|
| Latence p50 saisie d'intervention (de la phrase à la fiche reconstituée) | < 3 s |
| Latence p50 Q&A simple | < 4 s |
| Disponibilité | Aligné sur Ekylibre (pas de SLA distinct au MVP) |
| Sécurité | Token utilisateur jamais persisté côté Duke ; transmis en mémoire pour la durée de la session uniquement. TLS obligatoire sur le WS. |
| Souveraineté | Données agricoles sensibles : préférer un LLM hébergé en UE ou à défaut un fournisseur avec engagement RGPD (à trancher — voir Q1 ci-dessous) |
| Multi-tenant | Aucune fuite cross-tenant ; chaque appel API porte le `HTTP_X_TENANT` de la session |
| Observabilité | Logs structurés (intent détecté, entités extraites, appel API Ekylibre, latence) sans logguer le contenu utilisateur en clair par défaut |

## 6. User stories d'acceptation (MVP)

**US-1 — Saisie de pulvérisation**
> En tant que viticulteur, je dicte « pulvérisation de Karaté Zeon sur la parcelle Bel Air ce matin pendant 2h », Duke me présente une fiche pré-remplie, je valide, l'intervention apparaît dans Ekylibre.

*Critères :* procédure mappée à `vine_spraying_*` ou `spraying`, date du jour matin, durée 2h, parcelle « Bel Air » résolue, produit « Karaté Zeon » résolu via lexique, POST réussi, ID retourné.

**US-2 — Désambiguïsation**
> Si deux parcelles portent un nom proche, Duke me demande laquelle.

*Critères :* Duke ne crée rien tant que l'ambiguïté n'est pas levée ; la question proposée est en français naturel.

**US-3 — Question stock**
> Je demande « combien de Karaté Zeon me reste-t-il ? », Duke répond avec la quantité courante issue d'Ekylibre, sans rien écrire.

*Critères :* aucune mutation API, réponse mentionne l'unité, indique la date de dernière mise à jour si pertinent.

**US-4 — Hors périmètre assumé**
> Je demande « imprime-moi le grand livre », Duke répond clairement que cette fonction n'est pas encore disponible et propose la voie alternative (UI Ekylibre).

*Critères :* pas de tentative d'invention, message orienté action.

## 7. Hors périmètre MVP (rappel explicite)

- Toute écriture autre que les interventions (clients, fournisseurs, journal comptable, etc.).
- Saisie multi-interventions dans un seul tour de parole.
- Suggestions proactives (« vous devriez ressemer X »).
- Mode hors-ligne / queue de saisies différées.
- Connexion à plusieurs tenants simultanément dans une même session.

## 8. Questions ouvertes — à trancher avant `/sc:design`

1. **Fournisseur LLM** :On devra prévoir de pouvoir utiliser Claude (Anthropic, UE possible) ou Mistral (souveraineté FR).
2. **Source du lexique agricole** pour spaCy,on aura acces au schema 'lexicon' de la DB Ekylibre (meme reseau privé Docker).
3. **Modèle spaCy de base** : On va démarrer avec `fr_core_news_lg` puis on prévoiera d'utiliser `fr_dep_news_trf` avec un entraiement pour le NER agricole custom ?
4. **Q&A — voie d'accès aux données** : accès Postgres en lecture seule avec compte dédié qui sera dans le même environnement Docker qu'Ekylibre.
5. **Déploiement** : même infra qu'Ekylibre (dans le même réseau privé).
6. **Persistance conversationnelle** : stocker l'historique au-delà de la session (utile pour amélioration continue / replay).
7. **Évaluation NLU** : On mesurera la qualité avec des jeu de phrases d'or constitué avec des utilisateurs Ekylibre.
8. **Endpoint de validation token côté Ekylibre** : Il faut ajouter la route et le controller `/api/v2/users/me`.

## 9. Étape suivante

→ Lancer `/sc:design` avec ce document en entrée pour produire :
- l'architecture du service Python (couches, modules, dépendances) ;
- le contrat WebSocket (messages JSON, séquences d'échanges) ;
- la stratégie d'intégration spaCy + LLM (orchestration, fallback) ;
- le schéma de déploiement et la gestion des secrets/tokens.
