#!/usr/bin/env python3
"""
Test script for teslamate_fix_addrs.py using SQLite.

Creates a SQLite database with TeslaMate-compatible tables, inserts test data,
and runs the program in Mode 0 (OSM fix), Mode 1 (map API update) and
Mode 3 (map API fix without OSM) to verify functionality.
"""

import sqlite3
import subprocess
import sys
import os
import json
import time

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
MAIN_SCRIPT = os.path.join(SCRIPT_DIR, 'teslamate_fix_addrs.py')
DB_PATH = os.path.join(SCRIPT_DIR, 'test.db')
DB_URL = 'sqlite:///' + DB_PATH
CHECKPOINT_PATH = os.path.join(SCRIPT_DIR, 'test_checkpoint.json')

MAP_API_KEY = os.environ.get('TENCENT_KEY', '')
MAP_API_SK = os.environ.get('TENCENT_SK', '')

# Test coordinates: Chinese cities
POSITIONS = [
    (39.984154, 116.307490),    # Beijing Haidian
    (31.230416, 121.473701),    # Shanghai
    (23.129110, 113.264385),    # Guangzhou
]


def cleanup():
    """Remove test artifacts from previous runs."""
    for path in [DB_PATH, CHECKPOINT_PATH]:
        if os.path.exists(path):
            os.remove(path)
            print("  Removed %s" % path)


def create_database():
    """Create SQLite database with TeslaMate-compatible schema."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    c.execute('''CREATE TABLE addresses (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        display_name TEXT,
        latitude REAL,
        longitude REAL,
        name TEXT,
        house_number TEXT,
        road TEXT,
        neighbourhood TEXT,
        city TEXT,
        county TEXT,
        postcode TEXT,
        state TEXT,
        state_district TEXT,
        country TEXT,
        osm_id BIGINT,
        osm_type TEXT,
        raw TEXT,
        inserted_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )''')

    c.execute('''CREATE TABLE positions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        latitude REAL,
        longitude REAL
    )''')

    c.execute('''CREATE TABLE drives (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        start_position_id INTEGER REFERENCES positions(id),
        end_position_id INTEGER REFERENCES positions(id),
        start_address_id INTEGER REFERENCES addresses(id),
        end_address_id INTEGER REFERENCES addresses(id)
    )''')

    c.execute('''CREATE TABLE charging_processes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        position_id INTEGER REFERENCES positions(id),
        address_id INTEGER REFERENCES addresses(id)
    )''')

    conn.commit()
    return conn


def insert_test_data(conn):
    """Insert test positions, drives, and charging processes."""
    c = conn.cursor()

    # Insert 3 positions
    for lat, lng in POSITIONS:
        c.execute(
            'INSERT INTO positions (latitude, longitude) VALUES (?, ?)',
            (lat, lng))

    # Drive 1: Beijing (pos 1) -> Shanghai (pos 2), no addresses
    c.execute(
        'INSERT INTO drives (start_position_id, end_position_id, '
        'start_address_id, end_address_id) VALUES (?, ?, ?, ?)',
        (1, 2, None, None))

    # Drive 2: Shanghai (pos 2) -> Guangzhou (pos 3), no addresses
    c.execute(
        'INSERT INTO drives (start_position_id, end_position_id, '
        'start_address_id, end_address_id) VALUES (?, ?, ?, ?)',
        (2, 3, None, None))

    # Charging at Beijing (pos 1), no address
    c.execute(
        'INSERT INTO charging_processes (position_id, address_id) '
        'VALUES (?, ?)',
        (1, None))

    # Charging at Guangzhou (pos 3), no address
    c.execute(
        'INSERT INTO charging_processes (position_id, address_id) '
        'VALUES (?, ?)',
        (3, None))

    conn.commit()

    # Print summary
    c.execute('SELECT COUNT(*) FROM positions')
    print("  Positions: %d" % c.fetchone()[0])
    c.execute('SELECT COUNT(*) FROM drives')
    print("  Drives: %d" % c.fetchone()[0])
    c.execute('SELECT COUNT(*) FROM charging_processes')
    print("  Charging processes: %d" % c.fetchone()[0])
    c.execute('SELECT COUNT(*) FROM addresses')
    print("  Addresses: %d" % c.fetchone()[0])


def run_program(mode, extra_args=None):
    """Run the main program with specified mode via subprocess."""
    cmd = [
        sys.executable, MAIN_SCRIPT,
        '--db-url', DB_URL,
        '-u', 'x', '-p', 'x', '-H', 'x', '-P', '0', '-d', 'x',
        '-m', str(mode),
        '-k', MAP_API_KEY,
        '--sk', MAP_API_SK,
        '-c', CHECKPOINT_PATH,
        '-b', '10',
    ]
    if extra_args:
        cmd.extend(extra_args)

    print("\n" + "=" * 60)
    print("Running Mode %d" % mode)
    print("Command: %s" % ' '.join(cmd))
    print("=" * 60)

    env = os.environ.copy()
    env['LOG_LEVEL'] = 'DEBUG'

    result = subprocess.run(cmd, text=True, env=env)
    return result.returncode


def check_all_drives_fixed():
    """Check whether all drives have addresses assigned."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT COUNT(*) FROM drives '
        'WHERE start_address_id IS NULL OR end_address_id IS NULL')
    unfixed = c.fetchone()[0]
    conn.close()
    return unfixed == 0


