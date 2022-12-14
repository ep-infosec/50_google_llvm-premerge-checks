#!/usr/bin/env python3
import logging
import psycopg2
import os
import datetime
import requests
from typing import Optional, Dict, List
import json

PHABRICATOR_URL = "https://reviews.llvm.org/api/"
BUILDBOT_URL = "https://lab.llvm.org/buildbot/api/v2/"


# TODO(kuhnel): retry on connection issues, maybe resuse
# https://github.com/google/llvm-premerge-checks/blob/main/scripts/phabtalk/phabtalk.py#L44

# TODO(kuhnel): Import the step data so we can figure out in which step a build fails
# (e.g. compile vs. test)


def connect_to_db() -> psycopg2.extensions.connection:
    """Connect to the database."""
    conn = psycopg2.connect(
        f"host=127.0.0.1 sslmode=disable dbname=stats user=stats password={os.getenv('DB_PASSWORD')}")
    return conn


def create_tables(conn: psycopg2.extensions.connection):
    cur = conn.cursor()
    cur.execute(
        """CREATE TABLE IF NOT EXISTS buildbot_workers (
            timestamp timestamp NOT NULL, 
            worker_id integer NOT NULL, 
            data jsonb NOT NULL
            );"""
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS buildbot_worker_ids 
        ON buildbot_workers 
        (worker_id);"""
    )
    cur.execute(
        """CREATE INDEX IF NOT EXISTS buildbot_worker_timestamp 
        ON buildbot_workers 
        (timestamp);"""
    )
    # Note: step_data is not yet populated with data!
    cur.execute(
        """CREATE TABLE IF NOT EXISTS buildbot_builds (
            build_id integer PRIMARY KEY,
            builder_id integer NOT NULL, 
            build_number integer NOT NULL, 
            build_data jsonb NOT NULL,
            step_data jsonb
            );"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS buildbot_buildsets (
            buildset_id integer PRIMARY KEY, 
            data jsonb NOT NULL
            );"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS buildbot_buildrequests (
            buildrequest_id integer PRIMARY KEY, 
            buildset_id integer NOT NULL, 
            data jsonb NOT NULL
            );"""
    )
    cur.execute(
        """CREATE TABLE IF NOT EXISTS buildbot_builders (
            builder_id integer PRIMARY KEY, 
            timestamp timestamp NOT NULL, 
            name text NOT NULL, 
            data jsonb NOT NULL
            );"""
    )
    conn.commit()


def get_worker_status(
        worker_id: int, conn: psycopg2.extensions.connection
) -> Optional[Dict]:
    """Note: postgres returns a dict for a stored json object."""
    cur = conn.cursor()
    cur.execute(
        "SELECT data FROM buildbot_workers WHERE worker_id = %s ORDER BY timestamp DESC;",
        [worker_id],
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def get_builder_status(
        builder_id: int, conn: psycopg2.extensions.connection
) -> Optional[Dict]:
    """Note: postgres returns a dict for a stored json object."""
    cur = conn.cursor()
    cur.execute(
        """SELECT data FROM buildbot_builders WHERE builder_id = %s 
        ORDER BY timestamp DESC;""",
        [builder_id],
    )
    row = cur.fetchone()
    if row is None:
        return None
    return row[0]


def set_worker_status(
        timestamp: datetime.datetime,
        worker_id: int,
        data: str,
        conn: psycopg2.extensions.connection,
):
    cur = conn.cursor()
    cur.execute(
        """INSERT INTO buildbot_workers (timestamp, worker_id, data) 
        values (%s,%s,%s);""",
        (timestamp, worker_id, data),
    )


def update_workers(conn: psycopg2.extensions.connection):
    logging.info("Updating worker status...")
    response = requests.get(BUILDBOT_URL + "workers")
    timestamp = datetime.datetime.now()
    for worker in response.json()["workers"]:
        worker_id = worker["workerid"]
        data = json.dumps(worker)
        # TODO: It would be faster if request all worker info and cache it
        # locally
        old_data = get_worker_status(worker_id, conn)
        # only update worker information if it has changed as this data is quite
        # static
        if old_data is None or worker != old_data:
            set_worker_status(timestamp, worker_id, data, conn)
    conn.commit()


def update_builders(conn: psycopg2.extensions.connection):
    """get list of all builder ids."""
    logging.info("Updating builder status...")
    response = requests.get(BUILDBOT_URL + "builders")
    timestamp = datetime.datetime.now()
    for builder in response.json()["builders"]:
        builder_id = builder["builderid"]
        data = json.dumps(builder)
        # TODO: It would be faster if request all builder info and cache it
        # locally
        old_data = get_builder_status(builder_id, conn)
        # only update worker information if it has changed as this data is quite
        # static
        if old_data is None or builder != old_data:
            set_worker_status(timestamp, builder_id, data, conn)
    conn.commit()


def get_last_build(conn: psycopg2.extensions.connection) -> int:
    """Get the latest build number for a builder.

    This is used to only get new builds."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(build_id) FROM buildbot_builds")
    row = cur.fetchone()
    if row is None or row[0] is None:
        return 0
    return row[0]


