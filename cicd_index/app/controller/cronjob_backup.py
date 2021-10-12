import logging
import traceback
import arrow
from copy import deepcopy
from datetime import datetime
import time
import git
import threading
from . import WORKSPACE
from . import URL
from .. import db
import subprocess
from pathlib import Path
from .tools import _get_main_repo
import os
import shutil
from . import BUILDING_LOCK
from .tools import _odoo_framework
from .logsio_writer import LogsIOWriter


def _get_dump_name(site_name):
    return f"{site_name}.dump"

def _do_backups():
    while True:
        try:
            sites = list(db.sites.find({'do_backup_regularly': True}))
            for site in sites:
                logger = LogsIOWriter(
                    site['name'],
                    'backup',
                )
                if site.get('archive'):
                    continue
                # check for existing dumpname
                dump_name = _get_dump_name(site['name'])
                dump_path = Path(os.environ['DUMPS_PATH_MAPPED']) / dump_name
                if dump_path.exists():
                    age = (arrow.get() - arrow.get(dump_path.stat().st_mtime)).total_seconds() / 3600
                else:
                    age = 1000

                if age > 1:
                    logger.info(f"Starting backup of {site['name']}")
                    try:
                        _odoo_framework(
                            site,
                            ['backup', 'odoo-db', str(Path(os.environ['DUMPS_PATH']) / dump_name)],
                            log_writer=logger
                            )
                    except Exception as ex:
                        msg = traceback.format_exc()
                        logger.error(msg)

        except Exception as ex:
            msg = traceback.format_exc()
            logger.error(msg)

        finally:
            time.sleep(5)



def start():
    t = threading.Thread(target=_do_backups)
    t.daemon = True
    t.start()
