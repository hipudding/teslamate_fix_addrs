# teslamate fix addrs

Fix empty addresses in teslamate.

**Thanks [@WayneJz](https://github.com/WayneJz) for the inspiration. See [teslamate-addr-fix](https://github.com/WayneJz/teslamate-addr-fix), address fixer written by go.**




## Notice

**Must create a [backup](https://docs.teslamate.org/docs/maintenance/backup_restore) before doing this.**




## Pre-requisite

- You have teslamate [broken address issue](https://github.com/adriankumpf/teslamate/issues/2956)

- You have access to openstreetmap.org **via your HTTP proxy**, or you use mode 3, which resolves addresses through Tencent Maps and never contacts openstreetmap.org


## Guides
### How it works

Teslamate fix addrs has two main functions:

**Fix empty addresses**

Fix empty addressed by [open street map nominatim api](https://nominatim.openstreetmap.org/ui/reverse.html). All drives or charging processes will record a position with latitude and longitude infomations, use open street map api to resolve addresses.

teslamate_fix_addrs will search all drives or charging processes, get records which address id are not set, add these addresses and link these records and addresses.



**Update address details**

Update address by [Tencent Maps API](https://lbs.qq.com/service/webService/webServiceGuide/webServiceGcoder) (reverse geocoding). Some of address is not correctly resolved by open street map, use Tencent Maps to get addresses is much better (and faster) in China, update address if Tencent Maps has more details.

Tencent Maps supports `coord_type=1` to directly accept WGS84 (GPS) coordinates, so no coordinate transformation step is needed.

When the program runs in the first round, all addresses will be updated. In subsequent rounds, it will only check new added records by comparing the `updated_at` column.



### Run Mode

`-m` `--mode` or environment `MODE` is used to configure teslamate_fix_addrs' running mode.

* 0: fix empty address only, resolved by open street map.
* 1: use Tencent Maps to update address only.
* 2: do both.
* 3: fix empty address using Tencent Maps only, open street map is never
  contacted. Use this if open street map is unreachable from your network.

If you want to update addresses by Tencent Maps, you need to [apply for a key](https://lbs.qq.com/dev/console/application/mine) first, and pass the key value by `-k` `--key` or environment `TENCENT_KEY`. You also need to configure the SK (Secret Key) via `--sk` or environment `TENCENT_SK` for API signature verification.



### Short names

Tencent Maps returns names like `龙岗区中国邮政葵涌邮政支局(坪葵路西)`, which repeat the district and add a landmark hint in brackets. Set `--short-names` or environment `SHORT_NAMES=1` to store `中国邮政葵涌邮政支局` instead. Nothing is lost, the district is already kept in its own column. Names that would end up empty are left untouched.

Off by default, so existing setups keep the names they have.

### Infinity mode

`-i` `--interval` or environment `INTERVAL` is used to configure execution intervals. if `INTERVAL` equals 0, this program only run once, otherwise it will continuously run at interval seconds.



### Low memory support

User `-b` `--batch` or environment `BATCH` to limit the number of records for one loop which can save memory use.

All added or modified records will be commited at the end of each loop.



### Parameter priority

All parameters can be passed to teslamate_fix_addrs by command line parameters or set environment values, the parameter priority is:

>command line parameter > environment values > default values



### Proxy

Mode 3 needs no proxy at all, because it resolves addresses through Tencent Maps instead of open street map. The proxy settings below only apply to modes 0 and 2.

If open street map is baned in your region, you need a proxy to get access to it. Set proxy settings by environment values:

* HTTP_PROXY=http://proxy.ip:port
* HTTPS_PROXY=http://proxy.ip:port

If you use the socks protocol proxy, set environment variable:
* HTTP_PROXY=socks5://proxy.ip:port
* HTTPS_PROXY=socks5://proxy.ip:port



### Get access to DB

If you installed teslamate by docker, you can choose alternative solutions.

1. Expose DB port by add `ports` in your docker-compose.yaml

   ```
   services:
     database:
       image: postgres:15
       restart: always
       environment:
         - POSTGRES_USER=teslamate
         - POSTGRES_PASSWORD=123456
         - POSTGRES_DB=teslamate
       ports:
         - 5432:5432
   ```



2. Add teslamate_fix_addrs in your docker-compose.yaml, so they are in the same network.

   ```
   services:
     database:
       image: postgres:15
       restart: always
       environment:
         - POSTGRES_USER=teslamate
         - POSTGRES_PASSWORD=123456
         - POSTGRES_DB=teslamate

     teslamate_fix_addrs:
       image: hipudding/teslamate_fix_addrs:latest
       container_name: teslmate_fix_addrs
       restart: unless-stopped
       environment:
       - DB_USER=teslamate
       - DB_PASSWD=123456
       - DB_HOST=database
       - DB_PORT=5432
       - DB_NAME=teslamate
       - BATCH=10
       - HTTP_TIMEOUT=5
       - HTTP_RETRY=5
       - INTERVAL=10
       - MODE=0
       - SINCE=2024-01-24
       - TENCENT_KEY=your_tencent_key
       - TENCENT_SK=your_tencent_sk
       - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
       - OSM_INTERVAL=1.0
       - GEOCODER_INTERVAL=0.3
   ```





### Usage examples

**Docker compose**

```
teslamate_fix_addrs:
    image: hipudding/teslamate_fix_addrs:latest
    container_name: teslmate_fix_addrs
    restart: unless-stopped
    environment:
    - DB_USER=teslamate
    - DB_PASSWD=123456
    - DB_HOST=database
    - DB_PORT=5432
    - DB_NAME=teslamate
    - BATCH=10
    - HTTP_TIMEOUT=5
    - HTTP_RETRY=5
    - INTERVAL=5
    - MODE=0
    - SINCE=2024-01-24
    - TENCENT_KEY=your_tencent_key
    - TENCENT_SK=your_tencent_sk
    - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
    - OSM_INTERVAL=1.0
    - GEOCODER_INTERVAL=0.3
```



**Run python script**

```
usage: teslamate_fix_addrs.py [-h] -u USER -p PASSWORD -H HOST -P PORT -d DBNAME [-b BATCH] [-t TIMEOUT] [-r RETRY] [-i INTERVAL] [-m MODE] [-k KEY] [--sk SK] [-s SINCE] [-ua USER_AGENT] [--osm-interval OSM_INTERVAL] [--geocoder-interval GEOCODER_INTERVAL]

Usage of address fixer.

options:
  -h, --help                                         show this help message and exit
  -u USER, --user USER                               db user name(DB_USER).
  -p PASSWORD, --password PASSWORD                   db password(DB_PASSWD).
  -H HOST, --host HOST                               db host name or ip address(DB_HOST).
  -P PORT, --port PORT                               db port(DB_PORT).
  -d DBNAME, --dbname DBNAME                         db name(DB_NAME).
  -b BATCH, --batch BATCH                            batch size for one loop(BATCH).
  -t TIMEOUT, --timeout TIMEOUT                      http request timeout(s)(HTTP_TIMEOUT).
  -r RETRY, --retry RETRY                            http request max retries(HTTP_RETRY).
  -i INTERVAL, --interval INTERVAL                   if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL).
  -m MODE, --mode MODE                               run mode: 0 -> fix empty record; 1 -> update address by map api; 2 -> do both(MODE).
  -k KEY, --key KEY                                  API key for calling tencent maps(TENCENT_KEY).
  --sk SK                                            SK for tencent maps signature(TENCENT_SK).
  -s SINCE, --since SINCE                            Update from specified date(YYYY-mm-dd).
  -ua USER_AGENT, --user_agent USER_AGENT            Custom User-Agent for HTTP requests(USER_AGENT).
  --osm-interval OSM_INTERVAL                        seconds to sleep between OSM requests, default 1.0(OSM_INTERVAL).
  --geocoder-interval GEOCODER_INTERVAL              seconds to sleep between geocoder API requests, default 0.3(GEOCODER_INTERVAL).
```



### Run in sandbox

Worry about damaging existing data? You can have a try in sandbox.

1. Prepare a different machine than the one where your teslamate is located, or different docker containers.

2. [Backup](https://docs.teslamate.org/docs/maintenance/backup_restore/) your data from teslamate database.

3. Launch a simple demo (sandbox) in the machine or container in step 1.

   ```
   version: "3"

   services:
     database:
       image: postgres:15
       restart: always
       environment:
         - POSTGRES_USER=teslamate
         - POSTGRES_PASSWORD=123456
         - POSTGRES_DB=teslamate

     grafana:
       image: teslamate/grafana:latest
       restart: always
       environment:
         - DATABASE_USER=teslamate
         - DATABASE_PASS=123456
         - DATABASE_NAME=teslamate
         - DATABASE_HOST=database
       ports:
         - 3000:3000
       volumes:
         - teslamate-grafana-data:/var/lib/grafana

     teslamate_fix_addrs:
       image: hipudding/teslamate_fix_addrs:latest
       container_name: teslmate_fix_addrs
       restart: unless-stopped
       environment:
       - DB_USER=teslamate
       - DB_PASSWD=123456
       - DB_HOST=database
       - DB_PORT=5432
       - DB_NAME=teslamate
       - BATCH=10
       - HTTP_TIMEOUT=5
       - HTTP_RETRY=5
       - INTERVAL=10
       - MODE=0
       - SINCE=2024-01-24
       - TENCENT_KEY=your_tencent_key
       - TENCENT_SK=your_tencent_sk
       - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
       - OSM_INTERVAL=1.0
       - GEOCODER_INTERVAL=0.3

   volumes:
     teslamate-grafana-data:
   ```

4. [Restore](https://docs.teslamate.org/docs/maintenance/backup_restore/) your backup file to this demo.
5. Wait a moment and enjoy. (default username and password for grafana is admin/admin)



## Disclaimer

Only use this program after properly created backups, I am **not** responsible for any data loss or software failure related to this.

This project is only for study purpose, and **no web proxy (or its download link) provided**. If the network proxy is used in violation of local laws and regulations, the user is responsible for the consequences.

When you download, copy, compile or execute the source code or binary program of this project, it means that you have accepted the disclaimer as mentioned.



## Contributing and Issue

 Welcome to contribute code or submit an issue.
