from trio_lab.db import _split_statements


def test_split_statements_basic():
    sql = "CREATE TABLE a (x INT);\nCREATE TABLE b (y INT);"
    assert _split_statements(sql) == ["CREATE TABLE a (x INT)", "CREATE TABLE b (y INT)"]


def test_split_statements_ignores_semicolon_in_comment():
    # Cas trouvé en CI le 2026-09-02 (migration 001) : ";" utilisé comme
    # ponctuation de phrase dans un commentaire français.
    sql = (
        "-- calculées à l'ingestion ; le détail ordonné (quel drake,\n"
        "-- quelle tour, quand) vit ailleurs.\n"
        "CREATE TABLE match_trio_stats (match_id TEXT);"
    )
    assert _split_statements(sql) == ["CREATE TABLE match_trio_stats (match_id TEXT)"]


def test_split_statements_full_line_comment_dropped():
    sql = "-- juste un commentaire, rien d'autre\nCREATE TABLE a (x INT);"
    assert _split_statements(sql) == ["CREATE TABLE a (x INT)"]
