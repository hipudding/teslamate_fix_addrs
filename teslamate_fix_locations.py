from sqlalchemy.ext.automap import automap_base
from sqlalchemy.orm import Session
from sqlalchemy import create_engine, or_, and_
import requests
from requests.adapters import HTTPAdapter
import json
from datetime import datetime
import logging
import argparse
import os
import time

logging.basicConfig(level=logging.INFO)

class EnvDefault(argparse.Action):
    def __init__(self, envvar, required=True, default=None, **kwargs):
        if envvar in os.environ:
            default = os.environ[envvar]
        if required and default:
            required = False
        super(EnvDefault, self).__init__(default=default, required=required, 
                                         **kwargs)

    def __call__(self, parser, namespace, values, option_string=None):
        setattr(namespace, self.dest, values)


parser = argparse.ArgumentParser(description='Usage of address fixer.')
parser.add_argument("-u", "--user", required=True, type=str,
                    action=EnvDefault, envvar="DB_USER", help="db user name(DB_USER).")
parser.add_argument("-p", "--password", required=True, type=str,
                    action=EnvDefault, envvar="DB_PASSWD", help="db password(DB_PASSWD).")
parser.add_argument("-H", "--host", required=True, type=str, action=EnvDefault,
                    envvar="DB_HOST", help="db host name or ip address(DB_HOST).")
parser.add_argument("-P", "--port", required=True, type=str,
                    action=EnvDefault, envvar="DB_PORT", help="db port(DB_PORT).")
parser.add_argument("-d", "--dbname", required=True, type=str,
                    action=EnvDefault, envvar="DB_NAME", help="db name(DB_NAME).")
parser.add_argument("-b", "--batch", required=False, type=int, default=10,
                    action=EnvDefault, envvar="BATCH", help="batch size for one loop(BATCH).")
parser.add_argument("-t", "--timeout", required=False, type=int, default=5, action=EnvDefault,
                    envvar="HTTP_TIMEOUT", help="http request timeout(s)(HTTP_TIMEOUT).")
parser.add_argument("-r", "--retry", required=False, type=int, default=5,
                    action=EnvDefault, envvar="HTTP_RETRY", help="http request max retries(HTTP_RETRY).")
parser.add_argument("-i", "--interval", required=False, type=int, default=0, action=EnvDefault, envvar="INTERVAL",
                    help="if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL).")

args = parser.parse_args()

conn_str = "postgresql://%s:%s@%s:%s/%s" % (args.user, args.password, args.host, args.port, args.dbname)

# do not add backslash in json.
def custom_json_dumps(d):
    return d

engine = create_engine(conn_str, json_serializer=custom_json_dumps, echo=False)

# open street map api.
osm_resolve_url = "https://nominatim.openstreetmap.org/reverse?lat=%.6f&lon=%.6f&format=jsonv2&addressdetails=1&extratags=1&namedetails=1&zoom=19"

Base = automap_base()
Base.prepare(autoload_with=engine)
Drives = Base.classes.drives
ChargingProcesses = Base.classes.charging_processes
Positions = Base.classes.positions
Addresses = Base.classes.addresses

# reference to teslamate's source code, get address value from multiple keys. 
house_number_aliases = [
    'house_number',
    'street_number'
]

road_aliases = [
    "road",
    "footway",
    "street",
    "street_name",
    "residential",
    "path",
    "pedestrian",
    "road_reference",
    "road_reference_intl",
    "square",
    "place"
]

neighbourhood_aliases = [
    "neighbourhood",
    "suburb",
    "city_district",
    "district",
    "quarter",
    "borough",
    "city_block",
    "residential",
    "commercial",
    "houses",
    "subdistrict",
    "subdivision",
    "ward"
]

municipality_aliases = [
    "municipality",
    "local_administrative_area",
    "subcounty"
]

village_aliases = [
    "village",
    "municipality",
    "hamlet",
    "locality",
    "croft"
]

city_aliases = [
    "city",
    "town",
    "township"
]

city_aliases.extend(village_aliases)
city_aliases.extend(municipality_aliases)

county_aliases = [
    "county",
    "county_code",
    "department"
]

state_aliases = [
    'state',
    'province',
    'state_code'
]

country_aliases = [
    'country',
    'country_name'
]

