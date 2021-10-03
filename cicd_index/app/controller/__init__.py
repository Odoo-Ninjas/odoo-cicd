from threading import Lock, RLock
from pathlib import Path
import os
from . import models
from . import tools

URL = os.environ['REPO_URL']
WORKSPACE = Path("/cicd_workspace")



GIT_LOCK = RLock()
BUILDING_LOCK = RLock()