def check_all_chargings_fixed():
    """Check whether all charging processes have addresses assigned."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute(
        'SELECT COUNT(*) FROM charging_processes '
        'WHERE address_id IS NULL AND position_id IS NOT NULL')
    unfixed = c.fetchone()[0]
    conn.close()
    return unfixed == 0


def verify_mode0():
    """Verify Mode 0 results: addresses should be populated."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("\n" + "=" * 60)
    print("Verifying Mode 0 (OSM fix) results")
    print("=" * 60)

    c.execute('SELECT COUNT(*) FROM addresses')
    addr_count = c.fetchone()[0]
    print("  Addresses created: %d" % addr_count)

    # Check drives
    c.execute('SELECT id, start_address_id, end_address_id FROM drives')
    drives_ok = True
    for row in c.fetchall():
        ok = row[1] is not None and row[2] is not None
        print("  Drive %d: start_addr=%s, end_addr=%s [%s]" %
              (row[0], row[1], row[2], "OK" if ok else "MISSING"))
        if not ok:
            drives_ok = False

    # Check charging processes
    c.execute('SELECT id, address_id FROM charging_processes')
    chargings_ok = True
    for row in c.fetchall():
        ok = row[1] is not None
        print("  Charging %d: address_id=%s [%s]" %
              (row[0], row[1], "OK" if ok else "MISSING"))
        if not ok:
            chargings_ok = False

    # Show address details
    c.execute(
        'SELECT id, display_name, country, state, city FROM addresses')
    for row in c.fetchall():
        print("  Address %d: %s (country=%s, state=%s, city=%s)" %
              (row[0], row[1], row[2], row[3], row[4]))

    conn.close()

    success = addr_count > 0 and drives_ok and chargings_ok
    print("\n  Mode 0 verification: %s" % ("PASSED" if success else "FAILED"))
    return success


def verify_mode1():
    """Verify Mode 1 results: addresses should have map API data."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("\n" + "=" * 60)
    print("Verifying Mode 1 (map API update) results")
    print("=" * 60)

    c.execute(
        'SELECT id, display_name, country, state, city, county, road, '
        'name, house_number, neighbourhood, postcode '
        'FROM addresses')
    rows = c.fetchall()

    all_ok = True
    for row in rows:
        (addr_id, display, country, state, city, county, road,
         name, house_number, neighbourhood, postcode) = row
        ok = bool(country and state and city)
        print("  Address %d:" % addr_id)
        print("    display_name  = %s" % display)
        print("    country       = %s" % country)
        print("    state         = %s" % state)
        print("    city          = %s" % city)
        print("    county        = %s" % county)
        print("    road          = %s" % road)
        print("    name          = %s" % name)
        print("    house_number  = %s" % house_number)
        print("    neighbourhood = %s" % neighbourhood)
        print("    postcode      = %s" % postcode)
        print("    [%s]" % ("OK" if ok else "INCOMPLETE"))
        if not ok:
            all_ok = False

    conn.close()

    print("\n  Mode 1 verification: %s" % ("PASSED" if all_ok else "FAILED"))
    return all_ok


def reset_addresses():
    """Drop all addresses and unlink them, as if OSM had never run."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('UPDATE drives SET start_address_id = NULL, '
              'end_address_id = NULL')
    c.execute('UPDATE charging_processes SET address_id = NULL')
    c.execute('DELETE FROM addresses')
    conn.commit()
    conn.close()


