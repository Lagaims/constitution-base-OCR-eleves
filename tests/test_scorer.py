"""Tests de la notation mot à mot (logique pure, sans S3 ni réseau).

Le lexique est simulé par un petit ensemble de mots connus : les tests restent
déterministes et n'embarquent pas la dépendance pyspellchecker.
"""

from __future__ import annotations

import pytest

from scoledit.scorer import (
    FrenchLexicon,
    build_notation,
    notation_csv_rows,
    score_tei,
    tei_to_stream,
)

KNOWN = {
    "le", "robot", "et", "chat", "il", "était", "très", "intelligent",
    "une", "maison", "dans", "appelait", "petit", "grand",
}


def fake_check(word: str) -> bool:
    """Vérificateur factice : clitiques élidés gérés comme dans FrenchLexicon."""
    w = word.lower().replace("’", "'")
    if "'" in w:
        *lead, last = w.split("'")
        if all(p in {"l", "d", "s", "qu", "n", "c", "j", "m", "t"} for p in lead):
            return last == "" or last in KNOWN
    return w in KNOWN


def codes_for(tei: str) -> list[tuple[str, int, str]]:
    return [(t.mot, t.code, t.categorie) for t in score_tei(tei, fake_check)]


def test_etat_final_garde_add_et_retire_del():
    # subst : <del>o</del><add>s</add> soudé à "orta" -> "sorta" (état final).
    stream = tei_to_stream('<p><mod type="subst"><del>o</del><add>s</add></mod>orta</p>')
    assert "".join(c for c, _ in stream if c not in " ￿").strip() == "sorta"


def test_mot_correct_vs_faute_orthographe():
    toks = {t.mot: t for t in score_tei("<p>le robot était inteligent</p>", fake_check)}
    assert toks["le"].code == 1
    assert toks["robot"].code == 1
    assert toks["inteligent"].code == 9       # hors lexique -> erreur
    assert "hors_dico" in toks["inteligent"].details
    assert toks["inteligent"].source == "dictionnaire"


def test_mot_en_trop_est_code_9():
    # Suppression pure d'un mot entier -> « en trop » -> 9.
    toks = score_tei('<p>le <mod type="del"><del>les</del></mod> chat</p>', fake_check)
    en_trop = [t for t in toks if "en_trop" in t.details]
    assert len(en_trop) == 1
    assert en_trop[0].code == 9
    assert en_trop[0].source == "balise_tei"


def test_del_de_substitution_n_est_pas_en_trop():
    # Le <del> d'une substitution est l'original corrigé : aucun token « en trop ».
    toks = score_tei('<p><mod type="subst"><del>o</del><add>s</add></mod>orta</p>', fake_check)
    assert all("en_trop" not in t.details for t in toks)


def test_gap_est_illisible_code_9():
    toks = score_tei('<p>le <mod resp="E" seq="T1"><gap/></mod> chat</p>', fake_check)
    illisibles = [t for t in toks if t.categorie == "illisible"]
    assert len(illisibles) == 1
    assert illisibles[0].code == 9
    assert "gap" in illisibles[0].details


def test_gap_en_bordure_ne_penalise_pas_le_mot_voisin():
    # « <gap/>loup » : marque effacée accolée à un mot propre -> gap détaché, mot OK.
    toks = score_tei('<p>le <mod resp="E" seq="T1"><gap/></mod>robot</p>', fake_check)
    mots = {t.mot: t for t in toks}
    assert mots["robot"].code == 1 and mots["robot"].categorie == "mot"
    assert any(t.categorie == "illisible" and t.code == 9 for t in toks)


def test_gap_interieur_de_mot_reste_illisible():
    # « ouv□erte » : ratures au milieu d'un mot -> un seul token illisible.
    toks = score_tei("<p>une ouv<gap/>erte</p>", fake_check)
    interior = [t for t in toks if "□" in t.mot]
    assert len(interior) == 1 and interior[0].code == 9


def test_unclear_est_code_9_illisible():
    toks = {t.mot: t for t in score_tei("<p>le <unclear>robot</unclear></p>", fake_check)}
    assert toks["robot"].code == 9
    assert "illisible" in toks["robot"].details


def test_nom_propre_en_milieu_de_phrase_non_penalise():
    # "Michel" inconnu, capitalisé, non initial -> pas pénalisé mais signalé.
    toks = {t.mot: t for t in score_tei("<p>le robot Michel</p>", fake_check)}
    assert toks["Michel"].code == 1
    assert "nom_propre_possible" in toks["Michel"].details


def test_mot_capitalise_en_debut_de_phrase_est_evalue():
    # En début de phrase, un mot inconnu capitalisé reste évalué (-> 9 ici).
    toks = score_tei("<p>Zzz le robot</p>", fake_check)
    assert toks[0].mot == "Zzz" and toks[0].code == 9


def test_ponctuation_et_nombre():
    toks = score_tei("<p>le robot , 1987 .</p>", fake_check)
    cats = {t.mot: (t.code, t.categorie) for t in toks}
    assert cats[","] == (1, "ponctuation")
    assert cats["1987"] == (1, "nombre")


def test_clitique_elide_correct():
    toks = {t.mot: t for t in score_tei("<p>il s'appelait dans une maison</p>", fake_check)}
    assert toks["s'appelait"].code == 1     # clitique « s' » + tête « appelait » connue


def test_lb_separe_les_mots():
    toks = score_tei("<p>le<lb/>robot</p>", fake_check)
    assert [t.mot for t in toks] == ["le", "robot"]


def test_build_notation_compte_et_csv():
    tei = "<p>le robot inteligent</p>"
    tokens = score_tei(tei, fake_check)
    notation = build_notation(
        {"student_id": 1, "level": "CM2", "scan": "1a", "tei": tei}, tokens
    )
    assert notation["n_mots"] == 3
    assert notation["n_erreurs"] == 1
    assert notation["scan"] == "1a"
    rows = notation_csv_rows(notation)
    assert len(rows) == 3
    assert rows[0][0] == "1a" and rows[0][1] == "CM2"


@pytest.mark.parametrize("tei", ["<p></p>", "<p><lb/></p>", "<p> </p>"])
def test_copies_vides_ne_plantent_pas(tei):
    assert score_tei(tei, fake_check) == []


def test_lexique_reel_clitiques_et_composes():
    # Vérifie l'intégration réelle du dictionnaire embarqué (hors-ligne).
    lex = FrenchLexicon(extra_words=["Esquar"])
    assert lex.is_correct("intelligent")
    assert not lex.is_correct("inteligent")
    assert lex.is_correct("s'appelait")
    assert lex.is_correct("aujourd'hui")
    assert lex.is_correct("peux-tu")
    assert lex.is_correct("Esquar")          # nom propre ajouté
