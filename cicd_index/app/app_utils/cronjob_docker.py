import logging
import time
import threading
from .. import db
import docker as Docker
import arrow
logger = logging.getLogger(__name__)
client = Docker.from_env()

def _get_docker_state():
    while True:
        try:
            logger.info("Getting docker state from jenkins")
            sites = list(db.sites.find({}))
            for site in sites:
                site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'
                db.sites.update_one({
                    '_id': site['_id'],
                }, {'$set': {
                    'docker_state': site['docker_state'],
                }
                }, upsert=False)
            logger.info(f"Finished updating docker job for {len(sites)} sites.")
        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(10)

logger.info("Starting docker state updater")
t = threading.Thread(target=_get_docker_state)
t.daemon = True
t.start()

def cycle_down_apps():
    while True:
        try:
            sites = db.sites.find({'name': 1, 'last_access': 1})
            for site in sites:
                logger.debug(f"Checking site to cycle down: {site['name']}")
                if (arrow.get() - arrow.get(site.get('last_access', '1980-04-04') or '1980-04-04')).total_seconds() > 2 * 3600: # TODO configurable
                    if _get_docker_state(site['name']) == 'running':
                        logger.info(f"Cycling down instance due to inactivity: {site['name']}")
                        _odoo_framework(site['name'], 'kill')

        except Exception as e:
            logging.error(e)
        time.sleep(10)


t = threading.Thread(target=cycle_down_apps)
t.daemon = True
t.start()

