# constitution-base-OCR-eleves

Outils de constitution d'une base d'écrits d'élèves à partir du corpus
[SCOLEDIT](https://scoledit.org) : récupération des scans et des transcriptions,
puis dépôt sur un stockage objet S3 (MinIO sur le SSP Cloud).

Ce travail a été réalisé afin d'entrainer un modèle d'OCR pour extraire les écrits 
d'élèves réalisés dans le cadre des évaluations nationales par niveau du CP au CM2,
sur lesquels travaille la DEPP pour automatiser l'analyse de évaluation de copies d'élèves.

Le paquet expose quatre commandes :

- **`scoledit-scraper`** — télécharge les scans (images)
  et les upload sur S3, puis génère un fichier de métadonnées Parquet.
- **`scoledit-annotator`** — extrait les transcriptions TEI des fichiers XML d'un
  corpus local et upload une annotation JSON par scan sur S3.
- **`scoledit-scorer`** — note « erreur / pas erreur » chaque mot des transcriptions
  et dépose une notation JSON par copie + un CSV agrégé, exploitable par le pipeline
  [`evaluation_dictee`](../evaluation_dictee/).
- **`scoledit-consignes`** — extrait la consigne TEI de chaque copie et classe
  l'exercice (écriture libre / dictée / recopie) dans un manifeste CSV.

## Variables d'environnement

La configuration S3 se fait par variables d'environnement (un fichier `.env` est
chargé s'il est présent). Sur le SSP Cloud, les identifiants AWS sont en général
déjà injectés dans l'environnement.

| Variable | Requis | Défaut | Description |
|---|---|---|---|
| `S3_BUCKET` | non | `projet-production-ecrits-depp` | Bucket S3 cible. |
| `S3_PREFIX` | non | `scoledit/scans` | Préfixe sous lequel sont déposés les scans. Les annotations sont déposées sous `<racine>/annotation` (ex. `scoledit/annotation`). |
| `AWS_ACCESS_KEY_ID` | oui* | — | Clé d'accès AWS/MinIO. |
| `AWS_SECRET_ACCESS_KEY` | oui* | — | Clé secrète AWS/MinIO. |
| `AWS_SESSION_TOKEN` | oui* | — | Jeton de session (nécessaire sur le SSP Cloud). |
| `AWS_DEFAULT_REGION` | non | `us-east-1` | Région AWS. |
| `AWS_ENDPOINT_URL` | non | — | URL de l'endpoint S3 (ex. `https://minio.lab.sspcloud.fr`). `AWS_S3_ENDPOINT` est accepté en alternative. |

\* Sur le SSP Cloud, ces trois identifiants AWS sont nécessaires et habituellement
fournis automatiquement par l'environnement.

Exemple de fichier `.env` :

```ini
# Cible S3
S3_BUCKET=projet-production-ecrits-depp
S3_PREFIX=scoledit/scans

# Identifiants AWS (sur le SSP Cloud les 3 variables sont nécessaires)
# AWS_ACCESS_KEY_ID=
# AWS_SECRET_ACCESS_KEY=
# AWS_SESSION_TOKEN=
# AWS_DEFAULT_REGION=us-east-1
# AWS_ENDPOINT_URL=https://minio.lab.sspcloud.fr
```

## Installation

Le projet utilise [uv](https://docs.astral.sh/uv/). Depuis `constitution-base-OCR-eleves/` :

```bash
uv sync          # crée le venv et installe les dépendances
```

Les commandes ci-dessous peuvent être lancées via `uv run <cmd>` (sans activer le
venv) ou directement après `source .venv/bin/activate`.

## Utilisation (CLI)

Configurer d'abord l'accès S3 (voir [Variables d'environnement](#variables-denvironnement)).

### `scoledit-scraper` — récupération des scans

Télécharge les scans et les upload sur S3, puis écrit l'index
`scoledit/metadata.parquet`. Reprend là où il s'est arrêté (les scans déjà
présents sur S3 sont ignorés).

```bash
uv run scoledit-scraper
```

Sortie S3 : `s3://<bucket>/<prefix>/<niveau>/<id><lettre>.jpg` (ex. `scoledit/scans/CP/199a.jpg`).

### `scoledit-annotator` — extraction des transcriptions XML

Lit un corpus TEI local, extrait la transcription de chaque copie et upload une
annotation JSON par scan sur S3. Les annotations déjà présentes sont ignorées.

```bash
uv run scoledit-annotator --corpus ./Corpus       # défaut : ./Corpus
uv run scoledit-annotator --corpus ./Corpus --log-level DEBUG
```

| Option | Défaut | Description |
|---|---|---|
| `--corpus <chemin>` | `Corpus` | Répertoire racine du corpus (structure `Grade_NN_(NIVEAU)/Scoledit/`). |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR`. |

Sortie S3 : une annotation par scan sous
`s3://<bucket>/<racine>/annotation/<niveau>/<id><lettres>.json`. Le nom reprend
l'identifiant du scan : `199a.json` pour une page simple, `1318a/b/c.json` quand
la copie est découpée par page (`<pb/>`), et un fichier groupé `677ab.json`
lorsqu'une transcription couvre plusieurs scans sans découpage fiable.

> L'annotator a besoin que les scans soient déjà sur S3 : il liste
> `s3://<bucket>/<prefix>/` pour connaître les lettres (a-e) de chaque élève et
> aligner les annotations sur les scans. Lancer `scoledit-scraper` d'abord.

### `scoledit-scorer` — notation mot à mot des transcriptions

Lit les annotations TEI sous `scoledit/annotation/<niveau>/<id>.json`, reconstruit
pour chaque copie son **état final** (on conserve le contenu des `<add>` et on retire
celui des `<del>`/`<gap/>`, cf. décision D3 d'evaluation_dictee) et attribue à chaque
mot un code inspiré de la grille simplifiée (evaluation_dictee, décision D2) :

| Code | Signification |
|---|---|
| `1` | mot présent et orthographiquement correct (présent dans le lexique français) |
| `9` | **erreur** : mot hors lexique (faute), mot raturé (« en trop »), ou segment illisible (`<gap/>`, `<unclear>`) |
| `0` | mot absent (omission signalée explicitement par le balisage) |

La méthode est **hybride** : l'orthographe vient d'un dictionnaire français hors-ligne
([`pyspellchecker`](https://pypi.org/project/pyspellchecker/), ~140 000 formes), les
mots absents / en trop / illisibles viennent du balisage TEI.

**Noms propres** : un mot hors lexique mais **capitalisé et en milieu de phrase** est
considéré comme un nom propre probable → noté `1` et marqué `nom_propre_possible` ;
sinon (minuscule, ou capitale en début de phrase) il reste noté `9`. La liste
`--names` permet d'accepter explicitement les noms récurrents (personnages, lieux),
quelle que soit la casse.

> **Portée : orthographe lexicale, pas la grammaire.** Chaque mot est vérifié
> *isolément* contre le lexique. Le scorer attrape donc les fautes qui produisent un
> **mot inexistant** (« mangait », « inteligent ») — proche du code 3 (lexical) de la
> grille DEPP — mais **pas** les fautes de **conjugaison/accord** dont la forme reste
> un mot valide hors contexte (« tu mange », « les chien », « j'ai manger »), qui
> relèvent des codes 4/5 (grammatical). L'analyse grammaticale, qui exige le contexte,
> est laissée au modèle VLM/LLM en aval (méthode C d'`evaluation_dictee`). Ce scorer
> sert de **baseline orthographique déterministe**, pas de correcteur grammatical.

```bash
uv run scoledit-scorer                          # toutes les copies -> S3
uv run scoledit-scorer --level CM2 --limit 20    # un échantillon pour tester
uv run scoledit-scorer --names noms_propres.txt  # liste de mots acceptés en plus
uv run scoledit-scorer --no-s3 --local-dir ./notation   # écrire en local seulement
```

| Option | Défaut | Description |
|---|---|---|
| `--level <NIV>` | tous | Niveau(x) à traiter (répétable : `--level CM1 --level CM2`). |
| `--limit <n>` | tout | Nombre maximal de copies (utile pour un test rapide). |
| `--names <fichier>` | — | Mots supplémentaires acceptés (noms propres récurrents, 1 par ligne). |
| `--local-dir <dossier>` | — | Écrit aussi les notations dans ce dossier local. |
| `--no-s3` | écrit sur S3 | N'écrit rien sur S3 (combiné à `--local-dir` si pas de droits d'écriture). |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR`. |

**Sorties** (même arborescence que les annotations en entrée) :

- `s3://<bucket>/scoledit/notation/<niveau>/<id>.json` — une notation détaillée par
  copie : texte de l'état final, compteurs (`n_mots`, `n_erreurs`…) et la liste des
  tokens (`mot`, `forme_normalisee`, `code`, `categorie`, `source`, `details`).
- `s3://<bucket>/scoledit/notation/notation.csv` — CSV agrégé `;`-séparé en **format
  long** (une ligne par mot : `scan;level;student_id;position;mot;forme_normalisee;code;categorie;source;details`),
  directement lisible côté `evaluation_dictee` (regrouper par `scan`).

> **À garder en tête.** Le corpus SCOLEDIT est de l'écrit *libre*, pas une dictée à
> texte de référence : les transcriptions ne contiennent aucune correction
> orthographique. La justesse est donc *estimée* par comparaison au lexique
> (volontairement bruitée : noms propres, conjugaisons rares, élisions). Le champ
> `source` distingue `dictionnaire` (orthographe estimée) de `balise_tei` (issu du
> balisage, fiable). Le code `0` (mot absent) n'apparaît qu'en présence d'un signal
> d'omission explicite, rare dans de l'écrit libre.

### `scoledit-consignes` — type d'exercice (libre / dictée / recopie)

Parcourt le corpus local, lit la consigne de chaque copie (en-tête TEI
`<profileDesc>/<textDesc>/<factuality>`) et en déduit le **type d'exercice** :

| Type | Détection (mots-clés de la consigne) |
|---|---|
| `ecriture_libre` | raconter, inventer, imaginer, rédiger, « écrire une histoire/un texte »… |
| `dictee` | dictée, dicter, « sous la dictée », « écris ce que je dis »… |
| `recopie` | recopier, reproduire, « copie le texte »… |
| `indetermine` | consigne absente ou non reconnue |

Cette information conditionne l'usage côté `evaluation_dictee` : seules les **dictées**
(et la recopie) disposent d'un *texte de référence* permettant un codage mot à mot
par item ; l'écriture libre n'en a pas.

```bash
uv run scoledit-consignes --corpus ./Corpus --out consignes.csv   # manifeste local
uv run scoledit-consignes --corpus ./Corpus --s3                  # + upload S3
```

| Option | Défaut | Description |
|---|---|---|
| `--corpus <chemin>` | `Corpus` | Répertoire racine du corpus. |
| `--out <fichier>` | — | Écrit le manifeste CSV (`;`-séparé) localement. |
| `--s3` | non | Upload aussi le manifeste sous `scoledit/consignes.csv`. |
| `--log-level` | `INFO` | `DEBUG`, `INFO`, `WARNING` ou `ERROR`. |

Colonnes du manifeste : `file;subcorpus;level;student_id;exercise_type;derivation;consigne`.

> **Résultat sur le corpus actuel.** Les **2078** copies (SCOLEDIT CP→CM2, Resolco,
> du CP à la 3e) sont **toutes de l'écriture libre** (production narrative :
> « raconte une histoire », « écris l'histoire d'un petit chat »). On n'y trouve
> **aucune dictée ni recopie** — cohérent avec `<derivation type="original"/>`
> présent sur 100 % des copies. La dictée évaluée par la DEPP dans `evaluation_dictee`
> provient d'un **autre jeu de données** (imagettes + `resultat_dictee_2015.csv`),
> pas de ce corpus SCOLEDIT.

## Architecture du dépôt

```
constitution-base-OCR-eleves/
├── pyproject.toml          # dépendances + déclaration des quatre commandes CLI
├── .env                    # configuration S3 (non versionné)
└── src/scoledit/
    ├── config.py           # constantes (URLs, concurrence, retries) + S3Config / load_config
    ├── models.py           # dataclasses : StudentEntry, ScanInfo, ScanRecord
    ├── storage.py          # client S3 (boto3) : listing, upload images, métadonnées Parquet
    │
    ├── scraper.py          # parsing HTML SCOLEDIT (corpus.php, production.php) → ScanInfo
    ├── pipeline.py         # orchestration async du scraping + téléchargement + upload
    ├── __main__.py         # entrée CLI `scoledit-scraper`
    │
    ├── annotator.py        # extraction TEI des XML, découpage par page (<pb/>), upload JSON
    ├── annotator_cli.py    # entrée CLI `scoledit-annotator`
    │
    ├── scorer.py           # notation mot à mot : TEI -> état final -> codes 1/9/0 (logique pure)
    ├── scorer_cli.py       # entrée CLI `scoledit-scorer` (lecture S3, JSON + CSV)
    │
    ├── consignes.py        # extraction de la consigne TEI + classement du type d'exercice
    └── consignes_cli.py    # entrée CLI `scoledit-consignes` (manifeste CSV)
```

Quatre chaînes de traitement partagent la configuration S3 (`config.py`) et la
couche de stockage (`storage.py`) :

- **Scraper** : `__main__` → `pipeline` → `scraper` (HTML) + `storage` (S3).
  Les scans sont déposés sous `s3://<bucket>/<prefix>/<niveau>/<fichier>` et un
  index `scoledit/metadata.parquet` est produit en fin de traitement.
- **Annotator** : `annotator_cli` → `annotator`, qui lit un corpus local
  (`Corpus/Grade_NN_(NIVEAU)/Scoledit/EC-…-S<id>-V<n>.xml`), indexe les scans S3
  pour connaître les lettres de chaque élève, découpe la transcription par page
  (`<pb/>`) et dépose une annotation TEI par scan sous
  `s3://<bucket>/<racine>/annotation/<niveau>/<id><lettres>.json`. Seuls les
  niveaux Scoledit (CP→CM2) sont traités.
- **Scorer** : `scorer_cli` → `scorer`, qui lit les annotations sous
  `scoledit/annotation/`, reconstruit l'état final de chaque copie, note chaque mot
  (lexique français + balisage TEI) et dépose une notation par copie sous
  `scoledit/notation/<niveau>/<id>.json` ainsi qu'un CSV agrégé
  `scoledit/notation/notation.csv`. La logique de notation (`scorer.py`) est pure et
  testée hors S3/réseau (`tests/test_scorer.py`).
- **Consignes** : `consignes_cli` → `consignes`, qui lit le corpus local, extrait la
  consigne TEI (`<factuality>`) de chaque copie, en déduit le type d'exercice
  (écriture libre / dictée / recopie) et écrit le manifeste `scoledit/consignes.csv`.
  La logique de classification (`consignes.py`) est pure et testée
  (`tests/test_consignes.py`).
