from .. import MAIN_FOLDER_NAME
import time
import traceback
import subprocess
from functools import partial
import threading
import tempfile
import humanize
from flask import Flask, request, send_from_directory
import os
import base64
import arrow
from .tools import _get_host_path
from .tools import _delete_sourcecode, get_output
from .tools import _get_db_conn
from pathlib import Path
from flask import redirect
from flask import request
from flask import jsonify
from .. import app
from .. import login_required
from flask import render_template
from flask import make_response
from .tools import _format_dates_in_records
from .tools import _get_resources
from .. import db
from .tools import _drop_db
from .tools import _validate_input
from .tools import _get_all_databases
from .tools import _get_docker_state
from .tools import _delete_dockercontainers
from bson import ObjectId
import logging
from datetime import datetime
import docker as Docker
from .tools import get_output
import flask_login
import shutil
logger = logging.getLogger(__name__)

@app.route('/logs')
@login_required
def logs_index():

    return render_template(
        'index_logs.html',
        DATE_FORMAT=os.environ['DATE_FORMAT'].replace("_", "%"),
    )

def _get_dump_files_of_dir(path, relative_to):
    dump_names = sorted([x for x in path.glob("*")])

    def _get_value(filename):
        date = arrow.get((path / filename).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        size = "?"
        if filename.exists():
            size = filename.stat().st_size
            size = humanize.naturalsize(size)
        return f"{filename.relative_to(relative_to)} [{date}] {size}"

    def _get_name(filepath):
        if not relative_to:
            return filepath
        res = Path(filepath).relative_to(relative_to)
        return res

    dump_names = [{'id': str(_get_name(x)), 'value': _get_value(x)} for x in dump_names]
    return dump_names
