# teslamate fix addrs

Fix empty addresses in teslamate.

**Thanks [@WayneJz](https://github.com/WayneJz) for the inspiration. See [teslamate-addr-fix](https://github.com/WayneJz/teslamate-addr-fix), address fixer written by go.**




## Notice

**Must create a [backup](https://docs.teslamate.org/docs/maintenance/backup_restore) before doing this.**

Run once with `--dry-run` (or `DRY_RUN=true`) first: every change is logged but nothing is written to the database.




## Pre-requisite

- You have teslamate [broken address issue](https://github.com/adriankumpf/teslamate/issues/2956)

- You have access to openstreetmap.org **via your HTTP proxy**


## Guides
### How it works

Teslamate fix addrs has two main functions:

**Fix empty addresses**

Fix empty addressed by [open street map nominatim api](https://nominatim.openstreetmap.org/ui/reverse.html). All drives or charging processes will record a position with latitude and longitude infomations, use open street map api to resolve addresses.

teslamate_fix_addrs will search all drives or charging processes, get records which address id are not set, add these addresses and link these records and addresses.

* Records are scanned in id order. A record that OSM can not resolve (e.g. `Unable to geocode` for a position on the sea) is logged and skipped, it does not block the following records, and it is retried in the next round.
* For a drive, only the missing side (start or end) is resolved; a side that resolves is saved even if the other side fails.
* Addresses are de-duplicated by `(osm_id, osm_type)`, same as TeslaMate.
* If OSM can not be reached at all, the round stops (nothing is marked as failed) and the next round retries.



**Update address details**

Update address by [Tencent Maps API](https://lbs.qq.com/service/webService/webServiceGuide/webServiceGcoder) (reverse geocoding). Some of address is not correctly resolved by open street map, use Tencent Maps to get addresses is much better (and faster) in China.

*Position accuracy (no offset)*

* The coordinates sent to Tencent are GPS positions **recorded by the car** (charging, drive end and drive start positions linked to the address). `addresses.latitude/longitude` is where OSM put the matched object (its centroid), which can be hundreds of meters away from the car, so it is only used as a fallback.
* TeslaMate shares one address row between every drive/charge resolved to the same OSM object. The real position closest to the median of all linked positions is used (the most typical place, e.g. home rather than one occasional stop nearby). A warning is logged when the linked positions are more than 300 m apart.
* Coordinates are never written into `addresses`: TeslaMate re-geocodes addresses from them when its language setting changes, and expects the same OSM object back.
* Coordinates are sent as WGS84 (GPS) with `coord_type=1`, Tencent converts them to GCJ-02 internally. No local coordinate transformation is done, and **coordinates in the database are never modified**.

*What is overwritten*

* Only addresses in China are updated by default (`UPDATE_OVERSEAS=true` to change).
* Empty values returned by Tencent never overwrite existing values; empty string and NULL are treated as equal. `road` and `house_number` are always updated together, so they never mismatch.
* Before the first overwrite, the original values are saved in `addresses.raw` (key `teslamate_fix_addrs`). Run with `--restore-original` to roll every updated address back.

*Progress and failures*

When the program runs in the first round, all addresses will be updated. In subsequent rounds, it will only check new added addresses (id greater than the last processed one, stored in the checkpoint file).

* An address that Tencent can not resolve is retried in the next rounds, and given up after `GEOCODER_MAX_ATTEMPTS` (default 3) failures. Failed and abandoned ids are listed in the checkpoint file.
* Network errors, quota exceeded (`status` 120/121), invalid key or signature stop the round without skipping anything; the next round continues where it stopped.



### Run Mode

`-m` `--mode` or environment `MODE` is used to configure teslamate_fix_addrs' running mode.

* 0: fix empty address only.
* 1: use Tencent Maps to update address only.
* 2: do both.

If you want to update addresses by Tencent Maps, you need to [apply for a key](https://lbs.qq.com/dev/console/application/mine) first, and pass the key value by `-k` `--key` or environment `TENCENT_KEY`. If signature verification (SK) is enabled for the key, also configure the SK via `--sk` or environment `TENCENT_SK`.



### Infinity mode

`-i` `--interval` or environment `INTERVAL` is used to configure execution intervals. if `INTERVAL` equals 0, this program only run once (exit code 1 if a map service was unavailable), otherwise it will continuously run at interval seconds. In infinity mode, errors such as a database restart are logged and retried in the next round.

`SIGINT` / `SIGTERM` (`docker stop`) finish the current record, commit, and exit.



### Checkpoint

Progress is saved to `checkpoint.json` (`-c` `--checkpoint` or `CHECKPOINT_FILE`). In the docker image it is `/data/checkpoint.json`: **mount a volume on `/data`**, otherwise the progress is lost when the container is re-created and mode 1 calls the map API for every address again. `--reset-checkpoint` starts from scratch.



### Low memory support

User `-b` `--batch` or environment `BATCH` to limit the number of records for one loop which can save memory use.

All added or modified records will be commited at the end of each loop.



### Parameter priority

All parameters can be passed to teslamate_fix_addrs by command line parameters or set environment values, the parameter priority is:

>command line parameter > environment values > default values

Empty environment values are ignored (the default is used).



### Proxy

If open street map is baned in your region, you need a proxy to get access to it. Set proxy settings by environment values:

* HTTP_PROXY=http://proxy.ip:port
* HTTPS_PROXY=http://proxy.ip:port

If you use the socks protocol proxy, set environment variable:
* HTTP_PROXY=socks5://proxy.ip:port
* HTTPS_PROXY=socks5://proxy.ip:port

Use `NO_PROXY=apis.map.qq.com` if Tencent Maps should not go through the proxy.



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
       - INTERVAL=300
       - MODE=0
       - SINCE=2024-01-24
       - TENCENT_KEY=your_tencent_key
       - TENCENT_SK=your_tencent_sk
       - OSM_INTERVAL=1.0
       - GEOCODER_INTERVAL=0.3
       volumes:
       - teslamate-fix-addrs-data:/data

   volumes:
     teslamate-fix-addrs-data:
   ```





### Usage examples

**Docker compose**

See [docker-compose.yaml](docker-compose.yaml) and [.env.example](.env.example).

Images are built by GitHub Actions and published to GitHub Container Registry: `ghcr.io/<owner>/teslamate_fix_addrs:latest` (main branch) and `:<version>` (the `__version__` in the script, e.g. `:2.1.0`). To build your own image instead, replace `image:` with `build: ./teslamate_fix_addrs` (path of your clone). The first log line shows the version and the git commit of the running image.

**Changing TeslaMate's language**

Changing the language in TeslaMate settings re-geocodes every address with OSM and overwrites city, state, county, country, neighbourhood and display_name. Run mode 1 again with `--reset-checkpoint` afterwards.



**Run python script**

```
pip install -r requirements.txt
python3 teslamate_fix_addrs.py -u teslamate -p 123456 -H 127.0.0.1 -P 5432 -d teslamate -m 2 -k KEY --sk SK --dry-run
```

```
usage: teslamate_fix_addrs.py [-h] [--db-url DB_URL] [-u USER] [-p PASSWORD] [-H HOST] [-P PORT] [-d DBNAME] [-b BATCH] [-t TIMEOUT] [-r RETRY] [-i INTERVAL] [-m MODE] [-k KEY] [--sk SK] [-s SINCE] [-ua USER_AGENT] [--osm-interval OSM_INTERVAL] [--geocoder-interval GEOCODER_INTERVAL] [--geocoder-max-attempts GEOCODER_MAX_ATTEMPTS] [--update-overseas] [-c CHECKPOINT] [--reset-checkpoint] [--dry-run] [--restore-original]

Usage of address fixer.

options:
  -h, --help                                         show this help message and exit
  --db-url DB_URL                                    full database URL, overrides individual db params(DB_URL).
  -u USER, --user USER                               db user name(DB_USER).
  -p PASSWORD, --password PASSWORD                   db password(DB_PASSWD).
  -H HOST, --host HOST                               db host name or ip address(DB_HOST).
  -P PORT, --port PORT                               db port(DB_PORT).
  -d DBNAME, --dbname DBNAME                         db name(DB_NAME).
  -b BATCH, --batch BATCH                            batch size for one loop(BATCH).
  -t TIMEOUT, --timeout TIMEOUT                      http request timeout(s)(HTTP_TIMEOUT).
  -r RETRY, --retry RETRY                            http request max retries, also on 429/5xx(HTTP_RETRY).
  -i INTERVAL, --interval INTERVAL                   if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL).
  -m MODE, --mode MODE                               run mode: 0 -> fix empty record; 1 -> update address by map api; 2 -> do both(MODE).
  -k KEY, --key KEY                                  API key for calling tencent maps(TENCENT_KEY).
  --sk SK                                            SK for tencent maps signature(TENCENT_SK).
  -s SINCE, --since SINCE                            Update from specified date(YYYY-mm-dd)(SINCE).
  -ua USER_AGENT, --user_agent USER_AGENT            Custom User-Agent for HTTP requests(USER_AGENT).
  --osm-interval OSM_INTERVAL                        seconds to sleep between OSM requests, default 1.0(OSM_INTERVAL).
  --geocoder-interval GEOCODER_INTERVAL              seconds to sleep between geocoder API requests, default 0.3(GEOCODER_INTERVAL).
  --geocoder-max-attempts GEOCODER_MAX_ATTEMPTS      give up an address after this many failed geocoding attempts, default 3(GEOCODER_MAX_ATTEMPTS).
  --update-overseas                                  also overwrite addresses outside China with map api results(UPDATE_OVERSEAS).
  -c CHECKPOINT, --checkpoint CHECKPOINT             checkpoint file path(CHECKPOINT_FILE).
  --reset-checkpoint                                 reset checkpoint and start fresh.
  --dry-run                                          log what would change but write nothing to db(DRY_RUN).
  --restore-original                                 restore addresses overwritten by map api to saved values, then exit.
```



### Run in sandbox

Worry about damaging existing data? You can have a try in sandbox (or use `--dry-run`).

1. Prepare a different machine than the one where your teslamate is located, or different docker containers.

2. [Backup](https://docs.teslamate.org/docs/maintenance/backup_restore/) your data from teslamate database.

3. Launch a simple demo (sandbox) in the machine or container in step 1.

   ```
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
       - INTERVAL=300
       - MODE=0
       - SINCE=2024-01-24
       - TENCENT_KEY=your_tencent_key
       - TENCENT_SK=your_tencent_sk
       - OSM_INTERVAL=1.0
       - GEOCODER_INTERVAL=0.3
       volumes:
       - teslamate-fix-addrs-data:/data

   volumes:
     teslamate-grafana-data:
     teslamate-fix-addrs-data:
   ```

4. [Restore](https://docs.teslamate.org/docs/maintenance/backup_restore/) your backup file to this demo.
5. Wait a moment and enjoy. (default username and password for grafana is admin/admin)



## Development

```
pip install -r requirements-dev.txt
pytest -q tests                                   # offline, SQLite, mocked HTTP
TEST_DB_URL=postgresql+psycopg2://user:pass@127.0.0.1:5432/test pytest -q tests   # against PostgreSQL (drops tables!)
python3 test_with_sqlite.py                       # live: calls real OSM / Tencent (TENCENT_KEY)
```



## Disclaimer

Only use this program after properly created backups, I am **not** responsible for any data loss or software failure related to this.

This project is only for study purpose, and **no web proxy (or its download link) provided**. If the network proxy is used in violation of local laws and regulations, the user is responsible for the consequences.

When you download, copy, compile or execute the source code or binary program of this project, it means that you have accepted the disclaimer as mentioned.



## Contributing and Issue

 Welcome to contribute code or submit an issue.
