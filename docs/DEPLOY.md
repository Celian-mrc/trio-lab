# Déploiement Railway 24/24 (Phase 6)

Deux services sur le même repo GitHub (`Celian-mrc/trio-lab`) + le Postgres
Railway existant. Le builder Railway (Railpack) détecte Python via
`.python-version` et installe le paquet grâce à `requirements.txt` (qui
contient juste `.` : le projet + ses dépendances depuis `pyproject.toml` —
sans ce fichier, Railpack ne lance aucun install et les services crashent en
`No module named 'trio_lab'`).

## Checklist (dashboard Railway, ~10 min)

Dans le projet Railway qui contient déjà le Postgres :

### 1. Service « collector »

1. **New → GitHub Repo** → choisir `Celian-mrc/trio-lab`, branche `main`.
2. Renommer le service `collector` (Settings → Service name).
3. Settings → **Deploy → Custom Start Command** :
   `python -m trio_lab.collector --service`
4. Variables (onglet Variables) :
   - `RIOT_API_KEY` = ta clé projet
   - `DATABASE_URL` = **référence** au Postgres du projet : cliquer
     « Add Reference » → Postgres → `DATABASE_URL` (URL interne, trafic privé
     gratuit — ne pas coller l'URL publique)
   - `ARCHIVE_TIMELINES` = `0` (filesystem éphémère)
   - `LOG_LEVEL` = `INFO`
5. Deploy. Vérifier dans les logs : découverte des joueurs puis
   `cycle 1 : batch 16.xx (5000/plateforme)`.

### 2. Service « web »

1. **New → GitHub Repo** → le même repo, branche `main`.
2. Renommer `web`.
3. Custom Start Command : `python -m trio_lab.web`
   (Railway injecte `$PORT`, l'app écoute dessus).
4. Variables : `DATABASE_URL` (même référence Postgres), `LOG_LEVEL=INFO`.
5. Settings → **Networking → Generate Domain** → l'URL publique de
   l'interface (accès perso).

### 3. Vérifications

- `https://<domaine-web>/api/status` : volume par jour/plateforme, erreurs du
  journal, dernier match ingéré.
- Les migrations sont déjà appliquées sur ce Postgres depuis le poste local ;
  pour une base neuve il faudrait `python -m trio_lab.db` une fois.
- Auto-deploy : chaque push sur `main` redéploie les deux services (le
  collector reprend proprement — tout l'état est en base).

## Comportement du service collector

- Patch courant résolu à chaque cycle via Data Dragon : le passage 16.13 →
  16.14 ne demande **aucune intervention**. Si le patch manque dans
  `PATCH_DATES`, bornes de repli (fenêtre glissante) + warning dans les logs —
  ajouter les dates officielles à l'occasion pour économiser des appels.
- Après chaque batch (5 000 matchs/plateforme, `--target` pour changer) :
  refresh agrégats + scores de synergie + counters sur la fenêtre des patchs
  en base (≤ 3) — l'interface se met à jour plusieurs fois par jour.
- Purge de rétention quotidienne : matchs des patchs au-delà des 3 plus
  récents supprimés (cascade) ; `agg_*`, `score_*` et le journal conservés.
- Les 429 et erreurs sont visibles dans les logs Railway du collector
  (`rate restant`, `erreur de boucle`, compteurs de fin de batch).

## Monitoring

- `GET /api/status` (service web) : matchs/jour sur 7 jours par plateforme,
  total, dernier match, compteurs du journal (excluded / error_retryable /
  error_permanent).
- Logs Railway : chaque plateforme logge tous les 50 matchs + un résumé par
  batch ; la purge logge les patchs supprimés.
