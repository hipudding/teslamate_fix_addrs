import hashlib
import json
import re
import urllib.parse

import pytest
import responses

import teslamate_fix_addrs as fa


OSM_RE = re.compile(r'https://nominatim\.openstreetmap\.org/reverse\?.*')
TENCENT_RE = re.compile(r'https://apis\.map\.qq\.com/ws/geocoder/v1/\?.*')


def osm_result(osm_id, name='Some Place', osm_type='way', lat='39.9', lon='116.3'):
    return {
        'osm_id': osm_id, 'osm_type': osm_type, 'lat': lat, 'lon': lon,
        'name': name, 'display_name': '%s, Haidian, Beijing, China' % name,
        'address': {'road': 'Road %d' % osm_id, 'city': 'Beijing',
                    'state': 'Beijing', 'country': 'China'},
        'namedetails': {'name': name},
    }


def tencent_result(nation='中国', province='北京市', city='北京市',
                   district='海淀区', street='中关村大街', street_number='27号',
                   recommend='中关村大厦'):
    return {
        'status': 0, 'message': 'query ok',
        'result': {
            'address': province + district + street + street_number,
            'formatted_addresses': {'recommend': recommend, 'rough': recommend},
            'address_component': {
                'nation': nation, 'province': province, 'city': city,
                'district': district, 'street': street,
                'street_number': street_number},
        },
    }


def query_of(call):
    return dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(call.request.url).query))


def add_position(db, lat, lng):
    return db.insert('INSERT INTO positions (latitude, longitude) VALUES (:a, :b)',
                     a=lat, b=lng)


def add_address(db, lat=39.0, lng=116.0, osm_id=1, osm_type='way', **fields):
    cols = dict(latitude=lat, longitude=lng, osm_id=osm_id, osm_type=osm_type,
                display_name='OSM name', name='OSM name', road='OSM road',
                house_number='1', city='OSM city', state='OSM state',
                country='China', county='OSM county')
    cols.update(fields)
    keys = ', '.join(cols)
    vals = ', '.join(':' + k for k in cols)
    return db.insert('INSERT INTO addresses (%s) VALUES (%s)' % (keys, vals),
                     **cols)


def raw_of(db, addr):
    '''raw is jsonb (dict) in PostgreSQL, text in SQLite.'''
    raw = db.one('SELECT raw FROM addresses WHERE id = :i', i=addr)['raw']
    return json.loads(raw) if isinstance(raw, str) else raw


# ---------------------------------------------------------------- Mode 0

@responses.activate
def test_osm_unable_to_geocode_does_not_crash(engine, db, tables, http_client, config):
    sea = add_position(db, 30.0, 125.0)
    land = add_position(db, 39.984154, 116.307490)
    db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=sea)
    db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=land)

    def reply(request):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.url).query))
        if q['lat'] == '30.000000':
            return 200, {}, json.dumps({'error': 'Unable to geocode'})
        return 200, {}, json.dumps(osm_result(100))
    responses.add_callback(responses.GET, OSM_RE, callback=reply)

    fa.fix_empty_records(engine, http_client, config, tables, fa._default_checkpoint())

    rows = db.all('SELECT position_id, address_id FROM charging_processes ORDER BY id')
    assert rows[0]['address_id'] is None
    assert rows[1]['address_id'] is not None


@responses.activate
def test_failed_records_do_not_block_batch(engine, db, tables, http_client, config):
    config.batch = 1
    for i in range(3):
        p = add_position(db, 10.0 + i, 100.0)
        db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=p)

    def reply(request):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.url).query))
        if q['lat'] in ('10.000000', '11.000000'):
            return 200, {}, json.dumps({'error': 'Unable to geocode'})
        return 200, {}, json.dumps(osm_result(7))
    responses.add_callback(responses.GET, OSM_RE, callback=reply)

    fa.fix_empty_records(engine, http_client, config, tables, fa._default_checkpoint())

    rows = db.all('SELECT address_id FROM charging_processes ORDER BY id')
    assert [r['address_id'] is not None for r in rows] == [False, False, True]
    assert len(responses.calls) == 3


@responses.activate
def test_osm_unavailable_stops_round(engine, db, tables, http_client, config):
    p = add_position(db, 39.9, 116.3)
    db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=p)
    responses.add(responses.GET, OSM_RE, status=403, body='blocked')

    config.mode = 0
    ok = fa.run_once(engine, http_client, config, tables, fa._default_checkpoint(), None)

    assert ok is False
    assert db.one('SELECT address_id FROM charging_processes')['address_id'] is None


