import tempfile
import arrow
import uuid
from contextlib import contextmanager
import threading
from sarge import Capture, run
from odoo.exceptions import UserError
import time
from copy import deepcopy
from pathlib import Path
from ..tools.logsio_writer import LogsIOWriter
import logging
logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 6 * 3600
DEFAULT_ENV = {
    'BUILDKIT_PROGRESS': 'plain',
}


def duration(d):
    return (arrow.get() - d).total_seconds()


class ShellExecutor(object):
    class TimeoutConnection(Exception):
        pass

    class TimeoutFinished(Exception):
        pass

    def __init__(
        self, ssh_keyfile, machine,
        cwd, logsio, project_name=None, env=None, user=None
    ):
        self.machine = machine
        self._cwd = Path(cwd) if cwd else None
        self.logsio = logsio
        self.env = env or {}
        self.project_name = project_name
        if machine:
            assert machine._name == 'cicd.machine'
        if logsio:
            assert isinstance(logsio, LogsIOWriter)
        if project_name:
            assert isinstance(project_name, str)
        if env:
            assert isinstance(env, dict)
        self.ssh_keyfile = ssh_keyfile
        self.user = user

    @contextmanager
    def clone(self, cwd=None, env=None, user=None, project_name=None):
        env2 = deepcopy(self.env)
        env2.update(env or {})
        user = user or self.user
        cwd = cwd or self.cwd
        project_name = project_name or self.project_name
        shell2 = ShellExecutor(
            self.ssh_keyfile, self.machine, cwd,
            self.logsio, project_name, env2, user=user
        )
        yield shell2

    @property
    def cwd(self):
        return self._cwd

    def exists(self, path):
        try:
            res = self._internal_execute(["stat", path], logoutput=False)
        except Exception:
            res = self._internal_execute(["stat", path], logoutput=False)
        return res['exit_code'] == 0

    def rm(self, path):
        return self.remove(path)

    def grab_folder_as_tar(self, path):
        if not self.exists(path):
            return None
        filename = Path(tempfile.mktemp())
        self._internal_execute(["tar", "cfz", filename, '.'], cwd=path)
        content = self.get(filename)
        self.remove(filename)
        return content

    def remove(self, path):
        if self.exists(path):
            if self.logsio:
                self.logsio.info(f"Path {path} exists and is erased now.")
            self._internal_execute(["rm", "-Rf", path])
            if self.exists(path):
                raise UserError(f"Removing of {path} failed.")
        else:
            if self.logsio:
                if not str(path).startswith("/tmp"):
                    self.logsio.info(f"Path {path} did not exist - not erased")

    def _get_home_dir(self):
        with self.machine._shell() as shell:
            res = shell.X(
                ['echo', '$HOME'],
            )['stdout'].strip()
        if res.endswith("/~"):
            res = res[:-2]
        return res

    def odoo(self, *cmd, allow_error=False, force=False, timeout=None):
        env = {
            'NO_PROXY': "*",
            'DOCKER_CLIENT_TIMEOUT': "600",
            'COMPOSE_HTTP_TIMEOUT': "600",
            'PSYCOPG_TIMEOUT': "120",
        }
        if not self.project_name:
            raise Exception("Requires project_name for odoo execution")

        cmd = ["odoo", "--project-name", self.project_name] + list(cmd)
        if force:
            cmd.insert(1, "-f")
        res = self.X(cmd, allow_error=allow_error, env=env, timeout=timeout)
        if res['exit_code'] and not allow_error or res['exit_code'] is None:
            if '.FileNotFoundError: [Errno 2] No such file or directory:' in \
                    res['stderr']:
                raise Exception((
                    "Seems that a reload of the instance is required."
                ))
            else:
                raise Exception(res['stdout'])
        return res

    def checkout_branch(self, branch, cwd=None):
        cwd = cwd or self.cwd
        with self.clone(cwd=cwd) as self:
            if not self.branch_exists(branch):
                self.logsio and self.logsio.info(
                    f"Tracking remote branch and checking out {branch}")
                self.X([
                    "git", "checkout", "-b", branch,
                    "--track", "origin/" + branch], allow_error=True)

            self.logsio and self.logsio.info(
                f"Checking out {branch} regularly")
            self.X([
                "git", "checkout", "-f",
                "--no-guess", branch], allow_error=False)
            self.logsio and self.logsio.info(f"Checked out {branch}")
            self._after_checkout()

    def checkout_commit(self, commit, cwd=None):
        cwd = cwd or self.cwd
        with self.clone(cwd=cwd) as self:
            # otherwise checking out a commit brings error message
            self.X(["git", "config", "advice.detachedHead", "false"])
            self.X(["git", "clean", "-xdff", commit])
            self.X(["git", "checkout", "-f", commit])
            sha = self.X(["git", "log", "-n1", "--format=%H"])[
                'stdout'].strip()
            if sha != commit:
                raise Exception((
                    "Somehow checking out "
                    f"{commit} in {cwd} failed"))
            self._after_checkout()

    def branch_exists(self, branch, cwd=None):
        res = self.X(["git", "branch", "--no-color"], cwd=cwd)[
            'stdout'].strip().split("\n")

        def reformat(x):
            x = x.replace("* ", "")
            x = x.strip()
            return x
        res = [reformat(x) for x in res]
        return branch in res

    def _after_checkout(self):
        self.logsio and self.logsio.info("Cleaning git...")
        self.X([
            "git", "clean", "-xdff"])
        self.logsio and self.logsio.info("Updating submodules...")
        self.X([
            "git", "submodule", "update", "--init", "--force", "--recursive"])
        self.logsio and self.logsio.info("_after_checkout finished.")

    def X(
        self, cmd, allow_error=False, env=None,
        cwd=None, logoutput=True, timeout=None
    ):
        effective_env = deepcopy(self.env)
        if env:
            effective_env.update(env)
        res = self._internal_execute(
            cmd, cwd=cwd, env=env,
            logoutput=logoutput, timeout=timeout)
        if not allow_error:
            if res['exit_code'] is None:
                raise Exception("Timeout happend: {cmd}")
            if res['exit_code']:
                raise Exception(
                    f"Error happened: {res['exit_code']}:\n"
                    f"{res['stderr']}\n"
                    f"{res['stdout']}"
                    )

        return res

    def get(self, source):
        # not tested yet
        filename = Path(tempfile.mktemp(suffix='.'))

        cmd, host = self._get_ssh_client('scp', split_host=True)
        capt = Capture()
        p = run(
            cmd + f" '{host}:{source}' '{filename}'",
            stdout=capt, stderr=capt)
        if p.commands[0].returncode:
            raise Exception("Copy failed")
        try:
            return filename.read_bytes()
        finally:
            if filename.exists():
                filename.unlink()

    def put(self, content, dest):
        filename = Path(tempfile.mktemp(suffix='.'))
        if isinstance(content, str):
            content = content.encode('utf-8')
        filename.write_bytes(content)
        try:
            cmd, host = self._get_ssh_client('scp', split_host=True)
            capt = Capture()
            p = run(
                cmd + f" '{filename}' '{host}:{dest}'",
                stdout=capt, stderr=capt)
            if p.commands[0].returncode:
                raise Exception(f"Transfer failed to {host}:{dest}")
        finally:
            filename.unlink()

    def _get_ssh_client(self, cmd='ssh', split_host=False):
        host = self.machine.effective_host
        user = self.user or self.machine.ssh_user
        base = f"{cmd} -T -oStrictHostKeyChecking=no -i {self.ssh_keyfile}"
        user_host = f"{user}@{host}"
        if split_host:
            return base, user_host
        return base + " " + user_host + " "

    def _internal_execute(
        self, cmd, cwd=None, env=None, logoutput=True, timeout=None
    ):

        if timeout is None:
            timeout = DEFAULT_TIMEOUT

        # region: conversions
        def convert_path(x):
            if isinstance(x, Path):
                x = str(x)
            return x

        cmd = list(map(convert_path, cmd))

        if isinstance(cmd, (tuple, list)):
            cmd = f"{cmd[0]} " + " ".join(map(lambda x: f'"{x}"', cmd[1:]))
        cmd = cmd.replace('\n', ' ')
        # endregion

        # region: Writer Class
        class MyWriter(object):
            def __init__(self, ttype, logsio, logoutput):
                self.text = [""]
                self.ttype = ttype
                self.line = ""
                self.logsio = logsio
                self.logoutput = logoutput
                self.filepath = tempfile.mktemp(suffix='shellwriter.log')
                self.file = open(self.filepath, 'a+')

            def __del__(self):
                self.cleanup()

            def cleanup(self):
                if not self.file.closed:
                    self.file.close()

            def get_all_lines(self):
                self.file.seek(0)
                return self.file.read()

            def write(self, line):
                if line is None:
                    return
                line = line.decode("utf-8")
                self.file.write(line)
                if line.endswith("\n"):
                    line = line[:-1]
                if logoutput and self.logsio:
                    if self.ttype == 'error':
                        self.logsio.error(line)
                    else:
                        self.logsio.info(line)

        stdwriter = MyWriter('info', self.logsio, logoutput)
        errwriter = MyWriter('error', self.logsio, logoutput)

        # endregion

        sshcmd = self._get_ssh_client()
        stop_marker = str(uuid.uuid4()) + str(uuid.uuid4())
        start_marker = str(uuid.uuid4()) + str(uuid.uuid4())

        # region: start collecting threads
        stdout = Capture(buffer_size=-1)  # line buffering
        stderr = Capture(buffer_size=-1)  # line buffering
        data = {
            'stop': False,
            'started': False,
            'stop_marker': False,
        }

        def on_started():
            data['started'] = True

        def on_stop_marker():
            data['stop_marker'] = arrow.get()

        def collect(
            capture, writer, marker=None, on_marker=None,
            stop_marker=None, on_stop_marker=None
        ):

            while not data['stop']:
                for line in capture:
                    line_decoded = line.decode('utf-8')
                    is_marker = False
                    if marker and marker in line_decoded and on_started:
                        on_marker()
                        is_marker = True
                    if stop_marker and stop_marker in line_decoded and \
                            on_stop_marker:
                        on_stop_marker()
                        is_marker = True

                    if not is_marker:
                        writer.write(line)

        tstd = threading.Thread(target=collect, args=(
            stdout, stdwriter, start_marker,
            on_started, stop_marker, on_stop_marker))

        terr = threading.Thread(target=collect, args=(stderr, errwriter))
        tstd.daemon = True
        terr.daemon = True
        [x.start() for x in [tstd, terr]]  # NOQA
        # endregion

        # region: build command chain
        bashcmd = (
            "#!/bin/bash\n"
            "set -o pipefail\n"
        )

        cwd = cwd or self.cwd
        if cwd:
            bashcmd += f"cd '{cwd}' || exit 15\n"

        effective_env = deepcopy(DEFAULT_ENV)
        if self.env:
            effective_env.update(self.env)
        if env:
            effective_env.update(env)

        for k, v in effective_env.items():
            bashcmd += f'export {k}="{v}"\n'

        bashcmd += (
            f"echo '{start_marker}'\n"
            f"set -e\n"
            f"{cmd} | cat -\n"
            f"echo\n"
            f"echo 1>&2\n"
            f"echo '{stop_marker}' \n"
        )
        # endregion

        # region: run command
        p = run(
            sshcmd, async_=True, stdout=stdout,
            stderr=stderr, env=effective_env, input=bashcmd)
        deadline_started = arrow.get().shift(seconds=10)
        while True:
            if p.returncodes and any(x is not None for x in p.returncodes):
                break
            if arrow.get() > deadline_started:
                raise ShellExecutor.TimeoutConnection()
            if data['started']:
                break
            if p.commands:
                p.commands[0].poll()

        deadline = arrow.get().shift(seconds=timeout)
        timeout_happened = False
        try:
            if not p.commands:
                raise Exception(f"Command failed: {cmd}")
            while True:
                p.commands[0].poll()

                if p.commands[0].returncode is not None and \
                        not p.commands[0].returncode and \
                        data.get('stop_marker'):
                    # Perfect End
                    break

                if p.commands[0].returncode is not None and \
                        p.commands[0].returncode:
                    break

                if arrow.get() > deadline:
                    p.commands[0].kill()
                    timeout_happened = True
                    p.commands[0].kill()
                    break
                time.sleep(0.05)

                if data.get('stop_marker'):
                    if duration(
                            data['stop_marker']) > 10 and not p.returncodes:
                        break
                if p.commands[0].returncode is not None and \
                        not data.get('stop_marker'):
                    data.setdefault("waiting_for_stop", arrow.get())
                    if duration(data['waiting_for_stop']) > 5:
                        break

        finally:
            data['stop'] = True
        tstd.join()
        terr.join()
        # endregion

        # region: evaluate run result
        stdout = stdwriter.get_all_lines()
        stderr = errwriter.get_all_lines()

        stdwriter.cleanup()
        errwriter.cleanup()

        if p.returncodes:
            return_code = p.returncodes[0]
        elif data.get('stop_marker'):
            # script finished but ssh didnt get it
            return_code = 0
            if stderr.endswith("\n"):
                stderr = stderr[:-1]
        else:
            raise ShellExecutor.TimeoutFinished()

        # remove last line from bashcmd if good:
        if return_code == 0 and stdout.endswith("\n"):
            stdout = stdout[:-1]
        # endregion

        return {
            'timeout': timeout_happened,
            'exit_code': p.commands[0].returncode,
            'stdout': stdout,
            'stderr': stderr,
        }