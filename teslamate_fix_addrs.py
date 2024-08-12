from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, or_, func
import requests
from requests.adapters import HTTPAdapter
import json
from datetime import datetime
import logging
import argparse
import os
import signal
from threading import Timer

logging.basicConfig(level=logging.INFO)


def handler(signum, frame):
    '''Contrl-C handler.'''
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


parser = argparse.ArgumentParser(description='Usage of address fixer.')
parser.add_argument("-u",
                    "--user",
                    required=True,
                    type=str,
                    action=EnvDefault,
                    envvar="DB_USER",
                    help="db user name(DB_USER).")
parser.add_argument("-p",
                    "--password",
                    required=True,
                    type=str,
                    action=EnvDefault,
                    envvar="DB_PASSWD",
                    help="db password(DB_PASSWD).")
parser.add_argument("-H",
                    "--host",
                    required=True,
                    type=str,
                    action=EnvDefault,
                    envvar="DB_HOST",
                    help="db host name or ip address(DB_HOST).")
parser.add_argument("-P",
                    "--port",
                    required=True,
                    type=str,
                    action=EnvDefault,
                    envvar="DB_PORT",
                    help="db port(DB_PORT).")
parser.add_argument("-d",
                    "--dbname",
                    required=True,
                    type=str,
                    action=EnvDefault,
                    envvar="DB_NAME",
                    help="db name(DB_NAME).")
parser.add_argument("-b",
                    "--batch",
                    required=False,
                    type=int,
                    default=10,
                    action=EnvDefault,
                    envvar="BATCH",
                    help="batch size for one loop(BATCH).")
parser.add_argument("-t",
                    "--timeout",
                    required=False,
                    type=int,
                    default=5,
                    action=EnvDefault,
                    envvar="HTTP_TIMEOUT",
                    help="http request timeout(s)(HTTP_TIMEOUT).")
parser.add_argument("-r",
                    "--retry",
                    required=False,
                    type=int,
                    default=5,
                    action=EnvDefault,
                    envvar="HTTP_RETRY",
                    help="http request max retries(HTTP_RETRY).")
parser.add_argument(
    "-i",
    "--interval",
    required=False,
    type=int,
    default=0,
    action=EnvDefault,
    envvar="INTERVAL",
    help=
    "if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL)."
)
parser.add_argument(
    "-m",
    "--mode",
    required=False,
    type=int,
    default=0,
    action=EnvDefault,
    envvar="MODE",
    help=
    "run mode: 0 -> fix empty record; 1 -> update address by amap; 2 -> do both(MODE)."
)
parser.add_argument("-k",
                    "--key",
                    required=False,
                    type=str,
                    default='',
                    action=EnvDefault,
                    envvar="KEY",
                    help="API key for calling amap(KEY).")

parser.add_argument("-s",
                    "--since",
                    required=False,
                    type=lambda d: datetime.strptime(d, '%Y-%m-%d'),
                    default=datetime.min,
                    action=EnvDefault,
                    envvar="SINCE",
                    help="Update from specified date(YYYY-mm-dd).")
parser.add_argument(
    "-ua",
    "--user_agent",
    required=False,
    type=str,
    default='teslamate/#v1.29.2',
    action=EnvDefault,
    envvar="USER_AGENT",
    help="Custom User-Agent for HTTP requests(USER_AGENT)."
)
args = parser.parse_args()


def custom_json_dumps(d):
    '''do not add backslash in json.'''
    return d


conn_str = "postgresql://%s:%s@%s:%s/%s" % (args.user, args.password,
                                            args.host, args.port, args.dbname)
engine = create_engine(conn_str, json_serializer=custom_json_dumps, echo=False)

# open street map api.
osm_resolve_url = "https://nominatim.openstreetmap.org/reverse?lat=%.6f&lon=%.6f&format=jsonv2&addressdetails=1&extratags=1&namedetails=1&zoom=18"

# amap api.
amap_coordinate_transformation_url = "https://restapi.amap.com/v3/assistant/coordinate/convert?key=%s&coordsys=gps&output=json&locations=%s,%s"
amap_resolve_url = "https://restapi.amap.com/v3/geocode/regeo?key=%s&output=json&location=%s,%s&poitype=all&extensions=all"

# last updated record id.
last_update_id = 0

# reflact Objects from db tables.
Base = automap_base()
Base.prepare(autoload_with=engine)
Drives = Base.classes.drives
ChargingProcesses = Base.classes.charging_processes
Positions = Base.classes.positions
Addresses = Base.classes.addresses

# reference to teslamate's source code, get address value from multiple keys.
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


def get_position(session, position_id):
    '''get position id from table positions by position_ids.'''
    position = session.query(Positions).filter(
        Positions.id == position_id).first()
    # position_id is foreign key to table positions. position will never be None.
    if position == None:
        # fatal error, exit now.
        logging.fatal("Position with ID %s is not found." % position_id)
        assert (False)
    return position


