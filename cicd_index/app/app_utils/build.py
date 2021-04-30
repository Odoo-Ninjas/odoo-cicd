import arrow
import docker as Docker
from datetime import datetime
import requests
from functools import partial
import shutil
from pathlib import Path
import subprocess
import os
import sys
import logging
FORMAT = '[%(levelname)s] %(name) -12s %(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logging.getLogger().setLevel(logging.DEBUG)
logger = logging.getLogger('')  # root handler
