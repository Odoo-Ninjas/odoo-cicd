import logging
import time
import threading

from .logsio_writer import LogsIOWriter
from .. import db
import docker as Docker
import arrow
from .tools import _get_docker_state
from .tools import _odoo_framework
logger = logging.getLogger(__name__)
client = Docker.from_env()

def _get_docker_states_background():
    while True:
        try:
            logger.debug("Getting docker state from jenkins")
            sites = list(db.sites.find({}))
            for site in sites:
                site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'
                db.sites.update_one({
                    '_id': site['_id'],
                }, {'$set': {
                    'docker_state': site['docker_state'],
                }
                }, upsert=False)
            logger.debug(f"Finished updating docker job for {len(sites)} sites.")
        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(10)


def cycle_down_apps():
    while True:
        sites = db.sites.find({'name': 1, 'last_access': 1})
        for site in sites:
            try:
                logger = LogsIOWriter(site['name'], 'misc')
                logger.debug(f"Checking site to cycle down: {site['name']}")
                if (arrow.get() - arrow.get(site.get('last_access', '1980-04-04') or '1980-04-04')).total_seconds() > 2 * 3600: # TODO configurable
                    if _get_docker_state(site['name']) == 'running':
                        logger.debug(f"Cycling down instance due to inactivity: {site['name']}")
                        _odoo_framework(site['name'], 'kill', logs_writer=logger)

            except Exception as ex:
                import traceback
                msg = traceback.format_exc()
                logger.error(msg)
        time.sleep(10)


def start():
    t = threading.Thread(target=cycle_down_apps)
    t.daemon = True
    t.start()

    t = threading.Thread(target=_get_docker_states_background)
    t.daemon = True
    t.start()