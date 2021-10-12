import traceback
import base64
import logging
import arrow
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
from .tools import update_instance_folder
from .tools import _get_instance_config
from .logsio_writer import LogsIOWriter
from . import BUILDING_LOCK
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
RUN_CRONJOBS=0
RUN_CUPS=0
RUN_POSTGRES=0

DOCKER_LABEL_ODOO_CICD=1
DOCKER_LABEL_ODOO_CICD_INSTANCE_NAME={}

DB_HOST={}
DB_USER={}
DB_PWD={}
DB_PORT={}
""".format(
        instance_name,
        os.environ['DUMPS_PATH'],
        instance_name,
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
        'git_author': repo.head.commit.author.name,
        'git_desc': repo.head.commit.summary,
        'git_authored_date': arrow.get(repo.head.commit.authored_date).strftime("%Y-%m-%d %H:%M:%S"),
    }
    info['date'] = arrow.get().to('utc').strftime("%Y-%m-%d %H:%M:%S")
    db.updates.insert_one(info)
    info.pop("_id")

    site = db.sites.find_one({'name': site['name']})
    if site:
        db.sites.update_one(
            {'_id': site['_id']}, 
            {"$set": info },
            upsert=False
        )

def _last_success_full_sha(site):
    info = {'name': site['name']}
    updates = list(db.updates.find(info).sort([("date", pymongo.DESCENDING)]).limit(1))
    if updates:
        return updates[0]['sha']

def _reload_cmd(site_name):
    global_settings = _get_config('odoo_settings', '')
    local_settings = db.sites.find_one({'name': site_name}).get("odoo_settings", "")
    odoo_settings = global_settings + "\n" + local_settings + "\n"

    # dumps path must patch of cicd; otherwise conflicts when cicd triggers backup of odoo and checks if dump was done
    if "DUMPS_PATH" in odoo_settings:
        raise Exception("DUMPS_PATH not allowed")
    odoo_settings += f"\nDUMPS_PATH={os.environ['DUMPS_PATH']}\n"

    odoo_settings = base64.encodestring(odoo_settings.encode('utf-8')).strip().decode('utf-8')
    return [
        "reload", '-d', site_name,
        '--headless', '--devmode', '--additional_config',
        odoo_settings
        ]

def make_instance(site, use_dump):
    logger = LogsIOWriter(site['name'], 'build')
    logger.info(f"Make instance for {site}")
    settings = _get_instance_config(site['name'])
    _make_instance_docker_configs(site)

    store_output(site['name'], 'meta', (
        f"Date: {arrow.get()}"
    ))

    output = _odoo_framework(
        site['name'], 
        _reload_cmd(site['name']),
        logs_writer=logger,
    )
    store_output(site['name'], 'reload', output)

    build_command = ["build"]
    if site.get('docker_no_cache'):
        build_command += ["--no-cache"]
    output = _odoo_framework(site['name'], build_command, logs_writer=logger)
    store_output(site['name'], 'build', output)

    dump_date, dump_name = None, None
    if use_dump and not site.get('no_module_update'):
        logger.info(f"BUILD CONTROL: Restoring DB for {site['name']} from {use_dump}")
        flags = []
        if site.get('restore_no_dev_scripts'):
            flags += ['--no-dev-scripts']
        _odoo_framework(site, ["restore", "odoo-db", use_dump] + flags, logs_writer=logger)
        logger.info(f"Restoring dump {site['name']} finished")
        _odoo_framework(site, ["remove-web-assets"], logs_writer=logger)
        dump_file = Path("/opt/dumps") / use_dump
        dump_date = arrow.get(dump_file.stat().st_mtime).to('UTC').strftime("%Y-%m-%d %H:%M:%S")
        dump_name = use_dump

        _store(site['name'], {
            'dump_date': dump_date,
            'dump_name': dump_name,
            }
        )

    else:
        logger.info(f"BUILD CONTROL: Resetting DB for {site['name']}")
        if settings['DBNAME']:
            _odoo_framework(site, ["db", "reset", settings['DBNAME']], logs_writer=logger)

    if not site.get('no_module_update'):
        output = _odoo_framework(site, ["update"], logs_writer=logger)
    store_output(site['name'], 'update', output)
    _odoo_framework(site, ["turn-into-dev"], logs_writer=logger)
    set_config_parameters(site)


def fix_ownership():
    user = os.environ['HOST_SSH_USER']
    pass
    # _execute_shell
    # os.system(f"chown {user}:{user} /odoo_settings -R")

def run_robot_tests(site, files):
    output, success, failed = [], [], []
    logger = LogsIOWriter(site['name'], 'robot')
    for file in files:
        try:
            output.append(_odoo_framework(
                site,
                ["robot", str(file)],
                logs_writer=logger,
            ))
            success.append(file)
        except Exception as ex:
            failed.append(file)

    msg = []
    for failed in failed:
        msg.append(f"Failed: {failed}")
        logger.info(msg[-1])
    for success in success:
        msg.append(f"OK: {success}")
        logger.info(msg[-1])

    msg = '\n'.join(msg)
    output.append(msg)
    db.sites.update_one({
        'name': site['name'],
    }, {
        '$set': {
            'robot_result': msg
        }
    }, upsert=True
    )

    return '\n'.join(output)

def set_config_parameters(site):
    logger = LogsIOWriter(site['name'], 'misc')
    _odoo_framework(site, ["remove-settings", '--settings', 'web.base.url,web.base.url.freeze'], logs_writer=logger)
    _odoo_framework(site, ["update-setting", 'web.base.url', os.environ['CICD_URL']], logs_writer=logger)
    _odoo_framework(site, ["set-ribbon", site['name']], logs_writer=logger)
    _odoo_framework(site, ["prolong"], logs_writer=logger)


def build_instance(site):
    try:
        logger = LogsIOWriter(site, 'build')
        logger.info(f"Building instance {site['name']}")
        fix_ownership()
        started = arrow.get()
        settings = _get_instance_config(site['name'])
        _store(site['name'], {
            "build_started": started.to("utc").strftime("%Y-%m-%d %H:%M:%S"),
            })
        try:
            dump_name = site.get('dump') or os.getenv("DUMP_NAME")

            noi18n = _get_config('no_i18n', False)

            if site.get("build_mode") == 'reload_restart':
                _make_instance_docker_configs(site)
                logger.info(f"Reloading {site['name']}")
                _odoo_framework(site, 
                    _reload_cmd(site['name']),
                    logs_writer=logger,
                )
                logger.info(f"Downing {site['name']}")
                try:
                    _odoo_framework(site, ["down"], logs_writer=logger)
                except Exception as ex:
                    logger.warn(ex)

                logger.info(f"Building {site['name']}")
                _odoo_framework(site, ["build"], logs_writer=logger)
                logger.info(f"Upping {site['name']}")
                _odoo_framework(site, ["up", "-d"], logs_writer=logger)
                logger.info(f"Upped {site['name']}")
                _odoo_framework(site, ["prolong"], logs_writer=logger)

            elif site.get("build_mode") == 'update-all-modules':

                if not site.get('no_module_update'):
                    if site.get('odoo_settings_update_modules_before'):
                        output = _odoo_framework(
                            site,
                            ["update", "--no-dangling-check", site['odoo_settings_update_modules_before']],
                            logs_writer=logger
                        )

                _odoo_framework(site, ["remove-web-assets"], logs_writer=logger)
                if not site.get('no_module_update'):
                    output = _odoo_framework(
                        site,
                        ["update", "--no-dangling-check"] + ([] if noi18n else ["--i18n"]),
                        logs_writer=logger
                    )
                    store_output(site['name'], 'update', output)
                set_config_parameters(site)
                _odoo_framework(site, ["up", "-d"], logs_writer=logger)

            elif site.get("build_mode") == 'update-recent':
                if not site.get('no_module_update'):
                    if site.get('odoo_settings_update_modules_before'):
                        output = _odoo_framework(
                            site,
                            ["update", "--no-dangling-check", site['odoo_settings_update_modules_before']],
                            logs_writer=logger,
                        )
                last_sha = _last_success_full_sha(site)

                files = [Path(x) for x in _odoo_framework(
                    site,
                    ["list-changed-files", "-s", last_sha],
                    logs_writer=logger,
                ).split("---")[1].split("\n") if x]

                suffixes = set(x.suffix for x in files if x)
                output = ""

                if len(suffixes) == 1 and list(suffixes)[0] == '.robot':
                    pass
                else:

                    if not site.get('no_module_update'):
                        output += _odoo_framework(
                            site,
                            ["update", "--no-dangling-check", "--since-git-sha", last_sha] + ([] if noi18n else ["--i18n"]),
                            logs_writer=logger,
                        )
                        store_output(site['name'], 'update', output)
                        _odoo_framework(site, ["up", "-d"], logs_writer=logger)
                output = run_robot_tests(site, [x for x in files if x.suffix == '.robot'])
                if output:
                    store_output(site['name'], 'robot-tests', output)

            elif site.get("build_mode") == 'reset':
                if settings['DBNAME']:
                    _odoo_framework(
                        site,
                        ['db', 'reset', settings['DBNAME'], '--do-not-install-base'],
                        logs_writer=logger,
                        )
                make_instance(site, dump_name)
                _odoo_framework(site, ["up", "-d"], logs_writer=logger)

            else:
                raise NotImplementedError(site.get('build_mode'))

            notify_instance_updated(site)

            success = True

        except Exception as ex:
            success = False
            msg = traceback.format_exc()
            logger.error(msg)
            store_output(site['name'], 'error', str(msg))

        _store(site['name'], {
            'success': success,
            'reset-db': False,
            'docker_no_cache': False,
            'updated': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
            'duration': round((arrow.get() - started).total_seconds(), 0),
            'build_mode': False,
        })

    except Exception as ex:
        logger.error(ex)
    finally:
        with BUILDING_LOCK:
            _store(site['name'], {
                'is_building': False,
                'needs_build': False,
            })
        assert not db.sites.find_one({'name': site['name']})['needs_build']
        del threads[site['name']]


def _build():
    with BUILDING_LOCK:
        ids = map(lambda x: x['_id'], db.sites.find({}))
        for id in ids:
            db.sites.update({'_id': id}, {"$set": {'is_building': False}})  # update with empty {} did not work

    while True:
        try:
            concurrent_threads = _get_config('concurrent_builds', 5)

            count_active = len([x for x in threads.values() if x.is_alive()])
            logger.debug(f"Active builds: {count_active}, configured max builds: {concurrent_threads}")

            sites = list(db.sites.find({'needs_build': True}))
            sites = [x for x in sites if not x.get('is_building')]
            sites = [x for x in sites if not x.get('archive')]
            if not sites:
                logger.debug("Nothing to build")
            for site in sites:
                if not threads.get(site['name']) or not threads[site['name']].is_alive():
                    if count_active < concurrent_threads:
                        for key in ['reload', 'name', 'update', 'build', 'last_error']:
                            store_output(site['name'], key, "")

                        rolling_file = LogsIOWriter(site['name'], 'build')
                        rolling_file.write_text(f"Started new build: {arrow.get()}")

                        try:
                            rolling_file.write_text(f"Cloning from git...")
                            update_instance_folder(site['name'], rolling_file)
                            rolling_file.write_text(f"Cloned from git...")
                        except Exception as ex:
                            with BUILDING_LOCK:
                                msg = traceback.format_exc()
                                rolling_file.write_text(msg)
                                store_output(site['name'], 'last_error', msg)
                                _store(site['name'], {'is_building': False, 'needs_build': False, 'success': False})
                            continue

                        with BUILDING_LOCK:
                            _store(site['name'], {'is_building': True})
                        thread = threading.Thread(target=build_instance, args=(site,))
                        threads[site['name']] = thread
                        thread.start()
                        count_active += 1

        except Exception as ex:
            msg = traceback.format_exc()
            logger.error(msg)

        finally:
            time.sleep(1)


def start():
    logger.info("Starting job to build instances")
    t = threading.Thread(target=_build)
    t.daemon = True
    t.start()
