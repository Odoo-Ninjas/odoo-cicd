from .. import db
import socket
from io import BytesIO ## for Python 3
import docker as Docker
import base64
import psycopg2
import spur
import git
import arrow
import os
import json
import subprocess
import shutil
from pathlib import Path
from bson import ObjectId
import docker as Docker
import logging
import os
from .. import host_ip
from git import Repo
from .. import rolling_log_dir
from .. import MAIN_FOLDER_NAME


logger = logging.getLogger(__name__)

docker = Docker.from_env()

BOOL_VALUES = ['1', 1, 'true', 'True', 'y']

class OdooFrameworkException(Exception): pass

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

def write_rolling_log(file, line, prefix=''):
    with open(str(file), 'a') as fh:
        fh.write(f"{prefix}_____{arrow.get()}_____{line}\n")
        fh.flush()

def _odoo_framework(site_name, command, start_rolling_new=False, rolling_file_name=None, instance_folder=None):
    logger.info(f"Executing command: {site_name} {command}")
    if isinstance(site_name, dict):
        site_name = site_name['name']
    if isinstance(command, str):
        command = [command]

    file = rolling_log_dir / (rolling_file_name or site_name)
    if start_rolling_new:
        file.write_text("")

    if instance_folder:
        if not instance_folder.exists():
            raise Exception(f"Instance folder does not exist: {instance_folder}")
        if instance_folder.parent != Path('/cicd_workspace'):
            raise Exception(f"Parent of Instance folder must be /cicd_workspace")
        instance_folder = Path(os.environ['CICD_WORKSPACE']) / instance_folder.name
    else:
        instance_folder = f"{os.environ['CICD_WORKSPACE']}/{site_name}"

    def on_input(prefix, line):
        if line:
            write_rolling_log(file, line, prefix=prefix)

    res, stdout, stderr = _execute_shell(
        ["/opt/odoo/odoo", "-f", "--project-name", site_name] + command,
        cwd=instance_folder, 
        env={
            'NO_PROXY': "*",
            'DOCKER_CLIENT_TIMEOUT': "600",
            'COMPOSE_HTTP_TIMEOUT': "600",
            'PSYCOPG_TIMEOUT': "120",
        },
        callback=on_input
    )
    output = stdout + '\n' + stderr
    if res == 'error':
        store_output(site_name, 'last_error', output)
        write_rolling_log(file, stderr)
        raise OdooFrameworkException(output)

    store_output(site_name, 'last_error', '')
    logger.info(f"Executed command: {site_name} {command}")
    return output

def _execute_shell(command, cwd=None, env=None, callback=None):
    if isinstance(command, str):
        command = [command]

    env = env or {}

    class MyWriter(object):
        def __init__(self, prefix):
            self.text = [""]
            self.prefix = prefix

        def write(self, bytes):
            s = bytes.decode('utf-8', errors='ignore')
            for c in s:
                if c == '\n':
                    line = self.text[-1]
                    # if self.prefix == 'stderr':
                    #     logger.error(line)
                    # else:
                    #     logger.debug(line)
                    self.text.append("")
                    if callback:
                        callback(self.prefix, line)
                else:
                    self.text[-1] += c

        def getall(self):
            return '\n'.join(self.text)

    stdout, stderr = MyWriter('stdout'), MyWriter('stderr')


    with spur.SshShell(
        hostname=host_ip,
        username=os.environ['HOST_SSH_USER'],
        private_key_file="/root/.ssh/id_rsa",
        missing_host_key=spur.ssh.MissingHostKey.accept
        ) as shell:
        try:
            result = shell.run(
                command,
                cwd=str(cwd) if cwd else cwd,
                update_env=env,
                stdout=stdout,
                stderr=stderr,
                )
        except Exception as ex:
            logger.error(ex)
            if callback:
                callback('stderr', f"Process aborted")
            return 'error', stdout.getall(), stderr.getall()

    if callback:
        callback('stdout', f"Successfully Finished.")
    return result, stdout.getall(), stderr.getall()

    
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
        dbname="postgres",
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

