# Workflow — Container Ollama + 3ᵉ LLM sélectionnable par l'utilisateur

> **Statut : PLAN uniquement.** Ce document décrit la stratégie d'implémentation.
> Aucun code n'est modifié ici. Exécuter ensuite avec `/sc:implement` phase par phase.
>
> Stratégie : `systematic` · Profondeur : `deep` · Généré le 2026-05-30

## 1. Objectif

1. **Infra** : ajouter un service `ollama` au `docker compose` de Duke (LLM local, opt-in).
2. **Provider** : ajouter un 3ᵉ `LLMProvider` (`OllamaProvider`) à côté de Claude / Mistral.
3. **Sélection utilisateur** : permettre à l'utilisateur de **choisir** le LLM (Claude / Mistral / Ollama) depuis le widget, par session — capacité qui **n'existe pas aujourd'hui** (le choix est figé par `LLM_DEFAULT_PROVIDER` au démarrage).

## 2. État des lieux (déjà vérifié dans le code)

| Élément | Fichier | Constat |
|---|---|---|
| Contrat provider | `src/duke/nlu/llm/base.py` | `Protocol` : `extract_intervention`, `answer_query` (stream), `health`, `name` |
| Claude / Mistral | `src/duke/nlu/llm/{claude,mistral}.py` | Patron à copier pour Ollama |
| Router | `src/duke/nlu/llm/router.py` | `primary` + `secondary` figés, fallback sur `LLMUnavailableError` |
| Câblage | `src/duke/main.py` `_build_llm_providers` (l.141) | Construit le router **1×** au boot → `app.state` |
| Singletons | `src/duke/transport/ws_server.py` (l.134-135) | `recorder`/`orchestrator` partagés entre sessions |
| Config | `src/duke/config.py` (l.38-44) | `llm_default_provider`, `*_api_key`, `*_model`, `llm_max_tokens_out` |
| Session record | `ws_server.py` (l.159) | `start_session(llm_provider=…)` existe déjà |
| Messages WS | `src/duke/transport/messages.py` | `AuthMessage`, `UserMessage` (`extra="forbid"`), `AuthOkMessage` |
| Compose | `docker/docker-compose.yml` | `duke-api`, `postgres-duke`, réseaux `duke`/`ekylibre` |
| Config widget | Ekylibre `app/controllers/backend/duke_widget_controller.rb#show` | JSON ws_url/token/tenant/locale/stt — à étendre |
| Widget UI | Ekylibre `app/javascript/duke/widget.js` | Webpacker (`compile: true`), recompile via `bin/webpack` (NODE_OPTIONS=--openssl-legacy-provider) |

**Décision structurante** : comme `recorder`/`orchestrator` sont des singletons partagés, la sélection par utilisateur se fait en **passant le nom du provider par appel** (argument `provider`), pas en reconstruisant des instances par session. Le `LLMRouter` devient un **registre multi-provider** qui sait dispatcher par nom avec fallback.

## 3. Décisions techniques — ✅ VERROUILLÉES (Phase 0 close)

| # | Décision | Choix retenu |
|---|---|---|
| D1 | Client Ollama | **httpx brut** (déjà dépendance, zéro churn de `uv.lock`) |
| D2 | Extraction structurée | **`format=<JSON schema>`** d'Ollama (structured outputs ≥ v0.5), réutilise `EXTRACT_INTERVENTION_SCHEMA` |
| D3 | Q&A | **streaming réel** via `/api/chat` `stream:true` (NDJSON) |
| **D4** | **Modèle par défaut** | **`mistral-nemo`** ✅ |
| **D5** | **Compose** | **profile opt-in `local-llm`** + sidecar de `pull` + volume persistant ✅ |
| **D6** | **Surface de sélection** | **champ `llm_provider` sur les messages WS** (`AuthMessage` = défaut session, override optionnel sur `UserMessage`) ; liste renvoyée dans `AuthOkMessage` ✅ |
| **D7** | **Fallback** | **provider choisi → si `LLMUnavailableError`, fallback sur la chaîne par défaut** ✅ |
| D8 | Budget tokens | exempter Ollama de `llm_budget_tokens_per_session` (coût local nul) |

