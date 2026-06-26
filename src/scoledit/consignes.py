"""Extraction et classification de la consigne donnée à l'élève.

Chaque copie du corpus (SCOLEDIT, Resolco, Ecriscol) décrit, dans l'en-tête TEI
(`<profileDesc>/<textDesc>/<factuality>`), la **consigne** de l'exercice. Ce module
lit cette consigne et en déduit le **type d'exercice** :

    - "ecriture_libre" : production libre (raconter / inventer / écrire une histoire) ;
    - "dictee"         : l'élève écrit un texte qui lui est dicté ;
    - "recopie"        : l'élève recopie / reproduit un texte donné ;
    - "indetermine"    : consigne absente ou non reconnue.

Cette information est décisive pour le pipeline evaluation_dictee : seules les
**dictées** (et la recopie) disposent d'un *texte de référence* permettant un codage
mot à mot par item ; l'écriture libre n'en a pas.

L'attribut TEI `<derivation>` est conservé comme signal secondaire : `original` =
production propre de l'élève (cohérent avec l'écriture libre), une valeur dérivée
(`quotation`, `translation`…) signalerait un texte reproduit.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path

# Niveau lisible depuis le dossier « Grade_06_(CM2) » -> « CM2 ».
_LEVEL_DIR_RE = re.compile(r"Grade_\d+_\(([^)]+)\)$")
# Identifiant élève SCOLEDIT depuis « EC-CM2-2018-102-D1-S199-V1.xml » -> « 199 ».
_STUDENT_ID_RE = re.compile(r"-S(\d+)-")

# Règles de classification, évaluées dans l'ordre : la première qui matche gagne.
# La recopie et la dictée (plus spécifiques) sont testées avant l'écriture libre.
CONSIGNE_RULES: list[tuple[str, re.Pattern[str]]] = [
    (
        "recopie",
        re.compile(
            r"recopi|reprodui|copie[rz]?\b|copier?\s+le\s+texte|recopie[rz]?\s+le\s+texte",
            re.IGNORECASE,
        ),
    ),
    (
        "dictee",
        re.compile(
            r"dict[ée]e|dicter|sous\s+la\s+dict|"
            r"[ée]cri\w*\s+ce\s+que\s+(je|j['’]on|l['’]on)\s+(dis|dit|lis)|"
            r"[ée]cri\w*\s+ce\s+que\s+vous\s+entendez",
            re.IGNORECASE,
        ),
    ),
    (
        "ecriture_libre",
        re.compile(
            r"racont|invent|imagine|r[ée]dige|"
            r"[ée]cri\w*.{0,40}(histoire|texte|r[ée]cit)|"
            r"(une|l['’]|votre|cette|ton)\s+histoire",
            re.IGNORECASE,
        ),
    ),
]


def classify_consigne(consigne: str) -> str:
    """Déduit le type d'exercice d'un texte de consigne."""
    if not consigne or not consigne.strip():
        return "indetermine"
    for exercise_type, pattern in CONSIGNE_RULES:
        if pattern.search(consigne):
            return exercise_type
    return "indetermine"


def _text_of(el: ET.Element) -> str:
    """Texte concaténé d'un élément, espaces normalisés."""
    return re.sub(r"\s+", " ", "".join(el.itertext())).strip()


def extract_consigne(root: ET.Element) -> str:
    """Retourne le texte de la consigne (`<factuality>`), ou une chaîne vide."""
    fact = root.find(".//factuality")
    return _text_of(fact) if fact is not None else ""


def extract_derivation(root: ET.Element) -> str:
    """Retourne l'attribut `type` de `<derivation>` (signal secondaire)."""
    der = root.find(".//derivation")
    return der.get("type", "") if der is not None else ""


@dataclass
class ConsigneRecord:
    """Classification d'une copie d'après sa consigne.

    Attributes:
        file: chemin relatif du XML dans le corpus.
        subcorpus: sous-corpus (Scoledit, Resolco, Ecriscol…).
        level: niveau scolaire (CP, CE1…).
        student_id: identifiant élève si présent dans le nom de fichier.
        exercise_type: ecriture_libre / dictee / recopie / indetermine.
        derivation: attribut TEI `<derivation type>` (original, quotation…).
        consigne: texte intégral de la consigne.
    """

    file: str
    subcorpus: str
    level: str
    student_id: str
    exercise_type: str
    derivation: str
    consigne: str


def classify_file(xml_path: Path, corpus_root: Path) -> ConsigneRecord | None:
    """Lit un XML du corpus et le classe d'après sa consigne (None si illisible)."""
    try:
        root = ET.parse(xml_path).getroot()
    except ET.ParseError:
        return None

    rel = xml_path.relative_to(corpus_root)
    parts = rel.parts
    subcorpus = parts[1] if len(parts) >= 3 else ""
    level_match = _LEVEL_DIR_RE.match(parts[0]) if parts else None
    level = level_match.group(1) if level_match else ""
    id_match = _STUDENT_ID_RE.search(xml_path.name)

    consigne = extract_consigne(root)
    return ConsigneRecord(
        file=str(rel),
        subcorpus=subcorpus,
        level=level,
        student_id=id_match.group(1) if id_match else "",
        exercise_type=classify_consigne(consigne),
        derivation=extract_derivation(root),
        consigne=consigne,
    )


def classify_corpus(corpus_path: Path) -> list[ConsigneRecord]:
    """Classe tous les XML d'un corpus, triés par chemin."""
    records: list[ConsigneRecord] = []
    for xml_path in sorted(corpus_path.rglob("*.xml")):
        record = classify_file(xml_path, corpus_path)
        if record is not None:
            records.append(record)
    return records
