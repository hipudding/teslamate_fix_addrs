import os
import sys

import pytest
from sqlalchemy import text

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import teslamate_fix_addrs as fa  # noqa: E402


SQLITE_SCHEMA = [
    '''CREATE TABLE addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        display_name TEXT, latitude REAL, longitude REAL, name TEXT,
        house_number TEXT, road TEXT, neighbourhood TEXT, city TEXT,
        county TEXT, postcode TEXT, state TEXT, state_district TEXT,
        country TEXT, osm_id BIGINT, osm_type TEXT, raw TEXT,
        inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)''',
    '''CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        latitude REAL, longitude REAL)''',
    '''CREATE TABLE drives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_position_id INTEGER REFERENCES positions(id),
        end_position_id INTEGER REFERENCES positions(id),
        start_address_id INTEGER REFERENCES addresses(id),
        end_address_id INTEGER REFERENCES addresses(id))''',
    '''CREATE TABLE charging_processes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER REFERENCES positions(id),
        address_id INTEGER REFERENCES addresses(id))''',
]


# Mirrors TeslaMate's PostgreSQL types: numeric coordinates, jsonb raw and
# the (osm_id, osm_type) unique index.
PG_SCHEMA = [
    'DROP TABLE IF EXISTS charging_processes, drives, positions, addresses CASCADE',
    '''CREATE TABLE addresses (
        id SERIAL PRIMARY KEY,
        display_name VARCHAR(512), latitude NUMERIC(8, 6), longitude NUMERIC(9, 6),
        name VARCHAR(255), house_number VARCHAR(255), road VARCHAR(255),
        neighbourhood VARCHAR(255), city VARCHAR(255), county VARCHAR(255),
        postcode VARCHAR(255), state VARCHAR(255), state_district VARCHAR(255),
        country VARCHAR(255), raw JSONB,
        inserted_at TIMESTAMP NOT NULL DEFAULT now(),
        updated_at TIMESTAMP NOT NULL DEFAULT now(),
        osm_id BIGINT, osm_type TEXT)''',
    'CREATE UNIQUE INDEX addresses_osm_id_osm_type_index ON addresses (osm_id, osm_type)',
    '''CREATE TABLE positions (
        id SERIAL PRIMARY KEY,
        latitude NUMERIC(8, 6), longitude NUMERIC(9, 6))''',
    '''CREATE TABLE drives (
        id SERIAL PRIMARY KEY,
        start_position_id INTEGER REFERENCES positions(id),
        end_position_id INTEGER REFERENCES positions(id),
        start_address_id INTEGER REFERENCES addresses(id),
        end_address_id INTEGER REFERENCES addresses(id))''',
    '''CREATE TABLE charging_processes (
        id SERIAL PRIMARY KEY,
        position_id INTEGER REFERENCES positions(id),
        address_id INTEGER REFERENCES addresses(id))''',
]

# Set TEST_DB_URL=postgresql://user@host:port/db to run against PostgreSQL
# (tables are dropped and recreated for every test).
TEST_DB_URL = os.environ.get('TEST_DB_URL', '')


@pytest.fixture(autouse=True)
def no_stop():
    fa.STOP.clear()
    yield
    fa.STOP.clear()


@pytest.fixture
def config(tmp_path):
    return fa.Config(
        db_url=TEST_DB_URL or 'sqlite:///' + str(tmp_path / 'test.db'),
        batch=10, timeout=1, retry=0, mode=2,
        tencent_key='KEY', tencent_sk='SK',
        checkpoint_path=str(tmp_path / 'checkpoint.json'),
        osm_interval=0, geocoder_interval=0)


@pytest.fixture
def engine(config):
    engine = fa.create_engine_from_config(config)
    schema = PG_SCHEMA if engine.dialect.name == 'postgresql' else SQLITE_SCHEMA
    with engine.begin() as conn:
        for stmt in schema:
            conn.execute(text(stmt))
    yield engine
    engine.dispose()


@pytest.fixture
def db(engine):
    '''Helper to run raw SQL.'''
    class DB:
        def exec(self, sql, **params):
            with engine.begin() as conn:
                return conn.execute(text(sql), params)

        def insert(self, sql, **params):
            '''Run an INSERT and return the new id.'''
            with engine.begin() as conn:
                return conn.execute(text(sql + ' RETURNING id'), params).scalar()

        def all(self, sql, **params):
            with engine.connect() as conn:
                return conn.execute(text(sql), params).mappings().all()

        def one(self, sql, **params):
            rows = self.all(sql, **params)
            assert len(rows) == 1
            return rows[0]
    return DB()


@pytest.fixture
def tables(engine, db):
    # Tables must exist before reflection.
    return fa.reflect_tables(engine)


@pytest.fixture
def http_client(config):
    return fa.HttpClient(config)


@pytest.fixture
def geocoder(http_client, config):
    return fa.TencentGeocoder(http_client, config)
