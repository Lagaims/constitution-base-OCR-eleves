# constitution-base-OCR-eleves

Outils de constitution d'une base d'écrits d'élèves à partir du corpus
[SCOLEDIT](https://scoledit.org) : récupération des scans et des transcriptions,
puis dépôt sur un stockage objet S3 (MinIO sur le SSP Cloud).

Ce travail a été réalisé afin d'entrainer un modèle d'OCR pour extraire les écrits 
d'élèves réalisés dans le cadre des évaluations nationales par niveau du CP au CM2,
sur lesquels travaille la DEPP pour automatiser l'analyse de évaluation de copies d'élèves.

Le paquet expose deux commandes :

- **`scoledit-scraper`** — télécharge les scans (images)
  et les upload sur S3, puis génère un fichier de métadonnées Parquet.
- **`scoledit-annotator`** — extrait les transcriptions TEI des fichiers XML d'un
  corpus local et upload une annotation JSON par scan sur S3.

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

## Architecture du dépôt

```
constitution-base-OCR-eleves/
├── pyproject.toml          # dépendances + déclaration des deux commandes CLI
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
    └── annotator_cli.py    # entrée CLI `scoledit-annotator`
```

Deux chaînes de traitement partagent la configuration S3 (`config.py`) et la
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
