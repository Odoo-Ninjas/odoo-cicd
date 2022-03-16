import re
import uuid
import psycopg2
from odoo import fields
from pathlib import Path
import os
import arrow
from odoo import _, api, models, fields
from odoo.exceptions import UserError, RedirectWarning, ValidationError
import inspect
from pathlib import Path
from odoo.addons.queue_job.exception import RetryableJobError
import logging
current_dir = Path(os.path.dirname(os.path.abspath(inspect.getfile(inspect.currentframe()))))

logger = logging.getLogger(__name__)

class Branch(models.Model):
    _inherit = 'cicd.git.branch'

    def _prepare_a_new_instance(self, shell, task, logsio, **kwargs):
        dump = self.dump_id or self.repo_id.default_simulate_install_id_dump_id
        if not dump:
            self._reset_db(shell, task, logsio, **kwargs)
        else:
            self.backup_machine_id = dump.machine_id
            self.dump_id = dump
        self._restore_dump(shell, task, logsio, **kwargs)
        self._update_all_modules(shell, task, logsio, **kwargs)

    def _update_odoo(self, shell, task, logsio, **kwargs):
        if self.block_updates_until and \
                self.block_updates_until > fields.Datetime.now():
            raise RetryableJobError(
                "Branch is blocked - have to wait",
                seconds=10, ignore_retry=True
                )

        tasks = self.task_ids.with_context(prefetch_fields=False).filtered(
            lambda x: x.state == 'done' and x.name in [
                '_update_all_modules', '_update_odoo'])
        commit = None
        if tasks:
            commit = tasks[0].commit_id.name
        if commit:
            try:
                logsio.info("Updating")
                result = shell.odoo(
                    "update", "--since-git-sha", commit, "--no-dangling-check")

                if result['exit_code']:
                    raise Exception("Error at update")
            except Exception as ex:
                logger.error(ex)
                logsio.error(ex)
                logsio.info((
                    "Running full update now - "
                    f"update since sha {commit} did not succeed"))
                self._update_all_modules(shell=shell, task=task, logsio=logsio, **kwargs)
        else:
            self._update_all_modules(
                shell=shell, task=task, logsio=logsio, **kwargs)

    def _update_all_modules(self, shell, task, logsio, **kwargs):
        logsio.info("Reloading")
        self._reload(shell, task, logsio)
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Updating")
        shell.odoo('update', "--no-dangling-check")
        logsio.info("Upping")
        shell.odoo("up", "-d")

    def _update_installed_modules(self, shell, task, logsio, **kwargs):
        logsio.info("Reloading")
        self._reload(shell, task, logsio)
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Updating")
        shell.odoo('update', "--no-dangling-check", "--installed-modules")
        logsio.info("Upping")
        shell.odoo("up", "-d")

    def _reload_and_restart(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio)
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Upping")
        shell.odoo("kill")
        self._kill_tmux_sessions(shell)
        shell.odoo("rm")
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
        shell.odoo('rm')
        logsio.info(f"Restoring {self.dump_id.name}")
        shell.odoo(
            '-f', 'restore', 'odoo-db', '--no-remove-webassets',
            self.dump_id.name)
        if self.remove_web_assets_after_restore:
            shell.odoo('-f', 'remove-web-assets')

    def _docker_start(self, shell, task, logsio, **kwargs):
        shell.odoo('up', '-d')
        self.repo_id.machine_id._update_docker_containers()
        self._docker_get_state()

    def _docker_stop(self, shell, task, logsio, **kwargs):
        shell.odoo('kill')
        self.repo_id.machine_id._update_docker_containers()
        self._docker_get_state()

    def _docker_remove(self, shell, task, logsio, **kwargs):
        shell.odoo('rm')
        self.repo_id.machine_id._update_docker_containers()
        self._docker_get_state()

    def _docker_get_state(self, **kwargs):
        breakpoint()
        containers = self.mapped('repo_id.machine_id')._get_containers()
        for rec in self:
            updated_containers = set()
            for container_name in containers:
                if container_name.startswith(rec.project_name):
                    container_state = containers[container_name]
                    state = 'up' if container_state == 'running' else 'down'

                    container = rec.container_ids.filtered(
                        lambda x: x.name == container_name
                    )
                    if not container:
                        rec.container_ids = [[0, 0, {
                            'name': container_name,
                            'state': state,
                        }]]
                    else:
                        if container.state != state:
                            container.state = state
                    updated_containers.add(container_name)
                del container_name

            for container in rec.container_ids:
                if container.name not in updated_containers:
                    container.unlink()

    def _turn_into_dev(self, shell, task, logsio, **kwargs):
        shell.odoo('turn-into-dev')

    def _reload(
            self, shell, task, logsio,
            project_name=None, settings=None, commit=None, **kwargs
            ):

        with shell.clone(
            cwd=self._make_sure_source_exists(shell, logsio)
        ) as shell:
            self._make_instance_docker_configs(
                shell, forced_project_name=project_name, settings=settings)
            self._collect_all_files_by_their_checksum(shell)
            if commit:
                shell.checkout_commit(commit)
            shell.odoo('reload')

    def _build(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio, **kwargs)
        shell.odoo('build')

    def _dump(self, shell, task, logsio, **kwargs):
        volume = task.machine_id._get_volume('dumps')
        logsio.info(f"Dumping to {task.machine_id.name}:{volume}")
        filename = task.branch_id.backup_filename or (
            self.project_name + ".dump.gz")
        if '/' in filename:
            raise ValidationError("Filename mustn't contain slashses!")
        shell.odoo('backup', 'odoo-db', str(volume / filename))
        # to avoid serialize access erros which may occur
        task.machine_id.with_delay().update_dumps()

    def _update_git_commits(
            self, shell, logsio,
            force_instance_folder=None, **kwargs
            ):

        self.ensure_one()
        logsio.info(f"Updating commits for {self.project_name}")
        instance_folder = force_instance_folder or self._get_instance_folder(
            self.machine_id)

        def _extract_commits():
            # removing the 4 months filter:
            # old branches get stuck and stuck other branches because
            # latest commit # cannot be found, if that filter is active.
            return list(filter(bool, shell.X([
                "git",
                "log",
                "--pretty=format:%H___%ct",
                "-n", str(self.repo_id.analyze_last_n_commits),
                # "--since='last 4 months'",
            ], logoutput=False, cwd=instance_folder)[
                'stdout'].strip().split("\n")))

        commits = _extract_commits()
        commits = [list(x.split("___")) for x in commits]
        for commit in commits:
            commit[1] = arrow.get(int(commit[1]))

        all_commits = self.env['cicd.git.commit'].with_context(
            active_test=False).search([])
        all_commits = dict((x.name, x) for x in all_commits)

        for icommit, commit in enumerate(commits):
            sha, date = commit
            if sha in all_commits:
                cicd_commit = all_commits[sha]
                if self not in cicd_commit.branch_ids:
                    # memory error otherwise - reported by MT
                    # cicd_commit.branch_ids |= self
                    self.commit_ids |= cicd_commit
                continue

            env = {
                "TZ": "UTC0"
            }
            if date is None:
                line = shell.X([
                    "git",
                    "log",
                    sha,
                    "-n1",
                    "--pretty=format:%ct",
                ], logoutput=False, cwd=instance_folder, env=env)[
                    'stdout'].strip().split(',')

                if not line or not any(line):
                    continue
                date = arrow.get(int(line[0]))

            logsio.info((
                f"Getting detail information of sha "
                f"{sha} ({icommit} / {len(commits)})"))

            info = shell.X([
                "git",
                "log",
                sha,
                "--date=format:%Y-%m-%d %H:%M:%S",
                "-n1",
            ], logoutput=False, cwd=instance_folder, env=env)[
                'stdout'].strip().split("\n")

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

    def _shrink_db(self, shell, task, logsio, **kwargs):
        shell.odoo('cleardb')

    def _anonymize(self, shell, task, logsio, **kwargs):
        shell.odoo('update', 'anonymize')
        shell.odoo('anonymize')

    def _create_empty_db(self, shell, task, logsio, **kwargs):
        logsio.info("Reloading")
        self._reload(shell, task, logsio)
        shell.odoo('reload')
        logsio.info("Building")
        shell.odoo('build')
        logsio.info("Downing")
        shell.odoo('kill')
        shell.odoo('rm')
        shell.odoo('-f', 'db', 'reset')

    def _run_tests(self, shell, task, logsio, **kwargs):
        """
        If update_state is set, then the state is set to 'tested'
        """
        # try nicht unbedingt notwendig; bei __exit__ wird ein close aufgerufen
        breakpoint()
        b = task.branch_id

        if task.testrun_id:
            test_run = task.testrun_id
        else:
            test_run = self.test_run_ids.filtered(
                lambda x: x.commit_id ==
                    self.latest_commit_id and x.state == 'open')

        if not test_run:
            test_run = self.test_run_ids.filtered(
                lambda x: x.commit_id == self.latest_commit_id)
            if test_run:
                test_run = test_run.filtered(lambda x: x.state == 'failed')
                if test_run:
                    test_run[0].state = 'open'

            if not test_run:
                test_run = self.test_run_ids.create({
                    'commit_id': self.latest_commit_id.id,
                    'branch_id': b.id,
                })

                # so that it is available in sub cr in testrun execute
                self.env.cr.commit()
        else:
            test_run = test_run.filtered(lambda x: x.state == 'open')

        if test_run:
            test_run[0].execute(shell, task, logsio)

    def _after_build(self, shell, logsio, **kwargs):
        shell.odoo(
            "remove-settings", '--settings', 'web.base.url,web.base.url.freeze'
            )
        shell.odoo(
            "update-setting", 'web.base.url', shell.machine.external_url)
        shell.odoo("set-ribbon", self.name)
        shell.odoo("prolong")
        self._docker_get_state(shell=shell)

    def _build_since_last_gitsha(self, shell, logsio, **kwargs):
        # todo make button
        self._after_build(shell=shell, logsio=logsio, **kwargs)

    @api.model
    def _cron_garbage_collect(self):
        for branch in self.search([('garbage_collect', '=', True)]):
            branch.garbage_collect()

    def _gc(self, shell, logsio, **kwargs):
        logsio.write_text("Compressing git")
        shell.X(["git", "gc", "--aggressive", "--prune=now"])

    def _clone_instance_folder(self, machine, logsio):
        instance_folder = self._get_instance_folder(machine)
        self.repo_id._get_main_repo(
            logsio=logsio,
            destination_folder=instance_folder,
            limit_branch=self.name,
            machine=machine,
        )
        return instance_folder

    def _checkout_latest(self, shell, logsio, machine=None, **kwargs):
        machine = machine or shell.machine
        logsio.write_text(f"Updating instance folder {self.name}")
        instance_folder = self._clone_instance_folder(machine, logsio)
        logsio.write_text(f"Cloning {self.name} to {instance_folder}")

        with shell.clone(cwd=instance_folder) as shell:
            logsio.write_text(f"Checking out {self.name}")
            try:
                shell.X(["git", "checkout", "-f", self.name])
            except Exception as ex:
                logsio.error(ex)
                shell.rm(instance_folder)
                raise RetryableJobError(
                    "Cleared directory - branch not found - please retry",
                    ignore_retry=True) from ex

            try:
                shell.X(["git", "pull"])
            except Exception as ex:
                logsio.error((
                    "Error at pulling,"
                    f"cloning path {instance_folder} again:\n{ex}"))
                shell.rm(instance_folder)
                instance_folder = self._clone_instance_folder(machine, logsio)

            # delete all other branches:
            res = shell.X(["git", "branch"])['stdout'].strip().split("\n")
            for branch in list(filter(lambda x: '* ' not in x, res)):
                branch = self.repo_id._clear_branch_name(branch)

                if branch == self.name:
                    continue

                shell.X(["git", "branch", "-D", branch])
                del branch

            current_branch = list(filter(lambda x: '* ' in x, shell.X([
                "git", "branch"])['stdout'].strip().split("\n")))
            if not current_branch:
                raise Exception(f"Somehow no current branch found")
            branch_in_dir = self.repo_id._clear_branch_name(current_branch[0])
            if branch_in_dir != self.name:
                shell.rm(instance_folder)

                raise Exception((
                    f"Branch could not be checked out!"
                    f"Was {branch_in_dir} - but should be {self.name}"
                ))

            logsio.write_text(f"Clean git")
            shell.X(["git", "clean", "-xdff"])

            logsio.write_text("Updating submodules")
            shell.X(["git", "submodule", "update", "--recursive", "--init"])

            logsio.write_text("Getting current commit")
            commit = shell.X(["git", "rev-parse", "HEAD"])['stdout'].strip()
            logsio.write_text(commit)

        return str(commit)

    def inactivity_cycle_down(self):
        machines = self.mapped('repo_id.machine_id')
        for machine in machines:
            for rec in self.filtered(
                    lambda x: x.repo_id.machine_id == machine):
                with rec.machine_id._shell(
                    cwd=rec.project_path,
                    project_name=rec.project_name
                ) as shell:
                    last_access = arrow.get(rec.last_access or '1980-04-04')
                    uptime = (arrow.get() - last_access).total_seconds()
                    if uptime > rec.cycle_down_after_seconds:
                        rec._docker_get_state(shell=shell, now=True)
                        if rec.docker_state == 'up':
                            shell.logsio.info((
                                "Cycling down instance "
                                f"{rec.name} due to inactivity"
                            ))
                            shell.odoo('kill')
                            shell.odoo('rm', allow_error=True)

    def _make_instance_docker_configs(
            self, shell, forced_project_name=None, settings=None):

        home_dir = shell._get_home_dir()
        machine = shell.machine
        project_name = forced_project_name or self.project_name
        content = ((
            current_dir.parent
            / 'data'
            / 'template_cicd_instance.yml.template')).read_text()

        values = os.environ.copy()
        values['PROJECT_NAME'] = project_name
        content = content.format(**values)
        shell.put(content, (
            f"{home_dir}"
            f"/.odoo/docker-compose.{project_name}.yml"
            ))

        content = ((
            current_dir.parent
            / 'data'
            / 'template_cicd_instance.settings')).read_text()
        assert machine

        if not machine.postgres_server_id:
            raise ValidationError(
                _(f"Please configure a db server for {machine.name}"))

        content += "\n" + (self.reload_config or '')
        if settings:
            content += "\n" + settings

        shell.put(
            content.format(
                branch=self,
                project_name=project_name,
                machine=machine),

            home_dir + f'/.odoo/settings.{project_name}'
            )

    def _cron_autobackup(self):
        for rec in self:
            rec._make_task("_dump", machine=rec.backup_machine_id)

    def _reset_db(self, shell, task, logsio, **kwargs):
        self._reload(shell, task, logsio)
        shell.odoo('build')
        shell.odoo('-f', 'db', 'reset')
        shell.odoo('update')
        # shell.odoo('turn-into-dev')
        self._after_build(shell=shell, logsio=logsio, **kwargs)

    def _compress(self, shell, task, logsio, compress_job_id):
        self.ensure_one()
        compressor = self.env['cicd.compressor'].sudo().browse(
            compress_job_id).sudo()

        # get list of files

        logsio.info("Identifying latest dump")
        with compressor.source_volume_id.machine_id._shell(
                logsio=logsio, cwd="") as source_shell:
            output = list(reversed(source_shell.X([
                "ls", "-trA", compressor.source_volume_id.name])[
                    'stdout'].strip().split("\n")))

            for line in output:
                if line == '.' or line == '..':
                    continue
                if re.findall(compressor.regex, line):
                    filename = line.strip()
                    break
            else:
                logsio.info("No files found.")
                return

        # if the machines are the same, then just rewrite destination path
        # if machines are different then copy locally and then put it on the
        # machine
        appendix = "_compressor"
        if compressor.anonymize:
            appendix += '_anonymize'
        self = self.with_context(testrun=appendix)
        dest_file_path = shell.machine._get_volume('dumps') / self.project_name

        # release db resources:
        self.env.cr.commit()

        with compressor.source_volume_id.machine_id._put_temporary_file_on_machine(
            logsio,
            compressor.source_volume_id.name + "/" + filename,
            shell.machine,
            dest_file_path,
        ) as effective_dest_file_path:
            compressor.last_input_size = int(shell.X([
                'stat', '-c', '%s', effective_dest_file_path])[
                    'stdout'].strip())

            assert shell.machine.ttype == 'dev'

            with self.repo_id._temp_repo(
                machine=shell.machine, branch=compressor.branch_id.name,
                pull=True,
            ) as instance_path:
                # change working project/directory
                with shell.clone(
                    cwd=instance_path,
                    project_name=self.project_name
                ) as shell:

                    logsio.info("Reloading...")
                    settings = (
                        "\n"
                        f"DBNAME={self.database_project_name}\n"
                        "RUN_CRONJOBS=0\n"
                        "RUN_QUEUEJOBS=0\n"
                        "RUN_POSTGRES=1\n"
                        "RUN_PROXY_PUBLISHED=0\n"
                        "DB_HOST=postgres\n"
                        "DB_USER=odoo\n"
                        "DB_PWD=odoo\n"
                        "DB_PORT=5432\n"
                    )
                    self._reload(
                        shell, task, logsio,
                        project_name=self.project_name, settings=settings)
                    logsio.info(f"Restoring {effective_dest_file_path}...")
                    shell.odoo("up", "-d", "postgres")
                    try:
                        shell.odoo(
                            "-f", "restore", "odoo-db",
                            effective_dest_file_path)
                        logsio.info("Clearing DB...")
                        shell.odoo('-f', 'cleardb')
                        if compressor.anonymize:
                            logsio.info("Anonymizing DB...")
                            shell.odoo('-f', 'anonymize')
                        logsio.info("Dumping compressed dump")
                        output_path = compressor.volume_id.name + "/" + \
                            compressor.output_filename
                        shell.odoo('backup', 'odoo-db', output_path)
                        compressor.last_output_size = int(shell.X([
                            'stat', '-c', '%s', output_path])[
                                'stdout'].strip())
                        compressor.date_last_success = fields.Datetime.now()
                    finally:
                        shell.odoo('down', '-v', force=True, allow_error=True)

    def _make_sure_source_exists(self, shell, logsio):
        instance_folder = self._get_instance_folder(shell.machine)
        self.ensure_one()
        if not self.repo_id._is_healthy_repository(shell, instance_folder):
            try:
                self._checkout_latest(shell, logsio=logsio)
            except Exception:
                shell.rm(instance_folder)
                self._checkout_latest(shell, logsio=logsio)
        return instance_folder

    def _collect_all_files_by_their_checksum(self, shell):
        """

        STOP!
        odoo calls isdir on symlink which fails unfortunately
        hardlink on dir does not work
        so new idea needed

        Odoo stores its files by sha. If a db is restored then usually
        it has to rebuild the assets.
        And files are not available.
        To save space we make the following:

        ~/.odoo/files/filestore/project_name1/00/000000000
        ~/.odoo/files/filestore/project_name2/00/000000000

                           |
                           |
                           |
                          \ /
                           W

        ~/.odoo/files/filestore/_all/00/000000000
        ~/.odoo/files/filestore/_all/00/000000000
        ~/.odoo/files/filestore/project_name1 --> ~/.odoo/files/filestore/_all
        ~/.odoo/files/filestore/project_name2 --> ~/.odoo/files/filestore/_all
        """
        return # see comment

        python_script_executed = """
from pathlib import Path
import os
import subprocess
import shutil
base = Path(os.path.expanduser("~/.odoo/files/filestore"))

ALL_FOLDER = base / "_all"
ALL_FOLDER.mkdir(exist_ok=True, parents=True)

for path in base.glob("*"):
    if path == ALL_FOLDER:
        continue
    if path.is_dir():
        subprocess.check_call(["rsync", str(path) + "/", str(ALL_FOLDER) + "/", "-ar"])
        shutil.rmtree(path)
        path.symlink_to(ALL_FOLDER, target_is_directory=True)

        """
        shell.put(python_script_executed, '.cicd_reorder_files')
        try:
            shell.X(["python3", '.cicd_reorder_files'])
        finally:
            shell.rm(".cicd_reorder_files")

    def _kill_tmux_sessions(self, shell):
        for rec in self:
            machine = rec.repo_id.machine_id
            with machine._shell() as shell:
                machine.make_login_possible_for_webssh_container()

                test = shell.X([
                    "sudo", "pkill", "-9", "-f",
                    "-u", shell.machine.ssh_user_cicdlogin,
                    f'new-session.*-s.*{rec.project_name}'
                    ], allow_error=True)
