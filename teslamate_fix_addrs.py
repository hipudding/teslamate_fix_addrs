from dataclasses import dataclass, field
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, or_, and_
from sqlalchemy.engine.url import URL
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
import json
import hashlib
import urllib.parse
from datetime import datetime
import logging
import argparse
import math
import os
import re
import signal
import statistics
import sys
import tempfile
import threading

logging.basicConfig(
    level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)

__version__ = '2.1.0'

DEFAULT_USER_AGENT = 'teslamate_fix_addrs/%s' % __version__

# Set by SIGINT/SIGTERM. Loops check it between records so the current batch
# can be committed before exiting. Also used as an interruptible sleep.
STOP = threading.Event()


def handle_signal(signum, frame):
    '''SIGINT / SIGTERM handler: finish current record, commit, then exit.'''
    if STOP.is_set():
        logging.info("Signal %d received again, force exit." % signum)
        os._exit(1)
    logging.info("Signal %d received, exiting after current record." % signum)
    STOP.set()


class EnvDefault(argparse.Action):
    '''args priority: cli args -> ENV -> default. Empty ENV values are ignored.'''

    def __init__(self, envvar, required=True, default=None, **kwargs):
        if os.environ.get(envvar, '') != '':
            default = os.environ[envvar]
        if required and default:
            required = False
        super(EnvDefault, self).__init__(default=default,
                                         required=required,
                                         **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


def env_bool(envvar):
    '''Read a boolean flag from environment (1/true/yes/on).'''
    return os.environ.get(envvar, '').strip().lower() in ('1', 'true', 'yes', 'on')


class GeocoderUnavailable(Exception):
    '''The geocoding service can not be used right now (network down, quota
    exceeded, key rejected...). The current round stops without marking any
    record as failed, so nothing is skipped; the next round retries.'''


# ---------- OSM address-key aliases (from TeslaMate source) ----------

house_number_aliases = ['house_number', 'street_number']

road_aliases = [
    "road", "footway", "street", "street_name", "residential", "path",
    "pedestrian", "road_reference", "road_reference_intl", "square", "place"
]

neighborhood_aliases = [
    "neighbourhood", "suburb", "city_district", "district", "quarter",
    "borough", "city_block", "residential", "commercial", "houses",
    "subdistrict", "subdivision", "ward"
]

municipality_aliases = [
    "municipality", "local_administrative_area", "subcounty"
]

village_aliases = ["village", "municipality", "hamlet", "locality", "croft"]

city_aliases = ["city", "town", "township"]
city_aliases.extend(village_aliases)
city_aliases.extend(municipality_aliases)

county_aliases = ["county", "county_code", "department"]

state_aliases = ['state', 'province', 'state_code']

country_aliases = ['country', 'country_name']


# ---------- Config ----------

@dataclass
class Config:
    """Encapsulates all runtime configuration."""
    user: str = ''
    password: str = ''
    host: str = ''
    port: str = ''
    dbname: str = ''
    db_url: str = ''
    batch: int = 10
    timeout: int = 5
    retry: int = 5
    interval: int = 0
    mode: int = 0
    tencent_key: str = ''
    tencent_sk: str = ''
    since: datetime = field(default_factory=lambda: datetime.min)
    user_agent: str = DEFAULT_USER_AGENT
    checkpoint_path: str = 'checkpoint.json'
    reset_checkpoint: bool = False
    osm_interval: float = 1.0
    geocoder_interval: float = 0.3
    geocoder_max_attempts: int = 3
    update_overseas: bool = False
    dry_run: bool = False
    restore_original: bool = False


# ---------- Checkpoint persistence ----------

def _default_checkpoint():
    """Return a fresh default checkpoint dict."""
    return {
        "osm_fix": {
            "last_drive_id": 0,
            "last_charging_id": 0,
            "fixed_count": 0
        },
        "map_update": {
            "last_address_id": 0,
            "updated_count": 0,
            # address id (str) -> failed attempts, retried at every round.
            "failed_ids": {},
            # address ids given up after too many failed attempts.
            "abandoned_ids": []
        },
        "last_run": None
    }


def load_checkpoint(path):
    """Load checkpoint from JSON file. Returns defaults if file doesn't exist."""
    checkpoint = _default_checkpoint()
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            # Migrate old checkpoint key.
            if 'tencent_update' in data and 'map_update' not in data:
                data['map_update'] = data.pop('tencent_update')
            # Merge so that keys added in newer versions get their defaults.
            for key, value in data.items():
                if isinstance(value, dict) and isinstance(checkpoint.get(key), dict):
                    checkpoint[key].update(value)
                else:
                    checkpoint[key] = value
            logging.info("Loaded checkpoint from %s" % path)
        except (json.JSONDecodeError, IOError) as e:
            logging.error("Failed to load checkpoint: %s" % e)
    return checkpoint


def save_checkpoint(path, data):
    """Save checkpoint to JSON file atomically (write tmp then rename).
    A failed save is logged but does not stop the program."""
    data['last_run'] = datetime.now().replace(microsecond=0).isoformat()
    abs_path = os.path.abspath(path)
    dir_name = os.path.dirname(abs_path)
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp_path, abs_path)
        logging.info("Checkpoint saved to %s" % path)
    except OSError as e:
        logging.error("Failed to save checkpoint to %s: %s" % (path, e))
        if tmp_path and os.path.exists(tmp_path):
            os.unlink(tmp_path)


