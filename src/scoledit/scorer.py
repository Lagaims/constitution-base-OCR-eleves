"""Notation « erreur / pas erreur » mot à mot des transcriptions SCOLEDIT.

À partir des annotations TEI déposées sur S3 par `scoledit-annotator`
(`scoledit/annotation/<niveau>/<id>.json`), ce module produit, pour chaque copie,
un codage de chaque mot inspiré de la grille simplifiée du projet evaluation_dictee
(cf. evaluation_dictee/docs/decisions.md, décision D2) :

    1  = mot présent et orthographiquement correct (présent dans le lexique français)
    9  = erreur :
           - mot présent mais hors lexique (faute d'orthographe),
           - mot écrit puis raturé par l'élève (« mot en trop »),
           - segment illisible (<gap/>, <unclear>) ;
    0  = mot absent (omission signalée explicitement par le balisage).

Méthode (hybride, choix utilisateur) :
  - l'**orthographe** vient d'un dictionnaire français hors-ligne (pyspellchecker) ;
  - les **mots absents / en trop / illisibles** viennent du balisage TEI SCOLEDIT.

Portée — orthographe lexicale, PAS la grammaire :
  Chaque mot est vérifié *isolément* contre le lexique. Le scorer attrape donc les
  fautes produisant un mot inexistant (« mangait », « inteligent ») — proche du code 3
  (lexical) de la grille DEPP — mais PAS les fautes de conjugaison/accord dont la forme
  reste un mot valide hors contexte (« tu mange », « les chien », « j'ai manger »),
  qui relèvent des codes 4/5 (grammatical). L'analyse grammaticale, qui suppose le
  contexte, est laissée au modèle VLM/LLM en aval (méthode C d'evaluation_dictee).
  Ce module est une baseline orthographique déterministe, pas un correcteur grammatical.

Important — nature des données :
  Le corpus SCOLEDIT est de l'écrit *libre* (pas une dictée à texte de référence).
  Les annotations sont des *transcriptions fidèles* avec le balisage du processus
  d'écriture de l'élève (ratures `<mod>/<del>/<gap>`, ajouts `<add>`, passages
  illisibles `<unclear>`). Elles ne contiennent aucune correction orthographique :
  la justesse orthographique est donc *estimée* par comparaison au lexique, ce qui
  est volontairement bruité (noms propres, conjugaisons rares…). Le détail complet
  par token est conservé dans le JSON de sortie pour permettre toute reclassification.

Règle de l'« état final » (décision D3 d'evaluation_dictee) : quand l'élève rature et
réécrit, on lit la version finale. Concrètement on conserve le contenu des `<add>` et
on retire celui des `<del>`/`<gap/>` avant d'évaluer l'orthographe.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field

# Sentinelle interne représentant un <gap/> (mot/segment illisible ou effacé) dans
# le flux de caractères reconstruit. Choisie hors de tout texte réel.
_GAP = "￿"

# Clitiques élidés du français : « l'arbre », « d'une », « qu'il », « s'appelait »…
# On considère le mot correct si, après l'apostrophe, la tête lexicale est connue.
CLITICS = frozenset(
    {"l", "d", "j", "m", "t", "s", "n", "c", "qu",
     "lorsqu", "puisqu", "jusqu", "quoiqu", "quelqu", "presqu", "aujourd"}
)
# Mots à apostrophe interne soudés (la tête lexicale n'est pas un mot autonome).
ELISIONS_SOUDEES = frozenset({"aujourd'hui"})

# Caractères considérés comme « lettre ou chiffre » (français accentué inclus).
_WORDCHAR = r"0-9A-Za-zÀ-ÖØ-öø-ÿ"
_STRIP_LEFT = re.compile(rf"^[^{_WORDCHAR}]+")
_STRIP_RIGHT = re.compile(rf"[^{_WORDCHAR}]+$")
_WORD_RE = re.compile(rf"[{_WORDCHAR}'’\-]*[A-Za-zÀ-ÖØ-öø-ÿ][{_WORDCHAR}'’\-]*")
_NUMBER_RE = re.compile(r"^[0-9]+([.,][0-9]+)?$")
_SENTENCE_END = re.compile(r"[.!?…»\"]$")


# ---------------------------------------------------------------------------
# Lexique français (orthographe)
# ---------------------------------------------------------------------------


class FrenchLexicon:
    """Vérifie l'orthographe d'un mot via un dictionnaire français hors-ligne.

    S'appuie sur `pyspellchecker` (≈140 000 formes fléchies, embarquées, sans
    accès réseau). Gère les élisions (`l'`, `qu'`) et les mots composés (traits
    d'union). Une liste de mots supplémentaires (noms propres récurrents, lexique
    métier) peut être fournie pour réduire les faux positifs.
    """

    def __init__(self, extra_words: Iterable[str] = ()) -> None:
        from spellchecker import SpellChecker  # import paresseux (dépendance lourde)

        self._sp = SpellChecker(language="fr")
        self._extra = {w.strip().lower() for w in extra_words if w.strip()}

    def _known(self, word: str) -> bool:
        return word in self._extra or word in self._sp

    def is_correct(self, word: str) -> bool:
        """Vrai si `word` est une forme française plausible.

        Insensible à la casse. Traite les clitiques élidés et les composés à
        trait d'union (corrects si toutes les sous-parties sont connues).
        """
        w = word.strip().lower().replace("’", "'")
        if not w:
            return True
        if w in ELISIONS_SOUDEES or self._known(w):
            return True
        if "'" in w:
            *lead, last = w.split("'")
            if all(p in CLITICS for p in lead) and (last == "" or self._known(last)):
                return True
        if "-" in w:
            subs = [s for s in w.split("-") if s]
            if subs and all(self._known(s) or s in CLITICS for s in subs):
                return True
        return False


# Un vérificateur d'orthographe = mot -> est-correct. Injectable pour les tests.
SpellCheck = Callable[[str], bool]


# ---------------------------------------------------------------------------
# Reconstruction de l'état final + provenance par caractère
# ---------------------------------------------------------------------------


def _subtree_text(el: ET.Element) -> str:
    """Texte concaténé d'un sous-arbre, `<gap/>` ignoré (pour le contenu des `<del>`)."""
    parts: list[str] = [el.text or ""]
    for child in el:
        if child.tag != "gap":
            parts.append(_subtree_text(child))
        parts.append(child.tail or "")
    return "".join(parts)


