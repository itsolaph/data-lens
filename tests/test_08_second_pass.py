import json
import time
import pytest


def test_boolean_distinct(env):
    p = env.root / 'b.json'
    p.write_text('[{"b":true},{"b":false}]')
    assert env.profile(p)['fields'][0]['distinct'] == 2


@pytest.mark.parametrize('fmt,text', [('json', '[{"x":"' + '界'*40 + '"}]'), ('csv', 'x\n"' + '界'*15 + '\n' + '界'*15 + '"\n')])
def test_unicode_record_bytes(env, fmt, text):
    p = env.root / ('x.' + fmt)
    p.write_text(text)
    assert env.err(env.profile, p, _s=env.settings(max_line_bytes=60)).code == 'record_too_large'


def test_reject_before_decode(env, monkeypatch):
    p = env.root / 'big.json'
    p.write_text('[{"x":[' + ','.join(['1']*1000) + ']}]')
    def forbidden(*a):
        pytest.fail('decoder allocated oversized element')
    monkeypatch.setattr(env.L.scan, '_decoder', lambda: type('Decoder', (), {'raw_decode': forbidden})())
    assert env.err(env.profile, p, _s=env.settings(max_line_bytes=100)).code == 'record_too_large'


@pytest.mark.parametrize('direction,expected', [('asc', ['1','2',None]), ('desc', ['2','1',None])])
def test_explicit_group_null_last(env, direction, expected):
    p = env.root / 'g.json'
    p.write_text('[{"g":null},{"g":2},{"g":1}]')
    r = env.query(p, group_by=['g'], aggregates=[{'fn':'count'}], order_by=[{'by':'g','dir':direction}])
    assert [g['key'][0] for g in r['groups']] == expected
    r = env.query(p, group_by=['g'], aggregates=[{'fn':'count'}], order_by=[{'by':'g','dir':direction}], offset=1, limit=1)
    assert r['groups'][0]['key'][0] == expected[1]


def test_complete_profile_bounds(env):
    p = env.root / 'wide.json'
    p.write_text(json.dumps([{('界'*120+str(i)):i for i in range(256)}]))
    r = env.profile(p)
    assert len(env.L.core.dumps(r)) <= 20000
    assert r['schema_complete'] is False
    assert 'omitted_field_names' not in r


def test_output_floor(L):
    limits, warnings = L.core.build_limits({'max_output_chars':1})
    assert limits.max_output_chars >= 1024 and warnings


def test_expired_before_first_record(env):
    records = env.L.scan.Records(b'[{"x":1}]', 'json', env.settings().limits, time.monotonic()-1)
    with pytest.raises(env.L.core.LensError, match='time budget'):
        list(records)


def test_acquisition_deadline(env, monkeypatch):
    p = env.root / 'd.csv'
    p.write_text('x\n1\n')
    with pytest.raises(env.L.core.LensError, match='time budget'):
        env.L.source.load_source(str(p), env.settings(), time.monotonic()-1)


@pytest.mark.parametrize('text', ['[{"x":"escaped \\\" } [ brace"}]', '[{"x":{"a":[1,2]}}]'])
def test_scanner_valid(env, text):
    p = env.root / 'ok.json'
    p.write_text(text)
    assert env.profile(p)['record_count'] == 1


@pytest.mark.parametrize('text', ['[{},]', '[{}, {bad}, {}]', '[{"x":1,"x":2}]', '[{"x":[1,2}]]'])
def test_scanner_invalid(env, text):
    p = env.root / 'bad.json'
    p.write_text(text)
    assert env.err(env.profile,p).code == 'invalid_json'


def test_file_only_model_definitions(run_driver, tmp_path):
    result = run_driver('''
        boot(HOME)
        import model_tools
        defs = model_tools.get_tool_definitions(enabled_toolsets=['file'], quiet_mode=True, skip_tool_search_assembly=True)
        emit([d['function']['name'] for d in defs])
    ''')
    assert {'read_file','data_lens_profile','data_lens_query'} <= set(result)


def test_middleware_complete_bound(env):
    p = env.root / 'wide.json'
    p.write_text(json.dumps([{('界'*120+str(i)):i for i in range(256)}]))
    settings = {'roots':[str(env.root)], 'host_local':True}
    middleware = env.L.middleware.make_middleware(lambda k,d:settings.get(k,d))
    base = json.dumps({'content':'head', 'file_size':p.stat().st_size, 'truncated':True})
    result = middleware(tool_name='read_file',args={'path':str(p)},next_call=lambda a:base)
    assert len(result) <= 20000
    r = json.loads(result)
    assert r['data_lens_map'] and r['data_lens']['schema_complete'] is False


def test_scan_deadline_during_element(env, monkeypatch):
    calls = iter([0,0,0,10,10,10])
    monkeypatch.setattr(env.L.core.time, 'monotonic', lambda: next(calls,10))
    recs = env.L.scan.Records(('[{"x":"'+'a'*2000+'"}]').encode(), 'json', env.settings().limits, 5)
    with pytest.raises(env.L.core.LensError, match='time budget'):
        list(recs)


