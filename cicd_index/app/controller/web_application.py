from .. import MAIN_FOLDER_NAME
import time
import traceback
import subprocess
from functools import partial
from .tools import _get_src_path
from .tools import get_logs_url
import threading
import tempfile
import humanize
from flask import Flask, request, send_from_directory
import os
import base64
import arrow
from .tools import _get_host_path, _get_main_repo
from .tools import PREFIX_PREPARE_DUMP
from .tools import _delete_sourcecode, get_output
from .tools import _get_db_conn
from pathlib import Path
from flask import redirect
from flask import request
from flask import jsonify
from .. import app
from .. import cache
from .. import login_required
from flask import render_template
from flask import make_response
from .tools import _format_dates_in_records
from .tools import _get_resources
from .. import db
from .tools import _odoo_framework
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
from .tools import _get_repo
from .logsio_writer import LogsIOWriter
import flask_login
import shutil
logger = logging.getLogger(__name__)

docker = Docker.from_env()

@app.route('/')
@login_required
def index_func():

    return render_template(
        'index.html',
        HEIGHT_RESOURCES=len(os.environ['DISPLAY_RESOURCES'].split(";")),
        DATE_FORMAT=os.environ['DATE_FORMAT'].replace("_", "%"),
    )

def _get_dump_files_of_dir(path, relative_to):
    dump_names = sorted([x for x in path.glob("*")], key=lambda x: x.stat().st_mtime, reverse=True)

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

@app.route("/branches")
def get_branches():
    repo = _get_main_repo()
    branches = list(map(str, [x.name.split("/")[-1] for x in repo.remote().refs]))
    branches = list(filter(lambda x: x != 'HEAD', branches))
    return jsonify(branches)

@app.route("/possible_dumps")
def possible_dumps():
    path = Path(os.environ['DUMPS_PATH_MAPPED'])
    dump_names = _get_dump_files_of_dir(path, path)
    return jsonify(dump_names)

@app.route("/possible_input_dumps")
def possible_input_dumps():
    path = Path(os.environ['INPUT_DUMPS_PATH_MAPPED'])
    dump_names = []
    for subdir in path.glob("*"):
        if subdir.is_dir():
            dump_names += _get_dump_files_of_dir(subdir, relative_to=path)
    return jsonify(dump_names)

@app.route("/transform_input_dump")
def transform_input_dump():
    dump = Path(request.args['dump'])
    erase = request.args['erase'] == '1'
    anonymize = request.args['anonymize'] == '1'
    site = 'master'
    logger = LogsIOWriter("input_dump", f"{site}_{arrow.get().strftime('%Y-%m-%d_%H%M%S')}")

    def do():
        instance_folder = Path("/cicd_workspace") / f"{PREFIX_PREPARE_DUMP}{Path(tempfile.mktemp()).name}"
        try:
            # reverse lookup the path
            real_path = _get_host_path(Path("/input_dumps") / dump.parent) / dump.name

            def of(*args):
                _odoo_framework(
                    instance_folder.name,
                    list(args),
                    log_writer=logger,
                    instance_folder=instance_folder
                    )

            logger.info(f"Preparing Input Dump: {dump.name}")
            logger.info("Preparing instance folder")
            source = str(Path("/cicd_workspace") / "master") + "/"
            dest = str(instance_folder) + "/"
            branch = 'master'
            logger.info(f"checking out {branch} to {dest}")

            repo = _get_main_repo(destination_folder=dest)
            repo.git.checkout('master', force=True)
            repo.git.pull()

            custom_settings = """
RUN_POSTGRES=1
DB_PORT=5432
DB_HOST=postgres
DB_USER=odoo
DB_PWD=odoo
            """
            of("reload", '--additional_config', base64.encodestring(custom_settings.encode('utf-8')).strip().decode('utf-8'))
            of("down", "-v")

            # to avoid orphan messages, that return error codes although warning
            logger.info(f"Starting local postgres")
            of("up", "-d", 'postgres')

            of("restore", "odoo-db", str(real_path))
            suffix =''
            if erase:
                of("cleardb")
                suffix += '.cleared'
            if anonymize:
                of("anonymize")
                suffix += '.anonym'
            of("backup", "odoo-db", str(Path(os.environ['DUMPS_PATH']) / (dump.name + suffix + '.cicd_ready')))
            of("down", "-v")
        except Exception as ex:
            msg = traceback.format_exc()
            logger.info(msg)
        finally:
            if instance_folder.exists(): 
                shutil.rmtree(instance_folder)

    t = threading.Thread(target=do)
    t.start()

    

    return jsonify({
        'live_url': get_logs_url([rolling_file.source]),
    })