@responses.activate
def test_drive_only_missing_side_is_resolved(engine, db, tables, http_client, config):
    existing = add_address(db, osm_id=1)
    p1 = add_position(db, 39.1, 116.1)
    p2 = add_position(db, 39.2, 116.2)
    db.exec('INSERT INTO drives (start_position_id, end_position_id, start_address_id) '
            'VALUES (:a, :b, :c)', a=p1, b=p2, c=existing)
    responses.add(responses.GET, OSM_RE, json=osm_result(200))

    fa.fix_empty_records(engine, http_client, config, tables, fa._default_checkpoint())

    assert len(responses.calls) == 1
    q = query_of(responses.calls[0])
    # The car's own GPS position is used, exactly.
    assert (q['lat'], q['lon']) == ('39.200000', '116.200000')
    row = db.one('SELECT start_address_id, end_address_id FROM drives')
    assert row['start_address_id'] == existing
    assert row['end_address_id'] not in (None, existing)


@responses.activate
def test_osm_dedupe_uses_osm_type(engine, db, tables, http_client, config):
    node = add_address(db, osm_id=42, osm_type='node')
    p = add_position(db, 39.9, 116.3)
    db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=p)
    responses.add(responses.GET, OSM_RE, json=osm_result(42, osm_type='way'))

    fa.fix_empty_records(engine, http_client, config, tables, fa._default_checkpoint())

    addr_id = db.one('SELECT address_id FROM charging_processes')['address_id']
    assert addr_id != node
    assert db.one('SELECT osm_type FROM addresses WHERE id = :i', i=addr_id)['osm_type'] == 'way'


def test_address_name_priority():
    assert fa.get_address_name({'name': 'A', 'namedetails': {'name': 'B', 'alt_name': 'C'},
                                'display_name': 'D, E'}) == 'A'
    assert fa.get_address_name({'name': '', 'namedetails': {'name': 'B', 'alt_name': 'C'},
                                'display_name': 'D, E'}) == 'B'
    assert fa.get_address_name({'namedetails': {'alt_name': 'C'},
                                'display_name': 'D, E'}) == 'C'
    assert fa.get_address_name({'namedetails': None, 'display_name': 'D, E'}) == 'D'


# ---------------------------------------------------------------- Mode 1

@responses.activate
def test_geocode_uses_car_gps_position_without_offset(engine, db, tables, geocoder, config):
    # OSM centroid of a long road, far from where the car charged.
    addr = add_address(db, lat=39.900000, lng=116.400000)
    charge_pos = add_position(db, 39.984154, 116.307490)
    db.exec('INSERT INTO charging_processes (position_id, address_id) VALUES (:p, :a)',
            p=charge_pos, a=addr)
    responses.add(responses.GET, TENCENT_RE, json=tencent_result())

    fa.update_addresses(engine, geocoder, config, tables, fa._default_checkpoint())

    q = query_of(responses.calls[0])
    assert q['location'] == '39.984154,116.307490'   # lat,lng of the car, unchanged
    assert q['coord_type'] == '1'                     # GPS/WGS84, converted by Tencent
    # Signature over the unencoded, sorted params + SK.
    params = {k: v for k, v in q.items() if k != 'sig'}
    raw = '/ws/geocoder/v1/?' + '&'.join('%s=%s' % kv for kv in sorted(params.items())) + 'SK'
    assert q['sig'] == hashlib.md5(raw.encode()).hexdigest()

    row = db.one('SELECT * FROM addresses WHERE id = :i', i=addr)
    # Coordinates in the database are never rewritten (no GCJ-02 leak).
    assert (float(row['latitude']), float(row['longitude'])) == (39.9, 116.4)
    assert row['state'] == '北京市' and row['city'] == '北京市' and row['county'] == '海淀区'
    assert row['road'] == '中关村大街' and row['house_number'] == '27号'


@responses.activate
def test_geocode_position_fallback_order(engine, db, tables, config):
    addr_drive = add_address(db, lat=1.0, lng=1.0, osm_id=1)
    addr_none = add_address(db, lat=2.5, lng=2.5, osm_id=2)
    s = add_position(db, 3.0, 3.0)
    e = add_position(db, 4.0, 4.0)
    db.exec('INSERT INTO drives (start_position_id, end_position_id, start_address_id, '
            'end_address_id) VALUES (:s, :e, :a, :a)', s=s, e=e, a=addr_drive)
    from sqlalchemy.orm import Session
    with Session(engine) as session:
        Addresses = tables[3]
        assert fa.get_geocode_coordinates(session, tables, session.get(Addresses, addr_drive)) \
            == (4.0, 4.0, 'drive end (typical of 2)')
        assert fa.get_geocode_coordinates(session, tables, session.get(Addresses, addr_none)) \
            == (2.5, 2.5, 'address')


