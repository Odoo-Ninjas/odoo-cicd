import os
from pathlib import Path
from flask import request
from flask import jsonify
from .. import app
from .. import login_required
from flask import render_template
from .tools import _format_dates_in_records
from .tools import _get_resources
from .. import db

@app.route('/')
@login_required
def index_func():

    return render_template(
        'index.html',
        DATE_FORMAT=os.environ['DATE_FORMAT'].replace("_", "%"),
    )

@app.route("/possible_dumps")
def possible_dumps():
    path = Path("/opt/dumps")
    dump_names = sorted([x.name for x in path.glob("*")])

    def _get_value(filename):
        date = arrow.get((path / filename).stat().st_mtime).strftime("%Y-%m-%d %H:%M:%S")
        return f"{filename} [{date}]"

    dump_names = [{'id': x, 'value': _get_value(x)} for x in dump_names]
    return jsonify(dump_names)

@app.route("/turn_into_dev")
def _turn_into_dev():
    if not request.args.get('site'):
        raise Exception('site missing')
    site = db.sites.find_one({'name': request.args.get('site')})
    if site:
        site = site['name']
        _reload_instance(site)
        _odoo_framework(site, ["turn-into-dev", "turn-into-dev"])
    return jsonify({'result': 'ok'})

def _reload_instance(site):
    _odoo_framework(site, ["reload", "-d", site])

    
@app.route('/trigger/rebuild')
def trigger_rebuild():
    site = db.sites.find_one({'name': request.args['name']})
    _set_marker_and_restart(
        request.args['name'],
        {
            'reset-db-at-next-build': True
        }
    )
    db.updates.remove({'name': site['name']})
    return jsonify({
        'result': 'ok',
    })

@app.route("/data/site/live_values")
def site_jenkins():
    sites = list(db.sites.find())
    # for site in sites:
    #     try:
    #         job = _get_jenkins_job(site['git_branch'])
    #     except Exception as ex:
    #         site['last_build'] = f"Error: {ex}"
    #     else:
    #         if job:
    #             last_build = job.get_last_build_or_none()
    #             if last_build:
    #                 site['last_build'] = last_build.get_status()
    #                 site['duration'] = round(last_build.get_duration().total_seconds(), 0)
    #             site['update_in_progress'] = job.is_running()
    #         site['docker_state'] = 'running' if _get_docker_state(site['name']) else 'stopped'
    return jsonify(sites)


@app.route("/data/sites", methods=["GET", "POST"])
def data_variants():
    _filter = {}
    if request.args.get('git_branch', None):
        _filter['git_branch'] = request.args['git_branch']
    if request.args.get('name', None):
        _filter['name'] = request.args['name']

    sites = list(db.sites.find(_filter))

    sites = _format_dates_in_records(sites)
    sites = sorted(sites, key=lambda x: x.get('name'))

    for site in sites:
        site['id'] = site['_id']
        site['update_in_progress'] = False
        site['repo_url'] = f"{os.environ['REPO_URL']}/-/commit/{site['git_sha']}"

    return jsonify(sites)

@app.route('/update/site', methods=["GET", "POST"])
def update_site():
    if request.method == 'POST':
        data = request.form
    else:
        data = request.args
    data = _validate_input(data, int_fields=[])
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
    name = request.args['name']
    docker_state = _get_docker_state(name)
    logger.info(f"Opening user interface of cicd instance {name}; current docker state: {docker_state}")
    if not docker_state:
        _restart_docker(name, kill_before=False)

    response = make_response(
        render_template(
            'start_cicd.html',
            initial_path=request.args.get('initial_path') or '/web/login'
        ),
    )
    response.set_cookie('delegator-path', name)
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
    shellurl = f"/console/?hostname=127.0.0.1&username=root&password={pwd}&command="
    shellurl += ' '.join(command)
    return shellurl