# get address value from multiple keys.
def get_address_str(address, addr_keys):
    for addr_key in addr_keys:
        if addr_key in address:
            return address[addr_key]
    return None

# special process for address names.
# 1. address.name
# 2. address.namedetails.name
# 3. address.namedetails.alt_name
# 4. first element in address.display_name
def get_address_name(address):
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


# get position id from table positions by position_ids
def get_position(session, position_id):
    position = session.query(Positions).filter(
        Positions.id == position_id).first()
    # position_id is foreign key to table positions. position will never be None.
    if position == None:
        # fatal error, exit now.
        logging.error("Position with ID %s is not found." % position_id)
        assert(False)
    return position


# get address by position, calling open street map api.
def get_address_info(position):
    retries = args.retry
    timeout = args.timeout
    http_session = requests.Session()
    http_session.mount('http://', HTTPAdapter(max_retries=retries))
    http_session.mount('https://', HTTPAdapter(max_retries=retries))
    url = osm_resolve_url % (position.latitude, position.longitude)
    try:
        response = http_session.get(url=url, timeout=timeout).text
    except:
        logging.error("Can't get address, position: latitude: %.6f, longitude: %.6f" % (
            position.latitude, position.longitude))
        logging.error("Url = %s" % url)
        return None, None
    osm_address = json.loads(response)
    return osm_address, response


# select address from db, get address id which just added.
def get_address_in_db(session, osm_id):
    return session.query(Addresses).filter(
        Addresses.osm_id == osm_id).first()


# add address to db.
def add_address(session, osm_address, raw):
    exist_address = get_address_in_db(session, osm_address['osm_id'])
    if exist_address is None:
        session.add(Addresses(
            display_name=osm_address['display_name'],
            latitude=osm_address['lat'],
            longitude=osm_address['lon'],
            name=get_address_name(osm_address),
            house_number=get_address_str(osm_address['address'], house_number_aliases),
            road=get_address_str(osm_address['address'], road_aliases),
            neighbourhood=get_address_str(osm_address['address'], neighbourhood_aliases),
            city=get_address_str(osm_address['address'], city_aliases),
            county=get_address_str(osm_address['address'], county_aliases),
            postcode=get_address_str(osm_address['address'], ['postcode']),
            state=get_address_str(osm_address['address'], state_aliases),
            state_district=get_address_str(osm_address['address'], ['state_district']),
            country=get_address_str(osm_address['address'], country_aliases),
            raw=raw,
            inserted_at=datetime.now(),
            updated_at=datetime.now(),
            osm_id=osm_address['osm_id'],
            osm_type=osm_address['osm_type']))
        logging.info("address added: %s." % osm_address['display_name'])
    else:
        logging.info("address is already exist: %d, %s." %
                     (osm_address['osm_id'], osm_address['display_name']))

# return address id and display_name by position id. Address will add into db if not exists.
def get_address(session, position):
    osm_address, raw = get_address_info(position)
    if osm_address == None:
        return None, None

    add_address(session, osm_address, raw)
    added_address = get_address_in_db(session, osm_address['osm_id'])
    return added_address.id, added_address.display_name

def fix_address(session, batch_size, empty_count):
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
            logging.info("=================== processing drive address (%d left) ===================" %
                         (empty_count))
            
            # get positions.
            start_position_id = empty_drive_address.start_position_id
            end_position_id = empty_drive_address.end_position_id
            start_position = get_position(session, start_position_id)
            end_position = get_position(session, end_position_id)
            
            # get addresses.
            start_address_id, start_address = get_address(
                session, start_position)
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
            empty_count -= 1

        # processing charging.
        for empty_charging_address in empty_charging_addresses:
            logging.info("=================== processing charging address (%d left) ===================" %
                         (empty_count))
            
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
            empty_count -= 1
        
        # records processed.
        return (len(empty_drive_addresses) + len(empty_charging_addresses))

# get all empty records count.
def get_empty_record_count(session):
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


# main loop
def process():
    # for low memory devices.
    batch_size = args.batch
    interval = args.interval
    while True:
        with Session(engine) as session:
            logging.info("checking...")
            empty_count = get_empty_record_count(session)
            if fix_address(session, batch_size, empty_count) == 0:
                if interval != 0:
                    time.sleep(interval)
                else:
                    break
            else:
                # commit at end of each batch.
                logging.info("saving...")
                session.commit()

if __name__ == '__main__':
    process()
