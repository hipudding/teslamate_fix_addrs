from dataclasses import dataclass, field
from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, or_
from sqlalchemy.engine.url import URL
import requests
from requests.adapters import HTTPAdapter
import json
import hashlib
import urllib.parse
from datetime import datetime
import logging
import re
import argparse
import os
import signal
import tempfile
import time

logging.basicConfig(
    level=getattr(logging, os.environ.get('LOG_LEVEL', 'INFO').upper(), logging.INFO),
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)


def handler(signum, frame):
    '''Ctrl-C handler.'''
    logging.info("Ctrl-C pressed, exit.")
    os._exit(0)


signal.signal(signal.SIGINT, handler)


class EnvDefault(argparse.Action):
    '''args priority: cli args -> ENV -> default.'''

    def __init__(self, envvar, required=True, default=None, **kwargs):
        if envvar in os.environ:
            default = os.environ[envvar]
        if required and default:
            required = False
        super(EnvDefault, self).__init__(default=default,
                                         required=required,
                                         **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


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
    user_agent: str = 'teslamate/#v1.29.2'
    checkpoint_path: str = 'checkpoint.json'
    reset_checkpoint: bool = False
    osm_interval: float = 1.0
    geocoder_interval: float = 0.3
    short_names: bool = False


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
            "updated_count": 0
        },
        "last_run": None
    }


def load_checkpoint(path):
    """Load checkpoint from JSON file. Returns defaults if file doesn't exist."""
    if os.path.exists(path):
        try:
            with open(path, 'r') as f:
                data = json.load(f)
            # Migrate old checkpoint key.
            if 'tencent_update' in data and 'map_update' not in data:
                data['map_update'] = data.pop('tencent_update')
            logging.info("Loaded checkpoint from %s" % path)
            return data
        except (json.JSONDecodeError, IOError) as e:
            logging.error("Failed to load checkpoint: %s" % e)
    return _default_checkpoint()


