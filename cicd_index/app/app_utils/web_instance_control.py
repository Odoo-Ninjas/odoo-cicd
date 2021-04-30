from .. import app
from pathlib import Path
import docker as Docker
from .tools import _odoo_framework
from flask import jsonify
from .. import db
from flask import request
import logging
from .tools import _get_docker_state
logger = logging.getLogger(__name__)


docker = Docker.from_env()


@app.route("/instance/start")
def start_instance(name=None):
    name = name or request.args['name']
    _odoo_framework(name, ['up', '-d'])
    return jsonify({
        'result': 'ok',
    })

@app.route("/instance/stop")
def stop_instance(name=None):
    name = name or request.args['name']
    _odoo_framework(name, ['kill'])
    return jsonify({
        'result': 'ok'
    })

@app.route("/instance/status")
def instance_state():
    name = request.args['name']
    return jsonify({
        'state': 'running' if _get_docker_state(name) else 'stopped'
    })


@app.route("/restart_docker")
def restart_docker():
    site_name = request.args.get('name')
    if site_name == "all":
        site_name = None
    _restart_docker(site_name, kill_before=True)
    return jsonify({
        'result': 'ok',
    })


def _restart_docker(site_name, kill_before=True):
    if site_name:
        sites = [site_name]
    else:
        sites = [x['name'] for x in db.sites.find({})]
    del site_name

    logger.info(f"Restarting {sites}")
    for site_name in sites:

        if kill_before:
            _odoo_framework(site_name, ['kill'])
        
        _odoo_framework(site_name, ["up", "-d"])
        logger.info(f"Started via ssh call to odoo object: {site_name}")