@app.route("/turn_into_dev")
def _turn_into_dev():
    if not request.args.get('site'):
        raise Exception('site missing')
    site = db.sites.find_one({'name': request.args.get('site')})
    if site:
        site = site['name']
        logger = LogsIOWriter(site, 'misc')
        _reload_instance(site, logs_writer=logger)
        _odoo_framework(site, ["turn-into-dev"], logs_writer=logger)
    return jsonify({'result': 'ok'})

def _reload_instance(site, logs_writer):
    _odoo_framework(site, ["reload", "-d", site], logs_writer=logs_writer)

    
@app.route('/trigger/rebuild')
def trigger_rebuild():
    site = db.sites.find_one({'name': request.args['name']})
    db.updates.remove({'name': site['name']})
    data = {
        'needs_build': True,
        'archive': False,
        'build_mode': 'reset',
        'docker_no_cache': request.args.get('no_cache') == '1',
        'no_module_update': request.args.get('no_module_update') == '1',
    }
    if request.args.get('dump'):
        data['dump'] = request.args['dump']

    db.sites.update_one({'name': request.args.get('name')}, {'$set': data}, upsert=False)
    return jsonify({
        'result': 'ok',
    })

@app.route("/data/site/live_values")
def site_jenkins():
    sites = list(db.sites.find({}, {
        'name': 1, 'is_building': 1, 'duration': 1,
        'docker_state': 1, 'build_state': 1, 'success': 1,
        'needs_build': 1, 'db_size_humanize': 1, 'source_size_humanize': 1
        }))
    for site in sites:
        site['id'] = site['_id']
        site['build_state'] = _get_build_state(site)
    return jsonify({
        'sites': sites,
    })

def get_git_commits(path, count=30):
    return subprocess.check_output([
        "/usr/bin/git",
        "log",
        "-n", str(count),
    ], cwd=path).strip().decode('utf-8')
    
def _load_detail_data(site_dict, count_history=10):
    path = _get_src_path(site_dict['name'])

    if not path.exists():
        git_desc = ['no source found']
    else:
        git_desc = get_git_commits(path)
    site_dict['git_desc'] = git_desc

@app.route("/data/sites", methods=["GET", "POST"])
def data_variants():
    _filter = {}

    user = flask_login.current_user
        
    if request.args.get('git_branch', None):
        _filter['git_branch'] = request.args['git_branch']
    if request.args.get('name', None):
        _filter['name'] = request.args['name']

    sites = list(db.sites.find(_filter))

    sites = _format_dates_in_records(sites)
    sites = sorted(sites, key=lambda x: x.get('name'))
    if not request.args.get('name'):
        if request.args.get('archive') == '1':
            sites = [x for x in sites if x.get('archive')]
        else:
            sites = [x for x in sites if not x.get('archive')]

    for site in sites:
        site['id'] = site['_id']
        site['update_in_progress'] = False
        site['repo_url'] = f"{os.environ['REPO_URL']}/-/commit/{site.get('git_sha')}"
        site['build_state'] = _get_build_state(site)
        site['duration'] = site.get('duration', 0)
        site['last_access'] = arrow.get(site['last_access']).to(os.environ['DISPLAY_TIMEZONE']).strftime("%Y-%m-%d %H:%M:%S")

    if user.is_authenticated and not user.is_admin:
        user_db = db.users.find_one({'login': user.id})
        sites = [x for x in sites if x['name'] in user_db.get('sites')]

    sites = list(sorted(sites, key=lambda x: (1 if x.get('archive') else 0, x.get('name').lower())))

    if len(sites) == 1:
        _load_detail_data(sites[0])

    return jsonify(sites)

