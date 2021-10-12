import logging
import humanize 
import pymongo
import time
import threading
from .. import db
from .tools import _get_db_conn
from .tools import _get_config, _set_config
from .tools import _get_instance_config
import os
import re
import subprocess
import sys
from pathlib import Path
import json
import requests
import time
import arrow
import click
from .tools import _get_repo
from dotenv import load_dotenv
from datetime import datetime
from .tools import _store
from git import Repo
from .tools import store_output, get_output
logger = logging.getLogger(__name__)

def _get_db_size(dbname):
    try:
        conn = _get_db_conn()
        try:
            cr = conn.cursor()
            cr.execute(f"select pg_database_size('{dbname}')")
            db_size = cr.fetchone()[0]
        finally:
            conn.close()
    except: db_size = 0
    return db_size

def _usages():
    while True:
        try:
            for site in db.sites.find({}):
                name = site['name']

                path = Path('/cicd_workspace') / name
                source_size = 0
                source_size_humanize = ""
                if path.exists():
                    source_size = round(sum(f.stat().st_size for f in path.glob('**/*') if f.is_file()), 0)
                    source_size_humanize = humanize.naturalsize(source_size)

                settings = _get_instance_config(name)
                dbname = settings.get('DBNAME', "")
                db_size = 0
                if dbname:
                    db_size = _get_db_size(dbname)

                db.sites.update_one({'_id': site['_id']}, {'$set': {
                    'db_size': db_size,
                    'db_size_humanize': humanize.naturalsize(db_size),
                    'source_size': source_size,
                    'source_size_humanize': source_size_humanize,
                }}, upsert=False)

        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(5)


def start():
    logger.info("Starting job to scan resources")
    t = threading.Thread(target=_usages)
    t.daemon = True
    t.start()
