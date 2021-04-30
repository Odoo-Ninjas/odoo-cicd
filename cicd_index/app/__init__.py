#TODO clean source not only in workspace
import os
import subprocess
from flask import Flask
from bson.json_util import dumps
import flask_login
import logging
from flask_login import login_required
from pymongo import MongoClient

login_manager = flask_login.LoginManager()


logging.getLogger("requests").setLevel(logging.WARNING)
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
                     LOGGING SETUP                                     
"""
host_ip = '.'.join(subprocess.check_output(["/usr/bin/hostname", "-I"]).decode('utf-8').strip().split(".")[:3]) + '.1'

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
app = Flask(
    __name__,
    static_folder='/_static_index_files',
)
app.secret_key = 'asajdkasj24242184*$@'
from .app_utils.tools import JSONEncoder
app.json_encoder = JSONEncoder
login_manager.init_app(app)
from .app_utils import auth
from .app_utils import web_application
from .app_utils import web_instance_control
from .app_utils.tools import JSONEncoder
from . import app_utils