def verify_mode3():
    """Verify Mode 3 results: addresses created by the map API alone."""
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()

    print("\n" + "=" * 60)
    print("Verifying Mode 3 (map API fix, no OSM) results")
    print("=" * 60)

    ok = True

    c.execute('SELECT COUNT(*) FROM addresses')
    address_count = c.fetchone()[0]
    print("  Addresses created: %d (expected %d)" %
          (address_count, len(POSITIONS)))
    if address_count != len(POSITIONS):
        print("  FAILED: expected one address per distinct position")
        ok = False

    c.execute('SELECT DISTINCT osm_type FROM addresses')
    osm_types = [row[0] for row in c.fetchall()]
    print("  osm_type values: %s" % osm_types)
    if osm_types != ['tencent']:
        print("  FAILED: addresses should be tagged with the map source")
        ok = False

    c.execute('SELECT COUNT(DISTINCT osm_id) FROM addresses')
    if c.fetchone()[0] != address_count:
        print("  FAILED: osm_id is not unique per address")
        ok = False

    c.execute('SELECT display_name, city, road, neighbourhood FROM addresses')
    for display_name, city, road, neighbourhood in c.fetchall():
        print("    %s | city=%s | road=%s | neighbourhood=%s" %
              (display_name, city, road, neighbourhood))
        if not display_name:
            print("  FAILED: display_name is empty")
            ok = False
        if not neighbourhood:
            print("  FAILED: neighbourhood is empty")
            ok = False

    conn.close()

    if not check_all_drives_fixed() or not check_all_chargings_fixed():
        print("  FAILED: some records are still unlinked")
        ok = False

    if ok:
        print("  PASSED")
    return ok


def verify_checkpoint():
    """Verify checkpoint file was created and contains expected data."""
    print("\n" + "=" * 60)
    print("Verifying checkpoint file")
    print("=" * 60)

    if not os.path.exists(CHECKPOINT_PATH):
        print("  Checkpoint file NOT found at %s" % CHECKPOINT_PATH)
        return False

    with open(CHECKPOINT_PATH, 'r') as f:
        data = json.load(f)

    print("  Checkpoint contents:")
    for line in json.dumps(data, indent=2, ensure_ascii=False).split('\n'):
        print("    %s" % line)

    ok = data.get('last_run') is not None
    print("\n  Checkpoint verification: %s" % ("PASSED" if ok else "FAILED"))
    return ok


def main():
    print("TeslaMate Address Fixer - SQLite Test")
    print("=" * 60)

    # Step 1: Cleanup
    print("\nStep 1: Cleaning up previous test artifacts...")
    cleanup()

    # Step 2: Create database
    print("\nStep 2: Creating SQLite database...")
    conn = create_database()
    print("  Database created at %s" % DB_PATH)

    # Step 3: Insert test data
    print("\nStep 3: Inserting test data...")
    insert_test_data(conn)
    conn.close()

    # Step 4: Run Mode 0 (OSM fix) with retries for rate limiting
    print("\nStep 4: Running Mode 0 (OSM address fix)...")
    max_retries = 3
    for attempt in range(1, max_retries + 1):
        print("\n  --- Attempt %d/%d ---" % (attempt, max_retries))
        run_program(0, ['--reset-checkpoint'] if attempt == 1 else [])

        drives_done = check_all_drives_fixed()
        chargings_done = check_all_chargings_fixed()

        if drives_done and chargings_done:
            print("  All records fixed on attempt %d" % attempt)
            break
        else:
            if not drives_done:
                print("  Some drives still have NULL addresses")
            if not chargings_done:
                print("  Some charging processes still have NULL addresses")
            if attempt < max_retries:
                print("  Waiting 5 seconds before retry "
                      "(OSM rate limiting)...")
                time.sleep(5)

    # Step 5: Verify Mode 0
    print("\nStep 5: Verifying Mode 0 results...")
    mode0_ok = verify_mode0()

    # Step 6: Run Mode 1 (map API update)
    print("\nStep 6: Running Mode 1 (map API update)...")
    run_program(1, ['--reset-checkpoint'])

    # Step 7: Verify Mode 1
    print("\nStep 7: Verifying Mode 1 results...")
    mode1_ok = verify_mode1()

    # Step 8: Run Mode 3 (map API fix without OSM)
    print("\nStep 8: Running Mode 3 (map API address fix, no OSM)...")
    reset_addresses()
    run_program(3, ['--reset-checkpoint'])

    # Step 9: Verify Mode 3
    print("\nStep 9: Verifying Mode 3 results...")
    mode3_ok = verify_mode3()

    # Step 10: Verify checkpoint
    print("\nStep 10: Verifying checkpoint...")
    checkpoint_ok = verify_checkpoint()

    # Summary
    print("\n" + "=" * 60)
    print("TEST SUMMARY")
    print("=" * 60)
    results = [
        ("Mode 0 (OSM fix)", mode0_ok),
        ("Mode 1 (map API update)", mode1_ok),
        ("Mode 3 (map API fix)", mode3_ok),
        ("Checkpoint", checkpoint_ok),
    ]
    for name, ok in results:
        print("  %-26s %s" % (name + ":", "PASSED" if ok else "FAILED"))

    all_passed = all(ok for _, ok in results)
    print("\n  Overall: %s" %
          ("ALL TESTS PASSED" if all_passed else "SOME TESTS FAILED"))

    return 0 if all_passed else 1


if __name__ == '__main__':
    sys.exit(main())
