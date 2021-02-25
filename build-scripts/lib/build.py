import arrow
from datetime import datetime
import requests
from functools import partial
from pathlib import Path
import subprocess
import os
import sys
import logging
FORMAT = '[%(levelname)s] %(name) -12s %(asctime)s %(message)s'
logging.basicConfig(format=FORMAT)
logging.getLogger().setLevel(logging.DEBUG)
logger = logging.getLogger('')  # root handler

def _setup_new_working_path(workspace, instance_name):
    new_path = Path(workspace / f'cicd_instance_{instance_name}')
    new_path.mkdir(exist_ok=True) # set later False; avoids thresholing
    logger.info(f"Rsyncing {os.getcwd()}/ to {new_path}/")
    subprocess.check_call([
        '/usr/bin/rsync',
        str(os.getcwd()) + "/",
        str(new_path) + "/",
        '-ar',
        '--delete-after',
    ])
    logger.info(f"Changing working directory to: {new_path}")
    os.chdir(new_path)
    subprocess.check_call([
        '/usr/bin/git',
        'repack',
        '-a',
        '-d',
        '--depth=250',
        '--window=250',
    ])

def _exec(context, cmd, needs_result=False):
    print_cmd = ' '.join(map(lambda x: f"'{x}'".format(x), cmd))
    logger.info(f"Executing:\ncd '{os.getcwd()}';odoo {print_cmd}")

    method = 'check_output' if needs_result else 'check_call'
    res = getattr(subprocess, method)([
        "/opt/odoo/odoo",
    ] + cmd, env=context.env)
    if needs_result:
        return res.decode('utf-8')

def _notify_instance_updated(context, instance, duration, dump_date, dump_name):
    name = instance['name']
    logger.info(f"notify_instance name: {name}, duration: {duration}, branch: {instance['git_branch']}, dump: {dump_date} {dump_name}")

    data = {
        'name': name,
        'sha': instance['git_sha'],
        'duration': duration,
    }
    if dump_date:
        data['dump_date'] = dump_date
        data['dump_name'] = dump_name

    requests.get(context.cicd_url + "/notify_instance_updated", params=data).json()
    requests.get(context.cicd_url + '/set_updating', params={'name': name, 'value': 0})
    context.jira_wrapper.comment(
        instance['git_branch'],
        f"Instance updated {name} in {duration} seconds."
    )

def _make_instance_docker_configs(context, instance):
    instance_name = instance['name']
    file = context.odoo_settings / f'docker-compose.{instance_name}.yml'
    file.parent.mkdir(exist_ok=True)
    file.write_text("""
services:
    proxy:
        networks:
            - cicd_network
networks:
    cicd_network:
        external:
            name: {}
    """.format(os.environ["CICD_NETWORK_NAME"]))

    (context.odoo_settings / f'settings.{instance_name}').write_text("""
DEVMODE=1
DUMPS_PATH={}
RUN_PROXY_PUBLISHED=0
RUN_ODOO_CRONJOBS=0
RUN_ODOO_QUEUEJOBS=0
RUN_CRONJOBS=0
RUN_CUPS=0
RUN_POSTGRES=0

DB_HOST={}
DB_USER={}
DB_PWD={}
DB_PORT={}
""".format(
        os.environ['DUMPS_PATH'],
        os.environ['DB_HOST'],
        os.environ['DB_USER'],
        os.environ['DB_PASSWORD'],
        os.environ['DB_PORT'],
    ))

def augment_instance(context, instance):
    title = 'n/a'
    creator = 'n/a'
    try:
        fields = context.jira_wrapper.infos(instance['git_branch'])
        if not isinstance(fields, dict):
            title = fields.summary
            creator = fields.creator.displayName
    except Exception as ex:
        logger.warn(ex)
    instance['title'] = title
    instance['initiator'] = creator

def update_instance(context, instance, dump_name, force_rebuild=False):
    logger.info(f"Updating instance {instance['name']}")
    _setup_new_working_path(
        context.workspace,
        instance['name']
    )
    last_sha = requests.get(context.cicd_url + "/last_successful_sha", params={
        'name': instance['name'],
    }).json()
    requests.get(context.cicd_url + "/notify_instance_updating", params={
        'name': instance['name'],
    })
    logger.info(f"Result of asking for last_successful_sha: {last_sha}")
    if not last_sha.get('sha') or force_rebuild:
        logger.info(f"Make new instance: force rebuild: {force_rebuild} / last sha: {last_sha.get('sha')}")
        make_instance(context, instance, dump_name, use_previous_db=True) # TODO parametrized from jenkins
    else:
        logger.info(f"Updating current instance")
        # mark existing instance as being updated
        requests.get(context.cicd_url + '/set_updating', params={
            'name': instance['name'], 'value': 1
        })
        started = arrow.get()
        _exec(context, ["--project-name", instance['name'], "update", "--no-dangling-check", "--since-git-sha", last_sha['sha']])
        _notify_instance_updated(
            context, instance, (arrow.get() - started).total_seconds(), "", ""
        )

def make_instance(context, instance, use_dump, use_previous_db=False):
    _setup_new_working_path(context.workspace, instance['name'])
    logger.info(f"BUILD CONTROL: Making Instance for {instance['name']}")
    _make_instance_docker_configs(context, instance)

    def e(cmd, needs_result=False):
        cmd = ["-f", "--project-name", instance['name']] + cmd
        return _exec(context, cmd, needs_result)

    logger.info("Reloading...")
    e(["reload", '-d', instance['name'], '--headless', '--devmode'])
    logger.info(f"Calling register with branch {instance['git_branch']}")

    if not instance['git_branch']:
        raise Exception("required git branch!")
    instance['date_registered'] = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    requests.post(context.cicd_url + '/register', json=instance).raise_for_status()

    dump_date, dump_name = None, None
    if use_dump:
        logger.info(f"BUILD CONTROL: Restoring DB for {instance['name']} from {use_dump}")
        e(["restore", "odoo-db", use_dump])
        e(["remove-web-assets"])
        dump_file = Path(os.environ['DUMPS_PATH']) / use_dump
        dump_date = arrow.get(dump_file.stat().st_mtime).to('UTC').strftime("%Y-%m-%d %H:%M:%S")
        dump_name = use_dump

    else:
        logger.info(f"BUILD CONTROL: Resetting DB for {instance['name']}")
        e(["db", "reset"])

    e(["build"]) # build containers; use new pip packages

    started = arrow.get()
    e(["update"]) # odoo module updates
    e(["turn-into-dev", "turn-into-dev"])
    e(["set-ribbon", instance['name']])
    _notify_instance_updated(context, instance, (arrow.get() - started).total_seconds(), dump_date, dump_name)
