#TODO clean source not only in workspace
import time
import os
import subprocess
from flask import Flask
from flask_caching import Cache
from bson.json_util import dumps
import flask_login
import logging
from flask_login import login_required
from pymongo import MongoClient
from pathlib import Path



login_manager = flask_login.LoginManager()


logging.getLogger("requests").setLevel(logging.WARNING)
logging.getLogger("paramiko").setLevel(logging.WARNING)
logging.getLogger("paramiko.transport").setLevel(logging.WARNING)
logging.getLogger("werkzeug").setLevel(logging.WARNING)

"""
                     MONGO CONNECTION                                  
"""

mongoclient = MongoClient(
    os.environ["MONGO_HOST"],
    int(os.environ['MONGO_PORT']),
    username=os.environ['MONGO_USERNAME'],
    password=os.environ['MONGO_PASSWORD'],
    connectTimeoutMS=20000, socketTimeoutMS=20000, serverSelectionTimeoutMS=20000,
)
db = mongoclient.get_database('cicd_sites')
"""
                     HOST IP
"""
host_ip = '.'.join(subprocess.check_output(["/usr/bin/hostname", "-I"]).decode('utf-8').strip().split(".")[:3]) + '.1'

"""
                     CONSTANTS
"""

MAIN_FOLDER_NAME = '_main'

"""
                     LOGGING SETUP                                     
"""
FORMAT = '[%(levelname)s] %(name) -12s %(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger('')  # root handler
logger.info(f"Host IP: {host_ip}")

"""
                     APP SETUP                                         
"""
from .controller import cronjob_builder
from .controller import cronjob_usage
from .controller import cronjob_docker
from .controller import cronjob_fetch_git
from .controller import cronjob_backup

if os.getenv("CICD_CRONJOBS") == "1":

    cronjob_builder.start()
    cronjob_docker.start()
    cronjob_fetch_git.start()
    cronjob_usage.start()
    cronjob_backup.start()

    while True:
        time.sleep(1000)

app = None
cache = None

def create_app():
    global app
    global cache
    app = Flask(
        __name__,
        static_url_path='/static', 
        static_folder='templates/static'
    )
    app.config.from_mapping({
        # "DEBUG": True, 
        "CACHE_TYPE": "SimpleCache",
        "CACHE_DEFAULT_TIMEOUT": 300
    })
    cache = Cache(app)
    app.secret_key = 'asajdkasj24242184*$@'
    from .controller.tools import JSONEncoder
    app.json_encoder = JSONEncoder
    login_manager.init_app(app)
    from .controller import auth
    from .controller import web_application
    from .controller import logs
    from .controller import web_user_admin
    from .controller import web_instance_control
    from .controller import web_app_settings
    from .controller.tools import JSONEncoder
    from . import controller
    return app