def _emit(stream: list[tuple[str, frozenset[str]]], text: str, prov: frozenset[str]) -> None:
    for ch in text:
        stream.append((ch, prov))


def _walk(
    el: ET.Element,
    stream: list[tuple[str, frozenset[str]]],
    prov: frozenset[str],
    mod_type: str | None,
) -> None:
    """Parcourt l'arbre TEI en accumulant un flux (caractère, provenance).

    La provenance est l'ensemble des étiquettes actives : ``ajout`` (dans `<add>`),
    ``illisible`` (dans `<unclear>`), ``gap`` (sentinelle d'un `<gap/>`),
    ``en_trop`` (mot raturé réinjecté). Le contenu des `<del>` est exclu de l'état
    final ; un `<del>` de suppression pure (`<mod type="del">`) dont le texte forme
    un ou des mots entiers est réinjecté comme tokens « en trop ».
    """
    tag = el.tag

    if tag == "gap":
        stream.append((_GAP, frozenset(prov | {"gap"})))
        return
    if tag in ("lb", "pb", "figure"):
        stream.append((" ", prov))  # séparateur de mots
        return
    if tag == "del":
        # Contenu d'un <del> : original d'une correction (subst/add) -> on jette ;
        # suppression pure (mod type="del" ou <del> hors mod) -> « mots en trop ».
        if mod_type in (None, "del"):
            for m in _WORD_RE.finditer(_subtree_text(el)):
                word = m.group(0)
                if len(re.sub(rf"[^{_WORDCHAR}]", "", word)) >= 2:
                    stream.append((" ", prov))
                    _emit(stream, word, frozenset({"en_trop"}))
                    stream.append((" ", prov))
        return

    if tag == "add":
        child_prov = frozenset(prov | {"ajout"})
    elif tag == "unclear":
        child_prov = frozenset(prov | {"illisible"})
    else:
        child_prov = prov

    next_mod_type = el.get("type") if tag == "mod" else mod_type

    if el.text:
        _emit(stream, el.text, child_prov)
    for child in el:
        _walk(child, stream, child_prov, next_mod_type)
        if child.tail:
            _emit(stream, child.tail, child_prov)


