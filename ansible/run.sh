#!/bin/bash
# ansible-galaxy install emmetog.jenkins

cd ansible || exit -1
ansible-playbook -i data/hosts cicd.yml -vvvv