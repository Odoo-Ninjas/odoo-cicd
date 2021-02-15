import shutil
import os
import time
from bson import ObjectId
from flask import redirect
from operator import itemgetter
import docker as Docker
import arrow
import subprocess
from flask import jsonify
from flask import make_response
from flask import Flask
from flask import render_template
from flask import url_for
from datetime import datetime
from flask import request
from bson import ObjectId
from collections import defaultdict
import pymongo
import json
from pathlib import Path
from bson.json_util import dumps
import threading
import logging
import jenkins
import urllib


from pymongo import MongoClient
mongoclient = MongoClient(
    os.environ["MONGO_HOST"],
    int(os.environ['MONGO_PORT']),
    username=os.environ['MONGO_USERNAME'],
    password=os.environ['MONGO_PASSWORD'],
    connectTimeoutMS=20000, socketTimeoutMS=20000, serverSelectionTimeoutMS=20000,
)
db = mongoclient.get_database('cicd_sites')

FORMAT = '[%(levelname)s] %(name) -12s %(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logging.getLogger().setLevel(logging.INFO)
logger = logging.getLogger('')  # root handler

BOOL_VALUES = ['1', 1, 'true', 'True', 'y']
app = Flask(
    __name__,
    static_folder='/_static_index_files',
)

docker = Docker.from_env()

# jenkins = jenkins.Jenkins('http://192.168.101.122:8080', username='admin', password='1')
jenkins = jenkins.Jenkins(os.environ["JENKINS_URL"], username=os.environ["JENKINS_USER"], password=os.environ["JENKINS_PASSWORD"])
print(f"Jenkins {jenkins.get_whoami()} and version {jenkins.get_version()}")

def cycle_down_apps():
    while True:
        try:
            sites = db.sites.find({'enabled': True}, {'name': 1, 'last_access': 1})
            for site in sites:
                logger.debug(f"Checking site to cycle down: {site['name']}")
                if (arrow.get() - arrow.get(site.get('last_access', '1980-04-04') or '1980-04-04')).total_seconds() > 5 * 3600: # TODO configurable
                    if _get_docker_state(site['name']) == 'running':
                        logger.info(f"Cycling down instance due to inactivity: {site['name']}")
                        _stop_instance(site['name'])

        except Exception as e:
            logging.error(e)
        time.sleep(10)


t = threading.Thread(target=cycle_down_apps)
t.daemon = True
t.start()


class JSONEncoder(json.JSONEncoder):
    # for encoding ObjectId
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)

        return super(JSONEncoder, self).default(o)


app.json_encoder = JSONEncoder

