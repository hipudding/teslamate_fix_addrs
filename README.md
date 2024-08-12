# teslamate fix addrs

Fix empty addresses in teslamate.

**Thanks [@WayneJz](https://github.com/WayneJz) for the inspiration. See [teslamate-addr-fix](https://github.com/WayneJz/teslamate-addr-fix), address fixer written by go.**




## Notice

**Must create a [backup](https://docs.teslamate.org/docs/maintenance/backup_restore) before doing this.**




## Pre-requisite

- You have teslamate [broken address issue](https://github.com/adriankumpf/teslamate/issues/2956)

- You have access to openstreetmap.org **via your HTTP proxy**

  
## Guides
### How it works

Teslamate fix addrs has two main functions:

**Fix empty addresses**

Fix empty addressed by [open street map nominatim api](https://nominatim.openstreetmap.org/ui/reverse.html). All drives or charging processes will record a position with latitude and longitude infomations, use open street map api to resolve addresses.

teslamate_fix_addrs will search all drives or charging processes, get records which address id are not set, add these addresses and link these records and addresses.



**Update address details**

Update address by [amap api](https://lbs.amap.com/api/webservice/summary). Some of address is not correct resoved by open street map, use amap to get addresses is much better (and faster) in China, update address if amap has more details.

when the program run in first round, all addresses with comma (which means this address is added by open street map) in display_name column will be updated. In subsequent rounds, it will only check new added records by compare updated_at column.



### Run Mode

`-m` `--mode` or environment `MODE` is used to configure teslamate_fix_addrs' running mode.

* 0: fix empty address only.
* 1: use amap to update address only.
* 2: do both.

If you what to update addresses by amap, remember to [apply a key](https://lbs.amap.com/api/webservice/guide/create-project/get-key) first, and pass the key value by `-k` `--key` or environment `KEY`



### Infinity mode

`-i` `--interval` or environment `INTERVAL` is used to configure execution intervals. if `INTERVAL` equals 0, this program only run once, otherwise it will continuously run at interval seconds.



### Low memory support

User `-b` `--batch` or environment `BATCH` to limit the number of records for one loop which can save memory use. 

All added or modified records will be commited at the end of each loop.



### Parameter priority

All parameters can be passed to teslamate_fix_addrs by command line parameters or set environment values, the parameter priority is:

>command line parameter > environment values > default values



### Proxy

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
       image: huafengchun/teslamate_fix_addrs:latest
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
       - KEY=
       - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
   ```

   



### Usage examples

**Docker compose**

```
teslamate_fix_addrs:
    image: huafengchun/teslamate_fix_addrs:latest
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
    - KEY=
    - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
```



**Run python script**

```
usage: teslamate_fix_addrs.py [-h] -u USER -p PASSWORD -H HOST -P PORT -d DBNAME [-b BATCH] [-t TIMEOUT] [-r RETRY] [-i INTERVAL] [-ua USER_AGENT]

Usage of address fixer.

options:
  -h, --help                               show this help message and exit
  -u USER, --user USER                     db user name(DB_USER).
  -p PASSWORD, --password PASSWORD         db password(DB_PASSWD).
  -H HOST, --host HOST                     db host name or ip address(DB_HOST).
  -P PORT, --port PORT                     db port(DB_PORT).
  -d DBNAME, --dbname DBNAME               db name(DB_NAME).
  -b BATCH, --batch BATCH                  batch size for one loop(BATCH).
  -t TIMEOUT, --timeout TIMEOUT            http request timeout(s)(HTTP_TIMEOUT).
  -r RETRY, --retry RETRY                  http request max retries(HTTP_RETRY).
  -i INTERVAL, --interval INTERVAL         if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL).
  -m MODE, --mode MODE                     run mode: 0 -> fix empty record; 1 -> update address by amap; 2 -> do both(MODE).
  -k KEY, --key KEY                        API key for calling amap(KEY).
  -s SINCE, --since SINCE                  Update from specified date(YYYY-mm-dd).
  -ua USER_AGENT, --user_agent USER_AGENT  Custom User-Agent for HTTP requests(USER_AGENT).
```



### Run in sandbox

Worry about damaging existing data？ You can have a try in sandbox.

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
       image: huafengchun/teslamate_fix_addrs:latest
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
       - KEY=
       - USER_AGENT=Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36
   
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
