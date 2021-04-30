import logging
import time
import threading
from .. import db
from .tools import _odoo_framework
logger = logging.getLogger(__name__)



def _build():
    while True:
        try:

            sites = list(db.sites.find({'needs_build': True}))

        except Exception as ex:
            logger.error(ex)

        finally:
            time.sleep(5)


logger.info("Starting job to build instances")
t = threading.Thread(target=_get_git_state)
t.daemon = True
t.start()