@app.route("/show_logs")
def show_logs():
    name = request.args.get('name')
    name += '_odoo'
    containers = docker.containers.list(all=True, filters={'name': [name]})
    containers = [x for x in containers if x.name == name]
    shell_url = _get_shell_url(["docker", "logs", "-f", containers[0].id])
    return redirect(shell_url)

@app.route("/debug_instance")
def debug_instance():
    name = request.args.get('name')
    site_name = name
    name += '_odoo'
    # kill existing container and start odoo with debug command
    containers = docker.containers.list(all=True, filters={'name': [name]})
    containers = [x for x in containers if x.name == name]
    for container in containers:
        container.stop()
    shell_url = _get_shell_url([
        "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/cicd_instance_{site_name}", ";",
        "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "debug", "odoo", "--command", "/odoolib/debug.py",
    ])
    # TODO make safe; no harm on system, probably with ssh authorized_keys

    return redirect(shell_url)
    
@app.route("/get_resources")
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

        sites = set([x['name'] for x in db.sites.find({})])
        for dbname in dbnames:
            if dbname.startswith('template') or dbname == 'postgres':
                continue
            if dbname not in sites:

                _drop_db(cr, dbname)

        # Drop also old sourcecodes
        for dir in Path("/cicd_workspace").glob("cicd_instance_*"):
            instance_name = dir.name[len("cicd_instance_"):]
            if instance_name not in sites:
                _delete_sourcecode(instance_name)

        # remove artefacts from ~/.odoo/
        os.system("docker system prune -f -a")

    finally:
        cr.close()
        conn.close()

    return jsonify({'result': 'ok'})

    
    
    
@app.route("/delete")
def delete_instance():
    name = request.args.get('name')
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
        
    db.sites.remove({'name': name})
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
    job = _get_jenkins_job(site['git_branch'])
    build = job.get_last_build_or_none()
    return render_template(
        'log_view.html',
        name=site['name'],
        site=site,
        build=build,
        output=build.get_console(),
    )

@app.route("/dump")
def backup_db():
    _set_marker_and_restart(
        request.args.get('name'),
        {
            'backup-db': request.args['dumpname'],
        }
    )
    return jsonify({
        'result': 'ok',
    })

@app.route("/build_again")
def build_again():
    if request.args.get('all') == '1':
        param_name = 'do-build-all'
    else:
        param_name = 'do-build'
    _set_marker_and_restart(
        request.args.get('name'),
        {
            param_name: True,
        }
    )
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
        "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/cicd_instance_{site_name}", ";",
        "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "debug", "odoo_debug", "--command", "/odoolib/shell.py",
    ])
    # TODO make safe; no harm on system, probably with ssh authorized_keys

    return redirect(shell_url)



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
        'dump_date',
        'dump_name',
    ]:
        info[extra_args] = request.args.get(extra_args)

    info['date'] = arrow.get().strftime("%Y-%m-%d %H:%M:%S")

    db.updates.insert_one(info)

    site = db.sites.find_one({'name': info['name']})
    if not site:
        raise Exception(f"site not found: {info['name']}")
    db.sites.update_one({'_id': site['_id']}, {'$set': {
        'duration': request.args.get('duration'),
        'updated': datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S"),
    }}, upsert=False)

    # if there is dump information, then store at site
    if request.args.get('dump_name'):
        db.sites.update_one({'_id': site['_id']}, {'$set': {
            'dump_name': request.args['dump_name'],
            'dump_date': request.args['dump_date'],
        }}, upsert=False)

    return jsonify({
        'result': 'ok'
    })

    
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
    _restart_docker(None, kill_before=False)
    return jsonify({
        'result': 'ok',
    })
    
@app.route('/restart_delegator')
def restart_delegator():
    docker_project_name = os.environ['PROJECT_NAME']
    delegator_name = f"{docker_project_name}_cicd_delegator"
    containers = docker.containers.list(all=True, filters={'name': [delegator_name]})
    for container in containers:
        try:
            container.stop()
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