@app.route("/last_access")
def last_access():
    if not request.args.get('site'):
        raise Exception('site missing')
    site = db.sites.find_one({'name': request.args.get('site')})
    if site:
        db.sites.update_one({
            '_id': site['_id'],
        }, {'$set': {
            'last_access': datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        }, upsert=False)
    return jsonify({'result': 'ok'})

@app.route("/sites")
def show_sites():
    return jsonify(list(db.sites.find()))

@app.route("/next_instance")
def next_instance_name():
    branch = request.args.get('branch')
    key = request.args.get('key')
    assert branch
    assert key
    sites = list(db.sites.find({
        'git_branch': branch,
        'key': key
    }))
    sites = sorted(sites, key=lambda x: x['index'])
    index = max(list(filter(bool, [x.get('index') for x in sites])) + [0])

    info = {
        'commit_before': '',
    }
    if index:
        site = [x for x in sites if x['index'] == index]
        info['commit_before'] = site[0]['git_sha']
    info['index'] = 1 if 'kept' else index + 1
    info['name'] = f"{branch}_{key}_{str(info['index']).zfill(3)}"
    return jsonify(info)

@app.route('/register', methods=['POST'])
def register_site():
    if request.method == 'POST':
        site = dict(request.json)
        sites = list(db.sites.find({
            "git_branch": site['git_branch'],
            "key": site['key'],
            "index": site['index'],
        }))
        result = {'result': 'ok'}

        branches = db.branches.find({
            "git_branch": site['git_branch'],
        })
        if not branches:
            db.branches.insert({
                'git_branch': site['git_branch'],
            })
        else:
            for key in ['description', 'author']:
                update = {}
                if site.get(key):
                    update[key] = site[key]
                if update:
                    db.branches.update_one({'_id': branches[0]['_id']}, {'$set': update}, upsert=False)

        if not sites:
            site['enabled'] = False
            db.sites.insert_one(site)
            result['existing'] = True

        return jsonify(result)

    raise Exception("only POST")

@app.route("/site", methods=["GET"])
def site():
    q = {}
    for key in [
        'index', 'key', 'branch', 'name',
    ]:
        if request.args.get(key):
            q[key] = request.args.get(key)
    site = db.sites.find(q)
    return jsonify(site)

@app.route("/instance/start")
def start_instance(name=None):
    name = name or request.args['name']
    containers = docker.containers.list(all=True, filters={'name': [name]})
    for container in containers:
        container.start()
    return jsonify({
        'result': 'ok',
    })

def _stop_instance(name):
    containers = docker.containers.list(all=False, filters={'name': [name]})
    for container in containers:
        container.stop()

@app.route("/instance/stop")
def stop_instance(name=None):
    name = name or request.args['name']
    _stop_instance(name)
    return jsonify({
        'result': 'ok'
    })

@app.route("/instance/status")
def instance_state():
    name = request.args['name']
    return jsonify({
        'state': 'running' if _get_docker_state(name) else 'stopped'
    })

@app.route("/set_updating")
def set_updating():
    name = request.args['name']

    site = db.sites.find_one({'name': name})
    if not site:
        raise Exception(f"site not found: {name}")
    db.sites.update_one({'_id': site['_id']}, {'$set': {
        'updating': request.args['value'] in BOOL_VALUES,
    }}, upsert=False)
    return jsonify({
        'result': 'ok',
    })

@app.route("/notify_instance_updated")
def notify_instance_updated():
    info = {
        'name': request.args['name'],
        'sha': request.args['sha'],
    }
    assert info['name']
    assert info['sha']
    for extra_args in [
        'update_time',
    ]:
        info[extra_args] = request.args.get(extra_args)

    info['date'] = arrow.get().strftime("%Y-%m-%d %H:%M:%S")

    db.updates.insert_one(info)

    site = db.sites.find_one({'name': info['name']})
    if not site:
        raise Exception(f"site not found: {info['name']}")
    db.sites.update_one({'_id': site['_id']}, {'$set': {
        'updated': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
        'enabled': True,
    }}, upsert=False)

    return jsonify({
        'result': 'ok'
    })

@app.route("/last_successful_sha")
def last_success_full_sha():
    info = {
        'name': request.args['name'],
    }
    assert info['name']

    updates = db.updates.find(info)
    # TODO in mongo sorting
    updates = sorted(updates, key=lambda x: x['date'], reverse=True)
    if updates:
        return jsonify({
            'sha': updates[0]['sha']
        })
    return jsonify({
        'sha': '',
    })

@app.route("/instance/destroy")
def destroy_instance():
    info = {
        'name': request.args['name'],
    }
    for container in docker.containers.list(all=True, filters=info):
        container.remove()

    jenkins.build_job('clean_cicd_instance', info)

    db.sites.remove(info)
    db.updates.remove(info)

    return jsonify({
        'result': 'ok',
    })

def _get_docker_state(name):
    docker.ping()
    containers = docker.containers.list(all=True, filters={'name': [name]})
    states = set(map(lambda x: x.status, containers))
    return 'running' in states

def format_date(dt):
    DATE_FORMAT = os.environ['DATE_FORMAT'].replace("_", "%")
    tz = os.environ['DISPLAY_TIMEZONE']
    arrow.get(dt)
    return arrow.get(dt).to(tz).strftime(DATE_FORMAT)

def _format_dates_in_records(records):
    for rec in records:
        for k in rec:
            if not isinstance(rec[k], str):
                continue
            try:
                rec[k] = format_date(arrow.get(rec[k]))
            except arrow.parser.ParserError:
                continue
    return records

@app.route("/possible_dumps")
def possible_dumps():
    dump_names = db.config.find({})[0].get('dumps')
    dump_names = [{'id': x, 'value': x} for x in dump_names]
    return jsonify(dump_names)

@app.route("/data/branches", methods=["GET"])
def data_branches():
    data = request.args.get('request')
    find = {}
    if data:
        find = _validate_input(json.loads(data))

    branches = sorted(_format_dates_in_records(list(db.branches.find(find))), key=lambda x: x['git_branch'])
    return jsonify(branches)

@app.route("/data/instances", methods=["GET", "POST"])
def data_variants():
    if not request.args.get('git_branch'):
        return jsonify({
            "status": "success",
            "total": 0,
            "records": []
        })

    sites = _format_dates_in_records(list(db.sites.find({'enabled': True, 'git_branch': request.args['git_branch']})))
    sites = sorted(sites, key=lambda x: x.get('updated', x.get('last_access', arrow.get('1980-04-04'))), reverse=True)

    for site in sites:
        site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'

    sites_grouped = defaultdict(list)
    for site in sites:
        sites_grouped[site['git_branch']].append(site)
    for site in sites_grouped:
        sites_grouped[site] = sorted(sites_grouped[site], key=lambda x: x['index'], reverse=True)
    for site in sites:
        site['recid'] = site['name']

    return jsonify({
        "status": "success",
        "total": len(sites),
        "records": sites
    })

@app.route('/')
def index_func():

    sites = list(db.sites.find({'enabled': True}))

    for site in sites:
        for k in site:
            if not isinstance(site[k], str):
                continue
            try:
                site[k] = arrow.get(site[k]).to(os.environ['DISPLAY_TIMEZONE'])
            except arrow.parser.ParserError:
                continue
    sites = sorted(sites, key=lambda x: x.get('updated', x.get('last_access', arrow.get('1980-04-04'))), reverse=True)
    for site in sites:
        site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'

    sites_grouped = defaultdict(list)
    for site in sites:
        sites_grouped[site['git_branch']].append(site)
    for site in sites_grouped:
        sites_grouped[site] = sorted(sites_grouped[site], key=lambda x: x['index'], reverse=True)

    return render_template(
        'index.html',
        sites=sites_grouped,
        DATE_FORMAT=os.environ['DATE_FORMAT'].replace("_", "%"),
        message=request.args.get('message'),
    )

def _validate_input(data, int_fields=[]):
    data = dict(data)
    for int_field in int_fields:
        if int_field in data:
            try:
                data[int_field] = int(data[int_field].strip())
            except ValueError as ex:
                print(ex)
                data.pop(int_field)
    for k, v in list(data.items()):
        if v == 'true':
            data[k] = True
        elif v == 'false':
            data[k] = False

    if '_id' in data and isinstance(data, str):
        data['_id'] = ObjectId(data['_id'])
    return data

@app.route('/update/branch', methods=["POST"])
def update_branch():
    data = _validate_input(request.form, int_fields=['limit_instances'])
    id = ObjectId(data.pop('_id'))
    db.branches.update_one(
        {'_id': id},
        {'$set': data},
        upsert=False
    )
    return jsonify({'result': 'ok'})

@app.route('/start')
def start_cicd():
    return _start_cicd()

def _start_cicd():
    # name = request.cookies['delegator-path']
    name = request.args['name']
    if not _get_docker_state(name):
        start_instance(name=name)
        for i in range(30):
            if _get_docker_state(name):
                break
            time.sleep(1)
        else:
            url = request.url.split(request.host)[0] + request.host # TODO messy
            return redirect(url + "/index?" + urllib.parse.urlencode({
                "message": f"Please try again. Instance {name} not started within timeout.",
            }, quote_via=urllib.parse.quote_plus))

    response = make_response(render_template('start_cicd.html'))
    response.set_cookie('delegator-path', name)
    return response