def clone_repo(url, path):
    if not path.exists():
        git.Repo.clone_from(url, path)
    try:
        repo = Repo(path)
    except git.exc.InvalidGitRepositoryError:
        shutil.rmtree(path)
        git.Repo.clone_from(url, path)
        repo = Repo(path)
    return repo

def _get_main_repo():
    from . import GIT_LOCK
    with GIT_LOCK:
        from . import WORKSPACE
        from . import URL

        path = WORKSPACE / MAIN_FOLDER_NAME
        repo = clone_repo(URL, path)
    return repo

def update_instance_folder(branch, rolling_file, instance_folder=None):
    from . import GIT_LOCK
    from . import URL
    from . import WORKSPACE
    instance_folder = Path(instance_folder or WORKSPACE / branch)
    tries = 0
    with GIT_LOCK:
        while tries < 3:
            try:
                tries += 1
                write_rolling_log(rolling_file, f"Updating instance folder {branch}")
                _store(branch, {'is_building': True})
                write_rolling_log(rolling_file, f"Cloning {branch} {URL} to {instance_folder}")
                repo = clone_repo(URL, instance_folder)
                write_rolling_log(rolling_file, f"Checking out {branch}")
                repo.git.checkout(branch, force=True)
                write_rolling_log(rolling_file, f"Pulling {branch}")
                repo.git.pull()
                write_rolling_log(rolling_file, f"Clean git")
                run = subprocess.run(
                    ["git", "clean", "-xdff"],
                    capture_output=True,
                    cwd=instance_folder,
                    env=dict(os.environ, GIT_TERMINAL_PROMPT="0")
                    )

                run = subprocess.run(
                    ["git", "submodule", "update", "--init", "--force", "--recursive"],
                    capture_output=True,
                    cwd=instance_folder,
                    env=dict(os.environ, GIT_TERMINAL_PROMPT="0")
                    )
                if run.returncode:
                    msg = run.stdout.decode('utf-8') + "\n" + run.stderr.decode('utf-8')
                    write_rolling_log(rolling_file, msg)
                    raise Exception(msg)
                commit = repo.refs[branch].commit
                user_id = get_sshuser_id()
                write_rolling_log(rolling_file, f"Setting access rights in {instance_folder} to {user_id}")
                subprocess.check_call(["/usr/bin/chown", f"{user_id}:{user_id}", "-R", str(instance_folder)])
                return str(commit)

            except Exception as ex:
                if tries < 3:
                    rolling_file.write_text(str(ex))
                    logger.warn(ex)
                    rolling_file.write_text(f"Retrying update instance folder for {branch}")
                    if instance_folder.exists():
                        shutil.rmtree(instance_folder)
                else:
                    raise

def get_sshuser_id():
    user_name = os.environ['HOST_SSH_USER']
    res, stdout, stderr = _execute_shell(["/usr/bin/id", '-u', user_name])
    user_id = stdout.strip()
    return user_id

def _get_instance_config(sitename):
    settings = Path("/odoo_settings/run") / sitename / 'settings'
    dbname = ""
    if settings.exists():
        try:
            dbname = [x for x in settings.read_text().split("\n") if 'DBNAME=' in x][0].split("=")[1]
        except IndexError: pass
    
    return {
        "DBNAME": dbname
    }

def _get_host_path(path):
    """
    For the given path inside container the host path is returned.
    """
    hostname = socket.gethostname()
    container = [x for x in docker.containers.list(all=True) if x.id.startswith(hostname)][0]
    inspect = json.loads(subprocess.check_output(['docker', 'inspect', container.id]))
    source = [x for x in inspect[0]['Mounts'] if x['Destination'] == str(path)][0]['Source']
    return Path(source)