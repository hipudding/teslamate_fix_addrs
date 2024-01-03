# teslamate_fix_addrs

Fix blank address in teslamate.

**Thanks [@WayneJz](https://github.com/WayneJz) for the inspiration. See [teslamate-addr-fix](https://github.com/WayneJz/teslamate-addr-fix), address fixer written by go.**



## Notice

**Must create a [backup](https://docs.teslamate.org/docs/maintenance/backup_restore) before doing this.**



## Pre-requisite

- You have teslamate [broken address issue](https://github.com/adriankumpf/teslamate/issues/2956)

- You have access to openstreetmap.org **via your HTTP proxy**

  

## Guides

### 1. Docker compose

* Proxy is configured via environment variables, such as HTTP_PROXY, HTTPS_PROXY, make sure these environment variables are reachable in docker container.
* It is recommended to configure it in the same docker compose as the database. It can also be deployed independently, make sure database is accessable by this container.

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
```



### 2. Run on host

* Make sure database is accessable.

```
usage: teslamate_fix_addrs.py [-h] -u USER -p PASSWORD -H HOST -P PORT -d DBNAME [-b BATCH] [-t TIMEOUT] [-r RETRY] [-i INTERVAL]

Usage of address fixer.

options:
  -h, --help                        show this help message and exit
  -u USER, --user USER              db user name(DB_USER).
  -p PASSWORD, --password PASSWORD  db password(DB_PASSWD).
  -H HOST, --host HOST              db host name or ip address(DB_HOST).
  -P PORT, --port PORT              db port(DB_PORT).
  -d DBNAME, --dbname DBNAME        db name(DB_NAME).
  -b BATCH, --batch BATCH           batch size for one loop(BATCH).
  -t TIMEOUT, --timeout TIMEOUT     http request timeout(s)(HTTP_TIMEOUT).
  -r RETRY, --retry RETRY           http request max retries(HTTP_RETRY).
  -i INTERVAL, --interval INTERVAL  if value not 0, run in infinity mode, fix record in every interval seconds(INTERVAL).
```



## Disclaimer

Only use this program after properly created backups, I am **not** responsible for any data loss or software failure related to this.

This project is only for study purpose, and **no web proxy (or its download link) provided**. If the network proxy is used in violation of local laws and regulations, the user is responsible for the consequences.

When you download, copy, compile or execute the source code or binary program of this project, it means that you have accepted the disclaimer as mentioned.



## Contributing and Issue

 Welcome to contribute code or submit an issue.