def test_shared_address_uses_most_typical_position(engine, db, tables, caplog):
    '''Many charges at home and one recent stop 2 km away share an address:
    the home position must be used, not the most recent one.'''
    addr = add_address(db, lat=33.370000, lng=120.200000)
    home = [(33.368478, 120.211295), (33.368480, 120.211290), (33.368470, 120.211300)]
    for lat, lng in home:
        p = add_position(db, lat, lng)
        db.exec('INSERT INTO charging_processes (position_id, address_id) VALUES (:p, :a)',
                p=p, a=addr)
    far = add_position(db, 33.387000, 120.211295)
    db.exec('INSERT INTO charging_processes (position_id, address_id) VALUES (:p, :a)',
            p=far, a=addr)

    from sqlalchemy.orm import Session
    with Session(engine) as session:
        lat, lng, source = fa.get_geocode_coordinates(
            session, tables, session.get(tables[3], addr))
    assert (lat, lng) in home
    assert source == 'charging (typical of 4)'
    assert 'shared by 4 positions' in caplog.text


@responses.activate
def test_empty_string_and_null_are_equal(engine, db, tables, geocoder, config):
    addr = add_address(db, road='希望大道', house_number='', name='阳光水岸花园',
                       display_name='阳光水岸花园', state='江苏省', city='盐城市',
                       county='亭湖区', country='中国')
    responses.add(responses.GET, TENCENT_RE, json=tencent_result(
        province='江苏省', city='盐城市', district='亭湖区', street='希望大道',
        street_number='', recommend='阳光水岸花园'))

    checkpoint = fa._default_checkpoint()
    fa.update_addresses(engine, geocoder, config, tables, checkpoint)

    row = db.one('SELECT house_number, raw FROM addresses WHERE id = :i', i=addr)
    assert row['house_number'] == ''      # untouched, not rewritten to NULL
    assert row['raw'] is None             # no change -> no backup written
    assert checkpoint['map_update']['updated_count'] == 0


@responses.activate
def test_failed_address_is_retried_then_abandoned(engine, db, tables, geocoder, config):
    config.geocoder_max_attempts = 2
    bad = add_address(db, osm_id=1)
    good = add_address(db, osm_id=2, lat=40.0)

    def reply(request):
        q = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(request.url).query))
        if q['location'].startswith('39.'):
            return 200, {}, json.dumps({'status': 310, 'message': 'bad request'})
        return 200, {}, json.dumps(tencent_result())
    responses.add_callback(responses.GET, TENCENT_RE, callback=reply)

    checkpoint = fa._default_checkpoint()
    fa.update_addresses(engine, geocoder, config, tables, checkpoint)
    state = checkpoint['map_update']
    assert state['failed_ids'] == {str(bad): 1}
    assert state['last_address_id'] == good
    assert state['updated_count'] == 1

    # Next round: only the failed address is retried, then given up.
    fa.update_addresses(engine, geocoder, config, tables, checkpoint)
    assert len(responses.calls) == 3
    assert state['failed_ids'] == {}
    assert state['abandoned_ids'] == [bad]

    saved = json.load(open(config.checkpoint_path))
    assert saved['map_update']['abandoned_ids'] == [bad]


@responses.activate
@pytest.mark.parametrize('reply', [
    dict(json={'status': 121, 'message': 'daily quota exceeded'}),
    dict(status=500, body='oops'),
])
def test_quota_or_network_error_does_not_skip(engine, db, tables, geocoder, config, reply):
    addr = add_address(db)
    responses.add(responses.GET, TENCENT_RE, **reply)

    checkpoint = fa._default_checkpoint()
    with pytest.raises(fa.GeocoderUnavailable):
        fa.update_addresses(engine, geocoder, config, tables, checkpoint)

    assert checkpoint['map_update']['last_address_id'] == 0
    assert checkpoint['map_update']['failed_ids'] == {}
    assert db.one('SELECT road FROM addresses WHERE id = :i', i=addr)['road'] == 'OSM road'


