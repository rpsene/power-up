#!/bin/bash
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

set -e
source /etc/os-release

if [[ $ID == "ubuntu" ]]; then

    sudo apt-get update
    sudo apt-get -y install python-pip python-dev libffi-dev libssl-dev \
        python-netaddr ipmitool aptitude lxc vim vlan bridge-utils gcc cpp \
        g++ make unzip libncurses5 libncurses5-dev

    if [[ $VERSION_ID == "14.04" ]]; then
        sudo apt-get -y install lxc-dev liblxc1
    elif [[ $VERSION_ID == "16.04" ]]; then
        sudo apt-get -y install python-lxc
    fi

elif [[ $ID == "rhel" ]]; then
    sudo yum -y install python-pip python-devel libffi-devel openssl-devel \
        python-netaddr ipmitool lxc lxc-devel lxc-extra lxc-templates libvirt \
        debootstrap gcc vim vlan bridge-utils cpp flex bison unzip cmake \
        gcc-c++ patch perl-ExtUtils-MakeMaker perl-Thread-Queue ncurses-devel
    sudo systemctl start lxc.service
    sudo systemctl start libvirtd

else
    echo "Unsupported OS"
    exit 1
fi

sudo -H pip install --upgrade pip
sudo -H pip install --upgrade setuptools
sudo -H pip install --upgrade wheel
if [[ $VERSION_ID == "14.04" || $ID == "rhel" ]]; then
    sudo -H pip install lxc-python2
fi
sudo -H pip install virtualenv
virtualenv --no-wheel --system-site-packages deployenv
source deployenv/bin/activate
pip install 'ansible~=2.2.1.0' orderedattrdict pyroute2
deactivate
