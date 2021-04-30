import logging
from datetime import datetime
import time
import git
import threading
from .. import db
from git import Repo
import subprocess
from pathlib import Path
import os
import shutil
logger = logging.getLogger(__name__)

URL = os.environ['REPO_URL']
WORKSPACE = Path("/cicd_workspace")
MAIN_FOLDER_NAME = '_main'

path = WORKSPACE / MAIN_FOLDER_NAME

def clone(path):
    git.Repo.clone_from(
        URL, 
        path, 
        env={
            "GIT_SSH_COMMAND": "ssh -o StrictHostKeyChecking=no"
        }
    )

if not path.exists():
    clone(path)
try:
    repo = Repo(path)
except git.exc.InvalidGitRepositoryError:
    shutil.rmtree(path)
    clone(path)
    repo = Repo(path)


def _get_git_state():
    while True:
        try:
            new_branches = []
            for remote in repo.remotes:
                fetch_info = remote.fetch()
                for fi in fetch_info:
                    name = fi.ref.name.split("/")[-1]
                    try:
                        repo.refs[name]
                    except IndexError:
                        new_branches.append(name)
                    else:
                        if repo.refs[name].commit != fi.commit:
                            new_branches.append(name)

            logger.info(f"New Branches detected: {new_branches}")
            for branch in new_branches:
                repo.git.checkout(branch)
                repo.git.pull()
                commit = repo.refs[branch].commit
                instance_folder = WORKSPACE / branch
                instance_folder.mkdir(exist_ok=True)
                logger.info(f"Copying source code to {instance_folder}")
                subprocess.run(["rsync", str(path) + "/", str(instance_folder) + "/", "-ar", "--exclude=.git"])
            
                db.sites.update_one({
                    'name': branch,
                }, {'$set': {
                    'name': branch,
                    'needs_build': True,
                    'git_sha': str(commit),
                    'git_author': commit.author.name,
                    'git_desc': commit.message,
                    'date_registered': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
                }
                }, upsert=True)

        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(5)


logger.info("Starting job to fetch source code")
t = threading.Thread(target=_get_git_state)
t.daemon = True
t.start()