import pytest

# extra [ml] (joblib/scikit-learn), pas installé par la CI standard ([dev]) —
# même logique que tests/ml/test_embeddings.py.
pytest.importorskip("joblib")
pytest.importorskip("sklearn")

from trio_lab.ml.predict import _ROLE_ORDER, _parse_team, resolve_champion  # noqa: E402
from trio_lab.web.champions import Champion  # noqa: E402

INDEX = {
    1: Champion(id=1, name="Volibear", icon_url=""),
    2: Champion(id=2, name="Ahri", icon_url=""),
    3: Champion(id=3, name="Renata Glasc", icon_url=""),
    4: Champion(id=4, name="Aatrox", icon_url=""),
    5: Champion(id=5, name="Jinx", icon_url=""),
}


def test_resolve_champion_case_insensitive():
    assert resolve_champion("ahri", INDEX) == 2
    assert resolve_champion("AHRI", INDEX) == 2


def test_resolve_champion_unknown_raises():
    with pytest.raises(ValueError, match="inconnu"):
        resolve_champion("PasUnChampion", INDEX)


def test_parse_team_basic():
    team = _parse_team("Volibear, Ahri, Renata Glasc, Aatrox, Jinx", INDEX)
    assert team == dict(zip(_ROLE_ORDER, (1, 2, 3, 4, 5), strict=True))


def test_parse_team_wrong_count_raises():
    with pytest.raises(ValueError, match="attendu 5"):
        _parse_team("Volibear, Ahri", INDEX)
