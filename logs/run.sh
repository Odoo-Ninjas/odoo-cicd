#!/bin/bash
bin/log.io-server  & 
sleep 3
echo "Starting io file input"
log.io-file-input