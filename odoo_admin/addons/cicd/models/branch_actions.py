import re
from odoo import fields
from pathlib import Path
import os
import arrow
from odoo import _, api, models, fields
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import inspect
import os
from pathlib import Path
from odoo.addons.queue_job.exception import RetryableJobError
import logging
current_dir = Path(os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe()))))

logger = logging.getLogger(__name__)

class Branch(models.Model):
    _inherit = 'cicd.git.branch'

    def _update_odoo(self, shell, task, logsio, **kwargs):
        if self.block_updates_until and self.block_updates_until > fields.Datetime.now():
            raise RetryableJobError("Branch is blocked - have to wait", seconds=60, ignore_retry=True)
        tasks = self.task_ids.filtered(lambda x: x.state == 'done' and x.name in ['_update_all_modules', '_update_odoo']).sorted(lambda x: x.id, reverse=True)
        commit = None
        if tasks:
            commit = tasks[0].commit_id.name
        if commit:
            try:
                logsio.info("Updating")
                shell.odoo("update", "--since-git-sha", commit)
            except Exception as ex:
                logger.error(ex)
                logsio.error(ex)
                logsio.info(f"Running full update now - update since sha {commit} did not succeed")
                self._update_all_modules(shell=shell, task=task, logsio=logsio, **kwargs)
        else:
            self._update_all_modules(shell=shell, task=task, logsio=logsio, **kwargs)

    def _update_all_modules(self, shell, task, logsio, **kwargs):
        logsio.info("Reloading")
        self._reload(shell, task, logsio)
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Updating")
        shell.odoo('update')
        logsio.info("Upping")
        shell.odoo("up", "-d")

    def _reload_and_restart(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio)
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Upping")
        shell.odoo("up", "-d")
        self._after_build(shell, logsio)

    def _restore_dump(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio)
        task.sudo().write({'dump_used': self.dump_id.name})
        logsio.info("Reloading")
        shell.odoo('reload')
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Downing")
        shell.odoo('kill')
        shell.odoo('rm', '-f')
        logsio.info(f"Restoring {self.dump_id.name}")
        shell.odoo('-f', 'restore', 'odoo-db', self.dump_id.name)
    
    def _docker_start(self, shell, task, logsio, **kwargs):
        shell.odoo('up', '-d')
        self._docker_get_state(shell)

    def _docker_stop(self, shell, task, logsio, **kwargs):
        shell.odoo('kill')
        self._docker_get_state(shell)

    def _docker_get_state(self, shell, **kwargs):
        info = shell.odoo('ps').output

        passed = False
        updated_containers = set()
        for line in info.split("\n"):
            if line.startswith("------"):
                passed = True
                continue
            if not passed: continue
            while "  " in line:
                line = line.replace("  ", " ")

            if line.startswith("Version:"):
                continue
            container_name = line.split(" ")[0]
            state = line.split(" ", 1)[-1].lower()
            if 'exit' in state:
                state = 'down'
            elif 'up' in state:
                state = 'up'
            else:
                state = False
            
            container = self.container_ids.filtered(lambda x: x.name == container_name)
            if not container:
                self.container_ids = [[0, 0, {
                    'name': container_name,
                    'state': state,
                }]]
            else:
                container.state = state
            updated_containers.add(container_name)
        for container in self.container_ids:
            if container.name not in updated_containers:
                container.unlink()

    def _turn_into_dev(self, shell, task, logsio, **kwargs):
        shell.odoo('turn-into-dev')

    def _reload(self, shell, task, logsio, project_name=None, **kwargs):
        self._make_instance_docker_configs(shell, forced_project_name=project_name) 
        shell.odoo('reload')

    def _build(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio, **kwargs)
        shell.odoo('build')

    def _dump(self, shell, task, logsio, **kwargs):
        volume = task.machine_id._get_volume('dumps')
        logsio.info(f"Dumping to {task.machine_id.name}:{volume}")
        filename = task.branch_id.backup_filename or (self.project_name + ".dump.gz")
        if '/' in filename:
            raise ValidationError("Filename mustn't contain slashses!")
        shell.odoo('backup', 'odoo-db', str(volume / filename))
        task.machine_id.update_dumps()

    def _update_git_commits(self, shell, logsio, force_instance_folder=None, force_commits=None, **kwargs):
        self.ensure_one()
        logsio.info(f"Updating commits for {self.project_name}")
        instance_folder = force_instance_folder or self._get_instance_folder(self.machine_id)
        with shell.shell() as shell:

            def _extract_commits():
                return list(filter(bool, shell.check_output([
                    "/usr/bin/git",
                    "log",
                    "--pretty=format:%H",
                    "--since='last 4 months'",
                ], cwd=instance_folder).strip().split("\n")))

            if force_commits:
                commits = force_commits
            else:
                commits = _extract_commits()

            all_commits = self.env['cicd.git.commit'].search([])
            all_commits = dict((x.name, x.branch_ids) for x in all_commits)

            for sha in commits:
                if sha in all_commits:
                    if self not in all_commits[sha]:
                        self.env['cicd.git.commit'].search([('name', '=', sha)]).branch_ids = [[4, self.id]]
                    continue

                env = update_env={
                    "TZ": "UTC0"
                }
                
                line = shell.check_output([
                    "/usr/bin/git",
                    "log",
                    sha,
                    "-n1",
                    "--pretty=format:%ct",
                ], cwd=instance_folder, update_env=env).strip().split(',')
                if not line or not any(line):
                    continue

                date = arrow.get(int(line[0]))

                info = shell.check_output([
                    "/usr/bin/git",
                    "log",
                    sha,
                    "--date=format:%Y-%m-%d %H:%M:%S",
                    "-n1",
                ], cwd=instance_folder, update_env=env).split("\n")

                def _get_item(name):
                    for line in info:
                        if line.strip().startswith(f"{name}:"):
                            return line.split(":", 1)[-1].strip()

                def _get_body():
                    for i, line in enumerate(info):
                        if not line:
                            return info[i + 1:]

                text = ('\n'.join(_get_body())).strip()
                self.commit_ids = [[0, 0, {
                    'name': sha,
                    'author': _get_item("Author"),
                    'date': date.strftime("%Y-%m-%d %H:%M:%S"),
                    'text': text,
                    'branch_ids': [[4, self.id]],
                }]]
    
    def _remove_web_assets(self, shell, task, logsio, **kwargs):
        logsio.info("Killing...")
        shell.odoo('kill')
        logsio.info("Calling remove-web-assets")
        shell.odoo('-f', 'remove-web-assets')
        logsio.info("Restarting...")
        shell.odoo('up', '-d')

    def _clear_db(self, shell, task, logsio, **kwargs):
        shell.odoo('cleardb')

    def _anonymize(self, shell, task, logsio, **kwargs):
        shell.odoo('update', 'anonymize')
        shell.odoo('anonymize')

    def _create_empty_db(self, shell, task, logsio, **kwargs):
        logsio.info("Reloading")
        shell.odoo('reload')
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Downing")
        shell.odoo('kill')
        shell.odoo('rm', '-f')
        shell.odoo('-f', 'db' 'reset')

    def _run_tests(self, shell, task, logsio, **kwargs):
        """
        If update_state is set, then the state is set to 'tested'
        """
        # try nicht unbedingt notwendig; bei __exit__ wird ein close aufgerufen
        b = task.branch_id

        update_state = kwargs.get('update_state', False)
        # self._update_git_commits(shell, task=task, logsio=logsio) # why???

        test_run = self.test_run_ids.create({
            'commit_id': self.commit_ids[0].id,
            'branch_id': b.id,
        })

        # use tempfolder for tests to not interfere with updates or so
        repo_path = task.branch_id.repo_id._get_main_repo(tempfolder=True, machine=shell.machine)
        shell.cwd = repo_path
        try:
            self.env.cr.commit()
            shell.X(["git", "checkout", "-f", test_run.commit_id.name])
            test_run.execute(shell, task, logsio)
            if update_state:
                if test_run.state == 'failed':
                    self.state = 'rework'
                else:
                    self.state = 'tested'
        finally:
            shell.rmifexists(repo_path)

    def _after_build(self, shell, logsio, **kwargs):
        shell.odoo("remove-settings", '--settings', 'web.base.url,web.base.url.freeze')
        shell.odoo("update-setting", 'web.base.url', shell.machine.external_url)
        shell.odoo("set-ribbon", self.name)
        shell.odoo("prolong")
        self._docker_get_state(shell=shell)

    def _build_since_last_gitsha(self, shell, logsio, **kwargs):
        # todo make button
        self._after_build(shell=shell, logsio=logsio, **kwargs)

    def _reset(self, task, shell, **kwargs):
        shell.odoo('db', 'reset', '--do-not-install-base')

    def _checkout_latest(self, shell, machine, logsio, **kwargs):
        logsio.write_text(f"Updating instance folder {self.name}")
        instance_folder = self._get_instance_folder(machine)
        root_folder = self.repo_id._get_main_repo(logsio=logsio)
        logsio.write_text(f"Cloning {self.name} to {instance_folder}")
        shell.X(["rsync", str(root_folder) + "/", str(instance_folder) + "/", "-ar", '--delete-after'])

        logsio.write_text(f"Checking out {self.name}")
        shell.X(["git", "checkout", "-f", self.name])

        logsio.write_text(f"Clean git")
        shell.X(["git", "clean", "-xdff"])

        logsio.write_text("Updating submodules")
        shell.X(["git", "submodule", "update", "--recursive"])

        logsio.write_text("Getting current commit")
        commit = shell.X(["git", "rev-parse", "HEAD"]).output.strip()
        logsio.write_text(commit)

        return str(commit)

    def inactivity_cycle_down(self):
        self.ensure_one()

        logsio = self._get_new_logsio_instance("inactivity_cycle_down")
        dest_folder = self.machine_id._get_volume('source') / self.project_name
        try:
            with self.machine_id._shellexec(dest_folder, logsio, project_name=self.project_name) as shell:
                if (arrow.get() - arrow.get(self.last_access or '1980-04-04')).total_seconds() > self.cycle_down_after_seconds:
                    self._docker_get_state(shell=shell)
                    if self.docker_state == 'up':
                        logsio.info(f"Cycling down instance due to inactivity")
                        shell.odoo('kill')

        except Exception as ex:
            logsio.error(ex)

    def _make_instance_docker_configs(self, shell, forced_project_name=None):
        with shell.shell() as ssh_shell:
            home_dir = shell._get_home_dir()
            machine = shell.machine
            project_name = forced_project_name or self.project_name
            content = (current_dir.parent / 'data' / 'template_cicd_instance.yml.template').read_text()
            ssh_shell.write_text(home_dir + f"/.odoo/docker-compose.{project_name}.yml", content.format(**os.environ))

            content = (current_dir.parent / 'data' / 'template_cicd_instance.settings').read_text()
            assert machine
            if not machine.postgres_server_id:
                raise ValidationError(_(f"Please configure a db server for {machine.name}"))
            content += "\n" + (self.reload_config or '')
            ssh_shell.write_text(home_dir + f'/.odoo/settings.{project_name}', content.format(
                branch=self,
                project_name=project_name,
                machine=machine
                ))

    def _cron_autobackup(self):
        for rec in self:
            rec._make_task("_dump")

    def _reset_db(self, shell, task, logsio, **kwargs):
        shell.odoo('reload')
        shell.odoo('build')
        shell.odoo('-f', 'db', 'reset')
        shell.odoo('update')

    def _compress(self, shell, task, logsio, compress_job_id):
        compressor = self.env['cicd.compressor'].sudo().browse(compress_job_id).sudo()
        source_host = compressor.source_volume_id.machine_id.effective_host
        # get list of files
        logsio.info("Identifying latest dump")
        with compressor.source_volume_id.machine_id._shellexec(logsio=logsio, cwd="") as source_shell:
            output = list(reversed(source_shell.X(["ls", "-tra", compressor.source_volume_id.name]).output.strip().split("\n")))
            for line in output:
                if line == '.' or line == '..': continue
                if re.findall(compressor.regex, line):
                    filename = line.strip()
                    break
            else:
                logsio.info("No files found.")
                return

        # if the machines are the same, then just rewrite destination path
        # if machines are different then copy locally and then put it on the machine
        dest_file_path = shell.machine._get_volume('dumps') / (self.project_name + "_compressor")
        with compressor.source_volume_id.machine_id._put_temporary_file_on_machine(
            logsio,
            compressor.source_volume_id.name + "/" + filename,
            shell.machine,
            dest_file_path,
        ) as effective_dest_file_path:
            compressor.last_input_size = int(shell.X(['stat', '-c', '%s', effective_dest_file_path]).output.strip())

            instance_path = self.repo_id._get_main_repo(tempfolder=True, logsio=logsio, machine=shell.machine)
            assert shell.machine.ttype == 'dev'
            # change working project/directory
            project_name = self.project_name + "_compressor_" + str(compressor.id)
            with shell.machine._shellexec(instance_path, logsio=logsio, project_name=project_name) as shell2:
                try:
                    logsio.info(f"Reloading...")
                    self._reload(shell2, task, logsio, project_name=project_name)
                    logsio.info(f"Restoring {effective_dest_file_path}...")
                    shell2.odoo("-f", "restore", "odoo-db", effective_dest_file_path, allow_error=False)
                    logsio.info(f"Clearing DB...")
                    shell2.odoo('-f', 'cleardb', allow_error=False)
                    if compressor.anonymize:
                        logsio.info(f"Anonymizing DB...")
                        shell2.odoo('-f', 'anonymize', allow_error=False)
                    logsio.info(f"Dumping compressed dump")
                    output_path = compressor.volume_id.name + "/" + compressor.output_filename
                    shell2.odoo('backup', 'odoo-db', output_path, allow_error=False)
                    compressor.last_input_size = int(shell2.X(['stat', '-c', '%s', output_path]).output.strip())
                    compressor.date_last_success = fields.Datetime.now()

                finally:
                    shell.rmifexists(instance_path)