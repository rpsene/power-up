#!/usr/bin/env python
"""Container"""

# Copyright 2017 IBM Corp.
#
# All Rights Reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

from __future__ import nested_scopes, generators, division, absolute_import, \
    with_statement, print_function, unicode_literals

import logging
import os.path
import re
import platform
import ConfigParser
from enum import Enum
from orderedattrdict import AttrDict
from Crypto.PublicKey import RSA
import lxc

from lib.logger import Logger
from lib.config import Config
from lib.exception import UserException
from lib.ssh import SSH_CONNECTION, SSH_Exception
import lib.genesis as gen


class Container(object):
    """Container"""

    class Packages(Enum):
        DISTRO = 'pkgs-distro'
        DISTRO_AMD64 = 'pkgs-distro-amd64'
        DISTRO_PPC64EL = 'pkgs-distro-ppc64el'
        PIP = 'pkgs-pip'
        VENV = 'pkgs-pip-venv'

    ROOTFS = AttrDict({'dist': 'ubuntu', 'release': 'trusty', 'arch': None})
    ARCHITECTURE = {u'x86_64': 'amd64', u'ppc64le': 'ppc64el'}
    LXC_USERNET = '/etc/lxc/lxc-usernet'
    RESOLV_CONF = '/etc/resolv.conf'
    RESOLV_CONF_BASE = '/etc/resolvconf/resolv.conf.d/base'
    RSA_BIT_LENGTH = 2048
    PRIVATE_SSH_KEY_FILE = os.path.expanduser('~/.ssh/gen')
    PUBLIC_SSH_KEY_FILE = os.path.expanduser('~/.ssh/gen.pub')
    DEFAULT_CONTAINER_NAME = 'cluster-genesis'

    def __init__(self):
        self.log = logging.getLogger(Logger.LOG_NAME)
        self.cfg = Config()

        self.cont_package_path = gen.get_container_package_path()
        self.cont_id_file = gen.get_container_id_file()
        self.cont_venv_path = gen.get_container_venv_path()
        self.cont_scripts_path = gen.get_container_scripts_path()
        self.cont_python_path = gen.get_container_python_path()
        self.depl_package_path = gen.get_package_path()
        self.depl_python_path = gen.get_python_path()
        self.config_file = gen.get_config_file_name()

        self.cont_ini = os.path.join(self.depl_package_path, 'container.ini')

        self.rootfs = self.ROOTFS

        # Check if architecture is supported
        arch = platform.machine()
        if arch not in self.ARCHITECTURE.keys():
            msg = "Unsupported architecture '{}'".format(arch)
            self.log.error(msg)
            raise UserException(msg)
        self.rootfs.arch = self.ARCHITECTURE[arch]

        self.cont = None
        self.cont_name = None

    def _lxc_run_command(self, cmd):
        if self.cont.attach_wait(lxc.attach_run_command, cmd):
            error = "Failed running '{}' in the container '{}'".format(
                ' '.join(cmd), self.cont_name)
            self.log.error(error)
            raise UserException(error)
        self.log.info(
            "Successfully ran '{}' in the container '{}'".format(
                ' '.join(cmd), self.cont_name))

    def _open_sftp(self, ssh):
        try:
            return ssh.open_sftp_session()
        except Exception as exc:
            error = "Failed to open sftp session to the '{}' container - {}"
            error = error.format(self.cont_name, exc)
            self.log.error(error)
            raise UserException(error)
        self.log.debug("Opened sftp session to the '{}' container".format(
            self.cont_name))

    def _close_ssh(self, ssh):
        try:
            ssh.close()
        except Exception as exc:
            error = "Failed to close sftp session to the '{}' container - {}"
            error = error.format(self.cont_name, exc)
            self.log.error(error)
            raise UserException(error)
        self.log.debug("Closed sftp session to the '{}' container".format(
            self.cont_name))

    def _mkdir_sftp(self, sftp, dir_):
        try:
            sftp.mkdir(dir_)
        except Exception as exc:
            error = (
                "Failed via sftp to create the '{}' directory in the '{}'"
                " container - {}")
            error = error.format(dir_, self.cont_name, exc)
            self.log.error(error)
            raise UserException(error)
        msg = "Created via sftp the '{}' directory in the '{}' container"
        self.log.debug(msg.format(dir_, self.cont_name))

    def _copy_sftp(self, sftp, src, dst):
        try:
            sftp.put(src, dst)
        except Exception as exc:
            error = (
                "Failed via sftp to copy '{}' to '{}' in the '{}' container"
                " - {}")
            error = error.format(src, dst, self.cont_name, exc)
            self.log.error(error)
            raise UserException(error)
        self.log.debug(
            "Copied via sftp '{}' to '{}' in the '{}' container".format(
                src, dst, self.cont_name))

    def check_permissions(self, user):
        # Enumerate LXC bridge
        entry = AttrDict({
            'user': user,
            'type': 'veth',
            'bridge': 'lxcbr0'})
        allows = []
        allows.append(entry.copy())

        # Enumerate management bridges
        for vlan in self.cfg.yield_depl_netw_mgmt_vlan():
            if vlan is not None:
                entry.bridge = 'br-mgmt-%d' % vlan
                allows.append(entry.copy())

        # Enumerate client bridges
        for index, vlan in enumerate(self.cfg.yield_depl_netw_client_vlan()):
            if vlan is not None:
                type_ = self.cfg.get_depl_netw_client_type(index)
                entry.bridge = 'br-%s-%d' % (type_, vlan)
                allows.append(entry.copy())

        # Check bridge permissions
        for line in open(self.LXC_USERNET, 'r'):
            match = re.search(
                r'^\s*(\w+)\s+(\w+)\s+([\w-]+)\s+(\d+)\s*$', line)
            if match is not None:
                allows[:] = [
                    allow for allow in allows
                    if not (
                        allow.user == match.group(1) and
                        allow.type == match.group(2) and
                        allow.bridge == match.group(3))]

        # If bridge permissions are missing
        if allows:
            msg = "Missing entries in '%s':" % self.LXC_USERNET
            for allow in allows:
                msg += ' (%s %s %s <number>)' % \
                    (allow.user, allow.type, allow.bridge)
            self.log.error(msg)
            raise UserException(msg)

        # Success
        self.log.info(
            "Unprivileged/non-root container bridge support found in '%s'" %
            self.LXC_USERNET)

    def create(self, name):
        if name is None:
            for vlan in self.cfg.yield_depl_netw_client_vlan('pxe'):
                break
            name = '{}-pxe{}'.format(self.DEFAULT_CONTAINER_NAME, vlan)

        self.cont = lxc.Container(name)
        self.cont_name = name

        # Check if container already exists
        if self.cont.defined:
            msg = "Container '%s' already exists" % name
            self.log.error(msg)
            raise UserException(msg)

        # Create container
        if not self.cont.create('download', lxc.LXC_CREATE_QUIET, self.rootfs):
            msg = "Failed to create container '%s'" % name
            self.log.error(msg)
            raise UserException(msg)
        self.log.info("Created container '%s'" % name)

        # Start container
        if not self.cont.start():
            msg = "Failed to start container '%s'" % name
            self.log.error(msg)
            raise UserException(msg)
        self.log.info("Started container '%s'" % name)

        # Get nameservers from /etc/resolv.conf outside container
        nameservers = []
        try:
            with open(self.RESOLV_CONF, 'r') as resolv_conf:
                for line in resolv_conf:
                    if re.search(r'^nameserver', line):
                        nameservers.append(line.strip())
        except Exception as exc:
            msg = "Failed to read '{}' - '{}'".format(self.RESOLV_CONF, exc)
            self.log.error(msg)
            raise UserException(msg)

        # Update '/etc/resolv.conf' in container by updating
        # '/etc/resolvconf/resolv.conf.d/base'
        for line in nameservers:
            entry = 'a|%s' % line
            self._lxc_run_command(
                ['ex', '-sc', entry, '-cx', self.RESOLV_CONF_BASE])

        # Sleep to allow /etc/resolv.conf to update
        # Future enhancement is to poll for change
        self._lxc_run_command(["sleep", "5"])

        # Create user
        self._lxc_run_command(
            ['adduser', '--disabled-password', '--gecos', 'GECOS', 'deployer'])

        # Create '/root/.ssh' directory
        self._lxc_run_command(
            ['mkdir', '/root/.ssh'])

        # Create '/root/.ssh/authorized_keys' file
        self._lxc_run_command(
            ['touch', '/root/.ssh/authorized_keys'])

        # Change '/root/.ssh' permissions to 0700
        self._lxc_run_command(
            ['chmod', '700', '/root/.ssh'])

        # Change '/root/.ssh/authorized_keys' permissions to 0600
        self._lxc_run_command(
            ['chmod', '600', '/root/.ssh/authorized_keys'])

        key = RSA.generate(self.RSA_BIT_LENGTH)
        # Create private ssh key
        with open(self.PRIVATE_SSH_KEY_FILE, 'w') as ssh_key:
            ssh_key.write(key.exportKey())
        os.chmod(self.PRIVATE_SSH_KEY_FILE, 0o600)
        # Create public ssh key
        public_key = key.publickey().exportKey(format='OpenSSH')
        with open(self.PUBLIC_SSH_KEY_FILE, 'w') as ssh_key:
            ssh_key.write(public_key)

        # Add public ssh key to container
        self._lxc_run_command([
            'ex',
            '-sc', 'a|%s' % public_key,
            '-cx', '/root/.ssh/authorized_keys'])

        # Update/Upgrade container distro packages
        self._lxc_run_command(["apt-get", "update"])
        self._lxc_run_command(["apt-get", "dist-upgrade", "-y"])

        # Read INI file
        ini = ConfigParser.SafeConfigParser(allow_no_value=True)
        try:
            ini.read(self.cont_ini)
        except ConfigParser.Error as exc:
            msg = exc.message.replace('\n', ' - ')
            self.log.error(msg)
            raise UserException(msg)

        # Install distro container packages
        if ini.has_section(self.Packages.DISTRO.value):
            cmd = ['apt-get', 'install', '-y']
            for pkg in ini.options(self.Packages.DISTRO.value):
                cmd.append(pkg)
            self._lxc_run_command(cmd)

        # Install x86_64 arch specific packages
        if (self.rootfs.arch == 'amd64' and
                ini.has_section(self.Packages.DISTRO_AMD64.value)):
            cmd = ['apt-get', 'install', '-y']
            for pkg in ini.options(self.Packages.DISTRO_AMD64.value):
                cmd.append(pkg)
            self._lxc_run_command(cmd)

        # Install ppc64el arch specific packages
        if (self.rootfs.arch == 'ppc64el' and
                ini.has_section(self.Packages.DISTRO_PPC64EL.value)):
            cmd = ['apt-get', 'install', '-y']
            for pkg in ini.options(self.Packages.DISTRO_PPC64EL.value):
                cmd.append(pkg)
            self._lxc_run_command(cmd)

        # Install pip container packages
        if ini.has_section(self.Packages.PIP.value):
            cmd = ['pip', 'install']
            for pkg in ini.options(self.Packages.PIP.value):
                cmd.append(pkg)
            self._lxc_run_command(cmd)

        # Create project
        self._lxc_run_command(
            ['mkdir', self.cont_package_path])

        # Create virtual environment
        self._lxc_run_command([
            'virtualenv',
            '--no-wheel',
            '--system-site-packages',
            self.cont_venv_path])

        # Open SSH to container
        cont_ipaddr = self.cont.get_ips(
            interface='eth0', family='inet', timeout=5)[0]
        try:
            ssh = SSH_CONNECTION(
                cont_ipaddr,
                username='root',
                key_filename=self.PRIVATE_SSH_KEY_FILE)
        except SSH_Exception as exc:
            error = "SSH to container '{}' at '{}' failed".format(
                self.cont_name, cont_ipaddr)
            self.log.error(error)
            raise UserException(error)

        # Install pip venv container packages
        if ini.has_section(self.Packages.VENV.value):
            cmd = [
                'source', self.cont_venv_path + '/bin/activate',
                '&&', 'pip', 'install']
            for pkg, ver in ini.items(self.Packages.VENV.value):
                cmd.append('{}=={}'.format(pkg, ver))
            cmd.extend(['&&', 'deactivate'])
            status, stdout_, stderr_ = ssh.send_cmd(' '.join(cmd))
            if status:
                error = 'Failed venv pip install'
                self.log.error(error)
                raise UserException(error)

        # Open sftp session to container
        sftp = self._open_sftp(ssh)

        # Copy config file to container
        self._copy_sftp(
            sftp,
            os.path.join(self.depl_package_path, self.config_file),
            os.path.join(self.cont_package_path, self.config_file))

        # Copy scripts/python directory to container
        self._mkdir_sftp(sftp, self.cont_scripts_path)
        self._mkdir_sftp(sftp, self.cont_python_path)
        for dirpath, dirnames, filenames in os.walk(self.depl_python_path):
            for dirname in dirnames:
                self._mkdir_sftp(
                    sftp,
                    os.path.join(
                        self.cont_package_path,
                        os.path.relpath(dirpath, self.depl_package_path),
                        dirname))
            for filename in filenames:
                filepath = os.path.join(dirpath, filename)
                if os.path.islink(filepath):
                    error = "Symbolic links are not supported '{}'".format(
                        filepath)
                    self.log.error(error)
                    raise UserException(error)
                self._copy_sftp(
                    sftp,
                    os.path.join(dirpath, filename),
                    os.path.join(
                        self.cont_package_path,
                        os.path.relpath(dirpath, self.depl_package_path),
                        filename))

        # Create file to indicate whether project is installed in a container
        self._lxc_run_command(['touch', self.cont_id_file])

        # Close ssh session to container
        self._close_ssh(ssh)