# ---------- HTTP client ----------

_SECRET_PARAM_RE = re.compile(r'([?&](?:key|sig)=)[^&]*')


def redact_url(url):
    '''Hide API key and signature before writing a URL to logs.'''
    return _SECRET_PARAM_RE.sub(r'\1***', url)


class HttpClient:
    """Reusable HTTP client wrapping requests.Session."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        # Retry connection errors as well as 429/5xx, with exponential
        # backoff, and honour Retry-After sent by the server.
        retry = Retry(
            total=config.retry,
            backoff_factor=1,
            status_forcelist=(429, 500, 502, 503, 504),
            allowed_methods=frozenset(['GET']),
            respect_retry_after_header=True,
            raise_on_status=False)
        self.session.mount('http://', HTTPAdapter(max_retries=retry))
        self.session.mount('https://', HTTPAdapter(max_retries=retry))
        # Nominatim's usage policy asks for an honest User-Agent that
        # identifies the application; do not pretend to be a browser.
        self.session.headers.update({
            'accept': 'application/json',
            'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'User-Agent': config.user_agent or DEFAULT_USER_AGENT
        })

    def get(self, url):
        """Send GET request and return response text, or None on failure."""
        try:
            response = self.session.get(url=url, timeout=self.config.timeout)
            if response.status_code != requests.codes.ok:
                logging.error(
                    "Http request failed by url: %s, code: %d, body: %s" %
                    (redact_url(url), response.status_code, response.text[:500]))
                return None
            return response.text
        except Exception as e:
            logging.error("Http request exception by url: %s, %s: %s" %
                          (redact_url(url), type(e).__name__, e))
            return None


# ---------- Address backup (for rollback) ----------

# Original values are kept in addresses.raw under this key before a map API
# overwrites them, so that --restore-original can roll them back.
BACKUP_KEY = 'teslamate_fix_addrs'

BACKUP_FIELDS = ('display_name', 'name', 'house_number', 'road',
                 'neighbourhood', 'city', 'county', 'state', 'country')


def load_raw(raw):
    '''addresses.raw is jsonb in PostgreSQL (dict) but may be text elsewhere.'''
    if raw is None:
        return {}
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str):
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            return {'_raw': raw}
        return data if isinstance(data, dict) else {'_raw': data}
    return {'_raw': raw}


def backup_original(address_record, provider):
    '''Save the current values once, before the first overwrite.'''
    raw = load_raw(address_record.raw)
    meta = dict(raw.get(BACKUP_KEY) or {})
    if 'original' not in meta:
        meta['original'] = {f: getattr(address_record, f)
                            for f in BACKUP_FIELDS}
    meta['provider'] = provider
    meta['updated_at'] = datetime.now().replace(microsecond=0).isoformat()
    raw[BACKUP_KEY] = meta
    address_record.raw = json.dumps(raw, ensure_ascii=False)


# ---------- Reverse Geocoder ----------

class ReverseGeocoder:
    """Base class for reverse geocoding providers.

    Subclasses must implement reverse_geocode() and update_address().
    To switch to a different map API, create a new subclass.

    Coordinates passed to reverse_geocode() are always WGS84 (GPS), exactly
    as recorded by TeslaMate. A provider that works in another coordinate
    system must convert internally and must never write converted
    coordinates back to the database.
    """

    name = 'base'

    def __init__(self, http_client, config):
        self.http_client = http_client
        self.config = config

    def reverse_geocode(self, lat, lng):
        """Reverse geocode WGS84 coordinates.

        Returns provider result dict, or None when this location can not be
        resolved. Raises GeocoderUnavailable when the service can not be used
        at all (network error, quota exceeded, invalid key).
        """
        raise NotImplementedError

    def update_address(self, address_record, result):
        """Update address record fields from geocoding result.
        Returns True if the record was changed."""
        raise NotImplementedError


class TencentGeocoder(ReverseGeocoder):
    """Tencent Maps reverse geocoding implementation."""

    name = 'tencent'
    GEOCODER_PATH = "/ws/geocoder/v1/"
    GEOCODER_URL = "https://apis.map.qq.com" + GEOCODER_PATH

    # Statuses meaning the key/quota can not be used now: stop this round
    # instead of marking every address as failed.
    # 110 source not authorized, 111 signature error, 112 IP not authorized,
    # 113 feature not authorized, 120 QPS limit, 121 daily quota exceeded,
    # 122 per-IP QPS limit, 190 invalid key, 199 webservice not enabled,
    # 311 key format error.
    FATAL_STATUS = {110, 111, 112, 113, 120, 121, 122, 190, 199, 311}

    MUNICIPALITIES = ('北京市', '天津市', '上海市', '重庆市')

    @staticmethod
    def _calculate_sig(path, params, sk):
        """Calculate Tencent Maps SK signature (MD5)."""
        sorted_params = sorted(params.items(), key=lambda x: x[0])
        query_string = "&".join("%s=%s" % (k, v) for k, v in sorted_params)
        raw_string = "%s?%s%s" % (path, query_string, sk)
        return hashlib.md5(raw_string.encode('utf-8')).hexdigest()

    @staticmethod
    def _get_safe(d, key, default=''):
        """Safely get a string value from dict."""
        val = d.get(key, default)
        if isinstance(val, str):
            return val
        return default

    def reverse_geocode(self, lat, lng):
        """Call Tencent Maps reverse geocoding API. Returns parsed JSON or None."""
        if not self.config.tencent_key:
            raise GeocoderUnavailable("Tencent key is not set.")

        params = {
            'key': self.config.tencent_key,
            'location': "%.6f,%.6f" % (lat, lng),
            # 1 = GPS (WGS84). Tencent converts to GCJ-02 internally, so the
            # GPS coordinates recorded by TeslaMate are used without any
            # local conversion and without offset.
            'coord_type': '1',
            'get_poi': '0',
        }

        if self.config.tencent_sk:
            sig = self._calculate_sig(
                self.GEOCODER_PATH, params, self.config.tencent_sk)
            params['sig'] = sig

        query_string = urllib.parse.urlencode(params)
        url = "%s?%s" % (self.GEOCODER_URL, query_string)

        raw = self.http_client.get(url)
        # Rate limit: sleep regardless of outcome to avoid hammering the API.
        STOP.wait(self.config.geocoder_interval)
        if raw is None:
            raise GeocoderUnavailable("Tencent Maps API is unavailable.")

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            raise GeocoderUnavailable(
                "Tencent geocoder returned invalid JSON: %s" % e)
        if not isinstance(result, dict):
            raise GeocoderUnavailable(
                "Tencent geocoder returned unexpected data: %s" % raw[:500])

        status = result.get('status')
        if status in self.FATAL_STATUS:
            raise GeocoderUnavailable(
                "Tencent geocoder refused the request (status=%s, %s)." %
                (status, result.get('message')))
        if status != 0:
            logging.error("Tencent geocoder error: %s" % raw)
            return None

        logging.debug("Tencent raw response: %s" %
                      json.dumps(result, ensure_ascii=False))
        return result

    def update_address(self, address_record, result):
        """Update address record with Tencent Maps response data.

        Only non-empty values overwrite existing ones; road and house_number
        are always updated together so they never mismatch. Coordinates of
        the address are never touched.
        """
        r = result.get('result') or {}
        component = r.get('address_component') or {}
        formatted = r.get('formatted_addresses') or {}

        country = self._get_safe(component, 'nation')
        province = self._get_safe(component, 'province')
        city = self._get_safe(component, 'city')
        district = self._get_safe(component, 'district')
        street = self._get_safe(component, 'street')
        street_number = self._get_safe(component, 'street_number')
        neighbourhood = self._get_safe(component, 'neighbourhood')
        display_name = self._get_safe(formatted, 'recommend') or \
            self._get_safe(r, 'address')

        logging.info("Tencent raw: country=%s, province=%s, city=%s, "
                     "district=%s, street=%s, street_number=%s, "
                     "neighbourhood=%s, recommend=%s, rough=%s" %
                     (country, province, city, district, street, street_number,
                      neighbourhood, display_name,
                      self._get_safe(formatted, 'rough')))

        if not country:
            logging.warning("Tencent returned no address for id=%d, keep it." %
                            address_record.id)
            return False
        if country != '中国' and not self.config.update_overseas:
            logging.info("Address id=%d is outside China (%s), keep OSM "
                         "address." % (address_record.id, country))
            return False

        # Handle municipalities (directly-administered cities).
        # For 北京市/天津市/上海市/重庆市: state and city are both the municipality name,
        # district (e.g. 海淀区) stays in county.
        if province in self.MUNICIPALITIES:
            city = province

        # Derive name: use recommend, fall back to street.
        name = display_name or street

        new_values = {
            'country': country,
            'state': province,
            'city': city,
            'county': district,
            'neighbourhood': neighbourhood,
            'display_name': display_name,
            'name': name,
        }
        # '' and NULL display the same: not a change.
        changes = {k: v for k, v in new_values.items()
                   if v and (getattr(address_record, k) or None) != v}
        if street:
            # road and house_number belong together: replace both, even if
            # Tencent has no house number for this street.
            for k, v in (('road', street), ('house_number', street_number or None)):
                if (getattr(address_record, k) or None) != v:
                    changes[k] = v

        if not changes:
            logging.info("Address id=%d is already up to date." %
                         address_record.id)
            return False

        backup_original(address_record, self.name)
        for k, v in changes.items():
            logging.info("  address id=%d %s: %s -> %s" %
                         (address_record.id, k, getattr(address_record, k), v))
            setattr(address_record, k, v)
        address_record.updated_at = datetime.now().replace(microsecond=0)
        return True


# ---------- OSM helpers ----------

OSM_RESOLVE_URL = ("https://nominatim.openstreetmap.org/reverse?"
                   "lat=%.6f&lon=%.6f&format=jsonv2&addressdetails=1"
                   "&extratags=1&namedetails=1&zoom=18")


def get_address_str(address, addr_keys):
    '''get address value from multiple keys.'''
    for addr_key in addr_keys:
        if addr_key in address:
            return address[addr_key]
    return None


def get_address_name(address):
    '''
    address names comes from multiple places, in priority order:
    1. address.name.
    2. address.namedetails.name.
    3. address.namedetails.alt_name.
    4. first element in address.display_name.
    '''
    namedetails = address.get('namedetails') or {}
    name = address.get('name') or namedetails.get('name') or \
        namedetails.get('alt_name') or ''
    if not name:
        name = (address.get('display_name') or '').split(',')[0]
    return name


def parse_osm_response(raw, lat, lng):
    '''Return Nominatim result dict, or None if the location has no address.

    Nominatim answers HTTP 200 with {"error": "Unable to geocode"} for
    places without address (sea, remote areas). Such response must not crash
    the program.
    '''
    try:
        osm_address = json.loads(raw)
    except json.JSONDecodeError as e:
        raise GeocoderUnavailable("OSM returned invalid JSON: %s" % e)

    if not isinstance(osm_address, dict):
        logging.warning("OSM returned unexpected data for (%.6f, %.6f): %s" %
                        (lat, lng, raw[:500]))
        return None
    if 'error' in osm_address:
        logging.warning("OSM can not resolve (%.6f, %.6f): %s" %
                        (lat, lng, osm_address['error']))
        return None
    missing = [k for k in ('osm_id', 'osm_type', 'address', 'display_name')
               if not osm_address.get(k)]
    if missing:
        logging.warning("OSM result for (%.6f, %.6f) misses %s, skip." %
                        (lat, lng, ', '.join(missing)))
        return None

    logging.debug("OSM raw response: %s" %
                  json.dumps(osm_address, ensure_ascii=False))
    return osm_address


# ---------- Database helpers ----------

def json_dumps_keep_str(d):
    '''JSON serializer for the jsonb raw column: strings are already JSON
    (raw response text), anything else is serialized without escaping.'''
    if isinstance(d, str):
        return d
    return json.dumps(d, ensure_ascii=False)


def create_engine_from_config(config):
    """Create SQLAlchemy engine from Config."""
    kwargs = dict(json_serializer=json_dumps_keep_str, echo=False,
                  pool_pre_ping=True)
    if config.db_url:
        return create_engine(config.db_url, **kwargs)

    # Name the driver explicitly: SQLAlchemy 2.1 changed the default
    # postgresql driver from psycopg2 to psycopg (3).
    conn_url = URL.create(
        drivername="postgresql+psycopg2",
        username=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
        database=config.dbname
    )
    return create_engine(conn_url, **kwargs)


def reflect_tables(engine):
    """Reflect ORM classes from database tables."""
    Base = automap_base()
    Base.prepare(autoload_with=engine)
    return (
        Base.classes.drives,
        Base.classes.charging_processes,
        Base.classes.positions,
        Base.classes.addresses,
    )


def finish_session(session, config):
    '''Commit, or roll back in dry-run mode.'''
    if config.dry_run:
        logging.info("dry-run: rolling back changes.")
        session.rollback()
    else:
        logging.info("saving...")
        session.commit()


def get_position(session, position_id, Positions):
    '''get position from table positions by position_id.'''
    position = session.get(Positions, position_id)
    if position is None or position.latitude is None or \
            position.longitude is None:
        logging.warning("Position with ID %s is not found." % position_id)
        return None
    return position


def get_address_in_db(session, Addresses, osm_id, osm_type):
    '''select address from db by (osm_id, osm_type), TeslaMate's unique key.'''
    return session.query(Addresses).filter(
        Addresses.osm_id == osm_id,
        Addresses.osm_type == osm_type).first()


def add_osm_address(session, Addresses, osm_address, raw):
    '''Add osm address to db if not exists. Returns the address record.'''
    exist_address = get_address_in_db(session, Addresses,
                                      osm_address['osm_id'],
                                      osm_address['osm_type'])
    if exist_address is not None:
        logging.info("address is already exist: %s %d, %s." %
                     (osm_address['osm_type'], osm_address['osm_id'],
                      osm_address['display_name']))
        return exist_address

    logging.info("osm id = %d is not exist!" % osm_address['osm_id'])
    address = Addresses(
        display_name=osm_address['display_name'],
        latitude=osm_address.get('lat'),
        longitude=osm_address.get('lon'),
        name=get_address_name(osm_address),
        house_number=get_address_str(osm_address['address'],
                                     house_number_aliases),
        road=get_address_str(osm_address['address'], road_aliases),
        neighbourhood=get_address_str(osm_address['address'],
                                      neighborhood_aliases),
        city=get_address_str(osm_address['address'], city_aliases),
        county=get_address_str(osm_address['address'], county_aliases),
        postcode=get_address_str(osm_address['address'], ['postcode']),
        state=get_address_str(osm_address['address'], state_aliases),
        state_district=get_address_str(osm_address['address'],
                                       ['state_district']),
        country=get_address_str(osm_address['address'], country_aliases),
        raw=raw,
        inserted_at=datetime.now().replace(microsecond=0),
        updated_at=datetime.now().replace(microsecond=0),
        osm_id=osm_address['osm_id'],
        osm_type=osm_address['osm_type'])
    session.add(address)
    session.flush()
    logging.info("address added: %s." % osm_address['display_name'])
    return address


def resolve_osm_address(session, http_client, position, Addresses):
    '''
    Return (address_id, display_name) by resolving position via OSM, or
    (None, None) when OSM has no address there.
    Address will be added into db if not exists.
    Raises GeocoderUnavailable when OSM can not be reached.
    '''
    lat, lng = float(position.latitude), float(position.longitude)
    url = OSM_RESOLVE_URL % (lat, lng)
    raw = http_client.get(url)
    # OSM Nominatim rate limit (policy: max 1 req/s)
    STOP.wait(http_client.config.osm_interval)
    if raw is None:
        raise GeocoderUnavailable("OSM Nominatim is unavailable.")

    osm_address = parse_osm_response(raw, lat, lng)
    if osm_address is None:
        return None, None

    address = add_osm_address(session, Addresses, osm_address, raw)
    return address.id, address.display_name


# ---------- Mode 0: Fix empty records ----------

def empty_drives_query(session, Drives):
    '''drives having at least one empty address that can be resolved.'''
    return session.query(Drives).filter(or_(
        and_(Drives.start_address_id.is_(None),
             Drives.start_position_id.is_not(None)),
        and_(Drives.end_address_id.is_(None),
             Drives.end_position_id.is_not(None))))


def empty_chargings_query(session, ChargingProcesses):
    '''charging processes with empty address that can be resolved.'''
    return session.query(ChargingProcesses) \
        .filter(ChargingProcesses.address_id.is_(None)) \
        .filter(ChargingProcesses.position_id.is_not(None))


def fix_drive(session, http_client, tables, record):
    '''Fill empty start/end address of one drive. Returns True if complete.
    A side that resolves is saved even when the other side fails.'''
    Drives, ChargingProcesses, Positions, Addresses = tables
    complete = True
    for side in ('start', 'end'):
        if getattr(record, side + '_address_id') is not None:
            continue
        position_id = getattr(record, side + '_position_id')
        if position_id is None:
            continue
        position = get_position(session, position_id, Positions)
        if position is None:
            complete = False
            continue
        addr_id, addr = resolve_osm_address(
            session, http_client, position, Addresses)
        if addr_id is None:
            complete = False
            continue
        setattr(record, side + '_address_id', addr_id)
        logging.info("Changing drives(id = %d) %s address to %s" %
                     (record.id, side, addr))
    return complete


def fix_charging(session, http_client, tables, record):
    '''Fill empty address of one charging process. Returns True if fixed.'''
    Drives, ChargingProcesses, Positions, Addresses = tables
    position = get_position(session, record.position_id, Positions)
    if position is None:
        return False
    addr_id, addr = resolve_osm_address(
        session, http_client, position, Addresses)
    if addr_id is None:
        return False
    record.address_id = addr_id
    logging.info("Changing charging(id = %d) to %s" % (record.id, addr))
    return True


def fix_empty_records(engine, http_client, config, tables, checkpoint):
    """Fix all empty address records in batches.

    Records are scanned in id order with a cursor, so records that can not
    be resolved never block the following ones. They are retried in the next
    round (the cursor restarts from 0 each round).
    """
    Drives, ChargingProcesses, Positions, Addresses = tables
    jobs = (
        ('drive', Drives, empty_drives_query, fix_drive, 'last_drive_id'),
        ('charging', ChargingProcesses, empty_chargings_query, fix_charging,
         'last_charging_id'),
    )
    for kind, Table, make_query, fix_record, checkpoint_key in jobs:
        with Session(engine) as session:
            total = make_query(session, Table).count()
        logging.info("found %d %s records with empty address." %
                     (total, kind))
        last_id = 0
        done = 0
        while total and not STOP.is_set():
            with Session(engine) as session:
                records = make_query(session, Table) \
                    .filter(Table.id > last_id) \
                    .order_by(Table.id) \
                    .limit(config.batch) \
                    .all()
                if not records:
                    break
                fixed = 0
                try:
                    for record in records:
                        if STOP.is_set():
                            break
                        done += 1
                        logging.info("processing %s address %d/%d (id=%d)" %
                                     (kind, done, total, record.id))
                        if fix_record(session, http_client, tables, record):
                            fixed += 1
                        else:
                            logging.warning("%s id=%d is not fully fixed, "
                                            "retry in next round." %
                                            (kind, record.id))
                        last_id = record.id
                finally:
                    # Keep what was fixed, even if OSM became unavailable.
                    finish_session(session, config)
                    if not config.dry_run:
                        checkpoint['osm_fix'][checkpoint_key] = last_id
                        checkpoint['osm_fix']['fixed_count'] += fixed
                        save_checkpoint(config.checkpoint_path, checkpoint)


# ---------- Mode 1: Update addresses via reverse geocoder ----------

# Linked positions read per source (most recent first) to pick a typical one.
MAX_LINKED_POSITIONS = 500

# Warn when positions sharing one address are spread wider than this.
SPREAD_WARN_METERS = 300


def distance_m(lat1, lng1, lat2, lng2):
    '''Approximate distance in meters (equirectangular, fine below ~100 km).'''
    x = math.radians(lng2 - lng1) * math.cos(math.radians((lat1 + lat2) / 2))
    y = math.radians(lat2 - lat1)
    return math.hypot(x, y) * 6371000


def get_geocode_coordinates(session, tables, address_record):
    '''Return (lat, lng, source) used to reverse geocode an address.

    addresses.latitude/longitude is where OSM put the matched object (its
    centroid), which can be hundreds of meters away from the car, or more
    for a long road. Use the GPS positions the car actually recorded and
    that are linked to this address instead.

    TeslaMate shares one address row between every drive/charge resolved to
    the same OSM object, so these positions may be spread out. Pick the real
    position closest to their median: the most typical place, not skewed by
    one occasional stop. Fall back to the address coordinates.
    '''
    Drives, ChargingProcesses, Positions, Addresses = tables
    candidates = (
        ('charging', ChargingProcesses, ChargingProcesses.position_id,
         ChargingProcesses.address_id),
        ('drive end', Drives, Drives.end_position_id, Drives.end_address_id),
        ('drive start', Drives, Drives.start_position_id,
         Drives.start_address_id),
    )
    points = []
    for source, Table, position_col, address_col in candidates:
        rows = session.query(Positions.latitude, Positions.longitude) \
            .join(Table, position_col == Positions.id) \
            .filter(address_col == address_record.id) \
            .filter(Positions.latitude.is_not(None)) \
            .filter(Positions.longitude.is_not(None)) \
            .order_by(Table.id.desc()) \
            .limit(MAX_LINKED_POSITIONS) \
            .all()
        points.extend((float(r.latitude), float(r.longitude), source)
                      for r in rows)

    if not points:
        if address_record.latitude is None or \
                address_record.longitude is None:
            return None, None, None
        return float(address_record.latitude), \
            float(address_record.longitude), 'address'

    median_lat = statistics.median(p[0] for p in points)
    median_lng = statistics.median(p[1] for p in points)
    lat, lng, source = min(
        points, key=lambda p: distance_m(p[0], p[1], median_lat, median_lng))

    spread = max(distance_m(lat, lng, p[0], p[1]) for p in points)
    if spread > SPREAD_WARN_METERS:
        logging.warning("Address id=%d is shared by %d positions up to %d m "
                        "apart, using the most typical one." %
                        (address_record.id, len(points), spread))
    return lat, lng, '%s (typical of %d)' % (source, len(points))


def update_address_record(session, geocoder, tables, address_record):
    '''Geocode and update one address.
    Returns True if updated, False if kept, None if geocoding failed.'''
    lat, lng, source = get_geocode_coordinates(session, tables, address_record)
    if source is None:
        logging.warning("Address id=%d has no coordinates, skip." %
                        address_record.id)
        return False
    logging.info("geocoding address id=%d at (%.6f, %.6f) from %s position" %
                 (address_record.id, lat, lng, source))
    result = geocoder.reverse_geocode(lat, lng)
    if result is None:
        return None
    return geocoder.update_address(address_record, result)


def get_update_record_count(session, Addresses, config, last_address_id):
    """Count addresses that need updating."""
    return session \
        .query(Addresses) \
        .filter(Addresses.updated_at >= config.since) \
        .filter(Addresses.id > last_address_id) \
        .count()


def get_need_update_addresses(session, Addresses, config, last_address_id):
    """Get batch of addresses to update."""
    return session \
        .query(Addresses) \
        .filter(Addresses.updated_at >= config.since) \
        .filter(Addresses.id > last_address_id) \
        .order_by(Addresses.id) \
        .limit(config.batch) \
        .all()


def update_address_batch(session, geocoder, config, tables, records,
                         checkpoint, total_remaining=None):
    """Update given addresses via reverse geocoder and track failures.

    When total_remaining is given, records come from the cursor and the
    cursor advances; otherwise they are retries of failed addresses.
    Returns updated count.
    """
    state = checkpoint['map_update']
    failed = state['failed_ids']
    updated = 0
    try:
        for i, address_record in enumerate(records):
            if STOP.is_set():
                break
            if total_remaining is None:
                logging.info("retrying failed address %d/%d (id=%d)" %
                             (i + 1, len(records), address_record.id))
            else:
                logging.info("processing update address %d/%d "
                             "(total remaining: %d, id=%d)" %
                             (i + 1, len(records), total_remaining - i,
                              address_record.id))
            # Raises GeocoderUnavailable: the record is neither advanced nor
            # counted as failed, it is processed again next round.
            outcome = update_address_record(
                session, geocoder, tables, address_record)
            key = str(address_record.id)
            if outcome is None:
                attempts = failed.get(key, 0) + 1
                if attempts >= config.geocoder_max_attempts:
                    failed.pop(key, None)
                    if address_record.id not in state['abandoned_ids']:
                        state['abandoned_ids'].append(address_record.id)
                    logging.warning("Geocoding failed %d times for address "
                                    "id=%d, give up." %
                                    (attempts, address_record.id))
                else:
                    failed[key] = attempts
                    logging.warning("Geocoding failed for address id=%d "
                                    "(attempt %d), retry in next round." %
                                    (address_record.id, attempts))
            else:
                failed.pop(key, None)
                if outcome:
                    updated += 1
            if total_remaining is not None:
                state['last_address_id'] = address_record.id
    finally:
        finish_session(session, config)
        if not config.dry_run:
            state['updated_count'] += updated
            save_checkpoint(config.checkpoint_path, checkpoint)
    return updated


def update_addresses(engine, geocoder, config, tables, checkpoint):
    """Retry previously failed addresses, then update new addresses."""
    Addresses = tables[3]
    state = checkpoint['map_update']

    failed_ids = sorted(int(i) for i in state['failed_ids'])
    for start in range(0, len(failed_ids), config.batch):
        if STOP.is_set():
            return
        chunk = failed_ids[start:start + config.batch]
        with Session(engine) as session:
            records = session.query(Addresses) \
                .filter(Addresses.id.in_(chunk)) \
                .order_by(Addresses.id) \
                .all()
            # Addresses deleted meanwhile do not need retry.
            for missing in set(chunk) - {r.id for r in records}:
                state['failed_ids'].pop(str(missing), None)
            update_address_batch(session, geocoder, config, tables, records,
                                 checkpoint)

    while not STOP.is_set():
        with Session(engine) as session:
            logging.info("updating addresses...")
            last_address_id = state['last_address_id']
            total_remaining = get_update_record_count(
                session, Addresses, config, last_address_id)
            records = get_need_update_addresses(
                session, Addresses, config, last_address_id)
            if not records:
                break
            update_address_batch(session, geocoder, config, tables, records,
                                 checkpoint, total_remaining)


def restore_original_addresses(engine, config, tables):
    """Roll back addresses overwritten by a map API to the saved values."""
    Addresses = tables[3]
    last_id = 0
    restored = 0
    while not STOP.is_set():
        with Session(engine) as session:
            records = session.query(Addresses) \
                .filter(Addresses.id > last_id) \
                .order_by(Addresses.id) \
                .limit(max(config.batch, 100)) \
                .all()
            if not records:
                break
            for address_record in records:
                last_id = address_record.id
                raw = load_raw(address_record.raw)
                meta = raw.pop(BACKUP_KEY, None)
                if not meta or 'original' not in meta:
                    continue
                for k, v in meta['original'].items():
                    setattr(address_record, k, v)
                address_record.raw = json.dumps(raw, ensure_ascii=False)
                address_record.updated_at = datetime.now().replace(microsecond=0)
                logging.info("restored address id=%d to %s" %
                             (address_record.id, address_record.display_name))
                restored += 1
            finish_session(session, config)
    logging.info("%d addresses restored." % restored)


# ---------- Argument parsing ----------

def parse_args():
    """Parse command line arguments and return a Config object."""
    parser = argparse.ArgumentParser(description='Usage of address fixer.')
    parser.add_argument(
        "--db-url", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_URL",
        help="full database URL, overrides individual db params(DB_URL).")
    parser.add_argument(
        "-u", "--user", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_USER",
        help="db user name(DB_USER).")
    parser.add_argument(
        "-p", "--password", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_PASSWD",
        help="db password(DB_PASSWD).")
    parser.add_argument(
        "-H", "--host", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_HOST",
        help="db host name or ip address(DB_HOST).")
    parser.add_argument(
        "-P", "--port", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_PORT",
        help="db port(DB_PORT).")
    parser.add_argument(
        "-d", "--dbname", required=False, type=str, default='',
        action=EnvDefault, envvar="DB_NAME",
        help="db name(DB_NAME).")
    parser.add_argument(
        "-b", "--batch", required=False, type=int, default=10,
        action=EnvDefault, envvar="BATCH",
        help="batch size for one loop(BATCH).")
    parser.add_argument(
        "-t", "--timeout", required=False, type=int, default=5,
        action=EnvDefault, envvar="HTTP_TIMEOUT",
        help="http request timeout(s)(HTTP_TIMEOUT).")
    parser.add_argument(
        "-r", "--retry", required=False, type=int, default=5,
        action=EnvDefault, envvar="HTTP_RETRY",
        help="http request max retries(HTTP_RETRY).")
    parser.add_argument(
        "-i", "--interval", required=False, type=int, default=0,
        action=EnvDefault, envvar="INTERVAL",
        help="if value not 0, run in infinity mode, "
             "fix record in every interval seconds(INTERVAL).")
    parser.add_argument(
        "-m", "--mode", required=False, type=int, default=0,
        action=EnvDefault, envvar="MODE",
        help="run mode: 0 -> fix empty record; "
             "1 -> update address by map api; 2 -> do both(MODE).")
    parser.add_argument(
        "-k", "--key", required=False, type=str, default='',
        action=EnvDefault, envvar="TENCENT_KEY",
        help="API key for calling tencent maps(TENCENT_KEY).")
    parser.add_argument(
        "--sk", required=False, type=str, default='',
        action=EnvDefault, envvar="TENCENT_SK",
        help="SK for tencent maps signature(TENCENT_SK).")
    parser.add_argument(
        "-s", "--since", required=False,
        type=lambda d: datetime.strptime(d, '%Y-%m-%d'),
        default=datetime.min,
        action=EnvDefault, envvar="SINCE",
        help="Update from specified date(YYYY-mm-dd).")
    parser.add_argument(
        "-ua", "--user_agent", required=False, type=str,
        default=DEFAULT_USER_AGENT,
        action=EnvDefault, envvar="USER_AGENT",
        help="Custom User-Agent for HTTP requests(USER_AGENT).")
    parser.add_argument(
        "--osm-interval", required=False, type=float, default=1.0,
        action=EnvDefault, envvar="OSM_INTERVAL",
        help="seconds to sleep between OSM requests(OSM_INTERVAL).")
    parser.add_argument(
        "--geocoder-interval", required=False, type=float, default=0.3,
        action=EnvDefault, envvar="GEOCODER_INTERVAL",
        help="seconds to sleep between geocoder API requests(GEOCODER_INTERVAL).")
    parser.add_argument(
        "--geocoder-max-attempts", required=False, type=int, default=3,
        action=EnvDefault, envvar="GEOCODER_MAX_ATTEMPTS",
        help="give up an address after this many failed geocoding "
             "attempts(GEOCODER_MAX_ATTEMPTS).")
    parser.add_argument(
        "--update-overseas", required=False, action='store_true',
        default=env_bool("UPDATE_OVERSEAS"),
        help="also overwrite addresses outside China with map api "
             "results(UPDATE_OVERSEAS).")
    parser.add_argument(
        "-c", "--checkpoint", required=False, type=str,
        default='checkpoint.json',
        action=EnvDefault, envvar="CHECKPOINT_FILE",
        help="checkpoint file path(CHECKPOINT_FILE).")
    parser.add_argument(
        "--reset-checkpoint", required=False,
        action='store_true', default=False,
        help="reset checkpoint and start fresh.")
    parser.add_argument(
        "--dry-run", required=False, action='store_true',
        default=env_bool("DRY_RUN"),
        help="log what would change but write nothing to db(DRY_RUN).")
    parser.add_argument(
        "--restore-original", required=False, action='store_true',
        default=False,
        help="restore addresses overwritten by map api to saved values, "
             "then exit.")

    args = parser.parse_args()

    if not args.db_url and not all([args.user, args.password, args.host, args.port, args.dbname]):
        parser.error("Either --db-url (DB_URL) or all of -u/-p/-H/-P/-d must be provided.")

    mode = int(args.mode)
    if mode not in (0, 1, 2):
        parser.error("mode must be 0, 1 or 2, got %d." % mode)
    if mode in (1, 2) and not args.key and not args.restore_original:
        parser.error("mode %d needs a Tencent key (-k / TENCENT_KEY)." % mode)
    if int(args.batch) <= 0:
        parser.error("batch must be greater than 0.")

    since = args.since
    if isinstance(since, str):
        since = datetime.strptime(since, '%Y-%m-%d')

    return Config(
        user=args.user,
        password=args.password,
        host=args.host,
        port=args.port,
        dbname=args.dbname,
        db_url=args.db_url,
        batch=int(args.batch),
        timeout=int(args.timeout),
        retry=int(args.retry),
        interval=int(args.interval),
        mode=mode,
        tencent_key=args.key,
        tencent_sk=args.sk,
        since=since,
        user_agent=args.user_agent,
        checkpoint_path=args.checkpoint,
        reset_checkpoint=args.reset_checkpoint,
        osm_interval=float(args.osm_interval),
        geocoder_interval=float(args.geocoder_interval),
        geocoder_max_attempts=max(1, int(args.geocoder_max_attempts)),
        update_overseas=args.update_overseas,
        dry_run=args.dry_run,
        restore_original=args.restore_original,
    )


# ---------- Main ----------

def run_once(engine, http_client, config, tables, checkpoint, geocoder):
    """Run one cycle of the configured mode. Returns False if a geocoding
    service was unavailable."""
    ok = True
    if config.mode == 0 or config.mode == 2:
        try:
            fix_empty_records(engine, http_client, config, tables, checkpoint)
        except GeocoderUnavailable as e:
            logging.error("%s Stop fixing empty addresses in this round." % e)
            ok = False
    if (config.mode == 1 or config.mode == 2) and not STOP.is_set():
        try:
            update_addresses(engine, geocoder, config, tables, checkpoint)
        except GeocoderUnavailable as e:
            logging.error("%s Stop updating addresses in this round." % e)
            ok = False
    return ok


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    config = parse_args()
    # APP_BUILD is set by the docker image build (git commit).
    logging.info("teslamate_fix_addrs %s (build %s)" %
                 (__version__, os.environ.get('APP_BUILD') or 'local'))
    if config.dry_run:
        logging.warning("dry-run mode: changes are logged but not saved.")
    engine = create_engine_from_config(config)
    http_client = HttpClient(config)
    geocoder = TencentGeocoder(http_client, config)

    if config.reset_checkpoint:
        logging.info("Resetting checkpoint.")
        checkpoint = _default_checkpoint()
    else:
        checkpoint = load_checkpoint(config.checkpoint_path)

    tables = None
    ok = True
    while not STOP.is_set():
        try:
            if tables is None:
                tables = reflect_tables(engine)
            if config.restore_original:
                restore_original_addresses(engine, config, tables)
                break
            ok = run_once(engine, http_client, config, tables, checkpoint,
                          geocoder)
        except Exception:
            if config.interval == 0:
                raise
            # Database restarted, network glitch...: keep the service alive.
            logging.exception("Round failed, retry in %d seconds." %
                              config.interval)
        if config.interval == 0:
            break
        logging.info("sleeping for %d seconds..." % config.interval)
        STOP.wait(config.interval)

    logging.info("bye.")
    if config.interval == 0 and not ok:
        sys.exit(1)


if __name__ == '__main__':
    main()