def http_request(url):
    '''get response by calling map api.'''
    http_session = requests.Session()
    http_session.mount('http://', HTTPAdapter(max_retries=args.retry))
    http_session.mount('https://', HTTPAdapter(max_retries=args.retry))
    headers = {
        'User-Agent': args.user_agent
    }

    try:
        response = http_session.get(url=url, timeout=args.timeout, headers=headers)
        if response.status_code != requests.codes.ok:
            logging.error(
                "Http request failed by url: %s, code: %d, body: %s" %
                (url, response.status_code, response.text))
            return None
        raw = response.text
        return raw
    except:
        logging.error("Http request exception by url: %s" % (url))
        return None


def get_address_in_db(session, osm_id):
    '''select address from db, get address id which just added.'''
    return session.query(Addresses).filter(Addresses.osm_id == osm_id).first()


def add_osm_address(session, osm_address, raw):
    '''add osm address to db.'''
    exist_address = get_address_in_db(session, osm_address['osm_id'])
    if exist_address is None:
        logging.info("oms id = %d is not exist!" % osm_address['osm_id'])
    if exist_address is None:
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


def get_address(session, position):
    '''
    return address id and display_name by position id. 
    Address will add into db if not exists.
    '''
    url = osm_resolve_url % (position.latitude, position.longitude)
    raw = http_request(url)
    if raw is None:
        return None, None

    osm_address = json.loads(raw)
    if osm_address == None:
        return None, None

    add_osm_address(session, osm_address, raw)
    added_address = get_address_in_db(session, osm_address['osm_id'])
    return added_address.id, added_address.display_name


def fix_address(session, batch_size, empty_count):
    processed_count = 0
    # get empty records in drives.
    empty_drive_addresses = session\
        .query(Drives)\
        .filter(or_(Drives.start_address_id.is_(None), Drives.end_address_id.is_(None)))\
        .filter(Drives.start_position_id.is_not(None))\
        .filter(Drives.end_position_id.is_not(None))\
        .limit(batch_size)\
        .all()

    # get empty records in charging_processes, all records are LE batch_size.
    empty_charging_addresses = []
    if len(empty_drive_addresses) < batch_size:
        empty_charging_addresses = session\
            .query(ChargingProcesses)\
            .filter(ChargingProcesses.address_id.is_(None))\
            .filter(ChargingProcesses.position_id.is_not(None))\
            .limit(batch_size - len(empty_drive_addresses))\
            .all()

    # processing drives.
    for empty_drive_address in empty_drive_addresses:
        logging.info("processing drive address (%d left)" % (empty_count - processed_count))

        # get positions.
        start_position_id = empty_drive_address.start_position_id
        end_position_id = empty_drive_address.end_position_id
        start_position = get_position(session, start_position_id)
        end_position = get_position(session, end_position_id)

        # get addresses.
        start_address_id, start_address = get_address(session, start_position)
        end_address_id, end_address = get_address(session, end_position)
        if start_address_id is None or end_address_id is None:
            continue

        # update address ids.
        empty_drive_address.start_address_id = start_address_id
        empty_drive_address.end_address_id = end_address_id
        logging.info("Changing drives(id = %d) start address to %s" %
                     (empty_drive_address.id, start_address))
        logging.info("Changing drives(id = %d) end address to %s" %
                     (empty_drive_address.id, end_address))
        processed_count += 1

    # processing charging.
    for empty_charging_address in empty_charging_addresses:
        logging.info("processing charging address (%d left)" % (empty_count - processed_count))

        # get position.
        position_id = empty_charging_address.position_id
        position = get_position(session, position_id)

        # get address.
        address_id, address = get_address(session, position)
        if address_id is None:
            continue

        # update address id.
        empty_charging_address.address_id = address_id
        logging.info("Changing charging(id = %d) to %s" %
                     (empty_charging_address.id, address))
        processed_count += 1

    # records processed.
    return processed_count


def get_empty_record_count(session):
    '''get all empty records count.'''
    empty_count = session\
        .query(Drives.id)\
        .filter(or_(Drives.start_address_id.is_(None), Drives.end_address_id.is_(None)))\
        .filter(Drives.start_position_id.is_not(None))\
        .filter(Drives.end_position_id.is_not(None))\
        .count()

    empty_count += session\
        .query(ChargingProcesses.id)\
        .filter(ChargingProcesses.address_id.is_(None))\
        .filter(ChargingProcesses.position_id.is_not(None))\
        .count()
    return empty_count


def fix_empty_records():
    # for low memory devices.
    while True:
        with Session(engine) as session:
            logging.info("checking empty records...")
            empty_count = get_empty_record_count(session)
            if fix_address(session, args.batch, empty_count) == 0:
                # all recoreds are fixed.
                break
            else:
                # commit at end of each batch.
                logging.info("saving...")
                session.commit()


def get_field(find, keys):
    '''get field from a dict object'''
    item = find
    for key in keys:
        if isinstance(key, str):
            if key in item:
                item = item[key]
            else:
                return ''
        elif isinstance(key, int):
            if len(item) > 0:
                item = item[key]
            else:
                return ''
    # we should have find address str here.
    if not isinstance(item, str):
        # some address will be empty list.
        if len(item) == 0:
            return ''
        logging.fatal("key error when parse amap response.")
        assert (False)
    return item


