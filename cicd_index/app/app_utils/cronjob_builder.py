import logging
import pymongo
import time
import threading
from .. import db
from .tools import _odoo_framework
from .tools import _get_config, _set_config
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

threads = {} # for multitasking

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

def notify_instance_updated(site):
    repo = _get_repo(site['name'])
    sha = str(repo.active_branch.commit)
    info = {
        'name': site['name'],
        'sha': sha,
    }
    info['date'] = arrow.get().to('utc').strftime("%Y-%m-%d %H:%M:%S")
    db.updates.insert_one(info)

def _last_success_full_sha(site):
    info = {'name': site['name']}
    updates = list(db.updates.find(info).sort([("date", pymongo.DESCENDING)]).limit(1))
    if updates:
        return updates[0]['sha']


def make_instance(site, use_dump):
    logger.info(f"Make instance for {site}")
    _make_instance_docker_configs(site)

    output = _odoo_framework(
        site['name'], 
        ["reload", '-d', site['name'], '--headless', '--devmode']
    )
    store_output(site['name'], 'reload', output)

    output = _odoo_framework(
        site['name'], 
        ["build"], # build containers; use new pip packages
    )
    store_output(site['name'], 'build', output)

    dump_date, dump_name = None, None
    if use_dump:
        logger.info(f"BUILD CONTROL: Restoring DB for {site['name']} from {use_dump}")
        _odoo_framework(site, ["restore", "odoo-db", use_dump])
        _odoo_framework(site, ["remove-web-assets"])
        dump_file = Path("/opt/dumps") / use_dump
        dump_date = arrow.get(dump_file.stat().st_mtime).to('UTC').strftime("%Y-%m-%d %H:%M:%S")
        dump_name = use_dump

        _store(site['name'], {
            'dump_date': dump_date,
            'dump_name': dump_name,
            'is_building': True,
            }
        )

    else:
        logger.info(f"BUILD CONTROL: Resetting DB for {site['name']}")
        _odoo_framework(site, ["db", "reset"])

    output = _odoo_framework(site, ["update"])
    store_output(site['name'], 'update', output)

    _odoo_framework(site, ["turn-into-dev", "turn-into-dev"])
    _odoo_framework(site, ["set-ribbon", site['name']])



def build_instance(site):
    try:
        logger.info(f"Building instance {site['name']}")
        started = arrow.get()
        _store(site['name'], {
            "is_building": True,
            "build_started": started.to("utc").strftime("%Y-%m-%d %H:%M:%S"),
            })
        try:
            dump_name = site.get('dump') or os.getenv("DUMP_NAME")

            last_sha = _last_success_full_sha(site)
            if site.get('reset-db'):
                _odoo_framework(site, ['db', 'reset'])

            if not last_sha or site.get('force_rebuild'):
                logger.debug(f"Make new instance: force rebuild: {site.get('force_rebuild')} / last sha: {last_sha and last_sha.get('sha')}")
                make_instance(site, dump_name)
            else:
                if site.get('do-build-all'):
                    output = _odoo_framework(
                        site, 
                        ["update", "--no-dangling-check", "--i18n"]
                    )
                else:
                    output = _odoo_framework(
                        site, 
                        ["update", "--no-dangling-check", "--since-git-sha", last_sha, "--i18n"]
                    )

                store_output(site['name'], 'update', output)

                _odoo_framework(["up", "-d"])

            notify_instance_updated(site)

            success = True
        except (Exception, BaseException) as ex:
            success = False
            logger.error(ex)
        
        db.sites.update_one({
            'name': site['name'],
        }, {'$set': {
            'is_building': False,
            'needs_build': False,
            'success': success,
            'force_rebuild': False,
            'do-build-all': False,
            'reset-db': False,
            'updated': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            'duration': (arrow.get() - started).total_seconds(),
        }
        }, upsert=False)

        del threads[site['name']]
        
    except Exception as ex:
        logger.error(ex)


def _build():
    while True:
        try:
            # todo from db
            concurrent_threads = _get_config('concurrent_builds', 5)

            count_active = len([x for x in threads.values() if x.is_alive()])
            logger.info(f"Active builds: {count_active}, configured max builds: {concurrent_threads}")

            sites = list(db.sites.find({'needs_build': True}))
            for site in sites:
                if not threads.get(site['name']) or not threads[site['name']].is_alive():
                    if count_active < concurrent_threads:
                        thread = threading.Thread(target=build_instance, args=(site,))
                        threads[site['name']] = thread
                        thread.start()
                        count_active += 1

        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(1)


def start():
    logger.info("Starting job to build instances")
    t = threading.Thread(target=_build)
    t.daemon = True
    t.start()