> ✅ Toutes les décisions sont tranchées. Reste à confirmer en Phase 0 : version Ollama (support `format=<schema>`) et **GPU vs CPU** pour le service compose (`mistral-nemo` ≈ 12B → CPU possible mais lent ; GPU recommandé si dispo).

## 4. Phases & tâches

### Phase 0 — Cadrage & décisions (bloquant) — ✅ EN GRANDE PARTIE CLOSE
- [x] Trancher D1–D8 → cf. §3 (D4=`mistral-nemo`, D5=profile opt-in, D6=champ WS, D7=fallback défaut).
- [ ] Vérifier que l'image `ollama/ollama` visée supporte `format=<schema>` (structured outputs) avec `mistral-nemo`.
- [ ] Choisir **GPU vs CPU** pour le service compose (`mistral-nemo` ~12B : GPU recommandé ; CPU = `deploy.resources` mémoire ↑ + latence). → impacte `deploy`/`runtime` du service `ollama`.
- **Checkpoint** : ✅ décisions consignées, modèle cible figé (`mistral-nemo`). Reste : trancher GPU/CPU.

### Phase 1 — Provider Ollama (backend, isolé, testable seul)
Dépend de : Phase 0.
- [ ] `src/duke/config.py` : ajouter `ollama_base_url: str = "http://ollama:11434"`, `ollama_model: str = "mistral-nemo"`, et étendre la sémantique de `llm_default_provider` (accepter `"ollama"`).
- [ ] `src/duke/nlu/llm/ollama.py` (nouveau) : `class OllamaProvider` (`name="ollama"`), `from_config(base_url, model, max_tokens)` :
  - `extract_intervention` → `POST {base}/api/chat` avec `messages=[system, user]`, `format=EXTRACT_INTERVENTION_SCHEMA`, `stream:false` → parse JSON ; lever `LLMSchemaError`/`LLMUnavailableError` comme Claude/Mistral.
  - `answer_query` → `POST {base}/api/chat` `stream:true` → yield des deltas (`message.content`) ; mapper erreurs httpx → `LLMUnavailableError`.
  - `health` → `GET {base}/api/tags` (ou `/`), `True` si 200.
  - Réutiliser `prompts.py` (`EXTRACT_INTERVENTION_SYSTEM`, `ANSWER_QUERY_SYSTEM`, builders) et `tools.py` (schéma) — **aucune divergence de prompt** entre providers.
- [ ] Tests unitaires `tests/unit/test_ollama_provider.py` : httpx mocké (extraction OK, JSON invalide → `LLMSchemaError`, 5xx/timeout → `LLMUnavailableError`, stream Q&A).
- **Checkpoint** : `uv run pytest tests/unit/test_ollama_provider.py` vert ; provider conforme au Protocol.

### Phase 2 — Router multi-provider + threading de la sélection (backend)
Dépend de : Phase 1.
- [ ] `src/duke/nlu/llm/router.py` : refactor en **registre**.
  - Constructeur : `providers: dict[str, LLMProvider]` + `default_order: list[str]`.
  - `extract_intervention(text, hints, provider: str | None = None)` : tente `provider` si fourni & dispo, sinon `default_order` ; conserve le fallback sur `LLMUnavailableError` (D7).
  - `answer_query(question, evidence, provider=None)` : idem en streaming.
  - `available() -> list[str]` (providers configurés & sains) pour exposer au widget.
  - **Compat ascendante** : garder `primary`/`secondary` ou adapter `main.py` (cf. Phase 3).
- [ ] `src/duke/application/intervention_recorder.py` : `draft_from_text(..., provider: str | None = None)` → passe `provider` à `self._llm.extract_intervention`.
- [ ] `src/duke/application/query_answerer.py` : propager `provider` jusqu'à `answer_query`.
- [ ] `src/duke/application/orchestrator.py` : `handle(text, tenant_schema, provider=None)` → propage vers recorder/query_answerer.
- [ ] Tests : `tests/unit/test_router.py` (sélection par nom, fallback, provider inconnu → défaut), maj des fakes existants si signature changée.
- **Checkpoint** : suite unitaire complète verte (`uv run pytest tests/unit/`), pas de régression de signature non gérée.

