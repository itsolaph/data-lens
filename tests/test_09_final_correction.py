import json
import tracemalloc

import pytest


@pytest.mark.parametrize('fmt', ['json', 'jsonl'])
@pytest.mark.parametrize('select', [['id'], ['id', 'payload']])
def test_rows_release_decoded_numeric_arrays(env, fmt, select):
    # Real parser/query, ~200 KiB input; never construct the reviewer's huge case.
    p = env.root / ('numbers.' + fmt)
    row = '{"id":1,"payload":[' + ','.join(['1'] * 12000) + ']}'
    p.write_text('[' + ','.join([row] * 8) + ']' if fmt == 'json' else (row + '\n') * 8)
    warm = env.root / "warm.json"
    warm.write_text('[ {"id":1} ]')
    env.query(warm, select=["id"])  # exclude one-time runtime imports from parser memory
    tracemalloc.start()
    try:
        result = env.query(p, select=select)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert result['record_count'] == result['matched_count'] == result['returned'] == 8
    assert [r['record'] for r in result['rows']] == list(range(1, 9))
    if 'payload' in select:
        assert all(r['values']['payload'] == {'_nested': 'array'} for r in result['rows'])
    assert peak < 7_000_000, peak  # room for two decoded records, not eight retained arrays


def test_rows_future_fields_missing_and_full_validation(env):
    p = env.root / 'future.json'
    p.write_text('[{"a":1},{"later":2},{"a":3}]')
    r = env.query(p, select=['later'], limit=2)
    assert r['rows'] == [{'record': 1, 'values': {}}, {'record': 2, 'values': {'later': '2'}}]
    assert r['matched_count'] == 3 and r['truncated']
    p.write_text('[{"a":1},{"password":"synthetic"}]')
    assert env.err(env.query, p, select=['a'], limit=1).code == 'credential_like_content'


def test_map_structured_next_steps(env):
    p = env.root / 'sample.json'
    p.write_text('[{"id":1}]')
    profile = env.profile(p)
    mapped = json.loads(env.L.middleware.build_map_result(str(p), {'file_size': p.stat().st_size}, profile))
    guidance = ' '.join(mapped['next'])
    for phrase in ('derived tool result', 'never edit', 'NOT SQL', "queries=['data_lens_query']", 'rows[].record', '_row_number'):
        assert phrase in guidance
    example = mapped['query_example']
    assert example['path'] == str(p) and example['expect_fingerprint'] == profile['fingerprint']
    assert env.query(p, **{k: v for k, v in example.items() if k != 'path'})['groups'][0]['aggregates']['count'] == 1
