from .. import db
from io import BytesIO ## for Python 3
import docker as Docker
import base64
import psycopg2
import spur
import arrow
import os
import json
import subprocess
import shutil
from pathlib import Path
from bson import ObjectId
import docker as Docker
import logging
from .. import host_ip
from git import Repo

logger = logging.getLogger(__name__)

docker = Docker.from_env()

BOOL_VALUES = ['1', 1, 'true', 'True', 'y']

class OdooFrameworkException(BaseException): pass

class JSONEncoder(json.JSONEncoder):
    # for encoding ObjectId
    def default(self, o):
        if isinstance(o, ObjectId):
            return str(o)

        return super(JSONEncoder, self).default(o)

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
            except Exception:
                continue
    return records

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
        if v in ['true', 'True']:
            data[k] = True
        elif v in ['false', 'False']:
            data[k] = False
        elif v == 'undefined':
            data[k] = None

    if '_id' in data and isinstance(data['_id'], str):
        data['_id'] = ObjectId(data['_id'])
    return data

def _odoo_framework(site_name, command):
    if isinstance(site_name, dict):
        site_name = site_name['name']
    if isinstance(command, str):
        command = [command]

    res, stdout, stderr = _execute_shell(
        ["/opt/odoo/odoo", "-f", "--project-name", site_name] + command,
        cwd=f"{os.environ['CICD_WORKSPACE']}/{site_name}",
        env={
            'NO_PROXY': "*",
            'DOCKER_CLIENT_TIMEOUT': "600",
            'COMPOSE_HTTP_TIMEOUT': "600",
            'PSYCOPG_TIMEOUT': "120",
        }
    )
    output = stdout + '\n' + stderr
    if res == 'error':
        store_output(site_name, 'last_error', output)
        raise OdooFrameworkException(output)

    store_output(site_name, 'last_error', '')
    return output

def _execute_shell(command, cwd=None, env=None):
    if isinstance(command, str):
        command = [command]

    env = env or {}

    stdout, stderr = BytesIO(), BytesIO()

    def decoded(s):
        return s.decode('utf-8')

    with spur.SshShell(
        hostname=host_ip,
        username=os.environ['HOST_SSH_USER'],
        private_key_file="/root/.ssh/id_rsa",
        missing_host_key=spur.ssh.MissingHostKey.accept
        ) as shell:
        try:
            result = shell.run(
                command,
                cwd=cwd,
                update_env=env,
                stdout=stdout,
                stderr=stderr,
                )
        except Exception as ex:
            stdout.seek(0)
            stderr.seek(0)
            logger.error(ex)
            return 'error', decoded(stdout.read()), decoded(stderr.read())

    stdout.seek(0)
    stderr.seek(0)
    return result, decoded(stdout.read()), decoded(stderr.read())

    
def _get_resources():
    for disk in Path("/display_resources").glob("*"):
        total, used, free = shutil.disk_usage(disk)
        yield {
            'name': disk.name,
            'total': total // (2**30),
            'used': used // (2**30),
            'free': free // (2**30),
            'used_percent': round(used / total * 100),
            'color': 'green' if round(used / total * 100) < 80 else 'red',
        }

    """
              total        used        free      shared  buff/cache   available
Mem:       32165168    11465300      246788      401468    20453080    19849564
Swap:             0           0           0
    """
    res, stdout, stderr = _execute_shell("/usr/bin/free")
    ram = [x for x in stdout.split("\n") if 'Mem:' in x][0].strip()
    while '\t' in ram or '  ' in ram:
        ram = ram.replace("\t", "")
        ram = ram.replace("  ", " ")
    ram = ram.split(" ")
    yield {
        'name': "RAM",
        'total': int(ram[1]) / 1024 / 1024,
        'used': int(ram[2]) / 1024 / 1024,
        'free': int(ram[6]) / 1024 / 1024,
        'used_percent': round(float(ram[2]) / float(ram[1]) * 100, 0),
        'color': 'green' if round(int(ram[6]) / 1024 / 1024) > 4 else 'red',
    }

def _delete_dockercontainers(name):
    containers = docker.containers.list(all=True, filters={'name': [name]})
    for container in containers:
        if container.status == 'running':
            container.kill()
        container.remove(force=True)

def _get_src_path(name):
    path = Path("/cicd_workspace") / name
    return path
    
def _delete_sourcecode(name):
    path = _get_src_path(name)
    if path.exists():
        shutil.rmtree(path)

def _drop_db(cr, dbname):
    # Version 13:
    # DROP DATABASE mydb WITH (FORCE);
    dbnames = _get_all_databases(cr)
    if dbname not in dbnames:
        return
    cr.execute(f"ALTER DATABASE {dbname} CONNECTION LIMIT 0;")
    cr.execute("""
        SELECT pg_terminate_backend(pid)
        FROM pg_stat_activity
        WHERE datname = %s;
    """, (dbname,))
    cr.execute(f"DROP DATABASE {dbname}")

def _get_all_databases(cr):
    cr.execute("""
        SELECT d.datname as "Name"
        FROM pg_catalog.pg_database d
        ORDER BY 1;
    """)

    dbnames = [x[0] for x in cr.fetchall()]
    return dbnames

def _get_db_conn():
    conn = psycopg2.connect(
        host=os.environ['DB_HOST'],
        port=int(os.environ['DB_PORT']),
        user=os.environ['DB_USER'],
        password=os.environ['DB_PASSWORD'],
        dbname="template1",
    )
    conn.autocommit = True
    return conn


def _get_shell_url(command):
    pwd = base64.encodestring('odoo'.encode('utf-8')).decode('utf-8')
    shellurl = f"/console/?hostname=127.0.0.1&username=root&password={pwd}&command="
    shellurl += ' '.join(command)
    return shellurl



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





def _export_git_values():

    def g(v):
        git = ['/usr/bin/git', 'show', '-s']
        return subprocess.check_output(git + [f'--pretty={v}']).decode('utf-8').strip()

    os.environ['GIT_AUTHOR_NAME'] = g("%an")
    os.environ['GIT_DESC'] = g("%s")
    os.environ['GIT_SHA'] = g("%H")
    if not os.getenv("GIT_BRANCH"):
        if os.getenv("BRANCH_NAME"):
            os.environ['GIT_BRANCH'] = os.environ['BRANCH_NAME']

def _get_docker_state(name):
    docker.ping()
    containers = docker.containers.list(all=True, filters={'name': [name]})
    states = set(map(lambda x: x.status, containers))
    return 'running' in states

def _store(sitename, info, upsert=False):
    db.sites.update_one({
        'name': sitename,
    }, {
        '$set': info,
    }, upsert=upsert)

def _get_repo(sitename):
    path = _get_src_path(sitename)
    return Repo(path)

def store_output(sitename, ttype, output):
    db.outputs.update_one({
        'name': sitename,
        'ttype': ttype
    }, {
        '$set': {
            'log': output
        }
    }, upsert=True
    )

def get_output(sitename, ttype):
    rec = db.outputs.find_one({'name': sitename, 'ttype': ttype})
    if not rec:
        return ""
    return rec['log']

def _get_config(name, default):
    config = db.config.find_one({'name': name})
    if config:
        return config['value']
    return default

def _set_config(name, value):
    db.config.update_one({'name': name}, {'$set': {'name': name, 'value': value}}, upsert=True)
