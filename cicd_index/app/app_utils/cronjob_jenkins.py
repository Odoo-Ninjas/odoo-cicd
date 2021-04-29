# import logging
# import time
# import threading
# logger = logging.getLogger(__name__)

# def _get_jenkins_state():
#     while True:
#         try:
#             logger.info("Getting job state from jenkins")
#             sites = list(db.sites.find({}))
#             for site in sites:
#                 try:
#                     job = _get_jenkins_job(site['git_branch'])
#                 except Exception as ex:
#                     site['last_build'] = f"Error: {ex}"
#                 else:
#                     if not job:
#                         continue
#                     last_build = job.get_last_build_or_none()
#                     if last_build:
#                         site['last_build'] = last_build.get_status()
#                         site['duration'] = round(last_build.get_duration().total_seconds(), 0)
#                     site['update_in_progress'] = job.is_running()
#                     db.sites.update_one({
#                         '_id': site['_id'],
#                     }, {'$set': {
#                         'update_in_progress': site['update_in_progress'],
#                         'duration': site['duration'],
#                         'last_build': site['last_build'],
#                     }
#                     }, upsert=False)
#             logger.info(f"Finished updating jenkins job for {len(sites)} sites.")
#         except Exception as ex:
#             logger.error(ex)

#         finally:
#             time.sleep(60)

# logger.info("Starting jenkins job updater")
# t = threading.Thread(target=_get_jenkins_state)
# t.daemon = True
# t.start()

# def _get_docker_state():
#     while True:
#         try:
#             logger.info("Getting docker state from jenkins")
#             sites = list(db.sites.find({}))
#             for site in sites:
#                 site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'
#                 db.sites.update_one({
#                     '_id': site['_id'],
#                 }, {'$set': {
#                     'docker_state': site['docker_state'],
#                 }
#                 }, upsert=False)
#             logger.info(f"Finished updating docker job for {len(sites)} sites.")
#         except Exception as ex:
#             logger.error(ex)

#         finally:
#             time.sleep(10)

# logger.info("Starting docker state updater")
# t = threading.Thread(target=_get_docker_state)
# t.daemon = True
# t.start()

# def _get_jenkins(crumb=True):
#     # res = jenkins.Jenkins('http://192.168.101.122:8080', username='admin', password='1')
#     from jenkinsapi.utils.crumb_requester import CrumbRequester
#     from jenkinsapi.jenkins import Jenkins
#     crumb_requester = CrumbRequester(
#         username=os.environ['JENKINS_USER'],
#         password=os.environ["JENKINS_PASSWORD"],
#         baseurl=os.environ["JENKINS_URL"],
#     )

#     res = Jenkins(
#         os.environ["JENKINS_URL"],
#         username=os.environ["JENKINS_USER"],
#         password=os.environ["JENKINS_PASSWORD"],
#         requester=crumb_requester if crumb else None # https://stackoverflow.com/questions/45199374/remotely-build-specific-jenkins-branch/45200202
#     )
#     # print(f"Jenkins {res.get_whoami()} and version {res.get_version()}")
#     return res

# def _get_jenkins_job(branch):
#     logger.debug(f"Fetching jenkins job {branch}")
#     jenkins = _get_jenkins()
#     job = jenkins[f"{os.environ['JENKINS_JOB_MULTIBRANCH']}/{branch}"]
#     return job