def test_acquisition_expiry_after_read(env, monkeypatch):
    p = env.root / 'd.csv'
    p.write_text('x\n1\n')
    clock = [0]
    real = env.L.source.os.read
    def read(fd,n):
        result = real(fd,n)
        clock[0] = 10
        return result
    monkeypatch.setattr(env.L.core.time, 'monotonic', lambda:clock[0])
    monkeypatch.setattr(env.L.source.os,'read',read)
    with pytest.raises(env.L.core.LensError, match='time budget'):
        env.L.source.load_source(str(p), env.settings(),5)


def test_nesting_before_decode(env, monkeypatch):
    p = env.root / 'deep.json'
    p.write_text('[{"x":'+ '['*40+'0'+']'*40+'}]')
    def forbidden(*a):
        pytest.fail('decoded over-depth element')
    monkeypatch.setattr(env.L.scan, '_decoder', lambda:type('Decoder',(),{'raw_decode':forbidden})())
    assert env.err(env.profile,p).code == 'nesting_too_deep'


def test_distinct_hash_collision_and_scale(env, monkeypatch):
    monkeypatch.setattr(env.L.profile, 'hash', lambda _:0, raising=False)
    p = env.root / 'distinct.json'
    p.write_text('[{"x":1.0},{"x":1.00},{"x":true},{"x":false},{"x":"1.0"}]')
    assert env.profile(p)['fields'][0]['distinct'] == 5


def test_demo_inherited_tmpdir(tmp_path):
    import os
    import subprocess
    import sys
    from pathlib import Path
    # Simulates Hermes scratch without consulting any real profile.
    old_home = tmp_path / 'old_home'
    scratch = old_home / '.hermes' / 'cache' / 'scratch'
    scratch.mkdir(parents=True)
    out = tmp_path / 'evidence'
    proc = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / 'scripts/demo.py'), '--out',str(out)],
                          env={**os.environ,'HOME':str(old_home),'TMPDIR':str(scratch)},capture_output=True,text=True,timeout=120)
    assert proc.returncode == 0, proc.stdout[-2000:] + proc.stderr[-2000:]
    assert json.loads((out/'evidence.json').read_text())['all_match']
    assert not list(scratch.iterdir())


def test_demo_failure_restores_environment(tmp_path, monkeypatch):
    import importlib.util
    import os
    from pathlib import Path
    spec = importlib.util.spec_from_file_location('demo_test',Path(__file__).resolve().parents[1]/'scripts/demo.py')
    demo = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(demo)
    old = {k:os.environ.get(k) for k in ('HOME','HERMES_HOME','TERMINAL_ENV')}
    made = []
    real = demo.tempfile.mkdtemp
    def create(**kw):
        path = real(**kw)
        made.append(Path(path))
        return path
    def fail(*a):
        os.environ['HOME'] = 'changed'
        raise RuntimeError('fixture failure')
    monkeypatch.setattr(demo.tempfile,'mkdtemp',create)
    monkeypatch.setattr(demo,'write_fixtures',fail)
    monkeypatch.setattr(demo.sys,'argv',['demo','--out',str(tmp_path/'out')])
    with pytest.raises(RuntimeError,match='fixture failure'):
        demo.main()
    assert old == {k:os.environ.get(k) for k in old}
    assert made and all(not p.exists() for p in made)


def test_default_megabyte_element_rejected_before_decode(env, monkeypatch):
    p = env.root / 'large.json'
    p.write_text('[{"x":[' + ','.join(['1']*600000) + ']}]')
    def forbidden(*a):
        pytest.fail('allocated pathological numeric array')
    monkeypatch.setattr(env.L.scan,'_decoder',lambda:type('Decoder',(),{'raw_decode':forbidden})())
    assert env.err(env.profile,p).code == 'record_too_large'


def test_error_envelope_at_minimum(env):
    p = env.root / 'x.json'
    p.write_text('[{"x":1}]')
    config = {'roots':[str(env.root)],'host_local':True,'limits':{'max_output_chars':1024}}
    profile, query = env.L.tools.make_handlers(lambda k,d:config.get(k,d))
    for response in (profile({'path':str(p)}), query({'path':str(p),'select':['界'*128]})):
        assert len(response) <= 1024
        assert isinstance(json.loads(response),dict)


def test_explicit_order_ties_and_default_null_order(env):
    p = env.root / 'ties.json'
    p.write_text('[{"g":1,"h":"b"},{"g":null,"h":"a"},{"g":1,"h":"a"}]')
    default = env.query(p,group_by=['g','h'],aggregates=[{'fn':'count'}])
    assert default['groups'][0]['key'][0] is None
    for direction in ('asc','desc'):
        r = env.query(p,group_by=['g','h'],aggregates=[{'fn':'count'}],order_by=[{'by':'g','dir':direction}])
        assert [g['key'] for g in r['groups']] == [['1','a'],['1','b'],[None,'a']]
