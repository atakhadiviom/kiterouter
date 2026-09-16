import sqlite3
from pathlib import Path

from kiterouter.token_fetcher import TokenFetcher


def test_encrypted_omniroute_row_is_skipped_whole(monkeypatch, tmp_path):
    monkeypatch.setattr(Path, 'home', lambda: tmp_path)
    db = tmp_path / '.omniroute' / 'storage.sqlite'
    db.parent.mkdir()
    with sqlite3.connect(db) as conn:
        conn.execute('CREATE TABLE provider_connections (provider, auth_type, email, access_token, refresh_token, api_key, project_id, provider_specific_data, is_active, updated_at)')
        conn.execute('INSERT INTO provider_connections VALUES (?,?,?,?,?,?,?,?,?,?)',
                     ('command-code', 'apikey', None, None, None, 'enc:v1:fixture', None, None, 1, '2026'))
    result = TokenFetcher.fetch_from_omniroute()
    assert 'command_code' not in result
    assert result.skipped['command_code'] == 'encrypted-import-unsupported'