def tei_to_stream(tei: str) -> list[tuple[str, frozenset[str]]]:
    """Transforme un fragment TEI SCOLEDIT en flux (caractère, provenance) état final.

    Le fragment (plusieurs `<p>` sans racine unique) est encapsulé avant analyse.
    """
    root = ET.fromstring(f"<root>{tei}</root>")
    stream: list[tuple[str, frozenset[str]]] = []
    _walk(root, stream, frozenset(), None)
    return stream


# ---------------------------------------------------------------------------
# Tokenisation + codage
# ---------------------------------------------------------------------------


@dataclass
class Token:
    """Un mot noté de la copie.

    Attributes:
        position: rang du token dans la copie (0-based).
        mot: forme de surface telle que lue (état final).
        forme_normalisee: cœur lexical en minuscules (sans ponctuation périphérique).
        code: 1 (correct) / 9 (erreur) / 0 (absent).
        categorie: "mot", "ponctuation", "nombre" ou "illisible".
        source: "dictionnaire" (orthographe) ou "balise_tei" (issu du balisage).
        details: étiquettes explicatives (hors_dico, en_trop, ajout, illisible…).
    """

    position: int
    mot: str
    forme_normalisee: str
    code: int
    categorie: str
    source: str
    details: list[str] = field(default_factory=list)


def _split_tokens(
    stream: Sequence[tuple[str, frozenset[str]]],
) -> list[tuple[str, frozenset[str]]]:
    """Découpe le flux en tokens (surface, provenance) sur les espaces."""
    tokens: list[tuple[str, frozenset[str]]] = []
    chars: list[str] = []
    prov: set[str] = set()
    for ch, p in stream:
        if ch.isspace():
            if chars:
                tokens.append(("".join(chars), frozenset(prov)))
                chars, prov = [], set()
        else:
            chars.append(ch)
            prov |= p
    if chars:
        tokens.append(("".join(chars), frozenset(prov)))
    return tokens


def _core_word(surface: str) -> str:
    """Cœur lexical : sentinelle gap retirée, ponctuation périphérique ôtée."""
    core = surface.replace(_GAP, "")
    core = _STRIP_LEFT.sub("", core)
    core = _STRIP_RIGHT.sub("", core)
    return core


# Spécification d'un token avant attribution de sa position : (mot_affiché,
# forme_normalisée, code, catégorie, source, détails).
_Spec = tuple[str, str, int, str, str, list[str]]


def _classify_word(display: str, prov: frozenset[str], is_correct: SpellCheck,
                   sentence_initial: bool) -> _Spec:
    """Code un segment lisible (sans `<gap/>`) via le lexique et la provenance.

    `display` est la surface telle que lue (ponctuation périphérique comprise) ;
    le jugement orthographique porte sur son cœur lexical.
    """
    details: list[str] = []
    if "ajout" in prov:
        details.append("ajout")
    word = _core_word(display)

    if not word:  # ponctuation seule : non évaluée (pas de référence) -> correcte.
        return (display, "", 1, "ponctuation", "balise_tei", details)
    if _NUMBER_RE.match(word):
        return (display, word, 1, "nombre", "regle", details + ["nombre"])
    if "illisible" in prov:  # lu sous <unclear> : lecture incertaine -> erreur.
        return (display, word.lower(), 9, "mot", "balise_tei", details + ["illisible"])
    if is_correct(word):
        return (display, word.lower(), 1, "mot", "dictionnaire", details)
    # Hors lexique : un mot capitalisé en milieu de phrase est probablement un nom
    # propre — on ne le pénalise pas (mais on le signale).
    if word[0].isupper() and not sentence_initial:
        return (display, word.lower(), 1, "mot", "dictionnaire", details + ["nom_propre_possible"])
    return (display, word.lower(), 9, "mot", "dictionnaire", details + ["hors_dico"])