def _get_build_state(site):
    if site.get('is_building'):
        return "Building...."
    if site.get('needs_build'):
        return 'Scheduled'
    if 'success' in site:
        return 'SUCCESS' if site['success'] else 'FAILED'
    return 'idle'

@app.route('/update/site', methods=["GET", "POST"])
def update_site():
    if request.method == 'POST':
        data = request.form
    else:
        data = request.args
    data = _validate_input(data, int_fields=['archive'])
    if 'archive' in data and not data['archive']:
        # dont build immediatley:
        data['needs_build'] = False
        data['is_building'] = False
        data['success'] = False

    if '_id' not in data and 'git_branch' in data:
        branch_name = data.pop('git_branch')
        site = db.sites.find_one({'git_branch': branch_name})
        if not site:
            return jsonify({'result': 'not_found', 'msg': "Site not found"})
        id = site['_id']
    else:
        id = ObjectId(data.pop('_id'))
    db.sites.update_one(
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
    from .web_instance_control import _restart_docker
    name = request.args['name']
    docker_state = _get_docker_state(name)
    logger.info(f"Opening user interface of cicd instance {name}; current docker state: {docker_state}")
    if not docker_state:
        _restart_docker(name, kill_before=False)

    response = make_response(
        render_template(
            'start_cicd.html',
            initial_path=request.args.get('initial_path') or '/web'
        ),
    )
    response.set_cookie('delegator-path', name)
    response.set_cookie('frontend_lang', '', expires=0)
    response.set_cookie('im_livechat_history', '', expires=0)
    response.set_cookie('session_id', "", expires=0)
    return response

def get_setting(key, default=None):
    config = db.config.find_one({'key': key})
    if not config:
        return default
    return config['value']


def store_setting(key, value):
    db.sites.update_one({
        'key': key,
    }, {'$set': {
        'value': value,
    }
    }, upsert=True)

def _get_shell_url(command):
    pwd = base64.encodestring('odoo'.encode('utf-8')).decode('utf-8')
    shellurl = f"/console/?encoding=utf-8&term=xterm-256color&hostname=127.0.0.1&username=root&password={pwd}&command="
    shellurl += ' '.join(command)
    return shellurl

@app.route("/show_logs")
def show_logs():
    name = request.args.get('name')
    name += '_' + request.args.get('service')
    containers = docker.containers.list(all=True, filters={'name': [name]})
    containers = [x for x in containers if x.name == name]
    shell_url = _get_shell_url(["docker", "logs", "-f", containers[0].id])
    return redirect(shell_url)

@app.route("/pgcli")
def pgcli():
    site_name = request.args.get('name')

    shell_url = _get_shell_url([
        "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/{site_name}", ";",
        "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "pgcli",
        "--host", os.environ['DB_HOST'],
        "--user", os.environ['DB_USER'],
        "--password", os.environ['DB_PASSWORD'],
        "--port", os.environ['DB_PORT'],
    ])
    return redirect(shell_url)

@app.route("/debug_instance")
def debug_instance():
    site_name = request.args.get('name')
    logger = LogsIOWriter(site_name, 'misc')

    _odoo_framework(site_name, ['kill', 'odoo'], logs_writer=logger)
    _odoo_framework(site_name, ['kill', 'odoo_debug'], logs_writer=logger)

    shell_url = _get_shell_url([
        "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/{site_name}", ";",
        "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "debug", "odoo", "--command", "/odoolib/debug.py",
    ])
    # TODO make safe; no harm on system, probably with ssh authorized_keys

    return redirect(shell_url)
    
@app.route("/get_resources")
@cache.cached(timeout=120)
def get_free_resources():
    return render_template(
        'resources.html',
        resources=_get_resources(),
    )

@app.route("/cleanup")
def cleanup():
    """
    Removes all unused source directories, databases
    and does a docker system prune.
    """
    conn = _get_db_conn()
    try:
        cr = conn.cursor()

        dbnames = _get_all_databases(cr)

        sites = set([x['name'] for x in db.sites.find({}) if not x.get('archive')])
        for dbname in dbnames:
            if dbname.startswith('template') or dbname == 'postgres':
                continue

            # critical: reverse dbname to instance name
            def match(site, dbname):
                ignored_chars = "-!@#$%^&*()_-+=][{}';:,.<>/"
                site = site.lower()
                dbname = dbname.lower()
                for c in ignored_chars:
                    site = site.replace(c, '')
                    dbname = dbname.replace(c, '')
                return dbname == site

            if not [x for x in sites if match(x, dbname)]:
                _drop_db(cr, dbname)


        # Drop also old sourcecodes
        for dir in Path("/cicd_workspace").glob("*"):
            if dir.name == MAIN_FOLDER_NAME:
                continue
            instance_name = dir.name
            if instance_name not in sites:
                _delete_sourcecode(instance_name)

        # remove artefacts from ~/.odoo/
        os.system("docker system prune -f -a")

        # drop old docker containers
        cicd_prefix = os.environ['CICD_PREFIX']
        containers = docker.containers.list(all=True, filters={'label': f'ODOO_CICD={cicd_prefix}'})
        for container in containers:
            site_name = container.labels.get('ODOO_CICD_INSTANCE_NAME', '')
            if not site_name: continue
            if site_name not in sites:
                if container.status == 'running':
                    container.kill()
                container.remove(force=True)

        # drop transfer rests:
        for folder in Path("/cicd_workspace").glob("*"):
            if str(folder).startswith(PREFIX_PREPARE_DUMP):
                shutil.rmtree(folder)

    finally:
        cr.close()
        conn.close()

    return jsonify({'result': 'ok'})

    
    
    
@app.route("/delete")
def delete_instance():
    name = request.args.get('name')
    if not name:
        raise Exception("Name is missing!")
    site = db.sites.find_one({'name': name})

    _delete_sourcecode(name)

    _delete_dockercontainers(name)

    conn = _get_db_conn()
    try:
        cr = conn.cursor()
        _drop_db(cr, name)
    finally:
        cr.close()
        conn.close()
        
    db.sites.update_one(
        {'_id': site['_id']},
        {"$set": {'archive': True}}
        )
    db.updates.remove({'name': name})

    return jsonify({
        'result': 'ok',
    })

@app.route("/show_mails")
def show_mails():
    name = request.args.get('name')
    name += '_odoo'

    shell_url = _get_shell_url(["docker", "logs", "-f", name])
    return redirect(shell_url)

@app.route("/build_log")
def build_log():
    name = request.args.get('name')
    site = db.sites.find_one({'name': name})
    if site.get('is_building'):
        return redirect("/cicd/live_log?name=" + site['name'])
    else:
        output = []
        for heading in [
            ("Meta", 'meta'),
            ("Reload", 'reload'),
            ("Build", 'build'),
            ("Last Update", 'update'),
            ("Last Error", 'last_error'),
        ]:
            output.append(f"<h1>{heading[0]}</h1>")
            output.append(get_output(site['name'], heading[1]))

        return render_template(
            'log_view.html',
            name=site['name'],
            site=site,
            build_status="SUCCESS" if site.get('success') else "FAILURE",
            duration=site.get('duration', 0),
            output="<hr/>".join(output),
        )

@app.route("/dump")
def backup_db():
    site = db.sites.find_one({'name': request.args.get('name')})
    dump_name = request.args.get('dumpname')
    logger = LogsIOWriter(site['name'], 'misc')
    _odoo_framework(site, ['backup', 'odoo-db', dump_name], logs_writer=logger)
    return jsonify({
        'result': 'ok',
    })

@app.route("/build_again")
def build_again():
    if request.args.get('all') == '1':
        mode = 'update-all-modules'
    else:
        mode = 'update-recent'
    db.sites.update_one({'name': request.args.get('name')}, {'$set': {
        'build_mode': mode,
        'needs_build': True,
    }}, upsert=False)
    return jsonify({
        'result': 'ok',
    })


@app.route("/shell_instance")
def shell_instance():
    name = request.args.get('name')
    site_name = name
    name += '_odoo'
    # kill existing container and start odoo with debug command
    containers = docker.containers.list(all=True, filters={'name': [name]})
    containers = [x for x in containers if x.name == name]
    shell_url = _get_shell_url([
        "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/{site_name}", ";",
        "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "debug", "odoo_debug", "--command", "/odoolib/shell.py",
    ])
    # TODO make safe; no harm on system, probably with ssh authorized_keys

    return redirect(shell_url)


@app.route("/site", methods=["GET"])
def site():
    q = {}
    for key in [
        'branch', 'name',
    ]:
        if request.args.get(key):
            q[key] = request.args.get(key)
    site = db.sites.find(q)
    return jsonify(site)


@app.route('/start_all')
def start_all_instances():
    from .web_instance_control import _restart_docker
    _restart_docker(None, kill_before=False)
    return jsonify({
        'result': 'ok',
    })

@app.route('/restart_delegator')
def restart_delegator():
    docker_project_name = os.environ['PROJECT_NAME']
    names = []
    names.append(f"{docker_project_name}_cicd_delegator")
    names.append(f"{docker_project_name}_nginx")
    for name in names:
        containers = docker.containers.list(all=True, filters={'name': [name]})
        for container in containers:
            try:
                container.stop()
            except Exception:
                logger.info(f"Container not stoppable - maybe ok: {container.name}")
            container.start()
    return jsonify({
        'result': 'ok',
    })

@app.route('/restart_jobs')
def restart_jobs():
    docker_project_name = os.environ['PROJECT_NAME']
    names = []
    names.append(f"{docker_project_name}_cicd_cronjobs")
    for name in names:
        containers = docker.containers.list(all=True, filters={'name': [name]})
        for container in containers:
            try:
                container.stop()
                container.kill()
            except Exception:
                logger.info(f"Container not stoppable - maybe ok: {container.name}")
            container.start()
    return jsonify({
        'result': 'ok',
    })


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



@app.route("/start_info")
def start_info():
    u = flask_login.current_user
    return jsonify({
        'is_admin': u.is_authenticated and u.is_admin
    })

@app.route("/clear_db")
def clear_db():
    from .web_instance_control import _restart_docker
    site = db.sites.find_one({'name': request.args['name']})
    logger = LogsIOWriter(site['name'], 'misc')
    _odoo_framework(site, ['cleardb'], logs_writer=logger)
    _restart_docker(site['name'], kill_before=False)

    return jsonify({
        'result': 'ok',
    })


@app.route("/clear_webassets")
def clear_webassets():
    from .web_instance_control import _restart_docker
    site = db.sites.find_one({'name': request.args['name']})
    _odoo_framework(site, ['remove-web-assets'])
    docker_state = _get_docker_state(site['name'])
    _restart_docker(site['name'], kill_before=False)

    return jsonify({
        'result': 'ok',
    })

@app.route("/reload_restart")
def reload_restart():
    site = db.sites.find_one({'name': request.args['name']})
    data = {
        'needs_build': True,
        'build_mode': 'reload_restart',
    }
    db.sites.update_one({'name': site['name']}, {"$set": data}, upsert=True)

    return jsonify({
        'result': 'ok',
    })

@app.route("/make_custom_instance")
def make_custom_instance():
    name = request.args['name']
    assert name
    site = db.sites.find_one({'name': name})
    if site:
        raise Exception("site already exists")
    repo = _get_main_repo(tempfolder=True)
    try:
        try:
            was_head = repo.head.ref.name
        except:
            repo.git.checkout('master', '-f')
            was_head = repo.head.ref.name
        try:
            try:
                repo.heads[name]
            except:
                current = repo.create_head(name)
                current.checkout()
                origin = repo.remote(name='origin')
                try:
                    origin.pull()
                except:
                    pass
                repo.git.push("--set-upstream", origin, repo.head.ref)
            data = {
                'name': name,
                'needs_build': True,
                'force_rebuild': True,
            }
        finally:
            repo.heads[was_head].checkout()
    finally:
        shutil.rmtree(repo.working_dir)

    db.sites.update_one({'name': name}, {"$set": data}, upsert=True)

    return jsonify({
        'result': 'ok',
    })

@app.route("/live_log")
def livelog():
    name = request.args['name']
    get_logs_url(name)
    return redirect(get_logs_url(name))

@app.route("/run_robot_tests")
def run_robot_tests():
    site = request.args.get('site')
    logs_writer = LogsIOWriter(site, 'robot')
    def _run():
        _odoo_framework(site, ['robot', '-a'], logs_writer=logs_writer)
    threading.Thread(target=_run).start()
    return jsonify({'result': 'ok'})