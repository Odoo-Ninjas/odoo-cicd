import logging
import pymongo
import time
import threading
from .. import db
from .tools import _odoo_framework
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
from dotenv import load_dotenv
from datetime import datetime
logger = logging.getLogger(__name__)

# context.jira_wrapper.comment(
#     instance['git_branch'],
#     f"Instance updated {name} in {duration} seconds."
# )

def _make_instance_docker_configs(site):
    instance_name = site['name']
    odoo_settings = Path("/odoo_settings")  # e.g. /home/odoo/.odoo
    file = odoo_settings / f'docker-compose.{instance_name}.yml'
    file.write_text("""
services:
    proxy:
        networks:
            - cicd_network
networks:
    cicd_network:
        external:
            name: {}
    """.format(os.environ["CICD_NETWORK_NAME"]))

    (odoo_settings / f'settings.{instance_name}').write_text("""
DEVMODE=1
PROJECT_NAME={}
DUMPS_PATH={}
RUN_PROXY_PUBLISHED=0
RUN_ODOO_CRONJOBS=0
RUN_ODOO_QUEUEJOBS=0
RUN_CRONJOBS=0
RUN_CUPS=0
RUN_POSTGRES=0

DB_HOST={}
DB_USER={}
DB_PWD={}
DB_PORT={}
""".format(
        instance_name,
        os.environ['DUMPS_PATH'],
        os.environ['DB_HOST'],
        os.environ['DB_USER'],
        os.environ['DB_PASSWORD'],
        os.environ['DB_PORT'],
    ))

def make_instance(site, use_dump):
    logger.info(f"BUILD CONTROL: Making Instance for {site['name']}")
    _make_instance_docker_configs(site)

    _odoo_framework(
        site['name'], 
        ["reload", '-d', site['name'], '--headless', '--devmode']
    )
    _odoo_framework(
        site['name'], 
        ["build"], # build containers; use new pip packages
    )

    dump_date, dump_name = None, None
    if use_dump:
        logger.info(f"BUILD CONTROL: Restoring DB for {site['name']} from {use_dump}")
        _odoo_framework(site, ["restore", "odoo-db", use_dump])
        _odoo_framework(site, ["remove-web-assets"])
        dump_file = Path("/opt/dumps") / use_dump
        dump_date = arrow.get(dump_file.stat().st_mtime).to('UTC').strftime("%Y-%m-%d %H:%M:%S")
        dump_name = use_dump

        db.sites.update_one({
            'name': site['name'],
        }, {
            '$set': {
                'dump_date': dump_date,
                'dump_name': dump_name,
                'is_building': True,
                },
        }, upsert=False)

    else:
        logger.info(f"BUILD CONTROL: Resetting DB for {site['name']}")
        _odoo_framework(site, ["db", "reset"])

    _odoo_framework(site, ["update"])
    _odoo_framework(site, ["turn-into-dev", "turn-into-dev"])
    _odoo_framework(site, ["set-ribbon", site['name']])

def _last_success_full_sha(site):
    info = {'name': site['name']}
    updates = list(db.updates.find(info).sort([("date", pymongo.DESCENDING)]).limit(1))
    if updates:
        return updates[0]['sha']


def build_instance(site):
    db.sites.update_one({
        'name': site['name'],
    }, {'$set': {
        'is_building': True,
    }
    }, upsert=False)
    started = arrow.get()
    try:
        dump_name = site.get('dump') or os.getenv("DUMP_NAME")

        logger.info(f"Updating instance {site.get('name')}")
        last_sha = _last_success_full_sha(site)

        if not last_sha or site.get('force_rebuild'):
            logger.info(f"Make new instance: force rebuild: {site.get('force_rebuild')} / last sha: {last_sha.get('sha')}")
            make_instance(site, dump_name)
        else:
            if site.get('do-build-all'):
                _odoo_framework(
                    site, 
                    ["update", "--no-dangling-check", "--i18n"]
                )
            else:
                _odoo_framework(
                    site, 
                    ["update", "--no-dangling-check", "--since-git-sha", last_sha, "--i18n"]
                )

            _odoo_framework(["up", "-d"])

    except Exception as ex:
        success = False
        logger.error(ex)

    finally:
        success = True
    
    db.sites.update_one({
        'name': site['name'],
    }, {'$set': {
        'is_building': False,
        'needs_build': False,
        'success': success,
        'force_rebuild': False,
        'do-build-all': False,
        'duration': (arrow.get() - started).total_seconds(),
    }
    }, upsert=False)
    


def _build():
    while True:
        try:

            sites = list(db.sites.find({'needs_build': True}))
            for site in sites:
                build_instance(site)

        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(5)


logger.info("Starting job to build instances")
t = threading.Thread(target=_build)
t.daemon = True
t.start()