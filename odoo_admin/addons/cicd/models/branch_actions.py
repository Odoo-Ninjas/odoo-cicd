from pathlib import Path
import threading
import os
import requests
import arrow
# TODO turn into dev an?
import base64
from odoo import _, api, fields, models, SUPERUSER_ID
from odoo.exceptions import UserError, RedirectWarning, ValidationError

class Branch(models.Model):
    _inherit = 'cicd.git.branch'

    def _reload_and_restart(self, shell, task, logsio, **args):
        self._reload(task, logsio)
        self._checkout_latest(self.machine_id, logsio)
        shell.X(['odoo', '--project-name', self.name, 'build'])
        shell.X(['odoo', '--project-name', self.name, 'up', '-d'])

    def _restore_dump(self, shell, task, logsio, **args):
        self._reload(task, logsio)
        task.dump_used = self.dump_id.name
        shell.X(['odoo', '--project-name', self.name, 'reload'])
        shell.X(['odoo', '--project-name', self.name, 'build'])
        shell.X(['odoo', '--project-name', self.name, 'down'])
        shell.X([
            'odoo', '--project-name', self.name,
            '-f', 'restore', 'odoo-db',
            self.dump_id.name
        ])
    
    def _docker_start(self, shell, task, logsio, **args):
        shell.X(['odoo', '--project-name', self.name, 'up', '-d'])

    def _docker_stop(self, shell, task, logsio, **args):
        shell.X(['odoo', '--project-name', self.name, 'kill'])

    def _docker_get_state(self, shell, task, logsio, **args):
        import pudb;pudb.set_trace()
        info = shell.X(['odoo', '--project-name', self.name, 'ps', 'kill']).output
            
    def _turn_into_dev(self, task, logsio, **args):
        with self._shellexec(logsio=logsio) as shell:
            shell.X(['odoo', '--project-name', 'turn-into-dev'])

    def _reload(self, shell, task, logsio, **args):
        raw_settings = (task.machine_id.reload_config or '') + "\n" + (self.reload_config or '')
        odoo_settings = base64.encodestring((raw_settings).encode('utf-8').strip()).decode('utf-8')
        shell.X([
            'odoo', '--project-name', self.name,
            'reload', '--additional_config', odoo_settings
            ])

    def _build(self, shell, task, logsio, **args):
        self._reload()
        shell.X(['odoo', '--project-name', self.name, 'build'])

    def _dump(self, shell, task, logsio, **args):
        shell.X([
            'odoo', '--project-name', self.name, 
            'backup', 'odoo-db', self.name + ".dump.gz"
            ])

    def _update_git_commits(self, shell, task, logsio, **args):
        self.ensure_one()
        instance_folder = self._get_instance_folder(self.machine_id)

        commits = shell.check_output([
            "/usr/bin/git",
            "log",
            "--pretty=format:%H,%ct",
            "--since='last month'",
        ], cwd=instance_folder).output
        for line in commits:
            date = arrow.get(int(line.split(",")[-1]))
            sha = arrow.get(line.split(",")[0])
            commit = self.commit_ids.filtered(lambda x: x.name == sha)
            if commit:
                continue

            logsio.info(f"Found new commit: {commit}")
            import pudb;pudb.set_trace()

            info = shell.check_output([
                "/usr/bin/git",
                "log",
                "-n1",
            ], cwd=instance_folder).output.split("\n")
            self.commit_ids = [[0, 0, {
                'name': sha,
                'author': info[1].replace("Author: ", ""),
                'date': arrow.get(info[1].replace("Date: ", "").strip()).datetime,
                'text': '\n'.join(info[3:]).strip(),
            }]]
    
    def _remove_web_assets(self, shell, tasks, logsio, **args):
        shell.X([
            'odoo', '--project-name', self.name,
            'remove-web-assets'
            ])

    def _clear_db(self, shell, tasks, logsio, **args):
        shell.X([
            'odoo', '--project-name', self.name,
            'cleardb'
            ])

    def _run_robot_tests(self, shell, tasks, logsio, **args):
        shell.X([
            'odoo', '--project-name', self.name,
            'robot', '-a',
        ])

    def _run_unit_tests(self, shell, tasks, logsio, **args):
        shell.X([
            'odoo', '--project-name', self.name,
            'run-tests',
        ])

    def _transform_input_dump():
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

        
    def _after_build(self, shell, logsio):
        cmd = ['odoo', '--project-name', self.name]
        shell.X(cmd + ["remove-settings", '--settings', 'web.base.url,web.base.url.freeze'])
        shell.X(cmd + ["update-setting", 'web.base.url', os.environ['CICD_URL']])
        shell.X(cmd + ["set-ribbon", site['name']])
        shell.X(cmd + ["prolong"])

    def _build_since_last_gitsha(self, shell, logsio):
        # todo make button
        self._after_build(shell=shell, logsio=logsio, **args)

    def _reset(self, task, shell, **args):
        shell.X(
            ['odoo', '--project-name', self.name, 'db', 'reset', '--do-not-install-base'],
        )

    def _checkout_latest(self, shell, machine, logsio, **args):
        instance_folder = self._get_instance_folder(machine)
        with machine._shell() as shell:
            with machine._shellexec(
                logsio=logsio,
                env={
                    "GIT_TERMINAL_PROMPT": "0",
                }

            ) as shell_exec:
                logsio.write_text(f"Updating instance folder {self.name}")

                logsio.write_text(f"Cloning {self.name} to {instance_folder}")
                self.repo_id.clone_repo(machine, instance_folder, logsio)

                logsio.write_text(f"Checking out {self.name}")
                shell_exec.X(["git", "checkout", "-f", self.name])

                logsio.write_text(f"Pulling {self.name}")
                shell_exec.X(["git", "pull"])

                logsio.write_text(f"Clean git")
                shell_exec.X(["git", "clean", "-xdff"])

                logsio.write_text("Updating submodules")
                shell_exec.X(["git", "submodule", "update", "--init", "--force", "--recursive"])

                logsio.write_text("Getting current commit")
                commit = shell_exec.X(["git", "rev-parse", "HEAD"]).output.strip()
                logsio.write_text(commit)

                return str(commit)

    def debug_instance(self):
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
    
    def show_pgcli(self):
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

    def shell_instance(self):
        # kill existing container and start odoo with debug command
        def _get_shell_url(command):
            pwd = base64.encodestring('odoo'.encode('utf-8')).decode('utf-8')
            shellurl = f"/console/?encoding=utf-8&term=xterm-256color&hostname=127.0.0.1&username=root&password={pwd}&command="
            shellurl += ' '.join(command)
            return shellurl

        containers = docker.containers.list(all=True, filters={'name': [name]})
        containers = [x for x in containers if x.name == name]
        shell_url = _get_shell_url([
            "cd", f"/{os.environ['WEBSSH_CICD_WORKSPACE']}/{site_name}", ";",
            "/usr/bin/python3",  "/opt/odoo/odoo", "-f", "--project-name", site_name, "debug", "odoo_debug", "--command", "/odoolib/shell.py",
        ])
        # TODO make safe; no harm on system, probably with ssh authorized_keys

        return {
            'type': 'ir.actions.act_url',
            'url': 'shell_url',
            'target': 'self'
        }


    def clear_instance(self):
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

    @api.model
    def inactivity_cycle_down(shell):
        while True:
            sites = db.sites.find({'name': 1, 'last_access': 1})
            for site in sites:
                try:
                    logger = LogsIOWriter(site['name'], 'misc')
                    logger.debug(f"Checking site to cycle down: {site['name']}")
                    if (arrow.get() - arrow.get(site.get('last_access', '1980-04-04') or '1980-04-04')).total_seconds() > 2 * 3600: # TODO configurable
                        if _get_docker_state(site['name']) == 'running':
                            logger.debug(f"Cycling down instance due to inactivity: {site['name']}")
                            _odoo_framework(site['name'], 'kill', logs_writer=logger)

                except Exception as ex:
                    import traceback
                    msg = traceback.format_exc()
                    logger.error(msg)
            time.sleep(10)

    def _make_instance_docker_configs(site):
        instance_name = site['name']
        odoo_settings = Path("/odoo_settings")  # e.g. /home/odoo/.odoo
        file = odoo_settings / f'docker-compose.{instance_name}.yml'
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

        (odoo_settings / f'settings.{instance_name}').write_text("""
    DEVMODE=1
    PROJECT_NAME={}
    DUMPS_PATH={}
    RUN_PROXY_PUBLISHED=0
    RUN_CRONJOBS=0
    RUN_CUPS=0
    RUN_POSTGRES=0

    DOCKER_LABEL_ODOO_CICD=1
    DOCKER_LABEL_ODOO_CICD_INSTANCE_NAME={}

    DB_HOST={}
    DB_USER={}
    DB_PWD={}
    DB_PORT={}
    """.format(
            instance_name,
            os.environ['DUMPS_PATH'],
            instance_name,
            os.environ['DB_HOST'],
            os.environ['DB_USER'],
            os.environ['DB_PASSWORD'],
            os.environ['DB_PORT'],
        ))