def save_checkpoint(path, data):
    """Save checkpoint to JSON file atomically (write tmp then rename)."""
    data['last_run'] = datetime.now().replace(microsecond=0).isoformat()
    abs_path = os.path.abspath(path)
    dir_name = os.path.dirname(abs_path)
    fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix='.tmp')
    try:
        with os.fdopen(fd, 'w') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
        os.replace(tmp_path, abs_path)
        logging.info("Checkpoint saved to %s" % path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


# ---------- HTTP client ----------

class HttpClient:
    """Reusable HTTP client wrapping requests.Session."""

    def __init__(self, config: Config):
        self.config = config
        self.session = requests.Session()
        self.session.mount('http://', HTTPAdapter(max_retries=config.retry))
        self.session.mount('https://', HTTPAdapter(max_retries=config.retry))
        self.session.headers.update({
            'accept': 'text/html,application/xhtml+xml,application/xml;'
                      'q=0.9,image/avif,image/webp,image/apng,*/*;'
                      'q=0.8,application/signed-exchange;v=b3;q=0.7',
            'accept-language': 'zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7',
            'cache-control': 'max-age=0',
            'dnt': '1',
            'priority': 'u=0, i',
            'sec-ch-ua': '"Chromium";v="128", "Not;A=Brand";v="24", '
                         '"Google Chrome";v="128"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"macOS"',
            'sec-fetch-dest': 'document',
            'sec-fetch-mode': 'navigate',
            'sec-fetch-site': 'none',
            'sec-fetch-user': '?1',
            'upgrade-insecure-requests': '1',
            'User-Agent': config.user_agent
        })

    def get(self, url):
        """Send GET request and return response text, or None on failure."""
        try:
            response = self.session.get(url=url, timeout=self.config.timeout)
            if response.status_code != requests.codes.ok:
                logging.error(
                    "Http request failed by url: %s, code: %d, body: %s" %
                    (url, response.status_code, response.text))
                return None
            return response.text
        except Exception:
            logging.error("Http request exception by url: %s" % (url,))
            return None


# ---------- Reverse Geocoder ----------

class ReverseGeocoder:
    """Base class for reverse geocoding providers.

    Subclasses must implement reverse_geocode() and update_address().
    To switch to a different map API, create a new subclass.
    Subclasses must also set source_name, used as osm_type for addresses
    created by this provider.
    """

    source_name = None

    def __init__(self, http_client, config):
        self.http_client = http_client
        self.config = config

    def reverse_geocode(self, lat, lng):
        """Reverse geocode coordinates. Returns provider result dict or None."""
        raise NotImplementedError

    def update_address(self, address_record, result):
        """Update address record fields from geocoding result."""
        raise NotImplementedError


TRAILING_PARENTHETICAL = re.compile(r'\s*[（(][^（）()]*[）)]\s*$')


def shorten_name(name, district):
    '''Drop the leading district and the trailing parenthetical, so names line
    up with the shorter form Amap returns. Falls back to the original name if
    nothing would be left.'''
    shortened = name
    if district and shortened.startswith(district):
        shortened = shortened[len(district):]
    shortened = TRAILING_PARENTHETICAL.sub('', shortened).strip()
    return shortened or name


class TencentGeocoder(ReverseGeocoder):
    """Tencent Maps reverse geocoding implementation."""

    source_name = 'tencent'

    GEOCODER_PATH = "/ws/geocoder/v1/"
    GEOCODER_URL = "https://apis.map.qq.com" + GEOCODER_PATH

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

    @staticmethod
    def _get_reference_title(reference, *keys):
        """Return the title of the first present address_reference entry."""
        for key in keys:
            entry = reference.get(key)
            if isinstance(entry, dict):
                title = entry.get('title')
                if isinstance(title, str) and title:
                    return title
        return ''

    def reverse_geocode(self, lat, lng):
        """Call Tencent Maps reverse geocoding API. Returns parsed JSON or None."""
        if not self.config.tencent_key:
            logging.error("Tencent key is not set.")
            return None

        params = {
            'key': self.config.tencent_key,
            'location': "%s,%s" % (lat, lng),
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
        time.sleep(self.config.geocoder_interval)
        if raw is None:
            return None

        try:
            result = json.loads(raw)
        except json.JSONDecodeError as e:
            logging.error("Tencent geocoder returned invalid JSON: %s" % e)
            return None
        if result is None or result.get('status') != 0:
            logging.error("Tencent geocoder error: %s" % raw)
            return None

        logging.debug("Tencent raw response: %s" %
                      json.dumps(result, ensure_ascii=False))
        return result

    def update_address(self, address_record, result):
        """Update address record with Tencent Maps response data."""
        r = result.get('result', {})
        component = r.get('address_component', {})
        formatted = r.get('formatted_addresses', {})
        reference = r.get('address_reference', {})

        country = self._get_safe(component, 'nation')
        province = self._get_safe(component, 'province')
        city = self._get_safe(component, 'city')
        district = self._get_safe(component, 'district')
        street = self._get_safe(component, 'street')
        street_number = self._get_safe(component, 'street_number')
        neighbourhood = self._get_reference_title(
            reference, 'town', 'village')
        display_name = self._get_safe(formatted, 'recommend')

        if self.config.short_names and display_name:
            display_name = shorten_name(display_name, district)

        # Handle municipalities (directly-administered cities).
        # For 北京市/天津市/上海市/重庆市: state and city are both the municipality name,
        # district (e.g. 海淀区) stays in county.
        if province in ['北京市', '天津市', '上海市', '重庆市']:
            city = province

        # Derive name: use recommend, fall back to street.
        name = display_name
        if not name:
            name = street

        logging.info("update address from %s to %s" %
                     (address_record.display_name, display_name))
        logging.info("Tencent raw: country=%s, province=%s, city=%s, "
                     "district=%s, street=%s, street_number=%s, "
                     "neighbourhood=%s, recommend=%s, rough=%s" %
                     (country, province, city, district, street, street_number,
                      neighbourhood, display_name,
                      self._get_safe(formatted, 'rough')))

        address_record.country = country
        address_record.state = province
        address_record.city = city
        address_record.county = district
        address_record.display_name = display_name
        address_record.house_number = street_number
        address_record.updated_at = datetime.now().replace(microsecond=0)

        if street:
            address_record.road = street
        if name:
            address_record.name = name
        if neighbourhood:
            address_record.neighbourhood = neighbourhood


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
    address names comes from multiple places.
    1. address.name.
    2. address.namedetails.name.
    3. address.namedetails.alt_name.
    4. first element in address.display_name.
    '''
    name = ''
    if 'name' in address.keys() and len(address['name']):
        name = address['name']
    if 'namedetails' in address.keys() and address['namedetails'] is not None:
        if 'name' in address['namedetails'].keys():
            name = address['namedetails']['name']
        if 'alt_name' in address['namedetails'].keys():
            name = address['namedetails']['alt_name']
    if len(name) == 0:
        name = address['display_name'].split(',')[0]
    return name


# ---------- Database helpers ----------

def create_engine_from_config(config):
    """Create SQLAlchemy engine from Config."""
    if config.db_url:
        return create_engine(config.db_url, echo=False)

    def custom_json_dumps(d):
        '''do not add backslash in json.'''
        return d

    conn_url = URL.create(
        drivername="postgresql",
        username=config.user,
        password=config.password,
        host=config.host,
        port=config.port,
        database=config.dbname
    )
    return create_engine(conn_url, json_serializer=custom_json_dumps,
                         echo=False)


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


def get_position(session, position_id, Positions):
    '''get position from table positions by position_id.'''
    position = session.query(Positions).filter(
        Positions.id == position_id).first()
    if position is None:
        raise RuntimeError("Position with ID %s is not found." % position_id)
    return position


def get_address_in_db(session, Addresses, osm_id):
    '''select address from db by osm_id.'''
    return session.query(Addresses).filter(
        Addresses.osm_id == osm_id).first()


def add_osm_address(session, Addresses, osm_address, raw):
    '''add osm address to db.'''
    exist_address = get_address_in_db(session, Addresses,
                                      osm_address['osm_id'])
    if exist_address is None:
        logging.info("osm id = %d is not exist!" % osm_address['osm_id'])
        address = Addresses(
            display_name=osm_address['display_name'],
            latitude=osm_address['lat'],
            longitude=osm_address['lon'],
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
        logging.info("address added: %s." % osm_address['display_name'])
    else:
        logging.info("address is already exist: %d, %s." %
                     (osm_address['osm_id'], osm_address['display_name']))


def resolve_osm_address(session, http_client, position, Addresses):
    '''
    Return (address_id, display_name) by resolving position via OSM.
    Address will be added into db if not exists.
    '''
    url = OSM_RESOLVE_URL % (position.latitude, position.longitude)
    raw = http_client.get(url)
    # OSM Nominatim rate limit (policy: max 1 req/s)
    time.sleep(http_client.config.osm_interval)
    if raw is None:
        return None, None

    try:
        osm_address = json.loads(raw)
    except json.JSONDecodeError as e:
        logging.error("OSM returned invalid JSON: %s" % e)
        return None, None
    if osm_address is None:
        return None, None

    logging.debug("OSM raw response: %s" %
                  json.dumps(osm_address, ensure_ascii=False))

    add_osm_address(session, Addresses, osm_address, raw)
    added_address = get_address_in_db(session, Addresses,
                                      osm_address['osm_id'])
    return added_address.id, added_address.display_name


# ---------- Geocoder-created addresses ----------

COORD_KEY_DECIMALS = 4
COORD_KEY_SCALE = 10 ** COORD_KEY_DECIMALS
COORD_KEY_LON_DIGITS = 10 ** 7


def coord_key(latitude, longitude):
    '''Stable synthetic osm_id for a coordinate, so the same place resolves
    to a single address row instead of one row per drive.'''
    lat_units = round((float(latitude) + 90) * COORD_KEY_SCALE)
    lon_units = round((float(longitude) + 180) * COORD_KEY_SCALE)
    return lat_units * COORD_KEY_LON_DIGITS + lon_units


def get_address_by_source(session, Addresses, osm_id, osm_type):
    '''select address from db by the (osm_id, osm_type) unique key.'''
    return session.query(Addresses).filter(
        Addresses.osm_id == osm_id,
        Addresses.osm_type == osm_type).first()


def resolve_geocoder_address(session, geocoder, position, Addresses):
    '''
    Return (address_id, display_name) by resolving position via the geocoder,
    without contacting OSM. Address will be added into db if not exists.
    '''
    osm_id = coord_key(position.latitude, position.longitude)
    osm_type = geocoder.source_name

    exist_address = get_address_by_source(session, Addresses, osm_id, osm_type)
    if exist_address is not None:
        logging.info("address is already exist: %d, %s." %
                     (osm_id, exist_address.display_name))
        return exist_address.id, exist_address.display_name

    result = geocoder.reverse_geocode(position.latitude, position.longitude)
    if result is None:
        return None, None

    now = datetime.now().replace(microsecond=0)
    address = Addresses(
        latitude=position.latitude,
        longitude=position.longitude,
        raw=json.dumps(result, ensure_ascii=False),
        inserted_at=now,
        updated_at=now,
        osm_id=osm_id,
        osm_type=osm_type)
    geocoder.update_address(address, result)
    session.add(address)
    session.flush()
    logging.info("address added: %s." % address.display_name)
    return address.id, address.display_name


# ---------- Mode 0: Fix empty records ----------

def get_empty_record_count(session, Drives, ChargingProcesses):
    '''get all empty records count.'''
    empty_count = session \
        .query(Drives.id) \
        .filter(or_(Drives.start_address_id.is_(None),
                     Drives.end_address_id.is_(None))) \
        .filter(Drives.start_position_id.is_not(None)) \
        .filter(Drives.end_position_id.is_not(None)) \
        .count()

    empty_count += session \
        .query(ChargingProcesses.id) \
        .filter(ChargingProcesses.address_id.is_(None)) \
        .filter(ChargingProcesses.position_id.is_not(None)) \
        .count()
    return empty_count


def fix_address_batch(session, resolve, config, tables):
    """Fix one batch of empty addresses. Returns (count, max_drive_id, max_charging_id)."""
    Drives, ChargingProcesses, Positions, Addresses = tables
    batch_size = config.batch
    processed_count = 0
    max_drive_id = 0
    max_charging_id = 0

    empty_count = get_empty_record_count(session, Drives, ChargingProcesses)

    # get empty records in drives.
    empty_drives = session \
        .query(Drives) \
        .filter(or_(Drives.start_address_id.is_(None),
                     Drives.end_address_id.is_(None))) \
        .filter(Drives.start_position_id.is_not(None)) \
        .filter(Drives.end_position_id.is_not(None)) \
        .limit(batch_size) \
        .all()

    # get empty records in charging_processes, filling remaining capacity.
    empty_chargings = []
    if len(empty_drives) < batch_size:
        empty_chargings = session \
            .query(ChargingProcesses) \
            .filter(ChargingProcesses.address_id.is_(None)) \
            .filter(ChargingProcesses.position_id.is_not(None)) \
            .limit(batch_size - len(empty_drives)) \
            .all()

    batch_total = len(empty_drives) + len(empty_chargings)

    # processing drives.
    for i, record in enumerate(empty_drives):
        logging.info("processing drive address %d/%d (total remaining: %d, id=%d)" %
                     (i + 1, batch_total, empty_count - processed_count, record.id))
        start_position = get_position(session,
                                      record.start_position_id, Positions)
        end_position = get_position(session,
                                    record.end_position_id, Positions)
        start_addr_id, start_addr = resolve(
            session, start_position, Addresses)
        end_addr_id, end_addr = resolve(
            session, end_position, Addresses)
        if start_addr_id is None or end_addr_id is None:
            continue
        record.start_address_id = start_addr_id
        record.end_address_id = end_addr_id
        logging.info("Changing drives(id = %d) start address to %s" %
                     (record.id, start_addr))
        logging.info("Changing drives(id = %d) end address to %s" %
                     (record.id, end_addr))
        max_drive_id = max(max_drive_id, record.id)
        processed_count += 1

    # processing charging.
    for i, record in enumerate(empty_chargings):
        batch_pos = len(empty_drives) + i + 1
        logging.info("processing charging address %d/%d (total remaining: %d, id=%d)" %
                     (batch_pos, batch_total, empty_count - processed_count, record.id))
        position = get_position(session, record.position_id, Positions)
        addr_id, addr = resolve(session, position, Addresses)
        if addr_id is None:
            continue
        record.address_id = addr_id
        logging.info("Changing charging(id = %d) to %s" %
                     (record.id, addr))
        max_charging_id = max(max_charging_id, record.id)
        processed_count += 1

    return processed_count, max_drive_id, max_charging_id


def fix_empty_records(engine, resolve, config, tables, checkpoint):
    """Fix all empty address records in batches."""
    while True:
        with Session(engine) as session:
            logging.info("checking empty records...")
            count, max_drive_id, max_charging_id = fix_address_batch(
                session, resolve, config, tables)
            if count == 0:
                break
            else:
                logging.info("saving...")
                session.commit()
                if max_drive_id > 0:
                    checkpoint['osm_fix']['last_drive_id'] = max_drive_id
                if max_charging_id > 0:
                    checkpoint['osm_fix']['last_charging_id'] = \
                        max_charging_id
                checkpoint['osm_fix']['fixed_count'] += count
                save_checkpoint(config.checkpoint_path, checkpoint)


# ---------- Mode 1: Update addresses via reverse geocoder ----------


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


def update_address_batch(session, geocoder, config, Addresses, checkpoint):
    """Update one batch of addresses via reverse geocoder.
    Returns (updated_count, found_count)."""
    processed_count = 0
    last_address_id = checkpoint['map_update']['last_address_id']

    total_remaining = get_update_record_count(
        session, Addresses, config, last_address_id)
    need_update_addresses = get_need_update_addresses(
        session, Addresses, config, last_address_id)

    for i, address_record in enumerate(need_update_addresses):
        logging.info("processing update address %d/%d (total remaining: %d, id=%d)" %
                     (i + 1, len(need_update_addresses), total_remaining - i, address_record.id))

        result = geocoder.reverse_geocode(
            address_record.latitude,
            address_record.longitude
        )
        # Always advance checkpoint, even on failure, to prevent permanently
        # skipping records when a later record in the same batch succeeds.
        checkpoint['map_update']['last_address_id'] = address_record.id
        if result is None:
            logging.warning("Geocoding failed for address id=%d, skipping." %
                            address_record.id)
            continue

        geocoder.update_address(address_record, result)
        processed_count += 1

    return processed_count, len(need_update_addresses)


def update_addresses(engine, geocoder, config, Addresses, checkpoint):
    """Update all addresses via reverse geocoder in batches."""
    while True:
        with Session(engine) as session:
            logging.info("updating addresses...")
            count, found = update_address_batch(
                session, geocoder, config, Addresses, checkpoint)
            if found == 0:
                break
            if count > 0:
                logging.info("saving...")
                session.commit()
                checkpoint['map_update']['updated_count'] += count
            # Always save checkpoint so skipped (failed) records are not retried
            # indefinitely within the same run.
            save_checkpoint(config.checkpoint_path, checkpoint)


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
        help="run mode: 0 -> fix empty record via OSM; "
             "1 -> update address by map api; 2 -> do both; "
             "3 -> fix empty record via map api, no OSM(MODE).")
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
        default='teslamate/#v1.29.2',
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
        "--short-names", required=False, type=int, default=0,
        action=EnvDefault, envvar="SHORT_NAMES",
        help="set to 1 to drop the leading district and the trailing "
             "parenthetical from resolved names(SHORT_NAMES).")
    parser.add_argument(
        "-c", "--checkpoint", required=False, type=str,
        default='checkpoint.json',
        action=EnvDefault, envvar="CHECKPOINT_FILE",
        help="checkpoint file path(CHECKPOINT_FILE).")
    parser.add_argument(
        "--reset-checkpoint", required=False,
        action='store_true', default=False,
        help="reset checkpoint and start fresh.")

    args = parser.parse_args()

    if not args.db_url and not all([args.user, args.password, args.host, args.port, args.dbname]):
        parser.error("Either --db-url (DB_URL) or all of -u/-p/-H/-P/-d must be provided.")

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
        mode=int(args.mode),
        tencent_key=args.key,
        tencent_sk=args.sk,
        since=since,
        user_agent=args.user_agent,
        checkpoint_path=args.checkpoint,
        reset_checkpoint=args.reset_checkpoint,
        osm_interval=float(args.osm_interval),
        geocoder_interval=float(args.geocoder_interval),
        short_names=bool(int(args.short_names)),
    )


# ---------- Main ----------

def run_once(engine, http_client, config, tables, checkpoint, geocoder):
    """Run one cycle of the configured mode."""
    Drives, ChargingProcesses, Positions, Addresses = tables

    if config.mode == 0 or config.mode == 2:
        def resolve_via_osm(session, position, Addresses):
            return resolve_osm_address(
                session, http_client, position, Addresses)

        fix_empty_records(engine, resolve_via_osm, config, tables, checkpoint)
    if config.mode == 1 or config.mode == 2:
        update_addresses(engine, geocoder, config, Addresses, checkpoint)
    if config.mode == 3:
        def resolve_via_geocoder(session, position, Addresses):
            return resolve_geocoder_address(
                session, geocoder, position, Addresses)

        fix_empty_records(
            engine, resolve_via_geocoder, config, tables, checkpoint)

    if config.mode < 0 or config.mode > 3:
        logging.info("nothing to do, bye.")


def main():
    config = parse_args()
    engine = create_engine_from_config(config)
    http_client = HttpClient(config)
    tables = reflect_tables(engine)
    geocoder = TencentGeocoder(http_client, config)

    if config.reset_checkpoint:
        logging.info("Resetting checkpoint.")
        checkpoint = _default_checkpoint()
    else:
        checkpoint = load_checkpoint(config.checkpoint_path)

    if config.interval == 0:
        run_once(engine, http_client, config, tables, checkpoint, geocoder)
    else:
        while True:
            run_once(engine, http_client, config, tables, checkpoint, geocoder)
            logging.info("sleeping for %d seconds..." % config.interval)
            time.sleep(config.interval)


if __name__ == '__main__':
    main()
