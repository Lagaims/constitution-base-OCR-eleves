"""Tests de la classification des exercices d'après la consigne (logique pure)."""

from __future__ import annotations

import xml.etree.ElementTree as ET

import pytest

from scoledit.consignes import (
    classify_consigne,
    extract_consigne,
    extract_derivation,
)

# Consignes réelles observées dans le corpus.
CONSIGNE_SCOLEDIT_CE1 = (
    "[4 vignettes illustrant 4 personnages sont montrées aux élèves...]. Voici 4 "
    "personnages. Choisis un ou deux personnages et raconte une histoire."
)
CONSIGNE_SCOLEDIT_CP = (
    "Aujourd’hui vous allez écrire chacun l’histoire d’un petit chat. ... Vous allez "
    "écrire cette histoire ici."
)
CONSIGNE_RESOLCO = (
    "La consigne donnée aux enfants demande de raconter une histoire sans indication "
    "concernant la factualité."
)


@pytest.mark.parametrize(
    "consigne",
    [CONSIGNE_SCOLEDIT_CE1, CONSIGNE_SCOLEDIT_CP, CONSIGNE_RESOLCO],
)
def test_consignes_reelles_sont_ecriture_libre(consigne):
    assert classify_consigne(consigne) == "ecriture_libre"


def test_dictee_detectee():
    assert classify_consigne("Écrivez le texte que je vais vous dicter.") == "dictee"
    assert classify_consigne("Dictée : écris ce que je dis.") == "dictee"
    assert classify_consigne("Vous écrivez sous la dictée de la maîtresse.") == "dictee"


def test_recopie_detectee():
    assert classify_consigne("Recopie le texte suivant sans erreur.") == "recopie"
    assert classify_consigne("Reproduis fidèlement le modèle.") == "recopie"


def test_consigne_vide_ou_inconnue():
    assert classify_consigne("") == "indetermine"
    assert classify_consigne("   ") == "indetermine"
    assert classify_consigne("Réponds aux questions de mathématiques.") == "indetermine"


def test_recopie_prioritaire_sur_libre():
    # Une consigne mêlant les deux verbes -> recopie (règle plus spécifique d'abord).
    assert classify_consigne("Recopie l'histoire affichée au tableau.") == "recopie"


def test_extract_consigne_et_derivation_depuis_tei():
    xml = (
        "<TEI><text><body/></text>"
        "<profileDesc><textDesc>"
        '<derivation type="original"/>'
        '<factuality type="fiction">Raconte une histoire.</factuality>'
        "</textDesc></profileDesc></TEI>"
    )
    root = ET.fromstring(xml)
    assert extract_consigne(root) == "Raconte une histoire."
    assert extract_derivation(root) == "original"
    assert classify_consigne(extract_consigne(root)) == "ecriture_libre"