def update_address_in_db(need_update_address, address_details):
    # parse response.
    country = get_field(address_details,
                        ['regeocode', 'addressComponent', 'country'])
    province = get_field(address_details,
                         ['regeocode', 'addressComponent', 'province'])

    municipality = province in ['北京市', '天津市', '上海市', '重庆市']
    if municipality:
        city = province + get_field(
            address_details, ['regeocode', 'addressComponent', 'district'])
    else:
        city = get_field(address_details,
                         ['regeocode', 'addressComponent', 'city'])

    township = get_field(address_details,
                         ['regeocode', 'addressComponent', 'township'])
    display_name = get_field(address_details,
                             ['regeocode', 'formatted_address'])
    neighborhood = get_field(
        address_details,
        ['regeocode', 'addressComponent', 'neighborhood', 'name'])
    street_number = get_field(
        address_details,
        ['regeocode', 'addressComponent', 'streetNumber', 'number'])
    road = get_field(address_details, ['regeocode', 'roads', 0, 'name'])
    name = get_field(address_details, ['regeocode', 'aois', 0, 'name'])
    if len(name) == 0:
        name = get_field(address_details, ['regeocode', 'pois', 0, 'name'])
    if len(name) == 0:
        name = get_field(address_details, ['regeocode', 'roads', 0, 'name'])

    # update db record.
    logging.info("update address from %s to %s" %
                 (need_update_address.display_name, display_name))
    need_update_address.state = province
    need_update_address.county = township
    need_update_address.city = city
    need_update_address.house_number = street_number
    need_update_address.display_name = display_name
    need_update_address.country = country
    need_update_address.updated_at = datetime.now().replace(microsecond=0)

    # record last processed record id, skip records which id less than this.
    # assume that address record will not updated.
    # if language changed, teslamate will update all addressed, remember to
    # restart me to re-process all records.
    global last_update_id
    last_update_id = need_update_address.id

    # if some address is empty, do not update them.
    if len(road) > 0:
        need_update_address.road = road

    if len(name) > 0:
        need_update_address.name = name

    if len(neighborhood) > 0:
        need_update_address.neighbourhood = neighborhood


def request_amap_api(url):
    '''request from amap api and loads as dict'''
    response = http_request(url)
    if response is None:
        return None

    response_dict = json.loads(response)
    if response_dict is None or response_dict['status'] != '1':
        logging.error("request amap api error: %s" % response)
        return None
    return response_dict


def get_update_record_count(session):
    # record last updated id to save cpu time.
    return session\
        .query(Addresses)\
        .filter(Addresses.updated_at >= args.since)\
        .filter(Addresses.id > last_update_id)\
        .count()


def get_need_update_addresses(session, batch_size):
    # record last update id to save cpu time.
    return session\
        .query(Addresses)\
        .filter(Addresses.updated_at >= args.since)\
        .filter(Addresses.id > last_update_id)\
        .order_by(Addresses.id)\
        .limit(batch_size)\
        .all()


def update_address(session, batch_size, need_update_count):
    '''update address str by amap api.'''
    processed_count = 0
    if len(args.key) == 0:
        logging.error("Amap key is not set.")
        return 0

    need_update_addresses = get_need_update_addresses(session, batch_size)

    for need_update_address in need_update_addresses:
        logging.info("processing update address (%d left)" %
                     (need_update_count - processed_count))
        gps_lat = need_update_address.latitude
        gps_lon = need_update_address.longitude

        # transform coordinate
        url = amap_coordinate_transformation_url % (args.key, gps_lon, gps_lat)
        transformed_coordinate = request_amap_api(url)
        if transformed_coordinate is None:
            continue

        locations = transformed_coordinate['locations']
        amap_lon = round(float(locations.split(',')[0]), 6)
        amap_lat = round(float(locations.split(',')[1]), 6)

        # get address details
        url = amap_resolve_url % (args.key, amap_lon, amap_lat)
        address_details = request_amap_api(url)
        if address_details is None:
            continue

        # update db
        update_address_in_db(need_update_address, address_details)

        processed_count += 1

    return processed_count


def update_address_by_amap():
    while True:
        with Session(engine) as session:
            logging.info("updating address by amap...")
            need_update_count = get_update_record_count(session)
            if update_address(session, args.batch, need_update_count) == 0:
                # all recoreds are updated.
                break
            else:
                # commit at end of each batch.
                logging.info("saving...")
                session.commit()


def main():
    if args.mode == 0 or args.mode == 2:
        fix_empty_records()
    if args.mode == 1 or args.mode == 2:
        update_address_by_amap()

    # wrong mode, do nothing and exit.
    if args.mode < 0 or args.mode > 2:
        logging.info("nothing to do, bye.")
    # if interval is set, run in infinity mode.
    elif args.interval != 0:
        loop_timer = Timer(args.interval, main)
        loop_timer.start()


if __name__ == '__main__':
    main()
    