def update_build_status(conn: psycopg2.extensions.connection):
    start_id = get_last_build(conn)
    logging.info("Updating build results, starting with {}...".format(start_id))
    url = BUILDBOT_URL + "builds"
    cur = conn.cursor()
    for result_set in rest_request_iterator(url, "builds", "buildid", start_id=start_id):
        args_str = b",".join(
            cur.mogrify(
                b" (%s,%s,%s,%s) ",
                (
                    build["buildid"],
                    build["builderid"],
                    build["number"],
                    json.dumps(build, sort_keys=True),
                ),
            )
            for build in result_set
            if build["complete"]
        )
        cur.execute(
            b"INSERT INTO buildbot_builds (build_id, builder_id, build_number, build_data) values "
            + args_str
        )
        logging.info("last build id: {}".format(result_set[-1]["buildid"]))
        conn.commit()


def rest_request_iterator(
        url: str,
        array_field_name: str,
        id_field_name: str,
        start_id: int = 0,
        step: int = 1000,
):
    """Request paginated data from the buildbot master.

    This returns a generator. Each call to it gives you shards of
    <=limit results. This can be used to do a mass-SQL insert of data.

    Limiting the range of the returned IDs causes Buildbot to sort the data.
    This makes incremental imports much easier.
    """
    while True:
        count = 0
        stop_id = start_id + step
        response = requests.get(
            url
            + "?{id_field_name}__gt={start_id}&{id_field_name}__le={stop_id}&".format(
                **locals()
            )
        )
        if response.status_code != 200:
            raise Exception(
                "Got status code {} on request to {}".format(response.status_code, url)
            )
        results = response.json()[array_field_name]
        if len(results) == 0:
            return
        yield results
        start_id = stop_id


def get_latest_buildset(conn: psycopg2.extensions.connection) -> int:
    """Get the maximumg buildset id.

    This is useful for incremental updates."""
    cur = conn.cursor()
    cur.execute("SELECT MAX(buildset_id) from buildbot_buildsets;")
    row = cur.fetchone()
    if row[0] is None:
        return 0
    return row[0]


def update_buildsets(conn: psycopg2.extensions.connection):
    start_id = get_latest_buildset(conn)
    logging.info("Getting buildsets, starting with {}...".format(start_id))
    url = BUILDBOT_URL + "buildsets"
    cur = conn.cursor()

    for result_set in rest_request_iterator(
            url, "buildsets", "bsid", start_id=start_id
    ):
        args_str = b",".join(
            cur.mogrify(
                b" (%s,%s) ",
                (buildset["bsid"], json.dumps(buildset, sort_keys=True)),
            )
            for buildset in result_set
            if buildset["complete"]
        )

        if len(args_str) == 0:
            break
        cur.execute(
            b"INSERT INTO buildbot_buildsets (buildset_id, data) values " + args_str
        )
        logging.info("last id {}".format(result_set[-1]["bsid"]))
        conn.commit()


def get_latest_buildrequest(conn: psycopg2.extensions.connection) -> int:
    cur = conn.cursor()
    cur.execute("SELECT MAX(buildrequest_id) from buildbot_buildrequests;")
    row = cur.fetchone()
    if row[0] is None:
        return 0
    return row[0]


def update_buildrequests(conn: psycopg2.extensions.connection):
    start_id = get_latest_buildrequest(conn)
    logging.info("Getting buildrequests, starting with {}...".format(start_id))
    url = BUILDBOT_URL + "buildrequests"
    cur = conn.cursor()
    for result_set in rest_request_iterator(
            url, "buildrequests", "buildrequestid", start_id=start_id
    ):
        # cur.mogrify returns a byte string, so we need to join on a byte string
        args_str = b",".join(
            cur.mogrify(
                " (%s,%s,%s) ",
                (
                    buildrequest["buildrequestid"],
                    buildrequest["buildsetid"],
                    json.dumps(buildrequest),
                ),
            )
            for buildrequest in result_set
            if buildrequest["complete"]
        )
        if len(args_str) == 0:
            break
        cur.execute(
            b"INSERT INTO buildbot_buildrequests (buildrequest_id, buildset_id, data) values "
            + args_str
        )
        logging.info("{}".format(result_set[-1]["buildrequestid"]))
        conn.commit()


if __name__ == "__main__":
    logging.basicConfig(level='INFO', format='%(levelname)-7s %(message)s')
    conn = connect_to_db()
    create_tables(conn)
    update_workers(conn)
    update_builders(conn)
    update_build_status(conn)
    update_buildsets(conn)
    update_buildrequests(conn)