@responses.activate
def test_overseas_and_empty_values_are_kept(engine, db, tables, geocoder, config):
    overseas = add_address(db, osm_id=1, country='Japan')
    partial = add_address(db, osm_id=2)
    results = iter([
        tencent_result(nation='日本'),
        tencent_result(street='', street_number='', recommend=''),
    ])
    responses.add_callback(responses.GET, TENCENT_RE,
                           callback=lambda r: (200, {}, json.dumps(next(results))))

    fa.update_addresses(engine, geocoder, config, tables, fa._default_checkpoint())

    row = db.one('SELECT * FROM addresses WHERE id = :i', i=overseas)
    assert row['country'] == 'Japan' and row['road'] == 'OSM road'
    row = db.one('SELECT * FROM addresses WHERE id = :i', i=partial)
    assert row['state'] == '北京市'
    # No street from Tencent: road/house_number pair stays as it was.
    assert (row['road'], row['house_number']) == ('OSM road', '1')
    # No recommend: display_name falls back to Tencent's full address.
    assert row['display_name'] == '北京市海淀区'


@responses.activate
def test_street_without_number_clears_stale_house_number(engine, db, tables, geocoder, config):
    addr = add_address(db)
    responses.add(responses.GET, TENCENT_RE, json=tencent_result(street_number=''))

    fa.update_addresses(engine, geocoder, config, tables, fa._default_checkpoint())

    row = db.one('SELECT road, house_number FROM addresses WHERE id = :i', i=addr)
    assert (row['road'], row['house_number']) == ('中关村大街', None)


@responses.activate
def test_backup_and_restore(engine, db, tables, geocoder, config):
    addr = add_address(db, raw=json.dumps({'place_id': 1}))
    responses.add(responses.GET, TENCENT_RE, json=tencent_result())

    fa.update_addresses(engine, geocoder, config, tables, fa._default_checkpoint())
    raw = raw_of(db, addr)
    assert raw['place_id'] == 1
    assert raw[fa.BACKUP_KEY]['original']['road'] == 'OSM road'

    # A second update must not overwrite the saved original.
    responses.replace(responses.GET, TENCENT_RE, json=tencent_result(street='另一条路'))
    checkpoint = fa._default_checkpoint()
    fa.update_addresses(engine, geocoder, config, tables, checkpoint)
    raw = raw_of(db, addr)
    assert raw[fa.BACKUP_KEY]['original']['road'] == 'OSM road'

    fa.restore_original_addresses(engine, config, tables)
    row = db.one('SELECT * FROM addresses WHERE id = :i', i=addr)
    assert (row['road'], row['house_number'], row['state']) == ('OSM road', '1', 'OSM state')
    assert raw_of(db, addr) == {'place_id': 1}


@responses.activate
def test_dry_run_writes_nothing(engine, db, tables, http_client, geocoder, config):
    config.dry_run = True
    addr = add_address(db)
    p = add_position(db, 39.9, 116.3)
    db.exec('INSERT INTO charging_processes (position_id) VALUES (:p)', p=p)
    responses.add(responses.GET, OSM_RE, json=osm_result(99))
    responses.add(responses.GET, TENCENT_RE, json=tencent_result())

    assert fa.run_once(engine, http_client, config, tables, fa._default_checkpoint(), geocoder)

    assert db.one('SELECT address_id FROM charging_processes')['address_id'] is None
    assert db.one('SELECT road FROM addresses WHERE id = :i', i=addr)['road'] == 'OSM road'
    assert db.one('SELECT COUNT(*) AS n FROM addresses')['n'] == 1
    import os
    assert not os.path.exists(config.checkpoint_path)


# ---------------------------------------------------------------- misc

def test_redact_url():
    url = 'https://apis.map.qq.com/ws/geocoder/v1/?key=ABC&location=1,2&sig=XYZ'
    assert fa.redact_url(url) == \
        'https://apis.map.qq.com/ws/geocoder/v1/?key=***&location=1,2&sig=***'


def test_load_old_checkpoint_gets_new_keys(tmp_path):
    path = tmp_path / 'c.json'
    path.write_text(json.dumps({'tencent_update': {'last_address_id': 5, 'updated_count': 2}}))
    data = fa.load_checkpoint(str(path))
    assert data['map_update']['last_address_id'] == 5
    assert data['map_update']['failed_ids'] == {}
    assert data['osm_fix']['fixed_count'] == 0


def test_parse_args_env(monkeypatch):
    for k, v in {'DB_URL': 'sqlite://', 'MODE': '0', 'USER_AGENT': '', 'INTERVAL': ''}.items():
        monkeypatch.setenv(k, v)
    monkeypatch.setattr('sys.argv', ['prog'])
    config = fa.parse_args()
    assert config.user_agent == fa.DEFAULT_USER_AGENT
    assert config.interval == 0

    monkeypatch.setenv('MODE', '5')
    with pytest.raises(SystemExit):
        fa.parse_args()
    monkeypatch.setenv('MODE', '1')
    monkeypatch.delenv('TENCENT_KEY', raising=False)
    with pytest.raises(SystemExit):
        fa.parse_args()
