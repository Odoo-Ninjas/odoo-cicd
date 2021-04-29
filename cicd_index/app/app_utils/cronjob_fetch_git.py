import logging
import time
import threading
logger = logging.getLogger(__name__)

def _get_git_state():
    while True:
        try:
            logger.info("Getting job state from jenkins")
            sites = list(db.sites.find({}))
            for site in sites:
                try:
                    job = _get_jenkins_job(site['git_branch'])
                except Exception as ex:
                    site['last_build'] = f"Error: {ex}"
                else:
                    if not job:
                        continue
                    last_build = job.get_last_build_or_none()
                    if last_build:
                        site['last_build'] = last_build.get_status()
                        site['duration'] = round(last_build.get_duration().total_seconds(), 0)
                    site['update_in_progress'] = job.is_running()
                    db.sites.update_one({
                        '_id': site['_id'],
                    }, {'$set': {
                        'update_in_progress': site['update_in_progress'],
                        'duration': site['duration'],
                        'last_build': site['last_build'],
                    }
                    }, upsert=False)
            logger.info(f"Finished updating jenkins job for {len(sites)} sites.")
        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(60)

logger.info("Starting jenkins job updater")
t = threading.Thread(target=_get_jenkins_state)
t.daemon = True
t.start()

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