### Phase 3 — Câblage applicatif (main + session)
Dépend de : Phase 2.
- [ ] `src/duke/main.py` `_build_llm_providers` : construire les 3 providers configurés (clé API présente / Ollama joignable) → registre `{name: provider}` + `default_order` dérivé de `llm_default_provider`. Conserver `_NullLLMProvider` si aucun.
- [ ] `src/duke/transport/messages.py` :
  - `AuthMessage` : ajouter `llm_provider: str | None = None` (défaut session).
  - `UserMessage` : ajouter `llm_provider: str | None = None` (override ponctuel, optionnel).
  - `AuthOkMessage` : ajouter `available_providers: list[str]` + `selected_provider: str`.
- [ ] `src/duke/transport/ws_server.py` :
  - À l'auth : valider le provider demandé contre `router.available()`, sinon défaut ; stocker sur `_SessionContext` (nouveau champ `llm_provider`).
  - `start_session(llm_provider=<choisi>)` (l.159) au lieu du défaut global.
  - Renseigner `AuthOkMessage.available_providers/selected_provider`.
  - Passer `ctx.llm_provider` (ou l'override `UserMessage`) à `orchestrator.handle(...)` et aux ré-extractions clarify (`recorder.draft_from_text(..., provider=…)`).
- **Checkpoint** : démarrage OK ; `auth_ok` liste les providers ; une saisie route vers le provider choisi (vérifiable via log `intervention.extracted provider=…`).

### Phase 4 — Infra docker compose (Ollama)
Dépend de : Phase 1 (URL/most de config) — parallélisable avec Phases 2-3.
- [ ] `docker/docker-compose.yml` :
  - Service `ollama` : `image: ollama/ollama`, volume `ollama-models:/root/.ollama`, port `11434:11434`, réseau `duke`, `healthcheck` (`ollama list` / `GET /api/tags`), `restart: unless-stopped`, `profiles: ["local-llm"]`. GPU optionnel (cf. Phase 0).
  - Sidecar one-shot `ollama-pull` (D5) : `ollama pull mistral-nemo` puis exit, `depends_on: ollama (healthy)`, même profile.
  - `duke-api.depends_on` : ajouter `ollama: { condition: service_started }` (sous profile, ne pas casser le run sans LLM local).
  - Nouveau volume `ollama-models`.
- [ ] `.env` (exemple/README) : `OLLAMA_BASE_URL`, `OLLAMA_MODEL`.
- **Checkpoint** : `docker compose --profile local-llm up` démarre Ollama, le modèle est pull, `GET /api/tags` OK depuis `duke-api`.

### Phase 5 — Sélecteur dans le widget (Ekylibre)
Dépend de : Phase 3 (`AuthOkMessage.available_providers`).
- [ ] `app/javascript/duke/widget.js` :
  - À réception d'`auth_ok` : mémoriser `available_providers`/`selected_provider`, rendre un **menu déroulant** (libellés FR : « Claude », « Mistral », « Ollama (local) »).
  - Au changement : renvoyer le choix (ré-`auth` léger **ou** champ `llm_provider` sur les `user_message` suivants — cohérent avec D6) ; persister par session.
  - I18N : ajouter le libellé du sélecteur (cf. patron `fieldTools`/`fieldDoers`).
- [ ] (Optionnel) `DukeWidgetController#show` + test contrôleur : exposer `default_llm_provider`/liste si la source de vérité doit venir d'Ekylibre plutôt que de Duke (sinon Duke via `auth_ok` suffit — privilégié).
- [ ] Recompiler le pack : `docker exec -e NODE_OPTIONS=--openssl-legacy-provider app bin/webpack`.
- **Checkpoint** : le sélecteur apparaît, change le provider, une saisie part bien vers le LLM choisi (logs Duke).

### Phase 6 — Tests, docs, durcissement
Dépend de : Phases 1-5.
- [ ] Marqueur opt-in `ollama_smoke` dans `pyproject.toml` (patron `stt_smoke`) + test `RUN_OLLAMA_SMOKE=1` qui tape un vrai Ollama.
- [ ] `uv run pytest` complet ; `uv run ruff check` ; `uv lock --check` (si dépendance ajoutée en D1).
- [ ] Docs : `README.md` (profile `local-llm`, pull du modèle), `ARCHITECTURE.md` (3ᵉ provider + sélection par session), `CLAUDE.md` (tableau toggles : `OLLAMA_BASE_URL`/`OLLAMA_MODEL`, profile compose), `.env` exemple.
- [ ] Test contrôleur Ekylibre si `#show` modifié (asserts `expected_keys`).
- **Checkpoint** : suite verte, lint clean, lock à jour, docs alignées.

## 5. Graphe de dépendances

```
Phase 0 (décisions)
        │
        ▼
Phase 1 (OllamaProvider) ──────────────┐
        │                              │
        ▼                              ▼
Phase 2 (router + threading)     Phase 4 (compose Ollama)   ← parallélisable
        │
        ▼
Phase 3 (main + session WS)
        │
        ▼
Phase 5 (widget Ekylibre)
        │
        ▼
Phase 6 (tests + docs)
```

Chemin critique : 0 → 1 → 2 → 3 → 5 → 6. Phase 4 en parallèle dès la fin de Phase 1.

## 6. Fichiers touchés (récap)

**Duke (nouveaux)** : `src/duke/nlu/llm/ollama.py`, `tests/unit/test_ollama_provider.py`, `tests/unit/test_router.py`, `claudedocs/workflow_ollama_third_llm.md`.
**Duke (modifiés)** : `config.py`, `nlu/llm/router.py`, `application/{intervention_recorder,query_answerer,orchestrator}.py`, `transport/{messages,ws_server}.py`, `main.py`, `docker/docker-compose.yml`, `pyproject.toml` (si D1=pkg ou marqueur), `.env`, `README.md`, `ARCHITECTURE.md`, `CLAUDE.md`.
**Ekylibre** : `app/javascript/duke/widget.js` (+ `duke_widget_controller.rb` & test si D6 via Ekylibre).

## 7. Risques & mitigations

| Risque | Impact | Mitigation |
|---|---|---|
| Tool/JSON-calling faible sur petits modèles locaux | Extraction dégradée | Utiliser `format=<schema>` (D2) ; fallback existant `_draft_from_nlu_only` + ambiguities ; documenter modèles recommandés |
| Pull du modèle long / image lourde | DX au 1ᵉʳ run | Profile `local-llm` opt-in (D5) + sidecar `pull` + volume persistant |
| Changement de signature `handle/draft_from_text/answer_query` | Casse fakes de tests | Param `provider` **optionnel** (`=None`) → rétro-compatible ; MAJ ciblée des fakes |
| État partagé sur singletons | Fuite de provider entre sessions | Sélection passée **par appel** (aucun état mutable partagé) |
| Provider choisi indisponible | Échec utilisateur | Fallback chaîne par défaut (D7) + validation contre `available()` à l'auth |
| `extra="forbid"` sur messages | Rejet si champ inconnu | Ajouter explicitement `llm_provider` aux schémas avant que le widget l'émette |
| GPU absent | Lenteur Ollama | CPU par défaut documenté ; modèle léger en option |

## 8. Validation finale (Definition of Done)
- [ ] `docker compose --profile local-llm up` : Ollama healthy, modèle présent.
- [ ] `auth_ok` renvoie `available_providers` incluant `ollama` quand configuré.
- [ ] Sélecteur widget fonctionnel ; saisie d'intervention **et** Q&A routées vers le provider choisi (logs `provider=ollama`).
- [ ] Fallback OK si le provider choisi tombe.
- [ ] `uv run pytest` + `uv run ruff check` + `uv lock --check` verts ; docs à jour.

---
**Prochaine étape** : `/sc:implement claudedocs/workflow_ollama_third_llm.md` (commencer par Phase 0 puis Phase 1).