def _classify(surface: str, prov: frozenset[str], is_correct: SpellCheck,
              sentence_initial: bool) -> list[_Spec]:
    """Transforme un token brut en une ou plusieurs spécifications de tokens notés.

    Un `<gap/>` en bordure (mark raturé/illisible accolé à un mot lisible) est
    détaché en token « illisible » distinct, afin de ne pas pénaliser le mot propre
    voisin. Un `<gap/>` intérieur à un mot (ratures au milieu, ex. « ouv□erte ») est
    conservé : le token entier est marqué illisible.
    """
    if "en_trop" in prov:  # mot raturé réinjecté : « en trop » -> erreur (code 9).
        return [(surface.replace(_GAP, ""), surface.replace(_GAP, "").lower(),
                 9, "mot", "balise_tei", ["en_trop"])]

    if _GAP not in surface:
        return [_classify_word(surface, prov, is_correct, sentence_initial)]

    # Découpe en segments alternés texte / gap.
    runs = [r for r in re.split(rf"({_GAP}+)", surface) if r]
    text_runs = [r for r in runs if _GAP not in r]
    gap_details = ["illisible", "gap"] + (["ajout"] if "ajout" in prov else [])

    # Gap intérieur (≥ 2 fragments de texte autour d'un gap) : token unique illisible.
    if len(text_runs) >= 2:
        disp = surface.replace(_GAP, "□")
        return [(disp, _core_word(surface).lower(), 9, "mot", "balise_tei", gap_details)]

    # Sinon gaps en bordure : on les détache, le mot éventuel est évalué normalement.
    specs: list[_Spec] = []
    for run in runs:
        if _GAP in run:
            specs.append(("□", "", 9, "illisible", "balise_tei", list(gap_details)))
        else:
            specs.append(_classify_word(run, prov, is_correct, sentence_initial))
    return specs


def score_tei(tei: str, is_correct: SpellCheck) -> list[Token]:
    """Note chaque mot d'une transcription TEI. Cœur testable, sans S3 ni réseau."""
    stream = tei_to_stream(tei)
    tokens: list[Token] = []
    sentence_initial = True
    for surface, prov in _split_tokens(stream):
        for disp, norm, code, cat, source, details in _classify(
            surface, prov, is_correct, sentence_initial
        ):
            tokens.append(Token(len(tokens), disp, norm, code, cat, source, details))
        # La phrase recommence après une ponctuation forte en fin de surface.
        sentence_initial = bool(_SENTENCE_END.search(surface.replace(_GAP, "")))
    return tokens


# ---------------------------------------------------------------------------
# Mise en forme des sorties
# ---------------------------------------------------------------------------

#: Légende du schéma de codes, recopiée dans chaque JSON de notation.
SCHEMA_LEGENDE = {
    "1": "mot present et orthographiquement correct",
    "9": "erreur (hors lexique, mot en trop/rature, ou illisible)",
    "0": "mot absent (omission signalee)",
    "reference": "evaluation_dictee/docs/decisions.md (D2, D3)",
}


def build_notation(annotation: dict, tokens: list[Token]) -> dict:
    """Construit le document JSON de notation d'une copie."""
    codes = [t.code for t in tokens]
    return {
        "student_id": annotation.get("student_id"),
        "level": annotation.get("level"),
        "scan": annotation.get("scan"),
        "schema": "1/9/0",
        "schema_legende": SCHEMA_LEGENDE,
        "texte_etat_final": " ".join(
            t.mot for t in tokens if t.categorie in ("mot", "nombre")
        ),
        "n_tokens": len(tokens),
        "n_mots": sum(1 for t in tokens if t.categorie == "mot"),
        "n_erreurs": sum(1 for c in codes if c == 9),
        "n_corrects": sum(1 for c in codes if c == 1),
        "n_absents": sum(1 for c in codes if c == 0),
        "tokens": [
            {
                "position": t.position,
                "mot": t.mot,
                "forme_normalisee": t.forme_normalisee,
                "code": t.code,
                "categorie": t.categorie,
                "source": t.source,
                "details": t.details,
            }
            for t in tokens
        ],
    }


#: En-tête du CSV agrégé (format long, une ligne par mot).
CSV_HEADER = [
    "scan", "level", "student_id", "position",
    "mot", "forme_normalisee", "code", "categorie", "source", "details",
]


def notation_csv_rows(notation: dict) -> list[list[str]]:
    """Lignes CSV (format long) d'une copie : une ligne par token."""
    rows: list[list[str]] = []
    for tok in notation["tokens"]:
        rows.append(
            [
                str(notation.get("scan", "")),
                str(notation.get("level", "")),
                str(notation.get("student_id", "")),
                str(tok["position"]),
                tok["mot"],
                tok["forme_normalisee"],
                str(tok["code"]),
                tok["categorie"],
                tok["source"],
                "|".join(tok["details"]),
            ]
        )
    return rows
